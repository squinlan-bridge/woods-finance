"""
The Woods — QBO Budget Connector
================================
Pulls budget records from The Woods' QBO Budget module and upserts into the
Woods Supabase `budget` table.

Schema produced: budget_id, fiscal_year, gl_code, gl_name, category, year,
period, budget. budget_id = "<fiscal_year>_<gl_code>_<period>" → idempotent
upserts.

QBO API quirk (learned on Bridge): the Budget query only returns EXPENSE
BudgetDetail entries for ProfitAndLoss budgets. Income budget lines are pulled
separately via the BudgetOverview Reports endpoint. Income rows are ingested
too (harmless — the dashboard filters to expense-type GLs at read time via
gl_accounts).

Same env vars + token storage as qbo_connector.py.
"""

import os
import requests
from datetime import date, datetime

from qbo_connector import (
    load_tokens,
    refresh_access_token,
    fetch_account_map,
    fiscal_year_label,
    upsert_to_supabase,
    QBO_BASE,
    SUPABASE_URL,
    SUPABASE_KEY,
    CLIENT_ID,
    CLIENT_SECRET,
)


# ── QBO Budget API ─────────────────────────────────────────────────────────
def fetch_all_budgets(access_token, company_id):
    """Fetch every Budget object (each contains a BudgetDetail array)."""
    url = f"{QBO_BASE}/v3/company/{company_id}/query"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params = {"query": "SELECT * FROM Budget", "minorversion": "65"}
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 401:
        return None, "unauthorized"
    if response.status_code != 200:
        print(f"  ✗ Budget query failed: {response.status_code}")
        print(f"    {response.text[:500]}")
        return None, "error"
    return response.json().get("QueryResponse", {}).get("Budget", []), "ok"


def fetch_budget_overview(access_token, company_id, start_date, end_date):
    """BudgetOverview report — the only API surface that includes income budget lines."""
    url = f"{QBO_BASE}/v3/company/{company_id}/reports/BudgetOverview"
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    params = {
        "start_date":          start_date,
        "end_date":            end_date,
        "summarize_column_by": "Month",
        "minorversion":        "65",
    }
    response = requests.get(url, headers=headers, params=params)
    if response.status_code == 401:
        return None, "unauthorized"
    if response.status_code != 200:
        print(f"  ✗ BudgetOverview {start_date}→{end_date} failed: {response.status_code}")
        print(f"    {response.text[:300]}")
        return None, "error"
    return response.json(), "ok"


# ── Parsers ────────────────────────────────────────────────────────────────
def _acct_lookup_by_name(account_map):
    """FullyQualifiedName (and leaf name) → map entry, for report rows that
    only carry a display name (BudgetOverview has no AccountRef ids)."""
    by_name = {}
    for m in account_map.values():
        if m["full_name"]:
            by_name[m["full_name"].casefold()] = m
            by_name[m["full_name"].split(":")[-1].casefold()] = m
    return by_name


def parse_budget(budget_obj, account_map):
    """One QBO Budget object → budget rows, one per (GL account × month)."""
    records = []
    name = budget_obj.get("Name", "Unnamed budget")
    dropped = set()

    for detail in budget_obj.get("BudgetDetail", []):
        date_str = detail.get("BudgetDate", "")
        if not date_str:
            continue
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            continue
        # Skip class/location-split lines; aggregate handling would be needed
        # if The Woods ever budgets by class.
        if detail.get("Subdivision"):
            continue

        acct_ref  = detail.get("AccountRef", {})
        full_name = acct_ref.get("name", "")
        acct      = account_map.get(acct_ref.get("value", ""))

        gl_code, gl_name, category = "", full_name, "Uncategorized"
        if acct and acct["code"]:
            gl_code, gl_name, category = acct["code"], acct["full_name"] or full_name, acct["category"]
        if not gl_code:
            dropped.add(full_name or acct_ref.get("value", "?"))
            continue

        amount = float(detail.get("Amount", 0) or 0)
        if amount == 0:
            continue  # uninitialized/blank budget cells just clutter the data

        fy = fiscal_year_label(d)
        records.append({
            "budget_id":   f"{fy}_{gl_code}_{d.month}",
            "fiscal_year": fy,
            "gl_code":     gl_code,
            "gl_name":     gl_name,
            "category":    category,
            "year":        d.year,
            "period":      d.month,
            "budget":      round(amount, 2),
        })

    print(f"    '{name}': parsed {len(records)} rows")
    if dropped:
        print(f"    ⚠ '{name}': DROPPED {len(dropped)} account(s) with no resolvable GL code:")
        for n in sorted(dropped):
            print(f"        - {n}")
    return records


def parse_overview_income(report, account_map):
    """Walk the BudgetOverview report and pull Income-section rows."""
    records = []
    by_name = _acct_lookup_by_name(account_map)
    cols = report.get("Columns", {}).get("Column", [])
    month_for_col = {}
    for i, col in enumerate(cols):
        title = col.get("ColTitle", "")
        if col.get("ColType") != "Money" or title.upper() == "TOTAL":
            continue
        try:
            dt = datetime.strptime(title, "%b %Y")
            month_for_col[i] = (dt.year, dt.month)
        except ValueError:
            continue

    def walk(rows, in_income):
        for row in rows:
            header = row.get("Header", {})
            section_label = ""
            if header:
                hdr_cols = header.get("ColData", [])
                if hdr_cols:
                    section_label = (hdr_cols[0].get("value", "") or "").strip()
            nested = row.get("Rows", {}).get("Row", [])
            child_in_income = in_income or section_label.lower() == "income"
            if nested:
                walk(nested, child_in_income)
                continue
            if not in_income:
                continue

            col_data = row.get("ColData", [])
            if not col_data:
                continue
            account_name = col_data[0].get("value", "")
            # Resolve via the account map by name; fall back to numeric prefix.
            acct = by_name.get(account_name.casefold())
            if not acct:
                # Names in the report sometimes carry the "NNNNN " code prefix.
                import re as _re
                m = _re.match(r"^(\d{4,6})\s+(.*)$", account_name)
                if m:
                    acct = by_name.get(m.group(2).casefold())
            if not acct or not acct["code"]:
                continue
            gl_code, gl_name, category = acct["code"], acct["full_name"], acct["category"] or "Income"

            for col_idx, cell in enumerate(col_data):
                if col_idx == 0 or col_idx not in month_for_col:
                    continue
                if not cell.get("value"):
                    continue
                try:
                    amount = float(cell["value"])
                except ValueError:
                    continue
                if amount == 0:
                    continue
                cal_year, cal_month = month_for_col[col_idx]
                fy = fiscal_year_label(date(cal_year, cal_month, 1))
                records.append({
                    "budget_id":   f"{fy}_{gl_code}_{cal_month}",
                    "fiscal_year": fy,
                    "gl_code":     gl_code,
                    "gl_name":     gl_name,
                    "category":    category,
                    "year":        cal_year,
                    "period":      cal_month,
                    "budget":      round(amount, 2),
                })

    walk(report.get("Rows", {}).get("Row", []), in_income=False)
    return records


# ── Main ───────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("The Woods — QBO Budget Connector")
    print("=" * 60)

    missing = []
    if not SUPABASE_URL: missing.append("SUPABASE_URL")
    if not SUPABASE_KEY: missing.append("SUPABASE_SERVICE_KEY")
    if not CLIENT_ID:    missing.append("QBO_CLIENT_ID")
    if not CLIENT_SECRET:missing.append("QBO_CLIENT_SECRET")
    if missing:
        print(f"\n✗ Missing env vars: {', '.join(missing)}")
        return

    tokens = load_tokens()
    if not tokens:
        print("✗ Could not load QBO tokens. Run qbo_auth.py first.")
        return
    company_id    = tokens.get("company_id") or os.environ.get("QBO_COMPANY_ID", "")
    refresh_token = tokens["refresh_token"]
    access_token  = refresh_access_token(refresh_token)
    if not access_token:
        return

    print("\nFetching budgets from QBO...")
    budgets, status = fetch_all_budgets(access_token, company_id)
    if status == "unauthorized":
        access_token = refresh_access_token(refresh_token)
        budgets, status = fetch_all_budgets(access_token, company_id)
    if status != "ok" or budgets is None:
        print("✗ Could not fetch budgets.")
        return
    print(f"  Found {len(budgets)} budget(s) in QBO.")
    if not budgets:
        print("  Nothing to do.")
        return

    print("\nFetching chart of accounts for GL-code mapping...")
    account_map, status = fetch_account_map(access_token, company_id)
    if status == "unauthorized":
        access_token = refresh_access_token(refresh_token)
        account_map, status = fetch_account_map(access_token, company_id)
    print(f"  Mapped {len(account_map)} accounts "
          f"({sum(1 for v in account_map.values() if v['code'])} with AcctNum).")

    print("\nParsing expense lines from Budget query...")
    all_records = []
    for b in budgets:
        all_records.extend(parse_budget(b, account_map))

    print("\nFetching income via BudgetOverview report (one call per date range)...")
    seen_ranges = set()
    for b in budgets:
        start, end = b.get("StartDate"), b.get("EndDate")
        if not start or not end or (start, end) in seen_ranges:
            continue
        seen_ranges.add((start, end))
        report, status = fetch_budget_overview(access_token, company_id, start, end)
        if status == "unauthorized":
            access_token = refresh_access_token(refresh_token)
            report, status = fetch_budget_overview(access_token, company_id, start, end)
        if status != "ok" or report is None:
            print(f"  ⚠ Skipping {start}→{end} (couldn't load report)")
            continue
        income_records = parse_overview_income(report, account_map)
        print(f"  '{b.get('Name','?')}': {len(income_records)} income rows")
        all_records.extend(income_records)

    if not all_records:
        print("✗ No usable budget rows parsed. Check that BudgetDetail entries exist.")
        return

    from collections import Counter
    fy_counts = Counter(r["fiscal_year"] for r in all_records)
    print(f"\nRecords by fiscal year: {dict(sorted(fy_counts.items()))}")
    print(f"Total budget rows:      {len(all_records)}")

    if os.environ.get("CI", "").lower() == "true":
        print("\nCI=true → auto-confirming upsert.")
    else:
        confirm = input("\nProceed with upsert to Supabase? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted. No data was written.")
            return

    print("\nUpserting to Supabase...")
    inserted = upsert_to_supabase(all_records, table="budget", conflict="budget_id")
    print(f"\n✓ Woods budget connector complete. {inserted}/{len(all_records)} records loaded.")


if __name__ == "__main__":
    main()
