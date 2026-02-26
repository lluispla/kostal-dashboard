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
import json
from datetime import datetime, timezone, timedelta

from influxdb_client import InfluxDBClient

from config import (
    INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET,
    PRICING_PATH,
    PIKO_15_RATED_W, PIKO_CI_50_RATED_W, STATUS_MAP,
)

_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
_query_api = _client.query_api()

# -- helpers -----------------------------------------------------------------

_CET = timezone(timedelta(hours=1))


def _cet_now():
    """Return current datetime in CET (simplified: always +01:00)."""
    return datetime.now(_CET)


def _get_period(dt):
    """Return 3.0TD period 'P1'-'P6' for a CET datetime."""
    weekday = dt.weekday()  # 0=Mon .. 6=Sun
    h = dt.hour
    if weekday == 6:  # Sunday
        return "P6"
    if weekday == 5:  # Saturday
        if 8 <= h < 18:
            return "P4"
        return "P5"
    # Mon-Fri
    if 0 <= h < 8:
        return "P5"
    if 8 <= h < 10:
        return "P2"
    if 10 <= h < 14:
        return "P1"
    if 14 <= h < 18:
        return "P2"
    if 18 <= h < 22:
        return "P3"
    return "P5"  # 22-24


# -- pricing cache ----------------------------------------------------------

_indexed_tariff_cache = None
_pricing_cache = None


def invalidate_pricing_caches():
    """Clear all pricing caches so the next call re-reads pricing.json."""
    global _indexed_tariff_cache, _pricing_cache
    _indexed_tariff_cache = None
    _pricing_cache = None


def _load_indexed_tariff():
    """Load indexed tariff components from pricing.json (cached).

    Includes contract formula parameters for the full clause 2b computation.
    """
    global _indexed_tariff_cache
    if _indexed_tariff_cache is None:
        with open(PRICING_PATH) as f:
            data = json.load(f)
        block = data["indexed_tariff"]
        sc_sidx = data.get("scenarios", {}).get("som_indexada", {})
        _indexed_tariff_cache = {
            "peajes": block["peajes_eur_kwh"],
            "cargos": block["cargos_eur_kwh"],
            "margin": block["margin_comercialitzadora_eur_kwh"],
            "contract_formula": sc_sidx.get("contract_formula", {}),
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
    """Return effective energy rate EUR/kWh from pricing.json."""
    return _load_pricing()["energy"]["effective_rate_eur_kwh"]


def _get_energy_rates():
    """Return per-period energy rates dict from pricing.json.

    Falls back to flat effective rate if rates_eur_kwh not present.
    """
    pricing = _load_pricing()
    rates = pricing["energy"].get("rates_eur_kwh")
    if rates:
        return rates
    flat = pricing["energy"]["effective_rate_eur_kwh"]
    return {f"P{i}": flat for i in range(1, 7)}


def _get_injection_price():
    """Return injection compensation EUR/kWh from pricing.json."""
    return _load_pricing()["injection"]["price_eur_kwh"]


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

def get_economia():
    today = _today_start_iso()
    month = _month_start_iso()
    electricity_cost = _get_effective_rate()
    injection_price = _get_injection_price()

    # Today's energy balance
    gen_today = _generation_kwh(today)
    export_today = _export_kwh(today)
    import_today = _import_kwh(today)
    self_consumption_today = max(gen_today - export_today, 0.0)
    consumption_today = self_consumption_today + import_today

    savings_today = round(self_consumption_today * electricity_cost, 2)
    injection_income_today = round(export_today * injection_price, 2)
    total_benefit_today = round(savings_today + injection_income_today, 2)

    # Monthly totals (same approach)
    gen_month = _generation_kwh(month)
    export_month = _export_kwh(month)
    import_month = _import_kwh(month)
    self_consumption_month = max(gen_month - export_month, 0.0)

    monthly_savings = round(self_consumption_month * electricity_cost, 2)
    monthly_export_income = round(export_month * injection_price, 2)
    monthly_benefit = round(monthly_savings + monthly_export_income, 2)

    # Effective cost per kWh consumed
    if consumption_today > 0:
        effective_cost_kwh = round(
            (import_today * electricity_cost) / consumption_today, 4
        )
    else:
        effective_cost_kwh = 0.0

    return {
        "savings_today": savings_today,
        "self_consumption_kwh_today": round(self_consumption_today, 1),
        "injection_income_today": injection_income_today,
        "export_kwh_today": round(export_today, 1),
        "total_benefit_today": total_benefit_today,
        "monthly_benefit": monthly_benefit,
        "effective_cost_kwh": effective_cost_kwh,
        "imported_kwh_today": round(import_today, 1),
        "consumed_kwh_today": round(consumption_today, 1),
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
    voltage_l1 = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l1")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')
    voltage_l2 = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l2")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')
    voltage_l3 = _records_xy(f'''
        from(bucket: "{bucket}")
          |> range(start: {today})
          |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "piko_ci_50")
          |> filter(fn: (r) => r._field == "ac_voltage_l3")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')

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
        inner = (omie_price + cf_other) * (1 + cf_losses) + cf_fe + margin
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
    _inner = (omie_eur_kwh + cf_other) * (1 + cf_losses) + cf_fe + tariff["margin"]
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
            voltages[phase] = round(_scalar(f'''
                from(bucket: "{bucket}")
                  |> range(start: -5m)
                  |> filter(fn: (r) => r._measurement == "piko" and r.inverter == "{tag}")
                  |> filter(fn: (r) => r._field == "ac_voltage_{phase}")
                  |> last()
            '''), 1)

        overvoltage = any(v > 253.0 for v in voltages.values())

        return {
            "status": status_val,
            "text": STATUS_MAP.get(status_val, f"Desconegut ({status_val})"),
            "power_w": round(power, 0),
            "power_pct": pct,
            "voltage_l1": voltages["l1"],
            "voltage_l2": voltages["l2"],
            "voltage_l3": voltages["l3"],
            "overvoltage": overvoltage,
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
            inner = (omie_price + cf_other) * (1 + cf_losses) + cf_fe + idx_margin
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
        inner = (omie_price + cf_other) * (1 + cf_losses) + cf_fe + tariff["margin"]
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
        "voltage_l1": voltage_l1,
        "voltage_l2": voltage_l2,
        "voltage_l3": voltage_l3,
        "_fixed_rate": _get_effective_rate(),
        "summary": {
            "total_generation_kwh": round(total_gen, 1),
            "total_consumption_kwh": round(total_cons, 1),
            "total_import_kwh": round(total_imp, 1),
            "total_export_kwh": round(total_exp, 1),
            "avg_indexed_eur_kwh": round(avg_indexed, 5),
            "self_consumption_pct": self_cons_pct,
            "days": len(set(t[:10] for t in all_times)) if all_times else 0,
        },
    }


def get_all_dashboard_data():
    """Aggregate all sections + timestamp for the API endpoint."""
    return {
        "economia": get_economia(),
        "energia": get_energia(),
        "mercat": get_mercat_omie(),
        "previsio": get_previsio_factura(),
        "inversors": get_inversors(),
        "last_update": datetime.now(_CET).strftime("%H:%M:%S"),
    }
