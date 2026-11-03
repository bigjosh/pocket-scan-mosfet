"""One RAM scan per boot: bounded preparation, one upload, no archive writes."""
import os
import binascii

try:
    from . import storage
    from .https_upload import endpoint_parts
    from .ram_payload import RamScan
except ImportError:
    import storage
    from https_upload import endpoint_parts
    from ram_payload import RamScan


UPLOAD_ERROR = "WiFi Error, Upload Failed\nPlease reconnect WiFi in settings and redo scan."


class RamCloudManager:
    def __init__(self, buffer, transport=None, store=storage, path="cloud.json",
                 device_id=None, utc_provider=None, random_bytes=os.urandom):
        self.buffer, self._transport = buffer, transport
        self._store, self._path = store, path
        if device_id is None:
            import machine
            device_id = binascii.hexlify(machine.unique_id()).decode()
        self.device_id = device_id
        self._utc_provider, self._random_bytes = utc_provider, random_bytes
        self._url, self._enabled = "", False
        self.scan = None
        self._used = False
        self.state, self._message = "idle", ""
        # Fixed scalar progress survives transport close/error. Stage4 means
        # an exact server acknowledgement; sent bytes alone never reach it.
        self.upload_stage = 0  # prepare, connect, send, confirm, complete
        self.upload_sent = self.upload_total = 0
        try:
            value = store.load_json(path)
            if value is not None:
                if (not isinstance(value, dict) or value.get("version") != 1 or
                        not isinstance(value.get("enabled"), bool)):
                    raise ValueError()
                url = value.get("url", "")
                if url:
                    endpoint_parts(url)
                if value["enabled"] and not url:
                    raise ValueError()
                self._url, self._enabled = url, value["enabled"]
        except Exception:
            self._message = "Cloud settings could not be loaded"

    @property
    def status(self):
        # Counts remain zero for compatibility with shared Settings widgets;
        # there is no persistent queue or acknowledged-scan inventory here.
        return {"state": self.state, "message": self._message,
                "configured": bool(self._url), "enabled": self._enabled,
                "pending_count": 0, "saved_count": 0}

    def config_for_edit(self):
        return {"url": self._url, "enabled": self._enabled}

    def save_config(self, url, enabled=True):
        if self.scan is not None:
            raise RuntimeError("Finish this scan before changing cloud settings")
        if not isinstance(url, str) or not isinstance(enabled, bool):
            raise ValueError("Invalid cloud upload setting")
        url = url.strip()
        if url or enabled:
            endpoint_parts(url)
        # Only the explicit Settings path calls this persistence operation.
        try:
            self._store.save_json_atomic(self._path, {"version": 1, "url": url, "enabled": enabled})
        except Exception:
            raise RuntimeError("Cloud settings could not be saved")
        self._url, self._enabled = url, enabled
        return True

    def begin_scan(self, settings, now_ms, current_correction=None):
        if self._used or self.scan is not None or self.state not in ("idle",):
            raise RuntimeError("Restart before starting another scan")
        if not self._url or not self._enabled:
            raise ValueError("Set the cloud receiver in Settings first")
        if self._transport is None:
            raise ValueError("Cloud upload is unavailable")
        identifier = self.device_id + "-" + binascii.hexlify(self._random_bytes(8)).decode()
        utc = self._utc_provider() if self._utc_provider is not None else None
        self.scan = RamScan(self.buffer, settings, self.device_id, identifier, utc, now_ms,
                            current_correction=current_correction)
        self._used = True
        self.state, self._message = "recording", "Scanning"
        return identifier

    def record_point(self, row):
        if self.state != "recording" or self.scan is None:
            raise RuntimeError("No RAM scan is active")
        self.scan.append(row)

    def finish_scan(self, now_ms, fault=None):
        if self.state != "recording" or self.scan is None:
            raise RuntimeError("No RAM scan is active")
        self.scan.seal(now_ms, fault)
        self.upload_stage = 0
        self.upload_sent = self.upload_total = 0
        self.state, self._message = "preparing", "Preparing upload"

    def _capture_progress(self):
        # Synthetic transports may omit progress; the exact ACK still governs
        # success. Production transport supplies these preallocated integers.
        stage = getattr(self._transport, "upload_stage", self.upload_stage)
        if self.upload_stage <= stage <= 3:
            self.upload_stage = stage
        sent = getattr(self._transport, "payload_sent", self.upload_sent)
        if self.upload_sent <= sent <= self.upload_total:
            self.upload_sent = sent

    def pause(self):
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                raise RuntimeError("Upload connection could not be closed")
        return True

    def discard_scan(self):
        # The caller resets immediately after this. A reader closing itself
        # never clears the owner; only explicit discard drops the measurements.
        try:
            self.pause()
        finally:
            self.scan = None
            self.buffer.unfreeze()
            self.buffer.clear()
            self.state, self._message = "idle", ""
        return True

    def fail_upload(self):
        try:
            self.pause()
        except Exception:
            pass
        self.state, self._message = "error", UPLOAD_ERROR

    def _ack_matches(self, result):
        scan = self.scan
        if scan is None or not isinstance(result, dict):
            return False
        count, sheet_id = result.get("point_count"), result.get("sheet_id")
        return (result.get("ok") is True and result.get("scan_id") == scan.scan_id and
                result.get("sha256") == scan.sha256 and
                isinstance(count, int) and not isinstance(count, bool) and count == scan.point_count and
                isinstance(sheet_id, int) and not isinstance(sheet_id, bool) and sheet_id >= 0)

    def tick(self, now_ms, online=False):
        # No timer retries, queue walks, metadata writes or receipt files.
        try:
            if self.state == "preparing":
                if self.scan.prepare_step(256):
                    self.upload_stage = 1
                    self.upload_total = self.scan.wire_size
                    self.state, self._message = "waiting_wifi", "Connecting WiFi"
                return
            if self.state == "waiting_wifi":
                if online:
                    self._transport.start(self._url, self.scan, self.scan.scan_id,
                                          self.scan.sha256, self.scan.point_count)
                    self._capture_progress()
                    self.state, self._message = "uploading", "Uploading scan"
                return
            if self.state == "uploading":
                if not online:
                    self.fail_upload()
                    return
                try:
                    result = self._transport.step()
                finally:
                    self._capture_progress()
                if result is not None:
                    # Receipt is being checked; confirmation is still pending.
                    self.upload_stage = 3
                    if not self._ack_matches(result):
                        self.fail_upload()
                        return
                    self.pause()
                    self.upload_stage = 4
                    self.upload_sent = self.upload_total
                    self.state, self._message = "complete", "Upload confirmed"
        except Exception:
            # URLs, network credentials and server responses never enter the
            # screen or log. The result stays in RAM until OK/CANCEL resets.
            self.fail_upload()
