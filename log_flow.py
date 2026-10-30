#!/usr/bin/env python3
"""Read TUF-2000 flow rate over Modbus RTU and log it to an Excel-friendly CSV.

Uses PyModbus with pySerial. Reads holding registers 0001-0002 for flow
rate (m3/h) and 0113-0118 for net, positive and negative totals (m3).
"""

import argparse
import csv
from datetime import datetime, timezone
import math
import os
import re
from pathlib import Path
import signal
import sys
import threading
import time

import serial
from serial.tools import list_ports

from activity_led import ActivityLed

try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusSerialClient = None
    ModbusException = ()  # No library exceptions exist until it is installed.


def resolve_port(args):
    """Find the same physical FT232 adapter again after USB renumbering."""
    if args.port != "auto":
        return args.port
    identity = getattr(args, "_adapter_identity", None)
    ports = [port for port in list_ports.comports()
             if (port.vid, port.pid) == (0x0403, 0x6001)]
    if identity is not None:
        ports = [port for port in ports if port.serial_number == identity]
    elif args.serial_number:
        ports = [port for port in ports if port.serial_number == args.serial_number]
    if not ports:
        raise OSError("SH-U11/FT232 adapter not found" +
                      (f" (USB serial {identity or args.serial_number})"
                       if identity or args.serial_number else "") +
                      "; check USB connection/device visibility; use --list-ports")
    if len(ports) != 1:
        raise OSError("Multiple matching FT232 adapters; use --serial-number to select one")
    port = ports[0]
    if not port.serial_number:
        raise OSError("Adapter has no USB serial number; use an explicit --port path")
    args._adapter_identity = port.serial_number
    if getattr(args, "_resolved_port", None) != port.device:
        if not args.quiet:
            print(f"Adapter: {port.device}, USB serial {port.serial_number}", flush=True)
        args._resolved_port = port.device
    return port.device


def read_flow(args):
    if ModbusSerialClient is None:
        raise RuntimeError("PyModbus is not installed; install requirements.txt in the virtual environment")
    client = ModbusSerialClient(
        port=resolve_port(args), baudrate=args.baud, bytesize=8,
        parity=args.parity, stopbits=args.stopbits, timeout=args.timeout,
        retries=0,  # The logger controls retries, delay and adapter rediscovery.
    )
    try:
        if not client.connect():
            raise OSError("Could not open the serial adapter")
        response = client.read_holding_registers(address=0, count=2, device_id=args.slave_id)
        if response.isError():
            raise ValueError(f"Meter returned Modbus exception code {response.exception_code}")
        registers = response.registers
        if len(registers) != 2:
            raise ValueError(f"Expected 2 registers, received {len(registers)}")
        value = client.convert_from_registers(
            registers, data_type=client.DATATYPE.FLOAT32, word_order=args.word_order)
        if not math.isfinite(value):
            raise ValueError(f"Invalid flow value from registers {registers}")
        totals_response = client.read_holding_registers(address=112, count=6, device_id=args.slave_id)
        if totals_response.isError():
            raise ValueError(f"Totalizer read returned Modbus exception code {totals_response.exception_code}")
        total_registers = totals_response.registers
        if len(total_registers) != 6:
            raise ValueError(f"Expected 6 totalizer registers, received {len(total_registers)}")
        # Manual order is NET, POS, NEG, each a float in cubic metres.
        totals = tuple(client.convert_from_registers(
            total_registers[offset:offset + 2], data_type=client.DATATYPE.FLOAT32,
            word_order=args.word_order) for offset in (0, 2, 4))
        if not all(math.isfinite(total) for total in totals):
            raise ValueError(f"Invalid totalizer values from registers {total_registers}")
        return value, tuple(registers), totals
    except ModbusException as exc:
        raise OSError(f"Modbus communication failed: {exc}") from exc
    finally:
        client.close()


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


def duration(value):
    """Accept seconds by default, or an explicit s/m/h suffix."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*([smh]?)\s*", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError("use a positive duration, e.g. 5s, 5m, 1h or 300")
    return positive(positive(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2].lower()])


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--port", default="auto", help="auto-detect SH-U11, or specify a serial device path")
    parser.add_argument("--serial-number", help="USB adapter serial number, distinct from the meter's Modbus address")
    parser.add_argument("--list-ports", action="store_true", help="list visible ports and USB serial numbers, then exit")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--parity", choices=["N", "E", "O"], default="N")
    parser.add_argument("--stopbits", type=int, choices=[1, 2], default=1, help="serial stop bits")
    parser.add_argument("--slave-id", type=int, default=1, help="meter address from M46")
    parser.add_argument("--word-order", choices=["little", "big"], default="little")
    parser.add_argument("--interval", type=duration, default=300, help="time between samples: 5s, 5m, 1h; bare numbers mean seconds")
    parser.add_argument("--timeout", type=duration, default=2, help="serial response timeout, e.g. 2s")
    parser.add_argument("--retries", type=nonnegative_int, default=2, help="extra attempts after a failed read; 0 disables retries")
    parser.add_argument("--retry-delay", type=duration, default=0.5, help="pause between attempts, e.g. 0.5s")
    parser.add_argument("--count", type=nonnegative_int, default=0, help="stop after this many samples, including errors; 0 runs continuously")
    parser.add_argument("--delimiter", choices=["comma", "semicolon", "tab"], default="comma", help="CSV column separator for your Excel locale")
    parser.add_argument("--led", choices=["off", "auto", "ACT", "led0"], default="off",
                        help="blink onboard ACT LED after successful saved readings; requires LED permissions")
    parser.add_argument("--led-stop", choices=["off", "restore"], default="off",
                        help="leave the ACT LED off on exit, or restore its previous kernel trigger")
    parser.add_argument("--utc", action="store_true", help="use UTC for row and filename timestamps")
    parser.add_argument("--quiet", action="store_true", help="suppress routine console output; still report read errors")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "readings")
    parser.add_argument("--once", action="store_true", help="take one sample and exit, nonzero on read failure")
    args = parser.parse_args()
    if args.list_ports:
        ports = sorted(list_ports.comports(), key=lambda port: port.device)
        for port in ports:
            usb_id = f"{port.vid:04x}:{port.pid:04x}" if port.vid is not None and port.pid is not None else "unknown"
            print(f"{port.device}: {port.description}; USB {usb_id}; serial={port.serial_number or 'none'}")
        if not ports:
            print("No serial ports visible. Run on the host if a sandbox hides USB devices.")
        return 0
    if ModbusSerialClient is None:
        parser.error("PyModbus is not installed. From the project directory run: "
                     ".venv/bin/python -m pip install -r requirements.txt")
    if args.serial_number and args.port != "auto":
        parser.error("--serial-number is used with --port auto; omit --port to use it")
    if not 1 <= args.slave_id <= 247:
        parser.error("--slave-id must be between 1 and 247")
    if args.baud <= 0:
        parser.error("--baud must be positive")
    if args.once and args.count not in (0, 1):
        parser.error("--once cannot be combined with --count greater than 1")

    def timestamp():
        return datetime.now(timezone.utc) if args.utc else datetime.now().astimezone()

    def report(message):
        if not args.quiet:
            print(message, flush=True)

    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())

    args.output_dir.mkdir(parents=True, exist_ok=True)
    started = timestamp()
    path = args.output_dir / f"flow_{started:%Y-%m-%d_%H-%M-%S_%f%z}.csv"
    # Exclusive create prevents accidental overwrites; BOM helps Excel detect UTF-8.
    with ActivityLed(args.led, stop_mode=args.led_stop) as led, path.open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.writer(output, delimiter={"comma": ",", "semicolon": ";", "tab": "\t"}[args.delimiter])
        writer.writerow(["date", "time", "utc_offset", "flow_rate_m3_h", "status",
                         "error", "register_0001", "register_0002",
                         "positive_total_m3", "negative_total_m3", "net_total_m3"])
        output.flush()
        os.fsync(output.fileno())
        report(f"CSV: {path}")
        report(f"Reading {args.port}, {args.baud} 8{args.parity}{args.stopbits}, address {args.slave_id}, "
               f"every {args.interval:g} seconds. Ctrl+C stops cleanly.")
        next_sample = time.monotonic()
        samples, had_error = 0, False
        while not stopping.is_set():
            stamp = timestamp()
            value, registers, totals, error = None, ("", ""), None, ""
            for attempt in range(args.retries + 1):
                try:
                    value, registers, totals = read_flow(args)
                    error = ""
                    break
                except (OSError, serial.SerialException, ValueError) as exc:
                    error = str(exc)
                    led.set_recording(False)
                    if attempt < args.retries and stopping.wait(args.retry_delay):
                        break
            status = "error" if error else "ok"
            text_value = "" if value is None else format(value, ".9g")
            net, pos, neg = ("", "", "") if totals is None else tuple(format(total, ".9g") for total in totals)
            writer.writerow([stamp.strftime("%Y-%m-%d"), stamp.strftime("%H:%M:%S"),
                             stamp.strftime("%z"), text_value, status, error, *registers, pos, neg, net])
            output.flush()
            os.fsync(output.fileno())
            led.set_recording(not error)
            message = f"{stamp.isoformat(timespec='seconds')}  "
            if error:
                print(message + "ERROR: " + error, file=sys.stderr, flush=True)
            else:
                report(message + text_value + f" m3/h; positive={pos}, negative={neg}, net={net} m3")
            samples += 1
            had_error = had_error or bool(error)
            if args.once or (args.count and samples >= args.count):
                return 1 if had_error else 0
            next_sample += args.interval
            now = time.monotonic()
            if next_sample < now:
                next_sample += (math.floor((now - next_sample) / args.interval) + 1) * args.interval
            stopping.wait(max(0, next_sample - time.monotonic()))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as exc:
        sys.exit(f"File/operating-system error: {exc}")
