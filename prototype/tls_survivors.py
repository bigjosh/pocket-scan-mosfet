"""Temporary, value-free inspection of objects inside a released TLS reserve.

Import and capture(tls_memory) while the reserve is held. After a failed restore,
call report(app=app, cloud=cloud, wifi=wifi, transport=cloud._transport) from a
clean REPL frame. It prints only fixed owner paths and type names, never object
values, dictionary keys, string contents, addresses, or credential lengths.

MicroPython's non-verbose qstr totals are also printed before/after the transfer;
qstr strings are never printed. The return value is True if traversal completed within its bounds. False means
that a node/depth limit or allocation failure truncated it. This walker itself
allocates temporary traversal objects; let its frame return, then gc.collect()
before any later memory experiment. It does not retain the captured buffer.
"""

_start = 0
_end = 0

try:
    import micropython as _micropython
except ImportError:
    _micropython = None


def _qstr_snapshot(before):
    if _micropython is not None and hasattr(_micropython, "qstr_info"):
        print("TLS_QSTR_BEFORE" if before else "TLS_QSTR_AFTER")
        _micropython.qstr_info()  # Counts/bytes only, never string contents.

# These are code-owned attribute names, not user-controlled dictionary keys.
# Unknown names get numbered labels instead of being printed.
_ATTRIBUTES = (
    "d", "inputs", "engine", "store", "settings", "calibration", "error_logger",
    "wifi", "cloud", "screen", "hits", "dirty", "latest", "rows", "curves",
    "limit_flags", "notice", "notice_return", "draft", "edit_value", "edit_label",
    "_outputs_safe", "_archive_active", "_archive_ready", "_archive_error",
    "_cloud_error", "_cloud_tick_failed", "_cloud_transfer_hint", "_cloud_status_key",
    "_cloud_url_draft", "_cloud_enabled_draft", "_cloud_url_edit", "_cloud_show",
    "_cloud_keyboard", "_cloud_value_dirty", "_now_ms", "_wifi_paused",
    "_wifi_search_pending", "_wifi_rows", "_wifi_saved_list", "_wifi_page",
    "_wifi_selected", "_wifi_password", "_wifi_show", "_wifi_keyboard",
    "_wifi_value_dirty", "_wifi_status_key", "_wifi_error", "_wifi_connect_failed",
    "_shown_scan_header", "_plot_cursor", "_last_live_ms", "_plot_xmin",
    "_plot_xmax", "_plot_ymin", "_plot_ymax", "_solid", "_solid_view",
    "_glyph_data", "_glyph_fb", "_glyphs", "_raster_pool", "_raster_slots",
    "_raster_small_slots", "_raster_cache", "_raster_keys", "_text_buffer",
    "_text_views", "cs", "dc", "reset", "spi", "instrument", "hardware",
    "archive", "_transport", "_store", "_path", "_url", "_enabled", "_active",
    "_manual", "_message", "_memory_check_due", "_memory_unavailable",
    "_retry_started_ms", "_retry_delay_ms", "_profiles", "_pending", "_adapter",
    "_restore_ssid", "_desired_ssid", "_ssid", "_ip", "_last_poll_ms", "_sta",
    "_ap", "_want_connection", "_restoring", "_started_ms", "directory",
    "device_id", "_current", "_accepted_id", "_settings", "_hash", "_stream",
    "_free_bytes", "_random_bytes", "_utc_provider", "ca_file", "connector",
    "clock_sync", "clock", "source_factory", "_real_connector", "_memory",
    "_memory_released", "_ack_buffer", "_ack_length", "_parser", "sock",
    "stream", "state", "out", "response", "host", "path", "file_path",
    "method", "redirects", "started", "sent", "size", "buffer", "body",
    "headers", "status", "length", "chunked", "chunk_size", "done",
    "_data", "_buffer", "_view", "_format", "_capacity", "_count", "_piece",
    "_header", "_offset", "_index", "_stage", "_batches", "_duration",
)


def capture(memory):
    """Capture only the bytearray address range; retain no buffer reference."""
    global _start, _end
    import uctypes
    value = memory._buffer
    if value is None:
        _start = _end = 0
        return False
    _start = uctypes.addressof(value)
    _end = _start + len(value)
    _qstr_snapshot(True)
    return True


def report(app=None, cloud=None, wifi=None, transport=None, emit=print,
           max_nodes=4096, max_depth=10):
    """Emit owner paths/types for known graph objects in the captured region."""
    if not _start or _end <= _start:
        return False
    _qstr_snapshot(False)
    seen = set()
    complete = [True]

    def walk(value, path, depth):
        # Immediates have no allocated object body. Callable/native descriptor
        # wrappers may be manufactured by attribute access; never report them.
        if value is None or isinstance(value, (bool, int)):
            return
        name = type(value).__name__
        if name in ("method", "bound_method", "function", "builtin_function",
                    "builtin_function_or_method", "type", "property"):
            return
        address = id(value)
        if address in seen:
            return
        if len(seen) >= max_nodes:
            complete[0] = False
            return
        seen.add(address)
        if _start <= address < _end:
            # Type names come from firmware classes. Do not stringify value.
            emit(path + " : " + name)
        if isinstance(value, (str, bytes, bytearray, memoryview, float)):
            return
        if depth >= max_depth:
            complete[0] = False
            return
        if isinstance(value, dict):
            # Keys can contain credentials too. Neither keys nor values become
            # path labels; enumerate them using fixed numeric placeholders.
            for index, (key, item) in enumerate(value.items()):
                walk(key, path + "[key#%d]" % index, depth + 1)
                walk(item, path + "[value#%d]" % index, depth + 1)
            return
        if isinstance(value, (tuple, list, set, frozenset)):
            for index, item in enumerate(value):
                walk(item, path + "[item#%d]" % index, depth + 1)
            return
        if name == "module":
            return  # No traversal through global modules/functions/code.
        try:
            attributes = value.__dict__
        except (AttributeError, TypeError):
            return
        if not isinstance(attributes, dict):
            return
        # MicroPython may manufacture a __dict__ wrapper. Inspect its existing
        # values directly; never walk/report that wrapper or use getattr on its
        # keys (which could invoke a property getter or bind a method).
        for index, (key, item) in enumerate(attributes.items()):
            label = key if isinstance(key, str) and key in _ATTRIBUTES else "attribute#%d" % index
            walk(item, path + "." + label, depth + 1)

    try:
        for name, value in (("app", app), ("cloud", cloud), ("wifi", wifi),
                            ("transport", transport)):
            walk(value, name, 0)
    except MemoryError:
        return False
    return complete[0]
