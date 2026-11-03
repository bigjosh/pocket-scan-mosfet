"""Read-only replay of existing archives; no upload, deletion or output drive."""
import gc
import os
import hashlib
import binascii
import storage
from archive import UploadSource

verified = 0
for name in sorted(os.listdir('scans')):
    if not name.endswith(('.sent', '.pending')):
        continue
    gc.collect()
    info = storage.load_json('scans/' + name.rsplit('.', 1)[0] + '.meta')
    digest, size = hashlib.sha256(), 0
    source = UploadSource('scans/' + name)
    try:
        while True:
            block = source.read(256)
            if not block:
                break
            digest.update(block)
            size += len(block)
        assert size == info['wire_size']
        assert binascii.hexlify(digest.digest()).decode() == info['sha256']
        verified += 1
        print('EXISTING_SCAN_REPLAY_PASS', name, source.point_count)
    finally:
        source.close()
print('EXISTING_SCANS_VERIFIED', verified)
