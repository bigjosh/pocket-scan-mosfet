"""Small, verified HTTPS uploader for the Apps Script receiver.

Only setup/DNS/TLS can block briefly; body writes and response reads are
nonblocking and bounded. Call only while outputs are stopped. No URL, response
body, or underlying exception is exposed in errors: the URL is a capability.
"""

import json
import os
import time

try:
    from time import ticks_ms, ticks_diff
except ImportError:
    def ticks_ms():
        return int(time.monotonic() * 1000)

    def ticks_diff(now, before):
        return now - before

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_HEADER_BYTES = 8192
MAX_RESPONSE_BYTES = 4096
BLOCK_BYTES = 1024
TRANSFER_TIMEOUT_MS = 120000
_clock_ready = False


def endpoint_parts(url):
    """Accept only Google's published /exec URL, with no query or fragment."""
    prefix = "https://script.google.com/macros/s/"
    if not isinstance(url, str) or len(url) > 256 or not url.startswith(prefix) or not url.endswith("/exec"):
        raise ValueError("Use the published Google Apps Script URL ending in /exec")
    deployment = url[len(prefix):-5]
    if not 1 <= len(deployment) <= 180 or not all(
            char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in deployment):
        raise ValueError("Use the published Google Apps Script URL ending in /exec")
    return "script.google.com", url[len("https://script.google.com"):]


def _redirect_parts(url):
    prefix = "https://script.googleusercontent.com/"
    if not isinstance(url, str) or not url.startswith(prefix) or len(url) > 4096:
        raise ValueError("Upload response redirect was not accepted")
    if any(ord(char) < 33 or ord(char) > 126 or char in "\\#" for char in url):
        raise ValueError("Upload response redirect was not accepted")
    return "script.googleusercontent.com", "/" + url[len(prefix):]


def sync_clock():
    """Set UTC once per boot, before verified TLS; no certificate bypass."""
    global _clock_ready
    if _clock_ready:
        return
    import socket
    import struct
    import machine
    sock = None
    try:
        address = socket.getaddrinfo("time.google.com", 123, socket.AF_INET, socket.SOCK_DGRAM)[0][-1]
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3)
        request = bytearray(48)
        request[0] = 0x23
        nonce = os.urandom(8)
        request[40:48] = nonce
        sock.sendto(request, address)
        response, sender = sock.recvfrom(48)
        if (sender != address or len(response) != 48 or response[0] & 7 != 4
                or response[0] >> 6 == 3 or not 1 <= response[1] <= 15
                or response[24:32] != nonce):
            raise ValueError("Invalid clock response")
        seconds = struct.unpack("!I", response[40:44])[0]
        epoch = 2208988800 if time.gmtime(0)[0] == 1970 else 3155673600
        value = time.gmtime(seconds - epoch)
        if not 2025 <= value[0] <= 2099:
            raise ValueError("Invalid clock date")
        machine.RTC().datetime((value[0], value[1], value[2], value[6],
                                value[3], value[4], value[5], 0))
        _clock_ready = True
    finally:
        if sock is not None:
            sock.close()


def connect_tls(host, ca_file):
    import gc
    import socket
    import ssl
    raw = wrapped = None
    stage = "dns"
    try:
        gc.collect()
        address = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][-1]
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw.settimeout(5)
        stage = "tcp"
        raw.connect(address)
        stage = "context"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        stage = "ca"
        context.load_verify_locations(cafile=ca_file)
        stage = "handshake"
        wrapped = context.wrap_socket(raw, server_hostname=host)
        # RP2's TLS stream shares its underlying socket's blocking mode.
        if hasattr(wrapped, "setblocking"):
            wrapped.setblocking(False)
        else:
            raw.setblocking(False)
        return wrapped
    except Exception as error:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else 0
        diagnostic = "%s/%s/%d" % (stage, type(error).__name__, code)
        for item in (wrapped, raw):
            if item is not None:
                try:
                    item.close()
                except Exception:
                    pass
        # Only fixed operation/type/numeric code; never endpoint or raw text.
        raise RuntimeError("Verified HTTPS connection failed: " + diagnostic)


def _would_block(error):
    # mbedTLS WANT_READ/WANT_WRITE, POSIX EAGAIN, and CPython SSL test support.
    code = error.args[0] if error.args else None
    return code in (11, 35, 115, -26880, -26752) or type(error).__name__ in (
        "SSLWantReadError", "SSLWantWriteError", "BlockingIOError")


class Response:
    """Bounded HTTP header/body parser, including chunked acknowledgements."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.buffer = bytearray()
        self.body = bytearray()
        self.headers = None
        self.status = None
        self.length = None
        self.chunked = False
        self.chunk_size = None
        self.done = False

    def clear(self):
        # Leave no allocated response/header objects in a released TLS region.
        self.buffer = self.body = self.headers = None
        self.status = self.length = self.chunk_size = None
        self.chunked = self.done = False

    def feed(self, data, eof=False):
        if self.done:
            return
        self.buffer.extend(data)
        if self.headers is None:
            position = self.buffer.find(b"\r\n\r\n")
            if position < 0:
                if len(self.buffer) > MAX_HEADER_BYTES or eof:
                    raise ValueError("Invalid upload response headers")
                return
            if position > MAX_HEADER_BYTES:
                raise ValueError("Upload response headers too large")
            lines = bytes(self.buffer[:position]).decode("ascii").split("\r\n")
            # RP2 MicroPython supports slicing bytearray but not item/slice
            # deletion. Copy only the bounded unconsumed tail.
            self.buffer = self.buffer[position + 4:]
            status = lines.pop(0).split(" ")
            if len(status) < 2 or status[0] not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("Invalid upload response")
            self.status = int(status[1])
            self.headers = {}
            for line in lines:
                if ":" not in line:
                    raise ValueError("Invalid upload response header")
                key, value = line.split(":", 1)
                key, value = key.lower(), value.strip()
                if key in ("content-length", "transfer-encoding", "location", "content-type"):
                    if key in self.headers:
                        raise ValueError("Duplicate upload response header")
                    self.headers[key] = value
            if self.status in (302, 303):
                return  # The caller follows the acknowledgement URL with GET.
            if self.status != 200:
                raise ValueError("Upload server rejected the request")
            if self.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                raise ValueError("Upload did not return a JSON acknowledgement")
            transfer = self.headers.get("transfer-encoding", "").lower()
            if transfer:
                if transfer != "chunked" or "content-length" in self.headers:
                    raise ValueError("Unsupported upload response encoding")
                self.chunked = True
            elif "content-length" in self.headers:
                self.length = int(self.headers["content-length"])
                if not 0 <= self.length <= MAX_RESPONSE_BYTES:
                    raise ValueError("Upload acknowledgement too large")
        if self.chunked:
            self._chunks()
        else:
            self.body.extend(self.buffer)
            self.buffer = bytearray()
            if len(self.body) > MAX_RESPONSE_BYTES or (self.length is not None and len(self.body) > self.length):
                raise ValueError("Upload acknowledgement too large")
            self.done = self.length is not None and len(self.body) == self.length
            if eof:
                if self.length is not None and len(self.body) != self.length:
                    raise ValueError("Upload acknowledgement was truncated")
                self.done = True
        if eof and not self.done:
            raise ValueError("Upload acknowledgement was truncated")

    def _chunks(self):
        while not self.done:
            if self.chunk_size is None:
                end = self.buffer.find(b"\r\n")
                if end < 0:
                    if len(self.buffer) > 80:
                        raise ValueError("Invalid response chunk")
                    return
                self.chunk_size = int(bytes(self.buffer[:end]).split(b";", 1)[0], 16)
                self.buffer = self.buffer[end + 2:]
                if self.chunk_size < 0 or self.chunk_size + len(self.body) > MAX_RESPONSE_BYTES:
                    raise ValueError("Upload acknowledgement too large")
            if self.chunk_size == 0:
                if len(self.buffer) < 2:
                    return
                # No trailer fields are required or trusted for acknowledgements.
                if self.buffer[:2] != b"\r\n":
                    raise ValueError("Unsupported response trailers")
                self.done = True
                return
            if len(self.buffer) < self.chunk_size + 2:
                return
            self.body.extend(self.buffer[:self.chunk_size])
            if self.buffer[self.chunk_size:self.chunk_size + 2] != b"\r\n":
                raise ValueError("Invalid response chunk")
            self.buffer = self.buffer[self.chunk_size + 2:]
            self.chunk_size = None


class UploadTransport:
    def __init__(self, ca_file="google_roots.pem", connector=connect_tls,
                 clock_sync=sync_clock, clock=ticks_ms, source_factory=None,
                 memory=None, restore_after_transfer=True):
        self.ca_file, self.connector = ca_file, connector
        self.clock_sync, self.clock = clock_sync, clock
        self.source_factory = source_factory
        self._real_connector = connector is connect_tls
        if memory is None and self._real_connector:
            try:
                from . import tls_memory as memory
            except ImportError:
                import tls_memory as memory
        self._memory = memory
        self._memory_released = False
        self._restore_after_transfer = restore_after_transfer
        # This buffer survives socket cleanup, so allocate it while the boot
        # reserve is still held. Decode JSON only after restoring that reserve.
        self._ack_buffer = bytearray(MAX_RESPONSE_BYTES)
        self._ack_length = 0
        self._parser = Response()
        self.sock = self.stream = None
        self.state = "idle"
        # Retain compact progress through redirects and socket cleanup. These
        # fields are allocated before TLS work; headers never count as data.
        self.upload_stage = 1  # connect=1, send=2, confirm=3
        self.payload_sent = self.payload_total = 0
        self._out_is_body = False

    def maintain_memory(self):
        """Restore the reserve on an idle call, after caller ACK/error frames exit.

        close() deliberately does not allocate: its caller may still retain the
        response or exception that owns TLS allocations. A failed restoration
        leaves uploads unavailable but does not make outputs unsafe.
        """
        if self.state != "idle":
            return False
        return self._reserve_memory()

    def _reserve_memory(self):
        if self._memory is None:
            return True
        if not self._memory.reserve():
            return False
        self._memory_released = False
        return True

    def start(self, url, path, scan_id, sha256, point_count):
        self.close()
        self.upload_stage = 1
        self.payload_sent = self.payload_total = 0
        self._out_is_body = False
        if not self.maintain_memory():
            raise RuntimeError("Upload memory unavailable; scan remains in RAM")
        if self._real_connector:
            # Import retained modules while the reserve still occupies its
            # contiguous region. No sockets or network operations occur here.
            import socket
            import ssl
        self.host, self.path = endpoint_parts(url)
        if self.source_factory is None:
            raise ValueError("An upload source factory is required")
        self.stream = self.source_factory(path)
        self.size = self.stream.size
        if not 0 < self.size <= MAX_FILE_BYTES:
            self.close()
            raise ValueError("Scan is outside the upload size limit")
        self.payload_total = self.size
        self.file_path = path
        self.method = "POST"
        self.redirects = 0
        self.started = self.clock()
        self.sent = 0
        self._prepare_request()
        self.state = "clock"  # No network until a later step, after UI paints.

    def _prepare_request(self):
        self._out_is_body = False
        headers = "%s %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nAccept: application/json\r\nAccept-Encoding: identity\r\n" % (
            self.method, self.path, self.host)
        if self.method == "POST":
            headers += "Content-Type: application/json\r\nContent-Length: %d\r\n" % self.size
        self.out = (headers + "\r\n").encode()
        self._parser.reset()
        self.response = self._parser

    def _close_connection(self):
        failed = False
        for attribute in ("stream", "sock"):
            item = getattr(self, attribute)
            if item is not None:
                try:
                    item.close()
                    setattr(self, attribute, None)
                except Exception:
                    failed = True
        if failed:
            raise RuntimeError("Upload connection could not be closed")

    def close(self):
        self._close_connection()
        self.state = "idle"
        self.out = b""
        self._parser.clear()
        self.response = None
        self._ack_length = 0
        # Redirect locations and body buffers can have been allocated inside
        # the released region. Drop those references before a later restore.
        self.host = self.path = self.file_path = None

    def step(self):
        try:
            return self._step()
        except Exception:
            self.close()
            raise RuntimeError("Cloud upload failed; scan remains saved")

    def _step(self):
        if self.state == "idle":
            return None
        if ticks_diff(self.clock(), self.started) > TRANSFER_TIMEOUT_MS:
            raise RuntimeError("Upload timed out")
        if self.state == "restore":
            # The previous read/parser/socket frames have returned. Reserve
            # performs two collections so socket-finalizer context references
            # can also be reclaimed before JSON/archive allocations occur.
            if self._restore_after_transfer and not self._reserve_memory():
                raise RuntimeError("Upload memory unavailable; scan remains in RAM")
            if not self._restore_after_transfer:
                # A one-scan boot resets after success or explicit discard.
                # No second handshake needs a contiguous reserve in this VM.
                import gc
                gc.collect()
            result = json.loads(bytes(self._ack_buffer[:self._ack_length]).decode("utf-8"))
            if not isinstance(result, dict):
                raise ValueError("Invalid upload acknowledgement")
            self.close()
            return result
        if self.state == "clock":
            self.clock_sync()
            self.state = "connect"
            return None
        if self.state == "connect":
            if self._memory is not None and not self._memory_released:
                if not self._memory.release():
                    raise RuntimeError("Upload memory unavailable; scan remains in RAM")
                self._memory_released = True
            self.sock = self.connector(self.host, self.ca_file)
            if self.method == "POST":
                self.upload_stage = 2
            self.state = "write"
            return None
        if self.state == "write":
            if self.out:
                try:
                    count = self.sock.write(self.out)
                except OSError as error:
                    if _would_block(error):
                        return None
                    raise
                if count is None:
                    return None
                if count <= 0 or count > len(self.out):
                    raise RuntimeError("Upload write failed")
                if self._out_is_body:
                    self.payload_sent += count
                self.out = self.out[count:]
                return None
            if self.stream is not None:
                self.out = self.stream.read(BLOCK_BYTES)
                self._out_is_body = True
                self.sent += len(self.out)
                if self.out:
                    if self.sent > self.size:
                        raise RuntimeError("Saved scan size changed")
                    return None
                self.stream.close()
                self.stream = None
                if self.sent != self.size:
                    raise RuntimeError("Saved scan was truncated")
                if self.payload_sent != self.payload_total:
                    raise RuntimeError("Upload payload was not fully sent")
                self.upload_stage = 3
            self.state = "read"
            return None
        try:
            data = self.sock.read(BLOCK_BYTES)
        except OSError as error:
            if _would_block(error):
                return None
            raise
        if data is None:
            return None
        self.response.feed(data, eof=data == b"")
        if self.response.headers is not None and self.response.status in (302, 303):
            host, path = _redirect_parts(self.response.headers.get("location"))
            self.redirects += 1
            if self.redirects > 2:
                raise RuntimeError("Too many upload redirects")
            self._close_connection()
            self.host, self.path, self.method = host, path, "GET"
            self._prepare_request()
            self.state = "connect"
            return None
        if self.response.done:
            length = len(self.response.body)
            self._ack_buffer[:length] = self.response.body
            self.close()
            self._ack_length = length
            self.state = "restore"
            return None
        return None
