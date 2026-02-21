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

# Inverter status codes → Catalan text
STATUS_MAP = {
    0: "Apagat",
    1: "Inactiu",
    2: "Arrancant",
    3: "MPP (Producció)",
    4: "Regulat",
    5: "Error",
}

# Paths
INVOICES_DIR = "/app/invoices"
PRICING_PATH = "/app/pricing.json"
OFFERS_PATH = "/app/offers.json"
