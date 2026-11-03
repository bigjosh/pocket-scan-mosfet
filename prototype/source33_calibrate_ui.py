"""Explicit native open-fixture calibration; NEVER import/run with a DUT fitted.

Run only after the operator confirms open outputs, unbridged shunts, no probes,
the corrected drain offset wiring, and the zero-PWM fixture measurement.
Uses the production reserves/UI/save lifecycle; performs no scan or upload.
Requires the existing 3.3 V source setting. CALIBRATION_AVERAGES defaults to64
and may be set before run(). The real Averaging→Calibrate flow saves that count
for both scans and calibration; voltage ranges and manual rails stay intact.
LEFT/touch CANCEL remains active. No automatic soft reset is requested here.
"""

# Import before any helper allocations: main reserves TLS, scan and text pools
# and holds GP20/21 low, but does not call run() when imported.
import main

# Wiring plausibility only: measure the actual floor rather than predicting it
# from nominal resistor values. The installed PWM filter is now 1 kOhm, 5%.
DRAIN_ZERO_MIN_V = 3.0
DRAIN_ZERO_MAX_V = 3.8
ZERO_STABILITY_MS = 10000
ZERO_MAX_SPAN_V = 0.020
CALIBRATION_TIMEOUT_MS = 30 * 60 * 1000
CALIBRATION_AVERAGES = 64


class Stopped(BaseException):
    pass


class CalibrationApp(main.App):
    def _start_calibration(self):
        # The real confirmation has saved the selected hardware averaging.
        if not self.fixture_verified:
            raise RuntimeError("Zero-PWM fixture has not passed")
        return main.App._start_calibration(self)


def no_restart():
    raise Stopped("Restart requested; helper stopped without resetting")


def poll_cancel(inputs):
    for event in inputs.poll(main.time.ticks_ms()):
        if event[0] == "left" or (event[0] == "touch" and
                8 <= event[1] < 234 and 274 <= event[2] < 316):
            raise Stopped("Cancelled during zero-output verification")


def zero_fixture(app):
    """Require ten stable seconds before any settings write or positive duty."""
    app.engine.instrument.zero()
    # A zero-duty set re-enables unaveraged continuous monitoring if a prior
    # one-shot conversion left the sensors powered down. Neither pin is driven high.
    app.engine.instrument.set_duty("gate", 0.0)
    app.engine.instrument.set_duty("drain", 0.0)
    deadline = main.time.ticks_ms()
    while main.time.ticks_diff(main.time.ticks_ms(), deadline) < 250:
        poll_cancel(app.inputs)
        main.time.sleep_ms(5)
    app.engine.settings = app.settings
    sums = {role: [0.0, 1e6, -1e6, 0.0] for role in ("drain", "gate")}
    samples = 0
    window_started = main.time.ticks_ms()
    while main.time.ticks_diff(main.time.ticks_ms(), window_started) < ZERO_STABILITY_MS:
        poll_cancel(app.inputs)
        # Production finite/envelope checks and BOTH raw current limits apply.
        app.engine.instrument.poll_alerts()
        pair = app.engine._checked_read()
        for role in ("drain", "gate"):
            voltage = pair[role]["terminal_v"]
            if ((role == "drain" and not DRAIN_ZERO_MIN_V <= voltage <= DRAIN_ZERO_MAX_V) or
                    (role == "gate" and not -0.050 <= voltage < 0.3)):
                print("SOURCE33_ZERO_REJECT", role, voltage,
                      pair[role]["bus_v"], pair[role]["shunt_v"])
                raise RuntimeError("Zero-PWM terminal voltage outside fixture bounds")
            values = sums[role]
            values[0] += voltage
            values[1] = min(values[1], voltage)
            values[2] = max(values[2], voltage)
            values[3] += pair[role]["shunt_v"]
            if values[2] - values[1] > ZERO_MAX_SPAN_V:
                print("SOURCE33_ZERO_UNSTABLE", role, "min_v", values[1],
                      "max_v", values[2], "span_v", values[2] - values[1])
                raise RuntimeError("Zero-PWM terminal voltage changed by more than 20 mV")
        samples += 1
        main.time.sleep_ms(5)
    if samples < 2:
        raise RuntimeError("Zero-PWM stability window did not produce enough readings")
    for role in ("drain", "gate"):
        values = sums[role]
        print("SOURCE33_ZERO_PASS", role, "samples", samples,
              "mean_v", values[0] / samples, "min_v", values[1],
              "max_v", values[2], "span_v", values[2] - values[1],
              "window_ms", main.time.ticks_diff(main.time.ticks_ms(), window_started),
              "mean_shunt_v", values[3] / samples)


def report_channel(role, channel):
    points = channel["points"]
    first, last = points[0], points[-1]
    baseline_sum = 0.0
    max_std = 0.0
    for point in points:
        baseline_sum += point["shunt_v"]
        max_std = max(max_std, point["shunt_std_v"])
    print("SOURCE33_CAL_CHANNEL", main.storage.json.dumps({
        "channel": role, "retained_points": len(points),
        "shunt_ohm": channel["shunt_ohm"],
        "floor_mean_v": first["terminal_v"],
        "floor_min_v": first.get("zero_min_v"),
        "floor_max_v": first.get("zero_max_v"),
        "floor_shunt_mean_v": first["shunt_v"],
        "ceiling_mean_v": last["terminal_v"],
        "ceiling_shunt_mean_v": last["shunt_v"],
        "baseline_mean_across_points_v": baseline_sum / len(points),
        "max_shunt_std_v": max_std, "capability": channel["capability"],
    }))


def run():
    instrument = app = wifi = cloud = None
    saved_average_count = None
    started = main.time.ticks_ms()
    try:
        # Match main.run: reserve/read both packed tables before UI and hardware
        # constructors fragment the remaining heap into smaller free blocks.
        settings = main.storage.load_settings(migrate=False)
        saved_average_count = settings["ina_average_count"]
        if settings["source_voltage_uv"] != 3300000:
            raise RuntimeError("Calibration helper requires the existing 3.3 V source setting")
        if (isinstance(CALIBRATION_AVERAGES, bool) or not isinstance(CALIBRATION_AVERAGES, int)
                or CALIBRATION_AVERAGES not in main.model.INA_AVERAGE_COUNTS):
            raise RuntimeError("Unsupported calibration hardware average count")
        # Keep only settings/scalar provenance outside App, never an old map.
        expected_settings = dict(settings)
        expected_settings["ina_average_count"] = CALIBRATION_AVERAGES
        calibration = main.storage.load_calibration()
        if calibration is not None:
            main.model.validate_calibration(calibration)
        main.gc.collect()
        display = main.Display(raster_pool=main._display_buffer)
        instrument = main.Instrument()
        inputs = main.Inputs()
        wifi = main.WifiManager()
        if wifi.pause() is not True:
            raise RuntimeError("Wi-Fi could not be disabled")
        scan_buffer = main.PackedScanBuffer(main._scan_bytes)
        cloud = main.RamCloudManager(scan_buffer, transport=main._upload_transport,
                                    utc_provider=main.scan_utc)
        if calibration is not None:
            cloud.prepare_calibration(calibration)
        app = CalibrationApp(display, inputs, main.Engine(instrument), settings,
                             calibration, error_logger=main.record_error,
                             store=main.storage, wifi=wifi, cloud=cloud, scan_buffer=scan_buffer,
                             calibration_on_disk=calibration is not None,
                             restart=no_restart, validated_calibration=calibration)
        settings = calibration = None
        app.fixture_verified = False
        app.message("Checking zero outputs. LEFT / CANCEL stops.", "advanced")
        app.render()
        main.gc.collect()
        print("SOURCE33_CAL_SETUP", "free_heap", main.gc.mem_free(),
              "driver_supply_uv", app.settings["driver_supply_uv"],
              "saved_ina_average_count", saved_average_count, "radio", wifi.state,
              "drain_zero_min_v", DRAIN_ZERO_MIN_V,
              "drain_zero_max_v", DRAIN_ZERO_MAX_V,
              "zero_stability_ms", ZERO_STABILITY_MS,
              "zero_max_span_v", ZERO_MAX_SPAN_V)
        zero_fixture(app)
        app.fixture_verified = True

        # Execute the real Settings -> Advanced -> Averaging -> Calibrate actions.
        # Fresh WifiManager has no connection intent: resume cannot connect.
        app.goto("home")
        app.action("left")
        app._pause_wifi()
        app.action("advanced")
        app.action("averaging")
        app.action(("ina_average", CALIBRATION_AVERAGES))
        app.action("right")
        app.action("calibrate")
        if app.screen != "confirm_calibration":
            raise RuntimeError("Could not prepare the calibration settings action")
        poll_cancel(inputs)
        app.action("right")
        if app.engine.state != "calibrating":
            raise RuntimeError(getattr(app, "notice", "Calibration did not start"))
        if app._calibration_writer is None or app.calibration is not None:
            raise RuntimeError("Calibration did not preopen/release the previous map")
        if main.storage.load_settings(migrate=False) != expected_settings:
            raise RuntimeError("Saved settings changed beyond the hardware average count")
        expected_settings = None
        print("SOURCE33_CAL_BEGIN", "ina_average_count", CALIBRATION_AVERAGES,
              "calibration_repeat_blocks", main.model.CALIBRATION_REPEAT_BLOCKS,
              "free_heap", main.gc.mem_free())
        last_report = main.time.ticks_ms()
        minimum_observed_free = main.gc.mem_free()
        while app.engine.state == "calibrating":
            now = main.time.ticks_ms()
            if main.time.ticks_diff(now, started) >= CALIBRATION_TIMEOUT_MS:
                raise Stopped("Calibration exceeded the configured time limit")
            app.tick(now)  # Real button/touch polling, rendering, engine and save.
            if main.time.ticks_diff(now, last_report) >= 10000:
                last_report = now
                free = main.gc.mem_free()
                minimum_observed_free = min(minimum_observed_free, free)
                print("SOURCE33_CAL_PROGRESS", "elapsed_ms",
                      main.time.ticks_diff(now, started), "index", app.engine._index,
                      "total", app.engine._total, "phase", app.engine._phase,
                      "channel_counts", app.engine._cal_counts, "free_heap", free)
            main.time.sleep_ms(2)
        if app.engine.state == "cancelled":
            raise Stopped("Calibration cancelled; previous committed map retained")
        if (app.engine.state != "complete" or app.calibration is None or
                app.calibration is not app.engine.calibration or
                app._calibration_writer is not None or
                app.notice != "Calibration saved. Both outputs are at zero drive."):
            raise RuntimeError(app.engine.error or getattr(app, "notice", "Calibration failed"))
        main.model.validate_calibration(app.calibration, app.settings)
        metadata = app.calibration["metadata"]
        if (metadata["ina_average_count"] != CALIBRATION_AVERAGES or
                metadata["calibration_repeat_blocks"] != main.model.CALIBRATION_REPEAT_BLOCKS):
            raise RuntimeError("Calibration averaging metadata is incorrect")
        for role in ("drain", "gate"):
            report_channel(role, app.calibration["channels"][role])
        main.gc.collect()
        print("SOURCE33_CAL_SAVED", "elapsed_ms",
              main.time.ticks_diff(main.time.ticks_ms(), started),
              "drain_shunt_provenance", "drain_shunt_calibration" in metadata,
              "free_heap", main.gc.mem_free(),
              "minimum_observed_free_heap", minimum_observed_free)
    except Stopped as error:
        print("SOURCE33_CAL_STOP", str(error))
    except BaseException as error:
        print("SOURCE33_CAL_FAIL", type(error).__name__, str(error))
        raise
    finally:
        if app is not None:
            try:
                app.engine.cancel()
                app._release_calibration_candidate()
            except BaseException:
                pass
            writer = app._calibration_writer
            app._calibration_writer = None
            if writer is not None:
                try:
                    writer.abort()
                except BaseException:
                    print("SOURCE33_CAL_CLEANUP", "writer abort failed")
        if instrument is not None:
            try:
                instrument.close()
            except BaseException:
                print("SOURCE33_CAL_CLEANUP", "instrument close failed")
        pins_low = True
        for pin in (21, 20):
            try:
                main.Pin(pin, main.Pin.OUT, value=0)
                pins_low = pins_low and main.Pin(pin).value() == 0
            except BaseException:
                pins_low = False
                print("SOURCE33_CAL_CLEANUP", "pin low failed", pin)
        for name, service in (("cloud", cloud), ("wifi", wifi)):
            if service is not None:
                try:
                    service.pause()
                except BaseException:
                    print("SOURCE33_CAL_CLEANUP", name, "pause failed")
        print("SOURCE33_CAL_DONE", "pins_low", pins_low,
              "selected_ina_average_count", CALIBRATION_AVERAGES, "soft_reset", False)


if __name__ == "__main__":
    run()
