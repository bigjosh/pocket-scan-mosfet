"""Verify native MicroPython traceback streaming without touching the display."""
import diagnostics
import sys

diagnostics.LOG_PATH = "diagnostics-self-test.log"
try:
    raise ValueError("Intentional diagnostics self-test")
except ValueError as error:
    assert diagnostics.record_error("self-test only", error)
with open(diagnostics.LOG_PATH) as stream:
    contents = stream.read(1024)
assert "Traceback" in contents
assert "Intentional diagnostics self-test" in contents
print("DIAGNOSTICS_TEST_PASSED")
print(contents)
diagnostics.LOG_PATH = "last_error.log"
