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
from firmware.ui import App, UPLOAD_FAILURE
from firmware.ram_scan import PackedScanBuffer


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
    state = "idle"
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
    state = "uploading"
    upload_stage = 2
    upload_sent, upload_total = 3700, 10000
    status = {"state": "uploading", "message": "Uploading scan", "configured": True, "enabled": True}


class PreviewManual:
    """Fixed illustrative values; never creates an instrument or drives PWM."""
    targets_uv = (8300000, 5300000)
    duty_u16 = tuple(int(model.drive_duty(role, target / 1000000) * 65535 + .5)
                     for role, target in zip(("drain", "gate"), targets_uv))
    applied_duty_u16 = duty_u16
    outputs_enabled = True
    correction_active = True
    sample_revision = 1
    latest = {"drain_bus_v": 8.283, "gate_bus_v": 5.293,
              "id_a": .00293, "ig_a": 1.234e-6}


def preview_app():
    display = PillowDisplay()
    engine = PreviewEngine()
    app = App(display, PreviewInputs(), engine, settings=dict(model.DEFAULT_SETTINGS),
              cloud=PreviewCloud(), scan_buffer=PackedScanBuffer(bytearray(65536)),
              restart=lambda: None)
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
                   "curve": curve, "index": curve * 11 + index, "acquisition_blocks_completed": 1,
                   "requested_vds": drain, "requested_vgs": gate}


def fill_simulated_scan(app, count):
    app.settings.update(drain_start_uv=0, drain_stop_uv=2000000, drain_step_uv=200000,
                        gate_start_uv=500000, gate_stop_uv=2000000, gate_step_uv=500000)
    app.clear_results()
    app._ram_drain_count = 11
    for index, row in enumerate(simulated_rows()):
        if index >= count:
            break
        app._scan_buffer.append(row)
        app._add_point(row)
        app.latest = row
    app.engine.progress = count / 44


def save_screen(app, title, output, simulated=False):
    app.render()
    if app.screen == "scan":
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
                          ("Manual debug off", "manual"), ("Manual debug on", "manual"),
                          ("Keypad", "edit"), ("Preparing", "preparing"),
                          ("Scan", "scan"), ("Uploading 37 percent", "upload"),
                          ("Confirming 100 percent", "upload"), ("Upload confirmed", "upload"),
                          ("Partial upload", "upload"), ("Restarting", "restarting")):
        app = preview_app()
        app.screen = screen
        if screen == "manual":
            app._manual = PreviewManual()
            app._manual_cache = [None] * 6
            if title == "Manual debug off":
                app._manual.outputs_enabled = False
                app._manual.applied_duty_u16 = (0, 0)
                app._manual.latest = None
        if screen == "edit":
            app.edit_label, app.edit_value = "VGS stop (V)", "2.000"
        if screen in ("scan", "upload"):
            fill_simulated_scan(app, 29 if screen == "scan" else 44)
        if title == "Partial upload":
            app._scan_fault = {"channel": "gate", "kind": "shunt_saturation"}
        if title == "Confirming 100 percent":
            app.cloud.upload_stage = 3
            app.cloud.upload_sent = app.cloud.upload_total
        elif title == "Upload confirmed":
            app.cloud.state, app.cloud.upload_stage = "complete", 4
            app.cloud.upload_sent = app.cloud.upload_total
        screens.append(save_screen(app, title, output, screen in ("scan", "upload", "manual")))
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
    for title, screen in (("Cloud settings", "cloud"), ("Cloud URL editor", "cloud_url")):
        app = preview_app()
        app.screen = screen
        # Synthetic endpoint only. No saved configuration or credentials read.
        app._cloud_url_draft = "https://script.google.com/macros/s/EXAMPLE_ONLY/exec"
        app._cloud_url_edit = app._cloud_url_draft
        screens.append(save_screen(app, title, output, simulated=True))
    for title, screen, notice in (("Upload failure", "upload_error", UPLOAD_FAILURE),
                                  ("Scan fault", "scan_error", "Gate shunt saturation. Outputs stopped.")):
        app = preview_app()
        app.screen, app.notice = screen, notice
        if screen == "upload_error":
            fill_simulated_scan(app, 44)
            app.cloud.state = "error"
        screens.append(save_screen(app, title, output, screen == "upload_error"))
    app = preview_app()
    app.message("Settings could not be saved. Please retry.")
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
