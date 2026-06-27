#!/usr/bin/env python3
"""Per-period other_costs calibration from a full-month infoenergia CSV.

Generalises the manual Apr/May 2026 calibration: given a full-month official
meter CSV (Som Energia infoenergia export) plus the matching invoice(s) already
stored in /app/invoices, it back-calculates the contract-formula "other costs"
(€/kWh) for every tariff period that has consumption, and merges the result into
pricing.energy.contract_formula.other_costs_eur_kwh_by_period.

Why per-period: "other costs" (Pc+Sc+Dsv+GdO+POsOm) are genuinely period-
dependent. A single scalar over-prices some periods. See memory
formula_calibration / invoice_FE2600698309.

Formula solved per period (consumption-weighted over official hours):
    inv_rate = mult * [(w_omie + X) * (1+losses) + fe + margin] + peaje + cargo
=>  X = ((inv_rate - peaje - cargo)/mult - fe - margin)/(1+losses) - w_omie
where w_omie = official-kWh-weighted OMIE price for that period.

When a CSV spans several invoices (e.g. a full March covers the 01-11 and 12-31
invoices), each hour is attributed to its invoice by date and its period by the
tariff calendar; per-period X is pooled across invoices weighted by kWh.

Usage (inside Docker):
    # preview only (default — never writes):
    docker compose exec dashboard python3 /app/tools/calibrate_other_costs.py /tmp/march-full.csv
    # ingest the CSV into official_meter first, then preview:
    docker compose exec dashboard python3 /app/tools/calibrate_other_costs.py /tmp/march-full.csv --ingest
    # actually write the new per-period values into pricing.json:
    docker compose exec dashboard python3 /app/tools/calibrate_other_costs.py /tmp/march-full.csv --ingest --write
"""

import json
import sys
import csv as _csv
from datetime import datetime, timedelta

sys.path.insert(0, "/app")

from config import INFLUXDB_BUCKET, PRICING_PATH
from data import (
    _hourly_records, _get_period, _CET, _load_indexed_tariff,
    ingest_official_meter_csv, invalidate_pricing_caches,
)
from invoice import list_invoices

ALL_PERIODS = ["P1", "P2", "P3", "P4", "P5", "P6"]


def _csv_date_range(path):
    """Return (min_date, max_date) as date objects from an infoenergia CSV."""
    days = []
    with open(path, encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            d, m, y = row["Fecha"].strip().split("/")
            days.append(datetime(int(y), int(m), int(d)).date())
    return min(days), max(days)


def calibrate(csv_path, do_ingest=False, do_write=False, only_periods=None):
    """only_periods: optional set of period codes to limit what gets written.

    Use this to avoid clobbering an already-calibrated year-round period (P6)
    with single-season data — other_costs is seasonal as well as period-specific.
    """
    if do_ingest:
        print("Ingesting CSV into official_meter ...", ingest_official_meter_csv(csv_path))

    csv_start, csv_end = _csv_date_range(csv_path)
    print(f"CSV official range: {csv_start} .. {csv_end}")

    tariff = _load_indexed_tariff()
    cf = tariff["contract_formula"]
    mult = cf.get("adjustment_multiplier", 1.015)
    losses = cf.get("loss_coefficient", 0.12)
    fe = cf.get("efficiency_fund_eur_kwh", 0.001)
    margin = tariff["margin"]
    peajes = tariff["peajes"]
    cargos = tariff["cargos"]

    # Invoices overlapping the CSV range (need their per-period rates).
    invs = []
    for inv in list_invoices():
        if inv.get("supplier") != "som_energia" or not inv.get("rates"):
            continue
        try:
            s = datetime.strptime(inv["billing_start"], "%d/%m/%Y").date()
            e = datetime.strptime(inv["billing_end"], "%d/%m/%Y").date()
        except (KeyError, ValueError):
            continue
        if s <= csv_end and e >= csv_start:  # overlap
            invs.append((s, e, inv))
    if not invs:
        print("No overlapping invoice with rates found — cannot calibrate.")
        return
    print("Matched invoices:", [i[2].get("filename") for i in invs])

    # Pull official + OMIE hours for the whole CSV range.
    d1 = datetime(csv_start.year, csv_start.month, csv_start.day, tzinfo=_CET)
    d2 = datetime(csv_end.year, csv_end.month, csv_end.day, tzinfo=_CET) + timedelta(days=1)
    hk = lambda t: t.replace(minute=0, second=0, microsecond=0)
    off = _hourly_records(f'from(bucket:"{INFLUXDB_BUCKET}") |> range(start:{d1.isoformat()},stop:{d2.isoformat()}) '
                          f'|> filter(fn:(r)=>r._measurement=="official_meter") |> filter(fn:(r)=>r._field=="import_kwh")')
    omie = _hourly_records(f'from(bucket:"{INFLUXDB_BUCKET}") |> range(start:{d1.isoformat()},stop:{d2.isoformat()}) '
                           f'|> filter(fn:(r)=>r._measurement=="omie_prices") |> filter(fn:(r)=>r._field=="price_eur_kwh") '
                           f'|> aggregateWindow(every:1h,fn:mean,createEmpty:false)')
    omap = {hk(t): v for t, v in omie}

    def invoice_for(date_):
        for s, e, inv in invs:
            if s <= date_ <= e:
                return inv
        return None

    # Accumulate per (invoice_id, period): kwh and kwh*omie.
    acc = {}  # (fname, period) -> {"kwh":, "ko":}
    for t, kwh in off:
        if kwh <= 0:
            continue
        th = hk(t)
        inv = invoice_for(th.date())
        if inv is None:
            continue
        p = _get_period(th)
        key = (inv.get("filename"), p)
        a = acc.setdefault(key, {"kwh": 0.0, "ko": 0.0})
        a["kwh"] += kwh
        a["ko"] += kwh * omap.get(th, 0.0)

    # Solve X per (invoice, period), then pool per period weighted by kWh.
    pooled = {p: {"num": 0.0, "den": 0.0, "n": 0} for p in ALL_PERIODS}
    for (fname, p), a in acc.items():
        inv = next(i[2] for i in invs if i[2].get("filename") == fname)
        inv_rate = inv.get("rates", {}).get(p, 0)
        if inv_rate <= 0 or a["kwh"] < 1:
            continue
        w_omie = a["ko"] / a["kwh"]
        x = ((inv_rate - peajes[p] - cargos[p]) / mult - fe - margin) / (1 + losses) - w_omie
        pooled[p]["num"] += x * a["kwh"]
        pooled[p]["den"] += a["kwh"]
        pooled[p]["n"] += 1

    by_period = dict(cf.get("other_costs_eur_kwh_by_period") or {})
    scalar = cf.get("other_costs_eur_kwh", 0.0)
    print(f"\n{'Period':<8}{'old':>12}{'new':>12}{'kWh':>10}{'invoices':>8}  status")
    changed = {}
    for p in ALL_PERIODS:
        if pooled[p]["den"] < 1:
            continue
        new_x = round(pooled[p]["num"] / pooled[p]["den"], 6)
        old = by_period.get(p, f"{scalar} (scalar)")
        skipped = only_periods is not None and p not in only_periods
        status = "skipped (--periods)" if skipped else "will write" if do_write else "preview"
        print(f"{p:<8}{str(old):>12}{new_x:>12.6f}{pooled[p]['den']:>10.0f}{pooled[p]['n']:>8}  {status}")
        if not skipped:
            changed[p] = new_x

    if not changed:
        print("\nNo periods with both official data and invoice rates — nothing to update.")
        return

    if not do_write:
        print("\n[dry-run] Pass --write to merge these into pricing.json "
              "(other_costs_eur_kwh_by_period).")
        return

    # Merge (only periods we computed; leave the rest untouched) and persist.
    with open(PRICING_PATH) as f:
        pricing = json.load(f)
    for block in (pricing.get("energy", {}).get("contract_formula"),
                  pricing.get("scenarios", {}).get("som_indexada", {}).get("contract_formula")):
        if not block:
            continue
        bp = dict(block.get("other_costs_eur_kwh_by_period") or {})
        bp.update(changed)
        block["other_costs_eur_kwh_by_period"] = bp
    with open(PRICING_PATH, "w") as f:
        json.dump(pricing, f, indent=2, ensure_ascii=False)
    invalidate_pricing_caches()
    print(f"\nWrote per-period other_costs for {list(changed)} to {PRICING_PATH}.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        sys.exit(1)
    only = None
    for fl in flags:
        if fl.startswith("--periods="):
            only = {p.strip().upper() for p in fl.split("=", 1)[1].split(",") if p.strip()}
    calibrate(args[0], do_ingest="--ingest" in flags,
              do_write="--write" in flags, only_periods=only)
