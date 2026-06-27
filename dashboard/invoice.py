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


def _detect_supplier(text):
    """Detect invoice supplier from PDF text."""
    if "Som Energia" in text or "F55091367" in text:
        return "som_energia"
    return "iberdrola"


def parse_som_energia_pdf(text):
    """Parse a Som Energia invoice PDF text.

    Extracts period, per-period kWh, avg prices, and line totals.
    """
    result = {
        "supplier": "som_energia",
        "consumption": {},
        "rates": {},
    }

    # Period: "Període facturat: del DD/MM/YYYY al DD/MM/YYYY"
    period_match = re.search(
        r"[Pp]er[ií]ode\s+factura[dt]?\s*:?\s*(?:del\s+)?(\d{2}/\d{2}/\d{4})\s*(?:a|al)\s*(\d{2}/\d{2}/\d{4})",
        text
    )
    if period_match:
        result["billing_start"] = period_match.group(1)
        result["billing_end"] = period_match.group(2)
        try:
            d1 = datetime.strptime(period_match.group(1), "%d/%m/%Y")
            d2 = datetime.strptime(period_match.group(2), "%d/%m/%Y")
            # Som Energia: "del X al Y" is inclusive, so days = (Y - X) + 1
            result["billing_days"] = (d2 - d1).days + 1
        except ValueError:
            result["billing_days"] = 0
    else:
        # Fallback: any two dates
        dates = re.findall(r"(\d{2}/\d{2}/\d{4})", text)
        if len(dates) >= 2:
            result["billing_start"] = dates[0]
            result["billing_end"] = dates[1]
            try:
                d1 = datetime.strptime(dates[0], "%d/%m/%Y")
                d2 = datetime.strptime(dates[1], "%d/%m/%Y")
                result["billing_days"] = (d2 - d1).days + 1
            except ValueError:
                result["billing_days"] = 0
        else:
            result["billing_start"] = ""
            result["billing_end"] = ""
            result["billing_days"] = 0

    # Per-period kWh: "Electricitat utilitzada [kWh]" followed by 6 values (P1-P6)
    kwh_match = re.search(
        r"Electricitat utilitzada \[kWh\][^\n]*\n"
        r"([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)",
        text
    )
    if kwh_match:
        for i, p in enumerate(["P1", "P2", "P3", "P4", "P5", "P6"], 1):
            val = kwh_match.group(i).replace(".", "").replace(",", ".")
            result["consumption"][p] = float(val)

    # Per-period avg rate: "Preu mitjà de l'energia [€/kWh]" followed by 6 values
    rate_match = re.search(
        r"Preu mitj[àa] de l.energia \[€/kWh\]\n"
        r"([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)",
        text
    )
    if rate_match:
        for i, p in enumerate(["P1", "P2", "P3", "P4", "P5", "P6"], 1):
            val = rate_match.group(i).replace(",", ".")
            result["rates"][p] = float(val)

    # Total consumption
    result["total_consumption_kwh"] = sum(result["consumption"].values())

    # Monetary amounts — Som Energia format (values on next line after label)
    for label, key in [
        (r"Electricitat utilitzada", "energy_cost"),
        (r"Pot[eè]ncia contractada", "power_cost"),
        (r"Exc[eé]s de pot[eè]ncia", "excess_power_cost"),
        (r"Impost d.electricitat", "electricity_tax"),
        (r"IVA 21%", "iva"),
        (r"TOTAL IMPORT FACTURA", "total"),
        (r"Compensaci[oó] (?:simplificada|excedents)", "injection_income"),
        (r"Lloguer del comptador", "equipment_rental"),
        (r"Bo social", "bono_social"),
        (r"Altres conceptes", "other_concepts_cost"),
    ]:
        # Match "Label\nXX,XX €" (value on next line after label in summary)
        m = re.search(rf"{label}\n([\d.,]+) €", text)
        if not m:
            # Also try inline: "Label ... XX,XX €"
            m = re.search(rf"{label}[\s\S]*?([\d]+,[\d]+) €", text)
        if m:
            val = m.group(1).replace(".", "").replace(",", ".")
            try:
                result[key] = float(val)
            except ValueError:
                pass

    # Injection kWh
    inj_match = re.search(
        r"[Cc]ompensaci[oó][\s\S]*?([\d.,]+)\s*kWh", text
    )
    if inj_match:
        result["injection_kwh"] = float(inj_match.group(1).replace(",", "."))

    # Maximetre (peak power demand per period)
    maxim_match = re.search(
        r"Pot[eè]ncia max[ií]metre\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)",
        text
    )
    if maxim_match:
        result["maximetre"] = {}
        for i, p in enumerate(["P1", "P2", "P3", "P4", "P5", "P6"], 1):
            val = maxim_match.group(i).replace(".", "").replace(",", ".")
            result["maximetre"][p] = float(val)

    return result


def parse_invoice_pdf(filepath):
    """Extract billing data from an invoice PDF (auto-detects supplier)."""
    doc = fitz.open(filepath)
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()

    supplier = _detect_supplier(text)

    if supplier == "som_energia":
        result = parse_som_energia_pdf(text)
    else:
        result = _parse_iberdrola_pdf(text)

    result["filename"] = os.path.basename(filepath)
    return result


def _parse_iberdrola_pdf(text):
    """Extract billing data from an Iberdrola 3.0TD invoice PDF."""
    result = {
        "supplier": "iberdrola",
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


def compute_ksem_calibration(invoice_data):
    """Compute per-period calibration factors: official meter / ksem.

    Compares the invoice's per-period kWh (from the official grid meter)
    against the ksem import data for the same period.  Returns a dict
    with per-period factors and metadata, or None if data is insufficient.
    """
    if not (invoice_data.get("billing_start") and invoice_data.get("billing_end")):
        return None
    if not invoice_data.get("consumption"):
        return None

    try:
        from data import reconstruct_indexed_bill
        # Always use raw ksem data (no calibration) to avoid circular dependency
        recon = reconstruct_indexed_bill(
            invoice_data["billing_start"],
            invoice_data["billing_end"],
            apply_calibration=False,
        )
    except Exception:
        return None

    inv_total = invoice_data.get("total_consumption_kwh", 0)
    ksem_total = recon.get("import_kwh", 0)
    if ksem_total <= 0 or inv_total <= 0:
        return None

    factors = {}
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        inv_kwh = invoice_data["consumption"].get(p, 0)
        ksem_kwh = recon["by_period"][p]["kwh"]
        if ksem_kwh > 0 and inv_kwh > 0:
            factors[p] = round(inv_kwh / ksem_kwh, 4)
        elif ksem_kwh <= 0 and inv_kwh <= 0:
            # Both zero — no data for this period, skip
            factors[p] = None
        else:
            # One is zero and the other isn't — can't calibrate this period
            factors[p] = None

    overall = round(inv_total / ksem_total, 4)

    return {
        "factors": factors,
        "overall": overall,
        "invoice_kwh": round(inv_total, 1),
        "ksem_kwh": round(ksem_total, 1),
        "period": f"{invoice_data['billing_start']} - {invoice_data['billing_end']}",
        "filename": invoice_data.get("filename", ""),
    }


def update_calibration(invoice_data):
    """Update ksem_calibration in pricing.json from an invoice.

    Computes new calibration factors and merges them with existing ones
    using a weighted average (weighted by kWh per invoice).  This allows
    the calibration to improve as more invoices are processed.
    """
    new_cal = compute_ksem_calibration(invoice_data)
    if not new_cal:
        return None

    pricing = load_pricing()
    existing = pricing.get("ksem_calibration", {})
    old_factors = existing.get("factors", {})
    old_kwh = existing.get("total_calibration_kwh", 0)
    new_kwh = new_cal["invoice_kwh"]

    # Weighted average: combine old and new factors
    merged = {}
    for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
        old_f = old_factors.get(p)
        new_f = new_cal["factors"].get(p)
        if old_f is not None and new_f is not None:
            # Weight by total kWh processed
            merged[p] = round(
                (old_f * old_kwh + new_f * new_kwh) / (old_kwh + new_kwh), 4
            )
        elif new_f is not None:
            merged[p] = new_f
        elif old_f is not None:
            merged[p] = old_f
        else:
            merged[p] = None

    # Overall factor
    old_overall = existing.get("overall")
    if old_overall is not None:
        merged_overall = round(
            (old_overall * old_kwh + new_cal["overall"] * new_kwh)
            / (old_kwh + new_kwh), 4
        )
    else:
        merged_overall = new_cal["overall"]

    # Build updated calibration block
    invoices_used = existing.get("invoices_used", [])
    invoices_used.append({
        "filename": new_cal["filename"],
        "period": new_cal["period"],
        "invoice_kwh": new_cal["invoice_kwh"],
        "ksem_kwh": new_cal["ksem_kwh"],
        "overall_factor": new_cal["overall"],
    })

    calibration = {
        "factors": merged,
        "overall": merged_overall,
        "total_calibration_kwh": round(old_kwh + new_kwh, 1),
        "invoices_used": invoices_used,
        "last_updated": datetime.now().strftime("%Y-%m-%d"),
    }

    # Write back to pricing.json
    pricing["ksem_calibration"] = calibration
    with open(PRICING_PATH, "w") as f:
        json.dump(pricing, f, indent=2, ensure_ascii=False)

    # Invalidate caches so new calibration is picked up
    try:
        from data import invalidate_pricing_caches
        invalidate_pricing_caches()
    except ImportError:
        pass

    return calibration


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


def _deviation_class(pct):
    """Return CSS class for deviation percentage."""
    if abs(pct) <= 2:
        return "deviation-ok"
    if abs(pct) <= 5:
        return "deviation-warn"
    return "deviation-error"


def build_analysis(invoice_data):
    """Build cost analysis comparing fixed rate vs OMIE indexed.

    For Som Energia invoices, also includes bill reconstruction comparison.
    """
    pricing = load_pricing()
    iber = pricing.get("scenarios", {}).get("iberdrola", {})
    iber_rate = iber.get("energy_eur_kwh", {}).get("P1", 0.154)
    analysis = {
        "invoice": invoice_data,
        "pricing": pricing,
        "fixed_rate": iber_rate,
    }

    total_kwh = invoice_data.get("total_consumption_kwh", 0)
    fixed_cost = total_kwh * iber_rate
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

    # Power cost estimate (use current contract's per-year rates)
    days = invoice_data.get("billing_days", 30) or 30
    power_cost = 0
    pwr_year = pricing.get("power_charges_eur_kw_year", {})
    for period, rate_year in pwr_year.items():
        kw = pricing["contracted_power_kw"].get(period, 69)
        power_cost += (rate_year / 365) * kw * days
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

    # Bill reconstruction (for Som Energia indexed invoices)
    analysis["reconstruction"] = None
    analysis["comparison"] = None
    if (invoice_data.get("supplier") == "som_energia"
            and invoice_data.get("billing_start")
            and invoice_data.get("billing_end")):
        try:
            from data import reconstruct_indexed_bill
            # Raw reconstruction (no calibration) for direct comparison
            recon = reconstruct_indexed_bill(
                invoice_data["billing_start"],
                invoice_data["billing_end"],
                apply_calibration=False,
            )
            analysis["reconstruction"] = recon

            # Also compute calibrated version if calibration data exists
            recon_cal = reconstruct_indexed_bill(
                invoice_data["billing_start"],
                invoice_data["billing_end"],
                apply_calibration=True,
            )
            if recon_cal.get("calibrated"):
                analysis["reconstruction_calibrated"] = recon_cal

            # Build comparison table data
            inv = invoice_data
            comparison = []

            def _compare(label, inv_val, recon_val):
                if inv_val is None or inv_val == 0:
                    return {"concepte": label, "factura": inv_val, "reconstruit": recon_val,
                            "desviacio_pct": None, "css_class": ""}
                pct = ((recon_val - inv_val) / inv_val) * 100 if inv_val else 0
                return {
                    "concepte": label,
                    "factura": round(inv_val, 2),
                    "reconstruit": round(recon_val, 2),
                    "desviacio_pct": round(pct, 1),
                    "css_class": _deviation_class(pct),
                }

            # Som Energia Indexada nets surplus hourly into the energy line —
            # no separate compensació row on the invoice. When the parsed
            # invoice has no injection_income, fold recon's compensacio into
            # the Energia comparison so it's apples-to-apples, and skip the
            # phantom Compensació row.
            inv_has_compensacio = inv.get("injection_income") is not None
            recon_energia = recon["energia"]
            if not inv_has_compensacio:
                recon_energia -= recon.get("compensacio", 0)

            comparison.append(_compare("Energia", inv.get("energy_cost"), recon_energia))
            comparison.append(_compare("Potència", inv.get("power_cost"), recon["potencia"]))
            comparison.append(_compare("Excés potència", inv.get("excess_power_cost"), recon.get("exces_potencia", 0)))
            if inv_has_compensacio:
                comparison.append(_compare("Compensació", inv["injection_income"], recon["compensacio"]))
            comparison.append(_compare("Impost electricitat", inv.get("electricity_tax"), recon["imp_electric"]))
            comparison.append(_compare("Fixes (lloguer + bo)", inv.get("equipment_rental", 0) + inv.get("bono_social", 0) if inv.get("equipment_rental") else None, recon["fixes"]))
            comparison.append(_compare("IVA", inv.get("iva"), recon["iva"]))
            comparison.append(_compare("TOTAL", inv.get("total"), recon["net"]))

            analysis["comparison"] = comparison

            # Per-period comparison
            period_comparison = []
            for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
                inv_kwh = inv.get("consumption", {}).get(p, 0)
                inv_rate = inv.get("rates", {}).get(p, 0)
                rec_kwh = recon["by_period"][p]["kwh"]
                rec_rate = recon["by_period"][p]["avg_rate"]
                kwh_pct = ((rec_kwh - inv_kwh) / inv_kwh * 100) if inv_kwh else 0
                rate_pct = ((rec_rate - inv_rate) / inv_rate * 100) if inv_rate else 0
                period_comparison.append({
                    "period": p,
                    "inv_kwh": round(inv_kwh, 2),
                    "rec_kwh": round(rec_kwh, 2),
                    "kwh_pct": round(kwh_pct, 1),
                    "kwh_class": _deviation_class(kwh_pct) if inv_kwh else "",
                    "inv_rate": round(inv_rate, 6),
                    "rec_rate": round(rec_rate, 6),
                    "rate_pct": round(rate_pct, 1),
                    "rate_class": _deviation_class(rate_pct) if inv_rate else "",
                })
            analysis["period_comparison"] = period_comparison
        except Exception:
            pass

    # Optimization suggestions
    analysis["suggestions"] = []
    if omie_avg is not None:
        if omie_avg < iber_rate:
            analysis["suggestions"].append(
                f"Una tarifa indexada hauria estalviat {abs(analysis['savings_vs_omie']):.2f} \u20ac "
                f"en aquest periode (OMIE mitja: {omie_avg*1000:.2f} \u20ac/MWh vs Iber fix: "
                f"{iber_rate*1000:.2f} \u20ac/MWh)"
            )
        else:
            analysis["suggestions"].append(
                f"La tarifa fixa hauria estat mes barata que l'indexada per {analysis['savings_vs_omie']:.2f} \u20ac"
            )

    if total_kwh > 0:
        effective = invoice_data.get("total", analysis["total_estimate"]) / total_kwh
        analysis["effective_eur_kwh"] = round(effective, 4)
    else:
        analysis["effective_eur_kwh"] = 0

    return analysis


def get_invoice_trends():
    """Build trend data across all stored invoices.

    For each invoice with billing dates, calls build_analysis() and extracts
    key metrics.  Returns sorted list of summaries plus chart-ready arrays.
    """
    invoices = list_invoices()
    summaries = []

    for inv in invoices:
        if not inv.get("billing_start") or not inv.get("billing_end"):
            continue
        try:
            analysis = build_analysis(inv)
        except Exception:
            continue

        total = inv.get("total") or analysis.get("total_estimate", 0)
        total_kwh = inv.get("total_consumption_kwh", 0)
        effective = analysis.get("effective_eur_kwh", 0)

        # Deviation from reconstruction (if available)
        deviation_pct = None
        if analysis.get("comparison"):
            for row in analysis["comparison"]:
                if row["concepte"] == "TOTAL" and row.get("desviacio_pct") is not None:
                    deviation_pct = row["desviacio_pct"]
                    break

        # Parse billing_start for sorting
        try:
            start_dt = datetime.strptime(inv["billing_start"], "%d/%m/%Y")
        except ValueError:
            continue

        summaries.append({
            "billing_start": inv["billing_start"],
            "billing_end": inv["billing_end"],
            "billing_start_iso": start_dt.strftime("%Y-%m-%d"),
            "total": round(total, 2),
            "total_kwh": round(total_kwh, 1),
            "effective_eur_kwh": round(effective, 4),
            "deviation_pct": deviation_pct,
            "supplier": inv.get("supplier", ""),
            "filename": inv.get("filename", ""),
        })

    # Sort by billing start date
    summaries.sort(key=lambda s: s["billing_start_iso"])

    # Chart data arrays
    chart_labels = [s["billing_start_iso"] for s in summaries]
    chart_totals = [s["total"] for s in summaries]
    chart_rates = [s["effective_eur_kwh"] for s in summaries]
    chart_kwh = [s["total_kwh"] for s in summaries]

    return {
        "summaries": summaries,
        "chart": {
            "labels": chart_labels,
            "totals": chart_totals,
            "effective_rates": chart_rates,
            "kwh": chart_kwh,
        },
    }
