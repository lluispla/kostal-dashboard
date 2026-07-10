#!/usr/bin/env python3
"""Validate the v2 solar-forecast model against real production on clear days.

Compares, for each requested clear-sky day, three curves:
  * REAL    — measured plant AC total (both inverters) from InfluxDB
  * LEGACY  — the original due-south model (azimuth=0, tilt=30, no temp derate)
  * V2      — the geometry+temperature-corrected model from pricing.json

Both model curves are driven from the SAME historical Open-Meteo GTI (via the
forecast endpoint's start_date/end_date, which serves past days at 15-min), so
the only thing that differs between LEGACY and V2 is the model itself — this
isolates the model change cleanly. Metrics reported per day: peak local time,
PM/AM energy ratio about solar noon (13.75 h Madrid), and total daylight energy.

The diagnosis to confirm: v2 should move the peak from the legacy ~13:45 back
toward the real ~13:15-13:30, drop PM/AM from ~1.10 toward the real ~0.90, and
cut the ~+8% energy over-prediction.

Read-only. Reuses the ACTUAL production power functions from data.py so what we
validate is exactly what ships. Pure-Python (no numpy/pandas) so it runs inside
the dashboard container:

    docker compose exec dashboard python tools/validate_solar_forecast.py \
        2026-05-28 2026-06-17

Defaults to those two days if none are given.
"""
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# The tools/ dir sits next to data.py inside /app; import the real functions.
sys.path.insert(0, "/app")
import data  # noqa: E402

MADRID = ZoneInfo("Europe/Madrid")
SOLAR_NOON = 13.75  # Madrid-local hour of astronomical solar noon at this lon
DEFAULT_DAYS = ["2026-05-28", "2026-06-17"]

# Legacy geometry = the original hardcoded due-south array.
LEGACY_TILT, LEGACY_AZ = data._LEGACY_TILT, data._LEGACY_AZIMUTH


def real_curve(day):
    """{local 'HH:MM' -> kW} of measured plant AC total (summed inverters).

    Queried at 15-min mean per inverter, then summed across inverters per
    bucket. `day` is a Madrid-local calendar date string 'YYYY-MM-DD'.
    """
    d0 = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=MADRID)
    start_utc = (d0 - timedelta(hours=1)).astimezone(ZoneInfo("UTC"))
    stop_utc = (d0 + timedelta(days=1, hours=1)).astimezone(ZoneInfo("UTC"))
    per_bucket = {}  # local 'HH:MM' -> {inverter: kW}
    for table in data._q(f'''
        from(bucket: "{data.INFLUXDB_BUCKET}")
          |> range(start: {start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")},
                   stop: {stop_utc.strftime("%Y-%m-%dT%H:%M:%SZ")})
          |> filter(fn: (r) => r._measurement == "piko" and r._field == "ac_power_total")
          |> filter(fn: (r) => exists r.inverter)
          |> aggregateWindow(every: 15m, fn: mean, createEmpty: false)
    '''):
        for rec in table.records:
            t_local = rec.get_time().astimezone(MADRID)
            if t_local.strftime("%Y-%m-%d") != day:
                continue
            val = rec.get_value()
            if val is None:
                continue
            key = t_local.strftime("%H:%M")
            per_bucket.setdefault(key, {})[rec.values.get("inverter")] = val / 1000.0
    return {k: sum(v.values()) for k, v in per_bucket.items()}


def model_curve(day, tilt, azimuth, v2):
    """{local 'HH:MM' -> kW} from the real production math on historical GTI.

    Fetches that past day's 15-min GTI (+ temperature for v2) from Open-Meteo at
    the given plane, then applies data._conversion_efficiency (and, for v2,
    data._cell_temp_derate) — the exact functions get_solar_forecast() uses.
    """
    cfg = data._load_solar_config()
    minutely = ("global_tilted_irradiance,temperature_2m" if v2
                else "global_tilted_irradiance")
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": cfg["lat"], "longitude": cfg["lon"],
            "minutely_15": minutely,
            "tilt": tilt, "azimuth": azimuth,
            "timezone": "Europe/Madrid",
            "start_date": day, "end_date": day,
        },
        timeout=20,
    )
    resp.raise_for_status()
    m15 = resp.json().get("minutely_15", {})
    times = m15.get("time", [])
    gti = m15.get("global_tilted_irradiance", [])
    temps = m15.get("temperature_2m", []) if v2 else []

    kwp = cfg["kwp_nameplate"]
    rated, knee, exp = cfg["rated_efficiency"], cfg["low_light_knee_w"], cfg["low_light_exp"]
    noct, tempco = cfg["noct_c"], cfg["temp_coeff_per_c"]
    # Legacy uses the module-constant efficiency params (byte-identical old path).
    if not v2:
        rated, knee, exp = data._RATED_EFFICIENCY, data._LOW_LIGHT_KNEE, data._LOW_LIGHT_EXP

    curve = {}
    for i, (t_str, irr) in enumerate(zip(times, gti)):
        if irr is None or irr <= 0:
            continue
        eff = data._conversion_efficiency(irr, rated=rated, knee=knee, exp=exp)
        if v2:
            t_air = temps[i] if i < len(temps) else None
            eff *= data._cell_temp_derate(irr, t_air, noct, tempco)
        curve[t_str[11:16]] = irr * kwp * eff / 1000.0  # kW
    return curve


def metrics(curve):
    """(peak 'HH:MM', PM/AM ratio, daylight energy kWh) for a HH:MM->kW curve."""
    if not curve:
        return ("--:--", float("nan"), 0.0)
    peak = max(curve, key=curve.get)
    am = pm = 0.0
    for k, kw in curve.items():
        h = int(k[:2]) + int(k[3:]) / 60.0
        if h < SOLAR_NOON:
            am += kw
        else:
            pm += kw
    energy = sum(curve.values()) * 0.25  # 15-min buckets -> kWh
    return (peak, (pm / am if am else float("nan")), energy)


def norm_rmse(a, b):
    """RMSE of two HH:MM curves each normalized to equal total energy."""
    keys = sorted(set(a) & set(b))
    if not keys:
        return float("nan")
    sa, sb = sum(a[k] for k in keys), sum(b[k] for k in keys)
    if sa <= 0 or sb <= 0:
        return float("nan")
    se = sum(((a[k] / sa) - (b[k] / sb)) ** 2 for k in keys)
    return (se / len(keys)) ** 0.5


def main():
    days = sys.argv[1:] or DEFAULT_DAYS
    cfg = data._load_solar_config()
    print(f"Config: model={cfg['model']} az={cfg['azimuth_deg']} tilt={cfg['tilt_deg']} "
          f"tempco={cfg['temp_coeff_per_c']}  (legacy geom az={LEGACY_AZ}/tilt={LEGACY_TILT})")
    print(f"Solar noon reference: {SOLAR_NOON} h Madrid\n")

    hdr = f"{'day':11} {'curve':7} {'peak':6} {'PM/AM':6} {'energy':8} {'shapeRMSEvsReal'}"
    print(hdr)
    print("-" * len(hdr))
    for day in days:
        real = real_curve(day)
        legacy = model_curve(day, LEGACY_TILT, LEGACY_AZ, v2=False)
        v2 = model_curve(day, cfg["tilt_deg"], cfg["azimuth_deg"], v2=True)
        rows = [("REAL", real), ("LEGACY", legacy), ("V2", v2)]
        for name, c in rows:
            pk, ratio, en = metrics(c)
            rmse = "" if name == "REAL" else f"{norm_rmse(real, c):.4f}"
            print(f"{day:11} {name:7} {pk:6} {ratio:6.2f} {en:7.1f}  {rmse}")
        print()


if __name__ == "__main__":
    main()
