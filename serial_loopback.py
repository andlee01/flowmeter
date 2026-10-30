#!/usr/bin/env python3
"""Test pySerial with loop:// or an SH-U11 TX-to-RX hardware loopback."""

import argparse
import sys

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pySerial is missing. Install the 'pyserial' package (not 'serial').")


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyUSB0")
    parser.add_argument("--baud", type=positive_int, default=9600)
    parser.add_argument("--count", type=positive_int, default=5)
    parser.add_argument("--list", action="store_true", help="list visible serial ports and exit")
    parser.add_argument("--open-only", action="store_true", help="open/configure the port without sending bytes")
    args = parser.parse_args()

    if args.list:
        ports = sorted(list_ports.comports(), key=lambda p: p.device)
        for port in ports:
            print(f"{port.device}: {port.description} [{port.hwid}]")
        if not ports:
            print("No serial ports visible. A sandbox may hide host USB devices.")
        return 0

    simulated = args.port == "loop://"
    print("SOFTWARE loopback: USB hardware is not exercised." if simulated else
          "Hardware test: disconnect the meter; link TXD+ to RXD+ and TXD- to RXD-.")
    options = dict(baudrate=args.baud, bytesize=serial.EIGHTBITS,
                   parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                   timeout=2, write_timeout=2, xonxoff=False, rtscts=False,
                   dsrdtr=False)
    if sys.platform.startswith("linux") and not simulated:
        options["exclusive"] = True
    try:
        with serial.serial_for_url(args.port, **options) as port:
            print(f"OPEN OK: {args.port}, {args.baud} baud, 8N1, pySerial {serial.VERSION}")
            if args.open_only:
                print("Access/configuration verified; transmit/receive has not been tested.")
                return 0
            port.reset_input_buffer()
            for sequence in range(1, args.count + 1):
                # Exercise binary values as well as text, with a changing sequence.
                sent = f"SH-U11 loopback {sequence}\r\n".encode() + bytes(range(256))
                written = port.write(sent)
                port.flush()
                received = port.read(len(sent))
                if written != len(sent) or received != sent:
                    print(f"FAIL {sequence}: wrote {written}/{len(sent)} bytes, "
                          f"received {len(received)}/{len(sent)}", file=sys.stderr)
                    print(f"RX (first 64 bytes): {received[:64].hex(' ')}", file=sys.stderr)
                    print("Check the TX/RX links and terminal labels. A timeout does not "
                          "by itself prove the adapter is faulty.", file=sys.stderr)
                    return 1
                print(f"PASS {sequence}/{args.count}: {len(sent)} bytes matched exactly")
            print("PASS: software loopback only." if simulated else
                  "PASS: hardware loopback matched. Meter/Modbus communication is still untested.")
            return 0
    except (serial.SerialException, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print("Check the port path, device permissions, and whether another app has it open. "
              "If the host has /dev/ttyUSB0 but this session does not, run in a host terminal.",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
