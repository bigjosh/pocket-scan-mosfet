"""Explicit one-time native conversion; never called by boot or scan.

Requires old calibration.json and no calibration.bin. Preserve the JSON until
the host sees a verified, exact canonical fingerprint after binary reload.
Both PWM pins remain low; no calibration sweep, uploads or settings changes.
"""
import main
import os
import hashlib
import binascii
from calibration_json_legacy import load_calibration as load_legacy
from ram_payload import calibration_hash


def file_info(path):
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(256)
            if not block:
                break
            digest.update(block)
            total += len(block)
    return {"bytes": total, "sha256": binascii.hexlify(digest.digest()).decode()}


def run():
    try:
        os.stat("calibration.bin")
    except OSError as error:
        if error.args[0] != 2:
            raise
    else:
        raise ValueError("Binary calibration already exists; refusing conversion")
    original = file_info("calibration.json")
    with open("calibration.json", "r") as stream:
        calibration = load_legacy(stream)
    settings = main.storage.load_settings(migrate=False)
    main.model.validate_scan_calibration(calibration, settings)
    expected = calibration_hash(calibration)
    counts = [len(calibration["channels"][role]["points"]) for role in ("drain", "gate")]
    main.gc.collect()
    main.storage.save_calibration_atomic(calibration)
    calibration = None
    main.gc.collect()
    calibration = main.storage.load_calibration()
    main.model.validate_scan_calibration(calibration, settings)
    actual = calibration_hash(calibration)
    if actual != expected:
        raise RuntimeError("Binary calibration changed the canonical fingerprint")
    if file_info("calibration.json") != original:
        raise RuntimeError("Original calibration file changed")
    main.gc.collect()
    print("CALIBRATION_BINARY_MIGRATED", main.storage.json.dumps({
        "canonical_sha256": actual, "canonical_fingerprint_unchanged": True,
        "channel_points": counts, "json": original,
        "binary": file_info("calibration.bin"), "legacy_file_preserved": True,
        "free_heap": main.gc.mem_free(), "outputs_low": True}))


if __name__ == "__main__":
    run()
