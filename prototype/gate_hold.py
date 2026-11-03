"""Hold the unloaded gate near 2.8 V for an explicitly requested DMM check.

Runs in RAM. SUCCESS INTENTIONALLY LEAVES GATE PWM ACTIVE; drain stays zero.
Use mpremote connect id:3B8922C9B91BBE9F resume for subsequent reads, avoiding
a soft reset. Call stop_gate_hold() in that session when measurement is done.
Requires _confirm_gate_hold = True; assumes the existing 5 V LM358 supply.
"""
from machine import I2C, PWM, Pin
import json
import time

if not globals().pop("_confirm_gate_hold", False):
    raise RuntimeError("Explicit unloaded gate-hold authorization required")

hold_pwms = []
_gate_hold_state = {"active": False}


def stop_gate_hold():
    errors = []
    for pwm in hold_pwms:
        try:
            pwm.duty_u16(0)
            pwm.deinit()
        except Exception as error:
            errors.append(str(error))
    for gpio in (20, 21):
        try:
            Pin(gpio, Pin.OUT, value=0)
        except Exception as error:
            errors.append(str(error))
    _gate_hold_state.update({"active": False, "shutdown_errors": errors})
    print(json.dumps({"event": "hold_stopped", "errors": errors}))


def hold_read16(address, register):
    b = hold_bus.readfrom_mem(address, register, 2)
    return (b[0] << 8) | b[1]


def hold_readings(count=64):
    values = {"drain": [0, 0], "gate": [0, 0]}
    for _ in range(count):
        for role, address in (("drain", 0x44), ("gate", 0x41)):
            shunt = hold_read16(address, 1)
            if shunt & 0x8000:
                shunt -= 65536
            bus_code = hold_read16(address, 2)
            if bus_code * 0.00125 > 3.3 or abs(shunt * 0.0000025) > 0.020:
                raise RuntimeError("Unexpected voltage during gate hold: " + role)
            values[role][0] += bus_code
            values[role][1] += shunt
        time.sleep_ms(4)
    result = {}
    for role, pair in values.items():
        bus_v = pair[0] / count * 0.00125
        shunt_v = pair[1] / count * 0.0000025
        result[role] = {"bus_v": bus_v, "shunt_v": shunt_v,
                        "terminal_v": bus_v - shunt_v}
    return result


try:
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    hold_pwms.append(PWM(Pin(20), freq=100000, duty_u16=0))
    hold_pwms.append(PWM(Pin(21), freq=100000, duty_u16=0))
    time.sleep_ms(200)
    hold_bus = I2C(1, sda=Pin(18), scl=Pin(19), freq=100000)
    addresses = hold_bus.scan()
    for address in (0x44, 0x41):
        if address not in addresses:
            raise RuntimeError("Missing INA226")
        if hold_read16(address, 0xFE) != 0x5449 or hold_read16(address, 0xFF) not in (0x2260, 0x2261):
            raise RuntimeError("INA226 identity mismatch")
        if hold_read16(address, 0) != 0x4127:
            raise RuntimeError("Unexpected INA226 conversion timing")
    initial = hold_readings()
    if any(initial[role]["bus_v"] > 0.1 for role in initial):
        raise RuntimeError("Driver output did not return near zero")

    hold_target_v = 2.8
    hold_duty = 6554
    for attempt in range(5):
        hold_pwms[1].duty_u16(hold_duty)
        time.sleep_ms(500)
        measured = hold_readings()
        error = hold_target_v - measured["gate"]["terminal_v"]
        print(json.dumps({"event": "adjust", "attempt": attempt,
                          "duty_u16": hold_pwms[1].duty_u16(), "measurements": measured}))
        if abs(error) <= 0.012:
            break
        hold_duty = max(0, min(7209, hold_duty + round(error / 28.05 * 65535)))

    time.sleep_ms(500)
    measured = hold_readings(128)
    if not 2.77 <= measured["gate"]["terminal_v"] <= 2.83 or measured["drain"]["bus_v"] > 0.1:
        raise RuntimeError("Unable to establish requested gate-only hold")
    _gate_hold_state = {"active": True, "target_terminal_v": hold_target_v,
                        "pwm_hz": hold_pwms[1].freq(),
                        "gate_duty_u16": hold_pwms[1].duty_u16(),
                        "drain_duty_u16": hold_pwms[0].duty_u16(),
                        "measurements": measured}
    print(json.dumps({"event": "holding", "state": _gate_hold_state}))
except BaseException:
    stop_gate_hold()
    raise
