#!/usr/bin/env python3
"""Re-fit the GTI->AC efficiency (rated / low-light knee / exponent) for the v2
forecast, now that geometry (east-of-south) and cell-temperature derating are
explicit.

Why: the original knee (_conversion_efficiency, data.py) was fitted on ONE
morning (2026-05-27) and folded three effects into one saturation — inverter
part-load, AOI/cosine loss, and morning east-horizon shading. With v2 the
azimuth correction now explains the morning shape and the temp derate explains
the afternoon fade, so the old knee double-counts and v2 under-predicts daily
energy by ~5%. This fits (rated_efficiency, low_light_knee_w, low_light_exp)
against measured full-plant clear-day curves, using the SAME Open-Meteo GTI +
temp pipeline the live model uses (NOT pvlib — the knee calibrates the residual
of *that* irradiance source against reality, so it must be fit on it).

Read-only w.r.t. the app (prints a recommended block; does not write). Runs in
the dashboard container:

    docker compose exec dashboard python tools/fit_low_light_knee.py

Uses full-plant clear days from 2026-05-28 on (both inverters reliably online).
"""
import sys

import requests

sys.path.insert(0, "/app")
import data  # noqa: E402
from validate_solar_forecast import real_curve, SOLAR_NOON  # noqa: E402

# Full-plant clear-sky days (both inverters online). Selected from the pvlib
# fit's clean-day set, restricted to >= 2026-05-28.
CLEAR_DAYS = ["2026-05-28", "2026-05-30", "2026-06-16",
              "2026-06-17", "2026-06-21", "2026-06-22"]

# Search grid.
RATED = [0.78 + 0.01 * i for i in range(13)]        # 0.78 .. 0.90
KNEE = [100, 150, 200, 250, 300, 400, 500, 600]      # W/m2
EXP = [0.2, 0.3, 0.4, 0.5, 0.7, 1.0]                 # 1.0 = linear (least suppression)


def fetch_gti_temp(day, tilt, azimuth):
    """{HH:MM -> (gti_w_m2, t_air_c)} historical 15-min at the v2 plane."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": data._SOLAR_LAT, "longitude": data._SOLAR_LON,
            "minutely_15": "global_tilted_irradiance,temperature_2m",
            "tilt": tilt, "azimuth": azimuth,
            "timezone": "Europe/Madrid",
            "start_date": day, "end_date": day,
        },
        timeout=20,
    )
    r.raise_for_status()
    m = r.json().get("minutely_15", {})
    t, g = m.get("time", []), m.get("global_tilted_irradiance", [])
    temps = m.get("temperature_2m", [])
    out = {}
    for i, (ts, gv) in enumerate(zip(t, g)):
        if gv is None or gv <= 0:
            continue
        out[ts[11:16]] = (gv, temps[i] if i < len(temps) else None)
    return out


def model_curve(irr_temp, kwp, rated, knee, exp, noct, tempco):
    """{HH:MM -> kW} from GTI+temp using candidate efficiency params."""
    out = {}
    for hhmm, (gti, tair) in irr_temp.items():
        eff = data._conversion_efficiency(gti, rated=rated, knee=knee, exp=exp)
        eff *= data._cell_temp_derate(gti, tair, noct, tempco)
        out[hhmm] = gti * kwp * eff / 1000.0
    return out


def day_stats(curve):
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
    return (peak, (pm / am if am else float("nan")), sum(curve.values()) * 0.25)


def rmse_kw(model, real):
    keys = sorted(set(model) & set(real))
    if not keys:
        return float("nan")
    return (sum((model[k] - real[k]) ** 2 for k in keys) / len(keys)) ** 0.5


def main():
    cfg = data._load_solar_config()
    tilt, az, kwp = cfg["tilt_deg"], cfg["azimuth_deg"], cfg["kwp_nameplate"]
    noct, tempco = cfg["noct_c"], cfg["temp_coeff_per_c"]
    print(f"Fitting knee for v2 geom az={az}/tilt={tilt}, tempco={tempco}, kwp={kwp}")
    print(f"Days: {CLEAR_DAYS}\n")

    # Cache real + GTI/temp per day once.
    per_day = {}
    for d in CLEAR_DAYS:
        real = real_curve(d)
        it = fetch_gti_temp(d, tilt, az)
        if not real or not it:
            print(f"  skip {d}: real={len(real)} gti={len(it)}")
            continue
        per_day[d] = (real, it)
    days = sorted(per_day)
    print(f"Usable days: {days}\n")

    # Grid search: minimize mean absolute-power RMSE across days (captures both
    # magnitude and shape). Track mean energy error % as a tiebreaker/report.
    results = []
    for rated in RATED:
        for knee in KNEE:
            for exp in EXP:
                tot_rmse = 0.0
                tot_eerr = 0.0
                for d in days:
                    real, it = per_day[d]
                    mod = model_curve(it, kwp, rated, knee, exp, noct, tempco)
                    tot_rmse += rmse_kw(mod, real)
                    _, _, e_mod = day_stats(mod)
                    _, _, e_real = day_stats(real)
                    tot_eerr += (e_mod - e_real) / e_real * 100.0
                results.append((tot_rmse / len(days), tot_eerr / len(days),
                                rated, knee, exp))
    results.sort()

    print("=== TOP 12 (by mean kW RMSE) ===")
    print(f"{'RMSE_kW':8} {'E_err%':7} {'rated':6} {'knee':5} {'exp':4}")
    for rmse, eerr, rated, knee, exp in results[:12]:
        print(f"{rmse:7.3f}  {eerr:+6.1f}  {rated:.2f}   {knee:4d}  {exp:.1f}")

    # Current v2 params for comparison.
    cr = cfg["rated_efficiency"]; ck = cfg["low_light_knee_w"]; ce = cfg["low_light_exp"]
    cur = next((x for x in results
                if abs(x[2]-cr) < 1e-9 and x[3] == ck and abs(x[4]-ce) < 1e-9), None)
    if cur:
        print(f"\ncurrent (rated={cr}/knee={ck}/exp={ce}): "
              f"RMSE={cur[0]:.3f} E_err={cur[1]:+.1f}%")

    best = results[0]
    br, bk, be = best[2], best[3], best[4]
    print(f"\nBEST: rated={br} knee={bk} exp={be}  RMSE={best[0]:.3f} E_err={best[1]:+.1f}%")

    print("\n=== per-day peak / PM-AM / energy: REAL | BEST ===")
    for d in days:
        real, it = per_day[d]
        rp, rr, re = day_stats(real)
        mp, mr, me = day_stats(model_curve(it, kwp, br, bk, be, noct, tempco))
        print(f"{d}: REAL {rp}/{rr:.2f}/{re:.0f}kWh | BEST {mp}/{mr:.2f}/{me:.0f}kWh "
              f"({(me-re)/re*100:+.1f}%)")

    print("\nApply to pricing.json solar_forecast (hot-reload, no rebuild):")
    print(f'  "rated_efficiency": {br}, "low_light_knee_w": {bk}, "low_light_exp": {be}')


if __name__ == "__main__":
    main()
