"""Bounded open-output plateau diagnostic; never modifies saved calibration."""
if globals().get("_confirm_open_fixture_firmware_test") is not True:
    raise RuntimeError("Confirm open outputs and unbridged shunts before this test")
del _confirm_open_fixture_firmware_test

import time
import json
from hardware import Instrument
import model
import storage

instrument = Instrument()
try:
    settings = storage.load_settings()
    assert settings["driver_supply_uv"] == 5000000 and settings["source_voltage_uv"] == 0
    cap = model.calibration_duty_limit(settings)
    # Around the observed gate knee, then return through the same drives.
    duties = [0.120 + index * 0.002 for index in range(14)]
    duties += list(reversed(duties))
    for duty in duties:
        assert duty <= cap
        for role in ("drain", "gate"):
            instrument.set_duty(role, duty)
        time.sleep_ms(250)
        stats = {role: {"minimum_v": 99.0, "maximum_v": -99.0,
                        "terminal_sum": 0.0, "shunt_sum": 0.0}
                 for role in ("drain", "gate")}
        for index in range(25):
            pair = instrument.read_pair()
            for role, resistance, limit in (
                    ("drain", model.DRAIN_SHUNT_OHM, model.DRAIN_CURRENT_LIMIT_A),
                    ("gate", model.GATE_SHUNT_OHM, model.GATE_CURRENT_LIMIT_A)):
                row = pair[role]
                voltage = row["bus_v"] - row["shunt_v"]
                assert abs(row["shunt_v"] / resistance) <= limit, role + " current trip"
                assert -0.050 <= voltage <= 5.050, role + " voltage envelope"
                value = stats[role]
                value["minimum_v"] = min(value["minimum_v"], voltage)
                value["maximum_v"] = max(value["maximum_v"], voltage)
                value["terminal_sum"] += voltage
                value["shunt_sum"] += row["shunt_v"]
            time.sleep_ms(4)
        for role in ("drain", "gate"):
            value = stats[role]
            value["terminal_v"] = value.pop("terminal_sum") / 25
            value["shunt_v"] = value.pop("shunt_sum") / 25
        print("PLATEAU", json.dumps({"duty": duty, "channels": stats}))
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
