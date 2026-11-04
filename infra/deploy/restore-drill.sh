#!/usr/bin/env bash
# Restore the newest backup into a throw-away database and compare row counts.
# Run monthly (or after any change to backups). Never touches the live database.
#   docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \
#     --entrypoint bash backup /restore-drill.sh   (mount this file like backup.sh)
set -euo pipefail
latest="$(ls -1t /backups/autotrader_*.dump | head -n 1)"
sha256sum -c "$latest.sha256"
psql -d postgres -c "DROP DATABASE IF EXISTS autotrader_restore_drill"
psql -d postgres -c "CREATE DATABASE autotrader_restore_drill"
pg_restore --no-owner --dbname=autotrader_restore_drill "$latest"
for t in candles funding_rates forward_tests forward_decisions forward_events backtest_runs; do
  live="$(psql -At -c "SELECT count(*) FROM $t")"
  rest="$(psql -At -d autotrader_restore_drill -c "SELECT count(*) FROM $t")"
  echo "$t live=$live restored=$rest"
done
psql -d postgres -c "DROP DATABASE autotrader_restore_drill"
echo "restore drill OK: $latest"
