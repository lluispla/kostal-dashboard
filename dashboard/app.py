"""Flask dashboard — replaces Grafana + invoice-app."""

import json
import os
import re

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from config import INVOICES_DIR, PRICING_PATH
from data import get_all_dashboard_data, get_historic_data, get_previsio_solar_data, get_amortitzacio_data, _load_pricing, invalidate_pricing_caches, get_load_shifting, get_ev_solar_data, get_iberdrola_comparison, _get_period, _cet_now, get_consum_preus_data, ingest_official_meter_csv, get_official_vs_ksem_comparison, get_yoy_comparison, get_yoy_comparison_range, recover_data_from_backup
from invoice import parse_invoice_pdf, build_analysis, list_invoices, get_invoice_trends
from comparador import get_comparador_data, add_offer, update_offer, delete_offer
from simulator import get_estadistiques_data

app = Flask(__name__)
app.secret_key = os.urandom(24)

os.makedirs(INVOICES_DIR, exist_ok=True)


@app.template_filter("nd")
def _format_or_nd(value, spec="%.0f"):
    """Format a number, or render 'n/d' when it is None.

    data.py returns None for anything it cannot measure (e.g. consumption while
    an inverter is offline). Formatting that as a number would print a confident
    0 for a quantity nobody knows.
    """
    return "n/d" if value is None else spec % value


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    data = get_all_dashboard_data()
    return render_template("dashboard.html", d=data)


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(get_all_dashboard_data())


# ---------------------------------------------------------------------------
# Històric
# ---------------------------------------------------------------------------

@app.route("/historic")
def historic():
    return render_template("historic.html")


@app.route("/api/historic/<time_range>")
def api_historic(time_range):
    if time_range not in ("7d", "30d", "90d", "1y", "all"):
        return jsonify({"error": "Invalid range"}), 400
    return jsonify(get_historic_data(time_range))


# ---------------------------------------------------------------------------
# Estadístiques
# ---------------------------------------------------------------------------

@app.route("/estadistiques")
def estadistiques():
    return render_template("estadistiques.html")


@app.route("/api/estadistiques/<time_range>")
def api_estadistiques(time_range):
    if time_range not in ("3m", "6m", "1y", "all"):
        return jsonify({"error": "Invalid range"}), 400
    return jsonify(get_estadistiques_data(time_range))


# ---------------------------------------------------------------------------
# Previsió Solar
# ---------------------------------------------------------------------------

@app.route("/previsio-solar")
def previsio_solar():
    return render_template("previsio_solar.html")


@app.route("/api/previsio-solar")
def api_previsio_solar():
    return jsonify(get_previsio_solar_data())


# ---------------------------------------------------------------------------
# Invoices
# ---------------------------------------------------------------------------

@app.route("/factures")
def factures():
    invoices = list_invoices()
    return render_template("invoices.html", invoices=invoices)


@app.route("/factures/upload", methods=["POST"])
def factures_upload():
    if "file" not in request.files:
        flash("No s'ha seleccionat cap fitxer", "error")
        return redirect(url_for("factures"))

    file = request.files["file"]
    if file.filename == "":
        flash("No s'ha seleccionat cap fitxer", "error")
        return redirect(url_for("factures"))

    if not file.filename.lower().endswith(".pdf"):
        flash("Nomes s'accepten fitxers PDF", "error")
        return redirect(url_for("factures"))

    safe_name = re.sub(r"[^\w\-.]", "_", file.filename)
    pdf_path = os.path.join(INVOICES_DIR, safe_name)
    file.save(pdf_path)

    try:
        invoice_data = parse_invoice_pdf(pdf_path)
    except Exception as e:
        flash(f"Error analitzant el PDF: {e}", "error")
        return redirect(url_for("factures"))

    json_path = os.path.join(INVOICES_DIR, safe_name.replace(".pdf", ".json"))
    with open(json_path, "w") as fp:
        json.dump(invoice_data, fp, indent=2, default=str)

    # Update ksem calibration from this invoice (Som Energia only)
    if invoice_data.get("supplier") == "som_energia":
        try:
            from invoice import update_calibration
            cal = update_calibration(invoice_data)
            if cal:
                flash(
                    f"Calibratge KSEM actualitzat (factor global: {cal['overall']:.3f})",
                    "info",
                )
        except Exception:
            pass  # calibration failure is non-critical

    flash(f"Factura '{file.filename}' analitzada correctament", "success")
    return redirect(url_for("factures_analysis", filename=safe_name.replace(".pdf", ".json")))


@app.route("/factures/upload-csv", methods=["POST"])
def factures_upload_csv():
    if "csv_file" not in request.files:
        flash("No s'ha seleccionat cap fitxer CSV", "error")
        return redirect(url_for("factures"))

    file = request.files["csv_file"]
    if file.filename == "":
        flash("No s'ha seleccionat cap fitxer CSV", "error")
        return redirect(url_for("factures"))

    if not file.filename.lower().endswith(".csv"):
        flash("Nomes s'accepten fitxers CSV", "error")
        return redirect(url_for("factures"))

    safe_name = re.sub(r"[^\w\-.]", "_", file.filename)
    csv_path = os.path.join(INVOICES_DIR, safe_name)
    file.save(csv_path)

    try:
        result = ingest_official_meter_csv(csv_path)
        flash(
            f"CSV importat: {result['hours_count']} hores, "
            f"{result['total_kwh']} kWh, "
            f"període {result['date_range']}",
            "success",
        )

        # Auto-calibrate ksem factor + other_costs from the ingested data
        try:
            from data import auto_calibrate_from_official
            # Parse date range from result
            dr = result["date_range"]  # "DD/MM/YYYY - DD/MM/YYYY"
            parts = dr.split(" - ")
            if len(parts) == 2:
                from datetime import datetime as _dt
                d1 = _dt.strptime(parts[0].strip(), "%d/%m/%Y")
                d2 = _dt.strptime(parts[1].strip(), "%d/%m/%Y")
                cal_result = auto_calibrate_from_official(
                    d1.strftime("%Y-%m-%d"),
                    d2.strftime("%Y-%m-%d"),
                )
                if cal_result:
                    cal = cal_result["ksem_calibration"]
                    msg = f"Calibratge KSEM: factor={cal['overall']:.3f} ({cal['matched_hours']} hores)"
                    if cal_result.get("other_costs"):
                        oc = cal_result["other_costs"]
                        msg += f" | other_costs={oc['value']:.4f} (de {oc['from_invoice']})"
                    flash(msg, "info")
        except Exception as e:
            flash(f"Calibratge automàtic parcial: {e}", "warning")

    except Exception as e:
        flash(f"Error important el CSV: {e}", "error")

    return redirect(url_for("factures"))


@app.route("/api/official-meter/<start>/<end>")
def api_official_meter_comparison(start, end):
    try:
        return jsonify(get_official_vs_ksem_comparison(start, end))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/factures/analysis/<filename>")
def factures_analysis(filename):
    json_path = os.path.join(INVOICES_DIR, filename)
    if not os.path.exists(json_path):
        flash("Factura no trobada", "error")
        return redirect(url_for("factures"))

    with open(json_path) as fp:
        invoice_data = json.load(fp)

    analysis_data = build_analysis(invoice_data)
    return render_template("analysis.html", data=analysis_data)


@app.route("/api/factures/trends")
def api_factures_trends():
    return jsonify(get_invoice_trends())


# ---------------------------------------------------------------------------
# Comparador d'ofertes
# ---------------------------------------------------------------------------

@app.route("/comparador")
def comparador():
    data = get_comparador_data()
    return render_template("comparador.html", d=data)


@app.route("/api/comparador/data")
def api_comparador_data():
    return jsonify(get_comparador_data())


@app.route("/api/iberdrola-comparison")
def api_iberdrola_comparison():
    return jsonify(get_iberdrola_comparison())


@app.route("/api/calibration-history")
def api_calibration_history():
    pricing = _load_pricing()
    return jsonify({
        "history": pricing.get("calibration_history", []),
        "current_other_costs": pricing.get("energy", {}).get(
            "contract_formula", {}).get("other_costs_eur_kwh", 0.046),
        "current_ksem_overall": pricing.get("ksem_calibration", {}).get("overall", 1.0),
    })


@app.route("/api/current-period")
def api_current_period():
    now = _cet_now()
    period = _get_period(now)
    return jsonify({"period": period, "time": now.strftime("%H:%M:%S")})


@app.route("/api/ofertes", methods=["POST"])
def api_ofertes_create():
    offer = request.get_json()
    if not offer:
        return jsonify({"error": "JSON body required"}), 400
    created = add_offer(offer)
    return jsonify(created), 201


@app.route("/api/ofertes/<offer_id>", methods=["PUT"])
def api_ofertes_update(offer_id):
    updates = request.get_json()
    if not updates:
        return jsonify({"error": "JSON body required"}), 400
    result = update_offer(offer_id, updates)
    if result is None:
        return jsonify({"error": "Oferta no trobada"}), 404
    return jsonify(result)


@app.route("/api/ofertes/<offer_id>", methods=["DELETE"])
def api_ofertes_delete(offer_id):
    if delete_offer(offer_id):
        return jsonify({"ok": True})
    return jsonify({"error": "Oferta no trobada"}), 404


# ---------------------------------------------------------------------------
# Amortització
# ---------------------------------------------------------------------------

@app.route("/amortitzacio")
def amortitzacio():
    return render_template("amortitzacio.html")


@app.route("/api/amortitzacio")
def api_amortitzacio():
    return jsonify(get_amortitzacio_data())


# ---------------------------------------------------------------------------
# Recomanacions (Load Shifting)
# ---------------------------------------------------------------------------

@app.route("/recomanacions")
def recomanacions():
    return render_template("recomanacions.html")


@app.route("/api/recomanacions")
def api_recomanacions():
    return jsonify(get_load_shifting())


# ---------------------------------------------------------------------------
# MJF Optimizer
# ---------------------------------------------------------------------------

@app.route("/optimitzador")
def optimitzador():
    return render_template("optimitzador.html")


@app.route("/api/optimitzador")
def api_optimitzador():
    import sys
    sys.path.insert(0, "/app/tools")
    from mjf_optimizer import get_optimizer_data
    return jsonify(get_optimizer_data())


# ---------------------------------------------------------------------------
# Load Scheduler
# ---------------------------------------------------------------------------

@app.route("/programador")
def programador():
    return render_template("programador.html")


@app.route("/api/programador")
def api_programador():
    from scheduler import get_scheduler_data
    try:
        load_kw = float(request.args.get("load_kw", 5.0))
        duration_h = int(request.args.get("duration_h", 4))
        lookahead_h = int(request.args.get("lookahead_h", 48))
        use_forecast = request.args.get("use_forecast", "1") not in ("0", "false")
        use_baseline = request.args.get("use_baseline", "1") not in ("0", "false")
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid parameters"}), 400
    try:
        return jsonify(get_scheduler_data(
            load_kw, duration_h, lookahead_h,
            use_forecast=use_forecast, use_baseline=use_baseline,
        ))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ---------------------------------------------------------------------------
# Consumption model (baseline profiles from history)
# ---------------------------------------------------------------------------

@app.route("/api/consumption-model/stats")
def api_consumption_stats():
    from consumption_model import get_model_stats
    return jsonify(get_model_stats())


@app.route("/api/consumption-model/backfill", methods=["POST"])
def api_consumption_backfill():
    from consumption_model import backfill_profiles
    from datetime import date, datetime as _dt, timedelta
    payload = request.get_json(silent=True) or {}
    try:
        start = _dt.strptime(payload.get("start", ""), "%Y-%m-%d").date() \
            if payload.get("start") else date.today() - timedelta(days=60)
        end = _dt.strptime(payload.get("end", ""), "%Y-%m-%d").date() \
            if payload.get("end") else date.today() - timedelta(days=1)
    except ValueError:
        return jsonify({"error": "Invalid date format, use YYYY-MM-DD"}), 400
    result = backfill_profiles(start, end)
    return jsonify({"start": start.isoformat(), "end": end.isoformat(), **result})


@app.route("/api/consumption-model/day/<date_str>")
def api_consumption_day(date_str):
    from consumption_model import build_daily_profile
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"error": "Invalid date format, use YYYY-MM-DD"}), 400
    prof = build_daily_profile(d)
    if prof is None:
        return jsonify({"error": "No data for this date"}), 404
    return jsonify(prof)


@app.route("/consum-model")
def consum_model():
    return render_template("consum_model.html")


@app.route("/api/consumption-model/heatmap")
def api_consumption_heatmap():
    from consumption_model import get_weekly_heatmap
    return jsonify(get_weekly_heatmap())


@app.route("/api/consumption-model/timeline")
def api_consumption_timeline():
    from consumption_model import get_concurrent_timeline
    days = int(request.args.get("days", 30))
    return jsonify(get_concurrent_timeline(days=days))


@app.route("/api/consumption-model/predict")
def api_consumption_predict():
    from consumption_model import predict_baseline
    from datetime import datetime as _dt
    ts = request.args.get("ts")
    if not ts:
        return jsonify({"error": "ts query parameter required (ISO8601)"}), 400
    try:
        dt = _dt.fromisoformat(ts)
    except ValueError:
        return jsonify({"error": "Invalid ISO8601 timestamp"}), 400
    return jsonify(predict_baseline(dt))


# ---------------------------------------------------------------------------
# Vehicle Solar (EV V2H)
# ---------------------------------------------------------------------------

@app.route("/vehicle-solar")
def vehicle_solar():
    return render_template("vehicle_solar.html")


@app.route("/api/vehicle-solar")
def api_vehicle_solar():
    return jsonify(get_ev_solar_data())


# ---------------------------------------------------------------------------
# Consum i Preus
# ---------------------------------------------------------------------------

@app.route("/consum-preus")
def consum_preus():
    return render_template("consum_preus.html")


@app.route("/api/consum-preus/<time_range>")
def api_consum_preus(time_range):
    if time_range not in ("today", "7d", "30d"):
        return jsonify({"error": "Invalid range"}), 400
    return jsonify(get_consum_preus_data(time_range))


# ---------------------------------------------------------------------------
# Comparació Anual (Year-over-Year)
# ---------------------------------------------------------------------------

@app.route("/comparacio-anual")
def comparacio_anual():
    return render_template("comparacio_anual.html")


@app.route("/api/comparacio-anual/<int:month>/<int:day>")
def api_comparacio_anual(month, day):
    if month < 1 or month > 12 or day < 1 or day > 31:
        return jsonify({"error": "Invalid date"}), 400
    return jsonify(get_yoy_comparison(month, day))


@app.route("/api/comparacio-anual/<mode>/<int:param>")
def api_comparacio_anual_range(mode, param):
    if mode not in ("week", "month", "year"):
        return jsonify({"error": "Invalid mode"}), 400
    return jsonify(get_yoy_comparison_range(mode, param))


# ---------------------------------------------------------------------------
# Configuració
# ---------------------------------------------------------------------------

@app.route("/configuracio")
def configuracio():
    pricing = _load_pricing()
    return render_template("configuracio.html", p=pricing)


@app.route("/configuracio", methods=["POST"])
def save_configuracio():
    f = request.form
    periods = ["P1", "P2", "P3", "P4", "P5", "P6"]

    pricing = _load_pricing()

    # Contract formula
    cf = pricing.setdefault("energy", {}).setdefault("contract_formula", {})
    cf["adjustment_multiplier"] = float(f.get("cf_multiplier", 1.015))
    cf["other_costs_eur_kwh"] = float(f.get("cf_other_costs", 0.046))
    cf["loss_coefficient"] = float(f.get("cf_losses", 0.12))
    cf["efficiency_fund_eur_kwh"] = float(f.get("cf_efficiency_fund", 0.001))
    cf["margin_eur_kwh"] = float(f.get("cf_margin", 0.00968))

    # Contracted power
    for p in periods:
        pricing["contracted_power_kw"][p] = float(f.get(f"power_{p}", 69))

    # Power charges (€/kW/year)
    for p in periods:
        pricing["power_charges_eur_kw_year"][p] = float(f.get(f"pcharge_{p}", 0))

    # Taxes
    pricing["taxes"]["electricity_tax_pct"] = float(f.get("electricity_tax_pct", 5.11269))
    pricing["taxes"]["iva_pct"] = float(f.get("iva_pct", 21))

    # Fixed charges
    pricing["fixed_charges_eur_day"]["equipment_rental"] = float(f.get("equipment_rental", 0))
    pricing["fixed_charges_eur_day"]["bono_social"] = float(f.get("bono_social", 0))

    # Indexed tariff peajes/cargos
    for p in periods:
        pricing["indexed_tariff"]["peajes_eur_kwh"][p] = float(f.get(f"peaje_{p}", 0))
        pricing["indexed_tariff"]["cargos_eur_kwh"][p] = float(f.get(f"cargo_{p}", 0))
    pricing["indexed_tariff"]["margin_comercialitzadora_eur_kwh"] = float(
        f.get("indexed_margin", 0.00968))

    # Flux Solar
    flux = pricing.setdefault("scenarios", {}).setdefault("som_indexada", {}).setdefault("flux_solar", {})
    flux["enabled"] = f.get("flux_enabled") == "on"
    flux["credit_pct"] = float(f.get("flux_credit_pct", 0.80))

    # Investment / Amortització
    inv = pricing.setdefault("investment", {})
    inv["installation_cost_eur"] = float(f.get("investment_cost", 50000))
    inv["installation_date"] = f.get("investment_date", "2026-02-01")
    inv["expected_lifetime_years"] = int(f.get("investment_lifetime", 25))
    inv["annual_maintenance_eur"] = float(f.get("investment_maintenance", 0))

    # EV Config
    evc = pricing.setdefault("ev_config", {})
    evc["enabled"] = f.get("ev_enabled") == "on"
    evc["model"] = f.get("ev_model", "KIA EV9 99.8 kWh")
    evc["battery_kwh"] = float(f.get("ev_battery", 96))
    evc["charger_kw"] = float(f.get("ev_charger", 11))
    evc["v2h_kw"] = float(f.get("ev_v2h", 11.5))
    evc["efficiency"] = float(f.get("ev_efficiency", 0.88))
    evc["daily_driving_kwh"] = float(f.get("ev_driving", 1.3))
    evc["home_rate_eur_kwh"] = float(f.get("ev_home_rate", 0.17))
    evc["charger_cost_eur"] = float(f.get("ev_charger_cost", 5000))
    evc["schedule_arrive"] = f.get("ev_arrive", "08:00")
    evc["schedule_depart"] = f.get("ev_depart", "18:00")
    evc["saturday"] = f.get("ev_saturday") == "on"
    evc["sunday"] = f.get("ev_sunday") == "on"
    evc["home_battery_kwh"] = float(f.get("ev_home_battery", 20))
    evc["home_battery_efficiency"] = float(f.get("ev_home_battery_eff", 0.90))
    evc["aerotermia_cop_heating"] = float(f.get("ev_cop_heating", 3.0))
    evc["aerotermia_cop_cooling"] = float(f.get("ev_cop_cooling", 2.5))
    evc["aerotermia_kw"] = float(f.get("ev_aerotermia_kw", 5))
    evc["home_electrical_kwh_night"] = float(f.get("ev_home_elec_night", 5))

    # Battery simulation
    bat = pricing.setdefault("battery_simulation", {})
    bat["capacity_kwh"] = float(f.get("bat_capacity", 20))
    bat["max_charge_kw"] = float(f.get("bat_max_charge", 10))
    bat["max_discharge_kw"] = float(f.get("bat_max_discharge", 10))
    bat["round_trip_efficiency"] = float(f.get("bat_efficiency", 0.90))

    # Write and invalidate caches
    with open(PRICING_PATH, "w") as fp:
        json.dump(pricing, fp, indent=2)
    invalidate_pricing_caches()

    flash("Configuració desada correctament", "success")
    return redirect(url_for("configuracio"))


# ---------------------------------------------------------------------------
# Data Recovery
# ---------------------------------------------------------------------------

@app.route("/api/recover-data", methods=["POST"])
def api_recover_data():
    hours = request.json.get("hours", 48) if request.is_json else 48
    hours = min(max(int(hours), 1), 168)  # clamp 1h–7d
    try:
        result = recover_data_from_backup(lookback_hours=hours)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    try:
        from consumption_model import start_daily_thread
        start_daily_thread()
    except Exception:
        import logging
        logging.exception("Failed to start consumption_model daily thread")
    # threaded=True: the dev server otherwise serves one request at a time, so a
    # single slow InfluxDB query (cold /api/dashboard ~3.7s) blocks the page load and
    # every other tab/refresh behind it — the UI freezes, then "suddenly works" when
    # the query finishes. Concurrent handling keeps the UI responsive during slow queries.
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
