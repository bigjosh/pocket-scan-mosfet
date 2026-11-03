"""Full UI calibration at the configured rail, simulated inputs/outputs only.

Preload main and Display. The installed calibration and settings are untouched;
temporary calibration saves use a fresh isolated directory, then are removed.
"""
import gc
import os
import sys
import main


class QuietInputs:
    def poll(self, now):
        return []


class SimulatedInstrument:
    identity = {'synthetic': True}

    def __init__(self):
        self.duties = {'drain': 0.0, 'gate': 0.0}

    def zero(self):
        self.duties = {'drain': 0.0, 'gate': 0.0}
        main.Pin(20, main.Pin.OUT, value=0)
        main.Pin(21, main.Pin.OUT, value=0)

    def set_duty(self, role, fraction):
        self.duties[role] = fraction

    def read_pair(self):
        return {role: {'terminal_v': duty * main.model.PWM_FULL_SCALE_V,
                       'bus_v': duty * main.model.PWM_FULL_SCALE_V, 'shunt_v': 0.0}
                for role, duty in self.duties.items()}


class IsolatedStore:
    saves = 0

    def load_json(self, path):
        assert path == 'calibration.json'
        saved = main.storage.load_json(directory + '/' + path)
        return saved if saved is not None else main.storage.load_json(path)

    def save_settings(self, value):
        return main.model.validate_settings(value)

    def begin_json_atomic(self, path):
        assert path == 'calibration.json'
        return CountedTransaction(main.storage.begin_json_atomic(directory + '/' + path), self)

    def save_json_atomic(self, path, value):
        assert path == 'calibration.json'
        print('CAL_MEMORY_SAVE', gc.mem_free())
        try:
            main.storage.save_json_atomic(directory + '/' + path, value)
        except Exception as error:
            sys.print_exception(error)
            raise
        self.saves += 1


class CountedTransaction:
    def __init__(self, transaction, store):
        self.transaction, self.store = transaction, store

    def commit(self, value):
        print('CAL_MEMORY_SAVE', gc.mem_free())
        try:
            self.transaction.commit(value)
        except Exception as error:
            sys.print_exception(error)
            raise
        self.store.saves += 1

    def abort(self):
        self.transaction.abort()


directory = '__calibration_memory_test'
os.mkdir(directory)
hardware = SimulatedInstrument()
hardware.zero()
app = None
try:
    store = IsolatedStore()
    app = main.App(display, QuietInputs(), main.Engine(hardware),
                   main.storage.load_settings(), main.storage.load_json('calibration.json'),
                   store=store, plot_buffers=main._plot_buffers, calibration_on_disk=True)
    for attempt in range(2):
        app.action('left')
        app.action('advanced')
        app.action('calibrate')
        gc.collect()
        print('CAL_MEMORY_BEGIN', attempt, app.settings['driver_supply_uv'], gc.mem_free())
        app.action('right')
        assert app.engine.state == 'calibrating', app.notice
        for now in range(0, 1000000, 20):
            app.tick(now)
            if app.engine._phase == 'drive' and app.engine._index % 50 == 0:
                gc.collect()
                print('CAL_MEMORY_PROGRESS', app.engine._index, gc.mem_free())
            if app.engine.state != 'calibrating':
                break
        assert app.engine.state == 'complete', app.engine.error
        assert store.saves == attempt + 1, app.notice
        assert app.calibration is app.engine.calibration
        main.model.validate_calibration(app.calibration, app.settings)
        gc.collect()
        print('CAL_MEMORY_PASS', attempt,
              len(app.calibration['channels']['drain']['points']),
              len(app.calibration['channels']['gate']['points']), gc.mem_free())
        # Release completed result UI and navigate back to Home.
        app.action('right')
        app.action('right')
        app.action('right')
        assert app.screen == 'home', app.screen
    # Cancel a third calibration, then reload the committed map after the
    # engine event frame has unwound. Never replace the actual device file.
    app.action('left')
    app.action('advanced')
    app.action('calibrate')
    app.action('right')
    assert app.engine.state == 'calibrating', app.notice
    assert app.calibration is None
    app.action('left')
    app.tick(1000000)
    app.tick(1000020)
    assert app.calibration is not None
    assert app.engine.calibration is app.calibration
    assert len(app.calibration['channels']['gate']['points']) == 199
    assert store.saves == 2
    print('CAL_MEMORY_CANCEL_RESTORE_PASS')
    print('CAL_MEMORY_DONE', 'two complete simulated calibrations and saves; physical outputs low')
finally:
    hardware.zero()
    if app is not None and app._calibration_writer is not None:
        app._calibration_writer.abort()
        app._calibration_writer = None
    allowed = ('calibration.json', 'calibration.json.tmp')
    names = os.listdir(directory)
    assert all(name in allowed for name in names)
    for name in names:
        os.remove(directory + '/' + name)
    os.rmdir(directory)
    print('OUTPUTS_DISABLED')
