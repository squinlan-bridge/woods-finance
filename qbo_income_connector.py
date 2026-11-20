"""
The Woods — QBO Income Connector
================================
Pulls monthly income actuals from The Woods' QuickBooks Online ProfitAndLoss
report (accrual) and mirrors them into the Woods Supabase table
`income_monthly` — one row per income account per calendar month. Same
schema and grain as Bridge's income_monthly, so the combined-statement views
can union the two projects with no column mapping.

Why the Reports API and not transaction entities: income posts through many
entity types (Invoices, SalesReceipts, Deposits, JournalEntries). The P&L
report IS the P&L, so this feed ties to the camp's financials by
construction. Each pull is also checked against the report's own
"Total Income" line and fails rather than store numbers that don't foot.

The in-progress month is included as month-to-date.

The Woods chart of accounts has income accounts with no AcctNum, so expect
gl_code to be blank on some rows — gl_name is always populated.

Shared plumbing (tokens, QBO calls, upsert, stale-row deletion) lives in
qbo_connector.py.

Usage:
    python3 qbo_income_connector.py          (WOODS_DRY_RUN=1 to write nothing)
"""

import sys
from collections import defaultdict
from datetime import date

import qbo_connector as q

TABLE, ID_COL, SOURCE = "income_monthly", "income_id", "QBO_PL_REPORT"
INCOME_GROUPS = {"Income", "OtherIncome"}


def _collect(coldata, cols, out):
    acct_id = coldata[0].get("id") if coldata else None
    if not acct_id:
        return  # summary/total line, not an account
    values = q.report_values(coldata, cols)
    if not values:
        return  # parent header with no postings of its own
    acct = q.ACCOUNT_MAP.get(acct_id) or {}
    for (cy, cm), amount in values.items():
        d = date(cy, cm, 1)
        key = f"acct{acct_id}_{cy}_{cm:02d}"
        out[key] = {
            "income_id":          key,
            "fiscal_year":        q.fiscal_year_label(d),
            "gl_code":            acct.get("code", ""),
            "gl_name":            acct.get("full_name") or coldata[0].get("value", ""),
            "calendar_year":      cy,
            "calendar_month":     cm,
            "fiscal_month_index": q.fiscal_month_index(d),
            "amount":             round(amount, 2),
            "source":             SOURCE,
        }


def walk(rows, cols, out, totals, in_income=False):
    """Collect per-account monthly amounts under the income sections. QBO
    nests sub-accounts as sections; a parent account's own postings sit on
    the section Header. `totals` accumulates the report's own section totals
    for the footing check."""
    for row in rows.get("Row", []):
        is_income = in_income or row.get("group") in INCOME_GROUPS
        if not is_income:
            continue
        if row.get("group") in INCOME_GROUPS:
            for ym, v in q.report_values((row.get("Summary") or {}).get("ColData", []), cols).items():
                totals[ym] += v
        if row.get("type") == "Section" or row.get("Rows"):
            _collect((row.get("Header") or {}).get("ColData", []), cols, out)
            walk(row.get("Rows", {}), cols, out, totals, True)
        else:
            _collect(row.get("ColData", []), cols, out)


def build_records():
    records = {}
    today = date.today()
    for fy_start in q.fiscal_year_starts(today):
        fy_end = min(date(fy_start.year + (1 if fy_start.month >= q.FY_START_MONTH else 0), 5, 31), today)
        print(f"  Fetching P&L {fy_start} → {fy_end}...")
        report = q.qbo_report("ProfitAndLoss", start_date=fy_start.isoformat(),
                              end_date=fy_end.isoformat(), accounting_method="Accrual",
                              summarize_column_by="Month")
        cols = q.report_month_columns(report)
        fy_records, totals = {}, defaultdict(float)
        walk(report.get("Rows", {}), cols, fy_records, totals)

        # Footing check: account rows must sum to the report's Total Income.
        by_month = defaultdict(float)
        for r in fy_records.values():
            by_month[(r["calendar_year"], r["calendar_month"])] += r["amount"]
        for ym in sorted(set(by_month) | set(totals)):
            if abs(by_month[ym] - totals[ym]) > 0.01:
                raise q.QBOFetchError(
                    f"income rows for {ym[0]}-{ym[1]:02d} sum to {by_month[ym]:,.2f} but the "
                    f"report's Total Income is {totals[ym]:,.2f}")
        records.update(fy_records)
    return list(records.values())


def main():
    print("=" * 60)
    print("The Woods — QBO Income Connector (ProfitAndLoss report)")
    print("=" * 60)
    dry_run = q.is_dry_run()
    if not q.start_session():
        return 1
    try:
        records = build_records()
    except q.QBOFetchError as e:
        print(f"\n✗ Pull failed — nothing written, nothing deleted: {e}")
        return 1
    if not records:
        print("\n✗ No income rows parsed — treating as a failed pull.")
        return 1

    fy_tot = defaultdict(float)
    for r in records:
        fy_tot[r["fiscal_year"]] += r["amount"]
    print(f"\n  → {len(records)} account-month income rows; foots to the report's Total Income")
    for fy in sorted(fy_tot):
        print(f"    {fy}: ${fy_tot[fy]:,.2f}")
    unmapped = sorted({r["gl_name"] for r in records if not r["gl_code"]})
    if unmapped:
        print(f"  ⚠ {len(unmapped)} income account(s) with no AcctNum in QBO (gl_code blank)")

    ids = [r[ID_COL] for r in records]
    scope = {"source": f"eq.{SOURCE}"}
    try:
        if dry_run:
            print("\nDRY RUN → no upsert.")
            q.delete_stale(TABLE, ID_COL, ids, scope, dry_run=True)
            return 0
        print("\nUpserting to Supabase...")
        if q.upsert_to_supabase(records, table=TABLE, conflict=ID_COL) != len(records):
            print("✗ Upsert incomplete — skipping stale-row deletion.")
            return 1
        q.delete_stale(TABLE, ID_COL, ids, scope)
    except RuntimeError as e:
        print(f"  ✗ {e}")
        return 1
    print(f"\n✓ Woods income connector complete. {len(records)} rows loaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
