"""Run with mpremote resume after the upload probe to test a 64 KiB reserve."""
import micropython
assert tls_memory.reserve(65536), "64 KiB reserve unavailable"
try:
    wifi.resume(time.ticks_ms())
    wifi.autoconnect(time.ticks_ms())
    started = time.ticks_ms()
    while wifi.state == "connecting":
        wifi.tick(time.ticks_ms())
        assert time.ticks_diff(time.ticks_ms(), started) < 25000
        time.sleep_ms(10)
    assert wifi.state == "connected"
    cloud.request_upload()
    started = time.ticks_ms()
    while cloud.status["pending_count"]:
        app.tick(time.ticks_ms())
        assert cloud.state not in ("retry", "error"), cloud.status["message"]
        assert time.ticks_diff(time.ticks_ms(), started) < 120000
        time.sleep_ms(5)
    for _ in range(3):
        app.tick(time.ticks_ms())
    print("RESUME_UPLOAD", json.dumps({"status": cloud.status,
          "reserve_bytes": tls_memory.reserved_bytes(), "free_heap": gc.mem_free()}))
    assert tls_memory.available(), "Reserve restoration failed again"
finally:
    cloud.pause()
    wifi.pause()
    hardware.close()
    gc.collect()
    micropython.mem_info()
    print("RADIO_AND_OUTPUTS_DISABLED")
