"""Host serial timing protocol, using simulated bytes/clock and no USB access."""

from types import SimpleNamespace
import unittest

from prototype.measure_startup import (
    PICO_SERIAL, WINDOW_BYTES, _read_window, find_port, measure_boot,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Serial:
    def __init__(self, boot, prompt=True):
        self.boot = list(boot)
        self.prompt = prompt
        self.clock = Clock()
        self.pending = []
        self.writes = []

    @property
    def in_waiting(self):
        return len(self.pending[0]) if self.pending else 0

    def read(self, count):
        self.clock.now += 0.05
        if not self.pending:
            return b""
        data = self.pending[0][:count]
        self.pending[0] = self.pending[0][count:]
        if not self.pending[0]:
            self.pending.pop(0)
        return data

    def write(self, data):
        self.writes.append(data)
        if data == b"\x03\x03\x02":
            self.pending = [b"Scanner stopped; outputs disabled\r\n>>> "] if self.prompt else []
        elif data == b"\x04":
            self.pending = list(self.boot)
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.pending = []


class StartupTimingTests(unittest.TestCase):
    def test_split_marker_is_timed_after_reset_and_final_boot_is_not_interrupted(self):
        serial = Serial([b"MPY: soft reboot\n", b"Pocket MOS", b"FET ready {'private': 'not copied'}\n"])
        result = measure_boot(serial, timeout=1, observe=0.2, clock=serial.clock)
        self.assertTrue(result["ok"])
        self.assertEqual(result["ready_ms"], 150.0)
        self.assertEqual(serial.writes, [b"\x03\x03\x02", b"\x04"])
        self.assertNotIn("private", str(result))

    def test_each_measurement_confirms_repl_then_performs_one_normal_soft_reset(self):
        serial = Serial([b"Pocket MOSFET ready\n"])
        for _ in range(3):
            self.assertTrue(measure_boot(serial, 1, 0.1, serial.clock)["ok"])
        self.assertEqual(serial.writes, [b"\x03\x03\x02", b"\x04"] * 3)

    def test_fault_after_ready_invalidates_boot_without_logging_details(self):
        serial = Serial([b"Pocket MOSFET ready\n", b"Traceback private credential\nMemoryError\n>>> "])
        result = measure_boot(serial, 1, 0.2, serial.clock)
        self.assertFalse(result["ok"])
        self.assertTrue(result["fault_marker_seen"])
        self.assertEqual(result["error"], "startup_fault")
        self.assertNotIn("credential", str(result))

    def test_missing_prompt_sends_no_soft_reset_to_unconfirmed_interpreter(self):
        serial = Serial([], prompt=False)
        result = measure_boot(serial, 1, 0.1, serial.clock)
        self.assertEqual(result["error"], "repl_not_confirmed")
        self.assertEqual(serial.writes, [b"\x03\x03\x02"])

    def test_ready_timeout_does_not_claim_application_is_running(self):
        serial = Serial([b"MPY: soft reboot\n"])
        result = measure_boot(serial, 0.2, 0.1, serial.clock)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["ready_ms"])
        self.assertEqual(result["error"], "ready_timeout")

    def test_noise_window_is_bounded(self):
        serial = Serial([])
        serial.pending = [b"x" * 20000]
        window = bytearray()
        while serial.pending:
            _read_window(serial, window)
            self.assertLessEqual(len(window), WINDOW_BYTES)

    def test_selects_exact_known_serial_and_rejects_missing_or_ambiguous_match(self):
        correct = SimpleNamespace(device="COM_TEST", serial_number=PICO_SERIAL.lower())
        wrong = SimpleNamespace(device="COM_OTHER", serial_number="OTHER")
        self.assertEqual(find_port([wrong, correct]), "COM_TEST")
        for ports in ([], [wrong], [correct, correct]):
            with self.assertRaises(ValueError):
                find_port(ports)


if __name__ == "__main__":
    unittest.main()
