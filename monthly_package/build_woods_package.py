"""
The Woods — monthly financial package builder
=============================================
Rebuilds the monthly Woods financial package (previously seven hand-exported
QBO report tabs) straight from the QBO Reports API:

    1. Budget vs. Actuals        monthly, FY to date
    2. Profit & Loss             monthly with prior-year comparison
    3. Balance Sheet             month-ends with prior-period comparison
    4. Cash Flows                monthly statement of cash flows
    5. YTD Actuals v Budgets     12-month actuals against the annual budget
    6. <Mon><yy> P&L by Class    the period month
    7. YTD P&L by Class          FY to date
    8. Checks                    every statement total tied back to QBO

The statements are QBO report output, not rebuilt from transaction rows, so
they tie to the books by construction. Amounts are written as values;
every subtotal, variance and total is a live formula, and the Checks tab
compares the workbook's own totals with the totals QBO reported.

Budget comes from the Woods Supabase `budget` table (synced daily from the
QBO Budget module) because the Reports API has no Budget-vs-Actuals report.

Usage:
    python3 monthly_package/build_woods_package.py --year 2026 --month 10 --out pkg.xlsx
`--year/--month` are the CALENDAR year and month of the completed period.
Writes <out> and <out>.checks.json (expected values for the post-recalc
verification in package_job.py).
"""

import argparse
import calendar
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, Side
from openpyxl.utils import get_column_letter as L

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import qbo_connector as q  # noqa: E402

MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["", "January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
ENTITY = "The Woods"
FIRST_ROW = 7          # first body row on every statement tab
TOL = 0.01

# P&L sections in statement order: (heading, QBO AccountType, report group)
PL_SECTIONS = [
    ("Income",             "Income",             "Income"),
    ("Cost of Goods Sold", "Cost of Goods Sold", "COGS"),
    ("Expenses",           "Expense",            "Expenses"),
    ("Other Income",       "Other Income",       "OtherIncome"),
    ("Other Expenses",     "Other Expense",      "OtherExpenses"),
]

# ── Styles (Arial throughout, matching the QBO exports) ────────────────────
F_BODY, F_BOLD = Font(name="Arial", size=8), Font(name="Arial", size=8, bold=True)
F_HEAD = Font(name="Arial", size=9, bold=True)
F_T1, F_T3 = Font(name="Arial", size=14, bold=True), Font(name="Arial", size=10, bold=True)
F_NOTE = Font(name="Arial", size=8, italic=True, color="555555")
THIN = Side(style="thin")
NUM, PCT = "#,##0.00", "0.00%"


class BuildError(Exception):
    pass


# ── Period helpers ─────────────────────────────────────────────────────────
def add_months(ym, n):
    y, m = ym
    i = y * 12 + (m - 1) + n
    return (i // 12, i % 12 + 1)


def month_end(ym):
    return date(ym[0], ym[1], calendar.monthrange(*ym)[1])


def mlabel(ym):
    return f"{MONTHS[ym[1]]} {ym[0]}"


class Period:
    def __init__(self, year, month):
        self.ym = (year, month)
        fy_start_year = year if month >= q.FY_START_MONTH else year - 1
        self.fy_start = (fy_start_year, q.FY_START_MONTH)
        self.fy_months = [add_months(self.fy_start, i) for i in range(12)]
        self.months = [m for m in self.fy_months if m <= self.ym]      # elapsed
        self.fy_label = q.fiscal_year_label(date(year, month, 1))
        a, b = self.months[0], self.ym
        self.range_label = (f"{MONTHS_LONG[b[1]]} {b[0]}" if a == b else
                            f"{MONTHS_LONG[a[1]]} - {MONTHS_LONG[b[1]]}, {b[0]}" if a[0] == b[0] else
                            f"{MONTHS_LONG[a[1]]} {a[0]} - {MONTHS_LONG[b[1]]} {b[0]}")


# ── Report parsing ─────────────────────────────────────────────────────────
def parse_account_report(report, cols):
    """(values, summaries) from a P&L-shaped report.

    values[acct_id][col_key]   amounts posted to each account (a parent's own
                               postings sit on its section Header)
    summaries[group][col_key]  QBO's own totals (Income, Expenses, NetIncome…)
    """
    values, summaries = defaultdict(dict), defaultdict(dict)

    def take(coldata):
        if coldata and coldata[0].get("id"):
            for key, v in q.report_values(coldata, cols).items():
                values[coldata[0]["id"]][key] = round(values[coldata[0]["id"]].get(key, 0) + v, 2)

    def walk(rows):
        for row in rows.get("Row", []):
            take((row.get("Header") or {}).get("ColData"))
            take(row.get("ColData"))
            if row.get("group") and row.get("Summary"):
                summaries[row["group"]].update(q.report_values(row["Summary"]["ColData"], cols))
            if row.get("Rows"):
                walk(row["Rows"])

    walk(report.get("Rows", {}))
    return values, summaries


def fetch_pl_months(start_ym, end_ym):
    rep = q.qbo_report("ProfitAndLoss", start_date=date(*start_ym, 1).isoformat(),
                       end_date=month_end(end_ym).isoformat(),
                       accounting_method="Accrual", summarize_column_by="Month")
    return parse_account_report(rep, q.report_month_columns(rep))


def fetch_pl_by_class(start_ym, end_ym):
    """P&L summarized by class. Returns (values, summaries, columns) where
    columns is the ordered list of class columns as QBO lays them out:
    {"idx", "title", "kind": data|group_total, "members": [idx…], "group": parent title}."""
    rep = q.qbo_report("ProfitAndLoss", start_date=date(*start_ym, 1).isoformat(),
                       end_date=month_end(end_ym).isoformat(),
                       accounting_method="Accrual", summarize_column_by="Classes")
    raw = rep["Columns"]["Column"]
    keys = [(c.get("MetaData") or [{}])[0].get("Value") for c in raw]
    columns, open_groups = [], {}
    for i, c in enumerate(raw):
        if i == 0 or keys[i] == "total":
            continue
        title = (c.get("ColTitle") or "").strip()
        if keys[i] in open_groups and title.lower().startswith("total "):
            g = open_groups.pop(keys[i])
            members = [x["idx"] for x in columns if x["idx"] >= g["idx"] and x["kind"] == "data"]
            for x in columns:
                if x["idx"] in members:
                    x["group"] = g["title"]
            columns.append({"idx": i, "title": f"Total for {g['title']}", "kind": "group_total",
                            "members": members, "group": g["title"]})
        else:
            col = {"idx": i, "title": title, "kind": "data", "group": None}
            columns.append(col)
            if keys.count(keys[i]) > 1:
                open_groups[keys[i]] = col
    cols = {x["idx"]: x["idx"] for x in columns if x["kind"] == "data"}
    values, summaries = parse_account_report(rep, cols)
    return values, summaries, columns


def fetch_budget(period):
    """{acct_id: {(year, month): amount}} for the period's fiscal year, from
    the Supabase budget table (keyed by GL code → resolved to account id)."""
    rows, offset = [], 0
    while True:
        r = requests.get(f"{q.SUPABASE_URL}/rest/v1/budget", headers=q._sb_headers(), timeout=30,
                         params={"select": "gl_code,gl_name,period,budget", "order": "id",
                                 "fiscal_year": f"eq.{period.fy_label}", "limit": 1000, "offset": offset})
        if r.status_code != 200:
            raise BuildError(f"budget read failed: {r.status_code} {r.text[:200]}")
        rows.extend(r.json())
        if len(r.json()) < 1000:
            break
        offset += 1000
    pl_types = {s[1] for s in PL_SECTIONS}
    by_code = {}
    for aid, a in q.ACCOUNT_MAP.items():
        if a["code"] and a["type"] in pl_types:
            # a deleted account can share a number with its live replacement
            if a["code"] not in by_code or "(deleted)" in q.ACCOUNT_MAP[by_code[a["code"]]]["full_name"]:
                by_code[a["code"]] = aid
    month_of = {m: (y, m) for (y, m) in period.fy_months}
    budget, unmapped = defaultdict(dict), defaultdict(float)
    for row in rows:
        aid = by_code.get((row["gl_code"] or "").strip())
        amt = float(row["budget"] or 0)
        if not aid:
            unmapped[f"{row['gl_code']} {row['gl_name']}"] += amt
            continue
        key = month_of[int(row["period"])]
        budget[aid][key] = round(budget[aid].get(key, 0) + amt, 2)
    return budget, {k: round(v, 2) for k, v in unmapped.items() if abs(v) >= TOL}


# ── Chart-of-accounts tree ─────────────────────────────────────────────────
def acct_label(aid):
    a = q.ACCOUNT_MAP[aid]
    leaf = a["full_name"].split(":")[-1]
    return f"{a['code']} {leaf}" if a["code"] else leaf


def pl_tree(present):
    """Per section, the account tree limited to `present` accounts and their
    ancestors, siblings in QBO's order (by displayed name, numbers first)."""
    keep = set()
    for aid in present:
        while aid and aid in q.ACCOUNT_MAP and aid not in keep:
            keep.add(aid)
            aid = q.ACCOUNT_MAP[aid]["parent_id"]
    kids = defaultdict(list)
    for aid in keep:
        parent = q.ACCOUNT_MAP[aid]["parent_id"]
        kids[parent if parent in keep else None].append(aid)
    for v in kids.values():
        v.sort(key=lambda a: acct_label(a).lower())
    return {heading: [a for a in kids[None] if q.ACCOUNT_MAP[a]["type"] == atype]
            for heading, atype, _ in PL_SECTIONS}, kids


# ── Sheet writer ───────────────────────────────────────────────────────────
class Sheet:
    """A statement tab: rows are laid out first (so totals know which rows
    they add up), then written column by column."""

    def __init__(self, wb, title, report_name, period_label, ncols):
        self.ws = wb.create_sheet(title)
        self.title, self.ncols = title, ncols
        self.rows = []          # dicts: label, depth, kind, acct, sum, expr
        ws = self.ws
        for r, (text, font) in enumerate([(ENTITY, F_T1), (report_name, F_T1), (period_label, F_T3)], 1):
            ws.cell(r, 1, text).font = font
            ws.cell(r, 1).alignment = Alignment(horizontal="center")
            ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=ncols)
        ws.column_dimensions["A"].width = 48
        ws.freeze_panes = ws.cell(FIRST_ROW, 2)
        ws.sheet_view.showGridLines = False
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = "1:6"

    # layout ---------------------------------------------------------------
    def add(self, label, depth, kind, acct=None, sum_rows=None, expr=None, own=False):
        self.rows.append({"label": label, "depth": depth, "kind": kind, "acct": acct,
                          "sum": sum_rows, "expr": expr, "own": own})
        return FIRST_ROW + len(self.rows) - 1

    def header(self, row, col, text, span=1):
        c = self.ws.cell(row, col, text)
        c.font, c.alignment = F_HEAD, Alignment(horizontal="center", wrap_text=True)
        for i in range(col, col + span):
            self.ws.cell(row, i).border = Border(bottom=THIN)
        if span > 1:
            self.ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col + span - 1)

    # writing --------------------------------------------------------------
    def write(self, columns):
        """columns: list of dicts, one per sheet column from B onward:
             {"data": fn(acct_id) -> value|None}      statement amounts
             {"row":  fn(r) -> formula, "fmt": PCT}   same-row formula
        Structural rows (totals, calculated lines) get a column-wise formula
        in data columns and the same-row formula elsewhere."""
        ws = self.ws
        for i, row in enumerate(self.rows):
            r = FIRST_ROW + i
            bold = row["kind"] in ("total", "calc")
            a = ws.cell(r, 1, row["label"])
            a.font = F_BOLD if bold else F_BODY
            a.alignment = Alignment(indent=row["depth"])
            for j, col in enumerate(columns):
                c = j + 2
                value = None
                if "data" in col:
                    if row["kind"] == "total":
                        value = "=" + "+".join(f"{L(c)}{x}" for x in row["sum"]) if row["sum"] else 0
                    elif row["kind"] == "calc":
                        value = "=" + row["expr"].replace("{c}", L(c))
                    elif row["acct"] is not None:
                        value = col["data"](row["acct"])
                elif row["kind"] in ("total", "calc", "leaf") or row["own"]:
                    value = col["row"](r)
                if value is None:
                    continue
                cell = ws.cell(r, c, value)
                cell.font = F_BOLD if bold else F_BODY
                cell.number_format = col.get("fmt", NUM)
                if bold:
                    cell.border = Border(top=THIN)
            if bold:
                a.border = Border(top=THIN)
        for j in range(len(columns)):
            ws.column_dimensions[L(j + 2)].width = columns[j].get("width", 13)

    def footer(self, text):
        r = FIRST_ROW + len(self.rows) + 2
        self.ws.cell(r, 1, text).font = F_NOTE

    def row_of(self, label):
        for i, row in enumerate(self.rows):
            if row["label"] == label:
                return FIRST_ROW + i
        raise KeyError(label)


def layout_pl(sheet, present, has_own):
    """Lay out a P&L: sections, account hierarchy, subtotals and the
    calculated lines. `has_own(acct)` says whether a parent account carries
    postings of its own (its heading row then shows values)."""
    roots, kids = pl_tree(present)
    totals = {}

    def node(aid, depth):
        children = kids.get(aid, [])
        if not children:
            return sheet.add(acct_label(aid), depth, "leaf", acct=aid)
        own = has_own(aid)
        head = sheet.add(acct_label(aid), depth, "header", acct=aid, own=own)
        parts = ([head] if own else []) + [node(c, depth + 1) for c in children]
        return sheet.add(f"Total for {acct_label(aid)}", depth, "total", sum_rows=parts)

    def section(heading):
        sheet.add(heading, 0, "section")
        parts = [node(a, 1) for a in roots[heading]]
        totals[heading] = sheet.add(f"Total {heading}", 0, "total", sum_rows=parts)

    section("Income")
    if roots["Cost of Goods Sold"]:
        section("Cost of Goods Sold")
        gp = sheet.add("Gross Profit", 0, "calc",
                       expr=f"{{c}}{totals['Income']}-{{c}}{totals['Cost of Goods Sold']}")
    else:
        gp = sheet.add("Gross Profit", 0, "calc", expr=f"{{c}}{totals['Income']}-0")
    section("Expenses")
    noi = sheet.add("Net Operating Income", 0, "calc", expr=f"{{c}}{gp}-{{c}}{totals['Expenses']}")
    if roots["Other Income"] or roots["Other Expenses"]:
        section("Other Income")
        section("Other Expenses")
        other = sheet.add("Net Other Income", 0, "calc",
                          expr=f"{{c}}{totals['Other Income']}-{{c}}{totals['Other Expenses']}")
        ni = sheet.add("Net Income", 0, "calc", expr=f"{{c}}{noi}+{{c}}{other}")
    else:
        ni = sheet.add("Net Income", 0, "calc", expr=f"{{c}}{noi}+0")
    return {"income": totals["Income"], "expenses": totals["Expenses"], "net_income": ni}


def layout_report(sheet, report, cols):
    """Lay out a report in QBO's own row order (Balance Sheet, Cash Flows).
    Returns values keyed by sheet row and {group: row} for the totals."""
    values, groups = {}, {}

    def walk(rows, depth):
        results = []
        for row in rows.get("Row", []):
            header = (row.get("Header") or {}).get("ColData")
            summary = (row.get("Summary") or {}).get("ColData")
            data = row.get("ColData")
            if header:
                own = q.report_values(header, cols)
                head = sheet.add(header[0].get("value", ""), depth, "header", acct=-1, own=bool(own))
                values[head] = own
                parts = ([head] if own else []) + walk(row.get("Rows", {}), depth + 1)
                total = sheet.add(summary[0].get("value", ""), depth, "total", sum_rows=parts) if summary else head
                results.append(total)
            elif data:
                r = sheet.add(data[0].get("value", ""), depth, "leaf", acct=-1)
                values[r] = q.report_values(data, cols)
                results.append(r)
                total = r
            elif summary:      # calculated line with no rows of its own
                total = sheet.add(summary[0].get("value", ""), depth, "total", sum_rows=list(results))
                results.append(total)
            else:
                continue
            if row.get("group"):
                groups[row["group"]] = total
        return results

    walk(report.get("Rows", {}), 0)
    for i, row in enumerate(sheet.rows):      # row-number lookup instead of account id
        if row["acct"] == -1:
            row["acct"] = FIRST_ROW + i
    return values, groups


def pct_change(cur, base):
    return lambda r: f'=IFERROR(({cur}{r}-{base}{r})/ABS({base}{r}),"")'


def rowsum(letters):
    return lambda r: ("=" + "+".join(f"{x}{r}" for x in letters)) if letters else None


# ── Tabs ───────────────────────────────────────────────────────────────────
def build(period):
    p = period
    stamp = datetime.now(timezone.utc).strftime("%A, %B %d, %Y %I:%M %p UTC")
    foot = f"Accrual Basis · QuickBooks Online realm {q.AUTH['company_id']} · built {stamp} by woods-finance"
    checks = []      # {"sheet", "cell", "expected", "label"}

    def expect(sheet, cell, expected, label):
        checks.append({"sheet": sheet.title, "cell": cell, "expected": round(expected, 2), "label": label})

    # pulls ----------------------------------------------------------------
    py_start = add_months(p.fy_start, -12)
    print(f"  P&L by month {mlabel(py_start)} → {mlabel(p.ym)} ...")
    act, act_sum = fetch_pl_months(py_start, p.ym)
    print(f"  Budget {p.fy_label} ...")
    bud, bud_unmapped = fetch_budget(p)
    print("  Balance Sheet ...")
    bs_start = add_months(p.fy_start, -1)
    bs_rep = q.qbo_report("BalanceSheet", start_date=date(*bs_start, 1).isoformat(),
                          end_date=month_end(p.ym).isoformat(),
                          accounting_method="Accrual", summarize_column_by="Month")
    print("  Cash Flows ...")
    cf_rep = q.qbo_report("CashFlow", start_date=date(*p.fy_start, 1).isoformat(),
                          end_date=month_end(p.ym).isoformat(), summarize_column_by="Month")
    print("  P&L by class (month, YTD) ...")
    cls_m = fetch_pl_by_class(p.ym, p.ym)
    cls_y = fetch_pl_by_class(p.fy_start, p.ym)

    def a(key):
        return lambda aid: act.get(aid, {}).get(key)

    def b(key):
        return lambda aid: bud.get(aid, {}).get(key)

    def own_in(keys, *sources):
        return lambda aid: any(k in s.get(aid, {}) for s in sources for k in keys)

    wb = Workbook()
    wb.remove(wb.active)
    py = {m: add_months(m, -12) for m in p.months}

    # 1 ─ Budget vs. Actuals ────────────────────────────────────────────────
    n = len(p.months)
    s1 = Sheet(wb, "Budget vs. Actuals", f"Budget vs. Actuals: {p.fy_label} Budget", p.range_label, 1 + 4 * (n + 1))
    present = {x for x in set(act) | set(bud) if own_in(p.months, act, bud)(x)}
    k1 = layout_pl(s1, present, own_in(p.months, act, bud))
    cols = []
    for i, m in enumerate(p.months + ["Total"]):
        c0 = 2 + 4 * i
        A, B = L(c0), L(c0 + 1)
        s1.header(5, c0, "Total" if m == "Total" else mlabel(m), 4)
        for k, t in enumerate(["Actual", "Budget", "over Budget", "% of Budget"]):
            s1.header(6, c0 + k, t)
        if m == "Total":
            cols += [{"row": rowsum([L(2 + 4 * j) for j in range(n)])},
                     {"row": rowsum([L(3 + 4 * j) for j in range(n)])}]
        else:
            cols += [{"data": a(m)}, {"data": b(m)}]
        cols += [{"row": lambda r, A=A, B=B: f"={A}{r}-{B}{r}"},
                 {"row": lambda r, A=A, B=B: f'=IF({B}{r}=0,"",{A}{r}/{B}{r})', "fmt": PCT, "width": 10}]
    s1.write(cols)
    s1.footer(foot)
    tot = L(2 + 4 * n)
    expect(s1, f"{tot}{k1['net_income']}", sum(act_sum["NetIncome"].get(m, 0) for m in p.months),
           f"Net Income, {p.range_label}")
    for i, m in enumerate(p.months):
        expect(s1, f"{L(2 + 4 * i)}{k1['net_income']}", act_sum["NetIncome"].get(m, 0), f"Net Income, {mlabel(m)}")

    # 2 ─ Profit & Loss with prior-year comparison ──────────────────────────
    s2 = Sheet(wb, "Profit & Loss", "Profit and Loss", p.range_label, 1 + 4 * (n + 1))
    both = p.months + list(py.values())
    k2 = layout_pl(s2, {x for x in act if own_in(both, act)(x)}, own_in(both, act))
    cols = []
    for i, m in enumerate(p.months + ["Total"]):
        c0 = 2 + 4 * i
        A, B = L(c0), L(c0 + 1)
        s2.header(5, c0, "Total" if m == "Total" else mlabel(m), 4)
        pyt = (f"{mlabel(py[p.months[0]])} - {mlabel(py[p.months[-1]])} (PY)" if m == "Total"
               else f"{mlabel(py[m])} (PY)")
        for k, t in enumerate(["CURRENT", pyt, "$ Change (PY)", "% Change (PY)"]):
            s2.header(6, c0 + k, t)
        if m == "Total":
            cols += [{"row": rowsum([L(2 + 4 * j) for j in range(n)]), "width": 14},
                     {"row": rowsum([L(3 + 4 * j) for j in range(n)]), "width": 14}]
        else:
            cols += [{"data": a(m)}, {"data": a(py[m])}]
        cols += [{"row": lambda r, A=A, B=B: f"={A}{r}-{B}{r}"},
                 {"row": pct_change(A, B), "fmt": PCT, "width": 10}]
    s2.write(cols)
    s2.footer(foot)
    expect(s2, f"{tot}{k2['net_income']}", sum(act_sum["NetIncome"].get(m, 0) for m in p.months),
           f"Net Income, {p.range_label}")
    expect(s2, f"{L(3 + 4 * n)}{k2['net_income']}", sum(act_sum["NetIncome"].get(py[m], 0) for m in p.months),
           "Net Income, same months prior year")
    expect(s2, f"{tot}{k2['income']}", sum(act_sum["Income"].get(m, 0) for m in p.months),
           f"Total Income, {p.range_label}")
    expect(s2, f"{tot}{k2['expenses']}", sum(act_sum["Expenses"].get(m, 0) for m in p.months),
           f"Total Expenses, {p.range_label}")

    # 3 ─ Balance Sheet with prior-period comparison ────────────────────────
    s3 = Sheet(wb, "Balance Sheet", "Balance Sheet", f"As of {MONTHS[p.ym[1]]} {month_end(p.ym).day}, {p.ym[0]}",
               1 + 4 * n + 3)
    bs_cols = q.report_month_columns(bs_rep)
    bs_vals, bs_groups = layout_report(s3, bs_rep, bs_cols)

    def bsv(key):
        return lambda r: bs_vals.get(r, {}).get(key)

    cols = []
    for i, m in enumerate(p.months):
        c0 = 2 + 4 * i
        A, B = L(c0), L(c0 + 1)
        prev = add_months(m, -1)
        s3.header(5, c0, mlabel(m), 4)
        for k, t in enumerate(["CURRENT", f"As of {MONTHS[prev[1]]} {month_end(prev).day}, {prev[0]} (PP)",
                               "$ Change (PP)", "% Change (PP)"]):
            s3.header(6, c0 + k, t)
        cols += [{"data": bsv(m), "width": 14}, {"data": bsv(prev), "width": 14},
                 {"row": lambda r, A=A, B=B: f"={A}{r}-{B}{r}"},
                 {"row": pct_change(A, B), "fmt": PCT, "width": 10}]
    c0 = 2 + 4 * n
    cur, base = L(2 + 4 * (n - 1)), L(c0)
    s3.header(5, c0, "Fiscal year to date", 3)
    for k, t in enumerate([f"As of {MONTHS[bs_start[1]]} {month_end(bs_start).day}, {bs_start[0]}",
                           "$ Change (FYTD)", "% Change (FYTD)"]):
        s3.header(6, c0 + k, t)
    cols += [{"data": bsv(bs_start), "width": 14},
             {"row": lambda r: f"={cur}{r}-{base}{r}"},
             {"row": pct_change(cur, base), "fmt": PCT, "width": 10}]
    s3.write(cols)
    s3.footer(foot)
    bs_tot = {g: q.report_values((row.get("Summary") or {}).get("ColData", []), bs_cols)
              for row in bs_rep["Rows"]["Row"] for g in [row.get("group")]}
    for i, m in enumerate(p.months):
        expect(s3, f"{L(2 + 4 * i)}{bs_groups['TotalAssets']}", bs_tot["TotalAssets"].get(m, 0),
               f"Total Assets, {mlabel(m)}")
        expect(s3, f"{L(2 + 4 * i)}{bs_groups['TotalLiabilitiesAndEquity']}", bs_tot["TotalAssets"].get(m, 0),
               f"Liabilities + Equity = Assets, {mlabel(m)}")

    # 4 ─ Statement of Cash Flows ───────────────────────────────────────────
    s4 = Sheet(wb, "Cash Flows", "Statement of Cash Flows", p.range_label, 2 + n)
    cf_cols = q.report_month_columns(cf_rep)
    cf_vals, cf_groups = layout_report(s4, cf_rep, cf_cols)
    s4.header(5, 1, "Account Name")
    cols = []
    for i, m in enumerate(p.months):
        s4.header(5, 2 + i, mlabel(m))
        cols.append({"data": (lambda key: lambda r: cf_vals.get(r, {}).get(key))(m), "width": 14})
    s4.header(5, 2 + n, "Total")
    cols.append({"row": rowsum([L(2 + j) for j in range(n)]), "width": 14})
    s4.write(cols)
    s4.ws.column_dimensions["A"].width = 62
    s4.footer(foot)
    cf_inc = [row for row in cf_rep["Rows"]["Row"] if row.get("group") == "CashIncrease"]
    if cf_inc and "CashIncrease" in cf_groups:
        inc = q.report_values(cf_inc[0]["Summary"]["ColData"], cf_cols)
        for i, m in enumerate(p.months):
            expect(s4, f"{L(2 + i)}{cf_groups['CashIncrease']}", inc.get(m, 0), f"Net cash increase, {mlabel(m)}")

    # 5 ─ YTD Actuals v Budgets ─────────────────────────────────────────────
    s5 = Sheet(wb, "YTD Actuals v Budgets", f"Budget vs. Actuals: {p.fy_label} Budget",
               f"{MONTHS_LONG[p.fy_months[0][1]]} {p.fy_months[0][0]} - {MONTHS_LONG[p.fy_months[-1][1]]} {p.fy_months[-1][0]}", 17)
    present = {x for x in set(act) | set(bud) if own_in(p.months, act)(x) or own_in(p.fy_months, bud)(x)}
    k5 = layout_pl(s5, present, lambda x: own_in(p.months, act)(x) or own_in(p.fy_months, bud)(x))

    def gl_code(aid):
        code = q.ACCOUNT_MAP[aid]["code"]
        return int(code) if code.isdigit() else (code or None)

    def annual_budget(aid):
        return round(sum(bud[aid].values()), 2) if aid in bud else None

    s5.header(5, 2, "GL")
    s5.header(5, 3, "Annual")
    s5.header(6, 3, "Budget")
    cols = [{"data": gl_code, "fmt": "0", "width": 7}, {"data": annual_budget}]
    for i, m in enumerate(p.fy_months):
        s5.header(5, 4 + i, mlabel(m))
        s5.header(6, 4 + i, "Actual")
        cols.append({"data": a(m) if m in p.months else (lambda aid: None)})
    s5.header(5, 16, "Total")
    s5.header(6, 16, "Actual")
    s5.header(5, 17, "Budget")
    s5.header(6, 17, "Remaining")
    cols += [{"row": rowsum([L(4 + j) for j in range(n)]), "width": 14},
             {"row": lambda r: f"=C{r}-P{r}", "width": 14}]
    s5.write(cols)
    for i, row in enumerate(s5.rows):                  # GL column is a label, not an amount
        r = FIRST_ROW + i
        if row["kind"] != "leaf" and not (row["kind"] == "header" and row["own"]):
            s5.ws.cell(r, 2).value = None
        for j, m in enumerate(p.fy_months):            # months not yet closed stay blank
            if m not in p.months:
                s5.ws.cell(r, 4 + j).value = None
    s5.footer(foot)
    expect(s5, f"P{k5['net_income']}", sum(act_sum["NetIncome"].get(m, 0) for m in p.months),
           f"Net Income, {p.range_label}")
    types = {aid: q.ACCOUNT_MAP[aid]["type"] for aid in bud}
    expect(s5, f"C{k5['income']}", sum(sum(v.values()) for x, v in bud.items() if types[x] == "Income"),
           f"{p.fy_label} annual income budget (Supabase budget table)")
    expect(s5, f"C{k5['expenses']}", sum(sum(v.values()) for x, v in bud.items() if types[x] == "Expense"),
           f"{p.fy_label} annual expense budget (Supabase budget table)")

    # 6, 7 ─ P&L by Class ───────────────────────────────────────────────────
    def class_tab(title, label, pulled):
        vals, sums, columns = pulled
        s = Sheet(wb, title, "Profit and Loss by Class", label, 2 + len(columns))
        data_idx = [x["idx"] for x in columns if x["kind"] == "data"]
        k = layout_pl(s, {x for x in vals if vals[x]}, lambda aid: bool(vals.get(aid)))
        letter = {x["idx"]: L(2 + j) for j, x in enumerate(columns)}
        cols, group_start = [], {}
        for j, x in enumerate(columns):
            name = "Not specified" if x["title"].lower() == "not specified" else x["title"]
            s.header(6, 2 + j, name)
            if x["group"]:
                group_start.setdefault(x["group"], 2 + j)
            if x["kind"] == "data":
                cols.append({"data": (lambda i: lambda aid: vals.get(aid, {}).get(i))(x["idx"]), "width": 15})
            else:
                cols.append({"row": rowsum([letter[i] for i in x["members"]]), "width": 16})
                first = group_start[x["group"]]
                s.header(5, first, x["group"].upper(), 2 + j - first + 1)
        s.header(6, 2 + len(columns), "Total")
        cols.append({"row": rowsum([letter[i] for i in data_idx]), "width": 15})
        s.write(cols)
        s.footer(foot)
        total_ni = sum(sums["NetIncome"].get(i, 0) for i in data_idx)
        expect(s, f"{L(2 + len(columns))}{k['net_income']}", total_ni, f"Net Income, all classes, {label}")
        ns = [x for x in columns if x["title"].lower() == "not specified"]
        if ns:
            expect(s, f"{letter[ns[0]['idx']]}{k['net_income']}", sums["NetIncome"].get(ns[0]["idx"], 0),
                   f"Net Income, Not specified, {label}")
        return total_ni

    ni_month = class_tab(f"{MONTHS[p.ym[1]]}{str(p.ym[0])[2:]} P&L by Class",
                         f"{MONTHS_LONG[p.ym[1]]} {p.ym[0]}", cls_m)
    ni_ytd = class_tab("YTD P&L by Class", p.range_label, cls_y)

    # consistency across pulls (each tab is a separate QBO report)
    cross = [
        ("P&L by Class (month) Net Income = P&L by month", ni_month, act_sum["NetIncome"].get(p.ym, 0)),
        ("P&L by Class (YTD) Net Income = P&L by month", ni_ytd,
         sum(act_sum["NetIncome"].get(m, 0) for m in p.months)),
    ]
    recon = [row for row in s2.rows if row["kind"] == "leaf" and q.ACCOUNT_MAP[row["acct"]]["code"] == "9960"]
    flags = []
    if recon:
        amt = sum(act.get(recon[0]["acct"], {}).get(m, 0) for m in p.months)
        if abs(amt) >= TOL:
            flags.append(f"9960 Reconciliation Discrepancies has {amt:,.2f} of activity {p.range_label} — "
                         "nonzero here is a books issue to clear, not a data error.")
    for name, amt in bud_unmapped.items():
        flags.append(f"Budget line not shown (no matching P&L account): {name} {amt:,.2f}")

    # 8 ─ Checks ────────────────────────────────────────────────────────────
    ws = wb.create_sheet("Checks")
    ws.sheet_view.showGridLines = False
    ws["A1"], ws["A2"] = f"{ENTITY} — package checks", f"Period: {p.range_label}"
    ws["A1"].font, ws["A2"].font = F_T1, F_T3
    ws["A3"] = ("Each row compares a total calculated by this workbook's formulas with the total "
                "QuickBooks Online reported. Any row not OK means the workbook does not tie to the books.")
    ws["A3"].font = F_NOTE
    for c, t in enumerate(["Tab", "Check", "QBO reported", "Workbook", "Difference", "Status"], 1):
        cell = ws.cell(5, c, t)
        cell.font, cell.border = F_HEAD, Border(bottom=THIN)
    r = 6
    for chk in checks:
        ws.cell(r, 1, chk["sheet"])
        ws.cell(r, 2, chk["label"])
        ws.cell(r, 3, chk["expected"])
        ws.cell(r, 4, f"='{chk['sheet'].replace(chr(39), chr(39) * 2)}'!{chk['cell']}")
        ws.cell(r, 5, f"=D{r}-C{r}")
        ws.cell(r, 6, f'=IF(ABS(E{r})<{TOL},"OK","CHECK")')
        r += 1
    for name, got, want in cross:
        ws.cell(r, 1, "All")
        ws.cell(r, 2, name)
        ws.cell(r, 3, round(want, 2))
        ws.cell(r, 4, round(got, 2))
        ws.cell(r, 5, f"=D{r}-C{r}")
        ws.cell(r, 6, f'=IF(ABS(E{r})<{TOL},"OK","CHECK")')
        r += 1
    last = r - 1
    for row in ws.iter_rows(min_row=6, max_row=last):
        for cell in row:
            cell.font = F_BODY
            if cell.column in (3, 4, 5):
                cell.number_format = NUM
    ws.cell(r + 1, 2, "Checks not OK").font = F_BOLD
    ws.cell(r + 1, 6, f'=COUNTIF(F6:F{last},"CHECK")').font = F_BOLD
    summary_cell = f"F{r + 1}"
    r += 3
    ws.cell(r, 1, "Flags for review").font = F_HEAD
    for text in flags or ["None."]:
        r += 1
        ws.cell(r, 1, text).font = F_BODY
    ws.cell(r + 2, 1, foot).font = F_NOTE
    for col, w in zip("ABCDEF", (24, 62, 16, 16, 14, 10)):
        ws.column_dimensions[col].width = w

    meta = {"period": p.range_label, "checks": checks, "cross": [list(x) for x in cross], "flags": flags,
            "checks_sheet": {"first": 6, "last": last, "summary": summary_cell}}
    return wb, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--month", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    period = Period(args.year, args.month)
    if month_end(period.ym) >= date.today():
        print(f"✗ {mlabel(period.ym)} is not a completed month.")
        return 1
    print(f"Building Woods package for {mlabel(period.ym)} ({period.fy_label}, {period.range_label})")
    if not q.start_session():
        return 1
    try:
        wb, meta = build(period)
    except (q.QBOFetchError, BuildError) as e:
        print(f"✗ Build failed: {e}")
        return 1
    for name, got, want in meta["cross"]:
        if abs(got - want) > TOL:
            print(f"✗ {name}: {got:,.2f} vs {want:,.2f}")
            return 1
    wb.save(args.out)
    with open(args.out + ".checks.json", "w") as f:
        json.dump(meta, f, indent=1)
    print(f"✓ Wrote {args.out} — {len(wb.sheetnames)} tabs, {len(meta['checks'])} checks to verify after recalc")
    for flag in meta["flags"]:
        print(f"  ⚑ {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
