import os
import struct
import sys
import time
import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import schedule
import urllib3
from pymodbus.client import ModbusTcpClient
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS


class _InfluxUnreachable(Exception):
    """Raised when an InfluxDB write fails due to a connection error."""

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
OMIE_BACKFILL_DAYS = int(os.environ.get("OMIE_BACKFILL_DAYS", 30))

# Telegram alerts
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ALERT_FAIL_THRESHOLD = int(os.environ.get("ALERT_FAIL_THRESHOLD", 10))
INFLUXDB_URL = os.environ["INFLUXDB_URL"]
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]

# ---------------------------------------------------------------------------
# Telegram alerts
# ---------------------------------------------------------------------------

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_GETME = "https://api.telegram.org/bot{token}/getMe"


def verify_telegram_token():
    """Call getMe to check that the bot token is live. Returns True if valid."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    try:
        resp = requests.get(
            TELEGRAM_GETME.format(token=TELEGRAM_BOT_TOKEN), timeout=10,
        )
        if resp.status_code == 200:
            return True
        log.error(
            "[Telegram] ================================================\n"
            "[Telegram] TOKEN INVALID — alerts disabled (status %d)\n"
            "[Telegram] Fix: open @BotFather → /mybots → API Token → copy\n"
            "[Telegram] then update TELEGRAM_BOT_TOKEN in .env and\n"
            "[Telegram] run: docker compose restart collector\n"
            "[Telegram] ================================================",
            resp.status_code,
        )
    except Exception as e:
        log.error("[Telegram] getMe failed: %s — alerts disabled", e)
    return False


def send_telegram(message):
    """Send a Telegram notification."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            TELEGRAM_API.format(token=TELEGRAM_BOT_TOKEN),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("[Telegram] Alert sent")
            return True
        log.error("[Telegram] API error %d: %s", resp.status_code, resp.text)
    except Exception as e:
        log.error("[Telegram] Failed: %s", e)
    return False


class DeviceTracker:
    """Track consecutive failures per device and send alerts on threshold / recovery."""

    def __init__(self, threshold=ALERT_FAIL_THRESHOLD):
        self.threshold = threshold
        self.fail_counts = {}   # device -> consecutive failures
        self.alerted = {}       # device -> True if alert already sent
        self.was_online = {}    # device -> True if ever seen online

    def _is_daytime(self):
        """Check if it's daytime (7:00-22:00 CET) — inverter alerts only during day."""
        hour = datetime.now(ZoneInfo("Europe/Madrid")).hour
        return 7 <= hour < 22

    def report_ok(self, device):
        """Device polled successfully."""
        was_down = self.alerted.get(device, False)
        self.fail_counts[device] = 0
        self.alerted[device] = False
        self.was_online[device] = True
        if was_down:
            log.warning("[%s] RECOVERED — device back online", device)
            send_telegram(f"<b>RECUPERAT</b> {device}\nTorna a estar en línia.")

    def report_fail(self, device, is_inverter=False):
        """Device poll failed. Only alert for inverters during daytime."""
        if is_inverter and not self._is_daytime():
            return  # Normal for inverters to be offline at night
        self.fail_counts[device] = self.fail_counts.get(device, 0) + 1
        count = self.fail_counts[device]
        if count >= self.threshold and not self.alerted.get(device, False):
            minutes = count * POLL_INTERVAL // 60
            # Log at WARNING so the outage is visible even if Telegram is down.
            log.warning(
                "[%s] UNREACHABLE for ~%d min (%d consecutive failed polls)",
                device, minutes, count,
            )
            send_telegram(
                f"<b>ALERTA</b> {device}\n"
                f"No respon des de fa ~{minutes} min ({count} intents fallits)."
            )
            self.alerted[device] = True


tracker = DeviceTracker()


def _daily_summary_thread(query_api):
    """Send a daily production summary at 21:30 local time."""
    MADRID_TZ = ZoneInfo("Europe/Madrid")

    def send_summary():
        now = datetime.now(MADRID_TZ)
        if now.hour != 21 or now.minute < 25 or now.minute > 35:
            return
        try:
            today_str = now.strftime("%Y-%m-%d")
            offset = now.strftime("%z")  # e.g. "+0100" or "+0200"
            offset_fmt = offset[:3] + ":" + offset[3:]  # "+01:00" or "+02:00"
            flux = f'''
                from(bucket: "{INFLUXDB_BUCKET}")
                  |> range(start: {today_str}T00:00:00{offset_fmt}, stop: {today_str}T23:59:59{offset_fmt})
                  |> filter(fn: (r) => r._measurement == "piko")
                  |> filter(fn: (r) => r._field == "yield_daily")
                  |> last()
            '''
            tables = query_api.query(flux)
            total_kwh = 0.0
            for table in tables:
                for rec in table.records:
                    total_kwh += rec.get_value() or 0.0

            # Get import/export from KSEM
            flux_ksem = f'''
                from(bucket: "{INFLUXDB_BUCKET}")
                  |> range(start: {today_str}T00:00:00{offset_fmt}, stop: {today_str}T23:59:59{offset_fmt})
                  |> filter(fn: (r) => r._measurement == "ksem")
                  |> filter(fn: (r) => r._field == "active_power_total")
                  |> aggregateWindow(every: 30s, fn: mean, createEmpty: false)
            '''
            tables_ksem = query_api.query(flux_ksem)
            import_kwh = 0.0
            export_kwh = 0.0
            for table in tables_ksem:
                for rec in table.records:
                    val = rec.get_value() or 0.0
                    # W * 30s / 3600 / 1000 = kWh per 30s interval
                    energy = abs(val) * 30 / 3_600_000
                    if val < 0:
                        import_kwh += energy
                    else:
                        export_kwh += energy

            lines = [
                f"<b>RESUM DIARI</b> — {today_str}",
                f"Producció: <b>{total_kwh:.1f} kWh</b>",
                f"Exportació: {export_kwh:.1f} kWh",
                f"Importació: {import_kwh:.1f} kWh",
            ]
            if total_kwh > 0:
                autoconsum = max(0, total_kwh - export_kwh)
                pct = autoconsum / total_kwh * 100
                lines.append(f"Autoconsum: {autoconsum:.1f} kWh ({pct:.0f}%)")

            send_telegram("\n".join(lines))
        except Exception as e:
            log.error("[Daily summary] Error: %s", e)

    while True:
        send_summary()
        time.sleep(300)  # Check every 5 minutes


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
            if dxs_id in DXS_FIELDS and entry["value"] is not None:
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
    # Per-string DC. The strings do NOT follow a uniform stride on this CI 50:
    # each one is current, power, then voltage 6 registers later. Taken from the
    # KOSTAL PIKO CI MODBUS interface description, register table p. 30.
    #
    # A previous map paired each string's voltage with the NEXT string's current
    # and power (s1←DC2, s2←DC3, s3←DC1). That survived the 2026-07-03 solar-noon
    # scan because a rotation preserves Σ(p_s1..s4) ≈ dc_power_total exactly, and
    # V*I≈P still held while the strings sat at similar voltages. Corrected and
    # re-validated live 2026-08-09 on a day with dissimilar strings (563 W vs
    # 87 W): P/(V*I) = 1.07 / 1.01 / 1.07 / 1.02 across all four. Totals were
    # never affected — only which string got blamed. See scan_ci_strings.py.
    258: "dc_current_string1",
    260: "dc_power_string1",
    266: "dc_voltage_string1",
    268: "dc_current_string2",
    270: "dc_power_string2",
    276: "dc_voltage_string2",
    278: "dc_current_string3",
    280: "dc_power_string3",
    286: "dc_voltage_string3",
    300: "dc_current_string4",
    302: "dc_power_string4",
    308: "dc_voltage_string4",
}

# "Inverter state". KOSTAL's own table (PIKO CI MODBUS interface description,
# 0x38/56) labels the format U16 but gives N = 2 registers: the value arrives as
# a big-endian word pair with the payload in the LOW register, so reg 56 is
# always 0 and reg 57 carries the state. Reading only reg 56 pinned status to 0
# for the whole history, which made the dashboard print "Apagat" whenever the
# zero-export limiter held AC power at 0 W. The neighbouring Power-ID (reg 54,
# also "U16, N=2") confirms the layout: reg 55 reads 50050 on this PIKO CI 50.
CI_STATUS_REG = 56


def _read_float32(client, register):
    """Read a Kostal proprietary float32 from two holding registers (big-endian)."""
    result = client.read_holding_registers(register, count=2)
    if result.isError():
        return None
    raw = struct.pack(">HH", result.registers[0], result.registers[1])
    return struct.unpack(">f", raw)[0]


def _read_uint32(client, register):
    """Read a big-endian uint32 from two holding registers."""
    result = client.read_holding_registers(register, count=2)
    if result.isError():
        return None
    return (result.registers[0] << 16) | result.registers[1]


_last_yield_total = None  # monotonic guard across polls


def _read_sunspec_energy(client):
    """Read SunSpec lifetime AC energy (uint32 + SF) at regs 40092-40094, return kWh.

    The 32-bit register pair can produce torn reads when the inverter
    increments the counter between the two 16-bit halves — observed as
    spurious low-word-only values (~65 kWh) and ±200 kWh jitter on a real
    ~65k kWh counter. Mitigation: read 3 times, reject if the readings
    disagree by more than 1 kWh, then enforce monotonic increase.
    """
    global _last_yield_total

    def _one_read():
        result = client.read_holding_registers(40092, count=3)
        if result.isError():
            return None
        raw = (result.registers[0] << 16) | result.registers[1]
        if raw == 0xFFFFFFFF:  # SunSpec "not implemented"
            return None
        sf = struct.unpack(">h", struct.pack(">H", result.registers[2]))[0]
        kwh = raw * (10 ** sf) / 1000.0
        if kwh > 500000:  # garbage (inverter off, register noise)
            return None
        return kwh

    readings = [r for r in (_one_read() for _ in range(3)) if r is not None]
    if not readings:
        return None
    if max(readings) - min(readings) > 1.0:
        # Inconsistent snapshot — at least one half was torn this round.
        return None

    kwh = sorted(readings)[len(readings) // 2]

    if _last_yield_total is not None and kwh < _last_yield_total:
        return None  # lifetime counter cannot decrease
    _last_yield_total = kwh
    return kwh


def poll_piko_ci():
    """Poll Kostal PIKO CI 50 via Modbus TCP. Returns an InfluxDB Point or None."""
    client = ModbusTcpClient(INVERTER_CI_IP, port=1502, timeout=10)
    try:
        if not client.connect():
            return None

        point = Point("piko").tag("inverter", "piko_ci_50")

        # Status (uint16)
        status = _read_uint32(client, CI_STATUS_REG)
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

        # Voltage (uint16) — offset 5 is PhV (average LN), which this meter
        # reports as "not implemented"; the per-phase values start at 6.
        for name, off in [("voltage_l1", 6), ("voltage_l2", 7), ("voltage_l3", 8)]:
            val = _sunspec_uint16(d[off], v_sf)
            if val is not None:
                point = point.field(name, float(val))

        # Frequency (uint16)
        val = _sunspec_uint16(d[14], hz_sf)
        if val is not None:
            point = point.field("frequency", float(val))

        # Power — signed int16 (positive = import, negative = export; verified
        # at night with PV=0, where active_power_total reads positive)
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

MADRID_TZ = ZoneInfo("Europe/Madrid")

OMIE_URL = "https://www.omie.es/es/file-download?parents%5B0%5D=marginalpdbc&filename=marginalpdbc_{date}.1"


def _cet_now():
    """Current time in Europe/Madrid (CET/CEST with DST)."""
    return datetime.now(MADRID_TZ)


def _parse_omie_file(text, target_date):
    """Parse OMIE marginalpdbc flat file. Returns list of (datetime, eur_mwh) tuples.

    Layout: YYYY;MM;DD;period;price_PT;price_ES;
    We want the Spanish system marginal price, which is column index 5 (parts[5]).
    Column index 4 is Portugal — the two coincide most days (shared MIBEL market)
    but diverge when the interconnection saturates, so reading the wrong one is a
    real error on those days.
    """
    prices = []
    # Anchor each period to real elapsed time from local midnight so DST transition
    # days work: 92-period (spring-forward) and 100-period (fall-back) days both map
    # correctly because we add real duration to a UTC anchor instead of building a
    # naive wall-clock time (02:00-03:00 doesn't exist / 02:00-03:00 repeats locally).
    midnight_utc = datetime(
        target_date.year, target_date.month, target_date.day, tzinfo=MADRID_TZ
    ).astimezone(timezone.utc)
    for line in text.strip().splitlines():
        parts = line.split(";")
        if len(parts) < 6:
            continue
        try:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            period = int(parts[3])
            price_spain = float(parts[5].replace(",", "."))
        except (ValueError, IndexError):
            continue
        if year != target_date.year or month != target_date.month or day != target_date.day:
            continue
        # Up to 100 quarter-hourly periods (25h fall-back DST day); normally 96.
        if period < 1 or period > 100:
            continue
        # Period 1 = first quarter-hour after local midnight, period 2 the next, etc.
        ts = midnight_utc + timedelta(minutes=(period - 1) * 15)
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


def _omie_backfill(write_api, query_api):
    """Check for OMIE price gaps in the last N days and backfill missing dates."""
    if OMIE_BACKFILL_DAYS <= 0:
        return

    today = _cet_now().date()
    start_date = today - timedelta(days=OMIE_BACKFILL_DAYS)

    # Query InfluxDB for dates that already have OMIE data
    flux = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -{OMIE_BACKFILL_DAYS}d)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_mwh")
          |> aggregateWindow(every: 1d, fn: count, createEmpty: false)
          |> filter(fn: (r) => r._value >= 20)
    '''
    existing_dates = set()
    try:
        tables = query_api.query(flux)
        for table in tables:
            for rec in table.records:
                t = rec.get_time()
                if t is not None:
                    existing_dates.add(t.astimezone(MADRID_TZ).date())
    except Exception:
        log.exception("OMIE backfill: failed to query existing dates")
        return

    # Find missing dates (exclude tomorrow — may not be published yet)
    missing = []
    d = start_date
    while d <= today:
        if d not in existing_dates:
            missing.append(d)
        d += timedelta(days=1)

    if not missing:
        log.info("OMIE backfill: no gaps found in last %d days", OMIE_BACKFILL_DAYS)
        return

    log.info("OMIE backfill: found %d missing dates, fetching...", len(missing))
    filled = 0
    for d in missing:
        date_str = d.strftime("%Y%m%d")
        url = OMIE_URL.format(date=date_str)
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 404:
                log.debug("OMIE backfill: no file for %s (expected for future dates)", date_str)
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            log.warning("OMIE backfill: failed to fetch %s: %s", date_str, e)
            continue

        prices = _parse_omie_file(resp.text, d)
        if not prices:
            log.warning("OMIE backfill: no prices parsed for %s", date_str)
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
            log.info("OMIE backfill: wrote %d points for %s", len(points), date_str)
            filled += 1
        except Exception:
            log.exception("OMIE backfill: failed to write %s", date_str)

        # Small delay to avoid hammering OMIE
        time.sleep(0.5)

    log.info("OMIE backfill: completed, filled %d/%d missing dates", filled, len(missing))


# ---------------------------------------------------------------------------
# National Weather — Open-Meteo forecasts for renewable energy regions
# ---------------------------------------------------------------------------

WEATHER_REGIONS = {
    "andalucia":          {"lat": 37.38, "lon": -5.98},
    "castilla_la_mancha": {"lat": 39.47, "lon": -3.00},
    "extremadura":        {"lat": 39.47, "lon": -6.37},
    "murcia":             {"lat": 37.98, "lon": -1.13},
    "aragon":             {"lat": 41.65, "lon": -0.88},
    "galicia":            {"lat": 42.88, "lon": -8.54},
}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_national_weather(write_api):
    """Fetch solar irradiance and wind speed forecasts for key Spanish regions."""
    total_points = 0
    for region, coords in WEATHER_REGIONS.items():
        try:
            resp = requests.get(
                OPEN_METEO_URL,
                params={
                    "latitude": coords["lat"],
                    "longitude": coords["lon"],
                    "hourly": "global_tilted_irradiance,wind_speed_120m",
                    "tilt": 30,
                    "azimuth": 0,
                    "timezone": "Europe/Madrid",
                    "forecast_days": 3,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.warning("[NatWeather] Failed to fetch %s: %s", region, e)
            continue

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        gti_values = hourly.get("global_tilted_irradiance", [])
        wind_values = hourly.get("wind_speed_120m", [])

        if not times:
            log.warning("[NatWeather] No hourly data for %s", region)
            continue

        points = []
        for i, time_str in enumerate(times):
            # time_str is like "2026-03-07T00:00" in Europe/Madrid
            try:
                ts = datetime.strptime(time_str, "%Y-%m-%dT%H:%M").replace(tzinfo=MADRID_TZ)
            except ValueError:
                continue

            gti = gti_values[i] if i < len(gti_values) and gti_values[i] is not None else 0.0
            wind = wind_values[i] if i < len(wind_values) and wind_values[i] is not None else 0.0

            point = (
                Point("national_weather")
                .tag("region", region)
                .time(ts)
                .field("gti_wm2", float(gti))
                .field("wind_speed_ms", float(wind))
            )
            points.append(point)

        if points:
            try:
                write_api.write(bucket=INFLUXDB_BUCKET, record=points)
                log.info("[NatWeather] Wrote %d points for region %s", len(points), region)
                total_points += len(points)
            except Exception:
                log.exception("[NatWeather] Failed to write points for %s", region)

        # Small delay between regions to be polite to the API
        time.sleep(1)

    return total_points


def fetch_local_irradiance(write_api):
    """Fetch and store local site solar irradiance forecast for long-term analysis.

    Stores hourly GTI (global tilted irradiance) and estimated PV output
    for L'Escala site (42.12°N, 3.13°E, 30° tilt, south-facing, 65 kWp).
    """
    lat, lon = 42.12, 3.13
    kwp, tilt, azimuth, efficiency = 65.0, 30, 0, 0.80

    try:
        resp = requests.get(
            OPEN_METEO_URL,
            params={
                "latitude": lat, "longitude": lon,
                "hourly": "global_tilted_irradiance,temperature_2m,cloud_cover",
                "tilt": tilt, "azimuth": azimuth,
                "timezone": "Europe/Madrid",
                "forecast_days": 3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        log.warning("[LocalIrr] Failed to fetch: %s", e)
        return 0

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    gti_vals = hourly.get("global_tilted_irradiance", [])
    temp_vals = hourly.get("temperature_2m", [])
    cloud_vals = hourly.get("cloud_cover", [])

    points = []
    for i, t_str in enumerate(times):
        try:
            ts = datetime.strptime(t_str, "%Y-%m-%dT%H:%M").replace(tzinfo=MADRID_TZ)
        except ValueError:
            continue

        gti = gti_vals[i] if i < len(gti_vals) and gti_vals[i] is not None else 0.0
        temp = temp_vals[i] if i < len(temp_vals) and temp_vals[i] is not None else 0.0
        cloud = cloud_vals[i] if i < len(cloud_vals) and cloud_vals[i] is not None else 0.0
        pv_kw = gti * kwp * efficiency / 1000.0  # estimated output in kW

        point = (
            Point("local_irradiance")
            .time(ts)
            .field("gti_wm2", float(gti))
            .field("pv_estimated_kw", round(pv_kw, 2))
            .field("temperature_c", float(temp))
            .field("cloud_cover_pct", float(cloud))
        )
        points.append(point)

    if points:
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, record=points)
            log.info("[LocalIrr] Wrote %d points (3-day forecast)", len(points))
        except Exception:
            log.exception("[LocalIrr] Failed to write")

    return len(points)


def _national_weather_thread(write_api):
    """Background thread: fetch weather forecasts on startup, then every 6 hours."""
    log.info("[NatWeather] Thread started")

    # Initial fetch
    try:
        fetch_national_weather(write_api)
        fetch_local_irradiance(write_api)
    except Exception:
        log.exception("[NatWeather] Error on initial fetch")

    schedule.every(6).hours.do(fetch_national_weather, write_api)
    schedule.every(6).hours.do(fetch_local_irradiance, write_api)

    while True:
        schedule.run_pending()
        time.sleep(60)


def _omie_thread(write_api, query_api):
    """Background thread: backfill gaps, fetch today+tomorrow, then hourly."""
    log.info("OMIE thread started")

    # Backfill any gaps first
    _omie_backfill(write_api, query_api)

    # Normal fetch (today + tomorrow)
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
    query_api = client.query_api()

    # Start OMIE background thread
    if OMIE_ENABLED:
        omie = threading.Thread(target=_omie_thread, args=(write_api, query_api), daemon=True)
        omie.start()
        log.info("OMIE price collector enabled (backfill: %d days)", OMIE_BACKFILL_DAYS)
    else:
        log.info("OMIE price collector disabled (set OMIE_ENABLED=true to enable)")

    # Start national weather thread
    weather = threading.Thread(target=_national_weather_thread, args=(write_api,), daemon=True)
    weather.start()
    log.info("National weather collector enabled (6h interval, %d regions)", len(WEATHER_REGIONS))

    # Start daily summary thread
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and verify_telegram_token():
        summary = threading.Thread(target=_daily_summary_thread, args=(query_api,), daemon=True)
        summary.start()
        log.info("Telegram alerts enabled (threshold: %d failures)", ALERT_FAIL_THRESHOLD)
        send_telegram("<b>COLLECTOR INICIAT</b>\nEl col·lector de dades s'ha engegat.")
    else:
        log.info("Telegram alerts disabled (missing creds or token invalid)")

    # If InfluxDB writes fail this many times in a row, exit non-zero so
    # Docker restarts us — clears stale DNS / urllib3 pool on network blips.
    INFLUX_FAIL_LIMIT = 10
    influx_fail_streak = 0

    def _write_influx(point):
        """Write a point; on connection/DNS errors raise InfluxUnreachable.
        Other exceptions propagate as before (treated as device-side errors).
        """
        nonlocal influx_fail_streak
        try:
            write_api.write(bucket=INFLUXDB_BUCKET, record=point)
            influx_fail_streak = 0
        except (
            urllib3.exceptions.NameResolutionError,
            urllib3.exceptions.MaxRetryError,
            requests.exceptions.ConnectionError,
        ) as e:
            influx_fail_streak += 1
            log.error("InfluxDB write failed (%d/%d): %s",
                      influx_fail_streak, INFLUX_FAIL_LIMIT, e)
            if influx_fail_streak >= INFLUX_FAIL_LIMIT:
                log.critical(
                    "InfluxDB unreachable for %d consecutive writes — exiting "
                    "to let Docker restart the container", influx_fail_streak,
                )
                sys.exit(1)
            raise _InfluxUnreachable() from e

    while True:
        # --- PIKO 15 ---
        try:
            point = poll_piko15()
            _write_influx(point)
            log.info("PIKO 15 data written")
            tracker.report_ok("PIKO 15")
        except _InfluxUnreachable:
            pass  # already logged; keep loop alive until exit threshold
        except requests.exceptions.ConnectionError:
            log.debug("PIKO 15 unreachable (likely night time)")
            tracker.report_fail("PIKO 15", is_inverter=True)
        except Exception:
            log.exception("Error polling PIKO 15")
            tracker.report_fail("PIKO 15", is_inverter=True)

        # --- PIKO CI 50 ---
        if INVERTER_CI_IP:
            try:
                point = poll_piko_ci()
                if point is not None:
                    _write_influx(point)
                    log.info("PIKO CI data written")
                    tracker.report_ok("PIKO CI 50")
                else:
                    log.debug("PIKO CI 50 unreachable (likely night time)")
                    tracker.report_fail("PIKO CI 50", is_inverter=True)
            except _InfluxUnreachable:
                pass
            except Exception:
                log.exception("Error polling PIKO CI 50")
                tracker.report_fail("PIKO CI 50", is_inverter=True)

        # --- KSEM ---
        if KSEM_IP:
            try:
                point = poll_ksem()
                if point is not None:
                    _write_influx(point)
                    log.info("KSEM data written")
                    tracker.report_ok("KSEM")
                else:
                    log.debug("KSEM unreachable")
                    tracker.report_fail("KSEM", is_inverter=False)
            except _InfluxUnreachable:
                pass
            except Exception:
                log.exception("Error polling KSEM")
                tracker.report_fail("KSEM", is_inverter=False)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
