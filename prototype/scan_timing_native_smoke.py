"""Real-LCD timing smoke test with synthetic measurements and outputs low.

Run only from an idle REPL after stopping the application. Import main first
to reserve the production buffers, then import this precompiled helper and
call run(). No physical Instrument/PWM, Wi-Fi, upload, file writes, or reset.
The production RAM scan buffer is overwritten and discarded at completion.
DOT_US lines use production ScanTiming's default stdout writer unchanged.
"""
import main
import scan_timing


class SyntheticInstrument:
    identity = {"synthetic_test": True}

    def __init__(self, gate_pin, drain_pin, timing):
        self.gate_pin, self.drain_pin, self.timing = gate_pin, drain_pin, timing
        self.duties = {"drain": 0.0, "gate": 0.0}
        self.started = 0
        self.duration = 0

    def check_low(self):
        assert self.gate_pin.value() == 0 and self.drain_pin.value() == 0

    def set_duty(self, role, fraction):
        assert role in self.duties and 0 <= fraction <= 1
        self.check_low()
        self.duties[role] = fraction  # Logical values only: never create PWM.

    def zero(self):
        self.gate_pin.value(0)
        self.drain_pin.value(0)
        self.duties["gate"] = self.duties["drain"] = 0.0

    def poll_alerts(self):
        self.check_low()

    def begin_measurement(self, count):
        assert count == 64
        self.check_low()
        self.started = main.time.ticks_us()
        self.duration = count * 2200
        self.timing.count(scan_timing.ADC_BLOCKS)

    def poll_measurement(self):
        self.check_low()
        self.timing.count(scan_timing.POLLS)
        return main.time.ticks_diff(main.time.ticks_us(), self.started) >= self.duration

    def read_pair(self):
        self.check_low()
        result = {}
        for role, resistance in (("drain", main.model.DRAIN_SHUNT_OHM),
                                 ("gate", main.model.GATE_SHUNT_OHM)):
            terminal = self.duties[role] * main.model.PWM_FULL_SCALE_V
            current = terminal * 20e-6 if role == "drain" else 0.0
            shunt = current * resistance
            result[role] = {"terminal_v": terminal, "bus_v": terminal + shunt,
                            "shunt_v": shunt}
        return result


class Inputs:
    def __init__(self):
        self.next = None

    def poll(self, now):
        event, self.next = self.next, None
        return ((event,),) if event is not None else ()


class Store:
    def load_json(self, path):
        assert path == "cloud.json"
        return {"version": 1, "enabled": True,
                "url": "https://script.google.com/macros/s/TIMING_SMOKE_NO_NETWORK/exec"}

    def save_settings(self, *args):
        raise AssertionError("Timing smoke must not write settings")

    save_json_atomic = save_settings
    save_calibration_atomic = save_settings


class NoNetworkTransport:
    def close(self):
        pass

    def start(self, *args):
        raise AssertionError("Timing smoke must not start an upload")

    def step(self):
        raise AssertionError("Timing smoke must not access the network")


def run():
    gate_pin = main.Pin(21, main.Pin.OUT, value=0)
    drain_pin = main.Pin(20, main.Pin.OUT, value=0)
    timing = main._scan_timing
    timing.stop()
    instrument = SyntheticInstrument(gate_pin, drain_pin, timing)
    cloud = engine = None
    try:
        assert main.tls_memory.available(), "Production TLS reserve is missing"
        config = dict(main.model.DEFAULT_SETTINGS)
        config.update(drain_start_uv=500000, drain_stop_uv=1000000, drain_step_uv=500000,
                      gate_start_uv=1000000, gate_stop_uv=1000000, gate_step_uv=500000,
                      driver_supply_uv=5000000, source_voltage_uv=0,
                      ina_average_count=64, settle_ms=200)
        calibration = {"version": 1, "metadata": {
            "synthetic_test": True, "driver_supply_v": 5.0, "pwm_hz": main.model.PWM_HZ,
            "acquisition_mode": main.model.ACQUISITION_MODE, "ina_average_count": 64,
            "bus_conversion_us": 1100, "shunt_conversion_us": 1100,
            "calibration_repeat_blocks": 3}, "channels": {}}
        for role, resistance in (("drain", main.model.DRAIN_SHUNT_OHM),
                                 ("gate", main.model.GATE_SHUNT_OHM)):
            calibration["channels"][role] = {"shunt_ohm": resistance, "points": [
                {"terminal_v": 0.0, "shunt_v": 0.0, "duty": 0.0},
                {"terminal_v": 3.0, "shunt_v": 0.0,
                 "duty": 3.0 / main.model.PWM_FULL_SCALE_V}]}
        main.model.validate_scan_calibration(calibration, config)
        main.gc.collect()
        display = main.Display(raster_pool=main._display_buffer)
        inputs = Inputs()
        buffer = main.PackedScanBuffer(main._scan_bytes)
        store = Store()
        cloud = main.RamCloudManager(buffer, transport=NoNetworkTransport(), store=store,
                                    device_id="cafe", random_bytes=lambda count: b"\x01" * count)
        engine = main.Engine(instrument, timing=timing)
        restarted = [0]

        def restart_callback():
            restarted[0] += 1  # Assert the normal restart request; never reset.

        app = main.App(display, inputs, engine, config, calibration, store=store,
                       cloud=cloud, scan_buffer=buffer, restart=restart_callback,
                       validated_calibration=calibration, timing=timing)
        config = calibration = None
        app.tick(main.time.ticks_ms())
        assert app.screen == "home" and not app.dirty
        print("SCAN_TIMING_SMOKE_BEGIN synthetic=1 outputs_low=1 points=2 average=64 settle_ms=200")
        inputs.next = "right"
        started, reports = main.time.ticks_ms(), 0
        while True:
            assert main.time.ticks_diff(main.time.ticks_ms(), started) < 30000, "Timing smoke timed out"
            app.tick(main.time.ticks_ms())
            instrument.check_low()
            if timing.last_index == reports:
                assert timing.last_curve == 0 and timing.last_wall_us > 0
                assert timing.last[scan_timing.ADC_BLOCKS] == (3 if reports == 0 else 1)
                assert timing.last[scan_timing.ENGINE] > 0 and timing.last[scan_timing.I2C] == 0
                assert timing.last[scan_timing.RECORD] > 0
                if reports == 0:
                    # The real clock includes calls that find no pending LCD
                    # work; the first point has no prior point to draw.
                    assert timing.last[scan_timing.RENDER] >= 0
                else:
                    assert timing.last[scan_timing.RENDER] > 0
                assert sum(timing.last[scan_timing.OTHER:]) == timing.last_wall_us
                assert timing.drop_count == 0 and not timing.report_failed, "USB report failed"
                reports += 1
            if app.screen == "upload":
                break  # Do not enter preparation, Wi-Fi, or network upload ticks.
            assert app.screen not in ("scan_error", "upload_error", "notice"), engine.error or "UI error"
            sleep_started = timing.stamp()
            main.time.sleep_ms(2)
            timing.add(scan_timing.SLEEP, sleep_started)
        assert reports == 2 and len(buffer) == 2 and engine.state == "complete"
        assert engine.shutdown_succeeded and cloud.state == "preparing"
        assert main.tls_memory.available() and not timing.active
        print("SCAN_TIMING_SMOKE_COMPLETE reports=2 synthetic_i2c_us=0 network=0 files_written=0")
        inputs.next = "left"
        app.tick(main.time.ticks_ms())
        assert restarted[0] == 1 and app._restart_called and len(buffer) == 0
        assert cloud.state == "idle" and not timing.active
        instrument.check_low()
        display.fill(main.BG)
        display.text("TIMING TEST PASS", 16, 24, main.INK, bg=main.BG, scale=2)
        display.text("Synthetic scan. Outputs held low.", 16, 64, main.INK, bg=main.BG)
        print("SCAN_TIMING_SMOKE_PASS reports=2 restart_callback=1 outputs_low=1 reset=0")
    finally:
        timing.stop()
        try:
            if engine is not None and engine.state == "scanning":
                engine.cancel()
            if cloud is not None:
                cloud.discard_scan()
        finally:
            instrument.zero()
            main.Pin(21, main.Pin.OUT, value=0)
            main.Pin(20, main.Pin.OUT, value=0)


if __name__ == "__main__":
    run()
