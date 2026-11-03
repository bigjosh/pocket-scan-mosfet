"""Read native allocation costs with production reserves; no drive/file writes."""
import main
import gc

gc.collect()
start = gc.mem_free()
points = [[{"terminal_v": 0.0, "shunt_v": 0.0,
            "shunt_std_v": 0.0, "duty": 0.0} for index in range(199)]
          for role in range(2)]
gc.collect()
empty = gc.mem_free()
for role in range(2):
    for index in range(199):
        point = points[role][index]
        point["terminal_v"] = index * 0.1 + role * 0.001
        point["shunt_v"] = index * 0.000001 + role * 0.0000001
        point["shunt_std_v"] = index * 0.00000001
        point["duty"] = index / 300.0
gc.collect()
populated = gc.mem_free()
print("CAL_HEAP_PROBE", "before", start, "preallocated", empty,
      "populated", populated, "point_count", 398,
      "dict_and_list_bytes", start - empty,
      "additional_float_bytes", empty - populated)
points = point = None
gc.collect()
print("CAL_HEAP_PROBE_DONE", "free_heap", gc.mem_free(),
      "pins_low", main.Pin(20).value() == 0 and main.Pin(21).value() == 0)
