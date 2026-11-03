"""Fixed RAM-only scan records: measured VDS, VGS, ID and IG.

Production supplies the boot-reserved bytearray. Settings retained alongside
the scan define its requested grid; this buffer contains measured values only.
No filesystem operations or data-dependent storage growth occur here.
"""

import math
import struct

try:
    from . import model
except ImportError:
    import model


RECORD_FORMAT = "<4f"
RECORD_BYTES = 16
BUFFER_BYTES = model.MAX_POINTS * RECORD_BYTES
_FLOAT32_MAX = 3.4028234663852886e38


def _float32_value(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Scan values must be finite numbers")
    try:
        value = float(value)
    except OverflowError:
        raise ValueError("Scan value exceeds float32 range")
    if not math.isfinite(value):
        raise ValueError("Scan values must be finite numbers")
    if abs(value) > _FLOAT32_MAX:
        raise ValueError("Scan value exceeds float32 range")
    return value


class PackedScanBuffer:
    """Reusable 16-byte records without retaining input dictionaries.

    get()/get_tuple()/indexing return (vds, vgs, id_a, ig_a).
    clear() changes only the visible count; a reboot expunges the backing RAM.
    """
    def __init__(self, buffer=None, capacity=model.MAX_POINTS):
        if (isinstance(capacity, bool) or not isinstance(capacity, int) or
                not 1 <= capacity <= model.MAX_POINTS):
            raise ValueError("Scan capacity must be between 1 and %d" % model.MAX_POINTS)
        size = capacity * RECORD_BYTES
        if buffer is None:
            buffer = bytearray(size)
        elif not isinstance(buffer, bytearray) or len(buffer) != size:
            raise ValueError("RAM scan buffer size does not match capacity")
        self._data = buffer
        self.capacity = capacity
        self.count = 0
        self._frozen = False
        self._revision = 0

    @property
    def frozen(self):
        return self._frozen

    @property
    def revision(self):
        return self._revision

    def freeze(self):
        self._frozen = True

    def unfreeze(self):
        self._frozen = False
        self._revision += 1

    def __len__(self):
        return self.count

    def clear(self):
        if self._frozen:
            raise ValueError("Completed scan buffer is frozen")
        self.count = 0
        self._revision += 1

    def append(self, row):
        if self._frozen:
            raise ValueError("Completed scan buffer is frozen")
        if self.count >= self.capacity:
            raise ValueError("RAM scan buffer is full")
        if not isinstance(row, dict):
            raise ValueError("Scan point must be an object")
        try:
            vds = _float32_value(row["vds"])
            vgs = _float32_value(row["vgs"])
            id_a = _float32_value(row["id_a"])
            ig_a = _float32_value(row["ig_a"])
        except KeyError:
            raise ValueError("Scan point is missing a measured value")
        # Validate before touching storage; count publishes a complete record.
        struct.pack_into(RECORD_FORMAT, self._data, self.count * RECORD_BYTES,
                         vds, vgs, id_a, ig_a)
        self.count += 1
        self._revision += 1

    def get(self, index):
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("Scan index must be an integer")
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError("Scan index outside stored results")
        return struct.unpack_from(RECORD_FORMAT, self._data, index * RECORD_BYTES)

    get_tuple = get
    __getitem__ = get
