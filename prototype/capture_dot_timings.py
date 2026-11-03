"""Read DOT_US records without sending commands or interrupting the Pico.

Example: python prototype/capture_dot_timings.py --seconds 60 --output output/smoke-test/dot-timing.log
Only fixed timing records are saved; other USB text (including settings) is
discarded. Opening this serial connection does not request a reset or scan.
"""
import argparse
from pathlib import Path
import time

import serial
from serial.tools import list_ports


def capture(seconds, output, until_restart=False):
    ports = [port.device for port in list_ports.comports()
             if port.serial_number == "3B8922C9B91BBE9F"]
    if len(ports) != 1:
        raise RuntimeError("Expected exactly one known Pico USB connection")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    total_wall = total_render = total_engine = 0
    pending = bytearray()
    with serial.Serial(ports[0], baudrate=115200, timeout=0.1) as connection, \
            output.open("w", encoding="ascii") as stream:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            block = connection.read(min(512, connection.in_waiting or 1))
            if not block:
                continue
            for value in block:
                if value == 10:
                    if until_restart and count and b"Pocket MOSFET ready" in pending:
                        end = 0  # The profiled scan cycle has returned to Home.
                    if pending.startswith(b"DOT_US "):
                        try:
                            line = pending.decode("ascii").strip()
                            fields = dict(part.split("=", 1) for part in line.split()[1:])
                            values = {key: int(value) for key, value in fields.items()}
                            if len(values) < 20 or any(value < 0 for value in values.values()):
                                raise ValueError("Incomplete timing line")
                            total_wall += values["wall"]
                            total_render += values["render"]
                            total_engine += values["engine"]
                            stream.write(line + "\n")
                            stream.flush()
                            count += 1
                        except (ValueError, KeyError, UnicodeError):
                            pass
                    pending.clear()
                elif len(pending) < 1024:
                    pending.append(value)
                else:
                    pending.clear()
    print("Timing records:", count, "Saved:", str(output))
    if count:
        print("Mean ms: wall=%.1f render=%.1f engine=%.1f" %
              (total_wall / count / 1000, total_render / count / 1000,
               total_engine / count / 1000))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--output", required=True)
    parser.add_argument("--until-restart", action="store_true")
    arguments = parser.parse_args()
    if not 0 < arguments.seconds <= 3600:
        parser.error("seconds must be in (0, 3600]")
    capture(arguments.seconds, arguments.output, arguments.until_restart)
