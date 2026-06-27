"""
Battery investment analysis using healthy historical window.

Pulls hourly import/export/OMIE for a representative period (Feb 26 - Apr 15 2026),
simulates several battery sizes against the indexed-tariff formula, computes
payback and 15-yr NPV at 4% discount.

Read-only. Does not write to InfluxDB or pricing.json.
"""
import math
import json
from datetime import datetime, timezone

# Imported from running dashboard container
from data import (
    _hourly_records, INFLUXDB_BUCKET, _load_pricing, _load_indexed_tariff,
    _get_period, _CET
)

# --- Period of interest: healthy production window ---
START = "2026-02-26T00:00:00Z"
STOP  = "2026-04-15T00:00:00Z"
DAYS  = 48  # 2026-02-26 to 2026-04-14 inclusive ~= 48 days

pricing = _load_pricing()
tariff = _load_indexed_tariff()
cf = tariff.get("contract_formula", {})
cf_mult   = cf.get("adjustment_multiplier", 1.015)
cf_other  = cf.get("other_costs_eur_kwh", 0.046108)
cf_losses = cf.get("loss_coefficient", 0.12)
cf_fe     = cf.get("efficiency_fund_eur_kwh", 0.001)
margin    = tariff["margin"]
peajes    = tariff["peajes"]
cargos    = tariff["cargos"]
ksem_cal  = pricing.get("ksem_calibration", {})
overall_cal = ksem_cal.get("overall", 1.0) or 1.0

bucket = INFLUXDB_BUCKET

print(f"== Battery investment analysis ({START} -> {STOP}, {DAYS} days) ==")
print(f"KSEM overall calibration factor: {overall_cal}")

omie_hours = _hourly_records(f'''
from(bucket: "{bucket}")
  |> range(start: {START}, stop: {STOP})
  |> filter(fn: (r) => r._measurement == "omie_prices")
  |> filter(fn: (r) => r._field == "price_eur_kwh")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
''')
import_hours = _hourly_records(f'''
from(bucket: "{bucket}")
  |> range(start: {START}, stop: {STOP})
  |> filter(fn: (r) => r._measurement == "ksem")
  |> filter(fn: (r) => r._field == "energy_import_total")
  |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
''')
export_hours = _hourly_records(f'''
from(bucket: "{bucket}")
  |> range(start: {START}, stop: {STOP})
  |> filter(fn: (r) => r._measurement == "ksem")
  |> filter(fn: (r) => r._field == "energy_export_total")
  |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
''')

def to_dict(hours):
    return {t.replace(minute=0, second=0, microsecond=0): v for t, v in hours}

omie_by_h   = to_dict(omie_hours)
import_by_h = {h: v * overall_cal for h, v in to_dict(import_hours).items()}  # calibrate
export_by_h = to_dict(export_hours)  # export doesn't need KSEM calibration up-multi

all_hours = sorted(set(omie_by_h) | set(import_by_h) | set(export_by_h))
n_hours = len(all_hours)
print(f"Hours of data: {n_hours} (~{n_hours/24:.1f} days)")

# --- Aggregate statistics ---
tot_imp = sum(import_by_h.values())
tot_exp = sum(export_by_h.values())
print(f"Total import (KSEM, calibrated): {tot_imp:.0f} kWh  ({tot_imp/(n_hours/24):.1f} kWh/day)")
print(f"Total export (PV surplus):       {tot_exp:.0f} kWh  ({tot_exp/(n_hours/24):.1f} kWh/day)")

# Compute the "baseline" cost without battery
def indexed_rate(hour, omie):
    period = _get_period(hour)
    inner = (omie + cf_other) * (1 + cf_losses) + cf_fe + margin
    return cf_mult * inner + peajes[period] + cargos[period]

base_cost = 0.0
base_surplus_credit = 0.0
for h in all_hours:
    imp = import_by_h.get(h, 0.0)
    exp = export_by_h.get(h, 0.0)
    omie = omie_by_h.get(h, 0.0)
    if imp > 0:
        base_cost += imp * indexed_rate(h, omie)
    if exp > 0:
        base_surplus_credit += exp * max(omie, 0.0)  # OMIE-based netting, no negative

print(f"\nBaseline (no battery) over period:")
print(f"  Energy import cost: {base_cost:.2f} EUR")
print(f"  Surplus credit @OMIE: {base_surplus_credit:.2f} EUR")
print(f"  Net energy line: {base_cost - base_surplus_credit:.2f} EUR")

# --- OMIE distribution analysis ---
import_omie_weighted = sum(import_by_h.get(h,0)*omie_by_h.get(h,0) for h in all_hours)
export_omie_weighted = sum(export_by_h.get(h,0)*omie_by_h.get(h,0) for h in all_hours)
import_avg_omie = import_omie_weighted/tot_imp if tot_imp else 0
export_avg_omie = export_omie_weighted/tot_exp if tot_exp else 0
import_avg_rate = base_cost / tot_imp if tot_imp else 0
print(f"\nVolume-weighted avg OMIE during import hours:  {import_avg_omie:.4f} EUR/kWh")
print(f"Volume-weighted avg OMIE during export hours:  {export_avg_omie:.4f} EUR/kWh")
print(f"Volume-weighted avg total import rate (PVPC-style): {import_avg_rate:.4f} EUR/kWh")
print(f"Spread (import_rate - export_OMIE): {import_avg_rate - export_avg_omie:.4f} EUR/kWh")
print(f"  (This is the upper-bound saving per kWh shifted by battery)")

# --- Simulate battery: greedy "charge from surplus, discharge to imports" ---
def simulate(capacity_kwh, charge_kw, discharge_kw, eff):
    """Hourly greedy sim. Returns (avoided_imp_kwh, avoided_exp_kwh, savings_eur).

    savings_eur = (kWh delivered to load × indexed_rate at that hour)
                - (kWh of export forgone × OMIE at that hour)
    """
    eff_sqrt = math.sqrt(eff)
    soc = 0.0
    avoided_imp = 0.0
    avoided_exp = 0.0
    savings_imp = 0.0     # value of import avoided
    lost_export = 0.0     # OMIE credit forgone by absorbing export
    for h in all_hours:
        imp = import_by_h.get(h, 0.0)
        exp = export_by_h.get(h, 0.0)
        omie = omie_by_h.get(h, 0.0)

        # 1) absorb surplus
        if exp > 0 and soc < capacity_kwh:
            charge = min(exp, capacity_kwh - soc, charge_kw)
            soc_added = charge * eff_sqrt
            soc += soc_added
            avoided_exp += charge
            lost_export += charge * max(omie, 0.0)

        # 2) discharge to load
        if imp > 0 and soc > 0:
            dis_drawn = min(imp / eff_sqrt, soc, discharge_kw)
            delivered = dis_drawn * eff_sqrt
            soc -= dis_drawn
            avoided_imp += delivered
            savings_imp += delivered * indexed_rate(h, omie)
    return avoided_imp, avoided_exp, savings_imp, lost_export

# Capacity options. Charge/discharge rate scales with capacity (~ C/2)
configs = [
    (10,  5,  5, 0.90),
    (20, 10, 10, 0.90),
    (30, 15, 15, 0.90),
    (50, 25, 25, 0.90),
    (80, 30, 30, 0.90),
]

# --- CAPEX assumptions (2026 Spanish commercial LFP, AC-coupled inverter incl.) ---
# Low-end: bulk LFP install ~400 EUR/kWh
# Mid:     ~550 EUR/kWh (most realistic for a commercial 20-50 kWh job by integrator)
# High:    ~700 EUR/kWh (turnkey by branded installer w/ Victron + monitoring)
CAPEX_MID = 550   # EUR/kWh installed (LFP + AC-coupled inverter + install + IVA)
CAPEX_LOW = 400
CAPEX_HIGH = 700

DISCOUNT = 0.04   # 4%
LIFETIME_YR = 15
DEGRADATION = 0.02  # 2%/yr capacity fade

# Scaling: data spans ~ 48 days. Annualize. The window is late winter/early spring;
# summer typically has MORE surplus (better for batteries since more export to absorb)
# but ALSO lower OMIE prices typically. We apply a flat annualization with a sanity
# note. Adjust by 1.15x to account for summer being better-suited to batteries on
# net (more surplus to absorb) — conservative compared to memory's 1.3x.
DAYS_OBSERVED = n_hours / 24
ANNUAL_FACTOR = 365.0 / DAYS_OBSERVED
SEASON_UPLIFT = 1.15  # conservative

def npv(annual_saving, capex, years=LIFETIME_YR, r=DISCOUNT, degr=DEGRADATION):
    pv = -capex
    for k in range(1, years+1):
        cf = annual_saving * ((1-degr) ** (k-1))
        pv += cf / ((1+r)**k)
    return pv

print(f"\n== Sizing comparison ==")
print(f"{'Cap':>6} {'PB-yrs':>8} {'AvImp':>8} {'AvExp':>8} {'GrossSav':>10} {'NetSav':>10} {'Annual':>10} {'CAPEX@550':>11} {'PB-yr':>7} {'NPV15':>10}")
rows = []
for cap, c_kw, d_kw, eff in configs:
    av_imp, av_exp, sav_imp, lost_exp = simulate(cap, c_kw, d_kw, eff)
    period_net = sav_imp - lost_exp  # savings during observed window
    annual = period_net * ANNUAL_FACTOR * SEASON_UPLIFT
    capex_mid = cap * CAPEX_MID
    pb = capex_mid / annual if annual > 0 else float('inf')
    npv_mid = npv(annual, capex_mid)
    rows.append(dict(cap=cap, av_imp=av_imp, av_exp=av_exp, sav_imp=sav_imp,
                     lost_exp=lost_exp, period_net=period_net, annual=annual,
                     capex_mid=capex_mid, payback=pb, npv_mid=npv_mid))
    print(f"{cap:>6} {pb:>8.1f} {av_imp:>8.0f} {av_exp:>8.0f} {sav_imp:>10.2f} {period_net:>10.2f} {annual:>10.2f} {capex_mid:>11.0f} {pb:>7.1f} {npv_mid:>+10.0f}")

# Sensitivity table: low / mid / high CAPEX for each size
print(f"\n== CAPEX sensitivity (annual saving, payback yrs, NPV15) ==")
print(f"{'Cap':>5} {'Annual':>9} | {'@'+str(CAPEX_LOW):>11} {'PB':>5} {'NPV15':>10} | {'@'+str(CAPEX_MID):>11} {'PB':>5} {'NPV15':>10} | {'@'+str(CAPEX_HIGH):>11} {'PB':>5} {'NPV15':>10}")
for r in rows:
    cap = r["cap"]; annual = r["annual"]
    out = []
    for cx in (CAPEX_LOW, CAPEX_MID, CAPEX_HIGH):
        capex = cap * cx
        pb = capex/annual if annual > 0 else float('inf')
        nv = npv(annual, capex)
        out += [f"{capex:>11.0f}", f"{pb:>5.1f}", f"{nv:>+10.0f}"]
    print(f"{cap:>5} {annual:>9.2f} | " + " ".join(out))

# --- Peak-shaving / excess-power check ---
# Contracted P1=36, P2=36, P3=36, P4=40, P5=40, P6=69 kW
# 1-hour spread aggregates can mask sub-hour peaks, but check kW peak hour by period.
print(f"\n== Peak power import per period ==")
from collections import defaultdict
peak_by_period = defaultdict(float)
hours_over = defaultdict(int)
contracted = pricing["contracted_power_kw"]
for h in all_hours:
    imp = import_by_h.get(h, 0.0)
    p = _get_period(h)
    if imp > peak_by_period[p]:
        peak_by_period[p] = imp
    if imp > contracted[p]:
        hours_over[p] += 1
for p in sorted(peak_by_period):
    print(f"  {p}: peak hourly avg = {peak_by_period[p]:.1f} kW, "
          f"contracted = {contracted[p]} kW, "
          f"hours_over_contract = {hours_over[p]}")

# Surplus hours bucket
above_zero_exp = sum(1 for h in all_hours if export_by_h.get(h,0) > 0)
print(f"\nHours with surplus (export>0): {above_zero_exp} of {n_hours} ({100*above_zero_exp/n_hours:.1f}%)")
print(f"Mean surplus when present: {tot_exp/above_zero_exp:.2f} kWh/h")
