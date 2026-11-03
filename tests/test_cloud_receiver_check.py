"""Standalone receiver helper: exact payload bytes and strict HTTPS/ACK boundaries."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import urllib.request

from prototype import cloud_receiver_check as check


class ReceiverCheckTests(unittest.TestCase):
    def test_production_url_only_without_userinfo_query_fragment_or_controls(self):
        url = 'https://script.google.com/macros/s/EXAMPLE_ONLY/exec'
        self.assertEqual(url, check.validate_url(url))
        for invalid in ('http' + url[5:], url + '?key=x', url + '#x', url.replace('/exec', '/dev'),
                        url.replace('script.google.com', 'script.google.com.evil.invalid'),
                        url.replace('https://', 'https://user@'), url + '\n',
                        url.replace('/macros/', ':444/macros/')):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                check.validate_url(invalid)

    def test_explicit_json_body_bytes_are_not_reserialized_or_read_as_archive(self):
        body = b'{ "scan_id":"abcd-1234", "point_count":1, "points":[[1.000,2,3,4]] }\n'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.json'
            path.write_bytes(body)
            actual, expected = check.load_scan_payload(path)
        self.assertEqual(body, actual)
        self.assertEqual(hashlib.sha256(body).hexdigest(), expected['sha256'])
        self.assertNotEqual(json.dumps(json.loads(body)).encode(), actual)

    def test_empty_oversized_invalid_identity_or_zero_count_body_rejected(self):
        bodies = [b'', b'x' * (check.MAX_SCAN_BYTES + 1), b'[]', b'not-json',
                  b'{"scan_id":"abcd-1234","point_count":0}',
                  b'{"scan_id":"abcd-1234","point_count":true}',
                  b'{"scan_id":"private text","point_count":1}']
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'scan.json'
            for body in bodies:
                path.write_bytes(body)
                with self.assertRaises(ValueError):
                    check.load_scan_payload(path)

    def test_ack_requires_exact_identity_digest_count_and_typed_sheet_id(self):
        expected = {'scan_id': 'abcd-1234', 'sha256': 'a' * 64, 'point_count': 1}
        good = dict(expected, ok=True, sheet_id=123)
        check.validate_ack(good, expected)
        for key, value in (('ok', 1), ('scan_id', 'abcd-1235'), ('sha256', 'b' * 64),
                           ('point_count', True), ('point_count', 2), ('sheet_id', True),
                           ('sheet_id', -1), ('sheet_id', '123')):
            bad = dict(good); bad[key] = value
            with self.assertRaises(ValueError):
                check.validate_ack(bad, expected)

    def test_redirect_allows_only_https_google_content_with_default_tls_port(self):
        request = urllib.request.Request('https://script.google.com/macros/s/EXAMPLE_ONLY/exec')
        handler = check.GoogleRedirect()
        target = 'https://script.googleusercontent.com/macros/echo?token=synthetic'
        redirected = handler.redirect_request(request, None, 302, '', {}, target)
        self.assertEqual(target, redirected.full_url)
        for code, url in ((301, target), (302, target.replace('https:', 'http:')),
                          (302, target.replace('googleusercontent.com', 'evil.invalid')),
                          (302, target.replace('https://', 'https://user@')),
                          (302, target.replace('/macros/', ':444/macros/'))):
            with self.assertRaises(ValueError):
                handler.redirect_request(request, None, code, '', {}, url)


if __name__ == '__main__':
    unittest.main()
