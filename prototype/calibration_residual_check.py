"""Explicit open-output baseline check; never run with a DUT or loads fitted.

Run on a fresh interpreter after uploading this separately compiled helper:
    import calibration_residual_check; calibration_residual_check.run()
Production fixed buffers, calibration, driver, safety checks and averaging are
used. Settings/calibration are read only; no UI, upload, reset or file writes.
Each point is one triggered block at the saved INA226 average count. Its
standard deviation is unavailable and is reported as null, never zero.
CAL_RESIDUAL_COMPLETE means the diagnostic finished, not a metrology pass.
Set MIN_DRAIN_TARGET_UV=100000 in RAM to exclude the unresolved low boundary;
this changes diagnostic coverage only and does not relax production guards.
"""

import main  # Reserve the actual production buffers before diagnostic state.

TIMEOUT_MS = 5 * 60 * 1000
MIN_DRAIN_TARGET_UV = 0
DRAIN_CANDIDATES_UV = (0, 1000000, 5000000, 10000000)
GATE_CANDIDATES_UV = (-3000000, 0, 3000000, 10000000)


def emit(marker, value):
    print(marker, main.storage.json.dumps(value))


def grid(settings, calibration):
    """Use reachable interior targets; move the low drain point above a floor."""
    model = main.model
    if (isinstance(MIN_DRAIN_TARGET_UV, bool) or not isinstance(MIN_DRAIN_TARGET_UV, int)
            or MIN_DRAIN_TARGET_UV < 0):
        raise ValueError("Diagnostic minimum drain target must be integer microvolts")
    model.validate_calibration(calibration, settings)
    source_uv = settings["source_voltage_uv"]
    source = model.volts_from_uv(source_uv)
    floor = model.drain_parking_voltage(calibration, settings)
    first_drain = int((floor - source) * 1000000) + 20000 if floor > source else 0
    values = {}
    for role, candidates in (("drain", DRAIN_CANDIDATES_UV), ("gate", GATE_CANDIDATES_UV)):
        points = calibration["channels"][role]["points"]
        low = points[0]["terminal_v"]
        high = min(points[-1]["terminal_v"], model.voltage_limit(settings))
        selected = []
        for candidate in candidates:
            if role == "drain" and candidate == 0:
                candidate = max(first_drain, MIN_DRAIN_TARGET_UV)
            if role == "drain" and candidate < MIN_DRAIN_TARGET_UV:
                continue
            terminal = model.volts_from_uv(candidate + source_uv)
            if low <= terminal <= high and candidate not in selected:
                selected.append(candidate)
        if not selected:
            raise ValueError("No representative " + role + " targets fit calibration")
        values[role] = selected
    return values, floor


def point_settings(saved, drain_uv, gate_uv):
    settings = dict(saved)
    settings.update({"drain_start_uv": drain_uv, "drain_stop_uv": drain_uv,
                     "drain_step_uv": 1000000,
                     "gate_start_uv": gate_uv, "gate_stop_uv": gate_uv,
                     "gate_step_uv": 1000000})
    return main.model.validate_settings(settings)


def run():
    instrument = None
    completed = False
    count = 0
    maximum_drain = maximum_gate = 0.0
    try:
        # Reserve/load packed tables before driver construction fragments RAM.
        saved = main.storage.load_settings(migrate=False)
        calibration = main.storage.load_calibration()
        values, floor = grid(saved, calibration)
        # Prove every requested point before any positive PWM is possible.
        for gate_uv in values["gate"]:
            for drain_uv in values["drain"]:
                setting = point_settings(saved, drain_uv, gate_uv)
                main.model.validate_scan_calibration(calibration, setting)
        setting = None
        main.gc.collect()
        instrument = main.Instrument()
        scanner = main.Engine(instrument)
        started = main.time.ticks_ms()
        emit("CAL_RESIDUAL_BEGIN", {"points": len(values["drain"]) * len(values["gate"]),
             "acquisition": main.model.acquisition_metadata(saved),
             "minimum_drain_target_uv": values["drain"][0],
             "low_boundary_excluded": values["drain"][0] > 0,
             "source_voltage_uv": saved["source_voltage_uv"],
             "driver_supply_uv": saved["driver_supply_uv"], "drain_park_v": floor})
        for gate_uv in values["gate"]:
            for drain_uv in values["drain"]:
                scanner.start_scan(point_settings(saved, drain_uv, gate_uv), calibration)
                point = None
                while scanner.state == "scanning":
                    now = main.time.ticks_ms()
                    if main.time.ticks_diff(now, started) >= TIMEOUT_MS:
                        raise RuntimeError("Residual diagnostic timed out")
                    for kind, data in scanner.tick(now):
                        if kind == "point":
                            point = data
                    main.time.sleep_ms(2)
                if scanner.state != "complete" or point is None:
                    raise RuntimeError(scanner.error or "Residual point did not complete")
                if point["acquisition_blocks_completed"] != 1:
                    raise RuntimeError("Residual averaging count did not match")
                drain_ua, gate_ua = point["id_a"] * 1000000, point["ig_a"] * 1000000
                maximum_drain = max(maximum_drain, abs(drain_ua))
                maximum_gate = max(maximum_gate, abs(gate_ua))
                emit("CAL_RESIDUAL_POINT", {"index": count,
                     "requested_vds_uv": drain_uv, "requested_vgs_uv": gate_uv,
                     "vds_v": point["vds"], "vgs_v": point["vgs"],
                     "id_residual_ua": drain_ua, "ig_residual_ua": gate_ua,
                     "id_std_ua": None, "ig_std_ua": None,
                     "point_variance": "unavailable",
                     "voltage_limited": point["voltage_limited"],
                     "ina_average_count": saved["ina_average_count"],
                     "acquisition_blocks_completed": 1})
                count += 1
                point = None
        completed = True
    except Exception as error:
        print("CAL_RESIDUAL_ABORT", str(error))
        raise
    finally:
        # The actual driver closes/deinitializes gate before drain and attempts
        # every output. Force GPIO low too, even if close reports a failure.
        try:
            if instrument is not None:
                instrument.close()
        finally:
            try:
                main.Pin(21, main.Pin.OUT, value=0)
            finally:
                main.Pin(20, main.Pin.OUT, value=0)
    if completed:
        emit("CAL_RESIDUAL_COMPLETE", {"points": count,
             "max_abs_id_residual_ua": maximum_drain,
             "max_abs_ig_residual_ua": maximum_gate,
             "outputs_low": True})


if __name__ == "__main__":
    run()
