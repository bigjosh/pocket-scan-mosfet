"""Measure CANCEL feedback on the real LCD with both PWM outputs held at zero.

The production engine is armed but never ticked, so no scan voltage is applied.
Inject one LEFT input, exercise real RAM discard/radio pause, and observe the
restart callback without rebooting. No measurements, uploads or file writes.
"""
import main


class CancelInput:
    def poll(self, now):
        return (("left",),)


def run():
    instrument = None
    wifi = None
    try:
        settings = main.storage.load_settings(migrate=False)
        calibration = main.storage.load_calibration()
        main.gc.collect()
        display = main.Display(raster_pool=main._display_buffer)
        instrument = main.Instrument()
        wifi = main.WifiManager()
        wifi.pause()
        buffer = main.PackedScanBuffer(main._scan_bytes)
        cloud = main.RamCloudManager(buffer, transport=main._upload_transport)
        engine = main.Engine(instrument)
        engine.start_scan(settings, calibration)  # Arm only; never tick drive.
        if not (engine._drain_floor_active or settings["source_voltage_uv"] == 0):
            raise ValueError("Zero-only diagnostic requires zero-duty drain parking")
        times = [0, 0, 0, 0]

        def restarted():
            times[3] = main.time.ticks_us()

        app = main.App(display, CancelInput(), engine, settings, calibration,
                       wifi=wifi, cloud=cloud, scan_buffer=buffer, restart=restarted)
        app._scan_path = True
        app.goto("scan")
        app.render()
        fill, text = display.fill, display.text

        def timed_fill(color):
            assert all(instrument._pwms[role].duty_u16() == 0 for role in ("gate", "drain"))
            times[1] = main.time.ticks_us()
            fill(color)

        def timed_text(value, *args, **kwargs):
            text(value, *args, **kwargs)
            if value == "RESTARTING":
                times[2] = main.time.ticks_us()

        display.fill, display.text = timed_fill, timed_text
        times[0] = main.time.ticks_us()
        app.tick(main.time.ticks_ms())
        assert all(times) and app._restart_called and engine.shutdown_succeeded
        assert app.screen == "restarting" and not app.dirty and len(buffer) == 0
        assert cloud.state == "idle" and wifi.state == "paused"
        assert all(instrument._pwms[role].duty_u16() == 0 for role in ("gate", "drain"))
        print("CANCEL_FEEDBACK_PASS", main.storage.json.dumps({
            "clear_begin_ms": main.time.ticks_diff(times[1], times[0]) / 1000,
            "restarting_text_drawn_ms": main.time.ticks_diff(times[2], times[0]) / 1000,
            "restart_callback_ms": main.time.ticks_diff(times[3], times[0]) / 1000,
            "outputs_zero": True, "scan_drive_ticks": 0,
            "uploaded": False, "soft_reset": False}))
    finally:
        try:
            if instrument is not None:
                instrument.close()
        finally:
            main.Pin(21, main.Pin.OUT, value=0)
            main.Pin(20, main.Pin.OUT, value=0)
            if wifi is not None:
                wifi.pause()


if __name__ == "__main__":
    run()
