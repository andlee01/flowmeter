"""Verify the RTC wait is bounded by time since boot and always allows startup."""

from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
import unittest
from unittest.mock import Mock, patch

from wait_for_rtc import main, wait_for_rtc


class RtcWaitTest(unittest.TestCase):
    def test_available_rtc_starts_without_delay(self):
        device = Mock()
        device.exists.return_value = True
        with patch("wait_for_rtc.time.sleep") as sleep, redirect_stdout(StringIO()):
            self.assertEqual(wait_for_rtc(device), 0)
            sleep.assert_not_called()

    def test_rtc_arrival_starts_before_deadline(self):
        device = Mock()
        device.exists.side_effect = [False, False, True]
        with patch("wait_for_rtc.boot_seconds", side_effect=[100, 101]), \
                patch("wait_for_rtc.time.sleep") as sleep, redirect_stdout(StringIO()):
            self.assertEqual(wait_for_rtc(device), 0)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 1])

    def test_missing_rtc_waits_only_until_120_seconds_of_uptime(self):
        device = Mock()
        device.exists.return_value = False
        clock = [17.5]
        sleeps = []

        def advance(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        errors = StringIO()
        with patch("wait_for_rtc.boot_seconds", side_effect=lambda: clock[0]), \
                patch("wait_for_rtc.time.sleep", side_effect=advance), \
                redirect_stdout(StringIO()), redirect_stderr(errors):
            self.assertEqual(wait_for_rtc(device), 0)
        self.assertEqual(clock[0], 120)
        self.assertEqual(sum(sleeps), 102.5)
        self.assertIn("starting logging anyway", errors.getvalue())

    def test_late_restart_does_not_begin_another_two_minute_wait(self):
        device = Mock()
        device.exists.return_value = False
        with patch("wait_for_rtc.boot_seconds", return_value=900), \
                patch("wait_for_rtc.time.sleep") as sleep, redirect_stderr(StringIO()):
            self.assertEqual(wait_for_rtc(device), 0)
            sleep.assert_not_called()

    def test_check_failure_allows_recording(self):
        with patch("wait_for_rtc.wait_for_rtc", side_effect=OSError("unavailable")), \
                redirect_stderr(StringIO()) as errors:
            self.assertEqual(main(), 0)
        self.assertIn("starting logging anyway", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
