#!/usr/bin/env python3
"""MJF Print Job Optimizer — find the cheapest 12-hour start time.

Queries real OMIE prices, KSEM import/export data from InfluxDB,
applies the full Som Energia Indexada 3.0TD tariff formula, and
simulates every possible start hour across all historical days.

Run inside Docker:
    docker compose exec dashboard python3 /app/tools/mjf_optimizer.py
"""

import json
import sys
import os
import math
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Ensure /app is on sys.path so we can import dashboard modules
# ---------------------------------------------------------------------------
sys.path.insert(0, "/app")

from config import INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET, PRICING_PATH
from data import _hourly_records, _get_period, _CET, _load_pricing
try:
    from consumption_model import predict_baseline
except Exception:
    predict_baseline = None

from influxdb_client import InfluxDBClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
JOB_HOURS = 12
MJF_POWER_KW = 18.0  # estimated average draw from nighttime import pattern

# Solar installation: inverters are throttled to match demand (not legalized yet)
PV_KWP = 65.0
PV_THROTTLE_KW = 40.0       # current inverter output cap
PV_LEGALIZED = False         # set True once legalized

# Fallback baseline (only used when consumption_model returns no samples)
_FALLBACK_BASELINE_DAY_KW = 4.0
_FALLBACK_BASELINE_NIGHT_KW = 1.5

# Hourly capacity factors for the site (42°N, south-facing, 30° tilt)
# Feb & Mar: MEASURED from actual inverter data (when not throttled)
# Other months: theoretical × 0.53 (weather scaling from Feb-Mar observations)
# These represent AVERAGE production including cloudy days, not clear-sky.
# Will be updated as more months of real data become available.
_WEATHER_SCALE = 0.53  # actual/theoretical ratio from Feb-Mar 2026 data
_PV_CAPACITY_FACTORS = {
    1: {7:0.01, 8:0.03, 9:0.10, 10:0.17, 11:0.22, 12:0.25, 13:0.25, 14:0.22, 15:0.17, 16:0.10, 17:0.03},
    2: {9:0.06, 10:0.17, 11:0.22, 12:0.30, 13:0.33, 14:0.32, 15:0.30, 16:0.22, 17:0.14, 18:0.05},  # MEASURED
    3: {8:0.03, 9:0.09, 10:0.15, 11:0.19, 12:0.22, 13:0.27, 14:0.28, 15:0.23, 16:0.22, 17:0.17, 18:0.07},  # MEASURED
    4: {7:0.02, 8:0.07, 9:0.16, 10:0.25, 11:0.33, 12:0.38, 13:0.40, 14:0.38, 15:0.33, 16:0.25, 17:0.17, 18:0.08, 19:0.02},
    5: {7:0.03, 8:0.10, 9:0.19, 10:0.28, 11:0.35, 12:0.40, 13:0.42, 14:0.41, 15:0.36, 16:0.29, 17:0.20, 18:0.12, 19:0.04},
    6: {7:0.04, 8:0.11, 9:0.20, 10:0.29, 11:0.36, 12:0.41, 13:0.43, 14:0.42, 15:0.38, 16:0.31, 17:0.22, 18:0.13, 19:0.05},
    7: {7:0.04, 8:0.11, 9:0.20, 10:0.29, 11:0.36, 12:0.41, 13:0.43, 14:0.42, 15:0.38, 16:0.31, 17:0.22, 18:0.13, 19:0.05},
    8: {7:0.03, 8:0.10, 9:0.19, 10:0.28, 11:0.35, 12:0.40, 13:0.42, 14:0.41, 15:0.36, 16:0.29, 17:0.20, 18:0.12, 19:0.04},
    9: {7:0.02, 8:0.07, 9:0.16, 10:0.25, 11:0.33, 12:0.38, 13:0.40, 14:0.38, 15:0.33, 16:0.25, 17:0.17, 18:0.08, 19:0.02},
    10: {8:0.03, 9:0.09, 10:0.15, 11:0.19, 12:0.22, 13:0.27, 14:0.28, 15:0.23, 16:0.22, 17:0.17, 18:0.07},
    11: {9:0.06, 10:0.17, 11:0.22, 12:0.30, 13:0.33, 14:0.32, 15:0.30, 16:0.22, 17:0.14, 18:0.05},
    12: {7:0.01, 8:0.03, 9:0.10, 10:0.17, 11:0.22, 12:0.25, 13:0.25, 14:0.22, 15:0.17, 16:0.10, 17:0.03},
}

# ---------------------------------------------------------------------------
# InfluxDB client (reuse same connection style as data.py)
# ---------------------------------------------------------------------------
_client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
_query_api = _client.query_api()


def _q(flux):
    try:
        return _query_api.query(flux)
    except Exception as e:
        print(f"  [WARN] Query error: {e}", file=sys.stderr)
        return []


def _records(flux):
    """Return list of (datetime_CET, float) from a Flux query."""
    out = []
    for table in _q(flux):
        for rec in table.records:
            t = rec.get_time()
            v = rec.get_value()
            if t is not None and v is not None:
                t_cet = t.astimezone(_CET)
                out.append((t_cet, float(v)))
    return out


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_omie_hourly():
    """OMIE hourly prices in EUR/kWh, keyed by hour-truncated CET datetime."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -1y)
  |> filter(fn: (r) => r._measurement == "omie_prices" and r._field == "price_eur_kwh")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
'''
    data = {}
    for t, v in _records(flux):
        key = t.replace(minute=0, second=0, microsecond=0)
        data[key] = v
    print(f"  Loaded {len(data)} OMIE hourly price records")
    return data


def load_ksem_import_hourly():
    """Hourly KSEM import (kWh) via spread, keyed by hour-truncated CET."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -1y)
  |> filter(fn: (r) => r._measurement == "ksem" and r._field == "energy_import_total")
  |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
'''
    data = {}
    for t, v in _records(flux):
        key = t.replace(minute=0, second=0, microsecond=0)
        if v >= 0:
            data[key] = v
    print(f"  Loaded {len(data)} KSEM import hourly records")
    return data


def load_ksem_export_hourly():
    """Hourly KSEM export (kWh) via spread, keyed by hour-truncated CET."""
    flux = f'''
from(bucket: "{INFLUXDB_BUCKET}")
  |> range(start: -1y)
  |> filter(fn: (r) => r._measurement == "ksem" and r._field == "energy_export_total")
  |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
'''
    data = {}
    for t, v in _records(flux):
        key = t.replace(minute=0, second=0, microsecond=0)
        if v >= 0:
            data[key] = v
    print(f"  Loaded {len(data)} KSEM export hourly records")
    return data


# ---------------------------------------------------------------------------
# Tariff computation
# ---------------------------------------------------------------------------
def load_tariff_params():
    """Load all tariff parameters from pricing.json."""
    pricing = _load_pricing()
    it = pricing["indexed_tariff"]
    cf = pricing.get("energy", {}).get("contract_formula", {})
    cal = pricing.get("ksem_calibration", {}).get("overall", 1.115)
    taxes = pricing.get("taxes", {})
    return {
        "peajes": it["peajes_eur_kwh"],
        "cargos": it["cargos_eur_kwh"],
        "cf_mult": cf.get("adjustment_multiplier", 1.015),
        "cf_other": cf.get("other_costs_eur_kwh", 0.046),
        "cf_losses": cf.get("loss_coefficient", 0.12),
        "cf_fe": cf.get("efficiency_fund_eur_kwh", 0.001),
        "cf_margin": cf.get("margin_eur_kwh", 0.00968),
        "ksem_cal": cal,
        "iee_eur_kwh": taxes.get("electricity_tax_eur_kwh"),  # Art 99.2; None → %
        "iee_pct": taxes.get("electricity_tax_pct", 5.11269) / 100.0,
        "iva_pct": taxes.get("iva_pct", 21.0) / 100.0,
    }


# ---------------------------------------------------------------------------
# Baseline lookup — uses consumption_model when available
# ---------------------------------------------------------------------------
_baseline_cache = {}


def _baseline_kw(dt):
    """Return predicted facility baseline (kW) for the given CET datetime.

    Uses ``consumption_model.predict_baseline`` — median of similar days
    (weekday/weekend × holiday × month × hour). Falls back to a rough
    day/night constant if the model returns no samples yet.
    """
    key = (dt.weekday() >= 5, dt.month, dt.hour,
           dt.replace(tzinfo=None).date())
    # cache per (weekday-bucket, hour) lookup within a single run
    ck = (dt.weekday() >= 5, dt.month, dt.hour)
    if ck in _baseline_cache:
        return _baseline_cache[ck]
    val = None
    if predict_baseline is not None:
        try:
            pred = predict_baseline(dt)
            if pred and pred.get("samples", 0) > 0:
                val = pred["baseline_kw"]
        except Exception:
            val = None
    if val is None:
        val = _FALLBACK_BASELINE_DAY_KW if 8 <= dt.hour < 18 else _FALLBACK_BASELINE_NIGHT_KW
    _baseline_cache[ck] = val
    return val


def indexed_rate(omie_eur_kwh, period, tariff):
    """Compute the full indexed energy rate (EUR/kWh) for one hour.

    PH = mult * [(OMIE + other) * (1 + losses) + FE + margin] + peaje + cargo
    """
    base = (omie_eur_kwh + tariff["cf_other"]) * (1 + tariff["cf_losses"])
    base += tariff["cf_fe"] + tariff["cf_margin"]
    ph = tariff["cf_mult"] * base
    ph += tariff["peajes"].get(period, 0.000031)
    ph += tariff["cargos"].get(period, 0.002867)
    return ph


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------
def get_unique_days(omie_data):
    """Return sorted list of unique date objects from OMIE data."""
    days = set()
    for t in omie_data:
        days.add(t.date())
    return sorted(days)


def _sun_capacity_kw(month, hour):
    """Return estimated sun capacity (kW) for a given month and hour."""
    cf = _PV_CAPACITY_FACTORS.get(month, {}).get(hour, 0.0)
    return PV_KWP * cf


def simulate_job(start_hour, day, omie_data, export_data, tariff):
    """Simulate a 12-hour job starting at start_hour on the given day.

    Uses demand-responsive inverter model: PV output ramps up when MJF
    adds load, up to min(sun_capacity, throttle_cap). The "other loads"
    term comes from the consumption model's historical baseline rather
    than a static day/night constant.

    Cost is the incremental invoice cost attributable to MJF:
      incremental_grid_kwh × indexed_rate(hour)
      + incremental_grid_kwh × IEE  (Art 99.2: 0.001 €/kWh)
      then × (1 + IVA)

    Returns (total_cost_eur_with_taxes, total_grid_kwh) or None if data incomplete.
    """
    total_cost_pretax = 0.0
    total_grid_kwh = 0.0
    hours_found = 0
    mjf_kwh = MJF_POWER_KW * tariff["ksem_cal"]
    throttle = PV_THROTTLE_KW if not PV_LEGALIZED else None

    for offset in range(JOB_HOURS):
        hour = (start_hour + offset) % 24
        day_offset = (start_hour + offset) // 24
        actual_day = day + timedelta(days=day_offset)
        t = datetime(actual_day.year, actual_day.month, actual_day.day,
                     hour, 0, 0, tzinfo=_CET)

        omie_price = omie_data.get(t)
        if omie_price is None:
            return None

        period = _get_period(t)
        rate = indexed_rate(omie_price, period, tariff)

        # Demand-responsive solar model
        sun_kw = _sun_capacity_kw(actual_day.month, hour)
        other = _baseline_kw(t)
        total_demand = other + mjf_kwh

        # Inverter produces min(demand, sun, throttle)
        cap = min(sun_kw, throttle) if throttle else sun_kw
        pv_with = min(total_demand, cap)
        grid_with = max(0, total_demand - pv_with)

        # Without MJF
        pv_without = min(other, cap)
        grid_without = max(0, other - pv_without)

        # MJF's grid cost = incremental grid import
        mjf_grid = grid_with - grid_without
        total_cost_pretax += mjf_grid * rate
        total_grid_kwh += mjf_grid
        hours_found += 1

    if hours_found < JOB_HOURS:
        return None

    # Apply Art 99.2 IEE (per-kWh on incremental import) + 21 % IVA so that the
    # reported cost matches what the customer sees on the final invoice.
    iee_per_kwh = tariff["iee_eur_kwh"]
    if iee_per_kwh is not None:
        iee = total_grid_kwh * iee_per_kwh
    else:
        iee = total_cost_pretax * tariff["iee_pct"]
    total_cost = (total_cost_pretax + iee) * (1 + tariff["iva_pct"])

    return (total_cost, total_grid_kwh)


def run_simulation():
    """Run the full simulation and print results."""
    print("=" * 72)
    print("  MJF 12-HOUR PRINT JOB OPTIMIZER")
    print("  Optimal Start Time Analysis (Som Energia Indexada 3.0TD)")
    print("=" * 72)
    print()

    # Load data
    print("[1/4] Loading data from InfluxDB...")
    omie_data = load_omie_hourly()
    export_data = {}  # no longer used — demand-responsive solar model instead
    tariff = load_tariff_params()

    if not omie_data:
        print("ERROR: No OMIE data found. Exiting.")
        sys.exit(1)

    print(f"\n  KSEM calibration factor: {tariff['ksem_cal']:.4f}")
    print(f"  MJF power draw: {MJF_POWER_KW} kW (raw)")
    print(f"  MJF calibrated draw: {MJF_POWER_KW * tariff['ksem_cal']:.1f} kWh/h (meter-equivalent)")
    print(f"  Solar model: demand-responsive, {PV_KWP} kWp, throttle {PV_THROTTLE_KW} kW")
    print(f"  Other loads: day={OTHER_LOADS_DAY_KW} kWh/h, night={OTHER_LOADS_NIGHT_KW} kWh/h")

    # Identify days
    all_days = get_unique_days(omie_data)
    # Only use days where we have full 24h of OMIE data
    complete_days = []
    for d in all_days:
        hours_present = sum(1 for h in range(24)
                           if datetime(d.year, d.month, d.day, h, 0, 0, tzinfo=_CET) in omie_data)
        if hours_present >= 24:
            complete_days.append(d)

    weekdays = [d for d in complete_days if d.weekday() < 5]
    weekends = [d for d in complete_days if d.weekday() >= 5]

    print(f"\n[2/4] Dataset summary:")
    print(f"  Date range: {complete_days[0]} to {complete_days[-1]}")
    print(f"  Complete days: {len(complete_days)} ({len(weekdays)} weekdays, {len(weekends)} weekends)")

    # Run simulation for each start hour
    print(f"\n[3/4] Simulating all 24 start hours across {len(complete_days)} days...")

    # Results: start_hour -> list of (cost, grid_kwh, day, is_weekend)
    results_weekday = defaultdict(list)
    results_weekend = defaultdict(list)
    results_all = defaultdict(list)

    for day in complete_days:
        is_weekend = day.weekday() >= 5
        for start_hour in range(24):
            result = simulate_job(start_hour, day, omie_data, export_data, tariff)
            if result is not None:
                cost, grid_kwh = result
                entry = {"cost": cost, "grid_kwh": grid_kwh, "day": day}
                results_all[start_hour].append(entry)
                if is_weekend:
                    results_weekend[start_hour].append(entry)
                else:
                    results_weekday[start_hour].append(entry)

    # ---------------------------------------------------------------------------
    # Analysis & output
    # ---------------------------------------------------------------------------
    print(f"\n[4/4] Computing statistics...\n")

    def compute_stats(results_dict):
        """Compute statistics for each start hour."""
        stats = {}
        for hour in range(24):
            entries = results_dict.get(hour, [])
            if not entries:
                continue
            costs = [e["cost"] for e in entries]
            n = len(costs)
            mean = sum(costs) / n
            variance = sum((c - mean) ** 2 for c in costs) / n if n > 1 else 0
            std = math.sqrt(variance)
            costs_sorted = sorted(costs)
            p5 = costs_sorted[max(0, int(n * 0.05))]
            p25 = costs_sorted[max(0, int(n * 0.25))]
            median = costs_sorted[n // 2]
            p75 = costs_sorted[max(0, int(n * 0.75))]
            p95 = costs_sorted[max(0, min(n - 1, int(n * 0.95)))]
            worst = costs_sorted[-1]
            best = costs_sorted[0]
            avg_grid = sum(e["grid_kwh"] for e in entries) / n
            # 95% confidence interval for the mean
            ci_margin = 1.96 * std / math.sqrt(n) if n > 1 else 0
            stats[hour] = {
                "n": n, "mean": mean, "std": std, "median": median,
                "p5": p5, "p25": p25, "p75": p75, "p95": p95,
                "best": best, "worst": worst, "avg_grid_kwh": avg_grid,
                "ci_low": mean - ci_margin, "ci_high": mean + ci_margin,
            }
        return stats

    def count_wins(results_dict):
        """Count how many days each start hour would have been cheapest."""
        # Group by day
        days_data = defaultdict(dict)
        for hour, entries in results_dict.items():
            for e in entries:
                days_data[e["day"]][hour] = e["cost"]
        wins = defaultdict(int)
        for day, hour_costs in days_data.items():
            if hour_costs:
                best_hour = min(hour_costs, key=hour_costs.get)
                wins[best_hour] += 1
        return wins, len(days_data)

    def print_table(stats, title, wins, total_days):
        """Print a formatted results table."""
        if not stats:
            print(f"  No data available for {title}\n")
            return

        print("=" * 72)
        print(f"  {title}")
        print("=" * 72)

        # Find optimal
        optimal_hour = min(stats, key=lambda h: stats[h]["mean"])
        print(f"\n  >>> OPTIMAL START: {optimal_hour:02d}:00  "
              f"(avg cost EUR {stats[optimal_hour]['mean']:.2f}/job) <<<\n")

        # Main table
        print(f"  {'Start':>5}  {'Avg EUR':>8}  {'Median':>7}  {'Std':>6}  "
              f"{'P5':>7}  {'P95':>7}  {'95% CI':>15}  "
              f"{'Grid kWh':>9}  {'Wins':>5}  {'Win%':>5}")
        print(f"  {'─' * 5}  {'─' * 8}  {'─' * 7}  {'─' * 6}  "
              f"{'─' * 7}  {'─' * 7}  {'─' * 15}  "
              f"{'─' * 9}  {'─' * 5}  {'─' * 5}")

        for hour in range(24):
            if hour not in stats:
                continue
            s = stats[hour]
            w = wins.get(hour, 0)
            w_pct = (w / total_days * 100) if total_days > 0 else 0
            marker = " ***" if hour == optimal_hour else ""
            ci_str = f"[{s['ci_low']:.2f}-{s['ci_high']:.2f}]"
            print(f"  {hour:02d}:00  {s['mean']:8.2f}  {s['median']:7.2f}  "
                  f"{s['std']:6.2f}  {s['p5']:7.2f}  {s['p95']:7.2f}  "
                  f"{ci_str:>15}  {s['avg_grid_kwh']:9.1f}  "
                  f"{w:5d}  {w_pct:4.1f}%{marker}")

        print()

    # Compute and print for each category
    stats_weekday = compute_stats(results_weekday)
    stats_weekend = compute_stats(results_weekend)
    stats_all = compute_stats(results_all)

    wins_wd, total_wd = count_wins(results_weekday)
    wins_we, total_we = count_wins(results_weekend)
    wins_all, total_all = count_wins(results_all)

    print_table(stats_all, "ALL DAYS", wins_all, total_all)
    print_table(stats_weekday, "WEEKDAYS (Mon-Fri)", wins_wd, total_wd)
    print_table(stats_weekend, "WEEKENDS (Sat-Sun) — always P6 periods", wins_we, total_we)

    # ---------------------------------------------------------------------------
    # Comparison table: current (22:00) vs optimal vs midnight (00:00)
    # ---------------------------------------------------------------------------
    def print_comparison(stats, title):
        if not stats:
            return
        optimal_hour = min(stats, key=lambda h: stats[h]["mean"])
        compare_hours = {
            "Current (22:00)": 22,
            f"Optimal ({optimal_hour:02d}:00)": optimal_hour,
            "Midnight (00:00)": 0,
        }
        # Deduplicate if optimal is one of the others
        seen = set()
        unique_compare = []
        for label, h in compare_hours.items():
            if h not in seen:
                unique_compare.append((label, h))
                seen.add(h)

        print(f"  COMPARISON — {title}")
        print(f"  {'─' * 60}")
        print(f"  {'Scenario':<25} {'Avg EUR':>8} {'Median':>8} {'P95':>8} {'vs Current':>11}")
        print(f"  {'─' * 25} {'─' * 8} {'─' * 8} {'─' * 8} {'─' * 11}")

        current_mean = stats.get(22, {}).get("mean", 0)
        for label, h in unique_compare:
            if h not in stats:
                continue
            s = stats[h]
            diff = s["mean"] - current_mean
            diff_str = f"{diff:+.2f}" if h != 22 else "baseline"
            print(f"  {label:<25} {s['mean']:8.2f} {s['median']:8.2f} "
                  f"{s['p95']:8.2f} {diff_str:>11}")

        if current_mean > 0 and optimal_hour != 22:
            savings = current_mean - stats[optimal_hour]["mean"]
            pct = savings / current_mean * 100
            print(f"\n  Savings per job: EUR {savings:.2f} ({pct:.1f}%)")
            print(f"  At ~20 jobs/month: EUR {savings * 20:.0f}/month")

        print()

    print("=" * 72)
    print("  KEY COMPARISONS")
    print("=" * 72)
    print()

    print_comparison(stats_all, "All days")
    print_comparison(stats_weekday, "Weekdays")
    print_comparison(stats_weekend, "Weekends")

    # ---------------------------------------------------------------------------
    # Monthly breakdown
    # ---------------------------------------------------------------------------
    print("=" * 72)
    print("  MONTHLY BREAKDOWN (average cost per job)")
    print("=" * 72)

    months_data = defaultdict(lambda: defaultdict(list))
    for hour, entries in results_all.items():
        for e in entries:
            month_key = e["day"].strftime("%Y-%m")
            months_data[month_key][hour].append(e["cost"])

    if months_data:
        # Get candidate hours to show
        optimal_all = min(stats_all, key=lambda h: stats_all[h]["mean"]) if stats_all else 0
        show_hours = sorted(set([0, 8, 12, 14, 16, 20, 22, optimal_all]))

        header = f"\n  {'Month':>7}"
        for h in show_hours:
            label = f"{h:02d}:00"
            if h == optimal_all:
                label += "*"
            header += f"  {label:>8}"
        header += f"  {'Best':>8}"
        print(header)
        print(f"  {'─' * 7}" + f"  {'─' * 8}" * (len(show_hours) + 1))

        for month_key in sorted(months_data.keys()):
            mdata = months_data[month_key]
            row = f"  {month_key:>7}"
            month_means = {}
            for h in range(24):
                if h in mdata and mdata[h]:
                    month_means[h] = sum(mdata[h]) / len(mdata[h])
            for h in show_hours:
                if h in month_means:
                    row += f"  {month_means[h]:8.2f}"
                else:
                    row += f"  {'n/a':>8}"
            if month_means:
                best_h = min(month_means, key=month_means.get)
                row += f"  {best_h:02d}:00"
            print(row)

        print(f"\n  * = overall optimal start hour")

    # ---------------------------------------------------------------------------
    # Distribution: how often each hour wins
    # ---------------------------------------------------------------------------
    print(f"\n{'=' * 72}")
    print(f"  WIN DISTRIBUTION (how often each start hour is cheapest)")
    print(f"{'=' * 72}")

    def print_win_chart(wins, total, title):
        if not wins or total == 0:
            return
        print(f"\n  {title} ({total} days):")
        max_wins = max(wins.values()) if wins else 1
        for hour in range(24):
            w = wins.get(hour, 0)
            pct = w / total * 100
            bar_len = int(w / max_wins * 30) if max_wins > 0 else 0
            bar = "#" * bar_len
            if w > 0:
                print(f"  {hour:02d}:00  {bar:<30}  {w:3d} days ({pct:4.1f}%)")

    print_win_chart(wins_all, total_all, "All days")
    print_win_chart(wins_wd, total_wd, "Weekdays")
    print_win_chart(wins_we, total_we, "Weekends")

    print(f"\n{'=' * 72}")
    print(f"  END OF REPORT")
    print(f"{'=' * 72}")


def get_optimizer_data():
    """Return optimizer results as a JSON-serializable dict for the dashboard."""
    omie_data = load_omie_hourly()
    export_data = {}  # demand-responsive solar model used instead
    tariff = load_tariff_params()

    if not omie_data:
        return {"error": "No OMIE data"}

    all_days = get_unique_days(omie_data)
    complete_days = [
        d for d in all_days
        if sum(1 for h in range(24)
               if datetime(d.year, d.month, d.day, h, 0, 0, tzinfo=_CET) in omie_data) >= 24
    ]
    weekdays = [d for d in complete_days if d.weekday() < 5]
    weekends = [d for d in complete_days if d.weekday() >= 5]

    results_weekday = defaultdict(list)
    results_weekend = defaultdict(list)
    results_all = defaultdict(list)

    for day in complete_days:
        is_weekend = day.weekday() >= 5
        for start_hour in range(24):
            result = simulate_job(start_hour, day, omie_data, export_data, tariff)
            if result is not None:
                cost, grid_kwh = result
                entry = {"cost": cost, "grid_kwh": grid_kwh, "day": day}
                results_all[start_hour].append(entry)
                if is_weekend:
                    results_weekend[start_hour].append(entry)
                else:
                    results_weekday[start_hour].append(entry)

    def _stats(results_dict):
        stats = {}
        for hour in range(24):
            entries = results_dict.get(hour, [])
            if not entries:
                stats[hour] = None
                continue
            costs = [e["cost"] for e in entries]
            n = len(costs)
            mean = sum(costs) / n
            variance = sum((c - mean) ** 2 for c in costs) / n if n > 1 else 0
            std = math.sqrt(variance)
            cs = sorted(costs)
            ci = 1.96 * std / math.sqrt(n) if n > 1 else 0
            stats[hour] = {
                "n": n, "mean": round(mean, 2), "std": round(std, 2),
                "median": round(cs[n // 2], 2),
                "p5": round(cs[max(0, int(n * 0.05))], 2),
                "p95": round(cs[max(0, min(n - 1, int(n * 0.95)))], 2),
                "ci_low": round(mean - ci, 2), "ci_high": round(mean + ci, 2),
                "avg_grid_kwh": round(sum(e["grid_kwh"] for e in entries) / n, 1),
            }
        return stats

    def _wins(results_dict):
        days_data = defaultdict(dict)
        for hour, entries in results_dict.items():
            for e in entries:
                days_data[e["day"]][hour] = e["cost"]
        wins = defaultdict(int)
        for day, hc in days_data.items():
            if hc:
                wins[min(hc, key=hc.get)] += 1
        return {h: w for h, w in wins.items()}, len(days_data)

    def _optimal(stats):
        valid = {h: s for h, s in stats.items() if s is not None}
        return min(valid, key=lambda h: valid[h]["mean"]) if valid else 0

    s_all = _stats(results_all)
    s_wd = _stats(results_weekday)
    s_we = _stats(results_weekend)
    w_all, t_all = _wins(results_all)
    w_wd, t_wd = _wins(results_weekday)
    w_we, t_we = _wins(results_weekend)

    # Monthly breakdown
    months = defaultdict(lambda: defaultdict(list))
    for hour, entries in results_all.items():
        for e in entries:
            months[e["day"].strftime("%Y-%m")][hour].append(e["cost"])
    monthly = {}
    for mk in sorted(months):
        mm = {}
        for h in range(24):
            vals = months[mk].get(h, [])
            mm[h] = round(sum(vals) / len(vals), 2) if vals else None
        best = min((h for h in mm if mm[h] is not None), key=lambda h: mm[h])
        monthly[mk] = {"means": mm, "best": best}

    return {
        "date_range": [str(complete_days[0]), str(complete_days[-1])],
        "total_days": len(complete_days),
        "weekdays": len(weekdays),
        "weekends": len(weekends),
        "mjf_kw": MJF_POWER_KW,
        "cal_factor": tariff["ksem_cal"],
        "stats_all": {str(h): v for h, v in s_all.items()},
        "stats_weekday": {str(h): v for h, v in s_wd.items()},
        "stats_weekend": {str(h): v for h, v in s_we.items()},
        "wins_all": {str(h): v for h, v in w_all.items()},
        "wins_weekday": {str(h): v for h, v in w_wd.items()},
        "wins_weekend": {str(h): v for h, v in w_we.items()},
        "total_days_all": t_all,
        "total_days_wd": t_wd,
        "total_days_we": t_we,
        "optimal_all": _optimal(s_all),
        "optimal_weekday": _optimal(s_wd),
        "optimal_weekend": _optimal(s_we),
        "monthly": monthly,
    }


if __name__ == "__main__":
    run_simulation()
