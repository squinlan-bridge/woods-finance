"""Monthly Woods financial package job — runs in GitHub Actions on the 3rd.

Fully cloud-native, same shape as the Bridge board package job: build the
workbook fresh from the QBO Reports API (build_woods_package.py), recalculate
it with LibreOffice so every formula carries a cached value, VERIFY the
recalculated workbook against the totals QBO reported, then upload it to the
Finance SharePoint library under "The Woods/Monthly Packages" via Microsoft
Graph. Nothing is uploaded if verification fails.

A cloud Claude task reviews the uploaded package afterwards and sends Shane
the summary.

Environment:
  SUPABASE_URL / SUPABASE_SERVICE_KEY       - QBO tokens + budget table
  QBO_CLIENT_ID / QBO_CLIENT_SECRET         - QBO token refresh
  MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET
  SHAREPOINT_SITE_ID (or ONEDRIVE_DRIVE_ID) - Graph upload (skipped with a
                                              notice when MS_CLIENT_ID is unset)
Optional:
  WP_YEAR / WP_MONTH   - calendar year/month of the period (default: last month)
  WP_OUT_DIR           - also keep a copy of the finished workbook here
  WOODS_UPLOAD_FOLDER  - library folder (default "The Woods/Monthly Packages")
"""

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
from openpyxl import load_workbook

HERE = Path(__file__).resolve().parent
UPLOAD_FOLDER = os.environ.get("WOODS_UPLOAD_FOLDER", "").strip() or "The Woods/Monthly Packages"
GRAPH = "https://graph.microsoft.com/v1.0"
MONTHS = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
TOL = 0.01


def target_period():
    y, m = os.environ.get("WP_YEAR"), os.environ.get("WP_MONTH")
    if y and m:
        return int(y), int(m)
    prev_end = dt.date.today().replace(day=1) - dt.timedelta(days=1)
    return prev_end.year, prev_end.month


def run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


# ── Verification ───────────────────────────────────────────────────────────
def verify(path: Path, meta: dict):
    """Check the RECALCULATED workbook: no formula errors anywhere, and every
    statement total the formulas produce equals what QBO reported."""
    wb = load_workbook(path, data_only=True)
    problems = []
    for ws in wb:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and c.value.startswith("#"):
                    problems.append(f"{ws.title}!{c.coordinate} is {c.value}")
    for chk in meta["checks"]:
        got = wb[chk["sheet"]][chk["cell"]].value
        if not isinstance(got, (int, float)) or abs(got - chk["expected"]) > TOL:
            problems.append(f"{chk['sheet']}!{chk['cell']} {chk['label']}: workbook {got} vs QBO {chk['expected']}")
    not_ok = wb["Checks"][meta["checks_sheet"]["summary"]].value
    if not_ok != 0:
        problems.append(f"Checks tab reports {not_ok} check(s) not OK")
    print(f"VERIFY: {len(meta['checks'])} totals compared with QBO, "
          f"{len(wb.sheetnames)} tabs scanned for formula errors -> "
          f"{'OK' if not problems else str(len(problems)) + ' PROBLEM(S)'}")
    for p in problems[:40]:
        print("  ✗", p)
    return not problems


# ── SharePoint upload (Graph, app credentials) ─────────────────────────────
def graph_token():
    tenant = os.environ["MS_TENANT_ID"].strip()
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"grant_type": "client_credentials",
              "client_id": os.environ["MS_CLIENT_ID"].strip(),
              "client_secret": os.environ["MS_CLIENT_SECRET"].strip(),
              "scope": "https://graph.microsoft.com/.default"},
        timeout=30)
    if not r.ok:
        body = r.json() if "json" in r.headers.get("content-type", "") else {}
        print(f"Token request failed ({r.status_code}): "
              f"{body.get('error_description', r.text)[:300]}", file=sys.stderr)
    r.raise_for_status()
    return r.json()["access_token"]


def resolve_drive_id(headers):
    drive = os.environ.get("ONEDRIVE_DRIVE_ID", "").strip()
    if drive:
        return drive
    site = os.environ["SHAREPOINT_SITE_ID"].strip()
    r = requests.get(f"{GRAPH}/sites/{site}/drive", headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def ensure_folder(headers, drive_id, folder):
    """Create each level of `folder` that doesn't exist yet (409 = already there)."""
    parent = ""
    for name in [p for p in folder.split("/") if p]:
        where = "root/children" if not parent else f"root:/{requests.utils.quote(parent, safe='/')}:/children"
        r = requests.post(f"{GRAPH}/drives/{drive_id}/{where}", timeout=30,
                          headers={**headers, "Content-Type": "application/json"},
                          json={"name": name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})
        if r.status_code not in (200, 201, 409):
            r.raise_for_status()
        parent = f"{parent}/{name}" if parent else name


def upload(path: Path):
    headers = {"Authorization": f"Bearer {graph_token()}"}
    drive_id = resolve_drive_id(headers)
    ensure_folder(headers, drive_id, UPLOAD_FOLDER)
    rel = requests.utils.quote(f"{UPLOAD_FOLDER}/{path.name}", safe="/")
    url = f"{GRAPH}/drives/{drive_id}/root:/{rel}:/content?@microsoft.graph.conflictBehavior=replace"
    data = path.read_bytes()
    r = requests.put(url, headers={**headers, "Content-Type": "application/octet-stream"},
                     data=data, timeout=120)
    r.raise_for_status()
    print(f"Uploaded {path.name} ({len(data)} bytes) -> {UPLOAD_FOLDER}/  {r.json().get('webUrl', '')}")


def main():
    cy, cm = target_period()
    out_name = f"Woods_Financial_Package_{MONTHS[cm]}{cy}.xlsx"
    print(f"Building Woods financial package for {MONTHS[cm]} {cy} -> {out_name}")

    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / out_name
        run([sys.executable, str(HERE / "build_woods_package.py"),
             "--year", str(cy), "--month", str(cm), "--out", str(out_path)])
        meta = json.loads(Path(str(out_path) + ".checks.json").read_text())

        recalc_dir = Path(td) / "recalc"
        recalc_dir.mkdir()
        run(["soffice", "--headless", "--convert-to", "xlsx",
             "--outdir", str(recalc_dir), str(out_path)],
            env={**os.environ, "HOME": td})
        recalced = recalc_dir / out_name
        if not recalced.exists():
            print("LibreOffice recalc produced no output", file=sys.stderr)
            return 1

        if not verify(recalced, meta):
            print("✗ Package does not tie to QBO — NOT uploaded.", file=sys.stderr)
            return 1
        rc = meta["recon"]
        print(f"RECON: expenses feed vs QBO P&L — {rc['cells']} GL-months, {rc['exact']} exact, "
              f"{len(rc['drift'])} over ${rc['threshold']:,.0f}")
        for flag in meta["flags"]:
            print(f"⚑ {flag}")

        keep = os.environ.get("WP_OUT_DIR", "").strip()
        if keep:
            Path(keep).mkdir(parents=True, exist_ok=True)
            shutil.copy(recalced, Path(keep) / out_name)
            print(f"Kept a copy in {keep}/")
        if not os.environ.get("MS_CLIENT_ID", "").strip():
            print("MS_CLIENT_ID not set - skipping SharePoint upload (local run).")
        else:
            upload(recalced)

    print("Woods package job complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
