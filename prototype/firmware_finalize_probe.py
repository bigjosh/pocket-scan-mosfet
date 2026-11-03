"""Native stopped-scan finalization probe; physical GP20/21 always stay low.

Preload ``import main; display=main.Display(raster_pool=main._display_buffer)``
so production TLS/ACK/plot/display allocations precede this diagnostic. Reads
real settings.json/calibration.json, but never writes them or opens scans/,
wifi.json or cloud.json. No PWM, I2C or network instance is created.

Synthetic completed rows seed several scan positions; the real Engine then
emits a partial event from an injected current/saturation fault, and the real
App/CloudManager/Archive finalize it. Transient wrappers expose safe diagnostic
stages before production's generic error masking. Set FINALIZE_PROBE_COLLECT
True for a second run that collects immediately before finalization. An existing
__finalize_probe directory is refused. This script deletes only its own files.
"""

import main

main.Pin(20, main.Pin.OUT, value=0)
main.Pin(21, main.Pin.OUT, value=0)
if "display" not in globals():
    display = main.Display(raster_pool=main._display_buffer)

import os
import gc
import json
import hashlib
import binascii
import archive as archive_module
from engine import OverrangeError

TEST_DIR = "__finalize_probe"
COLLECT = bool(globals().get("FINALIZE_PROBE_COLLECT", False))
stage = "setup"


def free_heap():
    return gc.mem_free() if hasattr(gc, "mem_free") else None


def report_error(where, error):
    # Only local synthetic archive operations reach these wrappers. No private
    # cloud configuration or arbitrary transport exception is available here.
    print("FINALIZE_PROBE_ERROR", where, type(error).__name__, str(error),
          "heap", free_heap())


def guard(where, function):
    def wrapped(*args, **kwargs):
        global stage
        stage = where
        try:
            return function(*args, **kwargs)
        except Exception as error:
            report_error(where, error)
            if where == "source_init":
                try:
                    import struct
                    with open(args[1], "rb") as stream:
                        prefix = stream.read(12)
                        magic, length = struct.unpack("<8sI", prefix)
                        header = stream.read(length)
                        print("FAILED_HEADER", magic, length, len(header),
                              header[:40], header[-80:])
                        print("FAILED_HEADER_DECODE", len(header.decode("utf-8")))
                except Exception as detail:
                    report_error("header inspection", detail)
            raise
    return wrapped


class ProbeJSON:
    def load(self, *args, **kwargs):
        return guard("json.load", json.load)(*args, **kwargs)

    def dumps(self, *args, **kwargs):
        return guard("json.dumps", json.dumps)(*args, **kwargs)

    def loads(self, *args, **kwargs):
        return guard("json.loads", json.loads)(*args, **kwargs)

    def dump(self, *args, **kwargs):
        return json.dump(*args, **kwargs)


class ProbeHash:
    def sha256(self, *args):
        return guard("new_sha256", hashlib.sha256)(*args)


class Inputs:
    def poll(self, unused):
        return []


class Store:
    def load_json(self, path):
        assert path == "__finalize_memory_config"
        return None

    def save_json_atomic(self, *unused):
        raise AssertionError("Probe must not save configuration")

    save_settings = save_json_atomic


class NoNetwork:
    def close(self):
        pass

    def start(self, *unused):
        raise AssertionError("Probe must not use the network")

    step = start


class ZeroInstrument:
    identity = {"synthetic_test": True}

    def __init__(self):
        self.engine = None
        self.role, self.kind, self.sign = "drain", "current_limit", 1
        self.commands, self.zeroes = 0, 0

    def set_duty(self, role, fraction):
        assert role in ("drain", "gate") and 0 <= fraction <= 1
        self.commands += 1  # Logical command only; never construct PWM.

    def zero(self):
        self.zeroes += 1

    def read_pair(self):
        if self.kind == "shunt_saturation":
            code = 32767 if self.sign > 0 else 32768
            raise OverrangeError(self.role, self.kind, code, self.sign,
                                 code * 0.0000025, "V")
        pair = {}
        for role in ("drain", "gate"):
            shunt = 0.0
            if role == self.role:
                resistance = (main.model.DRAIN_SHUNT_OHM if role == "drain"
                              else main.model.GATE_SHUNT_OHM)
                limit = (main.model.DRAIN_CURRENT_LIMIT_A if role == "drain"
                         else main.model.GATE_CURRENT_LIMIT_A)
                shunt = self.sign * limit * 1.1 * resistance
            # Keep the synthetic terminal safely inside the envelope even for
            # negative current at a zero request; this isolates current faults.
            terminal = max(0.1, self.engine._target[role])
            pair[role] = {"terminal_v": terminal, "shunt_v": shunt,
                          "bus_v": terminal + shunt}
        return pair


class ProbeArchive(main.Archive):
    def finish_scan(self, now_ms, fault=None):
        if COLLECT:
            gc.collect()
        print("FINALIZE_PROBE_BEGIN", self._count, free_heap(),
              json.dumps(fault))
        try:
            return super().finish_scan(now_ms, fault)
        except Exception as error:
            report_error("finish_scan after " + stage, error)
            raise


def digest(path):
    gc.collect()
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(256)
            if not block:
                break
            value.update(block)
    return binascii.hexlify(value.digest()).decode()


def synthetic_row(index):
    curve, drain_index = divmod(index, len(engine._drains))
    vd = main.model.volts_from_uv(engine._drains[drain_index])
    vg = main.model.volts_from_uv(engine._gates[curve])
    row = {key: 0.0 for key in archive_module.COLUMNS}
    row.update(index=index, curve=curve, requested_vds=vd, requested_vgs=vg,
               vds=vd, vgs=vg, batches_completed=settings["batches"],
               drain_voltage_limited=False, gate_voltage_limited=False,
               total=engine._total, batchmeans=[])
    return row


def run_case(count, role, kind, sign, phase):
    global now
    app.action("right")
    for unused in range(8):
        now += 20
        app.tick(now)
        if app.screen != "preparing":
            break
    assert engine.state == "scanning", "Preparation failed: " + str(app.notice)
    scan_id = probe._current
    identifiers.append(scan_id)
    last = None
    for index in range(count):
        last = synthetic_row(index)
        app.cloud.record_point(last)
        app._add_point(last)
    engine._index = count
    engine._last_completed = last
    engine.latest = last
    complete = kind == "complete"
    curve, drain_index = divmod(min(count, engine._total - 1), len(engine._drains))
    engine._target = {
        "drain": main.model.volts_from_uv(engine._drains[drain_index] + settings["source_voltage_uv"]),
        "gate": main.model.volts_from_uv(engine._gates[curve] + settings["source_voltage_uv"])}
    engine._phase, engine._deadline = phase, 0
    instrument.role, instrument.kind, instrument.sign = role, kind, sign
    now += 20
    tick = engine.tick
    if complete:
        engine.state, engine.shutdown_succeeded = "complete", True
        engine.tick = lambda unused: [("complete", {"points": count})]
    try:
        app.tick(now)
    finally:
        engine.tick = tick
    outcome = "complete" if complete else "partial"
    passed = bool(engine.state == outcome and app.screen == outcome and
                  app._archive_ready and engine.shutdown_succeeded)
    print("FINALIZE_PROBE_CASE", json.dumps({"index": count, "curve": curve,
          "kind": kind, "channel": role, "sign": sign, "phase": phase,
          "passed": passed, "screen": app.screen, "heap": free_heap(),
          "fault": engine.fault}))
    if passed:
        # Verify the completed local draft through the same bounded reader used
        # for upload. Never accept it into an upload queue.
        gc.collect()
        gc.collect()
        info = main.storage.load_json(probe._path(scan_id, ".meta"))
        source = archive_module.UploadSource(probe._path(scan_id, ".draft"))
        try:
            assert source.outcome == outcome and source.point_count == count
            value, length = hashlib.sha256(), 0
            while True:
                block = source.read(256)
                if not block:
                    break
                value.update(block)
                length += len(block)
            assert length == info["wire_size"]
            assert binascii.hexlify(value.digest()).decode() == info["sha256"]
        finally:
            source.close()
    if app._archive_active:
        app._discard_archive()
    app.goto("home")
    return passed


created, app, probe = False, None, None
identifiers, now, results = [], 0, []
patches = []
try:
    os.mkdir(TEST_DIR)  # Refuse an existing directory; never reclaim user data.
    created = True
    before = {path: digest(path) for path in ("settings.json", "calibration.json")}
    settings = main.storage.load_settings()
    calibration = main.storage.load_json("calibration.json")
    main.model.validate_scan_calibration(calibration, settings)
    # Save every original before installing transient diagnostic wrappers.
    for owner, name, where in (
            (archive_module, "_validate_fault", "validate_fault"),
            (archive_module, "_read_tail", "read_tail"),
            (archive_module, "_metadata", "read_sidecar"),
            (archive_module.UploadSource, "__init__", "source_init"),
            (archive_module.UploadSource, "_next_piece", "source_next_piece"),
            (archive_module.UploadSource, "read", "source_read"),
            (archive_module.Archive, "_write", "binary_write"),
            (archive_module, "_replace", "rename"),
            (archive_module, "_sync", "sync"),
            (archive_module.storage, "save_json_atomic", "save_sidecar")):
        original = getattr(owner, name)
        patches.append((owner, name, original))
        setattr(owner, name, guard(where, original))
    patches.append((archive_module, "json", archive_module.json))
    archive_module.json = ProbeJSON()
    patches.append((archive_module, "hashlib", archive_module.hashlib))
    archive_module.hashlib = ProbeHash()
    instrument = ZeroInstrument()
    engine = main.Engine(instrument)
    instrument.engine = engine
    probe = ProbeArchive(TEST_DIR, "face")
    cloud = main.CloudManager(probe, NoNetwork(), Store(), "__finalize_memory_config")
    app = main.App(display, Inputs(), engine, settings, calibration, store=Store(),
                   cloud=cloud, plot_buffers=main._plot_buffers,
                   error_logger=lambda context, error: report_error(context, error))
    drains = main.model.sweep_uv(settings["drain_start_uv"], settings["drain_stop_uv"], settings["drain_step_uv"])
    gates = main.model.sweep_uv(settings["gate_start_uv"], settings["gate_stop_uv"], settings["gate_step_uv"])
    total, width = len(drains) * len(gates), len(drains)
    del drains, gates
    positions = (0, min(1, total - 1), min(width, total - 1),
                 min(width + 1, total - 1), total // 2, total - 1)
    print("FINALIZE_PROBE_START", json.dumps({"heap": free_heap(),
          "collect_before_finalize": COLLECT, "points": total,
          "drain_points": width, "settings": settings}))
    for case, count in enumerate(positions):
        if case >= globals().get("FINALIZE_PROBE_CASES", len(positions)):
            break
        role = "drain" if case % 2 == 0 else "gate"
        kind = "current_limit" if case < 3 else "shunt_saturation"
        sign = -1 if case in (1, 4) else 1
        phase = ("sample", "settle", "park_drain", "settle_gate", "sample", "settle")[case]
        results.append(run_case(count, role, kind, sign, phase))
    if globals().get("FINALIZE_PROBE_COMPLETE", False):
        results.append(run_case(total, "drain", "complete", 1, "sample"))
    assert all(before[path] == digest(path) for path in before), "User configuration changed"
    print("FINALIZE_PROBE_DONE", json.dumps({"passed": sum(results), "cases": len(results),
          "physical_outputs_always_low": True, "network_used": False,
          "configuration_unchanged": True, "collect_before_finalize": COLLECT}))
finally:
    main.Pin(20, main.Pin.OUT, value=0)
    main.Pin(21, main.Pin.OUT, value=0)
    if probe is not None:
        probe.discard_scan()
    for owner, name, original in reversed(patches):
        setattr(owner, name, original)
    if created:
        allowed = set(identifier + suffix for identifier in identifiers
                      for suffix in (".writing", ".draft", ".pending", ".sent", ".meta", ".meta.tmp"))
        names = os.listdir(TEST_DIR)
        assert all(name in allowed for name in names), "Unexpected probe file; leaving directory intact"
        for name in names:
            os.remove(TEST_DIR + "/" + name)
        os.rmdir(TEST_DIR)
    print("OUTPUTS_DISABLED")
