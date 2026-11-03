"""Time SCAN preparation using real UI/storage and physically zero outputs.

Preload `import main; display=main.Display(raster_pool=main._display_buffer)`.
Copies accepted scan files into a new isolated directory, never modifies the
real archive, and removes only those diagnostic copies/drafts on exit. Wi-Fi
is held off and no upload is attempted. Stop after the first logical command;
the instrument below never creates PWM or I2C peripherals or drives a pin high.
Works against both the old synchronous UI and the staged preparation UI.
"""
import main
import os
import time
import json
import hashlib
import archive as archive_module
from archive import Archive
from cloud import CloudManager
from engine import Engine
from ui import App
from wifi import WifiManager
import storage

DIRECTORY = '__startup_profile'
ATTEMPTS = globals().get('STARTUP_PROFILE_ATTEMPTS', 1)
assert ATTEMPTS in (1, 2)
assert DIRECTORY not in os.listdir(), 'Refusing an existing diagnostic directory'
if 'display' not in globals():
    display = main.Display(raster_pool=main._display_buffer)


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            piece = stream.read(512)
            if not piece:
                return value.digest()
            value.update(piece)


class Inputs:
    def poll(self, now):
        return []


class Config:
    def load_json(self, path):
        return None
    def save_json_atomic(self, *args):
        raise AssertionError('Do not save configuration in this diagnostic')


class NoDriveInstrument:
    identity = {'diagnostic': 'physical outputs held low'}
    def __init__(self):
        self.first_command = None
        self.zero()
    def zero(self):
        main.Pin(20, main.Pin.OUT, value=0)
        main.Pin(21, main.Pin.OUT, value=0)
    def set_duty(self, role, fraction):
        assert role in ('drain', 'gate') and 0 <= fraction <= 1
        if self.first_command is None:
            self.first_command = time.ticks_us()
        self.zero()
    def read_pair(self):
        raise AssertionError('Stop profiling before any measurement phase')


enabled = False
metrics = {}
views = []
hash_calls = 0
original_hash = archive_module.calibration_hash


def measured_hash(value):
    global hash_calls
    hash_calls += 1
    started = time.ticks_us()
    try:
        return original_hash(value)
    finally:
        if enabled:
            metrics['calibration_hash_us'] = metrics.get('calibration_hash_us', 0) + time.ticks_diff(time.ticks_us(), started)


class ProfileArchive(Archive):
    def scan_capacity(self, settings):
        started = time.ticks_us()
        try:
            return Archive.scan_capacity(self, settings)
        finally:
            if enabled:
                metrics['capacity_calls'] = metrics.get('capacity_calls', 0) + 1
                metrics['capacity_us'] = metrics.get('capacity_us', 0) + time.ticks_diff(time.ticks_us(), started)


class ProfileApp(App):
    def render(self):
        started = time.ticks_us()
        screen = self.screen
        App.render(self)
        if enabled:
            views.append([screen, started, time.ticks_us()])


hardware = NoDriveInstrument()
wifi = cloud = app = None
created = False
original_files = {}
try:
    # Snapshot user data before any isolated file work. No credential file is read.
    for name in os.listdir('scans'):
        original_files[name] = digest('scans/' + name)
    before = [digest(name) for name in ('settings.json', 'calibration.json')]
    os.mkdir(DIRECTORY)
    created = True
    for name in original_files:
        if not name.endswith(('.pending', '.sent', '.meta')):
            continue
        with open('scans/' + name, 'rb') as source, open(DIRECTORY + '/' + name, 'wb') as destination:
            while True:
                piece = source.read(512)
                if not piece:
                    break
                destination.write(piece)
    archive_module.calibration_hash = measured_hash
    archive = ProfileArchive(directory=DIRECTORY)
    cloud = CloudManager(archive, main._upload_transport, Config(), '__startup_memory_only')
    wifi = WifiManager()
    wifi.pause()
    settings = storage.load_settings()
    assert settings['source_voltage_uv'] == 0
    calibration = storage.load_json('calibration.json')
    engine = Engine(hardware)
    app = ProfileApp(display, Inputs(), engine, settings, calibration,
                     cloud=cloud, wifi=wifi, plot_buffers=main._plot_buffers)
    app.render()
    boot_hash_calls = hash_calls
    reports = []
    for attempt in range(ATTEMPTS):
        views = []
        metrics = {}
        hardware.first_command = None
        prior_hashes = hash_calls
        app._now_ms = time.ticks_ms()
        enabled = True
        started = time.ticks_us()
        app.action('right')
        returned = time.ticks_us()
        while hardware.first_command is None:
            app.tick(time.ticks_ms())
            assert time.ticks_diff(time.ticks_us(), started) < 15000000, 'Preparation did not reach logical drive'
            assert app.screen in ('preparing', 'scan'), 'Preparation failed'
            time.sleep_ms(2)
        enabled = False
        report = {'attempt': attempt + 1,
                  'action_return_ms': time.ticks_diff(returned, started) / 1000,
                  'first_logical_command_ms': time.ticks_diff(hardware.first_command, started) / 1000,
                  'calibration_hash_calls_during_start': hash_calls - prior_hashes,
                  'metrics': metrics,
                  'views': [{'screen': v[0], 'start_ms': time.ticks_diff(v[1], started) / 1000,
                             'complete_ms': time.ticks_diff(v[2], started) / 1000} for v in views]}
        if any(v[0] == 'preparing' for v in views):
            scan_view = next(v for v in views if v[0] == 'scan')
            assert time.ticks_diff(hardware.first_command, scan_view[2]) >= 0
            report['scan_view_painted_before_logical_drive'] = True
        reports.append(report)
        # The logical instrument always forces physical zero, including cancel.
        app.action('left')
        app.tick(time.ticks_ms())
        assert app.screen == 'home' and not app._archive_active
        wifi.pause()
    cancelled_stages = []
    if hasattr(app, '_scan_prep'):
        for stage in (1, 2, 3):
            hardware.first_command = None
            app.action('right')
            app.tick(time.ticks_ms())  # Paint before any storage preparation.
            while app._scan_prep < stage and app.screen == 'preparing':
                app.tick(time.ticks_ms())
            assert app.screen == 'preparing' and app._scan_prep == stage
            app.action('left')
            app.tick(time.ticks_ms())
            assert hardware.first_command is None
            assert app.screen == 'home' and not app._archive_active
            assert archive._current is None
            assert not any(name.endswith(('.writing', '.draft')) for name in os.listdir(DIRECTORY))
            cancelled_stages.append(stage)
            wifi.pause()
    assert before == [digest(name) for name in ('settings.json', 'calibration.json')]
    assert sorted(original_files) == sorted(os.listdir('scans'))
    assert all(value == digest('scans/' + name) for name, value in original_files.items())
    print('STARTUP_PROFILE', json.dumps({'attempts': reports, 'boot_calibration_hash_calls': boot_hash_calls,
          'cancelled_preparation_stages': cancelled_stages,
          'physical_outputs_always_low': True, 'wifi_held_off': True,
          'original_scan_files_preserved': len(original_files), 'settings_calibration_preserved': True}))
finally:
    enabled = False
    archive_module.calibration_hash = original_hash
    try:
        if cloud is not None:
            cloud.pause()
            cloud.discard_scan()
        if wifi is not None:
            wifi.pause()
    finally:
        hardware.zero()
        if created:
            for name in os.listdir(DIRECTORY):
                os.remove(DIRECTORY + '/' + name)
            os.rmdir(DIRECTORY)
        print('PROFILE_OUTPUTS_DISABLED')
