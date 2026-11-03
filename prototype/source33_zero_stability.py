"""Twenty seconds of zero-drive reads with display/input/radio state like Home."""
import main

instrument = wifi = None
try:
    display = main.Display(raster_pool=main._display_buffer)
    instrument = main.Instrument()
    inputs = main.Inputs()
    wifi = main.WifiManager()
    wifi.pause()
    instrument.zero()
    display.text("ZERO DRIVE CHECK", 16, 24, main.INK, bg=main.BG, scale=2)
    main.time.sleep_ms(500)
    stats = {role: [0.0, 1e6, -1e6] for role in ("drain", "gate")}
    for index in range(400):
        for event in inputs.poll(main.time.ticks_ms()):
            if event[0] == "left":
                raise RuntimeError("Zero-drive check cancelled")
        pair = instrument.read_pair()
        for role, values in stats.items():
            voltage = pair[role]["terminal_v"]
            values[0] += voltage
            values[1] = min(values[1], voltage)
            values[2] = max(values[2], voltage)
        if index % 20 == 19:
            print("SOURCE33_ZERO_STABLE", index + 1,
                  pair["drain"]["terminal_v"], pair["gate"]["terminal_v"])
            display.text("Readings: %03d" % (index + 1), 16, 64,
                         main.INK, bg=main.BG, scale=2)
        main.time.sleep_ms(50)
    for role, values in stats.items():
        print("SOURCE33_ZERO_SUMMARY", role, "samples", 400,
              "mean", values[0] / 400, "min", values[1], "max", values[2],
              "span", values[2] - values[1])
    print("SOURCE33_ZERO_STABILITY_PASS",
          all(values[2] - values[1] <= 0.020 for values in stats.values()))
finally:
    if instrument is not None:
        instrument.close()
    if wifi is not None:
        wifi.pause()
    main.Pin(21, main.Pin.OUT, value=0)
    main.Pin(20, main.Pin.OUT, value=0)
    print("SOURCE33_ZERO_DONE", "pins_low", main.Pin(20).value() == 0 and main.Pin(21).value() == 0)
