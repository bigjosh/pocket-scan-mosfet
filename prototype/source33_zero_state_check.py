"""Read zero-drive offsets before/after production UI and radio setup."""
import main


def measure(label, instrument):
    main.time.sleep_ms(300)
    stats = {role: [0.0, 100.0, -100.0, 0.0] for role in ("drain", "gate")}
    for _ in range(80):
        pair = instrument.read_pair()
        for role in stats:
            row = pair[role]
            values = stats[role]
            voltage = row["terminal_v"]
            values[0] += voltage
            values[1] = min(values[1], voltage)
            values[2] = max(values[2], voltage)
            values[3] += row["shunt_v"]
        main.time.sleep_ms(5)
    for role in stats:
        v = stats[role]
        print("ZERO_STATE", label, role, "mean_v", v[0] / 80,
              "min_v", v[1], "max_v", v[2], "shunt_mean_v", v[3] / 80)


def run():
    instrument = wifi = None
    try:
        settings = main.storage.load_settings(migrate=False)
        calibration = main.storage.load_calibration()
        main.model.validate_calibration(calibration, settings)
        main.gc.collect()
        instrument = main.Instrument()
        measure("before_display", instrument)
        display = main.Display(raster_pool=main._display_buffer)
        inputs = main.Inputs()
        display.fill(main.BG)
        display.text("Zero-output diagnostic", 16, 24, main.INK, bg=main.BG)
        measure("display_inputs", instrument)
        wifi = main.WifiManager()
        wifi.pause()
        measure("wifi_paused", instrument)
        main.time.sleep_ms(1000)
        measure("repeat", instrument)
    finally:
        try:
            if instrument is not None:
                instrument.close()
        finally:
            main.Pin(21, main.Pin.OUT, value=0)
            main.Pin(20, main.Pin.OUT, value=0)
            if wifi is not None:
                wifi.pause()
        print("ZERO_STATE_DONE", "pins_low", True)


if __name__ == "__main__":
    run()
