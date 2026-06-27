"""InfluxDB queries — compute all metrics from reliable raw measurements.

The PIKO 15's self-consumption fields (self_consumption_power,
self_consumption_daily, home_consumption_daily, self_consumption_rate_daily)
often report 0 when the inverter is offline or misconfigured.  The PIKO CI 50's
yield_total counter can also be stuck.

Strategy: derive everything from three reliable sources:
  1. ac_power_total (both inverters) — instantaneous + integral for kWh
  2. KSEM active_power_total — real-time grid flow
  3. KSEM energy_import_total / energy_export_total — daily kWh counters

Energy balance:
  generation = self_consumption + export
  consumption = self_consumption + import
  self_consumption = generation - export
"""

import calendar
import csv
import json
import logging
import math
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests
from influxdb_client import InfluxDBClient

from config import (
    INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET,
    BACKUP_INFLUXDB_URL,
    PRICING_PATH,
    PIKO_15_RATED_W, PIKO_CI_50_RATED_W, STATUS_MAP,
)

_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
_query_api = _client.query_api()

from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS
_write_api = _client.write_api(write_options=SYNCHRONOUS)

# -- helpers -----------------------------------------------------------------

_CET = ZoneInfo("Europe/Madrid")


def _cet_now():
    """Return current datetime in Europe/Madrid (CET/CEST with DST)."""
    return datetime.now(_CET)


def _is_holiday(dt):
    """Check if a date is a national/Catalunya holiday (3.0TD festiu → P6).

    Includes fixed national holidays and Catalunya-specific holidays.
    Easter-based movable holidays are computed for each year.
    """
    m, d = dt.month, dt.day
    y = dt.year

    # Fixed national holidays (Spain)
    fixed = {
        (1, 1),    # Cap d'Any
        (1, 6),    # Reis
        (5, 1),    # Dia del Treballador
        (8, 15),   # Assumpció
        (10, 12),  # Festa Nacional d'Espanya
        (11, 1),   # Tots Sants
        (12, 6),   # Dia de la Constitució
        (12, 8),   # Immaculada Concepció
        (12, 25),  # Nadal
    }

    # Catalunya-specific holidays
    fixed.add((6, 24))   # Sant Joan
    fixed.add((9, 11))   # Diada Nacional de Catalunya
    fixed.add((12, 26))  # Sant Esteve

    if (m, d) in fixed:
        return True

    # Easter-based movable holidays (anonymous algorithm for Gregorian calendar)
    a = y % 19
    b = y // 100
    c = y % 100
    dd = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    hh = (19 * a + b - dd - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - hh - k) % 7
    mm = (a + 11 * hh + 22 * l) // 451
    month_e = (hh + l - 7 * mm + 114) // 31
    day_e = (hh + l - 7 * mm + 114) % 31 + 1

    # Easter Sunday
    easter = datetime(y, month_e, day_e, tzinfo=dt.tzinfo)

    # Movable holidays
    divendres_sant = easter - timedelta(days=2)   # Divendres Sant
    dilluns_pasqua = easter + timedelta(days=1)    # Dilluns de Pasqua (Catalunya)

    easter_date = easter.date() if hasattr(easter, 'date') else easter
    dt_date = dt.date() if hasattr(dt, 'date') else dt

    if dt_date in (divendres_sant.date(), dilluns_pasqua.date()):
        return True

    return False


# 3.0TD monthly-rotating period scheme (Circular 3/2020 CNMC, Peninsular)
_PEAK_HOURS = {10, 11, 12, 13, 18, 19, 20, 21}
_SHOULDER_HOURS = {8, 9, 14, 15, 16, 17, 22, 23}
_MONTH_TO_PERIODS = {
    1:  ("P1", "P2"),  2:  ("P1", "P2"),   # Group A (alta)
    7:  ("P1", "P2"),  12: ("P1", "P2"),
    3:  ("P2", "P3"),  11: ("P2", "P3"),    # Group B (media alta)
    6:  ("P3", "P4"),  8:  ("P3", "P4"),    # Group C (media)
    9:  ("P3", "P4"),
    4:  ("P4", "P5"),  5:  ("P4", "P5"),    # Group D (baja)
    10: ("P4", "P5"),
}


def _get_period(dt):
    """Return 3.0TD period 'P1'-'P6' for a CET datetime.

    Uses the correct monthly-rotating scheme from the official BOE regulation.
    Weekend (Sat+Sun) and holidays are always P6.  Weekday hours rotate
    between peak/shoulder periods depending on the month group, with night
    hours (0-7) always P6.
    """
    weekday = dt.weekday()  # 0=Mon .. 6=Sun
    if weekday >= 5 or _is_holiday(dt):  # Saturday, Sunday, or holiday
        return "P6"
    h = dt.hour
    if h in _PEAK_HOURS:
        return _MONTH_TO_PERIODS[dt.month][0]
    if h in _SHOULDER_HOURS:
        return _MONTH_TO_PERIODS[dt.month][1]
    return "P6"  # night (0-7)


# -- pricing cache ----------------------------------------------------------

_indexed_tariff_cache = None
_pricing_cache = None

# TTL cache for the full dashboard payload. The underlying InfluxDB scans take
# ~4s; the collector polls every 30s so finer freshness wastes compute. A short
# TTL is also enough to coalesce the duplicate call made by `/` + the initial
# `/api/dashboard` refresh from the same page load.
_DASHBOARD_CACHE_TTL = 30.0
_dashboard_cache_ts = 0.0
_dashboard_cache_value = None


def invalidate_pricing_caches():
    """Clear all pricing caches so the next call re-reads pricing.json."""
    global _indexed_tariff_cache, _pricing_cache, _dashboard_cache_value
    _indexed_tariff_cache = None
    _pricing_cache = None
    _dashboard_cache_value = None


def _load_indexed_tariff():
    """Load indexed tariff components from pricing.json (cached).

    Includes contract formula parameters for the full clause 2b computation.
    Uses top-level energy.contract_formula if present, falls back to scenario.
    """
    global _indexed_tariff_cache
    if _indexed_tariff_cache is None:
        with open(PRICING_PATH) as f:
            data = json.load(f)
        block = data["indexed_tariff"]
        # Prefer top-level contract formula (active contract)
        cf = data.get("energy", {}).get("contract_formula")
        if not cf:
            sc_sidx = data.get("scenarios", {}).get("som_indexada", {})
            cf = sc_sidx.get("contract_formula", {})
        _indexed_tariff_cache = {
            "peajes": block["peajes_eur_kwh"],
            "cargos": block["cargos_eur_kwh"],
            "margin": block["margin_comercialitzadora_eur_kwh"],
            "contract_formula": cf,
        }
    return _indexed_tariff_cache


def _load_pricing():
    """Load full pricing.json (cached)."""
    global _pricing_cache
    if _pricing_cache is None:
        with open(PRICING_PATH) as f:
            _pricing_cache = json.load(f)
    return _pricing_cache


def _get_effective_rate():
    """Return Iberdrola baseline flat rate for comparison."""
    pricing = _load_pricing()
    iber = pricing.get("scenarios", {}).get("iberdrola", {})
    # Use Iberdrola P1 rate as representative flat rate
    rates = iber.get("energy_eur_kwh", {})
    return rates.get("P1", 0.154)


def _get_energy_rates():
    """Return Iberdrola per-period energy rates for baseline comparison."""
    pricing = _load_pricing()
    iber = pricing.get("scenarios", {}).get("iberdrola", {})
    return iber.get("energy_eur_kwh", {f"P{i}": 0.154 for i in range(1, 7)})


def _get_injection_price():
    """Return Iberdrola fixed surplus compensation for baseline."""
    pricing = _load_pricing()
    iber = pricing.get("scenarios", {}).get("iberdrola", {})
    return iber.get("surplus_eur_kwh", 0.05)


def _today_start_iso():
    """Midnight CET today as ISO string for Flux range(start:)."""
    t = _cet_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return t.isoformat()


def _month_start_iso():
    """First day of current month, midnight CET."""
    t = _cet_now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return t.isoformat()


def _q(flux):
    """Execute a Flux query, return list of tables."""
    try:
        return _query_api.query(flux)
    except Exception:
        return []


def _scalar(flux, default=0.0):
    """Run query, return the first _value or *default*."""
    for table in _q(flux):
        for rec in table.records:
            v = rec.get_value()
            if v is not None:
                return float(v)
    return default


def _records_xy(flux, x="_time", y="_value"):
    """Return list of {x, y} dicts for chart series."""
    out = []
    for table in _q(flux):
        for rec in table.records:
            xv = rec.values.get(x)
            yv = rec.values.get(y)
            if xv is not None and yv is not None:
                if hasattr(xv, "isoformat"):
                    xv = xv.isoformat()
                out.append({"x": xv, "y": round(float(yv), 2)})
    return out


# -- shared building blocks --------------------------------------------------

def _generation_kwh(range_start):
    """Total generation (kWh) via integral of ac_power_total for both inverters."""
    bucket = INFLUXDB_BUCKET
    return _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> integral(unit: 1h)
          |> group()
          |> sum()
          |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
    ''')


def _export_kwh(range_start):
    """Total energy exported to grid (kWh) from KSEM counter spread."""
    bucket = INFLUXDB_BUCKET
    return _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> spread()
    ''')


def _import_kwh(range_start):
    """Total energy imported from grid (kWh) from KSEM counter spread."""
    bucket = INFLUXDB_BUCKET
    return _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> spread()
    ''')


# -- public data functions ---------------------------------------------------

def _compute_economia_indexed(range_start):
    """Compute indexed economia for a time range using hourly OMIE prices.

    Returns (import_cost_indexed, savings_indexed, surplus_income,
             import_cost_iber, savings_iber, surplus_iber,
             total_import_kwh, total_export_kwh, total_gen_kwh,
             self_consumption_kwh, consumption_kwh, avg_indexed_rate).
    """
    bucket = INFLUXDB_BUCKET
    tariff = _load_indexed_tariff()
    iber_rates = _get_energy_rates()
    iber_surplus = _get_injection_price()

    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    # Hourly OMIE prices
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    # Hourly import (kWh)
    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Hourly export (kWh)
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Hourly generation (kWh) — mean power × 1h / 1000
    gen_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
    ''')

    # Build dicts keyed by hour
    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                 for t, v in omie_hours}
    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in import_hours}
    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in export_hours}
    gen_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                for t, v in gen_hours}

    all_hours = sorted(set(omie_by_h) | set(import_by_h) | set(export_by_h) | set(gen_by_h))

    import_cost_idx = 0.0
    import_cost_iber = 0.0
    savings_idx = 0.0
    savings_iber = 0.0
    surplus_income = 0.0
    surplus_iber = 0.0
    total_import = 0.0
    total_export = 0.0
    total_gen = 0.0
    total_self_cons = 0.0
    weighted_rate_sum = 0.0

    for hour in all_hours:
        omie_price = omie_by_h.get(hour, 0.0)
        imp_kwh = import_by_h.get(hour, 0.0)
        exp_kwh = export_by_h.get(hour, 0.0)
        gen_kwh = gen_by_h.get(hour, 0.0)
        self_cons_kwh = max(gen_kwh - exp_kwh, 0.0)

        period = _get_period(hour)

        # Full indexed rate: PH = mult × [(OMIE + other) × (1 + losses) + FE + margin] + PTD + CA
        inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
        indexed_rate = cf_mult * inner + peajes[period] + cargos[period]

        # Import cost at indexed rate
        if imp_kwh > 0:
            import_cost_idx += imp_kwh * indexed_rate
            import_cost_iber += imp_kwh * iber_rates.get(period, 0.154)
            total_import += imp_kwh
            weighted_rate_sum += imp_kwh * indexed_rate

        # Self-consumption savings (avoided import at indexed rate)
        if self_cons_kwh > 0:
            savings_idx += self_cons_kwh * indexed_rate
            savings_iber += self_cons_kwh * iber_rates.get(period, 0.154)
            total_self_cons += self_cons_kwh

        # Surplus compensation: raw OMIE price (contract clause 2e)
        if exp_kwh > 0:
            surplus_income += exp_kwh * omie_price
            surplus_iber += exp_kwh * iber_surplus
            total_export += exp_kwh

        total_gen += gen_kwh

    consumption = total_self_cons + total_import
    avg_rate = (weighted_rate_sum / total_import) if total_import > 0 else 0.0

    return (import_cost_idx, savings_idx, surplus_income,
            import_cost_iber, savings_iber, surplus_iber,
            total_import, total_export, total_gen,
            total_self_cons, consumption, avg_rate)


# -- daily cost percentile cache (refreshed every 6 hours) ------------------
_cost_percentile_cache = {"data": None, "fetched": None}


def _get_daily_cost_percentiles():
    """Compute today's cost percentile vs recent similar days.

    Uses a rolling 30-day window and separates weekdays from weekends
    so the comparison adapts to seasons and business patterns.
    Falls back to all day-types if fewer than 5 matching days exist.
    Compares partial costs up to the current hour (apples-to-apples).
    Cached for 1 hour.
    """
    now = _cet_now()
    cached = _cost_percentile_cache
    if cached["data"] and cached["fetched"]:
        age = (now - cached["fetched"]).total_seconds()
        if age < 3600:
            return cached["data"]

    bucket = INFLUXDB_BUCKET
    pricing = _load_pricing()
    tariff = _load_indexed_tariff()

    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    contracted = pricing.get("contracted_power_kw", {})
    pwr_year = pricing.get("power_charges_eur_kw_year", {})
    cal_overall = pricing.get("ksem_calibration", {}).get("overall", 1.0)

    # Query 60 days (enough for 30-day rolling + buffer for gaps)
    range_start = (now - timedelta(days=60)).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')

    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                 for t, v in omie_hours}
    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in import_hours}

    current_hour = now.hour
    today_key = now.strftime("%Y-%m-%d")

    # Aggregate hourly costs into daily buckets (partial = up to current_hour)
    from collections import defaultdict as _dd
    daily_cost_partial = _dd(float)
    daily_hours = _dd(int)
    for hour in sorted(set(omie_by_h) | set(import_by_h)):
        omie_price = omie_by_h.get(hour, 0.0)
        imp_kwh = import_by_h.get(hour, 0.0)
        period = _get_period(hour)
        day_key = hour.strftime("%Y-%m-%d")

        daily_hours[day_key] += 1

        # Only accumulate hours up to current_hour for fair comparison
        if hour.hour >= current_hour:
            continue

        hour_cost = 0.0
        if imp_kwh > 0:
            calibrated_kwh = imp_kwh * cal_overall
            inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
            rate = cf_mult * inner + peajes[period] + cargos[period]
            hour_cost += calibrated_kwh * rate

        cp = contracted.get(period, 0)
        pc = pwr_year.get(period, 0)
        hour_cost += cp * pc / 8760.0

        daily_cost_partial[day_key] += hour_cost

    today_cost = daily_cost_partial.get(today_key)

    # Collect comparable days from the last 30 days (rolling seasonal window).
    # Business operates 7 days/week, so no weekday/weekend split needed.
    cutoff_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    partial_costs = []
    for day in sorted(daily_cost_partial):
        if day == today_key:
            continue
        if day < cutoff_30d:
            continue
        if daily_hours.get(day, 0) < 20:
            continue
        pc = daily_cost_partial[day]
        if pc < 0.50:
            continue
        partial_costs.append(pc)

    if not partial_costs:
        result = {"p25": 0, "p50": 0, "p75": 0, "today_pct": 50,
                  "sample_days": 0}
        _cost_percentile_cache["data"] = result
        _cost_percentile_cache["fetched"] = now
        return result

    partial_sorted = sorted(partial_costs)
    n = len(partial_sorted)

    def _percentile(pct):
        k = (n - 1) * pct / 100
        f = int(k)
        c = f + 1 if f + 1 < n else f
        return partial_sorted[f] + (k - f) * (partial_sorted[c] - partial_sorted[f])

    today_pct = 50
    if today_cost is not None and n > 1:
        below = sum(1 for c in partial_sorted if c < today_cost)
        today_pct = round(100 * below / n)

    result = {
        "p25": round(_percentile(25), 2),
        "p50": round(_percentile(50), 2),
        "p75": round(_percentile(75), 2),
        "today_cost": round(today_cost, 2) if today_cost is not None else None,
        "today_pct": today_pct,
        "sample_days": n,
    }
    _cost_percentile_cache["data"] = result
    _cost_percentile_cache["fetched"] = now
    return result


def _estimate_bill(energy_cost, surplus_value, days, pricing, scenario_power=None):
    """Estimate a full electricity bill (pre-IVA).

    Includes: energy + power charges + rental + bo social + IEE - surplus compensation.
    Surplus compensation is capped at energy cost (can't go negative per regulation).
    """
    iee_pct = pricing["taxes"]["electricity_tax_pct"] / 100
    rental = pricing["fixed_charges_eur_day"]["equipment_rental"] * days
    bono = pricing["fixed_charges_eur_day"]["bono_social"] * days

    # Power charges
    contracted = pricing["contracted_power_kw"]
    power_cost = 0.0
    if scenario_power:
        # Scenario has its own power charges (€/kW/day for Iberdrola/Holaluz)
        sc_contracted = scenario_power.get("contracted_power_kw", contracted)
        for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            kw = sc_contracted.get(p, 69)
            rate_day = scenario_power["charges"].get(p, 0)
            power_cost += kw * rate_day * days
    else:
        # Active contract: power charges in €/kW/year
        pwr_year = pricing.get("power_charges_eur_kw_year", {})
        for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            kw = contracted.get(p, 69)
            power_cost += kw * (pwr_year.get(p, 0) / 365) * days

    # Surplus compensation capped at energy cost (regulation)
    compensated = min(surplus_value, energy_cost)

    # IEE applies to (energy - compensation + power)
    base = energy_cost - compensated + power_cost
    iee = base * iee_pct

    return energy_cost - compensated + power_cost + iee + rental + bono


def get_economia():
    today = _today_start_iso()
    month = _month_start_iso()
    pricing = _load_pricing()
    now = _cet_now()

    # Today — indexed + Iberdrola baseline
    (imp_cost_idx_d, sav_idx_d, surp_d,
     imp_cost_iber_d, sav_iber_d, surp_iber_d,
     imp_kwh_d, exp_kwh_d, gen_kwh_d,
     self_cons_d, cons_d, avg_rate_d) = _compute_economia_indexed(today)

    total_benefit_today = sav_idx_d + surp_d
    total_benefit_iber_today = sav_iber_d + surp_iber_d

    # Month — indexed + Iberdrola baseline
    (imp_cost_idx_m, sav_idx_m, surp_m,
     imp_cost_iber_m, sav_iber_m, surp_iber_m,
     imp_kwh_m, exp_kwh_m, gen_kwh_m,
     self_cons_m, cons_m, avg_rate_m) = _compute_economia_indexed(month)

    monthly_benefit = sav_idx_m + surp_m
    monthly_benefit_iber = sav_iber_m + surp_iber_m

    # --- Cost total avui (matching Consum i Preus methodology) ---
    # Use get_consum_preus_data for accuracy: applies KSEM calibration
    # and prorates power cost only for hours elapsed (not full day).
    _cp_today = get_consum_preus_data("today")
    total_cost_today = _cp_today["summary"]["subtotal_pre_iva"]

    # --- Full bill estimation (month) ---
    days_in_month = now.day  # days elapsed this month
    iber_sc = pricing.get("scenarios", {}).get("iberdrola", {})
    iber_power_info = {
        "charges": iber_sc.get("power_charges_eur_kw_day", {}),
        "contracted_power_kw": iber_sc.get("contracted_power_kw",
                                           pricing["contracted_power_kw"]),
    }

    # Som Indexada bill (energy import + power + rental + bo + IEE - surplus)
    bill_som = _estimate_bill(imp_cost_idx_m, surp_m, days_in_month, pricing)
    # Iberdrola bill (same but with Iber energy costs and surplus)
    bill_iber = _estimate_bill(imp_cost_iber_m, surp_iber_m, days_in_month,
                               pricing, scenario_power=iber_power_info)

    # Daily averages
    if days_in_month > 0:
        bill_som_day = bill_som / days_in_month
        bill_iber_day = bill_iber / days_in_month
    else:
        bill_som_day = 0.0
        bill_iber_day = 0.0

    # Projected full month
    days_total = calendar.monthrange(now.year, now.month)[1]
    bill_som_projected = bill_som_day * days_total
    bill_iber_projected = bill_iber_day * days_total

    # --- All-in cost per kWh (pre-IVA, comparable to invoice) ---
    # Full bill pre-IVA (energy + power + IEE + fixes - compensation) / calibrated kWh
    # No IVA: Binomi Produccions SL deduces VAT.
    cal_data = pricing.get("ksem_calibration", {})
    cal_overall = cal_data.get("overall", 1.0)
    import_kwh_calibrated_m = imp_kwh_m * cal_overall
    if import_kwh_calibrated_m > 0:
        cost_all_in_kwh = round(bill_som / import_kwh_calibrated_m, 4)
    else:
        cost_all_in_kwh = 0.0

    # --- Flux Solar what-if ---
    flux = pricing.get("scenarios", {}).get("som_indexada", {}).get("flux_solar", {})
    flux_enabled = flux.get("enabled", False)
    flux_credit_pct = flux.get("credit_pct", 0.80)
    # Non-compensated surplus: surplus that exceeds energy cost generates a credit
    non_comp = max(surp_m - imp_cost_idx_m, 0.0)
    flux_credit_eur = round(non_comp * flux_credit_pct, 2)
    flux_bill_projected = round(bill_som_projected - flux_credit_eur, 2)

    return {
        # Som Indexada (active contract)
        "savings_today": round(sav_idx_d, 2),
        "self_consumption_kwh_today": round(self_cons_d, 1),
        "injection_income_today": round(surp_d, 2),
        "export_kwh_today": round(exp_kwh_d, 1),
        "total_benefit_today": round(total_benefit_today, 2),
        "monthly_benefit": round(monthly_benefit, 2),
        "cost_all_in_kwh": cost_all_in_kwh,
        "imported_kwh_today": round(imp_kwh_d, 1),
        "consumed_kwh_today": round(cons_d, 1),
        "avg_indexed_rate": round(avg_rate_d, 4),
        "import_cost_today": round(imp_cost_idx_d, 2),
        "import_cost_month": round(imp_cost_idx_m, 2),
        "total_cost_today": round(total_cost_today, 2),
        "cost_percentile": _get_daily_cost_percentiles(),
        # Iberdrola baseline comparison
        "iber_savings_today": round(sav_iber_d, 2),
        "iber_injection_today": round(surp_iber_d, 2),
        "iber_total_benefit_today": round(total_benefit_iber_today, 2),
        "iber_monthly_benefit": round(monthly_benefit_iber, 2),
        "iber_import_cost_today": round(imp_cost_iber_d, 2),
        "iber_import_cost_month": round(imp_cost_iber_m, 2),
        # Difference (positive = Som Indexada is cheaper)
        "diff_today": round(total_benefit_today - total_benefit_iber_today, 2),
        "diff_month": round(monthly_benefit - monthly_benefit_iber, 2),
        "diff_import_today": round(imp_cost_iber_d - imp_cost_idx_d, 2),
        "diff_import_month": round(imp_cost_iber_m - imp_cost_idx_m, 2),
        # Full bill estimation (pre-IVA)
        "bill_som_month": round(bill_som, 2),
        "bill_som_day": round(bill_som_day, 2),
        "bill_som_projected": round(bill_som_projected, 2),
        "bill_iber_month": round(bill_iber, 2),
        "bill_iber_day": round(bill_iber_day, 2),
        "bill_iber_projected": round(bill_iber_projected, 2),
        "bill_diff_projected": round(bill_iber_projected - bill_som_projected, 2),
        "bill_days_elapsed": days_in_month,
        "bill_days_total": days_total,
        # Flux Solar what-if
        "flux_enabled": flux_enabled,
        "flux_credit_pct": flux_credit_pct,
        "flux_credit_eur": flux_credit_eur,
        "flux_bill_projected": flux_bill_projected,
        "flux_saving_vs_no_flux": round(bill_som_projected - flux_bill_projected, 2),
    }


# -- Solar forecast (Open-Meteo) -------------------------------------------

_SOLAR_LAT = 42.12
_SOLAR_LON = 3.13
_SOLAR_KWP = 65.0   # PIKO 15 (15 kWp) + PIKO CI 50 (50 kWp)
_SOLAR_TILT = 30
_SOLAR_AZIMUTH = 0   # 0 = south in Open-Meteo convention

_forecast_cache = {"data": None, "fetched": None}
_FORECAST_CACHE_TTL = timedelta(hours=1)

_log = logging.getLogger(__name__)

# Nameplate AC capacity per inverter tag — used to scale the solar forecast to
# whichever inverters are actually operational (see _online_solar_kwp).
_INVERTER_RATED_W = {
    "piko_15": PIKO_15_RATED_W,
    "piko_ci_50": PIKO_CI_50_RATED_W,
}

# Number of DC string/MPPT inputs per inverter — used by _inverter_strings()
# to surface per-string health on the dashboard. PIKO 15 has 3, CI 50 has 4.
_INVERTER_STRING_COUNT = {
    "piko_15": 3,
    "piko_ci_50": 4,
}


def _classify_string_state(v, w, peak_24h_v, peak_24h_w):
    """Classify a single DC string's state from a recent snapshot + 24h peaks.

    The 24h peaks (voltage AND power) are what separate chronic states from
    transient ones — a healthy string at night reads 0 V / 0 W right now but
    its peaks are still high from earlier daylight, while a truly broken
    string keeps its peaks at zero across the day. Returns
    (state_id, badge_class, label_ca, tooltip_ca).
    """
    # Open input — no voltage ever seen in last 24h: no panels reach this
    # MPPT, or the string is open at the array side.
    if peak_24h_v < 50 and peak_24h_w < 50:
        return ("unconnected", "status-off", "Desconnectada",
                "Sense tensió DC en 24 h. Cap panell connectat a aquesta "
                "entrada, o cablejat obert a l'array.")
    # Voltage seen but never carries current — chronic fault in the current
    # path. This catches the string-3 pattern regardless of time of day.
    if peak_24h_v >= 50 and peak_24h_w < 100:
        return ("fault", "status-error", "Sense corrent",
                "Tensió DC present però 0 A i 0 W de pic en 24 h. "
                "Probable: fusible de string obert/no instal·lat, isolador "
                "DC obert, connector MC4 defectuós, o l'entrada MPPT no "
                "està activada a la configuració de l'inversor.")
    # Currently producing.
    if w >= 50:
        return ("ok", "status-ok", "Produint", "Producció normal")
    # Had a healthy peak in last 24h but is currently low — night, low sun
    # or temporary clouds. Healthy, just not generating right now.
    return ("idle", "status-idle", "Baixa llum",
            "Pic recent normal — actualment baixa irradiància (nit o núvols).")


def _strings_summary(inverters):
    """Aggregate per-string problem counts across inverters for the one-line
    banner shown in the inverters section header.

    Only chronic states ('fault' and 'unconnected') are counted — 'idle' is
    just nighttime/low-light on a healthy string and shouldn't trigger a
    warning. Returns counts, a boolean flag, and a Catalan label ready for
    direct rendering (empty string when everything is fine).
    """
    n_fault = 0
    n_unconn = 0
    for inv in inverters:
        for s in (inv.get("strings") or []):
            if s["state"] == "fault":
                n_fault += 1
            elif s["state"] == "unconnected":
                n_unconn += 1

    parts = []
    if n_fault:
        noun = "string" if n_fault == 1 else "strings"
        parts.append(f"{n_fault} {noun} sense corrent")
    if n_unconn:
        # User-preferred wording: feminine form ("desconnectada/-es") since
        # the discussion treats strings as feminine in Catalan.
        word = "desconnectada" if n_unconn == 1 else "desconnectades"
        # Repeat "string(s)" only if there was no fault clause, to avoid
        # "1 string sense corrent, 1 string desconnectada" verbosity.
        prefix = f"{n_unconn}" if n_fault else f"{n_unconn} string{'s' if n_unconn > 1 else ''}"
        parts.append(f"{prefix} {word}")

    return {
        "faults": n_fault,
        "unconnected": n_unconn,
        "has_problems": bool(parts),
        "label": ("⚠ " + ", ".join(parts)) if parts else "",
    }


def _inverter_strings(tag, n_strings):
    """Return per-string state dicts for inverter `tag` (3–4 strings).

    Fetches last V/A/W + 24h max V/W per string in two batched Flux queries
    (regex on _field), so the cost is constant per inverter regardless of
    string count. Both peaks are needed: peak voltage tells us a string is
    physically connected; peak power tells us current actually flows. A
    chronic fault (V seen, W never) shows peak_V high + peak_W zero.
    """
    bucket = INFLUXDB_BUCKET
    # Last value of every DC-string field in the last 5 min.
    last_vals = {}
    for table in _q(f'''
        from(bucket: "{bucket}")
          |> range(start: -5m)
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "{tag}")
          |> filter(fn: (r) => r._field =~ /^dc_(voltage|current|power)_string[1-{n_strings}]$/)
          |> last()
          |> keep(columns: ["_field", "_value"])
    '''):
        for rec in table.records:
            last_vals[rec.values.get("_field")] = rec.get_value()

    # 24h peak voltage AND power per string — both signals are needed to
    # separate chronic states (connected-but-faulted vs unconnected) from
    # the transient "currently in the dark" state of a healthy string.
    peak_v = {}
    peak_w = {}
    for table in _q(f'''
        from(bucket: "{bucket}")
          |> range(start: -24h)
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "{tag}")
          |> filter(fn: (r) => r._field =~ /^dc_(voltage|power)_string[1-{n_strings}]$/)
          |> max()
          |> keep(columns: ["_field", "_value"])
    '''):
        for rec in table.records:
            field = rec.values.get("_field") or ""
            try:
                idx = int(field[-1])
            except ValueError:
                continue
            target = peak_v if field.startswith("dc_voltage_") else peak_w
            target[idx] = rec.get_value() or 0.0

    out = []
    for i in range(1, n_strings + 1):
        v = float(last_vals.get(f"dc_voltage_string{i}") or 0.0)
        a = float(last_vals.get(f"dc_current_string{i}") or 0.0)
        w = float(last_vals.get(f"dc_power_string{i}") or 0.0)
        pv = float(peak_v.get(i) or 0.0)
        pw = float(peak_w.get(i) or 0.0)
        state, badge_class, label, tooltip = _classify_string_state(v, w, pv, pw)
        out.append({
            "id": i,
            "voltage_v": round(v, 0),
            "current_a": round(a, 2),
            "power_w": round(w, 0),
            "peak_24h_v": round(pv, 0),
            "peak_24h_w": round(pw, 0),
            "state": state,
            "state_class": badge_class,
            "state_label": label,
            "state_tooltip": tooltip,
        })
    return out


def _online_solar_kwp(threshold_w=100.0):
    """Summed kWp of inverters that actually produced power in the last 24h.

    Why: the Open-Meteo forecast is a single irradiance→power scaling by total
    plant kWp. When an inverter is silently offline (e.g. PIKO 15's latched Riso
    fault since 2026-05-16) the full-plant 65 kWp makes the predicted curve sit
    ~40-50% above what the crippled plant can produce — it reads as a permanent
    "disruption" on the dashboard. Counting only inverters that produced in the
    last 24h keeps the forecast comparable to reality, and a repaired inverter
    is folded back in automatically once it produces again.

    A 24h window with max() (not a -5m "now" snapshot) is used so a healthy
    inverter is not mistaken for offline at night when it legitimately reads 0.
    Falls back to full nameplate (_SOLAR_KWP) if the query returns nothing
    (e.g. a fresh start with no daytime data yet) so the forecast is never
    flattened to zero.
    """
    online_w = 0.0
    for table in _q(f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -24h)
          |> filter(fn: (r) => r._measurement == "piko" and r._field == "ac_power_total")
          |> filter(fn: (r) => exists r.inverter)
          |> max()
    '''):
        for rec in table.records:
            tag = rec.values.get("inverter")
            peak = rec.get_value() or 0.0
            if tag in _INVERTER_RATED_W and peak > threshold_w:
                online_w += _INVERTER_RATED_W[tag]
    return online_w / 1000.0 if online_w > 0 else _SOLAR_KWP


# Effective GTI→AC efficiency parameters. The plant doesn't reach the rated
# efficiency at low light: inverters have a part-load efficiency curve, optical
# AOI/cosine losses grow at low sun angles, and Open-Meteo's flat-horizon GTI
# doesn't see local east-side morning shading. We fold all three into one soft
# saturation. Parameters fitted against measured plant data on 2026-05-27
# (both inverters online) — RMSE 0.026 across 6 daylight hours:
#   GTI  31 W/m² → measured 0.25, model 0.26
#   GTI 127 W/m² → measured 0.51, model 0.46
#   GTI 302 W/m² → measured 0.62, model 0.65
#   GTI ≥500 W/m² → 0.80 (rated, unchanged — clear-noon forecast intact)
_RATED_EFFICIENCY = 0.80
_LOW_LIGHT_KNEE = 500.0   # W/m² above which the rated efficiency holds
_LOW_LIGHT_EXP = 0.40     # shape of the sub-knee derating (smaller → steeper rise)


def _conversion_efficiency(irr_w_m2):
    """Effective GTI→AC plant efficiency at a given irradiance.

    Replaces the old constant 0.80 to correct early-morning/late-evening
    over-prediction. Saturates to _RATED_EFFICIENCY above _LOW_LIGHT_KNEE,
    so clear-noon forecasts are unchanged.
    """
    if irr_w_m2 <= 0:
        return 0.0
    return _RATED_EFFICIENCY * min(1.0, irr_w_m2 / _LOW_LIGHT_KNEE) ** _LOW_LIGHT_EXP


def get_solar_forecast():
    """Fetch solar irradiance forecast from Open-Meteo and convert to expected power.

    Returns dict with:
      - forecast_today: [{x: iso_ts, y: watts}, ...] (15-min intervals)
      - forecast_tomorrow: [{x: iso_ts, y: watts}, ...]
      - forecast_day3: [{x: iso_ts, y: watts}, ...]
      - total_today_kwh: predicted total production today
      - total_tomorrow_kwh: predicted total production tomorrow
      - total_day3_kwh: predicted total production day 3
    """
    now = _cet_now()

    # Return cached data if fresh
    if (_forecast_cache["data"] is not None
            and _forecast_cache["fetched"]
            and now - _forecast_cache["fetched"] < _FORECAST_CACHE_TTL):
        return _forecast_cache["data"]

    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": _SOLAR_LAT,
                "longitude": _SOLAR_LON,
                "minutely_15": "global_tilted_irradiance",
                "tilt": _SOLAR_TILT,
                "azimuth": _SOLAR_AZIMUTH,
                "timezone": "Europe/Madrid",
                "forecast_days": 3,
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _log.error("Solar forecast fetch failed: %s", e)
        return _forecast_cache["data"] or _empty_forecast()

    m15 = data.get("minutely_15", {})
    times = m15.get("time", [])
    gti = m15.get("global_tilted_irradiance", [])

    # Convert GTI (W/m²) to expected plant power (W).
    # P = GTI * kWp * eff(GTI) — efficiency is irradiance-dependent (see
    # _conversion_efficiency): rated 0.80 above 500 W/m², derated below to
    # capture inverter part-load curve + AOI + morning horizon shading.

    # Scale to inverters that are actually producing, not full nameplate —
    # otherwise an offline inverter makes the forecast look permanently missed.
    online_kwp = _online_solar_kwp()
    if online_kwp < _SOLAR_KWP:
        # warning (not info) so it surfaces under the default WARNING root level —
        # only fires when an inverter is actually offline, so it is not noise.
        _log.warning("Solar forecast scaled to %.0f kWp of %.0f (inverter(s) offline)",
                     online_kwp, _SOLAR_KWP)

    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    day3_str = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    forecast_today = []
    forecast_tomorrow = []
    forecast_day3 = []
    energy_today = 0.0
    energy_tomorrow = 0.0
    energy_day3 = 0.0

    for t_str, irr in zip(times, gti):
        if irr is None or irr <= 0:
            continue
        # Power in watts: P = GTI/STC * kWp * eff(GTI) * 1000
        power_w = round(irr * online_kwp * _conversion_efficiency(irr), 0)
        # Convert to ISO with timezone for Chart.js
        dt = datetime.fromisoformat(t_str)
        iso = dt.isoformat()
        point = {"x": iso, "y": power_w}

        if t_str.startswith(today_str):
            forecast_today.append(point)
            energy_today += power_w * 0.25 / 1000  # 15-min interval → kWh
        elif t_str.startswith(tomorrow_str):
            forecast_tomorrow.append(point)
            energy_tomorrow += power_w * 0.25 / 1000
        elif t_str.startswith(day3_str):
            forecast_day3.append(point)
            energy_day3 += power_w * 0.25 / 1000

    result = {
        "forecast_today": forecast_today,
        "forecast_tomorrow": forecast_tomorrow,
        "forecast_day3": forecast_day3,
        "total_today_kwh": round(energy_today, 1),
        "total_tomorrow_kwh": round(energy_tomorrow, 1),
        "total_day3_kwh": round(energy_day3, 1),
    }

    _forecast_cache["data"] = result
    _forecast_cache["fetched"] = now
    return result


def _empty_forecast():
    return {
        "forecast_today": [],
        "forecast_tomorrow": [],
        "forecast_day3": [],
        "total_today_kwh": 0.0,
        "total_tomorrow_kwh": 0.0,
        "total_day3_kwh": 0.0,
    }


def get_previsio_solar_data():
    """Return data for the Previsió Solar page.

    Returns dict with:
      - forecast: 3-day forecast (from get_solar_forecast)
      - historic_power: 7-day actual power curve (15-min, watts)
      - daily_actual: actual daily production (kWh) for the last 7 days
      - daily_forecast: estimated forecast for each of the past 7 days (kWh)
      - accuracy_pct: average forecast accuracy over past 7 days
      - omie_3d: OMIE hourly prices for the last 3 days
    """
    bucket = INFLUXDB_BUCKET
    now = _cet_now()

    # 1. 3-day forecast
    forecast = get_solar_forecast()

    # 2. Historical power curve — last 7 days, 15-min, both inverters summed
    historic_power = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: -7d)
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 15m, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
    ''')

    # 3. Daily actual production (kWh) — last 7 days
    daily_actual = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: -7d)
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
          |> map(fn: (r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> filter(fn: (r) => r._value > 0.1)
    ''')

    # 4. Estimate what the forecast would have predicted for each past day
    #    using the seasonal month factor model (since we don't store past forecasts).
    #    We use the current month's factor as a proxy for a "typical" day estimate.
    daily_forecast = []
    accuracy_values = []
    # Scale the "expected" estimate to the operational plant too, so accuracy
    # is not dragged down just because an inverter is offline (see
    # _online_solar_kwp). Past days are approximated with the current online set.
    online_kwp = _online_solar_kwp()
    for pt in daily_actual:
        try:
            day_dt = datetime.fromisoformat(pt["x"])
            month = day_dt.month
            factor = _SOLAR_MONTH_FACTOR.get(month, 1.0)
        except Exception:
            factor = 1.0
        # Expected daily kWh using Mediterranean annual yield ~1500 kWh/kWp
        # daily_avg = 1500 * kWp / 365 * month_factor
        estimated_kwh = round(1500.0 * online_kwp / 365.0 * factor, 1)

        daily_forecast.append({"x": pt["x"], "y": estimated_kwh})
        if estimated_kwh > 0 and pt["y"] > 0:
            ratio = min(pt["y"], estimated_kwh) / max(pt["y"], estimated_kwh)
            accuracy_values.append(ratio * 100.0)

    accuracy_pct = round(sum(accuracy_values) / len(accuracy_values), 1) if accuracy_values else 0.0

    # 5. OMIE prices — last 3 days, hourly
    omie_3d = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: -3d)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    return {
        "forecast": forecast,
        "historic_power": historic_power,
        "daily_actual": daily_actual,
        "daily_forecast": daily_forecast,
        "accuracy_pct": accuracy_pct,
        "omie_3d": omie_3d,
    }


def get_energia():
    bucket = INFLUXDB_BUCKET
    today = _today_start_iso()

    # Current plant power (sum of both inverters)
    plant_power_w = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: -5m)
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> last()
          |> group()
          |> sum()
    ''')

    # Grid flow (positive = importing, negative = exporting)
    grid_flow_w = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: -5m)
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "active_power_total")
          |> last()
    ''')

    # Consumption = generation + grid (derived, not from piko_15)
    consumption_w = plant_power_w + grid_flow_w

    # Today's energy for self-consumption rate and consumption breakdown
    gen_today = _generation_kwh(today)
    export_today = _export_kwh(today)
    import_today = _import_kwh(today)
    self_consumption_today = max(gen_today - export_today, 0.0)
    consumption_today = self_consumption_today + import_today
    if gen_today > 0:
        self_consumption_rate = round((self_consumption_today / gen_today) * 100, 1)
    else:
        self_consumption_rate = 0.0
    # Consumption origin breakdown (% from PV vs grid)
    if consumption_today > 0:
        from_pv_pct = round((self_consumption_today / consumption_today) * 100, 1)
        from_grid_pct = round((import_today / consumption_today) * 100, 1)
    else:
        from_pv_pct = 0.0
        from_grid_pct = 0.0

    # Power curve — generation and grid from DB, consumption computed
    generation = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
    ''')
    grid_curve = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "active_power_total")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')

    # Voltage curves — Piko CI 50 L1/L2/L3 (overvoltage curtailment tracking)
    # PIKO CI 50 reports line-to-line voltages; convert to phase-neutral (÷√3).
    voltage_l1 = [{"x": p["x"], "y": round(p["y"] / 1.732, 1)} for p in _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l1")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')]
    voltage_l2 = [{"x": p["x"], "y": round(p["y"] / 1.732, 1)} for p in _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l2")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')]
    voltage_l3 = [{"x": p["x"], "y": round(p["y"] / 1.732, 1)} for p in _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l3")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')]

    # Curtailment estimate — energy lost when voltage >253V causes derating
    v1 = {p["x"]: p["y"] for p in voltage_l1}
    v2 = {p["x"]: p["y"] for p in voltage_l2}
    v3 = {p["x"]: p["y"] for p in voltage_l3}
    gen_by_t = {p["x"]: p["y"] for p in generation}
    rated_w = PIKO_15_RATED_W + PIKO_CI_50_RATED_W

    curtailed_wh = 0.0
    for t in sorted(set(v1) | set(v2) | set(v3)):
        vmax = max(v1.get(t, 0), v2.get(t, 0), v3.get(t, 0))
        if vmax > 253.0:
            actual_w = gen_by_t.get(t, 0)
            lost_w = max(rated_w - actual_w, 0)
            curtailed_wh += lost_w / 60  # 1-minute window
    curtailment_kwh = round(curtailed_wh / 1000, 2)

    # Compute consumption curve = generation + grid at each minute
    gen_dict = {p["x"]: p["y"] for p in generation}
    grid_dict = {p["x"]: p["y"] for p in grid_curve}
    all_times = sorted(set(gen_dict) | set(grid_dict))
    consumption_curve = [
        {"x": t, "y": round(gen_dict.get(t, 0) + grid_dict.get(t, 0), 2)}
        for t in all_times
    ]

    # Daily yield 30d — use mean power × 24h approximation (reliable when
    # yield_total counters are stuck)
    daily_yield_30d = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
          |> map(fn: (r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> filter(fn: (r) => r._value > 0.1)
    ''')

    return {
        "plant_power_w": round(plant_power_w, 0),
        "consumption_w": round(max(consumption_w, 0), 0),
        "grid_flow_w": round(grid_flow_w, 0),
        "self_consumption_rate": self_consumption_rate,
        "yield_today_kwh": round(gen_today, 1),
        "consumption_today_kwh": round(consumption_today, 1),
        "from_pv_kwh": round(self_consumption_today, 1),
        "from_grid_kwh": round(import_today, 1),
        "from_pv_pct": from_pv_pct,
        "from_grid_pct": from_grid_pct,
        "power_curve": {
            "generation": generation,
            "consumption": consumption_curve,
            "grid": grid_curve,
            "voltage_l1": voltage_l1,
            "voltage_l2": voltage_l2,
            "voltage_l3": voltage_l3,
        },
        "daily_yield_30d": daily_yield_30d,
        "curtailment_kwh": curtailment_kwh,
    }


def _hourly_records(flux):
    """Return list of (datetime_CET, float) tuples from an hourly query."""
    out = []
    for table in _q(flux):
        for rec in table.records:
            t = rec.get_time()
            v = rec.get_value()
            if t is not None and v is not None:
                t_cet = t.astimezone(_CET)
                out.append((t_cet, float(v)))
    return out


def _compute_weighted_costs(omie_hours, import_hours, tariff):
    """Compute hourly-weighted indexed and fixed costs.

    Returns (cost_indexed, cost_fixed, imported_kwh, indexed_hourly_chart).
    Uses per-period energy rates from pricing.json for the fixed cost.
    Uses full contract formula (clause 2b) for indexed cost:
      PH = mult × [(OMIE + other) × (1 + losses) + FE + margin] + PTD + CA
    """
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]
    margin = tariff["margin"]
    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    energy_rates = _get_energy_rates()

    # Build dicts keyed by hour (truncated to hour)
    omie_by_hour = {}
    for t, price in omie_hours:
        key = t.replace(minute=0, second=0, microsecond=0)
        omie_by_hour[key] = price

    import_by_hour = {}
    for t, kwh in import_hours:
        key = t.replace(minute=0, second=0, microsecond=0)
        import_by_hour[key] = kwh

    cost_indexed = 0.0
    cost_fixed = 0.0
    total_import = 0.0
    indexed_hourly = []

    all_hours = sorted(set(omie_by_hour) | set(import_by_hour))
    for hour in all_hours:
        omie_price = omie_by_hour.get(hour, 0.0)
        imp_kwh = import_by_hour.get(hour, 0.0)
        period = _get_period(hour)
        inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
        real_indexed = cf_mult * inner + peajes[period] + cargos[period]

        indexed_hourly.append({
            "x": hour.isoformat(),
            "y": round(real_indexed, 5),
        })

        if imp_kwh > 0:
            cost_indexed += imp_kwh * real_indexed
            cost_fixed += imp_kwh * energy_rates.get(period, 0.154)
            total_import += imp_kwh

    return cost_indexed, cost_fixed, total_import, indexed_hourly


def get_mercat_omie():
    bucket = INFLUXDB_BUCKET
    today = _today_start_iso()
    month = _month_start_iso()
    tariff = _load_indexed_tariff()

    # Current spot price
    omie_eur_mwh = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: -2h)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_mwh")
          |> last()
    ''')

    omie_eur_kwh = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: -2h)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> last()
    ''')

    # Current period and real indexed rate right now (full contract formula)
    now = _cet_now()
    current_period = _get_period(now)
    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    _inner = (omie_eur_kwh + _resolve_cf_other(cf, current_period)) * (1 + cf_losses) + cf_fe + tariff["margin"]
    current_indexed_real = (
        cf_mult * _inner
        + tariff["peajes"][current_period]
        + tariff["cargos"][current_period]
    )

    # --- Today: hourly OMIE prices + hourly import ---
    omie_hours_today = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    import_hours_today = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    cost_idx_today, cost_fix_today, imp_today, indexed_hourly = \
        _compute_weighted_costs(omie_hours_today, import_hours_today, tariff)

    # --- Month: hourly OMIE prices + hourly import ---
    omie_hours_month = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    import_hours_month = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    cost_idx_month, cost_fix_month, _, _ = \
        _compute_weighted_costs(omie_hours_month, import_hours_month, tariff)

    # OMIE average today (for day flag)
    omie_avg_today = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_mwh")
          |> mean()
    ''')

    if omie_avg_today < 20:
        day_flag = "cheap"
    elif omie_avg_today > 80:
        day_flag = "expensive"
    else:
        day_flag = "normal"

    # OMIE hourly bar chart (raw spot prices)
    omie_hourly_chart = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
    ''')

    return {
        "omie_eur_mwh": round(omie_eur_mwh, 2),
        "omie_eur_kwh": round(omie_eur_kwh, 5),
        "current_period": current_period,
        "current_indexed_real": round(current_indexed_real, 5),
        "fixed_rate": _get_effective_rate(),
        # Today — hourly-weighted
        "cost_fixed_today": round(cost_fix_today, 2),
        "cost_indexed_today": round(cost_idx_today, 2),
        "diff_today": round(cost_fix_today - cost_idx_today, 2),
        "imported_kwh_today": round(imp_today, 1),
        # Month — hourly-weighted cumulative
        "cost_fixed_month": round(cost_fix_month, 2),
        "cost_indexed_month": round(cost_idx_month, 2),
        "diff_month": round(cost_fix_month - cost_idx_month, 2),
        # Day flag
        "omie_avg_today": round(omie_avg_today, 1),
        "day_flag": day_flag,
        # Chart data
        "omie_hourly": omie_hourly_chart,
        "indexed_hourly": indexed_hourly,
    }


def get_inversors():
    bucket = INFLUXDB_BUCKET

    def _inv(tag):
        status_val = int(_scalar(f'''
            from(bucket: "{bucket}")
              |> range(start: -5m)
              |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "{tag}")
              |> filter(fn: (r) => r._field == "status")
              |> last()
        '''))
        power = _scalar(f'''
            from(bucket: "{bucket}")
              |> range(start: -5m)
              |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "{tag}")
              |> filter(fn: (r) => r._field == "ac_power_total")
              |> last()
        ''')
        rated = PIKO_15_RATED_W if tag == "piko_15" else PIKO_CI_50_RATED_W
        pct = round((power / rated) * 100, 1) if rated else 0.0

        # Derive status from power when the status field is missing or stuck at 0
        if status_val == 0 and power > 0:
            status_val = 3  # MPP (Producció)

        # AC voltage per phase (from inverter measurement)
        voltages = {}
        for phase in ("l1", "l2", "l3"):
            v = round(_scalar(f'''
                from(bucket: "{bucket}")
                  |> range(start: -5m)
                  |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "{tag}")
                  |> filter(fn: (r) => r._field == "ac_voltage_{phase}")
                  |> last()
            '''), 1)
            # PIKO CI 50 reports line-to-line voltages (~390V);
            # convert to phase-neutral (÷√3) for consistency.
            if tag == "piko_ci_50":
                v = round(v / 1.732, 1)
            voltages[phase] = v

        overvoltage = any(v > 253.0 for v in voltages.values())

        # Per-DC-string health. The classifier uses 24h peak V & W so the
        # chronic states (fault, unconnected) keep showing correctly at
        # night when the inverter is legitimately off.
        strings = _inverter_strings(tag, _INVERTER_STRING_COUNT.get(tag, 0))

        return {
            "status": status_val,
            "text": STATUS_MAP.get(status_val, f"Desconegut ({status_val})"),
            "power_w": round(power, 0),
            "power_pct": pct,
            "voltage_l1": voltages["l1"],
            "voltage_l2": voltages["l2"],
            "voltage_l3": voltages["l3"],
            "overvoltage": overvoltage,
            "strings": strings,
        }

    piko_15 = _inv("piko_15")
    piko_ci_50 = _inv("piko_ci_50")

    frequency = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: -5m)
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "frequency")
          |> last()
    ''')

    phases = {}
    for phase in ("l1", "l2", "l3"):
        voltage = _scalar(f'''
            from(bucket: "{bucket}")
              |> range(start: -5m)
              |> filter(fn: (r) => r._measurement == "ksem")
              |> filter(fn: (r) => r._field == "voltage_{phase}")
              |> last()
        ''')
        current = _scalar(f'''
            from(bucket: "{bucket}")
              |> range(start: -5m)
              |> filter(fn: (r) => r._measurement == "ksem")
              |> filter(fn: (r) => r._field == "current_{phase}")
              |> last()
        ''')
        power = _scalar(f'''
            from(bucket: "{bucket}")
              |> range(start: -5m)
              |> filter(fn: (r) => r._measurement == "ksem")
              |> filter(fn: (r) => r._field == "active_power_{phase}")
              |> last()
        ''')
        phases[phase] = {
            "voltage": round(voltage, 1),
            "current": round(current, 1),
            "power": round(power, 0),
        }

    return {
        "piko_15": piko_15,
        "piko_ci_50": piko_ci_50,
        "frequency": round(frequency, 2),
        "phases": phases,
        "strings_summary": _strings_summary([piko_15, piko_ci_50]),
    }



# -- Seasonal energy model for projections ----------------------------------

# Solar irradiance factors for Catalonia (~41°N latitude).
# Each value is the relative monthly production vs the annual average (1.0).
# Source: PVGIS / typical Mediterranean climate data.
_SOLAR_MONTH_FACTOR = {
    1: 0.55, 2: 0.70, 3: 0.95, 4: 1.10, 5: 1.25, 6: 1.35,
    7: 1.40, 8: 1.30, 9: 1.10, 10: 0.85, 11: 0.60, 12: 0.50,
}


def _historical_daily_profile():
    """Compute average daily energy profile from ALL available data.

    Returns dict with avg daily generation/import/export/consumption in kWh,
    the number of data days, and the weighted seasonal factor of the data
    period (so we can de-seasonalise the averages).

    Returns None if fewer than 2 days of data are available.
    """
    bucket = INFLUXDB_BUCKET

    # Daily generation (both inverters summed)
    gen_daily = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: 0)
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
          |> map(fn: (r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> filter(fn: (r) => r._value > 0.1)
    ''')

    # Daily import / export (hourly spread → daily sum, filtered for sanity)
    imp_daily = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: 0)
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
          |> aggregateWindow(every: 1d, fn: sum, createEmpty: false)
    ''')
    exp_daily = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: 0)
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
          |> aggregateWindow(every: 1d, fn: sum, createEmpty: false)
    ''')

    if not gen_daily or len(gen_daily) < 2:
        return None

    num_days = len(gen_daily)
    avg_gen = sum(p["y"] for p in gen_daily) / num_days
    avg_imp = sum(p["y"] for p in imp_daily) / max(len(imp_daily), 1)
    avg_exp = sum(p["y"] for p in exp_daily) / max(len(exp_daily), 1)
    avg_cons = avg_gen - avg_exp + avg_imp

    # Weighted seasonal factor of the data period — tells us how
    # representative our sample is relative to a full year.
    month_counts = {}
    for p in gen_daily:
        # p["x"] is an ISO string; extract month
        m = int(p["x"][5:7])
        month_counts[m] = month_counts.get(m, 0) + 1
    avg_seasonal = sum(
        _SOLAR_MONTH_FACTOR[m] * c for m, c in month_counts.items()
    ) / num_days

    return {
        "avg_gen": avg_gen,
        "avg_import": avg_imp,
        "avg_export": avg_exp,
        "avg_consumption": max(avg_cons, 0),
        "num_days": num_days,
        "avg_seasonal_factor": avg_seasonal,
    }


def _compute_scenario_costs_month():
    """Query current month's hourly data and compute per-scenario costs.

    Returns dict with per-scenario energy costs, surplus values,
    total import/export, and per-scenario avg rates for projection.
    Returns None if no data available.
    """
    bucket = INFLUXDB_BUCKET
    month = _month_start_iso()

    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    # Build lookup dicts keyed by hour
    def _hk(t):
        return t.replace(minute=0, second=0, microsecond=0)

    import_by_hour = {_hk(t): kwh for t, kwh in import_hours if kwh <= 200}
    export_by_hour = {_hk(t): kwh for t, kwh in export_hours if kwh <= 200}
    omie_by_hour = {_hk(t): p for t, p in omie_hours}

    if not import_by_hour and not export_by_hour:
        return None

    pricing = _load_pricing()
    scenarios = pricing.get("scenarios", {})
    sc_iber = scenarios.get("iberdrola", {})
    sc_hola = scenarios.get("holaluz", {})
    sc_sper = scenarios.get("som_periodes", {})
    sc_sidx = scenarios.get("som_indexada", {})

    iber_rates = sc_iber.get("energy_eur_kwh", {})
    hola_rates = sc_hola.get("energy_eur_kwh", {})
    sper_rates = sc_sper.get("energy_eur_kwh", {})

    iber_surplus_rate = sc_iber.get("surplus_eur_kwh", 0.05)
    hola_surplus_rate = sc_hola.get("surplus_eur_kwh", 0.05)
    sper_surplus_rate = sc_sper.get("surplus_eur_kwh", 0.03)

    # Contract formula params for Som Indexada
    idx_margin = sc_sidx.get("margin_eur_kwh", 0.009680)
    peajes = pricing["indexed_tariff"]["peajes_eur_kwh"]
    cargos = pricing["indexed_tariff"]["cargos_eur_kwh"]
    cf = sc_sidx.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)

    result = {
        "energy_iber": 0.0, "energy_hola": 0.0,
        "energy_sper": 0.0, "energy_sidx": 0.0,
        "surplus_iber": 0.0, "surplus_hola": 0.0,
        "surplus_sper": 0.0, "surplus_sidx": 0.0,
        "total_import": 0.0, "total_export": 0.0,
        "omie_sum": 0.0, "omie_count": 0,
    }

    all_hours = sorted(set(import_by_hour) | set(omie_by_hour))
    for hour in all_hours:
        imp_kwh = import_by_hour.get(hour, 0.0)
        exp_kwh = export_by_hour.get(hour, 0.0)
        omie_price = omie_by_hour.get(hour, None)
        period = _get_period(hour)

        if omie_price is not None:
            result["omie_sum"] += omie_price
            result["omie_count"] += 1

        if imp_kwh > 0 and omie_price is not None:
            result["energy_iber"] += imp_kwh * iber_rates.get(period, 0.153962)
            result["energy_hola"] += imp_kwh * hola_rates.get(period, 0.14)
            result["energy_sper"] += imp_kwh * sper_rates.get(period, 0.13)
            inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + idx_margin
            ph = cf_mult * inner + peajes[period] + cargos[period]
            result["energy_sidx"] += imp_kwh * ph
            result["total_import"] += imp_kwh

        if exp_kwh > 0:
            result["surplus_iber"] += exp_kwh * iber_surplus_rate
            result["surplus_hola"] += exp_kwh * hola_surplus_rate
            result["surplus_sper"] += exp_kwh * sper_surplus_rate
            if omie_price is not None:
                result["surplus_sidx"] += exp_kwh * omie_price
            result["total_export"] += exp_kwh

    # Handle export-only hours not in all_hours
    for hour in sorted(export_by_hour):
        if hour not in set(import_by_hour) and hour not in set(omie_by_hour):
            exp_kwh = export_by_hour[hour]
            result["total_export"] += exp_kwh
            result["surplus_iber"] += exp_kwh * iber_surplus_rate
            result["surplus_hola"] += exp_kwh * hola_surplus_rate
            result["surplus_sper"] += exp_kwh * sper_surplus_rate

    # Compute avg rates for projection
    ti = result["total_import"]
    result["avg_rate_iber"] = result["energy_iber"] / ti if ti > 0 else 0.153962
    result["avg_rate_hola"] = result["energy_hola"] / ti if ti > 0 else 0.14
    result["avg_rate_sper"] = result["energy_sper"] / ti if ti > 0 else 0.13
    result["avg_rate_sidx"] = result["energy_sidx"] / ti if ti > 0 else 0.10
    result["avg_omie"] = result["omie_sum"] / result["omie_count"] if result["omie_count"] > 0 else 0.0

    return result


def _scenario_power_cost(scenario_key, pricing, days):
    """Compute power charges for a scenario over a number of days."""
    scenarios = pricing.get("scenarios", {})
    sc = scenarios.get(scenario_key, {})
    contracted = pricing["contracted_power_kw"]

    pwr_day = sc.get("power_charges_eur_kw_day")
    pwr_year = sc.get("power_charges_eur_kw_year")

    cost = 0.0
    if pwr_day:
        for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            cost += contracted.get(p, 69) * pwr_day.get(p, 0) * days
    elif pwr_year:
        for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            cost += contracted.get(p, 69) * (pwr_year.get(p, 0) / 365) * days
    else:
        # Fallback to top-level power charges
        for p, rate in pricing.get("power_charges_eur_kw_day", {}).items():
            cost += contracted.get(p, 69) * rate * days
    return cost


def _compute_regulation_bill(energy_cost, surplus_value, power_cost, days, pricing):
    """Compute bill following Spanish regulation.

    1. compensated = min(energy_cost, surplus_value)
    2. subtotal = (energy - compensated) + power_cost
    3. IEE = subtotal * 5.11%
    4. fixed_charges = (equipment_rental + bono_social) * days
    5. total = (subtotal + IEE + fixed_charges) * 1.21
    """
    elec_tax_pct = pricing["taxes"]["electricity_tax_pct"] / 100
    iva_pct = pricing["taxes"]["iva_pct"] / 100
    fixed_daily = sum(pricing["fixed_charges_eur_day"].values())
    fixed_charges = fixed_daily * days

    compensated = min(energy_cost, surplus_value)
    subtotal = (energy_cost - compensated) + power_cost
    iee = subtotal * elec_tax_pct
    total = (subtotal + iee + fixed_charges) * (1 + iva_pct)

    return {
        "energia": round(energy_cost, 2),
        "potencia": round(power_cost, 2),
        "imp_electric": round(iee, 2),
        "fixes": round(fixed_charges, 2),
        "iva": round((subtotal + iee + fixed_charges) * iva_pct, 2),
        "compensacio": round(compensated, 2),
        "net": round(total, 2),
    }


def _project_month(month, year, profile, pricing, scenario_rates):
    """Project bill for a single calendar month for all 4 scenarios.

    Uses the historical daily profile adjusted by seasonal solar factor.
    scenario_rates: dict with avg_rate_X and surplus rate per scenario.
    Returns dict of {scenario_key: net_bill}.
    """
    days = calendar.monthrange(year, month)[1]
    factor = _SOLAR_MONTH_FACTOR[month]
    avg_sf = profile["avg_seasonal_factor"]

    daily_gen = profile["avg_gen"] * factor / avg_sf
    daily_cons = profile["avg_consumption"]
    daily_self = min(daily_gen, daily_cons)
    daily_export = max(daily_gen - daily_self, 0)
    daily_import = max(daily_cons - daily_self, 0)

    month_import = daily_import * days
    month_export = daily_export * days

    result = {}
    for key in ["iberdrola", "holaluz", "som_periodes", "som_indexada"]:
        energy = month_import * scenario_rates[f"avg_rate_{key}"]
        surplus_rate = scenario_rates[f"surplus_rate_{key}"]
        surplus = month_export * surplus_rate
        power_cost = _scenario_power_cost(key, pricing, days)
        bill = _compute_regulation_bill(energy, surplus, power_cost, days, pricing)
        result[key] = bill["net"]

    return result


def get_previsio_factura(mercat_data=None):
    """Project full electricity bill (monthly + annual) for 4 scenarios.

    Monthly projection: actual hourly data for elapsed days + seasonal model
    for remaining days (or linear extrapolation as fallback).

    Annual projection: sum of 12 individually projected months using a
    seasonal solar model calibrated from ALL historical data.

    Scenarios: Iberdrola, Holaluz, Som Períodes, Som Indexada.
    """
    pricing = _load_pricing()
    now = _cet_now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = (now - month_start).days + now.hour / 24.0
    remaining_days = days_in_month - days_elapsed
    ratio = days_in_month / max(days_elapsed, 0.5)

    # Actual hourly data for all 4 scenarios
    actual = _compute_scenario_costs_month()

    profile = _historical_daily_profile()

    scenario_keys = ["iberdrola", "holaluz", "som_periodes", "som_indexada"]
    short = {"iberdrola": "iber", "holaluz": "hola",
             "som_periodes": "sper", "som_indexada": "sidx"}

    # Surplus rates for projection
    scenarios_cfg = pricing.get("scenarios", {})
    surplus_rates = {
        "iberdrola": scenarios_cfg.get("iberdrola", {}).get("surplus_eur_kwh", 0.05),
        "holaluz": scenarios_cfg.get("holaluz", {}).get("surplus_eur_kwh", 0.05),
        "som_periodes": scenarios_cfg.get("som_periodes", {}).get("surplus_eur_kwh", 0.03),
        "som_indexada": actual["avg_omie"] if actual and actual["avg_omie"] > 0 else 0.05,
    }

    if actual and profile:
        # --- MONTHLY: actual + seasonal estimate for remaining days ---
        factor = _SOLAR_MONTH_FACTOR[now.month]
        avg_sf = profile["avg_seasonal_factor"]
        daily_gen = profile["avg_gen"] * factor / avg_sf
        daily_cons = profile["avg_consumption"]
        daily_self = min(daily_gen, daily_cons)
        daily_export = max(daily_gen - daily_self, 0)
        daily_import = max(daily_cons - daily_self, 0)

        mensual = {}
        for key in scenario_keys:
            s = short[key]
            avg_rate = actual[f"avg_rate_{s}"]
            energy_projected = actual[f"energy_{s}"] + daily_import * remaining_days * avg_rate
            surplus_projected = actual[f"surplus_{s}"] + daily_export * remaining_days * surplus_rates[key]
            power_cost = _scenario_power_cost(key, pricing, days_in_month)
            mensual[key] = _compute_regulation_bill(
                energy_projected, surplus_projected, power_cost, days_in_month, pricing
            )

        # --- ANNUAL: sum of 12 individually projected months ---
        proj_rates = {}
        for key in scenario_keys:
            s = short[key]
            proj_rates[f"avg_rate_{key}"] = actual[f"avg_rate_{s}"]
            proj_rates[f"surplus_rate_{key}"] = surplus_rates[key]

        anual = {}
        for key in scenario_keys:
            anual[key] = 0.0
        for m in range(1, 13):
            month_bills = _project_month(m, now.year, profile, pricing, proj_rates)
            for key in scenario_keys:
                anual[key] += month_bills[key]
        for key in scenario_keys:
            anual[key] = round(anual[key], 2)

        projection_method = "seasonal"
        hist_days = profile["num_days"]

    elif actual:
        # Fallback: linear extrapolation (insufficient historical data)
        mensual = {}
        for key in scenario_keys:
            s = short[key]
            energy_projected = actual[f"energy_{s}"] * ratio
            surplus_projected = actual[f"surplus_{s}"] * ratio
            power_cost = _scenario_power_cost(key, pricing, days_in_month)
            mensual[key] = _compute_regulation_bill(
                energy_projected, surplus_projected, power_cost, days_in_month, pricing
            )

        anual = {key: round(mensual[key]["net"] * 12, 2) for key in scenario_keys}
        projection_method = "lineal"
        hist_days = 0

    else:
        # No data at all — return zeroes
        mensual = {}
        for key in scenario_keys:
            power_cost = _scenario_power_cost(key, pricing, days_in_month)
            mensual[key] = _compute_regulation_bill(0, 0, power_cost, days_in_month, pricing)
        anual = {key: round(mensual[key]["net"] * 12, 2) for key in scenario_keys}
        projection_method = "lineal"
        hist_days = 0

    # Best alternative vs Iberdrola
    iber_m = mensual["iberdrola"]["net"]
    alt_mensual = {k: mensual[k]["net"] for k in scenario_keys if k != "iberdrola"}
    millor_m = min(alt_mensual, key=alt_mensual.get)
    estalvi_m = round(iber_m - alt_mensual[millor_m], 2)

    iber_a = anual["iberdrola"]
    alt_anual = {k: anual[k] for k in scenario_keys if k != "iberdrola"}
    millor_a = min(alt_anual, key=alt_anual.get)
    estalvi_a = round(iber_a - alt_anual[millor_a], 2)

    return {
        "mensual": mensual,
        "anual": anual,
        "estalvi_mensual": estalvi_m,
        "estalvi_anual": estalvi_a,
        "millor_mensual": millor_m,
        "millor_anual": millor_a,
        "days_elapsed": round(days_elapsed, 1),
        "days_in_month": days_in_month,
        "projection_method": projection_method,
        "hist_days": hist_days,
    }


def get_historic_data(time_range="30d"):
    """Return generation, consumption, import, export, and OMIE data for charting."""
    bucket = INFLUXDB_BUCKET

    range_map = {
        "7d":  ("-7d",  "1h"),
        "30d": ("-30d", "1d"),
        "90d": ("-90d", "1d"),
        "1y":  ("-1y",  "1d"),
        "all": ("0",    "1d"),
    }
    flux_range, window = range_map.get(time_range, ("-30d", "1d"))

    # Hours per window — used to convert mean W to kWh
    hours = 1 if window == "1h" else 24

    # --- Generation (kWh per window) ---
    generation = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
          |> map(fn: (r) => ({{r with _value: r._value * {hours}.0 / 1000.0}}))
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> filter(fn: (r) => r._value > 0.01)
    ''')

    # --- Import / Export (kWh per window) ---
    # Always compute hourly spread first (reliable, avoids counter-jump
    # artifacts), then sum into daily windows when needed.
    _imp_hourly_q = f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    '''
    _exp_hourly_q = f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    '''

    if window == "1h":
        import_kwh = _records_xy(_imp_hourly_q)
        export_kwh = _records_xy(_exp_hourly_q)
    else:
        # Sum hourly spreads into daily buckets
        import_kwh = _records_xy(_imp_hourly_q + f'''
          |> aggregateWindow(every: {window}, fn: sum, createEmpty: false)
        ''')
        export_kwh = _records_xy(_exp_hourly_q + f'''
          |> aggregateWindow(every: {window}, fn: sum, createEmpty: false)
        ''')

    # --- Consumption = generation - export + import (per timestamp) ---
    gen_dict = {p["x"]: p["y"] for p in generation}
    imp_dict = {p["x"]: p["y"] for p in import_kwh}
    exp_dict = {p["x"]: p["y"] for p in export_kwh}
    all_times = sorted(set(gen_dict) | set(imp_dict) | set(exp_dict))
    consumption = [
        {"x": t, "y": round(max(gen_dict.get(t, 0) - exp_dict.get(t, 0) + imp_dict.get(t, 0), 0), 2)}
        for t in all_times
    ]

    # --- OMIE indexed rate (spot + peajes + cargos + margin) per hour ------
    tariff = _load_indexed_tariff()
    omie_hourly_raw = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    # Compute full indexed rate per hour (period-aware, full contract formula)
    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    indexed_hourly = []
    for t_cet, omie_price in omie_hourly_raw:
        period = _get_period(t_cet)
        inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + tariff["margin"]
        full_rate = cf_mult * inner + tariff["peajes"][period] + tariff["cargos"][period]
        indexed_hourly.append((t_cet, full_rate))

    if window == "1h":
        omie_avg = [{"x": t.isoformat(), "y": round(v, 5)}
                    for t, v in indexed_hourly]
    else:
        # Aggregate hourly indexed rates into daily means
        daily = {}
        for t, v in indexed_hourly:
            d = t.date()
            daily.setdefault(d, []).append(v)
        omie_avg = [
            {"x": datetime(d.year, d.month, d.day, tzinfo=_CET).isoformat(),
             "y": round(sum(vals) / len(vals), 5)}
            for d, vals in sorted(daily.items())
        ]

    # --- Cost all-in per window (import_cost / calibrated import kWh) ---
    # Uses import-only denominator (not total consumption) so the rate is
    # comparable to invoice effective rates.
    pricing = _load_pricing()
    cal_overall = pricing.get("ksem_calibration", {}).get("overall", 1.0)

    imp_hourly_raw = _hourly_records(_imp_hourly_q)
    imp_by_hour = {}
    for t, kwh in imp_hourly_raw:
        key = t.replace(minute=0, second=0, microsecond=0)
        imp_by_hour[key] = kwh
    idx_by_hour = {}
    for t, rate in indexed_hourly:
        key = t.replace(minute=0, second=0, microsecond=0)
        idx_by_hour[key] = rate

    # Build per-hour arrays: (datetime, import_cost, calibrated_import_kwh)
    all_hours_sorted = sorted(set(idx_by_hour) | set(imp_by_hour))
    hourly_cost_arr = []
    for h in all_hours_sorted:
        imp_h = imp_by_hour.get(h, 0)
        rate = idx_by_hour.get(h, 0)
        imp_cal = imp_h * cal_overall
        hourly_cost_arr.append((h, imp_h * rate, imp_cal))

    if window == "1h":
        cost_efectiu = []
        for h, ic, imp_cal in hourly_cost_arr:
            if imp_cal > 0.01:
                cost_efectiu.append({"x": h.isoformat(), "y": round(ic / imp_cal, 5)})
    else:
        daily_cost = {}
        daily_imp = {}
        for h, ic, imp_cal in hourly_cost_arr:
            d = h.date()
            daily_cost.setdefault(d, 0.0)
            daily_imp.setdefault(d, 0.0)
            daily_cost[d] += ic
            daily_imp[d] += imp_cal
        cost_efectiu = [
            {"x": datetime(d.year, d.month, d.day, tzinfo=_CET).isoformat(),
             "y": round(daily_cost[d] / daily_imp[d], 5)}
            for d in sorted(daily_cost)
            if daily_imp.get(d, 0) > 0.01
        ]

    # Rolling weighted moving average
    if window == "1h":
        ma_window = 24
        cost_efectiu_ma = []
        for i in range(len(hourly_cost_arr)):
            start = max(0, i - ma_window + 1)
            window_slice = hourly_cost_arr[start:i + 1]
            total_ic = sum(s[1] for s in window_slice)
            total_imp = sum(s[2] for s in window_slice)
            if total_imp > 0.01:
                t = window_slice[-1][0]
                cost_efectiu_ma.append({"x": t.isoformat(), "y": round(total_ic / total_imp, 5)})
    else:
        ma_window = 7
        sorted_days = sorted(daily_cost)
        cost_efectiu_ma = []
        for i in range(len(sorted_days)):
            start = max(0, i - ma_window + 1)
            days_slice = sorted_days[start:i + 1]
            total_ic = sum(daily_cost[d] for d in days_slice)
            total_imp = sum(daily_imp[d] for d in days_slice)
            if total_imp > 0.01:
                d = days_slice[-1]
                cost_efectiu_ma.append({
                    "x": datetime(d.year, d.month, d.day, tzinfo=_CET).isoformat(),
                    "y": round(total_ic / total_imp, 5)
                })

    # --- Cost per kWh importat by hour of day (0-23) ---
    # Weighted average: sum(import_cost) / sum(calibrated_import) per hour-of-day
    hod_cost = [0.0] * 24
    hod_imp = [0.0] * 24
    for h, ic, imp_cal in hourly_cost_arr:
        hod_cost[h.hour] += ic
        hod_imp[h.hour] += imp_cal
    cost_by_hour = [
        round(hod_cost[i] / hod_imp[i], 5) if hod_imp[i] > 0.01 else 0
        for i in range(24)
    ]
    cons_by_hour = [round(hod_imp[i], 2) for i in range(24)]

    # --- Voltage L1/L2/L3 (max per window — useful for overvoltage tracking) ---
    voltage_l1 = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l1")
          |> aggregateWindow(every: {window}, fn: max, createEmpty: false)
    ''')
    voltage_l2 = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l2")
          |> aggregateWindow(every: {window}, fn: max, createEmpty: false)
    ''')
    voltage_l3 = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l3")
          |> aggregateWindow(every: {window}, fn: max, createEmpty: false)
    ''')

    # --- Summary ---
    total_gen = sum(p["y"] for p in generation)
    total_imp = sum(p["y"] for p in import_kwh)
    total_exp = sum(p["y"] for p in export_kwh)
    total_cons = sum(p["y"] for p in consumption)
    total_imp_cost = sum(ic for _, ic, _ in hourly_cost_arr)
    avg_indexed = sum(p["y"] for p in omie_avg) / len(omie_avg) if omie_avg else 0
    self_cons_pct = round(((total_gen - total_exp) / total_gen) * 100, 1) if total_gen > 0 else 0

    return {
        "range": time_range,
        "granularity": window,
        "generation": generation,
        "consumption": consumption,
        "import_kwh": import_kwh,
        "export_kwh": export_kwh,
        "omie_avg": omie_avg,
        "cost_efectiu": cost_efectiu,
        "cost_efectiu_ma": cost_efectiu_ma,
        "cost_by_hour": cost_by_hour,
        "cons_by_hour": cons_by_hour,
        "voltage_l1": voltage_l1,
        "voltage_l2": voltage_l2,
        "voltage_l3": voltage_l3,
        "_fixed_rate": _get_effective_rate(),
        "summary": {
            "total_generation_kwh": round(total_gen, 1),
            "total_consumption_kwh": round(total_cons, 1),
            "total_import_kwh": round(total_imp, 1),
            "total_import_cost": round(total_imp_cost, 2),
            "total_export_kwh": round(total_exp, 1),
            "avg_indexed_eur_kwh": round(avg_indexed, 5),
            "self_consumption_pct": self_cons_pct,
            "days": len(set(t[:10] for t in all_times)) if all_times else 0,
        },
    }


def _get_lost_production():
    """Compute lost production: forecast potential minus actual, today and this month.

    Returns dict with lost_today_kwh, lost_month_kwh, lost_today_eur, lost_month_eur.
    Uses OMIE average price for EUR estimation (more accurate than flat injection price).
    """
    forecast = get_solar_forecast()
    bucket = INFLUXDB_BUCKET
    now = _cet_now()
    today_start = now.strftime("%Y-%m-%dT00:00:00+01:00")
    month_start = now.replace(day=1).strftime("%Y-%m-%dT00:00:00+01:00")

    # Actual generation today
    gen_today = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: {today_start})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
          |> map(fn: (r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
          |> group()
          |> sum()
    ''')

    # Actual generation this month
    gen_month = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: {month_start})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
          |> map(fn: (r) => ({{r with _value: r._value * 24.0 / 1000.0}}))
          |> group()
          |> sum()
    ''')

    # Forecast potential for today
    forecast_today_kwh = forecast.get("total_today_kwh", 0.0)

    # Estimate monthly potential using seasonal model
    month_factor = _SOLAR_MONTH_FACTOR.get(now.month, 1.0)
    days_elapsed = now.day
    daily_potential = 1500.0 * _SOLAR_KWP / 365.0 * month_factor
    forecast_month_kwh = daily_potential * days_elapsed

    lost_today = max(forecast_today_kwh - gen_today, 0.0)
    lost_month = max(forecast_month_kwh - gen_month, 0.0)

    # Average OMIE price this month for EUR valuation
    avg_omie = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: {month_start})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> mean()
    ''')
    if avg_omie <= 0:
        avg_omie = 0.05  # fallback

    return {
        "lost_today_kwh": round(lost_today, 1),
        "lost_month_kwh": round(lost_month, 1),
        "lost_today_eur": round(lost_today * avg_omie, 2),
        "lost_month_eur": round(lost_month * avg_omie, 2),
        "avg_omie_eur_kwh": round(avg_omie, 4),
    }


def get_compensacio():
    """Monthly surplus vs energy cost ratio for compensació simplificada.

    Shows how much surplus is wasted (exceeds the regulatory cap where
    surplus compensation cannot exceed import energy cost).
    """
    month = _month_start_iso()
    (imp_cost_idx, _sav, surplus_income,
     _ic_iber, _s_iber, _surp_iber,
     _imp_kwh, _exp_kwh, _gen_kwh,
     _self_cons, _cons, _avg) = _compute_economia_indexed(month)

    energy_cost = imp_cost_idx
    surplus_raw = surplus_income
    compensated = min(surplus_raw, energy_cost)
    wasted = max(surplus_raw - energy_cost, 0.0)

    if energy_cost > 0:
        ratio_pct = (surplus_raw / energy_cost) * 100
    else:
        ratio_pct = 0.0

    if surplus_raw > 0:
        aprofitament_pct = (compensated / surplus_raw) * 100
    else:
        aprofitament_pct = 100.0

    gauge_pct = min(ratio_pct, 200.0)

    if ratio_pct > 120:
        recommendation = ("Excedent molt superior al cost energètic. "
                          "Considera augmentar autoconsum (càrregues diürnes, bateries) "
                          "o derivar excedent a altres usos.")
    elif ratio_pct > 100:
        recommendation = ("Excedent lleugerament per sobre del límit de compensació. "
                          "Ajusta càrregues a hores solars per maximitzar l'autoconsum.")
    elif ratio_pct > 80:
        recommendation = ("Bon equilibri! Excedent proper al límit però dins de compensació.")
    else:
        recommendation = ("Marge de compensació disponible. L'excedent es compensa íntegrament.")

    return {
        "energy_cost": round(energy_cost, 2),
        "surplus_raw": round(surplus_raw, 2),
        "compensated": round(compensated, 2),
        "wasted": round(wasted, 2),
        "ratio_pct": round(ratio_pct, 1),
        "aprofitament_pct": round(aprofitament_pct, 1),
        "gauge_pct": round(gauge_pct, 1),
        "recommendation": recommendation,
    }


def get_negative_prices():
    """Track hours when OMIE price < 0, show count and financial impact."""
    bucket = INFLUXDB_BUCKET
    today = _today_start_iso()
    month = _month_start_iso()
    now = _cet_now()

    # Current OMIE price
    current_price_mwh = _scalar(f'''
        from(bucket: "{bucket}")
          |> range(start: -2h)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_mwh")
          |> last()
    ''')

    # Today: negative price hours + export during those hours
    omie_today = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    export_today_h = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in export_today_h}

    neg_hours_today = 0
    impact_today = 0.0
    export_kwh_neg_today = 0.0
    for t, price in omie_today:
        if price < 0:
            neg_hours_today += 1
            h = t.replace(minute=0, second=0, microsecond=0)
            exp = export_by_h.get(h, 0.0)
            impact_today += exp * abs(price)
            export_kwh_neg_today += exp

    # Month: negative price hours + export during those hours
    omie_month = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    export_month_h = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    export_by_h_m = {t.replace(minute=0, second=0, microsecond=0): v
                     for t, v in export_month_h}

    neg_hours_month = 0
    impact_month = 0.0
    export_kwh_neg_month = 0.0
    for t, price in omie_month:
        if price < 0:
            neg_hours_month += 1
            h = t.replace(minute=0, second=0, microsecond=0)
            exp = export_by_h_m.get(h, 0.0)
            impact_month += exp * abs(price)
            export_kwh_neg_month += exp

    # Last 30d daily negative hour counts for chart
    omie_30d = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: -30d)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    daily_neg = {}
    for t, price in omie_30d:
        if price < 0:
            day_str = t.strftime("%Y-%m-%d")
            daily_neg[day_str] = daily_neg.get(day_str, 0) + 1

    # Build 30-day series (fill zeros for days with no negative hours)
    daily_neg_30d = []
    for i in range(30, 0, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_neg_30d.append({"x": day, "y": daily_neg.get(day, 0)})

    return {
        "is_negative_now": current_price_mwh < 0,
        "current_price_mwh": round(current_price_mwh, 2),
        "neg_hours_today": neg_hours_today,
        "neg_hours_month": neg_hours_month,
        "impact_today": round(impact_today, 4),
        "impact_month": round(impact_month, 4),
        "export_kwh_neg_today": round(export_kwh_neg_today, 2),
        "export_kwh_neg_month": round(export_kwh_neg_month, 2),
        "daily_neg_30d": daily_neg_30d,
    }


def get_amortitzacio_data():
    """Compute cumulative savings and payback progress since installation."""
    pricing = _load_pricing()
    inv = pricing.get("investment", {})
    inv_cost = inv.get("installation_cost_eur", 50000)
    inv_date_str = inv.get("installation_date", "2026-02-01")
    lifetime_years = inv.get("expected_lifetime_years", 25)

    inv_date = datetime.strptime(inv_date_str, "%Y-%m-%d").replace(tzinfo=_CET)
    now = _cet_now()
    months_elapsed = (now.year - inv_date.year) * 12 + (now.month - inv_date.month)

    bucket = INFLUXDB_BUCKET
    tariff = _load_indexed_tariff()
    iber_rates = _get_energy_rates()
    iber_surplus = _get_injection_price()

    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    # Query all hourly data from installation date
    range_start = inv_date.isoformat()

    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    gen_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
    ''')

    # Build dicts by hour
    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in omie_hours}
    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in import_hours}
    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in export_hours}
    gen_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in gen_hours}

    all_hours = sorted(set(omie_by_h) | set(import_by_h) | set(export_by_h) | set(gen_by_h))

    # Aggregate by month — compute solar benefit:
    #   savings = self-consumption avoided cost + surplus income
    #   (what you'd pay WITHOUT solar minus what you pay WITH solar)
    # Only count hours where we have OMIE price data (otherwise rate is wrong)
    monthly = {}
    total_gen = 0.0
    for hour in all_hours:
        # Skip hours without OMIE data — we can't compute a meaningful rate
        if hour not in omie_by_h:
            gen_kwh = gen_by_h.get(hour, 0.0)
            total_gen += gen_kwh
            continue

        month_key = hour.strftime("%Y-%m")
        if month_key not in monthly:
            monthly[month_key] = {
                "self_cons_savings_idx": 0.0, "surplus_income_idx": 0.0,
                "self_cons_savings_iber": 0.0, "surplus_income_iber": 0.0,
                "hours": 0, "gen_kwh": 0.0,
            }

        omie_price = omie_by_h[hour]
        imp_kwh = import_by_h.get(hour, 0.0)
        exp_kwh = export_by_h.get(hour, 0.0)
        gen_kwh = gen_by_h.get(hour, 0.0)
        self_cons_kwh = max(gen_kwh - exp_kwh, 0.0)
        period = _get_period(hour)

        inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
        indexed_rate = cf_mult * inner + peajes[period] + cargos[period]
        iber_rate = iber_rates.get(period, 0.154)

        # Self-consumption savings: kWh you didn't have to buy from grid
        if self_cons_kwh > 0:
            monthly[month_key]["self_cons_savings_idx"] += self_cons_kwh * indexed_rate
            monthly[month_key]["self_cons_savings_iber"] += self_cons_kwh * iber_rate

        # Surplus income
        if exp_kwh > 0:
            monthly[month_key]["surplus_income_idx"] += exp_kwh * omie_price
            monthly[month_key]["surplus_income_iber"] += exp_kwh * iber_surplus

        monthly[month_key]["hours"] += 1
        monthly[month_key]["gen_kwh"] += gen_kwh
        total_gen += gen_kwh

    # --- 3rd perspective: vs Real Iberdrola invoices ---
    # Real monthly bill with Iberdrola (no solar): from historical invoices
    iber_real_monthly = inv.get("iberdrola_real_monthly_avg_eur", 2067)

    # For each month, compute the actual bill (Som + solar) using _estimate_bill
    # so the saving = what_we_paid_before - what_we_pay_now
    # We need monthly import cost and surplus for _estimate_bill
    monthly_import_cost_idx = {}
    monthly_surplus_idx = {}
    for hour in all_hours:
        if hour not in omie_by_h:
            continue
        month_key = hour.strftime("%Y-%m")
        if month_key not in monthly_import_cost_idx:
            monthly_import_cost_idx[month_key] = 0.0
            monthly_surplus_idx[month_key] = 0.0
        omie_price = omie_by_h[hour]
        imp_kwh = import_by_h.get(hour, 0.0)
        exp_kwh = export_by_h.get(hour, 0.0)
        period = _get_period(hour)
        inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
        indexed_rate = cf_mult * inner + peajes[period] + cargos[period]
        if imp_kwh > 0:
            monthly_import_cost_idx[month_key] += imp_kwh * indexed_rate
        if exp_kwh > 0:
            monthly_surplus_idx[month_key] += exp_kwh * omie_price

    # Build monthly savings series, normalizing partial months
    monthly_savings = []
    cumul_idx = 0.0
    cumul_iber = 0.0
    cumul_real = 0.0
    total_days_actual = 0.0
    for month_key in sorted(monthly.keys()):
        m = monthly[month_key]
        savings_idx = m["self_cons_savings_idx"] + m["surplus_income_idx"]
        savings_iber = m["self_cons_savings_iber"] + m["surplus_income_iber"]

        # Track actual days of data for averaging
        year, mon = int(month_key[:4]), int(month_key[5:7])
        days_in_month = calendar.monthrange(year, mon)[1]
        actual_days = m["hours"] / 24.0  # approximate days of data
        total_days_actual += actual_days

        # 3rd perspective: actual bill (Som + solar) vs real Iberdrola invoices
        # Compute the actual monthly bill using _estimate_bill
        imp_cost = monthly_import_cost_idx.get(month_key, 0.0)
        surp = monthly_surplus_idx.get(month_key, 0.0)
        actual_bill = _estimate_bill(imp_cost, surp, actual_days, pricing)
        # Pro-rate old Iberdrola bill to actual days
        iber_real_prorated = iber_real_monthly * actual_days / days_in_month
        savings_real = iber_real_prorated - actual_bill

        cumul_idx += savings_idx
        cumul_iber += savings_iber
        cumul_real += savings_real
        monthly_savings.append({
            "month": month_key,
            "savings_idx": round(savings_idx, 2),
            "savings_iber": round(savings_iber, 2),
            "savings_real": round(savings_real, 2),
            "actual_bill": round(actual_bill, 2),
            "iber_real_prorated": round(iber_real_prorated, 2),
            "cumulative_idx": round(cumul_idx, 2),
            "cumulative_iber": round(cumul_iber, 2),
            "cumulative_real": round(cumul_real, 2),
            "days_data": round(actual_days, 1),
            "days_in_month": days_in_month,
            "gen_kwh": round(m["gen_kwh"], 1),
        })

    total_savings_idx = cumul_idx
    total_savings_iber = cumul_iber
    total_savings_real = cumul_real

    # Average daily savings: normalize by actual days of data
    if total_days_actual > 0:
        daily_avg_idx = total_savings_idx / total_days_actual
        daily_avg_iber = total_savings_iber / total_days_actual
        daily_avg_real = total_savings_real / total_days_actual
    else:
        daily_avg_idx = 0.0
        daily_avg_iber = 0.0
        daily_avg_real = 0.0

    avg_monthly_idx = daily_avg_idx * 30.44
    avg_monthly_iber = daily_avg_iber * 30.44
    avg_monthly_real = daily_avg_real * 30.44

    payback_pct_idx = (total_savings_idx / inv_cost * 100) if inv_cost > 0 else 0
    payback_pct_iber = (total_savings_iber / inv_cost * 100) if inv_cost > 0 else 0
    payback_pct_real = (total_savings_real / inv_cost * 100) if inv_cost > 0 else 0

    # Projected payback dates
    def _payback_date(daily_avg, total_so_far):
        if daily_avg > 0:
            remaining = inv_cost - total_so_far
            if remaining <= 0:
                return "Amortitzat!"
            return (now + timedelta(days=remaining / daily_avg)).strftime("%Y-%m")
        return "N/A"

    projected_idx = _payback_date(daily_avg_idx, total_savings_idx)
    projected_iber = _payback_date(daily_avg_iber, total_savings_iber)
    projected_real = _payback_date(daily_avg_real, total_savings_real)

    roi_idx = ((total_savings_idx / inv_cost) * 100) if inv_cost > 0 else 0
    roi_iber = ((total_savings_iber / inv_cost) * 100) if inv_cost > 0 else 0

    chart_monthly_idx = [{"x": m["month"], "y": m["savings_idx"]} for m in monthly_savings]
    chart_monthly_iber = [{"x": m["month"], "y": m["savings_iber"]} for m in monthly_savings]
    chart_monthly_real = [{"x": m["month"], "y": m["savings_real"]} for m in monthly_savings]
    chart_cumul_idx = [{"x": m["month"], "y": m["cumulative_idx"]} for m in monthly_savings]
    chart_cumul_iber = [{"x": m["month"], "y": m["cumulative_iber"]} for m in monthly_savings]
    chart_cumul_real = [{"x": m["month"], "y": m["cumulative_real"]} for m in monthly_savings]

    return {
        "investment": {
            "cost": inv_cost,
            "date": inv_date_str,
            "lifetime_years": lifetime_years,
            "months_elapsed": months_elapsed,
        },
        "monthly_savings": monthly_savings,
        # Current indexed tariff perspective
        "total_savings": round(total_savings_idx, 2),
        "payback_pct": round(payback_pct_idx, 2),
        "avg_monthly_savings": round(avg_monthly_idx, 2),
        "projected_payback_date": projected_idx,
        "roi_pct": round(roi_idx, 2),
        "daily_avg_savings": round(daily_avg_idx, 2),
        # vs old Iberdrola fixed contract
        "total_savings_iber": round(total_savings_iber, 2),
        "payback_pct_iber": round(payback_pct_iber, 2),
        "avg_monthly_savings_iber": round(avg_monthly_iber, 2),
        "projected_payback_date_iber": projected_iber,
        "roi_pct_iber": round(roi_iber, 2),
        "daily_avg_savings_iber": round(daily_avg_iber, 2),
        # vs Real Iberdrola invoices (solar + tariff change combined)
        "iber_real_monthly": iber_real_monthly,
        "total_savings_real": round(total_savings_real, 2),
        "payback_pct_real": round(payback_pct_real, 2),
        "avg_monthly_savings_real": round(avg_monthly_real, 2),
        "projected_payback_date_real": projected_real,
        "daily_avg_savings_real": round(daily_avg_real, 2),
        # Shared
        "total_gen_kwh": round(total_gen, 1),
        "chart_monthly_idx": chart_monthly_idx,
        "chart_monthly_iber": chart_monthly_iber,
        "chart_monthly_real": chart_monthly_real,
        "chart_cumulative_idx": chart_cumul_idx,
        "chart_cumulative_iber": chart_cumul_iber,
        "chart_cumulative_real": chart_cumul_real,
    }


def _resolve_cf_other(cf, period):
    """Return the contract-formula 'other costs' (€/kWh) for a tariff period.

    'Other costs' (Pc+Sc+Dsv+GdO+POsOm: profile, balancing, deviation, GdO,
    capacity-payment costs) are genuinely period-dependent. Back-calculation
    from full-month official meter data + invoice rates (Apr+May 2026) showed
    stable per-period values (P4≈0.042, P5≈0.017, P6≈0.034) that a single
    scalar (0.046) over-priced by ~14%. Per-period overrides live in
    pricing.energy.contract_formula.other_costs_eur_kwh_by_period; periods not
    listed (e.g. winter P1/P2/P3, no ground-truth yet) fall back to the scalar
    other_costs_eur_kwh.
    """
    by_p = cf.get("other_costs_eur_kwh_by_period") or {}
    v = by_p.get(period)
    if v is not None:
        return v
    return cf.get("other_costs_eur_kwh", 0.0)


def _iee_reduced_rate_for_period(period_start, period_end, pricing):
    """Return the reduced Art 99.2 IEE rate (€/kWh) if this billing period
    falls entirely within a configured reduced-IEE window, else None.

    Som Energia applied the reduced rate (kWh × 0.001, "aplicant Art 99.2 de la
    Llei 28/2014") on some invoices (e.g. 12-31 Mar and Apr 2026) and the
    standard 5.11269% on others (Feb, early Mar, May 2026), with no predictable
    pattern. Known historical reduced windows are listed in
    pricing.taxes.iee_reduced_periods so those invoices reconstruct accurately
    while the default stays standard for the latest invoice and predictive runs.
    """
    windows = pricing.get("taxes", {}).get("iee_reduced_periods", [])
    for w in windows:
        try:
            ws = datetime.strptime(w["start"], "%d/%m/%Y").date()
            we = datetime.strptime(w["end"], "%d/%m/%Y").date()
        except (KeyError, ValueError):
            continue
        # Containment (not overlap) so a current-month partial range never
        # accidentally triggers a historical reduced rate.
        if ws <= period_start.date() and period_end.date() <= we:
            return w.get("rate_eur_kwh", 0.001)
    return None


def reconstruct_indexed_bill(start_date_str, end_date_str, apply_calibration=True):
    """Reconstruct an indexed bill from InfluxDB data for a date range.

    Args:
        start_date_str: "DD/MM/YYYY" format
        end_date_str: "DD/MM/YYYY" format
        apply_calibration: if True and ksem_calibration exists in pricing.json,
            scale ksem import data by per-period calibration factors derived
            from previous invoice comparisons.

    Returns dict with full bill breakdown matching invoice structure.
    """
    d1 = datetime.strptime(start_date_str, "%d/%m/%Y").replace(tzinfo=_CET)
    d2 = datetime.strptime(end_date_str, "%d/%m/%Y").replace(tzinfo=_CET)
    # Billing period is inclusive ("del X al Y" = X, X+1, ..., Y)
    days = (d2 - d1).days + 1
    # Flux range stop is exclusive, so add 1 day
    d2_exclusive = d2 + timedelta(days=1)

    range_start = d1.isoformat()
    range_stop = d2_exclusive.isoformat()

    bucket = INFLUXDB_BUCKET
    pricing = _load_pricing()
    tariff = _load_indexed_tariff()

    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    # Hourly queries for billing period
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start}, stop: {range_stop})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start}, stop: {range_stop})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start}, stop: {range_stop})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in omie_hours}
    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in import_hours}
    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in export_hours}

    # Try to use official meter data (ground truth) instead of ksem
    official_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start}, stop: {range_stop})
          |> filter(fn: (r) => r._measurement == "official_meter")
          |> filter(fn: (r) => r._field == "import_kwh")
    ''')
    official_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                     for t, v in official_hours}
    has_official = len(official_by_h) > 24  # at least 1 day

    # Load ksem calibration factors. Used for any hour that falls back to
    # KSEM data — including the case where partial official data exists for
    # the period (then official is used for those hours, KSEM-with-calibration
    # for the rest).
    cal_factors = {}
    cal_overall = 1.0
    calibrated = False
    data_source = "ksem"

    if apply_calibration:
        cal_data = pricing.get("ksem_calibration", {})
        if cal_data.get("factors"):
            cal_factors = cal_data["factors"]
            cal_overall = cal_data.get("overall", 1.0)
            calibrated = True

    if has_official:
        data_source = "official_meter"
        calibrated = True  # official data IS calibrated by definition

    all_hours = sorted(set(omie_by_h) | set(import_by_h) | set(export_by_h)
                       | set(official_by_h))

    # Per-period accumulators
    by_period = {}
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        by_period[p] = {"kwh": 0.0, "cost": 0.0, "rate_sum": 0.0, "hours": 0}

    total_import = 0.0
    total_export = 0.0
    total_energy_cost = 0.0
    total_surplus = 0.0

    for hour in all_hours:
        omie_price = omie_by_h.get(hour, 0.0)
        exp_kwh = export_by_h.get(hour, 0.0)
        period = _get_period(hour)

        # Prefer official meter data, fall back to KSEM (with calibration
        # applied per-hour for any hour that doesn't have official data).
        if has_official and hour in official_by_h:
            imp_kwh = official_by_h[hour]
        else:
            imp_kwh = import_by_h.get(hour, 0.0)
            if cal_factors and imp_kwh > 0:
                factor = cal_factors.get(period)
                if factor is not None:
                    imp_kwh *= factor
                else:
                    imp_kwh *= cal_overall

        inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
        indexed_rate = cf_mult * inner + peajes[period] + cargos[period]

        if imp_kwh > 0:
            by_period[period]["kwh"] += imp_kwh
            by_period[period]["cost"] += imp_kwh * indexed_rate
            by_period[period]["rate_sum"] += indexed_rate
            by_period[period]["hours"] += 1
            total_import += imp_kwh
            total_energy_cost += imp_kwh * indexed_rate

        if exp_kwh > 0:
            total_surplus += exp_kwh * omie_price
            total_export += exp_kwh

    # Compute average rate per period
    for p in by_period:
        if by_period[p]["hours"] > 0:
            by_period[p]["avg_rate"] = by_period[p]["rate_sum"] / by_period[p]["hours"]
        else:
            by_period[p]["avg_rate"] = 0.0

    # Power charges
    pwr_year = pricing.get("power_charges_eur_kw_year", {})
    contracted = pricing["contracted_power_kw"]
    power_cost = 0.0
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        kw = contracted.get(p, 69)
        power_cost += kw * (pwr_year.get(p, 0) / 365) * days

    # Compensation (capped at energy cost)
    compensated = min(total_surplus, total_energy_cost)

    # Excess power (Facturació per excés de potència)
    excess_cost, excess_by_period = _compute_excess_power(
        range_start, range_stop, pricing
    )

    # IEE — standard rate is a percentage of the base; some historical billing
    # periods used the reduced Art 99.2 rate (kWh × 0.001). A per-period override
    # list (pricing.taxes.iee_reduced_periods) captures those known windows so
    # they reconstruct accurately, while the global default stays standard for
    # the latest invoice and predictive (no-invoice) reconstructions.
    base = total_energy_cost - compensated + power_cost + excess_cost
    reduced_rate = _iee_reduced_rate_for_period(d1, d2, pricing)
    iee_per_kwh = pricing["taxes"].get("electricity_tax_eur_kwh")  # global override (usually null)
    if reduced_rate is not None:
        iee = total_import * reduced_rate
    elif iee_per_kwh is not None:
        iee = total_import * iee_per_kwh
    else:
        iee = base * (pricing["taxes"]["electricity_tax_pct"] / 100)

    # Fixed charges
    rental = pricing["fixed_charges_eur_day"]["equipment_rental"] * days
    bono = pricing["fixed_charges_eur_day"]["bono_social"] * days
    fixes = rental + bono

    # Subtotal before IVA
    subtotal = base + iee + fixes

    # IVA
    iva_pct = pricing["taxes"]["iva_pct"] / 100
    iva = subtotal * iva_pct

    net = subtotal + iva

    period_breakdown = {}
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        period_breakdown[p] = {
            "kwh": round(by_period[p]["kwh"], 2),
            "avg_rate": round(by_period[p]["avg_rate"], 6),
            "cost": round(by_period[p]["cost"], 2),
        }

    return {
        "energia": round(total_energy_cost, 2),
        "potencia": round(power_cost, 2),
        "exces_potencia": round(excess_cost, 2),
        "exces_by_period": excess_by_period,
        "imp_electric": round(iee, 2),
        "fixes": round(fixes, 2),
        "iva": round(iva, 2),
        "compensacio": round(compensated, 2),
        "net": round(net, 2),
        "by_period": period_breakdown,
        "import_kwh": round(total_import, 2),
        "export_kwh": round(total_export, 2),
        "days": days,
        "calibrated": calibrated,
        "data_source": data_source,
    }


def _compute_excess_power(range_start, range_stop, pricing):
    """Quarter-hourly excess-power billing for 3.0TD.

    Formula (ORDEN IET/107/2014, applied to 3.0TD):
      FEP_p = sum_i (Pm_i - Pc_p)  for all 15-min intervals where Pm_i > Pc_p
      cost_p = FEP_p × Te_p

    KSEM active_power systematically under-reads vs the official maximeter
    (same ~11% gap observed in kWh totals), so we apply the same per-period
    calibration factor used for energy before checking overages.

    Returns (total_excess_cost_eur, {period: {excess_kw, cost_eur}}).
    """
    contracted = pricing.get("contracted_power_kw", {})
    te_prices = pricing.get("excess_power_prices_eur_kw", {})
    if not contracted or not te_prices:
        return 0.0, {p: {"excess_kw": 0.0, "cost_eur": 0.0}
                     for p in ["P1", "P2", "P3", "P4", "P5", "P6"]}

    cal = pricing.get("ksem_calibration", {})
    cal_factors = cal.get("factors", {})
    cal_overall = cal.get("overall", 1.0)

    records = _hourly_records(f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {range_start}, stop: {range_stop})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "active_power_total")
          |> aggregateWindow(every: 15m, fn: mean, createEmpty: false)
    ''')

    result = {p: {"excess_kw": 0.0, "cost_eur": 0.0}
              for p in ["P1", "P2", "P3", "P4", "P5", "P6"]}
    for t_cet, val_w in records:
        if val_w is None or val_w <= 0:
            continue
        period = _get_period(t_cet)
        pc = contracted.get(period, 0.0)
        factor = cal_factors.get(period) or cal_overall
        pm_kw = (val_w / 1000.0) * factor
        if pm_kw > pc:
            result[period]["excess_kw"] += (pm_kw - pc)

    total_cost = 0.0
    for p in result:
        cost = result[p]["excess_kw"] * te_prices.get(p, 0.0)
        result[p]["excess_kw"] = round(result[p]["excess_kw"], 2)
        result[p]["cost_eur"] = round(cost, 2)
        total_cost += cost

    return total_cost, result


def get_maximetre_analysis():
    """Analyse peak 15-min power demand per 3.0TD period vs contracted power.

    Returns per-period max demand, utilisation %, recommended power, and
    potential annual savings from reducing contracted power.
    """
    bucket = INFLUXDB_BUCKET
    pricing = _load_pricing()
    contracted = pricing.get("contracted_power_kw", {})
    pwr_year = pricing.get("power_charges_eur_kw_year", {})
    install_date = pricing.get("investment", {}).get("installation_date", "2026-02-26")

    # Query 15-min average active_power_total (in W) since installation
    records = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {install_date}T00:00:00+01:00)
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "active_power_total")
          |> aggregateWindow(every: 15m, fn: mean, createEmpty: false)
    ''')

    if not records:
        return {
            "data_available": False,
            "periods": {},
            "savings_annual": 0.0,
            "alert": False,
            "recommendation": "No hi ha dades de potencia disponibles.",
        }

    # Find max demand per period (convert W to kW, only positive = import)
    period_max = {f"P{i}": 0.0 for i in range(1, 7)}
    for t_cet, val_w in records:
        if val_w <= 0:
            continue
        period = _get_period(t_cet)
        kw = val_w / 1000.0
        if kw > period_max[period]:
            period_max[period] = kw

    periods_data = {}
    savings_annual = 0.0
    alert = False

    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        contracted_kw = contracted.get(p, 69.0)
        actual_max = period_max[p]
        utilisation = (actual_max / contracted_kw * 100) if contracted_kw > 0 else 0.0
        # Recommended: 10% margin above actual peak, minimum 15 kW (industrial)
        recommended = max(actual_max * 1.10, 15.0)
        # Don't recommend increasing if already sufficient
        if recommended > contracted_kw:
            recommended = contracted_kw

        rate_per_kw_year = pwr_year.get(p, 0.0)
        saving = max(contracted_kw - recommended, 0.0) * rate_per_kw_year
        savings_annual += saving

        if utilisation > 85:
            alert = True

        periods_data[p] = {
            "contracted_kw": round(contracted_kw, 1),
            "actual_max_kw": round(actual_max, 1),
            "utilisation_pct": round(utilisation, 1),
            "recommended_kw": round(recommended, 1),
            "saving_annual": round(saving, 2),
        }

    recommendation = ""
    if savings_annual > 10:
        recommendation = (
            f"Es podria estalviar {savings_annual:.0f} EUR/any reduint la potencia contractada "
            f"als valors recomanats. Consulteu la distribuadora per verificar la viabilitat."
        )
    elif alert:
        recommendation = (
            "Algunes periodes superen el 85% d'utilitzacio. "
            "Vigileu que no se superi la potencia contractada per evitar penalitzacions."
        )
    else:
        recommendation = "La potencia contractada esta ben dimensionada per al consum actual."

    return {
        "data_available": True,
        "periods": periods_data,
        "savings_annual": round(savings_annual, 2),
        "alert": alert,
        "recommendation": recommendation,
    }


def get_reactive_tracking():
    """Track reactive energy and cos phi per 3.0TD period.

    Queries power_factor from KSEM, computes reactive kWh and penalty risk.
    Returns per-period cos_phi, reactive_pct, and estimated penalty.
    """
    bucket = INFLUXDB_BUCKET
    month = _month_start_iso()

    # Check if power_factor data exists
    pf_records = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "power_factor")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    if not pf_records:
        return {
            "data_available": False,
            "periods": {},
            "total_penalty_eur": 0.0,
            "has_penalty_risk": False,
        }

    # Hourly active power (W -> kW, only positive = import)
    ap_records = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "active_power_total")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    pf_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in pf_records}
    ap_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in ap_records}

    # Per-period accumulators
    period_active = {f"P{i}": 0.0 for i in range(1, 7)}
    period_reactive = {f"P{i}": 0.0 for i in range(1, 7)}

    all_hours = sorted(set(pf_by_h) & set(ap_by_h))
    for hour in all_hours:
        pf = pf_by_h[hour]
        ap_w = ap_by_h[hour]
        if ap_w <= 0 or pf <= 0 or pf > 1.0:
            continue

        period = _get_period(hour)
        active_kwh = ap_w / 1000.0  # 1h window, mean W -> kWh
        # reactive = active * tan(acos(pf))
        cos_phi = min(pf, 1.0)
        tan_phi = math.tan(math.acos(cos_phi))
        reactive_kwh = active_kwh * tan_phi

        period_active[period] += active_kwh
        period_reactive[period] += reactive_kwh

    # BOE standard penalty rate for excess reactive
    PENALTY_RATE = 0.041554  # EUR/kVArh

    periods_data = {}
    total_penalty = 0.0
    has_risk = False

    for p in ["P1", "P2", "P3", "P4", "P5"]:  # P6 exempt
        active = period_active[p]
        reactive = period_reactive[p]

        if active > 0:
            reactive_ratio = reactive / active
            cos_phi = math.cos(math.atan(reactive_ratio))
            reactive_pct = reactive_ratio * 100
        else:
            cos_phi = 1.0
            reactive_pct = 0.0
            reactive_ratio = 0.0

        # Penalty threshold: reactive > 33% of active (cos phi < 0.95)
        penalty_risk = reactive_ratio > 0.33
        excess_reactive = max(reactive - active * 0.33, 0.0)
        penalty_eur = excess_reactive * PENALTY_RATE

        if penalty_risk:
            has_risk = True
        total_penalty += penalty_eur

        periods_data[p] = {
            "cos_phi": round(cos_phi, 4),
            "reactive_pct": round(reactive_pct, 1),
            "penalty_risk": penalty_risk,
            "penalty_eur": round(penalty_eur, 2),
            "active_kwh": round(active, 1),
            "reactive_kvarh": round(reactive, 1),
        }

    return {
        "data_available": True,
        "periods": periods_data,
        "total_penalty_eur": round(total_penalty, 2),
        "has_penalty_risk": has_risk,
    }


def _run_battery_sim(capacity_kwh, max_charge_kw, max_discharge_kw, efficiency,
                      all_hours, export_by_h, import_by_h, omie_by_h,
                      tariff, cf_mult, cf_other, cf_losses, cf_fe, margin, peajes, cargos,
                      today_only_soc=False):
    """Run a single battery simulation pass.

    Returns (avoided_import_kwh, avoided_export_kwh, savings, soc_curve).
    soc_curve is only populated for today's hours when today_only_soc=True.
    """
    import math
    # cf_other is also received as a scalar param (back-compat), but per-period
    # 'other costs' are resolved from the contract formula via the tariff dict.
    cf = tariff.get("contract_formula", {})
    eff_sqrt = math.sqrt(efficiency)
    soc = 0.0
    avoided_import_kwh = 0.0
    avoided_export_kwh = 0.0
    savings = 0.0
    soc_curve = []
    now = _cet_now()
    today_date = now.date()

    for hour in all_hours:
        exp_kwh = export_by_h.get(hour, 0.0)
        imp_kwh = import_by_h.get(hour, 0.0)
        omie_price = omie_by_h.get(hour, 0.0)
        period = _get_period(hour)

        # Indexed rate for this hour
        inner = (omie_price + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
        indexed_rate = cf_mult * inner + peajes[period] + cargos[period]

        # Charge from export surplus
        if exp_kwh > 0 and soc < capacity_kwh:
            charge = min(exp_kwh, capacity_kwh - soc, max_charge_kw)
            soc += charge * eff_sqrt
            avoided_export_kwh += charge

        # Discharge to avoid import
        if imp_kwh > 0 and soc > 0:
            discharge = min(imp_kwh, soc, max_discharge_kw)
            delivered = discharge * eff_sqrt
            soc -= discharge
            avoided_import_kwh += delivered
            savings += delivered * indexed_rate

        # Also save from negative price export avoidance
        if exp_kwh > 0 and omie_price < 0:
            # Already charged what we could; savings from not exporting at negative price
            savings += 0  # accounted for by charging instead

        # SOC curve (only for today, for display)
        if today_only_soc and hour.date() == today_date:
            soc_curve.append({
                "x": hour.isoformat(),
                "y": round((soc / capacity_kwh) * 100, 1) if capacity_kwh > 0 else 0
            })

    return avoided_import_kwh, avoided_export_kwh, savings, soc_curve


def get_battery_simulation():
    """Simulate battery storage for the current month.

    Uses hourly import/export/OMIE data and battery config from pricing.json.
    Also runs sizing analysis for several capacities.
    """
    import math
    pricing = _load_pricing()
    bat_cfg = pricing.get("battery_simulation", {})
    capacity_kwh = bat_cfg.get("capacity_kwh", 20)
    max_charge_kw = bat_cfg.get("max_charge_kw", 10)
    max_discharge_kw = bat_cfg.get("max_discharge_kw", 10)
    efficiency = bat_cfg.get("round_trip_efficiency", 0.90)

    bucket = INFLUXDB_BUCKET
    month = _month_start_iso()
    tariff = _load_indexed_tariff()

    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin_val = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    # Hourly OMIE prices
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    # Hourly import
    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Hourly export
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in omie_hours}
    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in import_hours}
    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in export_hours}

    all_hours = sorted(set(omie_by_h) | set(import_by_h) | set(export_by_h))

    # Main simulation with configured capacity
    avoided_imp, avoided_exp, savings, soc_curve = _run_battery_sim(
        capacity_kwh, max_charge_kw, max_discharge_kw, efficiency,
        all_hours, export_by_h, import_by_h, omie_by_h,
        tariff, cf_mult, cf_other, cf_losses, cf_fe, margin_val, peajes, cargos,
        today_only_soc=True
    )

    # Estimate annual savings from monthly
    now = _cet_now()
    days_elapsed = now.day
    days_total = calendar.monthrange(now.year, now.month)[1]
    if days_elapsed > 0:
        savings_annual_est = (savings / days_elapsed) * 365
    else:
        savings_annual_est = 0.0

    # Sizing analysis
    sizing = []
    for cap in [5, 10, 15, 20, 30, 50]:
        s_imp, s_exp, s_sav, _ = _run_battery_sim(
            cap, max_charge_kw, max_discharge_kw, efficiency,
            all_hours, export_by_h, import_by_h, omie_by_h,
            tariff, cf_mult, cf_other, cf_losses, cf_fe, margin_val, peajes, cargos,
            today_only_soc=False
        )
        if days_elapsed > 0:
            annual_est = (s_sav / days_elapsed) * 365
        else:
            annual_est = 0.0
        # Rough cost estimate: 400 EUR/kWh for lithium battery system
        cost_est = cap * 400
        payback = (cost_est / annual_est) if annual_est > 0 else 999
        sizing.append({
            "capacity": cap,
            "savings_month": round(s_sav, 2),
            "savings_annual_est": round(annual_est, 2),
            "cost_est": cost_est,
            "payback_years": round(payback, 1),
        })

    return {
        "config": {
            "capacity_kwh": capacity_kwh,
            "max_charge_kw": max_charge_kw,
            "max_discharge_kw": max_discharge_kw,
            "round_trip_efficiency": efficiency,
        },
        "avoided_import_kwh": round(avoided_imp, 1),
        "avoided_export_kwh": round(avoided_exp, 1),
        "savings_month": round(savings, 2),
        "savings_annual_est": round(savings_annual_est, 2),
        "soc_curve": soc_curve,
        "sizing": sizing,
    }


def get_load_shifting():
    """Analyse load patterns and generate shifting recommendations.

    Queries 30 days of hourly import and generation data, builds 7x24 heatmaps,
    identifies shift potential, and returns recommendations in Catalan.
    """
    bucket = INFLUXDB_BUCKET
    now = _cet_now()
    range_start = (now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00+01:00")

    # Hourly import
    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Hourly generation
    gen_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
    ''')

    # Hourly export
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Hourly OMIE prices
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    # Build 7×24 matrices (counts + sums for averaging)
    import_sum = [[0.0]*24 for _ in range(7)]
    import_cnt = [[0]*24 for _ in range(7)]
    gen_sum = [[0.0]*24 for _ in range(7)]
    gen_cnt = [[0]*24 for _ in range(7)]
    export_sum = [[0.0]*24 for _ in range(7)]

    for t, v in import_hours:
        dow = t.weekday()  # 0=Mon
        h = t.hour
        import_sum[dow][h] += v
        import_cnt[dow][h] += 1

    for t, v in gen_hours:
        dow = t.weekday()
        h = t.hour
        gen_sum[dow][h] += v
        gen_cnt[dow][h] += 1

    for t, v in export_hours:
        dow = t.weekday()
        h = t.hour
        export_sum[dow][h] += v

    # Average matrices
    heatmap_import = [[0.0]*24 for _ in range(7)]
    heatmap_gen = [[0.0]*24 for _ in range(7)]
    for dow in range(7):
        for h in range(24):
            if import_cnt[dow][h] > 0:
                heatmap_import[dow][h] = round(import_sum[dow][h] / import_cnt[dow][h], 2)
            if gen_cnt[dow][h] > 0:
                heatmap_gen[dow][h] = round(gen_sum[dow][h] / gen_cnt[dow][h], 2)

    # Identify peak import hours
    peak_hours = []
    for dow in range(7):
        for h in range(24):
            if heatmap_import[dow][h] > 0:
                peak_hours.append({
                    "dow": dow,
                    "hour": h,
                    "avg_kwh": heatmap_import[dow][h],
                })
    peak_hours.sort(key=lambda x: x["avg_kwh"], reverse=True)
    top_peak = peak_hours[:5]

    # Compute average indexed rate from OMIE data
    tariff = _load_indexed_tariff()
    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin_val = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    # Average import rate weighted by hour
    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in omie_hours}
    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in import_hours}
    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v for t, v in export_hours}

    # Identify "shiftable" kWh: import during solar hours when there's also export
    # Solar hours: 8-17h
    total_shiftable_kwh = 0.0
    total_import_kwh = 0.0
    weighted_rate_total = 0.0

    all_hours_sorted = sorted(set(import_by_h) | set(export_by_h) | set(omie_by_h))
    for hour in all_hours_sorted:
        imp = import_by_h.get(hour, 0.0)
        exp = export_by_h.get(hour, 0.0)
        omie_p = omie_by_h.get(hour, 0.0)
        h = hour.hour
        period = _get_period(hour)

        inner = (omie_p + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin_val
        rate = cf_mult * inner + peajes[period] + cargos[period]

        total_import_kwh += imp
        weighted_rate_total += imp * rate

        # Import during solar hours when excess is available
        if 8 <= h <= 17 and exp > 0 and imp > 0:
            total_shiftable_kwh += min(imp, exp)

    avg_rate = (weighted_rate_total / total_import_kwh) if total_import_kwh > 0 else 0.12

    # Shift scenarios (10%, 20%, 30% of total import shifted to solar hours)
    days_30 = 30
    shift_scenarios = []
    for pct in [10, 20, 30]:
        kwh_shifted = total_import_kwh * (pct / 100)
        # Savings: shifted kWh * (peak rate - solar rate difference, estimated ~20% savings)
        savings = kwh_shifted * avg_rate * 0.20
        shift_scenarios.append({
            "pct": pct,
            "kwh_shifted": round(kwh_shifted, 1),
            "savings_month": round(savings / (days_30 / 30), 2),
        })

    # Generate Catalan recommendations
    recommendations = []

    # Find hours with highest import overlapping with solar generation
    dow_names = ["dilluns", "dimarts", "dimecres", "dijous", "divendres", "dissabte", "diumenge"]
    if top_peak:
        pk = top_peak[0]
        dow_name = dow_names[pk["dow"]]
        recommendations.append(
            f"L'hora de m\u00e0xima importaci\u00f3 \u00e9s {pk['hour']}h els {dow_name} "
            f"amb {pk['avg_kwh']:.1f} kWh de mitjana."
        )

    # Solar hours recommendation
    solar_gen_avg = sum(heatmap_gen[dow][h] for dow in range(5) for h in range(9, 17)) / max(1, 5 * 8)
    if solar_gen_avg > 1:
        recommendations.append(
            f"Concentrar el consum entre les 9h i les 16h pot aprofitar "
            f"{solar_gen_avg:.1f} kWh/h de generaci\u00f3 solar de mitjana."
        )

    if total_shiftable_kwh > 10:
        recommendations.append(
            f"Hi ha {total_shiftable_kwh:.0f} kWh potencialment despla\u00e7ables: "
            f"importaci\u00f3 durant hores amb excedent solar."
        )

    if shift_scenarios:
        best = shift_scenarios[1]  # 20%
        recommendations.append(
            f"Despla\u00e7ar un 20% de la c\u00e0rrega a hores solars pot estalviar "
            f"~{best['savings_month']:.2f} EUR/mes."
        )

    # Weekend vs weekday
    weekday_import = sum(heatmap_import[d][h] for d in range(5) for h in range(24))
    weekend_import = sum(heatmap_import[d][h] for d in range(5, 7) for h in range(24))
    if weekend_import > 0 and weekday_import > 0:
        ratio = weekend_import / weekday_import
        if ratio > 0.5:
            recommendations.append(
                "El consum de cap de setmana \u00e9s significatiu. "
                "Aprofitar la tarifa P6 (diumenge) per a c\u00e0rregues programables."
            )

    return {
        "heatmap_import": heatmap_import,
        "heatmap_gen": heatmap_gen,
        "shift_scenarios": shift_scenarios,
        "peak_import_hours": top_peak,
        "recommendations": recommendations,
        "total_import_kwh_30d": round(total_import_kwh, 1),
        "total_shiftable_kwh_30d": round(total_shiftable_kwh, 1),
        "avg_rate": round(avg_rate, 4),
    }


def get_iberdrola_comparison():
    """Compare real Iberdrola invoices vs projected Som Indexada cost.

    Uses ALL available months from the estadistiques simulator for the
    same-consumption comparison (tariff difference only), plus shows
    real Iberdrola invoices and current Som bill estimate.

    All values are pre-IVA (Binomi is an SL).
    """
    pricing = _load_pricing()
    invoices = pricing.get("iberdrola_invoices", [])
    if not invoices:
        return {"invoices": [], "has_data": False}

    now = _cet_now()
    days_elapsed = now.day
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    iva_pct = pricing["taxes"]["iva_pct"] / 100

    # --- Current month's Som bill (pre-IVA) ---
    month_start = _month_start_iso()
    (imp_cost_idx, sav_idx, surp,
     imp_cost_iber, sav_iber, surp_iber,
     imp_kwh, exp_kwh, gen_kwh,
     self_cons, cons, avg_rate) = _compute_economia_indexed(month_start)

    bill_som = _estimate_bill(imp_cost_idx, surp, days_elapsed, pricing)
    if days_elapsed > 0:
        som_daily = bill_som / days_elapsed
        som_monthly = som_daily * days_in_month
    else:
        som_monthly = 0.0

    # --- Iberdrola invoices (pre-IVA) ---
    full_invoices = [inv for inv in invoices if inv.get("days", 30) > 15]
    total_iber_with_iva = sum(inv["amount_eur"] for inv in full_invoices)
    total_iber = total_iber_with_iva / (1 + iva_pct)
    avg_iber = total_iber / len(full_invoices) if full_invoices else 0

    chart_data = []
    for inv in full_invoices:
        chart_data.append({
            "period": inv["period"],
            "iberdrola": round(inv["amount_eur"] / (1 + iva_pct), 2),
            "days": inv.get("days", 30),
        })

    # --- Multi-month estimate (from estadistiques, same-consumption) ---
    # This is the HONEST comparison: both tariffs on the SAME ksem data
    try:
        from simulator import get_estadistiques_data
        est = get_estadistiques_data("all")
        est_days = est.get("days_data", 0)
        est_annual = est.get("annual", {})
        est_iber = est_annual.get("iberdrola_total", 0)
        est_som_idx = est_annual.get("som_indexada_total", 0)

        if est_days > 7:
            # Annualize from the multi-month data
            multi_som_daily = est_som_idx / est_days
            multi_iber_daily = est_iber / est_days
            multi_som_annual = round(multi_som_daily * 365, 2)
            multi_iber_annual = round(multi_iber_daily * 365, 2)
            multi_saving = round(multi_iber_annual - multi_som_annual, 2)
            multi_saving_pct = round(
                (1 - multi_som_annual / multi_iber_annual) * 100, 1
            ) if multi_iber_annual > 0 else 0
            multi_days = est_days
        else:
            multi_som_annual = None
            multi_iber_annual = None
            multi_saving = None
            multi_saving_pct = None
            multi_days = 0
    except Exception:
        multi_som_annual = None
        multi_iber_annual = None
        multi_saving = None
        multi_saving_pct = None
        multi_days = 0

    # --- Full savings breakdown ---
    # Total saving = what you paid Iberdrola - what you pay now with Som + solar
    # Solar saving = Iberdrola (no solar) - Iberdrola (with solar, same consumption)
    # Tariff saving = Iberdrola (with solar) - Som Indexada (with solar)
    total_saving = None
    solar_saving = None
    tariff_saving = None
    investment = pricing.get("investment", {}).get("installation_cost_eur", 50000)
    payback_years = None

    if multi_som_annual is not None and multi_iber_annual is not None:
        total_saving = round(total_iber - multi_som_annual, 2)
        solar_saving = round(total_iber - multi_iber_annual, 2)
        tariff_saving = round(multi_iber_annual - multi_som_annual, 2)
        if total_saving > 0:
            payback_years = round(investment / total_saving, 1)

    return {
        "has_data": True,
        "invoices": chart_data,
        # Current month projection
        "som_current_month": round(som_monthly, 2),
        "som_current_month_label": now.strftime("%Y-%m"),
        # Historical Iberdrola (pre-IVA)
        "total_iberdrola_12m": round(total_iber, 2),
        "avg_iberdrola": round(avg_iber, 2),
        # Multi-month same-consumption comparison
        "multi_som_annual": multi_som_annual,
        "multi_iber_annual": multi_iber_annual,
        "multi_saving": multi_saving,
        "multi_saving_pct": multi_saving_pct,
        "multi_days": multi_days,
        # Full breakdown: total = solar + tariff
        "total_saving": total_saving,
        "solar_saving": solar_saving,
        "tariff_saving": tariff_saving,
        "payback_years": payback_years,
        "investment": investment,
        # Legacy single-month projection
        "som_annual_projected": round(som_monthly * 12, 2),
        "annual_savings": round(total_iber - som_monthly * 12, 2),
        "monthly_savings": round(avg_iber - som_monthly, 2),
        "savings_pct": round((1 - som_monthly * 12 / total_iber) * 100, 1) if total_iber > 0 else 0,
    }


def _solar_potential_kw(hour, month, rated_kw=65):
    """Estimate clear-sky solar potential (kW) for a plant in Catalunya.

    Uses a Gaussian model centered on solar noon (13:30 CET in Spain due to
    timezone offset). The monthly peak factor accounts for seasonal variation
    in day length and solar elevation angle.
    """
    # Solar noon is ~13:30 CET / ~14:30 CEST (Spain is ~1.5h ahead of solar time)
    is_cest = bool(_cet_now().dst())
    solar_noon = 14.5 if is_cest else 13.5
    # Monthly clear-sky peak capacity factors for a 65 kWp plant (Catalunya)
    monthly_peak = {
        1: 0.45, 2: 0.55, 3: 0.65, 4: 0.75, 5: 0.82, 6: 0.85,
        7: 0.85, 8: 0.80, 9: 0.72, 10: 0.60, 11: 0.48, 12: 0.42,
    }
    # Gaussian width widens in summer (longer effective production window)
    monthly_sigma = {
        1: 2.5, 2: 2.8, 3: 3.0, 4: 3.3, 5: 3.5, 6: 3.7,
        7: 3.7, 8: 3.5, 9: 3.3, 10: 3.0, 11: 2.8, 12: 2.5,
    }
    peak = monthly_peak.get(month, 0.65)
    sigma = monthly_sigma.get(month, 3.0)
    hour_center = hour + 0.5  # center of the hour window
    offset = hour_center - solar_noon
    factor = peak * math.exp(-0.5 * (offset / sigma) ** 2)
    return rated_kw * factor


def get_ev_solar_data():
    """Compute EV solar charging + V2H + aerotermia simulation.

    IMPORTANT: The PV plant is unlegalised and throttled — it only produces
    what the company consumes. With the EV plugged in, the plant would see
    11 kW more demand and ramp up accordingly. So we model the plant's
    POTENTIAL production (from solar irradiance) minus company consumption,
    not just the tiny export data.

    Energy flow: solar potential → EV battery → V2H → home battery → split:
      1. Home electrical loads (lights, appliances overnight)
      2. Aerotermia (heating in winter / cooling in summer, COP multiplied)
    """
    pricing = _load_pricing()
    ev = pricing.get("ev_config", {})
    if not ev.get("enabled", False):
        return {"enabled": False}

    charger_kw = ev.get("charger_kw", 11)
    v2h_kw = ev.get("v2h_kw", 11.5)
    ev_efficiency = ev.get("efficiency", 0.88)
    ev_battery_kwh = ev.get("battery_kwh", 96)
    driving_kwh = ev.get("daily_driving_kwh", 1.3)
    home_rate = ev.get("home_rate_eur_kwh", 0.17)
    charger_cost = ev.get("charger_cost_eur", 5000)
    arrive_h = int(ev.get("schedule_arrive", "08:00").split(":")[0])
    depart_h = int(ev.get("schedule_depart", "18:00").split(":")[0])
    sat_ok = ev.get("saturday", True)
    sun_ok = ev.get("sunday", False)

    # Aerotermia + home battery params
    home_bat_kwh = ev.get("home_battery_kwh", 20)
    home_bat_eff = ev.get("home_battery_efficiency", 0.90)
    cop_heating = ev.get("aerotermia_cop_heating", 3.0)
    cop_cooling = ev.get("aerotermia_cop_cooling", 2.5)
    aerotermia_kw = ev.get("aerotermia_kw", 5)
    home_elec_night = ev.get("home_electrical_kwh_night", 5)
    heating_months = ev.get("heating_months", [1, 2, 3, 4, 10, 11, 12])
    cooling_months = ev.get("cooling_months", [6, 7, 8, 9])

    rated_kw = (PIKO_15_RATED_W + PIKO_CI_50_RATED_W) / 1000  # 65 kW

    now = _cet_now()
    cur_month = now.month
    bucket = INFLUXDB_BUCKET
    month_start = _month_start_iso()

    # Determine current season
    if cur_month in heating_months:
        season = "heating"
        cop = cop_heating
    elif cur_month in cooling_months:
        season = "cooling"
        cop = cop_cooling
    else:
        season = "transition"
        cop = 0

    # Combined efficiency: EV round-trip × home battery round-trip
    chain_efficiency = ev_efficiency * home_bat_eff

    # Hourly generation (actual, throttled — mean W per hour → kWh)
    gen_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month_start})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
    ''')

    # Hourly import (kWh) — tells us company load that exceeds PV
    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Hourly export (kWh) — small due to throttling, but still relevant
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Hourly OMIE prices
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {month_start})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    gen_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                for t, v in gen_hours}
    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in import_hours}
    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in export_hours}
    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                 for t, v in omie_hours}

    def _is_work_hour(dt):
        wd = dt.weekday()
        h = dt.hour
        if wd == 6 and not sun_ok:
            return False
        if wd == 5 and not sat_ok:
            return False
        if wd < 5 or (wd == 5 and sat_ok) or (wd == 6 and sun_ok):
            return arrive_h <= h < depart_h
        return False

    # Build set of all hours with generation data
    from collections import defaultdict
    daily_data = defaultdict(lambda: {"charged": 0.0, "lost_comp": 0.0,
                                      "hourly": [], "month": cur_month})
    today_str = now.strftime("%Y-%m-%d")

    all_hours = sorted(set(gen_by_h.keys()) | set(import_by_h.keys()))
    for hour in all_hours:
        if not _is_work_hour(hour):
            continue

        h = hour.hour
        m = hour.month
        day_str = hour.strftime("%Y-%m-%d")

        gen_kwh = gen_by_h.get(hour, 0.0)
        imp_kwh = max(import_by_h.get(hour, 0.0), 0.0)
        exp_kwh = max(export_by_h.get(hour, 0.0), 0.0)
        omie_price = max(omie_by_h.get(hour, 0.0), 0.0)

        # Company load = what was actually consumed (generation + import - export)
        company_load = gen_kwh + imp_kwh - exp_kwh

        # Estimate solar potential for this hour
        clear_sky = _solar_potential_kw(h, m, rated_kw)

        # Determine if plant was throttled or at irradiance-limited max
        # If importing significantly (imp > 1 kWh), plant is at max → use actual gen
        # If grid near zero (throttled), potential is the clear-sky model
        if imp_kwh > 1.0:
            # Plant at max for current conditions; use actual gen as potential
            # Apply cloud factor: actual/clear_sky
            solar_potential = gen_kwh
        elif imp_kwh < 0.5 and exp_kwh < 0.5:
            # Plant throttled — grid ≈ 0, generation = load
            # Real potential is much higher; use clear-sky model
            # But discount by weather: use gen from nearby importing hours
            # as a conservative fallback, assume at least clear_sky * 0.7
            solar_potential = max(clear_sky, gen_kwh)
        else:
            # Small import/export — plant near balance
            solar_potential = max(gen_kwh, clear_sky * 0.8)

        # EV can charge from solar headroom (potential above company load)
        headroom = max(solar_potential - company_load, 0.0)
        # Also add any current export (plant already overproducing)
        headroom += exp_kwh
        charged = min(headroom, charger_kw)

        # Lost compensation: what we'd have earned exporting these kWh at OMIE
        # Only applies to kWh that WERE being exported (exp_kwh)
        # The unlocked headroom from throttled plant earns nothing currently
        lost = min(charged, exp_kwh) * omie_price

        daily_data[day_str]["charged"] += charged
        daily_data[day_str]["lost_comp"] += lost
        daily_data[day_str]["month"] = m
        if day_str == today_str:
            daily_data[day_str]["hourly"].append({
                "x": hour.strftime("%H:%M"),
                "surplus": round(headroom + exp_kwh, 2),
                "charged": round(charged, 2),
                "potential": round(solar_potential, 1),
                "load": round(company_load, 1),
            })

    def _day_energy_split(charged_kwh, day_month):
        """Split V2H energy between electrical loads and aerotermia."""
        # Cap charged by EV battery capacity
        capped = min(charged_kwh, ev_battery_kwh)
        available = max((capped - driving_kwh) * chain_efficiency, 0.0)
        available = min(available, home_bat_kwh)

        elec = min(available, home_elec_night)
        remaining = available - elec
        elec_value = elec * home_rate

        if day_month in heating_months:
            day_cop = cop_heating
        elif day_month in cooling_months:
            day_cop = cop_cooling
        else:
            day_cop = 0

        if day_cop > 0 and remaining > 0:
            max_aero_kwh = aerotermia_kw * 10
            aero_elec = min(remaining, max_aero_kwh)
            thermal = aero_elec * day_cop
            aero_value = aero_elec * home_rate
        else:
            aero_elec = 0.0
            thermal = 0.0
            aero_value = 0.0

        return elec, aero_elec, thermal, elec_value + aero_value

    # Today's data
    td = daily_data.get(today_str, {"charged": 0, "lost_comp": 0, "hourly": [],
                                    "month": cur_month})
    today_charged = td["charged"]
    t_elec, t_aero_elec, t_thermal, t_value = _day_energy_split(today_charged, td["month"])
    today_v2h = t_elec + t_aero_elec
    today_lost = td["lost_comp"]
    today_net = t_value - today_lost

    # Monthly totals
    month_charged = 0.0
    month_v2h = 0.0
    month_elec = 0.0
    month_aero_elec = 0.0
    month_thermal = 0.0
    month_savings = 0.0
    month_lost = 0.0
    days_count = 0
    daily_chart = []

    for day_str in sorted(daily_data.keys()):
        dd = daily_data[day_str]
        ch = dd["charged"]
        elec, aero_elec, thermal, value = _day_energy_split(ch, dd["month"])
        v2h = elec + aero_elec
        lost = dd["lost_comp"]
        net = value - lost

        month_charged += ch
        month_v2h += v2h
        month_elec += elec
        month_aero_elec += aero_elec
        month_thermal += thermal
        month_savings += value
        month_lost += lost
        days_count += 1

        daily_chart.append({
            "x": day_str,
            "solar": round(ch, 2),
            "v2h": round(v2h, 2),
            "elec": round(elec, 2),
            "aero": round(aero_elec, 2),
            "thermal": round(thermal, 2),
            "net": round(net, 2),
        })

    month_net = month_savings - month_lost

    # Projection (annualized from current month data)
    days_elapsed = now.day
    if days_elapsed > 0 and days_count > 0:
        daily_avg_savings = month_savings / days_elapsed
        daily_avg_net = month_net / days_elapsed
        daily_avg_v2h = month_v2h / days_elapsed
        daily_avg_thermal = month_thermal / days_elapsed
        daily_avg_charged = month_charged / days_elapsed
        annual_savings = daily_avg_savings * 365
        annual_net = daily_avg_net * 365
        payback_months = (charger_cost / (daily_avg_net * 30.44)) if daily_avg_net > 0 else 999
        home_coverage_pct = min(round((daily_avg_v2h / (home_elec_night + aerotermia_kw * 6)) * 100, 1), 100.0)
        if aerotermia_kw > 0 and days_count > 0:
            avg_aero_elec = month_aero_elec / days_count
            hvac_hours_night = avg_aero_elec / aerotermia_kw
        else:
            hvac_hours_night = 0.0
    else:
        annual_savings = 0.0
        annual_net = 0.0
        payback_months = 999
        home_coverage_pct = 0.0
        daily_avg_thermal = 0.0
        daily_avg_charged = 0.0
        hvac_hours_night = 0.0

    # Season labels in Catalan
    season_labels = {"heating": "Calefacci\u00f3", "cooling": "Refrigeraci\u00f3",
                     "transition": "Transici\u00f3"}
    season_label = season_labels[season]

    # Recommendation in Catalan
    if month_net > 0:
        if season == "heating":
            hvac_text = (f" D'aquests, {month_aero_elec:.0f} kWh alimenten l'aerot\u00e8rmia "
                         f"produint {month_thermal:.0f} kWh t\u00e8rmics de calefacci\u00f3 "
                         f"({hvac_hours_night:.1f}h/nit de terra radiant).")
        elif season == "cooling":
            hvac_text = (f" D'aquests, {month_aero_elec:.0f} kWh alimenten l'aerot\u00e8rmia "
                         f"produint {month_thermal:.0f} kWh de refrigeraci\u00f3 "
                         f"({hvac_hours_night:.1f}h/nit d'aire condicionat).")
        else:
            hvac_text = ""
        rec = (f"La planta solar (65 kWp) t\u00e9 capacitat sobrant per carregar el vehicle. "
               f"Estimem {daily_avg_charged:.0f} kWh/dia de c\u00e0rrega solar, "
               f"subministrant {month_v2h / max(days_count, 1):.0f} kWh/dia a la llar "
               f"via V2H + bateria.{hvac_text} "
               f"Benefici net: {month_net:.2f} \u20ac/mes ({annual_net:.0f} \u20ac/any projectat). "
               f"Payback Quasar 2: {payback_months:.1f} mesos.")
    else:
        rec = ("L'excedent solar \u00e9s insuficient per generar benefici net. "
               "Considera optimitzar l'horari de c\u00e0rrega a les hores de m\u00e0xim sol.")

    return {
        "enabled": True,
        "model": ev.get("model", "EV"),
        "season": season,
        "season_label": season_label,
        "cop": cop,
        "today": {
            "solar_charge_kwh": round(today_charged, 2),
            "available_v2h_kwh": round(today_v2h, 2),
            "elec_kwh": round(t_elec, 2),
            "aero_elec_kwh": round(t_aero_elec, 2),
            "thermal_kwh": round(t_thermal, 2),
            "home_value_eur": round(t_value, 2),
            "lost_compensation_eur": round(today_lost, 2),
            "net_benefit_eur": round(today_net, 2),
            "hourly_chart": td["hourly"],
        },
        "month": {
            "solar_charge_kwh": round(month_charged, 1),
            "v2h_kwh": round(month_v2h, 1),
            "elec_kwh": round(month_elec, 1),
            "aero_elec_kwh": round(month_aero_elec, 1),
            "thermal_kwh": round(month_thermal, 1),
            "home_savings_eur": round(month_savings, 2),
            "lost_compensation_eur": round(month_lost, 2),
            "net_benefit_eur": round(month_net, 2),
            "days": days_count,
        },
        "projection": {
            "annual_home_savings_eur": round(annual_savings, 2),
            "annual_net_benefit_eur": round(annual_net, 2),
            "charger_payback_months": round(payback_months, 1),
            "home_coverage_pct": home_coverage_pct,
            "avg_thermal_kwh_day": round(daily_avg_thermal, 1),
            "hvac_hours_night": round(hvac_hours_night, 1),
        },
        "daily_chart": daily_chart,
        "recommendation": rec,
    }


def _compute_bill_forecast(energy_cost_today, compensation_today,
                            daily_power_cost, pricing, iee_pct, days_elapsed):
    """Predict the current month's full bill (pre-IVA).

    Method:
    - Fixed costs (potència, lloguer, bo social): exact for the full month.
    - Variable cost (net energy = energy - compensation):
      * Days elapsed: use actual data.
      * Remaining days: EWMA of historical daily net energy cost.
        EWMA half-life = 14 days → recent data weighs more but all history
        contributes. This adapts to seasonal changes (solar ramp-up) and
        price trends without overfitting to a short noisy window.
    - IEE applied to the projected base.
    """
    import math
    now = _cet_now()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = now.day  # actual calendar days elapsed (ignore the passed param)
    days_remaining = days_in_month - days_elapsed

    # --- Fixed costs for full month (perfectly predictable) ---
    rental_day = pricing.get("fixed_charges_eur_day", {}).get("equipment_rental", 0)
    bono_day = pricing.get("fixed_charges_eur_day", {}).get("bono_social", 0)
    fixed_month = (daily_power_cost + rental_day + bono_day) * days_in_month

    # --- Variable cost: actual so far ---
    net_energy_today = energy_cost_today - compensation_today  # today only
    # We need the month-to-date net energy cost (not just today).
    # Query the current month's daily net energy costs from consum_preus.
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    bucket = INFLUXDB_BUCKET
    tariff = _load_indexed_tariff()
    cal_overall = pricing.get("ksem_calibration", {}).get("overall", 1.0)

    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin_t = tariff["margin"]
    peajes_t = tariff["peajes"]
    cargos_t = tariff["cargos"]

    # Query all hourly data from 90 days ago to compute EWMA
    range_start = (now - timedelta(days=90)).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()

    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {range_start})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    import_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in import_hours}
    export_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                   for t, v in export_hours}
    omie_by_h = {t.replace(minute=0, second=0, microsecond=0): v
                 for t, v in omie_hours}

    # Compute daily net energy cost (energy_import_cost - surplus_compensation)
    from collections import defaultdict as _dd
    daily_net = _dd(lambda: {"energy_cost": 0.0, "surplus": 0.0, "hours": 0})
    for hour in sorted(set(import_by_h) | set(export_by_h) | set(omie_by_h)):
        imp_kwh = import_by_h.get(hour, 0.0)
        exp_kwh = export_by_h.get(hour, 0.0)
        omie = omie_by_h.get(hour, 0.0)
        period = _get_period(hour)
        day_key = hour.strftime("%Y-%m-%d")

        daily_net[day_key]["hours"] += 1

        if imp_kwh > 0:
            cal_kwh = imp_kwh * cal_overall
            inner = (omie + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin_t
            rate = cf_mult * inner + peajes_t.get(period, 0) + cargos_t.get(period, 0)
            daily_net[day_key]["energy_cost"] += cal_kwh * rate

        if exp_kwh > 0:
            daily_net[day_key]["surplus"] += exp_kwh * max(omie, 0)

    # Build list of (date_str, net_energy_cost) for complete days
    today_key = now.strftime("%Y-%m-%d")
    month_key = now.strftime("%Y-%m")
    historical_days = []
    month_actual_net = 0.0
    month_actual_days = 0

    for day_str in sorted(daily_net):
        d = daily_net[day_str]
        if d["hours"] < 20:
            continue
        # Cap surplus at energy cost (regulation)
        comp = min(d["surplus"], d["energy_cost"])
        net = d["energy_cost"] - comp

        if day_str.startswith(month_key) and day_str != today_key:
            month_actual_net += net
            month_actual_days += 1

        if day_str != today_key:
            historical_days.append((day_str, net))

    # --- EWMA of daily net energy cost ---
    # Half-life = 14 days → lambda = ln(2)/14 ≈ 0.0495
    half_life = 14.0
    decay = math.log(2) / half_life
    ewma_num = 0.0
    ewma_den = 0.0
    for day_str, net in historical_days:
        days_ago = (now.date() - datetime.strptime(day_str, "%Y-%m-%d").date()).days
        weight = math.exp(-decay * days_ago)
        ewma_num += weight * net
        ewma_den += weight

    ewma_daily = ewma_num / ewma_den if ewma_den > 0 else 0

    # --- Projection ---
    # Actual this month (complete days, excluding today)
    # + today's partial (actual)
    today_net = net_energy_today  # from the caller
    total_variable_actual = month_actual_net + today_net
    actual_days_used = month_actual_days + 1  # +1 for today (partial)

    # Remaining days projection
    variable_remaining = ewma_daily * days_remaining

    # Total projection
    total_variable = total_variable_actual + variable_remaining
    base_for_iee_proj = total_variable + fixed_month
    iee_proj = base_for_iee_proj * iee_pct
    forecast_pre_iva = total_variable + fixed_month + iee_proj

    return {
        "forecast_pre_iva": round(forecast_pre_iva, 2),
        "days_in_month": days_in_month,
        "days_elapsed": actual_days_used,
        "days_remaining": days_remaining,
        "fixed_month": round(fixed_month, 2),
        "variable_actual": round(total_variable_actual, 2),
        "variable_projected": round(variable_remaining, 2),
        "ewma_daily": round(ewma_daily, 2),
        "ewma_half_life": int(half_life),
        "history_days": len(historical_days),
    }


def get_consum_preus_data(time_range="today"):
    """Return hourly consumption and pricing data color-coded by tariff period.

    time_range: "today", "7d", "30d"
    For 30d, aggregates to daily totals (too many hourly bars).
    """
    bucket = INFLUXDB_BUCKET
    pricing = _load_pricing()
    tariff = _load_indexed_tariff()

    # Calibration factor
    cal_overall = pricing.get("ksem_calibration", {}).get("overall", 1.0)

    # Contract formula parameters
    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    # Power cost parameters
    contracted_power = pricing.get("contracted_power_kw", {})
    power_charges = pricing.get("power_charges_eur_kw_year", {})

    # Determine Flux range
    if time_range == "today":
        start_iso = _cet_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        flux_range = start_iso
    elif time_range == "7d":
        flux_range = "-7d"
    elif time_range == "30d":
        flux_range = "-30d"
    else:
        flux_range = "-7d"

    # Query hourly import (spread)
    import_hourly = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')

    # Query hourly OMIE prices (mean)
    omie_hourly = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    # Build dicts by hour
    import_by_hour = {}
    for t, kwh in import_hourly:
        key = t.replace(minute=0, second=0, microsecond=0)
        import_by_hour[key] = kwh

    omie_by_hour = {}
    for t, price in omie_hourly:
        key = t.replace(minute=0, second=0, microsecond=0)
        omie_by_hour[key] = price

    # Compute per-hour data
    all_hours = sorted(set(import_by_hour) | set(omie_by_hour))
    hourly_data = []
    for hour in all_hours:
        raw_import = import_by_hour.get(hour, 0.0)
        omie = omie_by_hour.get(hour, 0.0)
        period = _get_period(hour)
        calibrated_kwh = raw_import * cal_overall

        # Energy rate (indexed formula)
        inner = (omie + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
        rate = cf_mult * inner + peajes.get(period, 0) + cargos.get(period, 0)

        # Energy cost
        energy_cost = calibrated_kwh * rate

        # Power cost per hour
        cp = contracted_power.get(period, 0)
        pc = power_charges.get(period, 0)
        power_cost = cp * pc / 8760.0

        hourly_data.append({
            "time": hour.isoformat(),
            "period": period,
            "kwh": round(calibrated_kwh, 3),
            "omie": round(omie, 5),
            "rate": round(rate, 5),
            "energy_cost": round(energy_cost, 4),
            "power_cost": round(power_cost, 4),
        })

    # For 30d range, aggregate to daily
    if time_range == "30d" and hourly_data:
        from collections import defaultdict
        daily_agg = defaultdict(lambda: {
            "kwh": 0.0, "energy_cost": 0.0, "power_cost": 0.0,
            "rate_sum": 0.0, "rate_count": 0,
            "periods": defaultdict(lambda: {"kwh": 0.0, "energy_cost": 0.0, "power_cost": 0.0}),
        })
        for h in hourly_data:
            day_key = h["time"][:10]  # YYYY-MM-DD
            d = daily_agg[day_key]
            d["kwh"] += h["kwh"]
            d["energy_cost"] += h["energy_cost"]
            d["power_cost"] += h["power_cost"]
            d["rate_sum"] += h["rate"]
            d["rate_count"] += 1
            p = d["periods"][h["period"]]
            p["kwh"] += h["kwh"]
            p["energy_cost"] += h["energy_cost"]
            p["power_cost"] += h["power_cost"]

        chart_data = []
        for day_key in sorted(daily_agg):
            d = daily_agg[day_key]
            avg_rate = d["rate_sum"] / d["rate_count"] if d["rate_count"] else 0
            # Dominant period for coloring
            dominant = max(d["periods"], key=lambda p: d["periods"][p]["kwh"]) if d["periods"] else "P6"
            chart_data.append({
                "time": day_key + "T12:00:00",
                "period": dominant,
                "kwh": round(d["kwh"], 2),
                "rate": round(avg_rate, 5),
                "energy_cost": round(d["energy_cost"], 2),
                "power_cost": round(d["power_cost"], 2),
            })
        display_data = chart_data
    else:
        display_data = hourly_data

    # Summary
    total_kwh = sum(h["kwh"] for h in hourly_data)
    total_energy_cost = sum(h["energy_cost"] for h in hourly_data)
    total_power_cost = sum(h["power_cost"] for h in hourly_data)
    avg_rate = total_energy_cost / total_kwh if total_kwh > 0 else 0

    # Full bill components (like _estimate_bill)
    # Count distinct days in the data
    distinct_days = len(set(h["time"][:10] for h in hourly_data)) or 1

    # Power charges (full daily cost, not just hourly fraction)
    daily_power_cost = 0.0
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        kw = contracted_power.get(p, 0)
        rate_year = power_charges.get(p, 0)
        daily_power_cost += kw * rate_year / 365.0
    full_power_cost = daily_power_cost * distinct_days

    # Fixed charges
    rental = pricing.get("fixed_charges_eur_day", {}).get("equipment_rental", 0) * distinct_days
    bono = pricing.get("fixed_charges_eur_day", {}).get("bono_social", 0) * distinct_days
    fixed_cost = rental + bono

    # Surplus compensation (from OMIE export)
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')
    surplus_income = 0.0
    for t, kwh in export_hours:
        key = t.replace(minute=0, second=0, microsecond=0)
        omie_price = omie_by_hour.get(key, 0.0)
        surplus_income += max(kwh, 0) * max(omie_price, 0)
    # Cap surplus at energy cost (regulation)
    compensated = min(surplus_income, total_energy_cost)

    # Electricity tax (IEE)
    iee_pct = pricing.get("taxes", {}).get("electricity_tax_pct", 5.11269) / 100
    base_for_iee = total_energy_cost - compensated + full_power_cost
    iee = base_for_iee * iee_pct

    # IVA
    iva_pct = pricing.get("taxes", {}).get("iva_pct", 21.0) / 100
    subtotal_pre_iva = total_energy_cost - compensated + full_power_cost + iee + fixed_cost
    iva = subtotal_pre_iva * iva_pct
    total_with_iva = subtotal_pre_iva + iva

    # Simple total (energy + power hourly, for backward compat)
    total_cost = total_energy_cost + total_power_cost

    # Period breakdown
    from collections import defaultdict as _dd
    period_totals = _dd(lambda: {"kwh": 0.0, "energy_cost": 0.0, "power_cost": 0.0, "hours": 0})
    for h in hourly_data:
        pt = period_totals[h["period"]]
        pt["kwh"] += h["kwh"]
        pt["energy_cost"] += h["energy_cost"]
        pt["power_cost"] += h["power_cost"]
        pt["hours"] += 1

    periods_breakdown = []
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        pt = period_totals.get(p)
        if pt and pt["hours"] > 0:
            periods_breakdown.append({
                "period": p,
                "hours": pt["hours"],
                "kwh": round(pt["kwh"], 2),
                "energy_cost": round(pt["energy_cost"], 2),
                "power_cost": round(pt["power_cost"], 2),
                "total_cost": round(pt["energy_cost"] + pt["power_cost"], 2),
                "avg_rate": round(pt["energy_cost"] / pt["kwh"], 5) if pt["kwh"] > 0 else 0,
            })

    # -- Bill forecast for current month (only when viewing "today") ----------
    bill_forecast = None
    if time_range == "today":
        bill_forecast = _compute_bill_forecast(
            total_energy_cost, compensated, daily_power_cost,
            pricing, iee_pct, distinct_days,
        )

    return {
        "hourly": display_data,
        "summary": {
            "total_kwh": round(total_kwh, 2),
            "energy_cost": round(total_energy_cost, 2),
            "power_cost": round(full_power_cost, 2),
            "fixed_cost": round(fixed_cost, 2),
            "compensation": round(compensated, 2),
            "iee": round(iee, 2),
            "subtotal_pre_iva": round(subtotal_pre_iva, 2),
            "iva": round(iva, 2),
            "total_cost": round(total_with_iva, 2),
            "avg_rate": round(avg_rate, 5),
            "days": distinct_days,
            "bill_forecast": bill_forecast,
        },
        "periods": periods_breakdown,
        "time_range": time_range,
        "aggregation": "daily" if time_range == "30d" else "hourly",
    }


def get_all_dashboard_data():
    """Aggregate all sections + timestamp for the API endpoint.

    Cached for ~30s — see `_DASHBOARD_CACHE_TTL`. Pricing changes invalidate
    via `invalidate_pricing_caches()`.
    """
    global _dashboard_cache_ts, _dashboard_cache_value
    now_mono = time.monotonic()
    if (_dashboard_cache_value is not None
            and now_mono - _dashboard_cache_ts < _DASHBOARD_CACHE_TTL):
        return _dashboard_cache_value

    forecast = get_solar_forecast()
    lost = _get_lost_production()
    maximetre = get_maximetre_analysis()
    reactiva = get_reactive_tracking()
    result = {
        "economia": get_economia(),
        "energia": get_energia(),
        "mercat": get_mercat_omie(),
        "previsio": get_previsio_factura(),
        "inversors": get_inversors(),
        "forecast": forecast,
        "lost_production": lost,
        "compensacio": get_compensacio(),
        "negatius": get_negative_prices(),
        "maximetre": maximetre,
        "reactiva": reactiva,
        "maximetre_savings_annual": maximetre.get("savings_annual", 0.0),
        "maximetre_alert": maximetre.get("alert", False),
        "bateria": get_battery_simulation(),
        "last_update": datetime.now(_CET).strftime("%H:%M:%S"),
    }
    _dashboard_cache_value = result
    _dashboard_cache_ts = now_mono
    return result


# ---------------------------------------------------------------------------
# Official meter CSV ingestion
# ---------------------------------------------------------------------------

def ingest_official_meter_csv(filepath):
    """Parse a Som Energia infoenergia CSV and write hourly data to InfluxDB.

    CSV format: CUPS,Fecha(DD/MM/YYYY),Hora(0-23),Consumo_kWh(int),Metodo_obtencion
    Writes measurement 'official_meter' with field 'import_kwh'.
    Returns summary dict: date_range, total_kwh, hours_count.
    """
    points = []
    total_kwh = 0
    dates = set()

    with open(filepath, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fecha_str = row["Fecha"].strip()
            hora = int(row["Hora"].strip())
            consumo = int(row["Consumo_kWh"].strip())

            # Parse date and build CET timestamp
            day, month, year = fecha_str.split("/")
            dt_cet = datetime(int(year), int(month), int(day), hora, 0, 0, tzinfo=_CET)

            point = (
                Point("official_meter")
                .field("import_kwh", consumo)
                .time(dt_cet)
            )
            points.append(point)
            total_kwh += consumo
            dates.add(fecha_str)

    if points:
        _write_api.write(bucket=INFLUXDB_BUCKET, record=points)

    sorted_dates = sorted(dates, key=lambda d: datetime.strptime(d, "%d/%m/%Y"))
    return {
        "date_range": f"{sorted_dates[0]} - {sorted_dates[-1]}" if sorted_dates else "",
        "total_kwh": total_kwh,
        "hours_count": len(points),
    }


def get_official_vs_ksem_comparison(start_date_str, end_date_str):
    """Compare official meter data with KSEM data for a date range.

    Args:
        start_date_str: 'YYYY-MM-DD'
        end_date_str:   'YYYY-MM-DD'

    Returns dict with hourly comparison and summary stats.
    """
    bucket = INFLUXDB_BUCKET
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=_CET)
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=_CET
    )
    start_rfc = start_dt.strftime("%Y-%m-%dT%H:%M:%S+01:00")
    end_rfc = end_dt.strftime("%Y-%m-%dT%H:%M:%S+01:00")

    # Query official meter (hourly points as written)
    official_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_rfc}, stop: {end_rfc})
          |> filter(fn: (r) => r._measurement == "official_meter")
          |> filter(fn: (r) => r._field == "import_kwh")
    ''')

    # Query KSEM hourly import (spread of cumulative counter)
    ksem_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_rfc}, stop: {end_rfc})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')

    # Build lookup dicts keyed by (date_str, hour)
    def _key(dt_cet):
        return (dt_cet.strftime("%Y-%m-%d"), dt_cet.hour)

    official_map = {}
    for t, v in official_hours:
        official_map[_key(t)] = v

    ksem_map = {}
    for t, v in ksem_hours:
        ksem_map[_key(t)] = v

    all_keys = sorted(set(official_map.keys()) | set(ksem_map.keys()))

    hourly = []
    total_official = 0.0
    total_ksem = 0.0
    missing_official = 0
    missing_ksem = 0
    period_totals = {}  # period -> {official, ksem}

    for key in all_keys:
        date_str, hour = key
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hour, tzinfo=_CET)
        period = _get_period(dt)
        off_v = official_map.get(key)
        ksem_v = ksem_map.get(key)

        if off_v is None:
            missing_official += 1
        else:
            total_official += off_v

        if ksem_v is None:
            missing_ksem += 1
        else:
            total_ksem += ksem_v

        ratio = None
        if off_v and ksem_v and ksem_v > 0:
            ratio = round(off_v / ksem_v, 3)

        # Accumulate per-period
        pt = period_totals.setdefault(period, {"official": 0.0, "ksem": 0.0})
        pt["official"] += off_v or 0
        pt["ksem"] += ksem_v or 0

        hourly.append({
            "date": date_str,
            "hour": hour,
            "period": period,
            "official_kwh": off_v,
            "ksem_kwh": round(ksem_v, 2) if ksem_v is not None else None,
            "ratio": ratio,
        })

    overall_ratio = round(total_official / total_ksem, 3) if total_ksem > 0 else None
    period_ratios = {}
    for p, pt in sorted(period_totals.items()):
        period_ratios[p] = {
            "official_kwh": round(pt["official"], 1),
            "ksem_kwh": round(pt["ksem"], 1),
            "ratio": round(pt["official"] / pt["ksem"], 3) if pt["ksem"] > 0 else None,
        }

    return {
        "hourly": hourly,
        "summary": {
            "total_official_kwh": round(total_official, 1),
            "total_ksem_kwh": round(total_ksem, 1),
            "overall_ratio": overall_ratio,
            "period_ratios": period_ratios,
            "missing_official_hours": missing_official,
            "missing_ksem_hours": missing_ksem,
            "total_hours": len(all_keys),
        },
    }


def auto_calibrate_from_official(start_date_str, end_date_str):
    """Auto-calibrate ksem factor AND other_costs from official meter data.

    Uses hourly official meter kWh + OMIE prices to:
    1. Compute ksem calibration factor (official / ksem, matched hours only)
    2. Back-calculate other_costs_eur_kwh from invoice-equivalent weighted rates

    Args:
        start_date_str: 'YYYY-MM-DD'
        end_date_str:   'YYYY-MM-DD'

    Returns dict with calibration results, or None if insufficient data.
    Updates pricing.json in place.
    """
    bucket = INFLUXDB_BUCKET
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=_CET)
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=_CET)
    end_exclusive = end_dt + timedelta(days=1)

    # Query all three data sources
    official_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_dt.isoformat()}, stop: {end_exclusive.isoformat()})
          |> filter(fn: (r) => r._measurement == "official_meter")
          |> filter(fn: (r) => r._field == "import_kwh")
    ''')
    ksem_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_dt.isoformat()}, stop: {end_exclusive.isoformat()})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')
    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_dt.isoformat()}, stop: {end_exclusive.isoformat()})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    def _hkey(t):
        return t.replace(minute=0, second=0, microsecond=0)

    off_map = {_hkey(t): v for t, v in official_hours}
    ksem_map = {_hkey(t): v for t, v in ksem_hours}
    omie_map = {_hkey(t): v for t, v in omie_hours}

    if len(off_map) < 24:
        return None  # need at least 1 day

    # --- 1. KSEM calibration (matched hours only) ---
    period_off = {}
    period_ksem = {}
    total_off = 0.0
    total_ksem = 0.0
    matched = 0

    for t, off_kwh in off_map.items():
        ksem_kwh = ksem_map.get(t)
        if ksem_kwh is None or ksem_kwh < 0:
            continue
        period = _get_period(t)
        period_off.setdefault(period, 0.0)
        period_ksem.setdefault(period, 0.0)
        period_off[period] += off_kwh
        period_ksem[period] += ksem_kwh
        total_off += off_kwh
        total_ksem += ksem_kwh
        matched += 1

    if total_ksem < 1 or matched < 24:
        return None

    overall_ratio = round(total_off / total_ksem, 4)
    per_period_factors = {}
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        p_off = period_off.get(p, 0)
        p_ksem = period_ksem.get(p, 0)
        if p_ksem > 1 and p_off > 1:
            per_period_factors[p] = round(p_off / p_ksem, 4)
        else:
            per_period_factors[p] = None

    # --- 2. Back-calculate other_costs from official data + OMIE ---
    pricing = _load_pricing()
    tariff = _load_indexed_tariff()
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]
    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.015)
    cf_losses = cf.get("loss_coefficient", 0.12)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.001)
    margin = tariff["margin"]

    # For each period with data, compute consumption-weighted OMIE avg
    # then solve: inv_rate = cf_mult * [(w_omie + X) * (1+losses) + fe + margin] + peaje + cargo
    # X = [(inv_rate - peaje - cargo) / cf_mult - fe - margin] / (1+losses) - w_omie
    # But we don't have the invoice rate directly. We need an invoice for this period.
    # Without an invoice, we can't compute other_costs.
    # However, if there IS a matching invoice in the store, use it.

    other_costs_result = None
    try:
        from invoice import list_invoices
        stored = list_invoices()
        # Find invoice that overlaps with our CSV period
        for inv in stored:
            if inv.get("supplier") != "som_energia":
                continue
            if not inv.get("billing_start") or not inv.get("rates"):
                continue
            inv_start = datetime.strptime(inv["billing_start"], "%d/%m/%Y")
            inv_end = datetime.strptime(inv["billing_end"], "%d/%m/%Y")
            csv_start = start_dt.replace(tzinfo=None)
            csv_end = end_dt.replace(tzinfo=None)
            # Check overlap
            if inv_start <= csv_end and inv_end >= csv_start:
                # Found matching invoice — back-calculate other_costs
                x_values = []
                for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
                    inv_rate = inv.get("rates", {}).get(p, 0)
                    if inv_rate <= 0:
                        continue
                    # Consumption-weighted OMIE for this period
                    p_kwh_omie = 0.0
                    p_kwh = 0.0
                    for t, off_kwh in off_map.items():
                        if _get_period(t) != p or off_kwh <= 0:
                            continue
                        omie_price = omie_map.get(t, 0)
                        p_kwh_omie += off_kwh * omie_price
                        p_kwh += off_kwh
                    if p_kwh < 1:
                        continue
                    w_omie = p_kwh_omie / p_kwh
                    # Solve for X
                    after_reg = inv_rate - peajes[p] - cargos[p]
                    inner = after_reg / cf_mult
                    x = ((inner - cf_fe - margin) / (1 + cf_losses)) - w_omie
                    x_values.append(x)

                if x_values:
                    avg_x = sum(x_values) / len(x_values)
                    other_costs_result = {
                        "value": round(avg_x, 6),
                        "from_invoice": inv.get("filename", "unknown"),
                        "n_periods": len(x_values),
                    }
                break
    except Exception:
        pass  # invoice matching is best-effort

    # --- 3. Update pricing.json ---
    pricing["ksem_calibration"] = {
        "factors": per_period_factors,
        "overall": overall_ratio,
        "total_calibration_kwh": round(total_off, 1),
        "matched_hours": matched,
        "period": f"{start_date_str} to {end_date_str}",
        "last_updated": datetime.now(_CET).strftime("%Y-%m-%d"),
        "_note": "Auto-calibrated from official hourly meter data (infoenergia CSV).",
    }

    if other_costs_result:
        cf_block = pricing.setdefault("energy", {}).setdefault("contract_formula", {})
        old_value = cf_block.get("other_costs_eur_kwh", 0.046)
        cf_block["other_costs_eur_kwh"] = other_costs_result["value"]
        cf_block["_other_costs_note"] = (
            f"Auto-calibrated from {other_costs_result['from_invoice']} "
            f"+ official hourly data ({other_costs_result['n_periods']} periods). "
            f"Previous: {old_value}"
        )
        # Also update the scenario copy
        sc = pricing.get("scenarios", {}).get("som_indexada", {})
        sc_cf = sc.get("contract_formula", {})
        if sc_cf:
            sc_cf["other_costs_eur_kwh"] = other_costs_result["value"]

    # --- 4. Append to calibration history (for tracking evolution) ---
    history = pricing.setdefault("calibration_history", [])
    entry = {
        "date": datetime.now(_CET).strftime("%Y-%m-%d"),
        "period": f"{start_date_str} to {end_date_str}",
        "ksem_overall": overall_ratio,
        "ksem_factors": per_period_factors,
        "matched_hours": matched,
        "total_kwh": round(total_off, 1),
    }
    if other_costs_result:
        entry["other_costs"] = other_costs_result["value"]
        entry["from_invoice"] = other_costs_result["from_invoice"]
    history.append(entry)

    with open(PRICING_PATH, "w") as f:
        json.dump(pricing, f, indent=2, ensure_ascii=False)

    invalidate_pricing_caches()

    return {
        "ksem_calibration": {
            "overall": overall_ratio,
            "factors": per_period_factors,
            "matched_hours": matched,
            "total_kwh": round(total_off, 1),
        },
        "other_costs": other_costs_result,
    }


# -- Year-over-Year comparison -----------------------------------------------

_OMIE_URL = "https://www.omie.es/es/file-download?parents%5B0%5D=marginalpdbc&filename=marginalpdbc_{date}.1"
_OPEN_METEO_ARCHIVE_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
_SOLAR_PR = 0.80  # Performance ratio for simulated production

# Cache for external API responses (historical data never changes)
_yoy_cache = {}


def _parse_omie_text(text, target_date):
    """Parse OMIE marginalpdbc flat file. Returns list of (hour, eur_mwh)."""
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
        if period < 1 or period > 24:
            continue
        prices.append((period - 1, price_spain))  # hour 0-23, EUR/MWh
    return prices


def _fetch_omie_historical(year, month, day):
    """Fetch OMIE prices for a specific date. Returns list of (hour, eur_mwh) or None."""
    cache_key = f"omie_{year}_{month}_{day}"
    if cache_key in _yoy_cache:
        return _yoy_cache[cache_key]

    from datetime import date
    target = date(year, month, day)
    date_str = target.strftime("%Y%m%d")
    url = _OMIE_URL.format(date=date_str)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code != 200:
            _yoy_cache[cache_key] = None
            return None
        prices = _parse_omie_text(resp.text, target)
        if not prices:
            _yoy_cache[cache_key] = None
            return None
        _yoy_cache[cache_key] = prices
        return prices
    except Exception as e:
        _log.warning("YoY: OMIE fetch failed for %s: %s", date_str, e)
        _yoy_cache[cache_key] = None
        return None


def _fetch_open_meteo_archive(year, month, day):
    """Fetch historical irradiance + temperature for a specific date.

    Uses the forecast API for dates within ~92 days (its lookback window),
    falls back to the archive API for older dates.
    Returns dict with hourly arrays or None.
    """
    cache_key = f"meteo_{year}_{month}_{day}"
    if cache_key in _yoy_cache:
        return _yoy_cache[cache_key]

    date_str = f"{year}-{month:02d}-{day:02d}"
    params = {
        "latitude": _SOLAR_LAT,
        "longitude": _SOLAR_LON,
        "start_date": date_str,
        "end_date": date_str,
        "hourly": "global_tilted_irradiance,temperature_2m",
        "tilt": _SOLAR_TILT,
        "azimuth": _SOLAR_AZIMUTH,
        "timezone": "Europe/Madrid",
    }

    # Try historical API first (has GTI for past dates), then forecast API
    data = None
    for url in [_OPEN_METEO_ARCHIVE_URL, "https://api.open-meteo.com/v1/forecast"]:
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                candidate = resp.json()
                if candidate.get("error"):
                    continue
                c_gti = candidate.get("hourly", {}).get("global_tilted_irradiance", [])
                if any(v is not None for v in c_gti):
                    data = candidate
                    break
        except Exception:
            continue

    if data is None:
        _log.warning("YoY: Open-Meteo failed for %s (both archive and forecast)", date_str)
        # Don't cache failures (may be rate-limited)
        return None

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    gti = hourly.get("global_tilted_irradiance", [])
    temp = hourly.get("temperature_2m", [])

    if not times:
        return None

    result = {
        "irradiance": [],  # W/m² per hour
        "temperature": [],  # °C per hour
    }
    for i, t in enumerate(times):
        hour = int(t[11:13]) if len(t) >= 13 else i
        irr_val = gti[i] if i < len(gti) and gti[i] is not None else 0
        temp_val = temp[i] if i < len(temp) and temp[i] is not None else None
        result["irradiance"].append({"h": hour, "v": round(irr_val, 1)})
        result["temperature"].append({"h": hour, "v": round(temp_val, 1) if temp_val is not None else None})

    _yoy_cache[cache_key] = result
    return result


def _get_actual_plant_data(year, month, day):
    """Get actual production + import/export from InfluxDB for a specific date (2026 only).

    Returns dict with hourly production, import, export arrays, or None.
    """
    from datetime import date
    target = date(year, month, day)
    now = _cet_now()
    plant_start = date(2026, 2, 19)

    if target < plant_start or target > now.date():
        return None

    start = datetime(year, month, day, 0, 0, 0, tzinfo=_CET)
    if target == now.date():
        end = now
    else:
        end = datetime(year, month, day, 23, 59, 59, tzinfo=_CET)

    bucket = INFLUXDB_BUCKET
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    # Generation (kWh per hour)
    gen_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_iso}, stop: {end_iso})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
    ''')

    # Import (kWh per hour)
    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_iso}, stop: {end_iso})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')

    # Export (kWh per hour)
    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_iso}, stop: {end_iso})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')

    production = [{"h": t.hour, "v": round(v, 2)} for t, v in gen_hours]
    imports = [{"h": t.hour, "v": round(v, 2)} for t, v in import_hours]
    exports = [{"h": t.hour, "v": round(v, 2)} for t, v in export_hours]

    return {
        "production": production,
        "import": imports,
        "export": exports,
    }


import time as _time


def _fetch_open_meteo_chunk(start_date, end_date):
    """Fetch a single chunk of Open-Meteo data (up to ~3 months).
    Includes retry with backoff for rate limiting (429)."""
    params = {
        "latitude": _SOLAR_LAT,
        "longitude": _SOLAR_LON,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": "global_tilted_irradiance,temperature_2m",
        "tilt": _SOLAR_TILT,
        "azimuth": _SOLAR_AZIMUTH,
        "timezone": "Europe/Madrid",
    }
    for url in [_OPEN_METEO_ARCHIVE_URL, "https://api.open-meteo.com/v1/forecast"]:
        for attempt in range(3):
            try:
                resp = requests.get(url, params=params, timeout=60)
                if resp.status_code == 429:
                    _time.sleep(2 * (attempt + 1))
                    continue
                if resp.status_code == 200:
                    candidate = resp.json()
                    if candidate.get("error"):
                        break  # Try next URL
                    c_gti = candidate.get("hourly", {}).get("global_tilted_irradiance", [])
                    if any(v is not None for v in c_gti):
                        return candidate
                    break  # GTI all null, try next URL
            except Exception:
                break  # Try next URL
    return None


def _fetch_open_meteo_range(year, start_date, end_date):
    """Fetch irradiance + temperature for a date range. Returns daily dict or None.

    For ranges > 90 days, chunks requests by month to avoid API timeouts.
    start_date and end_date are date objects.
    """
    cache_key = f"meteo_range_{year}_{start_date}_{end_date}"
    if cache_key in _yoy_cache:
        return _yoy_cache[cache_key]

    days_span = (end_date - start_date).days

    # For large ranges, chunk by month
    all_times = []
    all_gti = []
    all_temp = []
    if days_span > 90:
        from datetime import date as _date
        chunk_start = start_date
        while chunk_start <= end_date:
            # End of month or end_date, whichever is first
            if chunk_start.month == 12:
                chunk_end = _date(chunk_start.year, 12, 31)
            else:
                chunk_end = _date(chunk_start.year, chunk_start.month + 1, 1) - timedelta(days=1)
            chunk_end = min(chunk_end, end_date)

            data = _fetch_open_meteo_chunk(chunk_start, chunk_end)
            if data:
                hourly = data.get("hourly", {})
                all_times.extend(hourly.get("time", []))
                all_gti.extend(hourly.get("global_tilted_irradiance", []))
                all_temp.extend(hourly.get("temperature_2m", []))

            chunk_start = chunk_end + timedelta(days=1)
            _time.sleep(0.5)  # Rate-limit protection between chunks
    else:
        data = _fetch_open_meteo_chunk(start_date, end_date)
        if data:
            hourly = data.get("hourly", {})
            all_times = hourly.get("time", [])
            all_gti = hourly.get("global_tilted_irradiance", [])
            all_temp = hourly.get("temperature_2m", [])

    times = all_times
    gti = all_gti
    temp = all_temp

    if not times:
        # Don't cache — may be rate-limited
        return None

    # Group by date
    from collections import defaultdict as _dd
    daily = _dd(lambda: {"irr_sum": 0.0, "temp_sum": 0.0, "temp_count": 0})
    for i, t in enumerate(times):
        day_key = t[:10]  # "YYYY-MM-DD"
        irr_val = gti[i] if i < len(gti) and gti[i] is not None else 0
        temp_val = temp[i] if i < len(temp) and temp[i] is not None else None
        daily[day_key]["irr_sum"] += irr_val / 1000  # W/m² → kWh/m² per hour
        if temp_val is not None:
            daily[day_key]["temp_sum"] += temp_val
            daily[day_key]["temp_count"] += 1

    result = {}
    for day_key, d in daily.items():
        result[day_key] = {
            "irr_kwh_m2": round(d["irr_sum"], 2),
            "prod_est_kwh": round(d["irr_sum"] * _SOLAR_KWP * _SOLAR_PR * 1000 / 1000, 1),
            # irr_sum is already in kWh/m², production = irr_sum * kWp * PR
            "temp_avg_c": round(d["temp_sum"] / d["temp_count"], 1) if d["temp_count"] > 0 else None,
        }
    # Fix production: irr_sum is kWh/m²/day, production = irr_sum * kWp * PR
    for day_key in result:
        result[day_key]["prod_est_kwh"] = round(
            result[day_key]["irr_kwh_m2"] * _SOLAR_KWP * _SOLAR_PR, 1)

    _yoy_cache[cache_key] = result
    return result


def _get_actual_plant_range(start_date, end_date):
    """Get actual daily production + import from InfluxDB for a date range.

    Returns dict keyed by 'YYYY-MM-DD' with production_kwh and import_kwh, or empty dict.
    """
    now = _cet_now()
    plant_start = datetime(2026, 2, 19).date()

    if start_date > now.date() or end_date < plant_start:
        return {}

    # Clamp to available range
    eff_start = max(start_date, plant_start)
    eff_end = min(end_date, now.date())

    bucket = INFLUXDB_BUCKET
    start_iso = datetime(eff_start.year, eff_start.month, eff_start.day,
                         0, 0, 0, tzinfo=_CET).isoformat()
    end_iso = datetime(eff_end.year, eff_end.month, eff_end.day,
                       23, 59, 59, tzinfo=_CET).isoformat()

    # Daily generation
    gen_records = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_iso}, stop: {end_iso})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => exists r.inverter)
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
          |> group(columns: ["_time"])
          |> sum()
          |> group()
          |> map(fn: (r) => ({{r with _value: r._value / 1000.0}}))
    ''')

    # Daily import
    import_records = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {start_iso}, stop: {end_iso})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
          |> filter(fn: (r) => r._value >= 0 and r._value < 200)
    ''')

    from collections import defaultdict as _dd
    daily = _dd(lambda: {"prod": 0.0, "imp": 0.0})
    for t, v in gen_records:
        daily[t.strftime("%Y-%m-%d")]["prod"] += v
    for t, v in import_records:
        daily[t.strftime("%Y-%m-%d")]["imp"] += v

    return {day: {"production_kwh": round(d["prod"], 1),
                   "import_kwh": round(d["imp"], 1)}
            for day, d in daily.items()}


def _fetch_omie_range(year, start_date, end_date):
    """Fetch OMIE daily averages for a date range. Uses parallel requests."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import date

    dates = []
    current = start_date
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=1)

    def _fetch_one(d):
        prices = _fetch_omie_historical(d.year, d.month, d.day)
        if prices:
            avg = sum(p for _, p in prices) / len(prices)
            return (d.isoformat(), round(avg, 1))
        return None

    result = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r in pool.map(_fetch_one, dates):
            if r:
                result[r[0]] = r[1]
    return result


def get_yoy_comparison_range(mode, param):
    """Compare a time range across years 2024-2026.

    mode: 'week' (param=week_number), 'month' (param=month_number), 'year' (param ignored)
    Returns aggregated daily data for charts + summary KPIs per year.
    """
    from datetime import date
    now = _cet_now()

    def _get_date_range(year, mode, param):
        """Return (start_date, end_date, label) for the given mode/param/year."""
        if mode == "week":
            # ISO week number
            jan1 = date(year, 1, 1)
            # Find first Monday of ISO week 1
            iso_start = date.fromisocalendar(year, int(param), 1)
            iso_end = date.fromisocalendar(year, int(param), 7)
            return iso_start, iso_end, f"S{param}"
        elif mode == "month":
            m = int(param)
            start = date(year, m, 1)
            last_day = calendar.monthrange(year, m)[1]
            end = date(year, m, last_day)
            return start, end, f"{m:02d}/{year}"
        elif mode == "year":
            start = date(year, 1, 1)
            end = date(year, 12, 31)
            return start, end, str(year)
        return None, None, ""

    years_data = {}
    for year in [2024, 2025, 2026]:
        try:
            start_d, end_d, label = _get_date_range(year, mode, param)
        except (ValueError, TypeError):
            years_data[str(year)] = None
            continue

        if start_d is None or start_d > now.date():
            years_data[str(year)] = None
            continue

        # Clamp end to today
        end_d = min(end_d, now.date())

        # Fetch meteo data (one API call for the range)
        meteo_daily = _fetch_open_meteo_range(year, start_d, end_d) or {}

        # Fetch OMIE daily averages
        omie_daily = _fetch_omie_range(year, start_d, end_d)

        # Get actual plant data for 2026
        actual_daily = {}
        if year == 2026:
            actual_daily = _get_actual_plant_range(start_d, end_d)

        # Build daily chart data + accumulate totals
        chart_irr = []
        chart_prod = []
        chart_omie = []
        chart_temp = []

        total_irr = 0.0
        total_prod = 0.0
        total_omie_sum = 0.0
        total_omie_count = 0
        total_temp_sum = 0.0
        total_temp_count = 0
        total_cost = 0.0
        has_actual = False
        days_count = 0

        current = start_d
        while current <= end_d:
            day_str = current.isoformat()
            # Use MM-DD as x label for charts (so years align)
            x_label = f"{current.month:02d}-{current.day:02d}"
            days_count += 1

            # Irradiance
            m = meteo_daily.get(day_str, {})
            irr = m.get("irr_kwh_m2", 0)
            total_irr += irr
            chart_irr.append({"x": x_label, "y": round(irr, 2)})

            # Production (actual for 2026, estimated otherwise)
            if day_str in actual_daily:
                prod = actual_daily[day_str]["production_kwh"]
                has_actual = True
            else:
                prod = m.get("prod_est_kwh", 0)
            total_prod += prod
            chart_prod.append({"x": x_label, "y": round(prod, 1)})

            # OMIE
            omie_avg = omie_daily.get(day_str)
            if omie_avg is not None:
                total_omie_sum += omie_avg
                total_omie_count += 1
                chart_omie.append({"x": x_label, "y": omie_avg})

            # Temperature
            temp = m.get("temp_avg_c")
            if temp is not None:
                total_temp_sum += temp
                total_temp_count += 1
                chart_temp.append({"x": x_label, "y": temp})

            current += timedelta(days=1)

        production_source = "actual" if has_actual else "estimated"
        if has_actual and len(actual_daily) < days_count:
            production_source = "mixed"

        years_data[str(year)] = {
            "irradiance_kwh_m2": round(total_irr, 1),
            "production_kwh": round(total_prod, 0),
            "production_source": production_source,
            "omie_avg_eur_mwh": round(total_omie_sum / total_omie_count, 1) if total_omie_count > 0 else None,
            "temperature_avg_c": round(total_temp_sum / total_temp_count, 1) if total_temp_count > 0 else None,
            "days": days_count,
            "daily": {
                "irradiance": chart_irr,
                "production": chart_prod,
                "omie": chart_omie,
                "temperature": chart_temp,
            },
        }

    return {
        "mode": mode,
        "param": str(param),
        "years": years_data,
    }


def get_yoy_comparison(month, day):
    """Compare the same calendar day across years 2024-2026.

    Returns data for each year: irradiance, production (estimated or actual),
    OMIE prices, estimated cost, temperature.
    """
    now = _cet_now()
    current_year = now.year
    years_data = {}

    tariff = _load_indexed_tariff()
    cf = tariff.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)
    margin = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    for year in [2024, 2025, 2026]:
        # Skip future dates
        from datetime import date
        try:
            target = date(year, month, day)
        except ValueError:
            # Invalid date (e.g., Feb 29 in non-leap year)
            years_data[str(year)] = None
            continue

        if target > now.date():
            years_data[str(year)] = None
            continue

        # Fetch irradiance + temperature from Open-Meteo archive
        meteo = _fetch_open_meteo_archive(year, month, day)

        # Fetch OMIE prices
        omie_data = None
        if year == 2026:
            # Try InfluxDB first for 2026
            bucket = INFLUXDB_BUCKET
            start_iso = datetime(year, month, day, 0, 0, 0, tzinfo=_CET).isoformat()
            end_iso = datetime(year, month, day, 23, 59, 59, tzinfo=_CET).isoformat()
            omie_hours = _hourly_records(f'''
                from(bucket: "{bucket}")
                  |> range(start: {start_iso}, stop: {end_iso})
                  |> filter(fn: (r) => r._measurement == "omie_prices")
                  |> filter(fn: (r) => r._field == "price_eur_kwh")
                  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
            ''')
            if omie_hours:
                omie_data = [(t.hour, v * 1000) for t, v in omie_hours]  # convert €/kWh → €/MWh

        if omie_data is None:
            omie_data = _fetch_omie_historical(year, month, day)

        # Get actual plant data for 2026
        actual = None
        if year == 2026:
            actual = _get_actual_plant_data(year, month, day)

        # Build hourly arrays
        irr_hourly = []
        prod_hourly = []
        omie_hourly = []
        temp_hourly = []

        # Irradiance + estimated production
        total_irr_kwh_m2 = 0.0
        total_prod_kwh = 0.0
        if meteo:
            for pt in meteo["irradiance"]:
                irr_w = pt["v"]
                irr_hourly.append({"x": pt["h"], "y": irr_w})
                total_irr_kwh_m2 += irr_w / 1000  # W/m² → kWh/m² per hour
                # Estimated production
                est_kwh = irr_w * _SOLAR_KWP * _SOLAR_PR / 1000
                prod_hourly.append({"x": pt["h"], "y": round(est_kwh, 2)})
                total_prod_kwh += est_kwh
            for pt in meteo["temperature"]:
                temp_hourly.append({"x": pt["h"], "y": pt["v"]})

        # Override production with actual data for 2026
        production_source = "estimated"
        if actual and actual["production"]:
            prod_hourly = [{"x": pt["h"], "y": pt["v"]} for pt in actual["production"]]
            total_prod_kwh = sum(pt["v"] for pt in actual["production"])
            production_source = "actual"

        # OMIE prices
        omie_avg = 0.0
        if omie_data:
            omie_hourly = [{"x": h, "y": round(p, 2)} for h, p in omie_data]
            omie_avg = sum(p for _, p in omie_data) / len(omie_data) if omie_data else 0

        # Estimate energy cost using indexed formula + OMIE prices
        estimated_cost = 0.0
        if omie_data and meteo:
            omie_by_h = {h: p / 1000 for h, p in omie_data}  # €/MWh → €/kWh
            for pt in meteo["irradiance"]:
                h = pt["h"]
                irr_w = pt["v"]
                # Estimate: what you'd import = consumption - self_consumption
                # Simplified: use production to estimate avoided import
                omie_kwh = omie_by_h.get(h, 0)
                period = _get_period(datetime(year, month, day, h, 0, tzinfo=_CET))
                inner = (omie_kwh + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
                rate = cf_mult * inner + peajes.get(period, 0) + cargos.get(period, 0)
                # For actual 2026 data, use real import
                if actual and actual["import"]:
                    imp_map = {pt["h"]: pt["v"] for pt in actual["import"]}
                    imp_kwh = imp_map.get(h, 0)
                    estimated_cost += imp_kwh * rate
                # No actual import data: skip cost (we don't know consumption)

        # If we have actual import data, recalculate properly
        if actual and actual["import"] and omie_data:
            estimated_cost = 0.0
            omie_by_h = {h: p / 1000 for h, p in omie_data}
            cal = _load_pricing().get("ksem_calibration", {}).get("overall", 1.0)
            for pt in actual["import"]:
                h = pt["h"]
                imp_kwh = pt["v"] * cal
                omie_kwh = omie_by_h.get(h, 0)
                period = _get_period(datetime(year, month, day, h, 0, tzinfo=_CET))
                inner = (omie_kwh + _resolve_cf_other(cf, period)) * (1 + cf_losses) + cf_fe + margin
                rate = cf_mult * inner + peajes.get(period, 0) + cargos.get(period, 0)
                estimated_cost += imp_kwh * rate

        # Temperature average
        temp_avg = None
        if meteo and meteo["temperature"]:
            temps = [pt["v"] for pt in meteo["temperature"] if pt["v"] is not None]
            temp_avg = round(sum(temps) / len(temps), 1) if temps else None

        years_data[str(year)] = {
            "irradiance_kwh_m2": round(total_irr_kwh_m2, 2),
            "production_kwh": round(total_prod_kwh, 1),
            "production_source": production_source,
            "omie_avg_eur_mwh": round(omie_avg, 1),
            "estimated_cost_eur": round(estimated_cost, 2),
            "temperature_avg_c": temp_avg,
            "hourly": {
                "irradiance": irr_hourly,
                "production": prod_hourly,
                "omie": omie_hourly,
                "temperature": temp_hourly,
            },
        }

    return {
        "date": f"{month:02d}-{day:02d}",
        "years": years_data,
    }


# ---------------------------------------------------------------------------
# Data recovery from backup InfluxDB (Pi)
# ---------------------------------------------------------------------------

# Fields known to be integer type in local InfluxDB
_INTEGER_FIELDS = {"status"}

log = logging.getLogger(__name__)


def recover_data_from_backup(lookback_hours=48):
    """Detect gaps in local data and fill them from the backup Pi InfluxDB.

    Returns a dict with recovery stats.
    """
    if not BACKUP_INFLUXDB_URL:
        return {"error": "No backup InfluxDB configured (BACKUP_INFLUXDB_URL)"}

    # Use one representative field per measurement to detect gaps
    # (avoids schema collision when grouping mixed int/float fields)
    # daylight_only: PIKO inverters stop reporting at night, so empty
    # 10-min windows outside 06:00-22:00 Madrid are expected, not gaps.
    measurements = {
        "ksem": {"field": "active_power_total", "daylight_only": False},
        "piko": {"field": "ac_power_total", "daylight_only": True},
    }

    def _is_daylight(win_utc_label):
        dt_utc = datetime.strptime(
            win_utc_label, "%Y-%m-%dT%H:%M"
        ).replace(tzinfo=timezone.utc)
        return 6 <= dt_utc.astimezone(_CET).hour < 22
    headers = {
        "Authorization": f"Token {INFLUXDB_TOKEN}",
        "Content-Type": "application/vnd.flux",
    }
    write_headers = {
        "Authorization": f"Token {INFLUXDB_TOKEN}",
        "Content-Type": "text/plain",
    }
    local_query_url = f"{INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}"
    backup_query_url = f"{BACKUP_INFLUXDB_URL}/api/v2/query?org={INFLUXDB_ORG}"
    local_write_url = (
        f"{INFLUXDB_URL}/api/v2/write"
        f"?org={INFLUXDB_ORG}&bucket={INFLUXDB_BUCKET}&precision=ns"
    )

    results = {}

    for meas, cfg in measurements.items():
        detect_field = cfg["field"]
        daylight_only = cfg["daylight_only"]
        # 1. Find 10-min windows with data locally in lookback window
        count_query = (
            f'from(bucket: "{INFLUXDB_BUCKET}")'
            f" |> range(start: -{lookback_hours}h)"
            f' |> filter(fn: (r) => r._measurement == "{meas}"'
            f' and r._field == "{detect_field}")'
            f" |> group()"
            f' |> aggregateWindow(every: 10m, fn: count, timeSrc: "_start")'
            f" |> yield()"
        )
        resp = requests.post(local_query_url, headers=headers, data=count_query,
                             timeout=15)
        local_windows = set()
        if resp.status_code == 200 and resp.text.strip():
            for line in resp.text.strip().split("\r\n"):
                if line.startswith(",_result"):
                    cols = line.split(",")
                    # CSV columns: ,result,table,_time,_start,_stop,_value
                    time_col = cols[3] if len(cols) > 3 else ""
                    val_col = cols[6] if len(cols) > 6 else "0"
                    try:
                        if int(float(val_col)) > 0:
                            local_windows.add(time_col[:16])  # YYYY-MM-DDTHH:MM
                    except (ValueError, IndexError):
                        pass

        # 2. Find 10-min windows with data on backup
        resp_b = requests.post(backup_query_url, headers=headers, data=count_query,
                               timeout=15)
        if resp_b.status_code != 200:
            results[meas] = {"error": f"Backup query failed: {resp_b.status_code}"}
            continue

        backup_windows = set()
        if resp_b.text.strip():
            for line in resp_b.text.strip().split("\r\n"):
                if line.startswith(",_result"):
                    cols = line.split(",")
                    # CSV columns: ,result,table,_time,_start,_stop,_value
                    time_col = cols[3] if len(cols) > 3 else ""
                    val_col = cols[6] if len(cols) > 6 else "0"
                    try:
                        if int(float(val_col)) > 0:
                            backup_windows.add(time_col[:16])
                    except (ValueError, IndexError):
                        pass

        # 3. Identify missing windows (in backup but not local)
        missing = sorted(backup_windows - local_windows)
        if daylight_only:
            missing = [w for w in missing if _is_daylight(w)]
        if not missing:
            results[meas] = {"recovered": 0, "gaps": 0, "message": "Cap forat detectat"}
            continue

        # 4. Build time range spanning first to last missing window
        gap_start = missing[0] + ":00Z"
        end_dt = datetime.strptime(
            missing[-1], "%Y-%m-%dT%H:%M"
        ) + timedelta(minutes=10)
        gap_end = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        # 5. Fetch raw data from backup for the gap period
        raw_query = (
            f'from(bucket: "{INFLUXDB_BUCKET}")'
            f' |> range(start: {gap_start}, stop: {gap_end})'
            f' |> filter(fn: (r) => r._measurement == "{meas}")'
        )
        resp_raw = requests.post(backup_query_url, headers=headers, data=raw_query,
                                 timeout=60)
        if resp_raw.status_code != 200:
            results[meas] = {"error": f"Raw data fetch failed: {resp_raw.status_code}"}
            continue

        # 6. Parse CSV → line protocol
        lines_out = []
        tables = resp_raw.text.strip().split("\r\n\r\n")
        for table in tables:
            if not table.strip():
                continue
            rows = table.strip().split("\r\n")
            if len(rows) < 2:
                continue
            header = rows[0].lstrip(",").split(",")
            skip_keys = {
                "result", "table", "_start", "_stop",
                "_time", "_value", "_field", "_measurement",
            }
            for row_str in rows[1:]:
                if not row_str.strip():
                    continue
                vals = row_str.lstrip(",").split(",")
                row = dict(zip(header, vals))

                tags = {}
                for k, v in row.items():
                    if k not in skip_keys and k and v:
                        tags[k] = v.replace(" ", "\\ ")
                tag_str = ""
                if tags:
                    tag_str = "," + ",".join(
                        f"{k}={v}" for k, v in sorted(tags.items())
                    )

                time_str = row.get("_time", "")
                if not time_str:
                    continue
                dt = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
                ns = int(dt.timestamp() * 1_000_000_000)

                field_name = row.get("_field", "")
                val = row.get("_value", "")
                if field_name in _INTEGER_FIELDS:
                    val_str = f"{int(float(val))}i"
                else:
                    try:
                        val_str = f"{float(val)}"
                    except ValueError:
                        val_str = f'"{val}"'

                lines_out.append(f"{meas}{tag_str} {field_name}={val_str} {ns}")

        # 7. Write to local in batches
        written = 0
        batch_size = 5000
        write_error = None
        for i in range(0, len(lines_out), batch_size):
            batch = lines_out[i : i + batch_size]
            wr = requests.post(local_write_url, headers=write_headers,
                               data="\n".join(batch), timeout=30)
            if wr.status_code == 204:
                written += len(batch)
            else:
                write_error = wr.text[:200]
                log.error("Recovery write error for %s: %s", meas, write_error)
                break

        results[meas] = {
            "gaps": len(missing),
            "gap_range": f"{gap_start} → {gap_end}",
            "recovered": written,
            "total_points": len(lines_out),
        }
        if write_error:
            results[meas]["write_error"] = write_error

    return results
