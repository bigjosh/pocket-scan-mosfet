import io
import json
import unittest
from unittest.mock import patch

from firmware.https_upload import Response, UploadTransport, endpoint_parts


class Source(io.BytesIO):
    def __init__(self, value):
        super().__init__(value)
        self.size = len(value)


class Socket:
    def __init__(self, response, short=73):
        self.response = response
        self.written = bytearray()
        self.closed = False
        self.short = short

    def write(self, data):
        count = min(self.short, len(data))
        self.written.extend(data[:count])
        return count

    def read(self, size):
        value, self.response = self.response[:97], self.response[97:]
        return value

    def close(self):
        self.closed = True


class Memory:
    def __init__(self):
        self.held = False
        self.events = []
        self.fail = False

    def reserve(self):
        self.events.append("reserve")
        if self.fail:
            return False
        self.held = True
        return True

    def release(self):
        self.events.append("release")
        held, self.held = self.held, False
        return held


def response(body, chunked=False):
    raw = json.dumps(body).encode()
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
    if chunked:
        return head + b"Transfer-Encoding: chunked\r\n\r\n" + ("%x\r\n" % len(raw)).encode() + raw + b"\r\n0\r\n\r\n"
    return head + ("Content-Length: %d\r\n\r\n" % len(raw)).encode() + raw


class UploadTests(unittest.TestCase):
    def test_payload_progress_counts_only_accepted_body_bytes_through_redirect(self):
        payload = b"p" * 2500
        sockets = [Socket(b"HTTP/1.1 302 Found\r\nLocation: https://script.googleusercontent.com/macros/echo?key=private\r\n\r\n", short=73),
                   Socket(response({"ok": True}), short=11)]
        connections = []
        def connect(host, ca):
            connections.append(host)
            return sockets[len(connections) - 1]
        transport = UploadTransport(connector=connect, clock_sync=lambda: None,
                                    clock=lambda: 0, source_factory=lambda path: Source(payload))
        transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        self.assertEqual((transport.upload_stage, transport.payload_sent, transport.payload_total), (1, 0, 2500))
        saw_prefetch = saw_partial = saw_confirm = False
        result = None
        for unused in range(1000):
            previous = transport.payload_sent
            result = transport.step()
            written = bytes(sockets[0].written)
            actual = len(written.split(b"\r\n\r\n", 1)[1]) if b"\r\n\r\n" in written else 0
            self.assertEqual(transport.payload_sent, actual)
            self.assertGreaterEqual(transport.payload_sent, previous)
            if transport.sent > transport.payload_sent:
                saw_prefetch = True
            if 0 < transport.payload_sent < len(payload):
                saw_partial = True
            if len(connections) == 2 or transport.state == "restore":
                saw_confirm = True
                self.assertEqual((transport.upload_stage, transport.payload_sent), (3, len(payload)))
            if result is not None:
                break
        self.assertEqual(result, {"ok": True})
        self.assertTrue(saw_prefetch and saw_partial and saw_confirm)
        self.assertTrue(bytes(sockets[1].written).startswith(b"GET "))
        self.assertEqual(transport.payload_sent, len(payload))
        self.assertEqual(transport.upload_stage, 3)  # Only manager can confirm ACK identity.

    def test_blocked_body_writes_and_buffer_prefetch_never_advance_percentage(self):
        socket = Socket(response({"ok": True}), short=17)
        transport = UploadTransport(connector=lambda *args: socket, clock_sync=lambda: None,
                                    clock=lambda: 0, source_factory=lambda path: Source(b"x" * 2048))
        transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        for unused in range(100):
            transport.step()
            if transport._out_is_body and transport.out:
                break
        self.assertEqual(transport.upload_stage, 2)
        self.assertEqual(transport.sent, 1024)
        self.assertEqual(transport.payload_sent, 0)
        pending = transport.out
        with patch.object(socket, "write", return_value=None):
            transport.step()
        self.assertEqual(transport.payload_sent, 0)
        self.assertEqual(transport.out, pending)
        with patch.object(socket, "write", side_effect=OSError(11)):
            transport.step()
        self.assertEqual(transport.payload_sent, 0)
        self.assertEqual(transport.out, pending)
        transport.step()
        self.assertEqual(transport.payload_sent, 17)
        with patch.object(socket, "write", side_effect=OSError(5)):
            with self.assertRaises(RuntimeError):
                transport.step()
        self.assertEqual(transport.payload_sent, 17)
        self.assertEqual(transport.upload_stage, 2)

    def test_missing_explicit_source_factory_never_reads_file_or_connects(self):
        calls = []
        transport = UploadTransport(connector=lambda *args: calls.append("network"))
        with self.assertRaisesRegex(ValueError, "source factory is required"):
            transport.start("https://script.google.com/macros/s/EXAMPLE/exec", "private-file", "scan", "a" * 64, 1)
        self.assertEqual(calls, [])
        self.assertIsNone(transport.stream)

    def test_one_scan_boot_returns_ack_without_reserving_another_tls_workspace(self):
        memory = Memory()
        ack = {"ok": True, "scan_id": "face-1234"}
        sock = Socket(response(ack))
        transport = UploadTransport(connector=lambda host, ca: sock,
                                    source_factory=lambda path: Source(b"{}"),
                                    clock_sync=lambda: None, clock=lambda: 0,
                                    memory=memory, restore_after_transfer=False)
        transport.start("https://script.google.com/macros/s/id/exec", object(), "face-1234", "a" * 64, 1)
        for unused in range(100):
            transport.step()
            if transport.state == "restore":
                break
        self.assertEqual(transport.state, "restore")
        memory.fail = True  # No later handshake is allowed in this boot.
        self.assertEqual(transport.step(), ack)
        self.assertEqual(memory.events, ["reserve", "release"])
        self.assertTrue(sock.closed)

    def test_tls_reserve_is_held_during_setup_then_released_across_redirect(self):
        memory = Memory()
        ack = {"ok": True}
        sockets = [Socket(b"HTTP/1.1 302 Found\r\nLocation: https://script.googleusercontent.com/macros/echo?key=secret\r\n\r\n"),
                   Socket(response(ack))]
        calls = []
        def source(path):
            self.assertTrue(memory.held)
            return Source(b"{}")
        def connect(host, ca):
            self.assertFalse(memory.held)
            calls.append(host)
            return sockets[len(calls) - 1]
        transport = UploadTransport(connector=connect, source_factory=source,
                                    clock_sync=lambda: self.assertTrue(memory.held),
                                    clock=lambda: 0, memory=memory)
        transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        for _ in range(100):
            result = transport.step()
            if transport.state == "restore":
                break
        self.assertIsNone(result)
        self.assertEqual(["script.google.com", "script.googleusercontent.com"], calls)
        self.assertEqual(["reserve", "release"], memory.events)
        self.assertFalse(memory.held)
        self.assertIsNone(transport.sock)
        self.assertIsNone(transport.stream)
        self.assertIsNone(transport.response)
        self.assertIsNone(transport.path)
        self.assertIsNone(transport._parser.body)
        self.assertIsNone(transport._parser.headers)
        original_loads = json.loads
        def load(value):
            self.assertTrue(memory.held)
            return original_loads(value)
        with patch("firmware.https_upload.json.loads", side_effect=load):
            self.assertEqual(ack, transport.step())
        self.assertTrue(memory.held)
        self.assertEqual(["reserve", "release", "reserve"], memory.events)

    def test_failed_post_response_restore_never_returns_or_parses_ack(self):
        memory = Memory()
        sock = Socket(response({"ok": True}))
        transport = UploadTransport(connector=lambda host, ca: sock,
                                    source_factory=lambda path: Source(b"{}"),
                                    clock_sync=lambda: None, clock=lambda: 0, memory=memory)
        transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        with patch("firmware.https_upload.json.loads", side_effect=AssertionError("Decoded before restore")):
            for _ in range(100):
                self.assertIsNone(transport.step())
                if transport.state == "restore":
                    break
            self.assertEqual("restore", transport.state)
            self.assertTrue(sock.closed)
            memory.fail = True
            with self.assertRaisesRegex(RuntimeError, "Cloud upload failed; scan remains saved"):
                transport.step()
        self.assertEqual("idle", transport.state)
        self.assertIsNone(transport.response)
        self.assertEqual(0, transport._ack_length)

    def test_staged_ack_restoration_obeys_overall_transfer_timeout(self):
        memory, now = Memory(), [0]
        sock = Socket(response({"ok": True}))
        transport = UploadTransport(connector=lambda host, ca: sock,
                                    source_factory=lambda path: Source(b"{}"),
                                    clock_sync=lambda: None, clock=lambda: now[0], memory=memory)
        transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        for _ in range(100):
            transport.step()
            if transport.state == "restore":
                break
        memory.fail = True
        now[0] = 120001
        with self.assertRaisesRegex(RuntimeError, "Cloud upload failed; scan remains saved"):
            transport.step()
        self.assertEqual("idle", transport.state)

    def test_memory_restore_is_deferred_on_cancel_and_failure(self):
        for fail_connection in (False, True):
            with self.subTest(fail_connection=fail_connection):
                memory = Memory()
                sock = Socket(b"")
                def connect(host, ca):
                    if fail_connection:
                        raise OSError("secret")
                    return sock
                transport = UploadTransport(connector=connect, source_factory=lambda path: Source(b"{}"),
                                            clock_sync=lambda: None, clock=lambda: 0, memory=memory)
                transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
                transport.step()
                if fail_connection:
                    with self.assertRaisesRegex(RuntimeError, "Cloud upload failed"):
                        transport.step()
                else:
                    transport.step()
                    self.assertFalse(transport.maintain_memory())
                    transport.close()
                self.assertFalse(memory.held)
                self.assertEqual(["reserve", "release"], memory.events)
                memory.fail = True
                self.assertFalse(transport.maintain_memory())
                self.assertFalse(memory.held)
                memory.fail = False
                self.assertTrue(transport.maintain_memory())
                self.assertTrue(memory.held)

    def test_unavailable_memory_prevents_source_or_network_start(self):
        memory = Memory()
        memory.fail = True
        calls = []
        transport = UploadTransport(connector=lambda host, ca: calls.append("network"),
                                    source_factory=lambda path: calls.append("source"), memory=memory)
        with self.assertRaisesRegex(RuntimeError, "Upload memory unavailable; scan remains in RAM"):
            transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        self.assertEqual([], calls)
        self.assertEqual("idle", transport.state)

    def test_post_streams_once_redirect_is_get_and_ack_is_returned(self):
        payload = b'{"scan_id":"0123-abcd","points":[' + b"[1,2]," * 900 + b"[3,4]]}"
        ack = {"ok": True, "scan_id": "0123-abcd", "sha256": "a" * 64, "point_count": 901}
        sockets = [Socket(b"HTTP/1.1 302 Found\r\nLocation: https://script.googleusercontent.com/macros/echo?key=secret\r\n\r\n"),
                   Socket(response(ack, True))]
        hosts = []
        def connect(host, ca):
            hosts.append(host)
            return sockets[len(hosts) - 1]
        clock_calls = []
        transport = UploadTransport(connector=connect, clock_sync=lambda: clock_calls.append(1),
                                    clock=lambda: 1, source_factory=lambda path: Source(payload))
        transport.start("https://script.google.com/macros/s/secret/exec", "binary.pending", "0123-abcd", "a" * 64, 901)
        self.assertEqual(hosts, [])
        result = None
        for _ in range(500):
            result = transport.step()
            if result is not None:
                break
        self.assertEqual(result, ack)
        self.assertEqual(clock_calls, [1])
        self.assertEqual(hosts, ["script.google.com", "script.googleusercontent.com"])
        sent = bytes(sockets[0].written)
        self.assertTrue(sent.startswith(b"POST /macros/s/secret/exec HTTP/1.1"))
        self.assertEqual(sent.split(b"\r\n\r\n", 1)[1], payload)
        self.assertIn(("Content-Length: %d" % len(payload)).encode(), sent)
        self.assertTrue(bytes(sockets[1].written).startswith(b"GET /macros/echo?key=secret HTTP/1.1"))
        self.assertNotIn(payload, sockets[1].written)
        self.assertTrue(all(item.closed for item in sockets))

    def test_untrusted_redirect_is_not_followed_or_exposed(self):
        sock = Socket(b"HTTP/1.1 302 Found\r\nLocation: https://evil.invalid/secret\r\n\r\n")
        calls = []
        def connect(host, ca):
            calls.append(host)
            return sock
        transport = UploadTransport(connector=connect, clock_sync=lambda: None,
                                    clock=lambda: 1, source_factory=lambda path: Source(b"{}"))
        transport.start("https://script.google.com/macros/s/secret/exec", "x", "a-b", "a" * 64, 1)
        with self.assertRaisesRegex(RuntimeError, "Cloud upload failed; scan remains saved") as raised:
            for _ in range(100):
                transport.step()
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(calls, ["script.google.com"])
        self.assertTrue(sock.closed)

    def test_cancel_closes_source_and_socket_without_acknowledgement(self):
        source, sock = Source(b"{}"), Socket(b"")
        transport = UploadTransport(connector=lambda host, ca: sock, clock_sync=lambda: None,
                                    clock=lambda: 1, source_factory=lambda path: source)
        transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        transport.step()
        transport.step()
        transport.close()
        self.assertTrue(source.closed)
        self.assertTrue(sock.closed)
        self.assertIsNone(transport.step())

    def test_timeout_stops_nonblocking_wait(self):
        now = [0]
        transport = UploadTransport(clock=lambda: now[0], source_factory=lambda path: Source(b"{}"))
        transport.start("https://script.google.com/macros/s/id/exec", "x", "a-b", "a" * 64, 1)
        now[0] = 120001
        with self.assertRaises(RuntimeError):
            transport.step()
        self.assertEqual(transport.state, "idle")

    def test_endpoint_rejects_other_hosts_query_injection_and_dev(self):
        for url in ("http://script.google.com/macros/s/id/exec", "https://evil.invalid/macros/s/id/exec",
                    "https://script.google.com/macros/s/id/dev", "https://script.google.com/macros/s/id/exec?key=x",
                    "https://script.google.com/macros/s/id\r\nX:1/exec", "https://script.google.com/macros/s//exec"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                endpoint_parts(url)


class ParserTests(unittest.TestCase):
    def test_fragmented_content_length_and_chunked(self):
        for chunked in (False, True):
            parsed = Response()
            for byte in response({"ok": True}, chunked):
                parsed.feed(bytes([byte]))
            self.assertTrue(parsed.done)
            self.assertEqual(json.loads(parsed.body), {"ok": True})

    def test_truncated_oversize_non_json_and_conflicting_framing_rejected(self):
        values = [
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 90\r\n\r\n{}",
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 5000\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<html>",
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}",
        ]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Response().feed(value, eof=True)


if __name__ == "__main__":
    unittest.main()
