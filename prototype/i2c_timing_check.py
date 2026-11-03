"""Measure current 100 kHz I2C calls with both outputs at zero drive."""
import main


def run():
    instrument = main.Instrument()
    try:
        payload = bytes((0x41, 0x27))
        for address in (0x44, 0x41):
            for operation in ("read_ready", "write_config", "read_two_voltages"):
                minimum, maximum, total = 1000000, 0, 0
                for _ in range(200):
                    started = main.time.ticks_us()
                    if operation == "read_ready":
                        instrument._read16(address, 6)
                    elif operation == "write_config":
                        instrument._bus.writeto_mem(address, 0, payload)
                    else:
                        instrument._read16(address, 1)
                        instrument._read16(address, 2)
                    elapsed = main.time.ticks_diff(main.time.ticks_us(), started)
                    minimum = min(minimum, elapsed)
                    maximum = max(maximum, elapsed)
                    total += elapsed
                print("I2C_TIMING", address, operation, "mean_us", total / 200,
                      "min_us", minimum, "max_us", maximum)
            assert instrument._read16(address, 0) == 0x4127
    finally:
        try:
            instrument.close()
        finally:
            main.Pin(21, main.Pin.OUT, value=0)
            main.Pin(20, main.Pin.OUT, value=0)
    print("I2C_TIMING_DONE", "config_unchanged", True, "outputs_low", True)


run()
