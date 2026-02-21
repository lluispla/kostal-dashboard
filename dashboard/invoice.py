"""Invoice parsing & analysis — moved from invoice-app/app.py."""

import json
import os
import re
from datetime import datetime

import fitz  # pymupdf

from config import INVOICES_DIR, PRICING_PATH, INFLUXDB_BUCKET
from data import _query_api


def load_pricing():
    """Load tariff configuration from pricing.json."""
    with open(PRICING_PATH) as f:
        return json.load(f)


def list_invoices():
    """List all parsed invoices from the invoices directory."""
    os.makedirs(INVOICES_DIR, exist_ok=True)
    invoices = []
    for f in sorted(os.listdir(INVOICES_DIR)):
        if f.endswith(".json"):
            with open(os.path.join(INVOICES_DIR, f)) as fp:
                invoices.append(json.load(fp))
    return invoices


def parse_invoice_pdf(filepath):
    """Extract billing data from an Iberdrola 3.0TD invoice PDF."""
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

    # Billing period
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

    # Consumption per period
    for p in range(1, 7):
        pattern = rf"P{p}[:\s]+[\d.,]+\s*kWh\s*[\d.,]+\s*\u20ac/kWh\s*([\d.,]+)"
        m = re.search(pattern, text)
        if m:
            result["consumption"][f"P{p}"] = float(m.group(1).replace(",", "."))
            continue
        pattern2 = rf"P{p}\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)"
        m2 = re.search(pattern2, text)
        if m2:
            result["consumption"][f"P{p}"] = float(m2.group(1).replace(",", "."))
            result["rates"][f"P{p}"] = float(m2.group(2).replace(",", "."))

    # Total consumption
    total_kwh_match = re.search(
        r"[Tt]otal\s+[Cc]onsum[oi]\s*:?\s*([\d.,]+)\s*kWh", text
    )
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
        m = re.search(rf"{label}.*?([\d.,]+)\s*\u20ac", text)
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


def get_omie_avg_price(start_date, end_date):
    """Query InfluxDB for average OMIE price in a billing period."""
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
        tables = _query_api.query(query)
        for table in tables:
            for record in table.records:
                return record.get_value()
    except Exception:
        pass
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

    # OMIE comparison
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
                f"Una tarifa indexada hauria estalviat {abs(analysis['savings_vs_omie']):.2f} \u20ac "
                f"en aquest periode (OMIE mitja: {omie_avg*1000:.2f} \u20ac/MWh vs fix: "
                f"{pricing['energy']['effective_rate_eur_kwh']*1000:.2f} \u20ac/MWh)"
            )
        else:
            analysis["suggestions"].append(
                f"La tarifa fixa ha estat mes barata que l'indexada per {analysis['savings_vs_omie']:.2f} \u20ac"
            )

    if total_kwh > 0:
        effective = invoice_data.get("total", analysis["total_estimate"]) / total_kwh
        analysis["effective_eur_kwh"] = round(effective, 4)
    else:
        analysis["effective_eur_kwh"] = 0

    return analysis
