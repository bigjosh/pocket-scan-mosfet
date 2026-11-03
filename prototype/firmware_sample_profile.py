"""Retired profiler for the former continuous/Pico-batch acquisition path.

The preserved implementation below assumes sixteen settling reads and legacy
flash archives. Those assumptions do not describe INA226 triggered averaging.
Do not run it on current firmware; it intentionally fails before importing any
hardware module, changing pins, opening files or enabling outputs.
"""
raise RuntimeError("Retired sampling profiler: incompatible with INA226 triggered acquisition")

# Historical implementation, retained only to interpret old profiling logs.
SETTLE_SAMPLES = 16
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import main
import tls_memory
assert main._tls_reserved, "TLS startup reserve unavailable"
upload_transport = main._upload_transport
plot_buffers = main._plot_buffers
import gc
import hashlib
import json
import time
import storage
from archive import Archive
from cloud import CloudManager
from display import Display
from engine import Engine
from hardware import Instrument
from inputs import Inputs
from ui import App
from wifi import WifiManager

# Use normal main.py import order, then allocate the unchanged display cache
# before constructing the diagnostic subclasses and their bookkeeping.
gc.collect()
if "display" not in globals():
    display = Display()

SAVE_PROFILE_SCAN = bool(globals().get("SAVE_PROFILE_SCAN", False))


class Profile:
    def __init__(self):
        self.enabled = False
        self.metrics = {key: [0, 0] for key in (
            "read_pair", "engine_tick", "input_poll", "render_union",
            "record_point", "finish_scan", "loop_sleep", "start_action_excluding_render",
            "app_tick")}
        self.render_depth = 0
        self.render_started = 0
        self.render_calls = {key: 0 for key in ("render", "live", "points", "axes")}
        self.stages = {key: 0 for key in ("park_drain", "settle_gate", "settle")}
        self.verification_reads = dict(self.stages)
        self.windows = dict(self.stages)
        self.correction_windows = dict(self.stages)
        self.correction_attempts = {"drain": 0, "gate": 0}
        self.averaging_reads = 0
        self.archive_duration_ms = None

    def add(self, key, started):
        metric = self.metrics[key]
        metric[0] += 1
        metric[1] += time.ticks_diff(time.ticks_us(), started)

    def render_enter(self, key):
        self.render_calls[key] += 1
        if self.render_depth == 0:
            self.render_started = time.ticks_us()
        self.render_depth += 1

    def render_exit(self):
        self.render_depth -= 1
        if self.render_depth == 0:
            self.add("render_union", self.render_started)


profile = Profile()


class ProfileInstrument(Instrument):
    def read_pair(self):
        if not profile.enabled:
            return Instrument.read_pair(self)
        started = time.ticks_us()
        try:
            return Instrument.read_pair(self)
        finally:
            profile.add("read_pair", started)


class ProfileInputs(Inputs):
    def poll(self, now_ms):
        if not profile.enabled:
            return Inputs.poll(self, now_ms)
        started = time.ticks_us()
        try:
            return Inputs.poll(self, now_ms)
        finally:
            profile.add("input_poll", started)


class ProfileEngine(Engine):
    def tick(self, now_ms):
        if not profile.enabled:
            return Engine.tick(self, now_ms)
        started = time.ticks_us()
        try:
            return Engine.tick(self, now_ms)
        finally:
            profile.add("engine_tick", started)

    def _reset_settle(self):
        # Called only when a new park/gate/drain stage begins. Corrections
        # restart a window without calling this method, and are counted below.
        result = Engine._reset_settle(self)
        if profile.enabled:
            profile.stages[self._phase] += 1
        return result

    def _settle_average(self, pair):
        if profile.enabled:
            profile.verification_reads[self._phase] += 1
        return Engine._settle_average(self, pair)

    def _check_settled(self, pair, targets=None, strict_roles=()):
        if not profile.enabled:
            return Engine._check_settled(self, pair, targets, strict_roles)
        phase = self._phase
        before_drain = self._settle["drain"]["attempts"]
        before_gate = self._settle["gate"]["attempts"]
        profile.windows[phase] += 1
        result = Engine._check_settled(self, pair, targets, strict_roles)
        profile.correction_attempts["drain"] += self._settle["drain"]["attempts"] - before_drain
        profile.correction_attempts["gate"] += self._settle["gate"]["attempts"] - before_gate
        if not result:
            profile.correction_windows[phase] += 1
        return result

    def _sample(self, pair):
        result = Engine._sample(self, pair)
        if profile.enabled:
            profile.averaging_reads += 1
        return result


class ProfileArchive(Archive):
    def record_point(self, row):
        if not profile.enabled:
            return Archive.record_point(self, row)
        started = time.ticks_us()
        try:
            return Archive.record_point(self, row)
        finally:
            profile.add("record_point", started)

    def finish_scan(self, now_ms):
        if not profile.enabled:
            return Archive.finish_scan(self, now_ms)
        profile.archive_duration_ms = time.ticks_diff(now_ms, self._started_ms)
        started = time.ticks_us()
        try:
            return Archive.finish_scan(self, now_ms)
        finally:
            profile.add("finish_scan", started)


class ProfileApp(App):
    def _plot_axes(self):
        if not profile.enabled:
            return App._plot_axes(self)
        profile.render_enter("axes")
        try:
            return App._plot_axes(self)
        finally:
            profile.render_exit()

    def render(self):
        if not profile.enabled:
            return App.render(self)
        profile.render_enter("render")
        try:
            return App.render(self)
        finally:
            profile.render_exit()

    def _render_live(self):
        if not profile.enabled:
            return App._render_live(self)
        profile.render_enter("live")
        try:
            return App._render_live(self)
        finally:
            profile.render_exit()

    def _draw_points(self):
        if not profile.enabled:
            return App._draw_points(self)
        profile.render_enter("points")
        try:
            return App._draw_points(self)
        finally:
            profile.render_exit()


def digest(path):
    result = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(512)
            if not chunk:
                break
            result.update(chunk)
    return result.digest()


def utc():
    value = time.gmtime()
    if 2025 <= value[0] <= 2099:
        return "%04d-%02d-%02dT%02d:%02d:%02dZ" % value[:6]
    return None


hardware = wifi = cloud = app = None
try:
    hardware = ProfileInstrument()
    hardware.zero()
    inputs = ProfileInputs()
    wifi = WifiManager()
    wifi.pause()
    settings = storage.load_settings()
    assert settings["driver_supply_uv"] == 5000000 and settings["source_voltage_uv"] == 0
    before = [digest(name) for name in ("settings.json", "calibration.json")]
    samples, batches = settings["samples_per_batch"], settings["batches"]
    settings.update(drain_start_uv=500000, drain_stop_uv=1000000, drain_step_uv=500000,
                    gate_start_uv=1400000, gate_stop_uv=1500000, gate_step_uv=100000)
    calibration = storage.load_json("calibration.json")
    engine = ProfileEngine(hardware)
    cloud = CloudManager(archive=ProfileArchive(utc_provider=utc), transport=upload_transport)
    gc.collect()  # Same placement as main.py, before the fixed plot buffers.
    app = ProfileApp(display, inputs, engine, settings, calibration, wifi=wifi, cloud=cloud,
                     plot_buffers=plot_buffers)
    app.render()
    prior_pending = cloud.status["pending_count"]
    app._now_ms = time.ticks_ms()
    wall_started = time.ticks_us()
    deadline_started = time.ticks_ms()
    profile.enabled = True
    render_before_start = profile.metrics["render_union"][1]
    started = time.ticks_us()
    app.action("right")
    profile.add("start_action_excluding_render", started)
    # action() may render immediately. Its rendering already belongs to the
    # union category, so subtract that nested time from the additive start.
    profile.metrics["start_action_excluding_render"][1] -= (
        profile.metrics["render_union"][1] - render_before_start)
    assert engine.state == "scanning", "Profile scan did not start"
    while engine.state == "scanning":
        started = time.ticks_us()
        app.tick(time.ticks_ms())
        profile.add("app_tick", started)
        assert time.ticks_diff(time.ticks_ms(), deadline_started) < 180000, "Profile scan timed out"
        if engine.state == "scanning":
            started = time.ticks_us()
            time.sleep_ms(2)
            profile.add("loop_sleep", started)
    wall_us = time.ticks_diff(time.ticks_us(), wall_started)
    profile.enabled = False
    assert engine.state == "complete", engine.error or "Profile scan was cancelled"
    assert engine.shutdown_succeeded and len(app.curves) == 4 and app._archive_ready
    assert profile.averaging_reads == 4 * samples * batches
    assert sum(profile.verification_reads.values()) == SETTLE_SAMPLES * sum(profile.windows.values())
    assert profile.metrics["read_pair"][0] == profile.averaging_reads + sum(profile.verification_reads.values())
    app.action("right" if SAVE_PROFILE_SCAN else "left")
    assert app.screen == "home"
    assert cloud.status["pending_count"] == prior_pending + int(SAVE_PROFILE_SCAN)
    assert before == [digest(name) for name in ("settings.json", "calibration.json")]
    time.sleep_ms(250)
    idle = hardware.read_pair()
    assert all(abs(idle[role]["terminal_v"]) < 0.05 for role in ("drain", "gate"))
    exclusive_keys = ("engine_tick", "input_poll", "render_union", "record_point",
                      "finish_scan", "loop_sleep", "start_action_excluding_render")
    accounted = sum(profile.metrics[key][1] for key in exclusive_keys)
    summary = {
        "wall_us": wall_us, "archive_duration_ms": profile.archive_duration_ms,
        "points": 4, "samples_per_batch": samples, "batches": batches,
        "sample_pause_ms": settings["sample_pause_ms"], "settle_ms": settings["settle_ms"],
        "metrics_count_us": profile.metrics, "additive_categories": exclusive_keys,
        "unattributed_us": wall_us - accounted,
        "nested_categories": {"read_pair": "subset of engine_tick", "app_tick": "inclusive loop body"},
        "render_calls_including_nested": profile.render_calls,
        "settling_stages": profile.stages, "verification_reads": profile.verification_reads,
        "verification_windows": profile.windows, "correction_windows": profile.correction_windows,
        "correction_attempts_by_channel": profile.correction_attempts,
        "averaging_reads": profile.averaging_reads,
        "saved": SAVE_PROFILE_SCAN, "settings_and_calibration_preserved": True,
        "outputs_parked": True, "tls_reserve_bytes": tls_memory.reserved_bytes(),
    }
    if SAVE_PROFILE_SCAN:
        summary["scan_id"] = cloud.archive._accepted_id
    print("SAMPLE_PROFILE", json.dumps(summary))
finally:
    profile.enabled = False
    try:
        if hardware is not None:
            hardware.close()
    finally:
        Pin(20, Pin.OUT, value=0)
        Pin(21, Pin.OUT, value=0)
        try:
            if cloud is not None:
                cloud.pause()
                if app is not None and app._archive_active:
                    cloud.discard_scan()
        finally:
            if wifi is not None:
                wifi.pause()
    print("PROFILE_RADIO_AND_OUTPUTS_DISABLED")
