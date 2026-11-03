"""Read-only pre/post layout diagnostic; both PWM pins are held low."""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import time
import os
import json
import hashlib
import binascii
import network
import storage
from hardware import Instrument


def digest(path):
    sha = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            block = stream.read(256)
            if not block:
                break
            sha.update(block)
    return binascii.hexlify(sha.digest()).decode()


network.WLAN(network.STA_IF).active(False)
network.WLAN(network.AP_IF).active(False)
public = ('main.py', 'model.py', 'hardware.py', 'engine.py', 'ui.py',
          'storage.py', 'archive.py', 'cloud.py', 'wifi.py', 'https_upload.py',
          'display.py', 'inputs.py', 'diagnostics.py', 'tls_memory.py',
          'google_roots.pem', 'settings.json', 'calibration.json')
print('PUBLIC_HASHES', json.dumps({name: digest(name) for name in public}))
print('SETTINGS', json.dumps(storage.load_settings()))
print('SCAN_FILES', json.dumps([
    {'name': name, 'bytes': os.stat('scans/' + name)[6], 'sha256': digest('scans/' + name)}
    for name in sorted(os.listdir('scans'))]))
instrument = None
try:
    instrument = Instrument()
    print('IDENTITY', json.dumps(instrument.identity))
    print('I2C_SCAN', json.dumps(instrument._bus.scan()))
    time.sleep_ms(500)
    totals = {role: [0, 0, 32767, -32768, 32767, -32768] for role in ('drain', 'gate')}
    for _ in range(200):
        pair = instrument.read_pair()
        for role in totals:
            bus, shunt = pair[role]['bus_code'], pair[role]['shunt_code']
            row = totals[role]
            row[0] += bus
            row[1] += shunt
            row[2] = min(row[2], bus)
            row[3] = max(row[3], bus)
            row[4] = min(row[4], shunt)
            row[5] = max(row[5], shunt)
        time.sleep_ms(4)
    print('ZERO_READBACK', json.dumps({'count': 200, 'columns': [
        'bus_sum', 'shunt_sum', 'bus_min', 'bus_max', 'shunt_min', 'shunt_max'], 'channels': totals}))
finally:
    if instrument is not None:
        instrument.close()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print('OUTPUTS_DISABLED')
