#!/usr/bin/env python3
"""Read-only Modbus scanner for the Kostal PIKO CI 50 DC-input registers.

WHY: the collector's per-string map (CI_FLOAT_REGS in collector.py) assumes a
uniform +10 register stride (string4 = 296/298/300). That is WRONG for this
firmware — the installer's native Kostal portal shows all 4 strings producing,
but our dc_current_string3 / dc_power_string3 and the whole string4 block read 0.
A dusk scan (2026-07-02) found real DC-input voltages at unmapped regs 302 & 308,
so the true layout differs. This script dumps the full DC register neighbourhood
so the correct current/power addresses can be identified AT SOLAR NOON, when every
string carries 10-13 A / 5-8 kW and each register is unambiguous.

Run (from kostal-dashboard/, at ~13:00 local on a clear day):
    docker compose exec -T collector python - < collector/scan_ci_strings.py

It ONLY reads holding registers (read_holding_registers) — no writes.
Correlate the printed values against the Kostal portal per-string graph
(String1..4 kW) to build the corrected CI_FLOAT_REGS mapping.
"""
import os, struct, math
from pymodbus.client import ModbusTcpClient

IP = os.environ.get("INVERTER_CI_IP", "192.168.18.160")
PORT = 1502

c = ModbusTcpClient(IP, port=PORT, timeout=10)
if not c.connect():
    raise SystemExit(f"Could not connect to CI 50 at {IP}:{PORT}")


def f32(reg):
    r = c.read_holding_registers(reg, count=2)
    if r.isError():
        return None
    return struct.unpack(">f", struct.pack(">HH", r.registers[0], r.registers[1]))[0]


# Currently-mapped fields (collector.py CI_FLOAT_REGS) for reference/sanity.
KNOWN = {
    100: "dc_power_total", 172: "ac_power_total",
    266: "v_s1", 268: "i_s1", 270: "p_s1",
    276: "v_s2", 278: "i_s2", 280: "p_s2",
    286: "v_s3", 288: "i_s3", 290: "p_s3",
    296: "v_s4", 298: "i_s4", 300: "p_s4",
}

# Sanity anchors that we KNOW read correctly — print them so the operator can
# confirm the inverter really is at full production before trusting the scan.
print(f"CI 50 @ {IP}:{PORT}  — read-only DC register scan")
print("--- anchors (should be large at noon) ---")
for reg in (100, 172):
    print(f"  reg {reg} {KNOWN[reg]:<14} = {f32(reg)}")

print("\n--- DC neighbourhood 250-330 (sane floats only) ---")
print(f"{'reg':>4} {'value':>11}  {'mapped':<7} {'~type (by magnitude)'}")
for reg in range(250, 332):
    v = f32(reg)
    if v is None or math.isnan(v) or math.isinf(v) or abs(v) > 1e6:
        continue
    if abs(v) < 0.01:
        v = 0.0
    a = abs(v)
    t = "VOLTAGE" if 300 <= a <= 950 else \
        "CURRENT" if 3 <= a <= 25 else \
        "POWER-W" if 1000 <= a <= 13000 else ""
    tag = KNOWN.get(reg, "")
    if v != 0.0 or tag:
        print(f"{reg:>4} {v:>11.2f}  {tag:<7} {t}")

c.close()
print("\nNext: match POWER-W registers to Kostal portal String1..4 kW, then update"
      "\nCI_FLOAT_REGS in collector/collector.py (string3 current/power + string4 triplet).")
