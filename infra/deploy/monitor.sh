#!/bin/sh
# Pings an external dead-man's-switch monitor with the pipeline state every 5 minutes.
# /api/forward/health and /api/paper/health answer 200 (OK/DEGRADED) or 503 (DOWN) with
# alert codes. Both must be 200 for a success ping.
API="${API_URL:-http://api:8000}"
while true; do
  fwd="$(curl -s -m 20 -w '\n%{http_code}' "$API/api/forward/health")"
  ppr="$(curl -s -m 20 -w '\n%{http_code}' "$API/api/paper/health")"
  fcode="$(printf '%s' "$fwd" | tail -n 1)"
  pcode="$(printf '%s' "$ppr" | tail -n 1)"
  code="$fcode"
  [ "$fcode" = "200" ] && code="$pcode"
  body="$(printf 'forward: %s\npaper: %s' "$fwd" "$ppr")"
  if [ -n "$HEALTHCHECK_PING_URL" ]; then
    if [ "$code" = "200" ]; then
      curl -fsS -m 10 --retry 3 -o /dev/null "$HEALTHCHECK_PING_URL"
    else
      # body (alert codes only, no secrets) is attached to the failure notification
      printf '%s' "$body" | head -c 9000 | curl -fsS -m 10 --retry 3 -o /dev/null \
        --data-binary @- "$HEALTHCHECK_PING_URL/fail"
    fi
  fi
  echo "$(date -u +%FT%TZ) pipeline health forward=$fcode paper=$pcode"
  sleep 300
done
