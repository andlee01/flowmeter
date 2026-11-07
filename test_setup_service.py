"""Validate the startup-service installer without changing system services."""

import argparse
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from setup_service import install_text, interval, main, parser, render_service


class ServiceSetupTest(unittest.TestCase):
    def test_interval_validation(self):
        for value in ("10s", "5m", "0.5h", "300"):
            self.assertEqual(interval(value), value)
        for value in ("0s", "-1m", "nan", "5ms"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                interval(value)

    def test_service_uses_project_environment_and_normal_user(self):
        args = parser().parse_args(["--interval", "5m", "--serial-number", "ABC123", "--wait-for-rtc"])
        unit = render_service(args, "pi", Path("/home/pi/flowmeter"))
        self.assertIn('User=pi', unit)
        self.assertIn('ExecStart=:"/home/pi/flowmeter/.venv/bin/python"', unit)
        self.assertIn('"--interval" "5m"', unit)
        self.assertIn('"--serial-number" "ABC123"', unit)
        self.assertIn("SupplementaryGroups=dialout gpio", unit)
        self.assertIn('ExecStartPre=-:"/home/pi/flowmeter/.venv/bin/python"', unit)
        self.assertIn('"/home/pi/flowmeter/wait_for_rtc.py"', unit)
        self.assertIn("TimeoutStartSec=130", unit)
        self.assertNotIn("Requires=dev-rtc0.device", unit)
        self.assertNotIn("After=dev-rtc0.device", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("network-online.target", unit)

    def test_led_disabled_does_not_require_gpio(self):
        unit = render_service(parser().parse_args(["--led", "off"]), "pi", Path("/home/pi/flowmeter"))
        self.assertIn("SupplementaryGroups=dialout\n", unit)
        self.assertNotIn("Requires=dev-rtc0.device", unit)
        self.assertNotIn("ExecStartPre=", unit)

    def test_new_wait_flag_and_legacy_flag_generate_same_service(self):
        project = Path("/home/pi/flowmeter")
        legacy = render_service(parser().parse_args(["--require-rtc"]), "pi", project)
        current = render_service(parser().parse_args(["--wait-for-rtc"]), "pi", project)
        self.assertEqual(legacy, current)

    def test_paths_are_quoted_and_specifiers_are_escaped(self):
        unit = render_service(parser().parse_args([]), "pi", Path('/home/pi/a space % $test'))
        self.assertIn('WorkingDirectory=/home/pi/a space %% $test', unit)
        self.assertIn('ExecStart=:"/home/pi/a space %% $test/.venv/bin/python"', unit)

    def test_reinstallation_backs_up_changes_only(self):
        with tempfile.TemporaryDirectory() as directory:
            unit = Path(directory) / "flowmeter.service"
            install_text(unit, "first")
            install_text(unit, "first")
            self.assertEqual(len(list(Path(directory).glob("*.backup.*"))), 0)
            install_text(unit, "second")
            backups = list(Path(directory).glob("*.backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(), "first")
            self.assertEqual(unit.read_text(), "second")

    def test_install_enables_boot_and_respects_no_start(self):
        for no_start in (False, True):
            with self.subTest(no_start=no_start), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                (project / ".venv/bin").mkdir(parents=True)
                (project / ".venv/bin/python").touch()
                (project / "99-flowmeter-led.rules").write_text("LED rule")
                (project / "99-flowmeter-rtc.rules").write_text("RTC rule")
                args = ["setup_service.py", "--user", "test-user", "--wait-for-rtc"]
                if no_start:
                    args.append("--no-start")
                with patch("sys.argv", args), patch("setup_service.PROJECT", project), \
                        patch("setup_service.os.geteuid", return_value=0), \
                        patch("setup_service.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1000)), \
                        patch("setup_service.grp.getgrnam"), \
                        patch("setup_service.manual_loggers", return_value=[]), \
                        patch("setup_service.subprocess.run", return_value=SimpleNamespace(stdout="0", returncode=0)) as command, \
                        patch("setup_service.install_text") as install:
                    self.assertEqual(main(), 0)
                    commands = [call.args[0] for call in command.call_args_list]
                    self.assertIn(["systemctl", "enable", "flowmeter.service"], commands)
                    self.assertEqual(["systemctl", "restart", "flowmeter.service"] in commands, not no_start)
                    self.assertNotIn(["udevadm", "trigger", "--subsystem-match=rtc", "--action=add"], commands)
                    self.assertEqual(install.call_count, 2)

    def test_dry_run_has_no_system_side_effects(self):
        with patch("sys.argv", ["setup_service.py", "--dry-run", "--user", "test-user"]), \
                patch("setup_service.pwd.getpwnam", return_value=SimpleNamespace(pw_uid=1000)),                 patch("setup_service.subprocess.run") as command, \
                patch("setup_service.install_text") as install:
            self.assertEqual(main(), 0)
            command.assert_not_called()
            install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
