#!/usr/bin/env python3
"""Install the flowmeter systemd service on Raspberry Pi OS; run with sudo python3."""

import argparse
from datetime import datetime, timezone
import grp
import math
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parent
UNIT = Path("/etc/systemd/system/flowmeter.service")
RULE = Path("/etc/udev/rules.d/99-flowmeter-led.rules")


def interval(value):
    match = re.fullmatch(r"(\d+(?:\.\d*)?|\.\d+)([smh]?)", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError("use a duration such as 10s, 5m or 300")
    seconds = float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2].lower()]
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("interval must be a finite positive duration")
    return value.lower()


def quote(value):
    """Quote one systemd argument; ExecStart's ':' prefix disables dollar expansion."""
    value = str(value)
    if any(ord(char) < 32 for char in value):
        raise ValueError("Control characters cannot be used in service paths/options")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_service(args, user, project):
    command = [str(project / ".venv/bin/python"), "-u", str(project / "log_flow.py"),
               "--interval", args.interval, "--port", args.port,
               "--slave-id", str(args.slave_id), "--baud", str(args.baud),
               "--parity", args.parity, "--stopbits", str(args.stopbits),
               "--word-order", args.word_order, "--led", args.led]
    if args.serial_number:
        command += ["--serial-number", args.serial_number]
    if args.output_dir:
        command += ["--output-dir", str(args.output_dir.resolve())]
    groups = "dialout" + (" gpio" if args.led != "off" else "")
    rtc = ""
    if args.require_rtc:
        check = [str(project / ".venv/bin/python"), "-u", str(project / "wait_for_rtc.py")]
        # '-' means a failed check must never prevent the logger from starting.
        rtc = "ExecStartPre=-:" + " ".join(quote(arg) for arg in check) + "\nTimeoutStartSec=130\n"
    return ("[Unit]\nDescription=Flow meter CSV logger\n"
            "After=local-fs.target systemd-udev-trigger.service time-set.target\n" +
            "\n[Service]\nType=simple\n" +
            f"User={user}\nSupplementaryGroups={groups}\n" +
            f"WorkingDirectory={str(project).replace(chr(37), chr(37) * 2)}\n" +
            rtc + "ExecStart=:" + " ".join(quote(arg) for arg in command) + "\n" +
            "Restart=always\nRestartSec=10\nTimeoutStopSec=30\n" +
            "\n[Install]\nWantedBy=multi-user.target\n")


def install_text(path, content):
    """Back up changed configuration, then replace atomically with root-readable text."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(path.name + ".backup." + stamp)
        shutil.copy2(path, backup)
        print(f"Backup: {backup}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print(f"Installed: {path}")


def manual_loggers(project, user_id, service_pid):
    result = []
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            if int(entry.name) == service_pid or entry.stat().st_uid != user_id:
                continue
            args = (entry / "cmdline").read_bytes().split(b"\x00")
            for arg in args:
                if not arg or Path(os.fsdecode(arg)).name != "log_flow.py":
                    continue
                script = Path(os.fsdecode(arg))
                if not script.is_absolute():
                    script = (entry / "cwd").resolve() / script
                if script.resolve() == project / "log_flow.py":
                    result.append(int(entry.name))
                    break
        except (OSError, ValueError):
            continue
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__,
                                    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    result.add_argument("--user", help="service user; normally inferred from sudo")
    result.add_argument("--interval", type=interval, default="10s", help="logging interval, e.g. 10s or 5m")
    result.add_argument("--port", default="auto", help="USB auto-detection or explicit serial path")
    result.add_argument("--serial-number", help="USB adapter serial number for auto-detection")
    result.add_argument("--slave-id", type=int, default=1, help="Modbus meter address")
    result.add_argument("--baud", type=int, default=9600)
    result.add_argument("--parity", choices=["N", "E", "O"], default="N")
    result.add_argument("--stopbits", type=int, choices=[1, 2], default=1)
    result.add_argument("--word-order", choices=["little", "big"], default="little")
    result.add_argument("--led", choices=["auto", "off", "ACT", "led0"], default="auto")
    result.add_argument("--output-dir", type=Path, help="CSV directory; default is project/readings")
    result.add_argument("--wait-for-rtc", "--require-rtc", dest="require_rtc", action="store_true",
                        help="wait for rtc0 until 2 minutes after boot, then start anyway; --require-rtc is a compatibility alias")
    result.add_argument("--no-start", action="store_true", help="enable for boot without starting/restarting now")
    result.add_argument("--dry-run", action="store_true", help="show service configuration without installing or starting anything")
    return result


def main():
    options = parser()
    args = options.parse_args()
    user = args.user or os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name
    if user == "root":
        options.error("use sudo from your normal account, or specify --user YOUR_USERNAME")
    account = pwd.getpwnam(user)
    if account.pw_uid == 0:
        options.error("the logger must run as a normal user, not a root account")
    if not 1 <= args.slave_id <= 247 or args.baud <= 0:
        options.error("--slave-id must be 1-247 and --baud must be positive")
    if args.serial_number and args.port != "auto":
        options.error("--serial-number requires --port auto")
    content = render_service(args, user, PROJECT)
    if args.dry_run:
        print(content)
        print(f"Would install {UNIT}; LED permissions: {args.led != 'off'}; "
              f"start/restart now: {not args.no_start}")
        return 0
    if os.geteuid() != 0:
        options.error("run with sudo python3 setup_service.py")
    interpreter = PROJECT / ".venv/bin/python"
    if not interpreter.exists():
        raise RuntimeError("Create .venv and install requirements.txt as your normal user first (see README)")
    for group in (["dialout", "gpio"] if args.led != "off" else ["dialout"]):
        grp.getgrnam(group)
    # Import the actual logger as its service user, without creating root-owned caches.
    subprocess.run(["runuser", "-u", user, "--", str(interpreter), "-B", "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]); "
                    "import serial, pymodbus, log_flow; "
                    "assert log_flow.ModbusSerialClient is not None", str(PROJECT)], check=True)
    if not args.no_start:
        process = subprocess.run(["systemctl", "show", "--property=MainPID", "--value", "flowmeter.service"],
                                 capture_output=True, text=True)
        service_pid = int(process.stdout.strip() or "0") if process.returncode == 0 else 0
        running = manual_loggers(PROJECT, account.pw_uid, service_pid)
        if running:
            raise RuntimeError(f"A manual logger is running (PID(s) {running}); stop it with Ctrl+C and rerun setup")
    # Check syntax before replacing any installed service.
    with tempfile.TemporaryDirectory() as directory:
        candidate = Path(directory) / "flowmeter.service"
        candidate.write_text(content)
        subprocess.run(["systemd-analyze", "verify", str(candidate)], check=True)
    install_text(UNIT, content)
    if args.led != "off":
        install_text(RULE, (PROJECT / "99-flowmeter-led.rules").read_text())
    if args.led != "off":
        subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
    if args.led != "off":
        subprocess.run(["udevadm", "trigger", "--subsystem-match=leds", "--action=add"], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "flowmeter.service"], check=True)
    if not args.no_start:
        subprocess.run(["systemctl", "restart", "flowmeter.service"], check=True)
        subprocess.run(["systemctl", "is-active", "--quiet", "flowmeter.service"], check=True)
        print("Flowmeter service is running and enabled at boot.")
    else:
        print("Flowmeter service enabled at boot; existing running instance was not restarted.")
    print(f"CSV directory: {args.output_dir.resolve() if args.output_dir else PROJECT / 'readings'}")
    print("Check: systemctl status flowmeter.service")
    print("Logs: journalctl -u flowmeter.service -n 20 --no-pager")
    print("Stop: sudo systemctl stop flowmeter")
    print("No reboot is required to start logging. Do not also run the logger manually.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, KeyError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(f"ERROR: {error}")
