#!/bin/bash
# Полный бэкап vpnbot + Marzban (SQLite) + nginx на ГЛАВНОМ сервере панели.
# Запуск: root. Настройки: /etc/default/vpnbot-backup (опционально).
#
# Восстановление vpnbot.db: остановить бота → cp бэкап на место → chmod 640 → старт.
# Восстановление Marzban: остановить marzban → заменить db.sqlite3 → старт.

set -euo pipefail

# shellcheck source=/dev/null
[[ -f /etc/default/vpnbot-backup ]] && . /etc/default/vpnbot-backup

VPNDIR="${VPNDIR:-/opt/vpnbot}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/vpnbot}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
LOG_TAG="vpnbot-backup"
MARZBAN_CONTAINER="${MARZBAN_CONTAINER:-marzban-marzban-1}"
MARZBAN_DB_IN_CONTAINER="${MARZBAN_DB_IN_CONTAINER:-/var/lib/marzban/db.sqlite3}"

STAMP="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="${BACKUP_ROOT}/${STAMP}"
LATEST_LINK="${BACKUP_ROOT}/latest"
LOG_FILE="${BACKUP_ROOT}/backup.log"

log() {
  logger -t "$LOG_TAG" "$*" || true
  mkdir -p "$BACKUP_ROOT"
  echo "$(date -Iseconds) $*" >>"$LOG_FILE"
}

mkdir -p "$RUN_DIR"
log "start run_id=$STAMP"

# --- vpnbot SQLite (атомарный .backup, не голый cp) ---
if [[ -f "${VPNDIR}/vpnbot.db" ]]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${VPNDIR}/vpnbot.db" ".backup '${RUN_DIR}/vpnbot.db'"
  else
    log "WARN: sqlite3 not installed, falling back to cp vpnbot.db"
    cp -a "${VPNDIR}/vpnbot.db" "${RUN_DIR}/vpnbot.db"
  fi
  chmod 640 "${RUN_DIR}/vpnbot.db" 2>/dev/null || true
else
  log "WARN: no ${VPNDIR}/vpnbot.db"
fi

# --- .env (секреты; в архиве только на доверенном носителе) ---
if [[ -f "${VPNDIR}/.env" ]]; then
  install -m 600 "${VPNDIR}/.env" "${RUN_DIR}/env.dotenv"
fi

# --- Marzban: приоритет каталога на хосте, иначе дамп из контейнера ---
HOST_MZ="/var/lib/marzban"
if [[ -d "$HOST_MZ" ]]; then
  if [[ -f "${HOST_MZ}/db.sqlite3" ]] && command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${HOST_MZ}/db.sqlite3" ".backup '${RUN_DIR}/marzban-db.sqlite3'" || log "WARN host marzban db backup failed"
  fi
  # остальное (xray json, сертификаты) — без повторного db если уже скопировали
  tar -czf "${RUN_DIR}/marzban-var-lib.tgz" \
    --exclude='marzban/db.sqlite3' \
    -C "$(dirname "$HOST_MZ")" "$(basename "$HOST_MZ")" 2>/dev/null || log "WARN marzban tar partial"
elif docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$MARZBAN_CONTAINER"; then
  TMP_IN_CONTAINER="/tmp/marzban-sqlite-backup-${STAMP}.db"
  if docker exec "$MARZBAN_CONTAINER" sqlite3 "$MARZBAN_DB_IN_CONTAINER" ".backup '${TMP_IN_CONTAINER}'" 2>/dev/null; then
    docker cp "${MARZBAN_CONTAINER}:${TMP_IN_CONTAINER}" "${RUN_DIR}/marzban-db.sqlite3"
    docker exec "$MARZBAN_CONTAINER" rm -f "$TMP_IN_CONTAINER" 2>/dev/null || true
    log "marzban db from container ok"
  else
    log "WARN marzban docker sqlite backup failed (path ok? MARZBAN_DB_IN_CONTAINER)"
  fi
else
  log "WARN no host ${HOST_MZ} and container ${MARZBAN_CONTAINER} not running"
fi

# --- nginx (быстро поднять конфиг после переустановки) ---
if [[ -f /etc/nginx/nginx.conf ]]; then
  tar -czf "${RUN_DIR}/nginx-config.tgz" \
    /etc/nginx/nginx.conf \
    /etc/nginx/sites-enabled \
    /etc/nginx/sites-available \
    2>/dev/null || tar -czf "${RUN_DIR}/nginx-config.tgz" /etc/nginx/nginx.conf 2>/dev/null || log "WARN nginx tar skipped"
fi

# --- манифест ---
{
  echo "host=$(hostname)"
  echo "time=$(date -Iseconds)"
  echo "vpndir=${VPNDIR}"
  uname -a
  ls -la "$RUN_DIR"
} >"${RUN_DIR}/MANIFEST.txt"

ln -sfn "$RUN_DIR" "$LATEST_LINK"

# --- ротация ---
find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d -name '20*_*' -mtime "+${RETENTION_DAYS}" -exec rm -rf {} + 2>/dev/null || true

log "done $RUN_DIR"
exit 0
