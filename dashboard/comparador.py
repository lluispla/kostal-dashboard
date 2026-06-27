"""Comparador d'ofertes elèctriques — backend logic.

Loads/saves offers, queries InfluxDB for actual consumption and OMIE prices
by 3.0TD period, and assembles all data needed for client-side bill computation.
"""

import json
import uuid
from datetime import timedelta

from config import INFLUXDB_BUCKET, OFFERS_PATH, PRICING_PATH
from data import _get_period, _hourly_records, _cet_now

# ---------------------------------------------------------------------------
# Offer persistence
# ---------------------------------------------------------------------------

_offers_cache = None


def load_offers():
    """Load offers from JSON file (cached in-process)."""
    global _offers_cache
    if _offers_cache is None:
        try:
            with open(OFFERS_PATH) as f:
                _offers_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _offers_cache = {"offers": []}
    return _offers_cache


def save_offers(data):
    """Write offers to JSON and invalidate cache."""
    global _offers_cache
    with open(OFFERS_PATH, "w") as f:
        json.dump(data, f, indent=2)
    _offers_cache = data


def add_offer(offer):
    """Add an offer, auto-generating an id if missing."""
    data = load_offers()
    if not offer.get("id"):
        offer["id"] = str(uuid.uuid4())[:8]
    data["offers"].append(offer)
    save_offers(data)
    return offer


def update_offer(offer_id, updates):
    """Update an existing offer by id. Returns updated offer or None."""
    data = load_offers()
    for i, o in enumerate(data["offers"]):
        if o["id"] == offer_id:
            data["offers"][i].update(updates)
            data["offers"][i]["id"] = offer_id  # prevent id change
            save_offers(data)
            return data["offers"][i]
    return None


def delete_offer(offer_id):
    """Delete an offer by id. Returns True if found."""
    data = load_offers()
    original_len = len(data["offers"])
    data["offers"] = [o for o in data["offers"] if o["id"] != offer_id]
    if len(data["offers"]) < original_len:
        save_offers(data)
        return True
    return False


# ---------------------------------------------------------------------------
# InfluxDB queries for consumption and OMIE by period
# ---------------------------------------------------------------------------

# Max plausible kWh in one hour for a 69 kW connection.
# Anything above this is a counter-jump artefact (e.g. first reading).
_MAX_KWH_PER_HOUR = 200


def get_actual_consumption_by_period(months=3):
    """Query hourly import/export data and aggregate by 3.0TD period."""
    bucket = INFLUXDB_BUCKET

    import_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: -{months}mo)
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_import_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    export_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: -{months}mo)
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "energy_export_total")
          |> aggregateWindow(every: 1h, fn: spread, createEmpty: false)
    ''')

    # Filter out counter-jump artefacts (first reading after collector start
    # captures entire counter history in one window).
    import_hours = [(t, kwh) for t, kwh in import_hours if kwh <= _MAX_KWH_PER_HOUR]
    export_hours = [(t, kwh) for t, kwh in export_hours if kwh <= _MAX_KWH_PER_HOUR]

    kwh_by_period = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0, "P6": 0}
    total_kwh = 0
    for t, kwh in import_hours:
        period = _get_period(t)
        kwh_by_period[period] += kwh
        total_kwh += kwh

    export_kwh = sum(kwh for _, kwh in export_hours)

    # Days = elapsed hours of actual data / 24, minimum 1.
    if import_hours:
        first = import_hours[0][0]
        last = import_hours[-1][0]
        elapsed_hours = (last - first).total_seconds() / 3600
        days = max(elapsed_hours / 24, 1)
    else:
        days = 1

    pct_by_period = {}
    for p in kwh_by_period:
        pct_by_period[p] = round(kwh_by_period[p] / total_kwh * 100, 1) if total_kwh > 0 else 0

    return {
        "kwh_by_period": {p: round(v, 1) for p, v in kwh_by_period.items()},
        "total_kwh": round(total_kwh, 1),
        "pct_by_period": pct_by_period,
        "export_kwh": round(export_kwh, 1),
        "days": round(days, 1),
        "hours_data": len(import_hours),
        "months_analysed": months,
    }


def get_omie_avg_by_period(months=3):
    """Query hourly OMIE prices and return mean EUR/kWh per 3.0TD period."""
    bucket = INFLUXDB_BUCKET

    omie_hours = _hourly_records(f'''
        from(bucket: "{bucket}")
          |> range(start: -{months}mo)
          |> filter(fn: (r) => r._measurement == "omie_prices")
          |> filter(fn: (r) => r._field == "price_eur_kwh")
          |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)
    ''')

    sums = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0, "P6": 0}
    counts = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0, "P6": 0}

    for t, price in omie_hours:
        period = _get_period(t)
        sums[period] += price
        counts[period] += 1

    result = {}
    for p in sums:
        result[p] = round(sums[p] / counts[p], 6) if counts[p] > 0 else 0.0

    return result


# ---------------------------------------------------------------------------
# Aggregated data for the comparador page
# ---------------------------------------------------------------------------

def get_comparador_data():
    """Assemble all data needed for client-side bill computation."""
    actual = get_actual_consumption_by_period(months=3)
    omie = get_omie_avg_by_period(months=3)
    offers = load_offers()

    # Load regulated charges and contract formula from pricing.json
    contract_formula = {
        "adjustment_multiplier": 1.0,
        "other_costs_eur_kwh": 0.0,
        "loss_coefficient": 0.0,
        "efficiency_fund_eur_kwh": 0.0,
    }
    try:
        with open(PRICING_PATH) as f:
            pricing = json.load(f)
        peajes = pricing["indexed_tariff"]["peajes_eur_kwh"]
        cargos = pricing["indexed_tariff"]["cargos_eur_kwh"]
        # Contract formula for indexed tariff computation
        cf = pricing.get("energy", {}).get("contract_formula", {})
        for k in contract_formula:
            if k in cf:
                contract_formula[k] = cf[k]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        peajes = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0, "P6": 0}
        cargos = {"P1": 0, "P2": 0, "P3": 0, "P4": 0, "P5": 0, "P6": 0}

    # Monthly averages — extrapolate from available data
    days = actual["days"]
    monthly_kwh = round(actual["total_kwh"] / days * 30, 1) if days > 0 else 0
    monthly_export = round(actual["export_kwh"] / days * 30, 1) if days > 0 else 0

    # Data quality flag
    low_data = actual["hours_data"] < 168  # less than 1 week

    return {
        "offers": offers["offers"],
        "actual": actual,
        "monthly_kwh": monthly_kwh,
        "monthly_export": monthly_export,
        "low_data": low_data,
        "omie_by_period": omie,
        "regulated": {
            "peajes": peajes,
            "cargos": cargos,
        },
        "contract_formula": contract_formula,
        "taxes": {"electricity_tax_pct": 5.11269, "iva_pct": 21},
        "scenarios": {
            "real": actual["pct_by_period"],
            "diurn": {"P1": 15, "P2": 20, "P3": 15, "P4": 15, "P5": 5, "P6": 30},
            "nocturn": {"P1": 2, "P2": 4, "P3": 3, "P4": 4, "P5": 2, "P6": 85},
            "uniforme": {"P1": 8, "P2": 12, "P3": 10, "P4": 12, "P5": 6, "P6": 52},
        },
        "period_info": {
            "P1": {"label": "Punta", "hours_week": 40, "schedule": "Punta: Gen, Feb, Jul, Des", "color": "#E63946",
                    "months": "Gen, Feb, Jul, Des", "type": "peak"},
            "P2": {"label": "Pla alt", "hours_week": 40, "schedule": "Gen-Mar, Jul, Nov-Des", "color": "#F4845F",
                    "months": "Gen-Mar, Jul, Nov-Des", "type": "peak+shoulder"},
            "P3": {"label": "Pla", "hours_week": 40, "schedule": "Mar, Jun, Ago, Sep, Nov", "color": "#F0AD4E",
                    "months": "Mar, Jun, Ago-Sep, Nov", "type": "peak+shoulder"},
            "P4": {"label": "Pla baix", "hours_week": 40, "schedule": "Abr-Jun, Ago-Oct", "color": "#0C4DA2",
                    "months": "Abr-Jun, Ago-Oct", "type": "peak+shoulder"},
            "P5": {"label": "Vall", "hours_week": 40, "schedule": "Abr, Mai, Oct", "color": "#28A745",
                    "months": "Abr, Mai, Oct", "type": "shoulder"},
            "P6": {"label": "Supervall", "hours_week": 88, "schedule": "Nits 0-7h + caps de setmana + festius", "color": "#17A589",
                    "months": "Tot l'any", "type": "valley"},
        },
    }
