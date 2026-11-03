"""Bounded LM358 driver test for the 5 V bench configuration, MOSFET unplugged.

Requires the operator to confirm that the DUT is completely disconnected.
Set _confirm_unloaded_driver_test = True in the MicroPython session before
running this file. It runs in RAM and installs no boot/main script.

GP20 = drain PWM, INA226 0x44; GP21 = gate PWM, INA226 0x41.
One 1k/10uF RC per channel, 75k/10k feedback, gain 8.5, LM358 V+ = VBUS.
Only 0..10% duty is used. Nominal output prediction assumes a 3.3 V GPIO high.
Set _driver_test_profile = "gate_baseline" for two unloaded gate up/down
sweeps with longer settling. Gate shunt is user-confirmed 680 ohms; any
derived current is an uncorrected apparent current, not DUT leakage.
Drain shunt resistance remains unverified, so no drain current is calculated.
Set _gate_shunt_bridged = True when IN+ and IN- are jumpered; suppresses
gate-current conversion because the effective shunt resistance is unknown.
Profile "averaging" collects 500 samples per sensor at each of six fixed
PWM settings and streams raw codes in small batches for host-side statistics.
It reports voltages only; recent in-circuit resistance readings are provisional.
Profile "gate_calibration" collects 500 samples at each gate-only setpoint,
with either an open output or a user-connected known resistor to GND.
Set _gate_fixture_load_ohm to record that resistor; None means open circuit.
For gate_calibration only, _gate_calibration_duties may supply a bounded
0..10 percent sequence to refine the baseline near its nonlinear transition.
"""
from machine import I2C, PWM, Pin, freq
import json
import time

PWM_HZ = 100000
GAIN = 8.5
GPIO_HIGH_V = 3.3
SETTLE_MS = 200
SAMPLE_COUNT = 16
GATE_SHUNT_OHM = 680.0
SENSORS = (("drain", 0x44), ("gate", 0x41))


def read16(bus, address, register):
    value = bus.readfrom_mem(address, register, 2)
    return (value[0] << 8) | value[1]


def voltage_codes(bus, address):
    shunt = read16(bus, address, 1)
    if shunt & 0x8000:
        shunt -= 65536
    bus_code = read16(bus, address, 2)
    bus_v = bus_code * 0.00125
    shunt_v = shunt * 0.0000025
    if bus_v > 4.5 or abs(shunt_v) > 0.020:
        raise RuntimeError("Unexpected voltage at 0x%02X: bus=%s V shunt=%s V" %
                           (address, bus_v, shunt_v))
    return bus_code, shunt


def voltages(bus, address):
    bus_code, shunt_code = voltage_codes(bus, address)
    return bus_code * 0.00125, shunt_code * 0.0000025


def measure(bus, sample_count=SAMPLE_COUNT, stream_raw=False, point_index=None):
    values = {role: {"bus_v": [], "shunt_v": [], "terminal_v": []}
              for role, _ in SENSORS}
    batch = []
    started = time.ticks_ms()
    for sample_index in range(sample_count):
        raw = [time.ticks_diff(time.ticks_ms(), started)]
        for role, address in SENSORS:
            bus_code, shunt_code = voltage_codes(bus, address)
            bus_v, shunt_v = bus_code * 0.00125, shunt_code * 0.0000025
            raw.extend((bus_code, shunt_code))
            values[role]["bus_v"].append(bus_v)
            values[role]["shunt_v"].append(shunt_v)
            values[role]["terminal_v"].append(bus_v - shunt_v)
        if stream_raw:
            batch.append(raw)
            if len(batch) == 25 or sample_index + 1 == sample_count:
                emit({"event": "raw_samples", "point_index": point_index,
                      "first_sample_index": sample_index + 1 - len(batch),
                      "columns": ["elapsed_ms", "drain_bus_code", "drain_shunt_code",
                                  "gate_bus_code", "gate_shunt_code"],
                      "samples": batch})
                batch = []
        # With required config 0x4127 a bus+shunt cycle is nominally 2.2 ms.
        # Wait longer than that so a tight host loop cannot average stale codes.
        time.sleep_ms(4)
    result = {}
    for role, data in values.items():
        result[role] = {
            name: {"mean": sum(samples) / len(samples),
                   "min": min(samples), "max": max(samples)}
            for name, samples in data.items()
        }
    return result


def emit(value):
    # Keep each serial write short and independently parseable.
    print(json.dumps(value))


def run_driver_test(profile="drivers", gate_shunt_bridged=False):
    if profile not in ("drivers", "gate_baseline", "averaging", "gate_calibration"):
        raise ValueError("Unknown test profile")
    settle_ms = 500 if profile != "drivers" else SETTLE_MS
    stream_raw = profile in ("averaging", "gate_calibration")
    sample_count = 500 if stream_raw else (32 if profile == "gate_baseline" else SAMPLE_COUNT)
    gate_fixture_load_ohm = globals().pop("_gate_fixture_load_ohm", None)
    calibration_duties = globals().pop("_gate_calibration_duties", (0, 2, 4, 5, 6, 8, 10, 0))
    if profile == "gate_calibration" and (
            not calibration_duties or len(calibration_duties) > 32 or
            any(not 0 <= duty <= 10 for duty in calibration_duties)):
        raise ValueError("Calibration duty sequence must stay in 0..10 percent")
    if not globals().get("_confirm_unloaded_driver_test", False):
        raise RuntimeError("Confirm DUT is disconnected before enabling the driver test")
    # Consume confirmation so another run requires an explicit confirmation.
    globals()["_confirm_unloaded_driver_test"] = False
    bus = I2C(1, sda=Pin(18), scl=Pin(19), freq=100000)
    addresses = bus.scan()
    if not all(address in addresses for _, address in SENSORS):
        raise RuntimeError("Both sensors required before PWM: found %s" %
                           [hex(address) for address in addresses])
    identity = {}
    for role, address in SENSORS:
        manufacturer, die = read16(bus, address, 0xFE), read16(bus, address, 0xFF)
        config = read16(bus, address, 0)
        if manufacturer != 0x5449 or die not in (0x2260, 0x2261):
            raise RuntimeError("Unexpected sensor identity: %s" % role)
        if config != 0x4127:
            raise RuntimeError("Review conversion/averaging timing before test: %s config=%04X" %
                               (role, config))
        identity[role] = {"address": hex(address), "manufacturer": hex(manufacturer),
                          "die": hex(die), "config": hex(config)}
        voltages(bus, address)
    emit({"event": "preflight", "identity": identity, "system_clock_hz": freq(),
          "pwm_hz_requested": PWM_HZ, "gain_nominal": GAIN,
          "gpio_high_v_assumed": GPIO_HIGH_V, "settle_ms": settle_ms,
          "samples_per_point": sample_count, "dut_disconnected_confirmed": True,
          "profile": profile, "gate_shunt_ohm_user_confirmed": GATE_SHUNT_OHM,
          "gate_shunt_bridged": gate_shunt_bridged,
          "gate_fixture_load_ohm": gate_fixture_load_ohm,
          "gate_calibration_duties": calibration_duties if profile == "gate_calibration" else None})

    pwms = []
    completed = False
    try:
        Pin(20, Pin.OUT, value=0)
        Pin(21, Pin.OUT, value=0)
        pwms.append(PWM(Pin(20), freq=PWM_HZ, duty_u16=0))
        pwms.append(PWM(Pin(21), freq=PWM_HZ, duty_u16=0))
        emit({"event": "pwm_initialized", "drain_freq_reported_hz": pwms[0].freq(),
              "gate_freq_reported_hz": pwms[1].freq()})
        sequence = [(0, 0)]
        sequence += [(duty, 0) for duty in (1, 2, 4, 6, 8, 10)]
        sequence += [(0, 0)]
        sequence += [(0, duty) for duty in (1, 2, 4, 6, 8, 10)]
        sequence += [(0, 0)]
        if profile == "gate_baseline":
            # Stay within the already tested 0..10% range, drain held at zero.
            cycle = (0, 2, 4, 5, 6, 8, 10, 8, 6, 5, 4, 2, 0)
            sequence = [(0, duty) for duty in cycle] * 2
        elif profile == "averaging":
            sequence = [(duty, duty) for duty in (0, 2, 4, 6, 10, 0)]
        elif profile == "gate_calibration":
            sequence = [(0, duty) for duty in calibration_duties]
        for index, pair in enumerate(sequence):
            for pwm, percent in zip(pwms, pair):
                pwm.duty_u16(round(65535 * percent / 100))
            time.sleep_ms(settle_ms)
            readings = measure(bus, sample_count, stream_raw, index)
            if not stream_raw and not gate_shunt_bridged:
                readings["gate"]["apparent_current_a_uncorrected"] = {
                    key: value / GATE_SHUNT_OHM
                    for key, value in readings["gate"]["shunt_v"].items()
                }
            emit({"event": "point", "index": index,
                  "duty_percent_requested": {"drain": pair[0], "gate": pair[1]},
                  "duty_u16_reported": {"drain": pwms[0].duty_u16(),
                                        "gate": pwms[1].duty_u16()},
                  "nominal_output_v": {"drain": GPIO_HIGH_V * GAIN * pair[0] / 100,
                                       "gate": GPIO_HIGH_V * GAIN * pair[1] / 100},
                  "measurements": readings})
        completed = True
    finally:
        shutdown_errors = []
        for pwm in pwms:
            try:
                pwm.duty_u16(0)
            except Exception as error:
                shutdown_errors.append("zero duty: " + str(error))
        time.sleep_ms(SETTLE_MS)
        disabled = True
        for pwm in pwms:
            try:
                pwm.deinit()
            except Exception as error:
                disabled = False
                shutdown_errors.append("disable PWM: " + str(error))
        low_pins = []
        for gpio in (20, 21):
            try:
                Pin(gpio, Pin.OUT, value=0)
                low_pins.append(gpio)
            except Exception as error:
                shutdown_errors.append("force GP%d low: %s" % (gpio, error))
        emit({"event": "shutdown", "sequence_completed": completed,
              "pwm_disabled": disabled, "gp20_low": 20 in low_pins,
              "gp21_low": 21 in low_pins, "errors": shutdown_errors})
        if shutdown_errors:
            raise RuntimeError("Driver shutdown needs checking: %s" % shutdown_errors)


run_driver_test(globals().pop("_driver_test_profile", "drivers"),
                globals().pop("_gate_shunt_bridged", False))
