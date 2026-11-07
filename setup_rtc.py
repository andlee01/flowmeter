#!/usr/bin/env python3
"""Configure a DS3231 RTC on Raspberry Pi OS Bookworm/Trixie (Python standard library only)."""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

BEGIN = "# BEGIN FLOWMETER DS3231 RTC"
END = "# END FLOWMETER DS3231 RTC"
RTC_ROOT = Path("/sys/class/rtc")


def boot_config():
    for candidate in (Path("/boot/firmware/config.txt"), Path("/boot/config.txt")):
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("Cannot find the Raspberry Pi boot config.txt")


def build_boot_config(text):
    """Keep other settings intact and append an idempotent, global RTC block."""
    if BEGIN in text or END in text:
        if text.count(BEGIN) != 1 or text.count(END) != 1:
            raise RuntimeError("Malformed existing FLOWMETER RTC block; review config.txt first")
        pattern = rf"(?ms)^{re.escape(BEGIN)}\n.*?^{re.escape(END)}(?:\n|$)"
        text, count = re.subn(pattern, "", text)
        if count != 1:
            raise RuntimeError("Malformed existing FLOWMETER RTC block; review config.txt first")
    section, existing = "all", False
    for line in text.splitlines():
        value = line.split("#", 1)[0].strip()
        if re.fullmatch(r"\[.*\]", value):
            section = value[1:-1].lower()
        if re.match(r"dtoverlay\s*=\s*i2c-rtc(?:-gpio)?(?:,|$)", value):
            compact = re.sub(r"\s+", "", value)
            if compact != "dtoverlay=i2c-rtc,ds3231" or section != "all":
                raise RuntimeError("Existing custom, conditional, or different RTC overlay found: "
                                   f"{line.strip()}. Review it before running setup.")
            if existing:
                raise RuntimeError("Multiple RTC overlays found; review config.txt first")
            existing = True
    lines = [BEGIN, "[all]", "dtparam=i2c_arm=on"]
    if not existing:
        lines.append("dtoverlay=i2c-rtc,ds3231")
    lines.append(END)
    return text.rstrip() + "\n\n" + "\n".join(lines) + "\n"


def update_config(path, updated):
    if path.read_text() == updated:
        print(f"Boot configuration already set: {path}")
        return
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(path.name + ".before-rtc." + suffix)
    shutil.copy2(path, backup)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print(f"Updated: {path}\nBackup: {backup}")


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def ensure_hwclock():
    utility = shutil.which("hwclock")
    if utility is None:
        print("Installing util-linux-extra (provides hwclock)...", flush=True)
        run(["apt-get", "update"])
        run(["apt-get", "install", "-y", "util-linux-extra"])
        utility = shutil.which("hwclock")
    if utility is None:
        raise RuntimeError("hwclock was not found after installation")
    return utility


def ds3231_device():
    for rtc in sorted(RTC_ROOT.glob("rtc[0-9]*")):
        if "ds3231" in (rtc / "name").read_text().lower():
            if rtc.name != "rtc0":
                raise RuntimeError("DS3231 is not rtc0; this script expects it to be the "
                                   "primary RTC on a Pi 2/3. Review other RTC devices first.")
            return Path("/dev") / rtc.name
    raise RuntimeError("No DS3231 RTC registered. Run setup, reboot, then retry --init. "
                       "If already rebooted, check board orientation/connection and battery.")


def initialise_rtc(trust_system_time=False):
    device = ds3231_device()
    # Check time BEFORE any command that writes the RTC or changes clock policy.
    synced = run(["timedatectl", "show", "--property=NTPSynchronized", "--value"],
                 capture_output=True, text=True).stdout.strip() == "yes"
    if not synced and not trust_system_time:
        raise RuntimeError("System time is not NTP-synchronised. Connect to the internet, "
                           "check timedatectl status and retry. If you have manually set "
                           "and verified the correct system time, use --init --trust-system-time.")
    utility = ensure_hwclock()
    print("Writing current system time to DS3231 in UTC:",
          datetime.now().astimezone().isoformat(timespec="seconds"), flush=True)
    run([utility, "--systohc", "--utc", "--rtc", str(device)])
    run([utility, "--show", "--utc", "--rtc", str(device)])
    print("RTC initialised. Internet time synchronisation remains enabled.")
    print("At future boots Linux should initialise system time from rtc0, including offline.")
    print("After reboot, verify: cat /sys/class/rtc/rtc0/hctosys  (expected: 1)")
    print("Then test battery retention with a proper shutdown and a brief power-off.")


def status():
    print("System time:", datetime.now().astimezone().isoformat(timespec="seconds"))
    run(["timedatectl", "status"])
    devices = sorted(RTC_ROOT.glob("rtc[0-9]*"))
    if not devices:
        print("No RTC registered yet.")
    for rtc in devices:
        print(f"{rtc.name}: {(rtc / 'name').read_text().strip()}")
        for name in ("date", "time", "hctosys"):
            attribute = rtc / name
            if attribute.exists():
                print(f"  {name}: {attribute.read_text().strip()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--init", action="store_true", help="after reboot: copy verified system time into the RTC")
    actions.add_argument("--status", action="store_true", help="show system/RTC status without changing anything")
    actions.add_argument("--dry-run", action="store_true", help="show planned boot configuration without installing or writing anything")
    parser.add_argument("--trust-system-time", action="store_true", help="with --init: explicitly allow a manually verified clock without NTP sync")
    args = parser.parse_args()
    if args.trust_system_time and not args.init:
        parser.error("--trust-system-time requires --init")
    if args.status:
        status()
        return 0
    if not args.dry_run and os.geteuid() != 0:
        parser.error("run this script with sudo python3 setup_rtc.py")
    model = Path("/proc/device-tree/model")
    if not model.exists() or "Raspberry Pi" not in model.read_text():
        raise RuntimeError("This setup script is for Raspberry Pi hardware")
    if args.init:
        initialise_rtc(args.trust_system_time)
        return 0
    path = boot_config()
    updated = build_boot_config(path.read_text())
    if args.dry_run:
        print(f"Would configure: {path} (a dated backup is made if changed)")
        print(updated)
        print("Would install util-linux-extra if hwclock is missing. No reboot is automatic.")
        return 0
    ensure_hwclock()
    update_config(path, updated)
    print("Setup configured. This script has not rebooted or changed either clock.")
    print("Stop logging and reboot when ready: sudo reboot")
    print("After reboot, run from this directory: sudo python3 setup_rtc.py --init")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"ERROR: {error}")
