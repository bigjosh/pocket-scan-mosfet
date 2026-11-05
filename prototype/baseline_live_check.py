"""Physical open-output current calibration and independent residual check.

Run on a fresh Pico interpreter ONLY after confirming both outputs are open,
the DUT/probes/loads are removed, and the INA226 drain / INA228 gate shunts are unbridged:
    import baseline_live_check; baseline_live_check.run()

Uses main's actual boot reservations and production instrument/display/input
drivers. Settings, credentials and receiver configuration are never changed.
No scan is uploaded. Calibration is staged in a separate binary, reloaded and
checked with seven voltage pairs and sixteen fresh blocks/pair before its final
atomic rename. Cancel, transport failure or timeout before that rename leaves
the old committed baseline intact. Both tables and source-relative zero anchors
are retained, but current correction is applied only to drain. Gate current is
always the raw INA228 shunt measurement divided by the fitted shunt resistance.
Reported unloaded currents are not used as an invented current trip or pass/fail
threshold. The host returns to normal Home with a soft reset after this helper.
"""

import main
import time
import gc
import os
from machine import Pin
import current_baseline as cb
import model

_TIMEOUT_MS = 480000
_RESIDUAL_BLOCKS = 16
_PAIRS = 7
_STAGE = "current-baseline.live-stage.bin"


class Cancelled(Exception):
    pass


def _pins_low():
    try:
        Pin(21, Pin.OUT, value=0)
    finally:
        Pin(20, Pin.OUT, value=0)


def _target_uv(settings, index):
    source, rail = settings["source_voltage_uv"], settings["driver_supply_uv"]
    if index == 0:
        return source, source
    if index == 1:
        return min(rail, source + 100000), 0
    if index == 2:
        return min(rail, source + 1000000), max(0, source - 200000)
    if index == 3:
        return min(rail, source + 5000000), min(rail, source + 2000000)
    if index == 4:
        return min(rail, source + 10000000), min(rail, source + 5000000)
    if index == 6:
        return min(rail, source + 100000), min(rail, 100000)
    return rail, rail


class LiveCheck:
    def __init__(self, instrument, inputs, display, settings):
        self.instrument, self.inputs, self.display = instrument, inputs, display
        self.settings = settings
        self.started_ms = time.ticks_ms()
        self.codes = [0, 0, 0, 0]
        self.sums = [0, 0, 0, 0, 0]
        self.maximum_residual_ua = [0.0, 0.0]
        self.last_percent = -1
        self.last_channel = None
        self.controller = None
        self.table = None
        self.committed = False

    def service(self):
        now = time.ticks_ms()
        if time.ticks_diff(now, self.started_ms) >= _TIMEOUT_MS:
            raise RuntimeError("Open-output check timed out")
        for event in self.inputs.poll(now):
            if event[0] == "left" or (event[0] == "touch" and event[1] < 240 and event[2] >= 265):
                raise Cancelled("Open-output check cancelled")
        return now

    def paint(self, title, detail):
        display = self.display
        display.fill(main.BG)
        display.text(title, 16, 24, main.INK, bg=main.BG, scale=2)
        display.text(detail, 16, 88, main.INK, bg=main.BG)
        display.text("DUT / loads / probes must stay removed", 16, 126, main.INK, bg=main.BG)
        display.text("LEFT / CANCEL stops this check", 16, 282, main.INK, bg=main.BG)

    def calibrate(self):
        self.paint("OPEN OUTPUT CAL", "Preparing fixed baseline workspace")
        self.controller = main.create_calibrator(self.instrument, self.settings)
        gc.collect()
        print("BASELINE_LIVE_BEGIN", self.settings["driver_supply_uv"],
              self.settings["source_voltage_uv"], self.settings["ina_average_count"],
              self.settings["settle_ms"], gc.mem_free())
        self.controller.start()
        while self.controller.state == "running":
            now = self.service()
            self.controller.tick(now)
            percent = int(self.controller.progress * 100) // 5 * 5
            channel = self.controller.channel
            if percent != self.last_percent or channel != self.last_channel:
                self.last_percent, self.last_channel = percent, channel
                print("BASELINE_LIVE_PROGRESS", channel, percent, gc.mem_free())
                self.paint("OPEN OUTPUT CAL", "%s %d%%" % (channel, percent))
            time.sleep_ms(2)
        if self.controller.state != "complete" or not self.controller.shutdown_succeeded:
            raise RuntimeError("Calibration failed: " + str(self.controller.error or self.controller.state))
        candidate = self.controller.result
        assert candidate.available
        expected_hash = candidate.sha256
        # Only the separate staging file changes before all live checks finish.
        self.paint("VERIFY BASELINE", "Writing and reloading staged binary")
        gc.collect()
        candidate.save(_STAGE)
        self.table = cb.Baseline(main._baseline_bytes)
        if not self.table.load(_STAGE) or self.table.sha256 != expected_hash:
            raise RuntimeError("Staged baseline verification failed")
        if not self.table.compatible(self.settings, self.instrument.identity):
            raise RuntimeError("Staged baseline does not match this hardware/settings")
        print("BASELINE_LIVE_STAGED", expected_hash, cb.BUFFER_BYTES,
              self.table.count(0), self.table.count(1))
        metadata = self.table.metadata(self.settings)
        for role in cb.ROLES:
            zero = metadata[role]
            print("BASELINE_LIVE_ZERO", role, zero["zero_duty_u16"],
                  zero["zero_residual_uv"], zero["zero_valid"],
                  zero["min_bus_uv"], zero["max_bus_uv"])

    def _read_block(self, drain_duty, gate_duty):
        # Mirror normal scan history: continuous monitoring while settling,
        # one raw endpoint check, then one explicitly triggered average.
        self.instrument.set_duty("drain", drain_duty)
        self.instrument.set_duty("gate", gate_duty)
        applied = time.ticks_ms()
        last_poll = applied
        while True:
            now = self.service()
            if time.ticks_diff(now, last_poll) >= 5:
                self.instrument.poll_alerts()
                last_poll = now
            if time.ticks_diff(now, applied) >= self.settings["settle_ms"]:
                break
            time.sleep_ms(2)
        self.instrument.read_codes_into(self.codes)
        count = self.settings["ina_average_count"]
        self.instrument.begin_measurement(count)
        triggered = time.ticks_ms()
        deadline = (count * 2420 + 999) // 1000 + 50
        while True:
            now = self.service()
            if self.instrument.poll_measurement():
                break
            if time.ticks_diff(now, triggered) >= deadline:
                raise RuntimeError("Residual conversion timed out")
            time.sleep_ms(5)
        self.instrument.read_codes_into(self.codes)

    def residuals(self):
        print("BASELINE_LIVE_RESIDUAL_COLUMNS", "pair channel terminal_V relative_V raw_uA reported_uA basis blocks")
        for index in range(_PAIRS):
            self.service()
            drain_uv, gate_uv = _target_uv(self.settings, index)
            self.paint("CHECKING ZERO CURRENT", "Pair %d/%d; %d independent blocks" %
                       (index + 1, _PAIRS, _RESIDUAL_BLOCKS))
            drain_duty = self.table.drive_duty("drain", model.volts_from_uv(drain_uv), self.settings)
            gate_duty = self.table.drive_duty("gate", model.volts_from_uv(gate_uv), self.settings)
            for slot in range(5):
                self.sums[slot] = 0
            for block in range(_RESIDUAL_BLOCKS):
                self._read_block(drain_duty, gate_duty)
                for slot in range(4):
                    self.sums[slot] += self.codes[slot]
                self.sums[4] += self.table.offset_nv(0, self.codes[0] * 1250)
            for role in range(2):
                resistance = model.DRAIN_SHUNT_OHM if role == 0 else model.GATE_SHUNT_OHM
                bus_sum, shunt_sum = self.sums[role * 2], self.sums[role * 2 + 1]
                # Average native codes before rounding physical units. Divide
                # before multiplying to keep even 20-bit sums in small ints.
                if role == 0:
                    bus_uv = cb._round_scaled(bus_sum, 1250, _RESIDUAL_BLOCKS)
                    shunt_nv = cb._round_scaled(shunt_sum, 2500, _RESIDUAL_BLOCKS)
                else:
                    bus_uv = cb._round_scaled(bus_sum, 3125, _RESIDUAL_BLOCKS * 16)
                    shunt_nv = cb._round_scaled(shunt_sum, 625, _RESIDUAL_BLOCKS * 2)
                terminal_v = bus_uv / 1000000 - shunt_nv / 1000000000
                relative_v = terminal_v - model.volts_from_uv(self.settings["source_voltage_uv"])
                raw_ua = shunt_nv / (1000 * resistance)
                if role == 0:
                    baseline_nv = cb._round_div(self.sums[4], _RESIDUAL_BLOCKS)
                    reported_ua = (shunt_nv - baseline_nv) / (1000 * resistance)
                    basis = "baseline_corrected"
                else:
                    # Keep this check identical to normal scans: no gate tare
                    # lookup or subtraction, even when a gate table is saved.
                    reported_ua, basis = raw_ua, "raw"
                self.maximum_residual_ua[role] = max(self.maximum_residual_ua[role], abs(reported_ua))
                print("BASELINE_LIVE_RESIDUAL", index, cb.ROLES[role], terminal_v,
                      relative_v, raw_ua, reported_ua, basis, _RESIDUAL_BLOCKS)
            self.instrument.zero()
        print("BASELINE_LIVE_RESIDUAL_MAX_UA", "drain_corrected", self.maximum_residual_ua[0],
              "gate_raw", self.maximum_residual_ua[1])

    def commit(self):
        self.service()  # Last opportunity for CANCEL before the atomic commit.
        self.instrument.zero()
        expected_hash = self.table.sha256
        getattr(os, "replace", os.rename)(_STAGE, cb.PATH)
        self.committed = True
        if hasattr(os, "sync"):
            os.sync()
        if not self.table.load(cb.PATH) or self.table.sha256 != expected_hash:
            raise RuntimeError("Committed baseline verification failed")
        print("BASELINE_LIVE_SAVED", expected_hash, cb.BUFFER_BYTES)
        self.paint("BASELINE SAVED", "Outputs disabled; host will restart Home")


def run():
    instrument = display = inputs = wifi = check = None
    succeeded = False
    cleanup_failed = False
    _pins_low()
    try:
        settings = main.storage.load_settings(migrate=False)
        # This helper is authorized for the present open fixture, not arbitrary
        # future rails. Fail before positive PWM if the saved setup changed.
        if settings["source_voltage_uv"] != 3300000 or settings["driver_supply_uv"] != 18800000:
            raise ValueError("Expected present 3.3 V source / 18.8 V driver settings")
        blocks = 2 * (cb.SWEEP_POINTS + cb.ZERO_STEPS) * cb.BLOCKS_PER_KNOT + _PAIRS * _RESIDUAL_BLOCKS
        estimate = blocks * (settings["settle_ms"] + (settings["ina_average_count"] * 2420 + 999) // 1000 + 50)
        if estimate + 60000 >= _TIMEOUT_MS:
            raise ValueError("Saved acquisition settings exceed live-check deadline")
        wifi = main.WifiManager()
        wifi.pause()
        display = main.Display(raster_pool=main._display_buffer)
        inputs = main.Inputs()
        instrument = main.Instrument(timing=main._scan_timing)
        check = LiveCheck(instrument, inputs, display, settings)
        check.calibrate()
        check.residuals()
        check.commit()
        succeeded = True
    except BaseException as error:
        print("BASELINE_LIVE_ABORT", type(error).__name__, str(error),
              "committed", bool(check is not None and check.committed))
        raise
    finally:
        if instrument is not None:
            try:
                instrument.close()
            except Exception as error:
                cleanup_failed = True
                print("BASELINE_LIVE_CLEANUP_ERROR", type(error).__name__)
        try:
            _pins_low()
        except Exception as error:
            cleanup_failed = True
            print("BASELINE_LIVE_PIN_ERROR", type(error).__name__)
        if wifi is not None:
            try:
                wifi.pause()
            except Exception:
                cleanup_failed = True
        try:
            os.remove(_STAGE)
        except OSError:
            pass
        main._scan_timing.stop()
        if not succeeded and display is not None:
            try:
                display.fill(main.BG)
                display.text("CALIBRATION STOPPED", 16, 24, main.RED, bg=main.BG, scale=2)
                display.text("Power off: shutdown failed." if cleanup_failed else
                             "Outputs disabled. See USB log.", 16, 90, main.INK, bg=main.BG)
            except Exception:
                pass
        print("BASELINE_LIVE_CLEANUP", not cleanup_failed)
    if cleanup_failed:
        raise RuntimeError("Live calibration cleanup failed")
    assert Pin(21).value() == 0 and Pin(20).value() == 0
    print("BASELINE_LIVE_PASS", time.ticks_diff(time.ticks_ms(), check.started_ms), gc.mem_free())
