"""Native RAM-only scan smoke test; never drives outputs or resets the Pico.

Run from an idle REPL after stopping the production application. Preload
``import main`` once so its production TLS, ACK, 69,632-byte scan and 48 KiB
display reservations precede this diagnostic. Use the precompiled .mpy helper:

    import firmware_ram_smoke
    firmware_ram_smoke.run()

Reading the production calibration before the guard is optional; set
firmware_ram_smoke.RAM_SMOKE_USE_SAVED_CALIBRATION=False to skip that read.
The default retains the installed map for a realistic heap footprint only;
all fake measurements use explicitly synthetic triggered-calibration metadata.
An older installed map is never relabeled as a new hardware calibration.
No settings, calibration, credentials, scan files, or receipts are written.
The reserved RAM scan buffer is overwritten: run only after any wanted scan
has been uploaded or intentionally discarded.

All scan tests run behind a module-scoped file-open/filesystem guard, with a
fake configuration store and fake upload transport. This intercepts known
Python module APIs, not process-wide/native C filesystem access or new local
imports that bypass the guarded globals. Pair it with a static source audit
and external pre/post filesystem manifests. Importing main holds GP20/21
low; this script only reads them and never constructs Instrument, Engine, PWM,
WLAN or a real network connection. It never calls main.run or soft_reset.
Managers are recreated between cases to model separate boots without resetting.
"""

import main
import gc
import os
import io
import builtins
import sys
import json
import hashlib
import binascii


TEST_URL = "https://script.google.com/macros/s/RAM_SMOKE_NO_NETWORK/exec"
MAX_TICKS = 10000
RAM_SMOKE_INA_AVERAGE_COUNT = 64


def free_heap():
    return gc.mem_free() if hasattr(gc, "mem_free") else None


def hex_digest(digest):
    return binascii.hexlify(digest.digest()).decode("ascii")


def report(event, details):
    print("RAM_SMOKE_" + event, json.dumps(details))


class FilesystemProxy:
    """Shadow filesystem APIs without changing immutable native modules."""
    def __init__(self, original, denied, deny):
        self.original, self.denied, self.deny = original, denied, deny

    def __getattr__(self, name):
        if name in self.denied:
            return self.deny
        return getattr(self.original, name)


class NoFilesystem:
    """Fail closed for the loaded production modules' known Python APIs."""
    MODULES = ("main", "model", "storage", "ui", "engine", "hardware", "display",
               "inputs", "wifi", "ram_scan", "ram_payload", "ram_cloud", "https_upload",
               "tls_memory", "calibration_data", "calibration_json", "calibration_binary")
    OS_FILES = ("open", "remove", "unlink", "rename", "replace", "mkdir", "rmdir", "sync",
                "stat", "statvfs", "listdir", "ilistdir", "chdir", "getcwd", "mount", "umount")

    def __init__(self, modules=None):
        self.modules = modules
        self.originals = []
        self.attempts = 0
        self.restore_errors = 0
        self.missing = object()

    def deny(self, *args, **kwargs):
        self.attempts += 1
        raise AssertionError("RAM scan attempted filesystem access")

    def _set(self, module, name, value):
        original = getattr(module, name, self.missing)
        change = (module, name, original)
        setattr(module, name, value)
        try:
            self.originals.append(change)
        except BaseException:
            try:
                if original is self.missing:
                    delattr(module, name)
                else:
                    setattr(module, name, original)
            except Exception:
                self.restore_errors += 1
            raise

    def _probe(self, operation):
        before = self.attempts
        try:
            # Invalid even if interception is broken: no real path is touched.
            operation(None)
        except AssertionError:
            pass
        else:
            raise AssertionError("Filesystem guard did not intercept API")
        assert self.attempts == before + 1, "Filesystem guard self-test failed"

    def install(self):
        try:
            proxies = (("builtins", builtins, FilesystemProxy(builtins, ("open",), self.deny)),
                       ("io", io, FilesystemProxy(io, ("open",), self.deny)),
                       ("os", os, FilesystemProxy(os, self.OS_FILES, self.deny)))
            modules = self.modules
            if modules is None:
                modules = tuple(sys.modules[name] for name in self.MODULES if name in sys.modules)
            for module in modules:
                self._set(module, "open", self.deny)
                self._probe(module.open)
                for name, original, proxy in proxies:
                    if getattr(module, name, None) is original:
                        self._set(module, name, proxy)
                        self._probe(getattr(module, name).open)
                        if name == "os":
                            self._probe(module.os.remove)
                            self._probe(module.os.sync)
            report("GUARD_READY", {"modules": len(modules), "intercepted_probes": self.attempts,
                                   "scope": "known production Python module APIs"})
            self.attempts = 0
        except BaseException:
            self.restore()
            raise

    def restore(self):
        while self.originals:
            module, name, value = self.originals.pop()
            try:
                if value is self.missing:
                    delattr(module, name)
                else:
                    setattr(module, name, value)
            except Exception:
                # Keep cleaning up; never replace the original test failure.
                self.restore_errors += 1
        if self.restore_errors:
            report("GUARD_RESTORE_ERROR", {"failed_changes": self.restore_errors})
        return self.restore_errors


class FakeStore:
    def load_json(self, path):
        assert path == "cloud.json"
        return {"version": 1, "url": TEST_URL, "enabled": True}

    def save_json_atomic(self, *args):
        raise AssertionError("Scan attempted to persist configuration")


class FakeTransport:
    def __init__(self, behavior="ack"):
        self.behavior = behavior
        self.reader = None
        self.starts = 0
        self.closes = 0
        self.read_bytes = 0
        self.max_read = 0
        self.digest = None
        self.expected_size = 0
        self.expected_hash = None
        self.scan_id = None
        self.point_count = None

    def start(self, url, scan, scan_id, sha256, point_count):
        assert url == TEST_URL, "Fake transport received unexpected configuration"
        assert scan.sealed and scan.buffer.frozen
        assert scan.scan_id == scan_id and scan.sha256 == sha256 and scan.point_count == point_count
        self.reader = main.PayloadReader(scan)
        self.expected_size, self.expected_hash = self.reader.size, sha256
        self.scan_id, self.point_count = scan_id, point_count
        self.digest = hashlib.sha256()
        self.starts += 1

    def step(self):
        if self.behavior == "exception":
            raise OSError("Synthetic upload failure")
        block = self.reader.read(256)
        self.max_read = max(self.max_read, len(block))
        assert len(block) <= 256
        if block:
            self.digest.update(block)
            self.read_bytes += len(block)
            return None
        assert self.read_bytes == self.expected_size, "Transport wire size changed"
        assert hex_digest(self.digest) == self.expected_hash, "Transport wire digest changed"
        return {"ok": True, "scan_id": self.scan_id,
                "sha256": "0" * 64 if self.behavior == "bad_ack" else self.expected_hash,
                "point_count": self.point_count, "sheet_id": 123}

    def close(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None
        self.closes += 1


def config(count):
    value = dict(main.model.DEFAULT_SETTINGS)
    value.update(drain_start_uv=0, drain_stop_uv=(count - 1) * 1000,
                 drain_step_uv=1000, gate_start_uv=0, gate_stop_uv=0,
                 driver_supply_uv=5000000, source_voltage_uv=0,
                 ina_average_count=RAM_SMOKE_INA_AVERAGE_COUNT)
    return value


def populate(manager, count):
    # Reuse a single engine-shaped input dictionary. Only packed records grow.
    row = {"index": 0, "curve": 0, "requested_vds": 0.0, "requested_vgs": 0.0,
           "vds": 0.0, "vgs": 0.0, "id_a": 0.0, "ig_a": 0.0,
           "acquisition_blocks_completed": 1, "drain_voltage_limited": False, "gate_voltage_limited": False}
    for index in range(count):
        voltage = main.model.volts_from_uv(index * 1000)
        row["index"] = index
        row["requested_vds"], row["vds"] = voltage, voltage
        row["vgs"] = 0.101 if index % 7 == 0 else 0.0
        row["id_a"] = (index % 101 - 50) * 1e-6
        row["ig_a"] = (index % 21 - 10) * 1e-8
        row["drain_voltage_limited"] = index % 11 == 0
        row["gate_voltage_limited"] = index % 13 == 0
        manager.record_point(row)
        if count == 4096 and index in (0, 1023, 2047, 3071, 4095):
            report("FILL", {"points": index + 1, "free_heap": free_heap()})


def prepare(manager):
    for tick in range(MAX_TICKS):
        before = manager.scan._prepare_size
        manager.tick(200000 + tick, online=False)
        assert 0 <= manager.scan._prepare_size - before <= 256, "Unbounded prepare step"
        if manager.state != "preparing":
            assert manager.state == "waiting_wifi", "Preparation failed"
            assert manager.scan.sha256 is not None and manager.scan.wire_size > 0
            return tick + 1
    raise AssertionError("Preparation timed out")


def replay_digest(scan, size):
    reader = main.PayloadReader(scan)
    digest, total, maximum = hashlib.sha256(), 0, 0
    try:
        while True:
            block = reader.read(size)
            if not block:
                break
            assert len(block) <= size
            maximum = max(maximum, len(block))
            digest.update(block)
            total += len(block)
        assert total == reader.size == scan.wire_size
        assert hex_digest(digest) == scan.sha256
    finally:
        reader.close()
    assert scan.buffer.frozen and len(scan.buffer) == scan.point_count
    return maximum


def make_manager(buffer, cal, behavior="ack"):
    transport = FakeTransport(behavior)
    manager = main.RamCloudManager(buffer, transport=transport, store=FakeStore(),
                                   device_id="abcd", utc_provider=lambda: None,
                                   random_bytes=lambda count: b"\x01" * count)
    manager.prepare_calibration(cal)
    return manager, transport


def upload_to_terminal_state(manager):
    manager.tick(300000, online=True)
    assert manager.state == "uploading"
    for tick in range(MAX_TICKS):
        manager.tick(300001 + tick, online=True)
        if manager.state != "uploading":
            return
    raise AssertionError("Fake upload timed out")


def full_scan(buffer, cal):
    manager, transport = make_manager(buffer, cal)
    try:
        manager.begin_scan(config(4096), cal, 10)
        populate(manager, 4096)
        assert len(buffer) == 4096 and len(buffer._data) == 69632
        assert buffer._data is main._scan_bytes
        assert buffer.get(0)[4] == 7
        assert abs(buffer.get(-1)[0] - 4.095) < 0.000002
        assert buffer.get(1)[3] != 0.0, "Small gate current was lost"
        manager.finish_scan(100000)
        assert buffer.frozen
        try:
            buffer.clear()
        except ValueError:
            pass
        else:
            raise AssertionError("Frozen scan accepted clear")
        steps = prepare(manager)
        first = replay_digest(manager.scan, 97)
        second = replay_digest(manager.scan, 1024)
        assert manager.scan.created_utc is None
        for tick in range(3):
            manager.tick(250000 + tick, online=False)
            assert manager.state == "waiting_wifi" and transport.starts == 0
        upload_to_terminal_state(manager)
        assert manager.state == "complete", "Exact ACK was not accepted"
        assert transport.starts == 1 and transport.reader is None
        assert len(buffer) == 4096, "ACK unexpectedly discarded RAM"
        report("FULL_PASS", {"points": manager.scan.point_count, "record_bytes": 17,
                              "buffer_bytes": len(buffer._data), "wire_bytes": manager.scan.wire_size,
                              "prepare_steps": steps, "replay_max_reads": [first, second],
                              "upload_max_read": transport.max_read, "sha256": manager.scan.sha256,
                              "free_heap": free_heap()})
        manager.discard_scan()
        assert manager.scan is None and len(buffer) == 0 and not buffer.frozen
        assert buffer._data is main._scan_bytes
        try:
            manager.begin_scan(config(1), cal, 400000)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Manager allowed a second scan without restart")
        report("DISCARD_PASS", {"count": len(buffer), "backing_reused": True})
    finally:
        manager.discard_scan()


def failure_case(buffer, cal, behavior):
    mode = behavior if behavior in ("exception", "bad_ack") else "ack"
    manager, transport = make_manager(buffer, cal, mode)
    try:
        manager.begin_scan(config(2), cal, 10)
        populate(manager, 2)
        manager.finish_scan(100)
        prepare(manager)
        digest = manager.scan.sha256
        if behavior == "wifi_timeout":
            manager.tick(300000, online=False)
            manager.fail_upload()  # Same notification used after a failed join.
        elif behavior == "wifi_drop":
            manager.tick(300000, online=True)
            assert manager.state == "uploading"
            manager.tick(300001, online=False)
        else:
            upload_to_terminal_state(manager)
        assert manager.state == "error", "Failure incorrectly confirmed upload"
        assert manager.status["message"] == "WiFi Error, Upload Failed\nPlease reconnect WiFi in settings and redo scan."
        assert len(buffer) == 2 and buffer.frozen and manager.scan.sha256 == digest
        replay_digest(manager.scan, 113)
        starts = transport.starts
        manager.tick(500000, online=True)
        assert transport.starts == starts, "Failure retried automatically"
        report("FAILURE_PASS", {"case": behavior, "points_retained": len(buffer),
                                 "upload_starts": starts, "free_heap": free_heap()})
    finally:
        manager.discard_scan()
    assert len(buffer) == 0 and not buffer.frozen


def zero_partial(buffer, cal):
    manager, transport = make_manager(buffer, cal)
    try:
        manager.begin_scan(config(1), cal, 10)
        fault = {"channel": "gate", "kind": "shunt_saturation", "raw_code": 32767,
                 "sign": 1, "limit_value": 0.0819175, "limit_unit": "V", "measured_value": None,
                 "index": 0, "curve": 0, "requested_vds": 0.0, "requested_vgs": 0.0, "phase": "sample"}
        manager.finish_scan(100, fault)
        prepare(manager)
        assert manager.scan.point_count == 0 and manager.scan.outcome == "partial"
        replay_digest(manager.scan, 37)
        upload_to_terminal_state(manager)
        assert manager.state == "complete" and transport.point_count == 0
        report("PARTIAL_PASS", {"point_count": 0, "exact_zero_ack": True})
    finally:
        manager.discard_scan()


def run():
    assert len(main._scan_bytes) == 4096 * 17, "Installed main lacks the new RAM buffer"
    assert main.Pin(20).value() == 0 and main.Pin(21).value() == 0, "Physical PWM pins must already be low"
    retained_cal = None
    if globals().get("RAM_SMOKE_USE_SAVED_CALIBRATION", True):
        retained_cal = main.storage.load_calibration()
    metadata = main.model.acquisition_metadata(config(1))
    metadata.pop("blocks_per_point")
    metadata.pop("point_variance")
    metadata.update(driver_supply_v=5.0, pwm_hz=100000,
                    calibration_repeat_blocks=main.model.CALIBRATION_REPEAT_BLOCKS,
                    synthetic_no_hardware=True)
    cal = {"version": 2, "metadata": metadata, "channels": {}}
    buffer = main.PackedScanBuffer(main._scan_bytes)
    guard = NoFilesystem()
    gc.collect()
    report("BEGIN", {"scan_bytes": len(main._scan_bytes), "display_bytes": len(main._display_buffer),
                      "tls_reserved": bool(main._tls_reserved), "free_heap": free_heap(),
                      "saved_calibration_retained_for_heap_only": retained_cal is not None,
                      "payload_calibration": "synthetic_no_hardware",
                      "acquisition": main.model.acquisition_metadata(config(1))})
    guard.install()
    try:
        full_scan(buffer, cal)
        gc.collect()
        for behavior in ("wifi_timeout", "wifi_drop", "exception", "bad_ack"):
            failure_case(buffer, cal, behavior)
            gc.collect()
        zero_partial(buffer, cal)
        assert guard.attempts == 0, "Scan path touched the filesystem"
        assert main.Pin(20).value() == 0 and main.Pin(21).value() == 0
    finally:
        guard.restore()
        buffer.unfreeze()
        buffer.clear()
        cal = retained_cal = None
        gc.collect()
    assert guard.restore_errors == 0, "Filesystem guard cleanup failed"
    report("PASS", {"filesystem_attempts": guard.attempts, "remaining_records": len(buffer),
                     "filesystem_guard_scope": "known production Python module APIs",
                     "physical_pwm_pins_low": True, "real_network_calls": 0,
                     "reset_called": False, "free_heap": free_heap()})


if __name__ == "__main__":
    run()
