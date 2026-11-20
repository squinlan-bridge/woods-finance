"""
The Woods — QBO Expense Connector
=================================
Pulls every transaction line that posts to an expense-type account in The
Woods' QuickBooks Online company and mirrors it into the Woods Supabase
`expenses` table.

Entity coverage (amounts are SIGNED — positive = expense, negative = credit):
    Purchase       checks / card / cash expenses   (+; card refunds −)
    Bill           vendor bills                    (+)
    VendorCredit   vendor credits                  (−)
    JournalEntry   expense-account lines only      (debit +, credit −)
                   → payroll accruals/reversals and after-the-fact reclasses
    Deposit        expense-account lines only      (sign flipped)
                   → refunds and stop-payment credits deposited back
    Invoice / SalesReceipt / CreditMemo / RefundReceipt
                   item lines whose item posts to an expense account
                   → PayPal/PushPay fee lines on Bloomerang-imported
                     invoices (5222)

The feed is a MIRROR, not an append log: every run re-pulls full history
since START_DATE, upserts on qbo_id, then deletes QBO-sourced rows that no
longer exist upstream (deleted transactions, removed lines). Deletion only
runs when every entity fetched cleanly and is capped by a safety threshold.

Every row carries its QBO class (program) as a fully-qualified name, or
NULL when the line is unclassified ("Not Specified" on QBO reports).

There is NO hardcoded name→GL lookup table. The chart of accounts is resolved
from the live Account list (AccountRef.value → AcctNum); expense-ness is
decided by AccountType, not GL numbering.

Environment variables (see .env.example):
    SUPABASE_URL, SUPABASE_SERVICE_KEY, QBO_CLIENT_ID, QBO_CLIENT_SECRET
Optional:
    WOODS_START_DATE          earliest TxnDate to sync (default 2023-06-01)
    WOODS_DRY_RUN=1           fetch + parse + report, write nothing
    WOODS_MAX_STALE_DELETE    override the stale-row deletion cap
"""

import sys
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


class QBOFetchError(Exception):
    pass


# Mutable auth state so one 401 mid-run refreshes once and every later call
# picks up the new access token.
AUTH = {"access_token": None, "refresh_token": None, "company_id": None}


def qbo_query(query):
    """run_query with a single refresh-and-retry on 401. Raises on failure."""
    rows, status = run_query(query, AUTH["access_token"], AUTH["company_id"])
    if status == "unauthorized":
        new_access = refresh_access_token(AUTH["refresh_token"])
        if not new_access:
            raise QBOFetchError("token refresh failed")
        AUTH["access_token"] = new_access
        rows, status = run_query(query, AUTH["access_token"], AUTH["company_id"])
    if status != "ok" or rows is None:
        raise QBOFetchError(f"query failed ({status}): {query[:80]}")
    return rows


def _fetch_entity(entity):
    """Fetch all rows of a transaction entity since START_DATE, paginated.

    Raises QBOFetchError on any failed page — a partial pull must never be
    mistaken for a complete one, because stale-row deletion keys off it.
    """
    all_rows, start, page_size = [], 1, 1000
    print(f"  Fetching {entity} transactions...")
    while True:
        query = (f"SELECT * FROM {entity} WHERE TxnDate >= '{START_DATE}' "
                 f"STARTPOSITION {start} MAXRESULTS {page_size}")
        batch = qbo_query(query).get(entity, [])
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    print(f"    → {len(all_rows)} {entity} transactions")
    return all_rows


def qbo_report(name, **params):
    """Fetch a QBO report (ProfitAndLoss, BalanceSheet, ...) as JSON.

    Shared by the report-based feeds. One refresh-and-retry on 401; raises
    QBOFetchError on anything else so callers never store a partial pull.
    """
    params.setdefault("minorversion", "65")
    url = f"{QBO_BASE}/v3/company/{AUTH['company_id']}/reports/{name}"
    for attempt in (1, 2):
        r = requests.get(url, params=params, timeout=120,
                         headers={"Authorization": f"Bearer {AUTH['access_token']}",
                                  "Accept": "application/json"})
        if r.status_code == 401 and attempt == 1:
            new_access = refresh_access_token(AUTH["refresh_token"])
            if not new_access:
                raise QBOFetchError("token refresh failed")
            AUTH["access_token"] = new_access
            continue
        if r.status_code != 200:
            raise QBOFetchError(f"{name} report failed: {r.status_code} {r.text[:200]}")
        return r.json()


_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
_COL_RE = re.compile(r"^([A-Z][a-z]{2})\s+(?:(\d{1,2})-(\d{1,2}),\s+)?(\d{4})$")


def report_month_columns(report, complete_only=False):
    """Column index → (year, month) for a report summarized by month.

    QBO titles a full month "Oct 2026" and an in-progress one
    "Nov 1-20, 2026". `complete_only` drops the in-progress month (balance
    feeds store month-END balances only). The "Total" column never matches.
    """
    cols = {}
    for i, col in enumerate(report.get("Columns", {}).get("Column", [])):
        m = _COL_RE.match((col.get("ColTitle") or "").strip())
        if not m or m.group(1) not in _MONTHS:
            continue
        year, month = int(m.group(4)), _MONTHS[m.group(1)]
        if m.group(3):
            import calendar
            partial = (int(m.group(2)), int(m.group(3))) != (1, calendar.monthrange(year, month)[1])
            if partial and complete_only:
                continue
        cols[i] = (year, month)
    return cols


def report_values(coldata, cols):
    """{(year, month): float} for one report row; blank cells are absent."""
    out = {}
    for i, ym in cols.items():
        raw = (coldata[i].get("value") or "").strip() if i < len(coldata) else ""
        if raw:
            out[ym] = float(raw.replace(",", ""))
    return out


def fiscal_year_starts(today=None):
    """June-1 fiscal-year start dates from START_DATE's fiscal year to today."""
    today = today or date.today()
    first = date.fromisoformat(START_DATE)
    year = first.year if first.month >= FY_START_MONTH else first.year - 1
    starts = []
    while date(year, FY_START_MONTH, 1) <= today:
        starts.append(max(date(year, FY_START_MONTH, 1), first))
        year += 1
    return starts


def start_session():
    """Validate env, load tokens into AUTH and fetch the chart of accounts.
    Returns False (after printing why) if the run cannot proceed."""
    global ACCOUNT_MAP
    missing = [v for v in ("SUPABASE_URL", "QBO_CLIENT_ID", "QBO_CLIENT_SECRET") if not os.environ.get(v)]
    if not SUPABASE_KEY:
        missing.append("SUPABASE_SERVICE_KEY")
    if missing:
        print(f"\n⚠  Missing environment variables: {', '.join(missing)}")
        return False
    tokens = load_tokens()
    if not tokens:
        return False
    AUTH.update(access_token=tokens["access_token"],
                refresh_token=tokens["refresh_token"],
                company_id=tokens.get("company_id") or COMPANY_ID)
    print(f"\nCompany ID: {AUTH['company_id']}")

    # Chart of accounts FIRST — parsing depends on it for type/category/code.
    ACCOUNT_MAP, status = fetch_account_map(AUTH["access_token"], AUTH["company_id"])
    if status == "unauthorized":
        new_access = refresh_access_token(AUTH["refresh_token"])
        if not new_access:
            return False
        AUTH["access_token"] = new_access
        ACCOUNT_MAP, status = fetch_account_map(AUTH["access_token"], AUTH["company_id"])
    if status != "ok" or not ACCOUNT_MAP:
        print(f"  ✗ Could not fetch the chart of accounts ({status}).")
        return False
    print(f"  → {len(ACCOUNT_MAP)} accounts mapped "
          f"({sum(1 for v in ACCOUNT_MAP.values() if v['code'])} with AcctNum)")
    return True


def is_dry_run():
    return os.environ.get("WOODS_DRY_RUN", "").strip().lower() in ("1", "true", "yes")


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


# ── Item → account mapping ─────────────────────────────────────────────────
# Item-based lines carry an ItemRef, not an AccountRef; the posting account
# comes from the item. ItemId → {"income": AccountRef, "expense": AccountRef}.
ITEM_MAP = {}


def fetch_item_map():
    item_map, start = {}, 1
    while True:
        batch = qbo_query("SELECT * FROM Item WHERE Active IN (true, false) "
                          f"STARTPOSITION {start} MAXRESULTS 1000").get("Item", [])
        for it in batch:
            item_map[it["Id"]] = {
                "income":  it.get("IncomeAccountRef") or {},
                "expense": it.get("ExpenseAccountRef") or {},
            }
        if len(batch) < 1000:
            break
        start += 1000
    return item_map


# ── Class mapping ──────────────────────────────────────────────────────────
# QBO classes are the camp's programs (Summer Programs:150 High School Camp…).
# ClassRef sits on each LINE; its .name is only the leaf, so resolve the id
# to the fully-qualified name. ClassId → FullyQualifiedName.
CLASS_MAP = {}


def fetch_class_map():
    rows = qbo_query("SELECT * FROM Class WHERE Active IN (true, false) MAXRESULTS 1000").get("Class", [])
    return {c["Id"]: c.get("FullyQualifiedName") or c.get("Name", "") for c in rows}


def resolve_class(detail, txn):
    """Class name for a line: the line's ClassRef, else the transaction's.
    None when unclassified (QBO reports show these as "Not Specified")."""
    ref = (detail or {}).get("ClassRef") or txn.get("ClassRef") or {}
    if not ref.get("value"):
        return None
    return CLASS_MAP.get(ref["value"]) or ref.get("name") or None


# ── Transaction parsers ────────────────────────────────────────────────────
def _txn_header(txn):
    """Common header fields, or None if the transaction has no usable date."""
    txn_date_str = txn.get("TxnDate", "")
    try:
        txn_date = date.fromisoformat(txn_date_str)
    except ValueError:
        return None
    return {
        "date":   txn_date_str,
        "fy":     fiscal_year_label(txn_date),
        "fm":     fiscal_month_index(txn_date),
        "id":     txn.get("Id", ""),
        "memo":   (txn.get("PrivateNote") or "").strip(),
    }


def _expense_record(qbo_id, hdr, account_ref, amount, vendor, line, class_name=None):
    """Build one expenses row, or None if the line isn't an expense posting.

    `amount` is already signed (positive = expense, negative = credit).
    """
    amount = round(amount, 2)
    full_name = account_ref.get("name", "")
    if amount == 0 or not account_ref:
        return None

    acct = resolve_account(account_ref)
    if acct:
        # Authoritative: only keep expense-type accounts.
        if acct["type"] not in EXPENSE_ACCOUNT_TYPES:
            return None
        gl_code  = acct["code"]
        gl_name  = acct["full_name"] or full_name
        category = acct["category"]
        if not gl_code:
            # Expense account with no AcctNum — try the name prefix.
            m = re.match(r"^(\d{4,6})\s+", full_name)
            gl_code = m.group(1) if m else ""
    else:
        # Account map miss (shouldn't happen once map is fetched) — numeric
        # name prefix is the only fallback; unknown types skipped.
        m = re.match(r"^(\d{4,6})\s+", full_name)
        if not m:
            return None
        gl_code, gl_name, category = m.group(1), full_name, "Uncategorized"

    line_desc = (line.get("Description") or "").strip()
    return {
        "qbo_id":             qbo_id,
        "date":               hdr["date"],
        "fiscal_year":        hdr["fy"],
        "fiscal_month_index": hdr["fm"],
        "gl_code":            gl_code,
        "gl_name":            gl_name,
        "category":           category,
        "amount":             amount,
        "vendor":             vendor,
        "memo":               line_desc or hdr["memo"] or None,
        "class":              class_name,
        "source":             "QBO",
    }


def _parse_ap_txn(txn, kind, vendor_key, sign):
    """Purchase / Bill / VendorCredit → expense records.

    Negative lines are kept: a negative line on an expense form is a credit
    to that account. qbo_id keeps the original `{kind}_{txnId}_{lineIdx}`
    shape so existing Purchase rows upsert in place.
    """
    hdr = _txn_header(txn)
    if not hdr:
        return []
    vendor = (txn.get(vendor_key) or {}).get("name", "Unknown")
    records = []
    for idx, line in enumerate(txn.get("Line", [])):
        detail = line.get("AccountBasedExpenseLineDetail") or {}
        if detail:
            account_ref = detail.get("AccountRef") or {}
        else:
            # Item-based line: the item's expense account takes the posting.
            detail = line.get("ItemBasedExpenseLineDetail") or {}
            item_id = (detail.get("ItemRef") or {}).get("value")
            account_ref = (ITEM_MAP.get(item_id) or {}).get("expense") or {}
        if not account_ref:
            continue
        rec = _expense_record(
            f"{kind}_{hdr['id']}_{idx}", hdr, account_ref,
            sign * float(line.get("Amount", 0) or 0), vendor, line,
            resolve_class(detail, txn))
        if rec:
            records.append(rec)
    return records


def parse_purchase(txn):
    # Credit=true is a credit-card refund: money back, so the lines reduce expense.
    sign = -1 if txn.get("Credit") else 1
    return _parse_ap_txn(txn, "Purchase", "EntityRef", sign)


def parse_bill(txn):
    return _parse_ap_txn(txn, "Bill", "VendorRef", 1)


def parse_vendor_credit(txn):
    return _parse_ap_txn(txn, "VendorCredit", "VendorRef", -1)


def parse_journal_entry(txn):
    """JournalEntry → one record per line that posts to an expense account.

    Debits are positive, credits negative. Credits MUST be kept: reclasses
    out of an expense account and accrual reversals are credit lines, and
    dropping them would overstate the account. Lines hitting non-expense
    accounts (the balance-sheet side of the entry) fall out in
    _expense_record via the account-type check.
    """
    hdr = _txn_header(txn)
    if not hdr:
        return []
    doc = (txn.get("DocNumber") or "").strip()
    records = []
    for idx, line in enumerate(txn.get("Line", [])):
        detail = line.get("JournalEntryLineDetail") or {}
        if not detail:
            continue
        sign = -1 if detail.get("PostingType") == "Credit" else 1
        entity = (detail.get("Entity") or {}).get("EntityRef") or {}
        vendor = entity.get("name") or (f"Journal Entry {doc}" if doc else "Journal Entry")
        rec = _expense_record(
            f"je_{hdr['id']}_{line.get('Id') or idx}", hdr,
            detail.get("AccountRef") or {},
            sign * float(line.get("Amount", 0) or 0), vendor, line,
            resolve_class(detail, txn))
        if rec:
            records.append(rec)
    return records


def parse_deposit(txn):
    """Deposit → records for lines that post to an expense account.

    A deposit line CREDITS its account, so the expense sign is flipped:
    a negative line (processor fee netted out of a deposit) is a positive
    expense; a positive line (refund, stop-payment credit) reduces expense.
    Lines that only pull payments out of Undeposited Funds have no AccountRef
    and are skipped.
    """
    hdr = _txn_header(txn)
    if not hdr:
        return []
    records = []
    for idx, line in enumerate(txn.get("Line", [])):
        detail = line.get("DepositLineDetail") or {}
        account_ref = detail.get("AccountRef") or {}
        if not account_ref:
            continue
        entity = detail.get("Entity") or {}
        vendor = entity.get("name") or (entity.get("EntityRef") or {}).get("name") or "Deposit"
        rec = _expense_record(
            f"dep_{hdr['id']}_{line.get('Id') or idx}", hdr, account_ref,
            -float(line.get("Amount", 0) or 0), vendor, line,
            resolve_class(detail, txn))
        if rec:
            records.append(rec)
    return records


def _parse_sales_txn(txn, prefix, sign):
    """Sales forms → records for item lines whose item posts to an EXPENSE
    account (income-account lines fall out on the account-type check).

    A sales line credits the item's account, so on Invoice/SalesReceipt a
    negative fee line is a positive expense (sign=-1); CreditMemo and
    RefundReceipt reverse that (sign=+1).
    """
    hdr = _txn_header(txn)
    if not hdr:
        return []
    customer = (txn.get("CustomerRef") or {}).get("name", "Unknown")
    records = []
    for idx, line in enumerate(txn.get("Line", [])):
        detail = line.get("SalesItemLineDetail") or {}
        item_id = (detail.get("ItemRef") or {}).get("value")
        account_ref = (ITEM_MAP.get(item_id) or {}).get("income") or {}
        if not account_ref:
            continue
        rec = _expense_record(
            f"{prefix}_{hdr['id']}_{line.get('Id') or idx}", hdr, account_ref,
            sign * float(line.get("Amount", 0) or 0), customer, line,
            resolve_class(detail, txn))
        if rec:
            records.append(rec)
    return records


# entity → parser. Order is display-only.
ENTITY_PARSERS = [
    ("Purchase",     parse_purchase),
    ("Bill",         parse_bill),
    ("VendorCredit", parse_vendor_credit),
    ("JournalEntry", parse_journal_entry),
    ("Deposit",      parse_deposit),
    ("Invoice",       lambda t: _parse_sales_txn(t, "inv", -1)),
    ("SalesReceipt",  lambda t: _parse_sales_txn(t, "sr",  -1)),
    ("CreditMemo",    lambda t: _parse_sales_txn(t, "cm",   1)),
    ("RefundReceipt", lambda t: _parse_sales_txn(t, "rr",   1)),
]


def fetch_and_parse_all():
    """Pull every entity and parse to expense records. Raises on any failure."""
    global ITEM_MAP, CLASS_MAP
    ITEM_MAP = fetch_item_map()
    CLASS_MAP = fetch_class_map()
    print(f"  → {len(ITEM_MAP)} items, {len(CLASS_MAP)} classes mapped")
    all_records = []
    for entity, parser in ENTITY_PARSERS:
        txns = _fetch_entity(entity)
        recs = []
        for txn in txns:
            recs.extend(parser(txn))
        print(f"    → {len(recs)} expense lines from {entity}")
        all_records.extend(recs)
    # Belt and braces: a qbo_id may appear only once per upsert batch.
    seen, unique = set(), []
    for r in all_records:
        if r["qbo_id"] in seen:
            print(f"  ⚠ duplicate qbo_id skipped: {r['qbo_id']}")
            continue
        seen.add(r["qbo_id"])
        unique.append(r)
    return unique


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


# ── Stale-row deletion (mirror semantics) ──────────────────────────────────
def _sb_headers(extra=None):
    h = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    h.update(extra or {})
    return h


def fetch_existing_ids(table, id_col, filters):
    """All ids currently in a Supabase table within `filters` (PostgREST params)."""
    ids, offset, page = [], 0, 1000
    while True:
        params = {"select": id_col, id_col: "not.is.null", "order": id_col,
                  "limit": page, "offset": offset}
        params.update(filters)
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_sb_headers(),
                         params=params, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"could not list existing {table} ids: {r.status_code} {r.text[:200]}")
        batch = r.json()
        ids.extend(row[id_col] for row in batch)
        if len(batch) < page:
            break
        offset += page
    return ids


def delete_stale(table, id_col, fetched_ids, filters, dry_run=False):
    """Delete rows in `table` (within `filters`) that the source no longer returns.

    This is what makes a feed a mirror: deleted transactions, removed lines
    and accounts that dropped off a report all self-heal. Capped: if more
    rows than the threshold would go, nothing is deleted and the run fails
    loudly — that pattern means a bad pull, not bookkeeping.
    """
    existing = fetch_existing_ids(table, id_col, filters)
    stale = sorted(set(existing) - set(fetched_ids))
    if not stale:
        print("  ✓ No stale rows.")
        return 0
    cap = int(os.environ.get("WOODS_MAX_STALE_DELETE") or max(50, len(existing) // 50))
    print(f"  {len(stale)} stale {table} row(s) no longer in QBO (cap {cap}): "
          f"{stale[:10]}{' …' if len(stale) > 10 else ''}")
    if len(stale) > cap:
        raise RuntimeError(
            f"{len(stale)} stale rows exceeds the safety cap of {cap} — nothing deleted. "
            "If this is a real cleanup, re-run with WOODS_MAX_STALE_DELETE set higher.")
    if dry_run:
        print("  (dry run — not deleting)")
        return 0
    deleted = 0
    for i in range(0, len(stale), 100):
        chunk = stale[i:i + 100]
        params = {id_col: f"in.({','.join(chunk)})"}
        params.update(filters)
        r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}",
                            headers=_sb_headers({"Prefer": "return=minimal"}),
                            params=params, timeout=30)
        if r.status_code not in (200, 204):
            raise RuntimeError(f"stale-row delete failed: {r.status_code} {r.text[:200]}")
        deleted += len(chunk)
    print(f"  ✓ Deleted {deleted} stale row(s).")
    return deleted


def delete_stale_rows(fetched_ids, dry_run=False):
    """Expenses feed: QBO-sourced rows inside the sync window."""
    return delete_stale("expenses", "qbo_id", fetched_ids,
                        {"source": "eq.QBO", "date": f"gte.{START_DATE}"}, dry_run)


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    """Returns a process exit code — non-zero on any failure so the workflow
    heartbeat records `error` instead of a false `ok`."""
    print("=" * 60)
    print("The Woods — QBO Expense Connector")
    print("=" * 60)
    dry_run = is_dry_run()
    if not start_session():
        return 1
    print(f"Date range: {START_DATE} to present{'   [DRY RUN]' if dry_run else ''}\n")

    try:
        all_records = fetch_and_parse_all()
    except QBOFetchError as e:
        print(f"\n✗ QBO fetch failed — nothing written, nothing deleted: {e}")
        return 1
    print(f"\n  → {len(all_records)} expense line items parsed")

    if not all_records:
        print("\n  ✗ No expense records found — treating as a failed pull.")
        return 1

    from collections import Counter
    fy_counts = Counter(r["fiscal_year"] for r in all_records)
    print(f"Records by fiscal year: {dict(sorted(fy_counts.items()))}")
    no_code = sum(1 for r in all_records if not r["gl_code"])
    if no_code:
        print(f"⚠ {no_code} records have NO gl_code (expense account without AcctNum in QBO)")

    if dry_run:
        print("\nDRY RUN → no upsert.")
        try:
            delete_stale_rows([r["qbo_id"] for r in all_records], dry_run=True)
        except RuntimeError as e:
            print(f"  ⚠ {e}")
        return 0

    if os.environ.get("CI", "").lower() == "true":
        print("\nCI=true → auto-confirming upsert.")
    else:
        confirm = input("\nProceed with upsert to Supabase? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted. No data was written.")
            return 0

    print("\nUpserting to Supabase...")
    inserted = upsert_to_supabase(all_records)
    if inserted != len(all_records):
        print(f"\n✗ Only {inserted}/{len(all_records)} records upserted — skipping stale-row deletion.")
        return 1

    print("\nChecking for stale rows...")
    try:
        delete_stale_rows([r["qbo_id"] for r in all_records])
    except RuntimeError as e:
        print(f"  ✗ {e}")
        return 1

    print(f"\n✓ Woods QBO connector complete. {inserted}/{len(all_records)} records loaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
