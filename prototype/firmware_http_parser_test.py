"""Native bounded HTTP parser checks, including MicroPython bytearray behavior."""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import json
from https_upload import Response

for chunked in (False, True):
    body = b'{"ok":true,"point_count":2}'
    head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
    if chunked:
        raw = (head + b"Transfer-Encoding: chunked\r\n\r\n" +
               ("%x\r\n" % len(body)).encode() + body + b"\r\n0\r\n\r\n")
    else:
        raw = head + ("Content-Length: %d\r\n\r\n" % len(body)).encode() + body
    parser = Response()
    for offset in range(0, len(raw), 7):
        parser.feed(raw[offset:offset + 7])
    assert parser.done and json.loads(bytes(parser.body)) == {"ok": True, "point_count": 2}
    parser.clear()
    assert parser.buffer is None and parser.body is None and parser.headers is None

parser = Response()
parser.feed(b"HTTP/1.1 302 Found\r\nLocation: https://script.googleusercontent.com/test\r\n\r\n")
assert parser.status == 302
rejected = False
try:
    Response().feed(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\nno")
except ValueError:
    rejected = True
assert rejected
print("HTTP_PARSER_NATIVE", json.dumps({"fragmented_content_length": True,
      "fragmented_chunked": True, "redirect_headers": True,
      "html_rejected": True, "response_references_cleared": True,
      "no_network_or_output_drive": True}))
