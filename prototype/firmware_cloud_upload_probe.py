"""Retry queued scans with stage-only USB diagnostics; outputs stay at zero."""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import main
import tls_memory
assert main._tls_reserved
upload_transport = main._upload_transport
import gc
import json
import time
import storage
from archive import Archive
from cloud import CloudManager
from display import Display
from engine import Engine
from ui import App
from wifi import WifiManager


class NoInputs:
    def poll(self, now):
        return []


class ZeroOutputs:
    # Upload verification needs no sensor bus or PWM peripheral. A logical
    # acquisition is an error; retain the physical zero guard on every exit.
    identity = {"diagnostic": "upload only, outputs held low"}

    def zero(self):
        Pin(20, Pin.OUT, value=0)
        Pin(21, Pin.OUT, value=0)

    close = zero

    def set_duty(self, *unused):
        self.zero()
        raise AssertionError("Upload probe must not drive outputs")

    def read_pair(self):
        raise AssertionError("Upload probe must not acquire measurements")


hardware = ZeroOutputs()
wifi = WifiManager()
archive = Archive()
if globals().get("REPLAY_SENT", False):
    # Retry acknowledged bodies without modifying any on-device archive files.
    # The receiver must return its existing tabs for every identical retry.
    entries = []
    for scan_id in archive._names(".sent"):
        info = storage.load_json(archive._path(scan_id, ".meta"))
        entries.append({"scan_id": scan_id, "point_count": info["point_count"],
                        "sha256": info["sha256"], "path": archive._path(scan_id, ".sent")})
    assert entries, "Expected an acknowledged bench scan"
    if globals().get("REPLAY_ONE", False):
        entries = entries[-1:]

    class ReplayArchive:
        def __init__(self, entries, source_archive):
            self.source_archive = source_archive
            repeats = globals().get("REPLAY_REPEATS", 1 if globals().get("REPLAY_ONE", False) else 2)
            assert repeats in (1, 2), "Expected one or two diagnostic replay cycles"
            self.entries = entries * repeats
            self.index = 0

        def prepare_calibration(self, calibration):
            return self.source_archive.prepare_calibration(calibration)

        def invalidate_calibration(self):
            self.source_archive.invalidate_calibration()

        @property
        def pending_count(self):
            return len(self.entries) - self.index

        @property
        def saved_count(self):
            return len(entries)

        def next_pending(self):
            return self.entries[self.index] if self.pending_count else None

        def mark_sent(self, scan_id):
            assert self.entries[self.index]["scan_id"] == scan_id
            self.index += 1
            print("REPLAY_ACKNOWLEDGED", self.index)

    archive = ReplayArchive(entries, archive)
cloud = CloudManager(archive=archive, transport=upload_transport)
last_stage = None


def traced_step():
    global last_stage
    transport = cloud._transport
    stage = transport.state
    if stage != last_stage:
        print("UPLOAD_STAGE", stage, gc.mem_free())
        last_stage = stage
    try:
        result = transport._step()
        if result is not None:
            error = result.get("error", "")
            if not isinstance(error, str) or len(error) > 40 or any(
                    char not in "abcdefghijklmnopqrstuvwxyz_" for char in error):
                error = "unrecognized"
            entry = cloud._active
            print("UPLOAD_ACK", json.dumps({"ok": result.get("ok") is True,
                "error": error, "id_matches": result.get("scan_id") == entry["scan_id"],
                "hash_matches": result.get("sha256") == entry["sha256"],
                "count_matches": result.get("point_count") == entry["point_count"]}))
        return result
    except Exception as error:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else None
        response = getattr(transport, "response", None)
        print("UPLOAD_FAILURE", json.dumps({"stage": stage, "type": type(error).__name__,
              "code": code, "http_status": getattr(response, "status", None),
              "free_heap": gc.mem_free()}))
        if stage == "connect" and str(error).startswith("Verified HTTPS connection failed: "):
            print("TLS_DIAGNOSTIC", str(error))
        transport.close()
        raise RuntimeError("Upload probe failed")


try:
    hardware.zero()
    wifi.pause()
    if "display" not in globals():
        display = Display(raster_pool=main._display_buffer)
    settings = storage.load_settings()
    calibration = storage.load_json("calibration.json")
    gc.collect()
    app = App(display, NoInputs(), Engine(hardware), settings,
              calibration, wifi=wifi, cloud=cloud, plot_buffers=main._plot_buffers)
    app.render()
    if globals().get("DIAG_SURVIVORS", False):
        import tls_survivors
        assert tls_survivors.capture(tls_memory)
    wifi.resume(time.ticks_ms())
    wifi.autoconnect(time.ticks_ms())
    started = time.ticks_ms()
    while wifi.state == "connecting":
        wifi.tick(time.ticks_ms())
        assert time.ticks_diff(time.ticks_ms(), started) < 25000
        time.sleep_ms(10)
    assert wifi.state == "connected"
    cloud.request_upload()
    started = time.ticks_ms()
    installed = False
    while cloud.status["pending_count"]:
        if globals().get("SHALLOW_RESTORE", False) and upload_transport.state == "restore":
            print("SHALLOW_RESTORE", tls_memory.reserve())
        if globals().get("DIRECT_UPLOAD", False):
            cloud.tick(time.ticks_ms(), online=True, safe=True)
        else:
            app.tick(time.ticks_ms())
        if cloud._transport is not None and not installed:
            cloud._transport.step = traced_step
            installed = True
        state = cloud.status["state"]
        assert state not in ("retry", "error"), cloud.status["message"]
        assert time.ticks_diff(time.ticks_ms(), started) < 180000
        time.sleep_ms(5)
    for _ in range(3):
        app.tick(time.ticks_ms())
    assert tls_memory.available(), "TLS reserve not restored"
    print("CLOUD_UPLOAD_NATIVE", json.dumps({"status": cloud.status,
          "tls_reserve_bytes": tls_memory.reserved_bytes(),
          "free_heap": gc.mem_free(), "outputs_held_zero": True}))
finally:
    hardware.close()
    cloud.pause()
    wifi.pause()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print("RADIO_AND_OUTPUTS_DISABLED")
