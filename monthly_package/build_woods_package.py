"""
The Woods Christian Camp — monthly financial package builder
============================================================
Builds the monthly Woods financial package in the same shape and styling as
the Bridge board package (bridge-finance/board_package), replacing the seven
hand-exported QBO report tabs:

    Dashboard            month, year-to-date, financial position, programs
    P&L MTD vs Budget    }
    P&L MTD vs PY        }  four views, identical account layout
    P&L YTD vs Budget    }  (YTD vs Budget carries full-year budget, % used)
    P&L YTD vs PY        }
    P&L by Month         fiscal-year actuals by month against the FY budget
    Balance Sheet        balance, month activity, FYTD activity
    Cash Flow            QBO statement of cash flows, month and FYTD
    Programs             revenue by program (QBO class), monthly trend
    Commentary           auto-drafted variance commentary (DRAFT)
    Data Notes           sources, basis, known gaps
    Checks               every statement total tied back to QBO

Unlike the Bridge package (built from pipeline CSVs), the Woods statements
come straight from the QBO Reports API, so they are books basis and tie to
the camp's financials by construction. Budget comes from the Woods Supabase
`budget` table (synced daily from the QBO Budget module) because the Reports
API has no Budget-vs-Actuals report.

Amounts are written as values; every subtotal, variance and KPI is a live
formula.

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
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.marker import Marker
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as L

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import qbo_connector as q  # noqa: E402

ENTITY = "The Woods Christian Camp"
TOL = 0.01
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
MONTH_ABBR = [m[:3] for m in MONTH_NAMES]

# ── styling constants (identical to the Bridge board package) ──────────────
NAVY = "1F3864"; BLUE = "2A78D6"; ORANGE = "EB6834"; LTGRAY = "F2F2F2"; MDGRAY = "D9D9D9"; FONT = "Arial"
F_TITLE = Font(name=FONT, size=16, bold=True, color=NAVY)
F_SUB = Font(name=FONT, size=10, italic=True, color="595959")
F_HDR = Font(name=FONT, size=9, bold=True, color="FFFFFF")
F_SECT = Font(name=FONT, size=10, bold=True, color=NAVY)
F_BODY = Font(name=FONT, size=9); F_BODY10 = Font(name=FONT, size=10)
F_TOTAL = Font(name=FONT, size=9, bold=True)
F_GTOTAL = Font(name=FONT, size=10, bold=True, color=NAVY)
FILL_HDR = PatternFill("solid", fgColor=NAVY); FILL_SECT = PatternFill("solid", fgColor=LTGRAY)
FILL_TOT = PatternFill("solid", fgColor=MDGRAY)
THIN = Side(style="thin", color="BFBFBF"); B_TOP = Border(top=THIN)
B_TOPBOT = Border(top=THIN, bottom=Side(style="double", color="404040"))
NUM = '#,##0;(#,##0);"–"'; NUM2 = '$#,##0;($#,##0);"–"'; PCT = '0.0%;(0.0%);"–"'

INCOME_TYPES = {"Income", "Other Income"}
EXPENSE_TYPES = {"Expense", "Cost of Goods Sold", "Other Expense"}
OTHER_GROUP = "SPECIAL PROJECTS & OTHER"      # top-level expense accounts with no category


class BuildError(Exception):
    pass


# ── Period helpers ─────────────────────────────────────────────────────────
def add_months(ym, n):
    i = ym[0] * 12 + (ym[1] - 1) + n
    return (i // 12, i % 12 + 1)


def month_end(ym):
    return date(ym[0], ym[1], calendar.monthrange(*ym)[1])


class Period:
    def __init__(self, year, month):
        self.ym = (year, month)
        fy_start_year = year if month >= q.FY_START_MONTH else year - 1
        self.fy_start = (fy_start_year, q.FY_START_MONTH)
        self.fy_months = [add_months(self.fy_start, i) for i in range(12)]
        self.months = [m for m in self.fy_months if m <= self.ym]      # elapsed
        self.py = {m: add_months(m, -12) for m in self.fy_months}
        self.fy = q.fiscal_year_label(date(year, month, 1))
        self.pyfy = f"FY{int(self.fy[2:]) - 1}"
        self.no = len(self.months)                                      # fiscal period number
        self.mon, self.month_name = MONTH_ABBR[month], MONTH_NAMES[month]
        self.m_name = f"{self.month_name} {year}"
        self.prev = add_months(self.ym, -1)
        self.base = add_months(self.fy_start, -1)                       # May 31 before this FY


# ── Pulls ──────────────────────────────────────────────────────────────────
def parse_account_report(report, cols):
    """(values, summaries) from a P&L-shaped report.
    values[acct_id][col_key]   amounts posted to each account (a parent's own
                               postings sit on its section Header)
    summaries[group][col_key]  QBO's own totals (Income, Expenses, NetIncome…)"""
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


def fetch_class_income(start_ym, end_ym):
    """{class_key: {"income", "expenses"}} from P&L summarized by class.
    class_key is the QBO class id, or "not_specified"."""
    rep = q.qbo_report("ProfitAndLoss", start_date=date(*start_ym, 1).isoformat(),
                       end_date=month_end(end_ym).isoformat(),
                       accounting_method="Accrual", summarize_column_by="Classes")
    cols, seen = {}, set()
    for i, c in enumerate(rep["Columns"]["Column"]):
        key = (c.get("MetaData") or [{}])[0].get("Value")
        # a parent class appears twice (its own postings, then "Total …") — keep the first
        if i and key not in ("total", None) and key not in seen:
            seen.add(key)
            cols[i] = key
    _, sums = parse_account_report(rep, cols)
    out = {}
    for key in seen:
        inc = sums["Income"].get(key, 0) + sums["OtherIncome"].get(key, 0)
        exp = sums["Expenses"].get(key, 0) + sums["COGS"].get(key, 0) + sums["OtherExpenses"].get(key, 0)
        out[key] = {"income": round(inc, 2), "expenses": round(exp, 2)}
    return out


def fetch_budget(period):
    """{acct_id: {(year, month): amount}} for the period's fiscal year, from
    the Supabase budget table (keyed by GL code → resolved to account id)."""
    rows, offset = [], 0
    while True:
        r = requests.get(f"{q.SUPABASE_URL}/rest/v1/budget", headers=q._sb_headers(), timeout=30,
                         params={"select": "gl_code,gl_name,period,budget", "order": "id",
                                 "fiscal_year": f"eq.{period.fy}", "limit": 1000, "offset": offset})
        if r.status_code != 200:
            raise BuildError(f"budget read failed: {r.status_code} {r.text[:200]}")
        rows.extend(r.json())
        if len(r.json()) < 1000:
            break
        offset += 1000
    by_code = {}
    for aid, a in q.ACCOUNT_MAP.items():
        if a["code"] and a["type"] in INCOME_TYPES | EXPENSE_TYPES:
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


def flatten_balance_sheet(report, cols):
    """Account rows of a BalanceSheet report, each tagged with the Bridge-style
    section it belongs in, plus QBO's own totals and the computed Net Income line."""
    bucket_of = {"BankAccounts": "cash", "FixedAssets": "fixed", "Liabilities": "liab", "Equity": "equity"}
    rows, totals, net_income = [], {}, {}

    def emit(coldata, bucket):
        if coldata and coldata[0].get("id"):
            vals = q.report_values(coldata, cols)
            if vals:
                a = q.ACCOUNT_MAP.get(coldata[0]["id"]) or {}
                rows.append({"id": coldata[0]["id"], "bucket": bucket, "code": a.get("code", ""),
                             "name": (a.get("full_name") or coldata[0].get("value", "")).split(":")[-1],
                             "vals": vals})

    def walk(node, bucket):
        for row in node.get("Row", []):
            g = row.get("group") or ""
            b = bucket_of.get(g, bucket)
            if g == "TotalAssets":
                b = "recv"                      # default for assets that are neither cash nor fixed
            if g in ("TotalAssets", "TotalLiabilitiesAndEquity", "BankAccounts", "Liabilities", "Equity"):
                totals[g] = q.report_values((row.get("Summary") or {}).get("ColData", []), cols)
            if g == "NetIncome":
                net_income.update(q.report_values(row.get("ColData", []), cols))
                continue
            emit((row.get("Header") or {}).get("ColData"), b)
            emit(row.get("ColData"), b)
            if row.get("Rows"):
                walk(row["Rows"], b)

    walk(report.get("Rows", {}), None)
    return rows, totals, net_income


def flatten_cash_flow(report, cols, months):
    """{section: [(label, {ym: amount})]} for Operating / Investing / Financing,
    plus QBO's own net-cash-increase line."""
    sections = {"OperatingActivities": [], "InvestingActivities": [], "FinancingActivities": []}
    increase = {}

    def leaves(node, out):
        for row in node.get("Row", []):
            data = row.get("ColData")
            if data:
                vals = q.report_values(data, cols)
                if any(abs(vals.get(m, 0)) >= TOL for m in months):
                    label = data[0].get("value", "")
                    acct = q.ACCOUNT_MAP.get(data[0].get("id") or "")
                    if acct:
                        leaf = acct["full_name"].split(":")[-1]
                        label = f"{acct['code']} {leaf}".strip()
                    out.append((label, vals))
            if row.get("Rows"):
                leaves(row["Rows"], out)

    for row in report.get("Rows", {}).get("Row", []):
        g = row.get("group")
        if g in sections:
            leaves(row.get("Rows", {}), sections[g])
        elif g == "CashIncrease":
            increase.update(q.report_values((row.get("Summary") or {}).get("ColData", []), cols))
    return sections, increase


# ── Workbook helpers ───────────────────────────────────────────────────────
def sheet_title(ws, title, sub):
    ws["A1"] = title; ws["A1"].font = F_TITLE
    ws["A2"] = sub; ws["A2"].font = F_SUB
    ws.sheet_view.showGridLines = False


def hdr_row(ws, row, headers, start_col=1, height=26):
    for i, h in enumerate(headers):
        cc = ws.cell(row=row, column=start_col + i, value=h)
        cc.font = F_HDR; cc.fill = FILL_HDR
        cc.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = height


def page(ws, landscape=False):
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True


def chart_defaults(ch):
    ch.roundedCorners = False
    ch.legend.overlay = False
    if ch.title is not None:
        ch.title.overlay = False
    ch.x_axis.delete = False
    ch.y_axis.delete = False


# ── Build ──────────────────────────────────────────────────────────────────
def build(p):
    FY, PYFY, MON = p.fy, p.pyfy, p.mon
    checks, flags = [], []

    def expect(sheet, cell, expected, label):
        checks.append({"sheet": sheet, "cell": cell, "expected": round(expected, 2), "label": label})

    # ── pulls ──────────────────────────────────────────────────────────────
    py_start = add_months(p.fy_start, -12)
    print(f"  P&L by month {MONTH_ABBR[py_start[1]]} {py_start[0]} → {MON} {p.ym[0]} ...")
    act, act_sum = fetch_pl_months(py_start, p.ym)
    print(f"  Budget {FY} ...")
    bud, bud_unmapped = fetch_budget(p)
    print("  Balance Sheet ...")
    bs_rep = q.qbo_report("BalanceSheet", start_date=date(*p.base, 1).isoformat(),
                          end_date=month_end(p.ym).isoformat(),
                          accounting_method="Accrual", summarize_column_by="Month")
    bs_rows, bs_tot, bs_ni = flatten_balance_sheet(bs_rep, q.report_month_columns(bs_rep))
    print("  Cash Flow ...")
    cf_rep = q.qbo_report("CashFlow", start_date=date(*p.fy_start, 1).isoformat(),
                          end_date=month_end(p.ym).isoformat(), summarize_column_by="Month")
    cf_sections, cf_increase = flatten_cash_flow(cf_rep, q.report_month_columns(cf_rep), p.months)
    print("  Revenue by program (class): month, YTD, prior-year month, prior-year YTD ...")
    class_names = q.fetch_class_map()
    cls = {"m": fetch_class_income(p.ym, p.ym), "y": fetch_class_income(p.fy_start, p.ym),
           "pm": fetch_class_income(p.py[p.ym], p.py[p.ym]),
           "py": fetch_class_income(p.py[p.fy_start], p.py[p.ym])}
    subtype = {a["Id"]: a.get("AccountSubType") for a in q.qbo_query(
        "SELECT Id, AccountSubType FROM Account WHERE Active IN (true, false) MAXRESULTS 1000").get("Account", [])}
    retained = {i for i, t in subtype.items() if t == "RetainedEarnings"}
    undeposited = {i for i, t in subtype.items() if t == "UndepositedFunds"}

    # ── P&L rows ───────────────────────────────────────────────────────────
    def s(src, aid, months):
        return round(sum(src.get(aid, {}).get(m, 0) for m in months), 2)

    py_months = [p.py[m] for m in p.months]

    def pl_row(aid):
        a = q.ACCOUNT_MAP[aid]
        return {"id": aid, "code": a["code"], "name": a["full_name"].split(":")[-1].strip(),
                "m_a": s(act, aid, [p.ym]), "m_b": s(bud, aid, [p.ym]), "m_p": s(act, aid, [p.py[p.ym]]),
                "y_a": s(act, aid, p.months), "y_b": s(bud, aid, p.months), "y_p": s(act, aid, py_months),
                "fy_b": s(bud, aid, p.fy_months),
                "by_month": [act.get(aid, {}).get(m) if m in p.months else None for m in p.fy_months]}

    def top_category(aid):
        top = aid
        while q.ACCOUNT_MAP[top]["parent_id"] in q.ACCOUNT_MAP:
            top = q.ACCOUNT_MAP[top]["parent_id"]
        return OTHER_GROUP if top == aid else q.ACCOUNT_MAP[top]["full_name"].upper()

    INCOME, EXP = [], defaultdict(list)
    for aid in sorted(set(act) | set(bud), key=lambda x: (q.ACCOUNT_MAP.get(x, {}).get("code") or "~", x)):
        a = q.ACCOUNT_MAP.get(aid)
        if not a or a["type"] not in INCOME_TYPES | EXPENSE_TYPES:
            continue
        row = pl_row(aid)
        if not any(abs(row[k]) > 0.005 for k in ("m_a", "m_b", "m_p", "y_a", "y_b", "y_p", "fy_b")):
            continue
        if a["type"] in INCOME_TYPES:
            INCOME.append(row)
        else:
            EXP[top_category(aid)].append(row)
    GROUPS = sorted(g for g in EXP if g != OTHER_GROUP) + ([OTHER_GROUP] if OTHER_GROUP in EXP else [])
    ALL_EXP = [r for g in GROUPS for r in EXP[g]]

    def qsum(groups, months):
        return round(sum(act_sum[g].get(m, 0) for g in groups for m in months), 2)

    QBO = {  # QBO's own statement totals
        "inc_m": qsum(["Income", "OtherIncome"], [p.ym]), "inc_y": qsum(["Income", "OtherIncome"], p.months),
        "exp_m": qsum(["Expenses", "COGS", "OtherExpenses"], [p.ym]),
        "exp_y": qsum(["Expenses", "COGS", "OtherExpenses"], p.months),
        "ni_m": qsum(["NetIncome"], [p.ym]), "ni_y": qsum(["NetIncome"], p.months),
        "ni_pm": qsum(["NetIncome"], [p.py[p.ym]]), "ni_py": qsum(["NetIncome"], py_months),
    }
    for key, rows_, field in (("inc_m", INCOME, "m_a"), ("inc_y", INCOME, "y_a"),
                              ("exp_m", ALL_EXP, "m_a"), ("exp_y", ALL_EXP, "y_a")):
        got = round(sum(r[field] for r in rows_), 2)
        if abs(got - QBO[key]) > TOL:
            raise BuildError(f"account rows ({got:,.2f}) do not foot to QBO's total ({QBO[key]:,.2f}) for {key}")

    wb = Workbook(); wb.remove(wb.active)
    BASIS = "Books basis (QBO Profit and Loss, accrual). See Data Notes."

    def build_pl(ws, sub, cols, extra=None):
        sheet_title(ws, "Statement of Income & Expenses — " + cols["label"], sub)
        headers = ["GL", "Account", cols["h1"], cols["h2"], cols["vh"], cols["vph"]]
        widths = [8, 44, 13, 13, 13, 9]
        if extra == "fy":
            headers += [f"{FY} Budget", "% FY Used"]; widths += [14, 9]
        hdr_row(ws, 4, headers)
        for i, w in enumerate(widths):
            ws.column_dimensions[L(i + 1)].width = w
        ws.freeze_panes = "C5"; page(ws); ws.print_title_rows = "4:4"
        last_col = 8 if extra == "fy" else 6

        def numfmt(row, font=F_BODY, fill=None, border=None):
            for col in range(3, last_col + 1):
                cc = ws.cell(row=row, column=col); cc.font = font
                cc.number_format = PCT if col in (6, 8) else NUM
                if fill: cc.fill = fill
                if border: cc.border = border

        def var_f(r, income):
            a_, b_ = cols["var_inc"] if income else cols["var_exp"]
            ws.cell(r, 5, f"={a_}{r}-{b_}{r}")
            ws.cell(r, 6, f'=IF(D{r}=0,"",E{r}/D{r})')

        def fy_cols(r, value):
            if extra == "fy":
                ws.cell(r, 7, value); ws.cell(r, 8, f'=IF(G{r}=0,"",C{r}/G{r})')

        def line(r, row_, income):
            ws.cell(r, 1, int(row_["code"]) if row_["code"].isdigit() else row_["code"]).font = F_BODY
            ws.cell(r, 1).alignment = Alignment(horizontal="left")
            ws.cell(r, 2, row_["name"]).font = F_BODY
            ws.cell(r, 3, row_[cols["v1"]]); ws.cell(r, 4, row_[cols["v2"]]); var_f(r, income)
            fy_cols(r, row_["fy_b"]); numfmt(r)

        def total(r, label, formula, income, font=F_TOTAL, fill=None, border=B_TOP):
            ws.cell(r, 2, label).font = font
            ws.cell(r, 3, formula("C")); ws.cell(r, 4, formula("D")); var_f(r, income)
            fy_cols(r, formula("G")); numfmt(r, font, fill, border)
            if fill:
                ws.cell(r, 1).fill = fill; ws.cell(r, 2).fill = fill

        r = 5
        ws.cell(r, 2, "INCOME").font = F_SECT
        for col in range(1, last_col + 1): ws.cell(r, col).fill = FILL_SECT
        r += 1; inc_first = r
        inc_row = {}
        for row_ in INCOME:
            inc_row[row_["id"]] = r
            line(r, row_, True); r += 1
        inc_tot = r
        total(r, "TOTAL INCOME", lambda c: f"=SUM({c}{inc_first}:{c}{r - 1})", True, fill=FILL_TOT)
        r += 2
        group_rows = {}
        for g in GROUPS:
            ws.cell(r, 2, g).font = F_SECT
            for col in range(1, last_col + 1): ws.cell(r, col).fill = FILL_SECT
            r += 1; first = r
            for row_ in EXP[g]:
                line(r, row_, False); r += 1
            total(r, f"TOTAL {g}", lambda c, first=first, last=r - 1: f"=SUM({c}{first}:{c}{last})", False)
            group_rows[g] = r; r += 2
        exp_tot = r
        total(r, "TOTAL EXPENSES", lambda c: "=" + "+".join(f"{c}{t}" for t in group_rows.values()), False, fill=FILL_TOT)
        r += 2
        net = r
        ws.cell(r, 2, "NET SURPLUS / (DEFICIT)").font = F_GTOTAL
        ws.cell(r, 3, f"=C{inc_tot}-C{exp_tot}"); ws.cell(r, 4, f"=D{inc_tot}-D{exp_tot}")
        ws.cell(r, 5, f"=C{r}-D{r}"); ws.cell(r, 6, f'=IF(ABS(D{r})<1,"",E{r}/ABS(D{r}))')
        if extra == "fy": ws.cell(r, 7, f"=G{inc_tot}-G{exp_tot}")
        numfmt(r, F_GTOTAL, border=B_TOPBOT)
        r += 2
        ws.cell(r, 2, cols["note"]).font = F_SUB
        ws.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=last_col)
        ws.row_dimensions[r].height = 28
        return {"inc_first": inc_first, "inc_tot": inc_tot, "exp_tot": exp_tot, "net": net, "inc_row": inc_row}

    FAV = "Variance is favorable/(unfavorable): income above budget and expenses below budget are positive."
    DELTA = "Δ is current year minus prior year for income and expenses alike (a positive expense Δ means more spend than last year)."
    T1, T2, T3, T4 = "P&L MTD vs Budget", "P&L MTD vs PY", "P&L YTD vs Budget", "P&L YTD vs PY"
    loc1 = build_pl(wb.create_sheet(T1), f"{p.m_name} ({FY} Period {p.no}) — actual vs budget  ·  {BASIS}", {
        "label": "Month vs Budget", "h1": f"{MON} Actual", "h2": f"{MON} Budget", "vh": "Var Fav/(Unf)", "vph": "Var %",
        "v1": "m_a", "v2": "m_b", "var_inc": ("C", "D"), "var_exp": ("D", "C"), "note": FAV})
    loc2 = build_pl(wb.create_sheet(T2), f"{p.m_name} vs {p.month_name} {p.ym[0] - 1} ({PYFY})  ·  {BASIS}", {
        "label": "Month vs Prior Year", "h1": f"{MON} {FY}", "h2": f"{MON} {PYFY}", "vh": f"Δ vs {PYFY}", "vph": "Δ %",
        "v1": "m_a", "v2": "m_p", "var_inc": ("C", "D"), "var_exp": ("C", "D"), "note": DELTA})
    loc3 = build_pl(wb.create_sheet(T3), f"{FY} year-to-date (Jun–{MON}) — actual vs budget  ·  {BASIS}", {
        "label": "YTD vs Budget", "h1": "YTD Actual", "h2": "YTD Budget", "vh": "Var Fav/(Unf)", "vph": "Var %",
        "v1": "y_a", "v2": "y_b", "var_inc": ("C", "D"), "var_exp": ("D", "C"),
        "note": FAV + ' "% FY Used" compares YTD actual to the full fiscal-year budget.'}, extra="fy")
    loc4 = build_pl(wb.create_sheet(T4), f"{FY} vs {PYFY} year-to-date (Jun–{MON})  ·  {BASIS}", {
        "label": "YTD vs Prior Year", "h1": f"YTD {FY}", "h2": f"YTD {PYFY}", "vh": f"Δ vs {PYFY}", "vph": "Δ %",
        "v1": "y_a", "v2": "y_p", "var_inc": ("C", "D"), "var_exp": ("C", "D"), "note": DELTA})
    bud_net_m = round(sum(r["m_b"] for r in INCOME) - sum(r["m_b"] for r in ALL_EXP), 2)
    bud_net_fy = round(sum(r["fy_b"] for r in INCOME) - sum(r["fy_b"] for r in ALL_EXP), 2)
    expect(T1, f"C{loc1['net']}", QBO["ni_m"], f"Net surplus, {p.m_name}")
    expect(T1, f"C{loc1['inc_tot']}", QBO["inc_m"], f"Total income, {p.m_name}")
    expect(T1, f"D{loc1['net']}", bud_net_m, f"Budgeted net, {p.m_name} (Supabase budget table)")
    expect(T2, f"D{loc2['net']}", QBO["ni_pm"], f"Net surplus, {p.month_name} {p.ym[0] - 1}")
    expect(T3, f"C{loc3['net']}", QBO["ni_y"], f"Net surplus, {FY} YTD")
    expect(T3, f"C{loc3['exp_tot']}", QBO["exp_y"], f"Total expenses, {FY} YTD")
    expect(T3, f"G{loc3['net']}", bud_net_fy, f"Budgeted net, full {FY} (Supabase budget table)")
    expect(T4, f"D{loc4['net']}", QBO["ni_py"], f"Net surplus, {PYFY} YTD (same months)")

    # ── P&L by Month ───────────────────────────────────────────────────────
    TM = "P&L by Month"
    pm = wb.create_sheet(TM)
    sheet_title(pm, "Statement of Income & Expenses — by Month",
                f"{FY} actuals by month (Jun–{MON}) against the full-year budget  ·  {BASIS}")
    hdr_row(pm, 4, ["GL", "Account"] + [f"{MONTH_ABBR[m]} {str(y)[2:]}" for (y, m) in p.fy_months]
            + ["YTD Actual", f"{FY} Budget", "% FY Used"])
    for i, w in enumerate([8, 40] + [11] * 12 + [13, 13, 9]):
        pm.column_dimensions[L(i + 1)].width = w
    pm.freeze_panes = "C5"; page(pm, landscape=True); pm.print_title_rows = "4:4"
    n_el = p.no

    def pm_fmt(r, font=F_BODY, fill=None, border=None):
        for col in range(3, 18):
            cc = pm.cell(r, col); cc.font = font
            cc.number_format = PCT if col == 17 else NUM
            if fill: cc.fill = fill
            if border: cc.border = border

    def pm_tail(r):
        pm.cell(r, 15, f"=SUM(C{r}:{L(2 + n_el)}{r})")
        pm.cell(r, 17, f'=IF(P{r}=0,"",O{r}/P{r})')

    def pm_line(r, row_):
        pm.cell(r, 1, int(row_["code"]) if row_["code"].isdigit() else row_["code"]).font = F_BODY
        pm.cell(r, 1).alignment = Alignment(horizontal="left")
        pm.cell(r, 2, row_["name"]).font = F_BODY
        for i, v in enumerate(row_["by_month"]):
            if v is not None: pm.cell(r, 3 + i, v)
        pm.cell(r, 16, row_["fy_b"]); pm_tail(r); pm_fmt(r)

    def pm_total(r, label, formula, font=F_TOTAL, fill=None, border=B_TOP):
        pm.cell(r, 2, label).font = font
        for i in range(n_el): pm.cell(r, 3 + i, formula(L(3 + i)))
        pm.cell(r, 16, formula("P")); pm_tail(r); pm_fmt(r, font, fill, border)
        if fill:
            pm.cell(r, 1).fill = fill; pm.cell(r, 2).fill = fill

    r = 5
    pm.cell(r, 2, "INCOME").font = F_SECT
    for col in range(1, 18): pm.cell(r, col).fill = FILL_SECT
    r += 1; first = r
    for row_ in INCOME:
        pm_line(r, row_); r += 1
    pm_inc = r
    pm_total(r, "TOTAL INCOME", lambda c, first=first, last=r - 1: f"=SUM({c}{first}:{c}{last})", fill=FILL_TOT)
    r += 2
    pm_groups = []
    for g in GROUPS:
        pm.cell(r, 2, g).font = F_SECT
        for col in range(1, 18): pm.cell(r, col).fill = FILL_SECT
        r += 1; first = r
        for row_ in EXP[g]:
            pm_line(r, row_); r += 1
        pm_total(r, f"TOTAL {g}", lambda c, first=first, last=r - 1: f"=SUM({c}{first}:{c}{last})")
        pm_groups.append(r); r += 2
    pm_exp = r
    pm_total(r, "TOTAL EXPENSES", lambda c: "=" + "+".join(f"{c}{t}" for t in pm_groups), fill=FILL_TOT)
    r += 2
    pm_net = r
    pm_total(r, "NET SURPLUS / (DEFICIT)", lambda c: f"={c}{pm_inc}-{c}{pm_exp}", font=F_GTOTAL, border=B_TOPBOT)
    pm.cell(r, 17).value = None
    expect(TM, f"O{pm_net}", QBO["ni_y"], f"Net surplus, {FY} YTD")
    for i, m in enumerate(p.months):
        expect(TM, f"{L(3 + i)}{pm_net}", act_sum["NetIncome"].get(m, 0), f"Net surplus, {MONTH_ABBR[m[1]]} {m[0]}")

    # ── Balance Sheet ──────────────────────────────────────────────────────
    eom = month_end(p.ym).day
    roll = bs_ni.get(p.base, 0)                # prior-FY net income, rolled into net assets on June 1
    ni_now = bs_ni.get(p.ym, 0)

    def bs_tuple(x):
        now, prev, base = (x["vals"].get(k, 0) for k in (p.ym, p.prev, p.base))
        m_act, fytd = now - prev, now - base
        if x["id"] in retained:                # exclude the fiscal-year close roll: a reclass, not activity
            fytd -= roll
            if p.prev == p.base: m_act -= roll
        return (x["code"], x["name"], round(now, 2), round(m_act, 2), round(fytd, 2))

    BAL = defaultdict(list)
    for x in bs_rows:
        t = bs_tuple(x)
        if any(abs(v) > 0.005 for v in t[2:]): BAL[x["bucket"]].append(t)
    CASH_ACCTS, RECV, FIXED = BAL["cash"], BAL["recv"], BAL["fixed"]
    liab, equity = BAL["liab"], BAL["equity"]
    EQUITY = equity + [("", f"Net surplus, {FY} to date (per books)", round(ni_now, 2), QBO["ni_m"], round(ni_now, 2))]

    TB = "Balance Sheet"
    b = wb.create_sheet(TB)
    sheet_title(b, "Balance Sheet", f"As of {p.month_name} {eom}, {p.ym[0]}  ·  Source: QBO Balance Sheet (accrual, current books)  ·  {ENTITY}")
    hdr_row(b, 4, ["GL", "Account", f"Balance {p.ym[1]}/{eom}/{str(p.ym[0])[2:]}", f"{MON} Activity", "FYTD Activity"])
    for i, w in enumerate([9, 46, 16, 14, 14]): b.column_dimensions[L(i + 1)].width = w
    b.freeze_panes = "A5"; page(b); b.print_title_rows = "4:4"

    def bs_section(ws, r, label, rows, total_label):
        ws.cell(r, 2, label).font = F_SECT
        for col in range(1, 6): ws.cell(r, col).fill = FILL_SECT
        r += 1; first = r
        for code, nm, v1, v2, v3 in rows:
            ws.cell(r, 1, int(code) if code.isdigit() else code).font = F_BODY
            ws.cell(r, 1).alignment = Alignment(horizontal="left")
            ws.cell(r, 2, nm).font = F_BODY
            ws.cell(r, 3, v1); ws.cell(r, 4, v2); ws.cell(r, 5, v3)
            for col in (3, 4, 5): ws.cell(r, col).font = F_BODY; ws.cell(r, col).number_format = NUM
            r += 1
        ws.cell(r, 2, total_label).font = F_TOTAL
        for col, f in [(3, "C"), (4, "D"), (5, "E")]:
            ws.cell(r, col, f"=SUM({f}{first}:{f}{r - 1})" if rows else 0)
            ws.cell(r, col).font = F_TOTAL; ws.cell(r, col).number_format = NUM; ws.cell(r, col).border = B_TOP
        return r

    def bs_grand(ws, r, label, parts):
        ws.cell(r, 2, label).font = F_GTOTAL
        for col, f in [(3, "C"), (4, "D"), (5, "E")]:
            ws.cell(r, col, "=" + "+".join(f"{f}{x}" for x in parts))
            ws.cell(r, col).font = F_GTOTAL; ws.cell(r, col).number_format = NUM; ws.cell(r, col).border = B_TOPBOT

    r = 5
    b.cell(r, 1, "ASSETS").font = Font(name=FONT, size=11, bold=True, color=NAVY); r += 1
    r_cash = bs_section(b, r, "Cash", CASH_ACCTS, "Total Cash"); r = r_cash + 2
    r_recv = bs_section(b, r, "Receivables & Other Assets", RECV, "Total Receivables & Other"); r = r_recv + 2
    r_fix = bs_section(b, r, "Fixed Assets (net)", FIXED, "Total Fixed Assets"); r = r_fix + 2
    r_ta = r; bs_grand(b, r, "TOTAL ASSETS", [r_cash, r_recv, r_fix]); r += 3
    b.cell(r, 1, "LIABILITIES & EQUITY").font = Font(name=FONT, size=11, bold=True, color=NAVY); r += 1
    r_li = bs_section(b, r, "Liabilities", liab, "Total Liabilities"); r = r_li + 2
    r_eq = bs_section(b, r, "Equity / Fund Balances", EQUITY, "Total Equity"); r = r_eq + 2
    r_tle = r; bs_grand(b, r, "TOTAL LIABILITIES & EQUITY", [r_li, r_eq]); r += 2
    b.cell(r, 2, f'=IF(ABS(C{r_ta}-C{r_tle})<1,"Balance check: in balance","BALANCE CHECK FAILED")').font = F_SUB
    expect(TB, f"C{r_ta}", bs_tot["TotalAssets"].get(p.ym, 0), f"Total assets, {MON} {eom}")
    expect(TB, f"C{r_tle}", bs_tot["TotalAssets"].get(p.ym, 0), "Liabilities + equity = total assets")
    expect(TB, f"C{r_cash}", bs_tot["BankAccounts"].get(p.ym, 0), "Total cash (QBO bank accounts)")
    expect(TB, f"E{r_tle}", bs_tot["TotalAssets"].get(p.ym, 0) - bs_tot["TotalAssets"].get(p.base, 0),
           "FYTD activity: liabilities + equity = change in total assets")

    # ── Cash Flow ──────────────────────────────────────────────────────────
    TC = "Cash Flow"
    c = wb.create_sheet(TC)
    sheet_title(c, "Statement of Cash Flows", f"For the month of {p.m_name} and {FY} year-to-date  ·  {ENTITY}")
    hdr_row(c, 4, ["", "", MON, "FYTD"], height=18)
    for i, w in enumerate([9, 56, 14, 14]): c.column_dimensions[L(i + 1)].width = w
    page(c)

    def cf_num(ws, row, font=F_BODY, border=None):
        for col in (3, 4):
            ws.cell(row, col).font = font; ws.cell(row, col).number_format = NUM
            if border: ws.cell(row, col).border = border

    r = 5; cf_totals = []
    for key, label, total_label in (
            ("OperatingActivities", "OPERATING ACTIVITIES", "Net cash provided by / (used in) operating activities"),
            ("InvestingActivities", "INVESTING ACTIVITIES", "Net cash provided by / (used in) investing activities"),
            ("FinancingActivities", "FINANCING ACTIVITIES", "Net cash provided by / (used in) financing activities")):
        c.cell(r, 2, label).font = F_SECT
        for col in range(1, 5): c.cell(r, col).fill = FILL_SECT
        r += 1; first = r
        for nm, vals in cf_sections[key]:
            if nm == "Net Income": nm = "Net surplus for the period (per books)"
            c.cell(r, 2, nm).font = F_BODY
            c.cell(r, 3, round(vals.get(p.ym, 0), 2)); c.cell(r, 4, round(sum(vals.get(m, 0) for m in p.months), 2))
            cf_num(c, r); r += 1
        if first == r:
            c.cell(r, 2, "(no activity this period)").font = F_BODY; r += 1
        c.cell(r, 2, total_label).font = F_TOTAL
        c.cell(r, 3, f"=SUM(C{first}:C{r - 1})"); c.cell(r, 4, f"=SUM(D{first}:D{r - 1})")
        cf_num(c, r, F_TOTAL, B_TOP); cf_totals.append(r); r += 2
    r_net = r
    c.cell(r, 2, "NET CHANGE IN CASH").font = F_GTOTAL
    for col in ("C", "D"): c.cell(r, 3 if col == "C" else 4, "=" + "+".join(f"{col}{t}" for t in cf_totals))
    cf_num(c, r, F_GTOTAL, B_TOP); r += 1
    cash = bs_tot["BankAccounts"]
    # QBO's cash-flow statement counts Undeposited Funds as cash alongside the bank accounts.
    uf = {k: sum(x["vals"].get(k, 0) for x in bs_rows if x["id"] in undeposited) for k in (p.ym, p.prev, p.base)}
    cf_cash = {k: round(cash.get(k, 0) + uf[k], 2) for k in uf}
    c.cell(r, 2, "Cash and undeposited funds at beginning of period").font = F_BODY
    c.cell(r, 3, cf_cash[p.prev]); c.cell(r, 4, cf_cash[p.base]); cf_num(c, r); r += 1
    cf_end = r
    c.cell(r, 2, "CASH AND UNDEPOSITED FUNDS AT END OF PERIOD").font = F_GTOTAL
    c.cell(r, 3, f"=C{r_net}+C{r - 1}"); c.cell(r, 4, f"=D{r_net}+D{r - 1}")
    cf_num(c, r, F_GTOTAL, B_TOPBOT); r += 2
    c.cell(r, 2, "QBO Statement of Cash Flows: net surplus per books plus balance-sheet account movements. "
                 f"Ending cash ties to the Balance Sheet: Total Cash of ${cash.get(p.ym, 0):,.0f} plus "
                 f"Undeposited Funds of ${uf[p.ym]:,.0f} (receipts not yet deposited, which QBO counts as cash).").font = F_SUB
    c.cell(r, 2).alignment = Alignment(wrap_text=True, vertical="top")
    c.merge_cells(start_row=r, start_column=2, end_row=r, end_column=4); c.row_dimensions[r].height = 28
    expect(TC, f"C{r_net}", cf_increase.get(p.ym, 0), f"Net change in cash, {p.m_name}")
    expect(TC, f"C{cf_end}", cf_cash[p.ym], "Ending cash = Balance Sheet cash + undeposited funds (month column)")
    expect(TC, f"D{cf_end}", cf_cash[p.ym], "Ending cash = Balance Sheet cash + undeposited funds (FYTD column)")

    # ── Programs (revenue by QBO class) ────────────────────────────────────
    TP = "Programs"
    g = wb.create_sheet(TP)
    sheet_title(g, "Program Revenue Analysis", f"Income by program (QBO class)  ·  {p.m_name} and {FY} YTD")
    for i, w in enumerate([20, 32, 13, 13, 10, 13, 13, 10]): g.column_dimensions[L(i + 1)].width = w

    def class_label(key):
        if key == "not_specified": return ("Not specified", "Income with no program assigned")
        fq = class_names.get(key, key)
        parts = fq.split(":")
        if len(parts) > 1:
            return (parts[0], parts[-1])
        if any(n.startswith(fq + ":") for n in class_names.values()):
            return (fq, "(no program assigned within season)")
        return ("Other programs", fq)          # a stand-alone class with no season above it

    keys = sorted({k for d in cls.values() for k in d}, key=lambda k: (k == "not_specified",) + class_label(k))
    PROG = []
    for k in keys:
        vals = tuple(cls[d].get(k, {}).get("income", 0) for d in ("m", "pm", "y", "py"))
        if any(abs(v) > 0.005 for v in vals): PROG.append(class_label(k) + vals)
    r = 4
    g.cell(r, 1, "Revenue by Program").font = F_SECT; r += 1
    hdr_row(g, r, ["Season", "Program", f"{MON} {FY}", f"{MON} {PYFY}", "YoY %", f"YTD {FY}", f"YTD {PYFY}", "YoY %"], height=24)
    r += 1; first = r
    for season, prog, m_, pm_, y_, py_ in PROG:
        g.cell(r, 1, season).font = F_BODY; g.cell(r, 2, prog).font = F_BODY
        g.cell(r, 3, m_); g.cell(r, 4, pm_); g.cell(r, 5, f'=IF(D{r}=0,"",(C{r}-D{r})/ABS(D{r}))')
        g.cell(r, 6, y_); g.cell(r, 7, py_); g.cell(r, 8, f'=IF(G{r}=0,"",(F{r}-G{r})/ABS(G{r}))')
        for col in (3, 4, 6, 7): g.cell(r, col).number_format = NUM; g.cell(r, col).font = F_BODY
        for col in (5, 8): g.cell(r, col).number_format = PCT; g.cell(r, col).font = F_BODY
        r += 1
    prog_last = r - 1; prog_tot = r
    g.cell(r, 1, "Total income").font = F_TOTAL
    for col in (3, 4, 6, 7):
        g.cell(r, col, f"=SUM({L(col)}{first}:{L(col)}{prog_last})")
        g.cell(r, col).font = F_TOTAL; g.cell(r, col).number_format = NUM; g.cell(r, col).border = B_TOP
    g.cell(r, 5, f'=IF(D{r}=0,"",(C{r}-D{r})/ABS(D{r}))'); g.cell(r, 8, f'=IF(G{r}=0,"",(F{r}-G{r})/ABS(G{r}))')
    for col in (5, 8): g.cell(r, col).number_format = PCT; g.cell(r, col).font = F_TOTAL; g.cell(r, col).border = B_TOP
    expect(TP, f"C{prog_tot}", QBO["inc_m"], f"Program revenue total = total income, {p.m_name}")
    expect(TP, f"F{prog_tot}", QBO["inc_y"], f"Program revenue total = total income, {FY} YTD")
    r += 3
    g.cell(r, 1, f"Revenue by Season — {FY} YTD").font = F_SECT; r += 1
    hdr_row(g, r, ["Season", "", f"YTD {FY}", f"YTD {PYFY}", "YoY %"], height=18); r += 1
    seasons = list(dict.fromkeys(x[0] for x in PROG))
    season_first = r; season_row = {}
    for sname in seasons:
        season_row[sname] = r
        g.cell(r, 1, sname).font = F_BODY
        g.cell(r, 3, f"=SUMIF($A${first}:$A${prog_last},$A{r},F${first}:F${prog_last})")
        g.cell(r, 4, f"=SUMIF($A${first}:$A${prog_last},$A{r},G${first}:G${prog_last})")
        g.cell(r, 5, f'=IF(D{r}=0,"",(C{r}-D{r})/ABS(D{r}))')
        for col in (3, 4): g.cell(r, col).number_format = NUM; g.cell(r, col).font = F_BODY
        g.cell(r, 5).number_format = PCT; g.cell(r, 5).font = F_BODY
        r += 1
    season_last = r - 1
    cls_exp = round(sum(v["expenses"] for k, v in cls["y"].items() if k != "not_specified"), 2)
    r += 1
    g.cell(r, 1, f"Note: programs are QBO classes. Income is classed by program; expenses are not "
                 f"({FY} YTD: a net ${abs(cls_exp):,.0f} of ${QBO['exp_y']:,.0f} in expenses carries a class), "
                 f"so program profitability cannot be read from the books today — see Data Notes.").font = F_SUB
    g.cell(r, 1).alignment = Alignment(wrap_text=True, vertical="top")
    g.merge_cells(start_row=r, start_column=1, end_row=r, end_column=8); g.row_dimensions[r].height = 30
    r += 2
    g.cell(r, 1, "Income & Expenses by Fiscal Month").font = F_SECT; r += 1
    hdr_row(g, r, ["Fiscal month", "", f"Income {PYFY}", f"Income {FY}", "", f"Expenses {PYFY}", f"Expenses {FY}"], height=24)
    trend_hdr = r; trend_first = r + 1
    for i, m in enumerate(p.fy_months):
        rr = trend_first + i
        g.cell(rr, 1, MONTH_ABBR[m[1]]).font = F_BODY
        g.cell(rr, 3, qsum(["Income", "OtherIncome"], [p.py[m]]))
        g.cell(rr, 6, qsum(["Expenses", "COGS", "OtherExpenses"], [p.py[m]]))
        if m in p.months:
            g.cell(rr, 4, qsum(["Income", "OtherIncome"], [m]))
            g.cell(rr, 7, qsum(["Expenses", "COGS", "OtherExpenses"], [m]))
        for col in (3, 4, 6, 7): g.cell(rr, col).number_format = NUM; g.cell(rr, col).font = F_BODY
    trend_last = trend_first + 11

    def income_line():
        lc = LineChart(); lc.title = f"Monthly Income — {FY} vs {PYFY}"
        lc.style = 2; lc.y_axis.numFmt = "#,##0"; lc.x_axis.title = "Fiscal month"
        lc.add_data(Reference(g, min_col=3, max_col=4, min_row=trend_hdr, max_row=trend_last), titles_from_data=True)
        lc.set_categories(Reference(g, min_col=1, min_row=trend_first, max_row=trend_last))
        lc.series[0].graphicalProperties.line.solidFill = ORANGE
        lc.series[0].graphicalProperties.line.width = 16000
        lc.series[1].graphicalProperties.line.solidFill = BLUE
        lc.series[1].graphicalProperties.line.width = 26000
        for s_ in lc.series: s_.smooth = False; s_.marker = Marker(symbol="none")
        chart_defaults(lc)
        return lc

    lc = income_line(); lc.height = 9.5; lc.width = 22
    g.add_chart(lc, "J4")
    bc = BarChart(); bc.type = "col"; bc.style = 2
    bc.title = f"{FY} YTD Revenue by Season vs {PYFY}"; bc.height = 8.5; bc.width = 15
    bc.y_axis.numFmt = "#,##0"; bc.gapWidth = 60
    bc.add_data(Reference(g, min_col=3, max_col=4, min_row=season_first - 1, max_row=season_last), titles_from_data=True)
    bc.set_categories(Reference(g, min_col=1, min_row=season_first, max_row=season_last))
    bc.series[0].graphicalProperties.solidFill = BLUE
    bc.series[1].graphicalProperties.solidFill = ORANGE
    chart_defaults(bc)
    g.add_chart(bc, "J24")

    # ── Dashboard ──────────────────────────────────────────────────────────
    TD = "Dashboard"
    d = wb.create_sheet(TD, 0)
    sheet_title(d, f"Monthly Financial Summary — {p.m_name}",
                f"{FY} Period {p.no} (fiscal year June–May)  ·  {ENTITY}  ·  Monthly financial package")
    for i, w in enumerate([34, 15, 15, 15, 3, 34, 15, 15, 15]): d.column_dimensions[L(i + 1)].width = w
    page(d, landscape=True)

    def kpi_block(ws, r, col, title, rows):
        ws.cell(r, col, title).font = Font(name=FONT, size=11, bold=True, color="FFFFFF")
        for cc in range(col, col + 4): ws.cell(r, cc).fill = FILL_HDR
        r += 1
        for i, h in enumerate(["", "Actual", "Budget / Prior", "Δ"]):
            ws.cell(r, col + i, h).font = Font(name=FONT, size=8, bold=True, color="595959")
        r += 1
        for label, v, fmt, cmpv, dv in rows:
            ws.cell(r, col, label).font = F_BODY10
            cell = ws.cell(r, col + 1, v); cell.font = Font(name=FONT, size=10, bold=True); cell.number_format = fmt
            if cmpv is not None:
                c2 = ws.cell(r, col + 2, cmpv); c2.font = F_BODY10; c2.number_format = fmt
            if dv is not None:
                c3 = ws.cell(r, col + 3, dv); c3.font = F_BODY10; c3.number_format = PCT
            r += 1
        return r + 1

    MB, MP, YB, YP = (f"'{t}'" for t in (T1, T2, T3, T4))
    i1, e1, n1 = loc1["inc_tot"], loc1["exp_tot"], loc1["net"]
    i3, e3, n3 = loc3["inc_tot"], loc3["exp_tot"], loc3["net"]
    lead = max(INCOME, key=lambda x: x["y_a"])                         # the camp's largest income line
    lr2, lr4 = loc2["inc_row"][lead["id"]], loc4["inc_row"][lead["id"]]

    def safe(num, den):
        return f'=IF({den}=0,"",{num}/ABS({den}))'

    r = 4
    r1 = kpi_block(d, r, 1, f"{p.month_name.upper()} {p.ym[0]} (MONTH)", [
        ("Total income", f"={MB}!C{i1}", NUM2, f"={MB}!D{i1}", safe(f"{MB}!E{i1}", f"{MB}!D{i1}")),
        ("Total expenses", f"={MB}!C{e1}", NUM2, f"={MB}!D{e1}", safe(f"-{MB}!E{e1}", f"{MB}!D{e1}")),
        ("Net surplus / (deficit)", f"={MB}!C{n1}", NUM2, f"={MB}!D{n1}", None),
        (f"{lead['name']} vs prior year", f"={MP}!C{lr2}", NUM2, f"={MP}!D{lr2}", safe(f"{MP}!E{lr2}", f"{MP}!D{lr2}")),
    ])
    r2 = kpi_block(d, r, 6, f"{FY} YEAR-TO-DATE (JUN–{MON.upper()})", [
        ("Total income", f"={YB}!C{i3}", NUM2, f"={YB}!D{i3}", safe(f"{YB}!E{i3}", f"{YB}!D{i3}")),
        ("Total expenses", f"={YB}!C{e3}", NUM2, f"={YB}!D{e3}", safe(f"-{YB}!E{e3}", f"{YB}!D{e3}")),
        ("Net surplus / (deficit)", f"={YB}!C{n3}", NUM2, f"={YB}!D{n3}", None),
        (f"{lead['name']} vs prior year", f"={YP}!C{lr4}", NUM2, f"={YP}!D{lr4}", safe(f"{YP}!E{lr4}", f"{YP}!D{lr4}")),
        (f"% of {FY} income budget received", f'=IF({YB}!G{i3}=0,"",{YB}!C{i3}/{YB}!G{i3})', PCT, None, None),
    ])
    r = max(r1, r2)
    deferred = round(sum(x[2] for x in liab if "deferred" in x[1].lower()), 2)
    r3 = kpi_block(d, r, 1, "FINANCIAL POSITION", [
        ("Total cash", f"='{TB}'!C{r_cash}", NUM2, None, None),
        (f"Change in cash — {p.month_name}", f"='{TB}'!D{r_cash}", NUM2, None, None),
        ("Weeks of budgeted expenses in reserve", f"=IF({YB}!G{e3}=0,\"\",'{TB}'!C{r_cash}/({YB}!G{e3}/52))", "0.0", None, None),
        ("Deferred revenue", deferred, NUM2, None, None),
        ("Total liabilities", f"='{TB}'!C{r_li}", NUM2, None, None),
    ])
    r4 = kpi_block(d, r, 6, f"PROGRAM REVENUE ({FY} YTD vs {PYFY})", [
        (sname, f"={TP}!C{season_row[sname]}", NUM2, f"={TP}!D{season_row[sname]}", f"={TP}!E{season_row[sname]}")
        for sname in seasons])
    r = max(r3, r4) + 1
    dep_rows = [x for x in bs_rows if "accumulated depreciation" in x["name"].lower()]
    if dep_rows:
        dep_fytd = -sum(x["vals"].get(p.ym, 0) - x["vals"].get(p.base, 0) for x in dep_rows)
        dep_note = (f"Memo — depreciation: none booked in {FY} through {p.m_name} (expected as a year-end entry). "
                    f"Surplus figures above are before any {FY} depreciation." if abs(dep_fytd) < 1 else
                    f"Memo — depreciation booked in {FY} to date: ${dep_fytd:,.0f}.")
        d.cell(r, 1, dep_note).font = F_SUB
        d.merge_cells(start_row=r, start_column=1, end_row=r, end_column=9)
        r += 2
    lc2 = income_line(); lc2.height = 8.5; lc2.width = 30
    d.add_chart(lc2, f"A{r}")

    # ── Commentary (auto-drafted) ──────────────────────────────────────────
    m = wb.create_sheet("Commentary")
    sheet_title(m, f"Variance Commentary — {p.m_name}",
                "DRAFT — auto-generated from the data; review before distribution. Items in [brackets] need confirmation.")
    m.column_dimensions["A"].width = 4; m.column_dimensions["B"].width = 118
    page(m)

    def money(v): return f"${v:,.0f}" if v >= 0 else f"(${abs(v):,.0f})"
    def pct(a_, b_): return f"{(a_ - b_) / abs(b_) * 100:+.1f}%" if abs(b_) > 0.005 else "n/a"

    ti, tib, tip = (round(sum(x[k] for x in INCOME), 2) for k in ("m_a", "m_b", "m_p"))
    te, teb = (round(sum(x[k] for x in ALL_EXP), 2) for k in ("m_a", "m_b"))
    yi, yib = (round(sum(x[k] for x in INCOME), 2) for k in ("y_a", "y_b"))
    ye, yeb = (round(sum(x[k] for x in ALL_EXP), 2) for k in ("y_a", "y_b"))
    bullets_income = [
        f"Total income of {money(ti)} vs {money(tib)} budget ({pct(ti, tib)}) and {money(tip)} prior year ({pct(ti, tip)}).",
        f"{lead['name']} ({lead['code']}): {money(lead['m_a'])} vs {money(lead['m_b'])} budget ({pct(lead['m_a'], lead['m_b'])}) "
        f"and {money(lead['m_p'])} prior year ({pct(lead['m_a'], lead['m_p'])}).",
        f"{FY} YTD income of {money(yi)} vs {money(yib)} budget ({pct(yi, yib)}).",
    ]
    unfav = sorted([x for x in ALL_EXP if x["m_a"] - x["m_b"] > 1500], key=lambda x: x["m_b"] - x["m_a"])[:6]
    fav = sorted([x for x in ALL_EXP if x["m_b"] - x["m_a"] > 5000], key=lambda x: x["m_a"] - x["m_b"])[:3]
    bullets_exp = [f"Total expenses of {money(te)} vs {money(teb)} budget ({money(teb - te)} "
                   f"{'favorable' if teb >= te else 'unfavorable'}); {FY} YTD {money(ye)} vs {money(yeb)} budget "
                   f"({money(yeb - ye)} {'favorable' if yeb >= ye else 'unfavorable'})."]
    for x in unfav:
        bullets_exp.append(f"{x['name']} ({x['code']}): {money(x['m_a'])} vs {money(x['m_b'])} budget "
                           f"({money(x['m_a'] - x['m_b'])} unfavorable). [Confirm driver.]")
    for x in fav:
        bullets_exp.append(f"{x['name']} ({x['code']}): {money(x['m_a'])} vs {money(x['m_b'])} budget — "
                           f"{money(x['m_b'] - x['m_a'])} underspend, likely timing. [Confirm.]")
    credits = [x for x in ALL_EXP if x["m_a"] < -500]
    for x in credits[:4]:
        bullets_exp.append(f"{x['name']} ({x['code']}) shows a net credit of {money(x['m_a'])} for the month — typically an "
                           f"accrual reversal or reclass. [Confirm.]")
    cash_now = round(cash.get(p.ym, 0), 2); cash_chg = round(cash_now - cash.get(p.prev, 0), 2)
    bullets_bs = [
        f"Total cash of {money(cash_now)}, {'up' if cash_chg >= 0 else 'down'} {money(abs(cash_chg))} in {p.month_name}.",
        f"{FY} net surplus to date (per books): {money(QBO['ni_y'])}; {p.month_name} contribution {money(QBO['ni_m'])}.",
        f"Deferred revenue of {money(deferred)} is cash already received for programs not yet delivered.",
    ]
    for x in liab:
        if x[2] < -0.005 and x[0].startswith("2"):
            bullets_bs.append(f"FLAG: {x[1]} ({x[0]}) carries a negative balance of {money(x[2])} — review posting. [Confirm with bookkeeper.]")
    recon = [x for x in ALL_EXP if x["code"] == "9960"]
    if recon and abs(recon[0]["y_a"]) >= TOL:
        text = (f"9960 Reconciliation Discrepancies has {money(recon[0]['y_a'])} of activity {FY} YTD "
                f"({money(recon[0]['m_a'])} in {p.month_name}) — a books item to clear, not a data error.")
        bullets_bs.append("FLAG: " + text + " [Confirm with bookkeeper.]")
        flags.append(text)
    for name, amt in bud_unmapped.items():
        flags.append(f"Budget line not shown (no matching P&L account): {name} {amt:,.2f}")
    COMMENTS = [("INCOME", bullets_income), ("EXPENSES", bullets_exp), ("CASH & BALANCE SHEET", bullets_bs),
                ("[ADD: registration and occupancy trends, facility projects, and any known one-time items before distribution]", [])]
    r = 4
    for section, items in COMMENTS:
        m.cell(r, 2, section).font = F_SECT
        m.cell(r, 2).fill = FILL_SECT; m.cell(r, 1).fill = FILL_SECT
        r += 1
        for t in items:
            cell = m.cell(r, 2, "•  " + t); cell.font = F_BODY10
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            m.row_dimensions[r].height = max(15, 14 * (1 + len(t) // 110))
            r += 1
        r += 1

    # ── Data Notes ─────────────────────────────────────────────────────────
    nts = wb.create_sheet("Data Notes")
    sheet_title(nts, "Data Sources, Basis & Reconciliation", "Methodology — the package is built entirely from QuickBooks Online")
    nts.column_dimensions["A"].width = 30; nts.column_dimensions["B"].width = 100
    page(nts)
    stamp = datetime.now(timezone.utc).strftime("%B %d, %Y %H:%M UTC")
    NOTES = [
        ("Fiscal calendar", f"{FY} = June–May. {p.m_name} is {FY} Period {p.no}."),
        ("Sources", f"Statements are pulled directly from the QuickBooks Online Reports API for The Woods (realm {q.AUTH['company_id']}): "
                    "Profit and Loss by month (current and prior fiscal year), Balance Sheet by month, Statement of Cash Flows, and "
                    "Profit and Loss by class. Budget is the QBO budget for the fiscal year, read from the Woods pipeline's budget "
                    f"table (synced daily). Built {stamp} by the woods-finance GitHub Actions job; no manual exports or hand-keyed numbers."),
        ("Basis", "Books basis, accrual — the same numbers QBO's own reports show, including journal entries. Payroll and "
                  "benefits billed by The Bridge are accrued at month-end and reversed when the bill is paid, so each month "
                  "carries its own payroll cost."),
        ("P&L views", "Five tabs with an identical account layout: MTD vs Budget, MTD vs PY, YTD vs Budget (with full-year budget and "
                      "% used), YTD vs PY, and P&L by Month. Expenses are grouped by their top-level QBO category. Variance on "
                      "budget tabs is favorable/(unfavorable); on PY tabs a neutral year-over-year change."),
        ("Balance sheet & cash flow", "Month-end balances from the QBO Balance Sheet. Month activity = month-end minus prior month-end; "
                                      "FYTD = month-end minus May 31. QBO presents current-year net income as a computed line, carried as "
                                      f"\"Net surplus, {FY} to date (per books)\"; the June 1 roll of prior-year net income into net assets "
                                      "is excluded from activity because it is a reclass, not a movement. Cash flow is QBO's Statement of "
                                      "Cash Flows, which counts Undeposited Funds as cash; ending cash ties to Total Cash plus Undeposited Funds."),
        ("Programs", "Program = QBO class. Income is classed by program; expenses are almost entirely unclassed, so the Programs tab "
                     "reports revenue only. Classing direct program costs in QBO would make program-level results possible."),
        ("Verification", "Before upload the workbook is recalculated and every statement total is compared with the total QBO "
                         "reported (see the Checks tab). The job refuses to upload a package that does not tie."),
        ("Known gaps", "1) Program-level expenses are not available (see Programs). 2) Depreciation is booked at year-end, so "
                       "in-year surplus is before depreciation. 3) Prior-period figures reflect the books as of the build date; "
                       "entries posted to a closed month after a package was issued will change that month in later packages."),
        ("Refresh", "Rebuild any month from GitHub: Actions → Monthly Woods Financial Package → Run workflow, with the calendar "
                    "year and month. Or locally: python3 monthly_package/package_job.py with WP_YEAR / WP_MONTH set."),
    ]
    r = 4
    for label, text in NOTES:
        nts.cell(r, 1, label).font = F_TOTAL
        cell = nts.cell(r, 2, text); cell.font = F_BODY10
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        nts.row_dimensions[r].height = max(15, 14 * (1 + len(text) // 95))
        nts.cell(r, 1).alignment = Alignment(vertical="top")
        r += 2

    # ── Checks ─────────────────────────────────────────────────────────────
    ws = wb.create_sheet("Checks")
    sheet_title(ws, "Package Checks — tie-out to QuickBooks Online",
                "Each row compares a total this workbook calculates with the total QBO reported. Anything not OK means the package does not tie.")
    hdr_row(ws, 4, ["Tab", "Check", "QBO reported", "Workbook", "Difference", "Status"], height=18)
    for col, w in zip("ABCDEF", (22, 62, 16, 16, 13, 10)): ws.column_dimensions[col].width = w
    page(ws)
    r = 5
    for chk in checks:
        ws.cell(r, 1, chk["sheet"]); ws.cell(r, 2, chk["label"]); ws.cell(r, 3, chk["expected"])
        ws.cell(r, 4, f"='{chk['sheet']}'!{chk['cell']}")
        ws.cell(r, 5, f"=D{r}-C{r}"); ws.cell(r, 6, f'=IF(ABS(E{r})<{TOL},"OK","CHECK")')
        for col in range(1, 7): ws.cell(r, col).font = F_BODY
        for col in (3, 4, 5): ws.cell(r, col).number_format = '#,##0.00;(#,##0.00);"–"'
        ws.cell(r, 6).alignment = Alignment(horizontal="center")
        r += 1
    last = r - 1
    ws.cell(r, 2, "Checks not OK").font = F_TOTAL
    ws.cell(r, 6, f'=COUNTIF(F5:F{last},"CHECK")').font = F_TOTAL
    for col in range(1, 7): ws.cell(r, col).border = B_TOP
    summary_cell = f"F{r}"
    r += 2
    ws.cell(r, 1, "Flags for review").font = F_SECT
    for text in flags or ["None."]:
        r += 1
        ws.cell(r, 1, text).font = F_BODY

    meta = {"period": p.m_name, "fiscal_year": FY, "checks": checks, "flags": flags,
            "checks_sheet": {"summary": summary_cell},
            "headline": {"income_month": QBO["inc_m"], "expenses_month": QBO["exp_m"], "net_month": QBO["ni_m"],
                         "income_ytd": QBO["inc_y"], "expenses_ytd": QBO["exp_y"], "net_ytd": QBO["ni_y"],
                         "cash": cash_now}}
    return wb, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--month", type=int, required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    period = Period(args.year, args.month)
    if month_end(period.ym) >= date.today():
        print(f"✗ {period.m_name} is not a completed month.")
        return 1
    print(f"Building Woods package for {period.m_name} ({period.fy} Period {period.no})")
    if not q.start_session():
        return 1
    try:
        wb, meta = build(period)
    except (q.QBOFetchError, BuildError) as e:
        print(f"✗ Build failed: {e}")
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
