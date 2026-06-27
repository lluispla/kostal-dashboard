"""Load Scheduler — find the cheapest start time to run a configurable load.

For a user-specified load (kW) and duration (hours), scan every possible
start offset in the next N hours and compute the real invoice cost using
solar forecast (Open-Meteo) + OMIE day-ahead prices + the full Som Energia
Indexada formula with all corrections applied: Art 99.2 IEE (per-kWh) and
21% IVA.  Return ranked candidates with the best/worst starts and savings.

This differs from pa11's scheduler in three ways:
  1. Correct 3.0TD monthly-rotating periods (not summer/winter).
  2. Pricing parameters read from pricing.json, never hardcoded.
  3. Final cost includes Art 99.2 electricity tax and IVA — reflects what
     the customer actually pays on the invoice, not just the grid price.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config import INFLUXDB_BUCKET
from data import (
    _CET, _cet_now, _get_period, _hourly_records,
    _load_indexed_tariff, _load_pricing, get_solar_forecast,
)

_log = logging.getLogger(__name__)

_MIN_LOAD_KW = 0.1
_MAX_LOAD_KW = 200.0
_MIN_DURATION_H = 1
_MAX_DURATION_H = 24
_DEFAULT_LOOKAHEAD_H = 48


def _hourly_solar_kw(forecast):
    """Resample the 15-min solar forecast to {datetime(CET, hour): kW_avg}."""
    buckets = {}  # (date, hour) -> [kw_samples]
    for series in ("forecast_today", "forecast_tomorrow", "forecast_day3"):
        for point in forecast.get(series, []):
            try:
                dt = datetime.fromisoformat(point["x"])
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_CET)
            key = dt.replace(minute=0, second=0, microsecond=0)
            kw = (point["y"] or 0.0) / 1000.0  # W -> kW
            buckets.setdefault(key, []).append(kw)
    return {k: sum(v) / len(v) for k, v in buckets.items() if v}


def _indexed_rate_for(omie_eur_kwh, period, cf, margin, peajes, cargos):
    """Compute the €/kWh indexed rate for one hour (excluding IEE/IVA)."""
    inner = (omie_eur_kwh + cf.get("other_costs_eur_kwh", 0.0)) * (
        1 + cf.get("loss_coefficient", 0.0)
    ) + cf.get("efficiency_fund_eur_kwh", 0.0) + margin
    return cf.get("adjustment_multiplier", 1.0) * inner + peajes.get(
        period, 0.0
    ) + cargos.get(period, 0.0)


def _query_omie_hourly(range_start_iso, range_stop_iso):
    """Pull hourly OMIE prices keyed by CET hour."""
    records = _hourly_records(f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {range_start_iso}, stop: {range_stop_iso})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')
    return {t.replace(minute=0, second=0, microsecond=0): v for t, v in records}


def _query_omie_recent_by_weekday_hour(days=14):
    """Build a forecast-fallback map (weekday, hour) -> mean OMIE €/kWh.

    Used when the day-ahead market hasn't published prices for the requested
    window yet (typically before 13:30 CET). Uses the past ``days`` of
    realised hourly means as a proxy.
    """
    records = _hourly_records(f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -{days}d)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')
    buckets = {}
    for t, v in records:
        if v is None:
            continue
        key = (t.weekday(), t.hour)
        buckets.setdefault(key, []).append(v)
    return {k: sum(vs) / len(vs) for k, vs in buckets.items() if vs}


def get_scheduler_data(load_kw, duration_h, lookahead_h=_DEFAULT_LOOKAHEAD_H,
                       use_forecast=True, use_baseline=True):
    """Compute cheapest start time for a load of ``load_kw`` over ``duration_h``.

    Args:
        load_kw: load power in kW (e.g. 5.0 for a 5 kW appliance).
        duration_h: total hours the load runs continuously.
        lookahead_h: how far ahead to scan (48h by default; OMIE publishes
            tomorrow's prices around 13:30 CET so less than 24h of tomorrow
            may be available early in the day).
        use_forecast: when True, fill hours past the OMIE horizon with the
            14-day historical mean for the same (weekday, hour). Windows
            that depend on forecasted hours are flagged ``forecast=True``
            so the UI can mark them visibly.
        use_baseline: when True, subtract the predicted facility baseline
            (from the consumption model) from the plant forecast before
            computing how much solar is available to this load. Without
            baseline subtraction solar is over-credited because the
            existing non-scheduled consumption is already eating some of it.

    Returns a dict with the full ranked schedule, best/worst entries, and
    metadata.  All monetary values are € including Art 99.2 IEE and IVA.
    """
    load_kw = float(load_kw)
    duration_h = int(duration_h)
    lookahead_h = int(lookahead_h)
    if not (_MIN_LOAD_KW <= load_kw <= _MAX_LOAD_KW):
        raise ValueError(f"load_kw must be in [{_MIN_LOAD_KW}, {_MAX_LOAD_KW}]")
    if not (_MIN_DURATION_H <= duration_h <= _MAX_DURATION_H):
        raise ValueError(f"duration_h must be in [{_MIN_DURATION_H}, {_MAX_DURATION_H}]")

    pricing = _load_pricing()
    tariff = _load_indexed_tariff()
    cf = tariff.get("contract_formula", {}) or {}
    margin = tariff.get("margin", 0.0)
    peajes = tariff.get("peajes", {})
    cargos = tariff.get("cargos", {})

    iee_per_kwh = pricing["taxes"].get("electricity_tax_eur_kwh")
    iee_pct = pricing["taxes"].get("electricity_tax_pct", 0.0) / 100.0
    iva_pct = pricing["taxes"].get("iva_pct", 21.0) / 100.0

    now = _cet_now().replace(minute=0, second=0, microsecond=0)
    range_start = now.isoformat()
    range_stop = (now + timedelta(hours=lookahead_h + duration_h + 1)).isoformat()

    omie_by_hour = _query_omie_hourly(range_start, range_stop)
    solar_by_hour = _hourly_solar_kw(get_solar_forecast())
    forecast_by_wh = _query_omie_recent_by_weekday_hour() if use_forecast else {}

    last_real_omie = max(omie_by_hour.keys()) if omie_by_hour else None

    # Optional baseline prediction from the consumption model
    baseline_by_hour = {}
    baseline_samples_total = 0
    if use_baseline:
        try:
            from consumption_model import predict_baseline
        except Exception:
            predict_baseline = None
        if predict_baseline is not None:
            for i in range(lookahead_h + duration_h):
                h = now + timedelta(hours=i)
                pred = predict_baseline(h)
                baseline_by_hour[h] = pred
                baseline_samples_total += pred.get("samples", 0)

    # Build hour-indexed arrays covering the scan window
    horizon = [now + timedelta(hours=i) for i in range(lookahead_h + duration_h)]
    rate_by_hour = {}
    is_forecast_hour = {}
    solar_kw_by_hour = {}
    baseline_kw_by_hour = {}
    spare_solar_by_hour = {}
    period_by_hour = {}
    for h in horizon:
        period = _get_period(h)
        period_by_hour[h] = period
        omie = omie_by_hour.get(h)
        if omie is not None:
            rate_by_hour[h] = _indexed_rate_for(omie, period, cf, margin, peajes, cargos)
            is_forecast_hour[h] = False
        elif use_forecast:
            fallback = forecast_by_wh.get((h.weekday(), h.hour))
            if fallback is not None:
                rate_by_hour[h] = _indexed_rate_for(
                    fallback, period, cf, margin, peajes, cargos
                )
                is_forecast_hour[h] = True
        plant_kw = solar_by_hour.get(h, 0.0)
        solar_kw_by_hour[h] = plant_kw
        base = 0.0
        if use_baseline and h in baseline_by_hour:
            base = baseline_by_hour[h].get("baseline_kw", 0.0) or 0.0
        baseline_kw_by_hour[h] = base
        spare_solar_by_hour[h] = max(0.0, plant_kw - base)

    # Score every start offset where we have rates (real OR forecast) for the
    # whole window. Flag the window as forecast-dependent if any of its hours
    # came from the historical fallback.
    results = []
    max_offset = max(0, lookahead_h)
    for offset in range(max_offset):
        start = now + timedelta(hours=offset)
        window = [start + timedelta(hours=i) for i in range(duration_h)]
        if not all(h in rate_by_hour for h in window):
            continue  # neither real OMIE nor forecast covers this window
        forecast_window = any(is_forecast_hour.get(h, False) for h in window)

        energy_cost = 0.0
        grid_kwh = 0.0
        solar_kwh = 0.0
        periods_seen = {}
        hours_detail = []
        for h in window:
            rate = rate_by_hour[h]
            # Solar available to THIS load = plant output minus facility baseline
            spare = spare_solar_by_hour.get(h, solar_kw_by_hour.get(h, 0.0))
            solar_kw = min(load_kw, spare)
            grid_kw = max(0.0, load_kw - solar_kw)
            grid_kwh += grid_kw  # 1-hour slots
            solar_kwh += solar_kw
            cost_h = grid_kw * rate
            energy_cost += cost_h
            p = period_by_hour[h]
            periods_seen[p] = periods_seen.get(p, 0) + 1
            hours_detail.append({
                "hour": h.isoformat(),
                "period": p,
                "rate_eur_kwh": round(rate, 5),
                "plant_kw": round(solar_kw_by_hour.get(h, 0.0), 2),
                "baseline_kw": round(baseline_kw_by_hour.get(h, 0.0), 2),
                "solar_kw": round(solar_kw, 2),
                "grid_kw": round(grid_kw, 2),
                "cost_eur": round(cost_h, 4),
            })

        # Apply Art 99.2 IEE (per-kWh, regardless of base) + IVA
        if iee_per_kwh is not None:
            iee = grid_kwh * iee_per_kwh
        else:
            iee = energy_cost * iee_pct
        total_with_iva = (energy_cost + iee) * (1 + iva_pct)

        dom_period = max(periods_seen, key=periods_seen.get) if periods_seen else "-"
        results.append({
            "start": start.isoformat(),
            "start_label": start.strftime("%a %d/%m %H:%M"),
            "end": (start + timedelta(hours=duration_h)).isoformat(),
            "total_cost_eur": round(total_with_iva, 2),
            "energy_cost_eur": round(energy_cost, 2),
            "iee_eur": round(iee, 2),
            "grid_kwh": round(grid_kwh, 1),
            "solar_kwh": round(solar_kwh, 1),
            "dominant_period": dom_period,
            "forecast": forecast_window,
            "hours": hours_detail,
        })

    real_count = sum(1 for r in results if not r["forecast"])
    forecast_count = sum(1 for r in results if r["forecast"])

    # Build full-horizon series for the visualization (one entry per hour)
    horizon_series = []
    for h in horizon[:lookahead_h]:
        horizon_series.append({
            "hour": h.isoformat(),
            "solar_kw": round(solar_kw_by_hour.get(h, 0.0), 2),
            "baseline_kw": round(baseline_kw_by_hour.get(h, 0.0), 2),
            "spare_solar_kw": round(spare_solar_by_hour.get(h, 0.0), 2),
            "rate_eur_kwh": round(rate_by_hour[h], 5) if h in rate_by_hour else None,
            "period": period_by_hour[h],
            "forecast": is_forecast_hour.get(h, False),
        })

    if not results:
        return {
            "load_kw": load_kw,
            "duration_h": duration_h,
            "lookahead_h": lookahead_h,
            "schedule": [],
            "best": None,
            "worst": None,
            "savings_vs_worst_eur": 0.0,
            "iva_included": True,
            "forecast_source": "open-meteo",
            "real_omie_count": 0,
            "forecast_omie_count": 0,
            "last_real_omie": last_real_omie.isoformat() if last_real_omie else None,
            "horizon": horizon_series,
            "message": "No OMIE data available for the requested window.",
        }

    costs = [r["total_cost_eur"] for r in results]
    best_idx = costs.index(min(costs))
    worst_idx = costs.index(max(costs))

    msg = None
    if real_count <= 1 and forecast_count == 0:
        msg = ("Només {} finestra avaluable amb OMIE real publicat. "
               "Demà OMIE es publica ~13:30 CET — torna a provar després.").format(real_count)
    elif forecast_count > 0:
        msg = ("{} finestres amb OMIE real, {} amb estimació històrica "
               "(mitjana 14d per dia/hora) — marcades com a previsió.").format(
            real_count, forecast_count)

    return {
        "load_kw": load_kw,
        "duration_h": duration_h,
        "lookahead_h": lookahead_h,
        "schedule": results,
        "best": results[best_idx],
        "worst": results[worst_idx],
        "savings_vs_worst_eur": round(costs[worst_idx] - costs[best_idx], 2),
        "iva_included": True,
        "forecast_source": "open-meteo",
        "real_omie_count": real_count,
        "forecast_omie_count": forecast_count,
        "last_real_omie": last_real_omie.isoformat() if last_real_omie else None,
        "baseline_samples": baseline_samples_total,
        "baseline_enabled": use_baseline,
        "horizon": horizon_series,
        "now": now.isoformat(),
        "message": msg,
    }
