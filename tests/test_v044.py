from pathlib import Path
import csv
import importlib.util
import sys

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent / "x240c_i2c_logger.py"

spec = importlib.util.spec_from_file_location("x240c_v044", MODULE)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def mkrow(epoch, event, voltage="", vcode="", vrange="", current="", ccode="",
          parent="", phase="0", phase_count="1", i2c=""):
    return {
        "host_time_iso": "2026-09-25T14:36:00+09:00",
        "host_time_epoch": f"{epoch:.6f}",
        "source": "test",
        "source_time": "",
        "address": "0x6C",
        "direction": "W",
        "phase_count": phase_count,
        "i2c_text": i2c,
        "phase_summary": "",
        "start_reg": "",
        "data_hex": "",
        "read_data_hex": "",
        "ack_info": "",
        "event": event,
        "otg_voltage_v": voltage,
        "voltage_code": vcode,
        "voltage_range": vrange,
        "otg_current_est_a": current,
        "current_code": ccode,
        "flags": "",
        "raw": "",
        "_agg_parent_tx_id": parent,
        "_agg_phase_index": phase,
        "_agg_phase_count": phase_count,
    }


# 1) Reproduce the exact v0.4.3 failure:
#    48 V SET becomes pending, then the same physical tx contains
#    CURRENT(old SET) -> VOLTAGE(new SET).
agg = mod.VoltageChangeSetAggregator(idle_ms=300.0, tail_window_ms=5000.0)

v48 = mkrow(
    100.000,
    "OTG_VOLTAGE=48.000V",
    voltage="48.000", vcode="0x21B", vrange="HIGH_50mV",
    parent="T1",
)
assert agg.feed(v48) == []

current_phase = mkrow(
    101.322,
    "OTG_CURRENT~5.050A",
    current="5.050", ccode="0x3D",
    parent="T2", phase="0", phase_count="2",
    i2c="S 6C W 06 3D P",
)
# This moves the active SET to pending and parks current_phase.
assert agg.feed(current_phase) == []
assert agg.mode == "pending"
assert len(agg.pending_recent) == 1

voltage_phase = mkrow(
    101.322,
    "OTG_VOLTAGE=15.000V",
    voltage="15.000", vcode="0x2D3", vrange="LOW_20mV",
    parent="T2", phase="1", phase_count="2",
    i2c="S 6C W 04 D3 02 P",
)
out = agg.feed(voltage_phase)
assert len(out) == 1
s48 = out[0]
assert s48["to_voltage_v"] == "48.000"
assert s48["current_est_a"] == "5.050"
assert s48["current_code"] == "0x3D"
assert s48["transaction_count"] == "2"
assert s48["operation_count"] == "2"
assert s48["finish_reason"] == "next_voltage"

# The new 15 V SET must be active after the old one closes.
assert agg.active is not None
assert agg.active.to_voltage_v == "15.000"


# 2) An unrelated pending current from a DIFFERENT physical transaction
#    must NOT be force-attached merely because a new voltage arrives.
agg = mod.VoltageChangeSetAggregator(idle_ms=300.0, tail_window_ms=5000.0)
assert agg.feed(mkrow(
    200.000, "OTG_VOLTAGE=9.000V",
    voltage="9.000", vcode="0x1A7", vrange="LOW_20mV", parent="A1"
)) == []

assert agg.feed(mkrow(
    201.000, "OTG_CURRENT~2.575A",
    current="2.575", ccode="0x1C", parent="A2"
)) == []
assert agg.mode == "pending"
assert len(agg.pending_recent) == 1

out = agg.feed(mkrow(
    201.100, "OTG_VOLTAGE=9.100V",
    voltage="9.100", vcode="0x1AC", vrange="LOW_20mV", parent="A3"
))
assert len(out) == 1
s9 = out[0]
assert s9["to_voltage_v"] == "9.000"
assert s9["current_est_a"] == ""
assert s9["transaction_count"] == "1"


# 3) Existing semantic-tail behavior remains intact.
agg = mod.VoltageChangeSetAggregator()
rows = [
    mkrow(300.000, "OTG_VOLTAGE=5.080V", voltage="5.080",
          vcode="0x0E3", vrange="LOW_20mV", parent="S1"),
    mkrow(300.001, "OTG_CURRENT~3.025A", current="3.025", ccode="0x22", parent="S2"),
    mkrow(303.792, "OTG_CURRENT~3.025A", current="3.025", ccode="0x22", parent="S3"),
    mkrow(303.793, "REG10=0xF6(DCDC_DIS)", parent="S4"),
    mkrow(303.947, "OTG_CURRENT~3.025A", current="3.025", ccode="0x22", parent="S5"),
    mkrow(303.948, "REG10=0xF4(DCDC_EN)", parent="S6"),
    mkrow(303.949, "REGISTER_READ 0x11 => 0x18", parent="S7"),
    mkrow(303.950, "REG11=0x18", parent="S8"),
    mkrow(303.951, "REGISTER_READ 0x16 => 0xAB", parent="S9"),
    mkrow(303.952, "REG16=0xAB", parent="S10"),
]
out = []
for r in rows:
    out += agg.feed(r)
assert len(out) == 1
tail = out[0]
assert tail["semantic_tail"] == "yes"
assert tail["dcdc_sequence"] == "DIS>EN"
assert tail["reg11_read_sequence"] == "18"
assert tail["reg11_write_sequence"] == "18"
assert tail["reg16_read_sequence"] == "AB"
assert tail["reg16_write_sequence"] == "AB"


print("v0.4.4 regression tests: PASS")
