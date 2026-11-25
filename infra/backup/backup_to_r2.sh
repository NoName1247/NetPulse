#!/usr/bin/env bash
# Cron каждые 6 часов (0 */6 * * *):
# Потоковый pg_dump -> gzip -> клиентское шифрование age -> Cloudflare R2 (0 байт на диске VPS).
# Приватный ключ age хранится строго оффлайн!

set -Eeuo pipefail

: "${POSTGRES_CONTAINER:=netpulse-postgres}"
: "${POSTGRES_USER:=netpulse}"
: "${POSTGRES_DB:=netpulse}"
: "${AGE_PUBLIC_KEY:?AGE_PUBLIC_KEY (age1...) must be set}"
: "${RCLONE_REMOTE:=r2:netpulse-backups}"
: "${RETENTION_DAYS:=14}"
: "${TELEGRAM_BOT_TOKEN:=}"
: "${TELEGRAM_CHAT_ID:=}"

notify_failure() {
    local exit_code=$?
    local line_no=$1
    local msg="[NetPulse Backup FAILED] pg_dump->age->R2 failed on line ${line_no} (exit=${exit_code}). Retry scheduled in 15m."
    echo "${msg}" >&2
    if [[ -n "${TELEGRAM_BOT_TOKEN}" && -n "${TELEGRAM_CHAT_ID}" ]]; then
        curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${msg}" >/dev/null || true
    fi
    exit "${exit_code}"
}

trap 'notify_failure ${LINENO}' ERR

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OBJECT_PATH="${RCLONE_REMOTE}/pg_${POSTGRES_DB}_${TIMESTAMP}.sql.gz.age"

# 1. Потоковый дамп + сжатие + клиентское шифрование age прямо в R2 без временных файлов
docker exec "${POSTGRES_CONTAINER}" \
    pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" --no-owner --no-privileges \
    | gzip -6 \
    | age -r "${AGE_PUBLIC_KEY}" \
    | rclone rcat "${OBJECT_PATH}"

# 2. Ротация старых бэкапов в R2 (ретеншн 14 дней)
rclone delete "${RCLONE_REMOTE}" --min-age "${RETENTION_DAYS}d" --include "pg_${POSTGRES_DB}_*.sql.gz.age"

echo "[OK] Encrypted backup uploaded to ${OBJECT_PATH}"