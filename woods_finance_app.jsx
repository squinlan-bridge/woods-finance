import { useState, useRef, useEffect, useMemo } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend, ResponsiveContainer, Cell, ReferenceLine,
} from "recharts";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// THE WOODS — CAMP FINANCE DASHBOARD
// Expenses + Budget Pacing. Modeled on bridge-finance but fully separate:
// its own Supabase project, repo, and deployment. Fiscal year June–May,
// same as The Bridge.
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// SHARED CONSTANTS & UTILITIES
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const FY_MONTHS = ["Jun","Jul","Aug","Sep","Oct","Nov","Dec","Jan","Feb","Mar","Apr","May"];

// Default dashboard fiscal year — flips to the upcoming FY starting May 1 each
// year (~1 month head-start), same convention as the Bridge dashboard.
function defaultFiscalYear() {
  const now = new Date();
  const year  = now.getFullYear();
  const month = now.getMonth() + 1; // 1-12
  const fyNum = (month >= 5) ? (year + 1) : year;
  return `FY${String(fyNum).slice(2)}`;
}
function previousFiscalYear(fy) {
  const n = parseInt(fy.replace("FY", ""), 10);
  return `FY${String(n - 1).padStart(2, "0")}`;
}

// FY selector options: FY24 through the default FY, generated so this list
// never needs annual maintenance. (Woods data may not reach back to FY24 —
// empty years just render zero charts.)
const _DEF_FY_NUM = parseInt(defaultFiscalYear().slice(2), 10);
const FISCAL_YEARS = Array.from({ length: Math.max(1, _DEF_FY_NUM - 24 + 1) }, (_, i) => `FY${24 + i}`);
const YOY_PALETTE = ["#6B7280", "#4F8EF7", "#4FD1A0", "#F7CF4F", "#C084FC", "#F7914F"];
const YOY_COLORS = Object.fromEntries(FISCAL_YEARS.map((fy, i) => [fy, YOY_PALETTE[i % YOY_PALETTE.length]]));

// Fractional months elapsed in a fiscal year (0..12). Before FY start = 0,
// after FY end = 12, mid-year = days-elapsed / avg-month-length (30.44).
// SINGLE source of truth for every YTD-match / pacing / proration calc —
// Budget Pacing reconciling with Expenses depends on all callers using
// exactly this math, so don't fork a local copy.
function fyMonthsElapsed(fy) {
  const fyNum   = parseInt(fy.replace("FY", ""), 10);
  const fyStart = new Date(2000 + fyNum - 1, 5, 1);
  const fyEnd   = new Date(2000 + fyNum, 4, 31, 23, 59, 59);
  const now = new Date();
  if (now < fyStart) return 0;
  if (now > fyEnd)   return 12;
  return (now - fyStart) / 86400000 / 30.44;
}

// ── URL hash deep-linking ─────────────────────────────────────────────────
// Format: #/<dashboard>?fy=FY27&view=ytd — makes any view shareable/bookmarkable.
// replaceState, not pushState: filter tweaks shouldn't pile up history entries.
function hashPath() {
  const h = window.location.hash.replace(/^#\/?/, "");
  const q = h.indexOf("?");
  return (q >= 0 ? h.slice(0, q) : h) || null;
}
function hashParams() {
  const h = window.location.hash;
  const q = h.indexOf("?");
  return new URLSearchParams(q >= 0 ? h.slice(q + 1) : "");
}
function writeHash(path, params) {
  const q = params.toString();
  const next = `#/${path || ""}${q ? "?" + q : ""}`;
  if (window.location.hash !== next) window.history.replaceState(null, "", next);
}
// useState variant that initializes from a hash param and mirrors changes back.
function useHashState(key, defaultValue, validValues) {
  const [value, setValue] = useState(() => {
    const v = hashParams().get(key);
    return v != null && (!validValues || validValues.includes(v)) ? v : defaultValue;
  });
  useEffect(() => {
    const params = hashParams();
    if (value === defaultValue) params.delete(key); else params.set(key, value);
    writeHash(hashPath(), params);
  }, [key, value, defaultValue]);
  return [value, setValue];
}
const DEFAULT_FY      = defaultFiscalYear();
const DEFAULT_YOY_FYB = previousFiscalYear(DEFAULT_FY);

// Compact currency: <$1,000 shows real dollars ("$728"), $1K–$1M shows one
// decimal ("$728.9K"), $1M+ keeps two ("$1.87M"). Sign comes first.
const fmt = (n) => {
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  if (a >= 1_000_000) return `${sign}$${(a/1_000_000).toFixed(2)}M`;
  if (a >= 1_000)     return `${sign}$${(a/1_000).toFixed(1)}K`;
  return `${sign}$${Math.round(a)}`;
};
const fmtFull = (n) => "$" + Math.abs(Math.round(n)).toLocaleString("en-US");
const pct     = (a, b) => b === 0 ? "—" : `${a >= b ? "+" : ""}${(((a-b)/b)*100).toFixed(1)}%`;
const varObj  = (actual, budget) => {
  const v = budget - actual;
  return { val:v, pct: budget ? Math.abs(((v/budget)*100)).toFixed(1) : "0.0", favorable: v >= 0 };
};

// ── Supabase config + paginated fetch ─────────────────────────────────────
// THE WOODS' OWN Supabase project — never point this at Bridge's
// (izuumangibzhsvpumwzx): both apps ship their anon key in the bundle, so a
// shared project would let either audience read the other entity's books.
// Anon key is intended for client use; RLS enforces read-only access.
const SUPABASE_URL      = "https://zeewewahazzuzofhvwuu.supabase.co";
const SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InplZXdld2FoYXp6dXpvZmh2d3V1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODgzNjgzMzUsImV4cCI6MjEwMzk0NDMzNX0.wnpknHc8NJM_ey9nkYGXUBn5c0fC9Z7lbPx6qr_C8ho";
const SUPABASE_HEADERS  = {
  "apikey":        SUPABASE_ANON_KEY,
  "Authorization": `Bearer ${SUPABASE_ANON_KEY}`,
};
const SUPABASE_PAGE_SIZE = 1000;

// Module-level cache: one download per (table, query) per page load. Caching
// the PROMISE also dedupes concurrent requests (Expenses and Budget Pacing
// both ask for `expenses` while it's in flight).
const _rowsCache = new Map();
function fetchAllRows(table, query = "") {
  const key = `${table}|${query}`;
  if (_rowsCache.has(key)) return _rowsCache.get(key);
  const p = _fetchAllRowsUncached(table, query).catch(err => {
    _rowsCache.delete(key); // don't cache failures — a retry should re-fetch
    throw err;
  });
  _rowsCache.set(key, p);
  return p;
}

// Paginate around PostgREST's 1000-row cap using Range headers. First request
// learns the total via `Prefer: count=exact`; remaining pages fetch in parallel.
async function _fetchAllRowsUncached(table, query = "") {
  const sep = query ? (query.startsWith("?") ? "" : "?") : "";
  const url = `${SUPABASE_URL}/rest/v1/${table}${sep}${query}`;
  const MAX_PAGES = 200;

  const fetchPage = async (from, withCount) => {
    const headers = { ...SUPABASE_HEADERS, "Range-Unit": "items", "Range": `${from}-${from + SUPABASE_PAGE_SIZE - 1}` };
    if (withCount) headers["Prefer"] = "count=exact";
    const res = await fetch(url, { headers });
    if (!res.ok && res.status !== 206) {
      throw new Error(`Supabase ${table} ${res.status}: ${await res.text()}`);
    }
    const batch = await res.json();
    if (!Array.isArray(batch)) throw new Error(`Supabase ${table} returned non-array`);
    return { batch, contentRange: res.headers.get("content-range") || "" };
  };

  const first = await fetchPage(0, true);
  const total = parseInt(first.contentRange.split("/")[1], 10);
  if (!Number.isFinite(total) || total <= first.batch.length) return first.batch;

  const pages = Math.min(Math.ceil(total / SUPABASE_PAGE_SIZE), MAX_PAGES);
  const rest = await Promise.all(
    Array.from({ length: pages - 1 }, (_, i) => fetchPage((i + 1) * SUPABASE_PAGE_SIZE, false))
  );
  return first.batch.concat(...rest.map(r => r.batch));
}

// Calendar (period 1-12) → fiscal_month_index (Jun=0 ... May=11).
const calToFiscalMonthIdx = (period) => period >= 6 ? period - 6 : period + 6;

// 12-month zero-array helper used by all aggregators.
const zeroMonths = () => [0,0,0,0,0,0,0,0,0,0,0,0];

// ── Shared Dropdown ───────────────────────────────────────────────────────
function Dropdown({ label, value, options, onChange, multi=false, selectedSet, minWidth=160, getLabel, colorMap, searchable }) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef(null);
  const searchInputRef = useRef(null);
  useEffect(() => {
    const h = e => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h); return () => document.removeEventListener("mousedown", h);
  }, []);
  useEffect(() => { if (!open) setSearch(""); }, [open]);
  useEffect(() => {
    if (open && searchInputRef.current) {
      const id = setTimeout(() => searchInputRef.current?.focus(), 30);
      return () => clearTimeout(id);
    }
  }, [open]);

  const getColor = opt => colorMap ? colorMap[opt] : (YOY_COLORS[opt]);
  // Auto-enable search on any list with >12 options.
  const showSearch = searchable ?? options.length > 12;

  const displayLabel = multi
    ? selectedSet.size === options.length ? "All"
      : selectedSet.size === 0 ? "None"
      : selectedSet.size === 1 ? (getLabel ? getLabel([...selectedSet][0]) : [...selectedSet][0])
      : `${selectedSet.size} selected`
    : (getLabel ? getLabel(value) : value);

  const q = search.trim().toLowerCase();
  const visibleOptions = q
    ? options.filter(opt => {
        const lbl = (getLabel ? getLabel(opt) : opt) || "";
        return String(opt).toLowerCase().includes(q) || String(lbl).toLowerCase().includes(q);
      })
    : options;

  return (
    <div ref={ref} style={{ position:"relative", minWidth }}>
      <div style={{ fontSize:10, color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.1em", marginBottom:5 }}>{label}</div>
      <button onClick={() => setOpen(o => !o)} style={{
        width:"100%", padding:"8px 12px", background:"#0D1117",
        border:`1px solid ${open?"#4FD1A0":"#374151"}`, borderRadius:7,
        color:"#E5E7EB", fontSize:12, fontWeight:500, cursor:"pointer",
        display:"flex", justifyContent:"space-between", alignItems:"center", gap:8, transition:"border-color 0.15s",
      }}>
        <span style={{ overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{displayLabel}</span>
        <span style={{ color:"#6B7280", fontSize:9, flexShrink:0, transform:open?"rotate(180deg)":"none", transition:"transform 0.15s" }}>▼</span>
      </button>
      {open && (
        <div style={{
          position:"absolute", top:"calc(100% + 4px)", left:0, zIndex:200,
          background:"#161B22", border:"1px solid #374151", borderRadius:8,
          minWidth:"100%", boxShadow:"0 8px 32px rgba(0,0,0,0.6)", maxHeight:340, overflowY:"auto",
        }}>
          {showSearch && (
            <div style={{ padding:"8px 10px", borderBottom:"1px solid #21262D",
              position:"sticky", top:0, background:"#161B22", zIndex:1 }}>
              <input
                ref={searchInputRef}
                type="text"
                value={search}
                onChange={e => setSearch(e.target.value)}
                onKeyDown={e => { if (e.key === "Escape") setOpen(false); }}
                placeholder="Search…"
                style={{
                  width:"100%", padding:"6px 10px", background:"#0D1117",
                  border:"1px solid #374151", borderRadius:6,
                  color:"#E5E7EB", fontSize:12, outline:"none",
                }}
                onFocus={e => e.currentTarget.style.borderColor = "#4FD1A0"}
                onBlur={e => e.currentTarget.style.borderColor = "#374151"}
              />
            </div>
          )}
          {/* Select All hidden during search — ambiguous whether it means visible or all. */}
          {multi && !q && (
            <div onClick={() => onChange(selectedSet.size===options.length ? "NONE" : "ALL")}
              style={{ padding:"8px 14px", fontSize:11, color:"#6B7280", cursor:"pointer",
                borderBottom:"1px solid #21262D", fontWeight:600,
                position:"sticky", top: showSearch ? 42 : 0, background:"#161B22" }}>
              {selectedSet.size===options.length ? "Deselect All" : "Select All"}
            </div>
          )}
          {visibleOptions.length === 0 && (
            <div style={{ padding:"10px 14px", fontSize:11, color:"#6B7280", fontStyle:"italic" }}>
              No matches
            </div>
          )}
          {visibleOptions.map(opt => {
            const active = multi ? selectedSet.has(opt) : opt===value;
            const color  = getColor(opt);
            const lbl    = getLabel ? getLabel(opt) : opt;
            return (
              <div key={opt} onClick={() => { onChange(opt); if (!multi) setOpen(false); }}
                style={{ padding:"8px 14px", fontSize:12,
                  color:active?"#F9FAFB":"#9CA3AF",
                  background:active?"#21262D":"transparent",
                  cursor:"pointer", display:"flex", alignItems:"center", gap:8 }}
                onMouseEnter={e => { if (!active) e.currentTarget.style.background="#1C2128"; }}
                onMouseLeave={e => { if (!active) e.currentTarget.style.background="transparent"; }}
              >
                {multi && (
                  <span style={{ width:12, height:12, borderRadius:3, flexShrink:0,
                    border:`1.5px solid ${active&&color ? color : "#374151"}`,
                    background:active&&color ? color+"22" : "transparent",
                    display:"flex", alignItems:"center", justifyContent:"center" }}>
                    {active && <span style={{ color:color||"#4FD1A0", fontSize:8, fontWeight:700 }}>✓</span>}
                  </span>
                )}
                {color && <span style={{ width:7, height:7, borderRadius:"50%", background:color, flexShrink:0 }} />}
                <span style={{ overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{lbl}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ── Sync freshness badge ──────────────────────────────────────────────────
// Reads the sync_freshness view (same one the GH Actions freshness check and
// the pg_cron watchdog use). One cached request per page load.
function useSyncFreshness() {
  const [feeds, setFeeds] = useState(null);
  useEffect(() => {
    let cancelled = false;
    fetchAllRows("sync_freshness", "select=feed,last_run_at,status,stale")
      .then(rows => { if (!cancelled) setFeeds(rows); })
      .catch(() => { if (!cancelled) setFeeds([]); });
    return () => { cancelled = true; };
  }, []);
  return feeds;
}

function FreshnessBadge({ loading, loadError }) {
  const feeds = useSyncFreshness();
  let color = "#4FD1A0", text = "Live · Supabase", title = "";
  if (loadError) { color = "#F87171"; text = `Load error · ${loadError}`; }
  else if (loading) { color = "#F7CF4F"; text = "Loading live data…"; }
  else if (feeds && feeds.length) {
    const bad = feeds.filter(f => f.stale || f.status !== "ok");
    if (bad.length) {
      color = "#F7CF4F";
      text  = `⚠ ${bad.length} feed${bad.length > 1 ? "s" : ""} behind — data may be stale`;
      title = bad.map(f => `${f.feed}: ${f.status}${f.stale ? " · stale" : ""}`).join("\n");
    } else {
      const oldest = feeds.reduce((m, f) => Math.min(m, new Date(f.last_run_at).getTime()), Infinity);
      const hrs = Math.max(0, Math.round((Date.now() - oldest) / 3600000));
      text  = `Data synced ${hrs <= 1 ? "within the hour" : `${hrs}h ago`} ✓`;
      title = feeds.map(f => `${f.feed}: ${new Date(f.last_run_at).toLocaleString()}`).join("\n");
    }
  }
  return <span title={title} style={{ fontSize: 10, color, marginTop: 6, whiteSpace: "nowrap", cursor: title ? "help" : "default" }}>{text}</span>;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// CHART OF ACCOUNTS (shared by both dashboards)
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// GL accounts + categories come from the gl_accounts Supabase table, mirrored
// from The Woods' QBO daily. NEVER hardcode the GL list here — new/retired
// accounts must flow through the nightly sync automatically.

// Category colors are assigned at load time by cycling this palette, because
// The Woods' category names come from ITS chart of accounts, not a fixed list.
const CAT_PALETTE = [
  "#4FD1A0", "#4F8EF7", "#F7914F", "#C084FC", "#F7CF4F", "#F472B6",
  "#38BDF8", "#A3E635", "#FB923C", "#818CF8", "#34D399", "#E879F9",
  "#94A3B8", "#F87171", "#FBBF24", "#2DD4BF",
];
const CAT_COLOR_FALLBACK = "#64748B";
// Populated by loadGlAccounts(); read via CAT_COLORS[cat] || CAT_COLOR_FALLBACK.
const CAT_COLORS = {};
const catColor = (cat) => CAT_COLORS[cat] || CAT_COLOR_FALLBACK;

// GLs permanently excluded from both dashboards (e.g. wash accounts that
// aren't budgeted). EMPTY until the Woods chart of accounts is reviewed with
// Shane — add codes here the way Bridge excludes 89999/81300.
const EXPENSE_DASHBOARD_EXCLUDED = new Set([]);

// GLs treated as "non-operational" — excluded from actual + budget when the
// Operational-only toggle is on. EMPTY until reviewed with Shane; the toggle
// hides itself while this set is empty.
const NON_OPERATIONAL_GLS = new Set([]);

// Shared select for the `expenses` table. Both dashboards use this exact
// string so the fetchAllRows cache serves ONE download instead of two.
const EXPENSES_SELECT = "select=id,date,fiscal_year,fiscal_month_index,gl_code,gl_name,category,amount,vendor,source,qbo_id,memo";

let _glCache = null;
let _glListeners = new Set();
let _glInflight = null;

async function loadGlAccounts() {
  if (_glCache) return _glCache;
  if (_glInflight) return _glInflight;
  _glInflight = (async () => {
    // Expense-flavored account types only, active only, must have a code
    // (that's what expenses.gl_code joins on).
    const rows = await fetchAllRows(
      "gl_accounts",
      "select=code,name,category" +
      "&account_type=in.(Expense,Other Expense,Cost of Goods Sold)" +
      "&active=eq.true&code=not.is.null&order=code.asc"
    );
    const glAccounts = rows.map(r => ({
      code:     r.code,
      name:     r.name,
      category: r.category || "Uncategorized",
    }));
    const categories = [...new Set(glAccounts.map(g => g.category))].sort();
    categories.forEach((c, i) => { if (!CAT_COLORS[c]) CAT_COLORS[c] = CAT_PALETTE[i % CAT_PALETTE.length]; });
    _glCache = { glAccounts, categories };
    _glListeners.forEach(fn => fn(_glCache));
    return _glCache;
  })();
  return _glInflight;
}

function useSharedGlAccounts() {
  const [state, setState] = useState(_glCache || { glAccounts: [], categories: [] });
  useEffect(() => {
    if (_glCache) { setState(_glCache); return; }
    _glListeners.add(setState);
    loadGlAccounts().catch(err => console.error("gl_accounts load failed", err));
    return () => { _glListeners.delete(setState); };
  }, []);
  return state;
}

// Aggregate raw expense rows into [fy][gl_code] -> 12-month array (Jun=0..May=11).
function shapeExpenseRows(rows) {
  const out = {};
  for (const r of rows) {
    const fy   = r.fiscal_year;
    const code = r.gl_code;
    const idx  = r.fiscal_month_index;
    if (fy == null || code == null || idx == null || idx < 0 || idx > 11) continue;
    if (!out[fy])         out[fy] = {};
    if (!out[fy][code])   out[fy][code] = zeroMonths();
    out[fy][code][idx]   += Number(r.amount) || 0;
  }
  return out;
}

// Aggregate raw budget rows into [fy][gl_code] -> 12-month array.
function shapeBudgetRows(rows) {
  const out = {};
  for (const r of rows) {
    const fy   = r.fiscal_year;
    const code = r.gl_code;
    const idx  = calToFiscalMonthIdx(Number(r.period));
    if (fy == null || code == null || isNaN(idx) || idx < 0 || idx > 11) continue;
    if (!out[fy])         out[fy] = {};
    if (!out[fy][code])   out[fy][code] = zeroMonths();
    out[fy][code][idx]   += Number(r.budget) || 0;
  }
  return out;
}

// ── Tooltip ───────────────────────────────────────────────────────────────
const ExpTip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{ background:"#0D1117", border:"1px solid #374151", borderRadius:8,
      padding:"10px 14px", fontSize:12, boxShadow:"0 4px 24px rgba(0,0,0,0.5)" }}>
      <div style={{ fontWeight:700, marginBottom:6, color:"#E5E7EB" }}>{label}</div>
      {[...payload].reverse().map(p => (
        <div key={p.dataKey} style={{ display:"flex", alignItems:"center", gap:8, marginBottom:3 }}>
          <span style={{ width:8, height:8, borderRadius:2, background:p.color, display:"inline-block" }} />
          <span style={{ color:"#9CA3AF" }}>{p.name||p.dataKey}:</span>
          <span style={{ fontWeight:600, color:"#F9FAFB" }}>{fmtFull(p.value)}</span>
        </div>
      ))}
    </div>
  );
};

// Shared "Operational only" toggle — renders nothing while the
// NON_OPERATIONAL_GLS list is still empty.
function OperationalToggle({ operationalOnly, setOperationalOnly }) {
  if (NON_OPERATIONAL_GLS.size === 0) return null;
  return (
    <div style={{ marginTop:15, display:"flex", flexDirection:"column", gap:5 }}>
      <div style={{ fontSize:10, color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.1em" }}>Budget view</div>
      <button
        onClick={() => setOperationalOnly(v => !v)}
        title={`Excludes non-operational GLs (${[...NON_OPERATIONAL_GLS].join(", ")}) from actual + budget`}
        style={{
          padding:"8px 12px",
          background: operationalOnly ? "#4FD1A022" : "#0D1117",
          border:`1px solid ${operationalOnly ? "#4FD1A0" : "#374151"}`,
          borderRadius:7,
          color: operationalOnly ? "#4FD1A0" : "#9CA3AF",
          fontSize:11, fontWeight:600, cursor:"pointer",
          display:"flex", alignItems:"center", gap:6, whiteSpace:"nowrap",
        }}>
        <span style={{
          width:12, height:12, borderRadius:3, flexShrink:0,
          border:`1.5px solid ${operationalOnly ? "#4FD1A0" : "#374151"}`,
          background: operationalOnly ? "#4FD1A0" : "transparent",
          display:"inline-flex", alignItems:"center", justifyContent:"center",
          color:"#0D1117", fontSize:9, fontWeight:700,
        }}>{operationalOnly ? "✓" : ""}</span>
        Operational only
      </button>
    </div>
  );
}

// Shared YTD-match toggle button.
function YtdToggle({ value, onToggle, label="Time scope", title }) {
  return (
    <div style={{ marginTop:15, display:"flex", flexDirection:"column", gap:5 }}>
      <div style={{ fontSize:10, color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.1em" }}>{label}</div>
      <button
        onClick={onToggle}
        title={title}
        style={{
          padding:"8px 12px",
          background: value ? "#4FD1A022" : "#0D1117",
          border:`1px solid ${value ? "#4FD1A0" : "#374151"}`,
          borderRadius:7,
          color: value ? "#4FD1A0" : "#9CA3AF",
          fontSize:11, fontWeight:600, cursor:"pointer",
          display:"flex", alignItems:"center", gap:6, whiteSpace:"nowrap",
        }}>
        <span style={{
          width:12, height:12, borderRadius:3, flexShrink:0,
          border:`1.5px solid ${value ? "#4FD1A0" : "#374151"}`,
          background: value ? "#4FD1A0" : "transparent",
          display:"inline-flex", alignItems:"center", justifyContent:"center",
          color:"#0D1117", fontSize:9, fontWeight:700,
        }}>{value ? "✓" : ""}</span>
        Match to current YTD
      </button>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// EXPENSES DASHBOARD
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
function ExpensesDashboard() {
  const { glAccounts: _rawGlAccounts, categories: CATEGORIES } = useSharedGlAccounts();
  const GL_ACCOUNTS = useMemo(
    () => _rawGlAccounts.filter(g => !EXPENSE_DASHBOARD_EXCLUDED.has(g.code)),
    [_rawGlAccounts]
  );

  const [fy, setFy]                 = useHashState("fy", DEFAULT_FY, FISCAL_YEARS);
  const [activeMonths, setActiveMonths] = useState(new Set(FY_MONTHS));
  const [activeGL, setActiveGL]     = useState(new Set());
  const [operationalOnly, setOperationalOnly] = useState(false);
  const [activeView, setActiveView] = useHashState("view", "monthly", ["monthly","gl","budget","yoy","transactions"]);
  const [txSearch, setTxSearch]     = useState("");
  const [yoyFyA, setYoyFyA]         = useState(DEFAULT_FY);
  const [yoyFyB, setYoyFyB]         = useState(DEFAULT_YOY_FYB);
  const [ytdMatch, setYtdMatch]     = useState(false);
  const [yoyYtdMatch, setYoyYtdMatch] = useState(false);

  // Once the async gl_accounts load resolves, seed the GL filter set to "all".
  const _glInitRef = useRef(false);
  useEffect(() => {
    if (!_glInitRef.current && GL_ACCOUNTS.length > 0) {
      setActiveGL(new Set(GL_ACCOUNTS.map(g => g.code)));
      _glInitRef.current = true;
    }
  }, [GL_ACCOUNTS]);

  const [expenseData, setExpenseData] = useState({});
  const [budgetData,  setBudgetData]  = useState({});
  const [rawExpenses, setRawExpenses] = useState([]);
  // qbo_id -> note for transactions marked one-time (expense_one_time table).
  const [oneTimeMap,  setOneTimeMap]  = useState(new Map());
  const [loading,     setLoading]     = useState(true);
  const [loadError,   setLoadError]   = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      fetchAllRows("expenses", EXPENSES_SELECT),
      fetchAllRows("budget",   "select=fiscal_year,gl_code,period,budget"),
      fetchAllRows("expense_one_time", "select=qbo_id,note"),
    ])
      .then(([expRows, budRows, otRows]) => {
        if (cancelled) return;
        setRawExpenses(expRows);
        setOneTimeMap(new Map(otRows.map(r => [r.qbo_id, r.note])));
        setExpenseData(shapeExpenseRows(expRows));
        setBudgetData(shapeBudgetRows(budRows));
        setLoading(false);
      })
      .catch(err => {
        if (cancelled) return;
        setLoadError(err.message || String(err));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const expSeries = (f, code) => expenseData[f]?.[code] || zeroMonths();
  const budSeries = (f, code) => budgetData[f]?.[code]  || zeroMonths();

  const handleGL = val => {
    if (val==="ALL")  { setActiveGL(new Set(GL_ACCOUNTS.map(g=>g.code))); return; }
    if (val==="NONE") { setActiveGL(new Set()); return; }
    setActiveGL(prev => { const n=new Set(prev); n.has(val)?n.delete(val):n.add(val); return n; });
  };
  const handleMonth = val => {
    if (val==="ALL")  { setActiveMonths(new Set(FY_MONTHS)); return; }
    if (val==="NONE") { setActiveMonths(new Set()); return; }
    setActiveMonths(prev => { const n=new Set(prev); n.has(val)?n.delete(val):n.add(val); return n; });
  };

  const activeMonthIdxs = useMemo(() => {
    if (ytdMatch) {
      const count = Math.min(Math.ceil(fyMonthsElapsed(fy)), 12);
      return Array.from({ length: count }, (_, i) => i);
    }
    return FY_MONTHS.map((m, i) => activeMonths.has(m) ? i : -1).filter(i => i >= 0);
  }, [activeMonths, ytdMatch, fy]);
  const numMonths = activeMonthIdxs.length;
  const rangeLabel = ytdMatch
    ? `YTD · ${numMonths} of 12 mo`
    : (numMonths === 12 ? "Full year" : `${numMonths} of 12 mo`);

  const selectedGL = useMemo(() =>
    GL_ACCOUNTS.filter(g =>
      activeGL.has(g.code) &&
      !(operationalOnly && NON_OPERATIONAL_GLS.has(g.code))
    ),
    [activeGL, operationalOnly, GL_ACCOUNTS]);

  // Monthly stacked by category. selectedGL already applies BOTH the GL
  // filter and the operational exclusion.
  const monthlyByCat = useMemo(() =>
    activeMonthIdxs.map(idx => {
      const m = FY_MONTHS[idx];
      const row = { month: m };
      CATEGORIES.forEach(cat => {
        row[cat] = selectedGL
          .filter(g => g.category===cat)
          .reduce((s,g) => s + expSeries(fy, g.code)[idx], 0);
      });
      row.total = selectedGL.reduce((s,g) => s + expSeries(fy, g.code)[idx], 0);
      return row;
    }), [fy, activeMonthIdxs, selectedGL, expenseData, CATEGORIES]);

  const glTotals = useMemo(() =>
    selectedGL.map(g => {
      const exp = expSeries(fy, g.code);
      const bud = budSeries(fy, g.code);
      let actual = 0, budget = 0;
      for (const idx of activeMonthIdxs) { actual += exp[idx]; budget += bud[idx]; }
      return { ...g, actual, budget };
    }).sort((a,b) => b.actual-a.actual),
    [fy, activeMonthIdxs, selectedGL, expenseData, budgetData]);

  const budgetActual = useMemo(() =>
    activeMonthIdxs.map(idx => {
      const m = FY_MONTHS[idx];
      const actual = selectedGL.reduce((s,g)=>s+expSeries(fy, g.code)[idx], 0);
      const budget = selectedGL.reduce((s,g)=>s+budSeries(fy, g.code)[idx], 0);
      return { month:m, Actual:actual, Budget:budget, variance:budget-actual };
    }), [fy, activeMonthIdxs, selectedGL, expenseData, budgetData]);

  // Transactions list. MUST depend on GL_ACCOUNTS — it arrives async, so on
  // first render it's [] and the memo would freeze empty without the dep.
  const dashGLCodes = useMemo(() => new Set(GL_ACCOUNTS.map(g => g.code)), [GL_ACCOUNTS]);
  const selectedGLCodes = useMemo(() => new Set(selectedGL.map(g => g.code)), [selectedGL]);
  const filteredTransactions = useMemo(() => {
    const q = txSearch.trim().toLowerCase();
    return rawExpenses
      .filter(r => r.fiscal_year === fy)
      .filter(r => dashGLCodes.has(r.gl_code))
      .filter(r => selectedGLCodes.has(r.gl_code))
      .filter(r => activeMonths.has(FY_MONTHS[r.fiscal_month_index]))
      .filter(r => !q || [r.vendor, r.memo, r.gl_name, r.gl_code].some(v => v && String(v).toLowerCase().includes(q)))
      .sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  }, [rawExpenses, fy, activeMonths, selectedGLCodes, dashGLCodes, txSearch]);

  // YoY with YTD-match: limit months to what's elapsed in the primary FY, and
  // day-prorate the baseline FY's partial current month.
  const yoyData = useMemo(() => {
    const yoyGLs = GL_ACCOUNTS.filter(g => !(operationalOnly && NON_OPERATIONAL_GLS.has(g.code)));
    let monthIdxs = FY_MONTHS.map((m, i) => activeMonths.has(m) ? i : -1).filter(i => i >= 0);
    let partialIdx = -1, partialFrac = 0;
    if (yoyYtdMatch) {
      const elapsed = fyMonthsElapsed(yoyFyA);
      const count = Math.min(Math.ceil(elapsed), 12);
      monthIdxs = Array.from({ length: count }, (_, i) => i);
      const fullMonths = Math.floor(elapsed);
      const frac = elapsed - fullMonths;
      if (frac > 0 && fullMonths < 12) { partialIdx = fullMonths; partialFrac = frac; }
    }
    return monthIdxs.map(i => {
      const m = FY_MONTHS[i];
      const row = { month: m };
      [yoyFyA, yoyFyB].forEach(f => {
        let val = yoyGLs.reduce((s,g)=>s+expSeries(f, g.code)[i],0);
        if (yoyYtdMatch && f === yoyFyB && i === partialIdx && partialFrac > 0) val *= partialFrac;
        row[f] = val;
      });
      row.delta = row[yoyFyA]-row[yoyFyB];
      return row;
    });
  }, [yoyFyA, yoyFyB, expenseData, operationalOnly, GL_ACCOUNTS, yoyYtdMatch, activeMonths]);

  const grandActual = glTotals.reduce((s,g)=>s+g.actual, 0);
  const grandBudget = glTotals.reduce((s,g)=>s+g.budget, 0);
  const grandVar    = varObj(grandActual, grandBudget);
  const yoyTotalA   = yoyData.reduce((s,r)=>s+r[yoyFyA], 0);
  const yoyTotalB   = yoyData.reduce((s,r)=>s+r[yoyFyB], 0);
  const yoyDelta    = yoyTotalA-yoyTotalB;
  const isYoyUp     = yoyDelta>=0;

  const glLabel = code => { const g=GL_ACCOUNTS.find(x=>x.code===code); return g?`${g.code} · ${g.name}`:code; };

  const VIEWS = [
    {id:"monthly",     label:"Monthly Spend"},
    {id:"gl",          label:"By GL"},
    {id:"budget",      label:"Budget vs Actual"},
    {id:"yoy",         label:"Year-over-Year"},
    {id:"transactions",label:"Transactions"},
  ];

  return (
    <div className="dash-root" style={{ fontFamily:"'DM Sans','Segoe UI',sans-serif", background:"#0D1117", minHeight:"100vh", color:"#E5E7EB", padding:"28px 32px" }}>

      {/* Header */}
      <div className="dash-header" style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:22 }}>
        <div>
          <div style={{ fontSize:10, fontWeight:600, letterSpacing:"0.12em", color:"#6B7280", textTransform:"uppercase", marginBottom:4 }}>
            The Woods · Camp Finance
          </div>
          <h1 style={{ margin:0, fontSize:24, fontWeight:700, color:"#F9FAFB", letterSpacing:"-0.02em" }}>
            Expense Dashboard
          </h1>
        </div>
        <FreshnessBadge loading={loading} loadError={loadError} />
      </div>

      {/* Filter Bar */}
      {activeView !== "yoy" && (
        <div style={{ display:"flex", flexWrap:"wrap", gap:12, alignItems:"flex-end", background:"#161B22", border:"1px solid #21262D", borderRadius:10, padding:"14px 18px", marginBottom:20 }}>
          <Dropdown label="Fiscal Year" value={fy} options={FISCAL_YEARS} onChange={setFy} minWidth={110} />
          {!ytdMatch && (
            <Dropdown label="Months" multi options={FY_MONTHS} selectedSet={activeMonths} onChange={handleMonth} minWidth={140} />
          )}
          <Dropdown label="GL Account" multi options={GL_ACCOUNTS.map(g=>g.code)} selectedSet={activeGL}
            onChange={handleGL} getLabel={glLabel} minWidth={210}
            colorMap={Object.fromEntries(GL_ACCOUNTS.map(g=>[g.code, catColor(g.category)]))} />
          <YtdToggle value={ytdMatch} onToggle={() => setYtdMatch(v => !v)}
            title="Auto-limits months to what's elapsed in the selected fiscal year" />
          <OperationalToggle operationalOnly={operationalOnly} setOperationalOnly={setOperationalOnly} />
          <button
            onClick={() => { setFy(DEFAULT_FY); setActiveMonths(new Set(FY_MONTHS)); setActiveGL(new Set(GL_ACCOUNTS.map(g=>g.code))); setOperationalOnly(false); setYtdMatch(false); }}
            style={{ padding:"8px 12px", background:"transparent", border:"1px solid #374151", borderRadius:7, color:"#6B7280", fontSize:11, fontWeight:600, cursor:"pointer", marginTop:15 }}
            onMouseEnter={e=>{e.currentTarget.style.color="#9CA3AF";e.currentTarget.style.borderColor="#9CA3AF";}}
            onMouseLeave={e=>{e.currentTarget.style.color="#6B7280";e.currentTarget.style.borderColor="#374151";}}>
            Reset
          </button>
          <div style={{ marginLeft:"auto", marginTop:15, fontSize:11, color:"#4B5563" }}>
            {fy} · {rangeLabel} · {selectedGL.length} GL{selectedGL.length!==1?"s":""}
          </div>
        </div>
      )}

      {/* YoY Filter */}
      {activeView === "yoy" && (
        <div style={{ display:"flex", flexWrap:"wrap", gap:14, alignItems:"flex-end", background:"#161B22", border:"1px solid #21262D", borderRadius:10, padding:"14px 18px", marginBottom:20 }}>
          <Dropdown label="Compare (Primary)" value={yoyFyA} options={FISCAL_YEARS} onChange={setYoyFyA} minWidth={140} />
          <Dropdown label="vs. (Baseline)"    value={yoyFyB} options={FISCAL_YEARS} onChange={setYoyFyB} minWidth={140} />
          {!yoyYtdMatch && (
            <Dropdown label="Months" multi options={FY_MONTHS} selectedSet={activeMonths} onChange={handleMonth} minWidth={140} />
          )}
          <OperationalToggle operationalOnly={operationalOnly} setOperationalOnly={setOperationalOnly} />
          <YtdToggle value={yoyYtdMatch} onToggle={() => setYoyYtdMatch(v => !v)} label="Comparison"
            title="Limits both years to the months elapsed in the primary FY, with the baseline's partial current month prorated by day — apples-to-apples YTD" />
          <div style={{ marginLeft:"auto", marginTop:15, fontSize:11, color:"#4B5563" }}>{yoyFyA} vs {yoyFyB} · {yoyYtdMatch ? "YTD (day-matched)" : (yoyData.length === 12 ? "Full Year" : `${yoyData.length} of 12 mo`)}</div>
        </div>
      )}

      {/* KPIs */}
      {activeView !== "yoy" && (
        <div className="kpi-grid" style={{ display:"grid", gridTemplateColumns:"repeat(3,1fr)", gap:12, marginBottom:20 }}>
          {[
            { label:"Total Expenses", value:fmt(grandActual), sub:`${fy} · ${rangeLabel}` },
            { label:"Budget",         value:fmt(grandBudget), sub:"selected period" },
            { label:"Variance",
              value:`${grandVar.favorable?"+":"-"}${fmt(Math.abs(grandVar.val))}`,
              sub:`${grandVar.favorable?"Under":"Over"} budget · ${grandVar.pct}%`,
              color:grandVar.favorable?"#4FD1A0":"#F87171" },
          ].map(k=>(
            <div key={k.label} style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:10, padding:"14px 16px" }}>
              <div style={{ fontSize:10, color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.1em", marginBottom:5 }}>{k.label}</div>
              <div style={{ fontSize:22, fontWeight:700, color:k.color||"#F9FAFB", letterSpacing:"-0.02em" }}>{k.value}</div>
              <div style={{ fontSize:11, color:"#4B5563", marginTop:2 }}>{k.sub}</div>
            </div>
          ))}
        </div>
      )}

      {activeView === "yoy" && (
        <div className="kpi-grid" style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:12, marginBottom:20 }}>
          {[
            { label:`${yoyFyA} Total`, value:fmt(yoyTotalA), color:YOY_COLORS[yoyFyA] },
            { label:`${yoyFyB} Total`, value:fmt(yoyTotalB), color:YOY_COLORS[yoyFyB] },
            { label:"$ Change",  value:`${isYoyUp?"+":""}${fmt(yoyDelta)}`, color:isYoyUp?"#F87171":"#4FD1A0", sub:"↑ = unfavorable" },
            { label:"% Change",  value:pct(yoyTotalA,yoyTotalB), color:isYoyUp?"#F87171":"#4FD1A0" },
          ].map(k=>(
            <div key={k.label} style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:10, padding:"14px 16px" }}>
              <div style={{ fontSize:10, color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.1em", marginBottom:5 }}>{k.label}</div>
              <div style={{ fontSize:22, fontWeight:700, color:k.color||"#F9FAFB", letterSpacing:"-0.02em" }}>{k.value}</div>
              {k.sub && <div style={{ fontSize:10, color:"#4B5563", marginTop:2 }}>{k.sub}</div>}
            </div>
          ))}
        </div>
      )}

      {/* Tabs */}
      <div style={{ display:"flex", gap:3, marginBottom:14, background:"#161B22", borderRadius:8, padding:4, width:"fit-content", border:"1px solid #21262D" }}>
        {VIEWS.map(t=>(
          <button key={t.id} onClick={()=>setActiveView(t.id)} style={{
            padding:"6px 13px", borderRadius:6, border:"none",
            background:activeView===t.id?"#21262D":"transparent",
            color:activeView===t.id?"#F9FAFB":"#6B7280",
            fontSize:11, fontWeight:600, cursor:"pointer", transition:"all 0.15s",
          }}>{t.label}</button>
        ))}
      </div>

      {/* Chart Panel */}
      <div style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:12, padding:"18px 14px", marginBottom:20 }}>

        {/* Monthly Spend */}
        {activeView === "monthly" && (
          <>
            <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF", marginBottom:12 }}>
              Total Expense by Month — Stacked by Category · {fy} · {rangeLabel}
            </div>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={monthlyByCat} barSize={26} margin={{top:4,right:8,left:8,bottom:0}}>
                <CartesianGrid strokeDasharray="3 3" stroke="#21262D" vertical={false} />
                <XAxis dataKey="month" tick={{fill:"#6B7280",fontSize:11}} axisLine={false} tickLine={false} />
                <YAxis tickFormatter={fmt} tick={{fill:"#6B7280",fontSize:10}} axisLine={false} tickLine={false} width={52} />
                <Tooltip content={<ExpTip />} cursor={{fill:"#ffffff05"}} />
                <Legend wrapperStyle={{paddingTop:12,fontSize:10}} formatter={v=><span style={{color:"#9CA3AF"}}>{v}</span>} />
                {CATEGORIES.map((cat,i,arr)=>(
                  <Bar key={cat} dataKey={cat} stackId="a" fill={catColor(cat)}
                    radius={i===arr.length-1?[4,4,0,0]:[0,0,0,0]} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </>
        )}

        {/* By GL */}
        {activeView === "gl" && (
          <>
            <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF", marginBottom:4 }}>
              Expense by GL Account · {fy} · {rangeLabel}
              <span style={{ color:"#4B5563", fontWeight:400, marginLeft:8 }}>({glTotals.length} accounts)</span>
            </div>
            <div style={{ overflowX:"auto", overflowY:"auto", maxHeight:620 }}>
              <table style={{ width:"100%", borderCollapse:"collapse", fontSize:11 }}>
                <thead style={{ position:"sticky", top:0, background:"#0D1117", zIndex:10 }}>
                  <tr>
                    {["Code","Account","Category","Actual","Budget","$ Var","% Var"].map((h,i)=>(
                      <th key={h} style={{ padding:"8px 12px", textAlign:i<3?"left":"right",
                        color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.07em", fontSize:10,
                        borderBottom:"1px solid #374151" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {glTotals.map((g,i)=>{
                    const v=varObj(g.actual,g.budget);
                    return (
                      <tr key={g.code} style={{ borderTop:"1px solid #21262D", background:i%2===0?"transparent":"#0D111740" }}>
                        <td style={{ padding:"7px 12px", color:"#6B7280", fontWeight:600, whiteSpace:"nowrap" }}>{g.code}</td>
                        <td style={{ padding:"7px 12px", color:"#D1D5DB" }}>{g.name}</td>
                        <td style={{ padding:"7px 12px" }}>
                          <span style={{ display:"inline-flex", alignItems:"center", gap:5 }}>
                            <span style={{ width:7, height:7, borderRadius:"50%", background:catColor(g.category), flexShrink:0 }} />
                            <span style={{ color:catColor(g.category), fontSize:10, fontWeight:600 }}>{g.category}</span>
                          </span>
                        </td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:"#D1D5DB" }}>{fmtFull(g.actual)}</td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:"#6B7280" }}>{fmtFull(g.budget)}</td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:v.favorable?"#4FD1A0":"#F87171", fontWeight:600 }}>
                          {v.favorable?"+":"-"}{fmtFull(Math.abs(v.val))}
                        </td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:v.favorable?"#4FD1A0":"#F87171", fontWeight:600 }}>
                          {v.favorable?"+":"-"}{v.pct}%
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}

        {/* Budget vs Actual */}
        {activeView === "budget" && (
          <>
            <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF", marginBottom:12 }}>
              Budget vs Actual by Month · {fy} · {rangeLabel}
            </div>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={budgetActual} barGap={3} barCategoryGap="30%" margin={{top:4,right:8,left:8,bottom:0}}>
                <CartesianGrid strokeDasharray="3 3" stroke="#21262D" vertical={false} />
                <XAxis dataKey="month" tick={{fill:"#6B7280",fontSize:11}} axisLine={false} tickLine={false} />
                <YAxis tickFormatter={fmt} tick={{fill:"#6B7280",fontSize:10}} axisLine={false} tickLine={false} width={52} />
                <Tooltip content={<ExpTip />} cursor={{fill:"#ffffff05"}} />
                <Legend wrapperStyle={{paddingTop:12,fontSize:11}} formatter={v=><span style={{color:"#9CA3AF"}}>{v}</span>} />
                <Bar dataKey="Actual" fill="#4FD1A0" radius={[3,3,0,0]} />
                <Bar dataKey="Budget" fill="#374151" radius={[3,3,0,0]} />
              </BarChart>
            </ResponsiveContainer>

            <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF", marginTop:18, marginBottom:10 }}>
              Monthly Variance (Budget − Actual) · Green = Under Budget
            </div>
            <ResponsiveContainer width="100%" height={100}>
              <BarChart data={budgetActual} barSize={18} margin={{top:4,right:8,left:8,bottom:0}}>
                <CartesianGrid strokeDasharray="3 3" stroke="#21262D" vertical={false} />
                <XAxis dataKey="month" tick={{fill:"#6B7280",fontSize:11}} axisLine={false} tickLine={false} />
                <YAxis tickFormatter={fmt} tick={{fill:"#6B7280",fontSize:10}} axisLine={false} tickLine={false} width={52} />
                <ReferenceLine y={0} stroke="#374151" />
                <Tooltip formatter={v=>[fmtFull(v),"Variance"]} contentStyle={{background:"#0D1117",border:"1px solid #374151",borderRadius:6,fontSize:11}} />
                <Bar dataKey="variance" radius={[3,3,0,0]}>
                  {budgetActual.map((e,i)=><Cell key={i} fill={e.variance>=0?"#4FD1A0":"#F87171"} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>

            <div style={{ marginTop:20, overflowX:"auto" }}>
              <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF", marginBottom:10 }}>
                GL Summary — Budget vs Actual
                <span style={{ color:"#4B5563", fontWeight:400, marginLeft:8 }}>({glTotals.length} account{glTotals.length!==1?"s":""})</span>
              </div>
              <table style={{ width:"100%", borderCollapse:"collapse", fontSize:11 }}>
                <thead>
                  <tr style={{ borderBottom:"1px solid #374151" }}>
                    {["GL Code","Account","Actual","Budget","$ Var","% Var"].map((h,i)=>(
                      <th key={h} style={{ padding:"7px 12px", textAlign:i<=1?"left":"right",
                        color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.07em", fontSize:10 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {glTotals.map((g,i)=>{
                    const v = varObj(g.actual, g.budget);
                    return (
                      <tr key={g.code} style={{ borderTop:"1px solid #21262D", background:i%2===0?"transparent":"#0D111740" }}>
                        <td style={{ padding:"7px 12px", color:"#6B7280", fontFamily:"ui-monospace, SFMono-Regular, Menlo, monospace" }}>{g.code}</td>
                        <td style={{ padding:"7px 12px" }}>
                          <span style={{ display:"inline-flex", alignItems:"center", gap:7 }}>
                            <span title={g.category} style={{ width:8, height:8, borderRadius:"50%", background:catColor(g.category), flexShrink:0 }} />
                            <span style={{ color:"#D1D5DB" }}>{g.name}</span>
                          </span>
                        </td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:"#D1D5DB" }}>{fmtFull(g.actual)}</td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:"#6B7280" }}>{fmtFull(g.budget)}</td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:v.favorable?"#4FD1A0":"#F87171", fontWeight:600 }}>
                          {v.favorable?"+":"-"}{fmtFull(Math.abs(v.val))}
                        </td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:v.favorable?"#4FD1A0":"#F87171", fontWeight:600 }}>
                          {v.favorable?"+":"-"}{v.pct}%
                        </td>
                      </tr>
                    );
                  })}
                  <tr style={{ borderTop:"2px solid #374151", background:"#0D1117" }}>
                    <td style={{ padding:"8px 12px", color:"#F9FAFB", fontWeight:700 }} colSpan={2}>TOTAL</td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:"#F9FAFB", fontWeight:700 }}>{fmtFull(grandActual)}</td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:"#F9FAFB", fontWeight:700 }}>{fmtFull(grandBudget)}</td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:grandVar.favorable?"#4FD1A0":"#F87171", fontWeight:700 }}>
                      {grandVar.favorable?"+":"-"}{fmtFull(Math.abs(grandVar.val))}
                    </td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:grandVar.favorable?"#4FD1A0":"#F87171", fontWeight:700 }}>
                      {grandVar.favorable?"+":"-"}{grandVar.pct}%
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </>
        )}

        {/* YoY */}
        {activeView === "yoy" && (
          <>
            <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF", marginBottom:12 }}>
              Total Expenses — {yoyFyA} vs {yoyFyB} · {yoyYtdMatch ? "YTD (day-matched)" : (yoyData.length === 12 ? "Full Year" : `${yoyData.length} of 12 mo`)}
            </div>
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={yoyData} barGap={3} barCategoryGap="30%" margin={{top:4,right:8,left:8,bottom:0}}>
                <CartesianGrid strokeDasharray="3 3" stroke="#21262D" vertical={false} />
                <XAxis dataKey="month" tick={{fill:"#6B7280",fontSize:11}} axisLine={false} tickLine={false} />
                <YAxis tickFormatter={fmt} tick={{fill:"#6B7280",fontSize:10}} axisLine={false} tickLine={false} width={52} />
                <Tooltip content={<ExpTip />} cursor={{fill:"#ffffff05"}} />
                <Legend wrapperStyle={{paddingTop:12,fontSize:11}} formatter={v=><span style={{color:"#9CA3AF"}}>{v}</span>} />
                <Bar dataKey={yoyFyA} fill={YOY_COLORS[yoyFyA]} radius={[3,3,0,0]} />
                <Bar dataKey={yoyFyB} fill={YOY_COLORS[yoyFyB]} radius={[3,3,0,0]} opacity={0.55} />
              </BarChart>
            </ResponsiveContainer>

            <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF", marginTop:18, marginBottom:10 }}>
              Monthly Δ · Red = Expenses Increased (Unfavorable)
            </div>
            <ResponsiveContainer width="100%" height={100}>
              <BarChart data={yoyData} barSize={18} margin={{top:4,right:8,left:8,bottom:0}}>
                <CartesianGrid strokeDasharray="3 3" stroke="#21262D" vertical={false} />
                <XAxis dataKey="month" tick={{fill:"#6B7280",fontSize:11}} axisLine={false} tickLine={false} />
                <YAxis tickFormatter={fmt} tick={{fill:"#6B7280",fontSize:10}} axisLine={false} tickLine={false} width={52} />
                <ReferenceLine y={0} stroke="#374151" />
                <Tooltip formatter={v=>[fmtFull(v),"Δ"]} contentStyle={{background:"#0D1117",border:"1px solid #374151",borderRadius:6,fontSize:11}} />
                <Bar dataKey="delta" radius={[3,3,0,0]}>
                  {yoyData.map((e,i)=><Cell key={i} fill={e.delta>=0?"#F87171":"#4FD1A0"} />)}
                </Bar>
              </BarChart>
            </ResponsiveContainer>

            <div style={{ marginTop:20, overflowX:"auto" }}>
              <table style={{ width:"100%", borderCollapse:"collapse", fontSize:11 }}>
                <thead>
                  <tr style={{ borderBottom:"1px solid #374151" }}>
                    {["Month",yoyFyA,yoyFyB,"$ Change","% Change"].map((h,i)=>(
                      <th key={h} style={{ padding:"7px 12px", textAlign:i===0?"left":"right",
                        color:i===1?YOY_COLORS[yoyFyA]:i===2?YOY_COLORS[yoyFyB]:"#6B7280",
                        fontWeight:600, textTransform:"uppercase", letterSpacing:"0.07em", fontSize:10 }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {yoyData.map((row,i)=>{
                    const up=row.delta>=0;
                    return (
                      <tr key={row.month} style={{ borderTop:"1px solid #21262D", background:i%2===0?"transparent":"#0D111740" }}>
                        <td style={{ padding:"7px 12px", color:"#9CA3AF", fontWeight:600 }}>{row.month}</td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:"#D1D5DB" }}>{fmtFull(row[yoyFyA])}</td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:"#6B7280" }}>{fmtFull(row[yoyFyB])}</td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:up?"#F87171":"#4FD1A0", fontWeight:600 }}>
                          {up?"+":""}{fmtFull(row.delta)}
                        </td>
                        <td style={{ padding:"7px 12px", textAlign:"right", color:up?"#F87171":"#4FD1A0", fontWeight:600 }}>
                          {pct(row[yoyFyA],row[yoyFyB])}
                        </td>
                      </tr>
                    );
                  })}
                  <tr style={{ borderTop:"2px solid #374151", background:"#0D1117" }}>
                    <td style={{ padding:"8px 12px", color:"#F9FAFB", fontWeight:700 }}>TOTAL</td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:YOY_COLORS[yoyFyA], fontWeight:700 }}>{fmtFull(yoyTotalA)}</td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:YOY_COLORS[yoyFyB], fontWeight:700 }}>{fmtFull(yoyTotalB)}</td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:isYoyUp?"#F87171":"#4FD1A0", fontWeight:700 }}>
                      {isYoyUp?"+":""}{fmtFull(yoyDelta)}
                    </td>
                    <td style={{ padding:"8px 12px", textAlign:"right", color:isYoyUp?"#F87171":"#4FD1A0", fontWeight:700 }}>
                      {pct(yoyTotalA,yoyTotalB)}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </>
        )}

        {/* Transactions */}
        {activeView === "transactions" && (
          <>
            <div style={{ display:"flex", alignItems:"center", gap:12, flexWrap:"wrap", marginBottom:8 }}>
              <div style={{ fontSize:11, fontWeight:600, color:"#9CA3AF" }}>
                Transactions · {fy} · {rangeLabel}
                <span style={{ color:"#4B5563", fontWeight:400, marginLeft:8 }}>
                  ({filteredTransactions.length.toLocaleString()} matching · {filteredTransactions.length > 500 ? "showing top 500 by date" : "showing all"})
                </span>
              </div>
              <input
                type="text"
                value={txSearch}
                onChange={e => setTxSearch(e.target.value)}
                placeholder="Search vendor, memo, GL…"
                style={{
                  marginLeft:"auto", width:240, padding:"6px 10px",
                  background:"#0D1117", border:"1px solid #374151", borderRadius:6,
                  color:"#E5E7EB", fontSize:12, outline:"none",
                }}
                onFocus={e => e.currentTarget.style.borderColor = "#4FD1A0"}
                onBlur={e => e.currentTarget.style.borderColor = "#374151"}
              />
              {txSearch && (
                <button onClick={() => setTxSearch("")}
                  style={{ padding:"6px 10px", background:"transparent", border:"1px solid #374151",
                    borderRadius:6, color:"#6B7280", fontSize:11, fontWeight:600, cursor:"pointer" }}>
                  Clear
                </button>
              )}
            </div>
            <div style={{ overflowX:"auto", overflowY:"auto", maxHeight:620 }}>
              <table style={{ width:"100%", borderCollapse:"collapse", fontSize:11 }}>
                <thead style={{ position:"sticky", top:0, background:"#0D1117", zIndex:10 }}>
                  <tr>
                    {["Date","Vendor","Memo","Code","Account","Category","Amount"].map((h,i)=>(
                      <th key={h} style={{ padding:"8px 12px", textAlign:i===6?"right":"left",
                        color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.07em", fontSize:10,
                        borderBottom:"1px solid #374151" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {filteredTransactions.slice(0, 500).map((r, i) => (
                    <tr key={r.id} style={{ borderTop:"1px solid #21262D", background:i%2===0?"transparent":"#0D111740" }}>
                      <td style={{ padding:"7px 12px", color:"#9CA3AF", whiteSpace:"nowrap" }}>{r.date}</td>
                      <td style={{ padding:"7px 12px", color:"#D1D5DB" }}>
                        {r.vendor || "—"}
                        {oneTimeMap.has(r.qbo_id) && (
                          <span title={oneTimeMap.get(r.qbo_id) || "Marked one-time — excluded from Budget Pacing forecast extrapolation"}
                            style={{ marginLeft:6, padding:"1px 5px", background:"#C084FC22", border:"1px solid #C084FC44",
                              borderRadius:3, fontSize:8, color:"#C084FC", fontWeight:700, whiteSpace:"nowrap" }}>
                            1×
                          </span>
                        )}
                      </td>
                      <td title={r.memo || ""} style={{ padding:"7px 12px", color:"#9CA3AF", maxWidth:260,
                        overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>{r.memo || "—"}</td>
                      <td style={{ padding:"7px 12px", color:"#6B7280", fontWeight:600, whiteSpace:"nowrap" }}>{r.gl_code}</td>
                      <td style={{ padding:"7px 12px", color:"#D1D5DB" }}>{r.gl_name}</td>
                      <td style={{ padding:"7px 12px" }}>
                        <span style={{ display:"inline-flex", alignItems:"center", gap:5 }}>
                          <span style={{ width:7, height:7, borderRadius:"50%", background:catColor(r.category), flexShrink:0 }} />
                          <span style={{ color:catColor(r.category), fontSize:10, fontWeight:600 }}>{r.category}</span>
                        </span>
                      </td>
                      <td style={{ padding:"7px 12px", textAlign:"right", color:"#D1D5DB", whiteSpace:"nowrap" }}>{fmtFull(r.amount)}</td>
                    </tr>
                  ))}
                  {filteredTransactions.length === 0 && (
                    <tr><td colSpan={7} style={{ padding:"20px 12px", textAlign:"center", color:"#4B5563" }}>
                      No transactions match the current filters.
                    </td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      <div style={{ marginTop:16, fontSize:10, color:"#374151", textAlign:"center" }}>
        The Woods · Data syncs nightly from QuickBooks Online
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// BUDGET PACING DASHBOARD
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Sortable GL table with budget, YTD spend, % consumed, projected annual at
// current pace, and forecast variance. Projection uses prior-year monthly
// pattern scaled to current YTD (confidence-weighted, day-prorated window);
// falls back to linear when no prior-year history exists.
function BudgetPacingDashboard() {
  const { glAccounts: _rawGlAccounts } = useSharedGlAccounts();
  const GL_ACCOUNTS = useMemo(
    () => _rawGlAccounts.filter(g => !EXPENSE_DASHBOARD_EXCLUDED.has(g.code)),
    [_rawGlAccounts]
  );

  const [fy, setFy]                 = useHashState("fy", DEFAULT_FY, FISCAL_YEARS);
  const [activeGL, setActiveGL]     = useState(new Set());

  const _glInitRef = useRef(false);
  useEffect(() => {
    if (!_glInitRef.current && GL_ACCOUNTS.length > 0) {
      setActiveGL(new Set(GL_ACCOUNTS.map(g => g.code)));
      _glInitRef.current = true;
    }
  }, [GL_ACCOUNTS]);
  const [operationalOnly, setOperationalOnly] = useState(false);
  const [activeView, setActiveView] = useHashState("view", "forecast", ["forecast","ytd"]);
  const [sortBy, setSortBy]         = useState("varSign");
  const [sortDir, setSortDir]       = useState("desc");

  const [expenseData, setExpenseData] = useState({});
  const [budgetData,  setBudgetData]  = useState({});
  // Same [fy][gl][month] shape, but only transactions marked one-time.
  const [oneTimeData, setOneTimeData] = useState({});
  const [loading,     setLoading]     = useState(true);
  const [loadError,   setLoadError]   = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      fetchAllRows("expenses", EXPENSES_SELECT),
      fetchAllRows("budget",   "select=fiscal_year,gl_code,period,budget"),
      fetchAllRows("expense_one_time", "select=qbo_id"),
    ])
      .then(([expRows, budRows, otRows]) => {
        if (cancelled) return;
        const otIds = new Set(otRows.map(r => r.qbo_id));
        setExpenseData(shapeExpenseRows(expRows));
        setOneTimeData(shapeExpenseRows(expRows.filter(r => otIds.has(r.qbo_id))));
        setBudgetData(shapeBudgetRows(budRows));
        setLoading(false);
      })
      .catch(err => {
        if (cancelled) return;
        setLoadError(err.message || String(err));
        setLoading(false);
      });
    return () => { cancelled = true; };
  }, []);

  const expSeries = (f, code) => expenseData[f]?.[code] || zeroMonths();
  const budSeries = (f, code) => budgetData[f]?.[code]  || zeroMonths();
  const otSeries  = (f, code) => oneTimeData[f]?.[code] || zeroMonths();

  const handleGL = val => {
    if (val==="ALL")  { setActiveGL(new Set(GL_ACCOUNTS.map(g=>g.code))); return; }
    if (val==="NONE") { setActiveGL(new Set()); return; }
    setActiveGL(prev => { const n=new Set(prev); n.has(val)?n.delete(val):n.add(val); return n; });
  };

  const selectedGL = useMemo(() =>
    GL_ACCOUNTS.filter(g =>
      activeGL.has(g.code) &&
      !(operationalOnly && NON_OPERATIONAL_GLS.has(g.code))
    ),
    [activeGL, operationalOnly, GL_ACCOUNTS]);

  const monthsElapsed = useMemo(() => fyMonthsElapsed(fy), [fy]);
  const elapsedPct = (monthsElapsed / 12) * 100;
  const prevFY = previousFiscalYear(fy);

  // Whole months that have any elapsed time — matches what the Expenses
  // dashboard sums when all months are selected, so the two reconcile.
  const monthsIncluded = Math.min(Math.ceil(monthsElapsed), 12);

  const projectAnnual = (actualYTD, prevYearMonthly) => {
    if (monthsElapsed <= 0) return { value: 0, method: "none" };
    const linear = (actualYTD / monthsElapsed) * 12;
    if (prevYearMonthly) {
      // Day-prorated prior-year window: full elapsed months plus the same
      // fraction of the current month.
      const fullMonths = Math.min(Math.floor(monthsElapsed), 12);
      const frac = monthsElapsed - fullMonths;
      let prevYTD = 0;
      for (let i = 0; i < fullMonths; i++) prevYTD += prevYearMonthly[i];
      if (fullMonths < 12) prevYTD += prevYearMonthly[fullMonths] * frac;
      const prevAnnual = prevYearMonthly.reduce((a, b) => a + b, 0);
      if (prevYTD > 0 && prevAnnual > 0) {
        // Confidence-weighted blend: the prior-year-shape ratio is only
        // trustworthy in proportion to how much of last year's annual spend
        // had actually happened by this point in the year.
        const priorShare   = prevYTD / prevAnnual;
        const elapsedShare = monthsElapsed / 12;
        const w = Math.min(1, priorShare / elapsedShare);
        const ratioProj = (actualYTD / prevYTD) * prevAnnual;
        return {
          value: w * ratioProj + (1 - w) * linear,
          method: w >= 0.95 ? "prior" : "blend",
        };
      }
    }
    return { value: linear, method: "linear" };
  };

  const expectedYTDFn = (budgetAnnual, prevYearMonthly) => {
    if (monthsElapsed <= 0 || budgetAnnual <= 0) return { value: 0, method: "none" };
    if (prevYearMonthly) {
      const prevAnnual = prevYearMonthly.reduce((a, b) => a + b, 0);
      if (prevAnnual > 0) {
        const fullMonths = Math.floor(monthsElapsed);
        const frac = monthsElapsed - fullMonths;
        let cumFraction = 0;
        for (let i = 0; i < fullMonths && i < 12; i++) cumFraction += prevYearMonthly[i] / prevAnnual;
        if (fullMonths < 12) cumFraction += (prevYearMonthly[fullMonths] / prevAnnual) * frac;
        return { value: budgetAnnual * cumFraction, method: "prior" };
      }
    }
    return { value: (budgetAnnual * monthsElapsed) / 12, method: "linear" };
  };

  const rowsByGL = useMemo(() => {
    return selectedGL.map(g => {
      const exp = expSeries(fy, g.code);
      const bud = budSeries(fy, g.code);
      const ot     = otSeries(fy, g.code);
      const otPrev = otSeries(prevFY, g.code);
      // Recurring prior-year series: one-time transactions removed so they
      // don't shape the seasonal curve or inflate the ratio base.
      const prev = expSeries(prevFY, g.code).map((v, i) => v - otPrev[i]);
      let actualYTD = 0, oneTimeYTD = 0, budgetAnnual = 0, prevSum = 0;
      for (let i = 0; i < monthsIncluded; i++) { actualYTD += exp[i]; oneTimeYTD += ot[i]; }
      for (let i = 0; i < 12; i++) { budgetAnnual += bud[i]; prevSum += prev[i]; }
      // Forecast on RECURRING spend only, then add this year's one-times back
      // flat — they happened and land in the annual total, but must not be
      // extrapolated across remaining months.
      const proj  = projectAnnual(actualYTD - oneTimeYTD, prevSum > 0 ? prev : null);
      proj.value += oneTimeYTD;
      const exp_  = expectedYTDFn(budgetAnnual, prevSum > 0 ? prev : null);
      const projectedVar = budgetAnnual - proj.value;
      const pctConsumed = budgetAnnual > 0 ? (actualYTD / budgetAnnual) * 100 : 0;
      const varPct = budgetAnnual > 0 ? (projectedVar / budgetAnnual) * 100 : 0;
      const ytdVar = actualYTD - exp_.value;
      const ytdVarPct = exp_.value > 0 ? (ytdVar / exp_.value) * 100 : 0;
      return {
        key: g.code, label: `${g.code} · ${g.name}`, category: g.category,
        actualYTD, budgetAnnual, remaining: budgetAnnual - actualYTD,
        projectedAnnual: proj.value, projectedMethod: proj.method,
        projectedVar, varPct, pctConsumed,
        expectedYTD: exp_.value, expectedMethod: exp_.method,
        ytdVar, ytdVarPct,
      };
    }).filter(r => r.budgetAnnual > 0 || r.actualYTD > 0);
  }, [fy, prevFY, selectedGL, expenseData, budgetData, oneTimeData, monthsElapsed]);

  const rows = useMemo(() => {
    const sorters = {
      label:      (a,b) => a.label.localeCompare(b.label),
      budget:     (a,b) => a.budgetAnnual - b.budgetAnnual,
      ytd:        (a,b) => a.actualYTD - b.actualYTD,
      pct:        (a,b) => a.pctConsumed - b.pctConsumed,
      varAbs:     (a,b) => Math.abs(a.projectedVar) - Math.abs(b.projectedVar),
      varSign:    (a,b) => a.projectedVar - b.projectedVar,
      expected:   (a,b) => a.expectedYTD - b.expectedYTD,
      ytdVarAbs:  (a,b) => Math.abs(a.ytdVar) - Math.abs(b.ytdVar),
      ytdVarSign: (a,b) => a.ytdVar - b.ytdVar,
    };
    const fn = sorters[sortBy] || sorters.varSign;
    const sorted = [...rowsByGL].sort(fn);
    return sortDir === "desc" ? sorted.reverse() : sorted;
  }, [rowsByGL, sortBy, sortDir]);

  const toggleSort = (col) => {
    if (sortBy === col) setSortDir(d => d === "asc" ? "desc" : "asc");
    else { setSortBy(col); setSortDir("desc"); }
  };
  const sortArrow = (col) => sortBy === col ? (sortDir === "desc" ? " ▼" : " ▲") : "";

  const totalBudget      = rows.reduce((s,r) => s + r.budgetAnnual, 0);
  const totalYTD         = rows.reduce((s,r) => s + r.actualYTD, 0);
  const totalProjected   = rows.reduce((s,r) => s + r.projectedAnnual, 0);
  const totalExpectedYTD = rows.reduce((s,r) => s + r.expectedYTD, 0);
  const totalVar         = totalBudget - totalProjected;
  const totalYTDVar      = totalYTD - totalExpectedYTD;
  const totalPctConsumed = totalBudget > 0 ? (totalYTD / totalBudget) * 100 : 0;
  const totalYTDVarPct   = totalExpectedYTD > 0 ? (totalYTDVar / totalExpectedYTD) * 100 : 0;

  const statusFor = (r) => {
    if (r.budgetAnnual === 0) return { label: "N/A",     color: "#6B7280" };
    if (monthsElapsed === 0)  return { label: "PENDING", color: "#6B7280" };
    if (activeView === "ytd") {
      const pct = r.ytdVarPct;
      if (pct > 10) return { label: "OVER PACE",  color: "#F87171" };
      if (pct > 0)  return { label: "AHEAD",      color: "#F7CF4F" };
      return         { label: "ON PACE",   color: "#4FD1A0" };
    }
    const pct = r.varPct;
    if (pct < -5) return { label: "OVER",      color: "#F87171" };
    if (pct < 0)  return { label: "AT RISK",   color: "#F7CF4F" };
    return         { label: "ON TRACK", color: "#4FD1A0" };
  };

  return (
    <div className="dash-root" style={{ fontFamily:"'DM Sans','Segoe UI',sans-serif", background:"#0D1117", minHeight:"100vh", color:"#E5E7EB", padding:"28px 32px" }}>

      {/* Header */}
      <div className="dash-header" style={{ display:"flex", justifyContent:"space-between", alignItems:"flex-start", marginBottom:22 }}>
        <div>
          <div style={{ fontSize:10, fontWeight:600, letterSpacing:"0.12em", color:"#6B7280", textTransform:"uppercase", marginBottom:4 }}>
            The Woods · Camp Finance
          </div>
          <h1 style={{ margin:0, fontSize:24, fontWeight:700, color:"#F9FAFB", letterSpacing:"-0.02em" }}>
            Budget Pacing
          </h1>
        </div>
        <FreshnessBadge loading={loading} loadError={loadError} />
      </div>

      {/* Filter Bar */}
      <div style={{ display:"flex", flexWrap:"wrap", gap:12, alignItems:"flex-end", background:"#161B22", border:"1px solid #21262D", borderRadius:10, padding:"14px 18px", marginBottom:14 }}>
        <Dropdown label="Fiscal Year" value={fy} options={FISCAL_YEARS} onChange={setFy} minWidth={110} />
        <Dropdown label="GL Account" multi options={GL_ACCOUNTS.map(g=>g.code)} selectedSet={activeGL}
          onChange={handleGL} getLabel={code => { const g=GL_ACCOUNTS.find(x=>x.code===code); return g?`${g.code} · ${g.name}`:code; }}
          minWidth={210}
          colorMap={Object.fromEntries(GL_ACCOUNTS.map(g=>[g.code, catColor(g.category)]))} />
        <OperationalToggle operationalOnly={operationalOnly} setOperationalOnly={setOperationalOnly} />
        <button
          onClick={() => { setFy(DEFAULT_FY); setActiveGL(new Set(GL_ACCOUNTS.map(g=>g.code))); setOperationalOnly(false); }}
          style={{ padding:"8px 12px", background:"transparent", border:"1px solid #374151", borderRadius:7, color:"#6B7280", fontSize:11, fontWeight:600, cursor:"pointer", marginTop:15 }}
          onMouseEnter={e=>{e.currentTarget.style.color="#9CA3AF";e.currentTarget.style.borderColor="#9CA3AF";}}
          onMouseLeave={e=>{e.currentTarget.style.color="#6B7280";e.currentTarget.style.borderColor="#374151";}}>
          Reset
        </button>
      </div>

      {/* Aggregate strip — tab-aware so headline numbers match the table */}
      <div className="kpi-grid" style={{ display:"grid", gridTemplateColumns:`repeat(${activeView === "ytd" ? 4 : 5}, 1fr)`, gap:12, marginBottom:14 }}>
        {(activeView === "ytd" ? [
          { label:"Time Elapsed",   value:`${monthsElapsed.toFixed(1)} mo`, sub:`${elapsedPct.toFixed(0)}% through ${fy}` },
          { label:"Expected YTD",   value:monthsElapsed>0?fmt(totalExpectedYTD):"—", sub:monthsElapsed>0?`based on ${prevFY} shape`:"—" },
          { label:"Actual YTD",     value:fmt(totalYTD),                    sub:`${totalPctConsumed.toFixed(0)}% of annual budget` },
          { label:"YTD Variance",
            value: monthsElapsed>0 ? `${totalYTDVar>=0?"+":""}${fmt(totalYTDVar)}` : "—",
            sub:   monthsElapsed>0 ? `${totalYTDVar>=0?"+":""}${totalYTDVarPct.toFixed(1)}% ${totalYTDVar>=0?"over pace":"under pace"}` : "—",
            color: monthsElapsed>0 ? (totalYTDVar>0?"#F87171":"#4FD1A0") : "#9CA3AF" },
        ] : [
          { label:"Time Elapsed",      value:`${monthsElapsed.toFixed(1)} mo`, sub:`${elapsedPct.toFixed(0)}% through ${fy}` },
          { label:"Total Budget",      value:fmt(totalBudget),                sub:`${rows.length} lines` },
          { label:"Spent YTD",         value:fmt(totalYTD),                   sub:`${totalPctConsumed.toFixed(0)}% consumed` },
          { label:"Forecast Annual",   value:monthsElapsed>0?fmt(totalProjected):"—", sub:monthsElapsed>0?`vs ${prevFY} pattern`:"—" },
          { label:"Forecast Variance",
            value: monthsElapsed>0 ? `${totalVar>=0?"+":""}${fmt(totalVar)}` : "—",
            sub:   monthsElapsed>0 ? (totalVar>=0?"Under budget":"Over budget") : "—",
            color: monthsElapsed>0 ? (totalVar>=0?"#4FD1A0":"#F87171") : "#9CA3AF" },
        ]).map(k => (
          <div key={k.label} style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:10, padding:"14px 16px" }}>
            <div style={{ fontSize:10, color:"#6B7280", fontWeight:600, textTransform:"uppercase", letterSpacing:"0.1em", marginBottom:5 }}>{k.label}</div>
            <div style={{ fontSize:20, fontWeight:700, color:k.color||"#F9FAFB", letterSpacing:"-0.02em" }}>{k.value}</div>
            <div style={{ fontSize:11, color:"#4B5563", marginTop:2 }}>{k.sub}</div>
          </div>
        ))}
      </div>

      {/* View Tabs */}
      <div style={{ display:"flex", gap:3, marginBottom:12, background:"#161B22", borderRadius:8, padding:4, width:"fit-content", border:"1px solid #21262D" }}>
        {[
          {id:"forecast", label:"Forecast (Annual)"},
          {id:"ytd",      label:"YTD vs Expected"},
        ].map(t => (
          <button key={t.id} onClick={()=>setActiveView(t.id)} style={{
            padding:"6px 13px", borderRadius:6, border:"none",
            background:activeView===t.id?"#21262D":"transparent",
            color:activeView===t.id?"#F9FAFB":"#6B7280",
            fontSize:11, fontWeight:600, cursor:"pointer", transition:"all 0.15s",
          }}>{t.label}</button>
        ))}
      </div>

      {/* Methodology + edge-case banner */}
      <div style={{ fontSize:10, color:"#4B5563", marginBottom:10 }}>
        Actuals sum across whole months that have begun ({monthsIncluded} of 12), matching the Expenses dashboard totals.{" "}
        {activeView === "forecast"
          ? <>Forecast: take the ratio of current YTD to {prevFY} spend over the same day-prorated window ({monthsElapsed.toFixed(1)} mo), then apply to {prevFY} annual. When {prevFY}'s early-year spend on a GL is too small to trust its seasonal shape, the projection blends toward straight-line — flagged "blended". Pure straight-line when prior-year is empty — flagged "linear". Transactions marked one-time (1× on the Expenses Transactions tab) count toward YTD but are not extrapolated.</>
          : <>Expected YTD: distribute the annual budget across months using {prevFY}'s monthly shape, then sum through {monthsElapsed.toFixed(1)} months (day-prorated). Linear fallback (budget × elapsed ÷ 12) when prior-year is empty — flagged. Positive variance = spending faster than time-prorated pace.</>}
      </div>
      {monthsElapsed > 0 && monthsElapsed < 2 && (
        <div style={{ marginBottom:12, padding:"10px 14px", background:"#161B22", border:"1px solid #21262D", borderRadius:8, fontSize:11, color:"#9CA3AF" }}>
          Only {monthsElapsed.toFixed(1)} months of data so far — {activeView === "forecast" ? "projections" : "expected-YTD comparisons"} are noisy. Treat them as directional, not precise.
        </div>
      )}
      {monthsElapsed === 0 && (
        <div style={{ marginBottom:12, padding:"10px 14px", background:"#161B22", border:"1px solid #21262D", borderRadius:8, fontSize:11, color:"#9CA3AF" }}>
          {fy} hasn't started yet — showing budget only. {activeView === "forecast" ? "Forecast" : "Expected YTD"} appears once spend begins.
        </div>
      )}

      {/* Pacing Table */}
      <div style={{ background:"#161B22", border:"1px solid #21262D", borderRadius:10, overflow:"hidden" }}>
        <div style={{ overflowX:"auto", maxHeight:"calc(100vh - 380px)", overflowY:"auto" }}>
          {(() => {
            const forecastCols = [
              { id:"status",   label:"Status",            align:"left",  sort:"varSign", width:90 },
              { id:"label",    label:"GL Account",        align:"left",  sort:"label" },
              { id:"budget",   label:"Annual Budget",     align:"right", sort:"budget" },
              { id:"ytd",      label:"Spent YTD",         align:"right", sort:"ytd" },
              { id:"pct",      label:"% Consumed",        align:"left",  sort:"pct", width:200 },
              { id:"remaining",label:"Remaining",         align:"right", sort:null },
              { id:"proj",     label:"Projected Annual",  align:"right", sort:null },
              { id:"var",      label:"Forecast Variance", align:"right", sort:"varSign" },
            ];
            const ytdCols = [
              { id:"status",   label:"Status",        align:"left",  sort:"ytdVarSign", width:100 },
              { id:"label",    label:"GL Account",    align:"left",  sort:"label" },
              { id:"budget",   label:"Annual Budget", align:"right", sort:"budget" },
              { id:"expected", label:"Expected YTD",  align:"right", sort:"expected" },
              { id:"actual",   label:"Actual YTD",    align:"right", sort:"ytd" },
              { id:"pct",      label:"% of Budget Consumed", align:"left", sort:"pct", width:200 },
              { id:"ytdvar",   label:"YTD Variance",  align:"right", sort:"ytdVarSign" },
            ];
            const cols = activeView === "ytd" ? ytdCols : forecastCols;
            return (
              <table style={{ width:"100%", borderCollapse:"collapse", fontSize:12 }}>
                <thead style={{ position:"sticky", top:0, background:"#0D1117", zIndex:10 }}>
                  <tr>
                    {cols.map(col => (
                      <th key={col.id}
                        onClick={col.sort ? () => toggleSort(col.sort) : undefined}
                        style={{
                          padding:"10px 14px", textAlign:col.align,
                          color:"#9CA3AF", fontWeight:600, textTransform:"uppercase",
                          letterSpacing:"0.07em", fontSize:10,
                          borderBottom:"1px solid #374151",
                          cursor: col.sort ? "pointer" : "default",
                          userSelect:"none", whiteSpace:"nowrap",
                          width: col.width,
                        }}>
                        {col.label}{col.sort ? sortArrow(col.sort) : ""}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r, i) => {
                    const status = statusFor(r);
                    const overBudget = r.projectedVar < 0;
                    const overPace   = r.ytdVar > 0;
                    const remNegative = r.remaining < 0;
                    const rowBg = i%2===0?"transparent":"#0D111740";
                    return (
                      <tr key={r.key} style={{ borderTop:"1px solid #21262D", background:rowBg }}>
                        {/* Status */}
                        <td style={{ padding:"10px 14px" }}>
                          <span style={{
                            display:"inline-block", padding:"3px 8px", borderRadius:4,
                            fontSize:9, fontWeight:700, letterSpacing:"0.05em",
                            background: status.color + "22", color: status.color,
                            border: `1px solid ${status.color}44`, whiteSpace:"nowrap",
                          }}>{status.label}</span>
                        </td>
                        {/* Label */}
                        <td style={{ padding:"10px 14px" }}>
                          <span style={{ display:"inline-flex", alignItems:"center", gap:7 }}>
                            <span style={{ width:7, height:7, borderRadius:"50%", background:catColor(r.category), flexShrink:0 }} />
                            <span style={{ color:"#F9FAFB", fontWeight:600 }}>{r.label}</span>
                          </span>
                        </td>
                        {/* Annual Budget */}
                        <td style={{ padding:"10px 14px", textAlign:"right", color:"#D1D5DB", fontVariantNumeric:"tabular-nums" }}>{fmtFull(r.budgetAnnual)}</td>

                        {activeView === "forecast" ? (
                          <>
                            <td style={{ padding:"10px 14px", textAlign:"right", color:"#D1D5DB", fontVariantNumeric:"tabular-nums" }}>{fmtFull(r.actualYTD)}</td>
                            <td style={{ padding:"10px 14px" }}>
                              <div style={{ display:"flex", alignItems:"center", gap:8 }}>
                                <div style={{ position:"relative", flex:1, height:6, background:"#0D1117", borderRadius:3, overflow:"hidden" }}>
                                  <div style={{
                                    position:"absolute", left:0, top:0, bottom:0,
                                    width:`${Math.min(100, r.pctConsumed)}%`,
                                    background: r.pctConsumed > 100 ? "#F87171"
                                              : (r.pctConsumed - elapsedPct) > 5 ? "#F7CF4F"
                                              : "#4FD1A0",
                                  }} />
                                  <div style={{ position:"absolute", left:`${elapsedPct}%`, top:-1, bottom:-1, width:1, background:"#9CA3AF", opacity:0.5 }} />
                                </div>
                                <span style={{ fontSize:11, color:"#9CA3AF", fontVariantNumeric:"tabular-nums", minWidth:38, textAlign:"right" }}>{r.pctConsumed.toFixed(0)}%</span>
                              </div>
                            </td>
                            <td style={{ padding:"10px 14px", textAlign:"right",
                              color: remNegative ? "#F87171" : "#D1D5DB",
                              fontVariantNumeric:"tabular-nums",
                              fontWeight: remNegative ? 600 : 400 }}>
                              {remNegative ? "-" : ""}{fmtFull(Math.abs(r.remaining))}
                            </td>
                            <td style={{ padding:"10px 14px", textAlign:"right", color:"#D1D5DB", fontVariantNumeric:"tabular-nums" }}>
                              {monthsElapsed > 0 ? (
                                <span style={{ display:"inline-flex", alignItems:"center", gap:6, justifyContent:"flex-end" }}>
                                  {fmtFull(r.projectedAnnual)}
                                  {r.projectedMethod === "linear" && (
                                    <span title="Prior-year had no spend on this line — using linear projection"
                                      style={{ padding:"1px 5px", background:"#F7CF4F22", border:"1px solid #F7CF4F44", borderRadius:3, fontSize:8, color:"#F7CF4F", fontWeight:600 }}>
                                      linear
                                    </span>
                                  )}
                                  {r.projectedMethod === "blend" && (
                                    <span title="Last year's spend on this GL was mostly later in the year, so its seasonal shape isn't reliable yet — projection is blended toward straight-line. Firms up as the year progresses."
                                      style={{ padding:"1px 5px", background:"#9CA3AF22", border:"1px solid #9CA3AF44", borderRadius:3, fontSize:8, color:"#9CA3AF", fontWeight:600 }}>
                                      blended
                                    </span>
                                  )}
                                </span>
                              ) : "—"}
                            </td>
                            <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums",
                              color: monthsElapsed === 0 ? "#9CA3AF" : (overBudget ? "#F87171" : "#4FD1A0"),
                              fontWeight: 600 }}>
                              {monthsElapsed > 0 ? (
                                <>
                                  {overBudget ? "▲ -" : "▼ +"}{fmtFull(Math.abs(r.projectedVar))}
                                  <span style={{ marginLeft:6, fontSize:10, opacity:0.7 }}>
                                    ({overBudget?"-":""}{Math.abs(r.varPct).toFixed(1)}%)
                                  </span>
                                </>
                              ) : "—"}
                            </td>
                          </>
                        ) : (
                          <>
                            <td style={{ padding:"10px 14px", textAlign:"right", color:"#D1D5DB", fontVariantNumeric:"tabular-nums" }}>
                              {monthsElapsed > 0 ? (
                                <span style={{ display:"inline-flex", alignItems:"center", gap:6, justifyContent:"flex-end" }}>
                                  {fmtFull(r.expectedYTD)}
                                  {r.expectedMethod === "linear" && (
                                    <span title="Prior-year had no spend on this line — using straight-line distribution"
                                      style={{ padding:"1px 5px", background:"#F7CF4F22", border:"1px solid #F7CF4F44", borderRadius:3, fontSize:8, color:"#F7CF4F", fontWeight:600 }}>
                                      linear
                                    </span>
                                  )}
                                </span>
                              ) : "—"}
                            </td>
                            <td style={{ padding:"10px 14px", textAlign:"right", color:"#D1D5DB", fontVariantNumeric:"tabular-nums" }}>{fmtFull(r.actualYTD)}</td>
                            <td style={{ padding:"10px 14px" }}>
                              <div style={{ display:"flex", alignItems:"center", gap:8 }}>
                                <div style={{ position:"relative", flex:1, height:6, background:"#0D1117", borderRadius:3, overflow:"hidden" }}>
                                  <div style={{
                                    position:"absolute", left:0, top:0, bottom:0,
                                    width:`${Math.min(100, r.pctConsumed)}%`,
                                    background: r.pctConsumed > 100 ? "#F87171"
                                              : (r.pctConsumed - elapsedPct) > 5 ? "#F7CF4F"
                                              : "#4FD1A0",
                                  }} />
                                  <div style={{ position:"absolute", left:`${elapsedPct}%`, top:-1, bottom:-1, width:1, background:"#9CA3AF", opacity:0.5 }} />
                                </div>
                                <span style={{ fontSize:11, color:"#9CA3AF", fontVariantNumeric:"tabular-nums", minWidth:38, textAlign:"right" }}>{r.pctConsumed.toFixed(0)}%</span>
                              </div>
                            </td>
                            <td style={{ padding:"10px 14px", textAlign:"right", fontVariantNumeric:"tabular-nums",
                              color: monthsElapsed === 0 ? "#9CA3AF" : (overPace ? "#F87171" : "#4FD1A0"),
                              fontWeight: 600 }}>
                              {monthsElapsed > 0 ? (
                                <>
                                  {overPace ? "▲ +" : "▼ "}{fmtFull(Math.abs(r.ytdVar))}
                                  <span style={{ marginLeft:6, fontSize:10, opacity:0.7 }}>
                                    ({overPace?"+":"-"}{Math.abs(r.ytdVarPct).toFixed(1)}%)
                                  </span>
                                </>
                              ) : "—"}
                            </td>
                          </>
                        )}
                      </tr>
                    );
                  })}
                  {rows.length === 0 && !loading && (
                    <tr><td colSpan={cols.length} style={{ padding:"30px", textAlign:"center", color:"#4B5563", fontSize:12 }}>
                      No rows match the current filters.
                    </td></tr>
                  )}
                </tbody>
              </table>
            );
          })()}
        </div>
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// APP SHELL
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Auth is handled entirely by Cloudflare Access in front of the deployment —
// no in-app per-dashboard access control (2 dashboards, one audience).

const NAV_ITEMS = [
  { id:"expenses", label:"Expenses",      desc:"GL accounts, budget vs. actual", component: ExpensesDashboard },
  { id:"pacing",   label:"Budget Pacing", desc:"Spend forecast vs. budget",      component: BudgetPacingDashboard },
];

// ── Mobile styles ─────────────────────────────────────────────────────────
// Inline styles can't respond to screen size; this injected stylesheet
// overrides them (hence !important) on phones. Desktop untouched.
const MOBILE_CSS = `
@media (max-width: 640px) {
  .dash-root   { padding: 16px 10px !important; }
  .dash-header { flex-wrap: wrap; gap: 6px; }
  .kpi-grid    { grid-template-columns: repeat(2, 1fr) !important; }
}
`;

const ICONS = {
  expenses: (c) => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="5" width="20" height="14" rx="2"/><path d="M2 10h20"/></svg>,
  pacing: (c) => <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 17l6-6 4 4 8-8"/><path d="M17 7h4v4"/></svg>,
};

export default function App() {
  const [activeId, setActiveId] = useState(() => hashPath());
  const [collapsed, setCollapsed] = useState(() => window.innerWidth < 640);
  const navigate = (id) => { writeHash(id, new URLSearchParams()); setActiveId(id); };

  // Invalid/missing hash falls back to the first dashboard.
  useEffect(() => {
    if (!activeId || !NAV_ITEMS.some(n => n.id === activeId)) {
      writeHash(NAV_ITEMS[0].id, hashParams());
      setActiveId(NAV_ITEMS[0].id);
    }
  }, [activeId]);

  const active = NAV_ITEMS.find(n => n.id === activeId);
  const ActiveComponent = active?.component;

  return (
    <div style={{ display:"flex", height:"100vh", background:"#0D1117", fontFamily:"'DM Sans','Segoe UI',sans-serif", overflow:"hidden" }}>
      <style>{MOBILE_CSS}</style>

      {/* Sidebar */}
      <div style={{
        width: collapsed ? 52 : 210,
        flexShrink: 0,
        background: "#080C12",
        borderRight: "1px solid #21262D",
        display: "flex",
        flexDirection: "column",
        transition: "width 0.2s ease",
        overflow: "hidden",
      }}>

        {/* Wordmark */}
        <div style={{
          padding: collapsed ? "18px 0" : "16px 16px",
          borderBottom: "1px solid #21262D",
          display: "flex", alignItems: "center",
          justifyContent: collapsed ? "center" : "space-between",
          minHeight: 60, flexShrink: 0,
        }}>
          {!collapsed && (
            <div>
              <div style={{ fontSize:11, fontWeight:700, color:"#F9FAFB", letterSpacing:"0.06em", textTransform:"uppercase" }}>The Woods</div>
              <div style={{ fontSize:9, color:"#374151", marginTop:2, letterSpacing:"0.1em", textTransform:"uppercase" }}>Camp Finance</div>
            </div>
          )}
          <button onClick={() => setCollapsed(c => !c)} style={{
            background:"transparent", border:"none", cursor:"pointer",
            padding:4, color:"#4B5563", display:"flex", alignItems:"center",
            justifyContent:"center", borderRadius:4, flexShrink:0,
          }}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              {collapsed ? <path d="M9 18l6-6-6-6"/> : <path d="M15 18l-6-6 6-6"/>}
            </svg>
          </button>
        </div>

        {/* Primary nav */}
        <div style={{ flex:1, padding: collapsed ? "8px 0" : "8px 6px", overflowY:"auto" }}>
          {!collapsed && (
            <div style={{ padding:"10px 10px 4px", fontSize:9, color:"#374151", fontWeight:700, textTransform:"uppercase", letterSpacing:"0.12em" }}>
              Dashboards
            </div>
          )}

          {NAV_ITEMS.map(item => {
            const isActive = activeId === item.id;
            const iconColor = isActive ? "#F9FAFB" : "#4B5563";
            return (
              <button key={item.id} onClick={() => navigate(item.id)}
                title={collapsed ? item.label : ""}
                style={{
                  width:"100%", padding: collapsed ? "11px 0" : "8px 10px",
                  display:"flex", alignItems:"center", gap:9,
                  justifyContent: collapsed ? "center" : "flex-start",
                  background: isActive ? "#161B22" : "transparent",
                  border:"none", borderRadius:7, cursor:"pointer", marginBottom:1,
                  transition:"background 0.12s", position:"relative",
                }}
                onMouseEnter={e => { if (!isActive) e.currentTarget.style.background="#0F141C"; }}
                onMouseLeave={e => { if (!isActive) e.currentTarget.style.background="transparent"; }}
              >
                {isActive && (
                  <span style={{ position:"absolute", left:0, top:"18%", height:"64%", width:2.5,
                    background:"#4FD1A0", borderRadius:"0 2px 2px 0" }} />
                )}
                <span style={{ flexShrink:0 }}>{ICONS[item.id]?.(iconColor)}</span>
                {!collapsed && (
                  <span style={{ fontSize:12, fontWeight:isActive?600:400,
                    color:isActive?"#F9FAFB":"#6B7280", whiteSpace:"nowrap" }}>
                    {item.label}
                  </span>
                )}
              </button>
            );
          })}
        </div>

        {/* Footer */}
        {!collapsed && (
          <div style={{ padding:"12px 16px", borderTop:"1px solid #21262D", flexShrink:0 }}>
            <div style={{ fontSize:9, color:"#374151" }}>
              The Woods Christian Camp
            </div>
          </div>
        )}
      </div>

      {/* Main */}
      <div style={{ flex:1, overflow:"auto", minWidth:0 }}>
        {ActiveComponent && <ActiveComponent />}
      </div>
    </div>
  );
}
