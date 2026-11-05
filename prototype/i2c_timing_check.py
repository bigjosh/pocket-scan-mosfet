"""Measure current 100 kHz I2C calls with both outputs at zero drive."""
import main
from hardware import INA_CONFIG, INA228_ADC_CONFIG

SENSORS = ((0x44, 6, 0, INA_CONFIG, 1, 2),
           (0x40, 0x0B, 1, INA228_ADC_CONFIG, 4, 5))


def run():
    instrument = main.Instrument()
    try:
        for address, status_register, config_register, config, shunt_register, bus_register in SENSORS:
            for operation in ("read_ready", "write_config", "read_two_voltages"):
                minimum, maximum, total = 1000000, 0, 0
                for _ in range(200):
                    started = main.time.ticks_us()
                    if operation == "read_ready":
                        instrument._read16(address, status_register)
                    elif operation == "write_config":
                        instrument._write16(address, config_register, config)
                    else:
                        read = instrument._read16 if address == 0x44 else instrument._read24
                        read(address, shunt_register)
                        read(address, bus_register)
                    elapsed = main.time.ticks_diff(main.time.ticks_us(), started)
                    minimum = min(minimum, elapsed)
                    maximum = max(maximum, elapsed)
                    total += elapsed
                print("I2C_TIMING", address, operation, "mean_us", total / 200,
                      "min_us", minimum, "max_us", maximum)
            assert instrument._read16(address, config_register) == config
    finally:
        try:
            instrument.close()
        finally:
            main.Pin(21, main.Pin.OUT, value=0)
            main.Pin(20, main.Pin.OUT, value=0)
    print("I2C_TIMING_DONE", "config_unchanged", True, "outputs_low", True)


run()
