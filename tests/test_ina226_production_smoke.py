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
    def run_probe(self, gate_code=2, drain_code=0, timeout=False, read_fault=False):
        fake = MachineFake()
        now, schedule = [0], {}
        fake.registers[0x44].update({1: drain_code & 65535, 2: 2640})
        fake.registers[0x41].update({1: gate_code & 65535, 2: 10})
        original_read = fake.module.I2C.readfrom_mem
        original_write = fake.module.I2C.writeto_mem
        original_duty = fake.module.PWM.duty_u16

        def duty(pwm, value=None):
            return pwm.duty if value is None else original_duty(pwm, value)

        def write(bus, address, register, data):
            original_write(bus, address, register, data)
            if register == 0:
                config = fake.registers[address][0]
                count = model.INA_AVERAGE_COUNTS[(config >> 9) & 7]
                schedule[address] = [now[0] + count * 2200 + (7000 if address == 0x41 else 0), False, now[0]]

        def read(bus, address, register, size):
            if register == 6 and address in schedule:
                config = fake.registers[address][0]
                state = schedule[address]
                if (config & 7 == 3 and now[0] >= state[0] and not state[1]
                        and not (timeout and address == 0x41)):
                    fake.registers[address][6] |= 8
                    state[1] = True
            return original_read(bus, address, register, size)

        fake.module.PWM.duty_u16 = duty
        fake.module.I2C.writeto_mem = write
        fake.module.I2C.readfrom_mem = read
        if read_fault:
            fake.read_error = (0x41, 1)
        clock = types.SimpleNamespace(ticks_us=lambda: now[0], ticks_ms=lambda: now[0] // 1000,
            ticks_diff=lambda left, right: left - right,
            sleep_ms=lambda duration: now.__setitem__(0, now[0] + duration * 1000))
        hw_spec = importlib.util.spec_from_file_location("firmware.hardware", ROOT / "firmware/hardware.py")
        hardware = importlib.util.module_from_spec(hw_spec)
        with patch.dict("sys.modules", {"machine": fake.module}):
            hw_spec.loader.exec_module(hardware)
        spec = importlib.util.spec_from_file_location("production_smoke_probe", ROOT / "prototype/ina226_production_smoke.py")
        probe = importlib.util.module_from_spec(spec)
        output, failure = io.StringIO(), None
        with patch.dict("sys.modules", {"machine": fake.module, "hardware": hardware,
                        "engine": engine, "model": model, "time": clock}), contextlib.redirect_stdout(output):
            spec.loader.exec_module(probe)
            try:
                probe.run("host-test-unique")
            except Exception as error:
                failure = error
        records = [json.loads(line.split(" ", 1)[1]) for line in output.getvalue().splitlines()]
        self.assertTrue(records)
        self.assertTrue(all(row["nonce"] == "host-test-unique" for row in records))
        self.assertEqual(fake.pins[21], (1, 0))
        self.assertEqual(fake.pins[20], (1, 0))
        self.assertTrue(all(event[2] == 0 for event in fake.events if event[0] == "duty"))
        self.assertEqual(fake.events[-2:], [("pin", 21, 1, 0), ("pin", 20, 1, 0)])
        for address in (0x44, 0x41):
            self.assertEqual(fake.registers[address][0], 0x4127)
            self.assertEqual(fake.registers[address][6] & 0xFC03, 0)
        self.assertFalse(any(event[0] == "write" and event[2] == 7 for event in fake.events))
        return records, failure

    def test_success_uses_ready_latches_with_threshold_comparators_disabled(self):
        records, failure = self.run_probe()
        self.assertIsNone(failure)
        self.assertEqual(records[-1]["event"], "DONE")
        count = next(row for row in records if row["event"] == "COUNT64_PASS")
        self.assertLess(count["ready_us"][0], count["ready_us"][1])
        self.assertTrue(count["registers_stable"])
        status = next(row for row in records if row["event"] == "THRESHOLD_COMPARATORS_DISABLED")
        self.assertEqual(status["alert_mask"], 0)
        self.assertFalse(status["physical_saturation_test"])

    def test_all_unsaturated_codes_remain_measurable(self):
        for code in (0, -2, 32766, -32767):
            with self.subTest(code=code):
                records, failure = self.run_probe(gate_code=code, drain_code=code)
                self.assertIsNone(failure)
                self.assertEqual(records[-1]["event"], "DONE")

    def test_timeout_read_failure_or_saturation_never_claims_pass(self):
        for changes in ({"timeout": True}, {"read_fault": True},
                        {"gate_code": 32767}, {"gate_code": -32768},
                        {"drain_code": 32767}, {"drain_code": -32768}):
            with self.subTest(changes=changes):
                records, failure = self.run_probe(**changes)
                self.assertIsNotNone(failure)
                self.assertFalse(any(row["event"] == "DONE" for row in records))
                self.assertEqual(records[-1]["event"], "CLEANUP")
                self.assertFalse(records[-1]["passed"])
                self.assertTrue(records[-1]["configuration_restored"])
                self.assertTrue(records[-1]["outputs_low"])


if __name__ == "__main__":
    unittest.main()
