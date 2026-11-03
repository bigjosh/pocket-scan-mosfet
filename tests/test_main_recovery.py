"""Exercise the production run loop with host fakes, without hardware imports."""

import ast
import gc
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref


class Snapshot(dict):
    pass


def run_loop(tick, logger, prepare_calibration=None, calibration_error=None):
    # Only the function is compiled; boot-time machine imports and fixed device
    # buffers are intentionally not executed by a host recovery test.
    tree = ast.parse((Path(__file__).parents[1] / "firmware" / "main.py").read_text())
    body = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    state = SimpleNamespace(messages=[], serial=[], closes=0, pins=[], refs={}, app=None,
                            cloud_pauses=0, wifi_pauses=0, soft_resets=0, startup_order=[], validation_calls=0)

    def settings(**kwargs):
        state.startup_order.append("settings")
        assert kwargs == {"migrate": False}
        value = Snapshot(version=2)
        state.refs["settings"] = weakref.ref(value)
        return value

    def calibration():
        state.startup_order.append("calibration")
        if calibration_error is not None:
            raise calibration_error
        value = Snapshot(version=2)
        state.refs["calibration"] = weakref.ref(value)
        return value

    class Instrument:
        identity = {}

        def __init__(self, **kwargs):
            state.startup_order.append("instrument")

        def close(self):
            state.closes += 1

    class Display:
        def __init__(self, **kwargs):
            state.startup_order.append("display")

        def fill(self, color):
            pass

        def text(self, message, *args, **kwargs):
            state.messages.append(message)

    class Pin:
        OUT = 1

        def __init__(self, number, mode, value):
            state.pins.append((number, value))

    class App:
        screen = "calibrating"

        def __init__(self, display, inputs, engine, settings, calibration, **kwargs):
            self.settings = dict(settings)
            self.calibration = calibration
            self.validation_handoff_matched = kwargs.get("validated_calibration") is calibration
            self.calibration_on_disk = kwargs.get("calibration_on_disk", False)
            self.restart = kwargs["restart"]
            self.startup_notice = kwargs.get("startup_notice")
            state.app = self

        def tick(self, now):
            tick(self, state)

    def pause_wifi():
        state.wifi_pauses += 1
        return True

    def validate_calibration(value):
        state.validation_calls += 1

    def pause_cloud():
        state.cloud_pauses += 1
        return True

    wifi = SimpleNamespace(state="off", pause=pause_wifi, resume=lambda now: None,
                           autoconnect=lambda now: None)
    def create_wifi():
        state.startup_order.append("wifi")
        return wifi
    def create_inputs():
        state.startup_order.append("inputs")
        return SimpleNamespace(status={"touch_available": True})
    cloud = SimpleNamespace(state="idle", pause=pause_cloud,
                            prepare_calibration=prepare_calibration or (lambda cal: None))
    class RestartRequested(BaseException):
        pass
    def restart():
        raise RestartRequested()
    def soft_reset():
        assert state.pins[-2:] == [(21, 0), (20, 0)]
        assert state.cloud_pauses == 1 and state.wifi_pauses == 2
        state.soft_resets += 1
    namespace = {
        "Display": Display, "Instrument": Instrument, "Pin": Pin, "App": App,
        "Inputs": create_inputs,
        "WifiManager": create_wifi, "RamCloudManager": lambda *args, **kwargs: cloud,
        "Engine": lambda instrument, **kwargs: instrument,
        "PackedScanBuffer": lambda buffer: object(), "_scan_bytes": None,
        "scan_utc": lambda: None, "_display_buffer": None,
        "RestartRequested": RestartRequested, "request_restart": restart, "soft_reset": soft_reset,
        "_tls_reserved": True, "_upload_transport": SimpleNamespace(state="idle"),
        "_scan_timing": SimpleNamespace(stamp=lambda: None, add=lambda *args: None), "SLEEP": 5,
        "storage": SimpleNamespace(load_settings=settings, load_calibration=calibration),
        "model": SimpleNamespace(validate_calibration=validate_calibration),
        "gc": gc, "time": SimpleNamespace(ticks_ms=lambda: 1, sleep_ms=lambda delay: None),
        "record_error": logger, "print": lambda *args: state.serial.append(args),
        "sys": SimpleNamespace(print_exception=lambda error: state.messages.append(str(error))),
        "BG": 0, "INK": 1, "RED": 2,
    }
    exec(compile(ast.Module(body=[body], type_ignores=[]), "firmware/main.py", "exec"), namespace)
    namespace["run"]()
    return state


class MainRecoveryTests(unittest.TestCase):
    def test_settings_and_packed_calibration_load_before_hardware_construction(self):
        def tick(app, state):
            self.assertEqual(state.startup_order,
                             ["settings", "calibration", "display", "instrument", "inputs", "wifi"])
            raise KeyboardInterrupt()
        state = run_loop(tick, lambda context, error: None)
        self.assertIsNotNone(state.app.calibration)
        self.assertEqual(1, state.validation_calls)
        self.assertTrue(state.app.validation_handoff_matched)
        self.assertEqual(state.pins[-2:], [(21, 0), (20, 0)])

    def test_calibration_load_memory_error_keeps_home_and_settings_reachable(self):
        logged = []
        def tick(app, state):
            self.assertIsNone(app.calibration)
            self.assertFalse(app.calibration_on_disk)
            self.assertIn("Calibration unavailable", app.startup_notice)
            if not getattr(app, "first_tick_complete", False):
                app.first_tick_complete = True
                return
            raise KeyboardInterrupt()
        state = run_loop(tick, lambda context, error: logged.append(context),
                         calibration_error=MemoryError("packed allocation"))
        self.assertEqual(logged, ["calibration load"])
        self.assertTrue(any(row[0] == "Pocket MOSFET ready" for row in state.serial))
        self.assertNotIn("SCANNER STOPPED", state.messages)

    def test_fingerprint_allocation_failure_keeps_home_and_settings_reachable(self):
        logged = []
        def prepare(calibration):
            raise MemoryError("fingerprint allocation")
        def tick(app, state):
            self.assertIn("Scan will retry", app.startup_notice)
            self.assertIsNotNone(app.calibration)
            if not getattr(app, "first_tick_complete", False):
                app.first_tick_complete = True
                return
            raise KeyboardInterrupt()
        state = run_loop(tick, lambda context, error: logged.append(context), prepare)
        self.assertEqual(logged, ["calibration fingerprint"])
        self.assertIsNotNone(state.app)
        self.assertTrue(any(row[0] == "Pocket MOSFET ready" for row in state.serial))
        self.assertNotIn("SCANNER STOPPED", state.messages)

    def test_requested_restart_unwinds_and_closes_everything_before_reset(self):
        state = run_loop(lambda app, state: app.restart(), lambda context, error: None)
        self.assertEqual(state.soft_resets, 1)
        self.assertEqual(state.closes, 1)

    def test_exit_aborts_held_calibration_file_after_forcing_outputs_low(self):
        for error in (KeyboardInterrupt(), MemoryError("original 512 bytes")):
            with self.subTest(error=type(error).__name__):
                aborted = []

                def tick(app, state):
                    class Writer:
                        def abort(self):
                            self_outer.assertEqual(state.pins[-2:], [(21, 0), (20, 0)])
                            self_outer.assertGreaterEqual(state.closes, 1)
                            self_outer.assertIsNone(app._calibration_writer)
                            aborted.append(True)

                    app._calibration_writer = Writer()
                    raise error

                self_outer = self
                state = run_loop(tick, lambda context, original: None)
                self.assertEqual(aborted, [True])
                self.assertIsNone(state.app._calibration_writer)
                self.assertEqual(state.cloud_pauses, 1)
                self.assertEqual(state.wifi_pauses, 2)

    def test_failed_writer_abort_does_not_mask_original_fault_or_skip_radio_cleanup(self):
        def tick(app, state):
            class Writer:
                def abort(self):
                    raise MemoryError("cleanup allocation")

            app._calibration_writer = Writer()
            raise ValueError("original calibration failure")

        state = run_loop(tick, lambda context, error: None)
        self.assertIsNone(state.app._calibration_writer)
        self.assertIn("original calibration failure", state.messages)
        self.assertNotIn("cleanup allocation", state.messages)
        self.assertEqual(state.cloud_pauses, 1)
        self.assertEqual(state.wifi_pauses, 2)

    def test_ready_marker_follows_first_completed_input_and_render_tick(self):
        def tick(app, state):
            if not getattr(app, "first_tick_complete", False):
                self.assertFalse(state.serial)
                app.first_tick_complete = True
                return
            self.assertTrue(any(row[0] == "Pocket MOSFET ready" for row in state.serial))
            raise KeyboardInterrupt()

        state = run_loop(tick, lambda context, error: self.fail("Unexpected logger call"))
        self.assertEqual(sum(row[0] == "Pocket MOSFET ready" for row in state.serial), 1)

    def test_boot_snapshots_can_be_collected_after_app_replaces_calibration(self):
        def tick(app, state):
            self.assertTrue(app.calibration_on_disk)
            app.calibration = {"version": 2, "replacement": True}
            gc.collect()
            self.assertIsNone(state.refs["settings"]())
            self.assertIsNone(state.refs["calibration"]())
            raise KeyboardInterrupt()

        state = run_loop(tick, lambda context, error: self.fail("Unexpected logger call"))
        self.assertEqual(state.closes, 1)
        self.assertEqual(state.pins[-2:], [(21, 0), (20, 0)])

    def test_logger_memory_failure_still_draws_original_fault_and_finishes_cleanup(self):
        class Screen(str):
            def __radd__(self, prefix):
                raise MemoryError("context concatenation")

        screen = Screen("calibrating")
        seen = []

        def tick(app, state):
            app.screen = screen
            raise MemoryError("original 512 bytes")

        def logger(context, error):
            seen.append((context, error))
            raise MemoryError("logger allocation")

        state = run_loop(tick, logger)
        self.assertEqual(len(seen), 1)
        self.assertIs(seen[0][0], screen)
        self.assertEqual(str(seen[0][1]), "original 512 bytes")
        self.assertIn("SCANNER STOPPED", state.messages)
        self.assertIn("original 512 bytes", state.messages)
        self.assertEqual(state.closes, 2)
        self.assertEqual(state.pins[-2:], [(21, 0), (20, 0)])
        self.assertFalse(any(row[0] == "Pocket MOSFET ready" for row in state.serial))


if __name__ == "__main__":
    unittest.main()
