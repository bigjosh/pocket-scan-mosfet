"""Bounded, best-effort fault logs; no scan data or boot-time file clearing.

The log is streamed through small chunks because MicroPython's free heap
may be fragmented after an acquisition. A failed write preserves the prior
committed log and must never hide the original error.
"""

import io
import os
import sys

try:
    from time import ticks_ms as _ticks_ms
except ImportError:
    _ticks_ms = None

LOG_PATH = "last_error.log"
MAX_LOG_BYTES = 4096
_TRUNCATED = b"\n[log truncated]\n"
_CHUNK_SIZE = 128


def _short_text(value, limit):
    try:
        text = value if isinstance(value, str) else str(value)
        return text[:limit]
    except Exception:
        return "<message unavailable>"


class _BoundedWriter(io.IOBase):
    """IOBase makes this a stream for MicroPython's sys.print_exception."""
    def __init__(self, stream, limit):
        self.stream = stream
        self.remaining = max(0, limit - len(_TRUNCATED))
        self.truncated = False

    def _raw(self, data):
        count = min(self.remaining, len(data))
        if count:
            self.stream.write(data[:count])
            self.remaining -= count
        if count != len(data):
            self.truncated = True

    def write(self, data):
        count = len(data)
        # A native MicroPython printer supplies bytes; CPython supplies str.
        # Encode only a small slice at a time, never an entire traceback.
        for offset in range(0, count, _CHUNK_SIZE):
            part = data[offset:offset + _CHUNK_SIZE]
            if isinstance(part, str):
                part = part.encode("utf-8")
            self._raw(part)
            if self.remaining == 0 and offset + _CHUNK_SIZE < count:
                self.truncated = True
                break
        # Printers must see a successful full write even after truncation.
        return count

    def finish(self):
        if self.truncated:
            self.stream.write(_TRUNCATED)


def record_error(context, error):
    """Print a short USB message and replace last_error.log; return success.

    File/USB failures are swallowed. The previous committed file remains
    intact when writing or replacing the new log fails. Call only on errors,
    after disabling drive outputs; this function does not control hardware.
    """
    temporary = None
    try:
        label = _short_text(context, 96)
        message = _short_text(error, 192)
        try:
            print("Scanner error [%s]: %s" % (label, message))
        except Exception:
            pass
        # Path construction also allocates. Keep it inside the best-effort
        # boundary so a second MemoryError cannot mask the original fault.
        temporary = LOG_PATH + ".tmp"
        with open(temporary, "wb") as stream:
            writer = _BoundedWriter(stream, MAX_LOG_BYTES)
            writer.write("Context: ")
            writer.write(label)
            writer.write("\nUptime ms: ")
            writer.write(str(_ticks_ms()) if _ticks_ms is not None else "unavailable")
            writer.write("\n")
            if isinstance(error, BaseException):
                if hasattr(sys, "print_exception"):
                    sys.print_exception(error, writer)
                else:
                    # Never import the host traceback module on MicroPython.
                    import traceback
                    traceback.print_exception(type(error), error, error.__traceback__, file=writer)
            else:
                writer.write("Message: ")
                writer.write(message)
                writer.write("\n")
            writer.finish()
            stream.flush()
        if hasattr(os, "sync"):
            os.sync()
        replace = getattr(os, "replace", os.rename)
        replace(temporary, LOG_PATH)
        if hasattr(os, "sync"):
            os.sync()
        return True
    except Exception:
        try:
            if temporary is not None:
                os.remove(temporary)
        except Exception:
            pass
        return False
