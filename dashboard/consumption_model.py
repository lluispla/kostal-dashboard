"""Consumption pattern model — detect printer runs, extract baseline, predict.

The facility has ~3 HP MJF 3D printers each drawing ~11 kW when running. Their
activity dominates the KSEM load signal. To build a useful baseline (everything
except printers) we:

  1. Compute ``load = ksem_power × cal + piko_power`` so the signal is
     independent of solar output. Solar fluctuations don't move ``load`` —
     only real facility consumption does.
  2. Detect step changes in ``load`` around ±11 kW. Each step = one or more
     printer starts/stops; ``n_printers = round(ΔP / 11)`` decodes multi-unit
     steps that occurred within the smoothing window.
  3. Integrate the events to get ``concurrent(t)`` — printers running at each
     minute — and subtract ``concurrent(t) × 11 kW`` from load to expose the
     baseline.
  4. Aggregate the baseline per hour and write to InfluxDB as
     ``baseline_hourly`` tagged by (dow, is_weekend, is_holiday, month, hour).
     ``predict_baseline(dt)`` then returns the median of all prior days that
     match on those tags — robust even with 2 months of data.
"""

import json
import logging
import statistics
from datetime import date, datetime, time, timedelta, timezone

from influxdb_client import Point
from influxdb_client.client.write_api import SYNCHRONOUS

from config import INFLUXDB_BUCKET
from data import (
    _CET, _cet_now, _client, _hourly_records, _is_holiday, _load_pricing,
    _query_api, _write_api,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Detector constants
# ---------------------------------------------------------------------------
#
# HP MJF printers don't draw a flat 11 kW — they oscillate heavily (5–30 kW
# peaks) as fusing lamps, heating agents and motion phases cycle. Detecting
# individual "steps" on 1-min data massively over-counts starts.
#
# Instead we work on 15-min means (matching the regulatory quarter-hour
# maximeter window), subtract a per-day baseline proxy derived from the
# night hours (nobody prints at 3 AM), and decode printer count as
# ``round(net_load / 11)``. Events are transitions in that count.

PRINTER_NOMINAL_KW = 11.0        # HP MJF average draw
BIN_MIN = 15                     # analysis window (minutes)
ACTIVATION_THRESHOLD = 0.4       # need net ≥ 0.4 × 11 kW to count 1 printer
NIGHT_START_H = 0
NIGHT_END_H = 5                  # exclusive — 00:00–05:00 CET as "nobody prints"
SMOOTH_BINS = 3                  # 3 × 15 min = 45 min majority smoothing

BASELINE_MEASUREMENT = "baseline_hourly"


# ---------------------------------------------------------------------------
# Data assembly
# ---------------------------------------------------------------------------

def _load_1min_series(d1, d2):
    """Return ordered list of (datetime_CET, load_kW) at 1-minute resolution.

    ``load = ksem × overall_calibration + piko`` (both in kW). Solar is
    added back so transient cloud cover doesn't look like a step to the
    detector.
    """
    cal = _load_pricing().get("ksem_calibration", {}).get("overall", 1.0)

    ksem_records = _hourly_records(f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {d1.isoformat()}, stop: {d2.isoformat()})
          |> filter(fn: (r) => r._measurement == "ksem")
          |> filter(fn: (r) => r._field == "active_power_total")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')
    piko_records = _hourly_records(f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: {d1.isoformat()}, stop: {d2.isoformat()})
          |> filter(fn: (r) => r._measurement == "piko")
          |> filter(fn: (r) => r._field == "ac_power_total")
          |> aggregateWindow(every: 1m, fn: mean, createEmpty: false)
    ''')

    ksem_by_t = {t.replace(second=0, microsecond=0): v for t, v in ksem_records}
    piko_by_t = {t.replace(second=0, microsecond=0): v for t, v in piko_records}

    all_t = sorted(set(ksem_by_t) | set(piko_by_t))
    series = []
    for t in all_t:
        k = (ksem_by_t.get(t) or 0.0) * cal
        p = piko_by_t.get(t) or 0.0
        # KSEM active_power_total is in W (positive import, negative export).
        # PIKO ac_power_total is also in W (positive during production).
        load_w = k + p
        series.append((t, load_w / 1000.0))
    return series


# ---------------------------------------------------------------------------
# Step detection
# ---------------------------------------------------------------------------

def _resample_15min(series_1min):
    """Average 1-min load samples into clean 15-min bins aligned to :00/:15/:30/:45."""
    bins = {}
    for t, v in series_1min:
        bucket = t.replace(minute=(t.minute // BIN_MIN) * BIN_MIN,
                            second=0, microsecond=0)
        bins.setdefault(bucket, []).append(v)
    return {k: sum(v) / len(v) for k, v in bins.items() if v}


def _estimate_baseline_proxy(avg_15, minimum=0.5, cap=8.0):
    """Estimate the non-printer baseline using the night hours 00:00–05:00."""
    night_vals = [v for k, v in avg_15.items()
                  if NIGHT_START_H <= k.hour < NIGHT_END_H]
    if not night_vals:
        return 1.0
    proxy = statistics.median(night_vals)
    return max(minimum, min(proxy, cap))


def _concurrent_per_bin(avg_15, baseline_proxy, printer_kw=PRINTER_NOMINAL_KW):
    """Decode printer count per 15-min bin using ``round((load - base) / 11)``.

    Returns ``{bucket_ts: concurrent_count}`` then smooths with a rolling
    majority of ``SMOOTH_BINS`` bins so isolated spikes don't toggle a phantom
    start/stop pair.
    """
    raw = {}
    threshold = ACTIVATION_THRESHOLD * printer_kw
    for k, load in avg_15.items():
        net = load - baseline_proxy
        raw[k] = max(0, round(net / printer_kw)) if net >= threshold else 0

    # Majority-of-3 smoothing (median of self + neighbours)
    sorted_keys = sorted(raw)
    smoothed = {}
    for i, k in enumerate(sorted_keys):
        window = sorted_keys[max(0, i - SMOOTH_BINS // 2):
                             i + SMOOTH_BINS // 2 + 1]
        vals = [raw[kk] for kk in window]
        smoothed[k] = int(statistics.median(vals))
    return smoothed


def detect_printer_events(avg_15, concurrent_by_bin):
    """Convert a ``{bucket: count}`` series into start/stop transition events."""
    events = []
    sorted_keys = sorted(concurrent_by_bin)
    prev = 0
    for k in sorted_keys:
        cur = concurrent_by_bin[k]
        if cur != prev:
            diff = cur - prev
            events.append({
                "ts": k,
                "type": "start" if diff > 0 else "stop",
                "n_printers": abs(diff),
                "load_kw": round(avg_15.get(k, 0.0), 2),
            })
            prev = cur
    return events


# ---------------------------------------------------------------------------
# Daily profile
# ---------------------------------------------------------------------------

def classify_day(d):
    """Return features for a given date used as InfluxDB tags."""
    dt_noon = datetime(d.year, d.month, d.day, 12, tzinfo=_CET)
    return {
        "date": d.isoformat(),
        "dow": d.strftime("%a").lower(),
        "month": d.month,
        "is_weekend": d.weekday() >= 5,
        "is_holiday": _is_holiday(dt_noon),
    }


def build_daily_profile(d, printer_kw=PRINTER_NOMINAL_KW):
    """Compute the full baseline/load profile for a given date.

    Algorithm:
      1. Load 1-min load = ksem × cal + piko (kW, solar-agnostic).
      2. Resample to 15-min means aligned to the regulatory quarter-hour.
      3. Estimate baseline proxy from night hours (00–05 CET).
      4. Per bin, concurrent = round((load − proxy) / 11) if net ≥ 0.4 × 11.
      5. Smooth concurrent with 3-bin median so transient cycles don't toggle.
      6. Aggregate hourly baseline = load − concurrent × 11 (clamped ≥ 0).

    Returns a dict with ``events``, ``hours``, ``totals`` and metadata.
    """
    d1 = datetime(d.year, d.month, d.day, tzinfo=_CET)
    d2 = d1 + timedelta(days=1)
    series = _load_1min_series(d1, d2)
    if not series:
        return None

    avg_15 = _resample_15min(series)
    if not avg_15:
        return None
    baseline_proxy = _estimate_baseline_proxy(avg_15)
    concurrent_by_bin = _concurrent_per_bin(avg_15, baseline_proxy, printer_kw=printer_kw)
    events = detect_printer_events(avg_15, concurrent_by_bin)

    # Aggregate to hourly — baseline per bin = load − concurrent × 11 (≥ 0)
    hourly = {h: {"base": [], "load": [], "concurrent": []} for h in range(24)}
    for k, load in avg_15.items():
        c = concurrent_by_bin.get(k, 0)
        base = max(0.0, load - c * printer_kw)
        h = k.hour
        hourly[h]["base"].append(base)
        hourly[h]["load"].append(load)
        hourly[h]["concurrent"].append(c)

    hours_out = {}
    total_base_kwh = 0.0
    total_load_kwh = 0.0
    for h in range(24):
        bucket = hourly[h]
        if not bucket["base"]:
            hours_out[h] = None
            continue
        base_mean = sum(bucket["base"]) / len(bucket["base"])
        load_mean = sum(bucket["load"]) / len(bucket["load"])
        conc_max = max(bucket["concurrent"])
        hours_out[h] = {
            "baseline_kw": round(base_mean, 3),
            "load_kw": round(load_mean, 3),
            "concurrent_max": conc_max,
        }
        total_base_kwh += base_mean
        total_load_kwh += load_mean

    printer_kwh = total_load_kwh - total_base_kwh
    start_count = sum(e["n_printers"] for e in events if e["type"] == "start")
    return {
        "date": d.isoformat(),
        "day_features": classify_day(d),
        "baseline_proxy_kw": round(baseline_proxy, 2),
        "events": [{
            "ts": e["ts"].isoformat(),
            "type": e["type"],
            "n_printers": e["n_printers"],
            "load_kw": e["load_kw"],
        } for e in events],
        "hours": hours_out,
        "totals": {
            "baseline_kwh": round(total_base_kwh, 2),
            "load_kwh": round(total_load_kwh, 2),
            "printer_kwh": round(printer_kwh, 2),
            "printer_runs": start_count,
        },
    }


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def write_daily_profile(profile):
    """Write one day's hourly rows to InfluxDB."""
    if profile is None:
        return 0
    feat = profile["day_features"]
    tags = {
        "dow": feat["dow"],
        "is_weekend": "yes" if feat["is_weekend"] else "no",
        "is_holiday": "yes" if feat["is_holiday"] else "no",
        "month": str(feat["month"]),
    }
    d = datetime.fromisoformat(profile["date"]).date()
    points = []
    for h, row in profile["hours"].items():
        if row is None:
            continue
        ts = datetime(d.year, d.month, d.day, h, tzinfo=_CET)
        p = Point(BASELINE_MEASUREMENT)
        for k, v in tags.items():
            p = p.tag(k, v)
        p = p.tag("hour", f"{h:02d}")
        p = p.field("baseline_kw", float(row["baseline_kw"]))
        p = p.field("load_kw", float(row["load_kw"]))
        p = p.field("concurrent_max", int(row["concurrent_max"]))
        p = p.time(ts.astimezone(timezone.utc))
        points.append(p)
    if points:
        _write_api.write(bucket=INFLUXDB_BUCKET, record=points)
    return len(points)


def backfill_profiles(start_date, end_date, printer_kw=PRINTER_NOMINAL_KW):
    """Compute and write profiles for each date in [start_date, end_date]."""
    current = start_date
    written = 0
    processed = 0
    errors = []
    while current <= end_date:
        try:
            prof = build_daily_profile(current, printer_kw=printer_kw)
            if prof is not None:
                n = write_daily_profile(prof)
                written += n
                processed += 1
        except Exception as e:
            _log.exception("Backfill error on %s", current)
            errors.append({"date": current.isoformat(), "error": str(e)})
        current += timedelta(days=1)
    return {
        "days_processed": processed,
        "rows_written": written,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Prediction
# ---------------------------------------------------------------------------

def predict_baseline(dt):
    """Predict baseline kW for an arbitrary hour using stored profiles.

    Matches all stored rows with the same (is_weekend, is_holiday, month, hour)
    as ``dt`` and returns the median, with a confidence band and sample size.
    Falls back to broader buckets if the tight bucket has < 3 samples.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_CET)
    target_ct = dt.astimezone(_CET)
    is_weekend = target_ct.weekday() >= 5
    is_holiday = _is_holiday(target_ct)
    month = target_ct.month
    hour = target_ct.hour

    def _query(tags):
        flt = " and ".join(f'r.{k} == "{v}"' for k, v in tags.items())
        flux = f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -180d)
              |> filter(fn: (r) => r._measurement == "{BASELINE_MEASUREMENT}")
              |> filter(fn: (r) => r._field == "baseline_kw")
              |> filter(fn: (r) => {flt})
              |> keep(columns: ["_value"])
        '''
        values = []
        try:
            tables = _query_api.query(flux)
            for table in tables:
                for rec in table.records:
                    v = rec.get_value()
                    if v is not None:
                        values.append(v)
        except Exception:
            pass
        return values

    # Tiered fallback
    tiers = [
        {"is_weekend": "yes" if is_weekend else "no",
         "is_holiday": "yes" if is_holiday else "no",
         "month": str(month),
         "hour": f"{hour:02d}"},
        # Drop month (seasonal changes outside data range)
        {"is_weekend": "yes" if is_weekend else "no",
         "is_holiday": "yes" if is_holiday else "no",
         "hour": f"{hour:02d}"},
        # Drop holiday flag too
        {"is_weekend": "yes" if is_weekend else "no",
         "hour": f"{hour:02d}"},
    ]

    for i, tags in enumerate(tiers):
        vs = _query(tags)
        if len(vs) >= 3:
            vs_sorted = sorted(vs)
            median = statistics.median(vs_sorted)
            p10 = vs_sorted[max(0, int(len(vs_sorted) * 0.1))]
            p90 = vs_sorted[min(len(vs_sorted) - 1, int(len(vs_sorted) * 0.9))]
            return {
                "baseline_kw": round(median, 2),
                "p10_kw": round(p10, 2),
                "p90_kw": round(p90, 2),
                "samples": len(vs_sorted),
                "fallback_tier": i,  # 0 = tight match, 1/2 = broader
            }

    # No data at all
    return {
        "baseline_kw": 0.0,
        "p10_kw": 0.0,
        "p90_kw": 0.0,
        "samples": 0,
        "fallback_tier": -1,
    }


# ---------------------------------------------------------------------------
# Summary for dashboards
# ---------------------------------------------------------------------------

def update_yesterday():
    """Compute and store the profile for yesterday. Idempotent — if data for
    that date already exists, InfluxDB will upsert the same points."""
    yesterday = (_cet_now() - timedelta(days=1)).date()
    prof = build_daily_profile(yesterday)
    if prof is None:
        _log.info("consumption_model: no data for %s, skipping", yesterday)
        return None
    written = write_daily_profile(prof)
    _log.info("consumption_model: wrote %d rows for %s", written, yesterday)
    return {"date": yesterday.isoformat(), "rows_written": written}


def start_daily_thread():
    """Background thread that refreshes yesterday's profile at ~03:00 CET.

    Also triggers once at startup so containers coming up after a reboot
    catch up on missed days within a week.
    """
    import threading
    import time as _time

    def _loop():
        # Startup catch-up: any missing days in last 7 — keeps the model fresh
        # after container restarts without blocking request handling.
        try:
            today = _cet_now().date()
            for delta in range(7, 0, -1):
                d = today - timedelta(days=delta)
                prof = build_daily_profile(d)
                if prof is not None:
                    write_daily_profile(prof)
        except Exception:
            _log.exception("consumption_model: startup catch-up failed")

        while True:
            try:
                # Sleep until next 03:00 CET
                now = _cet_now()
                target = now.replace(hour=3, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                _time.sleep((target - now).total_seconds())
                update_yesterday()
            except Exception:
                _log.exception("consumption_model: daily update failed")
                _time.sleep(600)

    t = threading.Thread(target=_loop, daemon=True, name="consumption-daily")
    t.start()
    return t


def get_weekly_heatmap():
    """Return a 7×24 matrix of median baseline kW per (dow, hour).

    Used by the supervision page to visualise recurring consumption patterns.
    """
    flux = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -365d)
          |> filter(fn: (r) => r._measurement == "{BASELINE_MEASUREMENT}")
          |> filter(fn: (r) => r._field == "baseline_kw")
          |> keep(columns: ["_value", "dow", "hour"])
    '''
    dow_order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    buckets = {dow: {h: [] for h in range(24)} for dow in dow_order}
    try:
        tables = _query_api.query(flux)
        for table in tables:
            for rec in table.records:
                dow = rec.values.get("dow")
                hour = rec.values.get("hour")
                v = rec.get_value()
                if dow in buckets and hour is not None and v is not None:
                    try:
                        buckets[dow][int(hour)].append(v)
                    except (ValueError, TypeError):
                        continue
    except Exception:
        _log.exception("heatmap query failed")

    matrix = []
    for dow in dow_order:
        row = []
        for h in range(24):
            vs = buckets[dow][h]
            if vs:
                row.append({
                    "median": round(statistics.median(vs), 2),
                    "samples": len(vs),
                })
            else:
                row.append({"median": None, "samples": 0})
        matrix.append({"dow": dow, "hours": row})
    return {"matrix": matrix}


def get_concurrent_timeline(days=30):
    """Return peak / printer-hours / baseline kWh per day for the last N days.

    ``baseline_hourly`` is tagged heavily (dow, is_weekend, is_holiday, month,
    hour) so every row lives in its own series. We must ``group()`` first
    before aggregating, otherwise each series produces its own per-day max
    and the answer is much smaller than the true daily peak.
    """
    def _daily_flux(field, fn):
        return f'''
            from(bucket: "{INFLUXDB_BUCKET}")
              |> range(start: -{days}d)
              |> filter(fn: (r) => r._measurement == "{BASELINE_MEASUREMENT}")
              |> filter(fn: (r) => r._field == "{field}")
              |> group()
              |> aggregateWindow(every: 1d, fn: {fn},
                                 createEmpty: false, timeSrc: "_start")
        '''
    queries = {
        "peak_concurrent": _daily_flux("concurrent_max", "max"),
        "printer_hours": _daily_flux("concurrent_max", "sum"),
        "baseline_kwh": _daily_flux("baseline_kw", "sum"),
    }
    by_date = {}
    for key, q in queries.items():
        try:
            tables = _query_api.query(q)
            for table in tables:
                for rec in table.records:
                    t = rec.get_time()
                    if not t:
                        continue
                    d = t.astimezone(_CET).date()
                    by_date.setdefault(d, {})[key] = rec.get_value()
        except Exception:
            _log.exception("timeline query failed for %s", key)

    rows = []
    for d in sorted(by_date):
        r = by_date[d]
        rows.append({
            "date": d.isoformat(),
            "peak_concurrent": int(r.get("peak_concurrent") or 0),
            "printer_hours": int(r.get("printer_hours") or 0),
            "baseline_kwh": round(r.get("baseline_kwh") or 0.0, 1),
        })
    return {"days": rows}


def get_model_stats():
    """Return high-level stats about the stored model."""
    flux = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -365d)
          |> filter(fn: (r) => r._measurement == "{BASELINE_MEASUREMENT}")
          |> filter(fn: (r) => r._field == "baseline_kw")
          |> group(columns: ["dow", "is_weekend", "is_holiday", "hour"])
          |> count()
    '''
    total_rows = 0
    unique_days = set()
    by_dow = {}
    try:
        tables = _query_api.query(flux)
        for table in tables:
            for rec in table.records:
                total_rows += rec.get_value()
    except Exception:
        pass
    # Days present
    days_flux = f'''
        from(bucket: "{INFLUXDB_BUCKET}")
          |> range(start: -365d)
          |> filter(fn: (r) => r._measurement == "{BASELINE_MEASUREMENT}")
          |> filter(fn: (r) => r._field == "baseline_kw")
          |> keep(columns: ["_time"])
    '''
    try:
        tables = _query_api.query(days_flux)
        for table in tables:
            for rec in table.records:
                t = rec.get_time()
                if t:
                    unique_days.add(t.astimezone(_CET).date())
    except Exception:
        pass
    return {
        "total_rows": total_rows,
        "unique_days": len(unique_days),
        "first_day": min(unique_days).isoformat() if unique_days else None,
        "last_day": max(unique_days).isoformat() if unique_days else None,
    }
