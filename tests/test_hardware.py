"""Driver tests with a fake machine module: no serial or hardware access."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

from firmware.engine import Engine, OverrangeError


class MachineFake:
    def __init__(self):
        self.events = []
        self.pwms = {}
        self.pins = {}
        self.read_error = None
        self.write_error = None
        self.bad_config = None
        self.pwm_init_error = None
        self.registers = {}
        self.registers[0x44] = {0xFE: 0x5449, 0xFF: 0x2261,
                                0: 0, 1: 0, 2: 0, 6: 0}
        self.registers[0x40] = {0x3E: 0x5449, 0x3F: 0x2281,
                                0: 0, 1: 0, 4: 0, 5: 0, 0x0B: 1}
        self.bad_register = None
        self.short_read = None
        owner = self

        class Pin:
            OUT = 1

            def __init__(self, number, mode=None, value=None):
                self.number = number
                if mode is not None:
                    owner.events.append(("pin", number, mode, value))
                    owner.pins[number] = (mode, value)

        class PWM:
            def __init__(self, pin, freq=None, duty_u16=None):
                if owner.pwm_init_error == pin.number:
                    raise OSError("PWM init injected failure")
                self.pin = pin.number
                self.frequency = freq
                self.duty = duty_u16
                self.zero_error = False
                self.write_error = False
                self.deinit_error = False
                self.deinitialized = False
                owner.pwms[self.pin] = self
                owner.events.append(("pwm", self.pin, freq, duty_u16))

            def duty_u16(self, value):
                owner.events.append(("duty", self.pin, value))
                if self.write_error or (self.zero_error and value == 0):
                    raise OSError("PWM write injected failure")
                self.duty = value

            def deinit(self):
                owner.events.append(("deinit", self.pin))
                self.deinitialized = True
                if self.deinit_error:
                    raise OSError("PWM deinit injected failure")

        class I2C:
            def __init__(self, number, sda, scl, freq):
                owner.events.append(("i2c", number, sda.number, scl.number, freq))

            def readfrom_mem(self, address, register, size):
                owner.events.append(("read", address, register, size))
                if owner.read_error == (address, register):
                    raise OSError("I2C read injected failure")
                if address not in owner.registers:
                    raise OSError("No device")
                value = owner.registers[address][register]
                if (address, register) == (0x44, 6):
                    owner.registers[address][register] &= ~0x18
                if (address, register) == (0x40, 0x0B) and value & 0x8000:
                    owner.registers[address][register] &= ~0xFE
                if register == 0 and owner.bad_config is not None:
                    value = owner.bad_config
                if owner.bad_register and owner.bad_register[:2] == (address, register):
                    value = owner.bad_register[2]
                expected_size = 3 if address == 0x40 and register in (4, 5) else 2
                if size != expected_size:
                    raise AssertionError("Wrong I2C register width")
                data = value.to_bytes(size, "big")
                return data[:-1] if owner.short_read == (address, register) else data

            def readfrom_mem_into(self, address, register, buffer):
                buffer[:] = self.readfrom_mem(address, register, len(buffer))

            def writeto_mem(self, address, register, data):
                if owner.write_error == (address, register):
                    raise OSError("I2C write injected failure")
                value = (data[0] << 8) | data[1]
                owner.events.append(("write", address, register, value))
                if (address, register) == (0x40, 0x0B):
                    # Diagnostic flags are hardware status, not writable controls.
                    value = (value & 0xF000) | (owner.registers[address][register] & 0x0FFF)
                owner.registers[address][register] = value
                if (address, register) == (0x44, 0):
                    owner.registers[address][6] &= ~0x18
                if (address, register) == (0x40, 1):
                    owner.registers[address][0x0B] &= ~2

        self.module = types.SimpleNamespace(Pin=Pin, PWM=PWM, I2C=I2C)

    def set_codes(self, address, bus=0, shunt=0):
        if address == 0x44:
            self.registers[address].update({1: shunt & 0xFFFF, 2: bus})
        else:
            self.registers[address].update({4: (shunt & 0xFFFFF) << 4, 5: bus << 4})

    def set_ready(self, address):
        register, bit = (6, 8) if address == 0x44 else (0x0B, 2)
        self.registers[address][register] |= bit


class InstrumentTests(unittest.TestCase):
    def setUp(self):
        self.fake = MachineFake()
        spec = importlib.util.spec_from_file_location(
            "firmware.hardware",
            Path(__file__).resolve().parents[1] / "firmware" / "hardware.py")
        self.hw = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"machine": self.fake.module}):
            spec.loader.exec_module(self.hw)

    def assert_outputs_zero(self):
        for pwm in self.fake.pwms.values():
            self.assertEqual(pwm.duty, 0)

    def assert_closed_low(self):
        for pwm in self.fake.pwms.values():
            self.assertTrue(pwm.deinitialized)
        self.assertEqual(self.fake.pins[20], (1, 0))
        self.assertEqual(self.fake.pins[21], (1, 0))

    def drive(self, instrument):
        instrument.set_duty("drain", 0.1)
        instrument.set_duty("gate", 0.08)

    def test_initialization_stops_pins_before_pwm_and_verifies_both_revisions(self):
        instrument = self.hw.Instrument()
        self.assertEqual(self.fake.events[:4], [
            ("pin", 21, 1, 0), ("pin", 20, 1, 0),
            ("pwm", 20, 100000, 0), ("pwm", 21, 100000, 0)])
        self.assertIn(("i2c", 1, 18, 19, 100000), self.fake.events)
        self.assertEqual(instrument.identity["drain"]["address"], 0x44)
        self.assertEqual(instrument.identity["gate"]["die"], 0x2281)
        self.assertEqual(instrument.identity["gate"]["address"], 0x40)
        self.assertEqual(instrument.identity["gate"]["config"], 0xBB40)
        self.assertEqual(instrument.identity["drain"]["config"], 0x4127)
        self.assert_outputs_zero()

    def test_signed_lsb_conversion_and_upstream_terminal_readback(self):
        instrument = self.hw.Instrument()
        self.fake.registers[0x44].update({1: 400, 2: 2240})
        self.fake.set_codes(0x40, bus=1, shunt=-1)
        self.fake.events.clear()
        pair = instrument.read_pair()
        self.assertEqual([(event[1], event[2]) for event in self.fake.events if event[0] == "read"],
                         [(0x44, 6), (0x40, 0x0B), (0x40, 0), (0x44, 1), (0x44, 2), (0x40, 4), (0x40, 5)])
        self.assertAlmostEqual(pair["drain"]["bus_v"], 2.8)
        self.assertAlmostEqual(pair["drain"]["shunt_v"], 0.001)
        self.assertAlmostEqual(pair["drain"]["terminal_v"], 2.799)
        self.assertEqual(pair["gate"]["shunt_code"], -1)
        self.assertAlmostEqual(pair["gate"]["shunt_v"], -0.0000003125)
        self.assertAlmostEqual(pair["gate"]["terminal_v"], 0.000195625)

    def test_calibration_integer_read_reuses_caller_and_i2c_buffers(self):
        instrument = self.hw.Instrument()
        self.fake.registers[0x44].update({1: 400, 2: 2240})
        self.fake.set_codes(0x40, bus=1, shunt=-1)
        result = [0, 0, 0, 0]
        caller_id, i2c_id = id(result), id(instrument._i2c_bytes)
        self.assertIsNone(instrument.read_codes_into(result))
        self.assertEqual(result, [2240, 400, 1, -1])
        self.assertEqual(id(result), caller_id)
        self.assertEqual(id(instrument._i2c_bytes), i2c_id)
        self.drive(instrument)
        self.fake.set_codes(0x40, shunt=0x80000)
        with self.assertRaises(OverrangeError) as error:
            instrument.read_codes_into(result)
        self.assertEqual(error.exception.channel, "gate")
        self.assertEqual(error.exception.sign, -1)
        self.assert_outputs_zero()

    def test_calibration_read_checks_ready_invalid_bus_and_mutability(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        instrument.begin_measurement(4)
        with self.assertRaisesRegex(self.hw.HardwareError, "not ready"):
            instrument.read_codes_into([0, 0, 0, 0])
        self.assert_outputs_zero()
        self.drive(instrument)
        self.fake.registers[0x44][2] = 0x8000
        with self.assertRaisesRegex(self.hw.HardwareError, "bus ADC"):
            instrument.read_codes_into([0, 0, 0, 0])
        self.assert_outputs_zero()
        self.fake.registers[0x44][2] = 0
        self.drive(instrument)
        with self.assertRaises(self.hw.HardwareError):
            instrument.read_codes_into((0, 0, 0, 0))
        self.assert_outputs_zero()

    def test_trigger_counts_clear_stale_flags_and_latch_each_chip_independently(self):
        instrument = self.hw.Instrument()
        for index, count in enumerate(self.hw.model.INA_AVERAGE_COUNTS):
            for address in (0x44, 0x40):
                self.fake.set_ready(address)
            instrument.begin_measurement(count)
            self.assertEqual(self.fake.registers[0x44][0], 0x4123 | index << 9)
            self.assertEqual(self.fake.registers[0x40][1], 0x3B40 | index)
            self.assertFalse(instrument.poll_measurement())
            self.fake.registers[0x44][6] |= self.hw.READY_FLAG
            self.assertFalse(instrument.poll_measurement())
            # Status reads clear CVRF, so completion must be remembered.
            self.assertFalse(self.fake.registers[0x44][6] & self.hw.READY_FLAG)
            self.assertFalse(instrument.poll_measurement())
            self.fake.set_ready(0x40)
            self.assertTrue(instrument.poll_measurement())
            self.assertTrue(instrument.poll_measurement())
            instrument.read_pair()
            self.assertFalse(instrument._measurement_pending)

    def test_trigger_rejects_invalid_counts_pending_restart_and_premature_reads(self):
        instrument = self.hw.Instrument()
        for value in (True, 0, 2, 3, 4.0, "64", None):
            self.drive(instrument)
            with self.assertRaises(ValueError):
                instrument.begin_measurement(value)
            self.assert_outputs_zero()
        for action in (lambda: instrument.begin_measurement(4), instrument.read_pair):
            self.drive(instrument)
            instrument.begin_measurement(4)
            with self.assertRaises(self.hw.HardwareError):
                action()
            self.assert_outputs_zero()
            self.assertFalse(instrument._measurement_pending)

    def test_initialization_restores_controls_without_programming_thresholds(self):
        self.fake.registers[0x44].update({6: 0xFC13, 7: 1578})
        self.fake.registers[0x40].update({0: 0x3FF0, 0x0B: 0xF0FF, 0x0C: 123})
        instrument = self.hw.Instrument()
        self.assertEqual(self.fake.registers[0x44][6], 0)
        self.assertEqual(self.fake.registers[0x44][7], 1578)
        self.assertEqual(self.fake.registers[0x40][0], 0)
        self.assertEqual(self.fake.registers[0x40][0x0B], 0x8001)
        self.assertEqual(self.fake.registers[0x40][0x0C], 123)
        self.assertFalse(any(event[0] == "write" and
                             ((event[1] == 0x44 and event[2] == 7) or
                              (event[1] == 0x40 and 0x0C <= event[2] <= 0x11))
                             for event in self.fake.events))
        self.drive(instrument)
        # Comparator/math flags are not equivalent to raw ADC saturation.
        self.fake.registers[0x44][6] |= 0x10
        self.fake.registers[0x40][0x0B] |= 0x0EFE
        instrument.poll_alerts()
        self.assertNotEqual(self.fake.pwms[20].duty, 0)
        self.assertNotEqual(self.fake.pwms[21].duty, 0)

    def test_monitoring_resumes_before_new_pwm_and_zero_cancels_pending(self):
        instrument = self.hw.Instrument()
        instrument.begin_measurement(64)
        instrument.zero()
        self.assertFalse(instrument._measurement_pending)
        self.fake.events.clear()
        instrument.set_duty("drain", 0.1)
        writes = [event for event in self.fake.events if event[0] in ("write", "duty")]
        self.assertEqual(writes, [("write", 0x44, 0, 0x4127),
                                  ("write", 0x40, 1, 0xBB40), ("duty", 20, 6554)])

    def test_changed_alert_control_and_trigger_bus_fault_stop_outputs(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        self.fake.registers[0x44][6] |= 0x4000
        with self.assertRaises(self.hw.HardwareError):
            instrument.poll_alerts()
        self.assert_outputs_zero()
        self.fake.registers[0x44][6] = self.hw.ALERT_MASK
        self.drive(instrument)
        self.fake.write_error = (0x40, 1)
        with self.assertRaises(self.hw.HardwareError):
            instrument.begin_measurement(64)
        self.assert_outputs_zero()

    def test_zero_bus_is_valid_and_zero_does_not_remux_active_pwms(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        self.fake.events.clear()
        instrument.zero()
        instrument.zero()
        self.assertEqual(self.fake.events, [("duty", 21, 0), ("duty", 20, 0)] * 2)
        self.assertFalse(any(event[0] == "pin" for event in self.fake.events))
        instrument.set_duty("gate", 0.1)
        self.assertEqual(self.fake.pwms[21].duty, 6554)
        self.assertEqual(instrument.read_pair()["gate"]["bus_v"], 0)

    def test_invalid_duties_stop_both(self):
        instrument = self.hw.Instrument()
        for duty in (-0.01, 1.01, float("nan"), float("inf"), -float("inf"), None):
            with self.subTest(duty=duty):
                self.drive(instrument)
                with self.assertRaises(ValueError):
                    instrument.set_duty("gate", duty)
                self.assert_outputs_zero()
        self.drive(instrument)
        with self.assertRaises(ValueError):
            instrument.set_duty("other", 0.1)
        self.assert_outputs_zero()

    def test_missing_sensor_stops_and_deinitializes_both(self):
        del self.fake.registers[0x40]
        with self.assertRaises(self.hw.HardwareError):
            self.hw.Instrument()
        self.assert_outputs_zero()
        self.assert_closed_low()

    def test_wrong_identity_stops_and_deinitializes_both(self):
        self.fake.registers[0x44][0xFF] = 0x2190
        with self.assertRaises(self.hw.HardwareError):
            self.hw.Instrument()
        self.assert_closed_low()

    def test_configuration_readback_failure_stops_both(self):
        self.fake.bad_config = 0
        with self.assertRaises(self.hw.HardwareError):
            self.hw.Instrument()
        self.assert_closed_low()

    def test_configuration_write_failure_stops_both(self):
        self.fake.write_error = (0x40, 1)
        with self.assertRaises(self.hw.HardwareError):
            self.hw.Instrument()
        self.assert_closed_low()

    def test_partial_pwm_initialization_failure_forces_both_pins_low(self):
        self.fake.pwm_init_error = 21
        with self.assertRaises(self.hw.HardwareError):
            self.hw.Instrument()
        self.assert_closed_low()

    def test_i2c_read_failure_stops_both_before_returning_error(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        self.fake.read_error = (0x40, 5)
        self.fake.events.clear()
        with self.assertRaises(self.hw.HardwareError):
            instrument.read_pair()
        self.assertEqual([event for event in self.fake.events if event[0] == "duty"],
                         [("duty", 21, 0), ("duty", 20, 0)])
        self.assert_outputs_zero()

    def test_saturation_is_typed_and_stops_both_before_returning(self):
        self.assertIs(self.hw.OverrangeError, OverrangeError)
        instrument = self.hw.Instrument()
        for role, address, bits, lsb in (("drain", 0x44, 16, 2.5e-6),
                                         ("gate", 0x40, 20, 3.125e-7)):
            sign_bit = 1 << (bits - 1)
            for code, sign in ((sign_bit - 1, 1), (sign_bit, -1)):
                with self.subTest(role=role, code=code):
                    for sensor in (0x44, 0x40):
                        self.fake.set_codes(sensor)
                    self.fake.set_codes(address, shunt=code)
                    self.drive(instrument)
                    self.fake.events.clear()
                    with self.assertRaises(OverrangeError) as caught:
                        instrument.read_pair()
                    self.assertEqual([event for event in self.fake.events if event[0] == "duty"],
                                     [("duty", 21, 0), ("duty", 20, 0)])
                    self.assertEqual(caught.exception.details(), {
                        "channel": role, "kind": "shunt_saturation", "raw_code": code,
                        "sign": sign, "limit_value": code * lsb, "limit_unit": "V",
                        "measured_value": None})
                    self.assert_outputs_zero()

    def test_invalid_bus_reserved_bits_remain_hard_errors(self):
        instrument = self.hw.Instrument()
        for address, register, word in ((0x44, 2, 0x8000), (0x44, 2, 0xFFFF),
                                        (0x40, 5, 0x800000), (0x40, 5, 0xFFFFFF),
                                        (0x40, 5, 1), (0x40, 4, 1)):
            with self.subTest(address=address, register=register, word=word):
                for sensor in (0x44, 0x40):
                    self.fake.set_codes(sensor)
                self.fake.registers[address][register] = word
                self.drive(instrument)
                with self.assertRaises(self.hw.HardwareError):
                    instrument.read_pair()
                self.assert_outputs_zero()

    def test_near_full_scale_shunt_codes_are_valid_both_channels_and_signs(self):
        instrument = self.hw.Instrument()
        for role, address, bits, lsb in (("drain", 0x44, 16, 2.5e-6),
                                         ("gate", 0x40, 20, 3.125e-7)):
            sign_bit = 1 << (bits - 1)
            for code, signed in ((sign_bit - 2, sign_bit - 2),
                                 (sign_bit + 1, 1 - sign_bit)):
                for sensor in (0x44, 0x40):
                    self.fake.set_codes(sensor, bus=2640)
                self.fake.set_codes(address, bus=2640, shunt=code)
                self.drive(instrument)
                pair = instrument.read_pair()
                self.assertEqual(pair[role]["shunt_code"], signed)
                self.assertAlmostEqual(pair[role]["shunt_v"], signed * lsb)
                self.assertNotEqual(self.fake.pwms[20].duty, 0)
                self.assertNotEqual(self.fake.pwms[21].duty, 0)

    def test_engine_keeps_near_rail_point_then_stops_before_endpoint_point(self):
        for role, address, bits, lsb, resistance in (
                ("drain", 0x44, 16, 2.5e-6, self.hw.model.DRAIN_SHUNT_OHM),
                ("gate", 0x40, 20, 3.125e-7, self.hw.model.GATE_SHUNT_OHM)):
            sign_bit = 1 << (bits - 1)
            for near, endpoint, sign in ((sign_bit - 2, sign_bit - 1, 1),
                                         (sign_bit + 1, sign_bit, -1)):
                with self.subTest(role=role, sign=sign):
                    self.setUp()
                    instrument = self.hw.Instrument()
                    for sensor in (0x44, 0x40):
                        self.fake.set_codes(sensor, bus=2640)
                    self.fake.set_codes(address, bus=2640, shunt=near)
                    config = dict(self.hw.model.DEFAULT_SETTINGS,
                                  drain_start_uv=100000, drain_stop_uv=300000, drain_step_uv=100000,
                                  gate_start_uv=0, gate_stop_uv=0, ina_average_count=1)
                    scanner = Engine(instrument)
                    scanner.start_scan(config)
                    rows = []
                    for now in range(1000):
                        if scanner._measurement_waiting:
                            for sensor in (0x44, 0x40):
                                self.fake.set_ready(sensor)
                        for event, value in scanner.tick(now):
                            if event == "point":
                                rows.append(value)
                                self.fake.set_codes(address, bus=2640, shunt=endpoint)
                        if scanner.state != "scanning":
                            break
                    self.assertEqual(scanner.state, "partial", scanner.error)
                    self.assertEqual(len(rows), 1)
                    measured = rows[0]["id_a" if role == "drain" else "ig_a"]
                    signed = near if near < sign_bit else near - (1 << bits)
                    self.assertAlmostEqual(measured, signed * lsb / resistance)
                    self.assertEqual(scanner.fault["index"], 1)
                    self.assertEqual(scanner.fault["kind"], "shunt_saturation")
                    self.assertEqual(scanner.fault["raw_code"], endpoint)
                    self.assertIs(scanner.latest, rows[0])
                    self.assertTrue(scanner.shutdown_succeeded)
                    self.assertEqual([event for event in self.fake.events if event[0] == "duty"][-2:],
                                     [("duty", 21, 0), ("duty", 20, 0)])
                    self.assert_outputs_zero()

    def test_bus_values_outside_requested_operating_envelope_are_not_quality_faults(self):
        instrument = self.hw.Instrument()
        for role, address, codes, lsb in (
                ("drain", 0x44, (28801, 32766, 0x7FFF), 0.00125),
                ("gate", 0x40, (184321, 0x7FFFE, 0x7FFFF), 0.0001953125)):
            for code in codes:
                self.fake.set_codes(address, bus=code)
                self.drive(instrument)
                pair = instrument.read_pair()
                self.assertEqual(pair[role]["bus_code"], code)
                self.assertAlmostEqual(pair[role]["bus_v"], code * lsb)
                self.assertNotEqual(self.fake.pwms[20].duty, 0)
                self.assertNotEqual(self.fake.pwms[21].duty, 0)

    def test_invalid_bus_data_is_not_hidden_by_simultaneous_shunt_saturation(self):
        instrument = self.hw.Instrument()
        self.fake.registers[0x44].update({1: 0x7FFF, 2: 0xFFFF})
        with self.assertRaises(self.hw.HardwareError):
            instrument.read_pair()
        self.assert_outputs_zero()

    def test_saturation_with_failed_shutdown_is_not_a_typed_overrange(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        self.fake.set_codes(0x40, shunt=0x80000)
        self.fake.pwms[21].zero_error = True
        with self.assertRaises(self.hw.HardwareError) as caught:
            instrument.read_pair()
        self.assertNotIsInstance(caught.exception, self.hw.OverrangeError)
        self.assertIn("shutdown also failed", str(caught.exception))
        self.assert_closed_low()

    def test_gate_identity_uses_ina228_registers_and_rejects_wrong_parts(self):
        self.hw.Instrument()
        reads = [(event[1], event[2], event[3]) for event in self.fake.events if event[0] == "read"]
        self.assertIn((0x40, 0x3E, 2), reads)
        self.assertIn((0x40, 0x3F, 2), reads)
        self.assertNotIn((0x40, 0xFE, 2), reads)
        self.assertNotIn((0x40, 0xFF, 2), reads)
        for register, value in ((0x3E, 0), (0x3F, 0x2261), (0x3F, 0x2381)):
            with self.subTest(register=register, value=value):
                self.setUp()
                self.fake.registers[0x40][register] = value
                with self.assertRaisesRegex(self.hw.HardwareError, "identity"):
                    self.hw.Instrument()
                self.assert_closed_low()

    def test_gate_configuration_readback_failures_stop_both(self):
        for register, incorrect in ((0, 0x10), (1, 0xFB68), (0x0B, 0x4001)):
            with self.subTest(register=register):
                self.setUp()
                self.fake.bad_register = (0x40, register, incorrect)
                with self.assertRaises(self.hw.HardwareError):
                    self.hw.Instrument()
                self.assert_closed_low()

    def test_gate_range_change_after_initialization_never_uses_wrong_scale(self):
        for action in ("trigger", "read", "calibration"):
            with self.subTest(action=action):
                self.setUp()
                instrument = self.hw.Instrument()
                self.drive(instrument)
                self.fake.registers[0x40][0] = 0x10
                with self.assertRaisesRegex(self.hw.HardwareError, "range configuration"):
                    if action == "trigger":
                        instrument.begin_measurement(64)
                    elif action == "read":
                        instrument.read_pair()
                    else:
                        instrument.read_codes_into([0, 0, 0, 0])
                self.assert_outputs_zero()

    def test_gate_bad_trim_memory_and_changed_diagnostic_controls_stop_outputs(self):
        for value in (0x8000, 0xC001, 0x0001):
            with self.subTest(value=value):
                self.setUp()
                instrument = self.hw.Instrument()
                self.drive(instrument)
                self.fake.registers[0x40][0x0B] = value
                with self.assertRaises(self.hw.HardwareError):
                    instrument.poll_alerts()
                self.assert_outputs_zero()

    def test_gate_ready_is_latched_when_gate_finishes_before_drain(self):
        instrument = self.hw.Instrument()
        instrument.begin_measurement(64)
        self.fake.set_ready(0x40)
        self.assertFalse(instrument.poll_measurement())
        self.assertFalse(self.fake.registers[0x40][0x0B] & 2)
        self.assertFalse(instrument.poll_measurement())
        self.fake.set_ready(0x44)
        self.assertTrue(instrument.poll_measurement())
        self.assertTrue(instrument.poll_measurement())
        instrument.read_pair()

    def test_short_24_bit_reads_stop_outputs_and_reusable_buffers_do_not_grow(self):
        for register in (4, 5):
            with self.subTest(register=register):
                self.setUp()
                instrument = self.hw.Instrument()
                output = [0, 0, 0, 0]
                buffer_id = id(instrument._i2c_bytes24)
                self.fake.set_codes(0x40, bus=16896, shunt=-320)
                for _ in range(10):
                    instrument.read_codes_into(output)
                    self.assertEqual(output[2:], [16896, -320])
                self.assertEqual(id(instrument._i2c_bytes24), buffer_id)
                self.assertEqual(len(instrument._i2c_bytes24), 3)
                self.assertEqual(len(instrument._i2c_bytes), 2)
                self.drive(instrument)
                self.fake.short_read = (0x40, register)
                with self.assertRaisesRegex(self.hw.HardwareError, "Short INA228"):
                    instrument.read_codes_into(output)
                self.assert_outputs_zero()

    def test_gate_microamp_signal_uses_native_resolution_without_ina226_rounding(self):
        instrument = self.hw.Instrument()
        self.fake.set_codes(0x40, bus=16896, shunt=320)
        pair = instrument.read_pair()["gate"]
        self.assertAlmostEqual(pair["bus_v"], 3.3)
        self.assertAlmostEqual(pair["shunt_v"], 0.0001)
        self.assertAlmostEqual(pair["terminal_v"], 3.2999)
        self.assertAlmostEqual(pair["shunt_v"] / 100, 1e-6)
        self.fake.set_codes(0x40, shunt=1)
        self.assertEqual(instrument.read_pair()["gate"]["shunt_code"], 1)


    def test_close_attempts_every_step_after_one_channel_fails(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        self.fake.pwms[20].zero_error = True
        self.fake.pwms[20].deinit_error = True
        self.fake.events.clear()
        with self.assertRaises(self.hw.HardwareError):
            instrument.close()
        self.assertEqual(self.fake.events, [("duty", 21, 0), ("deinit", 21),
                                           ("duty", 20, 0), ("deinit", 20),
                                           ("pin", 21, 1, 0), ("pin", 20, 1, 0)])
        self.assert_closed_low()
        self.assertEqual(self.fake.pwms[21].duty, 0)
        instrument.close()
        instrument.zero()
        with self.assertRaises(self.hw.HardwareError):
            instrument.set_duty("gate", 0.1)

    def test_pwm_write_failure_closes_if_zero_also_fails(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        self.fake.pwms[20].write_error = True
        self.fake.events.clear()
        with self.assertRaises(self.hw.HardwareError):
            instrument.set_duty("drain", 0.05)
        self.assertEqual([event for event in self.fake.events if event[0] == "duty" and event[2] == 0][:2],
                         [("duty", 21, 0), ("duty", 20, 0)])
        self.assert_closed_low()
        self.assertEqual(self.fake.pwms[21].duty, 0)

    def test_failed_gate_shutdown_still_stops_drain_and_forces_both_pins_low(self):
        instrument = self.hw.Instrument()
        self.drive(instrument)
        self.fake.pwms[21].zero_error = True
        self.fake.pwms[21].deinit_error = True
        self.fake.events.clear()
        with self.assertRaises(self.hw.HardwareError):
            instrument.zero()
        zeroes = [event for event in self.fake.events if event[0] == "duty"]
        self.assertEqual(zeroes, [("duty", 21, 0), ("duty", 20, 0)] * 2)
        self.assertEqual(self.fake.events[-2:], [("pin", 21, 1, 0), ("pin", 20, 1, 0)])
        self.assertEqual(self.fake.pwms[20].duty, 0)
        self.assert_closed_low()


if __name__ == "__main__":
    unittest.main()
