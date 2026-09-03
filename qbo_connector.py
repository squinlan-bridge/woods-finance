"""
The Woods — QBO Expense Connector
=================================
Pulls expense transactions (Purchases + Bills) from The Woods' QuickBooks
Online company and upserts into the Woods Supabase `expenses` table.

Modeled on bridge-finance/qbo_connector.py with one deliberate difference:
there is NO hardcoded name→GL lookup table. The Woods chart of accounts is
resolved entirely from the live Account list (AccountRef.value → AcctNum),
with a numeric-prefix regex as the only fallback. Expense-ness is decided by
AccountType, not by GL-code numbering, so Woods' numbering scheme doesn't
matter.

Run qbo_auth.py once first to authorize the Woods QBO company.

Environment variables (see .env.example):
    SUPABASE_URL, SUPABASE_SERVICE_KEY, QBO_CLIENT_ID, QBO_CLIENT_SECRET
"""

import os
import json
import base64
import re
import requests
from datetime import date, datetime

# ── .env autoload (same pattern as bridge connectors) ──────────────────────
def _load_env_file(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            if s.startswith("export "):
                s = s[len("export "):]
            k, _, v = s.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v

_load_env_file()

# ── Config ─────────────────────────────────────────────────────────────────
SUPABASE_URL  = os.environ.get("SUPABASE_URL", "").strip()
# service_role key bypasses RLS; server-side only. anon fallback kept for parity.
SUPABASE_KEY  = (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")).strip()
CLIENT_ID     = os.environ.get("QBO_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("QBO_CLIENT_SECRET", "").strip()
COMPANY_ID    = os.environ.get("QBO_COMPANY_ID", "").strip()
TOKEN_FILE    = "qbo_tokens.json"

QBO_BASE      = "https://quickbooks.api.intuit.com"
TOKEN_URL     = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

# Earliest transaction date to sync. Override with WOODS_START_DATE if The
# Woods' QBO history starts later/earlier.
START_DATE = os.environ.get("WOODS_START_DATE", "2023-06-01")

# FY logic: June=0 through May=11 (same fiscal calendar as The Bridge).
FY_START_MONTH = 6

# Account types treated as expenses. Decides inclusion INSTEAD of GL-code
# numbering (Bridge filtered on 6/7/8xxxx prefixes — Woods numbering may differ).
EXPENSE_ACCOUNT_TYPES = {"Expense", "Other Expense", "Cost of Goods Sold"}


# ── FY helpers ─────────────────────────────────────────────────────────────
def fiscal_year_label(d):
    """FY runs June–May; named for the year it ends in. June 2026 → FY27."""
    if d.month >= FY_START_MONTH:
        return f"FY{str(d.year + 1)[2:]}"
    return f"FY{str(d.year)[2:]}"


def fiscal_month_index(d):
    return (d.month - FY_START_MONTH) % 12


# ── Token management ───────────────────────────────────────────────────────
# Tokens persist in Supabase (qbo_tokens, single row id=1) so GitHub Actions
# runs share state — QBO refresh tokens rotate ~every 100 days and would be
# lost between CI runs otherwise. qbo_tokens.json is the local bootstrap.

def _load_tokens_from_supabase():
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/qbo_tokens?id=eq.1&select=*",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            timeout=10,
        )
        if r.status_code == 200 and r.json():
            return r.json()[0]
    except requests.RequestException as e:
        print(f"  ⚠ Could not read qbo_tokens from Supabase: {e}")
    return None


def _save_tokens_to_supabase(access_token, refresh_token, company_id):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  ⚠ Supabase not configured — refresh token NOT persisted. Cron will break in <100 days.")
        return False
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    payload = [{
        "id":            1,
        "access_token":  access_token,
        "refresh_token": refresh_token,
        "company_id":    company_id,
        "refreshed_at":  datetime.utcnow().isoformat() + "Z",
    }]
    r = requests.post(f"{SUPABASE_URL}/rest/v1/qbo_tokens", headers=headers, json=payload, timeout=10)
    if r.status_code in (200, 201):
        return True
    print(f"  ✗ Could not save qbo_tokens to Supabase: {r.status_code} {r.text[:200]}")
    return False


def load_tokens():
    sb = _load_tokens_from_supabase()
    if sb:
        return sb
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    print(f"✗ No QBO tokens found in Supabase or {TOKEN_FILE}.")
    print("  Run python3 qbo_auth.py first to authorize The Woods' QBO company.")
    return None


def refresh_access_token(refresh_token):
    """QBO access tokens expire after 1 hour. Refresh tokens rotate ~every 100 days."""
    credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()
    headers = {
        "Accept":        "application/json",
        "Authorization": f"Basic {encoded}",
        "Content-Type":  "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
    response = requests.post(TOKEN_URL, headers=headers, data=data)
    if response.status_code == 200:
        new_tokens  = response.json()
        tokens      = load_tokens() or {}
        new_access  = new_tokens["access_token"]
        new_refresh = new_tokens.get("refresh_token", tokens.get("refresh_token", refresh_token))
        company_id  = tokens.get("company_id") or COMPANY_ID
        _save_tokens_to_supabase(new_access, new_refresh, company_id)
        if os.path.exists(TOKEN_FILE):
            tokens["access_token"]  = new_access
            tokens["refresh_token"] = new_refresh
            with open(TOKEN_FILE, "w") as f:
                json.dump(tokens, f, indent=2)
        print("  ✓ Access token refreshed.")
        return new_access
    print(f"  ✗ Token refresh failed: {response.status_code}")
    print(response.text)
    return None


# ── QBO API query ──────────────────────────────────────────────────────────
def run_query(query, access_token, company_id):
    """Run a QBO SQL-style query; returns (QueryResponse dict, status)."""
    url = f"{QBO_BASE}/v3/company/{company_id}/query"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params = {"query": query, "minorversion": "65"}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 401:
        return None, "unauthorized"
    if response.status_code != 200:
        print(f"  ✗ Query failed: {response.status_code}")
        print(f"    {response.text[:300]}")
        return None, "error"
    return response.json().get("QueryResponse", {}), "ok"


def _fetch_entity(entity, access_token, company_id):
    """Fetch all rows of a transaction entity since START_DATE, paginated."""
    all_rows, start, page_size = [], 1, 1000
    print(f"  Fetching {entity} transactions...")
    while True:
        query = (f"SELECT * FROM {entity} WHERE TxnDate >= '{START_DATE}' "
                 f"STARTPOSITION {start} MAXRESULTS {page_size}")
        rows, status = run_query(query, access_token, company_id)
        if status == "unauthorized":
            return None, "unauthorized"
        if status == "error" or rows is None:
            break
        batch = rows.get(entity, [])
        if not batch:
            break
        all_rows.extend(batch)
        print(f"    → {len(all_rows)} so far...")
        if len(batch) < page_size:
            break
        start += page_size
    return all_rows, "ok"


# ── Chart-of-accounts GL mapping ───────────────────────────────────────────
# AccountId → {code, full_name, type, category}. Populated in main(); the
# parsers consult it as the AUTHORITATIVE source for GL code, name, category
# and expense-ness.
ACCOUNT_MAP = {}


def fetch_account_map(access_token, company_id):
    """Fetch the Woods chart of accounts keyed by Account Id."""
    acct_map = {}
    start = 1
    while True:
        q = ("SELECT Id, Name, FullyQualifiedName, AcctNum, AccountType, ParentRef "
             f"FROM Account WHERE Active IN (true, false) STARTPOSITION {start} MAXRESULTS 1000")
        data, status = run_query(q, access_token, company_id)
        if status != "ok" or data is None:
            return acct_map, status
        batch = data.get("Account", [])
        for a in batch:
            acct_map[a["Id"]] = {
                "code":      (a.get("AcctNum") or "").strip(),
                "full_name": a.get("FullyQualifiedName") or a.get("Name", ""),
                "type":      (a.get("AccountType") or "").strip(),
                "parent_id": (a.get("ParentRef") or {}).get("value", ""),
            }
        if len(batch) < 1000:
            break
        start += 1000
    # Category = parent account's leaf name (top-level accounts categorize as themselves).
    id_to_leaf = {aid: (m["full_name"].split(":")[-1] if m["full_name"] else "") for aid, m in acct_map.items()}
    for m in acct_map.values():
        parent = m.get("parent_id")
        leaf = m["full_name"].split(":")[-1] if m["full_name"] else ""
        m["category"] = id_to_leaf.get(parent) or leaf or "Uncategorized"
    return acct_map, "ok"


def resolve_account(account_ref):
    """Return the ACCOUNT_MAP entry for an AccountRef, or None."""
    return ACCOUNT_MAP.get(account_ref.get("value", ""))


# ── Transaction parsers ────────────────────────────────────────────────────
def _parse_txn(txn, kind, vendor_key):
    """Shared parser for Purchase and Bill transactions → expense records."""
    records = []
    txn_date_str = txn.get("TxnDate", "")
    if not txn_date_str:
        return records
    try:
        txn_date = date.fromisoformat(txn_date_str)
    except ValueError:
        return records

    fy       = fiscal_year_label(txn_date)
    fm       = fiscal_month_index(txn_date)
    vendor   = txn.get(vendor_key, {}).get("name", "Unknown")
    txn_id   = txn.get("Id", "")
    txn_memo = (txn.get("PrivateNote") or "").strip()

    for idx, line in enumerate(txn.get("Line", [])):
        detail = line.get("AccountBasedExpenseLineDetail", {})
        if not detail:
            continue
        account_ref = detail.get("AccountRef", {})
        full_name   = account_ref.get("name", "")
        amount      = float(line.get("Amount", 0))
        if amount <= 0 or not full_name:
            continue

        acct = resolve_account(account_ref)
        if acct:
            # Authoritative: only keep expense-type accounts.
            if acct["type"] not in EXPENSE_ACCOUNT_TYPES:
                continue
            gl_code  = acct["code"]
            gl_name  = acct["full_name"] or full_name
            category = acct["category"]
            if not gl_code:
                # Expense account with no AcctNum — try the name prefix.
                m = re.match(r"^(\d{4,6})\s+", full_name)
                gl_code = m.group(1) if m else ""
        else:
            # Account map miss (shouldn't happen once map is fetched) —
            # numeric name prefix is the only fallback; unknown types skipped.
            m = re.match(r"^(\d{4,6})\s+", full_name)
            if not m:
                continue
            gl_code, gl_name, category = m.group(1), full_name, "Uncategorized"

        line_desc = (line.get("Description") or "").strip()
        memo = line_desc or txn_memo or None

        records.append({
            "qbo_id":             f"{kind}_{txn_id}_{idx}",
            "date":               txn_date_str,
            "fiscal_year":        fy,
            "fiscal_month_index": fm,
            "gl_code":            gl_code,
            "gl_name":            gl_name,
            "category":           category,
            "amount":             amount,
            "vendor":             vendor,
            "memo":               memo,
            "source":             "QBO",
        })
    return records


def parse_purchase(txn):
    return _parse_txn(txn, "Purchase", "EntityRef")


def parse_bill(txn):
    return _parse_txn(txn, "Bill", "VendorRef")


# ── Supabase upserter ──────────────────────────────────────────────────────
def upsert_to_supabase(records, table="expenses", conflict="qbo_id", batch_size=500):
    if not records:
        print("  No records to upsert.")
        return 0
    headers = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "resolution=merge-duplicates",
    }
    url = f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={conflict}"
    total, inserted = len(records), 0
    for i in range(0, total, batch_size):
        batch = records[i: i + batch_size]
        response = requests.post(url, headers=headers, json=batch)
        if response.status_code in (200, 201):
            inserted += len(batch)
            print(f"  ✓ Upserted rows {i+1}–{min(i+batch_size, total)} of {total}")
        else:
            print(f"  ✗ Error on batch {i//batch_size + 1}: {response.status_code}")
            print(f"    {response.text[:300]}")
    return inserted


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("The Woods — QBO Expense Connector")
    print("=" * 60)

    missing = [v for v in ("SUPABASE_URL", "QBO_CLIENT_ID", "QBO_CLIENT_SECRET") if not os.environ.get(v)]
    if not SUPABASE_KEY:
        missing.append("SUPABASE_SERVICE_KEY")
    if missing:
        print(f"\n⚠  Missing environment variables: {', '.join(missing)}")
        return

    tokens = load_tokens()
    if not tokens:
        return
    access_token  = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    company_id    = tokens["company_id"]
    print(f"\nCompany ID: {company_id}")
    print(f"Date range: {START_DATE} to present\n")

    # Chart of accounts FIRST — parsing depends on it for type/category/code.
    global ACCOUNT_MAP
    ACCOUNT_MAP, status = fetch_account_map(access_token, company_id)
    if status == "unauthorized":
        access_token = refresh_access_token(refresh_token)
        if not access_token:
            return
        ACCOUNT_MAP, status = fetch_account_map(access_token, company_id)
    print(f"  → {len(ACCOUNT_MAP)} accounts mapped "
          f"({sum(1 for v in ACCOUNT_MAP.values() if v['code'])} with AcctNum)")

    purchases, status = _fetch_entity("Purchase", access_token, company_id)
    if status == "unauthorized":
        access_token = refresh_access_token(refresh_token)
        if not access_token:
            return
        purchases, status = _fetch_entity("Purchase", access_token, company_id)
    purchases = purchases or []
    print(f"  → {len(purchases)} Purchase transactions fetched")

    bills, status = _fetch_entity("Bill", access_token, company_id)
    if status == "unauthorized":
        access_token = refresh_access_token(refresh_token)
        if not access_token:
            return
        bills, status = _fetch_entity("Bill", access_token, company_id)
    bills = bills or []
    print(f"  → {len(bills)} Bill transactions fetched")

    print("\nParsing transactions...")
    all_records = []
    for txn in purchases:
        all_records.extend(parse_purchase(txn))
    for txn in bills:
        all_records.extend(parse_bill(txn))
    print(f"  → {len(all_records)} expense line items parsed")

    if not all_records:
        print("\n  No expense records found. Check the date range and that the")
        print("  Woods QBO company has Purchase/Bill transactions.")
        return

    print("\n--- Sample records (first 5) ---")
    for r in all_records[:5]:
        print(f"  {r['date']} | {r['gl_name'][:34]:34} | {r['category'][:18]:18} | ${r['amount']:>10,.2f} | {r['vendor'][:20]}")

    from collections import Counter
    fy_counts = Counter(r["fiscal_year"] for r in all_records)
    print(f"\nRecords by fiscal year: {dict(sorted(fy_counts.items()))}")
    no_code = sum(1 for r in all_records if not r["gl_code"])
    if no_code:
        print(f"⚠ {no_code} records have NO gl_code (expense account without AcctNum in QBO)")

    if os.environ.get("CI", "").lower() == "true":
        print("\nCI=true → auto-confirming upsert.")
    else:
        confirm = input("\nProceed with upsert to Supabase? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted. No data was written.")
            return

    print("\nUpserting to Supabase...")
    inserted = upsert_to_supabase(all_records)
    print(f"\n✓ Woods QBO connector complete. {inserted}/{len(all_records)} records loaded.")


if __name__ == "__main__":
    main()
