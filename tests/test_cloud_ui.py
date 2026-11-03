"""Receiver configuration is independent, bounded and credential-safe."""
import unittest
from firmware import https_upload
from firmware.engine import Engine
from firmware.ram_scan import PackedScanBuffer
from firmware.ui import App
from test_ui import FakeDisplay, FakeInputs, HostInstrument, MemoryStore
from test_ram_ui import RamCloud, RamWifi

EXAMPLE_URL = "https://script.google.com/macros/s/EXAMPLE_PRIVATE_DEPLOYMENT/exec"

class ConfigCloud(RamCloud):
    def __init__(self, buffer):
        super().__init__(buffer)
        self.url, self.enabled = EXAMPLE_URL, False
        self.failure = None
    def save_config(self, url, enabled=True):
        if self.failure:
            raise self.failure
        https_upload.endpoint_parts(url)
        return super().save_config(url, enabled)

class CloudUITests(unittest.TestCase):
    def setUp(self):
        self.display, self.inputs = FakeDisplay(), FakeInputs()
        self.buffer, self.store, self.logs, self.restarts = PackedScanBuffer(capacity=32), MemoryStore(), [], []
        self.cloud, self.wifi = ConfigCloud(self.buffer), RamWifi()
        self.app = App(self.display, self.inputs, Engine(HostInstrument()), store=self.store,
                       wifi=self.wifi, cloud=self.cloud, scan_buffer=self.buffer,
                       restart=lambda: self.restarts.append(True),
                       error_logger=lambda context, error: self.logs.append(str(error)))
        self.now = 0
        self.app.render()
    def tick(self, action=None):
        self.now += 20
        self.inputs.events = [(action,)] if action else []
        self.app.tick(self.now)
    def action(self, action):
        self.app.action(action)
        self.tick()
    def settings(self):
        self.tick("left")
        self.action("advanced")
        self.action("cloud")

    def test_home_never_joins_uploads_or_reads_receiver_secret(self):
        self.tick()
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual(self.wifi.calls, [])
        self.assertNotIn(EXAMPLE_URL, repr(self.display.labels))

    def test_config_masks_url_and_cancel_discards_only_its_draft(self):
        self.settings()
        self.assertNotIn(EXAMPLE_URL, repr(self.display.labels))
        self.assertFalse(any(label in ("STORAGE", "RETRY", "AUTO UPLOAD") for label in self.display.labels))
        self.action("cloud_url")
        self.action("cloud_show")
        self.assertIn("<" + EXAMPLE_URL[-13:], self.display.labels)
        self.action("cloud_clear")
        self.action(("cloud_char", "x"))
        self.action("left")
        self.assertEqual(self.app._cloud_url_draft, EXAMPLE_URL)
        self.assertEqual(self.app._cloud_url_edit, "")
        self.assertFalse(self.app._cloud_show)
        self.action("left")
        self.assertEqual(self.app._cloud_url_draft, "")
        self.assertEqual(self.cloud.url, EXAMPLE_URL)
        self.assertFalse(self.cloud.enabled)
        self.assertNotIn("save_config", self.cloud.calls)

    def test_cloud_commit_survives_discarded_scan_settings_and_enables_upload(self):
        self.settings()
        self.action("cloud_url")
        changed = EXAMPLE_URL.replace("EXAMPLE_", "SECOND_")
        self.app._cloud_url_edit = changed
        self.action("right")
        self.action("right")
        self.assertEqual(self.cloud.url, changed)
        self.assertTrue(self.cloud.enabled)
        self.action("left")
        self.action("left")
        self.assertEqual(self.restarts, [True])
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.cloud.url, changed)

    def test_invalid_and_driver_error_urls_are_never_exposed(self):
        self.settings()
        self.action("cloud_url")
        self.app._cloud_url_edit = "https://wrong.invalid/private-value"
        self.action("right")
        self.action("right")
        self.assertEqual(self.cloud.url, EXAMPLE_URL)
        self.assertEqual(self.app.screen, "notice")
        self.assertNotIn("private-value", repr(self.logs) + repr(self.display.labels))
        self.action("right")
        self.cloud.failure = OSError(EXAMPLE_URL)
        self.action("right")
        self.assertNotIn(EXAMPLE_URL, repr(self.logs) + self.app.notice)

    def test_url_editor_is_bounded_and_independent_of_wifi_password(self):
        self.settings()
        self.action("cloud_url")
        self.action("cloud_clear")
        for _ in range(270):
            self.app.action(("cloud_char", "a"))
        self.assertEqual(len(self.app._cloud_url_edit), 256)
        self.app.action("cloud_backspace")
        self.assertEqual(len(self.app._cloud_url_edit), 255)
        self.assertEqual(self.app._wifi_password, "")

if __name__ == "__main__":
    unittest.main()
