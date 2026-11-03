"""Host check/configuration for the private endpoint; never prints its URL.

Read .local/cloud-url.txt. --configure writes ignored .local/cloud.json for
USB transfer. With --scan, send the exact archived payload to test idempotent
retry; otherwise request the receiver's fixed health response.
"""
import argparse
import hashlib
import json
from pathlib import Path
import ssl
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firmware.archive import UploadSource
from firmware.cloud import validate_url


class GoogleRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urllib.parse.urlsplit(newurl)
        if (code not in (302, 303) or parts.scheme != "https"
                or parts.hostname != "script.googleusercontent.com"
                or parts.port not in (None, 443) or parts.username or parts.password):
            raise ValueError("Unexpected receiver redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--scan", type=Path)
    args = parser.parse_args()
    url = validate_url((ROOT / ".local/cloud-url.txt").read_text(encoding="utf-8-sig").strip())
    if args.configure:
        (ROOT / ".local/cloud.json").write_text(json.dumps({
            "version": 1, "url": url, "enabled": True}), encoding="utf-8")
    body = None
    expected = None
    if args.scan:
        source = UploadSource(str(args.scan))
        try:
            blocks = []
            while True:
                block = source.read(1024)
                if not block:
                    break
                blocks.append(block)
            body = b"".join(blocks)
        finally:
            source.close()
        scan = json.loads(body)
        expected = {"scan_id": scan["scan_id"], "point_count": scan["point_count"],
                    "sha256": hashlib.sha256(body).hexdigest()}
        metadata = json.loads(args.scan.with_suffix(".meta").read_text(encoding="utf-8"))
        if expected["sha256"] != metadata["sha256"]:
            # CPython and RP2 MicroPython can spell the same float32 value
            # differently. An accepted scan must only retry its exact body.
            print("Binary scan verified; host JSON spelling differs from the device. "
                  "No upload attempted. Use the Pico queue to retry, or export CSV.")
            return
    context = ssl.create_default_context(cafile=str(ROOT / "firmware/google_roots.pem"))
    opener = urllib.request.build_opener(GoogleRedirect(), urllib.request.HTTPSHandler(context=context))
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with opener.open(request, timeout=45) as response:
        raw = response.read(4097)
        if response.status != 200 or len(raw) > 4096:
            raise ValueError("Invalid receiver response")
        result = json.loads(raw)
    if expected is None:
        assert result == {"ok": True, "service": "pocket-scan-mosfet", "schema_version": 1,
                          "settings_versions": [1, 2, 3], "scan_schema_versions": [1, 2, 3]}
        print(json.dumps({"receiver_health": True, "verified_https": True,
                          "settings_versions": result["settings_versions"],
                          "scan_schema_versions": result["scan_schema_versions"],
                          "configuration_prepared": args.configure}))
    else:
        assert result.get("ok") is True
        assert all(result.get(key) == value for key, value in expected.items())
        assert isinstance(result.get("sheet_id"), int)
        print(json.dumps({"acknowledged": True, "verified_https": True,
                          **expected, "sheet_id": result["sheet_id"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # urllib exceptions contain the capability URL. Report type only.
        print("Receiver check failed:", type(error).__name__)
        sys.exit(1)
