"""Invoice companion app for Solar Dashboard v2.

Parses Iberdrola 3.0TD invoices (PDF), extracts billing data,
and compares fixed-rate costs against OMIE indexed prices.
"""

import json
import os
import re
from datetime import datetime, timedelta

import fitz  # pymupdf
from flask import Flask, render_template, request, redirect, url_for, flash
from influxdb_client import InfluxDBClient

app = Flask(__name__)
app.secret_key = os.urandom(24)

INVOICES_DIR = "/app/invoices"
PRICING_PATH = "/app/pricing.json"
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "solar")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "solar")

os.makedirs(INVOICES_DIR, exist_ok=True)


def load_pricing():
    """Load tariff configuration from pricing.json."""
    with open(PRICING_PATH) as f:
        return json.load(f)


def get_influx_client():
    return InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)


# ---------------------------------------------------------------------------
# PDF Parsing
# ---------------------------------------------------------------------------

def parse_invoice_pdf(filepath):
    """Extract billing data from an Iberdrola 3.0TD invoice PDF.

    Returns a dict with:
      - filename, billing_start, billing_end, billing_days
      - consumption: {P1..P6: kWh}
      - energy_cost, power_cost, electricity_tax, iva, total
      - injection_kwh, injection_income
    """
    doc = fitz.open(filepath)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()

    result = {
        "filename": os.path.basename(filepath),
        "consumption": {},
        "rates": {},
    }

    # Billing period: look for patterns like "01/01/2025 a 31/01/2025" or similar
    period_match = re.search(
        r"(\d{2}/\d{2}/\d{4})\s*(?:a|al|-)\s*(\d{2}/\d{2}/\d{4})", text
    )
    if period_match:
        result["billing_start"] = period_match.group(1)
        result["billing_end"] = period_match.group(2)
        try:
            d1 = datetime.strptime(period_match.group(1), "%d/%m/%Y")
            d2 = datetime.strptime(period_match.group(2), "%d/%m/%Y")
            result["billing_days"] = (d2 - d1).days
        except ValueError:
            result["billing_days"] = 0
    else:
        result["billing_start"] = ""
        result["billing_end"] = ""
        result["billing_days"] = 0

    # Consumption per period: "P1: 1234 kWh" or "Periodo 1 ... 1234"
    for p in range(1, 7):
        pattern = rf"P{p}[:\s]+[\d.,]+\s*kWh\s*[\d.,]+\s*€/kWh\s*([\d.,]+)"
        m = re.search(pattern, text)
        if m:
            result["consumption"][f"P{p}"] = float(m.group(1).replace(",", "."))
            continue
        # Alternative: table row format "P1  1234  0.192453  237.53"
        pattern2 = rf"P{p}\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)"
        m2 = re.search(pattern2, text)
        if m2:
            result["consumption"][f"P{p}"] = float(m2.group(1).replace(",", "."))
            result["rates"][f"P{p}"] = float(m2.group(2).replace(",", "."))

    # Total consumption
    total_kwh_match = re.search(r"[Tt]otal\s+[Cc]onsum[oi]\s*:?\s*([\d.,]+)\s*kWh", text)
    if total_kwh_match:
        result["total_consumption_kwh"] = float(
            total_kwh_match.group(1).replace(".", "").replace(",", ".")
        )
    else:
        result["total_consumption_kwh"] = sum(result["consumption"].values())

    # Monetary amounts
    for label, key in [
        (r"[Ee]nerg[ií]a", "energy_cost"),
        (r"[Pp]otencia", "power_cost"),
        (r"[Ii]mpuesto\s+[Ee]lectricidad", "electricity_tax"),
        (r"IVA", "iva"),
        (r"[Tt]otal\s+[Ff]actura", "total"),
        (r"[Ee]xcedente|[Ii]nyecci[oó]n|[Cc]ompensaci[oó]n", "injection_income"),
    ]:
        m = re.search(rf"{label}.*?([\d.,]+)\s*€", text)
        if m:
            val = m.group(1).replace(".", "").replace(",", ".")
            try:
                result[key] = float(val)
            except ValueError:
                pass

    # Injection kWh
    inj_match = re.search(
        r"[Ee]xcedente|[Ii]nyecci[oó]n.*?([\d.,]+)\s*kWh", text
    )
    if inj_match:
        result["injection_kwh"] = float(inj_match.group(1).replace(",", "."))

    return result


# ---------------------------------------------------------------------------
# OMIE comparison
# ---------------------------------------------------------------------------

def get_omie_avg_price(start_date, end_date):
    """Query InfluxDB for average OMIE price in a billing period."""
    client = get_influx_client()
    query_api = client.query_api()

    start_str = start_date.strftime("%Y-%m-%dT00:00:00Z")
    end_str = end_date.strftime("%Y-%m-%dT23:59:59Z")

    query = f'''
    from(bucket: "{INFLUXDB_BUCKET}")
      |> range(start: {start_str}, stop: {end_str})
      |> filter(fn: (r) => r._measurement == "omie_prices")
      |> filter(fn: (r) => r._field == "price_eur_kwh")
      |> mean()
    '''

    try:
        tables = query_api.query(query)
        for table in tables:
            for record in table.records:
                return record.get_value()
    except Exception:
        pass
    finally:
        client.close()
    return None


def build_analysis(invoice_data):
    """Build cost analysis comparing fixed rate vs OMIE indexed."""
    pricing = load_pricing()
    analysis = {
        "invoice": invoice_data,
        "pricing": pricing,
        "fixed_rate": pricing["energy"]["effective_rate_eur_kwh"],
    }

    total_kwh = invoice_data.get("total_consumption_kwh", 0)
    fixed_cost = total_kwh * pricing["energy"]["effective_rate_eur_kwh"]
    analysis["fixed_energy_cost"] = round(fixed_cost, 2)

    # Try to get OMIE comparison
    omie_avg = None
    if invoice_data.get("billing_start") and invoice_data.get("billing_end"):
        try:
            d1 = datetime.strptime(invoice_data["billing_start"], "%d/%m/%Y")
            d2 = datetime.strptime(invoice_data["billing_end"], "%d/%m/%Y")
            omie_avg = get_omie_avg_price(d1, d2)
        except ValueError:
            pass

    if omie_avg is not None:
        analysis["omie_avg_eur_kwh"] = round(omie_avg, 6)
        analysis["omie_energy_cost"] = round(total_kwh * omie_avg, 2)
        analysis["savings_vs_omie"] = round(fixed_cost - (total_kwh * omie_avg), 2)
    else:
        analysis["omie_avg_eur_kwh"] = None
        analysis["omie_energy_cost"] = None
        analysis["savings_vs_omie"] = None

    # Power cost estimate
    days = invoice_data.get("billing_days", 30) or 30
    power_cost = 0
    for period, rate in pricing["power_charges_eur_kw_day"].items():
        kw = pricing["contracted_power_kw"].get(period, 69)
        power_cost += rate * kw * days
    analysis["power_cost_estimate"] = round(power_cost, 2)

    # Tax estimates
    base = fixed_cost + power_cost
    elec_tax = base * pricing["taxes"]["electricity_tax_pct"] / 100
    fixed_daily = sum(pricing["fixed_charges_eur_day"].values()) * days
    subtotal = base + elec_tax + fixed_daily
    iva = subtotal * pricing["taxes"]["iva_pct"] / 100
    analysis["electricity_tax_estimate"] = round(elec_tax, 2)
    analysis["fixed_charges_estimate"] = round(fixed_daily, 2)
    analysis["iva_estimate"] = round(iva, 2)
    analysis["total_estimate"] = round(subtotal + iva, 2)

    # Injection compensation
    inj_kwh = invoice_data.get("injection_kwh", 0) or 0
    analysis["injection_compensation"] = round(
        inj_kwh * pricing["injection"]["price_eur_kwh"], 2
    )

    # Optimization suggestions
    analysis["suggestions"] = []
    if omie_avg is not None:
        if omie_avg < pricing["energy"]["effective_rate_eur_kwh"]:
            analysis["suggestions"].append(
                f"Una tarifa indexada hauria estalviat {abs(analysis['savings_vs_omie']):.2f} EUR "
                f"en aquest període (OMIE mitjà: {omie_avg*1000:.2f} EUR/MWh vs fix: "
                f"{pricing['energy']['effective_rate_eur_kwh']*1000:.2f} EUR/MWh)"
            )
        else:
            analysis["suggestions"].append(
                f"La tarifa fixa ha estat més barata que l'indexada per {analysis['savings_vs_omie']:.2f} EUR"
            )

    if total_kwh > 0:
        effective = invoice_data.get("total", analysis["total_estimate"]) / total_kwh
        analysis["effective_eur_kwh"] = round(effective, 4)
    else:
        analysis["effective_eur_kwh"] = 0

    return analysis


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _list_invoices():
    """List all parsed invoices from the invoices directory."""
    invoices = []
    for f in sorted(os.listdir(INVOICES_DIR)):
        if f.endswith(".json"):
            with open(os.path.join(INVOICES_DIR, f)) as fp:
                invoices.append(json.load(fp))
    return invoices


@app.route("/")
def index():
    invoices = _list_invoices()
    return render_template("upload.html", invoices=invoices)


@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        flash("No s'ha seleccionat cap fitxer", "error")
        return redirect(url_for("index"))

    file = request.files["file"]
    if file.filename == "":
        flash("No s'ha seleccionat cap fitxer", "error")
        return redirect(url_for("index"))

    if not file.filename.lower().endswith(".pdf"):
        flash("Només s'accepten fitxers PDF", "error")
        return redirect(url_for("index"))

    # Save PDF
    safe_name = re.sub(r"[^\w\-.]", "_", file.filename)
    pdf_path = os.path.join(INVOICES_DIR, safe_name)
    file.save(pdf_path)

    # Parse
    try:
        invoice_data = parse_invoice_pdf(pdf_path)
    except Exception as e:
        flash(f"Error analitzant el PDF: {e}", "error")
        return redirect(url_for("index"))

    # Save parsed data as JSON
    json_path = os.path.join(INVOICES_DIR, safe_name.replace(".pdf", ".json"))
    with open(json_path, "w") as fp:
        json.dump(invoice_data, fp, indent=2, default=str)

    flash(f"Factura '{file.filename}' analitzada correctament", "success")
    return redirect(url_for("analysis", filename=safe_name.replace(".pdf", ".json")))


@app.route("/analysis/<filename>")
def analysis(filename):
    json_path = os.path.join(INVOICES_DIR, filename)
    if not os.path.exists(json_path):
        flash("Factura no trobada", "error")
        return redirect(url_for("index"))

    with open(json_path) as fp:
        invoice_data = json.load(fp)

    analysis_data = build_analysis(invoice_data)
    return render_template("analysis.html", data=analysis_data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
