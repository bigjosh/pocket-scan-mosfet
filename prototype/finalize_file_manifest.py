"""Bounded read-only hashes for a finalization repair; never print credentials."""
import gc
import os
import hashlib
import binascii
import json

gc.collect()
names = ['settings.json', 'calibration.bin', 'wifi.json', 'cloud.json']
names.extend('scans/' + name for name in sorted(os.listdir('scans')))
for name in names:
    hasher = hashlib.sha256()
    with open(name, 'rb') as stream:
        while True:
            block = stream.read(256)
            if not block:
                break
            hasher.update(block)
    print('PRESERVE_FILE', json.dumps({'path': name,
          'sha256': binascii.hexlify(hasher.digest()).decode()}))
print('FREE_FLASH', os.statvfs('/'))
