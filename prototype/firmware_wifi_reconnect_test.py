"""Verify saved-network reload and pause/resume; never export credentials.

Run after stopping the application. Both PWM pins stay low throughout. The
saved profile is used only on the Pico; output contains counts and timings.
"""

from machine import Pin

Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)

import json
import time
import network
from wifi import WifiManager


def wait_connected(manager):
    started = time.ticks_ms()
    while manager.state == "connecting":
        manager.tick(time.ticks_ms())
        if time.ticks_diff(time.ticks_ms(), started) > 22000:
            raise RuntimeError("Saved-network verification exceeded its deadline")
        time.sleep_ms(20)
    if manager.state != "connected":
        raise RuntimeError("Saved-network connection verification failed")
    if manager.status["message"] == "Connected; Wi-Fi settings could not be saved":
        raise RuntimeError("Saved-network persistence verification failed")
    return time.ticks_diff(time.ticks_ms(), started)


manager = None
try:
    manager = WifiManager()
    count = manager.status["saved_count"]
    assert count > 0, "No saved Wi-Fi profile found"
    assert manager.pause() is True
    manager.resume(time.ticks_ms())
    assert manager.autoconnect(time.ticks_ms()), "Saved connection did not start"
    connect_ms = wait_connected(manager)
    assert manager.pause() is True
    assert not network.WLAN(network.WLAN.IF_STA).active(), "Station remained active"
    assert not network.WLAN(network.WLAN.IF_AP).active(), "Access point remained active"
    assert manager.resume(time.ticks_ms()), "Saved connection did not resume"
    resume_ms = wait_connected(manager)
    print("WIFI_RECONNECT_NATIVE", json.dumps({
        "saved_profile_count": count,
        "saved_profile_reloaded": True,
        "real_network_join_verified": True,
        "radio_pause_verified": True,
        "resume_reconnected": True,
        "connect_ms": connect_ms,
        "resume_ms": resume_ms,
        "outputs_held_low": True,
        "credentials_exported": False,
    }))
finally:
    if manager is not None:
        manager.pause()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print("RADIO_AND_OUTPUTS_DISABLED")
