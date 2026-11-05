"""480x320 touchscreen application; hardware is injected for host-side QA."""

import math
import gc
# Resolve once: repeated imports cost about 4 ms each on this Pico firmware.
try:
    from time import ticks_diff as _ticks_diff
except ImportError:
    def _ticks_diff(now, before):
        return now - before

try:
    from . import model, storage, tls_memory, scan_timing
except ImportError:
    import model, storage, tls_memory, scan_timing

def rgb(value):
    return ((value >> 8) & 0xF800) | ((value >> 5) & 0x07E0) | ((value >> 3) & 31)

BG, INK, WHITE = rgb(0xE9F0F5), rgb(0x12354A), 0xFFFF
MUTED, GRID = rgb(0x58707E), rgb(0xD8E2E9)
TEAL, AMBER, RED = rgb(0x007E87), rgb(0xC75C0B), rgb(0xB33836)
CURVES = (TEAL, AMBER, rgb(0x6854A3), rgb(0x2670B3), rgb(0xAB4064), rgb(0x50742D))
WIFI_KEYS = {
    "abc": ("qwertyuiop", "asdfghjkl", "zxcvbnm"),
    "ABC": ("QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"),
    "123": ("1234567890", "`~()[]{}<>", "+-*/=.,:;?"),
    "#+=": ("!@#$%^&*()", "-_=+[]{}\\|", ";:'\",.<>/?"),
}
UPLOAD_FAILURE = "WiFi Error, Upload Failed\nPlease reconnect WiFi in settings and redo scan."
UPLOAD_STAGES = ("Prepare data", "Connect", "Upload data", "Confirm saved")
UPLOAD_DONE_MS = 600

class PackedPlotView:
    """Read plot coordinates directly from the shared RAM scan records."""
    def __init__(self, app, kind):
        self.app, self.kind = app, kind

    def __len__(self):
        return len(self.app._scan_buffer) * (2 if self.kind == "xy" else 1)

    def __getitem__(self, index):
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError("Plot index outside stored results")
        if self.kind == "curve":
            return index // self.app._ram_drain_count
        if self.kind == "xy":
            record = self.app._scan_buffer[index // 2]
            return record[0 if index % 2 == 0 else 2]
        raise ValueError("Unknown plot view")

    def clear(self):
        pass  # The cloud manager owns the one shared buffer's lifetime.

class App:
    def __init__(self, display, inputs, engine, settings=None,
                 store=storage, startup_notice=None, error_logger=None, wifi=None, cloud=None,
                 scan_buffer=None, restart=None, timing=None,
                 calibration_factory=None, calibration_path="current-baseline.bin",
                 manual_factory=None):
        self.d, self.inputs, self.engine, self.store = display, inputs, engine, store
        self._timing = timing
        self.settings = model.validate_settings(settings or model.DEFAULT_SETTINGS)
        self.error_logger = error_logger
        self.wifi = wifi
        self.cloud = cloud
        if scan_buffer is None or not callable(restart):
            raise ValueError("RAM scanning requires a buffer and restart callback")
        self._scan_buffer, self._restart_callback = scan_buffer, restart
        self._scan_path = False
        self._settings_path = False
        self._restart_pending = False
        self._restart_called = False
        self._ram_drain_count = 1
        self._upload_wifi_started = False
        self._upload_connect_ms = 0
        self._upload_complete_ms = None
        self._upload_marks = [None] * 4
        self._upload_percent = None
        self._startup_notice = str(startup_notice or "")
        # main holds both outputs low before constructing an idle engine.
        self._outputs_safe = getattr(engine, "state", "idle") == "idle"
        self._scan_fault = None
        self._cloud_error = None
        self._cloud_url_draft = ""
        self._cloud_url_edit = ""
        self._cloud_show = False
        self._cloud_keyboard = "abc"
        self._cloud_value_dirty = False
        self._now_ms = 0
        self._scan_prep = 0
        # The production factory reuses a boot-reserved binary workspace.
        # Keep only one controller, never a list of calibration samples.
        self._calibration_factory = calibration_factory
        self._calibration_path = calibration_path
        self._calibration = None
        self._calibration_prepare = False
        self._calibration_settings = None
        self._calibration_error = None
        self._calibration_summary = None
        self._calibration_percent = -1
        self._calibration_phase = None
        self._calibration_channel = None
        self._calibration_render_ms = None
        self._manual_factory = manual_factory
        self._manual = None
        self._manual_session = False
        self._manual_cache = None
        self._manual_render_ms = None
        self._manual_revision = -1
        self._manual_error = None
        self._manual_wait_tick = False
        self._wifi_paused = False
        self._wifi_search_pending = False
        self._wifi_rows = []
        self._wifi_saved_list = False
        self._wifi_page = 0
        self._wifi_selected = None
        self._wifi_password = ""
        self._wifi_show = False
        self._wifi_keyboard = "abc"
        self._wifi_value_dirty = False
        self._wifi_status_key = None
        self._wifi_error = None
        self._wifi_connect_failed = False
        self.screen = "home"
        self.hits = []
        self.dirty = True
        self.live_dirty = False
        self.edit_dirty = False
        self.latest = None
        # Plot views share the four-measurement RAM buffer without copies.
        self.rows = PackedPlotView(self, "xy")
        self.curves = PackedPlotView(self, "curve")
        self._shown_scan_header = None
        self._plot_cursor = 0
        self._reset_plot_bounds()
        # Fixed-size caches retain only the last rendered strings/colors.
        # They never grow with scan duration or replace the result buffers.
        self._live_values = [None] * 4
        self._live_label_colors = [None] * 4
        self._live_requests = [None] * 2
        self._live_misc = [None] * 2  # caption and progress pixels
        self._live_screen = None
        self._last_live_ms = None
        self.notice_return = "home"

    def goto(self, screen):
        if self._manual is not None and screen != "manual" and (
                self.screen == "manual" or self._manual.outputs_enabled):
            if not self._stop_manual():
                self._request_restart()
                return
        if screen == "home" and self._settings_path:
            self._request_restart()
            return
        if self.screen == "cloud_url" and screen != "cloud_url":
            self._cloud_url_edit = ""
            self._cloud_show = False
        if (self.screen.startswith("cloud") and not screen.startswith("cloud")
                and screen != "notice"):
            self._cloud_url_draft = ""
        if self.screen == "wifi_password" and screen != "wifi_password":
            self._wifi_password = ""
            self._wifi_show = False
        if self.screen == "wifi_search" and screen != "wifi_search":
            self._wifi_search_pending = False
        if self._manual_session and screen not in ("manual", "edit", "notice", "manual_error"):
            self._manual_session = False
            if self.wifi is not None and self._outputs_safe:
                self.wifi.resume(self._now_ms)
                self._wifi_paused = False
        self.screen, self.dirty = screen, True

    def _reset_plot_bounds(self):
        self._plot_xmin = model.volts_from_uv(self.settings["drain_start_uv"])
        self._plot_xmax = model.volts_from_uv(self.settings["drain_stop_uv"])
        if self._plot_xmax == self._plot_xmin:
            self._plot_xmin -= 0.05
            self._plot_xmax += 0.05
        self._plot_ymin, self._plot_ymax = 0.0, 10.0
        self._plot_axes_dirty = False

    def _reset_live_cache(self):
        for cache in (self._live_values, self._live_label_colors,
                      self._live_requests, self._live_misc):
            for index in range(len(cache)):
                cache[index] = None
        self._live_screen = None

    def clear_results(self):
        """Reuse all fixed result buffers together at the next scan."""
        self.rows.clear()
        self.curves.clear()
        self.latest = None
        self._scan_fault = None
        self._plot_cursor = 0
        self._reset_plot_bounds()
        self._reset_live_cache()

    def message(self, text, return_to=None):
        self.notice = str(text)
        self.notice_return = return_to or self.screen
        self.goto("notice")

    def _record_error(self, context, error):
        if self._scan_path:
            try:
                print(context, str(error))
            except Exception:
                pass
            return
        if self.error_logger is not None:
            try:
                self.error_logger(context, error)
            except Exception:
                # Reporting must never replace the original fault.
                pass

    def _text(self, value, x, y, color=INK, scale=2, bg=BG):
        self.d.text(str(value), x, y, color, bg=bg, scale=scale)

    def _button(self, label, x, y, w, h, action, color=WHITE, ink=INK, scale=2):
        self.d.fill_rect(x, y, w, h, color)
        self.d.rect(x, y, w, h, GRID)
        self._text(label, x + (w - len(label) * 8 * scale) // 2,
                   y + (h - 8 * scale) // 2, ink, scale, color)
        if action:
            self.hits.append((x, y, w, h, action))

    def _header(self, title, color=INK):
        self.d.fill_rect(0, 0, 480, 40, color)
        self._text(title, 16, 12, WHITE, bg=color)

    def _scan_header(self, force=False):
        if force or self.screen != self._shown_scan_header:
            self._header("SCANNING")
            self._shown_scan_header = self.screen

    def _footer(self, home=False, busy=False):
        self.d.fill_rect(0, 270, 480, 50, BG)
        self._button("SETTINGS" if home else "CANCEL", 8, 274, 226, 42,
                     "left", INK, WHITE)
        self._button("SCAN" if home else "OK", 246, 274, 226, 42,
                     None if busy else "right", GRID if busy else AMBER,
                     MUTED if busy else WHITE)

    def _arm_scan(self):
        self.engine.start_scan(self.settings)

    def _baseline_ready(self, settings):
        baseline = getattr(self.engine, "baseline", None)
        return (baseline is not None and baseline.available and
                baseline.compatible(settings, getattr(self.engine.instrument, "identity", None)))

    def _baseline_label(self, settings):
        if self._baseline_ready(settings):
            return "Id corrected | Ig raw"
        baseline = getattr(self.engine, "baseline", None)
        if baseline is not None and baseline.available:
            return "RAW currents | Recalibration needed"
        return "RAW currents | Calibrate in Advanced"

    def _count(self, settings):
        return model.scan_point_count(settings)

    def render(self):
        self.d.fill(BG)
        self._reset_live_cache()
        self.hits = []
        screen = self.screen
        if screen == "home":
            self._header("POCKET MOSFET")
            self._text("READY TO SCAN", 16, 58, TEAL)
            for role, label, y in (("drain", "VDS", 99), ("gate", "VGS", 138)):
                self._text(label, 16, y, MUTED)
                limits = "%s to %s V" % (
                    model.format_voltage_uv(self.settings[role + "_start_uv"]),
                    model.format_voltage_uv(self.settings[role + "_stop_uv"]))
                self._text(limits, 90, y, scale=2 if len(limits) <= 14 else 1)
                self._text("step %s" % model.format_voltage_uv(self.settings[role + "_step_uv"]), 330, y + 4, MUTED, 1)
            self.d.line(16, 172, 464, 172, GRID)
            supplies = "V+ %s V    VS %s V" % (
                model.format_voltage_uv(self.settings["driver_supply_uv"]),
                model.format_voltage_uv(self.settings["source_voltage_uv"]))
            self._text(supplies, 16, 185, scale=2 if len(supplies) <= 28 else 1)
            self._text("%d points | INA averages: %d" % (self._count(self.settings), self.settings["ina_average_count"]), 16, 219, MUTED, 1)
            hint = self._baseline_label(self.settings)
            if self._startup_notice:
                hint = self._startup_notice[:56]
            self._text(hint, 16, 244, TEAL, 1)
            self._footer(home=True)
        elif screen == "settings":
            self._header("SCAN SETTINGS")
            for label, x in (("START", 144), ("STOP", 256), ("STEP", 368)):
                self._text(label, x, 58, MUTED, 1)
            for role, label, y in (("drain", "VDS", 82), ("gate", "VGS", 148)):
                self._text(label, 16, y + 15)
                for index, field in enumerate(("start", "stop", "step")):
                    key = role + "_" + field + "_uv"
                    value = model.format_voltage_uv(self.draft[key])
                    self._button(value, 124 + index * 112, y, 104, 48,
                                 ("edit", key, label + " " + field + " (V)"),
                                 scale=2 if len(value) <= 6 else 1)
            self._button("ADVANCED", 16, 215, 192, 42, "advanced", TEAL, WHITE)
            self._text("Voltages relative to source", 224, 225, MUTED, 1)
            self._text("OK saves / CANCEL discards", 224, 242, MUTED, 1)
            self._footer()
        elif screen == "advanced":
            self._header("ADVANCED")
            for label, key, y in (("Driver supply (V+)", "driver_supply_uv", 53),
                                   ("Source voltage (VS)", "source_voltage_uv", 108)):
                self._text(label, 16, y + 13)
                value = model.format_voltage_uv(self.draft[key]) + " V"
                self._button(value, 338, y, 126, 44,
                             ("edit", key, label), scale=2 if len(value) <= 7 else 1)
            self._text(self._baseline_label(self.draft), 16, 165, MUTED, 1)
            self._button("AVERAGING", 16, 183, 144, 40, "averaging", WHITE, INK, 1)
            self._button("CALIBRATE", 168, 183, 144, 40, "calibrate", TEAL, WHITE, 1)
            self._button("DEBUG", 320, 183, 144, 40, "manual", WHITE, INK, 1)
            self._button("WI-FI", 16, 232, 208, 32, "wifi", WHITE, INK, 1)
            self._button("CLOUD", 240, 232, 224, 32, "cloud", WHITE, INK, 1)
            self._footer()
        elif screen == "manual":
            self._header("MANUAL DEBUG")
            enabled = self._manual.outputs_enabled
            badge = TEAL if enabled else RED
            self.d.fill_rect(360, 6, 104, 28, badge)
            self._text("ON" if enabled else "OFF", 396 if enabled else 388,
                       12, WHITE, bg=badge)
            self._text("100 kHz | Targets and readback to GND", 16, 47, MUTED, 1)
            for index, (label, x) in enumerate((("Drain target (V to GND)", 16), ("Gate target (V to GND)", 248))):
                self._text(label, x, 63, MUTED, 1)
                value = model.format_voltage_uv(self._manual.targets_uv[index]) + " V"
                self._button(value, x, 77, 216, 35, ("manual_edit", index), scale=2 if len(value) < 12 else 1)
                if enabled:
                    code = self._manual.applied_duty_u16[index]
                    self._text("PWM %.2f%% %d/65535" % (code * 100 / 65535, code), x, 119, TEAL, 1)
                else:
                    self._text("OFF", x, 119, RED)
                self._text("Actual VBUS (V to GND)", x, 145, MUTED, 1)
                self._text("Id (uA)" if index == 0 else "Ig (uA)", x, 189, MUTED, 1)
            self._text("Bus ADC at IN+, before shunt", 16, 230, MUTED, 1)
            self._footer()
            self._render_manual_values(force=True)
        elif screen == "manual_error":
            self._header("DEBUG STOPPED", RED)
            self._wrapped(self._manual_error, 16, 62, 52, 1)
            self._text("Outputs OFF. Return to Advanced.", 16, 237, INK, 1)
            self._footer()
        elif screen == "calibration_confirm":
            self._header("CURRENT CALIBRATION")
            self._text("Remove DUT, loads and meter probes.", 16, 58, INK, 1)
            self._text("Leave BOTH shunts unbridged.", 16, 83, INK, 1)
            self._text("Both outputs must be open.", 16, 108, RED, 1)
            self._text("Drain baseline and both zero PWMs; Ig raw.", 16, 148, MUTED, 1)
            self._text("Zero means VDS = VGS = 0, relative to VS.", 16, 170, MUTED, 1)
            self._text("OK saves settings and starts calibration.", 16, 205, INK, 1)
            self._text("Old baseline is kept until the new one saves.", 16, 230, MUTED, 1)
            self._footer()
        elif screen == "calibration":
            self._header("CALIBRATING")
            self._text("Keep both outputs open", 16, 58, TEAL)
            self._text("Measuring the unloaded current baseline.", 16, 92, MUTED, 1)
            self._text("CANCEL stops outputs and restarts.", 16, 222, INK, 1)
            self._footer(busy=True)
            self._calibration_percent = -1
            self._calibration_phase = None
            self._calibration_channel = None
            self._render_calibration_progress()
        elif screen == "calibration_done":
            self._header("CALIBRATION SAVED", TEAL)
            self._text("Both current baselines saved", 16, 60, TEAL, 1)
            self._text("Outputs stopped. Binary baseline saved.", 16, 84, MUTED, 1)
            self._render_calibration_zeros()
            self._text("OK or CANCEL restarts to Home.", 16, 237, INK, 1)
            self._footer()
        elif screen == "calibration_error":
            self._header("CALIBRATION FAILED", RED)
            self._wrapped(self._calibration_error, 16, 62, 52, 1)
            self._text("Outputs stopped. OK returns to Home.", 16, 237, INK, 1)
            self._footer()
        elif screen.startswith("cloud"):
            self._render_cloud()
        elif screen.startswith("wifi"):
            self._render_wifi()
        elif screen == "averaging":
            self._header("SENSOR AVERAGING")
            self._text("Choose averages per measured point", 16, 51, MUTED, 1)
            for index, count in enumerate(model.INA_AVERAGE_COUNTS):
                selected = self.draft["ina_average_count"] == count
                self._button(str(count), 16 + (index % 4) * 114, 77 + (index // 4) * 54,
                             106, 46, ("ina_average", count),
                             TEAL if selected else WHITE, WHITE if selected else INK)
            self.d.line(16, 178, 464, 178, GRID)
            milliseconds = self.draft["ina_average_count"] * model.INA_CONVERSION_US * 2 / 1000
            self._text("Conversion: %.1f ms + settling" % milliseconds, 16, 192, TEAL, 1)
            self._text("More averages take longer", 16, 218, MUTED, 1)
            self._footer()
        elif screen == "edit":
            self._header(self.edit_label)
            self._render_edit_value()
            keys = (("7", "8", "9", "BACK"), ("4", "5", "6", "CLEAR"),
                    ("1", "2", "3", "+/-"),
                    ("0", ".", "", ""))
            for row, labels in enumerate(keys):
                for col, label in enumerate(labels):
                    if label:
                        self._button(label, 16 + col * 114, 94 + row * 43, 106, 37,
                                     ("key", label), scale=1 if len(label) > 3 else 2)
            self._footer()
        elif screen == "preparing":
            self._header("PREPARING SCAN")
            self._text("Preparing scan", 16, 72, TEAL)
            self._text("Preparing measurement.", 16, 116, MUTED, 1)
            self._text("Outputs have not started.", 16, 152, MUTED, 1)
            self._text("CANCEL discards and restarts.", 16, 210, INK, 1)
            self._footer(busy=True)
        elif screen == "scan":
            self._scan_header(force=True)
            self._footer(busy=True)
            self._plot_axes()
            self._plot_cursor = 0
            self._render_live()
        elif screen == "notice":
            self._header("NOTICE")
            self._wrapped(self.notice, 16, 62, 28, 2)
            self._footer()
        elif screen in ("upload", "upload_error"):
            self._render_upload_screen()
        elif screen == "scan_error":
            self._header("SCAN STOPPED", RED)
            self._wrapped(self.notice, 16, 64, 28, 2)
            self._footer()
        elif screen == "restarting":
            self._header("RESTARTING")
            self._text("Returning to Home", 16, 80)
        self.dirty = False

    def _render_calibration_progress(self):
        controller = self._calibration
        percent = max(0, min(100, int(controller.progress * 100))) if controller else 0
        phase = controller.phase if controller else "Preparing fixed buffers"
        channel = controller.channel if controller else ""
        if phase != self._calibration_phase or channel != self._calibration_channel:
            self.d.fill_rect(16, 127, 448, 16, BG)
            label = channel + " / " + phase if controller else phase
            self._text(label[:54], 16, 127, INK, 1)
            self._calibration_phase, self._calibration_channel = phase, channel
        if percent != self._calibration_percent:
            self.d.fill_rect(16, 160, 448, 42, BG)
            self._text("%d%%" % percent, 16, 160, TEAL)
            self.d.fill_rect(16, 191, 448, 5, GRID)
            self.d.fill_rect(16, 191, 448 * percent // 100, 5, TEAL)
            self._calibration_percent = percent
        self._calibration_render_ms = self._now_ms

    def _render_calibration_zeros(self):
        summary = self._calibration_summary
        if summary is None:
            return
        for role, label, y in (("drain", "Drain", 116), ("gate", "Gate", 167)):
            channel = summary[role]
            self._text("%s zero residual: %s V" %
                       (label, model.format_voltage_uv(channel["zero_residual_uv"])),
                       16, y, INK, 1)
            self._text("Zero trim captured" if channel["zero_valid"] else
                       "Closest only; zero trim not used", 16, y + 20,
                       TEAL if channel["zero_valid"] else AMBER, 1)
        self._text("Zero trim applies to scan commands.", 16, 216, MUTED, 1)

    def _calibration_failed(self, error):
        self._calibration_prepare = False
        self._record_error("current calibration", error)
        # Also covers construction/start/save failures outside the controller's
        # normal tick error path. Never draw an error before stopping outputs.
        try:
            self.engine.instrument.zero()
            self._outputs_safe = True
        except Exception as stop_error:
            self._outputs_safe = False
            self._record_error("calibration shutdown", stop_error)
            self._request_restart()
            return
        self._calibration_error = ("Not enough memory. Restart and retry calibration."
                                   if isinstance(error, MemoryError) else str(error))
        self.goto("calibration_error")

    def _tick_calibration(self, now_ms):
        try:
            if self._calibration_prepare:
                # User has explicitly confirmed the open-output fixture. Save
                # settings before any PWM; a failed commit cannot start a sweep.
                if not callable(self._calibration_factory):
                    raise RuntimeError("Calibration is unavailable in this build")
                self._pause_wifi()
                self.settings = self.store.save_settings(self._calibration_settings)
                self._calibration_settings = self.settings
                gc.collect()
                controller = self._calibration_factory(self.engine.instrument,
                                                        self._calibration_settings)
                self._calibration = controller
                controller.start()
                self._calibration_prepare = False
                self._outputs_safe = False
                return  # Give inputs another opportunity before the first drive.
            controller = self._calibration
            controller.tick(now_ms)
            if controller.state == "complete":
                self._outputs_safe = bool(controller.shutdown_succeeded)
                if not self._outputs_safe:
                    self._request_restart()
                    return
                # Nothing is committed before a complete, stopped sweep. File
                # staging/atomic rename belongs to the bounded binary writer.
                self._calibration_summary = controller.result.metadata(self._calibration_settings)
                controller.result.save(self._calibration_path)
                self.goto("calibration_done")
            elif controller.state == "cancelled":
                self._outputs_safe = bool(controller.shutdown_succeeded)
                self._request_restart()
            elif controller.state == "error":
                self._calibration_failed(controller.error or "Calibration did not complete")
        except (ValueError, OSError, RuntimeError, TypeError, KeyError, MemoryError) as error:
            self._calibration_failed(error)

    def _render_upload_screen(self):
        failed = self.screen == "upload_error"
        self._header("UPLOAD FAILED" if failed else "SAVING SCAN", RED if failed else INK)
        self._text("%d points | Outputs stopped%s" %
                   (len(self._scan_buffer), " | Partial scan" if self._scan_fault else ""),
                   16, 50, MUTED, 1)
        for index in range(4):
            self._text(UPLOAD_STAGES[index], 52, 76 + index * 35)
            self._upload_marks[index] = None
        self._upload_percent = None
        if failed:
            self._text("WiFi Error, Upload Failed", 16, 220, RED, 1)
            self._text("Please reconnect WiFi in settings and redo scan.", 16, 238, MUTED, 1)
        else:
            self._text("Sent is not saved until confirmation.", 16, 220, MUTED, 1)
            self._text("CANCEL discards; confirmed uploads restart.", 16, 238, MUTED, 1)
        self._footer(busy=not failed)
        self._render_upload_progress()

    def _render_upload_progress(self):
        state = self.cloud.state if self.cloud is not None else "error"
        fallback = 0 if state == "preparing" else (1 if state == "waiting_wifi" else
                   (4 if state == "complete" else 2))
        stage = getattr(self.cloud, "upload_stage", fallback)
        failed = self.screen == "upload_error" or state == "error"
        for index in range(4):
            mark = 2 if index < stage else (3 if failed and index == stage else
                                          (1 if index == stage else 0))
            if self._upload_marks[index] == mark:
                continue
            x, y = 20, 76 + index * 35
            self.d.fill_rect(x - 2, y - 2, 25, 22, BG)
            if mark == 2:
                # Vector symbols avoid unsupported Unicode font glyphs.
                self.d.line(x, y + 9, x + 6, y + 15, TEAL)
                self.d.line(x + 6, y + 15, x + 19, y + 1, TEAL)
            elif mark == 3:
                self.d.line(x + 2, y + 1, x + 17, y + 16, RED)
                self.d.line(x + 2, y + 16, x + 17, y + 1, RED)
            else:
                color = AMBER if mark == 1 else MUTED
                for offset in (0, 8, 16):
                    self.d.fill_rect(x + offset, y + 8, 3, 3, color)
            self._upload_marks[index] = mark
        total = getattr(self.cloud, "upload_total", 0)
        sent = getattr(self.cloud, "upload_sent", 0)
        percent = 100 if state == "complete" else (sent * 100 // total if total else 0)
        percent = max(0, min(100, percent))
        if self._upload_percent != percent:
            self.d.fill_rect(368, 146, 96, 16, BG)
            self._text("%d%%" % percent, 368, 146, INK, 2)
            self._upload_percent = percent

    def _render_edit_value(self):
        self.d.fill_rect(16, 49, 448, 37, WHITE)
        self._text(self.edit_value or "_", 28, 59, INK, bg=WHITE)
        self.edit_dirty = False

    def _stop_manual(self):
        if self._manual is None:
            return True
        try:
            self._manual.stop()
            self._outputs_safe = bool(self._manual.shutdown_succeeded)
        except Exception as error:
            self._outputs_safe = False
            self._record_error("manual shutdown", error)
        return self._outputs_safe

    def _manual_failed(self, error):
        # Never publish an OFF message until zeroing has actually succeeded.
        self._record_error("manual debug", error)
        if not self._stop_manual():
            self._request_restart()
            return
        self._manual_error = ("Not enough memory. Return and retry."
                              if isinstance(error, MemoryError) else str(error))
        self.goto("manual_error")

    def _open_manual(self):
        self._pause_cloud()
        self._pause_wifi()
        factory = self._manual_factory
        if factory is None:
            try:
                from .manual_debug import ManualDebug
            except ImportError:
                from manual_debug import ManualDebug
            factory = ManualDebug
        self._manual_session = True
        self._manual_cache = [None] * 6
        self._manual_render_ms = None
        self._manual_revision = -1
        self._manual_error = None
        self._manual = factory(self.engine.instrument, self.draft,
                               baseline=getattr(self.engine, "baseline", None))
        self._manual.set_targets(0, 0)
        self._return_manual()

    def _return_manual(self):
        # Entry and numeric CANCEL resume monitoring with both outputs off.
        self._manual_wait_tick = True
        self._manual.start()
        self._outputs_safe = bool(self._manual.shutdown_succeeded)
        if self._manual.state == "error":
            self._manual_failed(self._manual.error)
            return
        self.goto("manual")

    def _apply_manual(self):
        # Main-page OK and numeric OK share the same explicit apply path.
        # Do not tick using the UI timestamp captured before these I2C writes.
        self._manual_wait_tick = True
        self._manual.apply()
        self._outputs_safe = (not self._manual.outputs_enabled and
                              bool(self._manual.shutdown_succeeded))
        if self._manual.state == "error":
            self._manual_failed(self._manual.error)
        else:
            self.goto("manual")

    def _tick_manual(self, now_ms):
        try:
            self._manual.tick(now_ms)
            self._outputs_safe = (not self._manual.outputs_enabled and
                                  bool(self._manual.shutdown_succeeded))
            if self._manual.state == "error":
                self._manual_failed(self._manual.error)
        except Exception as error:
            self._manual_failed(error)

    def _render_manual_values(self, force=False):
        controller, cache = self._manual, self._manual_cache
        row = controller.latest
        for index, (field, x, y, multiplier) in enumerate((
                ("drain_bus_v", 16, 160, 1), ("gate_bus_v", 248, 160, 1),
                ("id_a", 16, 204, 1e6), ("ig_a", 248, 204, 1e6))):
            color = RED if index == 3 and row and abs(row[field]) > 1e-6 else INK
            changed_color = index == 3 and cache[5] != color
            if index == 3 and (force or changed_color):
                self._text("Ig (uA)", 248, 189, RED if color == RED else MUTED, 1)
                cache[5] = color
            value = "%.3f" % (row[field] * multiplier) if row else "--"
            if len(value) > 12:
                value = "%.5g" % (row[field] * multiplier)
            self._live_text(cache, index, value, x, y, 216, color,
                            force=force or changed_color)
        status = "ON | CANCEL stops" if controller.outputs_enabled else "OFF | OK applies targets"
        status += " | Id corr" if controller.correction_active else " | Id raw"
        self._live_text(cache, 4, status, 16, 250, 448,
                        TEAL if controller.outputs_enabled else RED, 1, force)
        self._manual_render_ms = self._now_ms
        self._manual_revision = controller.sample_revision

    def _acquiring(self):
        return (self._scan_prep != 0 or getattr(self.engine, "state", "idle") == "scanning"
                or self._manual_session
                or self._calibration_prepare or
                (self._calibration is not None and self._calibration.state == "running"))

    def _request_restart(self):
        if self._restart_pending or self._restart_called:
            return
        self._restart_pending = True
        if self._timing is not None:
            self._timing.stop()
        self._scan_prep = 0
        self._calibration_prepare = False
        if self._manual is not None:
            self._stop_manual()
        if self._calibration is not None and self._calibration.state == "running":
            try:
                self._calibration.cancel()
                self._outputs_safe = bool(self._calibration.shutdown_succeeded)
            except Exception as error:
                self._outputs_safe = False
                self._record_error("calibration cancel", error)
        if getattr(self.engine, "state", "idle") == "scanning":
            self.engine.cancel()
            self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
        if not self._outputs_safe:
            # Also covers an engine already in error after a failed shutdown.
            # Main retries hardware close and forced GPIO low before radio
            # cleanup. Do not delay that path to paint a status screen.
            self._finish_restart()
            return
        self.screen, self.dirty = "restarting", True
        # Stop the outputs first, then acknowledge CANCEL before filesystem,
        # transport or radio cleanup can block. The restart path bypasses the
        # normal end-of-tick render, so paint this frame here explicitly.
        try:
            self.render()
        except Exception:
            # A display/allocation failure must never prevent discard/reset.
            self.dirty = False
            self._record_error("restart display", "Could not draw restart screen")
        if self.cloud is not None:
            try:
                self.cloud.discard_scan()
            except Exception:
                # Restart expunges RAM even if closing the network reports an
                # error. Never print a transport exception containing its URL.
                self._record_error("scan discard", "Could not close upload")
        if self.wifi is not None:
            try:
                self.wifi.pause()
            except Exception:
                self._record_error("wifi shutdown", "Could not stop Wi-Fi")
        self.latest = None

    def _finish_restart(self):
        if self._restart_pending and not self._restart_called:
            self._restart_pending = False
            self._restart_called = True
            self._restart_callback()

    def _ram_hard_error(self, message):
        self._scan_prep = 0
        if getattr(self.engine, "state", "idle") == "scanning":
            self.engine.cancel()
            self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
        elif getattr(self.engine, "state", "idle") == "error":
            self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
        if not self._outputs_safe:
            self._request_restart()
            return
        if self.cloud is not None:
            try:
                self.cloud.pause()
            except Exception:
                pass
        self.notice = str(message)
        self.goto("scan_error")

    def _ram_upload_error(self):
        if self.cloud is not None:
            try:
                self.cloud.fail_upload()
            except Exception:
                pass
        self.notice = UPLOAD_FAILURE
        self.goto("upload_error")

    def _ram_prepare_tick(self):
        if self._scan_prep == 1:
            gc.collect()
            if self.cloud is None:
                raise RuntimeError("Cloud receiver is unavailable")
            self._ram_drain_count = len(model.sweep_uv(
                self.settings["drain_start_uv"], self.settings["drain_stop_uv"],
                self.settings["drain_step_uv"]))
            correction_provider = getattr(self.engine, "current_correction", None)
            correction = correction_provider(self.settings) if callable(correction_provider) else None
            if correction is None:
                self._cloud_call("begin_scan", "Scan could not be prepared.",
                                 self.settings, self._now_ms)
            else:
                self._cloud_call("begin_scan", "Scan could not be prepared.",
                                 self.settings, self._now_ms, current_correction=correction)
            self.clear_results()
            self._scan_prep = 2
        elif self._scan_prep == 2:
            self._pause_wifi()
            self._scan_prep = 3
        elif self._scan_prep == 3:
            self._arm_scan()
            self._outputs_safe = False
            self._scan_prep = 0
            self.goto("scan")

    def _ram_seal(self, fault=None):
        if not self._outputs_safe:
            self._ram_hard_error("Output shutdown failed")
            return
        if fault is not None and len(self._scan_buffer) == 0:
            # An empty overrange result is a diagnostic, not an upload. Use
            # only fixed fault fields here; never expose arbitrary exceptions.
            self._scan_fault = fault
            channel = fault.get("channel") if isinstance(fault, dict) else None
            kind = fault.get("kind") if isinstance(fault, dict) else None
            channel = channel.upper() if channel in ("drain", "gate") else "OUTPUT"
            if kind not in ("current_limit", "current_limit_alert", "shunt_saturation", "bus_saturation"):
                kind = "overrange"
            message = "%s %s. No measured points." % (channel, kind.replace("_", " "))
            if kind == "current_limit":
                measured, limit = fault.get("measured_value"), fault.get("limit_value")
                if (isinstance(measured, (int, float)) and not isinstance(measured, bool)
                        and isinstance(limit, (int, float)) and not isinstance(limit, bool)
                        and math.isfinite(measured) and math.isfinite(limit)
                        and abs(measured) <= 1 and 0 < limit <= 1):
                    message += " Measured %.3f mA; limit %.3f mA." % (measured * 1000, limit * 1000)
            self._record_error("acquisition", message)
            self._ram_hard_error(message)
            return
        self._cloud_call("finish_scan", "Scan could not be prepared for upload.", self._now_ms, fault)
        self._scan_fault = fault
        gc.collect()
        self.goto("upload")

    def _ram_upload_tick(self):
        state = self._cloud_status()["state"]
        if state == "error":
            self._ram_upload_error()
            return
        if state == "complete":
            if self._upload_complete_ms is None:
                self._upload_complete_ms = self._now_ms
            elif _ticks_diff(self._now_ms, self._upload_complete_ms) >= UPLOAD_DONE_MS:
                self._request_restart()
            return
        if state in ("waiting_wifi", "uploading"):
            try:
                if not self._upload_wifi_started:
                    self._upload_wifi_started = True
                    self._upload_connect_ms = self._now_ms
                    if self.wifi is None or not self.wifi.autoconnect(self._now_ms, persist=False):
                        self._ram_upload_error()
                        return
                    self._wifi_paused = False
                self.wifi.tick(self._now_ms)
            except Exception:
                self._record_error("wifi upload", "Wi-Fi connection failed")
                self._ram_upload_error()
                return
            wifi_state = self.wifi.state
            if (wifi_state not in ("connecting", "connected") or
                    (wifi_state == "connecting" and
                     _ticks_diff(self._now_ms, self._upload_connect_ms) >= 30000)):
                self._ram_upload_error()
                return
        online = self.wifi is not None and self.wifi.state == "connected"
        self._cloud_call("tick", "Upload failed.", self._now_ms, online=online)
        state = self._cloud_status()["state"]
        if state == "complete":
            self._upload_complete_ms = self._now_ms
        elif state == "error":
            self._ram_upload_error()

    def _tick_ram_scan(self, now_ms):
        self._now_ms = now_ms
        if self._restart_called:
            return
        if self._restart_pending:
            self._finish_restart()
            return
        # One event/transition per input opportunity. A newly armed scan gets
        # painted before its first drive, and upload work waits for its screen.
        screen_before, painted = self.screen, not self.dirty
        timing = self._timing
        if (timing is not None and not timing.active and self.screen == "scan"
                and self.engine.state == "scanning"):
            # Exclude preparation and the first full scan-screen paint.
            timing.begin_scan()
        started = timing.stamp() if timing is not None else None
        try:
            input_events = self.inputs.poll(now_ms)
        finally:
            if timing is not None:
                timing.add(scan_timing.INPUT, started)
        for event in input_events:
            if event[0] == "touch":
                for x, y, w, h, action in self.hits:
                    if x <= event[1] < x + w and y <= event[2] < y + h:
                        self.action(action)
                        break
            else:
                self.action(event[0])
            if self.dirty or self._restart_pending:
                break
        if self._restart_pending:
            self._finish_restart()
            return
        point_received = False
        try:
            if self.screen == "preparing" and painted:
                self._ram_prepare_tick()
            elif self.screen == "scan" and screen_before == "scan":
                events = self.engine.tick(now_ms)
                for kind, data in events:
                    self.latest = self.engine.latest
                    if kind == "point":
                        point_received = True
                        if timing is not None:
                            timing.phase(scan_timing.OTHER)
                        started = timing.stamp() if timing is not None else None
                        try:
                            self._cloud_call("record_point", "Measurement could not be kept in RAM.", data)
                        finally:
                            if timing is not None:
                                timing.add(scan_timing.RECORD, started)
                        self._add_point(data)
                        if timing is not None:
                            timing.point(data["index"], data["curve"])
                    elif kind in ("complete", "partial", "error", "cancelled"):
                        self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
                        if kind in ("complete", "partial"):
                            started = timing.stamp() if timing is not None else None
                            try:
                                self._ram_seal(data if kind == "partial" else None)
                            finally:
                                if timing is not None:
                                    timing.add(scan_timing.RECORD, started)
                        elif kind == "cancelled":
                            self._request_restart()
                        else:
                            self._record_error("acquisition", data)
                            self._ram_hard_error(data)
                    self.live_dirty = True
            elif self.screen == "upload" and screen_before == "upload" and painted:
                self._ram_upload_tick()
        except (ValueError, OSError, RuntimeError, TypeError, KeyError, MemoryError) as error:
            self._record_error("scan flow", error)
            if self.screen == "upload":
                self._ram_upload_error()
            else:
                self._ram_hard_error("Scan stopped. " + str(error))
        if self._restart_pending:
            self._finish_restart()
            return
        if self.dirty:
            self._timed_render(self.render)
        elif self.screen == "upload":
            # Recompute after every bounded transport step. Only changed
            # symbols/percentage touch the LCD; no full screen redraw per chunk.
            self._timed_render(self._render_upload_progress)
        if self.screen == "scan":
            elapsed = 1000 if self._last_live_ms is None else _ticks_diff(now_ms, self._last_live_ms)
            # A point is retained immediately, but drawing waits until the next
            # tick has driven the next voltage. Display work then overlaps that
            # output's settling window instead of extending every point cycle.
            # Leave conversion polls and cancellation opportunities responsive.
            if not point_received and self.engine.waiting_for_settle:
                if self.live_dirty and elapsed >= 150:
                    self._timed_render(self._render_live)
                    self._last_live_ms = now_ms
                self._timed_render(self._draw_points)
        if timing is not None:
            timing.finish_point(plotted=self._plot_cursor)
            if self.screen != "scan":
                timing.stop()

    def _timed_render(self, draw):
        timing = self._timing
        started = timing.stamp() if timing is not None else None
        try:
            return draw()
        finally:
            if timing is not None:
                timing.add(scan_timing.RENDER, started)

    def _queue_scan(self):
        if self._acquiring():
            return
        self._scan_path = True
        self._scan_prep = 1
        self.goto("preparing")

    def _pause_wifi(self):
        if self.wifi is not None:
            stopped = False
            try:
                stopped = self.wifi.pause() is True
            except Exception:
                pass
            # Raise outside the handler: traceback chaining must not retain
            # radio exceptions that could include credentials.
            if not stopped:
                raise RuntimeError("Wi-Fi could not be disabled")
            self._wifi_paused = True

    def _cloud_call(self, operation, message, *args, **kwargs):
        try:
            return getattr(self.cloud, operation)(*args, **kwargs)
        except Exception as error:
            # Receiver URLs are bearer credentials. No driver exception or
            # action payload may flow into a notice, traceback or error log.
            if operation == "save_config" and str(error) in (
                    "Enter a published Google Apps Script URL",
                    "Use the published Google Apps Script URL ending in /exec",
                    "Use a published Google Apps Script URL without query parameters",
                    "Invalid cloud upload setting"):
                message = str(error)
        # Raise outside the handler so host traceback printers cannot chain
        # an underlying exception containing the private receiver URL.
        raise RuntimeError(message)

    def _pause_cloud(self):
        if self.cloud is not None:
            if self._cloud_call("pause", "Cloud upload could not be stopped.") is not True:
                raise RuntimeError("Cloud upload could not be stopped.")

    def _cloud_status(self):
        if self.cloud is None:
            return {"state": "unavailable", "message": "Cloud saving is unavailable",
                    "configured": False, "enabled": False}
        status = self.cloud.status
        if self._cloud_error:
            status = dict(status)
            status["message"] = self._cloud_error
        return status

    def _render_text_keys(self, mode, prefix):
        for row, keys in enumerate(WIFI_KEYS[mode]):
            offset = (10 - len(keys)) * 47 // 2
            for col, char in enumerate(keys):
                self._button(char, 8 + offset + col * 47, 106 + row * 37, 42, 32,
                             (prefix + "_char", char))
        alpha = "abc" if mode == "ABC" else "ABC"
        for index, (label, action) in enumerate(((alpha, (prefix + "_mode", alpha)),
                ("123", (prefix + "_mode", "123")), ("#+=", (prefix + "_mode", "#+=")),
                ("SPACE", (prefix + "_char", " ")), ("<-", prefix + "_backspace"))):
            self._button(label, 8 + index * 94, 224, 88, 32, action, scale=1)

    def _render_cloud_value(self):
        self.d.fill_rect(16, 64, 240, 32, WHITE)
        value = self._cloud_url_edit if self._cloud_show else "*" * min(256, len(self._cloud_url_edit))
        if len(value) > 14:
            value = "<" + value[-13:]
        self._text(value or "_", 24, 72, bg=WHITE)
        self._cloud_value_dirty = False

    def _render_cloud(self):
        if self.screen == "cloud":
            self._header("CLOUD RECEIVER")
            self._text("Each scan uploads automatically.", 16, 64, MUTED, 1)
            self._button("RECEIVER URL: " + ("SET" if self._cloud_url_draft else "NOT SET"),
                         16, 112, 448, 42, "cloud_url")
            self._text("Google Apps Script", 16, 188, MUTED, 1)
            self._text("OK saves / CANCEL discards", 16, 236, MUTED, 1)
            self._footer()
        elif self.screen == "cloud_url":
            self._header("RECEIVER URL")
            self._text("Provision over USB, or enter the full URL", 16, 47, MUTED, 1)
            self._render_cloud_value()
            self._button("HIDE" if self._cloud_show else "SHOW", 272, 64, 88, 32, "cloud_show", scale=1)
            self._button("CLEAR", 368, 64, 96, 32, "cloud_clear", scale=1)
            self._render_text_keys(self._cloud_keyboard, "cloud")
            self._footer()

    def _cloud_action(self, action):
        if self._acquiring():
            return
        if self.cloud is None:
            self.message("Cloud saving is unavailable", "advanced")
            return
        screen = self.screen
        if isinstance(action, tuple):
            kind, value = action
            if kind == "cloud_char" and screen == "cloud_url":
                if len(value) == 1 and 32 <= ord(value) < 127 and len(self._cloud_url_edit) < 256:
                    self._cloud_url_edit += value
                    self._cloud_value_dirty = True
            elif kind == "cloud_mode" and screen == "cloud_url" and value in WIFI_KEYS:
                self._cloud_keyboard = value
                self.dirty = True
            return
        if action == "cloud" and screen == "advanced":
            config = self._cloud_call("config_for_edit", "Cloud settings could not be opened.")
            self._cloud_url_draft = config["url"]
            self.goto("cloud")
        elif screen == "cloud":
            # Editing or leaving this page must close TLS before drawing a
            # different view can retain objects in its temporary heap region.
            self._pause_cloud()
            if action == "left":
                self.goto("advanced")
            elif action == "right":
                self._cloud_call("save_config", "Cloud settings could not be saved. Use a valid Apps Script /exec URL.",
                                 self._cloud_url_draft, enabled=True)
                self._cloud_error = None
                self.goto("advanced")
            elif action == "cloud_url":
                self._cloud_url_edit = self._cloud_url_draft
                self._cloud_show, self._cloud_keyboard = False, "abc"
                self.goto("cloud_url")
        elif screen == "cloud_url":
            if action == "left":
                self.goto("cloud")
            elif action == "right":
                self._cloud_url_draft = self._cloud_url_edit
                self.goto("cloud")
            elif action == "cloud_show":
                self._cloud_show = not self._cloud_show
                self.dirty = True
            elif action == "cloud_clear":
                self._cloud_url_edit = ""
                self._cloud_value_dirty = True
            elif action == "cloud_backspace":
                self._cloud_url_edit = self._cloud_url_edit[:-1]
                self._cloud_value_dirty = True

    def _wifi_status(self):
        if self.wifi is None:
            return {"state": "off", "ssid": "", "ip": "", "saved_count": 0,
                    "message": "Wi-Fi is unavailable"}
        status = self.wifi.status
        if self._wifi_error:
            status = dict(status)
            status["message"] = self._wifi_error
        return status

    @staticmethod
    def _wifi_name(value, limit=32):
        # Display's bitmap font is ASCII; preserve the real SSID for connection.
        return "".join(char if 32 <= ord(char) < 127 else "?" for char in value)[:limit]

    def _wifi_call(self, operation, *args, **kwargs):
        try:
            if operation in ("scan", "connect", "cancel", "forget"):
                self._pause_cloud()
            return getattr(self.wifi, operation)(*args, **kwargs)
        except Exception:
            self._wifi_error = "Wi-Fi operation failed"
            self._record_error("wifi " + operation, self._wifi_error)
            return None

    def _wifi_connect(self, use_saved=False):
        if self._wifi_paused:
            self.message("Wi-Fi remains off because outputs could not be stopped.", "wifi")
            return
        selected = self._wifi_selected
        if not selected or not selected.get("supported", False):
            return
        self._wifi_error = None
        started = self._wifi_call("connect", selected["ssid"],
                                  password=self._wifi_password,
                                  use_saved=use_saved, now_ms=self._now_ms)
        # goto clears the plaintext candidate and resets SHOW even on failure.
        self.goto("wifi_status")
        # A failed new join may be restoring the previous saved connection.
        self._wifi_connect_failed = not started and self.wifi.state != "connecting"

    def _render_wifi_value(self):
        self.d.fill_rect(16, 64, 336, 32, WHITE)
        value = self._wifi_password if self._wifi_show else "*" * len(self._wifi_password)
        if len(value) > 20:
            value = "<" + value[-19:]
        self._text(value or "_", 24, 72, bg=WHITE)
        self._wifi_value_dirty = False

    def _render_wifi(self):
        screen = self.screen
        status = self._wifi_status()
        if screen == "wifi":
            self._header("WI-FI")
            self._text(status["state"].upper(), 16, 54, TEAL)
            self._text(self._wifi_name(status["ssid"], 28) or "No network selected", 16, 82)
            self._text("IP " + status["ip"] if status["ip"] else status["message"], 16, 112, MUTED, 1)
            connecting = status["state"] == "connecting"
            self._button("FIND NETWORKS", 16, 141, 272, 42,
                         None if connecting else "wifi_scan", TEAL, WHITE)
            self._button("SAVED (%d)" % status["saved_count"], 304, 141, 160, 42,
                         "wifi_saved", scale=1)
            self._button("CANCEL CONNECTION" if connecting else "DISCONNECT", 16, 192, 272, 34,
                         "wifi_disconnect", scale=1)
            self._text("Saved only after successful connection", 16, 239, MUTED, 1)
            self._text("Wi-Fi changes apply now, outside scan settings", 16, 255, MUTED, 1)
            self._footer()
        elif screen == "wifi_search":
            self._header("FINDING NETWORKS")
            self._text("Searching...", 16, 75, TEAL)
            self._text("May take up to 10 seconds.", 16, 119)
            self._text("Buttons respond when the search returns.", 16, 157, MUTED, 1)
            self._footer(busy=True)
        elif screen == "wifi_list":
            self._header("SAVED NETWORKS" if self._wifi_saved_list else "NEARBY NETWORKS")
            count = len(self._wifi_rows)
            start = self._wifi_page * 4
            if not count:
                self._text("No saved networks" if self._wifi_saved_list else "No networks found", 16, 75)
                self._wrapped(status["message"], 16, 113, 52, 1)
            for index in range(start, min(start + 4, count)):
                row = self._wifi_rows[index]
                y = 48 + (index - start) * 43
                self._button("", 16, y, 448, 38, ("wifi_select", index))
                self._text(self._wifi_name(row["ssid"], 26), 24, y + 4, scale=1, bg=WHITE)
                detail = ("LOCK " if row["requires_password"] else "OPEN ") + row["security_label"]
                if row["saved"]:
                    detail += " | SAVED"
                self._text(detail, 24, y + 23, MUTED, 1, WHITE)
                if row["rssi"] is not None:
                    self._text("%d dBm" % row["rssi"], 396, y + 6, MUTED, 1, WHITE)
            pages = max(1, (count + 3) // 4)
            self._button("PREV", 16, 229, 112, 32, "wifi_prev" if self._wifi_page else None, scale=1)
            self._text("%d / %d" % (self._wifi_page + 1, pages), 210, 240, MUTED, 1)
            self._button("NEXT", 352, 229, 112, 32,
                         "wifi_next" if self._wifi_page + 1 < pages else None, scale=1)
            self._footer()
        elif screen in ("wifi_network", "wifi_forget"):
            row = self._wifi_selected
            self._header("FORGET NETWORK?" if screen == "wifi_forget" else "NETWORK")
            name = self._wifi_name(row["ssid"])
            self._text(name, 16, 58, scale=2 if len(name) <= 28 else 1)
            self._text(row["security_label"], 16, 88, MUTED, 1)
            if screen == "wifi_forget":
                self._text("OK removes this saved password.", 16, 136, scale=1)
                self._text("An active connection will disconnect.", 16, 166, MUTED, 1)
            elif not row["supported"]:
                self._text("Network security is unsupported.", 16, 136, AMBER, 1)
            else:
                saved = self.wifi.has_saved(row["ssid"])
                if saved:
                    self._button("USE SAVED PW" if row["requires_password"] else "CONNECT SAVED",
                                 16, 112, 448, 42, "wifi_use_saved", TEAL, WHITE)
                label = "ENTER PASSWORD" if row["requires_password"] else "CONNECT"
                self._button(label, 16, 164 if saved else 112, 448, 42,
                             "wifi_password" if row["requires_password"] else "wifi_connect")
                if saved:
                    self._button("FORGET SAVED", 16, 217, 160, 30, "wifi_forget", scale=1)
                hint = "OK: saved password" if row["requires_password"] else "OK: connect saved"
                self._text(hint if saved else "OK: " + label.lower(), 200 if saved else 16, 229, MUTED, 1)
                self._text("Connect now; save only after success", 16, 256, MUTED, 1)
            self._footer(busy=screen == "wifi_network" and not row["supported"])
        elif screen == "wifi_password":
            self._header("WI-FI PASSWORD")
            self._text(self._wifi_name(self._wifi_selected["ssid"]), 16, 47, MUTED, 1)
            self._render_wifi_value()
            self._button("HIDE" if self._wifi_show else "SHOW", 368, 64, 96, 32, "wifi_show", scale=1)
            self._render_text_keys(self._wifi_keyboard, "wifi")
            self._footer()
        elif screen == "wifi_status":
            connecting = status["state"] == "connecting" and not self._wifi_connect_failed
            self._header("CONNECTING" if connecting else ("CANNOT CONNECT" if self._wifi_connect_failed else "WI-FI STATUS"))
            name = self._wifi_selected["ssid"] if self._wifi_connect_failed else (status["ssid"] or self._wifi_selected["ssid"])
            self._text(self._wifi_name(name, 28), 16, 64)
            self._wrapped(status["message"], 16, 105, 28, 2)
            if status["ip"] and not self._wifi_connect_failed:
                self._text("IP " + status["ip"], 16, 209, TEAL)
            self._text("CANCEL stops this connection" if connecting else "Wi-Fi changes are already applied", 16, 250, MUTED, 1)
            self._footer(busy=connecting)

    def _wifi_action(self, action):
        if self._acquiring():
            return  # Ignore stale network hit targets during acquisition.
        if self.wifi is None:
            self.message("Wi-Fi is unavailable", "advanced")
            return
        screen = self.screen
        if isinstance(action, tuple):
            kind, value = action
            if kind == "wifi_select" and screen == "wifi_list":
                if 0 <= value < len(self._wifi_rows):
                    self._wifi_selected = self._wifi_rows[value]
                    self.goto("wifi_network")
            elif kind == "wifi_char" and screen == "wifi_password":
                if len(value) == 1 and 32 <= ord(value) < 127 and len(self._wifi_password) < 64:
                    self._wifi_password += value
                    self._wifi_value_dirty = True
            elif kind == "wifi_mode" and screen == "wifi_password" and value in WIFI_KEYS:
                self._wifi_keyboard = value
                self.dirty = True
            return
        if action == "wifi" and screen == "advanced":
            self.goto("wifi")
        elif screen == "wifi":
            if action in ("left", "right"):
                if action == "left" and self.wifi.state == "connecting":
                    self._wifi_call("cancel")
                self.goto("advanced")
            elif action == "wifi_disconnect":
                self._wifi_error = None
                self._wifi_call("cancel")
                self.dirty = True
            elif action == "wifi_scan" and self.wifi.state != "connecting":
                if self._wifi_paused:
                    self.message("Wi-Fi remains off because outputs could not be stopped.", "wifi")
                    return
                self._wifi_error = None
                self._wifi_search_pending = True
                self.goto("wifi_search")
            elif action == "wifi_saved":
                self._wifi_rows = self._wifi_call("saved_networks") or []
                self._wifi_saved_list, self._wifi_page = True, 0
                self.goto("wifi_list")
        elif screen == "wifi_search":
            if action == "left":
                self.goto("wifi")
        elif screen == "wifi_list":
            if action in ("left", "right"):
                self.goto("wifi")
            elif action == "wifi_prev" and self._wifi_page:
                self._wifi_page -= 1
                self.dirty = True
            elif action == "wifi_next" and (self._wifi_page + 1) * 4 < len(self._wifi_rows):
                self._wifi_page += 1
                self.dirty = True
        elif screen == "wifi_network":
            row = self._wifi_selected
            if action == "left":
                self.goto("wifi_list")
            elif action == "wifi_forget":
                self.goto("wifi_forget")
            elif action in ("wifi_use_saved", "wifi_connect", "wifi_password", "right") and row["supported"]:
                if action == "wifi_use_saved" or (action == "right" and self.wifi.has_saved(row["ssid"])):
                    self._wifi_connect(use_saved=True)
                elif action == "wifi_password" or row["requires_password"]:
                    self._wifi_password, self._wifi_show, self._wifi_keyboard = "", False, "abc"
                    self.goto("wifi_password")
                else:
                    self._wifi_connect()
        elif screen == "wifi_password":
            if action == "left":
                self.goto("wifi_network")
            elif action == "right":
                self._wifi_connect()
            elif action == "wifi_show":
                self._wifi_show = not self._wifi_show
                self.dirty = True
            elif action == "wifi_backspace":
                self._wifi_password = self._wifi_password[:-1]
                self._wifi_value_dirty = True
        elif screen == "wifi_status":
            status = self._wifi_status()
            connecting = status["state"] == "connecting" and not self._wifi_connect_failed
            if action == "left":
                if connecting:
                    self._wifi_call("cancel")
                self.goto("wifi")
            elif action == "right" and not connecting:
                self.goto("wifi" if status["state"] == "connected" else "wifi_network")
        elif screen == "wifi_forget":
            if action == "left":
                self.goto("wifi_network")
            elif action == "right":
                if self._wifi_call("forget", self._wifi_selected["ssid"]):
                    self._wifi_selected["saved"] = False
                    if self._wifi_saved_list:
                        self._wifi_rows = self._wifi_call("saved_networks") or []
                        self._wifi_page = 0
                    self.goto("wifi_list")
                else:
                    self._wifi_connect_failed = True
                    self.goto("wifi_status")

    def _tick_wifi(self, now_ms, search_ready):
        if not self._settings_path:
            return
        if self.wifi is None or self._acquiring() or self._wifi_paused:
            return
        if search_ready and self._wifi_search_pending and self.screen == "wifi_search":
            self._wifi_search_pending = False
            self._wifi_rows = self._wifi_call("scan") or []
            self._wifi_saved_list, self._wifi_page = False, 0
            self.goto("wifi_list")
        self._wifi_call("tick", now_ms)
        if self.screen in ("wifi", "wifi_status"):
            status = self._wifi_status()
            key = (status["state"], status["ssid"], status["ip"], status["message"], status["saved_count"])
            if key != self._wifi_status_key:
                self._wifi_status_key = key
                self.dirty = True

    def _wrapped(self, message, x, y, width, scale):
        line = ""
        for word in str(message).split():
            while len(word) > width:
                if line:
                    self._text(line, x, y, scale=scale)
                    y += 24
                    line = ""
                self._text(word[:width], x, y, scale=scale)
                y += 24
                word = word[width:]
            if len(line) + len(word) + 1 > width:
                self._text(line, x, y, scale=scale)
                y += 24
                line = ""
            line = (line + " " + word).strip()
            if y > 232:
                break
        if line and y <= 232:
            self._text(line, x, y, scale=scale)

    def _plot_axes(self):
        self.d.fill_rect(0, 42, 330, 200, BG)
        self.d.fill_rect(50, 64, 270, 151, WHITE)
        for index in range(4):
            self.d.line(50, 64 + index * 50, 319, 64 + index * 50, GRID)
        self.d.line(50, 64, 50, 215, INK)
        self.d.line(50, 215, 319, 215, INK)
        self._text("Id (uA)", 16, 47, MUTED, 1)
        self._text(("%.0f" if abs(self._plot_ymax) >= 10 else "%.1f") % self._plot_ymax, 0, 66, MUTED, 1)
        self._text(("%.0f" if abs(self._plot_ymin) >= 10 else "%.1f") % self._plot_ymin, 0, 205, MUTED, 1)
        self._text("%.3g" % self._plot_xmin, 50, 224, MUTED, 1)
        right_label = "%.3g" % self._plot_xmax
        self._text(right_label, 320 - len(right_label) * 8, 224, MUTED, 1)
        self._text("VDS (V)", 150, 224, MUTED, 1)
        self._plot_axes_dirty = False

    def _xy(self, index):
        start = self._plot_xmin
        span = self._plot_xmax - start
        x = 50 + int((self.rows[index * 2] - start) / (span or 1) * 269)
        y = 214 - int((self.rows[index * 2 + 1] * 1e6 - self._plot_ymin) /
                     (self._plot_ymax - self._plot_ymin) * 149)
        return min(319, max(51, x)), min(214, max(65, y))

    def _draw_points(self):
        # Bound drawing work so buttons are polled between plot segments.
        if self._plot_axes_dirty:
            self._plot_axes()
            self._plot_cursor = 0
        end = min(len(self.curves), self._plot_cursor + 4)
        for index in range(self._plot_cursor, end):
            x, y = self._xy(index)
            color = CURVES[self.curves[index] % len(CURVES)]
            if index and self.curves[index] == self.curves[index - 1]:
                px, py = self._xy(index - 1)
                self.d.line(px, py, x, y, color)
            self.d.fill_rect(x - 1, y - 1, 3, 3, color)
        self._plot_cursor = end

    def _add_point(self, row):
        redraw = False
        # Snap beyond the measurement, leaving room for ordinary millivolt
        # scatter around a requested endpoint instead of clearing every dot.
        x_step = max(0.05, min(model.volts_from_uv(self.settings["drain_step_uv"]),
                               (self._plot_xmax - self._plot_xmin) * 0.1))
        if row["vds"] < self._plot_xmin:
            self._plot_xmin = math.floor((row["vds"] - x_step * 0.5) / x_step) * x_step
            redraw = True
        if row["vds"] > self._plot_xmax:
            self._plot_xmax = math.ceil((row["vds"] + x_step * 0.5) / x_step) * x_step
            redraw = True
        current = row["id_a"] * 1e6
        if current < self._plot_ymin or current > self._plot_ymax:
            margin = max(1.0, abs(current) * 0.5)
            self._plot_ymin = min(self._plot_ymin, current - margin)
            self._plot_ymax = max(self._plot_ymax, current + margin)
            redraw = True
        if redraw:
            # Acquisition must never wait on an LCD transfer. The scheduler
            # consumes this work while the next output is settling.
            self._plot_axes_dirty = True
            self._plot_cursor = 0

    def _live_text(self, cache, index, value, x, y, width, color=INK, scale=2, force=False):
        if not force and cache[index] == value:
            return
        # Clear the entire bounded field so a shorter replacement cannot leave
        # trailing digits behind. Publish the cache only after drawing succeeds.
        self.d.fill_rect(x, y, width, 8 * scale, BG)
        if value:
            self._text(value, x, y, color, scale)
        cache[index] = value

    def _live_static(self):
        if self._live_screen == self.screen:
            return
        self._reset_live_cache()
        self.d.fill_rect(334, 46, 146, 193, BG)
        self.d.fill_rect(16, 263, 448, 4, GRID)
        self._live_misc[1] = 0
        self._live_screen = self.screen

    def _render_live(self):
        progress = self.engine.progress
        self._live_static()
        self._scan_header()
        row = self.latest
        for index, (label, field, multiplier, y) in enumerate((
                ("VDS  V", "vds", 1, 52), ("VGS  V", "vgs", 1, 95),
                ("Id  uA", "id_a", 1e6, 138), ("Ig  uA", "ig_a", 1e6, 181))):
            label_color = RED if index == 3 and row and abs(row[field]) > 1e-6 else MUTED
            color_changed = self._live_label_colors[index] != label_color
            if color_changed:
                self._text(label, 338, y, label_color, 1)
                self._live_label_colors[index] = label_color
            value = "%.3f" % (row[field] * multiplier) if row else "--"
            if len(value) > 8:
                value = "%.4g" % (row[field] * multiplier)
            # A threshold crossing can keep the same rounded text ("1.000").
            self._live_text(self._live_values, index, value, 338, y + 15, 142,
                            RED if label_color == RED else INK, force=color_changed)
            if index < 2:
                requested_key = "requested_" + field
                value = "SET %.3g V" % row[requested_key] if row and requested_key in row else ""
                self._live_text(self._live_requests, index, value, 338, y + 33, 142, MUTED, 1)
        caption = "%d points | %d%%" % (len(self.curves), int(progress * 100))
        if row:
            caption += " | INA avg %d" % self.settings["ina_average_count"]
        self._live_text(self._live_misc, 0, caption, 16, 248, 448, MUTED, 1)
        width = max(0, min(448, int(448 * progress)))
        previous = self._live_misc[1]
        if width > previous:
            self.d.fill_rect(16 + previous, 263, width - previous, 4, TEAL)
        elif width < previous:
            self.d.fill_rect(16 + width, 263, previous - width, 4, GRID)
        self._live_misc[1] = width
        self.live_dirty = False

    def _save_draft(self, exiting=False):
        self.settings = self.store.save_settings(self.draft)
        # RAM Settings exits immediately reboot. Avoid an allocation after
        # the commit that could misleadingly report a successful save as failed.
        if not exiting:
            self.draft = dict(self.settings)

    def action(self, action):
        try:
            self._action(action)
        except (ValueError, OSError, RuntimeError, TypeError, KeyError, MemoryError) as error:
            self._record_error("ui action: " + self.screen, error)
            message = "Not enough memory. Please retry." if isinstance(error, MemoryError) else str(error)
            if self._manual_session and self.screen == "manual":
                self._manual_failed(error)
            elif self._manual_session and not self._stop_manual():
                self._request_restart()
            elif self._scan_path:
                self._ram_hard_error(message)
            else:
                self.message(message, "home" if self.screen == "preparing" else None)

    def _action(self, action):
        if self._restart_pending or self._restart_called:
            return
        if self._scan_path:
            if (action == "left" or
                    (action == "right" and self.screen in ("upload_error", "scan_error"))):
                self._request_restart()
            return
        if self.screen == "calibration":
            if action == "left":
                self._request_restart()
            return
        if self.screen in ("calibration_done", "calibration_error"):
            if action in ("left", "right"):
                self._request_restart()
            return
        if self.screen == "manual_error":
            if action in ("left", "right"):
                self.goto("advanced")
            return
        if self.screen == "manual":
            if action == "left":
                self.goto("advanced")
            elif action == "right":
                self._apply_manual()
            elif isinstance(action, tuple) and action[0] == "manual_edit":
                index = action[1]
                if index not in (0, 1):
                    raise ValueError("Unknown manual channel")
                self.edit_key = index
                self.edit_label = "Drain target (V to GND)" if index == 0 else "Gate target (V to GND)"
                self.edit_return = "manual"
                self.edit_value = model.format_voltage_uv(self._manual.targets_uv[index])
                self.edit_fresh = True
                self.goto("edit")
            return
        wifi_action = action[0] if isinstance(action, tuple) else action
        if self.screen.startswith("cloud") or wifi_action.startswith("cloud"):
            self._cloud_action(action)
            return
        if self.screen.startswith("wifi") or wifi_action.startswith("wifi"):
            self._wifi_action(action)
            return
        if isinstance(action, tuple):
            if action[0] == "ina_average" and self.screen == "averaging":
                count = action[1]
                if isinstance(count, bool) or not isinstance(count, int) or count not in model.INA_AVERAGE_COUNTS:
                    raise ValueError("Unsupported sensor average count")
                self.draft["ina_average_count"] = count
                self.dirty = True
            elif action[0] == "edit":
                self.edit_key, self.edit_label = action[1:]
                self.edit_return = self.screen
                self.edit_value = model.format_voltage_uv(self.draft[self.edit_key])
                self.edit_fresh = True
                self.goto("edit")
            elif action[0] == "key":
                key = action[1]
                if key == "CLEAR":
                    self.edit_value = ""
                elif key == "BACK":
                    self.edit_value = self.edit_value[:-1]
                elif key == "+/-":
                    self.edit_value = self.edit_value[1:] if self.edit_value.startswith("-") else "-" + self.edit_value
                else:
                    if self.edit_fresh:
                        self.edit_value = ""
                    if len(self.edit_value) < 12 and not (key == "." and "." in self.edit_value):
                        self.edit_value += key
                self.edit_fresh = False
                self.edit_dirty = True
            return
        screen = self.screen
        if screen == "home":
            if action == "left":
                self._pause_cloud()
                self._settings_path = True
                # Settings exits reboot, so its editors may reuse the TLS reserve.
                tls_memory.release()
                if self.wifi is not None:
                    self.wifi.resume(self._now_ms)
                    self._wifi_paused = False
                self.draft = dict(self.settings)
                self.goto("settings")
            elif action == "right":
                self._queue_scan()
        elif screen == "settings":
            if action == "left":
                self.goto("home")
            elif action == "right":
                self._save_draft(exiting=True)
                self.goto("home")
            elif action == "advanced":
                self.advanced_entry = dict(self.draft)
                self.goto("advanced")
        elif screen == "advanced":
            if action == "left":
                self.draft = dict(self.advanced_entry)
                self.goto("settings")
            elif action == "right":
                self.goto("settings")
            elif action == "averaging":
                self.averaging_entry = dict(self.draft)
                self.goto("averaging")
            elif action == "calibrate":
                self.goto("calibration_confirm")
            elif action == "manual":
                self._open_manual()
        elif screen == "calibration_confirm":
            if action == "left":
                self.goto("advanced")
            elif action == "right":
                self._calibration_settings = model.validate_settings(self.draft)
                self._calibration_prepare = True
                self._calibration = None
                self._calibration_summary = None
                self._calibration_error = None
                self.goto("calibration")
        elif screen == "averaging":
            if action == "left":
                self.draft = dict(self.averaging_entry)
                self.goto("advanced")
            elif action == "right":
                self.goto("advanced")
        elif screen == "edit":
            if action == "left":
                if self.edit_return == "manual":
                    self._return_manual()
                else:
                    self.goto(self.edit_return)
            elif action == "right":
                value = model.parse_voltage_uv(self.edit_value)
                if self.edit_return == "manual":
                    drain, gate = self._manual.targets_uv
                    self._manual.set_targets(value if self.edit_key == 0 else drain,
                                             value if self.edit_key == 1 else gate)
                    self._apply_manual()
                else:
                    self.draft[self.edit_key] = value
                    self.goto(self.edit_return)
        elif screen == "notice":
            if action in ("left", "right"):
                self.goto(self.notice_return)

    def tick(self, now_ms):
        if self._scan_path or self._restart_pending or self._restart_called:
            self._tick_ram_scan(now_ms)
            return
        self._now_ms = now_ms
        # Paint network search before its synchronous scan; allow CANCEL first.
        search_ready = self.screen == "wifi_search" and not self.dirty
        calibration_ready = self.screen == "calibration" and not self.dirty
        for event in self.inputs.poll(now_ms):
            if event[0] == "touch":
                for x, y, w, h, action in self.hits:
                    if x <= event[1] < x + w and y <= event[2] < y + h:
                        self.action(action)
                        break
            else:
                self.action(event[0])
            if self.dirty or self._restart_pending:
                break
        if self._restart_pending:
            self._finish_restart()
            return
        if self.screen == "manual":
            # start/apply snapshot the clock after this loop's now_ms was
            # captured. Imports and I2C work can make that timestamp stale.
            # The next loop supplies a fresh time and allows CANCEL first.
            if self._manual_wait_tick:
                self._manual_wait_tick = False
            else:
                self._tick_manual(now_ms)
        elif self.screen == "calibration":
            if calibration_ready:
                self._tick_calibration(now_ms)
        else:
            self._tick_wifi(now_ms, search_ready)
        if self._restart_pending:
            self._finish_restart()
            return
        if self.dirty:
            try:
                self.render()
            except Exception:
                if self._manual_session:
                    self._stop_manual()
                raise
        elif (self.screen == "manual" and
              (self._manual_render_ms is None or
               _ticks_diff(now_ms, self._manual_render_ms) >= 200)):
            try:
                self._render_manual_values()
            except Exception:
                self._stop_manual()
                raise
        elif (self.screen == "calibration" and
              (self._calibration_render_ms is None or
               _ticks_diff(now_ms, self._calibration_render_ms) >= 100)):
            self._render_calibration_progress()
        elif self.screen == "edit" and self.edit_dirty:
            self._render_edit_value()
        elif self.screen == "wifi_password" and self._wifi_value_dirty:
            self._render_wifi_value()
        elif self.screen == "cloud_url" and self._cloud_value_dirty:
            self._render_cloud_value()
