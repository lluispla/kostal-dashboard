"""Flask dashboard — replaces Grafana + invoice-app."""

import json
import os
import re

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify

from config import INVOICES_DIR, PRICING_PATH
from data import get_all_dashboard_data, get_historic_data, _load_pricing, invalidate_pricing_caches
from invoice import parse_invoice_pdf, build_analysis, list_invoices
from comparador import get_comparador_data, add_offer, update_offer, delete_offer

app = Flask(__name__)
app.secret_key = os.urandom(24)

os.makedirs(INVOICES_DIR, exist_ok=True)


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

    flash(f"Factura '{file.filename}' analitzada correctament", "success")
    return redirect(url_for("factures_analysis", filename=safe_name.replace(".pdf", ".json")))


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
# Configuració
# ---------------------------------------------------------------------------

@app.route("/configuracio")
def configuracio():
    pricing = _load_pricing()
    # Ensure rates_eur_kwh exists for template
    if "rates_eur_kwh" not in pricing.get("energy", {}):
        eff = pricing["energy"]["effective_rate_eur_kwh"]
        pricing["energy"]["rates_eur_kwh"] = {f"P{i}": eff for i in range(1, 7)}
    return render_template("configuracio.html", p=pricing)


@app.route("/configuracio", methods=["POST"])
def save_configuracio():
    f = request.form
    periods = ["P1", "P2", "P3", "P4", "P5", "P6"]

    # Build per-period energy rates
    rates = {}
    for p in periods:
        rates[p] = float(f.get(f"rate_{p}", 0))

    # Compute effective rate as average (used by economia calculations)
    rate_values = list(rates.values())
    effective = sum(rate_values) / len(rate_values) if rate_values else 0

    pricing = _load_pricing()

    # Energy
    pricing["energy"]["rates_eur_kwh"] = rates
    pricing["energy"]["effective_rate_eur_kwh"] = round(effective, 6)
    pricing["energy"]["base_rate_eur_kwh"] = float(f.get("base_rate_eur_kwh", 0))
    pricing["energy"]["discount_pct"] = float(f.get("discount_pct", 0))

    # Contracted power
    for p in periods:
        pricing["contracted_power_kw"][p] = float(f.get(f"power_{p}", 69))

    # Power charges
    for p in periods:
        pricing["power_charges_eur_kw_day"][p] = float(f.get(f"pcharge_{p}", 0))

    # Taxes
    pricing["taxes"]["electricity_tax_pct"] = float(f.get("electricity_tax_pct", 5.11))
    pricing["taxes"]["iva_pct"] = float(f.get("iva_pct", 21))

    # Fixed charges
    pricing["fixed_charges_eur_day"]["equipment_rental"] = float(f.get("equipment_rental", 0))
    pricing["fixed_charges_eur_day"]["bono_social"] = float(f.get("bono_social", 0))

    # Injection
    pricing["injection"]["price_eur_kwh"] = float(f.get("injection_price", 0.05))

    # Indexed tariff
    for p in periods:
        pricing["indexed_tariff"]["peajes_eur_kwh"][p] = float(f.get(f"peaje_{p}", 0))
        pricing["indexed_tariff"]["cargos_eur_kwh"][p] = float(f.get(f"cargo_{p}", 0))
    pricing["indexed_tariff"]["margin_comercialitzadora_eur_kwh"] = float(
        f.get("indexed_margin", 0.01))

    # Write and invalidate caches
    with open(PRICING_PATH, "w") as fp:
        json.dump(pricing, fp, indent=2)
    invalidate_pricing_caches()

    flash("Configuració desada correctament", "success")
    return redirect(url_for("configuracio"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
