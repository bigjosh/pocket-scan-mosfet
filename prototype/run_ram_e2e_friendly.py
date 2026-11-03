"""Run the staged 9-point live helper on the exact known Pico, once.

This host program opens USB only from main(). Preparation uses raw REPL solely
to soft-reset into a clean interpreter without main.py autorun, then returns
to friendly REPL BEFORE importing/executing ram_e2e_live. Its successful
production soft_reset therefore autoruns normal main.py. Observe ACK -> reboot
-> ready, then close serial without interrupting Home. No upload retry.

Requires root to stage ram_e2e_live.mpy and authorize the live fixture first.
Only allowlisted, sanitized status/measurement records are logged. Raw USB,
tracebacks, receiver URLs, passwords and arbitrary exception text are omitted.
On failure, interrupt and attempt gate-first PWM/pin shutdown, leaving REPL.
"""

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time


PICO_SERIAL = "3B8922C9B91BBE9F"
MAX_SECONDS = 600
MAX_LINE = 4096
RAW_PROMPT = b"raw REPL; CTRL-B to exit\r\n>"
EXECUTE = b"import ram_e2e_live; ram_e2e_live.run()\r\n"
CLEANUP_SOURCE = """import machine
_e2e_bad = False
for _e2e_pin in (21, 20):
 try:
  _e2e_pwm = machine.PWM(machine.Pin(_e2e_pin), freq=100000, duty_u16=0)
  _e2e_pwm.deinit()
 except Exception:
  _e2e_bad = True
 try:
  machine.Pin(_e2e_pin, machine.Pin.OUT, value=0)
 except Exception:
  _e2e_bad = True
try:
 _e2e_bad = _e2e_bad or machine.Pin(21).value() != 0 or machine.Pin(20).value() != 0
except Exception:
 _e2e_bad = True
print('RAM_E2E_HOST_CLEANUP_FAILED' if _e2e_bad else 'RAM_E2E_HOST_CLEANUP_OK')
"""
STATES = {
    "screen": ("home", "preparing", "scan", "upload", "upload_error", "scan_error", "restarting", "notice"),
    "engine": ("idle", "scanning", "calibrating", "complete", "partial", "error", "cancelled"),
    "cloud": ("idle", "recording", "preparing", "waiting_wifi", "uploading", "complete", "error"),
    "wifi": ("off", "paused", "connecting", "connected", "error", "idle", "disconnected", "scanning"),
}


class ProtocolError(Exception):
    pass


def find_port(ports):
    matches = [port.device for port in ports
               if (port.serial_number or "").upper() == PICO_SERIAL]
    if len(matches) != 1:
        raise ProtocolError("known_pico_not_unique")
    return matches[0]


def write(connection, data):
    if connection.write(data) != len(data):
        raise ProtocolError("short_serial_write")
    connection.flush()


def wait_prompt(connection, token, deadline, clock):
    window = bytearray()
    while clock() < deadline:
        window.extend(connection.read(min(512, connection.in_waiting or 1)))
        if token in window:
            return
        if len(window) > MAX_LINE:
            del window[:-MAX_LINE]
    raise ProtocolError("repl_prompt_timeout")


def prepare_friendly(connection, deadline, clock):
    connection.reset_input_buffer()
    write(connection, b"\x03\x03\x02")
    wait_prompt(connection, b">>> ", min(deadline, clock() + 10), clock)
    write(connection, b"\x01")
    wait_prompt(connection, RAW_PROMPT, min(deadline, clock() + 10), clock)
    write(connection, b"\x04")
    wait_prompt(connection, RAW_PROMPT, min(deadline, clock() + 15), clock)
    write(connection, b"\x02")
    wait_prompt(connection, b">>> ", min(deadline, clock() + 10), clock)


def lines(connection, deadline, clock):
    """Discard oversized/unrecognized text without retaining an unbounded log."""
    pending = bytearray()
    dropping = False
    while clock() < deadline:
        chunk = connection.read(min(512, connection.in_waiting or 1))
        for byte in chunk:
            if byte == 10:
                if not dropping:
                    yield pending.decode("utf-8", "replace").rstrip("\r")
                pending.clear()
                dropping = False
            elif not dropping:
                pending.append(byte)
                if len(pending) > MAX_LINE:
                    pending.clear()
                    dropping = True


def record_json(line, prefix):
    try:
        data = json.loads(line[len(prefix):].strip())
    except (ValueError, TypeError):
        raise ProtocolError("invalid_diagnostic_record") from None
    if not isinstance(data, dict):
        raise ProtocolError("invalid_diagnostic_record")
    return data


def safe_ack(data):
    if (data.get("exact_match") is not True or type(data.get("point_count")) is not int
            or data["point_count"] != 9 or data.get("cloud_state") != "complete"
            or type(data.get("sheet_id")) is not int or data["sheet_id"] < 0
            or not isinstance(data.get("scan_id"), str)
            or re.fullmatch(r"[0-9a-fA-F-]{1,128}", data["scan_id"]) is None
            or not isinstance(data.get("sha256"), str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", data["sha256"]) is None):
        raise ProtocolError("invalid_exact_ack")
    return {key: data[key] for key in
            ("exact_match", "point_count", "scan_id", "sha256", "sheet_id", "cloud_state")}


def safe_begin(data):
    expected = {"point_count": 9, "settings_version": 3,
                "source_voltage_uv": 3300000, "drain_start_uv": 100000,
                "drain_stop_uv": 1100000, "drain_step_uv": 500000,
                "gate_start_uv": -1000000, "gate_stop_uv": 1000000, "gate_step_uv": 1000000}
    if any(type(data.get(key)) is not int or data[key] != value for key, value in expected.items()):
        raise ProtocolError("invalid_acquisition_plan")
    count, rail = data.get("ina_average_count"), data.get("driver_supply_uv")
    if (type(count) is not int or count not in (1, 4, 16, 64, 128, 256, 512, 1024)
            or type(rail) is not int or not 4400000 <= rail <= 30000000
            or data.get("low_boundary_excluded") is not True):
        raise ProtocolError("invalid_acquisition_plan")
    acquisition = {"acquisition_mode": "ina226_triggered_v1", "ina_average_count": count,
                   "bus_conversion_us": 1100, "shunt_conversion_us": 1100,
                   "blocks_per_point": 1, "point_variance": "unavailable"}
    supplied = data.get("acquisition")
    if not isinstance(supplied, dict) or any(type(supplied.get(key)) is not type(value)
            or supplied[key] != value for key, value in acquisition.items()):
        raise ProtocolError("invalid_acquisition_plan")
    # Return explicit numeric/fixed-string fields only, never unknown device text.
    expected.update(ina_average_count=count, driver_supply_uv=rail,
                    low_boundary_excluded=True, acquisition=acquisition)
    if type(data.get("free_heap")) is int and data["free_heap"] >= 0:
        expected["free_heap"] = data["free_heap"]
    return expected


def cleanup(connection, clock):
    """Best effort; return a confirmation, never replace the primary failure."""
    try:
        connection.reset_input_buffer()
        write(connection, b"\x03\x03\x02")
        wait_prompt(connection, b">>> ", clock() + 10, clock)
        write(connection, ("exec(" + repr(CLEANUP_SOURCE) + ")\r\n").encode("ascii"))
        for line in lines(connection, clock() + 10, clock):
            if line == "RAM_E2E_HOST_CLEANUP_OK":
                return True
            if line == "RAM_E2E_HOST_CLEANUP_FAILED":
                return False
    except BaseException:
        pass
    return False


def run(connection, emit, timeout=MAX_SECONDS, clock=time.monotonic):
    if not 1 <= timeout <= MAX_SECONDS:
        raise ValueError("Timeout must be 1..600 seconds")
    result = {"ok": False, "ack": None, "reboot_seen": False, "home_left_running": False,
              "cleanup_confirmed": None, "error": None}
    started = clock()
    try:
        deadline = started + timeout
        prepare_friendly(connection, deadline, clock)
        emit("HOST_READY_FRIENDLY", {})
        write(connection, EXECUTE)
        begun = initial_ready_seen = False
        for line in lines(connection, deadline, clock):
            if line.startswith("RAM_E2E_BEGIN "):
                if begun:
                    raise ProtocolError("unexpected_begin")
                plan = safe_begin(record_json(line, "RAM_E2E_BEGIN"))
                begun = True
                emit("RAM_E2E_BEGIN", plan)
            elif line.startswith("RAM_E2E_POINT "):
                data = record_json(line, "RAM_E2E_POINT")
                safe = {key: value for key, value in data.items()
                        if key in ("index", "vds", "vgs", "id_a", "ig_a", "flags")
                        and type(value) in (int, float) and math.isfinite(value)}
                emit("RAM_E2E_POINT", safe)
            elif line.startswith("RAM_E2E_STATE "):
                data = record_json(line, "RAM_E2E_STATE")
                safe = {key: value for key, value in data.items()
                        if key in STATES and value in STATES[key]}
                if type(data.get("free_heap")) is int:
                    safe["free_heap"] = data["free_heap"]
                emit("RAM_E2E_STATE", safe)
            elif line.startswith("RAM_E2E_ACK "):
                if not begun or result["ack"] is not None:
                    raise ProtocolError("unexpected_ack")
                result["ack"] = safe_ack(record_json(line, "RAM_E2E_ACK"))
                emit("RAM_E2E_ACK", result["ack"])
            elif line.startswith("RAM_E2E_ABORT") or line == "RAM_E2E_RETURNED":
                raise ProtocolError("device_aborted_or_returned")
            elif line == "MPY: soft reboot":
                if result["ack"] is None:
                    raise ProtocolError("reboot_before_ack")
                result["reboot_seen"] = True
                emit("MPY_SOFT_REBOOT", {})
            elif line.startswith("Pocket MOSFET ready"):
                # The helper starts inside the production application's first
                # tick, before its initial ready announcement. Only a later
                # ready after the acknowledged reset proves the final boot.
                if result["ack"] is None and begun and not initial_ready_seen:
                    initial_ready_seen = True
                    emit("INITIAL_POCKET_MOSFET_READY", {})
                    continue
                if result["ack"] is None or not result["reboot_seen"]:
                    raise ProtocolError("ready_before_ack_reboot")
                result["ok"] = result["home_left_running"] = True
                emit("POCKET_MOSFET_READY", {})
                break
            elif line.startswith(("Traceback", "MemoryError", "Scanner error", "Scanner stopped;")):
                raise ProtocolError("device_fault")
        if not result["ok"]:
            raise ProtocolError("diagnostic_timeout")
    except BaseException as error:
        result["error"] = str(error) if isinstance(error, ProtocolError) else "host_interrupted_or_failed"
        result["cleanup_confirmed"] = cleanup(connection, clock)
        emit("HOST_FAILED", {"error": result["error"], "cleanup_confirmed": result["cleanup_confirmed"]})
    result["elapsed_seconds"] = round(clock() - started, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=MAX_SECONDS)
    args = parser.parse_args()
    if not 1 <= args.timeout <= MAX_SECONDS:
        parser.error("Timeout must be 1..600 seconds")
    import serial
    from serial.tools import list_ports
    args.log.parent.mkdir(parents=True, exist_ok=True)
    with args.log.open("w", encoding="utf-8", buffering=1) as log:
        def emit(event, data):
            line = event + " " + json.dumps(data, separators=(",", ":"), allow_nan=False)
            print(line, flush=True)
            log.write(line + "\n")
        emit("HOST_BEGIN", {"serial": PICO_SERIAL, "utc": datetime.now(timezone.utc).isoformat()})
        try:
            port = find_port(list_ports.comports())
            with serial.Serial(port, 115200, timeout=0.1, write_timeout=5) as connection:
                result = run(connection, emit, args.timeout)
        except Exception:
            result = {"ok": False, "error": "device_selection_or_serial_open_failed"}
        emit("HOST_RESULT", result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
