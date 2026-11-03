"""Build public firmware modules for the verified Pico MicroPython runtime.

One-time Windows x64 dependency installation, confined to this workspace:
    python -m pip install --target .tools/mpy-cross --no-deps --only-binary=:all: \
        --require-hashes -r .tools/mpy-cross-requirements.txt

Build / verify without connecting to or changing the Pico:
    python prototype/build_mpy.py
    python prototype/build_mpy.py --check

Outputs: output/firmware-mpy/*.mpy and manifest.json. main.py and boot.py stay
as source and are never compiled here. The manifest identifies exact public
source and output hashes. Keep assertions and source line numbers (-O0).

Deployment is deliberately separate. In each device directory foo.py takes
precedence over foo.mpy; back up/remove only the corresponding public .py
modules before testing the .mpy deployment. Preserve main.py, boot.py, settings,
credentials, CA bundle and old scan files. Retired calibration modules are not deployed. Do not copy a stale
build: --check must pass after every source edit and before device transfer.

Reference: https://docs.micropython.org/en/v1.29.0/reference/mpyfiles.html
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "firmware"
OUTPUT = ROOT / "output" / "firmware-mpy"
COMPILER = ROOT / ".tools" / "mpy-cross" / "mpy_cross" / "mpy-cross.exe"
TARGET_VERSION = "1.29.0"
TARGET_MPY = 7942  # Verified sys.implementation._mpy on this RPI_PICO2_W.
SMALL_INT_BITS = 31
EXCLUDED = {"main.py", "boot.py"}
FLAGS = ("-O0", "-msmall-int-bits=31", "-X", "emit=bytecode", "-X", "source-lines")


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def header(path):
    data = path.read_bytes()[:4]
    if len(data) != 4 or data[0] != ord("M") or data[1] != TARGET_MPY & 255:
        raise RuntimeError("Incompatible .mpy version: " + path.name)
    # Pure bytecode has no native architecture/architecture flags. Its
    # subversion is ignored by the target loader, per MicroPython docs.
    if data[2] & 0xFC or data[3] != SMALL_INT_BITS:
        raise RuntimeError("Expected portable bytecode with 31-bit small ints: " + path.name)
    return {"version": data[1], "flags": data[2], "small_int_bits": data[3]}


def files():
    return sorted(path for path in SOURCE.glob("*.py") if path.name not in EXCLUDED)


def check():
    manifest = json.loads((OUTPUT / "manifest.json").read_text(encoding="utf-8"))
    expected = {path.name for path in files()}
    if {item["source"] for item in manifest["modules"]} != expected:
        raise RuntimeError("Public module inventory changed; rebuild")
    if {path.name for path in OUTPUT.glob("*.mpy")} != {Path(name).with_suffix(".mpy").name for name in expected}:
        raise RuntimeError("Unexpected/missing .mpy output; review the output directory")
    for item in manifest["modules"]:
        source, output = SOURCE / item["source"], OUTPUT / item["output"]
        if sha256(source) != item["source_sha256"] or sha256(output) != item["output_sha256"]:
            raise RuntimeError("Build is stale or modified: " + item["source"])
        if header(output) != item["header"]:
            raise RuntimeError("Changed bytecode header: " + output.name)
    for item in manifest["source_only"]:
        if sha256(SOURCE / item["source"]) != item["source_sha256"]:
            raise RuntimeError("Source entry point changed; rebuild manifest: " + item["source"])
    return manifest


def build():
    if not COMPILER.is_file():
        raise RuntimeError("Install the pinned compiler using this script's instructions")
    version = subprocess.run([str(COMPILER), "--version"], check=True,
                             capture_output=True, text=True).stdout.strip()
    match = re.search(r"MicroPython v([^ ]+).*emitting mpy v(\d+)\.(\d+)", version)
    if not match or match[1] != TARGET_VERSION or int(match[2]) != TARGET_MPY & 255:
        raise RuntimeError("Compiler does not match the verified runtime: " + version)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sources = files()
    expected = {path.with_suffix(".mpy").name for path in sources}
    if any(path.name not in expected for path in OUTPUT.glob("*.mpy")):
        raise RuntimeError("Unexpected old .mpy output; review before building")
    # A failed rebuild must not leave a previously valid manifest beside a
    # partial set of replaced modules. No device files are affected.
    manifest_path = OUTPUT / "manifest.json"
    if manifest_path.exists():
        manifest_path.unlink()
    manifest = {
        "target_runtime": {"version": TARGET_VERSION, "mpy": TARGET_MPY,
                           "board": "RPI_PICO2_W", "small_int_bits": SMALL_INT_BITS},
        "compiler": {"version": version, "sha256": sha256(COMPILER),
                     "package": "mpy-cross==1.29.0.post2", "flags": list(FLAGS)},
        "modules": [], "source_only": [],
        "deployment_note": "Matching .py modules shadow .mpy; preserve source entry points and all device data.",
    }
    for source in sources:
        before = sha256(source)
        output = OUTPUT / source.with_suffix(".mpy").name
        temporary = output.with_suffix(".mpy.tmp")
        try:
            subprocess.run([str(COMPILER), *FLAGS, "-s", source.name,
                            "-o", str(temporary), str(source)], check=True,
                           capture_output=True, text=True)
            if sha256(source) != before:
                raise RuntimeError("Source changed during compilation: " + source.name)
            info = header(temporary)
            temporary.replace(output)
        finally:
            if temporary.exists():
                temporary.unlink()
        manifest["modules"].append({"source": source.name, "source_sha256": before,
                                    "source_bytes": source.stat().st_size,
                                    "output": output.name, "output_sha256": sha256(output),
                                    "output_bytes": output.stat().st_size, "header": info})
    for name in sorted(EXCLUDED):
        source = SOURCE / name
        if source.is_file():
            manifest["source_only"].append({"source": name, "source_sha256": sha256(source)})
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return check()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify existing output without running compiler")
    args = parser.parse_args()
    result = check() if args.check else build()
    print(json.dumps({"verified": True, "module_count": len(result["modules"]),
                      "source_bytes": sum(row["source_bytes"] for row in result["modules"]),
                      "bytecode_bytes": sum(row["output_bytes"] for row in result["modules"]),
                      "manifest": str(OUTPUT / "manifest.json"), "device_access": False}))
