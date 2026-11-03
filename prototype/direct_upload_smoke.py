"""Real three-point scan/upload check; run on a fresh native interpreter.

Uses main's real instrument, display, Wi-Fi and HTTPS transport. Only the first
three configured drain targets and first gate target are selected, in RAM;
saved settings/credentials are never edited. No URL/password is printed.
Success requires all three measured points, the production manager's exact ACK
match, normal App discard/restart cleanup, and main's hardware cleanup.

The cooperative overall deadline is 180 seconds. A transport step retains its
normal bounded timeouts. Any Cancel, partial/error, timeout or exception stops
the check and cannot print DIRECT_UPLOAD_PASS. The host should soft-reset to
normal Home afterwards. Compile with -O0 to retain diagnostic assertions.
"""

import main
import time
from machine import Pin
import model

_TIMEOUT_MS = 180000
_result = None
_reason = "not_started"
_started_ms = 0


class SmokeExit(BaseException):
    """Unwind main's finally without asking main to soft-reset yet."""


def first_three_settings(saved):
    result = model.validate_settings(saved)
    drains = model.sweep_uv(result["drain_start_uv"], result["drain_stop_uv"],
                            result["drain_step_uv"])
    if len(drains) < 3:
        raise ValueError("Three configured drain points are required")
    result["drain_stop_uv"] = drains[2]
    result["gate_stop_uv"] = result["gate_start_uv"]
    assert model.scan_point_count(result) == 3
    return result


def _pins_low():
    try:
        Pin(21, Pin.OUT, value=0)
    finally:
        Pin(20, Pin.OUT, value=0)


class UploadApp(main.App):
    def __init__(self, display, inputs, engine, settings=None, **kwargs):
        self._queued = False
        self._printed_points = 0
        self._upload_tick_active = False
        self._user_cancel = False
        self._progress_stage = self._progress_percent = -1
        self._progress_sent = 0
        kwargs["restart"] = self._exit
        super().__init__(display, inputs, engine, first_three_settings(settings), **kwargs)

    def _exit(self):
        global _result, _reason
        if _result is not None and (len(self._scan_buffer) != 0 or
                self.cloud.state != "idle" or not self.engine.shutdown_succeeded or
                (self.wifi is not None and self.wifi.state != "paused")):
            _result, _reason = None, "cleanup_failed"
        raise SmokeExit()

    def _action(self, action):
        # The check never enters a Settings editor or writes saved settings.
        # Retain physical/touch CANCEL during every stage; other input cannot
        # expand the diagnostic into a full saved-grid scan.
        if action == "left":
            self._user_cancel = True
            self._request_restart()
        elif action == "right" and self.screen in ("scan_error", "upload_error"):
            self._request_restart()

    def _add_point(self, row):
        assert row["index"] == self._printed_points and row["curve"] == 0
        assert self._printed_points < 3
        super()._add_point(row)
        # Scalar output avoids a transient JSON copy of the measured row.
        print("DIRECT_UPLOAD_POINT", row["index"], row["requested_vds"],
              row["requested_vgs"], row["vds"], row["vgs"], row["id_a"], row["ig_a"])
        self._printed_points += 1

    def _ram_upload_tick(self):
        self._upload_tick_active = True
        try:
            self._log_upload_progress()
            return super()._ram_upload_tick()
        finally:
            self._upload_tick_active = False

    def _log_upload_progress(self):
        stage = self.cloud.upload_stage
        sent, total = self.cloud.upload_sent, self.cloud.upload_total
        assert sent >= self._progress_sent and 0 <= sent <= total
        percent = sent * 100 // total if total else 0
        if stage != self._progress_stage or percent != self._progress_percent:
            print("DIRECT_UPLOAD_PROGRESS", stage, sent, total, percent)
            self._progress_stage, self._progress_percent = stage, percent
        self._progress_sent = sent

    def _request_restart(self):
        global _result, _reason
        scan = self.cloud.scan if self.cloud is not None else None
        if (not self._user_cancel and self._upload_tick_active and
                self.screen == "upload" and self.cloud.state == "complete" and
                self.engine.state == "complete" and self.engine.shutdown_succeeded and
                self._printed_points == 3 and len(self._scan_buffer) == 3 and
                scan is not None and scan.point_count == 3 and scan.outcome == "complete" and
                scan.fault is None and scan.sealed and self._scan_buffer.frozen and
                isinstance(scan.sha256, str) and len(scan.sha256) == 64):
            # RamCloudManager reaches complete only after matching the actual
            # server scan_id, SHA256, point count and typed sheet ID exactly.
            _result = (scan.scan_id, scan.sha256, 3)
            _reason = "acknowledged"
            self._log_upload_progress()
            assert self.cloud.upload_stage == 4
            assert self.cloud.upload_sent == self.cloud.upload_total == scan.wire_size
        elif _reason == "running":
            _reason = "cancelled" if self._user_cancel else "incomplete"
        return super()._request_restart()

    def tick(self, now_ms):
        global _reason
        if time.ticks_diff(now_ms, _started_ms) >= _TIMEOUT_MS:
            _reason = "timeout"
            self._request_restart()
            return
        super().tick(now_ms)
        if self._restart_called:
            return
        if self.screen in ("scan_error", "upload_error") or self.engine.state in ("partial", "error"):
            _reason = "upload_error" if self.screen == "upload_error" else "scan_error"
            self._request_restart()
            return
        if not self._queued and self.screen == "home" and not self.dirty:
            self._queued = True
            print("DIRECT_UPLOAD_BEGIN", self.settings["drain_start_uv"],
                  self.settings["drain_stop_uv"], self.settings["drain_step_uv"],
                  self.settings["gate_start_uv"], self.settings["ina_average_count"])
            self._queue_scan()


def run():
    global _result, _reason, _started_ms
    _result, _reason = None, "running"
    _started_ms = time.ticks_ms()
    original = main.App
    exited = False
    _pins_low()
    try:
        main.App = UploadApp
        try:
            main.run()
        except SmokeExit:
            exited = True
    finally:
        main.App = original
        try:
            main._scan_timing.stop()
        finally:
            _pins_low()
    if not exited or _result is None or _reason != "acknowledged":
        print("DIRECT_UPLOAD_ABORT", _reason if exited else "main_returned")
        raise RuntimeError("Three-point upload check did not complete")
    assert Pin(21).value() == 0 and Pin(20).value() == 0
    print("DIRECT_UPLOAD_ACK", _result[0], _result[1], _result[2])
    print("DIRECT_UPLOAD_PASS")
