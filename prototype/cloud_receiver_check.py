"""Host check/configuration for the private endpoint; never prints its URL.

Read .local/cloud-url.txt. --configure writes ignored .local/cloud.json for
USB transfer. With --scan, send an explicit JSON payload file byte-for-byte for
retry; otherwise request the receiver's fixed health response.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import ssl
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MAX_SCAN_BYTES = 2 * 1024 * 1024


def validate_url(url):
    """Accept only a production Google Apps Script endpoint; never echo it."""
    if (not isinstance(url, str) or not 1 <= len(url) <= 2048 or
            any(ord(character) <= 32 or ord(character) > 126 for character in url)):
        raise ValueError("Invalid receiver endpoint")
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname != 'script.google.com' or
            parts.port not in (None, 443) or parts.username or parts.password or
            parts.query or parts.fragment or
            re.fullmatch(r'/macros/s/[A-Za-z0-9_-]+/exec', parts.path) is None):
        raise ValueError("Invalid receiver endpoint")
    return url


def load_scan_payload(path):
    """Load bounded exact JSON bytes; never regenerate an accepted scan body."""
    with path.open('rb') as stream:
        body = stream.read(MAX_SCAN_BYTES + 1)
    if not 1 <= len(body) <= MAX_SCAN_BYTES:
        raise ValueError("Invalid scan payload size")
    scan = json.loads(body.decode('utf-8'))
    if (not isinstance(scan, dict) or not isinstance(scan.get('scan_id'), str) or len(scan['scan_id']) > 96 or
            re.fullmatch(r'[0-9a-fA-F]+-[0-9a-fA-F]+', scan['scan_id']) is None or
            type(scan.get('point_count')) is not int or not 1 <= scan['point_count'] <= 4096):
        raise ValueError("Invalid scan payload identity")
    return body, {"scan_id": scan["scan_id"], "point_count": scan["point_count"],
                  "sha256": hashlib.sha256(body).hexdigest()}


def validate_ack(result, expected):
    if (not isinstance(result, dict) or result.get('ok') is not True or
            not all(result.get(key) == value for key, value in expected.items()) or
            type(result.get('point_count')) is not int or
            type(result.get('sheet_id')) is not int or result['sheet_id'] < 0):
        raise ValueError("Receiver acknowledgement did not match")


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
    parser.add_argument("--scan", type=Path, help="Explicit JSON payload to upload without reserialization")
    args = parser.parse_args()
    url = validate_url((ROOT / ".local/cloud-url.txt").read_text(encoding="utf-8-sig").strip())
    if args.configure:
        (ROOT / ".local/cloud.json").write_text(json.dumps({
            "version": 1, "url": url, "enabled": True}), encoding="utf-8")
    body = None
    expected = None
    if args.scan:
        body, expected = load_scan_payload(args.scan)
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
                          "settings_versions": [1, 2, 3], "scan_schema_versions": [1, 2, 3, 4, 5]}
        print(json.dumps({"receiver_health": True, "verified_https": True,
                          "settings_versions": result["settings_versions"],
                          "scan_schema_versions": result["scan_schema_versions"],
                          "configuration_prepared": args.configure}))
    else:
        validate_ack(result, expected)
        print(json.dumps({"acknowledged": True, "verified_https": True,
                          **expected, "sheet_id": result["sheet_id"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # urllib exceptions contain the capability URL. Report type only.
        print("Receiver check failed:", type(error).__name__)
        sys.exit(1)
