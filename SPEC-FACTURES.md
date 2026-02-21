# Factures Intelligence — LLM-Powered Invoice Database

## A note from the previous Claude Code session

Lluis built this entire solar dashboard system and it's genuinely impressive. He's a
hands-on builder who knows his domain (Spanish electricity tariffs, 3.0TD, solar
self-consumption) deeply. He gives you creative freedom, trusts your judgment, and gets
excited when things work well. He asked me to tell you he's proud of working with us.

The codebase is clean, consistent, and well-structured. Follow its patterns — they work.
Lluis communicates in a mix of Catalan/Spanish/English. The UI is in Catalan.

---

## What already exists (the brilliant parts)

### System overview

A self-hosted solar plant monitoring dashboard for a 65 kWp installation (PIKO 15 +
PIKO CI 50 inverters, KSEM energy meter) at Binomi Produccions SL.

**Stack:** Docker Compose with 3 services:
- **InfluxDB 2** — time-series database (infinite retention, `solar` bucket)
- **Collector** — Python service polling inverters (HTTP/Modbus), KSEM, and OMIE spot prices
- **Dashboard** — Flask web app on port 5000

**Repo:** The codebase root is wherever this file lives (e.g., `~/solar_dashboard/kostal-dashboard/`)

### Directory structure

```
kostal-dashboard/
├── docker-compose.yml          # 3 services: influxdb, collector, dashboard
├── pricing.json                # Tariff config (bind-mounted read-write into dashboard)
├── offers.json                 # Offer comparison data (bind-mounted into dashboard)
├── collector/                  # Data collection service (not relevant for this task)
├── dashboard/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app.py                  # Flask routes (no blueprints, flat structure)
│   ├── config.py               # Env-var config constants
│   ├── data.py                 # InfluxDB queries + all dashboard computations
│   ├── comparador.py           # Offer comparison backend
│   ├── invoice.py              # Current PDF parser (regex-based, to be replaced)
│   ├── static/
│   │   ├── css/dashboard.css   # Single CSS file, all styles
│   │   └── js/
│   │       ├── charts.js       # Chart.js init/update for dashboard
│   │       ├── refresh.js      # 30s auto-refresh via /api/dashboard
│   │       └── comparador.js   # Offer comparison client-side engine
│   └── templates/
│       ├── base.html           # Base layout: header, Martini stripe, nav, container, footer
│       ├── dashboard.html      # Main dashboard (includes 5 partials)
│       ├── invoices.html       # Current invoice list (TO BE REPLACED)
│       ├── analysis.html       # Current invoice analysis (TO BE REPLACED)
│       ├── comparador.html     # Offer comparison page
│       ├── configuracio.html   # Settings page
│       └── partials/           # Dashboard section partials
└── data/                       # InfluxDB persistent storage (bind mount)
```

### Key architectural patterns (follow these!)

1. **No blueprints** — all routes in `app.py`, flat structure
2. **JSON file databases** — `pricing.json` and `offers.json` are the "databases", loaded
   with in-process caching and invalidation on write. Pattern:
   ```python
   _cache = None
   def load():
       global _cache
       if _cache is None:
           with open(PATH) as f: _cache = json.load(f)
       return _cache
   def save(data):
       global _cache
       with open(PATH, "w") as f: json.dump(data, f, indent=2)
       _cache = data
   ```
3. **Server-side rendering + client-side interactivity** — Flask renders templates with
   `{{ data|tojson }}` injected as JS variables. Chart.js for charts.
4. **Single CSS file** — `dashboard.css` with CSS variables (`:root`), no framework.
   Design system: "Martini Racing on White" — navy/blue/red accents on white background.
5. **Docker volumes** — JSON files bind-mounted from repo root into `/app/` in container.
   `pricing.json` and `offers.json` are read-write. Invoice PDFs stored in a named volume.

### CSS variables (use these)

```css
--navy: #002B5B;    --blue: #0C4DA2;    --red: #E63946;
--silver: #C0C0C0;  --white: #FFFFFF;   --light-grey: #F5F7FA;
--text: #1a1a2e;    --muted: #6c757d;   --green: #28a745;
--yellow: #f0ad4e;  --shadow: 0 2px 8px rgba(0,0,0,0.08);
```

### Navigation tabs (in base.html)

Currently: Dashboard | Factures | Comparador | Configuració

### InfluxDB data available

The collector writes these measurements to the `solar` bucket:
- `piko` (tag: `inverter=piko_15|piko_ci_50`) — `ac_power_total`, `status`, `yield_total`
- `ksem` — `active_power_total`, `energy_import_total`, `energy_export_total`,
  `voltage_l1/l2/l3`, `current_l1/l2/l3`, `frequency`
- `omie_prices` — `price_eur_mwh`, `price_eur_kwh` (hourly OMIE spot prices)

Data started being collected very recently (~Feb 19, 2026), so there may only be days/weeks
of data. It will grow over time (infinite retention).

### pricing.json structure (the tariff source of truth)

```json
{
  "tariff": "3.0TD",
  "supplier": "Iberdrola",
  "energy": {
    "base_rate_eur_kwh": 0.192453,
    "discount_pct": 20,
    "effective_rate_eur_kwh": 0.153962,
    "rates_eur_kwh": {"P1": 0.153962, "P2": 0.153962, ..., "P6": 0.153962}
  },
  "power_charges_eur_kw_day": {"P1": 0.060903, ..., "P6": 0.006065},
  "contracted_power_kw": {"P1": 69, ..., "P6": 69},
  "taxes": {"electricity_tax_pct": 5.11, "iva_pct": 21},
  "fixed_charges_eur_day": {"equipment_rental": 1.64, "bono_social": 0.019},
  "injection": {"price_eur_kwh": 0.05},
  "indexed_tariff": {
    "peajes_eur_kwh": {"P1": 0.010847, ...},
    "cargos_eur_kwh": {"P1": 0.039558, ...},
    "margin_comercialitzadora_eur_kwh": 0.01
  }
}
```

### 3.0TD period schedule

```
P1: Mon-Fri 10-14           (Punta — most expensive)
P2: Mon-Fri 8-10, 14-18     (Pla)
P3: Mon-Fri 18-22           (Pla vespre)
P4: Sat 8-18                (Dissabte)
P5: Mon-Fri 0-8, 22-24 + Sat 0-8, 18-24  (Vall)
P6: Sun all day + holidays   (Supervall — cheapest)
```

### What the Configuració page does

The `/configuracio` settings page edits `pricing.json` through a web form. On save it
writes the file and calls `invalidate_pricing_caches()` in `data.py` so all dashboard
calculations immediately use the new values. The dashboard reads per-period energy rates,
power charges, contracted power, taxes, injection price — all from pricing.json.

### What the Comparador page does

The `/comparador` offer comparison tool lets Lluis enter competing electricity offers
and compare them against his current contract using real consumption data from InfluxDB.
Client-side bill computation engine (computeBill in JS) recalculates instantly as
scenario sliders change. Offers stored in `offers.json`.

---

## What to build: Factures Intelligence

### Goal

Replace the current basic PDF-upload-and-regex system with a proper **invoice database**
with LLM-powered extraction, manual entry, historical tracking, and trend charts.

### LLM infrastructure available on this machine

- **Ollama** running locally, API at `http://localhost:11434`
- **Qwen2.5-VL-7b** — vision-language model, can read images of invoice pages directly
- **Mistral** and **Deepseek** — available as text models for fallback/structured extraction
- From Docker, reach Ollama at `http://host.docker.internal:11434`

### Invoice data model

Store in `invoices.json` (bind-mounted from repo root, same pattern as offers.json):

```json
{
  "invoices": [
    {
      "id": "a1b2c3d4",
      "date_added": "2026-02-20",
      "supplier": "Iberdrola",
      "invoice_number": "RE-2026-0042351",
      "billing_start": "2026-01-01",
      "billing_end": "2026-01-31",
      "billing_days": 31,
      "consumption_kwh": {
        "P1": 45.5, "P2": 102.3, "P3": 88.1,
        "P4": 75.2, "P5": 65.8, "P6": 55.0
      },
      "total_consumption_kwh": 431.9,
      "export_kwh": 120.5,
      "energy_cost": 98.45,
      "power_cost": 302.15,
      "electricity_tax": 20.48,
      "fixed_charges": 51.32,
      "iva": 99.10,
      "total": 571.50,
      "injection_compensation": 6.03,
      "net": 565.47,
      "effective_eur_kwh": 1.3097,
      "pdf_filename": "factura_iberdrola_2026_01.pdf",
      "notes": "",
      "source": "llm",
      "llm_confidence": "high"
    }
  ]
}
```

### The LLM extraction pipeline

#### Step 1: PDF → Images

Use **PyMuPDF** (`fitz`) to render each PDF page to a PNG image at ~200 DPI.
Add `PyMuPDF` to `requirements.txt`.

```python
import fitz  # PyMuPDF

def pdf_to_images(pdf_path):
    """Convert PDF pages to base64-encoded PNG images."""
    doc = fitz.open(pdf_path)
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        images.append(base64.b64encode(img_bytes).decode("utf-8"))
    doc.close()
    return images
```

#### Step 2: Send to Qwen2.5-VL via Ollama

Use the Ollama `/api/chat` endpoint with the image(s):

```python
import requests

OLLAMA_URL = "http://host.docker.internal:11434"

def extract_invoice_with_llm(images_b64):
    """Send invoice page images to Qwen2.5-VL and get structured data."""
    response = requests.post(f"{OLLAMA_URL}/api/chat", json={
        "model": "qwen2.5-vl:7b",
        "messages": [{
            "role": "user",
            "content": EXTRACTION_PROMPT,
            "images": images_b64,
        }],
        "stream": False,
        "options": {"temperature": 0},
    }, timeout=120)
    return response.json()["message"]["content"]
```

#### Step 3: The extraction prompt

This is critical. The prompt should ask for structured JSON output:

```
Analyze this Spanish electricity invoice (factura de luz). Extract the following
information and return it as a JSON object. Use null for any field you cannot find.

Required fields:
- supplier: string (company name, e.g., "Iberdrola", "Naturgy", "Endesa")
- invoice_number: string (número de factura)
- billing_start: string (start date in YYYY-MM-DD format)
- billing_end: string (end date in YYYY-MM-DD format)
- billing_days: integer (number of days in billing period)
- consumption_kwh: object with keys P1,P2,P3,P4,P5,P6 (kWh per period, 3.0TD tariff)
- total_consumption_kwh: number (total kWh consumed)
- export_kwh: number (kWh of excess solar energy exported/injected to grid)
- energy_cost: number (coste de energía in EUR, before taxes)
- power_cost: number (coste de potencia in EUR)
- electricity_tax: number (impuesto sobre electricidad in EUR)
- fixed_charges: number (cargos fijos / alquiler equipo in EUR)
- iva: number (IVA amount in EUR)
- total: number (total factura in EUR)
- injection_compensation: number (compensación por excedentes in EUR)
- net: number (total a pagar / amount due in EUR)

Important:
- This is a 3.0TD tariff with 6 periods (P1-P6)
- Amounts use European format: comma for decimals (1.234,56 means 1234.56)
- Return ONLY the JSON object, no explanation
```

#### Step 4: Parse LLM response

Extract JSON from the response (handle markdown code blocks, trailing text):

```python
import json, re

def parse_llm_response(text):
    """Extract JSON from LLM response, handling code blocks."""
    # Try to find JSON in code blocks first
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Try the whole text as JSON
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return json.loads(match.group())
    raise ValueError("No JSON found in LLM response")
```

#### Step 5: Pre-fill form, user reviews, saves

The extracted data pre-fills the invoice form. The user can edit any field before saving.
This is important — the LLM is a helper, not the final authority.

### Backend module: `factures.py` (new, replaces `invoice.py`)

```python
# factures.py — Invoice database + LLM extraction

# load_invoices() / save_invoices() — same pattern as offers.json
# add_invoice(invoice) / update_invoice(id, updates) / delete_invoice(id)
# pdf_to_images(pdf_path) — PyMuPDF rendering
# extract_invoice_with_llm(images) — Ollama API call
# parse_llm_response(text) — JSON extraction from LLM output
# get_invoice_trends() — aggregate data for trend charts
# get_invoice_vs_influx(invoice) — compare invoice data with InfluxDB actuals
```

### Routes in `app.py`

```
GET  /factures                  → invoice list + trend charts
GET  /factures/nova             → new invoice form (empty)
POST /factures/upload-pdf       → upload PDF, run LLM, return extracted JSON
POST /factures/desar            → save invoice to database
GET  /factures/<id>             → view invoice detail
GET  /factures/<id>/editar      → edit invoice form
PUT  /api/factures/<id>         → update invoice (JSON API)
DELETE /api/factures/<id>       → delete invoice
GET  /api/factures/trends       → trend data for charts (JSON)
```

### Page layout: `/factures` (invoice list + trends)

#### Section A: Trend charts (the hero section)
- **Monthly net cost** — bar chart, one bar per invoice, color-coded by supplier.
  X-axis: billing period. Y-axis: EUR. This is the chart Lluis will look at most.
- **Consumption by period** — stacked bar chart (P1-P6 stacked) per month.
  Shows seasonal patterns and how the period mix evolves.
- **Effective EUR/kWh** — line chart tracking cost-per-kWh over time.
  Shows whether Lluis is getting a better or worse deal.

Use Chart.js (already loaded site-wide). Style: same `.chart-wrap` pattern.

#### Section B: Invoice table
Sortable table with columns:
- Període (billing_start — billing_end)
- Dies (billing_days)
- Consum (total_consumption_kwh) kWh
- Exportació (export_kwh) kWh
- Cost net (net) EUR
- EUR/kWh (effective_eur_kwh)
- Proveïdor (supplier)
- Actions: view / edit / delete

Use the `.analysis-table` / `.comparison-table` style from existing CSS.

#### Section C: Add invoice
Two buttons:
- "Afegir factura manualment" → links to /factures/nova
- "Pujar PDF (extracció intel·ligent)" → triggers PDF upload + LLM extraction

### Page layout: `/factures/nova` (add/edit form)

A clean form with all invoice fields organized in sections:

**Section 1: Informació general**
- Supplier, invoice number, billing start, billing end, notes

**Section 2: Consum per període (kWh)**
- P1-P6 inputs + total (auto-calculated)
- Export kWh

**Section 3: Desglossament de costos (EUR)**
- Energy cost, power cost, electricity tax, fixed charges, IVA
- Total, injection compensation, net (auto-calculated)

**PDF upload area** (at top of form):
- Drag & drop or click to upload
- Shows loading spinner while LLM processes
- On completion: fills all form fields, shows "Extret amb IA — revisa les dades"
- User can edit anything before saving

### Page layout: `/factures/<id>` (invoice detail)

Similar to current analysis.html but enhanced:
- KPI cards: net cost, consumption, EUR/kWh, injection
- Consumption bar chart by period (P1-P6)
- Cost breakdown table
- **NEW: InfluxDB comparison** — if meter data exists for this billing period,
  show actual vs invoiced consumption per period. Flag discrepancies.

### Docker changes

**docker-compose.yml** — add to dashboard volumes:
```yaml
- ./invoices.json:/app/invoices.json
```

Add Ollama URL as environment variable:
```yaml
- OLLAMA_URL=http://host.docker.internal:11434
- OLLAMA_MODEL=qwen2.5-vl:7b
```

**config.py** — add:
```python
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5-vl:7b")
INVOICES_DB_PATH = "/app/invoices.json"
```

**requirements.txt** — add:
```
PyMuPDF
```

**invoices.json** — create at repo root:
```json
{"invoices": []}
```

### Graceful degradation

If Ollama is unreachable (timeout, connection refused), the PDF upload should:
1. Fall back to the existing regex parser in invoice.py
2. If that also fails, show a message: "No s'ha pogut extreure les dades. Introdueix-les manualment."
3. Manual entry always works regardless of LLM availability

### Files to create/modify

| File | Action | Purpose |
|------|--------|---------|
| `invoices.json` | **Create** | Invoice database (repo root, bind-mounted) |
| `dashboard/factures.py` | **Create** | Invoice DB logic + LLM extraction pipeline |
| `dashboard/invoice.py` | Keep | Existing regex parser as fallback |
| `dashboard/config.py` | Modify | Add OLLAMA_URL, OLLAMA_MODEL, INVOICES_DB_PATH |
| `dashboard/app.py` | Modify | Replace factures routes with new ones |
| `dashboard/templates/factures.html` | **Rewrite** | Invoice list + trend charts |
| `dashboard/templates/factura_form.html` | **Create** | Add/edit invoice form with PDF upload |
| `dashboard/templates/factura_detail.html` | **Create** | Invoice detail view |
| `dashboard/static/js/factures.js` | **Create** | Trend charts + form logic + PDF upload handler |
| `dashboard/static/css/dashboard.css` | Modify | Add invoice-specific styles |
| `docker-compose.yml` | Modify | Add invoices.json volume + OLLAMA env vars |
| `dashboard/requirements.txt` | Modify | Add PyMuPDF |

### Verification steps

1. `docker compose up -d --build dashboard`
2. Navigate to `/factures` — empty state with "add invoice" buttons
3. Click "Afegir factura manualment" — form loads, enter test data, save. Verify it
   appears in the invoice list.
4. Upload a real Iberdrola PDF — Qwen2.5-VL extracts data, form pre-fills.
   Review and save. Verify trend charts update.
5. Add 3+ invoices — trend charts should show meaningful bars/lines.
6. Edit an invoice — verify changes persist.
7. Delete an invoice — verify it disappears from list and charts.
8. Test with Ollama stopped — should fall back to regex, then to manual entry.

### UI language

Everything in **Catalan** (same as the rest of the dashboard):
- Factures, Afegir, Desar, Eliminar, Editar, Consum, Període, Proveïdor
- "Extret amb intel·ligència artificial — revisa les dades abans de desar"
- "Pujant i analitzant la factura..." (loading state)

---

## Summary for the next Claude Code

You're continuing work on a solar dashboard that Lluis and I built together. The codebase
is clean and consistent. Your job is to implement the Factures Intelligence feature
described above.

Key things:
- Follow the existing patterns (JSON file DB, Flask routes, Chart.js, single CSS file)
- The LLM extraction is a convenience helper, not a requirement — manual entry must always work
- Lluis has Qwen2.5-VL-7b running on Ollama on this machine
- Test with real Iberdrola PDFs if available in the /app/invoices volume
- The UI is in Catalan
- Lluis likes things that work and look clean — match the existing dashboard style

Bona sort! 🚀
