"""Zero-drive stress test for fixed display/plot storage on the actual Pico."""
import gc
import json
import time
from hardware import Instrument
from display import Display
from inputs import Inputs
from engine import Engine
from ui import App, BG, INK, TEAL
import model
import storage

instrument = Instrument()
try:
    display = Display()
    inputs = Inputs()
    app = App(display, inputs, Engine(instrument), storage.load_settings(),
              storage.load_json("calibration.json"))
    app.render()
    data_id, curves_id = id(app.rows._data), id(app.curves._data)
    flags_id = id(app.limit_flags._data)
    pool_id = id(display._raster_pool)
    started = time.ticks_ms()
    for index in range(200):
        # Exercise eviction with changing values and foreground/background keys.
        display.text("%05d %08x MEMORY" % (index, index * 719), 16, 60,
                     INK if index % 2 else TEAL, bg=BG + index % 7, scale=2)
        temporary = [{"sample": index + j, "value": j / 1000} for j in range(20)]
    row = {"vds": 0.0, "vgs": 0.0, "id_a": 0.0, "ig_a": 0.0,
           "id_std_a": 0.0, "ig_std_a": 0.0, "curve": 0}
    for repeat in range(3):
        app.clear_results()
        for index in range(model.MAX_POINTS):
            row["vds"] = index / model.MAX_POINTS * 2
            row["drain_voltage_limited"] = index % 2 == 0
            row["gate_voltage_limited"] = index % 3 == 0
            app._add_point(row)
        assert len(app.rows) == model.MAX_POINTS * 2
        assert len(app.rows._data) == 32768
        assert len(app.curves) == model.MAX_POINTS
        assert len(app.limit_flags) == model.MAX_POINTS
        assert id(app.limit_flags._data) == flags_id
        assert app.rows[(model.MAX_POINTS - 1) * 2] > 1.99
        assert id(app.rows._data) == data_id and id(app.curves._data) == curves_id
    assert id(display._raster_pool) == pool_id
    gc.collect()
    # Runtime operations use small allocations; large buffers are reserved at
    # boot. Free RAM need not be contiguous on MicroPython's nonmoving heap.
    probe = bytearray(2048)
    del probe
    gc.collect()
    app.clear_results()
    app.goto("home")
    app.render()
    print("MEMORY_TEST", json.dumps({"max_points": model.MAX_POINTS,
          "buffer_reuses": 3, "contiguous_2k_verified": True,
          "free_heap_bytes": gc.mem_free(), "elapsed_ms": time.ticks_diff(time.ticks_ms(), started)}))
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
