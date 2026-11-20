"""
The Woods — QBO Balance Sheet Connector
=======================================
Pulls month-end balances by GL account from The Woods' QuickBooks Online
BalanceSheet report (accrual) and mirrors them into the Woods Supabase table
`balance_monthly`. Same schema and grain as Bridge's balance_monthly so the
combined-statement views can union the two projects directly.

One row per account per COMPLETED month, holding the month-end balance.
Monthly activity is a delta (this month minus last) computed in the
consuming view, so the balance sheet and the movements behind the cash-flow
statement both come from this one feed. The in-progress month is never
stored; a month loads on the 1st of the following month.

Each pull is checked against the report's own totals (Total Assets, Total
Liabilities, Total Equity) and fails rather than store balances that don't
foot. Note QBO's current-year "Net Income" equity line is computed, not an
account, so it is NOT stored: Equity rows sum to Total Equity minus
year-to-date net income.

Woods-specific: the camp carries restricted-fund equity sub-accounts (Dalton
Grant projects) plus deferred camp/scholarship revenue — exactly the detail
the combination work needs. Do not collapse it here.

Shared plumbing lives in qbo_connector.py.

Usage:
    python3 qbo_balance_connector.py         (WOODS_DRY_RUN=1 to write nothing)
"""

import sys
from collections import defaultdict
from datetime import date, timedelta

import qbo_connector as q

TABLE, ID_COL, SOURCE = "balance_monthly", "balance_id", "QBO_BS_REPORT"
SECTION_OF_GROUP = {"TotalAssets": "Assets", "Liabilities": "Liabilities", "Equity": "Equity"}


def _collect(coldata, cols, out, section):
    acct_id = coldata[0].get("id") if coldata else None
    if not acct_id:
        return
    values = q.report_values(coldata, cols)
    if not values:
        return
    acct = q.ACCOUNT_MAP.get(acct_id) or {}
    for (cy, cm), balance in values.items():
        d = date(cy, cm, 1)
        key = f"acct{acct_id}_{cy}_{cm:02d}"
        out[key] = {
            "balance_id":         key,
            "fiscal_year":        q.fiscal_year_label(d),
            "gl_code":            acct.get("code", ""),
            "gl_name":            acct.get("full_name") or coldata[0].get("value", ""),
            "section":            section,
            "calendar_year":      cy,
            "calendar_month":     cm,
            "fiscal_month_index": q.fiscal_month_index(d),
            "balance":            round(balance, 2),
            "source":             SOURCE,
        }


def walk(rows, cols, out, totals, net_income, section=None):
    """Collect per-account month-end balances, tracking Assets / Liabilities /
    Equity. `totals[section]` gets the report's own section totals and
    `net_income` the computed Net Income line, for the footing check."""
    for row in rows.get("Row", []):
        group = row.get("group") or ""
        sec = SECTION_OF_GROUP.get(group, section)
        if group in SECTION_OF_GROUP:
            for ym, v in q.report_values((row.get("Summary") or {}).get("ColData", []), cols).items():
                totals[sec][ym] += v
        if group == "NetIncome":
            for ym, v in q.report_values(row.get("ColData", []), cols).items():
                net_income[ym] += v
            continue
        if row.get("type") == "Section" or row.get("Rows"):
            if sec:
                _collect((row.get("Header") or {}).get("ColData", []), cols, out, sec)
            walk(row.get("Rows", {}), cols, out, totals, net_income, sec)
        elif sec:
            _collect(row.get("ColData", []), cols, out, sec)


def build_records():
    records = {}
    last_closed = date.today().replace(day=1) - timedelta(days=1)   # prior month-end
    for fy_start in q.fiscal_year_starts(last_closed):
        fy_end = min(date(fy_start.year + (1 if fy_start.month >= q.FY_START_MONTH else 0), 5, 31), last_closed)
        print(f"  Fetching BalanceSheet {fy_start} → {fy_end}...")
        report = q.qbo_report("BalanceSheet", start_date=fy_start.isoformat(),
                              end_date=fy_end.isoformat(), accounting_method="Accrual",
                              summarize_column_by="Month")
        cols = q.report_month_columns(report, complete_only=True)
        fy_records, net_income = {}, defaultdict(float)
        totals = {s: defaultdict(float) for s in SECTION_OF_GROUP.values()}
        walk(report.get("Rows", {}), cols, fy_records, totals, net_income)

        # Footing check: account rows must sum to the report's section totals
        # (Equity excludes the computed Net Income line).
        sums = {s: defaultdict(float) for s in totals}
        for r in fy_records.values():
            sums[r["section"]][(r["calendar_year"], r["calendar_month"])] += r["balance"]
        for sec in totals:
            for ym in sorted(set(sums[sec]) | set(totals[sec])):
                expected = totals[sec][ym] - (net_income[ym] if sec == "Equity" else 0)
                if abs(sums[sec][ym] - expected) > 0.01:
                    raise q.QBOFetchError(
                        f"{sec} rows for {ym[0]}-{ym[1]:02d} sum to {sums[sec][ym]:,.2f} but the "
                        f"report shows {expected:,.2f}")
        for ym in totals["Assets"]:
            if abs(totals["Assets"][ym] - totals["Liabilities"][ym] - totals["Equity"][ym]) > 0.01:
                raise q.QBOFetchError(f"balance sheet for {ym[0]}-{ym[1]:02d} does not balance")
        records.update(fy_records)
    return list(records.values())


def main():
    print("=" * 60)
    print("The Woods — QBO Balance Sheet Connector (BalanceSheet report)")
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
        print("\n✗ No balance rows parsed — treating as a failed pull.")
        return 1

    latest = max((r["calendar_year"], r["calendar_month"]) for r in records)
    sec_tot = defaultdict(float)
    for r in records:
        if (r["calendar_year"], r["calendar_month"]) == latest:
            sec_tot[r["section"]] += r["balance"]
    months = len({(r["calendar_year"], r["calendar_month"]) for r in records})
    print(f"\n  → {len(records)} account-month balance rows across {months} month-ends; "
          "every month foots to the report")
    print(f"  As of {latest[0]}-{latest[1]:02d}: " + " | ".join(
        f"{s} ${sec_tot.get(s, 0):,.2f}" for s in ("Assets", "Liabilities", "Equity"))
        + "  (Equity excludes YTD net income)")

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
    print(f"\n✓ Woods balance connector complete. {len(records)} rows loaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
