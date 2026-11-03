"""Bounded Wi-Fi setup and successful-profile cache; no radio activity at import.

The manager is portable and accepts a fake adapter for host tests. The CYW43
scan itself is synchronous (up to ten seconds); call scan only from an explicit
idle/setup screen, after painting that screen. Joins and timeouts are polled.
Passwords are private: never include driver exceptions in public messages.
"""

try:
    from time import ticks_diff as _ticks_diff
except ImportError:
    def _ticks_diff(now, before):
        return now - before

try:
    from . import storage
except ImportError:
    import storage

MAX_PROFILES = 20
MAX_NETWORKS = 32
CONNECT_TIMEOUT_MS = 20000
POLL_MS = 250

# CYW43 security flags, including the older compact scan codes. These are not
# the generic network.WLAN documentation's enumeration on every port.
OPEN = 0
WPA = 0x00200002
WPA2 = 0x00400004
WPA_MIXED = 0x00400006
WPA3 = 0x01000004
WPA23 = 0x01400004
PERSONAL = (WPA, WPA2, WPA_MIXED, WPA3, WPA23)


def _security(value):
    if not isinstance(value, int):
        return -1
    if value == OPEN:
        return OPEN
    # Scan results OR the AP's privacy bit into the auth flags. It is not
    # evidence of WEP when WPA/RSN is also present. The installed Pico reports
    # compact 5 = privacy|RSN, while newer CYW43 sources expose full auth flags.
    stripped = value & ~1
    if stripped in PERSONAL:
        return stripped
    return {2: WPA, 4: WPA2, 6: WPA_MIXED}.get(stripped, -1)


def _ssid(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    if not isinstance(value, str) or not 1 <= len(value.encode("utf-8")) <= 32:
        raise ValueError("Invalid network name")
    for character in value:
        if ord(character) < 32 or ord(character) == 127:
            raise ValueError("Network name cannot be displayed")
    return value


def _password(value, security):
    if not isinstance(value, str):
        raise ValueError("Invalid password")
    if security == OPEN:
        if value:
            raise ValueError("Open networks do not use a password")
        return value
    if security not in PERSONAL:
        raise ValueError("Network security is not supported")
    if len(value) == 64:
        if all(character in "0123456789abcdefABCDEF" for character in value):
            return value
    elif 8 <= len(value) <= 63:
        if all(32 <= ord(character) <= 126 for character in value):
            return value
    raise ValueError("Use 8-63 printable characters or a 64-digit hex key")


def _row(ssid, security, saved, rssi=None):
    supported = security == OPEN or security in PERSONAL
    label = "Open" if security == OPEN else ("Personal" if supported else "Unsupported")
    return {"ssid": ssid, "rssi": rssi, "security": security,
            "security_label": label, "supported": supported,
            "requires_password": security != OPEN, "saved": saved}


def _profiles(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("Saved Wi-Fi settings are invalid")
    records = data.get("profiles")
    if not isinstance(records, list) or len(records) > MAX_PROFILES:
        raise ValueError("Saved Wi-Fi settings are invalid")
    result = []
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("Saved Wi-Fi settings are invalid")
        name = _ssid(item.get("ssid"))
        security = _security(item.get("security", WPA_MIXED))
        password = _password(item.get("password"), security)
        if any(profile["ssid"] == name for profile in result):
            continue
        result.append({"ssid": name, "password": password, "security": security})
    return result


class MicroPythonAdapter:
    """Lazy CYW43 station adapter. Deactivation also handles soft-reset residue."""

    def __init__(self):
        self._sta = None
        self._ap = None

    def _interfaces(self):
        if self._sta is None:
            import network
            self._sta = network.WLAN(network.WLAN.IF_STA)
            self._ap = network.WLAN(network.WLAN.IF_AP)

    def activate(self):
        self._interfaces()
        self._ap.active(False)
        self._sta.active(True)

    def deactivate(self):
        self._interfaces()
        failed = False
        for interface in (self._sta, self._ap):
            try:
                interface.active(False)
            except Exception:
                failed = True
        for interface in (self._sta, self._ap):
            try:
                if interface.active():
                    failed = True
            except Exception:
                failed = True
        return not failed

    def scan(self):
        return self._sta.scan()

    def disconnect(self):
        self._sta.disconnect()

    def connect(self, ssid, password, security):
        self._sta.connect(ssid, password, security=security)

    def status(self):
        return self._sta.status()

    def isconnected(self):
        return self._sta.isconnected()

    def ip(self):
        return self._sta.ifconfig()[0]


class WifiManager:
    def __init__(self, adapter=None, path="wifi.json", store=storage):
        self._adapter = adapter
        self._path, self._store = path, store
        self._profiles = []
        self._networks = []
        self._pending = None
        self._restore_ssid = None
        self._desired_ssid = None
        self._want_connection = False
        self._persist_connection = True
        self._started_ms = 0
        self._last_poll_ms = None
        self._now_ms = 0
        self.state, self._ssid, self._ip = "off", "", ""
        self._message = "Wi-Fi off"
        try:
            data = store.load_json(path)
            if data is not None:
                self._profiles = _profiles(data)
        except Exception:
            # Parse errors can contain file content. Never propagate them.
            self.state = "error"
            self._message = "Saved Wi-Fi settings could not be loaded"

    @property
    def status(self):
        return {"state": self.state, "ssid": self._ssid, "ip": self._ip,
                "message": self._message, "saved_count": len(self._profiles)}

    def _radio(self):
        if self._adapter is None:
            self._adapter = MicroPythonAdapter()
        return self._adapter

    def _profile(self, ssid):
        for profile in self._profiles:
            if profile["ssid"] == ssid:
                return profile
        return None

    def has_saved(self, ssid):
        return self._profile(ssid) is not None

    def saved_networks(self):
        return [_row(profile["ssid"], profile["security"], True)
                for profile in self._profiles]

    def _shutdown(self):
        self._pending = None
        self._ip = ""
        try:
            stopped = self._radio().deactivate()
        except Exception:
            stopped = False
        if not stopped:
            self.state = "error"
            self._message = "Wi-Fi could not be disabled"
            return False
        return True

    def pause(self):
        """Fail closed: acquisition may begin only after this returns True."""
        if not self._shutdown():
            raise RuntimeError("Wi-Fi could not be disabled")
        self._restore_ssid = None
        self.state, self._message = "paused", "Wi-Fi paused for measurement"
        return True

    def resume(self, now_ms=0):
        if self.state != "paused":
            return False
        self.state, self._message = "off", "Wi-Fi off"
        if self._want_connection and self.has_saved(self._desired_ssid):
            return self.connect(self._desired_ssid, use_saved=True, now_ms=now_ms)
        return False

    def autoconnect(self, now_ms=0, persist=True):
        if not isinstance(persist, bool):
            raise ValueError("Invalid Wi-Fi persistence setting")
        if self.state == "paused" and not persist:
            # Explicit post-scan connection. Do not resume a previous intent
            # or enable profile writes in the RAM-only acquisition path.
            self.state, self._message = "off", "Wi-Fi off"
        if self.state == "paused" or not self._profiles:
            return False
        return self.connect(self._profiles[0]["ssid"], use_saved=True, now_ms=now_ms,
                            persist=persist)

    def scan(self):
        if self.state in ("paused", "connecting"):
            self._message = "Finish the current operation before finding networks"
            return []
        previous = self.state
        self.state, self._message = "scanning", "Finding networks"
        result = []
        try:
            radio = self._radio()
            radio.activate()
            raw = radio.scan()
            for item in raw:
                try:
                    name, strength, security = _ssid(item[0]), int(item[3]), _security(item[4])
                except (ValueError, TypeError, IndexError):
                    continue
                existing = None
                for row in result:
                    if row["ssid"] == name:
                        existing = row
                        break
                if existing is not None:
                    if strength > existing["rssi"]:
                        existing.update(_row(name, security, self.has_saved(name), strength))
                    continue
                row = _row(name, security, self.has_saved(name), strength)
                if len(result) < MAX_NETWORKS:
                    result.append(row)
                else:
                    weakest = min(range(len(result)), key=lambda index: result[index]["rssi"])
                    if strength > result[weakest]["rssi"]:
                        result[weakest] = row
            result.sort(key=lambda row: row["rssi"], reverse=True)
            self._networks = result
            self.state = "connected" if previous == "connected" and radio.isconnected() else "idle"
            if self.state != "connected":
                self._ip = ""
            self._message = "Choose a network" if result else "No visible networks found"
            return result
        except Exception:
            self._networks = []
            if self._shutdown():
                self.state, self._message = "error", "Network search failed"
            return []

    def connect(self, ssid, password="", use_saved=False, now_ms=0, persist=True):
        if self.state == "paused":
            self._message = "Wi-Fi is paused for measurement"
            return False
        if not isinstance(persist, bool):
            raise ValueError("Invalid Wi-Fi persistence setting")
        try:
            name = _ssid(ssid)
            saved = self._profile(name)
            if use_saved:
                if saved is None:
                    raise ValueError("No saved password for this network")
                candidate = dict(saved)
            else:
                security = saved["security"] if saved else WPA_MIXED
                for row in self._networks:
                    if row["ssid"] == name:
                        security = row["security"]
                        break
                candidate = {"ssid": name, "security": security,
                             "password": _password(password, security)}
            # A changed AP security type must not silently reuse a cached PSK.
            for row in self._networks:
                if row["ssid"] == name:
                    if not row["supported"]:
                        raise ValueError("Network security is not supported")
                    if use_saved and row["security"] != candidate["security"]:
                        raise ValueError("Network security changed; enter connection details again")
        except (ValueError, TypeError) as error:
            # Only our fixed validation messages reach here.
            self._message = str(error) if isinstance(error, ValueError) else "Invalid network settings"
            return False
        previous = self._ssid if self.state == "connected" and self.has_saved(self._ssid) else None
        self._restore_ssid = previous
        self._want_connection = True
        self._persist_connection = persist
        self._desired_ssid = name if saved else previous
        return self._begin(candidate, now_ms)

    def _begin(self, candidate, now_ms, restoring=False):
        self._now_ms, self._started_ms = now_ms, now_ms
        self._last_poll_ms = None
        self._ssid, self._ip = candidate["ssid"], ""
        self._pending = candidate
        self._restoring = restoring
        try:
            radio = self._radio()
            radio.activate()
            radio.disconnect()
            radio.connect(candidate["ssid"], candidate["password"], candidate["security"])
        except Exception:
            self._failed("Connection could not be started", now_ms)
            return False
        self.state = "connecting"
        self._message = "Restoring previous network" if restoring else "Connecting"
        return True

    def _failed(self, message, now_ms):
        previous = self._restore_ssid
        self._restore_ssid = None
        if not self._shutdown():
            return
        self.state, self._message = "error", message
        self._want_connection = False
        self._desired_ssid = None
        if previous is not None:
            profile = self._profile(previous)
            if profile is not None:
                self._want_connection = True
                self._desired_ssid = previous
                self._begin(dict(profile), now_ms, restoring=True)

    def _commit(self, candidate):
        if not self._persist_connection:
            return  # Upload-only connection never writes or reorders profiles.
        profiles = [dict(candidate)]
        profiles.extend(profile for profile in self._profiles if profile["ssid"] != candidate["ssid"])
        profiles = profiles[:MAX_PROFILES]
        if profiles != self._profiles:
            self._store.save_json_atomic(self._path, {"version": 1, "profiles": profiles})
            self._profiles = profiles

    def tick(self, now_ms):
        self._now_ms = now_ms
        if self.state not in ("connecting", "connected"):
            return
        if self.state == "connecting" and _ticks_diff(now_ms, self._started_ms) >= CONNECT_TIMEOUT_MS:
            self._failed("Connection timed out", now_ms)
            return
        if self._last_poll_ms is not None and _ticks_diff(now_ms, self._last_poll_ms) < POLL_MS:
            return
        self._last_poll_ms = now_ms
        try:
            radio = self._radio()
            connected = radio.isconnected()
            if self.state == "connected":
                if not connected:
                    self._failed("Wi-Fi connection lost", now_ms)
                return
            if connected:
                address = radio.ip()
                if not isinstance(address, str) or not address or address == "0.0.0.0":
                    return
                candidate = self._pending
                self._pending = None
                self._restore_ssid = None
                self._ip = address
                self.state = "connected"
                self._desired_ssid = candidate["ssid"]
                self._message = "Previous network restored" if self._restoring else "Connected"
                try:
                    self._commit(candidate)
                except Exception:
                    self._message = "Connected; Wi-Fi settings could not be saved"
                return
            result = radio.status()
            if result < 0:
                # CYW43_LINK_FAIL=-1, NONET=-2, BADAUTH=-3.
                message = {-3: "Password was not accepted", -2: "Network was not found"}.get(
                    result, "Connection failed")
                self._failed(message, now_ms)
        except Exception:
            self._failed("Wi-Fi status could not be read", now_ms)

    def cancel(self):
        self._want_connection = False
        self._desired_ssid = None
        self._restore_ssid = None
        was_paused = self.state == "paused"
        if not self._shutdown():
            return False
        self.state = "paused" if was_paused else "off"
        self._ssid = ""
        self._message = "Wi-Fi paused for measurement" if was_paused else "Wi-Fi off"
        return True

    def disconnect(self):
        return self.cancel()

    def forget(self, ssid):
        profiles = [profile for profile in self._profiles if profile["ssid"] != ssid]
        if len(profiles) == len(self._profiles):
            return True
        try:
            self._store.save_json_atomic(self._path, {"version": 1, "profiles": profiles})
        except Exception:
            self._message = "Saved network could not be removed"
            return False
        self._profiles = profiles
        if self._ssid == ssid or self._desired_ssid == ssid:
            return self.cancel()
        return True
