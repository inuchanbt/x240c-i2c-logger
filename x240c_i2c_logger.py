#!/usr/bin/env python3
"""
x240c_i2c_logger.py
PowerPi X240C I2C sniffer logger / decoder

v0.4.4
- Direct PicoXTools WebSocket input:
      ws://<host>/ws/i2c
- PicoXTools binary stream decoder (experimentally derived)
- Read + repeated-START reconstruction
- Multi-phase Write/Write repeated-START decoding
- ACK/NACK decoding for PicoXTools DATA events
- Existing PicoXTools copy/paste text-log input
- Existing RP2040 PIO sniffer USB-CDC/COM input
- X240C 0x6C register decoding
- CSV logging with raw data preserved
- Voltage-change set aggregation with a second summary CSV
- Groups the burst following each OTG voltage write into one logical set

Direct PicoXTools usage:
    py -m pip install websocket-client
    py x240c_i2c_logger.py --picotools 192.168.33.1

Useful debug mode:
    py x240c_i2c_logger.py --picotools 192.168.33.1 --ws-debug

Observed PicoXTools WebSocket format:
    WebSocket URL:
        ws://192.168.33.1/ws/i2c

    Each binary WebSocket message:
        uint16_le total_message_length
        zero or more uint32_le event words

    Known event words:
        0x4000006C  -> START, address 0x6C, Write
        0x4200006C  -> START, address 0x6C, Read
        0x000000XX  -> DATA XX, ACK
        0x010000XX  -> DATA XX, NACK
        0xC0000000  -> STOP

    Example split across TWO WebSocket messages:
        06 00 6C 00 00 40

        0E 00
        06 00 00 00
        22 00 00 00
        00 00 00 C0

    Reconstructed I2C:
        S 6C W 06 22 P

Important:
    - WebSocket-message boundaries are NOT I2C-frame boundaries.
    - This decoder therefore maintains an I2C state machine across messages.
    - Read direction and DATA NACK flags are now decoded from live captures.
    - Any still-unknown event bits are preserved/flagged instead of silently guessed.

X240C / SHP(SC/SCV)8808-family observations:
    7-bit I2C address: 0x6C

    Reg 0x04 + 0x05 : OTG output voltage
        code = ((reg05 & 0x03) << 8) | reg04

        reg05 bit2 = 0:
            V = 0.54 + code * 0.020 V

        reg05 bit2 = 1:
            V = 21.05 + code * 0.050 V

    Reg 0x06 : OTG current-limit estimate
        Current decoding remains an estimate until the X240C current-sense
        resistor scaling is physically confirmed.

        With effective Rsense = 10 mOhm:
            0x1C -> ~2.575 A
            0x22 -> ~3.025 A
            0x3D -> ~5.050 A
"""

from __future__ import annotations

import argparse
import csv
import re
import struct
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional


VERSION = "0.4.4"
DEFAULT_ADDR = 0x6C

TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}\.\d{3,6}$")
HEXBYTE_RE = re.compile(r"^[0-9A-Fa-f]{2}$")
RP_TOKEN_RE = re.compile(r"^([0-9A-Fa-f]{2})([aAnN])?$")

# PicoXTools binary-event tags derived from captures.
PT_EVT_MASK = 0xC0000000
PT_EVT_DATA = 0x00000000
PT_EVT_START = 0x40000000
PT_EVT_UNKNOWN_80 = 0x80000000
PT_EVT_STOP = 0xC0000000
PT_START_READ = 0x02000000
PT_DATA_NACK = 0x01000000


@dataclass
class I2CPhase:
    address: int
    direction: str
    data: list[int] = field(default_factory=list)
    ack: list[str] = field(default_factory=list)
    raw_words: list[int] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class Frame:
    source_time: str
    address: int
    direction: str
    data: list[int]
    raw: str
    parse_flags: list[str]
    ack_info: str = ""
    source: str = ""
    phases: list[I2CPhase] = field(default_factory=list)
    i2c_text: str = ""


def _phase_text(phase: I2CPhase, repeated: bool = False) -> str:
    parts = ["Sr" if repeated else "S", f"{phase.address:02X}", phase.direction]
    for i, b in enumerate(phase.data):
        parts.append(f"{b:02X}")
        if i < len(phase.ack) and phase.ack[i] == "N":
            parts.append("N")
    return " ".join(parts)


def _transaction_text(phases: list[I2CPhase], stop_seen: bool = True) -> str:
    out = [_phase_text(p, repeated=(i > 0)) for i, p in enumerate(phases)]
    if stop_seen:
        out.append("P")
    return " ".join(out)


@dataclass
class PicoToolsWsState:
    """Reassemble complete I2C transactions across WebSocket messages."""

    active: bool = False
    phases: list[I2CPhase] = field(default_factory=list)
    current: Optional[I2CPhase] = None
    flags: list[str] = field(default_factory=list)

    def reset(self) -> None:
        self.active = False
        self.phases.clear()
        self.current = None
        self.flags.clear()

    def feed_word(self, word: int) -> list[Frame]:
        frames: list[Frame] = []
        tag = word & PT_EVT_MASK

        if tag == PT_EVT_START:
            address = word & 0x7F
            direction = "R" if (word & PT_START_READ) else "W"
            unknown = word & ~(PT_EVT_MASK | PT_START_READ | 0x7F)
            phase_flags: list[str] = []
            if unknown:
                phase_flags.append(f"START_EXTRA_BITS:0x{unknown:08X}")

            phase = I2CPhase(
                address=address,
                direction=direction,
                raw_words=[word],
                flags=phase_flags,
            )

            if not self.active:
                self.active = True
                self.phases = [phase]
                self.current = phase
                self.flags = []
            else:
                # START inside an active transaction = repeated START.
                self.phases.append(phase)
                self.current = phase
            return frames

        if tag == PT_EVT_DATA:
            if not self.active or self.current is None:
                return frames  # attached in the middle of a transaction

            byte = word & 0xFF
            ack = "N" if (word & PT_DATA_NACK) else "A"
            unknown = word & ~(PT_DATA_NACK | 0xFF)
            if unknown:
                self.current.flags.append(f"DATA_EXTRA_BITS:0x{word:08X}")

            self.current.data.append(byte)
            self.current.ack.append(ack)
            self.current.raw_words.append(word)
            return frames

        if tag == PT_EVT_STOP:
            if not self.active:
                return frames
            if self.current is not None:
                self.current.raw_words.append(word)
            frames.append(self._finish(stop_seen=True))
            return frames

        if self.active:
            self.flags.append(f"UNKNOWN_EVENT:0x{word:08X}")
            if self.current is not None:
                self.current.raw_words.append(word)
        return frames

    def _finish(self, stop_seen: bool) -> Frame:
        phases = [
            I2CPhase(
                address=p.address,
                direction=p.direction,
                data=list(p.data),
                ack=list(p.ack),
                raw_words=list(p.raw_words),
                flags=list(p.flags),
            )
            for p in self.phases
        ]

        flags = list(self.flags)
        for i, p in enumerate(phases):
            for flag in p.flags:
                flags.append(f"P{i}:{flag}")

            # v0.4.4: a NACK on data written by the master is exceptional
            # and should be visible as a parser flag.  A NACK on the final
            # byte of a READ is normal I2C termination and is not flagged.
            if p.direction == "W":
                for data_index, (byte, ack) in enumerate(zip(p.data, p.ack)):
                    if ack == "N":
                        flags.append(
                            f"P{i}:WRITE_DATA_NACK@{data_index}:0x{byte:02X}"
                        )

        if not stop_seen:
            flags.append("NO_STOP")

        first = phases[0] if phases else I2CPhase(-1, "?")
        direction = first.direction if len(phases) == 1 else "/".join(p.direction for p in phases)
        raw = " | ".join(" ".join(f"{w:08X}" for w in p.raw_words) for p in phases)
        ack_info = ";".join(
            f"P{i}:" + "".join(p.ack)
            for i, p in enumerate(phases)
            if p.ack
        )

        frame = Frame(
            source_time="",
            address=first.address,
            direction=direction,
            data=list(first.data),
            raw=raw,
            parse_flags=flags,
            ack_info=ack_info,
            source="picotools_ws",
            phases=phases,
            i2c_text=_transaction_text(phases, stop_seen=stop_seen),
        )
        self.reset()
        return frame


def parse_int_auto(s: str) -> int:
    s = s.strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    return int(s, 0)


def decode_voltage(reg04: int, reg05: int) -> tuple[float, int, str]:
    code = ((reg05 & 0x03) << 8) | reg04
    if reg05 & 0x04:
        return 21.05 + code * 0.050, code, "HIGH_50mV"
    return 0.54 + code * 0.020, code, "LOW_20mV"


def decode_current(code: int, rsense_mohm: float) -> float:
    return (2.375 + code * 0.375) * (2.0 / rsense_mohm)


def parse_picox_line(line: str) -> Optional[Frame]:
    raw = line.rstrip("\r\n")
    s = raw.strip()
    if not s:
        return None

    tokens = re.split(r"\s+", s)
    flags: list[str] = []
    source_time = ""

    if tokens and TIME_RE.match(tokens[0]):
        source_time = tokens.pop(0)
    elif tokens and TIME_RE.match(tokens[-1]):
        source_time = tokens.pop()

    try:
        s_idx = next(i for i, t in enumerate(tokens) if t.upper() == "S")
    except StopIteration:
        return None

    if len(tokens) <= s_idx + 2:
        return Frame(source_time, -1, "?", [], raw, ["SHORT_FRAME"], source="picox")

    try:
        address = int(tokens[s_idx + 1], 16)
    except ValueError:
        return Frame(source_time, -1, "?", [], raw, ["BAD_ADDRESS"], source="picox")

    direction = tokens[s_idx + 2].upper()
    if direction not in ("W", "R"):
        flags.append("BAD_DIRECTION")

    payload_tokens = tokens[s_idx + 3:]
    if any(t.upper() == "P" for t in payload_tokens):
        p_idx = next(i for i, t in enumerate(payload_tokens) if t.upper() == "P")
        payload_tokens = payload_tokens[:p_idx]
    else:
        flags.append("NO_STOP")

    data: list[int] = []
    for t in payload_tokens:
        if HEXBYTE_RE.match(t):
            data.append(int(t, 16))
        else:
            flags.append(f"BAD_TOKEN:{t}")

    if not data:
        flags.append("NO_DATA")

    return Frame(
        source_time=source_time,
        address=address,
        direction=direction,
        data=data,
        raw=raw,
        parse_flags=flags,
        source="picox",
    )


def parse_rp2040_line(line: str) -> list[Frame]:
    raw = line.rstrip("\r\n")
    s = raw.strip()
    if not s:
        return []

    if s.lower().startswith("i2c sniffer") or s.startswith("["):
        return []

    tokens = re.split(r"\s+", s)
    source_time = ""
    if tokens and re.fullmatch(r"\d{6,10}", tokens[0]):
        source_time = f"us:{tokens.pop(0)}"

    if not any(t.lower() == "s" for t in tokens):
        return []

    frames: list[Frame] = []
    current_tokens: list[str] = []
    in_segment = False

    def finish_segment(stop_seen: bool) -> None:
        nonlocal current_tokens
        if not current_tokens:
            return

        flags: list[str] = []
        parsed: list[tuple[int, Optional[str]]] = []
        for tok in current_tokens:
            m = RP_TOKEN_RE.match(tok)
            if not m:
                flags.append(f"BAD_TOKEN:{tok}")
                continue
            parsed.append((int(m.group(1), 16), (m.group(2) or "").lower() or None))

        if not parsed:
            current_tokens = []
            return

        addr_byte, addr_ack = parsed[0]
        address = (addr_byte >> 1) & 0x7F
        direction = "R" if (addr_byte & 1) else "W"
        data = [b for b, _ack in parsed[1:]]

        ack_parts: list[str] = []
        if addr_ack:
            ack_parts.append(f"ADDR:{addr_ack}")
        for i, (_b, ack) in enumerate(parsed[1:]):
            if ack:
                ack_parts.append(f"D{i}:{ack}")

        if not stop_seen:
            flags.append("REPEATED_START")

        frames.append(
            Frame(
                source_time=source_time,
                address=address,
                direction=direction,
                data=data,
                raw=raw,
                parse_flags=flags,
                ack_info=",".join(ack_parts),
                source="rp2040_cdc",
            )
        )
        current_tokens = []

    for tok in tokens:
        tl = tok.lower()
        if tl == "s":
            if in_segment and current_tokens:
                finish_segment(stop_seen=False)
            in_segment = True
            continue

        if tl == "p":
            if in_segment:
                finish_segment(stop_seen=True)
                in_segment = False
            continue

        if in_segment:
            current_tokens.append(tok)

    if in_segment and current_tokens:
        finish_segment(stop_seen=False)
        if frames:
            frames[-1].parse_flags.append("NO_STOP")

    return frames


def parse_any_text_line(line: str) -> list[Frame]:
    s = line.strip()
    tokens = re.split(r"\s+", s) if s else []

    probe = tokens[:]
    if probe and re.fullmatch(r"\d{6,10}", probe[0]):
        probe = probe[1:]

    if len(probe) >= 2 and probe[0].lower() == "s" and RP_TOKEN_RE.match(probe[1]):
        return parse_rp2040_line(line)

    f = parse_picox_line(line)
    return [f] if f is not None else []


def parse_picotools_ws_message(
    payload: bytes,
    state: PicoToolsWsState,
    debug: bool = False,
) -> list[Frame]:
    if len(payload) < 2:
        if debug:
            print(f"WS! short payload ({len(payload)} B): {payload.hex(' ')}", file=sys.stderr)
        return []

    declared = struct.unpack_from("<H", payload, 0)[0]
    body = payload[2:]

    if debug:
        preview = []
        if len(body) % 4 == 0:
            preview = [
                f"0x{x:08X}"
                for x in struct.unpack("<" + "I" * (len(body) // 4), body)
            ]
        print(
            f"WS< {len(payload):3d} B declared={declared:3d} "
            f"hex={payload.hex(' ').upper()} words={' '.join(preview)}",
            file=sys.stderr,
        )

    if declared != len(payload) and debug:
        print(
            f"WS! length mismatch: declared={declared}, actual={len(payload)}",
            file=sys.stderr,
        )

    usable = len(body) - (len(body) % 4)
    if usable != len(body) and debug:
        print(f"WS! trailing {len(body) - usable} byte(s)", file=sys.stderr)

    frames: list[Frame] = []
    for off in range(0, usable, 4):
        word = struct.unpack_from("<I", body, off)[0]
        frames.extend(state.feed_word(word))

    return frames


def iter_picotools_frames(host: str, timeout: float, debug: bool) -> Iterator[Frame]:
    try:
        import websocket
    except ImportError as exc:
        raise RuntimeError(
            "websocket-client is required for --picotools mode: "
            "py -m pip install websocket-client"
        ) from exc

    if host.startswith("ws://") or host.startswith("wss://"):
        url = host
        bare_host = re.sub(r"^wss?://", "", host).split("/", 1)[0]
    else:
        bare_host = host.rstrip("/")
        url = f"ws://{bare_host}/ws/i2c"

    origin = f"http://{bare_host}"
    print(f"# PicoXTools WebSocket: {url}", file=sys.stderr)
    print("# Ctrl+C to stop.", file=sys.stderr)

    state = PicoToolsWsState()

    ws = websocket.create_connection(
        url,
        timeout=timeout,
        origin=origin,
        http_proxy_host=None,
        http_proxy_port=None,
    )

    try:
        while True:
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue

            if msg is None:
                break

            if isinstance(msg, str):
                if debug:
                    print(f"WS<TEXT {msg!r}", file=sys.stderr)
                continue

            if isinstance(msg, (bytes, bytearray)):
                yield from parse_picotools_ws_message(bytes(msg), state, debug=debug)
            elif debug:
                print(f"WS! unsupported object: {type(msg).__name__}", file=sys.stderr)
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _add_flag(existing: str, flag: str) -> str:
    if not existing:
        return flag
    parts = existing.split(";")
    if flag not in parts:
        parts.append(flag)
    return ";".join(parts)


def _decode_write_phase(
    phase: I2CPhase,
    rsense_mohm: float,
    row: dict,
    phase_index: int,
) -> list[str]:
    """Decode one X240C write phase and merge useful fields into row."""
    data = phase.data
    if not data:
        return [f"P{phase_index}:EMPTY_WRITE"]

    start_reg = data[0]
    payload = data[1:]

    # For backwards-compatible scalar CSV fields, keep the first write register.
    if not row["start_reg"]:
        row["start_reg"] = f"0x{start_reg:02X}"
        row["data_hex"] = " ".join(f"{b:02X}" for b in data)

    if not payload:
        return [f"P{phase_index}:REGISTER_POINTER_ONLY"]

    regvals = {start_reg + i: v for i, v in enumerate(payload)}
    events: list[str] = []

    if 0x04 in regvals or 0x05 in regvals:
        if 0x04 in regvals and 0x05 in regvals:
            v, vcode, mode = decode_voltage(regvals[0x04], regvals[0x05])
            row["otg_voltage_v"] = f"{v:.3f}"
            row["voltage_code"] = f"0x{vcode:03X}"
            row["voltage_range"] = mode
            events.append(f"OTG_VOLTAGE={v:.3f}V")
        else:
            events.append("OTG_VOLTAGE_PARTIAL")
            row["flags"] = _add_flag(
                row["flags"], f"P{phase_index}:VOLTAGE_BYTES_INCOMPLETE"
            )

    if 0x06 in regvals:
        cur_code = regvals[0x06]
        amps = decode_current(cur_code, rsense_mohm)
        row["current_code"] = f"0x{cur_code:02X}"
        row["otg_current_est_a"] = f"{amps:.3f}"
        events.append(f"OTG_CURRENT~{amps:.3f}A")

    if 0x10 in regvals:
        v = regvals[0x10]
        dcdc_disabled = bool(v & 0x02)
        events.append(f"REG10=0x{v:02X}(DCDC_{'DIS' if dcdc_disabled else 'EN'})")

    if 0x11 in regvals:
        events.append(f"REG11=0x{regvals[0x11]:02X}")

    if 0x16 in regvals:
        events.append(f"REG16=0x{regvals[0x16]:02X}")

    if not events:
        events.append(
            f"REGISTER_WRITE 0x{start_reg:02X}="
            + " ".join(f"{b:02X}" for b in payload)
        )

    return events


def decode_frame(frame: Frame, target_addr: int, rsense_mohm: float) -> dict:
    now = datetime.now().astimezone()
    phase_summary = ";".join(
        f"{p.address:02X}{p.direction}:" + " ".join(f"{b:02X}" for b in p.data)
        for p in frame.phases
    ) if frame.phases else ""

    row = {
        "host_time_iso": now.isoformat(timespec="milliseconds"),
        "host_time_epoch": f"{time.time():.6f}",
        "source": frame.source,
        "source_time": frame.source_time,
        "address": f"0x{frame.address:02X}" if frame.address >= 0 else "",
        "direction": frame.direction,
        "phase_count": str(len(frame.phases) if frame.phases else 1),
        "i2c_text": frame.i2c_text,
        "phase_summary": phase_summary,
        "start_reg": "",
        "data_hex": " ".join(f"{b:02X}" for b in frame.data),
        "read_data_hex": "",
        "ack_info": frame.ack_info,
        "event": "",
        "otg_voltage_v": "",
        "voltage_code": "",
        "voltage_range": "",
        "otg_current_est_a": "",
        "current_code": "",
        "flags": ";".join(frame.parse_flags),
        "raw": frame.raw,
    }

    if frame.address != target_addr:
        row["event"] = "OTHER_ADDRESS"
        return row

    phases = frame.phases
    if not phases:
        phases = [
            I2CPhase(
                address=frame.address,
                direction=frame.direction,
                data=list(frame.data),
            )
        ]

    # Register read:
    # S addr W <reg> Sr addr R <data...> [N] P
    if (
        len(phases) >= 2
        and phases[0].address == target_addr
        and phases[0].direction == "W"
        and len(phases[0].data) == 1
        and phases[1].address == target_addr
        and phases[1].direction == "R"
    ):
        reg = phases[0].data[0]
        rdata = phases[1].data
        row["start_reg"] = f"0x{reg:02X}"
        row["data_hex"] = f"{reg:02X}"
        row["read_data_hex"] = " ".join(f"{b:02X}" for b in rdata)

        if len(rdata) == 1:
            row["event"] = f"REGISTER_READ 0x{reg:02X} => 0x{rdata[0]:02X}"
        elif rdata:
            row["event"] = (
                f"REGISTER_READ 0x{reg:02X} => "
                + " ".join(f"{b:02X}" for b in rdata)
            )
        else:
            row["event"] = f"REGISTER_READ 0x{reg:02X} => <no data>"

        # If there are additional phases after the read, don't silently lose them.
        if len(phases) > 2:
            row["flags"] = _add_flag(row["flags"], "EXTRA_PHASES_AFTER_REGISTER_READ")
        return row

    # v0.4.4: decode ALL repeated-START write phases in order.
    # Example:
    #   S 6C W 06 22 Sr 6C W 04 D3 02 P
    # -> OTG_CURRENT~3.025A | OTG_VOLTAGE=15.000V
    if all(p.address == target_addr and p.direction == "W" for p in phases):
        events: list[str] = []
        pointer_only = False

        for i, phase in enumerate(phases):
            phase_events = _decode_write_phase(phase, rsense_mohm, row, i)
            if phase_events == [f"P{i}:REGISTER_POINTER_ONLY"]:
                pointer_only = True
            events.extend(phase_events)

        if pointer_only:
            row["flags"] = _add_flag(row["flags"], "NO_PAYLOAD")

        row["event"] = " | ".join(events) if events else "EMPTY_WRITE"
        return row

    # Single-phase read.
    if len(phases) == 1 and phases[0].address == target_addr and phases[0].direction == "R":
        row["read_data_hex"] = " ".join(f"{b:02X}" for b in phases[0].data)
        row["event"] = "READ" if not phases[0].data else "READ " + row["read_data_hex"]
        return row

    # Preserve rather than mislabel any still-unhandled mixed transaction.
    row["event"] = "COMPLEX_I2C_TRANSACTION"
    row["flags"] = _add_flag(row["flags"], "UNHANDLED_PHASE_PATTERN")
    return row




def _clone_phase(phase: I2CPhase) -> I2CPhase:
    return I2CPhase(
        address=phase.address,
        direction=phase.direction,
        data=list(phase.data),
        ack=list(phase.ack),
        raw_words=list(phase.raw_words),
        flags=list(phase.flags),
    )


def _single_phase_frame_for_aggregation(
    parent: Frame,
    phase: I2CPhase,
    phase_index: int,
) -> Frame:
    """
    Create an internal one-phase view of a multi-write transaction.

    This does NOT change the normal transaction CSV.  It is used only by the
    VoltageChangeSetAggregator so a physical transaction such as:

        S 6C W 06 1C Sr 6C W 04 C0 01 P

    can contribute the current phase to the old voltage set and the voltage
    phase to the new set.
    """
    p = _clone_phase(phase)

    phase_flags: list[str] = []
    for flag in p.flags:
        phase_flags.append(f"P{phase_index}:{flag}")
    if p.direction == "W":
        for data_index, (byte, ack) in enumerate(zip(p.data, p.ack)):
            if ack == "N":
                phase_flags.append(
                    f"P{phase_index}:WRITE_DATA_NACK@{data_index}:0x{byte:02X}"
                )

    return Frame(
        source_time=parent.source_time,
        address=p.address,
        direction=p.direction,
        data=list(p.data),
        raw=" ".join(f"{w:08X}" for w in p.raw_words),
        parse_flags=phase_flags,
        ack_info=(
            f"P{phase_index}:" + "".join(p.ack)
            if p.ack else ""
        ),
        source=parent.source,
        phases=[p],
        i2c_text=_transaction_text([p], stop_seen=True),
    )


def rows_for_set_aggregation(
    frame: Frame,
    transaction_row: dict,
    target_addr: int,
    rsense_mohm: float,
    parent_tx_id: str,
) -> list[dict]:
    """
    Return logical rows for voltage-set grouping.

    Normal single-phase transactions and W/R register reads remain one row.
    A repeated-START all-W transaction is split into ordered one-phase rows.

    Each internal row carries private keys used only by the set aggregator:
      _agg_parent_tx_id
      _agg_phase_index
      _agg_phase_count
    """
    phases = frame.phases or []

    split_all_write = (
        len(phases) > 1
        and all(p.address == target_addr and p.direction == "W" for p in phases)
    )

    if not split_all_write:
        row = dict(transaction_row)
        row["_agg_parent_tx_id"] = str(parent_tx_id)
        row["_agg_phase_index"] = "0"
        row["_agg_phase_count"] = str(max(1, len(phases)))
        return [row]

    out: list[dict] = []
    for i, phase in enumerate(phases):
        phase_frame = _single_phase_frame_for_aggregation(frame, phase, i)
        row = decode_frame(phase_frame, target_addr, rsense_mohm)

        # Keep the physical transaction's host timestamp for every phase.
        row["host_time_iso"] = transaction_row["host_time_iso"]
        row["host_time_epoch"] = transaction_row["host_time_epoch"]
        row["source_time"] = transaction_row.get("source_time", "")
        row["_agg_parent_tx_id"] = str(parent_tx_id)
        row["_agg_phase_index"] = str(i)
        row["_agg_phase_count"] = str(len(phases))
        out.append(row)

    return out


@dataclass
class VoltageChangeSet:
    set_id: int
    from_voltage_v: str
    to_voltage_v: str
    voltage_code: str
    voltage_range: str
    start_time_iso: str
    start_epoch: float
    last_time_iso: str
    last_epoch: float
    rows: list[dict] = field(default_factory=list)
    tail_start_index: Optional[int] = None
    tail_trigger_epoch: Optional[float] = None


class VoltageChangeSetAggregator:
    """
    Hybrid time + semantic voltage-change grouping.

    Base behavior:
      - A set starts on an OTG voltage write.
      - Transactions within idle_ms are part of the immediate/base burst.
      - After that idle gap, the set remains pending instead of being emitted
        immediately.

    Semantic tail behavior:
      - While pending, a later DCDC_DIS within tail_window_ms can reopen the
        same voltage set as a semantic tail.
      - A recent current-limit write immediately preceding DCDC_DIS can be
        pulled into the semantic tail if it falls within tail_lead_ms.
      - The semantic tail is considered complete when the observed sequence
        contains DCDC_DIS, DCDC_EN, Reg11 read/write and Reg16 read/write.

    This matches the observed X240C 5 V return sequence where the semantic
    tail started about 2.27 s after the initial 5.08 V programming write.

    Host timing is receive/decode timing, not a hardware bus timestamp.
    """

    def __init__(
        self,
        idle_ms: float = 300.0,
        tail_window_ms: float = 5000.0,
        tail_lead_ms: float = 250.0,
    ):
        self.idle_ms = float(idle_ms)
        self.tail_window_ms = float(tail_window_ms)
        self.tail_lead_ms = float(tail_lead_ms)

        self.active: Optional[VoltageChangeSet] = None
        self.mode: str = "idle"  # idle / base / pending / tail
        self.previous_voltage_v: str = ""
        self.next_set_id = 1

        # Target-device rows seen while a set is pending but not yet known
        # to belong to a semantic tail. Only a short recent window is kept.
        self.pending_recent: list[dict] = []

    @staticmethod
    def _is_dcdc_dis(row: dict) -> bool:
        return "DCDC_DIS" in row.get("event", "")

    @staticmethod
    def _is_tail_candidate(row: dict) -> bool:
        ev = row.get("event", "")
        return (
            "OTG_CURRENT" in ev
            or "DCDC_" in ev
            or "REGISTER_READ 0x11" in ev
            or "REG11=" in ev
            or "REGISTER_READ 0x16" in ev
            or "REG16=" in ev
        )

    @staticmethod
    def _semantic_tail_complete(rows: list[dict]) -> bool:
        events = " || ".join(r.get("event", "") for r in rows)
        return (
            "DCDC_DIS" in events
            and "DCDC_EN" in events
            and "REGISTER_READ 0x11" in events
            and "REG11=" in events
            and "REGISTER_READ 0x16" in events
            and "REG16=" in events
        )

    def _append(self, row: dict) -> None:
        assert self.active is not None
        self.active.rows.append(dict(row))
        self.active.last_time_iso = row["host_time_iso"]
        self.active.last_epoch = float(row["host_time_epoch"])

    def _start(self, row: dict) -> None:
        t = float(row["host_time_epoch"])
        self.active = VoltageChangeSet(
            set_id=self.next_set_id,
            from_voltage_v=self.previous_voltage_v,
            to_voltage_v=row["otg_voltage_v"],
            voltage_code=row.get("voltage_code", ""),
            voltage_range=row.get("voltage_range", ""),
            start_time_iso=row["host_time_iso"],
            start_epoch=t,
            last_time_iso=row["host_time_iso"],
            last_epoch=t,
            rows=[],
        )
        self.next_set_id += 1
        self.mode = "base"
        self.pending_recent = []
        self._append(row)

    def _trim_pending_recent(self, now_epoch: float) -> None:
        cutoff = now_epoch - (self.tail_lead_ms / 1000.0)
        self.pending_recent = [
            r for r in self.pending_recent
            if float(r["host_time_epoch"]) >= cutoff
        ]

    def _commit_pending_from_same_transaction(self, row: dict) -> int:
        """
        Commit pending phase rows that belong to the same physical I2C
        transaction as `row`.

        This fixes the boundary case:

            old SET is already pending
            phase 0: CURRENT for the old voltage
            phase 1: VOLTAGE starting the next SET

        Because both phases share _agg_parent_tx_id, phase 0 must be attached
        to the old SET before phase 1 closes it.
        """
        if self.active is None or self.mode != "pending":
            return 0

        parent_tx_id = row.get("_agg_parent_tx_id")
        if not parent_tx_id:
            return 0

        same_tx = [
            r for r in self.pending_recent
            if r.get("_agg_parent_tx_id") == parent_tx_id
        ]
        if not same_tx:
            return 0

        # Preserve their original phase order.
        same_tx.sort(key=lambda r: int(r.get("_agg_phase_index", "0") or 0))

        for pending_row in same_tx:
            self._append(pending_row)

        committed_ids = {id(r) for r in same_tx}
        self.pending_recent = [
            r for r in self.pending_recent
            if id(r) not in committed_ids
        ]
        return len(same_tx)

    def feed(self, row: dict) -> list[dict]:
        out: list[dict] = []
        t = float(row["host_time_epoch"])
        has_voltage = bool(row.get("otg_voltage_v"))

        # A new voltage closes the prior logical set first.
        #
        # v0.4.4: if this voltage is a later phase of the SAME physical
        # repeated-START transaction, first attach any pending earlier phases
        # (typically CURRENT) to the old SET.  Otherwise that CURRENT phase
        # would be lost when _finish() clears pending_recent.
        if has_voltage:
            if self.active is not None:
                self._commit_pending_from_same_transaction(row)
                out.append(self._finish("next_voltage"))
            self._start(row)
            return out

        if self.active is None:
            return out

        # Immediate burst after voltage programming.
        if self.mode == "base":
            gap_ms = (t - self.active.last_epoch) * 1000.0
            if gap_ms <= self.idle_ms:
                self._append(row)
                return out

            # Do not emit yet. Park the set so a delayed semantic tail can
            # still attach to it.
            self.mode = "pending"
            self.pending_recent = []

        # Pending: wait for a semantic tail trigger, or eventually expire.
        if self.mode == "pending":
            since_last_included_ms = (t - self.active.last_epoch) * 1000.0

            # If the semantic window has passed, finalize the set before this
            # unrelated transaction.
            if since_last_included_ms > self.tail_window_ms:
                out.append(self._finish("semantic_window_expired"))
                return out

            self._trim_pending_recent(t)

            if self._is_dcdc_dis(row):
                # Pull in a very recent current write that immediately led
                # into DCDC_DIS. This captures the observed 5 V sequence.
                lead_rows = [
                    r for r in self.pending_recent
                    if self._is_tail_candidate(r)
                    and (t - float(r["host_time_epoch"])) * 1000.0 <= self.tail_lead_ms
                ]

                self.active.tail_start_index = len(self.active.rows)
                if lead_rows:
                    self.active.tail_start_index = len(self.active.rows)
                    for lead in lead_rows:
                        self._append(lead)

                self.active.tail_trigger_epoch = t
                self._append(row)
                self.mode = "tail"
                self.pending_recent = []
                return out

            # Keep only plausible tail-prelude rows. They are not yet counted
            # as part of the set unless a DCDC_DIS follows shortly.
            if self._is_tail_candidate(row):
                self.pending_recent.append(dict(row))
                self._trim_pending_recent(t)

            return out

        # Semantic tail: preserve the whole tail until completion.
        if self.mode == "tail":
            gap_ms = (t - self.active.last_epoch) * 1000.0
            if gap_ms > self.idle_ms:
                out.append(self._finish("semantic_tail_timeout"))
                return out

            self._append(row)

            if self._semantic_tail_complete(self.active.rows):
                out.append(self._finish("semantic_tail_complete"))

            return out

        return out

    def flush(self, reason: str = "shutdown") -> list[dict]:
        if self.active is None:
            return []
        return [self._finish(reason)]

    def _finish(self, reason: str) -> dict:
        s = self.active
        assert s is not None

        rows = s.rows
        events = [r.get("event", "") for r in rows if r.get("event")]
        i2c = [r.get("i2c_text", "") for r in rows if r.get("i2c_text")]

        current_values = [r["otg_current_est_a"] for r in rows if r.get("otg_current_est_a")]
        current_codes = [r["current_code"] for r in rows if r.get("current_code")]

        dcdc_seq: list[str] = []
        reg11_read: list[str] = []
        reg11_write: list[str] = []
        reg16_read: list[str] = []
        reg16_write: list[str] = []
        flags: list[str] = []

        for r in rows:
            ev = r.get("event", "")

            for m in re.finditer(r"DCDC_(DIS|EN)", ev):
                dcdc_seq.append(m.group(1))

            m = re.search(r"REGISTER_READ 0x11 => 0x([0-9A-Fa-f]{2})", ev)
            if m:
                reg11_read.append(m.group(1).upper())

            m = re.search(r"REG11=0x([0-9A-Fa-f]{2})", ev)
            if m:
                reg11_write.append(m.group(1).upper())

            m = re.search(r"REGISTER_READ 0x16 => 0x([0-9A-Fa-f]{2})", ev)
            if m:
                reg16_read.append(m.group(1).upper())

            m = re.search(r"REG16=0x([0-9A-Fa-f]{2})", ev)
            if m:
                reg16_write.append(m.group(1).upper())

            for f in filter(None, r.get("flags", "").split(";")):
                if f not in flags:
                    flags.append(f)

        duration_ms = max(0.0, (s.last_epoch - s.start_epoch) * 1000.0)

        def unique_tx_count(items: list[dict]) -> int:
            keys: list[str] = []
            for idx, r in enumerate(items):
                key = r.get("_agg_parent_tx_id") or f"legacy:{idx}:{r.get('host_time_epoch','')}"
                if key not in keys:
                    keys.append(key)
            return len(keys)

        if s.tail_start_index is None:
            base_operation_count = len(rows)
            tail_operation_count = 0
            semantic_tail = "no"
            tail_trigger_delay_ms = ""
        else:
            base_operation_count = s.tail_start_index
            tail_operation_count = len(rows) - s.tail_start_index
            semantic_tail = "yes"
            if s.tail_trigger_epoch is not None:
                tail_trigger_delay_ms = f"{(s.tail_trigger_epoch - s.start_epoch) * 1000.0:.3f}"
            else:
                tail_trigger_delay_ms = ""

        summary = {
            "set_id": str(s.set_id),
            "start_time_iso": s.start_time_iso,
            "end_time_iso": s.last_time_iso,
            "start_epoch": f"{s.start_epoch:.6f}",
            "end_epoch": f"{s.last_epoch:.6f}",
            "duration_ms": f"{duration_ms:.3f}",
            "finish_reason": reason,
            "from_voltage_v": s.from_voltage_v,
            "to_voltage_v": s.to_voltage_v,
            "voltage_code": s.voltage_code,
            "voltage_range": s.voltage_range,
            "current_est_a": current_values[-1] if current_values else "",
            "current_code": current_codes[-1] if current_codes else "",
            "current_sequence_a": ">".join(current_values),
            "current_code_sequence": ">".join(current_codes),
            "dcdc_sequence": ">".join(dcdc_seq),
            "reg11_read_sequence": ">".join(reg11_read),
            "reg11_write_sequence": ">".join(reg11_write),
            "reg16_read_sequence": ">".join(reg16_read),
            "reg16_write_sequence": ">".join(reg16_write),
            "semantic_tail": semantic_tail,
            "tail_trigger_delay_ms": tail_trigger_delay_ms,
            "base_transaction_count": str(unique_tx_count(rows[:base_operation_count])),
            "tail_transaction_count": str(unique_tx_count(rows[base_operation_count:])),
            "transaction_count": str(unique_tx_count(rows)),
            "base_operation_count": str(base_operation_count),
            "tail_operation_count": str(tail_operation_count),
            "operation_count": str(len(rows)),
            "i2c_sequence": " || ".join(i2c),
            "event_sequence": " || ".join(events),
            "flags": ";".join(flags),
        }

        self.previous_voltage_v = s.to_voltage_v
        self.active = None
        self.mode = "idle"
        self.pending_recent = []
        return summary


def format_set_console(summary: dict, use_color: bool = True) -> str:
    sid = int(summary["set_id"])
    src_v = summary["from_voltage_v"] or "?"
    dst_v = summary["to_voltage_v"] or "?"
    cur = summary["current_est_a"]
    tx = summary["transaction_count"]
    ops = summary.get("operation_count", tx)
    dt = float(summary["duration_ms"])

    extras: list[str] = []
    if summary["dcdc_sequence"]:
        extras.append("DCDC:" + summary["dcdc_sequence"])
    if summary["reg11_read_sequence"]:
        extras.append("R11:" + summary["reg11_read_sequence"])
    if summary["reg11_write_sequence"]:
        extras.append("W11:" + summary["reg11_write_sequence"])
    if summary["reg16_read_sequence"]:
        extras.append("R16:" + summary["reg16_read_sequence"])
    if summary["reg16_write_sequence"]:
        extras.append("W16:" + summary["reg16_write_sequence"])
    if summary.get("semantic_tail") == "yes":
        extras.append("TAIL:" + summary.get("tail_trigger_delay_ms", "") + "ms")

    cur_text = f" I~{cur}A" if cur else ""
    extra_text = ("  [" + ", ".join(extras) + "]") if extras else ""

    count_text = f"tx={tx}" if ops == tx else f"tx={tx}/ops={ops}"
    core = (
        f"SET#{sid:03d}  {src_v:>7} -> {dst_v:>7} V"
        f"{cur_text}  {count_text}  span={dt:.1f}ms{extra_text}"
    )

    if use_color and sys.stdout.isatty():
        return f"\033[95m{core}\033[0m"
    return core


def format_console(row: dict, use_color: bool = True) -> str:
    t = row["source_time"] or row["host_time_iso"].split("T")[-1]
    src = row["source"] or "?"
    event = row["event"]
    flags = row["flags"]

    if use_color and sys.stdout.isatty():
        if "OTG_VOLTAGE" in event:
            event = f"[96m{event}[0m"
        elif "OTG_CURRENT" in event:
            event = f"[92m{event}[0m"
        elif "REGISTER_READ" in event:
            event = f"[94m{event}[0m"
        elif flags:
            event = f"[93m{event}[0m"

    suffix = f" flags={flags}" if flags else ""
    if row.get("i2c_text"):
        return f"{t:>15} {src:<12} {row['i2c_text']:<34} {event}{suffix}"

    addr = row["address"] or "??"
    reg = row["start_reg"] or "--"
    data = row["data_hex"]
    return f"{t:>15} {src:<12} {addr} {row['direction']} {reg} {data:<16} {event}{suffix}"


def iter_file_lines(path: Optional[Path]) -> Iterator[str]:
    if path is None:
        print(
            "# Reading text from stdin. Ctrl+Z then Enter (Windows) / Ctrl+D (Unix) to finish.",
            file=sys.stderr,
        )
        yield from sys.stdin
        return

    with path.open("r", encoding="utf-8-sig", errors="replace") as f:
        yield from f


def list_serial_ports() -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is required: py -m pip install pyserial", file=sys.stderr)
        return 2

    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return 0

    for p in ports:
        extra = []
        if p.vid is not None:
            extra.append(f"VID:PID={p.vid:04X}:{p.pid:04X}")
        if p.serial_number:
            extra.append(f"SN={p.serial_number}")
        suffix = "  " + " ".join(extra) if extra else ""
        print(f"{p.device:<10} {p.description}{suffix}")
    return 0


def iter_serial_lines(port: str, baud: int, timeout: float) -> Iterator[str]:
    try:
        import serial
    except ImportError as exc:
        raise RuntimeError("pyserial is required for --port mode: py -m pip install pyserial") from exc

    print(f"# Opening {port} @ {baud} baud", file=sys.stderr)
    print("# Ctrl+C to stop.", file=sys.stderr)

    with serial.Serial(port=port, baudrate=baud, timeout=timeout) as ser:
        try:
            ser.dtr = False
            ser.rts = False
        except Exception:
            pass

        while True:
            raw = ser.readline()
            if raw:
                yield raw.decode("utf-8", errors="replace")


def make_default_out() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("logs") / f"x240c_i2c_{stamp}.csv"


def make_default_sets_out(out_path: Path) -> Path:
    return out_path.with_name(out_path.stem + "_sets.csv")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="PowerPi X240C I2C logger (PicoXTools WebSocket / text / RP2040 CDC)."
    )

    source = p.add_mutually_exclusive_group()
    source.add_argument("--input", "-i", type=Path, help="Existing sniffer text log.")
    source.add_argument("--port", "-p", help="RP2040 USB-CDC port, e.g. COM14.")
    source.add_argument(
        "--picotools",
        nargs="?",
        const="192.168.33.1",
        metavar="HOST",
        help="Connect directly to PicoXTools WebSocket (default: 192.168.33.1).",
    )

    p.add_argument("--baud", type=int, default=115200, help="Serial baud (default 115200).")
    p.add_argument("--timeout", type=float, default=1.0, help="Socket/serial timeout seconds.")
    p.add_argument("--list-ports", action="store_true", help="List serial ports and exit.")
    p.add_argument("--ws-debug", action="store_true", help="Dump PicoXTools WebSocket binary messages.")
    p.add_argument("--out", "-o", type=Path, help="Output CSV path.")
    p.add_argument(
        "--sets-out",
        type=Path,
        help="Voltage-change set summary CSV (default: <out>_sets.csv).",
    )
    p.add_argument(
        "--set-idle-ms",
        type=float,
        default=300.0,
        help="Immediate/base set idle threshold (default 300 ms).",
    )
    p.add_argument(
        "--set-tail-window-ms",
        type=float,
        default=5000.0,
        help="Keep a completed base set pending for a delayed semantic tail (default 5000 ms).",
    )
    p.add_argument(
        "--set-tail-lead-ms",
        type=float,
        default=250.0,
        help="Attach a recent current write immediately preceding DCDC_DIS (default 250 ms).",
    )
    p.add_argument(
        "--no-sets",
        action="store_true",
        help="Disable voltage-change set aggregation and summary CSV.",
    )
    p.add_argument("--addr", default="0x6C", help="Target 7-bit I2C address (default 0x6C).")
    p.add_argument(
        "--rsense-mohm",
        type=float,
        default=10.0,
        help="Effective Rsense in mOhm for Reg06 current estimate (default 10).",
    )
    p.add_argument("--all-addresses", action="store_true", help="Print non-0x6C traffic too.")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress decoded console output.")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colors.")
    p.add_argument("--append", action="store_true", help="Append to existing CSV.")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p


def main() -> int:
    args = build_argparser().parse_args()

    if args.list_ports:
        return list_serial_ports()

    try:
        target_addr = parse_int_auto(args.addr)
    except ValueError:
        print(f"ERROR: invalid --addr: {args.addr}", file=sys.stderr)
        return 2

    if not (0 <= target_addr <= 0x7F):
        print("ERROR: --addr must be 0x00..0x7F", file=sys.stderr)
        return 2

    if args.rsense_mohm <= 0:
        print("ERROR: --rsense-mohm must be > 0", file=sys.stderr)
        return 2

    if args.set_idle_ms <= 0:
        print("ERROR: --set-idle-ms must be > 0", file=sys.stderr)
        return 2
    if args.set_tail_window_ms <= 0:
        print("ERROR: --set-tail-window-ms must be > 0", file=sys.stderr)
        return 2
    if args.set_tail_lead_ms < 0:
        print("ERROR: --set-tail-lead-ms must be >= 0", file=sys.stderr)
        return 2

    out_path = args.out or make_default_out()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sets_enabled = not args.no_sets
    sets_path = args.sets_out or make_default_sets_out(out_path)
    if sets_enabled:
        sets_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "host_time_iso",
        "host_time_epoch",
        "source",
        "source_time",
        "address",
        "direction",
        "phase_count",
        "i2c_text",
        "phase_summary",
        "start_reg",
        "data_hex",
        "read_data_hex",
        "ack_info",
        "event",
        "otg_voltage_v",
        "voltage_code",
        "voltage_range",
        "otg_current_est_a",
        "current_code",
        "flags",
        "raw",
    ]

    set_fieldnames = [
        "set_id",
        "start_time_iso",
        "end_time_iso",
        "start_epoch",
        "end_epoch",
        "duration_ms",
        "finish_reason",
        "from_voltage_v",
        "to_voltage_v",
        "voltage_code",
        "voltage_range",
        "current_est_a",
        "current_code",
        "current_sequence_a",
        "current_code_sequence",
        "dcdc_sequence",
        "reg11_read_sequence",
        "reg11_write_sequence",
        "reg16_read_sequence",
        "reg16_write_sequence",
        "semantic_tail",
        "tail_trigger_delay_ms",
        "base_transaction_count",
        "tail_transaction_count",
        "transaction_count",
        "base_operation_count",
        "tail_operation_count",
        "operation_count",
        "i2c_sequence",
        "event_sequence",
        "flags",
    ]

    mode = "a" if args.append else "w"
    write_header = not (args.append and out_path.exists() and out_path.stat().st_size > 0)
    sets_write_header = not (
        args.append
        and sets_enabled
        and sets_path.exists()
        and sets_path.stat().st_size > 0
    )

    total_frames = 0
    target_frames = 0
    flagged = 0
    voltages: list[float] = []
    completed_sets = 0

    set_agg = (
        VoltageChangeSetAggregator(
            args.set_idle_ms,
            args.set_tail_window_ms,
            args.set_tail_lead_ms,
        )
        if sets_enabled
        else None
    )
    sets_file = None
    sets_writer = None

    try:
        with out_path.open(mode, newline="", encoding="utf-8-sig") as outf:
            writer = csv.DictWriter(outf, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()

            if sets_enabled:
                sets_file = sets_path.open(mode, newline="", encoding="utf-8-sig")
                sets_writer = csv.DictWriter(sets_file, fieldnames=set_fieldnames)
                if sets_write_header:
                    sets_writer.writeheader()

            def emit_set(summary: dict) -> None:
                nonlocal completed_sets
                completed_sets += 1
                if sets_writer is not None:
                    sets_writer.writerow(summary)
                    sets_file.flush()
                if not args.quiet:
                    print(format_set_console(summary, use_color=not args.no_color))

            def consume_frame(frame: Frame) -> None:
                nonlocal total_frames, target_frames, flagged
                total_frames += 1
                if frame.address == target_addr:
                    target_frames += 1
                if frame.parse_flags:
                    flagged += 1

                row = decode_frame(frame, target_addr, args.rsense_mohm)
                writer.writerow(row)
                outf.flush()

                if row["otg_voltage_v"]:
                    try:
                        voltages.append(float(row["otg_voltage_v"]))
                    except ValueError:
                        pass

                if not args.quiet and (frame.address == target_addr or args.all_addresses):
                    print(format_console(row, use_color=not args.no_color))

                # Voltage-change sets are defined only from target-device traffic.
                # v0.4.4: repeated-START all-W transactions are split into
                # ordered phase-level logical rows for SET grouping only.
                if set_agg is not None and frame.address == target_addr:
                    agg_rows = rows_for_set_aggregation(
                        frame,
                        row,
                        target_addr,
                        args.rsense_mohm,
                        parent_tx_id=str(total_frames),
                    )
                    for agg_row in agg_rows:
                        for summary in set_agg.feed(agg_row):
                            emit_set(summary)

            if args.picotools:
                for frame in iter_picotools_frames(args.picotools, args.timeout, args.ws_debug):
                    consume_frame(frame)

            elif args.port:
                for line in iter_serial_lines(args.port, args.baud, args.timeout):
                    for frame in parse_any_text_line(line):
                        consume_frame(frame)

            else:
                for line in iter_file_lines(args.input):
                    for frame in parse_any_text_line(line):
                        consume_frame(frame)

    except KeyboardInterrupt:
        print("\n# Stopped by user.", file=sys.stderr)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        if set_agg is not None and sets_writer is not None:
            for summary in set_agg.flush("shutdown"):
                completed_sets += 1
                sets_writer.writerow(summary)
                if not args.quiet:
                    print(format_set_console(summary, use_color=not args.no_color))
            sets_file.flush()
            sets_file.close()

    print(f"\n# CSV: {out_path.resolve()}", file=sys.stderr)
    if sets_enabled:
        print(f"# SET CSV: {sets_path.resolve()}", file=sys.stderr)
    print(
        f"# decoded transactions={total_frames}, "
        f"target_0x{target_addr:02X}={target_frames}, flagged={flagged}",
        file=sys.stderr,
    )
    if voltages:
        print(
            f"# OTG voltage decoded={len(voltages)}, "
            f"min={min(voltages):.3f} V, max={max(voltages):.3f} V",
            file=sys.stderr,
        )
    if sets_enabled:
        print(
            f"# voltage-change sets={completed_sets}, "
            f"idle={args.set_idle_ms:.0f} ms, "
            f"tail_window={args.set_tail_window_ms:.0f} ms, "
            f"tail_lead={args.set_tail_lead_ms:.0f} ms",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
