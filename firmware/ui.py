"""480x320 touchscreen application; hardware is injected for host-side QA."""

import struct
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
        return self.app._scan_buffer[index][4]

    def clear(self):
        pass  # The cloud manager owns the one shared buffer's lifetime.


class PlotValues:
    """Fixed storage allocated before long acquisition can fragment the heap."""
    def __init__(self, format, capacity, buffer=None):
        self.format = format
        self.stride = struct.calcsize(format)
        self.capacity = capacity
        if buffer is None:
            buffer = bytearray(capacity * self.stride)
        elif not isinstance(buffer, bytearray) or len(buffer) != capacity * self.stride:
            raise ValueError("Plot buffer size does not match scan capacity")
        self._data = buffer
        self.count = 0

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError("Plot index outside stored results")
        return struct.unpack_from(self.format, self._data, index * self.stride)[0]

    def append(self, value):
        if self.count == self.capacity:
            raise ValueError("Plot storage is full")
        struct.pack_into(self.format, self._data, self.count * self.stride, value)
        self.count += 1

    def clear(self):
        self.count = 0


class App:
    def __init__(self, display, inputs, engine, settings=None, calibration=None,
                 store=storage, startup_notice=None, error_logger=None, wifi=None, cloud=None,
                 plot_buffers=None, calibration_on_disk=False, scan_buffer=None, restart=None,
                 validated_calibration=None, timing=None):
        self.d, self.inputs, self.engine, self.store = display, inputs, engine, store
        self._timing = timing
        self.settings = model.validate_settings(settings or model.DEFAULT_SETTINGS)
        self.calibration = calibration
        # This reference is an explicit ownership contract, never a serialized
        # flag or global identity cache. A trusted table is read-only until it
        # is replaced; all replacement/drop paths must release this alias.
        self._validated_calibration = (calibration if calibration is not None
                                       and validated_calibration is calibration else None)
        self._calibration_on_disk = bool(calibration_on_disk)
        self._calibration_restore_pending = False
        self._calibration_restore_ready = False
        self._calibration_writer = None
        self.error_logger = error_logger
        self.wifi = wifi
        self.cloud = cloud
        self._ram_mode = scan_buffer is not None
        if self._ram_mode and not callable(restart):
            raise ValueError("RAM scanning requires a restart callback")
        self._scan_buffer, self._restart_callback = scan_buffer, restart
        self._scan_path = False
        self._settings_path = False
        self._restart_pending = False
        self._restart_called = False
        self._ram_drain_count = 1
        self._upload_wifi_started = False
        self._upload_connect_ms = 0
        self._upload_status_key = None
        self._startup_notice = str(startup_notice or "") if self._ram_mode else ""
        # main holds both outputs low before constructing an idle engine.
        self._outputs_safe = getattr(engine, "state", "idle") == "idle"
        self._archive_active = False
        self._archive_ready = False
        self._archive_error = None
        self._scan_fault = None
        self._cloud_error = None
        self._cloud_tick_failed = False
        self._cloud_transfer_hint = False
        self._cloud_status_key = None
        self._cloud_url_draft = ""
        self._cloud_enabled_draft = False
        self._cloud_url_edit = ""
        self._cloud_show = False
        self._cloud_keyboard = "abc"
        self._cloud_value_dirty = False
        self._storage_rows = []
        self._storage_capacity = None
        self._storage_selected = None
        self._storage_page = 0
        self._storage_return = "home"
        self._now_ms = 0
        self._scan_prep = 0
        self._prep_was_paused = False
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
        # Only plotting coordinates stay in RAM. Complete averaged rows are
        # streamed to the binary archive before plotting; latest holds readback.
        # Production supplies raw buffers allocated before the large imports;
        # wrapping them here must not copy or reallocate their storage.
        if plot_buffers is None:
            plot_buffers = (None, None, None)
        elif not isinstance(plot_buffers, (tuple, list)) or len(plot_buffers) != 3:
            raise ValueError("Three plot buffers are required")
        self.rows = (PackedPlotView(self, "xy") if self._ram_mode else
                     PlotValues("<f", model.MAX_POINTS * 2, plot_buffers[0]))
        self.curves = (PackedPlotView(self, "curve") if self._ram_mode else
                       PlotValues("<H", model.MAX_POINTS, plot_buffers[1]))
        # One fixed byte per point: bits 0/1 drain/gate-limited; bit 2 means
        # measured gate is off target and must not join a constant-gate line.
        self.limit_flags = (PackedPlotView(self, "flags") if self._ram_mode else
                           PlotValues("<B", model.MAX_POINTS, plot_buffers[2]))
        self.limited_points = 0
        self._shown_scan_header = None
        self._plot_cursor = 0
        self._reset_plot_bounds()
        # Fixed-size caches retain only the last rendered strings/colors.
        # They never grow with scan duration or replace the result buffers.
        self._live_values = [None] * 4
        self._live_label_colors = [None] * 4
        self._live_requests = [None] * 2
        self._live_misc = [None] * 4  # caption, progress pixels, calibration %, voltages
        self._live_screen = None
        self._last_live_ms = None
        self.notice_return = "home"
        if (not self._ram_mode and self.cloud is not None and self.calibration is not None
                and hasattr(self.cloud, "prepare_calibration")):
            try:
                self._pause_cloud()
                self._cloud_call("prepare_calibration", "Calibration could not be prepared for saving.", self.calibration)
            except RuntimeError as error:
                # A later explicit SCAN retries; fingerprint failure must not
                # prevent the user from reaching Settings or Calibration.
                self._record_error("calibration fingerprint", error)
        if startup_notice and not self._ram_mode:
            self.message(startup_notice, "home")

    def goto(self, screen):
        if self._ram_mode and screen == "home" and self._settings_path:
            self._request_restart()
            return
        if self.screen == "cloud_url" and screen != "cloud_url":
            self._cloud_url_edit = ""
            self._cloud_show = False
        if (self.screen.startswith("cloud") and not screen.startswith("cloud")
                and screen not in ("notice", "storage", "storage_confirm")):
            self._cloud_url_draft = ""
        if self.screen.startswith("storage") and not screen.startswith("storage") and screen != "notice":
            self._storage_rows = []
            self._storage_selected = None
            self._storage_capacity = None
            if screen == "home":
                self._cloud_url_draft = ""
        if self.screen == "wifi_password" and screen != "wifi_password":
            self._wifi_password = ""
            self._wifi_show = False
        if self.screen == "wifi_search" and screen != "wifi_search":
            self._wifi_search_pending = False
        self.screen, self.dirty = screen, True

    def _reset_plot_bounds(self):
        self._plot_xmin = model.volts_from_uv(self.settings["drain_start_uv"])
        self._plot_xmax = model.volts_from_uv(self.settings["drain_stop_uv"])
        if self._plot_xmax == self._plot_xmin:
            self._plot_xmin -= 0.05
            self._plot_xmax += 0.05
        self._plot_ymin, self._plot_ymax = -1.0, 1.0
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
        self.limit_flags.clear()
        self.limited_points = 0
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
        if self._ram_mode and self._scan_path:
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

    @staticmethod
    def _limit_bits(row):
        if row is None:
            return 0
        return ((1 if row.get("drain_voltage_limited", False) else 0) |
                (2 if row.get("gate_voltage_limited", False) else 0))

    @staticmethod
    def _gate_off_target(row):
        if "requested_vgs" not in row:
            return False
        target = row["requested_vgs"]
        return not (target - model.GATE_PLOT_TOLERANCE_V <= row["vgs"] <=
                    target + model.GATE_PLOT_TOLERANCE_V)

    def _scan_header(self, force=False):
        limited = bool(self._limit_bits(self.latest))
        if self.screen == "complete":
            limited = limited or self.limited_points > 0
        state = (self.screen, limited)
        if force or state != self._shown_scan_header:
            if self.screen == "partial":
                self._header("SCAN STOPPED", RED)
                self._text("OVER RANGE", 384, 16, WHITE, 1, RED)
            elif limited:
                self._header("VOLTAGE LIMITED", AMBER)
                self._text("COMPLETE" if self.screen == "complete" else "SCANNING",
                           400, 16, WHITE, 1, AMBER)
            else:
                self._header("SCAN COMPLETE" if self.screen == "complete" else "SCANNING")
            self._shown_scan_header = state

    def _footer(self, home=False, busy=False):
        self.d.fill_rect(0, 270, 480, 50, BG)
        self._button("SETTINGS" if home else "CANCEL", 8, 274, 226, 42,
                     "left", INK, WHITE)
        self._button("SCAN" if home else "OK", 246, 274, 226, 42,
                     None if busy else "right", GRID if busy else AMBER,
                     MUTED if busy else WHITE)

    def calibration_problem(self):
        try:
            self._ensure_calibration_validated()
            # Settings may change independently of the owned read-only map.
            # Check acquisition, supply/source and coverage every time.
            model.validate_scan_calibration_settings(self.calibration, self.settings)
        except (ValueError, KeyError, TypeError) as error:
            return str(error)
        return None

    def _ensure_calibration_validated(self):
        if self.calibration is None or self._validated_calibration is not self.calibration:
            had_previous = self._validated_calibration is not None
            self._validated_calibration = None
            if had_previous and self.cloud is not None and hasattr(self.cloud, "invalidate_calibration"):
                self._cloud_call("invalidate_calibration", "Calibration cache could not be released.")
            model.validate_calibration(self.calibration)
            self._validated_calibration = self.calibration

    def _arm_scan(self):
        self._ensure_calibration_validated()
        self.engine.start_scan(self.settings, self.calibration,
                               validated_calibration=self._validated_calibration)

    def _count(self, settings):
        return model.scan_point_count(settings)

    def render(self):
        self.d.fill(BG)
        self._reset_live_cache()
        self.hits = []
        screen = self.screen
        if screen == "home":
            self._header("POCKET MOSFET")
            problem = self.calibration_problem()
            self._text("CALIBRATION REQUIRED" if problem else "READY TO SCAN", 16, 58,
                       AMBER if problem else TEAL)
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
            self._text("%d points | INA226 averages: %d" % (self._count(self.settings), self.settings["ina_average_count"]), 16, 219, MUTED, 1)
            hint = "Settings > Advanced > Calibrate" if problem else "Id vs VDS, one curve per VGS"
            if self._ram_mode and self._startup_notice:
                hint = self._startup_notice[:56]
            self._text("Uploading saved scans" if self._cloud_transfer_hint else hint, 16, 244, TEAL, 1)
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
            self._text("Manual values relative to Pico GND", 16, 165, MUTED, 1)
            self._button("CALIBRATE", 16, 183, 208, 40, "calibrate", TEAL, WHITE)
            self._button("AVERAGING", 240, 183, 224, 40, "averaging", WHITE, INK)
            if self._ram_mode:
                self._button("WI-FI", 16, 232, 208, 32, "wifi", WHITE, INK, 1)
                self._button("CLOUD", 240, 232, 224, 32, "cloud", WHITE, INK, 1)
            else:
                self._button("WI-FI", 16, 232, 144, 32, "wifi", WHITE, INK, 1)
                self._button("CLOUD", 168, 232, 144, 32, "cloud", WHITE, INK, 1)
                self._button("STORAGE", 320, 232, 144, 32, "storage", WHITE, INK, 1)
            self._footer()
        elif screen.startswith("cloud"):
            self._render_cloud()
        elif screen.startswith("wifi"):
            self._render_wifi()
        elif screen.startswith("storage"):
            self._render_storage()
        elif screen == "averaging":
            self._header("INA226 AVERAGING")
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
            try:
                metadata = self.calibration.get("metadata") if isinstance(self.calibration, dict) else None
                model.validate_calibration_acquisition(metadata, self.draft)
                self._text("Calibration matches this averaging", 16, 244, MUTED, 1)
            except (ValueError, TypeError, KeyError):
                self._text("Recalibration required", 16, 244, RED, 1)
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
        elif screen == "confirm_calibration":
            self._header("OPEN-OUTPUT CALIBRATION")
            for text, y in (("Disconnect DUT and all loads.", 61),
                            ("Remove meter probes and", 94),
                            ("BOTH shunt bypass jumpers.", 119),
                            ("OK saves settings and checks", 169),
                            ("output range and baselines.", 194)):
                self._text(text, 16, y, INK, 1 if len(text) > 28 else 2)
            self._text("Driver supply: %s V" % model.format_voltage_uv(self.draft["driver_supply_uv"]), 16, 224, MUTED, 1)
            self._text("CANCEL preserves the previous calibration.", 16, 245, MUTED, 1)
            self._footer()
        elif screen == "calibrating":
            self._header("CALIBRATING - OUTPUTS OPEN")
            self._text("Measuring voltage-dependent", 16, 62)
            self._text("baselines on both channels", 16, 89)
            self._text("Keep DUT and loads disconnected.", 16, 126, MUTED, 1)
            self._text("Previous calibration stays until completion.", 16, 248, MUTED, 1)
            self._footer(busy=True)
            self._render_live()
        elif screen == "preparing":
            self._header("PREPARING SCAN")
            self._text("Preparing scan", 16, 72, TEAL)
            self._text("Preparing measurement." if self._ram_mode else "Checking available storage.", 16, 116, MUTED, 1)
            self._text("Outputs have not started.", 16, 152, MUTED, 1)
            self._text("CANCEL discards and restarts." if self._ram_mode else "CANCEL returns home.", 16, 210, INK, 1)
            self._footer(busy=True)
        elif screen in ("scan", "complete", "partial"):
            self._scan_header(force=True)
            self._footer(busy=screen == "scan")
            self._plot_axes()
            self._plot_cursor = 0
            self._render_live()
        elif screen == "notice":
            self._header("NOTICE")
            self._wrapped(self.notice, 16, 62, 28, 2)
            self._footer()
        elif screen == "upload":
            state = self._cloud_status()["state"]
            title = "PREPARING UPLOAD" if state == "preparing" else (
                "CONNECTING WI-FI" if state == "waiting_wifi" else "UPLOADING SCAN")
            self._header(title)
            self._text("%d measured points" % len(self._scan_buffer), 16, 76, TEAL)
            self._text("Partial scan: outputs stopped" if self._scan_fault else "Scan complete: outputs stopped", 16, 116, MUTED, 1)
            self._text("Restarts after confirmed upload.", 16, 170, INK, 1)
            self._text("CANCEL discards and restarts.", 16, 204, MUTED, 1)
            self._footer(busy=True)
        elif screen in ("upload_error", "scan_error"):
            self._header("UPLOAD FAILED" if screen == "upload_error" else "SCAN STOPPED", RED)
            self._wrapped(self.notice, 16, 64, 28, 2)
            self._footer()
        elif screen == "restarting":
            self._header("RESTARTING")
            self._text("Returning to Home", 16, 80)
        self.dirty = False

    def _render_edit_value(self):
        self.d.fill_rect(16, 49, 448, 37, WHITE)
        self._text(self.edit_value or "_", 28, 59, INK, bg=WHITE)
        self.edit_dirty = False

    def _acquiring(self):
        return self._scan_prep != 0 or getattr(self.engine, "state", "idle") in ("scanning", "calibrating")

    def _request_restart(self):
        if self._restart_pending or self._restart_called:
            return
        self._restart_pending = True
        if self._timing is not None:
            self._timing.stop()
        self._scan_prep = 0
        if getattr(self.engine, "state", "idle") in ("scanning", "calibrating"):
            self.engine.cancel()
            self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
            if not self._outputs_safe:
                # Main's finally block retries hardware shutdown before radio
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
        self._release_calibration_candidate()
        self._calibration_restore_pending = self._calibration_restore_ready = False
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
            self._ensure_calibration_validated()
            model.validate_scan_calibration_settings(self.calibration, self.settings)
            self._ram_drain_count = len(model.sweep_uv(
                self.settings["drain_start_uv"], self.settings["drain_stop_uv"],
                self.settings["drain_step_uv"]))
            self._cloud_call("begin_scan", "Scan could not be prepared.",
                             self.settings, self.calibration, self._now_ms)
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
        # The sealed cloud source owns its small provenance snapshot. Neither
        # plotting nor upload needs the live calibration/interpolation map.
        self.calibration = None
        self._validated_calibration = None
        self.engine.calibration = None
        if hasattr(self.cloud, "invalidate_calibration"):
            self._cloud_call("invalidate_calibration", "Calibration cache could not be released.")
        gc.collect()
        self.goto("upload")

    def _ram_upload_tick(self):
        state = self._cloud_status()["state"]
        if state == "error":
            self._ram_upload_error()
            return
        if state == "complete":
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
            self._request_restart()
        elif state == "error":
            self._ram_upload_error()
        elif state != self._upload_status_key:
            self._upload_status_key = state
            self.dirty = True

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
        if self._ram_mode:
            self._scan_path = True
            self._scan_prep = 1
            self.goto("preparing")
            return
        # Close TLS before retaining a new screen/hit list or archive objects.
        self._pause_cloud()
        self._prep_was_paused = self._wifi_paused
        self._archive_error = None
        self._archive_ready = False
        self._scan_prep = 1
        self.goto("preparing")

    def _cancel_scan_preparation(self):
        self._scan_prep = 0
        discarded = not self._archive_active or self._discard_archive(quiet=True)
        if not self._prep_was_paused:
            self._resume_wifi()
        if not discarded:
            raise RuntimeError("Scan draft could not be discarded.")

    def _prepare_scan_tick(self):
        # One stage per input/render loop. No output writes occur here;
        # Engine.start_scan only arms its first future tick.
        try:
            if self._scan_prep == 1:
                # Reclaim temporary drawing objects before allocating the
                # archive header. This runs after Preparing is visible and
                # before any output is driven; retain all fixed/TLS buffers.
                gc.collect()
                if self.cloud is not None:
                    # begin_scan is the single authoritative capacity check.
                    # Mark active first so a partially failed open is cleaned.
                    self._archive_active = True
                    result = self._cloud_call("begin_scan", "Scan could not be prepared for saving.",
                                              self.settings, self.calibration, self._now_ms)
                    if result is False:
                        self._archive_active = False
                        self._cancel_scan_preparation()
                        self._open_storage("home")
                        return
                self._scan_prep = 2
            elif self._scan_prep == 2:
                self._pause_wifi()
                self._scan_prep = 3
            elif self._scan_prep == 3:
                self._arm_scan()
                self._outputs_safe = False
                self._scan_prep = 0
                self.clear_results()
                self.goto("scan")
        except (ValueError, OSError, RuntimeError, TypeError, KeyError) as error:
            try:
                self._cancel_scan_preparation()
            except RuntimeError:
                error = RuntimeError("Scan draft could not be discarded.")
            self._record_error("scan preparation", error)
            self.message(str(error), "home")

    def _start_calibration(self):
        if self._acquiring():
            raise RuntimeError("An acquisition is already running")
        was_paused = self._wifi_paused
        self._pause_cloud()
        self._pause_wifi()
        self._archive_error = None
        self._validated_calibration = None
        try:
            # This small record describes the current scale, not the voltage
            # map being replaced. Keep it without retaining the old tables.
            shunt_record = None
            try:
                if self.calibration["channels"]["drain"]["shunt_ohm"] == model.DRAIN_SHUNT_OHM:
                    shunt_record = self.calibration["metadata"].get("drain_shunt_calibration")
            except (KeyError, TypeError, AttributeError):
                pass
            if (not isinstance(shunt_record, dict) or
                    shunt_record.get("effective_shunt_ohm") != model.DRAIN_SHUNT_OHM):
                shunt_record = None
            if self._calibration_on_disk:
                # The committed file remains authoritative until atomic save
                # succeeds. Do not keep the old map alongside a new large one.
                if self.cloud is not None and hasattr(self.cloud, "invalidate_calibration"):
                    self._cloud_call("invalidate_calibration", "Calibration cache could not be released.")
                self._calibration_restore_pending = True
                self._calibration_restore_ready = False
                self.calibration = None
                self._validated_calibration = None
                self.engine.calibration = None
            gc.collect()
            # Allocate the actual LittleFS open-file buffer before the sweep
            # fragments the heap. The committed file remains untouched.
            if hasattr(self.store, "begin_calibration_atomic"):
                self._calibration_writer = self.store.begin_calibration_atomic()
            self.engine.start_calibration(self.settings)
            if shunt_record is not None:
                self.engine.calibration["metadata"]["drain_shunt_calibration"] = shunt_record
            self._outputs_safe = False
            return True
        except Exception:
            # Setup can fail after the engine has entered calibrating but
            # before its first tick. Stop that partially armed operation;
            # consume its terminal event so it cannot hide the error notice.
            if getattr(self.engine, "state", "idle") == "calibrating":
                self.engine.cancel()
                self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
                if not self._acquiring():
                    self.engine.tick(self._now_ms)
            self._release_calibration_candidate()
            gc.collect()
            if not was_paused and self._outputs_safe and not self._calibration_restore_pending:
                self._resume_wifi()
            raise

    def _release_calibration_candidate(self):
        # A cancelled/failed candidate must not survive alongside the next
        # one. Only call after calibration stops; scans retain their data.
        if not self._acquiring():
            if hasattr(self.engine, "discard_calibration_workspace"):
                self.engine.discard_calibration_workspace()
            if self._calibration_writer is not None:
                try:
                    self._calibration_writer.abort()
                except (OSError, ValueError, MemoryError) as error:
                    self._record_error("calibration temporary file cleanup", error)
                self._calibration_writer = None
            self.engine.calibration = self.calibration
            self._calibration_restore_ready = self._calibration_restore_pending

    def _restore_saved_calibration(self):
        # Called at the start of a later UI tick: the failed candidate's engine
        # events, save arguments and exception frames have all unwound.
        self._calibration_restore_pending = False
        self._calibration_restore_ready = False
        try:
            gc.collect()
            self._validated_calibration = None
            self.calibration = self.store.load_calibration()
            model.validate_calibration(self.calibration)
            self._validated_calibration = self.calibration
            self.engine.calibration = self.calibration
        except (OSError, ValueError, TypeError, KeyError, MemoryError) as error:
            self.calibration = None
            self._validated_calibration = None
            self.engine.calibration = None
            self._record_error("calibration restore", error)
            self.message("Saved calibration could not be loaded. Restart or recalibrate.", "home")
        if self._outputs_safe:
            self._resume_wifi()

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

    def _resume_wifi(self):
        if self._wifi_paused:
            self._wifi_paused = False
            try:
                self.wifi.resume(self._now_ms)
            except Exception:
                self._wifi_error = "Wi-Fi could not be resumed"
                self._record_error("wifi resume", self._wifi_error)

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
            elif operation == "finish_scan":
                detail = getattr(getattr(self.cloud, "archive", None), "last_finalize_error", None)
                # Only fixed local archive categories may cross this boundary.
                # Never include exception text, filenames or receiver URLs.
                if (isinstance(detail, (tuple, list)) and len(detail) == 2
                        and detail[0] in ("validate", "trailer", "write", "source",
                                          "stream", "metadata", "rename")
                        and detail[1] in ("memory", "storage", "invalid", "other")):
                    message += " [%s/%s]" % (detail[0], detail[1])
        # Raise outside the handler so host traceback printers cannot chain
        # an underlying exception containing the private receiver URL.
        raise RuntimeError(message)

    def _pause_cloud(self):
        if self.cloud is not None:
            if self._cloud_call("pause", "Cloud upload could not be stopped.") is not True:
                raise RuntimeError("Cloud upload could not be stopped.")
        self._cloud_transfer_hint = False

    def _discard_archive(self, quiet=False):
        if self.cloud is None:
            return True
        try:
            self._cloud_call("discard_scan", "Scan draft could not be discarded.")
            self._archive_active = False
            self._archive_ready = False
            return True
        except RuntimeError:
            if not quiet:
                raise
            self._record_error("scan discard", "Scan draft could not be discarded.")
            return False

    def _archive_failed(self, message):
        self._archive_error = message
        if self._acquiring():
            self.engine.cancel()
        self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
        self._discard_archive(quiet=True)
        if self._outputs_safe:
            self._resume_wifi()
        self._record_error("scan storage", message)
        self.message(message, "home")

    def _cloud_status(self):
        if self.cloud is None:
            return {"state": "unavailable", "message": "Cloud saving is unavailable",
                    "pending_count": 0, "saved_count": 0, "configured": False, "enabled": False}
        status = self.cloud.status
        if self._cloud_error:
            status = dict(status)
            status["message"] = self._cloud_error
        return status

    def _cloud_can_work(self):
        return (not self._ram_mode and self.cloud is not None and not self._acquiring() and self._outputs_safe
                and not self._wifi_paused and not self._wifi_search_pending
                and self.screen in ("home", "cloud") and not self._cloud_tick_failed)

    def _prepare_cloud_view(self):
        if self._ram_mode:
            return
        if self.cloud is None or self.screen not in ("home", "cloud"):
            return
        status = self._cloud_status()
        online = self.wifi is not None and self.wifi.state == "connected"
        hint = self._cloud_can_work() and online and status["state"] == "uploading"
        key = (status["state"], status["message"], status["pending_count"],
               status["saved_count"], status["configured"], status["enabled"], hint)
        if key != self._cloud_status_key:
            self._cloud_status_key = key
            self._cloud_transfer_hint = hint
            self.dirty = True

    def _tick_cloud(self, now_ms):
        if not self._cloud_can_work():
            return
        online = self.wifi is not None and self.wifi.state == "connected"
        try:
            # Called after render. start() only prepares the transport; the
            # next tick paints uploading status before a bounded network step.
            self._cloud_call("tick", "Cloud transfer failed. Saved scans remain queued.",
                             now_ms, online=online, safe=True)
        except RuntimeError:
            self._cloud_error = "Cloud transfer failed. Saved scans remain queued."
            self._record_error("cloud transfer", self._cloud_error)
            self._cloud_tick_failed = True
            try:
                self._pause_cloud()
            except RuntimeError:
                pass
            self.dirty = True

    def _refresh_storage(self):
        # list_scans stops the uploader, keeping this snapshot stable until the
        # user leaves Storage. The storage screens never tick cloud transfers.
        self._storage_rows = self._cloud_call("list_scans", "Saved scans could not be listed.")
        self._storage_capacity = self._cloud_call(
            "scan_capacity", "Scan storage could not be checked.", self.settings)
        pages = max(1, (len(self._storage_rows) + 2) // 3)
        self._storage_page = min(self._storage_page, pages - 1)

    def _open_storage(self, return_to):
        if self._acquiring():
            return
        if self.cloud is None:
            raise RuntimeError("Local scan storage is unavailable")
        self._pause_cloud()
        self._storage_return = return_to
        self._storage_page = 0
        self._storage_selected = None
        try:
            self._refresh_storage()
        except RuntimeError:
            self._storage_rows = []
            self._storage_capacity = None
            raise
        self.goto("storage")

    def _render_storage(self):
        if self.screen == "storage_confirm":
            row = self._storage_selected
            self._header("DELETE LOCAL SCAN?", RED)
            self._wrapped(row["scan_id"], 16, 54, 56, 1)
            if row["status"] == "pending":
                self._text("No confirmed cloud copy.", 16, 119, RED, 2)
                self._text("This may be the only copy of this scan.", 16, 155, INK, 1)
            else:
                self._text("Upload was acknowledged.", 16, 119, TEAL, 2)
                self._text("The Google Sheets copy is not deleted.", 16, 155, INK, 1)
            self._text("OK permanently deletes this local scan.", 16, 195, RED, 1)
            self._text("CANCEL keeps it. Other scans stay unchanged.", 16, 227, MUTED, 1)
            self._footer()
            return
        self._header("LOCAL SCAN STORAGE")
        capacity = self._storage_capacity
        self._text("Free %.1f KiB | uploaded reclaim %.1f KiB" % (
            capacity["actual_free_bytes"] / 1024, capacity["reclaimable_bytes"] / 1024),
            16, 49, MUTED, 1)
        if capacity["can_start"]:
            text, color = "Ready for saved scan settings", TEAL
        elif capacity["reason"] == "queue":
            text, color = "Queue full: %d/%d not uploaded" % (
                capacity["pending_count"], capacity["pending_limit"]), AMBER
        else:
            text, color = "Need %.1f KiB for saved scan settings" % (capacity["required_bytes"] / 1024), AMBER
        self._text(text, 16, 68, color, 1)
        stale = capacity.get("reclaimable_unaccepted_bytes", 0)
        hint = ("Old drafts reclaim %.1f KiB | Oldest first" % (stale / 1024)
                if stale else "Oldest first | Tap one scan to review deletion")
        self._text(hint, 16, 88, MUTED, 1)
        start = self._storage_page * 3
        for index in range(start, min(start + 3, len(self._storage_rows))):
            row = self._storage_rows[index]
            y = 105 + (index - start) * 41
            self._button("", 16, y, 448, 36, ("storage_select", index))
            created = row.get("created_utc")
            when = created[:16].replace("T", " ") if created else "Time unknown"
            state = "QUEUED" if row["status"] == "pending" else "UPLOADED"
            self._text(when + " | " + state, 24, y + 4, INK, 1, WHITE)
            count = row.get("point_count")
            detail = "metadata unreadable" if row.get("damaged") else "%d points" % count
            self._text("...%s | %s | %.1f KiB" % (
                row["scan_id"][-12:], detail, row["size_bytes"] / 1024),
                24, y + 21, MUTED, 1, WHITE)
        if not self._storage_rows:
            self._text("No accepted local scans", 16, 132, MUTED)
        pages = max(1, (len(self._storage_rows) + 2) // 3)
        self._button("PREV", 16, 232, 96, 30,
                     "storage_prev" if self._storage_page else None, scale=1)
        self._text("Page %d/%d | OK: Home" % (self._storage_page + 1, pages),
                   120, 244, MUTED, 1)
        self._button("NEXT", 368, 232, 96, 30,
                     "storage_next" if self._storage_page + 1 < pages else None, scale=1)
        self._footer()

    def _storage_action(self, action):
        if self._acquiring():
            return
        if action == "storage" and self.screen in ("advanced", "cloud"):
            self._open_storage(self.screen)
        elif self.screen == "storage":
            if isinstance(action, tuple) and action[0] == "storage_select":
                index = action[1]
                if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(self._storage_rows):
                    self._storage_selected = self._storage_rows[index]
                    self.goto("storage_confirm")
            elif action == "storage_prev" and self._storage_page:
                self._storage_page -= 1
                self.dirty = True
            elif action == "storage_next" and (self._storage_page + 1) * 3 < len(self._storage_rows):
                self._storage_page += 1
                self.dirty = True
            elif action == "left":
                self.goto(self._storage_return)
            elif action == "right":
                self.goto("home")  # A new explicit SCAN press is always needed.
        elif self.screen == "storage_confirm":
            if action == "left":
                self._storage_selected = None
                self.goto("storage")
            elif action == "right" and self._storage_selected is not None:
                scan_id = self._storage_selected["scan_id"]
                self._pause_cloud()
                try:
                    self._cloud_call("delete_scan", "Local scan deletion failed. Review the refreshed list.", scan_id)
                except RuntimeError:
                    self._storage_selected = None
                    try:
                        self._refresh_storage()
                    except RuntimeError:
                        pass
                    self.message("Local scan deletion failed. Review the refreshed list.", "storage")
                    return
                self._storage_selected = None
                self.goto("storage")
                self._refresh_storage()

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
            if self._ram_mode:
                self._header("CLOUD RECEIVER")
                self._text("Each scan uploads automatically.", 16, 64, MUTED, 1)
                self._button("RECEIVER URL: " + ("SET" if self._cloud_url_draft else "NOT SET"),
                             16, 112, 448, 42, "cloud_url")
                self._text("Google Apps Script", 16, 188, MUTED, 1)
                self._text("OK saves / CANCEL discards", 16, 236, MUTED, 1)
                self._footer()
                return
            status = self._cloud_status()
            self._header("CLOUD SAVING")
            self._text("%d pending | %d saved" % (status["pending_count"], status["saved_count"]), 16, 54)
            self._text("Uploading saved scans" if self._cloud_transfer_hint else status["message"][:56],
                       16, 87, TEAL if self._cloud_transfer_hint else MUTED, 1)
            self._button("RECEIVER URL: " + ("SET" if self._cloud_url_draft else "NOT SET"),
                         16, 112, 448, 42, "cloud_url")
            self._button("AUTO UPLOAD: " + ("ON" if self._cloud_enabled_draft else "OFF"),
                         16, 165, 272, 42, "cloud_toggle", scale=1)
            self._button("RETRY", 304, 165, 160, 42, "cloud_retry")
            self._button("STORAGE", 16, 219, 144, 32, "storage", WHITE, INK, 1)
            self._text("Google Apps Script", 184, 222, MUTED, 1)
            self._text("RETRY uses saved settings", 184, 239, MUTED, 1)
            self._text("OK saves cloud settings / CANCEL discards", 16, 258, MUTED, 1)
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
            self._cloud_enabled_draft = config["enabled"]
            self.goto("cloud")
        elif screen == "cloud":
            # Editing or leaving this page must close TLS before drawing a
            # different view can retain objects in its temporary heap region.
            self._pause_cloud()
            if action == "left":
                self.goto("advanced")
            elif action == "right":
                self._cloud_call("save_config", "Cloud settings could not be saved. Use a valid Apps Script /exec URL.",
                                 self._cloud_url_draft, enabled=True if self._ram_mode else self._cloud_enabled_draft)
                self._cloud_error, self._cloud_tick_failed = None, False
                self.goto("advanced")
            elif action == "cloud_toggle" and not self._ram_mode:
                self._cloud_enabled_draft = not self._cloud_enabled_draft
                self.dirty = True
            elif action == "cloud_url":
                self._cloud_url_edit = self._cloud_url_draft
                self._cloud_show, self._cloud_keyboard = False, "abc"
                self.goto("cloud_url")
            elif action == "cloud_retry" and not self._ram_mode:
                self._cloud_call("request_upload", "Cloud upload could not be requested.")
                self._cloud_error, self._cloud_tick_failed = None, False
                self.dirty = True
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
        if self._ram_mode and not self._settings_path:
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
            # Both endpoints must belong to the requested gate curve. Keep
            # an off-target point visible, but never connect into/out of it
            # or bridge across it, including after a redraw or draw chunk.
            if (index and self.curves[index] == self.curves[index - 1]
                    and not ((self.limit_flags[index] | self.limit_flags[index - 1]) & 4)):
                px, py = self._xy(index - 1)
                self.d.line(px, py, x, y, color)
            if self.limit_flags[index]:
                self.d.rect(x - 2, y - 2, 5, 5, AMBER)
                self.d.fill_rect(x, y, 1, 1, AMBER)
            else:
                self.d.fill_rect(x - 1, y - 1, 3, 3, color)
        self._plot_cursor = end

    def _add_point(self, row):
        if not self._ram_mode:
            for field in ("vds", "id_a"):
                self.rows.append(row[field])
            self.curves.append(row["curve"])
        flags = self._limit_bits(row)
        if flags:
            self.limited_points += 1
        if self._gate_off_target(row):
            flags |= 4
        if not self._ram_mode:
            self.limit_flags.append(flags)
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
        if self.screen == "calibrating":
            self.d.fill_rect(16, 159, 448, 65, BG)
        else:
            self.d.fill_rect(334, 46, 146, 193, BG)
            if self.screen == "partial":
                fault = self._scan_fault
                reason = {"shunt_saturation": "SHUNT ADC LIMIT", "bus_saturation": "BUS ADC LIMIT",
                          "current_limit": "CURRENT LIMIT"}.get(fault["kind"], "OVER RANGE")
                self._text(fault["channel"].upper(), 338, 54, RED, 1)
                self._text(reason, 338, 76, RED, 1)
                self._text("%d good points" % len(self.curves), 338, 108, INK, 1)
                self._text("Fault point not", 338, 140, MUTED, 1)
                self._text("measured.", 338, 155, MUTED, 1)
                self._text("Outputs stopped", 338, 190, MUTED, 1)
        self.d.fill_rect(16, 263, 448, 4, GRID)
        self._live_misc[1] = 0
        self._live_screen = self.screen

    def _render_live(self):
        progress = self.engine.progress
        self._live_static()
        if self.screen == "calibrating":
            self._live_text(self._live_misc, 2, "%d%% complete" % int(progress * 100),
                            16, 164, 448, TEAL)
            value = ""
            if self.latest:
                source_v = model.volts_from_uv(self.settings["source_voltage_uv"])
                value = "D %.3f V  G %.3f V" % (
                    self.latest["vds"] + source_v, self.latest["vgs"] + source_v)
            self._live_text(self._live_misc, 3, value, 16, 198, 448)
        else:
            self._scan_header()
            row = self.latest
            bits = self._limit_bits(row)
            if self.screen != "partial":
                for index, (label, field, multiplier, y) in enumerate((
                        ("VDS  V", "vds", 1, 52), ("VGS  V", "vgs", 1, 95),
                        ("Id  uA", "id_a", 1e6, 138), ("Ig  uA", "ig_a", 1e6, 181))):
                    color = AMBER if index < 2 and bits & (1 << index) else MUTED
                    color_changed = self._live_label_colors[index] != color
                    if color_changed:
                        self._text(label, 338, y, color, 1)
                        self._live_label_colors[index] = color
                    value = "%.3f" % (row[field] * multiplier) if row else "--"
                    if len(value) > 8:
                        value = "%.4g" % (row[field] * multiplier)
                    self._live_text(self._live_values, index, value, 338, y + 15, 142)
                    if index < 2:
                        requested_key = "requested_" + field
                        value = "SET %.3g V" % row[requested_key] if row and requested_key in row else ""
                        self._live_text(self._live_requests, index, value, 338, y + 33,
                                        142, color, 1, color_changed)
            caption = "%d points | %d%%" % (len(self.curves), int(progress * 100))
            if self.screen == "partial" or (self.screen == "complete" and self.cloud is not None):
                caption = "%d points | OK saves / CANCEL discards" % len(self.curves)
            elif row:
                caption += " | INA avg %d" % self.settings["ina_average_count"]
            if self.limited_points:
                caption += " | %d limited" % self.limited_points
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
            if self._ram_mode and self._scan_path:
                self._ram_hard_error(message)
            else:
                self.message(message, "home" if self.screen == "preparing" else None)

    def _action(self, action):
        if self._ram_mode:
            if self._restart_pending or self._restart_called:
                return
            if self._scan_path:
                if (action == "left" or
                        (action == "right" and self.screen in ("upload_error", "scan_error"))):
                    self._request_restart()
                return
            action_name = action[0] if isinstance(action, tuple) else action
            if action_name.startswith("storage"):
                return
        if self.screen == "preparing":
            if action == "left":
                self._cancel_scan_preparation()
                self.goto("home")
            return  # Disabled OK and stale actions cannot replace this screen.
        wifi_action = action[0] if isinstance(action, tuple) else action
        if self.screen.startswith("storage") or wifi_action.startswith("storage"):
            self._storage_action(action)
            return
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
                    raise ValueError("Unsupported INA226 average count")
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
                if self._ram_mode:
                    self._settings_path = True
                    # This path cannot scan/upload before a mandatory reboot.
                    # Give Settings the contiguous upload workspace before any
                    # UI/radio activity: LittleFS needs a 1148-byte file buffer
                    # when saving, even if total free heap is much larger.
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
                self._save_draft(exiting=self._ram_mode)
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
            elif action == "calibrate":
                model.validate_settings(self.draft)
                self.goto("confirm_calibration")
            elif action == "averaging":
                self.averaging_entry = dict(self.draft)
                self.goto("averaging")
        elif screen == "averaging":
            if action == "left":
                self.draft = dict(self.averaging_entry)
                self.goto("advanced")
            elif action == "right":
                self.goto("advanced")
        elif screen == "confirm_calibration":
            if action == "left":
                self.goto("advanced")
            elif action == "right":
                self._save_draft()
                self.advanced_entry = dict(self.draft)
                self._start_calibration()
                self.latest = None
                self.goto("calibrating")
        elif screen == "edit":
            if action == "left":
                self.goto(self.edit_return)
            elif action == "right":
                value = model.parse_voltage_uv(self.edit_value)
                self.draft[self.edit_key] = value
                self.goto(self.edit_return)
        elif screen in ("scan", "calibrating"):
            if action == "left":
                self.engine.cancel()
        elif screen in ("complete", "partial"):
            if action in ("left", "right"):
                if self.cloud is not None:
                    if action == "right":
                        if not self._archive_ready:
                            raise RuntimeError("No scan result is ready to save.")
                        self._cloud_call("accept_scan", "Scan could not be saved. OK retries; CANCEL discards.")
                        self._archive_active = False
                        self._archive_ready = False
                    else:
                        self._discard_archive()
                self.goto("home")
        elif screen == "notice":
            if action in ("left", "right"):
                self.goto(self.notice_return)

    def tick(self, now_ms):
        if self._ram_mode and (self._scan_path or self._restart_pending or self._restart_called):
            self._tick_ram_scan(now_ms)
            return
        self._now_ms = now_ms
        if self._calibration_restore_ready and not self._acquiring():
            self._restore_saved_calibration()
        preparing = self.screen == "preparing"
        preparation_painted = preparing and not self.dirty
        # Reclaim the contiguous TLS workspace after acknowledgement frames
        # have unwound, before drawing the changed cloud status allocates keys.
        if self._cloud_can_work() and hasattr(self.cloud, "maintain_memory"):
            self.cloud.maintain_memory(now_ms)
        # The previous tick must have painted the search screen before the
        # synchronous driver call. An input CANCEL can still remove this work.
        search_ready = self.screen == "wifi_search" and not self.dirty
        for event in self.inputs.poll(now_ms):
            if event[0] == "touch":
                for x, y, w, h, action in self.hits:
                    if x <= event[1] < x + w and y <= event[2] < y + h:
                        self.action(action)
                        break
            else:
                self.action(event[0])
            # Do not apply a second simultaneous press to a newly opened screen.
            if self.dirty or (self._ram_mode and self._restart_pending):
                break
        if self._ram_mode and self._restart_pending:
            self._finish_restart()
            return
        skip_engine = preparing or self.screen == "preparing"
        if preparation_painted and self.screen == "preparing":
            self._prepare_scan_tick()
        # The arm stage paints the scan screen later in this tick. The first
        # drive waits for the next input loop, allowing CANCEL before it.
        for kind, data in (() if skip_engine else self.engine.tick(now_ms)):
            if kind in ("complete", "partial", "cancelled", "error"):
                self._outputs_safe = bool(getattr(self.engine, "shutdown_succeeded", False))
            self.latest = self.engine.latest
            if kind == "point":
                if self.cloud is not None and self._archive_active:
                    try:
                        self._cloud_call("record_point", "Scan storage failed. Acquisition stopped.", data)
                    except RuntimeError as error:
                        self._archive_failed(str(error))
                        break
                self._add_point(data)
            elif kind == "calibrated":
                try:
                    gc.collect()
                    # Real Engine events already passed full validation. An
                    # injected/unknown candidate must validate before commit.
                    if data is not self.engine.calibration or self.engine.state != "complete":
                        model.validate_calibration(data)
                    if self._calibration_writer is not None:
                        self._calibration_writer.commit(data)
                        self._calibration_writer = None
                    else:
                        self.store.save_calibration_atomic(data)
                    self.calibration = data
                    self._validated_calibration = data
                    self._calibration_on_disk = True
                    self._calibration_restore_pending = False
                    self._calibration_restore_ready = False
                    if self.cloud is not None and hasattr(self.cloud, "prepare_calibration"):
                        try:
                            self._cloud_call("prepare_calibration", "Calibration could not be prepared for saving.", data)
                        except RuntimeError as error:
                            self._record_error("calibration fingerprint", error)
                    self.message("Calibration saved. Both outputs are at zero drive.", "advanced")
                except (OSError, ValueError, MemoryError) as error:
                    self._record_error("calibration save", error)
                    message = ("Calibration could not be saved: not enough memory."
                               if isinstance(error, MemoryError) else
                               "Calibration could not be saved: " + str(error))
                    self.message(message, "advanced")
                finally:
                    self._release_calibration_candidate()
            elif kind == "complete" and self.screen == "scan":
                if self.cloud is not None and self._archive_active:
                    try:
                        gc.collect()
                        self._cloud_call("finish_scan", "Completed scan could not be finalized.", now_ms)
                        self._archive_ready = True
                    except RuntimeError as error:
                        self._archive_failed(str(error))
                        break
                self.goto("complete")
            elif kind == "partial" and self.screen == "scan":
                if not self._outputs_safe:
                    self._discard_archive(quiet=True)
                    self._record_error("acquisition", "Output shutdown failed")
                    self.message("Output shutdown failed", "home")
                    break
                if self.cloud is not None and self._archive_active:
                    try:
                        gc.collect()
                        self._cloud_call("finish_scan", "Stopped scan could not be finalized.", now_ms, data)
                        self._archive_ready = True
                    except RuntimeError as error:
                        self._archive_failed(str(error))
                        break
                self._scan_fault = data
                self._record_error("acquisition", "%s %s" % (data["channel"], data["kind"]))
                self.goto("partial")
            elif kind == "cancelled":
                if self.screen == "calibrating":
                    self._release_calibration_candidate()
                if self._archive_active:
                    if not self._discard_archive(quiet=True):
                        self._archive_error = "Scan draft could not be discarded."
                if self._archive_error:
                    self.message(self._archive_error, "home")
                else:
                    self.goto("advanced" if self.screen == "calibrating" else "home")
            elif kind == "error":
                if self.screen == "calibrating":
                    self._release_calibration_candidate()
                if self._archive_active:
                    self._discard_archive(quiet=True)
                self._record_error("acquisition", data)
                self.message(self._archive_error or data, "home")
            if (kind in ("complete", "partial", "cancelled", "error") and self._outputs_safe
                    and not self._calibration_restore_pending):
                # Finalize/discard local data before radio reconnection allocates
                # more objects on the fragmented post-acquisition heap.
                self._resume_wifi()
            self.live_dirty = True
        self._tick_wifi(now_ms, search_ready)
        self._prepare_cloud_view()
        if self.dirty:
            self.render()
        elif self.screen == "edit" and self.edit_dirty:
            self._render_edit_value()
        elif self.screen == "wifi_password" and self._wifi_value_dirty:
            self._render_wifi_value()
        elif self.screen == "cloud_url" and self._cloud_value_dirty:
            self._render_cloud_value()
        if self.screen in ("scan", "complete", "partial", "calibrating"):
            elapsed = 1000 if self._last_live_ms is None else _ticks_diff(now_ms, self._last_live_ms)
            if self.live_dirty and elapsed >= 150:
                self._render_live()
                self._last_live_ms = now_ms
            if self.screen != "calibrating":
                self._draw_points()
        self._tick_cloud(now_ms)
        if self._ram_mode and self._restart_pending:
            self._finish_restart()
