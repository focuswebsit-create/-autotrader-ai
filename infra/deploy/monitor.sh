#!/bin/sh
# Pings an external dead-man's-switch monitor with the pipeline state every 5 minutes.
# /api/forward/health answers 200 (OK/DEGRADED) or 503 (DOWN) with alert codes.
API="${API_URL:-http://api:8000}"
while true; do
  body="$(curl -s -m 20 -w '\n%{http_code}' "$API/api/forward/health")"
  code="$(printf '%s' "$body" | tail -n 1)"
  if [ -n "$HEALTHCHECK_PING_URL" ]; then
    if [ "$code" = "200" ]; then
      curl -fsS -m 10 --retry 3 -o /dev/null "$HEALTHCHECK_PING_URL"
    else
      # body (alert codes only, no secrets) is attached to the failure notification
      printf '%s' "$body" | head -c 9000 | curl -fsS -m 10 --retry 3 -o /dev/null \
        --data-binary @- "$HEALTHCHECK_PING_URL/fail"
    fi
  fi
  echo "$(date -u +%FT%TZ) pipeline health http=$code"
  sleep 300
done
