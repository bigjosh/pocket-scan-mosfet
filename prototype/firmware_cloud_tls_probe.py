"""Real Google TLS/clock check with UI resident and both outputs held at zero."""
from machine import Pin
Pin(20, Pin.OUT, value=0)
Pin(21, Pin.OUT, value=0)
import tls_memory
assert tls_memory.reserve(), "TLS startup reserve unavailable"
import gc
import json
import time
import socket
import ssl
import storage
from display import Display
from hardware import Instrument
from engine import Engine
from ui import App
from wifi import WifiManager
from https_upload import sync_clock, connect_tls


def diagnostic_connect(host):
    """Public-host-only probe: report stage/type/code, never any credentials."""
    raw = None
    stage = "dns"
    try:
        gc.collect()
        address = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)[0][-1]
        raw = socket.socket()
        raw.settimeout(5)
        stage = "tcp"
        raw.connect(address)
        stage = "context"
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        stage = "ca_file"
        largest = 0
        for block in (131072, 65536, 32768, 16384, 8192):
            try:
                trial = bytearray(block)
                largest = block
                del trial
                gc.collect()
                break
            except MemoryError:
                gc.collect()
        print("TLS_MEMORY", json.dumps({"free_heap": gc.mem_free(), "largest_tested_block": largest}))
        with open("google_roots.pem", "rb") as stream:
            roots = stream.read()
        context.load_verify_locations(cadata=roots)
        del roots
        gc.collect()
        stage = "handshake"
        wrapped = context.wrap_socket(raw, server_hostname=host)
        stage = "nonblocking"
        if hasattr(wrapped, "setblocking"):
            wrapped.setblocking(False)
        else:
            raw.setblocking(False)
        return wrapped
    except Exception as error:
        code = error.args[0] if error.args and isinstance(error.args[0], int) else None
        print("TLS_STAGE", json.dumps({"stage": stage, "type": type(error).__name__,
                                      "code": code, "free_heap": gc.mem_free(),
                                      "utc": time.gmtime()[:6]}))
        if raw is not None:
            raw.close()
        raise RuntimeError("Public Google TLS probe failed")


class NoInputs:
    def poll(self, now):
        return []


hardware = Instrument()
wifi = WifiManager()
sock = None
try:
    hardware.zero()
    app = App(Display(), NoInputs(), Engine(hardware), storage.load_settings(),
              storage.load_json("calibration.json"), wifi=wifi)
    app.render()
    wifi.pause()
    wifi.resume(time.ticks_ms())
    assert wifi.autoconnect(time.ticks_ms())
    deadline = time.ticks_ms()
    while wifi.state == "connecting":
        wifi.tick(time.ticks_ms())
        assert time.ticks_diff(time.ticks_ms(), deadline) < 22000
        time.sleep_ms(20)
    assert wifi.state == "connected", "Saved network unavailable"
    sync_clock()
    gc.collect()
    before = gc.mem_free()
    result = []
    for host in ("script.google.com", "script.googleusercontent.com"):
        started = time.ticks_ms()
        tls_memory.release()
        sock = diagnostic_connect(host)
        connected_heap = gc.mem_free()
        sock.close()
        sock = None
        gc.collect()
        restored = tls_memory.reserve()
        assert restored, "TLS reserve did not restore"
        result.append({"host": host, "verified_tls": True,
                       "elapsed_ms": time.ticks_diff(time.ticks_ms(), started),
                       "free_heap_with_tls": connected_heap})
    # A wrong hostname must fail even though the same CA signs the connection.
    address = socket.getaddrinfo("script.google.com", 443, socket.AF_INET, socket.SOCK_STREAM)[0][-1]
    raw = socket.socket()
    raw.settimeout(5)
    rejected = False
    tls_memory.release()
    try:
        raw.connect(address)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.verify_mode = ssl.CERT_REQUIRED
        with open("google_roots.pem", "rb") as stream:
            context.load_verify_locations(cadata=stream.read())
        try:
            sock = context.wrap_socket(raw, server_hostname="wrong-hostname.invalid")
        except (OSError, ValueError):
            rejected = True
    finally:
        raw.close()
        if sock is not None:
            sock.close()
            sock = None
    assert rejected, "TLS hostname mismatch was accepted"
    print("CLOUD_TLS_NATIVE", json.dumps({"utc_year": time.gmtime()[0],
          "clock_synced": True, "ui_resident": True, "free_heap_before_tls": before,
          "endpoints": result, "wrong_hostname_rejected": rejected,
          "outputs_held_zero": True, "credentials_exported": False}))
finally:
    if sock is not None:
        sock.close()
    hardware.close()
    wifi.pause()
    Pin(20, Pin.OUT, value=0)
    Pin(21, Pin.OUT, value=0)
    print("RADIO_AND_OUTPUTS_DISABLED")
