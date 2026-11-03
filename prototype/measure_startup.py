"""Host-only timing of three normal Pico soft boots; leave the final boot running.

Run only while the scanner is idle: each trial interrupts the application and
soft-resets it. This does not write device files, drive outputs, or read saved
credentials. Serial text is inspected in bounded memory and never logged.

Current main.py prints "Pocket MOSFET ready" AFTER its first complete input/UI
tick. Older firmware prints it before the first frame; do not label results
from older firmware as UI timing. A startup notice may replace Home, so this
measures first-UI-tick completion, not proof that Home is displayed or Wi-Fi
has connected. USB buffering and host scheduling contribute to measured time.

Usage: python prototype/measure_startup.py --output output/smoke-test/startup.json
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time


PICO_SERIAL = "3B8922C9B91BBE9F"
READY = b"Pocket MOSFET ready"
PROMPT = b">>> "
FAILURE_MARKERS = (b"Traceback", b"Scanner error [", b"SCANNER STOPPED",
                   b"Scanner stopped; outputs disabled", b"MemoryError")
WINDOW_BYTES = 2048


def _read_window(connection, window):
    # A corrupt/noisy device cannot grow a host log or expose printed secrets.
    chunk = connection.read(min(512, connection.in_waiting or 1))
    window.extend(chunk)
    if len(window) > WINDOW_BYTES:
        del window[:-WINDOW_BYTES]
    return bool(chunk)


def prepare_repl(connection, timeout=10, clock=time.monotonic):
    """Confirm friendly REPL before Ctrl-D, excluding shutdown from timing."""
    connection.reset_input_buffer()
    connection.write(b"\x03\x03\x02")
    connection.flush()
    window = bytearray()
    started = clock()
    while clock() - started < timeout:
        if _read_window(connection, window) and PROMPT in window:
            return True
    return False


def measure_boot(connection, timeout=45, observe=2, clock=time.monotonic):
    """Return only fixed markers/timings, never raw USB output."""
    if not prepare_repl(connection, clock=clock):
        return {"ok": False, "error": "repl_not_confirmed", "ready_ms": None}
    connection.reset_input_buffer()
    started = clock()
    connection.write(b"\x04")
    connection.flush()
    window = bytearray()
    ready_at = None
    fault = False
    returned_to_repl = False
    while True:
        now = clock()
        if ready_at is None:
            if now - started >= timeout:
                break
        elif now - ready_at >= observe:
            break
        if not _read_window(connection, window):
            continue
        now = clock()
        if ready_at is None and READY in window:
            ready_at = now
        fault = fault or any(marker in window for marker in FAILURE_MARKERS)
        returned_to_repl = returned_to_repl or PROMPT in window
        if returned_to_repl:
            break
    ready_ms = None if ready_at is None else round((ready_at - started) * 1000, 1)
    ok = ready_at is not None and not fault and not returned_to_repl
    return {"ok": ok, "ready_ms": ready_ms, "fault_marker_seen": fault,
            "returned_to_repl": returned_to_repl,
            "post_ready_observation_ms": round(observe * 1000) if ok else 0,
            "error": None if ok else ("startup_fault" if fault else
                "returned_to_repl" if returned_to_repl else "ready_timeout")}


def find_port(ports):
    matches = [port.device for port in ports
               if (port.serial_number or "").upper() == PICO_SERIAL]
    if len(matches) != 1:
        raise ValueError("Expected exactly one known Pico serial number")
    return matches[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--observe", type=float, default=2,
                        help="Seconds to check for faults after each ready marker")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.attempts <= 10 or not 1 <= args.timeout <= 120 or not 0.1 <= args.observe <= 10:
        parser.error("Use 1..10 attempts, 1..120 s timeout, and 0.1..10 s observation")
    # Import lazily so parsing/tests never open or enumerate a USB device.
    import serial
    from serial.tools import list_ports

    report = {"serial": PICO_SERIAL, "started_utc": datetime.now(timezone.utc).isoformat(),
              "milestone": "Pocket MOSFET ready after first complete UI/input tick in current main.py",
              "requires_current_ready_marker_semantics": True,
              "home_screen_verified": False, "wifi_connected_required": False,
              "raw_serial_output_saved": False, "attempts": []}
    try:
        port = find_port(list_ports.comports())
        with serial.Serial(port, 115200, timeout=0.1, write_timeout=5) as connection:
            for attempt in range(1, args.attempts + 1):
                sample = measure_boot(connection, args.timeout, args.observe)
                sample["attempt"] = attempt
                report["attempts"].append(sample)
                if not sample["ok"]:
                    break
            # No final Ctrl-C/reset: closing this host connection leaves the
            # last successfully observed application running normally.
    except Exception as error:
        report["host_error_type"] = type(error).__name__
        # Serial/OS exception messages could contain arbitrary device output.
    samples = report["attempts"]
    times = [item["ready_ms"] for item in samples if item["ok"]]
    report["ok"] = len(samples) == args.attempts and all(item["ok"] for item in samples) and "host_error_type" not in report
    report["final_application_observed_running"] = bool(
        samples and samples[-1]["ok"] and "host_error_type" not in report)
    if times:
        report["ready_ms"] = {"minimum": min(times), "median": statistics.median(times),
                              "maximum": max(times), "mean": round(statistics.mean(times), 1)}
    text = json.dumps(report, indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
