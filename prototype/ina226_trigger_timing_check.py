"""Verify triggered INA226 averages with both PWM outputs held low."""
import main

COUNTS = (1, 4, 16, 64, 128, 256, 512, 1024)
ADDRESSES = (0x44, 0x41)


def run():
    instrument = main.Instrument()
    original = [instrument._read16(address, 0) for address in ADDRESSES]
    try:
        for index, count in enumerate(COUNTS):
            config = 0x4123 | (index << 9)
            payload = bytes((config >> 8, config & 255))
            started = main.time.ticks_us()
            for address in ADDRESSES:
                instrument._bus.writeto_mem(address, 0, payload)
                assert instrument._read16(address, 0) == config
            ready = [None, None]
            polls = 0
            while ready[0] is None or ready[1] is None:
                for sensor, address in enumerate(ADDRESSES):
                    if ready[sensor] is None and instrument._read16(address, 6) & 8:
                        ready[sensor] = main.time.ticks_diff(main.time.ticks_us(), started)
                polls += 1
                if main.time.ticks_diff(main.time.ticks_us(), started) > 4000000:
                    raise RuntimeError("Triggered conversion did not finish")
                if ready[0] is None or ready[1] is None:
                    main.time.sleep_ms(2)
            first = [(instrument._read16(address, 1), instrument._read16(address, 2))
                     for address in ADDRESSES]
            main.time.sleep_ms(20)
            last = [(instrument._read16(address, 1), instrument._read16(address, 2))
                    for address in ADDRESSES]
            assert first == last
            print("INA_TRIGGER_PASS", "averages", count, "config", config,
                  "nominal_us", count * 2200, "ready_us", ready,
                  "polls", polls, "registers_stable", True, "codes", first)
    finally:
        for sensor, address in enumerate(ADDRESSES):
            config = original[sensor]
            instrument._bus.writeto_mem(address, 0, bytes((config >> 8, config & 255)))
        instrument.close()
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)
    print("INA_TRIGGER_DONE", "restored_config", original, "outputs_low", True)


run()
