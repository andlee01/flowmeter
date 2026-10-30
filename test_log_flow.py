"""Exercise the real pySerial transport against a simulated meter on a PTY."""

import csv
import os
from pathlib import Path
import pty
import select
import struct
import subprocess
import sys
import tempfile
import threading
import unittest

from log_flow import duration, resolve_port, read_flow, ModbusSerialClient
import argparse
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def crc16(data):
    # Independent RTU meter emulator; production framing/CRC is handled by PyModbus.
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return struct.pack("<H", crc)


class LoggerTest(unittest.TestCase):
    def test_client_is_closed_on_connection_or_meter_error(self):
        args = SimpleNamespace(port="/dev/test", baud=9600, parity="N", stopbits=1,
                               timeout=2, slave_id=1, word_order="little")
        for connected in (False, True):
            client = MagicMock()
            client.connect.return_value = connected
            response = client.read_holding_registers.return_value
            response.isError.return_value = True
            response.exception_code = 2
            with patch("log_flow.ModbusSerialClient", return_value=client):
                with self.assertRaises((OSError, ValueError)):
                    read_flow(args)
            client.close.assert_called_once()
            if not connected:
                client.read_holding_registers.assert_not_called()

    def test_invalid_library_result_is_rejected(self):
        args = SimpleNamespace(port="/dev/test", baud=9600, parity="N", stopbits=1,
                               timeout=2, slave_id=1, word_order="little")
        client = MagicMock()
        client.connect.return_value = True
        response = client.read_holding_registers.return_value
        response.isError.return_value = False
        response.registers = [0, 0]
        client.convert_from_registers.return_value = float("nan")
        with patch("log_flow.ModbusSerialClient", return_value=client):
            with self.assertRaisesRegex(ValueError, "Invalid flow"):
                read_flow(args)
        client.close.assert_called_once()

    @staticmethod
    def adapter(device="/dev/ttyUSB0", serial_number="SH123"):
        return SimpleNamespace(device=device, serial_number=serial_number, vid=0x0403, pid=0x6001)

    def test_adapter_follows_serial_number_after_usb_move(self):
        args = SimpleNamespace(port="auto", serial_number=None, quiet=True)
        with patch("log_flow.list_ports.comports", return_value=[self.adapter()]):
            self.assertEqual(resolve_port(args), "/dev/ttyUSB0")
        with patch("log_flow.list_ports.comports", return_value=[
                self.adapter("/dev/ttyUSB0", "OTHER"), self.adapter("/dev/ttyUSB2")]):
            self.assertEqual(resolve_port(args), "/dev/ttyUSB2")
        with patch("log_flow.list_ports.comports", return_value=[self.adapter(serial_number="OTHER")]):
            with self.assertRaises(OSError):
                resolve_port(args)

    def test_adapter_requires_unambiguous_identity(self):
        args = SimpleNamespace(port="auto", serial_number=None, quiet=True)
        for ports in ([], [self.adapter(), self.adapter("/dev/ttyUSB1", "OTHER")],
                      [self.adapter(serial_number=None)]):
            with patch("log_flow.list_ports.comports", return_value=ports), self.assertRaises(OSError):
                resolve_port(args)
        args.serial_number = "OTHER"
        with patch("log_flow.list_ports.comports", return_value=[self.adapter(), self.adapter("/dev/ttyUSB1", "OTHER")]):
            self.assertEqual(resolve_port(args), "/dev/ttyUSB1")

    def run_meter(self, replies, extra_args=None, count=1, delimiter=","):
        if ModbusSerialClient is None:
            self.skipTest("PyModbus not installed; serial integration tests require requirements.txt")
        master, slave = pty.openpty()
        path = os.ttyname(slave)
        requests, failures = [], []

        def meter():
            try:
                for reply in replies:
                    request = b""
                    while len(request) < 8:
                        if not select.select([master], [], [], 5)[0]:
                            raise TimeoutError("Logger did not send request")
                        request += os.read(master, 8 - len(request))
                    requests.append(request)
                    if reply is not None:
                        os.write(master, reply)
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=meter, daemon=True)
        try:
            with tempfile.TemporaryDirectory() as directory:
                thread.start()
                result = subprocess.run(
                    [sys.executable, str(Path(__file__).with_name("log_flow.py")),
                     "--port", path, "--count", str(count), "--timeout", "0.15",
                     "--output-dir", directory, *(extra_args or [])], capture_output=True, text=True, timeout=10)
                thread.join(6)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
                files = list(Path(directory).glob("flow_*.csv"))
                self.assertEqual(len(files), 1)
                contents = files[0].read_bytes()
                self.assertTrue(contents.startswith(b"\xef\xbb\xbf"))
                self.assertIn(b"\r\n", contents)
                with files[0].open(encoding="utf-8-sig", newline="") as source:
                    rows = list(csv.DictReader(source, delimiter=delimiter))
                self.assertEqual(len(rows), count)
                for request in requests:
                    # Address 1, read holding registers, offset 0, count 2.
                    self.assertEqual(request, bytes.fromhex("01 03 00 00 00 02 c4 0b"))
                self.assertRegex(rows[0]["date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertRegex(rows[0]["utc_offset"], r"^[+-]\d{4}$")
                return result, rows[0]
        finally:
            os.close(master)
            os.close(slave)

    @staticmethod
    def reply(value):
        high, low = struct.unpack(">HH", struct.pack(">f", value))
        payload = b"\x01\x03\x04" + struct.pack(">HH", low, high)
        return payload + crc16(payload)

    def test_positive_negative_and_zero_flow(self):
        for value in (12.5, -2.75, 0.0):
            with self.subTest(value=value):
                result, row = self.run_meter([self.reply(value)])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(row["status"], "ok")
                self.assertEqual(float(row["flow_rate_m3_h"]), value)
                self.assertEqual(row["error"], "")

    def test_crc_failure_then_recovery(self):
        result, row = self.run_meter([b"\x01\x03\x04\x00\x00\x00\x00\x00\x00",
                                      self.reply(7.25)])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(float(row["flow_rate_m3_h"]), 7.25)

    def test_timeout_records_blank_instead_of_zero(self):
        result, row = self.run_meter([None, None, None])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["flow_rate_m3_h"], "")
        self.assertIn("modbus communication failed", row["error"].lower())

    def test_modbus_exception_is_recorded(self):
        payload = bytes.fromhex("01 83 02")
        result, row = self.run_meter([payload + crc16(payload)] * 3)
        self.assertEqual(result.returncode, 1)
        self.assertIn("exception code 2", row["error"])

    def test_duration_units_and_rejections(self):
        for text, expected in [("5s", 5), ("5m", 300), ("1.5m", 90),
                               ("1h", 3600), ("300", 300), (" 5M ", 300)]:
            self.assertEqual(duration(text), expected)
        for text in ("0s", "-5m", "nan", "inf", "5ms", "five", ""):
            with self.subTest(text=text), self.assertRaises(argparse.ArgumentTypeError):
                duration(text)

    def test_multiple_samples_semicolon_utc_and_quiet(self):
        result, row = self.run_meter([self.reply(1.25)] * 3,
                                    ["--interval", "0.1s", "--delimiter", "semicolon",
                                     "--utc", "--quiet"], count=3, delimiter=";")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(row["utc_offset"], "+0000")

    def test_retries_disabled_and_error_visible_when_quiet(self):
        result, row = self.run_meter([None], ["--retries", "0", "--quiet"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("ERROR:", result.stderr)
        self.assertEqual(row["status"], "error")


if __name__ == "__main__":
    unittest.main()
