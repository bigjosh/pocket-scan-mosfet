"""Native calibration/lookup/maximum-payload test with the production heap.

Run on a fresh Pico interpreter after compiling this helper and
direct_memory_smoke. Uses the real display/UI and main's boot reservations,
but synthetic measurements only: no PWM objects, network, or file writes.
The TLS reserve is deliberately kept throughout calibration for extra pressure.
The binary save method is replaced only for this test and restored in finally.
"""

import main
import gc
import current_baseline
import direct_memory_smoke as smoke
import model
from engine import Engine
from ram_scan import PackedScanBuffer
from ui import App


class SyntheticInstrument(smoke.SyntheticInstrument):
    identity = {"drain": {"address": 0x44, "die": 0x2260},
                "gate": {"address": 0x40, "die": 0x2281}}

    def read_codes_into(self, codes):
        self.reads += 1
        # Include rail clipping and voltage-dependent signed input offsets.
        codes[0] = min(13000, int((3.25 + 22.44 * self.drain) / 0.00125))
        codes[1] = -2 + codes[0] // 5000
        codes[2] = min(83200, int(25.74 * self.gate / 0.0001953125))
        codes[3] = 3 + codes[2] // 6400


class MemorySettings(smoke.NoFiles):
    def save_settings(self, settings):
        # Exercise normalization and allocation but never touch the filesystem.
        self.writes += 1
        return model.validate_settings(settings)


def run():
    smoke._pins_low()
    original_save = current_baseline.Baseline.save
    marks = [0]

    def fake_save(table, path):
        assert path == "current-baseline.bin" and table.available
        table._validate()
        assert table.sha256 and len(table.sha256) == 64
        marks[0] += 1

    try:
        current_baseline.Baseline.save = fake_save
        display = smoke.SmokeDisplay()
        inputs, instrument = smoke.SyntheticInputs(), SyntheticInstrument()
        store, restart = MemorySettings(), smoke.RestartCounter()
        table = current_baseline.Baseline(main._baseline_bytes)
        buffer = PackedScanBuffer(main._scan_bytes)

        def factory(device, settings):
            return current_baseline.Calibrator(device, settings,
                    buffer=main._baseline_workspace, clock_ms=lambda: now[0])

        now = [0]
        app = App(display, inputs, Engine(instrument, baseline=table),
                  store=store, scan_buffer=buffer, restart=restart,
                  calibration_factory=factory)
        app.tick(0)
        # Enter the normal calibration UI while deliberately retaining TLS.
        app.draft = dict(app.settings)
        app.advanced_entry = dict(app.settings)
        app._settings_path = True
        app.goto("advanced")
        app.tick(1)
        app.action("calibrate")
        app.tick(2)
        assert app.screen == "calibration_confirm" and instrument.commands == 0
        app.action("right")
        before = smoke._heap()
        print("BASELINE_MEMORY_BEGIN", before)
        for step in range(20000):
            now[0] = 100 + step * 250
            app.tick(now[0])
            if app.screen == "calibration_done":
                break
            if app.screen in ("calibration_error", "notice", "restarting"):
                raise AssertionError("Calibration UI failed: " + str(
                    app._calibration_error or getattr(app, "notice", None)))
        else:
            raise AssertionError("Calibration exceeded bounded tick count")
        assert marks[0] == 1 and store.writes == 1 and restart.count == 0
        result = app._calibration.result
        assert result.available and app._calibration.shutdown_succeeded
        assert instrument.drain == instrument.gate == 0
        assert len(result._data) == current_baseline.BUFFER_BYTES
        after = smoke._heap()
        print("BASELINE_MEMORY_SWEEP_OK", instrument.reads, result.count(0),
              result.count(1), before, after)
        # Long lookup run uses the same packed table without retained samples.
        for index in range(20000):
            bus_uv = (index % 16000) * 1250
            result.offset_nv(0, bus_uv)
            result.offset_nv(1, bus_uv)
        print("BASELINE_MEMORY_LOOKUP_OK", smoke._heap())
        # Exercise corrected metadata with all4096 packedrecords+twohashstreams.
        smoke._full_payload(buffer, display, smoke.NoFiles(), app,
                            current_correction=result.metadata(app.settings))
        assert main._tls_reserved and len(buffer) == 0
        print("BASELINE_MEMORY_PASS", smoke._heap())
    except BaseException as error:
        print("BASELINE_MEMORY_FAIL", type(error).__name__)
        raise
    finally:
        current_baseline.Baseline.save = original_save
        main._scan_timing.stop()
        smoke._pins_low()
