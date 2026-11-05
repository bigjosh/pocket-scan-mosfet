"""Native diagnostic uses the production driver, simulated registers, no USB."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from firmware import engine, model
from test_hardware import MachineFake

ROOT = Path(__file__).resolve().parents[1]


class ProductionSmokeTests(unittest.TestCase):
    def run_probe(self, gate_code=2, drain_code=0, timeout=False, read_fault=False,
                  script="ina226_production_smoke.py"):
        fake = MachineFake()
        now, schedule = [0], {}
        fake.set_codes(0x44, bus=2640, shunt=drain_code)
        fake.set_codes(0x40, bus=10, shunt=gate_code)
        original_read = fake.module.I2C.readfrom_mem
        original_write = fake.module.I2C.writeto_mem
        original_duty = fake.module.PWM.duty_u16

        def duty(pwm, value=None):
            return pwm.duty if value is None else original_duty(pwm, value)

        def write(bus, address, register, data):
            original_write(bus, address, register, data)
            if (address, register) in ((0x44, 0), (0x40, 1)):
                config = fake.registers[address][register]
                count = model.INA_AVERAGE_COUNTS[(config >> 9) & 7 if address == 0x44 else config & 7]
                interval = 2200 if address == 0x44 else 2104
                schedule[address] = [now[0] + count * interval + (20000 if address == 0x40 else 0), False, now[0]]

        def read(bus, address, register, size):
            if (address, register) in ((0x44, 6), (0x40, 0x0B)) and address in schedule:
                config = fake.registers[address][0 if address == 0x44 else 1]
                mode = config & 7 if address == 0x44 else config >> 12
                state = schedule[address]
                if (mode == 3 and now[0] >= state[0] and not state[1]
                        and not (timeout and address == 0x40)):
                    fake.set_ready(address)
                    state[1] = True
            return original_read(bus, address, register, size)

        fake.module.PWM.duty_u16 = duty
        fake.module.I2C.writeto_mem = write
        fake.module.I2C.readfrom_mem = read
        if read_fault:
            fake.read_error = (0x40, 4)
        clock = types.SimpleNamespace(ticks_us=lambda: now[0], ticks_ms=lambda: now[0] // 1000,
            ticks_diff=lambda left, right: left - right,
            sleep_ms=lambda duration: now.__setitem__(0, now[0] + duration * 1000))
        hw_spec = importlib.util.spec_from_file_location("firmware.hardware", ROOT / "firmware/hardware.py")
        hardware = importlib.util.module_from_spec(hw_spec)
        with patch.dict("sys.modules", {"machine": fake.module}):
            hw_spec.loader.exec_module(hardware)
        spec = importlib.util.spec_from_file_location("production_smoke_probe", ROOT / "prototype" / script)
        probe = importlib.util.module_from_spec(spec)
        output, failure = io.StringIO(), None
        main_fake = types.SimpleNamespace(Instrument=hardware.Instrument, time=clock, Pin=fake.module.Pin,
            Display=lambda **kwargs: None, Inputs=lambda: None, _display_buffer=None,
            WifiManager=lambda: types.SimpleNamespace(pause=lambda: None))
        with patch.dict("sys.modules", {"machine": fake.module, "hardware": hardware,
                        "engine": engine, "model": model, "time": clock,
                        "main": main_fake}), contextlib.redirect_stdout(output):
            try:
                spec.loader.exec_module(probe)
                if script == "ina226_production_smoke.py":
                    probe.run("host-test-unique")
            except Exception as error:
                failure = error
        if script == "ina226_production_smoke.py":
            records = [json.loads(line.split(" ", 1)[1]) for line in output.getvalue().splitlines()]
            self.assertTrue(records)
            self.assertTrue(all(row["nonce"] == "host-test-unique" for row in records))
        else:
            records = output.getvalue().splitlines()
        self.assertEqual(fake.pins[21], (1, 0))
        self.assertEqual(fake.pins[20], (1, 0))
        self.assertTrue(all(event[2] == 0 for event in fake.events if event[0] == "duty"))
        self.assertEqual(fake.events[-2:], [("pin", 21, 1, 0), ("pin", 20, 1, 0)])
        self.assertEqual(fake.registers[0x44][0], 0x4127)
        self.assertEqual(fake.registers[0x44][6] & 0xFC03, 0)
        self.assertEqual(fake.registers[0x40][0], 0)
        self.assertEqual(fake.registers[0x40][1], 0xBB40)
        self.assertEqual(fake.registers[0x40][0x0B] & 0xF000, 0x8000)
        self.assertFalse(any(event[0] == "write" and event[2] == 7 for event in fake.events))
        return records, failure

    def test_success_uses_ready_latches_with_threshold_comparators_disabled(self):
        records, failure = self.run_probe()
        self.assertIsNone(failure)
        self.assertEqual(records[-1]["event"], "DONE")
        count = next(row for row in records if row["event"] == "COUNT64_PASS")
        self.assertLess(count["ready_us"][0], count["ready_us"][1])
        self.assertTrue(count["registers_stable"])
        status = next(row for row in records if row["event"] == "RANGE_STOP_POLICY")
        self.assertEqual(status["drain_alert_mask"], 0)
        self.assertEqual(status["gate_diagnostic_control"], 0x8000)
        self.assertFalse(status["comparator_flags_trip"])
        self.assertFalse(status["physical_saturation_test"])

    def test_all_unsaturated_codes_remain_measurable(self):
        for code in (0, -2, 32766, -32767):
            with self.subTest(code=code):
                records, failure = self.run_probe(gate_code=code, drain_code=code)
                self.assertIsNone(failure)
                self.assertEqual(records[-1]["event"], "DONE")
        for code in (524286, -524287):
            records, failure = self.run_probe(gate_code=code)
            self.assertIsNone(failure)
            self.assertEqual(records[-1]["event"], "DONE")

    def test_timeout_read_failure_or_saturation_never_claims_pass(self):
        for changes in ({"timeout": True}, {"read_fault": True},
                        {"gate_code": 524287}, {"gate_code": -524288},
                        {"drain_code": 32767}, {"drain_code": -32768}):
            with self.subTest(changes=changes):
                records, failure = self.run_probe(**changes)
                self.assertIsNotNone(failure)
                self.assertFalse(any(row["event"] == "DONE" for row in records))
                self.assertEqual(records[-1]["event"], "CLEANUP")
                self.assertFalse(records[-1]["passed"])
                self.assertTrue(records[-1]["configuration_restored"])
                self.assertTrue(records[-1]["outputs_low"])

    def test_timing_helpers_use_mixed_register_widths_and_restore_continuous_configs(self):
        for script, marker in (("i2c_timing_check.py", "I2C_TIMING_DONE"),
                               ("ina226_trigger_timing_check.py", "INA_TRIGGER_DONE"),
                               ("ina226_zero_mode_check.py", "ZERO_MODE_DONE")):
            with self.subTest(script=script):
                records, failure = self.run_probe(script=script)
                self.assertIsNone(failure)
                self.assertTrue(records[-1].startswith(marker), records)

    def test_trigger_helpers_timeout_and_restore_both_chips_with_outputs_low(self):
        for script in ("ina226_trigger_timing_check.py", "ina226_zero_mode_check.py"):
            with self.subTest(script=script):
                records, failure = self.run_probe(script=script, timeout=True)
                self.assertIsNotNone(failure)
                self.assertFalse(any("DONE" in line for line in records))


if __name__ == "__main__":
    unittest.main()
