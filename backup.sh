#!/bin/sh
# InfluxDB 2 daily backup script
# Runs inside the influxdb container via docker compose exec
#
# Keeps the last BACKUP_KEEP_DAYS daily snapshots (default: 7).
# Backups stored in /backups/ (mounted from host ./backups/).

set -e

BACKUP_DIR="/backups"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-7}"
DATE=$(date +%Y%m%d_%H%M%S)
TARGET="${BACKUP_DIR}/${DATE}"

INFLUX_HOST="${INFLUX_HOST:-http://influxdb:8086}"

echo "[backup] Starting InfluxDB backup to ${TARGET}"
influx backup "${TARGET}" \
  --host "${INFLUX_HOST}" \
  --token "${DOCKER_INFLUXDB_INIT_ADMIN_TOKEN}"

# Verify backup is non-empty
FILE_COUNT=$(find "${TARGET}" -type f | wc -l)
if [ "${FILE_COUNT}" -lt 1 ]; then
  echo "[backup] ERROR: backup appears empty, aborting cleanup"
  exit 1
fi
echo "[backup] Success: ${FILE_COUNT} files written"

# Prune old backups — keep only the last KEEP_DAYS
TOTAL=$(ls -1d "${BACKUP_DIR}"/[0-9]* 2>/dev/null | wc -l)
if [ "${TOTAL}" -gt "${KEEP_DAYS}" ]; then
  REMOVE=$((TOTAL - KEEP_DAYS))
  echo "[backup] Pruning ${REMOVE} old backup(s), keeping ${KEEP_DAYS}"
  ls -1d "${BACKUP_DIR}"/[0-9]* | head -n "${REMOVE}" | while read -r dir; do
    echo "[backup] Removing ${dir}"
    rm -rf "${dir}"
  done
fi

echo "[backup] Done"
