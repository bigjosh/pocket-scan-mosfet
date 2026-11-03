"""Real LCD/RAM zero-point fault screen; outputs low, no upload or file writes.

Run from an idle REPL after stopping main. Imports reserve the production
buffers; no Instrument, PWM, Wi-Fi or transport is constructed. Only the
fault input is synthetic: App._ram_seal, screen painting, button dispatch,
discard and restart request use production UI code unchanged.
"""
import main


class Inputs:
    next = None

    def poll(self, now):
        event, self.next = self.next, None
        return ((event,),) if event is not None else ()


class StoppedEngine:
    state = "partial"
    progress = 0.0
    shutdown_succeeded = True
    calibration = None


class NoFiles:
    def forbidden(self, *args, **kwargs):
        raise AssertionError("Empty fault check must not access data files")

    load_json = load_calibration = save_json_atomic = forbidden
    save_settings = save_calibration_atomic = begin_calibration_atomic = forbidden


class Cloud:
    def __init__(self, buffer):
        self.buffer = buffer
        self.pauses = self.discards = self.finishes = self.uploads = 0

    def pause(self):
        self.pauses += 1
        return True

    def finish_scan(self, *args):
        self.finishes += 1
        raise AssertionError("Empty result must not be finalized")

    def tick(self, *args, **kwargs):
        self.uploads += 1
        raise AssertionError("Empty result must not upload")

    def discard_scan(self):
        self.discards += 1
        self.buffer.unfreeze()
        self.buffer.clear()


class CheckedDisplay(main.Display):
    """Observe bounded visible-label flags without retaining rendered strings."""
    def fill(self, color):
        self.seen = 0
        return main.Display.fill(self, color)

    def text(self, value, *args, **kwargs):
        for bit, fragment in ((1, "SCAN STOPPED"), (2, "DRAIN current limit."),
                              (4, "measured points."), (8, "-20.916 mA"),
                              (16, "5.000 mA"), (32, "CANCEL"), (64, "OK")):
            if fragment in value:
                self.seen |= bit
        return main.Display.text(self, value, *args, **kwargs)


def run():
    gate = main.Pin(21, main.Pin.OUT, value=0)
    drain = main.Pin(20, main.Pin.OUT, value=0)
    main._scan_timing.stop()
    cloud = None
    try:
        assert main.tls_memory.available(), "Production TLS reserve is missing"
        main.gc.collect()
        display = CheckedDisplay(raster_pool=main._display_buffer)
        buffer = main.PackedScanBuffer(main._scan_bytes)
        restarts = [0]

        def restart():
            restarts[0] += 1  # Observe request only; do not reset the board.

        print("EMPTY_SCAN_FAULT_BEGIN synthetic=1 outputs_low=1")
        for button in ("left", "right"):
            restarts[0] = 0
            inputs = Inputs()
            cloud = Cloud(buffer)
            app = main.App(display, inputs, StoppedEngine(), store=NoFiles(),
                           cloud=cloud, scan_buffer=buffer, restart=restart)
            app._scan_path = True
            app._outputs_safe = True
            app.screen = "scan"
            app._ram_seal({"channel": "drain", "kind": "current_limit",
                           "measured_value": -0.020915716, "limit_value": 0.005,
                           "phase": "park_drain", "index": 0})
            app.tick(main.time.ticks_ms())
            assert app.screen == "scan_error" and display.seen == 127
            assert "No measured points." in app.notice
            assert len(buffer) == 0 and cloud.finishes == 0 and cloud.uploads == 0
            assert gate.value() == 0 and drain.value() == 0
            print("EMPTY_SCAN_FAULT_SCREEN button=%s labels=127 points=0 finish=0 upload=0" % button)
            inputs.next = button
            app.tick(main.time.ticks_ms())
            assert restarts[0] == 1 and cloud.discards == 1
            assert len(buffer) == 0 and app._restart_called
            assert gate.value() == 0 and drain.value() == 0
            app = inputs = cloud = None
            main.gc.collect()
        assert main.tls_memory.available()
        display.fill(main.BG)
        display.text("EMPTY SCAN CHECK PASS", 16, 24, main.INK, bg=main.BG, scale=2)
        display.text("Both exits checked. Outputs low.", 16, 64, main.INK, bg=main.BG)
        print("EMPTY_SCAN_FAULT_PASS exits=2 finish=0 upload=0 files_written=0 outputs_low=1")
    finally:
        if cloud is not None:
            cloud.discard_scan()
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)


if __name__ == "__main__":
    run()
