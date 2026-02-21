import os
import struct
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

import requests
import schedule
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
KSEM_IP = os.environ.get("KSEM_IP", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 30))
OMIE_ENABLED = os.environ.get("OMIE_ENABLED", "false").lower() == "true"
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
    # Real-time home consumption (from KSEM via PIKO 15)
    83888128: ("self_consumption_power", float),
    83886336: ("home_solar_power", float),
    83886848: ("home_grid_power", float),
    83887106: ("home_power_l1", float),
    83887362: ("home_power_l2", float),
    83887618: ("home_power_l3", float),
    # Cumulative totals
    251659009: ("home_consumption_total", float),
    251659265: ("self_consumption_total", float),
    251659279: ("autarky_rate_daily", float),
    251659280: ("self_consumption_rate_total", float),
    251659281: ("autarky_rate_total", float),
}

BASE_URL = f"http://{INVERTER_IP}/api/dxs.json"

# Split DXS IDs into chunks of 20 to stay within PIKO 15 URL length limits
_DXS_IDS = list(DXS_FIELDS.keys())
DXS_CHUNKS = [_DXS_IDS[i:i+20] for i in range(0, len(_DXS_IDS), 20)]


def poll_piko15():
    """Poll Kostal PIKO 15 via HTTP API. Returns an InfluxDB Point or None."""
    point = Point("piko").tag("inverter", "piko_15")

    for chunk in DXS_CHUNKS:
        params = [("dxsEntries", str(k)) for k in chunk]
        resp = requests.get(BASE_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

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
    if raw == 0xFFFFFFFF:  # SunSpec "not implemented"
        return None
    sf_raw = result.registers[2]
    sf = struct.unpack(">h", struct.pack(">H", sf_raw))[0]
    wh = raw * (10 ** sf)
    kwh = wh / 1000.0
    # Reject garbage readings when inverter is off (real value ~65k kWh)
    if kwh > 500000:
        return None
    return kwh


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
# KSEM — Kostal Smart Energy Meter (Modbus TCP, port 502)
# SunSpec Model 203 (Three Phase Wye Meter) at register 40070
# Data registers start at 40072 (after model ID + length)
# ---------------------------------------------------------------------------

KSEM_SUNSPEC_BASE = 40072  # Model 203 data start


def _sunspec_sf(raw):
    """Decode a SunSpec scale factor (signed int16)."""
    return struct.unpack(">h", struct.pack(">H", raw))[0]


def _sunspec_int16(raw, sf):
    """Decode a SunSpec signed int16 value with scale factor."""
    if raw == 0x8000:  # SunSpec "not implemented"
        return None
    val = struct.unpack(">h", struct.pack(">H", raw))[0]
    return val * (10 ** sf)


def _sunspec_uint16(raw, sf):
    """Decode a SunSpec unsigned uint16 value with scale factor."""
    if raw in (0xFFFF, 0x8000):  # SunSpec "not implemented"
        return None
    return raw * (10 ** sf)


def _sunspec_uint32(hi, lo, sf):
    """Decode a SunSpec uint32 (two registers) with scale factor."""
    raw = (hi << 16) | lo
    if raw == 0xFFFFFFFF:
        return None
    return raw * (10 ** sf)


def poll_ksem():
    """Poll Kostal Smart Energy Meter via SunSpec Modbus TCP."""
    client = ModbusTcpClient(KSEM_IP, port=502, timeout=10)
    try:
        if not client.connect():
            return None

        # Read Model 203 data block (offsets 0-52, 53 registers)
        result = client.read_holding_registers(KSEM_SUNSPEC_BASE, count=53)
        if result.isError():
            return None
        d = result.registers

        point = Point("ksem")

        # Scale factors
        a_sf = _sunspec_sf(d[4])    # current
        v_sf = _sunspec_sf(d[13])   # voltage
        hz_sf = _sunspec_sf(d[15])  # frequency
        w_sf = _sunspec_sf(d[20])   # power
        pf_sf = _sunspec_sf(d[35])  # power factor

        # Current (uint16)
        for name, off in [("current_l1", 1), ("current_l2", 2), ("current_l3", 3)]:
            val = _sunspec_uint16(d[off], a_sf)
            if val is not None:
                point = point.field(name, float(val))

        # Voltage (uint16)
        for name, off in [("voltage_l1", 5), ("voltage_l2", 6), ("voltage_l3", 7)]:
            val = _sunspec_uint16(d[off], v_sf)
            if val is not None:
                point = point.field(name, float(val))

        # Frequency (uint16)
        val = _sunspec_uint16(d[14], hz_sf)
        if val is not None:
            point = point.field("frequency", float(val))

        # Power — signed int16 (positive = export, negative = import per SunSpec)
        for name, off in [("active_power_total", 16), ("active_power_l1", 17),
                          ("active_power_l2", 18), ("active_power_l3", 19)]:
            val = _sunspec_int16(d[off], w_sf)
            if val is not None:
                point = point.field(name, float(val))

        # Power factor (int16)
        val = _sunspec_int16(d[31], pf_sf)
        if val is not None:
            point = point.field("power_factor", float(val))

        # Energy counters (uint32, Wh) — store in kWh
        wh_sf = _sunspec_sf(d[52])
        # Export total (offset 36-37)
        val = _sunspec_uint32(d[36], d[37], wh_sf)
        if val is not None:
            point = point.field("energy_export_total", float(val) / 1000.0)
        # Import total (offset 44-45)
        val = _sunspec_uint32(d[44], d[45], wh_sf)
        if val is not None:
            point = point.field("energy_import_total", float(val) / 1000.0)

        return point
    finally:
        client.close()


# ---------------------------------------------------------------------------
# OMIE — Day-ahead market prices
# ---------------------------------------------------------------------------

CET = timezone(timedelta(hours=1))
CEST = timezone(timedelta(hours=2))

OMIE_URL = "https://www.omie.es/es/file-download?parents%5B0%5D=marginalpdbc&filename=marginalpdbc_{date}.1"


def _cet_now():
    """Current time in CET/CEST (simplified: use CET year-round for OMIE alignment)."""
    return datetime.now(CET)


def _parse_omie_file(text, target_date):
    """Parse OMIE marginalpdbc flat file. Returns list of (datetime, eur_mwh) tuples."""
    prices = []
    for line in text.strip().splitlines():
        parts = line.split(";")
        if len(parts) < 6:
            continue
        try:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            period = int(parts[3])
            price_spain = float(parts[4].replace(",", "."))
        except (ValueError, IndexError):
            continue
        if year != target_date.year or month != target_date.month or day != target_date.day:
            continue
        if period < 1 or period > 96:
            continue
        # Period 1 = 00:00-00:15, Period 2 = 00:15-00:30, etc.
        minutes = (period - 1) * 15
        ts = datetime(year, month, day, minutes // 60, minutes % 60, tzinfo=CET)
        prices.append((ts, price_spain))
    return prices


def fetch_omie_prices(write_api):
    """Fetch OMIE prices for today and tomorrow, write to InfluxDB."""
    today = _cet_now().date()
    dates = [today, today + timedelta(days=1)]

    for d in dates:
        date_str = d.strftime("%Y%m%d")
        url = OMIE_URL.format(date=date_str)
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 404:
                log.debug("OMIE: no data yet for %s", date_str)
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("OMIE: failed to fetch %s: %s", date_str, e)
            continue

        prices = _parse_omie_file(resp.text, d)
        if not prices:
            log.warning("OMIE: no prices parsed for %s", date_str)
            continue

        points = []
        for ts, eur_mwh in prices:
            point = (
                Point("omie_prices")
                .time(ts)
                .field("price_eur_mwh", float(eur_mwh))
                .field("price_eur_kwh", float(eur_mwh / 1000.0))
            )
            points.append(point)

        try:
            write_api.write(bucket=INFLUXDB_BUCKET, record=points)
            log.info("OMIE: wrote %d price points for %s", len(points), date_str)
        except Exception:
            log.exception("OMIE: failed to write prices for %s", date_str)


def _omie_thread(write_api):
    """Background thread: fetch OMIE prices on startup, then hourly."""
    log.info("OMIE thread started")
    # Initial fetch
    fetch_omie_prices(write_api)

    schedule.every(1).hours.do(fetch_omie_prices, write_api)

    while True:
        schedule.run_pending()
        time.sleep(60)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    log.info("Starting solar plant collector")
    log.info("PIKO 15: %s | PIKO CI 50: %s | KSEM: %s | Poll interval: %ds",
             INVERTER_IP, INVERTER_CI_IP or "(disabled)",
             KSEM_IP or "(disabled)", POLL_INTERVAL)

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    # Start OMIE background thread
    if OMIE_ENABLED:
        omie = threading.Thread(target=_omie_thread, args=(write_api,), daemon=True)
        omie.start()
        log.info("OMIE price collector enabled")
    else:
        log.info("OMIE price collector disabled (set OMIE_ENABLED=true to enable)")

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

        # --- KSEM ---
        if KSEM_IP:
            try:
                point = poll_ksem()
                if point is not None:
                    write_api.write(bucket=INFLUXDB_BUCKET, record=point)
                    log.info("KSEM data written")
                else:
                    log.debug("KSEM unreachable")
            except Exception:
                log.exception("Error polling KSEM")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
