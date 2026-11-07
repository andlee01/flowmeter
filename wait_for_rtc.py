#!/usr/bin/env python3
"""Wait for rtc0 until two minutes after boot, then allow logging regardless."""

from pathlib import Path
import sys
import time


def boot_seconds():
    # Includes suspended time and is independent of RTC/NTP wall-clock adjustments.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def wait_for_rtc(device=Path("/dev/rtc0"), boot_deadline=120):
    announced = False
    while not device.exists():
        remaining = boot_deadline - boot_seconds()
        if remaining <= 0:
            print("WARNING: RTC unavailable two minutes after boot; starting logging "
                  "anyway using current system time.", file=sys.stderr, flush=True)
            return 0
        if not announced:
            print(f"Waiting for {device}; at most {remaining:.1f}s remain until "
                  "the two-minute boot deadline.", flush=True)
            announced = True
        time.sleep(min(1, remaining))
    print(f"RTC available ({device}); starting logging.", flush=True)
    return 0


def main():
    try:
        return wait_for_rtc()
    except (OSError, AttributeError) as error:
        print(f"WARNING: RTC startup check failed ({error}); starting logging anyway.",
              file=sys.stderr, flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
