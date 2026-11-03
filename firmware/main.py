"""Pocket scanner entry point. Boot never calibrates or starts a scan."""

from machine import Pin, soft_reset

# Remove gate drive before parking drain, before imports or hardware setup.
Pin(21, Pin.OUT, value=0)
Pin(20, Pin.OUT, value=0)

import tls_memory
# MicroPython's heap does not compact. Keep one contiguous block for TLS
# before display, plot and calibration objects divide up the remaining heap.
_tls_reserved = tls_memory.reserve()
from https_upload import UploadTransport
_upload_transport = UploadTransport(restore_after_transfer=False)
# One scan per boot: four float32 measurements and one flag byte per point.
# Plotting and upload share this storage; there are no separate plot arrays.
_scan_bytes = bytearray(4096 * 17)
# Reserve the unchanged 96-slot text raster pool before the large imports too.
# Display construction and all hardware initialization still occur in run().
_display_buffer = bytearray(48 * 1024)

from scan_timing import ScanTiming, SLEEP
_scan_timing = ScanTiming()

import time
import sys
import gc
# Compile the largest module before the other driver/engine modules occupy
# the heap. All fixed buffers remain reserved above; UI import does no I/O.
gc.collect()
from ui import App, BG, INK, RED
gc.collect()
from hardware import Instrument
from display import Display
from inputs import Inputs
from engine import Engine
import model
import storage
from wifi import WifiManager
from ram_scan import PackedScanBuffer
from ram_payload import PayloadReader
from ram_cloud import RamCloudManager

_upload_transport.source_factory = PayloadReader


class RestartRequested(BaseException):
    """Unwind the application so files and hardware close before soft reset."""


def request_restart():
    raise RestartRequested()


def record_error(context, error):
    """USB-only reporting: boot/acquisition/upload never write a fault file."""
    try:
        print("Scanner error", context, str(error))
    except Exception:
        pass


def scan_utc():
    # A cold Pico has no battery-backed clock. Unknown capture time stays null;
    # an upload synchronizes UTC, and the server separately records receipt time.
    value = time.gmtime()
    if not 2025 <= value[0] <= 2099:
        return None
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % value[:6]


def run():
    instrument = None
    display = None
    app = None
    wifi = None
    cloud = None
    restarting = False
    try:
        notices = []
        # Reserve/load contiguous calibration tables before display, input and
        # radio construction fragments the heap. Boot pins remain forced low.
        try:
            # Normalize old settings in RAM. Only Settings may commit a file.
            settings = storage.load_settings(migrate=False)
        except (ValueError, OSError, TypeError) as error:
            record_error("settings load", error)
            settings = dict(model.DEFAULT_SETTINGS)
            notices.append("Settings could not be loaded; defaults are in use. " + str(error))
        try:
            calibration = storage.load_calibration()
            if calibration is not None:
                model.validate_calibration(calibration)
        except (ValueError, OSError, TypeError, KeyError, MemoryError) as error:
            record_error("calibration load", error)
            calibration = None
            notices.append("Calibration unavailable. " + str(error))
        gc.collect()
        display = Display(raster_pool=_display_buffer)
        instrument = Instrument(timing=_scan_timing)
        inputs = Inputs()
        if not _tls_reserved:
            notices.append("Upload memory unavailable. Restart before scanning.")
        wifi = WifiManager()
        if wifi.state == "error":
            notices.append(wifi.status["message"])
        try:
            # Soft resets can preserve CYW43 state. Explicitly establish an
            # inactive radio before preparing the scanner and its UI buffers.
            wifi.pause()
        except RuntimeError:
            notices.append("Wi-Fi could not be stopped. Retry from Wi-Fi settings before scanning.")
        if not inputs.status["touch_available"]:
            notices.append("Touch unavailable; buttons remain active. Check touch wiring, then restart.")
        scan_buffer = PackedScanBuffer(_scan_bytes)
        cloud = RamCloudManager(scan_buffer, transport=_upload_transport, utc_provider=scan_utc)
        if calibration is not None:
            try:
                cloud.prepare_calibration(calibration)
            except Exception as error:
                # The digest is an optimization, not a prerequisite for Home.
                # Keep Settings reachable after an allocation/hash failure;
                # begin_scan retries when no cached fingerprint is available.
                gc.collect()
                record_error("calibration fingerprint", error)
                notices.append("Calibration fingerprint unavailable. Scan will retry; Settings remains available.")
        gc.collect()
        app = App(display, inputs, Engine(instrument, timing=_scan_timing), settings, calibration,
                  startup_notice=" ".join(notices) if notices else None,
                  error_logger=record_error, wifi=wifi, cloud=cloud,
                  calibration_on_disk=calibration is not None,
                  scan_buffer=scan_buffer, restart=request_restart,
                  validated_calibration=calibration, timing=_scan_timing)
        # App owns normalized settings and the active calibration. Keeping
        # these boot references in this never-returning frame would retain an
        # obsolete calibration after a later successful replacement.
        settings = None
        calibration = None
        # Home is offline. Settings can explicitly use Wi-Fi; a scan first
        # acquires with the radio off, then connects only for its own upload.
        gc.collect()
        # Announce readiness only after input polling and the first UI frame
        # finish. Home leaves the radio inactive until it is needed.
        app.tick(time.ticks_ms())
        print("Pocket MOSFET ready", instrument.identity, inputs.status)
        while True:
            app.tick(time.ticks_ms())
            started = _scan_timing.stamp()
            time.sleep_ms(2)
            _scan_timing.add(SLEEP, started)
    except RestartRequested:
        restarting = True
    except KeyboardInterrupt:
        print("Scanner stopped; outputs disabled")
    except Exception as error:
        # Disable outputs before spending time drawing a fault message.
        if instrument is not None:
            try:
                instrument.close()
            except Exception as cleanup_error:
                sys.print_exception(cleanup_error)
        Pin(21, Pin.OUT, value=0)
        Pin(20, Pin.OUT, value=0)
        gc.collect()
        try:
            # Reuse the screen string: formatting context can itself fail
            # when the original fault is an exhausted/fragmented heap.
            record_error(app.screen if app is not None else "startup", error)
        except Exception:
            # Even an unavailable logger must not prevent the stopped screen
            # or the final hardware cleanup after the original error.
            pass
        if display is not None:
            try:
                display.fill(BG)
                display.text("SCANNER STOPPED", 16, 24, RED, bg=BG, scale=2)
                display.text("Outputs disabled. Check USB log.", 16, 64, INK, bg=BG)
                display.text("Restart to return to Home.", 16, 84, INK, bg=BG)
                display.text(str(error)[:54], 16, 120, INK, bg=BG)
            except Exception as render_error:
                # Keep the original fault visible in USB output if drawing fails.
                sys.print_exception(render_error)
    finally:
        if instrument is not None:
            try:
                instrument.close()
            except Exception as error:
                sys.print_exception(error)
        Pin(21, Pin.OUT, value=0)
        Pin(20, Pin.OUT, value=0)
        if app is not None:
            try:
                writer = getattr(app, "_calibration_writer", None)
                if writer is not None:
                    # Drop ownership even if cleanup fails. Shutdown never
                    # commits the candidate or reloads calibration into RAM.
                    app._calibration_writer = None
                    writer.abort()
            except Exception:
                # Preserve the original fault and continue radio cleanup.
                pass
        if cloud is not None:
            try:
                cloud.pause()
            except Exception:
                print("Cloud connection shutdown failed")
        if wifi is not None:
            try:
                wifi.pause()
            except Exception:
                # Never print third-party errors that might contain keys.
                print("Wi-Fi shutdown failed")
    if restarting:
        soft_reset()


if __name__ == "__main__":
    run()
