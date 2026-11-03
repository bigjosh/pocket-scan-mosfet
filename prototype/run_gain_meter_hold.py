"""Host monitor: keep USB open; creating the specified stop file interrupts hold.

Device has its own ten-minute timeout and finally shutdown. Host also attempts
an explicit output shutdown on every exit, without restarting the normal UI.
"""
import argparse
from pathlib import Path
import time
from serial.tools import list_ports
from mpremote.transport_serial import SerialTransport

parser = argparse.ArgumentParser()
parser.add_argument('--stop-file', type=Path, required=True)
args = parser.parse_args()
if args.stop_file.exists():
    raise SystemExit('Stop file already exists; choose a fresh path')
matches = [port for port in list_ports.comports()
           if (port.serial_number or '').upper() == '3B8922C9B91BBE9F']
if len(matches) != 1:
    raise SystemExit('Expected exactly one known Pico')
transport = None
try:
    transport = SerialTransport(matches[0].device, timeout=1)
    transport.enter_raw_repl(soft_reset=True)
    source = Path(__file__).with_name('gain_drain_meter_hold.py').read_text(encoding='utf-8')
    transport.exec_raw_no_follow('_confirm_open_gain_hold=True\n' + source)
    transport.serial.timeout = 0.1
    started, stopping = time.monotonic(), False
    eof_count = 0
    while eof_count < 2 and time.monotonic() - started < 620:
        if args.stop_file.exists() and not stopping:
            transport.serial.write(b'\x03')
            stopping = True
        chunk = transport.serial.read(transport.serial.in_waiting or 1)
        if chunk:
            eof_count += chunk.count(b'\x04')
            print(chunk.replace(b'\x04', b'').decode('utf-8', 'replace'), end='', flush=True)
finally:
    if transport is not None:
        try:
            transport.enter_raw_repl(soft_reset=False)
            result = transport.exec("from machine import Pin\n"
                                    "Pin(20,Pin.OUT,value=0)\nPin(21,Pin.OUT,value=0)\n"
                                    "print('HOST_VERIFIED_GPIO20_21_LOW')")
            print(result.decode('utf-8', 'replace'), end='', flush=True)
        finally:
            transport.close()
