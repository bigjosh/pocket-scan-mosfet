"""Keep a contiguous heap region available for verified TLS on the Pico.

Call reserve() early at boot, before importing the application or allocating UI
buffers. Prepare retained upload objects while it is held, then release() just
before TLS work. After closing sockets and dropping all TLS references, call
reserve() again. A failed reservation returns False; it never silently reduces
the requested size or changes certificate verification.

The reserve limits fragmentation before a transfer. Its size is a measured
budget, not a guarantee that an arbitrary TLS handshake will fit. Avoid creating
unrelated retained objects while it is released. This module owns no sockets.
"""

import gc


DEFAULT_BYTES = 96 * 1024
_buffer = None
_size = DEFAULT_BYTES


def reserve(size=None):
    """Hold a contiguous region, returning False if it cannot be allocated.

    With no size, restore the last requested budget. Repeated calls while held
    are harmless. A size change while held is rejected rather than discarding a
    usable reserve. The caller must ensure no live TLS work remains on restore.
    """
    global _buffer, _size
    requested = _size if size is None else size
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValueError("TLS reserve size must be a positive integer")
    if _buffer is not None:
        if requested != _size:
            raise ValueError("Release TLS reserve before changing its size")
        return True
    _size = requested
    gc.collect()
    # A closed TLS socket may be finalized in this pass, releasing its context
    # only after that context was marked live. Collect again before allocating
    # the large reserve so context/CA children can be reclaimed too.
    gc.collect()
    try:
        _buffer = bytearray(_size)
    except MemoryError:
        return False
    return True


def release():
    """Release the held region and collect garbage; return whether it existed."""
    global _buffer
    held = _buffer is not None
    _buffer = None
    gc.collect()
    return held


def available():
    """Whether a full contiguous reserve is currently held for the next upload."""
    return _buffer is not None


def reserved_bytes():
    """Public diagnostic count; zero means no memory is currently held."""
    return _size if _buffer is not None else 0
