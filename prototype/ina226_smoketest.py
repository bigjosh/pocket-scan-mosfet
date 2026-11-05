"""Read-only mixed INA226 drain / INA228 gate discovery (historical filename).

I2C1: SDA GP18 (Pico header 24), SCL GP19 (header 25), 100 kHz.
No sensor configuration/calibration writes, PWM, Wi-Fi or ADC operations.
No current calculation: the fitted shunt value must be verified separately.
Gate 0x40 uses the INA228 register map; other addresses use INA226's map.
TI register references: https://www.ti.com/lit/ds/symlink/ina226.pdf
and https://www.ti.com/lit/ds/symlink/ina228.pdf
"""
from machine import I2C, Pin
import json
import os
import time


def read16(bus, address, register):
    data = bus.readfrom_mem(address, register, 2)
    return (data[0] << 8) | data[1]


def signed16(value):
    return value - 65536 if value & 0x8000 else value


def read24(bus, address, register):
    data = bus.readfrom_mem(address, register, 3)
    return (data[0] << 16) | (data[1] << 8) | data[2]


def main():
    result = {
        "runtime": str(os.uname()),
        "i2c": {"id": 1, "sda_gpio": 18, "scl_gpio": 19, "frequency_hz": 100000},
        "sensor_register_writes": False,
        "drive_outputs_configured": False,
        "devices": [],
        "pass": False,
    }
    bus = I2C(1, sda=Pin(18), scl=Pin(19), freq=100000)
    addresses = bus.scan()
    result["scan"] = ["0x%02X" % address for address in addresses]
    for address in addresses:
        if not 0x40 <= address <= 0x4F:
            continue
        device = {"address": "0x%02X" % address, "errors": [], "samples": []}
        result["devices"].append(device)
        try:
            gate228 = address == 0x40
            manufacturer_reg, die_reg = (0x3E, 0x3F) if gate228 else (0xFE, 0xFF)
            manufacturer = read16(bus, address, manufacturer_reg)
            die = read16(bus, address, die_reg)
            device["manufacturer_id"] = "0x%04X" % manufacturer
            device["die_id"] = "0x%04X" % die
            device["identity_ok"] = manufacturer == 0x5449 and (
                die >> 4 == 0x228 if gate228 else die in (0x2260, 0x2261))
            if not device["identity_ok"]:
                device["errors"].append("Unexpected identity; measurement interpretation skipped")
                continue
            config = read16(bus, address, 0x00)
            device["configuration"] = "0x%04X" % config
            device["calibration"] = "0x%04X" % read16(bus, address, 2 if gate228 else 5)
            bus_lsb = 0.0001953125 if gate228 else 0.00125
            shunt_lsb_uv = (0.078125 if config & 16 else 0.3125) if gate228 else 2.5
            device["identity_checks_passed"] = 0
            for index in range(100):
                try:
                    if read16(bus, address, manufacturer_reg) != manufacturer or read16(bus, address, die_reg) != die:
                        raise ValueError("Identity changed")
                    device["identity_checks_passed"] += 1
                    if gate228:
                        shunt_word = read24(bus, address, 4)
                        bus_word = read24(bus, address, 5)
                        if shunt_word & 15 or bus_word & 15 or bus_word & 0x800000:
                            raise ValueError("Invalid INA228 ADC code")
                        shunt = shunt_word >> 4
                        if shunt & 0x80000:
                            shunt -= 0x100000
                        voltage = bus_word >> 4
                    else:
                        shunt = signed16(read16(bus, address, 0x01))
                        voltage = read16(bus, address, 0x02)
                    device["samples"].append({
                        "index": index,
                        "bus_raw": voltage,
                        "bus_v": voltage * bus_lsb,
                        "shunt_raw": shunt,
                        "shunt_uv": shunt * shunt_lsb_uv,
                    })
                except (OSError, ValueError) as error:
                    device["errors"].append("sample %d: %s" % (index, error))
                time.sleep_ms(20)
        except OSError as error:
            device["errors"].append(str(error))
    result["pass"] = bool(result["devices"]) and all(
        device.get("identity_ok", False)
        and device.get("identity_checks_passed", 0) == 100
        and len(device["samples"]) == 100
        and not device["errors"]
        for device in result["devices"]
    )
    # A large, single USB-serial write can lose output on the host. Retain
    # full sample counts and measured ranges while keeping the report small.
    for device in result["devices"]:
        samples = device.pop("samples")
        device["sample_count"] = len(samples)
        if samples:
            for field in ("bus_v", "shunt_uv"):
                values = [sample[field] for sample in samples]
                device[field] = {
                    "min": min(values),
                    "max": max(values),
                    "mean": sum(values) / len(values),
                }
    print(json.dumps(result))
    if not result["pass"]:
        raise RuntimeError("Sensor communication smoke test did not pass; see JSON result")


main()
