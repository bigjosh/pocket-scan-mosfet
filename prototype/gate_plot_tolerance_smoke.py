"""Verify native float32 plot classification; never drive outputs or write files."""
import model
from ram_scan import PackedScanBuffer, GATE_OFF_TARGET
from ui import App

assert model.GATE_PLOT_TOLERANCE_V == 0.100
assert model.VOLTAGE_TOLERANCE_V == 0.020
buffer = PackedScanBuffer(capacity=1)
row = {"vds": 1.0, "vgs": 0.0, "id_a": 0.0, "ig_a": 0.0,
       "requested_vgs": 0.0}
checked = 0
for target in (-3.3, -1.0, 0.0, 1.0, 18.8):
    for difference, off_target in ((-0.1001, True), (-0.1, False),
                                    (-0.05, False), (0.0, False),
                                    (0.05, False), (0.1, False), (0.1001, True)):
        row["requested_vgs"] = target
        row["vgs"] = target + difference
        buffer.clear()
        buffer.append(row)
        assert bool(buffer[0][4] & GATE_OFF_TARGET) == off_target
        assert App._gate_off_target(row) == off_target
        checked += 1
print("GATE_PLOT_TOLERANCE_PASS", checked, "cases; inclusive +/-100mV; calibration +/-20mV")
