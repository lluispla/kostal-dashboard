import os
import struct
import time
import logging

import requests
from pymodbus.client import ModbusTcpClient
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("collector")

INVERTER_IP = os.environ["INVERTER_IP"]
INVERTER_CI_IP = os.environ.get("INVERTER_CI_IP", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 30))
INFLUXDB_URL = os.environ["INFLUXDB_URL"]
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]

# ---------------------------------------------------------------------------
# PIKO 15 — HTTP / dxs.json
# ---------------------------------------------------------------------------

DXS_FIELDS = {
    67109120: ("ac_power_total", float),
    33556736: ("dc_power_total", float),
    33555203: ("dc_power_string1", float),
    33555459: ("dc_power_string2", float),
    33555715: ("dc_power_string3", float),
    33555202: ("dc_voltage_string1", float),
    33555458: ("dc_voltage_string2", float),
    33555714: ("dc_voltage_string3", float),
    33555201: ("dc_current_string1", float),
    33555457: ("dc_current_string2", float),
    33555713: ("dc_current_string3", float),
    67109379: ("ac_power_l1", float),
    67109635: ("ac_power_l2", float),
    67109891: ("ac_power_l3", float),
    67109378: ("ac_voltage_l1", float),
    67109634: ("ac_voltage_l2", float),
    67109890: ("ac_voltage_l3", float),
    67110400: ("grid_frequency", float),
    251658753: ("yield_total", float),
    251658754: ("yield_daily", float),
    251658496: ("operating_hours", float),
    16780032: ("status", int),
    251659010: ("home_consumption_daily", float),
    251659266: ("self_consumption_daily", float),
    251659278: ("self_consumption_rate_daily", float),
}

BASE_URL = f"http://{INVERTER_IP}/api/dxs.json"
DXS_PARAMS = [("dxsEntries", str(k)) for k in DXS_FIELDS]


def poll_piko15():
    """Poll Kostal PIKO 15 via HTTP API. Returns an InfluxDB Point or None."""
    resp = requests.get(BASE_URL, params=DXS_PARAMS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    point = Point("piko").tag("inverter", "piko_15")
    for entry in data.get("dxsEntries", []):
        dxs_id = entry["dxsId"]
        if dxs_id in DXS_FIELDS:
            field_name, cast = DXS_FIELDS[dxs_id]
            point = point.field(field_name, cast(entry["value"]))

    return point


# ---------------------------------------------------------------------------
# PIKO CI 50 — Modbus TCP (port 1502)
# ---------------------------------------------------------------------------

# Proprietary float32 registers (holding, device_id=1)
CI_FLOAT_REGS = {
    100: "dc_power_total",
    152: "grid_frequency",
    154: "ac_current_l1",
    156: "ac_power_l1",
    158: "ac_voltage_l1",
    160: "ac_current_l2",
    162: "ac_power_l2",
    164: "ac_voltage_l2",
    166: "ac_current_l3",
    168: "ac_power_l3",
    170: "ac_voltage_l3",
    172: "ac_power_total",
    266: "dc_voltage_string1",
    268: "dc_current_string1",
    270: "dc_power_string1",
    276: "dc_voltage_string2",
    278: "dc_current_string2",
    280: "dc_power_string2",
    286: "dc_voltage_string3",
    288: "dc_current_string3",
    290: "dc_power_string3",
    296: "dc_voltage_string4",
    298: "dc_current_string4",
    300: "dc_power_string4",
}

CI_STATUS_REG = 56  # uint16, single register


def _read_float32(client, register):
    """Read a Kostal proprietary float32 from two holding registers (big-endian)."""
    result = client.read_holding_registers(register, count=2)
    if result.isError():
        return None
    raw = struct.pack(">HH", result.registers[0], result.registers[1])
    return struct.unpack(">f", raw)[0]


def _read_uint16(client, register):
    """Read a single uint16 holding register."""
    result = client.read_holding_registers(register, count=1)
    if result.isError():
        return None
    return result.registers[0]


def _read_sunspec_energy(client):
    """Read SunSpec lifetime AC energy (uint32 + SF) at regs 40092-40094, return kWh."""
    result = client.read_holding_registers(40092, count=3)
    if result.isError():
        return None
    raw = (result.registers[0] << 16) | result.registers[1]
    sf_raw = result.registers[2]
    # SF is a signed int16
    sf = struct.unpack(">h", struct.pack(">H", sf_raw))[0]
    wh = raw * (10 ** sf)
    return wh / 1000.0  # convert Wh → kWh


def poll_piko_ci():
    """Poll Kostal PIKO CI 50 via Modbus TCP. Returns an InfluxDB Point or None."""
    client = ModbusTcpClient(INVERTER_CI_IP, port=1502, timeout=10)
    try:
        if not client.connect():
            return None

        point = Point("piko").tag("inverter", "piko_ci_50")

        # Status (uint16)
        status = _read_uint16(client, CI_STATUS_REG)
        if status is not None:
            point = point.field("status", int(status))

        # Proprietary float32 registers
        for reg, field_name in CI_FLOAT_REGS.items():
            val = _read_float32(client, reg)
            if val is not None:
                point = point.field(field_name, float(val))

        # SunSpec lifetime energy → yield_total in kWh
        yield_total = _read_sunspec_energy(client)
        if yield_total is not None:
            point = point.field("yield_total", float(yield_total))

        return point
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    log.info("Starting solar plant collector")
    log.info("PIKO 15: %s | PIKO CI 50: %s | Poll interval: %ds",
             INVERTER_IP, INVERTER_CI_IP or "(disabled)", POLL_INTERVAL)

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    while True:
        # --- PIKO 15 ---
        try:
            point = poll_piko15()
            write_api.write(bucket=INFLUXDB_BUCKET, record=point)
            log.info("PIKO 15 data written")
        except requests.exceptions.ConnectionError:
            log.debug("PIKO 15 unreachable (likely night time)")
        except Exception:
            log.exception("Error polling PIKO 15")

        # --- PIKO CI 50 ---
        if INVERTER_CI_IP:
            try:
                point = poll_piko_ci()
                if point is not None:
                    write_api.write(bucket=INFLUXDB_BUCKET, record=point)
                    log.info("PIKO CI data written")
                else:
                    log.debug("PIKO CI 50 unreachable (likely night time)")
            except Exception:
                log.exception("Error polling PIKO CI 50")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
