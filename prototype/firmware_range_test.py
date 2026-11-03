"""Fast open-fixture range check; NEVER saves the low-sample diagnostic map."""
if globals().get("_confirm_open_fixture_firmware_test") is not True:
    raise RuntimeError("Confirm open outputs and unbridged shunts before this test")
del _confirm_open_fixture_firmware_test

import time
import json
from hardware import Instrument
from engine import Engine
import model

instrument = Instrument()
try:
    engine = Engine(instrument)
    settings = dict(model.DEFAULT_SETTINGS)
    settings.update(batches=2, samples_per_batch=1)
    engine.start_calibration(settings)
    started = time.ticks_ms()
    while engine.state == "calibrating":
        engine.tick(time.ticks_ms())
        if time.ticks_diff(time.ticks_ms(), started) > 60000:
            engine.cancel()
            raise RuntimeError("Range check exceeded one minute")
        time.sleep_ms(2)
    assert engine.state == "complete", engine.error
    print("RANGE_CHECK", json.dumps({"elapsed_ms": time.ticks_diff(time.ticks_ms(), started),
          "saved": False, "channels": {role: {
              "points": len(engine.calibration["channels"][role]["points"]),
              "endpoint": engine.calibration["channels"][role]["points"][-1],
              "capability": engine.calibration["channels"][role].get("capability")}
              for role in ("drain", "gate")}}))
finally:
    instrument.close()
    print("OUTPUTS_DISABLED")
