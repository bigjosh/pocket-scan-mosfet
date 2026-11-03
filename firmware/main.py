"""Pocket scanner entry point. Boot never drives or starts a scan."""

from machine import Pin, soft_reset

# Remove gate drive before parking drain, before imports or hardware setup.
Pin(21, Pin.OUT, value=0)
Pin(20, Pin.OUT, value=0)

import tls_memory
# MicroPython's heap does not compact. Keep one contiguous block for TLS
# before display and UI objects divide up the remaining heap.
_tls_reserved = tls_memory.reserve()
from https_upload import UploadTransport
_upload_transport = UploadTransport(restore_after_transfer=False)
# One scan per boot: four float32 measured values per point.
# Plotting and upload share this storage; there are no separate plot arrays.
_scan_bytes = bytearray(4096 * 16)
# Reserve the unchanged 96-slot text raster pool before the large imports too.
# Display construction and all hardware initialization still occur in run().
_display_buffer = bytearray(48 * 1024)

# Both calibration tables are small, fixed binary buffers. Reserve the active
# table and the candidate workspace before UI imports fragment the heap. A
# calibration never borrows or grows the RAM scan records.
import current_baseline
_baseline_bytes = bytearray(current_baseline.BUFFER_BYTES)
_baseline_workspace = bytearray(current_baseline.WORKSPACE_BYTES)

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


def create_calibrator(instrument, settings):
    return current_baseline.Calibrator(instrument, settings,
                                      buffer=_baseline_workspace,
                                      clock_ms=time.ticks_ms)


def run():
    instrument = None
    display = None
    app = None
    wifi = None
    cloud = None
    restarting = False
    try:
        notices = []
        try:
            # Normalize old settings in RAM. Only Settings may commit a file.
            settings = storage.load_settings(migrate=False)
        except (ValueError, OSError, TypeError) as error:
            record_error("settings load", error)
            settings = dict(model.DEFAULT_SETTINGS)
            notices.append("Settings could not be loaded; defaults are in use. " + str(error))
        baseline = current_baseline.Baseline(buffer=_baseline_bytes)
        try:
            baseline.load()
        except (ValueError, OSError, TypeError) as error:
            record_error("current baseline load", error)
            notices.append("Current baseline could not be loaded; readings are uncorrected.")
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
        gc.collect()
        app = App(display, inputs, Engine(instrument, timing=_scan_timing, baseline=baseline), settings,
                  startup_notice=" ".join(notices) if notices else None,
                  error_logger=record_error, wifi=wifi, cloud=cloud,
                  scan_buffer=scan_buffer, restart=request_restart,
                  timing=_scan_timing, calibration_factory=create_calibrator)
        settings = None
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
