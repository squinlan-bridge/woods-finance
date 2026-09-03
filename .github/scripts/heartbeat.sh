#!/usr/bin/env bash
# Write a per-feed heartbeat row to Supabase sync_heartbeat.
# Usage: heartbeat.sh <feed> <status> [note]
#   feed:    gl_accounts | expenses | budget
#   status:  ok | error
#   note:    optional free text
set -euo pipefail

# Secrets pasted into GitHub with a stray trailing newline break curl headers
# with an opaque 400 — strip ALL whitespace defensively before use.
SUPABASE_URL="$(printf '%s' "${SUPABASE_URL:-}" | tr -d '[:space:]')"
SUPABASE_SERVICE_KEY="$(printf '%s' "${SUPABASE_SERVICE_KEY:-}" | tr -d '[:space:]')"

FEED="${1:?feed name required}"
STATUS="${2:-ok}"
NOTE="${3:-workflow run ${GITHUB_RUN_ID:-manual}}"

: "${SUPABASE_URL:?SUPABASE_URL not set}"
: "${SUPABASE_SERVICE_KEY:?SUPABASE_SERVICE_KEY not set}"

# Upsert on the primary key (feed). Bumps last_run_at and updated_at via NOW()
# passed from Postgres — we send an ISO timestamp from the runner so a
# clock-skewed row is impossible.
NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

BODY=$(cat <<JSON
{
  "feed":         "${FEED}",
  "last_run_at":  "${NOW}",
  "updated_at":   "${NOW}",
  "status":       "${STATUS}",
  "note":         "${NOTE}"
}
JSON
)

curl -sS --fail-with-body \
  -X POST \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" \
  -H "Content-Type: application/json" \
  -H "Prefer: resolution=merge-duplicates" \
  "${SUPABASE_URL}/rest/v1/sync_heartbeat?on_conflict=feed" \
  --data "${BODY}" >/dev/null

echo "✓ Heartbeat written: ${FEED} = ${STATUS} @ ${NOW}"
