"""Native, offline smoke test for the direct scanner's fixed production heap.

Compile this helper separately and run ``import direct_memory_smoke;
direct_memory_smoke.run()`` on a fresh interpreter. It imports main's actual
64 KiB scan, 48 KiB LCD and 96 KiB TLS reserves, paints the physical LCD, but
uses synthetic inputs, instrument, credentials and upload transport. No PWM
object, Wi-Fi connection, file write or real upload is created here.

4096 records are sealed, hashed, then streamed twice in <=256-byte chunks.
The exact prepared digest/size must match both streams before a synthetic ACK.
Two fresh App lifecycles exercise scan Cancel and Settings Cancel; their restart
callbacks record success without resetting the interpreter. Settings runs last
because its documented path releases TLS memory. Failure propagates to USB.
"""

import main  # Reserve production buffers before importing helper dependencies.
import gc
import hashlib
import binascii
from machine import Pin

from engine import Engine
from ram_scan import PackedScanBuffer
from ram_payload import PayloadReader, CHUNK_BYTES, MAX_WIRE_BYTES
from ram_cloud import RamCloudManager
from ui import App
import model
import tls_memory


_HOME, _SETTINGS, _RESTART, _SCAN = 1, 2, 4, 8
_MAX_STEPS = MAX_WIRE_BYTES // CHUNK_BYTES * 3 + 32


def _pins_low():
    # Attempt drain even if the gate Pin operation itself fails.
    try:
        Pin(21, Pin.OUT, value=0)
    finally:
        Pin(20, Pin.OUT, value=0)


def _heap():
    gc.collect()
    return gc.mem_free()


class NoFiles:
    def __init__(self):
        self.writes = 0

    def load_json(self, path, default=None):
        if path != "cloud.json":
            raise AssertionError("Unexpected configuration read")
        return {"version": 1, "enabled": True,
                "url": "https://script.google.com/macros/s/OFFLINE_SMOKE/exec"}

    def save_json_atomic(self, *args):
        self.writes += 1
        raise AssertionError("Smoke test attempted a file write")

    save_settings = save_json_atomic


class SmokeDisplay(main.Display):
    def __init__(self):
        self.frames = 0
        super().__init__(raster_pool=main._display_buffer)

    def text(self, text, *args, **kwargs):
        if text == "READY TO SCAN":
            self.frames |= _HOME
        elif text == "SCAN SETTINGS":
            self.frames |= _SETTINGS
        elif text == "RESTARTING":
            self.frames |= _RESTART
        elif text == "SCANNING":
            self.frames |= _SCAN
        return super().text(text, *args, **kwargs)


class SyntheticInputs:
    def __init__(self):
        self.event = None

    def poll(self, now_ms):
        event, self.event = self.event, None
        return ((event,),) if event is not None else ()


class SyntheticInstrument:
    def __init__(self):
        self.drain = self.gate = 0.0
        self.commands = self.zeroes = self.reads = 0

    def set_duty(self, role, value):
        self.commands += 1
        if role == "drain":
            self.drain = value
        else:
            self.gate = value

    def zero(self):
        self.zeroes += 1
        self.gate = self.drain = 0.0

    def poll_alerts(self):
        pass

    def begin_measurement(self, count):
        pass

    def poll_measurement(self):
        return True

    def read_pair(self):
        self.reads += 1
        return {"drain": {"bus_v": 3.3 + 22.44 * self.drain, "shunt_v": -2.5e-6},
                "gate": {"bus_v": 25.74 * self.gate, "shunt_v": 1.25e-6}}


class SyntheticTransport:
    """Incremental exact-payload checks; never creates a socket or file."""
    def __init__(self, display):
        self.display = display
        self.owner = self.reader = self.digest = None
        self.count = self.passes = self.maximum_chunk = self.closes = 0
        self.upload_stage = 1
        self.payload_sent = self.payload_total = 0
        self.instrument = None
        self.check_cancel_frame = False

    def start(self, url, owner, scan_id, sha256, point_count):
        assert point_count == 4096 and owner.sealed and owner.buffer.frozen
        assert owner.scan_id == scan_id and owner.sha256 == sha256
        self.owner = owner
        self.reader = PayloadReader(owner)
        self.digest = hashlib.sha256()
        self.count = self.passes = 0
        self.upload_stage = 1
        self.payload_sent, self.payload_total = 0, owner.wire_size

    def step(self):
        chunk = self.reader.read(CHUNK_BYTES)
        if chunk:
            assert 0 < len(chunk) <= CHUNK_BYTES
            self.maximum_chunk = max(self.maximum_chunk, len(chunk))
            self.digest.update(chunk)
            self.count += len(chunk)
            if self.passes == 0:
                # The first pass is the synthetic transport's accepted data;
                # the second pass independently verifies its exact replay.
                self.upload_stage = 2
                self.payload_sent = self.count
            return None
        assert self.count == self.owner.wire_size
        actual = binascii.hexlify(self.digest.digest()).decode()
        assert actual == self.owner.sha256
        self.reader.close()
        self.passes += 1
        self.upload_stage = 3
        print("DIRECT_MEMORY_STREAM", self.passes, self.count, actual)
        if self.passes == 1:
            self.reader = PayloadReader(self.owner)
            self.digest = hashlib.sha256()
            self.count = 0
            return None
        return {"ok": True, "scan_id": self.owner.scan_id, "sha256": actual,
                "point_count": 4096, "sheet_id": 0}

    def close(self):
        if self.check_cancel_frame:
            assert self.display.frames & _RESTART
            assert self.instrument.gate == 0 and self.instrument.drain == 0
        if self.reader is not None:
            self.reader.close()
        self.owner = self.reader = self.digest = None
        self.closes += 1


class RestartCounter:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


def _random_bytes(count):
    return b"s" * count


def _cloud(buffer, transport, store):
    return RamCloudManager(buffer, transport=transport, store=store,
                           device_id="d1ce", random_bytes=_random_bytes)


def _full_payload(buffer, display, store, app, current_correction=None):
    settings = dict(model.DEFAULT_SETTINGS)
    settings.update(drain_start_uv=100000, drain_stop_uv=6400000, drain_step_uv=100000,
                    gate_start_uv=-3000000, gate_stop_uv=3300000, gate_step_uv=100000,
                    driver_supply_uv=18800000, source_voltage_uv=3300000,
                    ina_average_count=64, settle_ms=100)
    transport = SyntheticTransport(display)
    cloud = _cloud(buffer, transport, store)
    cloud.begin_scan(settings, 0, current_correction=current_correction)
    row = {"index": 0, "curve": 0, "requested_vds": 0., "requested_vgs": 0.,
           "vds": 0., "vgs": 0., "id_a": 0., "ig_a": 0.,
           "acquisition_blocks_completed": 1}
    for index in range(4096):
        curve, drain = divmod(index, 64)
        row["index"], row["curve"] = index, curve
        row["requested_vds"] = (100000 + drain * 100000) / 1000000
        row["requested_vgs"] = (-3000000 + curve * 100000) / 1000000
        # Include signed/sub-microamp values and measured/commanded differences.
        row["vds"] = row["requested_vds"] + 0.037
        row["vgs"] = row["requested_vgs"] - 0.15
        row["id_a"] = (index - 2048) * 0.00000025
        row["ig_a"] = (curve - 32) * 0.00000003125
        cloud.record_point(row)
    assert len(buffer) == 4096 and len(buffer._data) == 65536
    cloud.finish_scan(123456)
    assert buffer.frozen and cloud.state == "preparing"
    app.cloud = cloud
    app.goto("upload")
    app.render()
    marks = app._upload_marks
    assert marks == [1, 0, 0, 0] and app._upload_percent == 0
    print("DIRECT_MEMORY_SEALED", len(buffer), _heap())
    for step in range(_MAX_STEPS):
        cloud.tick(step, online=True)  # Synthetic transport only.
        app._render_upload_progress()
        if cloud.state == "complete":
            break
        if cloud.state == "error":
            raise AssertionError("Synthetic payload/ACK check failed")
    else:
        raise AssertionError("Payload preparation exceeded bounded steps")
    assert transport.passes == 2 and transport.maximum_chunk == CHUNK_BYTES
    assert app._upload_marks is marks and marks == [2, 2, 2, 2]
    assert app._upload_percent == 100
    assert tls_memory.available()
    print("DIRECT_MEMORY_PAYLOAD_OK", cloud.scan.point_count,
          cloud.scan.wire_size, _heap())
    print("DIRECT_MEMORY_PROGRESS_OK", cloud.upload_stage, cloud.upload_sent,
          cloud.upload_total, app._upload_percent)
    cloud.discard_scan()
    assert len(buffer) == 0 and not buffer.frozen and store.writes == 0


def _cancel_ui(buffer, display, store, settings_path):
    inputs, instrument, restart = SyntheticInputs(), SyntheticInstrument(), RestartCounter()
    transport = SyntheticTransport(display)
    transport.instrument = instrument
    cloud = _cloud(buffer, transport, store)
    app = App(display, inputs, Engine(instrument), store=store,
              cloud=cloud, scan_buffer=buffer, restart=restart)
    display.frames = 0
    app.tick(0)
    assert app.screen == "home" and display.frames & _HOME
    inputs.event = "left" if settings_path else "right"
    app.tick(20)
    if settings_path:
        assert app.screen == "settings" and display.frames & _SETTINGS
        assert not tls_memory.available()
        assert instrument.commands == 0
    else:
        assert app.screen == "preparing"
        for now in (40, 60, 80):
            app.tick(now)
        assert app.screen == "scan" and display.frames & _SCAN
        assert instrument.commands == 0  # Scan paints before first command.
        app.tick(100)
        assert instrument.commands > 0 and instrument.reads == 0
    transport.check_cancel_frame = True
    inputs.event = "left"
    app.tick(120)
    assert restart.count == 1 and app.screen == "restarting"
    assert display.frames & _RESTART
    assert instrument.gate == 0 and instrument.drain == 0
    assert len(buffer) == 0 and store.writes == 0
    assert cloud.state == "idle" and transport.passes == 0
    # App catches cleanup exceptions; require close() to have finished rather
    # than accepting only its final visible state after a swallowed assertion.
    assert transport.closes == (2 if settings_path else 1)
    print("DIRECT_MEMORY_CANCEL_OK", "settings" if settings_path else "scan", _heap())


def _run():
    assert len(main._scan_bytes) == 65536
    assert len(main._display_buffer) == 48 * 1024
    assert tls_memory.available()
    print("DIRECT_MEMORY_BEGIN", _heap())
    store = NoFiles()
    buffer = PackedScanBuffer(main._scan_bytes)
    display = SmokeDisplay()
    # Hold the actual App's views/caches/hit targets during the maximum payload.
    app = App(display, SyntheticInputs(), Engine(SyntheticInstrument()), store=store,
              scan_buffer=buffer, restart=RestartCounter())
    app.tick(0)
    assert display.frames & _HOME
    print("DIRECT_MEMORY_LCD_HOME", _heap())
    _full_payload(buffer, display, store, app)
    app = None
    _heap()
    _cancel_ui(buffer, display, store, False)
    _heap()
    _cancel_ui(buffer, display, store, True)
    assert store.writes == 0 and len(buffer) == 0


def run():
    _pins_low()
    try:
        _run()
    except BaseException as error:
        print("DIRECT_MEMORY_FAIL", type(error).__name__)
        raise
    finally:
        main._scan_timing.stop()
        _pins_low()
    print("DIRECT_MEMORY_PASS")
