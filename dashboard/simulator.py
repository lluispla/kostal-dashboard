"""Estadístiques — 4-scenario tariff comparison using real metered data.

Compares four tariffs using actual hourly import/export from KSEM + OMIE
spot prices from InfluxDB:
  1. Iberdrola Pla Estable (flat rate)
  2. Holaluz Fix (per-period fixed)
  3. Som Energia Períodes (per-period fixed)
  4. Som Energia Indexada + Flux Solar (OMIE-indexed + virtual battery)

The 3.0TD period mapping follows the correct monthly-rotating scheme from the
official regulation (BOE).
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from config import INFLUXDB_BUCKET, PRICING_PATH
from data import _hourly_records, _q, _CET

# ---------------------------------------------------------------------------
# 3.0TD correct rotating period mapping (from binomi calculator.py)
# ---------------------------------------------------------------------------

PEAK_HOURS = {10, 11, 12, 13, 18, 19, 20, 21}
SHOULDER_HOURS = {8, 9, 14, 15, 16, 17, 22, 23}

# month -> (peak_period, shoulder_period); night + weekends = P6
MONTH_TO_PERIODS = {
    1:  ("P1", "P2"),  2:  ("P1", "P2"),   # Group A
    7:  ("P1", "P2"),  12: ("P1", "P2"),
    3:  ("P2", "P3"),  11: ("P2", "P3"),    # Group B
    6:  ("P3", "P4"),  8:  ("P3", "P4"),    # Group C
    9:  ("P3", "P4"),
    4:  ("P4", "P5"),  5:  ("P4", "P5"),    # Group D
    10: ("P4", "P5"),
}


def get_period(hour, month, is_weekend):
    """Return 3.0TD period (P1-P6) with correct monthly rotation."""
    if is_weekend:
        return "P6"
    if hour in PEAK_HOURS:
        return MONTH_TO_PERIODS[month][0]
    if hour in SHOULDER_HOURS:
        return MONTH_TO_PERIODS[month][1]
    return "P6"  # night (0-8h)


# ---------------------------------------------------------------------------
# Pricing loader
# ---------------------------------------------------------------------------

def _load_pricing():
    """Load pricing.json."""
    with open(PRICING_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# InfluxDB queries — reuse patterns from comparador.py
# ---------------------------------------------------------------------------

_MAX_KWH_PER_HOUR = 200  # filter counter-jump artefacts


def _range_spec(time_range):
    """Convert user-facing range to Flux range string and month count."""
    mapping = {
        "3m": ("-3mo", 3),
        "6m": ("-6mo", 6),
        "1y": ("-1y", 12),
        "all": ("0", 60),
    }
    return mapping.get(time_range, ("-3mo", 3))


def _query_hourly_import(flux_range):
    """Hourly import kWh from KSEM counter spread."""
    bucket = INFLUXDB_BUCKET
    hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')
    return [(t, kwh) for t, kwh in hours if kwh <= _MAX_KWH_PER_HOUR]


def _query_hourly_export(flux_range):
    """Hourly export kWh from KSEM counter spread."""
    bucket = INFLUXDB_BUCKET
    hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')
    return [(t, kwh) for t, kwh in hours if kwh <= _MAX_KWH_PER_HOUR]


def _query_hourly_omie(flux_range):
    """Hourly OMIE price (EUR/kWh) from InfluxDB."""
    bucket = INFLUXDB_BUCKET
    return _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: {flux_range})
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def simulate(time_range="3m"):
    """Run 3-scenario comparison over the requested time range.

    Returns dict with monthly breakdown, annual totals, breakeven, and chart data.
    """
    pricing = _load_pricing()
    flux_range, _ = _range_spec(time_range)

    # Query all hourly data
    import_hours = _query_hourly_import(flux_range)
    export_hours = _query_hourly_export(flux_range)
    omie_hours = _query_hourly_omie(flux_range)

    # Build lookup dicts keyed by hour (truncated)
    def _hour_key(t):
        return t.replace(minute=0, second=0, microsecond=0)

    import_by_hour = {}
    for t, kwh in import_hours:
        import_by_hour[_hour_key(t)] = kwh

    export_by_hour = {}
    for t, kwh in export_hours:
        export_by_hour[_hour_key(t)] = kwh

    omie_by_hour = {}
    for t, price in omie_hours:
        omie_by_hour[_hour_key(t)] = price

    # Scenario parameters from pricing.json
    scenarios = pricing.get("scenarios", {})
    sc_iber = scenarios.get("iberdrola", {})
    sc_hola = scenarios.get("holaluz", {})
    sc_sper = scenarios.get("som_periodes", {})
    sc_sidx = scenarios.get("som_indexada", {})

    iber_rates = sc_iber.get("energy_eur_kwh", {f"P{i}": 0.153962 for i in range(1, 7)})
    iber_surplus = sc_iber.get("surplus_eur_kwh", 0.05)

    hola_rates = sc_hola.get("energy_eur_kwh", {})
    hola_surplus = sc_hola.get("surplus_eur_kwh", 0.05)

    sper_rates = sc_sper.get("energy_eur_kwh", {})
    sper_surplus = sc_sper.get("surplus_eur_kwh", 0.03)

    idx_margin = sc_sidx.get("margin_eur_kwh", 0.009680)
    peajes = pricing["indexed_tariff"]["peajes_eur_kwh"]
    cargos = pricing["indexed_tariff"]["cargos_eur_kwh"]

    # Full contract formula: PH = mult × [(OMIE + other) × (1 + losses) + FE + margin] + PTD + CA
    cf = sc_sidx.get("contract_formula", {})
    cf_mult = cf.get("adjustment_multiplier", 1.0)
    cf_other = cf.get("other_costs_eur_kwh", 0.0)
    cf_losses = cf.get("loss_coefficient", 0.0)
    cf_fe = cf.get("efficiency_fund_eur_kwh", 0.0)

    flux_cfg = sc_sidx.get("flux_solar", {})
    flux_enabled = flux_cfg.get("enabled", True)
    flux_credit_pct = flux_cfg.get("credit_pct", 0.80)

    # Shared parameters
    contracted_power = pricing["contracted_power_kw"]
    elec_tax_pct = pricing["taxes"]["electricity_tax_pct"]
    iva_pct = pricing["taxes"]["iva_pct"]
    equipment_rental = pricing["fixed_charges_eur_day"]["equipment_rental"]
    bono_social = pricing["fixed_charges_eur_day"]["bono_social"]

    # Power charges per scenario
    iber_pwr = sc_iber.get("power_charges_eur_kw_day", pricing["power_charges_eur_kw_day"])
    hola_pwr = sc_hola.get("power_charges_eur_kw_day", pricing["power_charges_eur_kw_day"])
    sper_pwr_year = sc_sper.get("power_charges_eur_kw_year", {})
    sidx_pwr_year = sc_sidx.get("power_charges_eur_kw_year", sper_pwr_year)

    # --- Hourly computation ---
    monthly_data = defaultdict(lambda: {
        "energy_iber": 0.0,
        "energy_hola": 0.0,
        "energy_sper": 0.0,
        "energy_sidx": 0.0,
        "surplus_iber": 0.0,
        "surplus_hola": 0.0,
        "surplus_sper": 0.0,
        "surplus_sidx": 0.0,
        "import_kwh": 0.0,
        "export_kwh": 0.0,
        "omie_sum": 0.0,
        "omie_count": 0,
        "dates": set(),
    })

    all_hours = sorted(set(import_by_hour.keys()) | set(omie_by_hour.keys()))
    for hour in all_hours:
        imp_kwh = import_by_hour.get(hour, 0.0)
        exp_kwh = export_by_hour.get(hour, 0.0)
        omie_price = omie_by_hour.get(hour, None)

        month_num = hour.month
        is_weekend = hour.weekday() >= 5
        period = get_period(hour.hour, month_num, is_weekend)
        ym = hour.strftime("%Y-%m")

        md = monthly_data[ym]
        md["dates"].add(hour.date())

        if omie_price is not None:
            md["omie_sum"] += omie_price * 1000  # accumulate EUR/MWh
            md["omie_count"] += 1

        if imp_kwh > 0 and omie_price is not None:
            # Scenario 1: Iberdrola flat
            md["energy_iber"] += imp_kwh * iber_rates.get(period, 0.153962)
            # Scenario 2: Holaluz per-period
            md["energy_hola"] += imp_kwh * hola_rates.get(period, 0.14)
            # Scenario 3: Som Períodes
            md["energy_sper"] += imp_kwh * sper_rates.get(period, 0.13)
            # Scenario 4: Som Indexada — full contract formula (clause 2b):
            # PH = mult × [(OMIE + other_costs) × (1 + losses) + FE + margin] + PTD + CA
            inner = (omie_price + cf_other) * (1 + cf_losses) + cf_fe + idx_margin
            ph = cf_mult * inner + peajes[period] + cargos[period]
            md["energy_sidx"] += imp_kwh * ph
            md["import_kwh"] += imp_kwh

        if exp_kwh > 0:
            # Surplus valuation per scenario
            md["surplus_iber"] += exp_kwh * iber_surplus
            md["surplus_hola"] += exp_kwh * hola_surplus
            md["surplus_sper"] += exp_kwh * sper_surplus
            if omie_price is not None:
                md["surplus_sidx"] += exp_kwh * omie_price
            md["export_kwh"] += exp_kwh

    # Add remaining export hours not in all_hours
    for hour in sorted(export_by_hour.keys()):
        ym = hour.strftime("%Y-%m")
        md = monthly_data[ym]
        if hour not in set(import_by_hour.keys()) and hour not in set(omie_by_hour.keys()):
            exp_kwh = export_by_hour[hour]
            md["export_kwh"] += exp_kwh
            md["surplus_iber"] += exp_kwh * iber_surplus
            md["surplus_hola"] += exp_kwh * hola_surplus
            md["surplus_sper"] += exp_kwh * sper_surplus
            md["dates"].add(hour.date())

    # --- Power cost helpers ---
    def _power_cost_daily(pwr_day, days):
        """EUR/kW/day * kW * days (Iberdrola, Holaluz)."""
        cost = 0.0
        for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            kw = contracted_power.get(p, 69)
            rate = pwr_day.get(p, 0)
            cost += kw * rate * days
        return cost

    def _power_cost_som(pwr_year, days):
        """Som Energia: EUR/kW/year ÷ 365 * kW * days."""
        cost = 0.0
        for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            kw = contracted_power.get(p, 69)
            rate = pwr_year.get(p, 0)
            cost += kw * (rate / 365) * days
        return cost

    # --- Bill computation per Spanish regulation ---
    def _compute_bill(energy_cost, surplus_value, power_cost, days):
        """Compute bill following Spanish regulation.

        1. compensated = min(energy_term, surplus_value) — surplus can't reduce energy below 0
        2. subtotal = (energy - compensated) + power_cost
        3. IEE = subtotal * 5.11%
        4. total = (subtotal + IEE + fixed_charges) * 1.21
        """
        compensated = min(energy_cost, surplus_value)
        subtotal = (energy_cost - compensated) + power_cost
        iee = subtotal * elec_tax_pct / 100
        extras = (equipment_rental + bono_social) * days
        total = (subtotal + iee + extras) * (1 + iva_pct / 100)
        return round(total, 2), compensated

    # --- Monthly aggregation ---
    results_monthly = []
    omie_monthly_chart = []
    flux_balance = 0.0  # Flux Solar running balance (sequential across months)

    for ym in sorted(monthly_data.keys()):
        md = monthly_data[ym]
        days = len(md["dates"])
        if days == 0:
            continue

        avg_omie_mwh = md["omie_sum"] / md["omie_count"] if md["omie_count"] > 0 else 0.0

        # Power costs per scenario
        pwr_iber = _power_cost_daily(iber_pwr, days)
        pwr_hola = _power_cost_daily(hola_pwr, days)
        pwr_sper = _power_cost_som(sper_pwr_year, days)
        pwr_sidx = _power_cost_som(sidx_pwr_year, days)

        # Bills
        iber_total, iber_comp = _compute_bill(md["energy_iber"], md["surplus_iber"], pwr_iber, days)
        hola_total, hola_comp = _compute_bill(md["energy_hola"], md["surplus_hola"], pwr_hola, days)
        sper_total, sper_comp = _compute_bill(md["energy_sper"], md["surplus_sper"], pwr_sper, days)
        sidx_total, sidx_comp = _compute_bill(md["energy_sidx"], md["surplus_sidx"], pwr_sidx, days)

        # Flux Solar (Scenario 3 only)
        non_compensated = max(0, md["surplus_sidx"] - md["energy_sidx"])
        if flux_enabled:
            flux_balance += non_compensated * flux_credit_pct
        flux_used = min(flux_balance, sidx_total) if flux_enabled else 0.0
        sidx_after_flux = round(sidx_total - flux_used, 2)
        if flux_enabled:
            flux_balance -= flux_used

        # Best tariff
        costs = {
            "Iberdrola": iber_total,
            "Holaluz": hola_total,
            "Som Per.": sper_total,
            "Som Idx.": sidx_after_flux,
        }
        best = min(costs, key=costs.get)

        results_monthly.append({
            "month": ym,
            "days": days,
            "import_kwh": round(md["import_kwh"], 1),
            "export_kwh": round(md["export_kwh"], 1),
            "avg_omie_mwh": round(avg_omie_mwh, 2),
            "iberdrola_total": iber_total,
            "holaluz_total": hola_total,
            "som_periodes_total": sper_total,
            "som_indexada_total": sidx_total,
            "som_indexada_after_flux": sidx_after_flux,
            "flux_credit_used": round(flux_used, 2),
            "flux_balance": round(flux_balance, 2),
            "best": best,
        })

        omie_monthly_chart.append({"x": ym, "y": round(avg_omie_mwh, 2)})

    # --- Annual totals ---
    total_iber = sum(m["iberdrola_total"] for m in results_monthly)
    total_hola = sum(m["holaluz_total"] for m in results_monthly)
    total_sper = sum(m["som_periodes_total"] for m in results_monthly)
    total_sidx = sum(m["som_indexada_total"] for m in results_monthly)
    total_sidx_flux = sum(m["som_indexada_after_flux"] for m in results_monthly)
    total_import = sum(m["import_kwh"] for m in results_monthly)
    total_export = sum(m["export_kwh"] for m in results_monthly)

    # Best alternative for saving comparison (vs Iberdrola as baseline)
    best_alt = min(total_hola, total_sper, total_sidx_flux)
    saving_vs_iber = total_iber - best_alt
    saving_holaluz_vs_iber = total_iber - total_hola
    saving_periodes_vs_iber = total_iber - total_sper
    saving_indexada_vs_iber = total_iber - total_sidx_flux

    all_omie_values = [m["avg_omie_mwh"] for m in results_monthly if m["avg_omie_mwh"] > 0]
    avg_omie_mwh = sum(all_omie_values) / len(all_omie_values) if all_omie_values else 0

    total_days = sum(len(monthly_data[ym]["dates"]) for ym in monthly_data)

    annual = {
        "iberdrola_total": round(total_iber, 2),
        "holaluz_total": round(total_hola, 2),
        "som_periodes_total": round(total_sper, 2),
        "som_indexada_total": round(total_sidx, 2),
        "som_indexada_after_flux": round(total_sidx_flux, 2),
        "saving_vs_iberdrola": round(saving_vs_iber, 2),
        "saving_holaluz_vs_iberdrola": round(saving_holaluz_vs_iber, 2),
        "saving_periodes_vs_iberdrola": round(saving_periodes_vs_iber, 2),
        "saving_indexada_vs_iberdrola": round(saving_indexada_vs_iber, 2),
        "total_import_kwh": round(total_import, 1),
        "total_export_kwh": round(total_export, 1),
        "avg_omie_mwh": round(avg_omie_mwh, 2),
    }

    # --- Breakeven ---
    breakeven = _calculate_breakeven(
        import_by_hour, omie_by_hour, export_by_hour,
        peajes, cargos, idx_margin, iber_rates, iber_surplus,
        iber_pwr, sidx_pwr_year,
        contracted_power,
        elec_tax_pct, iva_pct, equipment_rental, bono_social,
        avg_omie_mwh,
        cf_mult, cf_other, cf_losses, cf_fe,
    )

    return {
        "range": time_range,
        "days_data": total_days,
        "monthly": results_monthly,
        "annual": annual,
        "breakeven": breakeven,
        "charts": {
            "omie_monthly": omie_monthly_chart,
        },
    }


def _calculate_breakeven(
    import_by_hour, omie_by_hour, export_by_hour,
    peajes, cargos, idx_margin, iber_rates, iber_surplus,
    iber_pwr, sidx_pwr_year,
    contracted_power,
    elec_tax_pct, iva_pct, equipment_rental, bono_social,
    current_avg_mwh,
    cf_mult=1.0, cf_other=0.0, cf_losses=0.0, cf_fe=0.0,
):
    """Binary search for OMIE multiplier where Som Indexada total == Iberdrola total."""
    if not import_by_hour or not omie_by_hour:
        return {
            "breakeven_multiplier": None,
            "breakeven_avg_mwh": None,
            "current_avg_mwh": round(current_avg_mwh, 1),
            "headroom_pct": None,
        }

    def _total_at_multiplier(mult):
        """Compute total indexed (Som Indexada) and Iberdrola cost with OMIE prices scaled."""
        monthly = defaultdict(lambda: {
            "energy_iber": 0.0, "energy_sidx": 0.0,
            "surplus_iber": 0.0, "surplus_sidx": 0.0,
            "export_kwh": 0.0, "dates": set(),
        })

        for hour in sorted(set(import_by_hour.keys()) & set(omie_by_hour.keys())):
            imp_kwh = import_by_hour[hour]
            if imp_kwh <= 0:
                continue
            omie_price = omie_by_hour[hour] * mult
            month_num = hour.month
            is_weekend = hour.weekday() >= 5
            period = get_period(hour.hour, month_num, is_weekend)
            ym = hour.strftime("%Y-%m")
            md = monthly[ym]
            md["dates"].add(hour.date())
            inner = (omie_price + cf_other) * (1 + cf_losses) + cf_fe + idx_margin
            md["energy_sidx"] += imp_kwh * (cf_mult * inner + peajes[period] + cargos[period])
            md["energy_iber"] += imp_kwh * iber_rates.get(period, 0.153962)

        for hour, kwh in export_by_hour.items():
            ym = hour.strftime("%Y-%m")
            md = monthly[ym]
            md["export_kwh"] += kwh
            md["surplus_iber"] += kwh * iber_surplus
            omie_price = omie_by_hour.get(hour, 0) * mult
            md["surplus_sidx"] += kwh * omie_price
            md["dates"].add(hour.date())

        total_sidx = 0.0
        total_iber = 0.0
        for ym, md in monthly.items():
            days = len(md["dates"])
            if days == 0:
                continue
            pwr_iber = sum(
                contracted_power.get(p, 69) * iber_pwr.get(p, 0) * days
                for p in ["P1", "P2", "P3", "P4", "P5", "P6"]
            )
            pwr_sidx = sum(
                contracted_power.get(p, 69) * (sidx_pwr_year.get(p, 0) / 365) * days
                for p in ["P1", "P2", "P3", "P4", "P5", "P6"]
            )

            for label, energy, surplus, pwr in [
                ("iber", md["energy_iber"], md["surplus_iber"], pwr_iber),
                ("sidx", md["energy_sidx"], md["surplus_sidx"], pwr_sidx),
            ]:
                compensated = min(energy, surplus)
                subtotal = (energy - compensated) + pwr
                iee = subtotal * elec_tax_pct / 100
                extras = (equipment_rental + bono_social) * days
                total = (subtotal + iee + extras) * (1 + iva_pct / 100)
                if label == "iber":
                    total_iber += total
                else:
                    total_sidx += total

        return total_sidx, total_iber

    lo, hi = 0.1, 20.0
    for _ in range(60):
        mid = (lo + hi) / 2
        idx_total, iber_total = _total_at_multiplier(mid)
        if iber_total > 0 and abs(idx_total - iber_total) / iber_total < 0.001:
            break
        if idx_total < iber_total:
            lo = mid
        else:
            hi = mid

    breakeven_mult = (lo + hi) / 2
    breakeven_avg_mwh = current_avg_mwh * breakeven_mult
    headroom_pct = (breakeven_mult - 1.0) * 100

    return {
        "breakeven_multiplier": round(breakeven_mult, 2),
        "breakeven_avg_mwh": round(breakeven_avg_mwh, 1),
        "current_avg_mwh": round(current_avg_mwh, 1),
        "headroom_pct": round(headroom_pct, 1),
    }


# ---------------------------------------------------------------------------
# Public API — called from app.py
# ---------------------------------------------------------------------------

def get_estadistiques_data(time_range="3m"):
    """Entry point for the /api/estadistiques/<range> endpoint."""
    return simulate(time_range)
