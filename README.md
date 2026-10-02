# X240C I2C Logger

English | [日本語](README.ja.md)

A Python logger and decoder for PowerPi X240C I2C traffic. Current version: **0.4.5**.
It accepts PicoXTools WebSocket streams, saved text logs, or serial output from an RP2040 PIO sniffer, and records register operations, OTG voltage settings, and estimated current limits to CSV.

## Hardware

- [PowerPi X240C (Taobao)](https://item.taobao.com/item.htm?id=1071762544986)
- [PicoXtools (Taobao)](https://item.taobao.com/item.htm?id=895785154737)

## Features

- Reconstructs I2C transactions from PicoXTools binary events across WebSocket message boundaries
- Decodes reads, writes, and multiple phases connected by repeated STARTs
- Records ACK/NACK information, treating a NACK on the final read byte as normal
- Decodes registers at the default 7-bit I2C address `0x6C`
- Exports a physical-transaction CSV and a SET summary CSV grouping operations associated with each voltage change

## Requirements and installation

Use Python 3.9 or later and the hardware or log file appropriate for your input method.
For PicoXTools, your PC must be able to reach the device at `ws://<host>/ws/i2c`.
RP2040 firmware and hardware wiring instructions are not included in this repository.

```powershell
git clone https://github.com/inuchanbt/x240c-i2c-logger.git
cd x240c-i2c-logger
py -m pip install -r requirements.txt
```

The examples use the Windows `py` launcher. On other systems, substitute your Python command, such as `python3`.
`websocket-client` is used for WebSocket input; `pyserial` is used for serial input and listing serial ports.

## Usage

### Connect directly to PicoXTools

```powershell
py x240c_i2c_logger.py --picotools
py x240c_i2c_logger.py --picotools 192.168.33.1
```

The logger automatically enables **I2C Sniffer** through the device's HTTP setup API before opening the WebSocket. You no longer need to open the I2C settings page first. Connect **SCL to GPIO9** and **SDA to GPIO8**, with a common ground.
The setup API must be reachable at `http://<host>/api/setup?type=i2c` (HTTPS for a `wss://` URL). This follows the web UI shipped in PicoXTools 3.3.1; other firmware versions may differ. Setup failures stop the capture with an error.

To use an existing device configuration or firmware requiring manual setup:

```powershell
py x240c_i2c_logger.py --picotools --no-picotools-setup
```

Automatic setup selects Sniffer mode and may stop an active SPI Sniffer, as in the device's web UI. The logger leaves I2C Sniffer enabled when it exits; use the web UI to close it if needed.
For protocol details and verification limits, see [PicoXTools automatic setup](docs/picotools-auto-setup.md).

The default host is `192.168.33.1`. On Windows, you can also launch `run_picotools.bat`.
To specify another host and additional options, use `run_picotools.bat 192.168.33.2 --ws-debug`.
The batch file switches to its own directory before starting the logger.

### Decode a saved text log

```powershell
py x240c_i2c_logger.py --input capture.txt --out logs/replay.csv
```

Provide a sniffer text log, such as text copied from PicoXTools. The generated CSV files cannot be used directly as input.
For PicoXTools text, use timestamped lines such as `14:36:00.000 S 6C W 04 D3 02 P`. Lines beginning with `S 6C ...` without a timestamp are detected as RP2040 format.

### Capture RP2040 serial output

```powershell
py x240c_i2c_logger.py --list-ports
py x240c_i2c_logger.py --port COM14 --baud 115200
```

With no input method specified, the logger reads standard input. Press `Ctrl+C` to stop a live capture.

## Output files

By default, the logger creates the following files in `logs/` under the current working directory. Output directories are created automatically.

| File | Contents |
| --- | --- |
| `x240c_i2c_YYYYMMDD_HHMMSS.csv` | Timestamps, I2C data, decoded results, flags, and raw data for each physical transaction |
| `x240c_i2c_YYYYMMDD_HHMMSS_sets.csv` | Operations grouped by voltage change, estimated current, transaction counts, completion reasons, and related fields |

CSV files use UTF-8 with a BOM. With `--out logs/capture.csv`, the default summary path is `logs/capture_sets.csv`.
A physical transaction containing repeated STARTs may contain multiple logical operations, so a SET's `transaction_count` and `operation_count` can differ.

| Option | Purpose / default |
| --- | --- |
| `--out PATH` | Transaction CSV output path |
| `--sets-out PATH` | SET summary CSV output path |
| `--no-sets` | Disable SET aggregation |
| `--set-idle-ms 300` | Idle threshold before moving the base SET into a pending state (ms) |
| `--set-tail-window-ms 5000` | Window for delayed related operations (ms) |
| `--set-tail-lead-ms 250` | Window for associating a current write immediately before DCDC disable (ms) |
| `--addr 0x6C` | Target 7-bit I2C address |
| `--rsense-mohm 10` | Effective sense resistance used for current estimation (mΩ) |
| `--no-picotools-setup` | Skip automatic Sniffer setup and use the existing configuration |
| `--ws-debug` | Display binary WebSocket messages |
| `--append` | Append to existing CSV files |
| `--quiet` | Suppress decoded console output |

Run `py x240c_i2c_logger.py --help` for all options.

## Decoding notes

The WebSocket binary format and parts of the register interpretation were inferred from observed captures. Unknown bits are retained in flags or raw data.
Voltage is decoded from the settings in registers `0x04` and `0x05`; it is not a measured output voltage.
Current decoded from register `0x06` is also an estimate. The X240C current-sense resistor scaling still requires physical verification.

Version 0.4.4 fixes a SET boundary case where one physical transaction contains both the previous SET's current write and the next SET's voltage write.
See the [v0.4.4 technical notes](docs/v0.4.4-notes.md) for the reproduction example.

## Project layout

```text
x240c-i2c-logger/
├── x240c_i2c_logger.py  # Main logger
├── run_picotools.bat    # Windows launcher
├── requirements.txt    # Python dependencies
├── tests/              # Regression tests
├── docs/               # Technical notes
├── logs/               # Captured CSV files (ignored by Git)
├── README.md           # English (main)
├── README.ja.md        # Japanese
└── LICENSE
```

Local ZIP archives of older versions are stored in `archives/`. Both these archives and captured logs are excluded from Git.

## Validation

Run the SET aggregation regression tests without external hardware:

```powershell
py tests/test_v044.py
py tests/test_picotools_setup.py
py x240c_i2c_logger.py --help
```

The setup tests use a local HTTP server and simulated WebSocket data to check startup order, failure handling, and manual-setup mode. Automatic startup and live capture were confirmed on the user's PicoXTools on 2026-10-02: 12 transactions at `0x6C`, zero flagged transactions, and both CSV output paths reported. See the [hardware verification notes](docs/picotools-auto-setup.md#verification).

The regression tests cover SET boundaries across repeated STARTs, prevention of incorrect current association across separate transactions, and aggregation of delayed DCDC-related operations.

## License

[MIT License](LICENSE)
