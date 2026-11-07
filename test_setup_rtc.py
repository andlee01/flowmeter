"""Check RTC configuration changes and clock-write guards without accessing hardware."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from setup_rtc import BEGIN, build_boot_config, ds3231_device, initialise_rtc, update_config


class RtcSetupTest(unittest.TestCase):
    def test_global_block_is_idempotent_and_preserves_other_settings(self):
        original = "# custom\ndtparam=audio=on\n[pi5]\ndtoverlay=nospi10\n"
        updated = build_boot_config(original)
        self.assertIn(original, updated)
        self.assertIn("[all]\ndtparam=i2c_arm=on\ndtoverlay=i2c-rtc,ds3231", updated)
        self.assertEqual(build_boot_config(updated), updated)
        self.assertEqual(updated.count(BEGIN), 1)

    def test_existing_global_ds3231_is_not_duplicated(self):
        updated = build_boot_config("[all]\ndtoverlay=i2c-rtc,ds3231\n")
        self.assertEqual(updated.count("dtoverlay=i2c-rtc,ds3231"), 1)
        self.assertEqual(build_boot_config(updated), updated)

    def test_conflicting_or_conditional_overlay_is_rejected(self):
        for config in ("dtoverlay=i2c-rtc,pcf8523\n", "[pi4]\ndtoverlay=i2c-rtc,ds3231\n",
                       "dtoverlay=i2c-rtc-gpio,ds3231\n", BEGIN + "\npartial\n"):
            with self.subTest(config=config), self.assertRaises(RuntimeError):
                build_boot_config(config)

    def test_backup_and_no_extra_write_when_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.txt"
            original = "dtparam=audio=on\n"
            config.write_text(original)
            updated = build_boot_config(original)
            update_config(config, updated)
            backups = list(Path(directory).glob("*.before-rtc.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), original)
            self.assertEqual(config.read_text(), updated)
            update_config(config, updated)
            self.assertEqual(len(list(Path(directory).glob("*.before-rtc.*"))), 1)

    def test_only_primary_ds3231_can_be_initialised(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("setup_rtc.RTC_ROOT", root):
                with self.assertRaisesRegex(RuntimeError, "No DS3231"):
                    ds3231_device()
                secondary = root / "rtc1"
                secondary.mkdir()
                (secondary / "name").write_text("ds3231 1-0068")
                with self.assertRaisesRegex(RuntimeError, "not rtc0"):
                    ds3231_device()
                secondary.rename(root / "rtc0")
                self.assertEqual(ds3231_device(), Path("/dev/rtc0"))

    def test_unsynchronised_clock_is_never_copied_implicitly(self):
        with patch("setup_rtc.ds3231_device", return_value=Path("/dev/rtc0")), \
                patch("setup_rtc.run", return_value=SimpleNamespace(stdout="no\n")) as command, \
                patch("setup_rtc.ensure_hwclock") as install:
            with self.assertRaisesRegex(RuntimeError, "not NTP-synchronised"):
                initialise_rtc()
            install.assert_not_called()
            self.assertEqual(command.call_count, 1)

    def test_initialisation_writes_in_correct_direction_and_utc(self):
        for synced, trust in (("yes\n", False), ("no\n", True)):
            with self.subTest(synced=synced), \
                    patch("setup_rtc.ds3231_device", return_value=Path("/dev/rtc0")), \
                    patch("setup_rtc.run", return_value=SimpleNamespace(stdout=synced)) as command, \
                    patch("setup_rtc.ensure_hwclock", return_value="/usr/sbin/hwclock"):
                initialise_rtc(trust)
                self.assertEqual(command.call_args_list[1].args[0],
                                 ["/usr/sbin/hwclock", "--systohc", "--utc", "--rtc", "/dev/rtc0"])
                self.assertEqual(command.call_args_list[2].args[0],
                                 ["/usr/sbin/hwclock", "--show", "--utc", "--rtc", "/dev/rtc0"])


if __name__ == "__main__":
    unittest.main()
