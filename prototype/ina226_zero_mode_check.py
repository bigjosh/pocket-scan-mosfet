"""Compare zero-drive continuous and triggered readings in one hardware session."""
import main
from hardware import INA_CONFIG, INA228_ADC_CONFIG


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
            instrument.set_duty("gate", 0)
            instrument.set_duty("drain", 0)
            main.time.sleep_ms(500)
            before = means(instrument, 80)
            instrument.begin_measurement(64)
            started = main.time.ticks_ms()
            while not instrument.poll_measurement():
                if main.time.ticks_diff(main.time.ticks_ms(), started) > 1000:
                    raise RuntimeError("Conversion timeout")
                main.time.sleep_ms(2)
            after = means(instrument, 1)
            print("ZERO_MODE", "repeat", repetition, "continuous", before,
                  "triggered_64", after, "fields", "D_V,G_V,D_shunt_V,G_shunt_V")
    finally:
        if instrument is not None:
            try:
                errors = []
                for address, register, config in ((0x44, 0, INA_CONFIG), (0x40, 1, INA228_ADC_CONFIG)):
                    try:
                        instrument._write16(address, register, config)
                    except Exception as error:
                        errors.append(type(error).__name__)
            finally:
                try:
                    instrument.close()
                finally:
                    main.Pin(21, main.Pin.OUT, value=0)
                    main.Pin(20, main.Pin.OUT, value=0)
            if errors:
                raise RuntimeError("Zero-mode configuration restore failed")
        else:
            main.Pin(21, main.Pin.OUT, value=0)
            main.Pin(20, main.Pin.OUT, value=0)
        if wifi is not None:
            wifi.pause()
    print("ZERO_MODE_DONE", "config_restored", True, "outputs_low", True)


run()
