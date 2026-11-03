"""Fast native full-UI calibration stress test. Synthetic outputs; NO file writes.

Import after a clean soft boot, then call run(). Production main reserves all
fixed buffers; the real display and touch/buttons are exercised. GP20/21 remain
low and no INA226/PWM Instrument is constructed. Wi-Fi stays inactive.
Default outputs reproduce the offset fixture and 16.5 V plateau. Pass
worst_case=True for 199 retained points on BOTH channels at an 18.8 V rail.
Pass maximum_grid=True as well to exercise all 311 slots at the 30 V setting.
Binary persistence is the only mode: run(binary=True) is the default. The save
transaction holds a real READ-ONLY calibration.bin file buffer, streams the
production binary codec into a hash/count sink, and never replaces or creates
a file. This exercises serialization/buffer pressure, not atomic filesystem
replacement. The canonical logical calibration fingerprint is also checked.
Engine sampling uses accelerated virtual time. Physical input debounce/touch
polling and live display throttling use actual wall time; each new point still
gets a display update so the complete GUI/raster-cache path is exercised.
"""
import main
from ram_payload import _HashWriter, _hex, calibration_hash
from calibration_binary import dump_calibration

SIM_TIMEOUT_MS = 20 * 60 * 1000
LIVE_PAINT_INTERVAL_MS = 150


class WallClockInputs:
    def __init__(self, delegate):
        self.delegate = delegate

    def poll(self, unused_virtual_now):
        # GT911 polling and physical button debounce must not be accelerated
        # with the synthetic sample clock. Inputs retains its normal cadence.
        return self.delegate.poll(main.time.ticks_ms())


class SimulationApp(main.App):
    def __init__(self, *args, **kwargs):
        main.App.__init__(self, *args, **kwargs)
        self._sim_paint_ms = None
        self._sim_paint_index = None
        self._sim_paints = 0

    def _render_live(self):
        wall_now = main.time.ticks_ms()
        index = self.engine._index
        if (self._sim_paint_ms is not None and index == self._sim_paint_index and
                main.time.ticks_diff(wall_now, self._sim_paint_ms) < LIVE_PAINT_INTERVAL_MS):
            # Keep live_dirty set: a later production tick will try again.
            return
        main.App._render_live(self)
        self._sim_paint_ms = wall_now
        self._sim_paint_index = index
        self._sim_paints += 1


class StopSimulation(BaseException):
    pass


class SimulatedInstrument:
    def __init__(self, worst_case, maximum_grid=False):
        self.worst_case = worst_case
        self.maximum_grid = maximum_grid
        self.pending = False
        self.duties = {"drain": 0.0, "gate": 0.0}
        self.reads = 0
        self.identity = {
            "synthetic": True,
            "drain": {"address": 0x44, "manufacturer": 0x5449,
                      "die": 0x2260, "config": 0x4127},
            "gate": {"address": 0x41, "manufacturer": 0x5449,
                     "die": 0x2260, "config": 0x4127},
        }

    def zero(self):
        self.duties["drain"] = 0.0
        self.duties["gate"] = 0.0
        self.pending = False

    close = zero

    def set_duty(self, role, fraction):
        if role not in self.duties or not 0.0 <= fraction <= 1.0:
            self.zero()
            raise ValueError("Invalid simulated drive")
        self.duties[role] = fraction

    def poll_alerts(self):
        pass

    def begin_measurement(self, count):
        if self.pending or count not in main.model.INA_AVERAGE_COUNTS:
            raise ValueError("Invalid synthetic trigger")
        self.pending = True

    def poll_measurement(self):
        if not self.pending:
            raise ValueError("No synthetic measurement pending")
        return True

    def read_pair(self):
        self.pending = False
        self.reads += 1
        result = {}
        for role in ("drain", "gate"):
            if self.worst_case:
                gain = 33.0 if self.maximum_grid else main.model.PWM_FULL_SCALE_V
                voltage = self.duties[role] * gain
            else:
                floor = 3.295 if role == "drain" else 0.020
                gain = 22.44 if role == "drain" else 25.74
                voltage = min(16.5, floor + gain * self.duties[role])
            # Small alternating ADC-like noise supplies finite support ranges.
            voltage = max(0.0, voltage + (0.00002 if self.reads & 1 else -0.00002))
            shunt = (0.000010 if role == "drain" else 0.001200) + voltage * 0.000010
            shunt_code = int(shunt / 0.0000025 + 0.5)
            shunt = shunt_code * 0.0000025
            bus_code = int((voltage + shunt) / 0.00125 + 0.5)
            bus = bus_code * 0.00125
            result[role] = {"bus_v": bus, "shunt_v": shunt,
                            "terminal_v": bus - shunt,
                            "bus_code": bus_code, "shunt_code": shunt_code}
        return result


class HashTransaction(_HashWriter):
    def __init__(self, owner):
        _HashWriter.__init__(self)
        self.owner = owner
        self.reader = open("calibration.bin", "rb")
        self.active = True
        self.byte_count = 0
        self.max_write_bytes = 0
        print("CAL_SIM_PREOPEN", "free_heap", main.gc.mem_free())

    def write(self, value):
        if not self.active:
            raise RuntimeError("Inactive simulation transaction")
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("Binary codec must emit byte buffers")
        if len(value) > 256:
            raise ValueError("Binary codec chunk exceeds 256 bytes")
        # Native json.dump can pass a buffer view through _MetadataWriter.
        # Normalize only this bounded chunk so typed views count bytes rather
        # than elements, without relying on CPython-only memoryview.nbytes.
        data = value if isinstance(value, bytes) else bytes(value)
        if len(data) > 256:
            raise ValueError("Binary codec chunk exceeds 256 bytes")
        count = _HashWriter.write(self, data)
        self.byte_count += count
        self.max_write_bytes = max(self.max_write_bytes, count)
        return count

    def abort(self):
        self.active = False
        reader, self.reader = self.reader, None
        if reader is not None:
            reader.close()

    def commit(self, candidate):
        if not self.active:
            raise RuntimeError("Inactive simulation transaction")
        print("CAL_SIM_COMMIT_BEGIN", "free_heap", main.gc.mem_free())
        try:
            # The binary integrity digest is distinct from the canonical
            # logical fingerprint used by scans. Record both; the caller
            # checks that the candidate's logical identity stayed unchanged.
            self.owner.digest = calibration_hash(candidate)
            dump_calibration(candidate, self)
            self.owner.binary_digest = _hex(self.hash.digest())
            self.owner.binary_bytes = self.byte_count
            self.owner.max_write_bytes = self.max_write_bytes
            self.owner.counts = (len(candidate["channels"]["drain"]["points"]),
                                 len(candidate["channels"]["gate"]["points"]))
            self.owner.commits += 1
        finally:
            self.abort()
        print("CAL_SIM_COMMIT_END", "free_heap", main.gc.mem_free())


class SimulationStore:
    def __init__(self):
        self.settings_saves = 0
        self.commits = 0
        self.digest = None
        self.binary_digest = None
        self.binary_bytes = 0
        self.max_write_bytes = 0
        self.counts = None

    def load_calibration(self):
        return main.storage.load_calibration()

    def save_settings(self, value):
        self.settings_saves += 1
        return main.model.validate_settings(value)  # RAM only.

    def begin_calibration_atomic(self):
        return HashTransaction(self)

    def save_calibration_atomic(self, value):
        raise RuntimeError("Simulation requires the preopened hash transaction")


def no_restart():
    raise StopSimulation("Restart requested; simulation stopped without resetting")


def run(worst_case=False, maximum_grid=False, binary=True):
    if binary is not True:
        raise ValueError("Calibration stress supports binary persistence only")
    app = wifi = cloud = instrument = None
    original_fail = main.Engine._fail
    started = main.time.ticks_ms()

    def traced_fail(engine, error):
        # Capture the ORIGINAL failing allocation's traceback before Engine
        # converts it into a UI-safe string. This never contacts any service.
        main.sys.print_exception(error)
        return original_fail(engine, error)

    main.Engine._fail = traced_fail
    try:
        for pin in (21, 20):
            main.Pin(pin, main.Pin.OUT, value=0)
        # Match production boot: the loader needs two contiguous packed arrays
        # before display, input and radio constructors consume the free heap.
        settings = main.storage.load_settings(migrate=False)
        settings["driver_supply_uv"] = 30000000 if maximum_grid else 18800000
        settings["source_voltage_uv"] = 3300000
        settings["ina_average_count"] = 64
        expected_points = len(main.model.calibration_targets_uv(settings))
        calibration = main.storage.load_calibration()
        main.model.validate_calibration(calibration)
        main.gc.collect()
        display = main.Display(raster_pool=main._display_buffer)
        inputs = WallClockInputs(main.Inputs())
        instrument = SimulatedInstrument(worst_case, maximum_grid)
        wifi = main.WifiManager()
        if wifi.pause() is not True:
            raise RuntimeError("Wi-Fi could not be disabled")
        scan_buffer = main.PackedScanBuffer(main._scan_bytes)
        cloud = main.RamCloudManager(scan_buffer, transport=main._upload_transport,
                                    utc_provider=main.scan_utc)
        cloud.prepare_calibration(calibration)
        store = SimulationStore()
        engine = main.Engine(instrument, clock_ms=lambda: engine._last_clock)
        app = SimulationApp(display, inputs, engine, settings,
                       calibration, store=store, error_logger=main.record_error,
                       wifi=wifi, cloud=cloud, calibration_on_disk=True,
                       scan_buffer=scan_buffer, restart=no_restart,
                       validated_calibration=calibration)
        # No boot frame or test store retains the old map during replacement.
        settings = calibration = None
        main.gc.collect()
        print("CAL_SIM_SETUP", "worst_case", worst_case,
              "maximum_grid", maximum_grid, "persistence", "binary",
              "free_heap", main.gc.mem_free(), "physical_pwm", "LOW",
              "input_clock", "wall", "live_paint_clock", "wall",
              "live_paint_interval_ms", LIVE_PAINT_INTERVAL_MS,
              "sampling_clock", "virtual", "virtual_tick_ms", 10,
              "wall_timeout_ms", SIM_TIMEOUT_MS)
        app.action("left")
        app._pause_wifi()
        app.action("advanced")
        app.action("calibrate")
        app.action("right")
        if app.engine.state != "calibrating":
            raise RuntimeError(getattr(app, "notice", "Simulation did not start"))
        if app.calibration is not None or app._calibration_writer is None:
            raise RuntimeError("Old map release/preopen lifecycle did not occur")
        print("CAL_SIM_BEGIN", "targets", app.engine._total,
              "ina_average_count", 64, "calibration_repeat_blocks", 3,
              "free_heap", main.gc.mem_free())
        now = main.time.ticks_ms()
        next_index = 10
        ticks = 0
        while app.engine.state == "calibrating":
            if ticks >= 300000:
                raise StopSimulation("Simulation exceeded 300000 cooperative ticks")
            if main.time.ticks_diff(main.time.ticks_ms(), started) >= SIM_TIMEOUT_MS:
                raise StopSimulation("Simulation exceeded the configured wall-time limit")
            now = (now + 10) % (1 << 30)
            app.tick(now)  # Real UI/input polling; no sleep and no INA reads.
            ticks += 1
            if app.engine._index >= next_index:
                print("CAL_SIM_PROGRESS", "index", app.engine._index,
                      "phase", app.engine._phase,
                      "counts", app.engine._cal_counts,
                      "free_heap", main.gc.mem_free(), "ticks", ticks,
                      "wall_elapsed_ms", main.time.ticks_diff(main.time.ticks_ms(), started),
                      "live_paints", app._sim_paints,
                      "virtual_engine_ms", app.engine._clock)
                next_index += 10
        if app.engine.state == "cancelled":
            raise StopSimulation("Cancelled; production calibration untouched")
        if (app.engine.state != "complete" or app.calibration is None or
                app.calibration is not app.engine.calibration or store.commits != 1):
            raise RuntimeError(app.engine.error or getattr(app, "notice", "Simulation failed"))
        main.model.validate_calibration(app.calibration, app.settings)
        if (app.calibration["metadata"]["ina_average_count"] != 64 or
                app.calibration["metadata"]["calibration_repeat_blocks"] != 3 or
                app.calibration["metadata"]["driver_supply_uv"] != (30000000 if maximum_grid else 18800000)):
            raise RuntimeError("Incorrect simulated calibration metadata")
        if calibration_hash(app.calibration) != store.digest:
            raise RuntimeError("Candidate changed after hash-only commit")
        if worst_case and store.counts != (expected_points, expected_points):
            raise RuntimeError("Worst-case simulation did not retain every point")
        for role in ("drain", "gate"):
            points = app.calibration["channels"][role]["points"]
            print("CAL_SIM_CHANNEL", role, "points", len(points),
                  "floor_v", points[0]["terminal_v"],
                  "ceiling_v", points[-1]["terminal_v"])
        # Keep both complete tables and the production UI alive across GC.
        main.gc.collect()
        print("CAL_SIM_PASS", "counts", store.counts, "sha256", store.digest,
              "binary_sha256", store.binary_digest, "binary_bytes", store.binary_bytes,
              "max_write_bytes", store.max_write_bytes,
              "free_heap_full_tables", main.gc.mem_free(),
              "elapsed_ms", main.time.ticks_diff(main.time.ticks_ms(), started),
              "live_paints", app._sim_paints,
              "settings_disk_writes", 0, "calibration_disk_writes", 0)
        app.message("SIMULATION PASSED. No files saved. PWM pins stayed low.", "home")
        app.render()
    except StopSimulation as error:
        print("CAL_SIM_STOP", str(error))
    except BaseException as error:
        main.sys.print_exception(error)
        print("CAL_SIM_FAIL", type(error).__name__, str(error))
        raise
    finally:
        main.Engine._fail = original_fail
        if app is not None:
            try:
                app.engine.cancel()
                app._release_calibration_candidate()
            except BaseException:
                pass
            writer = app._calibration_writer
            app._calibration_writer = None
            if writer is not None:
                try:
                    writer.abort()
                except BaseException:
                    print("CAL_SIM_CLEANUP", "read-only file close failed")
        if instrument is not None:
            instrument.zero()
        pins_low = True
        for pin in (21, 20):
            try:
                main.Pin(pin, main.Pin.OUT, value=0)
                pins_low = pins_low and main.Pin(pin).value() == 0
            except BaseException:
                pins_low = False
        for service in (cloud, wifi):
            if service is not None:
                try:
                    service.pause()
                except BaseException:
                    print("CAL_SIM_CLEANUP", "service pause failed")
        print("CAL_SIM_DONE", "physical_pins_low", pins_low,
              "production_files_untouched", True, "soft_reset", False)


if __name__ == "__main__":
    run()
