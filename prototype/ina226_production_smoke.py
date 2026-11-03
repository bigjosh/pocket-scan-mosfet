"""Zero-PWM native smoke test of the production INA226 acquisition driver.

Run only with normal main.py stopped and the agreed open-output fixture.
    import ina226_production_smoke
    ina226_production_smoke.run("root-chosen-run-nonce")

No files, network, calibration or positive PWM commands. The alert test changes
only the gate comparator threshold, explicitly labeled synthetic; it does not
produce or claim a real 100-uA gate-current excursion. All thresholds, masks
and continuous-monitor configs are restored in finally, then outputs close.
"""
import json
import time
import model
from machine import Pin
from hardware import Instrument, HardwareError, INA_CONFIG, ALERT_MASK
from engine import OverrangeError

TEST_ID = "ina226-production-trigger-v1"
ADDRESSES = (0x44, 0x41)
ROLES = ("drain", "gate")


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
    originals = [int(model.DRAIN_CURRENT_LIMIT_A * model.DRAIN_SHUNT_OHM / 0.0000025),
                 int(model.GATE_CURRENT_LIMIT_A * model.GATE_SHUNT_OHM / 0.0000025)]
    passed = False
    emit(nonce, "BEGIN", zero_pwm_only=True)
    try:
        Pin(21, Pin.OUT, value=0)
        Pin(20, Pin.OUT, value=0)
        instrument = Instrument()
        for index, address in enumerate(ADDRESSES):
            originals[index] = instrument._read16(address, 7)
        assert_zero(instrument)

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
        for address in ADDRESSES:
            assert instrument._read16(address, 0) == INA_CONFIG
        assert_zero(instrument)
        caught = None
        for signed_threshold in (0, -1):
            code = signed_threshold & 65535
            emit(nonce, "SYNTHETIC_THRESHOLD_TEST", channel="gate",
                 injected_signed_code=signed_threshold, injected_shunt_v=signed_threshold * 0.0000025,
                 real_current_excursion=False)
            instrument._write16(0x41, 7, code)
            assert instrument._read16(0x41, 7) == code
            started = time.ticks_ms()
            while time.ticks_diff(time.ticks_ms(), started) < 40:
                try:
                    instrument.poll_alerts()
                except OverrangeError as error:
                    assert error.channel == "gate" and error.kind == "current_limit_alert"
                    assert error.sign == 1 and error.raw_code is None and error.measured_value is None
                    caught = error
                    break
                time.sleep_ms(2)
            if caught is not None:
                assert_zero(instrument)
                emit(nonce, "SYNTHETIC_ALERT_PASS", injected_signed_code=signed_threshold,
                     kind=caught.kind, raw_code=None, measured_value=None,
                     production_limit_a=caught.limit_value, pwm_zero=True,
                     real_current_excursion=False)
                break
        if caught is None:
            raise RuntimeError("Injected zero/-1 gate thresholds produced no alert")
        passed = True
    finally:
        errors = []
        if instrument is not None:
            try:
                instrument.zero()
            except Exception as error:
                errors.append("zero:" + type(error).__name__)
            # Attempt every restore even if another sensor or cleanup failed.
            for index, address in enumerate(ADDRESSES):
                threshold = originals[index]
                for register, value in ((7, threshold), (6, ALERT_MASK), (0, INA_CONFIG)):
                    try:
                        instrument._write16(address, register, value)
                        actual = instrument._read16(address, register)
                        assert actual == value if register != 6 else actual & 0xFC03 == value
                    except Exception as error:
                        errors.append(ROLES[index] + ":restore" + str(register) + ":" + type(error).__name__)
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
             continuous_config=INA_CONFIG, thresholds_restored=instrument is not None and not errors,
             outputs_low=not errors, cleanup_errors=errors)
        if errors:
            raise RuntimeError("Production smoke cleanup failed")
    emit(nonce, "DONE", passed=True, zero_pwm_only=True)
