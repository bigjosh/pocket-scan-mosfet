"""Measure the first 100 points of the SAVED scan, then cancel and discard.

This is a powered DUT scan using the user's existing voltage grid and
protection limits. It must only run when that scan is authorized. There
are no settings writes or uploads. Production cleanup disables the outputs;
the host must restart normal main.py after the helper returns.
"""
import main

BaseApp = main.App
POINTS = 100


class BenchmarkDone(BaseException):
    pass


def stop_benchmark():
    raise BenchmarkDone()


class BenchmarkApp(BaseApp):
    def __init__(self, *args, **kwargs):
        BaseApp.__init__(self, *args, **kwargs)
        assert main.model.scan_point_count(self.settings) > POINTS
        assert self.settings["settle_ms"] == 100
        self._restart_callback = stop_benchmark
        self.benchmark_queued = False
        self.benchmark_started = main.time.ticks_ms()

    def tick(self, now):
        if main.time.ticks_diff(now, self.benchmark_started) > 120000:
            print("DISPLAY_BENCHMARK_FAILED timeout")
            raise BenchmarkDone()
        BaseApp.tick(self, now)
        if main._scan_timing.last_index >= POINTS - 1:
            print("DISPLAY_BENCHMARK_COMPLETE points=100 settle_ms=100")
            self.action("left")
            self._finish_restart()
        if self.screen in ("scan_error", "upload_error", "upload"):
            print("DISPLAY_BENCHMARK_FAILED", self.screen)
            raise BenchmarkDone()
        if not self.benchmark_queued:
            assert self.screen == "home" and not self.dirty
            self.benchmark_queued = True
            print("DISPLAY_BENCHMARK_BEGIN points=100 settle_ms=100 average=%d" %
                  self.settings["ina_average_count"])
            self.action("right")


def run():
    main.App = BenchmarkApp
    try:
        main.run()
    except BenchmarkDone:
        pass
    finally:
        main.App = BaseApp
        main._scan_timing.stop()
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)
    print("DISPLAY_BENCHMARK_STOPPED outputs_low=1 upload=0")


if __name__ == "__main__":
    run()
