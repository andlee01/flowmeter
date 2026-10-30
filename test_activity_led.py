"""Test onboard LED state, permissions and cleanup without changing real hardware."""

from pathlib import Path
import tempfile
import os
import signal
import subprocess
import sys
import time
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
                self.assertEqual((led / "trigger").read_text().strip(), "none")

    def test_blinks_only_when_recording_and_stays_off_on_exit(self):
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
                self.assertEqual((led / "trigger").read_text().strip(), "none")
                self.assertEqual((led / "brightness").read_text().strip(), "0")

    def test_exception_turns_led_off(self):
        with tempfile.TemporaryDirectory() as directory:
            led = self.led_files(directory)
            with self.assertRaisesRegex(OSError, "disk full"):
                with ActivityLed("auto", root=directory) as indicator:
                    indicator.set_recording(True)
                    raise OSError("disk full")
            self.assertFalse(indicator._thread.is_alive())
            self.assertEqual((led / "trigger").read_text().strip(), "none")

    def test_restore_mode_returns_original_trigger(self):
        with tempfile.TemporaryDirectory() as directory:
            led = self.led_files(directory)
            with ActivityLed("auto", root=directory, stop_mode="restore"):
                pass
            self.assertEqual((led / "trigger").read_text().strip(), "mmc0")

    def test_ctrl_c_stops_logger_and_leaves_indicator_dark(self):
        # Real logger lifecycle and signal handling, with simulated readings and LED files.
        with tempfile.TemporaryDirectory() as directory:
            led = self.led_files(directory)
            script = """
import sys
from pathlib import Path
import log_flow
from activity_led import ActivityLed
root = Path(sys.argv[1])
log_flow.ActivityLed = lambda selection, **kwargs: ActivityLed(selection, root=root, **kwargs)
log_flow.read_flow = lambda args: (1.25, (0, 0), (80.25, 100.5, 20.25))
sys.argv = ['log_flow.py', '--led', 'auto', '--interval', '5m', '--output-dir', str(root / 'readings')]
sys.exit(log_flow.main())
"""
            process = subprocess.Popen([sys.executable, "-c", script, directory],
                                       cwd=Path(__file__).parent, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while (led / "brightness").read_text().strip() != "1":
                    self.assertIsNone(process.poll(), "Logger exited before first LED flash")
                    self.assertLess(time.monotonic(), deadline, "No LED flash after saved reading")
                    time.sleep(0.01)
                os.kill(process.pid, signal.SIGINT)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                self.assertEqual((led / "trigger").read_text().strip(), "none")
                self.assertEqual((led / "brightness").read_text().strip(), "0")
                self.assertEqual(len(list((Path(directory) / 'readings').glob('*.csv'))), 1)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

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
