#!/usr/bin/env bash
# Read sync_freshness view. If any feed's last_run_at is older than the view threshold (or its
# status is 'error' or 'seed'), print a report and exit non-zero so GH Actions
# marks the workflow failed. The 20h threshold is baked into the view — see
# migration `woods_initial_schema (20h threshold)`.
set -euo pipefail

# Secrets pasted into GitHub with a stray trailing newline break curl headers
# with an opaque 400 — strip ALL whitespace defensively before use.
SUPABASE_URL="$(printf '%s' "${SUPABASE_URL:-}" | tr -d '[:space:]')"
SUPABASE_SERVICE_KEY="$(printf '%s' "${SUPABASE_SERVICE_KEY:-}" | tr -d '[:space:]')"

: "${SUPABASE_URL:?SUPABASE_URL not set}"
: "${SUPABASE_SERVICE_KEY:?SUPABASE_SERVICE_KEY not set}"

RESP=$(curl -sS --fail-with-body \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" \
  -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" \
  "${SUPABASE_URL}/rest/v1/sync_freshness?select=feed,last_run_at,status,age,stale&order=stale.desc,age.desc")

echo "── Feed freshness ──"
echo "${RESP}" | python3 -c "
import json, sys
rows = json.load(sys.stdin)
if not rows:
    print('No heartbeats found (empty sync_heartbeat table?)'); sys.exit(2)
w = max(len(r['feed']) for r in rows)
for r in rows:
    flag = '✗ STALE' if r['stale'] else '✓ fresh'
    print(f\"  {flag}  {r['feed']:<{w}}  last_run={r['last_run_at']}  status={r['status']}  age={r['age']}\")
stale   = [r for r in rows if r['stale']]
errored = [r for r in rows if r['status'] in ('error','seed')]
bad = {r['feed'] for r in stale} | {r['feed'] for r in errored}
if bad:
    print()
    print(f'✗ {len(bad)} feed(s) failed freshness/status check: {sorted(bad)}')
    sys.exit(1)
print()
print('✓ All feeds fresh and status=ok')
"
