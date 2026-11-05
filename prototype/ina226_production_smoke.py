"""Zero-PWM smoke test of the mixed INA226 drain / INA228 gate driver.

Run only with normal main.py stopped and the agreed open-output fixture.
    import ina226_production_smoke
    ina226_production_smoke.run("root-chosen-run-nonce")

No files, network, calibration or positive PWM commands. Verifies triggered
averaging, ready-bit latching, cancellation and the raw ADC range-stop policy.
It does not produce or claim a physical saturation excursion. Continuous-mode
configs and the per-chip diagnostic controls are restored in finally, then outputs close.
"""
import json
import time
import model
from machine import Pin
from hardware import (Instrument, HardwareError, INA_CONFIG, ALERT_MASK,
                      INA228_CONFIG, INA228_ADC_CONFIG, INA228_ALERT_MASK)

TEST_ID = "mixed-ina-production-trigger-v3"
ADDRESSES = (0x44, 0x40)
ROLES = ("drain", "gate")
RESTORE = ((0x44, 6, ALERT_MASK, 0xFC03), (0x44, 0, INA_CONFIG, 0xFFFF),
           (0x40, 0, INA228_CONFIG, 0xFFFF), (0x40, 1, INA228_ADC_CONFIG, 0xFFFF),
           (0x40, 0x0B, INA228_ALERT_MASK, 0xF000))


def emit(nonce, event, **fields):
    fields["test_id"], fields["nonce"], fields["event"] = TEST_ID, nonce, event
    print("INA_PRODUCTION_SMOKE", json.dumps(fields))


def assert_zero(instrument):
    for role in ROLES:
        assert instrument._pwms[role].duty_u16() == 0, "Nonzero PWM in zero-only test"


def run(nonce):
    if (not isinstance(nonce, str) or not 1 <= len(nonce) <= 48 or
            any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in nonce)):
        raise ValueError("Use a short alphanumeric run nonce")
    instrument = None
    passed = False
    emit(nonce, "BEGIN", zero_pwm_only=True)
    try:
        Pin(21, Pin.OUT, value=0)
        Pin(20, Pin.OUT, value=0)
        instrument = Instrument()
        assert ALERT_MASK == 0, "Current threshold comparators must be disabled"
        assert instrument._read16(0x44, 6) & 0xFC03 == 0
        assert instrument._read16(0x40, 0x0B) & 0xF000 == INA228_ALERT_MASK
        assert_zero(instrument)
        emit(nonce, "RANGE_STOP_POLICY", drain_alert_mask=0,
             gate_diagnostic_control=INA228_ALERT_MASK, comparator_flags_trip=False,
             physical_saturation_test=False)

        started = time.ticks_us()
        instrument.begin_measurement(64)
        ready_us, polls = [None, None], 0
        while True:
            complete = instrument.poll_measurement()
            polls += 1
            elapsed = time.ticks_diff(time.ticks_us(), started)
            for index in range(2):
                if ready_us[index] is None and instrument._ready_mask & (1 << index):
                    ready_us[index] = elapsed
            if complete:
                break
            if elapsed > 500000:
                raise RuntimeError("Production count64 conversion timed out")
            time.sleep_ms(2)
        assert instrument._ready_mask == 3
        pair = instrument.read_pair()
        assert not instrument._measurement_pending
        # Triggered registers must remain stable after conversion completes.
        time.sleep_ms(20)
        repeat = instrument.read_pair()
        for role in ROLES:
            assert pair[role] == repeat[role], "Triggered registers changed after completion"
        assert_zero(instrument)
        emit(nonce, "COUNT64_PASS", ready_us=ready_us, polls=polls,
             registers_stable=True, pair=pair)

        started = time.ticks_us()
        instrument.begin_measurement(1024)
        time.sleep_ms(10)
        assert not instrument.poll_measurement(), "Count1024 unexpectedly complete after 10ms"
        instrument.zero()
        assert not instrument._measurement_pending and instrument._ready_mask == 0
        assert_zero(instrument)
        try:
            instrument.poll_measurement()
        except HardwareError:
            pass
        else:
            raise AssertionError("Cancelled conversion was still readable as pending")
        emit(nonce, "CANCEL1024_PASS", elapsed_us=time.ticks_diff(time.ticks_us(), started),
             pending=False, pwm_zero=True)

        # set_duty(0) resumes the real continuous-monitoring path after trigger.
        instrument.set_duty("gate", 0)
        instrument.set_duty("drain", 0)
        assert instrument._read16(0x44, 0) == INA_CONFIG
        assert instrument._read16(0x40, 0) == INA228_CONFIG
        assert instrument._read16(0x40, 1) == INA228_ADC_CONFIG
        assert_zero(instrument)
        instrument.poll_alerts()
        emit(nonce, "CONTINUOUS_RESUME_PASS", drain_alert_mask=0, pwm_zero=True)
        passed = True
    finally:
        errors = []
        if instrument is not None:
            try:
                instrument.zero()
            except Exception as error:
                errors.append("zero:" + type(error).__name__)
            # Attempt every restore even if another sensor or cleanup failed.
            for address, register, value, mask in RESTORE:
                try:
                    instrument._write16(address, register, value)
                    actual = instrument._read16(address, register)
                    assert actual & mask == value
                except Exception as error:
                    errors.append(hex(address) + ":restore" + str(register) + ":" + type(error).__name__)
            try:
                instrument.close()
            except Exception as error:
                errors.append("close:" + type(error).__name__)
        for gpio in (21, 20):
            try:
                Pin(gpio, Pin.OUT, value=0)
            except Exception as error:
                errors.append("pin" + str(gpio) + ":" + type(error).__name__)
        emit(nonce, "CLEANUP", passed=passed and not errors,
             continuous_config=[INA_CONFIG, INA228_ADC_CONFIG],
             configuration_restored=instrument is not None and not errors,
             outputs_low=not errors, cleanup_errors=errors)
        if errors:
            raise RuntimeError("Production smoke cleanup failed")
    emit(nonce, "DONE", passed=True, zero_pwm_only=True)
