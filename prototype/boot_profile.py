"""One real startup, stopping after its first UI tick; no scan or file writes.

Times wrap existing production calls and print only fixed labels and numbers.
Import main first with a host-supplied timer to account for imports separately.
The normal main finally block closes hardware/radio; the host restarts the UI
after retrieving the measurements. Profiling wrappers add some overhead.
"""
import main


class ProfileDone(BaseException):
    pass


def run():
    metrics = {}
    originals = []

    def wrap(owner, name, label):
        original = getattr(owner, name)

        def timed(*args, **kwargs):
            started = main.time.ticks_us()
            try:
                return original(*args, **kwargs)
            finally:
                elapsed = main.time.ticks_diff(main.time.ticks_us(), started)
                previous = metrics.get(label, (0, 0))
                metrics[label] = (previous[0] + 1, previous[1] + elapsed)

        originals.append((owner, name, original))
        setattr(owner, name, timed)

    original_app = main.App

    class ProfileApp(original_app):
        def __init__(self, *args, **kwargs):
            started = main.time.ticks_us()
            original_app.__init__(self, *args, **kwargs)
            metrics["app_construct"] = (1, main.time.ticks_diff(main.time.ticks_us(), started))

        def tick(self, now):
            started = main.time.ticks_us()
            original_app.tick(self, now)
            metrics["first_ui_tick"] = (1, main.time.ticks_diff(main.time.ticks_us(), started))
            if self.screen != "home":
                raise RuntimeError("Startup did not reach Home")
            raise ProfileDone()

    wrap(main.storage, "load_settings", "load_settings")
    wrap(main.storage, "load_json", "all_json_loads")
    if hasattr(main.storage, "load_calibration"):
        wrap(main.storage, "load_calibration", "load_calibration")
    wrap(main.model, "validate_calibration", "full_calibration_validation")
    wrap(main.RamCloudManager, "prepare_calibration", "calibration_fingerprint")
    for name in ("Display", "Instrument", "Inputs", "WifiManager", "RamCloudManager"):
        wrap(main, name, name)
    main.App = ProfileApp
    started = main.time.ticks_us()
    completed = False
    try:
        main.run()
    except ProfileDone:
        completed = True
    finally:
        main.App = original_app
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)
        main.Pin(21, main.Pin.OUT, value=0)
        main.Pin(20, main.Pin.OUT, value=0)
    if not completed:
        raise RuntimeError("Startup profile did not complete")
    print("BOOT_PROFILE", main.storage.json.dumps({
        "metrics_count_us": metrics,
        "run_including_cleanup_us": main.time.ticks_diff(main.time.ticks_us(), started),
        "home_painted": True, "outputs_low": True, "scan_started": False,
        "overlapping_timings": True}))


if __name__ == "__main__":
    run()
