# Flow meter CSV logger

## Flow logger

For Raspberry Pi OS with Python 3.10 or newer, clone the repository and install
the dependencies in a local virtual environment:

```bash
sudo apt update
sudo apt install git python3-venv
git clone https://github.com/andlee01/flowmeter.git
cd flowmeter
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

For a private repository, use an authenticated GitHub connection to clone it.
If your user cannot access the USB serial device, add it to `dialout` and then
log out and back in:

```bash
sudo usermod -aG dialout "$USER"
```

The requirements pin PyModbus to 3.11.3 to match the client API used here.
Run commands below from the cloned `flowmeter` directory. To run the tests:

```bash
.venv/bin/python -m unittest -v test_log_flow.py
```

Without PyModbus, transport tests are explicitly skipped. The logger refuses
sampling until it is installed, while `--help` and `--list-ports` still work.

`log_flow.py` uses PyModbus for Modbus RTU framing, CRC validation, response
handling and float conversion, with pySerial providing serial access. It reads
instantaneous flow rate in m3/h immediately, then every 300 seconds. These are
point samples, not five-minute average flow or accumulated volume.

Before running, remove the loopback wires and connect the meter's RS485 pair
to the SH-U11 A+/B- terminals according to the meter wiring diagram. Close
QModMaster. Check M46 for the meter address, M62 for baud/parity, and M63 for
MODBUS_RTU. The defaults below assume address 1 and 9600 baud, 8N1; the manual
says the factory protocol may instead be Modbus ASCII, which this logger does
not support. No meter settings are changed by the script.

Run in a terminal with access to the USB serial device:

```bash
# Verify one reading against the display first.
.venv/bin/python log_flow.py --once

# Continue logging until Ctrl+C.
.venv/bin/python log_flow.py

# Example with a different meter address and baud rate:
.venv/bin/python log_flow.py --slave-id 2 --baud 19200
```

### Command-line options

Durations accept `s` (seconds), `m` (minutes), or `h` (hours), including
fractions such as `0.5s` and `1.5m`. Bare numbers still mean seconds.

```bash
# Every five seconds, stop after 12 samples.
.venv/bin/python log_flow.py --interval 5s --count 12

# Every five minutes, save to Downloads using UTC timestamps.
.venv/bin/python log_flow.py --interval 5m --utc --output-dir ~/Downloads

# Allow a longer response timeout, retry up to three times.
.venv/bin/python log_flow.py --timeout 3s --retries 3 --retry-delay 1s
```

| Option | Purpose | Default |
| --- | --- | --- |
| `--interval 5m` | Time between sample starts | 5 minutes |
| `--count 12` | Stop after this many rows (errors included); 0 means unlimited | 0 |
| `--once` | One reading; equivalent to count 1 | Off |
| `--port PATH` | Auto-detect SH-U11, or specify a serial device path | `auto` |
| `--serial-number SERIAL` | Select a particular USB adapter when several are attached | Auto-select a unique adapter |
| `--list-ports` | Show device paths, USB IDs and serial numbers, then exit | Off |
| `--slave-id 1` | Meter's Modbus address (M46) | 1 |
| `--baud 9600` | Serial baud rate (M62) | 9600 |
| `--parity N` | N, E or O parity (M62) | N |
| `--stopbits 1` | 1 or 2 stop bits | 1 |
| `--timeout 2s` | Maximum response wait per attempt | 2 seconds |
| `--retries 2` | Additional attempts after failure; 0 disables retries | 2 |
| `--retry-delay 0.5s` | Pause between attempts | 0.5 seconds |
| `--output-dir PATH` | Directory for new timestamped CSV files | `flowmeter/readings` |
| `--delimiter semicolon` | `comma`, `semicolon` or `tab`, for Excel import | comma |
| `--utc` | UTC row and filename timestamps rather than local time | Off |
| `--quiet` | Suppress routine output; read errors still go to stderr | Off |
| `--word-order little` | Register word order: little or big | little |

Use `--help` for all options. Data bits remain fixed at 8 and protocol remains
Modbus RTU. Changing the serial options only configures the computer; match
them to the meter. The interval is time between samples, not samples per second.
Sampling faster than the meter updates may repeat the same value. Failed reads
can take longer than the chosen interval; missed slots are skipped. With a
finite count, exit status is 1 if any recorded sample failed, otherwise 0.

### Moving the adapter to another USB socket

The logger now defaults to `--port auto`. It searches for FT232 adapters with
USB ID `0403:6001`, matching the detected SH-U11. These IDs identify the chip,
not the adapter model uniquely. When exactly one is present, its USB serial
number is remembered for this run. Every read attempt rechecks the device path
for that serial number, so moving the adapter or renumbering `/dev/ttyUSB0` to
`/dev/ttyUSB1` is handled automatically. During disconnection, samples can be
recorded as errors; subsequent samples resume when the same adapter returns.

With multiple adapters, specify which one to use:

```bash
.venv/bin/python log_flow.py --list-ports
.venv/bin/python log_flow.py --serial-number YOUR_USB_SERIAL --interval 5m
```

Specifying the serial number also preserves the selection across script
restarts. Ambiguous matches, duplicate serial numbers and adapters without a
serial number are rejected in auto mode rather than choosing an arbitrary
port. You can still use `--port /dev/serial/by-id/...` or an explicit device
path. Explicit paths are used as given and disable discovery. USB permissions
and sandbox visibility still apply.

Each run creates a new timestamped file in `readings/`,
for example `flow_2026-10-24_14-30-00_123456+0100.csv`. Override the directory
with `--output-dir ~/Downloads` if preferred. Files contain a header,
comma separators, Windows line endings and a UTF-8 BOM for Excel. Each row has
local date, local time, UTC offset, numeric flow rate, status, error, and the
two raw register values. Separate date/time columns allow Excel to interpret
them as dates and times; the offset distinguishes repeated times when clocks
change. Import through Excel's Data > From Text/CSV if necessary.

Rows are flushed and synced after each sample. A failed sample is retried up
to twice, then recorded with a blank flow value and an error; the next sample
is still attempted. There is no zero substitution. Scheduling uses a monotonic
clock to avoid drift or clock-change effects; overdue slots are skipped rather
than read in a burst. The computer must remain awake and the process running.

The register mapping comes from the companion TUF-2000 communication document:
function 03, zero-based address 0, two registers, IEEE754 flow rate in m3/h.
The TSONIC TUF-2000 series user manual covers operation; the register table
is in the separate communication reference linked below. Compatibility with ZT-LU-2001BBJ has not been confirmed on the hardware.
Low-word-first decoding is the initial assumption; compare a one-shot reading
with the meter display (convert display units if necessary). `--word-order big`
is available for other firmware. A finite decoded number alone does not prove
the mapping/word order is correct, or that the meter's ultrasonic signal is valid.
The logger does not currently read the meter's diagnostic status register.

Communication reference:
https://doc.diytrade.com/docdvr/2386800/51832895/1742695590.pdf

## Serial loopback test

This test uses pySerial, the serial transport library also used by PyModbus.
Raw serial loopback verifies the transport; it cannot validate Modbus requests
or the flow meter's registers. PyModbus is not needed for this stage.

Use the same virtual environment for the loopback test:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Hardware setup

Disconnect the flow meter from the adapter for this test. Using the SH-U11's
separate RS422 transmit and receive terminals, connect:

- TXD+ (may be labelled T/R+ or A+) to RXD+.
- TXD- (may be labelled T/R- or B-) to RXD-.

Follow the labels on your adapter rather than assuming a pin order. Do not
short A+ to B-. If your adapter has only A/B/GND and no separate receive pair,
this single-adapter wiring test does not apply; use a second RS485 adapter
or an echoing peer. A failure to receive may reflect the adapter's receiver
enable behavior and is not conclusive evidence of faulty hardware.

Close QModMaster or any other program using the serial port. Run these commands
in a terminal on the host, where `/dev/ttyUSB0` is visible:

```bash
.venv/bin/python serial_loopback.py --list
.venv/bin/python serial_loopback.py --open-only
.venv/bin/python serial_loopback.py
```

The default is `/dev/ttyUSB0`, 9600 baud, 8 data bits, no parity, one stop bit.
Override the port with `--port /dev/serial/by-id/...` if desired. The script sends
five binary/text payloads, checks that each comes back unchanged, and exits
nonzero on mismatch or an access error. Open-only sends no test data.

## Software check

```bash
.venv/bin/python serial_loopback.py --port loop://
```

`loop://` simulates serial echo entirely in software. Passing this check does
not test the USB adapter, driver, wiring, or device permissions.

## Sources

- SH-U11 manufacturer: https://www.deshide.com/product-details_SH-U11.html
- pySerial usage: https://pyserial.readthedocs.io/en/latest/shortintro.html
- Simulated loopback: https://pyserial.readthedocs.io/en/latest/url_handlers.html#loop

PyModbus client API: https://pymodbus.readthedocs.io/en/v3.11.3/source/client.html
