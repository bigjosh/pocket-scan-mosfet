"""Fixed-capacity float32 calibration tables; JSON remains a table of objects.

Only four numeric cells are retained for each point. Mapping snapshots are for
diagnostics/streaming JSON, never the acquisition or interpolation hot paths.
"""

from array import array

MAX_CALIBRATION_POINTS = 311
FIELDS = ("terminal_v", "shunt_v", "shunt_std_v", "duty")


def float_buffer(count):
    # MicroPython allocates an array once when the initializer has a known
    # length. range avoids a second full-sized bytes/list temporary; its small
    # integer values are overwritten before the buffer becomes usable.
    values = array("f", range(count))
    for index in range(count):
        values[index] = 0.0
    return values


class PackedPoints:
    def __init__(self, capacity):
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 1 <= capacity <= MAX_CALIBRATION_POINTS:
            raise ValueError("Invalid calibration capacity")
        self.capacity = capacity
        self._values = float_buffer(capacity * 4)
        self._count = 0
        self._zero_min = self._zero_max = None
        self._drive_v = self._drive_duty = None

    def __len__(self):
        return self._count

    def _index(self, index):
        if not isinstance(index, int):
            raise TypeError("Calibration index must be an integer")
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError("Calibration index out of range")
        return index

    def append(self, terminal_v, shunt_v, shunt_std_v, duty):
        if self._count >= self.capacity:
            raise ValueError("Calibration capacity exceeded")
        if self._drive_v is not None:
            raise ValueError("Cannot append after calibration endpoint")
        self._count += 1
        self.replace(self._count - 1, terminal_v, shunt_v, shunt_std_v, duty)

    def replace(self, index, terminal_v, shunt_v, shunt_std_v, duty):
        offset = self._index(index) * 4
        self._values[offset] = terminal_v
        self._values[offset + 1] = shunt_v
        self._values[offset + 2] = shunt_std_v
        self._values[offset + 3] = duty

    def set_zero_bounds(self, minimum, maximum):
        if not self._count:
            raise ValueError("Calibration has no zero endpoint")
        self._zero_min, self._zero_max = minimum, maximum

    def set_drive_endpoint(self, voltage, duty):
        if not self._count:
            raise ValueError("Calibration has no drive endpoint")
        self._drive_v, self._drive_duty = voltage, duty

    def value(self, index, field, default=None):
        index = self._index(index)
        for column in range(4):
            if field == FIELDS[column]:
                return self._values[index * 4 + column]
        if index == 0:
            if field == "zero_min_v" and self._zero_min is not None:
                return self._zero_min
            if field == "zero_max_v" and self._zero_max is not None:
                return self._zero_max
        if index == self._count - 1:
            if field == "drive_v" and self._drive_v is not None:
                return self._drive_v
            if field == "drive_duty" and self._drive_duty is not None:
                return self._drive_duty
        return default

    def __getitem__(self, index):
        """Return an independent snapshot; modifying it never edits the table."""
        index = self._index(index)
        result = {field: self._values[index * 4 + column] for column, field in enumerate(FIELDS)}
        if index == 0 and self._zero_min is not None:
            result["zero_min_v"], result["zero_max_v"] = self._zero_min, self._zero_max
        if index == self._count - 1 and self._drive_v is not None:
            result["drive_v"], result["drive_duty"] = self._drive_v, self._drive_duty
        return result

    def __iter__(self):
        for index in range(self._count):
            yield self[index]


def point_value(points, index, field, default=None):
    if isinstance(points, PackedPoints):
        return points.value(index, field, default)
    return points[index].get(field, default)
