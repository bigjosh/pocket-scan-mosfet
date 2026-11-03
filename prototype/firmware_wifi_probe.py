"""Identify WLAN capabilities and scan availability; never prints SSIDs/keys.

Both analog outputs are held at zero. The radio is disabled on exit. This
does not connect to a network or read/write any saved Wi-Fi credentials.
"""
import json
import network
import time
from hardware import Instrument

instrument = Instrument()
sta = ap = None
try:
    sta_id = getattr(network.WLAN, "IF_STA", getattr(network, "STA_IF", 0))
    ap_id = getattr(network.WLAN, "IF_AP", getattr(network, "AP_IF", 1))
    sta, ap = network.WLAN(sta_id), network.WLAN(ap_id)
    sta.active(False)
    ap.active(False)
    sta.active(True)
    started = time.ticks_ms()
    rows = sta.scan()
    security = {}
    for row in rows:
        key = str(row[4])
        security[key] = security.get(key, 0) + 1
    print("WIFI_PROBE", json.dumps({"networks_seen": len(rows),
          "security_codes": security, "scan_ms": time.ticks_diff(time.ticks_ms(), started),
          "sta_id": sta_id, "ap_id": ap_id,
          "status_supported": hasattr(sta, "status"),
          "ipconfig_supported": hasattr(sta, "ipconfig"),
          "ifconfig_supported": hasattr(sta, "ifconfig"),
          "analogs": instrument.read_pair()}))
finally:
    instrument.close()
    for interface in (sta, ap):
        if interface is not None:
            interface.active(False)
    print("RADIO_AND_OUTPUTS_DISABLED")
