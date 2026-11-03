"""Lean native save/readback regression; real UI/calibration, no drive/network.

Preload main and its Display. All test files stay in a fresh isolated folder.
No settings, calibration, real scans or network credentials are written.
"""
import main
import os
import gc
import hashlib
import binascii
from archive import UploadSource, COLUMNS
from engine import OverrangeError


class QuietInputs:
    def poll(self, now):
        return []


class MemoryConfig:
    def load_json(self, path):
        return None


class NoDrive:
    def zero(self):
        main.Pin(20, main.Pin.OUT, value=0)
        main.Pin(21, main.Pin.OUT, value=0)

    def set_duty(self, role, duty):
        self.zero()

    def read_pair(self):
        raise OverrangeError('drain', 'current_limit', None, 1, .005, 'A', .0055)


def verify_saved(archive, scan_id, count):
    # Keep diagnostic parser/hash objects in a separate frame so conservative
    # GC cannot retain them across the next synthetic acquisition.
    info = main.storage.load_json(archive._path(scan_id, '.meta'))
    source = UploadSource(archive._path(scan_id, '.draft'))
    try:
        digest, size = hashlib.sha256(), 0
        while True:
            block = source.read(256)
            if not block:
                break
            digest.update(block)
            size += len(block)
        assert source.point_count == count
        assert size == info['wire_size']
        assert binascii.hexlify(digest.digest()).decode() == info['sha256']
    finally:
        source.close()


directory = '__finalize_smoke'
os.mkdir(directory)  # Refuse an existing directory.
archive = app = None
identifiers = []
try:
    hardware = NoDrive()
    hardware.zero()
    calibration = main.storage.load_json('calibration.json')
    # Synthetic scan settings use the existing calibration rail, regardless of
    # a user's not-yet-calibrated hardware-settings draft. Never save this map.
    settings = dict(main.model.DEFAULT_SETTINGS)
    settings.update(driver_supply_uv=main.model.volts_to_uv(calibration['metadata']['driver_supply_v']),
                    drain_step_uv=100000, gate_step_uv=100000,
                    batches=2, samples_per_batch=2)
    engine = main.Engine(hardware)
    archive = main.Archive(directory, 'face')
    cloud = main.CloudManager(archive, store=MemoryConfig())
    app = main.App(display, QuietInputs(), engine, settings, calibration,
                   cloud=cloud, plot_buffers=main._plot_buffers)
    now = 0
    drains = main.model.sweep_uv(settings['drain_start_uv'], settings['drain_stop_uv'], settings['drain_step_uv'])
    gates = main.model.sweep_uv(settings['gate_start_uv'], settings['gate_stop_uv'], settings['gate_step_uv'])
    total = len(drains) * len(gates)
    for count in (0, 1, len(drains), total - 1, total):
        app.action('right')
        for unused in range(8):
            now += 20
            app.tick(now)
            if app.screen != 'preparing':
                break
        assert engine.state == 'scanning'
        scan_id = archive._current
        identifiers.append(scan_id)
        last = None
        for index in range(count):
            curve, offset = divmod(index, len(drains))
            last = {key: 0.0 for key in COLUMNS}
            vd, vg = main.model.volts_from_uv(drains[offset]), main.model.volts_from_uv(gates[curve])
            last.update(index=index, curve=curve, requested_vds=vd,
                        requested_vgs=vg, vds=vd, vgs=vg,
                        batches_completed=settings['batches'], drain_voltage_limited=False,
                        gate_voltage_limited=False, total=total, batchmeans=[])
            cloud.record_point(last)
            app._add_point(last)
        engine._index, engine._last_completed, engine.latest = count, last, last
        original_tick = engine.tick
        if count == total:
            engine.state, engine.shutdown_succeeded = 'complete', True
            engine.tick = lambda unused: [('complete', {'points': total})]
        else:
            engine._phase, engine._deadline = 'sample', 0
        now += 20
        try:
            app.tick(now)
        finally:
            engine.tick = original_tick
        assert app._archive_ready, app.notice
        assert app.screen == ('complete' if count == total else 'partial')
        gc.collect()
        verify_saved(archive, scan_id, count)
        print('FINALIZE_SMOKE_PASS', count, app.screen, 'hash_and_readback_verified')
        gc.collect()
        app.action('left')
    print('FINALIZE_SMOKE_DONE', '5 cases passed; outputs low; no network')
finally:
    main.Pin(20, main.Pin.OUT, value=0)
    main.Pin(21, main.Pin.OUT, value=0)
    if archive is not None:
        archive.discard_scan()
    allowed = set(identifier + suffix for identifier in identifiers
                  for suffix in ('.writing', '.draft', '.meta', '.meta.tmp'))
    names = os.listdir(directory)
    assert all(name in allowed for name in names)
    for name in names:
        os.remove(directory + '/' + name)
    os.rmdir(directory)
    print('OUTPUTS_DISABLED')
