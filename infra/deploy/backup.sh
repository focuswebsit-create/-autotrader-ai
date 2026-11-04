#!/usr/bin/env bash
# Logical PostgreSQL backup with an integrity check. PAPER-ONLY data; no secrets inside
# besides what the DB itself stores (no wallet keys exist in this system).
#   backup.sh once   -> one backup now
#   backup.sh loop   -> one backup per day at BACKUP_HOUR_UTC
set -euo pipefail
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"
DIR=/backups

one() {
  local ts file
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  file="$DIR/autotrader_${ts}.dump"
  pg_dump --format=custom --compress=9 --no-owner --file="$file.partial"
  # a dump that pg_restore can't list is not a backup
  pg_restore --list "$file.partial" > /dev/null
  mv "$file.partial" "$file"
  sha256sum "$file" > "$file.sha256"
  find "$DIR" -name 'autotrader_*.dump*' -mtime +"$KEEP_DAYS" -delete
  echo "$(date -u +%FT%TZ) backup ok $file $(du -h "$file" | cut -f1)"
}

if [ "${1:-once}" = "loop" ]; then
  while true; do
    if [ "$(date -u +%H)" = "$(printf '%02d' "${BACKUP_HOUR_UTC:-3}")" ]; then
      one || echo "$(date -u +%FT%TZ) BACKUP FAILED" >&2
      sleep 3600
    fi
    sleep 300
  done
else
  one
fi
