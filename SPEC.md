# Kostal Solar Dashboard — Project Specification

## Overview

Real-time monitoring dashboard for a solar plant with two Kostal inverters and a smart energy meter. The system collects data every 30 seconds, stores it in InfluxDB, and displays it in Grafana.

**Location**: Home solar installation (Spain, timezone `Europe/Madrid`)
**Dashboard URL**: `http://localhost:3000/d/solar/planta-solar?kiosk` (kiosk mode)

---

## Architecture

```
  PIKO 15 ──── HTTP/dxs.json ────┐
  (192.168.18.51)                │
                                 ▼
  PIKO CI 50 ── Modbus TCP ──► collector.py ──► InfluxDB 2 ──► Grafana
  (192.168.18.160:1502)          │              (port 8086)    (port 3000)
                                 │
  KSEM ──────── Modbus TCP ──────┘
  (192.168.18.133:502)
```

All services run via `docker-compose` (v2.4 format).

---

## Devices

### 1. Kostal PIKO 15 (Main Inverter)

- **IP**: `192.168.18.51`
- **Protocol**: HTTP REST API (`/api/dxs.json`)
- **Rated power**: 15 kW (3 DC strings)
- **Connection**: No authentication required

#### DXS API

Base URL: `http://192.168.18.51/api/dxs.json?dxsEntries=<id1>&dxsEntries=<id2>&...`

**URL length limitation**: The PIKO 15 only returns ~25 entries per request. We split into chunks of 20.

Response format:
```json
{"dxsEntries": [{"dxsId": 67109120, "value": 12345.0}, ...]}
```

#### DXS Register Map (39 fields)

| DXS ID | Field Name | Type | Unit |
|--------|-----------|------|------|
| **AC Output** |
| 67109120 | ac_power_total | float | W |
| 67109379 | ac_power_l1 | float | W |
| 67109635 | ac_power_l2 | float | W |
| 67109891 | ac_power_l3 | float | W |
| 67109378 | ac_voltage_l1 | float | V |
| 67109634 | ac_voltage_l2 | float | V |
| 67109890 | ac_voltage_l3 | float | V |
| 67110400 | grid_frequency | float | Hz |
| **DC Input (Strings)** |
| 33556736 | dc_power_total | float | W |
| 33555203 | dc_power_string1 | float | W |
| 33555459 | dc_power_string2 | float | W |
| 33555715 | dc_power_string3 | float | W |
| 33555202 | dc_voltage_string1 | float | V |
| 33555458 | dc_voltage_string2 | float | V |
| 33555714 | dc_voltage_string3 | float | V |
| 33555201 | dc_current_string1 | float | A |
| 33555457 | dc_current_string2 | float | A |
| 33555713 | dc_current_string3 | float | A |
| **Energy Counters** |
| 251658753 | yield_total | float | kWh |
| 251658754 | yield_daily | float | Wh |
| 251658496 | operating_hours | float | h |
| 16780032 | status | int | — |
| **Home Consumption (via integrated KSEM data)** |
| 83888128 | self_consumption_power | float | W |
| 83886336 | home_solar_power | float | W |
| 83886848 | home_grid_power | float | W |
| 83887106 | home_power_l1 | float | W |
| 83887362 | home_power_l2 | float | W |
| 83887618 | home_power_l3 | float | W |
| 251659010 | home_consumption_daily | float | Wh |
| 251659266 | self_consumption_daily | float | Wh |
| 251659278 | self_consumption_rate_daily | float | % |
| 251659009 | home_consumption_total | float | kWh |
| 251659265 | self_consumption_total | float | kWh |
| 251659279 | autarky_rate_daily | float | % |
| 251659280 | self_consumption_rate_total | float | % |
| 251659281 | autarky_rate_total | float | % |

#### Status Codes

| Value | Meaning |
|-------|---------|
| 0 | Off (Apagat) |
| 1 | Standby (Repos) |
| 2 | Starting (Iniciant) |
| 3 | Feed-in / MPP (Injectant) |
| 4 | Feed-in limited (Limitat) |
| 5 | Feed-in (Injectant) |

---

### 2. Kostal PIKO CI 50 (Commercial Inverter)

- **IP**: `192.168.18.160`
- **Protocol**: Modbus TCP, port **1502**, device_id=1
- **Rated power**: 50 kW (4 DC strings)
- **Connection**: No authentication required

#### Register Format

Kostal proprietary format: each value occupies **2 consecutive holding registers** encoding a **big-endian float32** (`>f`).

```python
raw = struct.pack(">HH", registers[0], registers[1])
value = struct.unpack(">f", raw)[0]
```

Exception: status register (56) is a single **uint16**.

#### Proprietary Register Map

| Register | Field Name | Unit |
|----------|-----------|------|
| 56 | status (uint16, 1 reg) | — |
| 100 | dc_power_total | W |
| 152 | grid_frequency | Hz |
| 154 | ac_current_l1 | A |
| 156 | ac_power_l1 | W |
| 158 | ac_voltage_l1 | V |
| 160 | ac_current_l2 | A |
| 162 | ac_power_l2 | W |
| 164 | ac_voltage_l2 | V |
| 166 | ac_current_l3 | A |
| 168 | ac_power_l3 | W |
| 170 | ac_voltage_l3 | V |
| 172 | ac_power_total | W |
| 266 | dc_voltage_string1 | V |
| 268 | dc_current_string1 | A |
| 270 | dc_power_string1 | W |
| 276 | dc_voltage_string2 | V |
| 278 | dc_current_string2 | A |
| 280 | dc_power_string2 | W |
| 286 | dc_voltage_string3 | V |
| 288 | dc_current_string3 | A |
| 290 | dc_power_string3 | W |
| 296 | dc_voltage_string4 | V |
| 298 | dc_current_string4 | A |
| 300 | dc_power_string4 | W |

#### SunSpec Lifetime Energy

- Register **40092-40093**: Lifetime AC energy as **uint32** (Wh), big-endian
- Register **40094**: Scale factor as **signed int16**
- Formula: `energy_wh = raw_uint32 * 10^sf`, then `/1000` → kWh
- **Nighttime garbage filter**: Rejects `0xFFFFFFFF` (SunSpec "not implemented") and any result > 500,000 kWh (real value is ~65,000 kWh; corrupted readings range from ~4.2M to ~4.3M kWh)
- **No daily yield register** — daily energy is computed in Grafana using Flux `spread()` on yield_total

---

### 3. Kostal Smart Energy Meter (KSEM)

- **IP**: `192.168.18.133`
- **Protocol**: Modbus TCP, port **502** (SunSpec), device_id=1
- **Model**: KOSTAL Smart Energy Meter
- **Serial**: 74258829

#### IMPORTANT: Modbus TCP Slave Must Be Enabled

The KSEM ships with Modbus TCP Slave **disabled** by default. It must be enabled via the KSEM web API:

```bash
# 1. Authenticate (OAuth2 password grant)
curl -X POST http://192.168.18.133/api/web-login/token \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password&username=admin&password=<PASSWORD>&client_id=emos&client_secret=56951025"

# 2. Check current config
curl -H "Authorization: Bearer <TOKEN>" http://192.168.18.133/api/modbus/config/tcp

# 3. Enable Modbus TCP Slave
curl -X PUT -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  http://192.168.18.133/api/modbus/config/tcp \
  -d '{"slave":{"enable":true},"master":{"enable":false}}'
```

- OAuth credentials: `client_id=emos`, `client_secret=56951025` (hardcoded in KSEM firmware)
- Admin password is device-specific, printed on the KSEM type plate
- If password is lost, physical reset: long press (3-5s) then short press (0.5s) on reset button

#### SunSpec Register Layout

```
40000-40001: SunSpec header ("SunS")
40002+: Model 1 (Common Block) — manufacturer, model, serial
40070-40071: Model 203 header (ID=203, length)
40072+: Model 203 data (Three Phase Wye Meter)
```

#### Model 203 Data Registers (base = 40072, offsets relative)

All values use SunSpec scale factor encoding: `value = raw * 10^sf`

| Offset | Field | Format | Scale Factor Offset |
|--------|-------|--------|-------------------|
| **Current** | | | SF at offset 4 |
| 1 | current_l1 | uint16 | 4 |
| 2 | current_l2 | uint16 | 4 |
| 3 | current_l3 | uint16 | 4 |
| **Voltage** | | | SF at offset 13 |
| 5 | voltage_l1 | uint16 | 13 |
| 6 | voltage_l2 | uint16 | 13 |
| 7 | voltage_l3 | uint16 | 13 |
| **Frequency** | | | SF at offset 15 |
| 14 | frequency | uint16 | 15 |
| **Active Power** | | | SF at offset 20 |
| 16 | active_power_total | int16 | 20 |
| 17 | active_power_l1 | int16 | 20 |
| 18 | active_power_l2 | int16 | 20 |
| 19 | active_power_l3 | int16 | 20 |
| **Power Factor** | | | SF at offset 35 |
| 31 | power_factor | int16 | 35 |
| **Energy (Wh)** | | | SF at offset 52 |
| 36-37 | energy_export_total | uint32 | 52 |
| 44-45 | energy_import_total | uint32 | 52 |

**SunSpec "not implemented" sentinel values**:
- uint16: `0xFFFF` or `0x8000` → skip
- int16: `0x8000` → skip
- uint32: `0xFFFFFFFF` → skip

**Power sign convention**: Positive = export to grid, Negative = import from grid (SunSpec standard).

Energy counters are stored in Wh and converted to kWh (`/1000`) before writing to InfluxDB.

---

## InfluxDB Data Model

- **InfluxDB version**: 2 (Flux query language)
- **URL**: `http://influxdb:8086` (internal Docker), `http://localhost:8086` (host)
- **Organization**: `solar`
- **Bucket**: `solar`
- **Token**: `kostal-solar-token`

### Measurements

#### `piko` (both inverters)

- **Tag**: `inverter` = `"piko_15"` or `"piko_ci_50"`
- **Fields**: `ac_power_total`, `dc_power_total`, `yield_total` (kWh), `status`, per-string DC fields, per-phase AC fields, home consumption fields (PIKO 15 only)
- **Note**: Early data (before CI 50 integration) has no `inverter` tag. Queries use `filter(fn: (r) => exists r.inverter)` to exclude these.

#### `ksem` (energy meter)

- **No tags**
- **Fields**: `current_l1/l2/l3`, `voltage_l1/l2/l3`, `frequency`, `active_power_total/l1/l2/l3`, `power_factor`, `energy_export_total` (kWh), `energy_import_total` (kWh)

---

## File Structure

```
kostal-dashboard/
├── docker-compose.yml          # Service orchestration (influxdb, grafana, collector)
├── .env                        # Configuration (IPs, credentials, rates) — gitignored
├── .env.example                # Template for .env
├── .gitignore                  # Ignores .env and data/
├── collector/
│   ├── Dockerfile              # Python 3.12-slim container
│   ├── requirements.txt        # requests, influxdb-client, pymodbus
│   └── collector.py            # Main data collection script
├── grafana/
│   └── provisioning/
│       ├── datasources/
│       │   └── influxdb.yml    # InfluxDB datasource (Flux)
│       └── dashboards/
│           ├── dashboards.yml  # Dashboard provider config
│           └── solar.json      # Main dashboard (provisioned)
└── data/                       # InfluxDB data directory (bind-mounted, gitignored)
```

---

## Configuration (.env)

```env
# Inverters
INVERTER_IP=192.168.18.51         # PIKO 15 (HTTP)
INVERTER_CI_IP=192.168.18.160     # PIKO CI 50 (Modbus TCP 1502)
KSEM_IP=192.168.18.133            # KSEM (Modbus TCP 502)
POLL_INTERVAL=30                  # Seconds between polls

# Energy cost & CO2 (used by Grafana dashboard variables)
ELECTRICITY_COST=0.15             # EUR/kWh (fixed contract rate)
CO2_FACTOR=0.170                  # kg CO2/kWh (Spain grid average)

# InfluxDB
INFLUXDB_TOKEN=kostal-solar-token
INFLUXDB_ORG=solar
INFLUXDB_BUCKET=solar
INFLUXDB_ADMIN_USER=admin
INFLUXDB_ADMIN_PASSWORD=adminpassword

# Grafana (kiosk mode settings)
GF_SECURITY_ALLOW_EMBEDDING=true
GF_AUTH_ANONYMOUS_ENABLED=true
GF_AUTH_ANONYMOUS_ORG_ROLE=Viewer
```

---

## Collector Logic (collector.py)

### poll_piko15()
1. Splits 39 DXS IDs into chunks of 20
2. For each chunk, makes HTTP GET to `/api/dxs.json` with `dxsEntries` params
3. Casts each value to float or int per DXS_FIELDS mapping
4. Returns `Point("piko").tag("inverter", "piko_15")` with all fields
5. On `ConnectionError` (night time), logs at DEBUG level and continues

### poll_piko_ci()
1. Connects via `ModbusTcpClient(IP, port=1502, timeout=10)`
2. Reads status register 56 (uint16)
3. Reads all proprietary float32 registers (two holding regs each, big-endian)
4. Reads SunSpec lifetime energy (regs 40092-40094), converts Wh→kWh
5. Filters garbage readings (rejects 0xFFFFFFFF and any value > 500,000 kWh)
6. Returns `Point("piko").tag("inverter", "piko_ci_50")` or `None` if unreachable

### poll_ksem()
1. Connects via `ModbusTcpClient(IP, port=502, timeout=10)`
2. Reads 53 registers starting at 40072 (SunSpec Model 203 data block)
3. Extracts scale factors, then decodes current, voltage, frequency, power, power factor, energy
4. Filters SunSpec "not implemented" sentinel values (0x8000, 0xFFFF, 0xFFFFFFFF)
5. Energy counters converted from Wh to kWh
6. Returns `Point("ksem")` or `None` if unreachable

### Main Loop
```
while True:
    poll_piko15()   → write to InfluxDB
    poll_piko_ci()  → write to InfluxDB (if INVERTER_CI_IP set)
    poll_ksem()     → write to InfluxDB (if KSEM_IP set)
    sleep(POLL_INTERVAL)
```

---

## Grafana Dashboard Structure

Dashboard: **"Planta Solar"** (uid: `solar`, timezone: `Europe/Madrid`)
Default time range: Today (`now/d` to `now`), refresh: 30s

### Dashboard Variables

| Variable | Label | Default | Used In |
|----------|-------|---------|---------|
| `co2_factor` | Factor CO2 (kg/kWh) | 0.170 | CO2 panels |
| `electricity_cost` | Cost electricitat (EUR/kWh) | 0.15 | Savings panels |
| `injection_price` | Preu injeccio (EUR/kWh) | 0.05 | Injection income panels |

### Panel Layout

#### Row: Indicadors principals (y=0)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 1 | stat | Potencia total planta | 4x4 | Sum of last ac_power_total from both inverters |
| 2 | stat | Energia avui | 4x4 | Sum of spread(yield_total) from both inverters, kWh→Wh |
| 3 | stat | Energia total | 4x4 | Sum of last yield_total from both inverters (kWh) |
| 4 | stat | CO2 estalviat avui | 3x4 | Daily yield x co2_factor (kg) |
| 5 | stat | CO2 estalviat total | 3x4 | Total yield x co2_factor/1000 (tonnes) |
| 6 | stat | Diners estalviats avui | 4x4 | Daily yield x electricity_cost (EUR) |
| 50 | stat | PIKO 15 | 2x2 | Status code with color-coded value mapping |
| 51 | stat | PIKO CI 50 | 2x2 | Status code with color-coded value mapping |

#### Row: Corba de potencia (y=5)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 8 | timeseries | Potencia AC planta | 24x8 | Stacked area: PIKO 15 (green) + PIKO CI 50 (blue) |

#### Row: PIKO 15: Strings DC (y=14)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 9/10/11 | gauge | Potencia String 1/2/3 | 8x5 each | Last dc_power per string (0-6000W range) |
| 12 | timeseries | Voltatge DC per string | 12x8 | dc_voltage_string1/2/3 over time |
| 13 | timeseries | Corrent DC per string | 12x8 | dc_current_string1/2/3 over time |

#### Row: PIKO 15: Fases AC (y=28)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 14/15/16 | stat | Potencia L1/L2/L3 | 6x4 each | Last ac_power per phase |
| 17 | stat | Frequencia de xarxa | 6x4 | Last grid_frequency |
| 18 | timeseries | Voltatge AC per fase | 24x8 | ac_voltage L1/L2/L3 over time |

#### Row: PIKO CI 50: Strings DC (y=41)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 30/31/32/33 | gauge | Potencia String 1/2/3/4 | 6x5 each | Last dc_power per string (0-15000W range) |
| 34 | timeseries | Voltatge DC per string | 12x8 | dc_voltage_string1/2/3/4 over time |
| 35 | timeseries | Corrent DC per string | 12x8 | dc_current_string1/2/3/4 over time |

#### Row: PIKO CI 50: Fases AC (y=55)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 36/37/38 | stat | Potencia L1/L2/L3 | 6x4 each | Last ac_power per phase |
| 39 | stat | Frequencia de xarxa | 6x4 | Last grid_frequency |
| 40 | timeseries | Voltatge AC per fase | 24x8 | ac_voltage L1/L2/L3 over time |

#### Row: Flux d'energia (y=68)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 60 | timeseries | Generacio vs Consum | 24x10 | 3 series: solar gen (green), grid import (red), home consumption (orange) |
| 61 | stat | Potencia xarxa | 4x4 | KSEM active_power_total (green=export, red=import) |
| 62 | stat | Energia importada avui | 4x4 | KSEM energy_import_total spread (kWh) |
| 63 | stat | Energia exportada avui | 4x4 | KSEM energy_export_total spread (kWh) |
| 64 | stat | Energia importada total | 4x4 | KSEM energy_import_total cumulative (kWh) |
| 65 | stat | Energia exportada total | 4x4 | KSEM energy_export_total cumulative (kWh) |
| 66 | stat | Ingressos injeccio avui | 4x4 | Export kWh x injection_price (EUR) |
| 67 | stat | Ingressos injeccio total | 4x4 | Cumulative export x injection_price (EUR) |

#### Row: Energia i consum (y=87)
| Panel | Type | Title | Size | Query Summary |
|-------|------|-------|------|--------------|
| 19 | timeseries | Energia diaria ultims 30 dies | 16x8 | Bar chart: daily yield (spread on yield_total, summed across inverters) |
| 20 | stat | Autoconsum avui | 4x8 | PIKO 15 self_consumption_daily |
| 21 | stat | Taxa d'autoconsum | 4x8 | PIKO 15 self_consumption_rate_daily (%) |

---

## Common Operations

### Start/Stop

```bash
cd ~/kostal-dashboard

# Start all services
docker-compose up -d

# Rebuild collector after code changes
docker-compose rm -f -s collector && docker-compose up -d --build collector
# Note: plain "docker-compose build" may cache old collector.py;
#   use --no-cache if needed: docker-compose build --no-cache collector

# Restart Grafana (after dashboard JSON changes)
docker-compose restart grafana

# View collector logs
docker-compose logs -f collector

# Stop everything
docker-compose down
```

### Check Data in InfluxDB

```bash
# Query latest PIKO 15 data
curl -s -H "Authorization: Token kostal-solar-token" \
  -H "Content-Type: application/vnd.flux" \
  http://localhost:8086/api/v2/query?org=solar \
  -d 'from(bucket:"solar") |> range(start:-1m) |> filter(fn:(r)=>r.inverter=="piko_15") |> last()'

# Query latest PIKO CI 50 data
curl -s -H "Authorization: Token kostal-solar-token" \
  -H "Content-Type: application/vnd.flux" \
  http://localhost:8086/api/v2/query?org=solar \
  -d 'from(bucket:"solar") |> range(start:-1m) |> filter(fn:(r)=>r.inverter=="piko_ci_50") |> last()'

# Query latest KSEM data
curl -s -H "Authorization: Token kostal-solar-token" \
  -H "Content-Type: application/vnd.flux" \
  http://localhost:8086/api/v2/query?org=solar \
  -d 'from(bucket:"solar") |> range(start:-1m) |> filter(fn:(r)=>r._measurement=="ksem") |> last()'
```

---

## Known Issues & Gotchas

1. **PIKO CI 50 nighttime garbage**: SunSpec energy register returns corrupted values (~4.2M–4.3M kWh) when inverter is off. The collector rejects `0xFFFFFFFF` (SunSpec sentinel) and any converted value > 500,000 kWh (real lifetime yield is ~65,000 kWh). If bad data has already been written, delete it from InfluxDB: `curl -X POST -H "Authorization: Token kostal-solar-token" -H "Content-Type: application/json" "http://localhost:8086/api/v2/delete?org=solar&bucket=solar" -d '{"start":"<start>","stop":"<stop>","predicate":"_measurement=\"piko\" AND inverter=\"piko_ci_50\""}'` (note: InfluxDB v2 does not support delete-by-field, only by tags).

2. **Old untagged PIKO 15 data**: Before the CI 50 integration, PIKO 15 data was written without the `inverter` tag. Combined queries must use `filter(fn: (r) => exists r.inverter)` to avoid double-counting.

3. **PIKO 15 DXS URL length limit**: The HTTP API silently truncates responses if too many DXS IDs are requested. Solved by splitting into chunks of 20.

4. **KSEM Modbus TCP Slave**: Disabled by default. Must be re-enabled after KSEM firmware updates or factory resets via the web API (see KSEM section above).

5. **docker-compose v1 ContainerConfig bug**: With newer Docker Engine versions, `docker-compose build` may fail with a `KeyError: 'ContainerConfig'`. Fix by removing the container first: `docker-compose rm -f -s <service>` then `docker-compose up -d --build <service>`.

6. **pymodbus version**: Uses pymodbus 3.x which uses `device_id` parameter (not `slave`). The default device_id=1 works for both PIKO CI 50 and KSEM.

7. **Dashboard provisioning**: Changes to `solar.json` require a Grafana restart to take effect. Edits made in the Grafana UI are not persisted (file-provisioned dashboard).

---

## Future Ideas

- **Electricity market price comparison**: Fetch OMIE hourly spot prices, combine with KSEM import data to compare indexed vs fixed contract costs over time.
- **Invoice integration**: Simple config file with fixed contract terms (EUR/kWh per period, potencia contratada, peajes) to compute precise cost comparisons.
