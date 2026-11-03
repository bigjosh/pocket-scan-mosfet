"""Explicit OPEN-FIXTURE raw drive probe; no DUT, loads or meter probes.

Run from a fresh interpreter. Uses the production display/inputs/paused Wi-Fi,
calibrated PWM commands and raw envelope/current checks. It intentionally does
not apply baseline correction or require voltage targets to match. No writes,
uploads or reset. Left physical button or bottom-left touch cancels.
"""

import main

SAMPLES = 80
PAUSE_MS = 5
SETTLE_MS = 500
CASES = ((None, None), (3.3, None), (3.3, 0.3), (3.3, 3.3),
         (3.4, None), (3.4, 0.3), (3.4, 3.3))


class Cancelled(Exception):
    pass


def emit(marker, value):
    print(marker, main.storage.json.dumps(value))


def check_cancel(inputs):
    for event in inputs.poll(main.time.ticks_ms()):
        if event[0] == "left" or (event[0] == "touch" and event[1] < 240 and event[2] >= 264):
            raise Cancelled()


def measure(scanner, inputs):
    started = main.time.ticks_ms()
    while main.time.ticks_diff(main.time.ticks_ms(), started) < SETTLE_MS:
        check_cancel(inputs)
        scanner._checked_read()  # Check every settling read, but do not average it.
        main.time.sleep_ms(PAUSE_MS)
    stats = {role: [0.0, 100.0, -100.0, 0.0, 100.0, -100.0]
             for role in ("drain", "gate")}
    for _ in range(SAMPLES):
        check_cancel(inputs)
        pair = scanner._checked_read()
        for role in ("drain", "gate"):
            value = pair[role]
            row = stats[role]
            voltage, shunt = value["terminal_v"], value["shunt_v"]
            row[0] += voltage
            row[1], row[2] = min(row[1], voltage), max(row[2], voltage)
            row[3] += shunt
            row[4], row[5] = min(row[4], shunt), max(row[5], shunt)
        main.time.sleep_ms(PAUSE_MS)
    return stats


def run():
    instrument = wifi = None
    complete = False
    try:
        settings = main.storage.load_settings(migrate=False)
        calibration = main.storage.load_calibration()
        main.model.validate_calibration(calibration, settings)
        if settings["source_voltage_uv"] != 3300000:
            raise ValueError("Drive probe requires the configured 3.3 V source")
        plan = []
        for drain_v, gate_v in CASES:
            drain_duty = (0.0 if drain_v is None else
                          main.model.duty_for_voltage(calibration["channels"]["drain"], drain_v))
            gate_duty = (0.0 if gate_v is None else
                         main.model.duty_for_voltage(calibration["channels"]["gate"], gate_v))
            plan.append((drain_v, gate_v, drain_duty, gate_duty))
        main.gc.collect()
        display = main.Display(raster_pool=main._display_buffer)
        instrument = main.Instrument()
        inputs = main.Inputs()
        wifi = main.WifiManager()
        if wifi.pause() is not True:
            raise RuntimeError("Wi-Fi could not be paused")
        scanner = main.Engine(instrument)
        scanner.settings = main.model.validate_settings(settings)
        # Idle excludes scan baseline/floor matching while retaining the same
        # sensor saturation, absolute voltage-envelope and raw current limits.
        scanner.state = "idle"
        display.fill(main.BG)
        display.text("Open-output drive probe", 16, 24, main.INK, bg=main.BG)
        display.text("LEFT / tap below: CANCEL", 16, 282, main.INK, bg=main.BG)
        emit("DRIVE_PROBE_BEGIN", {"cases": len(plan), "samples": SAMPLES,
             "spacing_ms": PAUSE_MS, "settling_ms": SETTLE_MS,
             "source_voltage_uv": settings["source_voltage_uv"],
             "driver_supply_uv": settings["driver_supply_uv"], "wifi": "paused"})
        for index, (drain_v, gate_v, drain_duty, gate_duty) in enumerate(plan):
            check_cancel(inputs)
            display.fill_rect(16, 65, 420, 60, main.BG)
            display.text("Stage %d / %d" % (index + 1, len(plan)), 16, 65, main.INK, bg=main.BG)
            # Each fixture change starts with gate disabled; raw safety reads
            # begin immediately afterward and continue throughout settling.
            instrument.set_duty("gate", 0.0)
            instrument.set_duty("drain", drain_duty)
            instrument.set_duty("gate", gate_duty)
            commands = {}
            for role, requested, duty in (("drain", drain_v, drain_duty), ("gate", gate_v, gate_duty)):
                pwm = instrument._pwms[role]
                commands[role] = {"target_v": requested, "zero_pwm": requested is None,
                                  "command_fraction": duty,
                                  "command_u16": int(duty * 65535 + 0.5),
                                  "readback_u16": pwm.duty_u16(), "pwm_hz": pwm.freq()}
            emit("DRIVE_PROBE_COMMAND", {"index": index, "channels": commands})
            stats = measure(scanner, inputs)
            for role in ("drain", "gate"):
                row = stats[role]
                emit("DRIVE_PROBE_MEASURE", {"index": index, "channel": role,
                     "samples": SAMPLES, "terminal_mean_v": row[0] / SAMPLES,
                     "terminal_min_v": row[1], "terminal_max_v": row[2],
                     "raw_shunt_mean_v": row[3] / SAMPLES,
                     "raw_shunt_min_v": row[4], "raw_shunt_max_v": row[5]})
        complete = True
    except Cancelled:
        print("DRIVE_PROBE_CANCELLED")
    except Exception as error:
        print("DRIVE_PROBE_ABORT", str(error))
        raise
    finally:
        try:
            if instrument is not None:
                instrument.close()
        finally:
            try:
                main.Pin(21, main.Pin.OUT, value=0)
            finally:
                try:
                    main.Pin(20, main.Pin.OUT, value=0)
                finally:
                    if wifi is not None:
                        wifi.pause()
    if complete:
        emit("DRIVE_PROBE_COMPLETE", {"cases": len(CASES), "outputs_low": True})


if __name__ == "__main__":
    run()
