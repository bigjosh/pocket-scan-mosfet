"""Save a real two-point 2N7000 scan, then upload if cloud is configured.

Uses the already-connected fixture and saved calibration. Scan ranges exist
only in RAM; the accepted scan remains in the normal persistent archive.
Never calibrates and never prints network credentials or the upload URL.
"""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import tls_memory
assert tls_memory.reserve(), "TLS startup reserve unavailable"
from https_upload import UploadTransport
upload_transport = UploadTransport()
import gc
import hashlib
import json
import time
import struct
import storage
from archive import Archive
from cloud import CloudManager
from display import Display
from engine import Engine
from hardware import Instrument
from ui import App
from wifi import WifiManager


class NoInputs:
    def poll(self, now):
        return []


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            data = stream.read(512)
            if not data:
                break
            result.update(data)
    return result.digest()


def utc():
    value = time.gmtime()
    if 2025 <= value[0] <= 2099:
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % value[:6]
    return None


hardware = Instrument()
wifi = WifiManager()
cloud = CloudManager(archive=Archive(utc_provider=utc), transport=upload_transport)
try:
    hardware.zero()
    wifi.pause()
    settings = storage.load_settings()
    assert settings["driver_supply_uv"] == 5000000 and settings["source_voltage_uv"] == 0
    before = [digest(name) for name in ("settings.json", "calibration.json")]
    settings.update(drain_start_uv=500000, drain_stop_uv=1000000, drain_step_uv=500000,
                    gate_start_uv=1500000, gate_stop_uv=1500000, gate_step_uv=500000)
    engine = Engine(hardware)
    display = Display()
    calibration = storage.load_json("calibration.json")
    gc.collect()
    app = App(display, NoInputs(), engine, settings,
              calibration, wifi=wifi, cloud=cloud)
    app.render()
    wifi.resume(time.ticks_ms())
    wifi.autoconnect(time.ticks_ms())
    started = time.ticks_ms()
    while wifi.state == "connecting":
        # Start queued transfers only after this scan is saved, so this run
        # exercises consecutive uploads without an incidental pre-scan cancel.
        wifi.tick(time.ticks_ms())
        assert time.ticks_diff(time.ticks_ms(), started) < 25000
        time.sleep_ms(10)
    # Physical input normally reaches action() through tick(), which sets this
    # timestamp. Direct harness actions must supply the same current clock.
    app._now_ms = time.ticks_ms()
    app.action("right")
    point_count = 0
    while engine.state == "scanning":
        app.tick(time.ticks_ms())
        assert time.ticks_diff(time.ticks_ms(), started) < 90000
        if len(app.curves) > point_count:
            point_count = len(app.curves)
            row = engine.latest
            print("CLOUD_SCAN_POINT", json.dumps({key: row[key] for key in (
                "index", "vds", "vgs", "id_a", "ig_a", "batches_completed")}))
        time.sleep_ms(2)
    assert engine.state == "complete", engine.error or engine.state
    assert engine.shutdown_succeeded and point_count == 2
    assert app._archive_ready, "Completed scan was not staged"
    prior_pending = cloud.status["pending_count"]
    app.action("right")
    assert app.screen == "home"
    assert cloud.status["pending_count"] == prior_pending + 1
    print("CLOUD_SCAN_SAVED", json.dumps({"scan_id": cloud.archive._accepted_id,
          "pending_count": cloud.status["pending_count"]}))
    with open(cloud.archive._path(cloud.archive._accepted_id, ".pending"), "rb") as stream:
        stream.seek(-8, 2)
        saved_points, duration_ms = struct.unpack("<II", stream.read(8))
    assert saved_points == 2 and 0 < duration_ms < 90000
    print("SCAN_DURATION_MS", duration_ms)
    if cloud.status["configured"] and not globals().get("SAVE_ONLY", False):
        cloud.request_upload()
        started = time.ticks_ms()
        previous = None
        while cloud.status["pending_count"]:
            if upload_transport.state == "restore":
                tls_memory.reserve()  # Same outer-loop cleanup as main.py.
            app.tick(time.ticks_ms())
            state = cloud.status["state"]
            if state != previous:
                print("CLOUD_STATE", state)
                previous = state
            assert state not in ("retry", "error"), cloud.status["message"]
            assert time.ticks_diff(time.ticks_ms(), started) < 135000
            time.sleep_ms(5)
        # Give deferred TLS memory maintenance a clean stack after the ack.
        for _ in range(3):
            app.tick(time.ticks_ms())
        assert tls_memory.available(), "TLS memory was not restored"
    assert before == [digest(name) for name in ("settings.json", "calibration.json")]
    time.sleep_ms(250)
    idle = hardware.read_pair()
    assert all(abs(idle[role]["terminal_v"]) < 0.05 for role in ("drain", "gate"))
    print("CLOUD_SCAN_NATIVE", json.dumps({"points": point_count,
          "saved_only_after_ok": True, "status": cloud.status,
          "settings_and_calibration_preserved": True,
          "outputs_parked": True, "tls_reserve_bytes": tls_memory.reserved_bytes(),
          "free_heap": gc.mem_free()}))
finally:
    hardware.close()
    cloud.pause()
    wifi.pause()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print("RADIO_AND_OUTPUTS_DISABLED")
