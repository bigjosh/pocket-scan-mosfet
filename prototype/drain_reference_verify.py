"""Verify the installed drain shunt calibration against a 9.98 kOhm load.

Stop normal main.py first. The reference load must be attached as authorized;
set DRAIN_REFERENCE_VERIFY_CONFIRMED=True in the same REPL session. This drives
drain 0.5..3.5 V in 0.5 V steps, gate 0 V, using the installed calibration and
unmodified Engine with 10 batches of 50 readings. It never writes settings,
calibration, archives or logs. Reference current uses actual measured VDS.
"""

if globals().get("DRAIN_REFERENCE_VERIFY_CONFIRMED") is not True:
    raise RuntimeError("Confirm the 9.98 kOhm drain reference fixture before running")

from machine import Pin
try:
    Pin(20, Pin.OUT, value=0)
finally:
    Pin(21, Pin.OUT, value=0)

instrument = None
network = None


def radio_off():
    failed = False
    for interface in (network.STA_IF, network.AP_IF):
        try:
            radio = network.WLAN(interface)
            radio.active(False)
            if radio.active():
                failed = True
        except Exception:
            failed = True
    if failed:
        raise RuntimeError("Both Wi-Fi interfaces must be inactive")


try:
    import time
    import math
    import json
    import hashlib
    import network
    import model
    import storage
    from engine import Engine
    from hardware import Instrument

    radio_off()
    assert model.DRAIN_CURRENT_LIMIT_A == 0.005, "The 5 mA drain trip must remain enabled"

    def digest(path):
        value = hashlib.sha256()
        with open(path, "rb") as stream:
            while True:
                block = stream.read(512)
                if not block:
                    return value.digest()
                value.update(block)

    before = (digest("settings.json"), digest("calibration.json"))
    settings = storage.load_settings()
    assert settings["driver_supply_uv"] == 5000000 and settings["source_voltage_uv"] == 0, "Expected 5 V supply / 0 V source fixture"
    settings.update(drain_start_uv=500000, drain_stop_uv=3500000, drain_step_uv=500000,
                    gate_start_uv=0, gate_stop_uv=0, gate_step_uv=500000,
                    batches=10, samples_per_batch=50)
    calibration = storage.load_json("calibration.json")
    model.validate_scan_calibration(calibration, settings)
    assert abs(calibration["channels"]["drain"]["shunt_ohm"] - model.DRAIN_SHUNT_OHM) < 1e-6, "Installed drain shunt value does not match calibration"

    instrument = Instrument()
    engine = Engine(instrument)
    points, max_error_uA = 0, 0.0
    started = time.ticks_ms()
    engine.start_scan(settings, calibration)
    while engine.state == "scanning":
        now = time.ticks_ms()
        if time.ticks_diff(now, started) >= 120000:
            raise RuntimeError("Drain reference verification timed out")
        for kind, data in engine.tick(now):
            if kind == "point":
                assert data["index"] == points and data["curve"] == 0
                assert data["batches_completed"] == 10
                assert abs(data["requested_vds"] - (0.5 + points * 0.5)) < 1e-5
                assert data["requested_vgs"] == 0.0
                assert math.isfinite(data["vds"]) and math.isfinite(data["id_a"])
                expected_a = data["vds"] / 9980.0
                error_uA = (data["id_a"] - expected_a) * 1e6
                max_error_uA = max(max_error_uA, abs(error_uA))
                print("DRAIN_REFERENCE_POINT", json.dumps({
                    "index": points, "vds": data["vds"], "id_a": data["id_a"],
                    "expected_a": expected_a, "error_uA": error_uA,
                    "drain_voltage_limited": data["drain_voltage_limited"],
                    "gate_voltage_limited": data["gate_voltage_limited"]}))
                points += 1
            elif kind in ("partial", "error", "cancelled"):
                print("DRAIN_REFERENCE_STOPPED", kind)
                raise RuntimeError("Drain reference scan stopped before completion")
        if engine.state == "scanning":
            time.sleep_ms(2)

    assert engine.state == "complete" and engine.shutdown_succeeded, "Reference scan did not complete safely"
    assert points == 7, "Reference scan did not produce seven completed points"
    assert before == (digest("settings.json"), digest("calibration.json")), "Settings or calibration changed"
    assert max_error_uA <= 5.0, "Drain reference error exceeds 5 uA"
    print("DRAIN_REFERENCE_VERIFY", json.dumps({
        "complete": True, "points": points, "reference_ohm": 9980,
        "drain_shunt_ohm": model.DRAIN_SHUNT_OHM, "max_abs_error_uA": max_error_uA,
        "absolute_limit_uA": 5.0, "batches": 10, "samples_per_batch": 50,
        "duration_ms": time.ticks_diff(time.ticks_ms(), started),
        "settings_and_calibration_preserved": True, "no_archive_written": True}))
finally:
    try:
        if instrument is not None:
            instrument.close()
    finally:
        try:
            Pin(20, Pin.OUT, value=0)
        finally:
            try:
                Pin(21, Pin.OUT, value=0)
            finally:
                try:
                    if network is not None:
                        radio_off()
                finally:
                    print("REFERENCE_VERIFICATION_STOPPED")
