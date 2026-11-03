"""Capture an open-output calibration candidate without changing saved files.

Set DRAIN_OPEN_BASELINE_CONFIRMED=True only after removing the reference load
and DUT, leaving both outputs open and neither shunt bridged. The established
Engine calibrates both channels in RAM. The host must merge only the new drain
channel into the saved calibration, retaining its original gate channel.
"""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)

if globals().get("DRAIN_OPEN_BASELINE_CONFIRMED") is not True:
    raise RuntimeError("Confirm both outputs open and neither shunt bridged")

import time
import json
import network
import model
import storage
from hardware import Instrument
from engine import Engine

instrument = None
original_drain_shunt = model.DRAIN_SHUNT_OHM
try:
    settings = dict(storage.load_settings())
    if settings["driver_supply_uv"] != 5000000 or settings["source_voltage_uv"] != 0:
        raise RuntimeError("Open baseline requires declared 5 V supply and 0 V source")
    settings.update(batches=10, samples_per_batch=50,
                    sample_pause_ms=4, settle_ms=200)
    # The fitted value is not available yet. Nominal 1 ohm avoids using the
    # obsolete 7.3-ohm raw-current trip and labels the candidate accurately. This
    # temporary constant is never installed or persisted by this helper.
    model.DRAIN_SHUNT_OHM = 1.0
    network.WLAN(network.STA_IF).active(False)
    network.WLAN(network.AP_IF).active(False)
    instrument = Instrument()
    instrument.zero()
    engine = Engine(instrument)
    engine.start_calibration(settings)
    started = last_report = time.ticks_ms()
    print("DRAIN_OPEN_BASELINE_BEGIN", json.dumps({
        "fixture": "both_outputs_open", "temporary_drain_shunt_ohm": 1.0,
        "batches": 10, "samples_per_batch": 50,
        "saved_files_unchanged": True, "host_keeps_original_gate": True}))
    while engine.state == "calibrating":
        now = time.ticks_ms()
        if time.ticks_diff(now, started) >= 600000:
            engine.cancel()
            raise RuntimeError("Open baseline exceeded ten minutes")
        engine.tick(now)
        if time.ticks_diff(now, last_report) >= 10000:
            print("DRAIN_OPEN_BASELINE_PROGRESS", int(engine.progress * 100),
                  time.ticks_diff(now, started) // 1000)
            last_report = now
        time.sleep_ms(2)
    if engine.state != "complete" or not engine.shutdown_succeeded:
        raise RuntimeError(engine.error or "Open baseline did not complete")
    model.validate_calibration(engine.calibration, settings)
    instrument.zero()
    print("DRAIN_OPEN_BASELINE_CANDIDATE", json.dumps(engine.calibration))
    print("DRAIN_OPEN_BASELINE_COMPLETE", time.ticks_diff(time.ticks_ms(), started))
finally:
    try:
        if instrument is not None:
            try:
                instrument.zero()
            finally:
                instrument.close()
    finally:
        model.DRAIN_SHUNT_OHM = original_drain_shunt
        try:
            Pin(20, Pin.OUT, value=0)
        finally:
            Pin(21, Pin.OUT, value=0)
        print("BOTH_PWM_PINS_LOW")
