"""Test onboard LED state, permissions and cleanup without changing real hardware."""

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from activity_led import ActivityLed


class ActivityLedTest(unittest.TestCase):
    def led_files(self, root, name="ACT"):
        led = Path(root) / name
        led.mkdir()
        for key, value in {"trigger": "none [mmc0] timer", "brightness": "0",
                           "max_brightness": "1"}.items():
            (led / key).write_text(value)
        return led

    def test_off_never_looks_for_hardware(self):
        with ActivityLed("off", root=Path("/missing-led-directory")) as indicator:
            indicator.set_recording(True)
            indicator.set_recording(False)
        self.assertIsNone(indicator.path)

    def test_detects_modern_and_legacy_names(self):
        for name in ("ACT", "led0"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                led = self.led_files(directory, name)
                with ActivityLed("auto", root=directory) as indicator:
                    self.assertEqual(indicator.path, led)
                self.assertEqual((led / "trigger").read_text().strip(), "mmc0")

    def test_blinks_only_when_recording_and_restores_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            led = self.led_files(directory)
            illuminated = threading.Event()
            writes = []
            original = ActivityLed._write

            def record(indicator, name, value):
                original(indicator, name, value)
                writes.append((name, value))
                if name == "brightness" and value == 1:
                    illuminated.set()

            with patch.object(ActivityLed, "_write", record):
                with ActivityLed("auto", root=directory) as indicator:
                    self.assertNotIn(("brightness", 1), writes)
                    self.assertEqual((led / "trigger").read_text().strip(), "none")
                    indicator.set_recording(True)
                    self.assertTrue(illuminated.wait(2), "LED never illuminated")
                    indicator.set_recording(False)
                    self.assertEqual((led / "brightness").read_text().strip(), "0")
                self.assertFalse(indicator._thread.is_alive())
                self.assertEqual((led / "trigger").read_text().strip(), "mmc0")
                self.assertEqual((led / "brightness").read_text().strip(), "0")

    def test_exception_restores_led(self):
        with tempfile.TemporaryDirectory() as directory:
            led = self.led_files(directory)
            with self.assertRaisesRegex(OSError, "disk full"):
                with ActivityLed("auto", root=directory) as indicator:
                    indicator.set_recording(True)
                    raise OSError("disk full")
            self.assertFalse(indicator._thread.is_alive())
            self.assertEqual((led / "trigger").read_text().strip(), "mmc0")

    def test_missing_led_reports_configuration_problem(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OSError, "ACT LED not found"):
                ActivityLed("auto", root=directory)

    def test_permission_failure_reports_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            led = self.led_files(directory)
            original = ActivityLed._write

            def permission_error(indicator, name, value):
                if name == "trigger" and value == "none":
                    raise PermissionError("denied")
                original(indicator, name, value)

            with patch.object(ActivityLed, "_write", permission_error):
                with self.assertRaisesRegex(OSError, "udev rule"):
                    with ActivityLed("auto", root=directory):
                        pass
            self.assertEqual((led / "trigger").read_text().strip(), "mmc0")


if __name__ == "__main__":
    unittest.main()
