"""Configuration — reads env vars once at import time."""

import os

# InfluxDB
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "http://influxdb:8086")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "solar")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "solar")

# Tariff / economics
ELECTRICITY_COST = float(os.environ.get("ELECTRICITY_COST", "0.154"))
INJECTION_PRICE = float(os.environ.get("INJECTION_PRICE", "0.05"))
CO2_FACTOR = float(os.environ.get("CO2_FACTOR", "0.170"))

# Inverter nominal capacities (watts)
PIKO_15_RATED_W = 15_000
PIKO_CI_50_RATED_W = 50_000

# PIKO 15 status codes (dxs.json API) → Catalan text
STATUS_MAP = {
    0: "Apagat",
    1: "Inactiu",
    2: "Arrancant",
    3: "MPP (Producció)",
    4: "Regulat",
    5: "Error",
}

STATUS_CLASS = {
    0: "status-off",
    1: "status-idle",
    2: "status-idle",
    3: "status-ok",
    4: "status-ok",
    5: "status-error",
}

# PIKO CI 50 "Inverter state" (Modbus reg 56) → Catalan text. This is a DIFFERENT
# enum from the PIKO 15's: KOSTAL documents only four values (interface
# description MODBUS/SunSpec, note 2): 1 Init, 6 FeedIn, 10 Standby, 15 Shutdown.
# Verified live 2026-08-09: reg 56/57 read 6 (FeedIn) while grid-synced at
# 50.03 Hz. FeedIn means "connected and feeding" — it stays 6 while the
# zero-export limiter holds AC output at 0 W, which is exactly the case the old
# code mislabelled as "Apagat".
CI_STATUS_MAP = {
    1: "Arrancant",
    6: "Producció",
    10: "En espera",
    15: "Aturat",
}

CI_STATUS_CLASS = {
    1: "status-idle",
    6: "status-ok",
    10: "status-idle",
    15: "status-off",
}

# Backup InfluxDB (Pi)
BACKUP_INFLUXDB_URL = os.environ.get("BACKUP_INFLUXDB_URL", "")

# Paths
INVOICES_DIR = "/app/invoices"
PRICING_PATH = "/app/pricing.json"
OFFERS_PATH = "/app/offers.json"
