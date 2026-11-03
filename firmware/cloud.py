"""Explicit local scan acceptance, bounded upload retry, and safe public status."""

try:
    from time import ticks_diff as _ticks_diff
except ImportError:
    def _ticks_diff(now, before):
        return now - before

try:
    from . import storage
    from .archive import Archive, ArchiveCapacityError
except ImportError:
    import storage
    from archive import Archive, ArchiveCapacityError

BACKOFF_MS = (60000, 300000, 1800000)
MAX_FAILURES = 4
URL_PREFIX = "https://script.google.com/macros/s/"
MEMORY_UNAVAILABLE = "Upload memory unavailable; saved scans remain queued"


def validate_url(value):
    if not isinstance(value, str):
        raise ValueError("Enter a published Google Apps Script URL")
    value = value.strip()
    if not value or len(value) > 256 or not value.startswith(URL_PREFIX) or not value.endswith("/exec"):
        raise ValueError("Use the published Google Apps Script URL ending in /exec")
    deployment = value[len(URL_PREFIX):-5]
    if not deployment or not all(character in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                                 for character in deployment):
        raise ValueError("Use a published Google Apps Script URL without query parameters")
    return value


class CloudManager:
    def __init__(self, archive=None, transport=None, store=storage, path="cloud.json"):
        self.archive = archive if archive is not None else Archive()
        self._transport = transport
        self._store, self._path = store, path
        self._url, self._enabled = "", False
        self._active = None
        self._manual = False
        self._failures = 0
        self._retry_started_ms = None
        self._retry_delay_ms = 0
        self._memory_check_due = False
        self._memory_unavailable = False
        self._pending_count, self._saved_count = 0, 0
        self.state, self._message = "unconfigured", "Cloud upload is not configured"
        try:
            self._refresh_counts()
            value = store.load_json(path)
            if value is not None:
                if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("enabled"), bool):
                    raise ValueError()
                self._url = validate_url(value["url"]) if value.get("url") else ""
                self._enabled = value["enabled"]
                if self._enabled and not self._url:
                    raise ValueError()
                self._idle_status()
        except Exception:
            self._url, self._enabled = "", False
            self.state, self._message = "error", "Cloud settings could not be loaded"

    @property
    def status(self):
        return {"state": self.state, "message": self._message,
                "pending_count": self._pending_count, "saved_count": self._saved_count,
                "configured": bool(self._url), "enabled": self._enabled}

    def _refresh_counts(self):
        # Status is rendered/polled frequently. Read the directory only when
        # archive state changes, never once per 2 ms application tick.
        self._pending_count = self.archive.pending_count
        self._saved_count = self.archive.saved_count

    def config_for_edit(self):
        return {"url": self._url, "enabled": self._enabled}

    def scan_capacity(self, settings):
        """Read the same capacity policy used before acquisition starts."""
        return self.archive.scan_capacity(settings)

    def prepare_calibration(self, calibration):
        return self.archive.prepare_calibration(calibration)

    def invalidate_calibration(self):
        self.archive.invalidate_calibration()

    def list_scans(self):
        """Stop upload before exposing accepted scans for explicit deletion."""
        self.pause()
        result = self.archive.list_scans()
        self._refresh_counts()
        self._idle_status()
        return result

    def delete_scan(self, scan_id):
        """Delete one UI-confirmed local scan; never a currently uploading one."""
        active_id = self._active["scan_id"] if self._active is not None else None
        self.pause()
        if scan_id == active_id:
            raise RuntimeError("Review the stopped upload before deleting it")
        try:
            result = self.archive.delete_scan(scan_id)
        except Exception:
            try:
                self._refresh_counts()
                self._idle_status()
            except Exception:
                pass
            raise
        self._refresh_counts()
        self._idle_status()
        return result

    def _idle_status(self):
        if not self._url:
            self.state, self._message = "unconfigured", "Scans stay local until cloud is configured"
        elif self._memory_unavailable:
            self.state, self._message = "retry", MEMORY_UNAVAILABLE
        elif not self._enabled and not self._manual:
            self.state, self._message = "disabled", "Automatic upload is off"
        elif self._pending_count:
            self.state, self._message = "queued", "Saved scans are waiting to upload"
        else:
            self.state, self._message = "idle", "No scans waiting to upload"

    def save_config(self, url, enabled=True):
        if not isinstance(enabled, bool):
            raise ValueError("Invalid cloud upload setting")
        normalized = validate_url(url) if url or enabled else ""
        self.pause()
        try:
            self._store.save_json_atomic(self._path, {"version": 1, "url": normalized, "enabled": enabled})
        except Exception:
            raise RuntimeError("Cloud settings could not be saved")
        self._url, self._enabled = normalized, enabled
        self._manual, self._failures, self._retry_started_ms = False, 0, None
        self._idle_status()
        return True

    def begin_scan(self, settings, calibration, now_ms):
        self.pause()
        try:
            result = self.archive.begin_scan(settings, calibration, now_ms)
        except ArchiveCapacityError:
            self._refresh_counts()
            self._idle_status()
            return False
        self._refresh_counts()
        return result

    def record_point(self, row):
        return self.archive.record_point(row)

    def finish_scan(self, now_ms, fault=None):
        return self.archive.finish_scan(now_ms, fault)

    def accept_scan(self):
        try:
            scan_id = self.archive.accept_scan()
        except Exception:
            # The atomic rename may have succeeded before a later sync failed.
            # Preserve that original error, but do not hide the committed queue
            # entry from the UI/automatic uploader until the next reboot.
            try:
                self._refresh_counts()
                self._idle_status()
            except Exception:
                pass
            raise
        self._refresh_counts()
        self._idle_status()
        return scan_id

    def discard_scan(self):
        return self.archive.discard_scan()

    def _close(self):
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                self.state, self._message = "error", "Upload connection could not be closed"
                raise RuntimeError("Upload connection could not be closed")
        self._active = None
        self._memory_check_due = True

    def _maintain_memory(self):
        """Optional transport housekeeping, only after close() has returned."""
        self._memory_check_due = False
        try:
            maintain = getattr(self._transport, "maintain_memory", None)
            self._memory_unavailable = maintain is not None and not maintain()
        except Exception:
            self._memory_unavailable = True
        return not self._memory_unavailable

    def _memory_failed(self, now_ms):
        self._failed(now_ms, MEMORY_UNAVAILABLE)
        # _failed closes the transport, but repeating restoration on the next
        # 2 ms tick would defeat its backoff. Retry at the ordinary deadline.
        self._memory_check_due = False
        self._memory_unavailable = True

    def maintain_memory(self, now_ms):
        """Deferred idle housekeeping; call before rendering on a safe tick.

        Active uploads retain their released region across redirects. Failed
        restoration uses the upload backoff instead of allocating every tick.
        The caller must not invoke this during analog acquisition.
        """
        if self._active is not None:
            return True
        retry_due = (self._retry_started_ms is None or
                     _ticks_diff(now_ms, self._retry_started_ms) >= self._retry_delay_ms)
        if self._memory_check_due or (self._memory_unavailable and retry_due and
                                     self._failures < MAX_FAILURES):
            if not self._maintain_memory():
                self._memory_failed(now_ms)
                return False
        return not self._memory_unavailable

    def pause(self):
        self._close()
        # User cancellation/start-scan calls arrive on a new application tick,
        # outside the transport's ACK/error stack. Failure here blocks cloud
        # uploads only; the successfully closed connection permits acquisition.
        self._maintain_memory()
        self._idle_status()
        return True

    def request_upload(self):
        if not self._url:
            self.state, self._message = "unconfigured", "Configure cloud upload first"
            return False
        try:
            self._refresh_counts()
        except Exception:
            self.state, self._message = "error", "Saved scan queue could not be read"
            return False
        if not self._pending_count:
            self._idle_status()
            return False
        self._manual, self._failures, self._retry_started_ms = True, 0, None
        if self._active is None:
            self.state, self._message = "queued", "Upload requested"
        return True

    def _failed(self, now_ms, message="Upload failed; saved scans remain queued"):
        try:
            self._close()
        except RuntimeError:
            return
        self._failures += 1
        self._retry_started_ms = now_ms
        if self._failures >= MAX_FAILURES:
            self._manual = False
            self.state, self._message = "error", "Upload paused after repeated failures; choose Upload now to retry"
        else:
            self._retry_delay_ms = BACKOFF_MS[self._failures - 1]
            self.state, self._message = "retry", message

    def tick(self, now_ms, online=False, safe=False):
        if not safe:
            if self._active is not None:
                self._close()
                self._idle_status()
            return
        if not online and self._active is not None:
            self.pause()
            return
        if not self.maintain_memory(now_ms):
            return
        if not online:
            return
        if not self._url or not (self._enabled or self._manual) or self._failures >= MAX_FAILURES:
            return
        if self._retry_started_ms is not None and _ticks_diff(now_ms, self._retry_started_ms) < self._retry_delay_ms:
            return
        if self._active is None:
            try:
                if not self._pending_count:
                    self._manual = False
                    self._idle_status()
                    return
                entry = self.archive.next_pending()
                if entry is None:
                    self._refresh_counts()
                    self._manual = False
                    self._idle_status()
                    return
                if self._transport is None:
                    try:
                        from .https_upload import UploadTransport
                    except ImportError:
                        from https_upload import UploadTransport
                    self._transport = UploadTransport()
                if not self._maintain_memory():
                    self._memory_failed(now_ms)
                    return
                self._active = entry
                self._transport.start(self._url, entry["path"], entry["scan_id"],
                                      entry["sha256"], entry["point_count"])
                self.state, self._message = "uploading", "Uploading saved scan"
                return  # Paint status before the first transport/network step.
            except Exception:
                self._failed(now_ms)
                return
        try:
            acknowledgement = self._transport.step()
            if acknowledgement is None:
                return
            entry = self._active
            if (not isinstance(acknowledgement, dict) or acknowledgement.get("ok") is not True
                    or acknowledgement.get("scan_id") != entry["scan_id"]
                    or acknowledgement.get("sha256") != entry["sha256"]
                    or isinstance(acknowledgement.get("point_count"), bool)
                    or not isinstance(acknowledgement.get("point_count"), int)
                    or acknowledgement.get("point_count") != entry["point_count"]):
                raise ValueError("Upload acknowledgement did not match")
            self._close()
            self.archive.mark_sent(entry["scan_id"])
            self._refresh_counts()
            self._failures, self._retry_started_ms = 0, None
            self._idle_status()
        except Exception:
            self._failed(now_ms)
