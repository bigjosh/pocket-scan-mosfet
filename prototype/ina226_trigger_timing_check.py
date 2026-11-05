"""Verify mixed INA226/INA228 averages with both PWM outputs held low."""
import main
import model
from hardware import INA_CONFIG, INA228_ADC_CONFIG

COUNTS = (1, 4, 16, 64, 128, 256, 512, 1024)
ADDRESSES = (0x44, 0x40)


def run():
    instrument = main.Instrument()
    original = [instrument._read16(0x44, 0), instrument._read16(0x40, 1)]
    first, last = [0, 0, 0, 0], [0, 0, 0, 0]
    try:
        for index, count in enumerate(COUNTS):
            started = main.time.ticks_us()
            instrument.begin_measurement(count)
            ready = [None, None]
            polls = 0
            while True:
                complete = instrument.poll_measurement()
                for sensor in range(2):
                    if ready[sensor] is None and instrument._ready_mask & (1 << sensor):
                        ready[sensor] = main.time.ticks_diff(main.time.ticks_us(), started)
                polls += 1
                if complete:
                    break
                if main.time.ticks_diff(main.time.ticks_us(), started) > 4000000:
                    raise RuntimeError("Triggered conversion did not finish")
                if ready[0] is None or ready[1] is None:
                    main.time.sleep_ms(2)
            instrument.read_codes_into(first)
            main.time.sleep_ms(20)
            instrument.read_codes_into(last)
            assert first == last
            print("INA_TRIGGER_PASS", "averages", count,
                  "nominal_us", [count * 2 * model.INA_CONVERSION_US,
                                 count * 2 * model.INA228_CONVERSION_US], "ready_us", ready,
                  "polls", polls, "registers_stable", True, "codes", first)
    finally:
        errors = []
        try:
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
            raise RuntimeError("Timing probe configuration restore failed")
    print("INA_TRIGGER_DONE", "restored_config", original, "outputs_low", True)


run()
