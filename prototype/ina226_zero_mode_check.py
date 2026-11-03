"""Compare zero-drive continuous and triggered readings in one hardware session."""
import main


def config(instrument, value):
    payload = bytes((value >> 8, value & 255))
    for address in (0x44, 0x41):
        instrument._bus.writeto_mem(address, 0, payload)


def means(instrument, count):
    sums = [0.0, 0.0, 0.0, 0.0]
    for _ in range(count):
        pair = instrument.read_pair()
        sums[0] += pair["drain"]["terminal_v"]
        sums[1] += pair["gate"]["terminal_v"]
        sums[2] += pair["drain"]["shunt_v"]
        sums[3] += pair["gate"]["shunt_v"]
        main.time.sleep_ms(5)
    return [value / count for value in sums]


def run():
    instrument = wifi = None
    try:
        display = main.Display(raster_pool=main._display_buffer)
        inputs = main.Inputs()
        wifi = main.WifiManager()
        wifi.pause()
        instrument = main.Instrument()
        for repetition in range(3):
            config(instrument, 0x4127)
            main.time.sleep_ms(500)
            before = means(instrument, 80)
            config(instrument, 0x4723)
            ready = [False, False]
            started = main.time.ticks_ms()
            while not all(ready):
                for index, address in enumerate((0x44, 0x41)):
                    if instrument._read16(address, 6) & 8:
                        ready[index] = True
                if main.time.ticks_diff(main.time.ticks_ms(), started) > 1000:
                    raise RuntimeError("Conversion timeout")
                main.time.sleep_ms(2)
            after = means(instrument, 1)
            print("ZERO_MODE", "repeat", repetition, "continuous", before,
                  "triggered_64", after, "fields", "D_V,G_V,D_shunt_V,G_shunt_V")
    finally:
        if instrument is not None:
            try:
                config(instrument, 0x4127)
            finally:
                instrument.close()
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)
        if wifi is not None:
            wifi.pause()
    print("ZERO_MODE_DONE", "config_restored", True, "outputs_low", True)


run()
