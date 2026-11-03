"""Run in RAM with mpremote; read INA226 registers without configuring drives.

I2C1: SDA GP18 (Pico header 24), SCL GP19 (header 25), 100 kHz.
No sensor configuration/calibration writes, PWM, Wi-Fi or ADC operations.
No current calculation: the fitted shunt value must be verified separately.
TI register reference: https://www.ti.com/lit/ds/symlink/ina226.pdf
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
            manufacturer = read16(bus, address, 0xFE)
            die = read16(bus, address, 0xFF)
            device["manufacturer_id"] = "0x%04X" % manufacturer
            device["die_id"] = "0x%04X" % die
            device["identity_ok"] = manufacturer == 0x5449 and die in (0x2260, 0x2261)
            if not device["identity_ok"]:
                device["errors"].append("Unexpected identity; measurement interpretation skipped")
                continue
            device["configuration"] = "0x%04X" % read16(bus, address, 0x00)
            device["calibration"] = "0x%04X" % read16(bus, address, 0x05)
            device["identity_checks_passed"] = 0
            for index in range(100):
                try:
                    if read16(bus, address, 0xFE) != manufacturer or read16(bus, address, 0xFF) != die:
                        raise ValueError("Identity changed")
                    device["identity_checks_passed"] += 1
                    shunt = signed16(read16(bus, address, 0x01))
                    voltage = read16(bus, address, 0x02)
                    device["samples"].append({
                        "index": index,
                        "bus_raw": voltage,
                        "bus_v": voltage * 0.00125,
                        "shunt_raw": shunt,
                        "shunt_uv": shunt * 2.5,
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
        raise RuntimeError("INA226 communication smoke test did not pass; see JSON result")


main()
