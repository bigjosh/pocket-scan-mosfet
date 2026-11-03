"""Resume-only inspection of the isolated finalize smoke failure; never drive."""
import archive as archive_module
import sys
import os
import gc

original_open = open


def traced_open(path, mode='r'):
    try:
        return original_open(path, mode)
    except Exception as error:
        print('PREPARE_OPEN_FAILURE', mode)
        sys.print_exception(error)
        raise


archive_module.open = traced_open
assert directory == '__finalize_smoke'
os.mkdir(directory)
try:
    gc.collect()
    archive.begin_scan(settings, calibration, now)
    print('PREPARE_DIRECT_PASS')
except Exception as error:
    sys.print_exception(error)
finally:
    archive.discard_scan()
    assert not os.listdir(directory)
    os.rmdir(directory)
    archive_module.open = original_open
