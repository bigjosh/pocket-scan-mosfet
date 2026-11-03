"""Exercise the production run loop with host fakes, without hardware imports."""

import ast
import gc
from pathlib import Path
from types import SimpleNamespace
import unittest
import weakref


class Snapshot(dict):
    pass


def run_loop(tick, logger, settings_error=None, baseline_error=None):
    # Only the function is compiled; boot-time machine imports and fixed device
    # buffers are intentionally not executed by a host recovery test.
    tree = ast.parse((Path(__file__).parents[1] / "firmware" / "main.py").read_text())
    body = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    state = SimpleNamespace(messages=[], serial=[], closes=0, pins=[], refs={}, app=None,
                            cloud_pauses=0, wifi_pauses=0, soft_resets=0, startup_order=[], validation_calls=0)

    def settings(**kwargs):
        state.startup_order.append("settings")
        assert kwargs == {"migrate": False}
        if settings_error is not None:
            raise settings_error
        value = Snapshot(version=3)
        state.refs["settings"] = weakref.ref(value)
        return value

    class Instrument:
        identity = {}

        def __init__(self, **kwargs):
            state.startup_order.append("instrument")

        def close(self):
            state.closes += 1

    class Baseline:
        def __init__(self, buffer):
            pass

        def load(self):
            state.startup_order.append("baseline")
            if baseline_error is not None:
                raise baseline_error
            return False

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
        screen = "home"

        def __init__(self, display, inputs, engine, settings, **kwargs):
            self.settings = dict(settings)
            self.restart = kwargs["restart"]
            self.startup_notice = kwargs.get("startup_notice")
            state.app = self

        def tick(self, now):
            tick(self, state)

    def pause_wifi():
        state.wifi_pauses += 1
        return True

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
    cloud = SimpleNamespace(state="idle", pause=pause_cloud)
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
        "storage": SimpleNamespace(load_settings=settings),
        "current_baseline": SimpleNamespace(Baseline=Baseline),
        "_baseline_bytes": None, "create_calibrator": lambda *args: None,
        "model": SimpleNamespace(DEFAULT_SETTINGS={"version": 3}),
        "gc": gc, "time": SimpleNamespace(ticks_ms=lambda: 1, sleep_ms=lambda delay: None),
        "record_error": logger, "print": lambda *args: state.serial.append(args),
        "sys": SimpleNamespace(print_exception=lambda error: state.messages.append(str(error))),
        "BG": 0, "INK": 1, "RED": 2,
    }
    exec(compile(ast.Module(body=[body], type_ignores=[]), "firmware/main.py", "exec"), namespace)
    namespace["run"]()
    return state


class MainRecoveryTests(unittest.TestCase):


    def test_boot_reads_settings_and_baseline_before_hardware_and_releases_settings(self):
        def tick(app, state):
            self.assertEqual(state.startup_order,
                             ["settings", "baseline", "display", "instrument", "inputs", "wifi"])
            gc.collect()
            self.assertIsNone(state.refs["settings"]())
            self.assertFalse(hasattr(app, "calibration"))
            raise KeyboardInterrupt()
        state = run_loop(tick, lambda *args: self.fail("Unexpected logger call"))
        self.assertEqual(state.pins[-2:], [(21, 0), (20, 0)])
        self.assertEqual(state.closes, 1)

    def test_unreadable_settings_uses_defaults_and_preserves_reachable_home(self):
        logged = []
        def tick(app, state):
            self.assertIn("Settings could not be loaded", app.startup_notice)
            self.assertEqual(app.settings["version"], 3)
            if not getattr(app, "first_tick_complete", False):
                app.first_tick_complete = True
                return
            raise KeyboardInterrupt()
        state = run_loop(tick, lambda context, error: logged.append(context),
                         settings_error=OSError("read failed"))
        self.assertEqual(logged, ["settings load"])
        self.assertTrue(any(row[0] == "Pocket MOSFET ready" for row in state.serial))
        self.assertNotIn("SCANNER STOPPED", state.messages)

    def test_bad_baseline_reports_raw_fallback_and_keeps_home_reachable(self):
        logged = []
        def tick(app, state):
            self.assertIn("Current baseline could not be loaded", app.startup_notice)
            self.assertIn("uncorrected", app.startup_notice)
            raise KeyboardInterrupt()
        state = run_loop(tick, lambda context, error: logged.append(context),
                         baseline_error=ValueError("Invalid baseline digest"))
        self.assertEqual(logged, ["current baseline load"])
        self.assertNotIn("SCANNER STOPPED", state.messages)
        self.assertEqual(state.pins[-2:], [(21, 0), (20, 0)])

    def test_requested_restart_unwinds_and_closes_everything_before_reset(self):
        state = run_loop(lambda app, state: app.restart(), lambda context, error: None)
        self.assertEqual(state.soft_resets, 1)
        self.assertEqual(state.closes, 1)


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


    def test_logger_memory_failure_still_draws_original_fault_and_finishes_cleanup(self):
        class Screen(str):
            def __radd__(self, prefix):
                raise MemoryError("context concatenation")

        screen = Screen("scan")
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
