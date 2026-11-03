"""Staged scan acceptance, sanitized cloud settings, and upload interlocks."""

import traceback
import unittest
import tempfile
from pathlib import Path

from firmware import model
from firmware.engine import Engine, OverrangeError
from firmware.ui import App
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore, fixture_calibration, finish_scan_preparation
from test_wifi_ui import FakeWifi


EXAMPLE_URL = "https://script.google.com/macros/s/EXAMPLE_PRIVATE_DEPLOYMENT/exec"


class FakeCloud:
    def __init__(self):
        self.url, self.enabled = EXAMPLE_URL, False
        self.state, self.message = "disabled", "Automatic upload is off"
        self.pending, self.saved = 0, 0
        self.current = None
        self.calls, self.accepted, self.fail = [], [], {}
        self.manual = False
        self.step_check = None

    @property
    def status(self):
        return {"state": self.state, "message": self.message,
                "pending_count": self.pending, "saved_count": self.saved,
                "configured": bool(self.url), "enabled": self.enabled}

    def check(self, name):
        self.calls.append(name)
        if name in self.fail:
            raise self.fail[name]

    def config_for_edit(self):
        self.check("config_for_edit")
        return {"url": self.url, "enabled": self.enabled}

    def save_config(self, url, enabled=True):
        self.check("save_config")
        from firmware.cloud import validate_url
        self.url = validate_url(url) if url or enabled else ""
        self.enabled = enabled
        return True

    def pause(self):
        self.check("pause")
        self.state = "queued" if self.pending else "idle"
        return True

    def begin_scan(self, settings, calibration, now_ms):
        self.check("begin_scan")
        self.current = {"points": [], "complete": False}
        return "test-scan"

    def prepare_calibration(self, calibration):
        self.check("prepare_calibration")
        return "0" * 64

    def invalidate_calibration(self):
        self.check("invalidate_calibration")

    def record_point(self, row):
        self.check("record_point")
        self.current["points"].append(dict(row))

    def finish_scan(self, now_ms, fault=None):
        self.check("finish_scan")
        self.current["complete"] = True
        self.current["fault"] = fault

    def accept_scan(self):
        self.check("accept_scan")
        if not self.current["complete"]:
            raise RuntimeError("Not complete")
        self.accepted.append(self.current)
        self.current = None
        self.pending += 1
        self.saved += 1
        return "test-scan"

    def discard_scan(self):
        self.check("discard_scan")
        self.current = None
        return True

    def request_upload(self):
        self.check("request_upload")
        self.manual = True
        return True

    def tick(self, now_ms, online=False, safe=False):
        self.check("tick")
        if not online or not safe or not self.pending or not (self.enabled or self.manual):
            return
        if self.state != "uploading":
            self.state, self.message = "uploading", "Uploading saved scan"
            return  # The real transport also starts without network work.
        if self.step_check:
            self.step_check()
        self.check("transport_step")
        self.pending -= 1
        self.state, self.message = "idle", "No scans waiting to upload"


class CloudUITests(unittest.TestCase):
    def setUp(self):
        self.display, self.inputs = FakeDisplay(), FakeInputs()
        self.instrument = HostInstrument()
        self.engine = Engine(self.instrument)
        self.wifi, self.cloud, self.store, self.logs = FakeWifi(), FakeCloud(), MemoryStore(), []
        settings = dict(model.DEFAULT_SETTINGS)
        settings.update(drain_start_uv=0, drain_stop_uv=1000000, drain_step_uv=1000000,
                        gate_start_uv=1000000, gate_stop_uv=1000000, gate_step_uv=1000000,
                        ina_average_count=4)
        def log(context, error):
            detail = "".join(traceback.format_exception(type(error), error, error.__traceback__)) if isinstance(error, Exception) else str(error)
            self.logs.append((context, detail))
        self.app = App(self.display, self.inputs, self.engine, settings,
                       fixture_calibration(), self.store, wifi=self.wifi,
                       cloud=self.cloud, error_logger=log)
        self.now = 0
        self.app.render()

    def tick(self, events=()):
        self.now += 20
        self.inputs.events = list(events)
        self.app.tick(self.now)

    def press(self, key):
        starting = self.app.screen == "home" and key == "right"
        self.tick(((key,),))
        if starting:
            finish_scan_preparation(self)

    def touch(self, action):
        for x, y, width, height, target in self.app.hits:
            if target == action:
                self.tick((("touch", x + width // 2, y + height // 2),))
                return
        self.fail("Missing touch target %r on %s" % (action, self.app.screen))

    def settings(self):
        self.press("left")
        self.touch("advanced")
        self.touch("cloud")

    def finish(self):
        for _ in range(2000):
            self.tick()
            if self.engine.state not in ("scanning", "calibrating"):
                return
        self.fail("Acquisition did not finish")

    def test_constructor_has_no_archive_network_or_secret_read(self):
        self.assertEqual(self.cloud.calls, ["pause", "prepare_calibration"])
        self.assertNotIn(EXAMPLE_URL, repr(self.display.labels))
        self.assertEqual(self.instrument.commands, [])

    def test_tls_memory_maintenance_precedes_input_and_render_only_when_safe(self):
        order = []
        self.cloud.maintain_memory = lambda now: order.append("memory") or True
        poll, render = self.inputs.poll, self.app.render
        self.inputs.poll = lambda now: order.append("input") or poll(now)
        self.app.render = lambda: order.append("render") or render()
        self.app.dirty = True
        self.tick()
        self.assertEqual(order[:3], ["memory", "input", "render"])
        order.clear()
        self.app._outputs_safe = False
        self.app.dirty = True
        self.tick()
        self.assertNotIn("memory", order)

    def test_navigation_closes_upload_before_rendering_and_preserves_queue(self):
        for screen, action, target in (("home", "left", "settings"),
                                        ("cloud", "left", "advanced"),
                                        ("cloud", "cloud_url", "cloud_url")):
            with self.subTest(screen=screen, action=action):
                self.setUp()
                self.app.draft = dict(self.app.settings)
                self.app.screen = screen
                self.cloud.pending = 1
                self.cloud.state = "uploading"
                original = self.app.render
                def render():
                    self.assertIn("pause", self.cloud.calls)
                    original()
                self.app.render = render
                self.press(action)
                self.assertEqual(self.app.screen, target)
                self.assertEqual(self.cloud.pending, 1)
                self.assertNotIn("transport_step", self.cloud.calls)

    def test_completed_scan_is_staged_until_ok_and_contains_only_averaged_points(self):
        self.press("right")
        self.finish()
        self.assertEqual(self.app.screen, "complete")
        self.assertTrue(self.cloud.current["complete"])
        self.assertEqual(len(self.cloud.current["points"]), 2)
        self.assertEqual(self.cloud.pending, 0)
        self.assertEqual(self.cloud.calls.count("record_point"), 2)
        self.assertTrue(all(row["acquisition_blocks_completed"] == 1 for row in self.cloud.current["points"]))
        self.assertTrue(all(row["point_variance"] == "unavailable" for row in self.cloud.current["points"]))
        self.assertTrue(any("OK saves / CANCEL discards" in label for label in self.display.labels))
        self.press("right")
        self.assertEqual(self.app.screen, "home")
        self.assertEqual(self.cloud.pending, 1)
        self.assertEqual(len(self.cloud.accepted), 1)
        self.assertIsNone(self.cloud.current)

    def test_complete_cancel_discards_only_staged_scan(self):
        self.cloud.pending, self.cloud.saved = 3, 3
        self.press("right")
        self.finish()
        self.press("left")
        self.assertEqual(self.app.screen, "home")
        self.assertEqual(self.cloud.pending, 3)
        self.assertEqual(self.cloud.accepted, [])
        self.assertIsNone(self.cloud.current)

    def test_overrange_keeps_only_finished_points_until_explicit_ok_or_cancel(self):
        for count, accept in ((0, False), (0, True), (1, False), (1, True)):
            with self.subTest(count=count, accept=accept):
                self.setUp()
                self.cloud.enabled = True
                self.press("right")
                while len(self.cloud.current["points"]) < count:
                    self.tick()
                self.instrument.error = OverrangeError("gate", "shunt_saturation", 32767, 1, 0.0819175, "V")
                self.finish()
                self.assertEqual("partial", self.app.screen)
                self.assertEqual(count, len(self.app.curves))
                self.assertEqual(count, len(self.cloud.current["points"]))
                self.assertEqual(count, self.cloud.current["fault"]["index"])
                self.assertEqual(0, self.cloud.pending)
                self.assertTrue(self.app._outputs_safe)
                self.assertEqual(self.instrument.duties, {"drain": 0, "gate": 0})
                self.assertIn("SCAN STOPPED", self.display.labels)
                self.assertIn("OVER RANGE", self.display.labels)
                for unused in range(3):
                    self.tick()
                self.assertNotIn("transport_step", self.cloud.calls)
                self.assertNotIn("accept_scan", self.cloud.calls)
                self.press("right" if accept else "left")
                self.assertEqual("home", self.app.screen)
                self.assertEqual(int(accept), self.cloud.pending)
                self.assertEqual(int(accept), len(self.cloud.accepted))

    def test_overrange_failed_shutdown_is_hard_error_without_save_path(self):
        self.press("right")
        self.instrument.error = OverrangeError("drain", "shunt_saturation", 32767, 1, 0.0819175, "V")
        def failed_zero():
            raise OSError("output zero failed")
        self.instrument.zero = failed_zero
        self.finish()
        self.assertEqual("notice", self.app.screen)
        self.assertFalse(self.app._outputs_safe)
        self.assertFalse(self.app._archive_ready)
        self.assertIsNone(self.cloud.current)
        self.assertNotIn("finish_scan", self.cloud.calls)
        self.assertNotIn("accept_scan", self.cloud.calls)

    def test_partial_real_binary_archive_survives_accept_and_reload(self):
        import json
        from firmware.archive import Archive, UploadSource
        from firmware.cloud import CloudManager
        from test_archive import settings as historical_settings, row, fault, CALIBRATION
        with tempfile.TemporaryDirectory() as folder:
            scans = str(Path(folder) / "scans")
            manager = CloudManager(Archive(scans, "aabb", free_bytes=lambda: 1000000),
                                   path=str(Path(folder) / "cloud.json"))
            self.app.cloud = manager
            # Stage a historical result directly; current Engine points have
            # no between-block variance and cannot populate old binary rows.
            snapshot = historical_settings(2)
            manager.begin_scan(snapshot, CALIBRATION, 0)
            manager.record_point(row())
            diagnostic = fault(1)
            diagnostic.update(kind="bus_saturation", limit_value=40.95875)
            manager.finish_scan(100, diagnostic)
            self.app._archive_active = self.app._archive_ready = True
            self.app._scan_fault = diagnostic
            self.app.goto("partial")
            self.assertEqual("partial", self.app.screen)
            self.assertEqual(0, manager.status["pending_count"])
            self.press("right")
            source = UploadSource(Archive(scans, "aabb").next_pending()["path"])
            try:
                blocks = []
                while True:
                    block = source.read()
                    if not block:
                        break
                    blocks.append(block)
            finally:
                source.close()
            scan = json.loads(b"".join(blocks))
            self.assertEqual("partial", scan["outcome"])
            self.assertEqual(1, scan["point_count"])
            self.assertEqual(1, len(scan["points"]))
            self.assertEqual("bus_saturation", scan["fault"]["kind"])
            self.assertEqual(snapshot, scan["settings"])
            self.assertNotIn("acquisition_mode", scan["calibration"]["metadata"])
            self.assertGreater(scan["points"][0][8], 0)
            self.assertEqual([], self.instrument.commands)

    def test_cancel_and_fault_discard_without_accepting_partial_scan(self):
        for fault in (False, True):
            with self.subTest(fault=fault):
                self.setUp()
                self.press("right")
                if fault:
                    self.instrument.error = OSError("sensor fault")
                    self.finish()
                else:
                    self.press("left")
                self.assertIsNone(self.cloud.current)
                self.assertEqual(self.cloud.pending, 0)
                self.assertNotIn("accept_scan", self.cloud.calls)
                self.assertEqual(self.instrument.duties, {"drain": 0, "gate": 0})

    def test_calibration_never_opens_or_writes_scan_archive(self):
        self.press("left")
        self.touch("advanced")
        self.touch("calibrate")
        self.press("right")
        self.finish()
        self.assertIn("pause", self.cloud.calls)
        self.assertFalse(any(name in self.cloud.calls for name in ("begin_scan", "record_point", "finish_scan", "accept_scan")))

    def test_archive_begins_before_drive_and_cloud_pause_precedes_wifi_pause(self):
        order = []
        original_cloud, original_wifi = self.cloud.pause, self.wifi.pause
        def cloud_pause():
            order.append("cloud")
            return original_cloud()
        def wifi_pause():
            order.append("wifi")
            return original_wifi()
        self.cloud.pause, self.wifi.pause = cloud_pause, wifi_pause
        original_drive = self.instrument.set_duty
        def drive(role, value):
            self.assertEqual(order[:2], ["cloud", "wifi"])
            self.assertIn("begin_scan", self.cloud.calls)
            self.assertEqual(self.wifi.state, "paused")
            original_drive(role, value)
        self.instrument.set_duty = drive
        self.press("right")
        self.assertGreater(len(self.instrument.commands), 0)

    def test_pause_or_archive_open_failure_prevents_drive(self):
        for operation in ("pause", "begin_scan"):
            with self.subTest(operation=operation):
                self.setUp()
                self.cloud.fail[operation] = OSError("private " + EXAMPLE_URL)
                self.press("right")
                self.assertEqual(self.instrument.commands, [])
                self.assertEqual(self.engine.state, "idle")
                self.assertEqual(self.app.screen, "notice")
                self.assertNotIn(EXAMPLE_URL, repr(self.logs) + self.app.notice)
                if operation == "pause":
                    self.assertNotIn(("pause",), self.wifi.calls)
                else:
                    # Archive opening precedes radio shutdown; a storage
                    # failure does not interrupt or restart Wi-Fi at all.
                    self.assertNotIn(("pause",), self.wifi.calls)
                    self.assertNotIn(("resume",), self.wifi.calls)

    def test_engine_preflight_failure_discards_prepared_archive(self):
        self.app.calibration = None
        self.press("right")
        self.assertEqual(self.instrument.commands, [])
        self.assertIsNone(self.cloud.current)
        self.assertIn("discard_scan", self.cloud.calls)
        self.assertIn(("resume",), self.wifi.calls)

    def test_point_write_failure_stops_before_logging_and_never_finishes(self):
        self.cloud.fail["record_point"] = OSError("private " + EXAMPLE_URL)
        original_log = self.app.error_logger
        def log_after_stop(context, error):
            self.assertEqual(self.instrument.duties, {"drain": 0, "gate": 0})
            original_log(context, error)
        self.app.error_logger = log_after_stop
        self.press("right")
        self.finish()
        self.tick()  # Consume cancellation event without replacing the notice.
        self.assertEqual(self.app.screen, "notice")
        self.assertIn("Scan storage failed", self.app.notice)
        self.assertNotIn("finish_scan", self.cloud.calls)
        self.assertIsNone(self.cloud.current)
        self.assertNotIn(EXAMPLE_URL, repr(self.logs))
        self.assertEqual(self.cloud.pending, 0)

    def test_final_point_write_failure_cannot_be_overridden_by_complete_event(self):
        original = self.cloud.record_point
        def write(row):
            if row["index"] == 1:
                raise OSError("write failed")
            original(row)
        self.cloud.record_point = write
        self.press("right")
        self.finish()
        self.assertEqual(self.app.screen, "notice")
        self.assertEqual(self.engine.state, "complete")
        self.assertEqual(self.cloud.calls.count("record_point"), 1)
        self.assertNotIn("finish_scan", self.cloud.calls)
        self.assertEqual(self.instrument.duties, {"drain": 0, "gate": 0})

    def test_finish_failure_discards_and_does_not_offer_save(self):
        self.cloud.fail["finish_scan"] = OSError("flash full")
        self.press("right")
        self.finish()
        self.assertEqual(self.app.screen, "notice")
        self.assertFalse(self.app._archive_ready)
        self.assertIsNone(self.cloud.current)
        self.assertNotIn("accept_scan", self.cloud.calls)

    def test_finalization_precedes_radio_resume_for_complete_and_partial(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                self.setUp()
                original = self.cloud.finish_scan

                def finish(now_ms, fault=None):
                    self.assertEqual("paused", self.wifi.state)
                    self.assertTrue(self.engine.shutdown_succeeded)
                    return original(now_ms, fault)

                self.cloud.finish_scan = finish
                self.wifi.resume_check = lambda: self.assertTrue(self.cloud.current["complete"])
                self.press("right")
                if partial:
                    self.instrument.error = OverrangeError("drain", "shunt_saturation",
                                                          32767, 1, 0.0819175, "V")
                self.finish()
                self.assertEqual("partial" if partial else "complete", self.app.screen)
                self.assertTrue(self.app._archive_ready)
                self.assertIn(("resume",), self.wifi.calls)

    def test_failed_finalization_discards_before_radio_resume(self):
        self.cloud.fail["finish_scan"] = MemoryError("synthetic allocation failure")
        self.wifi.resume_check = lambda: self.assertIsNone(self.cloud.current)
        self.press("right")
        self.finish()
        self.assertEqual("notice", self.app.screen)
        self.assertIn(("resume",), self.wifi.calls)
        self.assertTrue(self.app._outputs_safe)

    def test_finalization_diagnostic_exposes_only_fixed_local_categories(self):
        from types import SimpleNamespace
        for detail, suffix in ((("source", "memory"), "[source/memory]"),
                               ((EXAMPLE_URL, "memory"), ""),
                               (("source", EXAMPLE_URL), ""),
                               (("source", "memory", EXAMPLE_URL), "")):
            with self.subTest(detail=detail):
                self.setUp()
                self.cloud.archive = SimpleNamespace(last_finalize_error=detail)
                self.cloud.fail["finish_scan"] = RuntimeError(EXAMPLE_URL)
                self.press("right")
                self.finish()
                self.assertEqual("notice", self.app.screen)
                log = self.logs[-1][1]
                self.assertIn("Completed scan could not be finalized.", log)
                self.assertNotIn(EXAMPLE_URL, log)
                self.assertEqual(bool(suffix), "[source/memory]" in log)
                self.assertNotIn(EXAMPLE_URL, repr(self.display.labels))

    def test_accept_failure_keeps_completed_draft_for_retry(self):
        self.press("right")
        self.finish()
        self.cloud.fail["accept_scan"] = OSError("rename failed")
        self.press("right")
        self.assertEqual(self.app.notice_return, "complete")
        self.assertTrue(self.cloud.current["complete"])
        self.assertEqual(self.cloud.pending, 0)
        self.press("right")
        del self.cloud.fail["accept_scan"]
        self.press("right")
        self.assertEqual(self.app.screen, "home")
        self.assertEqual(self.cloud.pending, 1)

    def test_cloud_configuration_has_independent_draft_and_masked_url_editor(self):
        self.settings()
        self.assertNotIn(EXAMPLE_URL, repr(self.display.labels))
        self.touch("cloud_toggle")
        self.touch("cloud_url")
        self.assertFalse(self.app._cloud_show)
        self.assertNotIn(EXAMPLE_URL, repr(self.display.labels))
        self.touch("cloud_show")
        self.assertIn("<" + EXAMPLE_URL[-13:], self.display.labels)
        self.touch("cloud_clear")
        for char in "bad":
            self.touch(("cloud_char", char))
        self.press("left")
        self.assertEqual(self.app._cloud_url_draft, EXAMPLE_URL)
        self.assertEqual(self.app._cloud_url_edit, "")
        self.assertFalse(self.app._cloud_show)
        self.press("left")
        self.assertFalse(self.cloud.enabled)
        self.assertEqual(self.cloud.url, EXAMPLE_URL)
        self.assertEqual(self.app._cloud_url_draft, "")
        self.assertNotIn("save_config", self.cloud.calls)

    def test_cloud_config_save_is_immediate_and_survives_scan_settings_cancel(self):
        self.settings()
        self.touch("cloud_toggle")
        self.touch("cloud_url")
        self.app._cloud_url_edit = EXAMPLE_URL.replace("EXAMPLE_", "SECOND_")
        self.press("right")
        self.press("right")
        self.assertTrue(self.cloud.enabled)
        self.assertIn("SECOND_", self.cloud.url)
        self.press("left")
        self.press("left")
        self.assertEqual(self.store.writes, 0)
        self.settings()
        self.assertEqual(self.app._cloud_url_draft, self.cloud.url)
        self.assertTrue(self.app._cloud_enabled_draft)

    def test_invalid_url_and_save_error_do_not_replace_saved_config_or_leak(self):
        self.settings()
        self.touch("cloud_url")
        self.app._cloud_url_edit = "https://wrong.invalid/private-value"
        self.press("right")
        self.press("right")
        self.assertEqual(self.cloud.url, EXAMPLE_URL)
        self.assertIn("published Google Apps Script", self.app.notice)
        self.assertNotIn("private-value", repr(self.logs) + repr(self.display.labels))
        self.press("right")
        self.cloud.fail["save_config"] = OSError(EXAMPLE_URL)
        self.press("right")
        self.assertNotIn(EXAMPLE_URL, repr(self.logs) + self.app.notice)

    def test_receiver_editor_is_bounded_and_keyboard_does_not_change_wifi_editor(self):
        self.settings()
        self.touch("cloud_url")
        self.touch("cloud_clear")
        for _ in range(270):
            self.app.action(("cloud_char", "a"))
        self.assertEqual(len(self.app._cloud_url_edit), 256)
        self.app.action("cloud_backspace")
        self.assertEqual(len(self.app._cloud_url_edit), 255)
        self.assertEqual(self.app._wifi_password, "")

    def test_upload_only_runs_after_uploading_status_has_been_painted(self):
        self.cloud.enabled = True
        self.cloud.pending = 1
        self.wifi.state = "connected"
        self.cloud.step_check = lambda: self.assertIn("Uploading saved scans", self.display.labels)
        self.tick()  # transport start is local preparation only
        self.assertNotIn("transport_step", self.cloud.calls)
        self.tick()
        self.assertIn("transport_step", self.cloud.calls)
        self.assertEqual(self.cloud.pending, 0)

    def test_upload_work_is_excluded_from_acquisition_and_noncloud_settings(self):
        self.cloud.pending, self.cloud.enabled = 1, True
        self.wifi.state = "connected"
        self.press("right")
        before = self.cloud.calls.count("tick")
        for _ in range(10):
            self.tick()
        self.assertEqual(self.cloud.calls.count("tick"), before)
        self.press("left")
        self.press("left")
        before = self.cloud.calls.count("tick")
        self.touch("advanced")
        self.touch("cloud")
        self.touch("cloud_url")
        before_editor = self.cloud.calls.count("tick")
        self.tick()
        self.assertEqual(self.cloud.calls.count("tick"), before_editor)

    def test_cloud_pauses_before_wifi_discovery_connection_or_cancel(self):
        self.press("left")
        self.touch("advanced")
        self.touch("wifi")
        self.touch("wifi_scan")
        self.wifi.scan_check = lambda: self.assertEqual(self.cloud.calls[-1], "pause")
        self.tick()
        self.touch(("wifi_select", 0))
        original = self.wifi.connect
        def connect(*args, **kwargs):
            self.assertEqual(self.cloud.calls[-1], "pause")
            return original(*args, **kwargs)
        self.wifi.connect = connect
        self.touch("wifi_use_saved")
        pauses = self.cloud.calls.count("pause")
        self.press("left")
        self.assertEqual(self.cloud.calls.count("pause"), pauses + 1)

    def test_failed_shutdown_blocks_cloud_even_without_wifi_manager(self):
        self.app.wifi = None
        self.press("right")
        self.instrument.error = OSError("sensor fault")
        def zero_failed():
            raise OSError("cannot zero")
        self.instrument.zero = zero_failed
        self.finish()
        self.assertFalse(self.app._outputs_safe)
        self.press("left")
        count = self.cloud.calls.count("tick")
        self.tick()
        self.assertEqual(self.cloud.calls.count("tick"), count)

    def test_cloud_transport_exception_is_sanitized_and_queue_retained(self):
        self.cloud.pending, self.cloud.enabled = 1, True
        self.wifi.state = "connected"
        self.cloud.fail["tick"] = OSError(EXAMPLE_URL)
        self.tick()
        self.assertTrue(self.app._cloud_tick_failed)
        self.assertEqual(self.cloud.pending, 1)
        self.assertNotIn(EXAMPLE_URL, repr(self.logs))

    def test_retry_uses_saved_config_without_committing_draft(self):
        self.cloud.pending = 1
        self.wifi.state = "connected"
        self.settings()
        self.touch("cloud_toggle")
        self.touch("cloud_retry")
        self.assertTrue(self.cloud.manual)
        self.assertFalse(self.cloud.enabled)
        self.assertNotIn("save_config", self.cloud.calls)

    def test_actual_archive_save_cancel_and_cloud_config_survive_reload(self):
        from firmware.archive import Archive
        from firmware.cloud import CloudManager
        from test_archive import settings as historical_settings, row, CALIBRATION
        with tempfile.TemporaryDirectory() as folder:
            scans = str(Path(folder) / "scans")
            config = str(Path(folder) / "cloud.json")
            def manager():
                return CloudManager(archive=Archive(scans, device_id="aabb", free_bytes=lambda: 1000000), path=config)
            self.app.cloud = manager()
            def stage_historical_complete():
                self.app.cloud.begin_scan(historical_settings(), CALIBRATION, self.now)
                self.app.cloud.record_point(row())
                self.app.cloud.finish_scan(self.now + 100)
                self.app._archive_active = self.app._archive_ready = True
                self.app.goto("complete")
            stage_historical_complete()
            self.assertEqual(len(list(Path(scans).glob("*.draft"))), 1)
            self.assertEqual(self.app.cloud.status["pending_count"], 0)
            self.press("right")
            self.assertEqual(len(list(Path(scans).glob("*.pending"))), 1)
            stage_historical_complete()
            self.press("left")
            self.assertEqual(len(list(Path(scans).glob("*.pending"))), 1)
            self.assertEqual(list(Path(scans).glob("*.draft")), [])
            self.assertEqual(list(Path(scans).glob("*.writing")), [])
            self.settings()
            self.touch("cloud_url")
            self.app._cloud_url_edit = EXAMPLE_URL
            self.press("right")
            self.touch("cloud_toggle")
            self.press("right")
            restored = manager()
            self.assertEqual(restored.status["pending_count"], 1)
            self.assertEqual(restored.config_for_edit(), {"url": EXAMPLE_URL, "enabled": True})
            self.assertEqual([], self.instrument.commands)


if __name__ == "__main__":
    unittest.main()
