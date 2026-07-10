#!/usr/bin/env python3
"""Fit the plant's array geometry (azimuth, tilt) + temperature coefficient
against measured clear-sky AC production curves, using pvlib.

Why: the live forecast in dashboard/data.py hardcodes azimuth=0 (due south)
and tilt=30. Measured clear-day curves peak ~30 min before solar noon and are
morning-leaning, implying the array is actually east-of-south. This script
finds the (azimuth, tilt, tempco) that best reproduces the *shape* of the
measured curves, using a real sky model (Ineichen clear-sky + Perez
transposition) rather than Open-Meteo's single flat GTI.

Self-contained / offline-friendly: reads a JSON dump of measured curves
({day: [[iso_utc, kW], ...]}) produced from InfluxDB, and (optionally) fetches
ERA5 archive air-temp/wind for the fit days. If the temp fetch fails it falls
back to a clear-day temperature profile. NOT wired into the app.

Usage:
    python fit_solar_geometry.py <curves.json>

The measured curves should be a *single* inverter (one physical array) so the
geometry is well-defined — the CI 50 is used because its AC total is clean back
to Feb 2026 (winter/spring declination spread breaks the az/tilt degeneracy).
"""
import json
import sys
import urllib.request
from datetime import timezone

import numpy as np
import pandas as pd
import pvlib

LAT, LON = 42.12, 3.13
ALT = 20  # m, approximate
TZ = "UTC"


def load_curves(path):
    raw = json.load(open(path))
    curves = {}
    for day, rows in raw.items():
        if not rows:
            continue
        idx = pd.DatetimeIndex([pd.Timestamp(r[0]) for r in rows]).tz_convert("UTC")
        s = pd.Series([r[1] for r in rows], index=idx).sort_index()
        # keep daylight, drop tiny tails/noise
        s = s[s > 0.05 * s.max()]
        curves[day] = s
    return curves


def fetch_temps(days):
    """ERA5 archive air-temp (C) + wind (m/s) per day, hourly, UTC. Optional."""
    d0, d1 = min(days), max(days)
    url = (
        "https://archive-api.open-meteo.com/v1/archive?latitude=42.12&longitude=3.13"
        f"&start_date={d0}&end_date={d1}"
        "&hourly=temperature_2m,wind_speed_10m&timezone=UTC&wind_speed_unit=ms"
    )
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            j = json.load(r)
        h = j["hourly"]
        idx = pd.DatetimeIndex(pd.to_datetime(h["time"])).tz_localize("UTC")
        return (pd.Series(h["temperature_2m"], index=idx),
                pd.Series(h["wind_speed_10m"], index=idx), "ERA5 archive")
    except Exception as e:  # noqa
        print(f"  [temp] archive fetch failed ({e}); using clear-day profile", file=sys.stderr)
        return None, None, "clear-day sinusoid fallback"


def model_poa(index, surface_tilt, surface_az):
    """Clear-sky POA (W/m2) for a plane, via Ineichen + Perez transposition.

    surface_az in pvlib convention: degrees clockwise from North (180 = south).
    """
    loc = pvlib.location.Location(LAT, LON, tz=TZ, altitude=ALT)
    solpos = loc.get_solarposition(index)
    cs = loc.get_clearsky(index, model="ineichen")  # ghi/dni/dhi
    dni_extra = pvlib.irradiance.get_extra_radiation(index)
    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt, surface_az,
        solpos["apparent_zenith"], solpos["azimuth"],
        dni=cs["dni"], ghi=cs["ghi"], dhi=cs["dhi"],
        dni_extra=dni_extra, model="perez",
    )
    return poa["poa_global"].fillna(0.0), cs, solpos


def apply_temp(poa, tair, wind, tempco):
    """Faiman cell temp -> multiplicative efficiency derate (ref 25C)."""
    tcell = pvlib.temperature.faiman(poa, tair, wind)
    return poa * (1.0 + tempco * (tcell - 25.0))


def shape_rmse(meas, modeled):
    """RMSE of curves each normalized to unit sum (equal daily energy)."""
    m = meas / meas.sum()
    q = modeled / modeled.sum() if modeled.sum() > 0 else modeled
    return float(np.sqrt(((m - q) ** 2).mean()))


def peak_and_pmam(series):
    """Peak local (Madrid) time + PM/AM energy ratio about solar noon 13.75h."""
    loc = series.tz_convert("Europe/Madrid")
    peak_t = loc.idxmax()
    h = loc.index.hour + loc.index.minute / 60.0
    am = loc.values[h < 13.75].sum()
    pm = loc.values[h >= 13.75].sum()
    return peak_t.strftime("%H:%M"), (pm / am if am else float("nan"))


def main():
    path = sys.argv[1]
    curves = load_curves(path)
    days = sorted(curves)
    print(f"Loaded {len(days)} clean days: {days[0]}..{days[-1]}")

    tair_all, wind_all, tsrc = fetch_temps(days)
    print(f"Temperature source: {tsrc}")

    # Pre-compute POA per day per candidate is expensive; cache clear-sky+solpos
    # by reusing model_poa per (day, tilt, az). Grid is modest so fine.
    AZ = list(range(150, 196, 3))      # pvlib az: 180=S; 150=30E of S ... 195=15W
    TILT = list(range(5, 51, 3))
    TEMPCO = -0.0035

    def air_wind(idx):
        if tair_all is not None:
            ta = tair_all.reindex(idx, method="nearest")
            wi = wind_all.reindex(idx, method="nearest").clip(lower=0.5)
            return ta, wi
        # fallback clear-day profile (Madrid local hour)
        loc_h = idx.tz_convert("Europe/Madrid")
        h = loc_h.hour + loc_h.minute / 60.0
        ta = pd.Series(18 + 10 * np.sin(np.radians((h - 9) * 12)), index=idx).clip(5, 35)
        wi = pd.Series(2.0, index=idx)
        return ta, wi

    # Grid search (with temperature at fixed tempco)
    results = []
    for az in AZ:
        for tilt in TILT:
            tot = 0.0
            for d in days:
                meas = curves[d]
                poa, _, _ = model_poa(meas.index, tilt, az)
                ta, wi = air_wind(meas.index)
                mod = apply_temp(poa, ta, wi, TEMPCO)
                tot += shape_rmse(meas, mod)
            results.append((tot / len(days), az, tilt))
    results.sort()

    def az_label(az):
        off = 180 - az  # +east
        return f"{off:+d}E" if off else "0(S)"

    print("\n=== TOP 10 (pvlib Perez + Faiman temp, tempco -0.35%/C) ===")
    print("rmse     az(pvlib)  offsetE  tilt")
    for r, az, tilt in results[:10]:
        print(f"{r:.5f}   {az:3d}      {az_label(az):>5}   {tilt}")

    best_r, best_az, best_tilt = results[0]

    def eval_geom(az, tilt, tc=TEMPCO):
        tot = 0.0
        for d in days:
            meas = curves[d]
            poa, _, _ = model_poa(meas.index, tilt, az)
            ta, wi = air_wind(meas.index)
            tot += shape_rmse(meas, apply_temp(poa, ta, wi, tc))
        return tot / len(days)

    cur_r = eval_geom(180, 30)
    n_better = sum(1 for r, _, _ in results if r < cur_r)
    print(f"\ncurrent az=180(S)/tilt=30: rmse={cur_r:.5f} ({n_better}/{len(results)} grid pts fit better)")
    print(f"BEST: az={best_az}(={az_label(best_az)}) tilt={best_tilt} rmse={best_r:.5f}")
    print(f"  worst grid rmse={results[-1][0]:.5f}  -> best is {results[-1][0]/best_r:.1f}x better than worst")
    # how far does rmse stay within +5% of the minimum? (surface sharpness)
    thr = best_r * 1.05
    within = [(az, tilt) for r, az, tilt in results if r <= thr]
    azs = sorted(set(180 - a for a, _ in within)); ts = sorted(set(t for _, t in within))
    print(f"  within +5% of min: azE {azs[0]}..{azs[-1]}, tilt {ts[0]}..{ts[-1]} ({len(within)} pts)")

    # tempco sensitivity at best az/tilt
    print("\n=== tempco sensitivity at best geometry ===")
    for tc in (-0.0025, -0.0035, -0.0045):
        tot = 0.0
        for d in days:
            meas = curves[d]
            poa, _, _ = model_poa(meas.index, best_tilt, best_az)
            ta, wi = air_wind(meas.index)
            tot += shape_rmse(meas, apply_temp(poa, ta, wi, tc))
        print(f"  tempco {tc*100:+.2f}%/C -> mean rmse {tot/len(days):.5f}")

    # per-day peak/PM-AM: real vs best vs current
    print("\n=== per-day peak-time / PM-AM: REAL | BEST | current(S/30) ===")
    for d in days:
        meas = curves[d]
        rp, rr = peak_and_pmam(meas)
        pb, _, _ = model_poa(meas.index, best_tilt, best_az)
        ta, wi = air_wind(meas.index)
        mb = apply_temp(pb, ta, wi, TEMPCO)
        mp, mr = peak_and_pmam(mb)
        pc, _, _ = model_poa(meas.index, 30, 180)
        mc = apply_temp(pc, ta, wi, TEMPCO)
        cp, cr = peak_and_pmam(mc)
        print(f"{d}: REAL {rp}/{rr:.2f} | BEST {mp}/{mr:.2f} | S30 {cp}/{cr:.2f}")


if __name__ == "__main__":
    main()
