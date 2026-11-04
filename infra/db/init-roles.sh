#!/bin/sh
# Docker postgres init hook: creates the least-privilege login users on first start.
# Passwords come from the container environment (see docker-compose.yml / .env).
set -eu
: "${AUTOTRADER_API_DB_PASSWORD:?set AUTOTRADER_API_DB_PASSWORD}"
: "${AUTOTRADER_WORKER_DB_PASSWORD:?set AUTOTRADER_WORKER_DB_PASSWORD}"
: "${AUTOTRADER_CONTROL_DB_PASSWORD:?set AUTOTRADER_CONTROL_DB_PASSWORD}"
: "${AUTOTRADER_PAPER_DB_PASSWORD:?set AUTOTRADER_PAPER_DB_PASSWORD}"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v api_password="$AUTOTRADER_API_DB_PASSWORD" \
  -v worker_password="$AUTOTRADER_WORKER_DB_PASSWORD" \
  -v control_password="$AUTOTRADER_CONTROL_DB_PASSWORD" \
  -v paper_password="$AUTOTRADER_PAPER_DB_PASSWORD" \
  -f /docker-entrypoint-initdb.d/roles.sql.in
