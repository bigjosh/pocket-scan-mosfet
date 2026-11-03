"""Read the revised open fixture with both PWM pins held low; no file writes."""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import time
import json
from hardware import Instrument

instrument = Instrument()
report = {"identity": instrument.identity, "pwm_duty": 0, "samples": 40}
try:
    instrument.zero()
    time.sleep_ms(250)
    stats = {role: {"terminal_v": [0.0, 1e6, -1e6],
                    "shunt_v": [0.0, 1e6, -1e6]} for role in ("drain", "gate")}
    for unused in range(report["samples"]):
        pair = instrument.read_pair()
        for role in stats:
            for field, accumulator in stats[role].items():
                value = pair[role][field]
                accumulator[0] += value
                accumulator[1] = min(accumulator[1], value)
                accumulator[2] = max(accumulator[2], value)
        time.sleep_ms(5)
    report["readback"] = {role: {field: {"mean": value[0] / report["samples"],
                                              "min": value[1], "max": value[2]}
                                    for field, value in fields.items()}
                            for role, fields in stats.items()}
finally:
    instrument.close()
    report["pins_low"] = Pin(20).value() == 0 and Pin(21).value() == 0
print("SOURCE33_ZERO", json.dumps(report))
