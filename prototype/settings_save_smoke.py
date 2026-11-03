"""Exercise real Settings -> OK with no scan drive or network upload.

Uses normal boot objects/display and saves the existing settings unchanged.
The diagnostic catches restart so the host can inspect the result and then
restart normal main.py. Only the Settings save may write device data.
"""
import main


class ProbeDone(BaseException):
    pass


class NoInput:
    def poll(self, now):
        return ()


def run():
    original_app = main.App
    original_logger = main.record_error
    original_restart = main.request_restart
    phase = [0]
    result = {"restart_requested": False, "action_failed": False}

    def log_error(context, error):
        result["action_failed"] = True
        print("SETTINGS_ERROR", context)
        main.sys.print_exception(error)

    def restarted():
        result["restart_requested"] = True
        raise ProbeDone()

    class ProbeApp(original_app):
        def __init__(self, *args, **kwargs):
            original_app.__init__(self, *args, **kwargs)
            self.inputs = NoInput()

        def tick(self, now):
            if phase[0] == 1:
                self.action("left")
            elif phase[0] == 2:
                self.action("right")
            original_app.tick(self, now)
            print("SETTINGS_PHASE", phase[0], self.screen, "FREE", main.gc.mem_free())
            assert all(self.engine.instrument._pwms[role].duty_u16() == 0
                       for role in ("gate", "drain"))
            if phase[0] == 2 or result["action_failed"]:
                raise ProbeDone()
            phase[0] += 1

    main.App, main.record_error, main.request_restart = ProbeApp, log_error, restarted
    try:
        main.run()
    except ProbeDone:
        pass
    finally:
        main.App, main.record_error, main.request_restart = original_app, original_logger, original_restart
    print("SETTINGS_RESULT", main.storage.json.dumps(result))
    assert result["restart_requested"] and not result["action_failed"]


if __name__ == "__main__":
    run()
