"""Hashes of public firmware/CA files and measurement settings, never credentials."""
import os
import json
import hashlib
import binascii

result = {}
for name in os.listdir():
    if name.endswith((".py", ".mpy")) or name in ("settings.json", "google_roots.pem"):
        with open(name, "rb") as stream:
            hasher = hashlib.sha256()
            while True:
                chunk = stream.read(512)
                if not chunk:
                    break
                hasher.update(chunk)
            digest = hasher.digest()
        result[name] = binascii.hexlify(digest).decode()
print(json.dumps(result))
