"""Serial protocol simulation only: no ports are enumerated or opened."""

import json
from types import SimpleNamespace
import unittest

from prototype.run_ram_e2e_friendly import (
    CLEANUP_SOURCE, EXECUTE, MAX_LINE, PICO_SERIAL, RAW_PROMPT, find_port, run,
)


ACK = {"exact_match": True, "point_count": 9, "scan_id": "abcd-123-456",
       "sha256": "a" * 64, "sheet_id": 7, "cloud_state": "complete"}
BEGIN = {"point_count": 9, "settings_version": 3, "ina_average_count": 64,
         "source_voltage_uv": 3300000, "driver_supply_uv": 18800000,
         "drain_start_uv": 100000, "drain_stop_uv": 1100000, "drain_step_uv": 500000,
         "gate_start_uv": -1000000, "gate_stop_uv": 1000000, "gate_step_uv": 1000000,
         "low_boundary_excluded": True,
         "acquisition": {"acquisition_mode": "ina226_triggered_v1", "ina_average_count": 64,
                         "bus_conversion_us": 1100, "shunt_conversion_us": 1100,
                         "blocks_per_point": 1, "point_variance": "unavailable"}}


def marker(name, data):
    return (name + " " + json.dumps(data) + "\r\n").encode()


class FakeSerial:
    def __init__(self, output, cleanup_ok=True, raw_prompt=True):
        self.output = list(output)
        self.pending = []
        self.writes = []
        self.now = 0.0
        self.cleanup_ok = cleanup_ok
        self.raw_prompt = raw_prompt

    def clock(self):
        return self.now

    @property
    def in_waiting(self):
        return len(self.pending[0]) if self.pending else 0

    def read(self, count):
        self.now += 0.05
        if not self.pending:
            return b""
        value = self.pending[0][:count]
        self.pending[0] = self.pending[0][count:]
        if not self.pending[0]:
            self.pending.pop(0)
        return value

    def write(self, data):
        self.writes.append(data)
        if data == b"\x03\x03\x02":
            self.pending = [b"Scanner stopped; outputs disabled\r\n>>> "]
        elif data == b"\x01":
            self.pending = [RAW_PROMPT] if self.raw_prompt else []
        elif data == b"\x04":
            self.pending = [b"MPY: soft reboot\r\n", RAW_PROMPT]
        elif data == b"\x02":
            self.pending = [b"MicroPython fake\r\n>>> "]
        elif data == EXECUTE:
            self.pending = list(self.output)
        elif data.startswith(b"exec("):
            # Echo contains the marker string but is not confirmation.
            self.pending = [b">>> " + data]
            if self.cleanup_ok:
                self.pending.append(b"RAM_E2E_HOST_CLEANUP_OK\r\n>>> ")
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.pending = []


class RamFriendlyRunnerTests(unittest.TestCase):
    def execute(self, output, **options):
        connection = FakeSerial(output, **options)
        events = []
        result = run(connection, lambda event, value: events.append((event, value)),
                     timeout=3, clock=connection.clock)
        return result, connection, events

    def test_clean_raw_preparation_friendly_helper_and_ack_reboot_home_sequence(self):
        output = [marker("RAM_E2E_BEGIN", dict(BEGIN, private="not logged")),
                  marker("RAM_E2E_ACK", ACK), b"MPY: soft re", b"boot\r\n",
                  b"Pocket MOS", b"FET ready {'private': 'not logged'}\r\n"]
        result, connection, events = self.execute(output)
        self.assertTrue(result["ok"])
        self.assertTrue(result["home_left_running"])
        self.assertIsNone(result["cleanup_confirmed"])
        self.assertEqual(connection.writes, [b"\x03\x03\x02", b"\x01", b"\x04", b"\x02", EXECUTE])
        self.assertEqual([name for name, _ in events], ["HOST_READY_FRIENDLY", "RAM_E2E_BEGIN",
                         "RAM_E2E_ACK", "MPY_SOFT_REBOOT", "POCKET_MOSFET_READY"])
        self.assertNotIn("private", str(events))

    def test_only_allowlisted_fields_reach_the_log(self):
        secret = "https://script.google.com/secret-password"
        output = [marker("RAM_E2E_BEGIN", dict(BEGIN, url=secret)),
                  (secret + "\n").encode(),
                  marker("RAM_E2E_POINT", {"index": 0, "vds": 1.25, "ig_a": 1e-7, "flags": 0, "url": secret}),
                  marker("RAM_E2E_STATE", {"screen": "scan", "wifi": secret, "free_heap": 40000, "password": secret}),
                  marker("RAM_E2E_ACK", dict(ACK, url=secret)),
                  b"MPY: soft reboot\nPocket MOSFET ready\n"]
        result, _, events = self.execute(output)
        self.assertTrue(result["ok"])
        self.assertNotIn(secret, str(events) + str(result))
        self.assertIn(("RAM_E2E_POINT", {"index": 0, "vds": 1.25, "ig_a": 1e-7, "flags": 0}), events)
        self.assertIn(("RAM_E2E_STATE", {"screen": "scan", "free_heap": 40000}), events)

    def test_initial_production_ready_after_begin_is_not_the_final_boot(self):
        output = [marker("RAM_E2E_BEGIN", BEGIN), b"Pocket MOSFET ready initial\r\n"]
        output += [marker("RAM_E2E_POINT", {"index": n, "vds": 1.0, "vgs": 0.0,
                                           "id_a": 0.0, "ig_a": 0.0, "flags": 0})
                   for n in range(9)]
        output += [marker("RAM_E2E_ACK", ACK), b"MPY: soft reboot\r\n", b"Pocket MOSFET ready final\r\n"]
        result, connection, events = self.execute(output)
        self.assertTrue(result["ok"])
        self.assertEqual(sum(event == "RAM_E2E_POINT" for event, _ in events), 9)
        self.assertIn(("INITIAL_POCKET_MOSFET_READY", {}), events)
        self.assertEqual(events[-1], ("POCKET_MOSFET_READY", {}))
        self.assertEqual(connection.writes[-1], EXECUTE)

    def test_exact_ack_validation_failure_never_retries_and_runs_cleanup(self):
        for changes in ({"exact_match": False}, {"point_count": 8}, {"point_count": True},
                        {"sha256": "invalid"}, {"scan_id": "https://private"}, {"sheet_id": -1}):
            with self.subTest(changes=changes):
                result, connection, events = self.execute([
                    marker("RAM_E2E_BEGIN", BEGIN), marker("RAM_E2E_ACK", dict(ACK, **changes))])
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"], "invalid_exact_ack")
                self.assertTrue(result["cleanup_confirmed"])
                self.assertEqual(connection.writes.count(EXECUTE), 1)
                self.assertEqual(connection.writes[-2], b"\x03\x03\x02")
                self.assertTrue(connection.writes[-1].startswith(b"exec("))
                self.assertNotIn("private", str(events))

    def test_abort_return_fault_and_timeout_are_terminal_and_cleanup_is_gate_first(self):
        for tail in (b"RAM_E2E_ABORT unknown-private-detail\n", b"RAM_E2E_RETURNED\n",
                     b"Traceback private-password\n", b""):
            with self.subTest(tail=tail):
                result, connection, events = self.execute([marker("RAM_E2E_BEGIN", BEGIN), tail])
                self.assertFalse(result["ok"])
                self.assertFalse(result["home_left_running"])
                self.assertTrue(result["cleanup_confirmed"])
                self.assertEqual(connection.writes.count(EXECUTE), 1)
                self.assertNotIn("private", str(events))
        self.assertIn("for _e2e_pin in (21, 20):", CLEANUP_SOURCE)
        self.assertLess(CLEANUP_SOURCE.index("duty_u16=0"), CLEANUP_SOURCE.index(".deinit()"))
        self.assertIn("machine.Pin.OUT, value=0", CLEANUP_SOURCE)

    def test_ack_without_reboot_or_ready_does_not_claim_success(self):
        for tail in (b"", b"Pocket MOSFET ready\n", b"MPY: soft reboot\n"):
            with self.subTest(tail=tail):
                result, _, _ = self.execute([marker("RAM_E2E_BEGIN", BEGIN), marker("RAM_E2E_ACK", ACK), tail])
                self.assertFalse(result["ok"])
                self.assertTrue(result["cleanup_confirmed"])

    def test_unsolicited_reboot_or_stale_ack_are_rejected(self):
        for output in ([b"MPY: soft reboot\n"], [marker("RAM_E2E_ACK", ACK)]):
            result, _, _ = self.execute(output)
            self.assertFalse(result["ok"])
            self.assertTrue(result["cleanup_confirmed"])

    def test_command_echo_is_not_a_cleanup_confirmation(self):
        result, _, _ = self.execute([b"RAM_E2E_ABORT\n"], cleanup_ok=False)
        self.assertFalse(result["cleanup_confirmed"])

    def test_missing_raw_prompt_never_executes_live_helper(self):
        result, connection, _ = self.execute([], raw_prompt=False)
        self.assertEqual(result["error"], "repl_prompt_timeout")
        self.assertNotIn(EXECUTE, connection.writes)
        self.assertTrue(result["cleanup_confirmed"])

    def test_oversized_noise_line_is_discarded_then_normal_markers_work(self):
        output = [b"x" * (MAX_LINE + 200) + b"\n", marker("RAM_E2E_BEGIN", BEGIN),
                  marker("RAM_E2E_ACK", ACK), b"MPY: soft reboot\nPocket MOSFET ready\n"]
        result, _, events = self.execute(output)
        self.assertTrue(result["ok"])
        self.assertLess(len(str(events)), 1000)

    def test_triggered_plan_records_saved_count_and_rejects_legacy_or_mismatched_mode(self):
        for count in (4, 64, 128, 1024):
            plan = dict(BEGIN, ina_average_count=count,
                        acquisition=dict(BEGIN["acquisition"], ina_average_count=count))
            result, _, events = self.execute([marker("RAM_E2E_BEGIN", plan), marker("RAM_E2E_ACK", ACK),
                                              b"MPY: soft reboot\nPocket MOSFET ready\n"])
            self.assertTrue(result["ok"])
            recorded = next(value for event, value in events if event == "RAM_E2E_BEGIN")
            self.assertEqual(count, recorded["ina_average_count"])
            self.assertEqual("unavailable", recorded["acquisition"]["point_variance"])
        for changes in ({"ina_average_count": 2}, {"settings_version": 2}, {"source_voltage_uv": 0},
                        {"drain_start_uv": 0}, {"acquisition": dict(BEGIN["acquisition"], ina_average_count=4)},
                        {"acquisition": dict(BEGIN["acquisition"], point_variance=0)}):
            result, _, _ = self.execute([marker("RAM_E2E_BEGIN", dict(BEGIN, **changes))])
            self.assertFalse(result["ok"])
            self.assertEqual("invalid_acquisition_plan", result["error"])

    def test_exact_serial_selection_has_no_port_fallback(self):
        correct = SimpleNamespace(device="COM_FAKE", serial_number=PICO_SERIAL.lower())
        other = SimpleNamespace(device="COM4", serial_number="unknown")
        self.assertEqual(find_port([other, correct]), "COM_FAKE")
        for ports in ([], [other], [correct, correct]):
            with self.assertRaises(Exception):
                find_port(ports)


if __name__ == "__main__":
    unittest.main()
