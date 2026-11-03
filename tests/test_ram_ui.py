"""One-scan RAM lifecycle with a real engine and no scan filesystem writes."""

import unittest
from unittest.mock import patch

from firmware import model, tls_memory
from firmware.engine import Engine, OverrangeError
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App, PackedPlotView, UPLOAD_FAILURE, CURVES
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore, fixture_calibration
from test_wifi_ui import FakeWifi


class RamWifi(FakeWifi):
    def __init__(self):
        super().__init__()
        self.state = "paused"
        self.available = True
        self.auto_join = True
        self.failure = False

    def autoconnect(self, now_ms=0, persist=True):
        self.calls.append(("autoconnect", persist))
        if not self.available:
            return False
        self.state = "connecting"
        return True

    def tick(self, now_ms):
        self.calls.append(("tick",))
        if self.failure:
            self.state = "error"
        elif self.auto_join and self.state == "connecting":
            self.state = "connected"


class RamCloud:
    def __init__(self, buffer):
        self.buffer = buffer
        self.state = "idle"
        self.calls = []
        self.fault = None
        self.cached = None
        self.ack = True
        self.upload_error = False
        self.url = "https://script.google.com/macros/s/EXAMPLE/exec"
        self.enabled = True

    @property
    def status(self):
        return {"state": self.state, "message": self.state, "configured": True,
                "enabled": self.enabled}

    def pause(self):
        self.calls.append("pause")
        return True

    def begin_scan(self, settings, calibration, now_ms):
        self.calls.append("begin")
        self.buffer.clear()
        self.cached = calibration
        self.state = "recording"

    def record_point(self, row):
        self.calls.append("point")
        self.buffer.append(row)

    def finish_scan(self, now_ms, fault=None):
        self.calls.append("finish")
        self.fault = fault
        self.state = "preparing"

    def invalidate_calibration(self):
        self.calls.append("invalidate")
        self.cached = None

    def tick(self, now_ms, online=False):
        self.calls.append("tick:" + self.state)
        if self.state == "preparing":
            self.state = "waiting_wifi"
        elif self.state == "waiting_wifi" and online:
            self.state = "uploading"
        elif self.state == "uploading":
            if self.upload_error:
                self.state = "error"
            elif self.ack:
                self.state = "complete"

    def fail_upload(self):
        self.calls.append("fail")
        self.state = "error"

    def discard_scan(self):
        self.calls.append("discard")
        self.buffer.clear()
        self.state = "idle"

    def config_for_edit(self):
        return {"url": self.url, "enabled": self.enabled}

    def save_config(self, url, enabled=True):
        self.calls.append("save_config")
        self.url, self.enabled = url, enabled


class RamUITests(unittest.TestCase):
    def setUp(self):
        self.display, self.inputs = FakeDisplay(), FakeInputs()
        self.instrument = HostInstrument()
        self.engine = Engine(self.instrument)
        self.store = MemoryStore()
        self.buffer = PackedScanBuffer(capacity=32)
        self.cloud, self.wifi = RamCloud(self.buffer), RamWifi()
        self.restarts, self.logs = [], []
        self.settings = dict(model.DEFAULT_SETTINGS)
        self.settings.update(drain_stop_uv=1000000, drain_step_uv=1000000,
                             gate_start_uv=1000000, gate_stop_uv=1000000,
                             gate_step_uv=1000000, ina_average_count=4)
        self.app = App(self.display, self.inputs, self.engine, self.settings,
                       fixture_calibration(), self.store, wifi=self.wifi, cloud=self.cloud,
                       scan_buffer=self.buffer, restart=lambda: self.restarts.append(True),
                       error_logger=lambda context, error: self.logs.append((context, str(error))))
        self.now = 0
        self.tick()

    def tick(self, action=None):
        self.now += 20
        self.inputs.events = [(action,)] if action else []
        self.app.tick(self.now)

    def action(self, action):
        self.app.action(action)
        self.tick()

    def until(self, predicate, limit=3000):
        for unused in range(limit):
            if predicate():
                return
            self.tick()
        self.fail("State did not arrive: %s / %s" % (self.app.screen, self.cloud.state))

    def start(self):
        self.tick("right")
        self.until(lambda: self.app.screen == "scan")

    def test_home_is_quiet_and_startup_notice_is_inline(self):
        for unused in range(5):
            self.tick()
        self.assertEqual(self.app.screen, "home")
        self.assertEqual(self.wifi.calls, [])
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.store.writes, 0)
        app = App(FakeDisplay(), FakeInputs(), Engine(HostInstrument()), self.settings,
                  fixture_calibration(), startup_notice="Sensor setup needs checking",
                  cloud=self.cloud, scan_buffer=self.buffer, restart=lambda: None)
        app.render()
        self.assertEqual(app.screen, "home")
        self.assertIn("Sensor setup needs checking", app.d.labels)
        self.assertEqual([hit[4] for hit in app.hits], ["left", "right"])

    def test_plot_views_allocate_no_legacy_buffers_and_read_same_records(self):
        with patch("firmware.ui.PlotValues", side_effect=AssertionError("legacy allocation")):
            app = App(FakeDisplay(), FakeInputs(), Engine(HostInstrument()), self.settings,
                      fixture_calibration(), cloud=self.cloud,
                      scan_buffer=self.buffer, restart=lambda: None)
        self.assertIsInstance(app.rows, PackedPlotView)
        row = {"vds": 0.75, "vgs": 0.899999, "id_a": 0.125e-6, "ig_a": 0.002e-6,
               "requested_vgs": 1.0, "curve": 0}
        self.buffer.append(row)
        app._add_point(row)
        self.assertEqual(len(self.buffer), 1)
        self.assertAlmostEqual(app.rows[0], 0.75)
        self.assertAlmostEqual(app.rows[1], 0.125e-6)
        self.assertEqual(app.limit_flags[0], 4)

    def test_packed_plot_breaks_off_gate_segments_and_derives_new_curve(self):
        self.app._ram_drain_count = 4
        lines = []
        self.display.line = lambda *args: lines.append(args)
        for index, difference in enumerate((0, 0, 0.100001, 0, 0, 0)):
            row = {"vds": (index % 4) / 10, "vgs": 1 + difference,
                   "id_a": index * 0.1e-6, "ig_a": 0.0,
                   "requested_vgs": 1.0, "curve": index // 4}
            self.buffer.append(row)
            self.app._add_point(row)
        self.app._draw_points()
        self.app._draw_points()
        self.assertEqual([0, 0, 4, 0, 0, 0], list(self.app.limit_flags))
        self.assertEqual([0, 0, 0, 0, 1, 1], list(self.app.curves))
        self.assertEqual([(*self.app._xy(0), *self.app._xy(1), CURVES[0]),
                          (*self.app._xy(4), *self.app._xy(5), CURVES[1])], lines)

    def test_packed_plot_joins_50_and_100_mv_offsets_but_breaks_one_uv_beyond(self):
        self.app._ram_drain_count = 10
        lines = []
        self.display.line = lambda *args: lines.append(args)
        offsets = (0, 50000, 100000, 100001, 50000, -50000, -100000, -100001, 0, 0)
        for index, offset_uv in enumerate(offsets):
            row = {"vds": index / 10, "vgs": 1 + offset_uv / 1000000,
                   "id_a": index * 0.1e-6, "ig_a": 0.0,
                   "requested_vgs": 1.0, "curve": 0}
            self.buffer.append(row)
            self.app._add_point(row)
        while self.app._plot_cursor < len(self.app.curves):
            self.app._draw_points()
        self.assertEqual([0, 0, 0, 4, 0, 0, 0, 4, 0, 0], list(self.app.limit_flags))
        self.assertEqual([(*self.app._xy(left), *self.app._xy(right), CURVES[0])
                          for left, right in ((0, 1), (1, 2), (4, 5), (5, 6), (8, 9))], lines)

    def test_settings_exit_save_or_cancel_restarts_and_hides_storage(self):
        for save in (False, True):
            with self.subTest(save=save):
                self.setUp()
                self.tick("left")
                self.action("advanced")
                self.assertNotIn("STORAGE", self.display.labels)
                self.action("cloud")
                self.assertFalse(any("pending" in text or "AUTO UPLOAD" in text or text == "RETRY"
                                     for text in self.display.labels))
                self.cloud.enabled = False
                self.action("right")
                self.assertTrue(self.cloud.enabled)
                self.action("right")
                self.assertEqual(self.app.screen, "settings")
                self.app.draft["drain_stop_uv"] = 2000000
                self.tick("right" if save else "left")
                self.assertEqual(self.restarts, [True])
                self.assertEqual(self.store.writes, int(save))
                self.assertEqual(self.wifi.state, "paused")

    def test_settings_releases_tls_before_wifi_draft_render_and_save(self):
        order = []
        release, save, fill = tls_memory.release, self.store.save_settings, self.display.fill
        scan_bytes = self.buffer._data

        def release_for_settings():
            self.assertTrue(self.app._settings_path)
            self.assertTrue(tls_memory.available())
            result = release()
            order.append("release")
            return result

        def checked_resume():
            self.assertFalse(tls_memory.available())
            order.append("wifi")

        def checked_dict(value):
            self.assertFalse(tls_memory.available())
            order.append("draft")
            return dict(value)

        def checked_fill(color):
            self.assertFalse(tls_memory.available())
            order.append("render")
            return fill(color)

        def checked_save(value):
            self.assertFalse(tls_memory.available())
            order.append("save")
            return save(value)

        self.wifi.resume_check = checked_resume
        self.display.fill, self.store.save_settings = checked_fill, checked_save
        with patch.object(tls_memory, "_buffer", bytearray(tls_memory.DEFAULT_BYTES)), \
                patch.object(tls_memory, "release", side_effect=release_for_settings) as released, \
                patch.object(tls_memory, "reserve", side_effect=AssertionError("Settings restored TLS")) as reserved, \
                patch("firmware.ui.dict", side_effect=checked_dict, create=True):
            self.tick("left")
            self.assertEqual(order, ["release", "wifi", "draft", "render"])
            self.app.draft["drain_stop_uv"] = 2000000
            self.tick("right")
            self.assertEqual(order, ["release", "wifi", "draft", "render", "save", "render"])
            released.assert_called_once_with()
            reserved.assert_not_called()
            self.assertFalse(tls_memory.available())
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.store.settings["drain_stop_uv"], 2000000)
        self.assertEqual(self.store.writes, 1)
        self.assertIs(self.buffer._data, scan_bytes)
        self.assertEqual(self.logs, [])

    def test_settings_cancel_restarts_without_restoring_tls_reserve(self):
        with patch.object(tls_memory, "_buffer", bytearray(tls_memory.DEFAULT_BYTES)), \
                patch.object(tls_memory, "release", wraps=tls_memory.release) as released, \
                patch.object(tls_memory, "reserve", side_effect=AssertionError("Settings restored TLS")) as reserved:
            self.tick("left")
            self.assertFalse(tls_memory.available())
            self.app.draft["drain_stop_uv"] = 2000000
            self.tick("left")
            self.tick("left")  # An input after the restart callback cannot reenter Settings.
            released.assert_called_once_with()
            reserved.assert_not_called()
            self.assertFalse(tls_memory.available())
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.app.settings["drain_stop_uv"], 1000000)
        self.assertEqual(self.wifi.state, "paused")

    def test_scan_retains_tls_reserve_through_acquisition(self):
        reserve = bytearray(tls_memory.DEFAULT_BYTES)
        with patch.object(tls_memory, "_buffer", reserve), \
                patch.object(tls_memory, "release", side_effect=AssertionError("Scan released TLS early")) as released:
            self.start()
            self.until(lambda: self.app.screen == "upload")
            released.assert_not_called()
            self.assertIs(tls_memory._buffer, reserve)
            self.assertTrue(tls_memory.available())
        self.assertFalse(self.app._settings_path)
        self.assertEqual(len(self.buffer), 2)
        self.assertEqual(self.store.writes, 0)

    def test_settings_ok_does_not_allocate_a_draft_after_commit(self):
        self.tick("left")
        self.app.draft["drain_stop_uv"] = 2000000
        committed = []
        save = self.store.save_settings

        def save_and_mark(value):
            result = save(value)
            committed.append(result)
            return result

        def no_post_commit_copy(value):
            if committed:
                raise MemoryError("synthetic post-commit draft allocation")
            return dict(value)

        self.store.save_settings = save_and_mark
        with patch("firmware.ui.dict", side_effect=no_post_commit_copy, create=True) as copied:
            self.tick("right")
            copied.assert_not_called()
        self.assertEqual(self.store.writes, 1)
        self.assertEqual(self.store.settings["drain_stop_uv"], 2000000)
        self.assertIs(self.app.settings, committed[0])
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.app.screen, "restarting")
        self.assertEqual(self.logs, [])

    def test_scan_arms_after_paint_and_records_once_then_uploads_without_prompt(self):
        self.start()
        self.assertEqual(self.instrument.commands, [])
        self.assertIn("SCANNING", self.display.labels)
        self.until(lambda: self.app.screen == "upload")
        self.assertEqual(len(self.buffer), 2)
        self.assertEqual(self.cloud.calls.count("point"), 2)
        self.assertEqual(self.cloud.calls.count("finish"), 1)
        self.assertIsNone(self.app.calibration)
        self.assertIsNone(self.engine.calibration)
        self.assertIsNone(self.cloud.cached)
        self.assertFalse(any(call[0] == "autoconnect" for call in self.wifi.calls))
        self.assertEqual(self.store.writes, 0)
        self.until(lambda: bool(self.restarts))
        self.assertEqual(self.restarts, [True])
        self.assertIn(("autoconnect", False), self.wifi.calls)
        self.assertEqual(len(self.buffer), 0)
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.logs, [])
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})

    def test_cancel_at_preparation_scan_hash_connect_and_upload_restarts_once(self):
        for stage in ("preparing", "scan", "preparing_upload", "waiting_wifi", "uploading"):
            with self.subTest(stage=stage):
                self.setUp()
                self.cloud.ack = False
                self.tick("right")
                if stage == "scan":
                    self.until(lambda: self.app.screen == "scan")
                    self.tick()
                elif stage == "preparing_upload":
                    self.until(lambda: self.app.screen == "upload")
                elif stage in ("waiting_wifi", "uploading"):
                    self.until(lambda: self.cloud.state == stage)
                self.tick("left")
                self.assertEqual(self.restarts, [True])
                self.assertEqual(len(self.buffer), 0)
                self.assertEqual(self.store.writes, 0)
                self.assertEqual(self.wifi.state, "paused")
                self.tick("left")
                self.assertEqual(self.restarts, [True])

    def test_scan_cancel_paints_after_output_stop_before_cleanup_for_button_and_touch(self):
        for touch in (False, True):
            with self.subTest(touch=touch):
                self.setUp()
                self.start()
                self.until(lambda: self.instrument.duties["gate"] > 0)
                order = []
                original_cancel, original_fill = self.engine.cancel, self.display.fill
                original_discard, original_pause = self.cloud.discard_scan, self.wifi.pause

                def cancel():
                    original_cancel()
                    order.append(("stopped", dict(self.instrument.duties)))

                def fill(color):
                    order.append(("clear", dict(self.instrument.duties)))
                    original_fill(color)

                def discard():
                    order.append(("discard", list(self.display.labels)))
                    original_discard()

                def pause():
                    order.append(("pause", list(self.display.labels)))
                    return original_pause()

                def restart():
                    order.append(("restart", list(self.display.labels)))
                    self.restarts.append(True)

                self.engine.cancel, self.display.fill = cancel, fill
                self.cloud.discard_scan, self.wifi.pause = discard, pause
                self.app._restart_callback = restart
                with patch.object(self.engine, "tick") as engine_tick, patch.object(self.cloud, "tick") as cloud_tick:
                    self.inputs.events = [("touch", 100, 295)] if touch else [("left",)]
                    self.now += 20
                    self.app.tick(self.now)
                    self.tick("left")
                    engine_tick.assert_not_called()
                    cloud_tick.assert_not_called()
                self.assertEqual([item[0] for item in order],
                                 ["stopped", "clear", "discard", "pause", "restart"])
                for unused, duties in order[:2]:
                    self.assertEqual(duties, {"drain": 0.0, "gate": 0.0})
                for unused, labels in order[2:]:
                    self.assertIn("RESTARTING", labels)
                    self.assertIn("Returning to Home", labels)
                    self.assertNotIn("SCANNING", labels)
                self.assertEqual(self.restarts, [True])
                self.assertEqual(self.app.hits, [])
                self.assertEqual(self.store.writes, 0)

    def test_restart_frame_is_visible_before_failing_upload_cleanup(self):
        self.cloud.ack = False
        self.start()
        self.until(lambda: self.cloud.state == "uploading")
        observed = []

        def fail_close():
            observed.append(list(self.display.labels))
            raise OSError("synthetic close failure")

        self.cloud.discard_scan = self.wifi.pause = fail_close
        self.tick("left")
        self.assertEqual(len(observed), 2)
        self.assertTrue(all("RESTARTING" in labels for labels in observed))
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.store.writes, 0)

    def test_restart_render_failure_cannot_prevent_discard_or_reset(self):
        self.start()
        self.until(lambda: len(self.buffer) == 1)
        with patch.object(self.display, "fill", side_effect=MemoryError("synthetic display failure")), \
                patch.object(self.app, "_record_error") as record_error:
            self.tick("left")
            record_error.assert_called_once_with("restart display", "Could not draw restart screen")
        self.assertEqual(self.restarts, [True])
        self.assertEqual(len(self.buffer), 0)
        self.assertEqual(self.wifi.state, "paused")
        self.assertEqual(self.instrument.duties, {"drain": 0.0, "gate": 0.0})
        self.assertEqual(self.store.writes, 0)

    def test_failed_output_stop_prioritizes_final_shutdown_over_painting(self):
        self.start()

        def failed_stop():
            self.engine.state = "error"
            self.engine.shutdown_succeeded = False

        self.engine.cancel = failed_stop
        with patch.object(self.display, "fill") as paint, \
                patch.object(self.cloud, "discard_scan") as discard, \
                patch.object(self.wifi, "pause") as pause:
            self.tick("left")
            paint.assert_not_called()
            discard.assert_not_called()
            pause.assert_not_called()
        self.assertEqual(self.restarts, [True])

    def test_overrange_automatically_uploads_only_completed_points_and_fault(self):
        self.start()
        self.until(lambda: len(self.buffer) == 1)
        self.instrument.error = OverrangeError("drain", "shunt_saturation", 32767, 1, 0.0819175, "V")
        self.until(lambda: self.app.screen == "upload")
        self.assertEqual(len(self.buffer), 1)
        self.assertEqual(self.cloud.fault["kind"], "shunt_saturation")
        self.assertEqual(self.cloud.fault["index"], 1)
        self.assertEqual(self.store.writes, 0)
        self.until(lambda: bool(self.restarts))

    def test_zero_point_current_fault_shows_signed_measurement_without_upload(self):
        for button in ("left", "right"):
            with self.subTest(button=button):
                self.setUp()
                self.start()
                self.instrument.error = OverrangeError(
                    "drain", "current_limit", None, -1, 0.005, "A", -0.020915716)
                with patch("builtins.print"):
                    self.until(lambda: self.app.screen == "scan_error")
                self.assertEqual(self.engine.state, "partial")
                self.assertEqual(self.app._scan_fault["phase"], "park_drain")
                self.assertEqual(len(self.buffer), 0)
                self.assertIn("DRAIN current limit", self.app.notice)
                self.assertIn("No measured points.", self.app.notice)
                self.assertIn("Measured -20.916 mA; limit 5.000 mA.", self.app.notice)
                self.assertIn("SCAN STOPPED", self.display.labels)
                self.assertEqual({"drain": 0.0, "gate": 0.0}, self.instrument.duties)
                self.assertTrue(self.engine.shutdown_succeeded)
                for unused in range(5):
                    self.tick()
                self.assertNotIn("finish", self.cloud.calls)
                self.assertFalse(any(call.startswith("tick:") for call in self.cloud.calls))
                self.assertFalse(any(call[0] == "autoconnect" for call in self.wifi.calls))
                self.assertEqual(self.store.writes, 0)
                self.tick(button)
                self.assertEqual(self.restarts, [True])
                self.assertEqual(self.cloud.state, "idle")
                self.assertIn("discard", self.cloud.calls)

    def test_zero_point_alert_and_adc_faults_show_fixed_diagnostic_kind(self):
        failures = (
            OverrangeError("gate", "current_limit_alert", None, 1, 0.0001, "A"),
            OverrangeError("drain", "shunt_saturation", 32767, 1, 0.0819175, "V"),
            OverrangeError("gate", "bus_saturation", 32767, 1, 40.95875, "V"))
        for failure in failures:
            with self.subTest(kind=failure.kind):
                self.setUp()
                self.start()
                self.instrument.error = failure
                with patch("builtins.print"):
                    self.until(lambda: self.app.screen == "scan_error")
                self.assertIn(failure.channel.upper() + " " + failure.kind.replace("_", " "), self.app.notice)
                self.assertIn("No measured points.", self.app.notice)
                self.assertNotIn("Measured ", self.app.notice)
                self.assertNotIn("finish", self.cloud.calls)
                self.assertEqual(len(self.buffer), 0)

    def test_empty_fault_notice_does_not_echo_unrecognized_diagnostic_text(self):
        self.app._outputs_safe = True
        with patch("builtins.print"):
            self.app._ram_seal({"channel": "private-channel", "kind": "private-message"})
        self.assertEqual(self.app.notice, "OUTPUT overrange. No measured points.")
        self.assertEqual(self.app.screen, "scan_error")
        self.assertNotIn("finish", self.cloud.calls)

    def test_hard_sensor_error_accepts_cancel_or_ok_and_never_uploads(self):
        for button in ("left", "right"):
            for touch in (False, True):
                with self.subTest(button=button, touch=touch):
                    self.setUp()
                    self.start()
                    self.instrument.error = OSError("sensor offline")
                    self.until(lambda: self.app.screen == "scan_error")
                    self.assertNotIn("finish", self.cloud.calls)
                    self.assertEqual([hit[4] for hit in self.app.hits], ["left", "right"])
                    if touch:
                        self.inputs.events = [("touch", 100 if button == "left" else 350, 295)]
                        self.now += 20
                        self.app.tick(self.now)
                    else:
                        self.tick(button)
                    self.assertEqual(self.restarts, [True])
                    self.assertEqual(len(self.buffer), 0)
                    self.assertEqual(self.store.writes, 0)
                    self.assertEqual(self.logs, [])

    def test_wifi_and_upload_failures_show_exact_notice_and_cancel_or_ok(self):
        for cause in ("missing", "connection", "timeout", "upload"):
            with self.subTest(cause=cause):
                self.setUp()
                if cause == "missing":
                    self.wifi.available = False
                elif cause == "connection":
                    self.wifi.failure = True
                elif cause == "timeout":
                    self.wifi.auto_join = False
                else:
                    self.cloud.upload_error = True
                self.start()
                self.until(lambda: self.app.screen == "upload_error")
                self.assertEqual(self.app.notice, UPLOAD_FAILURE)
                self.assertEqual([hit[4] for hit in self.app.hits], ["left", "right"])
                self.assertIn("fail", self.cloud.calls)
                self.tick("left" if cause in ("missing", "timeout") else "right")
                self.assertEqual(self.restarts, [True])
                self.assertEqual(len(self.buffer), 0)
                self.assertEqual(self.store.writes, 0)

    def test_right_stays_disabled_through_scan_and_upload(self):
        for stage in ("preparing", "scan", "preparing_upload", "waiting_wifi", "uploading"):
            with self.subTest(stage=stage):
                self.setUp()
                self.cloud.ack = False
                self.tick("right")
                if stage == "scan":
                    self.until(lambda: self.app.screen == "scan")
                elif stage == "preparing_upload":
                    self.until(lambda: self.app.screen == "upload")
                elif stage in ("waiting_wifi", "uploading"):
                    self.until(lambda: self.cloud.state == stage)
                self.assertEqual([hit[4] for hit in self.app.hits], ["left"])
                before = (self.app.screen, self.engine.state, self.cloud.state,
                          len(self.buffer), list(self.cloud.calls))
                self.app.action("right")
                self.assertEqual(before, (self.app.screen, self.engine.state, self.cloud.state,
                                          len(self.buffer), self.cloud.calls))
                self.assertEqual(self.restarts, [])
                self.assertEqual(self.store.writes, 0)

    def test_real_ram_manager_keeps_frozen_buffer_through_ack_then_resets(self):
        from firmware.ram_cloud import RamCloudManager

        class Transport:
            def __init__(self):
                self.owner = None
                self.started = False

            def close(self):
                pass

            def start(self, url, owner, scan_id, sha256, count):
                self.owner = owner
                self.result = {"ok": True, "scan_id": scan_id, "sha256": sha256,
                               "point_count": count, "sheet_id": 1}
                self.started = True

            def step(self):
                return self.result

        self.store.files["cloud.json"] = {"version": 1, "url": self.cloud.url, "enabled": True}
        transport = Transport()
        self.cloud = RamCloudManager(self.buffer, transport=transport, store=self.store,
                                     device_id="cafe", random_bytes=lambda count: b"a" * count)
        self.app.cloud = self.cloud
        self.start()
        self.until(lambda: self.app.screen == "upload")
        with self.assertRaises(ValueError):
            self.buffer.clear()
        self.until(lambda: bool(self.restarts))
        self.assertTrue(transport.started)
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(len(self.buffer), 0)


if __name__ == "__main__":
    unittest.main()
