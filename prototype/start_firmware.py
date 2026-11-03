"""Host: soft reboot the known Pico, verify startup, leave its UI running."""
import json
from pathlib import Path
import time
import serial
from serial.tools import list_ports

matches = [port for port in list_ports.comports()
           if (port.serial_number or "").upper() == "3B8922C9B91BBE9F"]
if len(matches) != 1:
    raise SystemExit("Expected exactly one Pico with serial 3B8922C9B91BBE9F")
with serial.Serial(matches[0].device, 115200, timeout=0.2) as connection:
    connection.write(b"\x03\x03\x02")
    time.sleep(0.25)
    connection.reset_input_buffer()
    connection.write(b"\x04")
    started = time.monotonic()
    received = bytearray()
    while time.monotonic() - started < 8:
        received.extend(connection.read(connection.in_waiting or 1))
text = received.decode("utf-8", "replace")
passed = "Pocket MOSFET ready" in text and "Traceback" not in text
report = {"port": matches[0].device, "serial": "3B8922C9B91BBE9F",
          "boot_verified": passed, "left_running": passed, "output": text}
destination = Path(__file__).resolve().parents[1] / "output/smoke-test/firmware-boot-20261101.json"
destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
if not passed:
    raise SystemExit("Startup did not pass; inspect the captured USB log")
