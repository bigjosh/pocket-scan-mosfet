"""Run up to three current-grid points; report early faults, never upload."""
import main
import json

BaseApp = main.App
_diagnostic_passed = False


class DiagnosticDone(BaseException):
    pass


def stop():
    raise DiagnosticDone()


class DiagnosticApp(BaseApp):
    def __init__(self, *args, **kwargs):
        BaseApp.__init__(self, *args, **kwargs)
        self._restart_callback = stop
        self._diagnostic_started = main.time.ticks_ms()
        self._diagnostic_queued = False

    def _ram_seal(self, fault=None):
        print("EARLY_STOP", self.engine.state, self.engine._index,
              self._outputs_safe, self.engine.error)
        if fault is not None:
            for key in ("channel", "kind", "phase", "index", "measured_value",
                        "limit_value", "requested_vds", "requested_vgs"):
                print("EARLY_STOP_FAULT", key, fault.get(key))
        stop()

    def tick(self, now):
        global _diagnostic_passed
        if main.time.ticks_diff(now, self._diagnostic_started) > 30000:
            print("EARLY_STOP_TIMEOUT")
            stop()
        BaseApp.tick(self, now)
        if self.screen == "scan_error":
            print("EARLY_STOP_ERROR", self.notice)
            stop()
        # A timing snapshot can survive stop() in the same interpreter. Only
        # this operation's retained records establish three completed points.
        if self._diagnostic_queued and self.screen == "scan" and len(self._scan_buffer) >= 3:
            assert len(self._scan_buffer) == 3 and self.engine._index == 3
            # Formatting the full row as JSON needs a growing contiguous
            # string on the already fragmented acquisition heap. Stream only
            # the four scalar measurements, with no snapshot or joined string.
            for key in ("vds", "vgs", "id_a", "ig_a"):
                print("EARLY_STOP_MEASURED", key, self.engine.latest[key])
            print("EARLY_STOP_THREE_POINTS_OK")
            self.action("left")
            assert (self._outputs_safe and self.engine.shutdown_succeeded
                    and self.engine.state == "cancelled" and len(self._scan_buffer) == 0
                    and self.cloud.state == "idle"), "Three-point cleanup failed"
            _diagnostic_passed = True
            self._finish_restart()
        if not self._diagnostic_queued:
            assert self.screen == "home" and not self.dirty
            self._diagnostic_queued = True
            print("EARLY_STOP_BEGIN", json.dumps(self.settings))
            self.action("right")


def run():
    global _diagnostic_passed
    _diagnostic_passed = False
    stopped = False
    main.App = DiagnosticApp
    try:
        main.run()
    except DiagnosticDone:
        stopped = True
    finally:
        main.App = BaseApp
        main._scan_timing.stop()
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)
    # main.run swallows ordinary exceptions. A normal return is not the
    # expected diagnostic stop, even if the preceding cleanup looked good.
    if not stopped:
        _diagnostic_passed = False
    if _diagnostic_passed:
        print("EARLY_STOP_PASS points=3 outputs_low=1 upload=0 settings_written=0")
    print("EARLY_STOP_END passed=%d outputs_low=1 upload=0 settings_written=0" % _diagnostic_passed)


if __name__ == "__main__":
    run()
