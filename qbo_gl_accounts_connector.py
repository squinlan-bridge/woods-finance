"""
The Woods — QBO Chart of Accounts Connector
===========================================
Mirrors the Woods QBO Account list into Supabase table `gl_accounts` so the
dashboard reads the current chart of accounts at runtime instead of a
hardcoded JSX constant. New/retired accounts flow through automatically on
the nightly sync.

Captured per account:
  code, name, full_name, category (parent account's leaf name), account_type,
  account_subtype, active, parent_qbo_id.

Same env vars + token storage as qbo_connector.py.
"""

import os
import sys
import time
import requests

from qbo_connector import (
    load_tokens,
    refresh_access_token,
    run_query,
    SUPABASE_URL,
    SUPABASE_KEY,
)


# ── QBO fetcher ───────────────────────────────────────────────────────────
def fetch_all_accounts(access_token, company_id, refresh_token):
    """Fetch every Account (active + inactive) with pagination. Inactive
    accounts are kept but flagged active=false — historical transactions still
    reference them; the dashboard filters them out."""
    all_rows, start, page_size = [], 1, 1000
    while True:
        query = (
            "SELECT Id, Name, FullyQualifiedName, AcctNum, AccountType, "
            "AccountSubType, Active, ParentRef FROM Account "
            "WHERE Active IN (true, false) "
            f"STARTPOSITION {start} MAXRESULTS {page_size}"
        )
        rows, status = run_query(query, access_token, company_id)
        if status == "unauthorized":
            new_tok = refresh_access_token(refresh_token)
            if not new_tok:
                print("✗ Could not refresh token"); return None, access_token
            access_token = new_tok
            continue  # retry the same page
        if status != "ok" or rows is None:
            break
        batch = rows.get("Account", [])
        if not batch:
            break
        all_rows.extend(batch)
        print(f"  → fetched {len(all_rows)} accounts")
        if len(batch) < page_size:
            break
        start += page_size
    return all_rows, access_token


# ── Parser ────────────────────────────────────────────────────────────────
def build_records(accounts):
    """Two-pass: need every Name available before resolving parent → category."""
    id_to_name = {a.get("Id"): (a.get("Name") or "").strip() for a in accounts if a.get("Id")}
    records = []
    for a in accounts:
        qbo_id = (a.get("Id") or "").strip()
        if not qbo_id:
            continue
        name      = (a.get("Name") or "").strip()
        full_name = (a.get("FullyQualifiedName") or name).strip()
        parent_id = ((a.get("ParentRef") or {}).get("value") or "").strip() or None
        records.append({
            "qbo_id":          qbo_id,
            "code":            (a.get("AcctNum") or "").strip() or None,
            "name":            name,
            "full_name":       full_name,
            "category":        id_to_name.get(parent_id) if parent_id and parent_id in id_to_name else name,
            "account_type":    (a.get("AccountType") or "").strip() or None,
            "account_subtype": (a.get("AccountSubType") or "").strip() or None,
            "active":          bool(a.get("Active", True)),
            "parent_qbo_id":   parent_id,
            "source":          "QBO",
        })
    return records


# ── Supabase upsert (with transient-failure retry) ────────────────────────
def upsert_records(records, batch_size=500):
    if not records:
        print("  (no rows to upsert)"); return 0
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    url = f"{SUPABASE_URL}/rest/v1/gl_accounts?on_conflict=qbo_id"
    inserted = 0
    for i in range(0, len(records), batch_size):
        batch = records[i:i+batch_size]
        for attempt in range(4):
            try:
                r = requests.post(url, headers=headers, json=batch, timeout=30)
                break
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait = min(30, 2 ** attempt)
                print(f"  Supabase {type(e).__name__}; retry {attempt+1}/4 in {wait}s")
                time.sleep(wait)
        else:
            print(f"  ✗ gave up on batch {i//batch_size + 1}"); continue
        if r.status_code in (200, 201):
            inserted += len(batch)
        else:
            print(f"  ✗ Supabase {r.status_code}: {r.text[:300]}")
    return inserted


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    print("=" * 60); print("The Woods — QBO Chart of Accounts Connector"); print("=" * 60)

    missing = [v for v in ("SUPABASE_URL", "QBO_CLIENT_ID", "QBO_CLIENT_SECRET") if not os.environ.get(v)]
    if not SUPABASE_KEY:
        missing.append("SUPABASE_SERVICE_KEY")
    if missing:
        print(f"\n⚠  Missing env vars: {', '.join(missing)}"); sys.exit(1)

    tokens = load_tokens()
    if not tokens:
        sys.exit(1)

    print("\nFetching chart of accounts from QBO...")
    accounts, _ = fetch_all_accounts(tokens["access_token"], tokens["company_id"], tokens["refresh_token"])
    if accounts is None:
        print("✗ Fetch failed"); sys.exit(1)
    print(f"✓ {len(accounts)} accounts total ({sum(1 for a in accounts if a.get('Active', True))} active)")

    records = build_records(accounts)
    print(f"✓ Built {len(records)} records")

    print("\n--- Sample (first 5) ---")
    for r in records[:5]:
        print(f"  {r['code'] or '(no code)':<8} | {r['full_name'][:50]:<50} | "
              f"type={r['account_type'] or '?':<12} | active={r['active']}")

    n = upsert_records(records)
    print(f"\n✓ Upserted {n}/{len(records)} rows to gl_accounts.")


if __name__ == "__main__":
    main()
