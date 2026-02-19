import os
import time
import logging

import requests
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("collector")

INVERTER_IP = os.environ["INVERTER_IP"]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 30))
INFLUXDB_URL = os.environ["INFLUXDB_URL"]
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_ORG = os.environ["INFLUXDB_ORG"]
INFLUXDB_BUCKET = os.environ["INFLUXDB_BUCKET"]

# dxsId → field name mapping
DXS_FIELDS = {
    67109120: ("ac_power_total", float),
    33556736: ("dc_power_total", float),
    33555203: ("dc_power_string1", float),
    33555459: ("dc_power_string2", float),
    33555715: ("dc_power_string3", float),
    33555202: ("dc_voltage_string1", float),
    33555458: ("dc_voltage_string2", float),
    33555714: ("dc_voltage_string3", float),
    33555201: ("dc_current_string1", float),
    33555457: ("dc_current_string2", float),
    33555713: ("dc_current_string3", float),
    67109379: ("ac_power_l1", float),
    67109635: ("ac_power_l2", float),
    67109891: ("ac_power_l3", float),
    67109378: ("ac_voltage_l1", float),
    67109634: ("ac_voltage_l2", float),
    67109890: ("ac_voltage_l3", float),
    67110400: ("grid_frequency", float),
    251658753: ("yield_total", float),
    251658754: ("yield_daily", float),
    251658496: ("operating_hours", float),
    16780032: ("status", int),
    251659010: ("home_consumption_daily", float),
    251659266: ("self_consumption_daily", float),
    251659278: ("self_consumption_rate_daily", float),
}

BASE_URL = f"http://{INVERTER_IP}/api/dxs.json"
DXS_PARAMS = [("dxsEntries", str(k)) for k in DXS_FIELDS]


def poll_inverter():
    resp = requests.get(BASE_URL, params=DXS_PARAMS, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    point = Point("piko")
    for entry in data.get("dxsEntries", []):
        dxs_id = entry["dxsId"]
        if dxs_id in DXS_FIELDS:
            field_name, cast = DXS_FIELDS[dxs_id]
            point = point.field(field_name, cast(entry["value"]))

    return point


def main():
    log.info("Starting Kostal PIKO 15 collector")
    log.info("Inverter: %s | Poll interval: %ds", INVERTER_IP, POLL_INTERVAL)

    client = InfluxDBClient(url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)

    while True:
        try:
            point = poll_inverter()
            write_api.write(bucket=INFLUXDB_BUCKET, record=point)
            log.info("Data written successfully")
        except requests.exceptions.ConnectionError:
            log.debug("Inverter unreachable (likely night time)")
        except Exception:
            log.exception("Error polling inverter")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
