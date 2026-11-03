"""Atomic settings JSON and integrity-checked binary calibration storage."""

import json
import os

try:
    from .model import DEFAULT_SETTINGS, validate_settings
except ImportError:
    from model import DEFAULT_SETTINGS, validate_settings


def load_json(path, default=None):
    try:
        with open(path, "r") as stream:
            return json.load(stream)
    except OSError as error:
        code = getattr(error, "errno", None)
        if code is None and error.args:
            code = error.args[0]
        if code == 2:  # ENOENT: absent file, not a filesystem/power fault.
            return default
        raise


def load_calibration(default=None, path="calibration.bin"):
    """Binary only: a missing file returns default; any corrupt file fails.

    Old calibration JSON is deliberately ignored, including when the binary
    file is missing. Conversion is an explicit one-time maintenance action.
    """
    try:
        stream = open(path, "rb")
    except OSError as error:
        code = getattr(error, "errno", None)
        if code is None and error.args:
            code = error.args[0]
        if code == 2:
            return default
        raise
    with stream:
        try:
            from .calibration_binary import load_calibration as load_binary
        except ImportError:
            from calibration_binary import load_calibration as load_binary
        return load_binary(stream)


class _JsonTransaction:
    """Hold the actual file buffer before an acquisition grows its heap.

    One transaction at a time may own a path's fixed .tmp staging file.
    The committed path is untouched until a successful complete write/close.
    """
    def __init__(self, path, mode="w"):
        self.path = path
        self.temporary = path + ".tmp"
        self._stream = None
        self._active = True
        # Opening with w claims the staging path, including a stale .tmp left
        # by a prior reset. open() can create it before a buffer allocation
        # fails, so construction failure must clean that path too.
        self._owns_temporary = True
        try:
            self._stream = open(self.temporary, mode)
        except Exception:
            self.abort()
            raise

    def abort(self):
        """Idempotent, best-effort close/removal of our staging file only."""
        self._active = False
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        if self._owns_temporary:
            try:
                os.remove(self.temporary)
                self._owns_temporary = False
            except OSError as error:
                code = getattr(error, "errno", None)
                if code is None and error.args:
                    code = error.args[0]
                if code == 2:
                    self._owns_temporary = False
            except Exception:
                pass

    def _serialize(self, data):
        json.dump(data, self._stream)

    def commit(self, data):
        """Stream/close before rename; attempt close/cleanup on every failure.

        As with save_json_atomic, a sync failure after successful rename can
        report an error although the new file is already committed.
        """
        if not self._active or self._stream is None:
            raise ValueError("File transaction is no longer active")
        self._active = False
        try:
            self._serialize(data)
            self._stream.flush()
            self._stream.close()
            self._stream = None
            if hasattr(os, "sync"):
                os.sync()
            replace = getattr(os, "replace", os.rename)
            replace(self.temporary, self.path)
            self._owns_temporary = False
            if hasattr(os, "sync"):
                os.sync()
        except Exception:
            self.abort()
            raise


def begin_json_atomic(path):
    """Preopen a temporary JSON file; caller must commit(data) or abort()."""
    return _JsonTransaction(path)


def save_json_atomic(path, data):
    begin_json_atomic(path).commit(data)


class _CalibrationTransaction(_JsonTransaction):
    def __init__(self, path):
        # First calibration may have no existing binary to load at boot.
        # Import the codec before acquisition fragments memory, never at commit.
        try:
            from .calibration_binary import dump_calibration
        except ImportError:
            from calibration_binary import dump_calibration
        self._dump_calibration = dump_calibration
        super().__init__(path, "wb")

    def _serialize(self, data):
        self._dump_calibration(data, self._stream)


def begin_calibration_atomic(path="calibration.bin"):
    """Preopen a binary staging file; caller must commit(value) or abort()."""
    return _CalibrationTransaction(path)


def save_calibration_atomic(value, path="calibration.bin"):
    begin_calibration_atomic(path).commit(value)


def load_settings(path="settings.json", migrate=True):
    saved = load_json(path, dict(DEFAULT_SETTINGS))
    normalized = validate_settings(saved)
    if migrate and saved.get("version", 1) != DEFAULT_SETTINGS["version"]:
        # Commit only a fully validated migration. Failed writes leave the
        # original settings available for a later retry.
        save_json_atomic(path, normalized)
    return normalized


def save_settings(settings, path="settings.json"):
    normalized = validate_settings(settings)
    save_json_atomic(path, normalized)
    return normalized
