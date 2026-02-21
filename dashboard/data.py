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
    """Load indexed tariff components from pricing.json (cached)."""
    global _indexed_tariff_cache
    if _indexed_tariff_cache is None:
        with open(PRICING_PATH) as f:
            data = json.load(f)
        block = data["indexed_tariff"]
        _indexed_tariff_cache = {
            "peajes": block["peajes_eur_kwh"],
            "cargos": block["cargos_eur_kwh"],
            "margin": block["margin_comercialitzadora_eur_kwh"],
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

    # Today's energy for self-consumption rate
    gen_today = _generation_kwh(today)
    export_today = _export_kwh(today)
    self_consumption_today = max(gen_today - export_today, 0.0)
    if gen_today > 0:
        self_consumption_rate = round((self_consumption_today / gen_today) * 100, 1)
    else:
        self_consumption_rate = 0.0

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
        "power_curve": {
            "generation": generation,
            "consumption": consumption_curve,
            "grid": grid_curve,
        },
        "daily_yield_30d": daily_yield_30d,
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
    """
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]
    margin = tariff["margin"]
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
        real_indexed = omie_price + peajes[period] + cargos[period] + margin

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

    # Current period and real indexed rate right now
    now = _cet_now()
    current_period = _get_period(now)
    current_indexed_real = (
        omie_eur_kwh
        + tariff["peajes"][current_period]
        + tariff["cargos"][current_period]
        + tariff["margin"]
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

        return {
            "status": status_val,
            "text": STATUS_MAP.get(status_val, f"Desconegut ({status_val})"),
            "power_w": round(power, 0),
            "power_pct": pct,
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


def get_previsio_factura(mercat_data):
    """Project full electricity bill (monthly + annual) for treasury management.

    Reuses energy costs from mercat_data to avoid duplicate InfluxDB queries.
    Adds power charges, taxes, fixed charges, and injection compensation.
    """
    pricing = _load_pricing()
    now = _cet_now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = (now - month_start).days + now.hour / 24.0
    ratio = days_in_month / max(days_elapsed, 0.5)

    # Reuse cumulative energy costs from mercat
    energy_fix = mercat_data["cost_fixed_month"] * ratio
    energy_idx = mercat_data["cost_indexed_month"] * ratio

    # Export projection for injection compensation
    export_month = _export_kwh(_month_start_iso())
    export_projected = export_month * ratio
    injection_price = pricing["injection"]["price_eur_kwh"]
    compensacio = round(export_projected * injection_price, 2)

    # Power charges — deterministic (same for both tariffs)
    power_cost = 0.0
    for period, rate in pricing["power_charges_eur_kw_day"].items():
        kw = pricing["contracted_power_kw"].get(period, 69)
        power_cost += rate * kw * days_in_month
    power_cost = round(power_cost, 2)

    # Fixed charges — deterministic
    fixed_daily = sum(pricing["fixed_charges_eur_day"].values())
    fixed_charges = round(fixed_daily * days_in_month, 2)

    # Tax rates
    elec_tax_pct = pricing["taxes"]["electricity_tax_pct"] / 100
    iva_pct = pricing["taxes"]["iva_pct"] / 100

    def _compute_bill(energy):
        base = energy + power_cost
        impost = round(base * elec_tax_pct, 2)
        subtotal = base + impost + fixed_charges
        iva = round(subtotal * iva_pct, 2)
        total = round(subtotal + iva, 2)
        net = round(total - compensacio, 2)
        return {
            "energia": round(energy, 2),
            "potencia": power_cost,
            "impost_electric": impost,
            "carregues_fixes": fixed_charges,
            "iva": iva,
            "total": total,
            "compensacio": compensacio,
            "net": net,
        }

    mensual_fix = _compute_bill(energy_fix)
    mensual_idx = _compute_bill(energy_idx)

    anual_fix_net = round(mensual_fix["net"] * 12, 2)
    anual_idx_net = round(mensual_idx["net"] * 12, 2)

    return {
        "days_elapsed": round(days_elapsed, 1),
        "days_in_month": days_in_month,
        "mensual_fix": mensual_fix,
        "mensual_indexat": mensual_idx,
        "diff_mensual": round(mensual_fix["net"] - mensual_idx["net"], 2),
        "anual_fix_net": anual_fix_net,
        "anual_indexat_net": anual_idx_net,
        "estalvi_anual_indexat": round(anual_fix_net - anual_idx_net, 2),
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
        {"x": t, "y": round(gen_dict.get(t, 0) - exp_dict.get(t, 0) + imp_dict.get(t, 0), 2)}
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

    # Compute full indexed rate per hour (period-aware)
    indexed_hourly = []
    for t_cet, omie_price in omie_hourly_raw:
        period = _get_period(t_cet)
        full_rate = (omie_price
                     + tariff["peajes"][period]
                     + tariff["cargos"][period]
                     + tariff["margin"])
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
    mercat = get_mercat_omie()
    return {
        "economia": get_economia(),
        "energia": get_energia(),
        "mercat": mercat,
        "previsio": get_previsio_factura(mercat),
        "inversors": get_inversors(),
        "last_update": datetime.now(_CET).strftime("%H:%M:%S"),
    }
