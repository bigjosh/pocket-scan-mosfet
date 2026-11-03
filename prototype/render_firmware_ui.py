"""Render the actual firmware UI with Pillow, without connecting to a Pico.

Usage: python prototype/render_firmware_ui.py [--output output/firmware-ui]

The labels outside each 480x320 screen identify these as host previews.
An approximate monospace font is fitted to the firmware's 8x8 text cells;
the layout, colors, text, controls and plot come from firmware/ui.py itself.
Simulated curves illustrate the display and are not hardware measurements.
"""

import argparse
from pathlib import Path
import sys

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from firmware import model
from firmware.ui import App, COUNT_FIELDS


def rgb888(color):
    red, green, blue = (color >> 11) & 31, (color >> 5) & 63, color & 31
    return (red * 255 // 31, green * 255 // 63, blue * 255 // 31)


def font(size=12):
    candidates = (
        "C:/Windows/Fonts/consola.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "DejaVuSansMono.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


class PillowDisplay:
    """Implements the display API with fixed 8x8 cells and approximate glyphs."""

    width, height = 480, 320

    def __init__(self):
        self.image = Image.new("RGB", (self.width, self.height), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.font = font(12)
        self.glyphs = {}

    def fill(self, color):
        self.draw.rectangle((0, 0, self.width - 1, self.height - 1), fill=rgb888(color))

    def fill_rect(self, x, y, width, height, color):
        if width > 0 and height > 0:
            self.draw.rectangle((x, y, x + width - 1, y + height - 1), fill=rgb888(color))

    def rect(self, x, y, width, height, color):
        if width > 0 and height > 0:
            self.draw.rectangle((x, y, x + width - 1, y + height - 1), outline=rgb888(color))

    def line(self, x0, y0, x1, y1, color):
        self.draw.line((x0, y0, x1, y1), fill=rgb888(color))

    def pixel(self, x, y, color):
        if 0 <= x < self.width and 0 <= y < self.height:
            self.image.putpixel((x, y), rgb888(color))

    @staticmethod
    def _plain_text(value):
        for original, replacement in (("\u00b5", "u"), ("\u03bc", "u"),
                                      ("\u03a9", "ohm"), ("\u2212", "-"),
                                      ("\u00d7", "x"), ("\u00b0", "deg")):
            value = str(value).replace(original, replacement)
        return value

    def text_width(self, value, scale=1):
        return max(len(line) for line in self._plain_text(value).split("\n")) * 8 * scale

    def _glyph(self, character):
        if character not in self.glyphs:
            # Render into a consistent monospace cell, then fit the whole cell
            # to 8x8. This preserves baseline/spacing, unlike fitting each glyph.
            cell = Image.new("L", (8, 14), 0)
            ImageDraw.Draw(cell).text((0, 0), character, font=self.font, fill=255)
            self.glyphs[character] = cell.resize((8, 8), Image.Resampling.LANCZOS)
        return self.glyphs[character]

    def text(self, value, x, y, color, bg=None, scale=1):
        origin = x
        size = 8 * scale
        for character in self._plain_text(value):
            if character == "\n":
                x, y = origin, y + size
                continue
            if bg is not None:
                self.fill_rect(x, y, size, size, bg)
            mask = self._glyph(character).resize((size, size), Image.Resampling.NEAREST)
            self.image.paste(rgb888(color), (int(x), int(y), int(x + size), int(y + size)), mask)
            x += size

    def blit_buffer(self, buffer, x, y, width, height):
        image = Image.new("RGB", (width, height))
        image.putdata([rgb888((buffer[index] << 8) | buffer[index + 1])
                       for index in range(0, width * height * 2, 2)])
        self.image.paste(image, (x, y))


class PreviewInputs:
    def poll(self, now):
        return []


class PreviewEngine:
    progress = 0.0
    latest = None

    def tick(self, now):
        return []


class PreviewWifi:
    """Synthetic network labels only; never reads real saved credentials."""
    state = "connected"
    status = {"state": "connected", "ssid": "Example workshop", "ip": "192.0.2.10",
              "message": "Connected", "saved_count": 20}

    def has_saved(self, ssid):
        return ssid == "Example workshop"


class PreviewCloud:
    status = {"state": "queued", "message": "Saved scans are waiting to upload",
              "pending_count": 2, "saved_count": 5, "configured": True, "enabled": True}


def calibration():
    return {
        "version": 1,
        "metadata": {"driver_supply_v": 5.0, "pwm_hz": model.PWM_HZ},
        "channels": {
            role: {"shunt_ohm": resistance,
                   "points": [{"terminal_v": voltage,
                               "shunt_v": 0.0,
                               "duty": voltage / model.PWM_FULL_SCALE_V}
                              for voltage in (0.0, 1.5, 3.0)]}
            for role, resistance in (("drain", model.DRAIN_SHUNT_OHM),
                                     ("gate", model.GATE_SHUNT_OHM))
        },
    }


def preview_app():
    display = PillowDisplay()
    engine = PreviewEngine()
    app = App(display, PreviewInputs(), engine,
              settings=dict(model.DEFAULT_SETTINGS), calibration=calibration())
    app.draft = dict(app.settings)
    return app


def simulated_rows():
    for curve in range(4):
        gate = 0.5 + 0.5 * curve
        for index in range(11):
            drain = index * 0.2
            current = max(0.0, gate - 0.7) ** 2 * (1.0 - 2.718281828 ** (-drain * 3)) * 0.0003
            yield {"vds": drain, "vgs": gate, "id_a": current,
                   "ig_a": (gate - 0.9) * 0.08e-6,
                   "id_std_a": 0.2e-6, "ig_std_a": 0.02e-6,
                   "curve": curve, "batches_completed": 10,
                   "requested_vds": drain, "requested_vgs": gate,
                   "drain_voltage_limited": False, "gate_voltage_limited": False}


def save_screen(app, title, output, simulated=False):
    app.render()
    if app.screen in ("scan", "complete", "partial"):
        while app._plot_cursor < len(app.curves):
            app._draw_points()
    # Leave the complete firmware image at its actual resolution. The caption
    # occupies a separate band, never covering UI content or controls.
    labeled = Image.new("RGB", (480, 352), "#ffffff")
    labeled.paste(app.d.image, (0, 0))
    draw = ImageDraw.Draw(labeled)
    draw.text((8, 324), title + " | Host preview; approximate font", font=font(11), fill="#12354a")
    if simulated:
        draw.text((8, 338), "Simulated data; no hardware measurements", font=font(10), fill="#58707e")
    path = output / (title.lower().replace(" ", "-") + ".png")
    labeled.save(path)
    return path


def render_all(output):
    output.mkdir(parents=True, exist_ok=True)
    screens = []
    for title, screen in (("Home", "home"), ("Settings", "settings"),
                          ("Advanced", "advanced"), ("Averaging", "averaging"),
                          ("Keypad", "edit"), ("Count keypad", "edit"),
                          ("Calibration confirmation", "confirm_calibration"),
                          ("Calibration", "calibrating"),
                          ("Scan", "scan"), ("Complete", "complete")):
        app = preview_app()
        app.screen = screen
        if screen == "edit":
            if title == "Count keypad":
                app.edit_key = "samples_per_batch"
                app.edit_label = COUNT_FIELDS[app.edit_key][0]
                app.edit_value = "50"
                app.edit_integer = True
            else:
                app.edit_label, app.edit_value = "VGS stop (V)", "2.000"
        elif screen == "calibrating":
            app.engine.progress = 0.42
            app.latest = {"vds": 1.3, "vgs": 1.3}
        elif screen in ("scan", "complete"):
            points = list(simulated_rows())
            if screen == "scan":
                points = points[:29]
            for row in points:
                app._add_point(row)
            app.latest = points[-1]
            app.engine.progress = len(points) / 44
        screens.append(save_screen(app, title, output, screen in ("scan", "complete")))
    for title, screen, count in (("Voltage limited scan", "scan", 2),
                                  ("Voltage limited complete", "complete", 3)):
        app = preview_app()
        app.settings.update(drain_start_uv=4000000, drain_stop_uv=5000000,
                            gate_start_uv=4000000, gate_stop_uv=4000000)
        app.clear_results()
        app.screen = screen
        for index in range(count):
            row = {"vds": 3.48 + index * 0.015, "vgs": 2.81,
                   "id_a": (120 + index * 4) * 1e-6, "ig_a": 0.05e-6,
                   "id_std_a": 0.2e-6, "ig_std_a": 0.02e-6,
                   "curve": 0, "batches_completed": 10,
                   "requested_vds": 4.0 + index * 0.5, "requested_vgs": 4.0,
                   "drain_voltage_limited": True, "gate_voltage_limited": True}
            app._add_point(row)
        app.latest = row
        app.engine.progress = count / 3
        screens.append(save_screen(app, title, output, simulated=True))
    for title, screen, mode in (("WiFi", "wifi", "abc"),
                                ("WiFi search", "wifi_search", "abc"),
                                ("WiFi networks", "wifi_list", "abc"),
                                ("WiFi saved password", "wifi_network", "abc"),
                                ("WiFi keyboard", "wifi_password", "abc"),
                                ("WiFi uppercase", "wifi_password", "ABC"),
                                ("WiFi numbers", "wifi_password", "123"),
                                ("WiFi symbols", "wifi_password", "#+="),
                                ("WiFi connected", "wifi_status", "abc"),
                                ("WiFi forget", "wifi_forget", "abc")):
        app = preview_app()
        app.wifi = PreviewWifi()
        app.screen = screen
        app._wifi_rows = [{"ssid": name, "rssi": -42 - i * 9,
                           "saved": i == 0, "security_label": "Personal" if i != 2 else "Open",
                           "requires_password": i != 2, "supported": True}
                          for i, name in enumerate(("Example workshop", "Example home",
                                                     "Example guest", "Example network 4",
                                                     "Example network 5"))]
        app._wifi_selected = app._wifi_rows[0]
        app._wifi_password = "Example key!123"  # Explicitly synthetic preview data.
        app._wifi_keyboard = mode
        app._wifi_connect_failed = False
        screens.append(save_screen(app, title, output, simulated=True))
    for title, screen in (("Cloud settings", "cloud"), ("Cloud URL editor", "cloud_url"),
                           ("Save scan confirmation", "complete")):
        app = preview_app()
        app.cloud = PreviewCloud()
        app.screen = screen
        # Synthetic endpoint only. No saved configuration or credentials read.
        app._cloud_url_draft = "https://script.google.com/macros/s/EXAMPLE_ONLY/exec"
        app._cloud_enabled_draft = True
        app._cloud_url_edit = app._cloud_url_draft
        if screen == "complete":
            for row in simulated_rows():
                app._add_point(row)
            app.latest = row
            app.engine.progress = 1.0
        screens.append(save_screen(app, title, output, simulated=True))
    for count in (0, 4):
        app = preview_app()
        app.cloud = PreviewCloud()
        app.screen = "partial"
        app._scan_fault = {"channel": "gate", "kind": "shunt_saturation"}
        for index, row in enumerate(simulated_rows()):
            if index == count:
                break
            app._add_point(row)
            app.latest = row
        app.engine.progress = count / 44
        screens.append(save_screen(app, "Over range %d points" % count, output, simulated=True))
    for title, screen, pending in (("Storage full", "storage", True),
                                     ("Storage ready", "storage", False),
                                     ("Delete queued scan", "storage_confirm", True),
                                     ("Delete uploaded scan", "storage_confirm", False)):
        app = preview_app()
        app.screen = screen
        app._storage_rows = [{"scan_id": "abcd0123-000000000000000%d" % index,
                              "status": "pending" if index < 2 else "sent",
                              "size_bytes": 184000 + index * 650,
                              "point_count": 2401, "created_utc": "2026-11-01T15:40:00Z",
                              "damaged": False} for index in range(3)]
        app._storage_capacity = {"actual_free_bytes": 22000 if pending else 400000,
                                  "reclaimable_bytes": 186000, "required_bytes": 258068,
                                  "can_start": not pending, "reason": "space" if pending else None,
                                  "pending_count": 2, "pending_limit": 20}
        app._storage_selected = app._storage_rows[0 if pending else 2]
        screens.append(save_screen(app, title, output, simulated=True))
    # Also cover first boot and a representative error, since these are common
    # paths during breadboard bring-up and have different text constraints.
    app = preview_app()
    app.calibration = None
    screens.append(save_screen(app, "Calibration required", output))
    app.message("Calibration could not be saved: filesystem is full. The previous calibration remains available.")
    screens.append(save_screen(app, "Notice", output))
    sheet = Image.new("RGB", (480 * 2 + 24, ((len(screens) + 1) // 2) * 360 + 8), "#c5d2da")
    for index, path in enumerate(screens):
        with Image.open(path) as screen:
            sheet.paste(screen, (8 + (index % 2) * 488, 8 + (index // 2) * 360))
    contact = output / "contact-sheet.png"
    sheet.save(contact)
    print(str(contact.resolve()))
    for path in screens:
        print(str(path.resolve()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "firmware-ui")
    render_all(parser.parse_args().output)
