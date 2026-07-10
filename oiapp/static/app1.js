// ===== GLOBAL STATE =====
let currentSymbol = "SPY";
let currentExpiration = null;
let perSide = 12;

// ===== HELPER =====

// ---------- Generic helpers ----------
const $ = (sel, root = document) => root.querySelector(sel);

async function apiJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r.json();
}

async function fetchExpirations(symbol) {
  let data = await apiJSON(
    `/api/expirations?symbol=${encodeURIComponent(symbol)}`
  );
  if (data && data.expirations) data = data.expirations;
  return Array.isArray(data) ? data : [];
}

async function fetchSpot(symbol) {
  const d = await apiJSON(`/api/spot?symbol=${encodeURIComponent(symbol)}`);
  return d && typeof d.spot === "number"
    ? d.spot
    : typeof d === "number"
    ? d
    : null;
}

function forceUppercaseInput(el) {
  if (!el) return;
  el.addEventListener("input", (e) => {
    const i = e.target.selectionStart;
    e.target.value = e.target.value.toUpperCase();
    e.target.setSelectionRange(i, i);
  });
}

// Reusable chart renderer: give it a container id + arrays [{strike, oi}] for calls/puts
function renderOptionsChart(containerId, symbol, expiry, calls, puts) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const traces = [
    {
      x: calls.map((d) => d.strike),
      y: calls.map((d) => d.oi),
      type: "bar",
      name: "Calls OI",
    },
    {
      x: puts.map((d) => d.strike),
      y: puts.map((d) => d.oi),
      type: "bar",
      name: "Puts OI",
    },
  ];
  const layout = {
    barmode: "group",
    title: `${symbol} ${expiry} OI`,
    xaxis: { title: "Strike" },
    yaxis: { title: "OI" },
  };
  Plotly.newPlot(el, traces, layout);
}
// cfg = { symbolId, expiryId, spotLabelId, chartContainerId, fetchOptionsData }
async function refreshSection(cfg) {
  const symEl = document.getElementById(cfg.symbolId);
  const expEl = document.getElementById(cfg.expiryId);
  const spotEl = document.getElementById(cfg.spotLabelId);

  const symbol = symEl.value.trim().toUpperCase();
  if (!symbol) return;

  // Ensure uppercase UI
  symEl.value = symbol;

  // Expirations (only if empty)
  if (!expEl.options.length) {
    const exps = await fetchExpirations(symbol);
    expEl.innerHTML = exps
      .map((e) => `<option value="${e}">${e}</option>`)
      .join("");
  }

  const expiry = expEl.value;
  if (!expiry) return;

  // Spot
  if (spotEl) {
    spotEl.textContent = "…";
    const spot = await fetchSpot(symbol);
    spotEl.textContent = spot != null ? spot.toFixed(2) : "—";
  }

  // Options data (shape: {calls:[{strike,oi}], puts:[{strike,oi}]})
  const data = await cfg.fetchOptionsData(symbol, expiry);
  renderOptionsChart(
    cfg.chartContainerId,
    symbol,
    expiry,
    data.calls || [],
    data.puts || []
  );
}

async function safeFetchJSON(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  try {
    return await res.json();
  } catch {
    return {};
  }
}

// ===== TAB SETUP =====
function setupTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document
        .querySelectorAll(".tab")
        .forEach((t) => t.classList.remove("active"));
      document
        .querySelectorAll(".section")
        .forEach((s) => s.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById(tab.dataset.target).classList.add("active");
    });
  });
}

// ===== INITIAL LOAD =====
window.addEventListener("DOMContentLoaded", async () => {
  setupTabs();
  await initDashboard();
  await initAggregateTab();
  await loadSchedulerList();
  await initSqlViewer();
  await loadScanner();
  updateSchedulerStatus();
  setInterval(updateSchedulerStatus, 15000);
});

// ===== DASHBOARD =====
async function initDashboard() {
  const symInput = document.getElementById("symbol-input");
  const expSelect = document.getElementById("expiration-select");
  const sideInput = document.getElementById("per-side-input");

  if (!symInput || !expSelect) return;

  symInput.value = currentSymbol;
  await refreshExpirations(currentSymbol);
  await loadSpot(currentSymbol);
  await loadHistory(currentSymbol);
  await loadOptions(currentSymbol, currentExpiration, perSide);
  await loadOIChange(currentSymbol, currentExpiration);

  symInput.addEventListener("change", async (e) => {
    currentSymbol = e.target.value.trim().toUpperCase() || "SPY";
    await refreshExpirations(currentSymbol);
    await loadSpot(currentSymbol);
    await loadHistory(currentSymbol);
    await loadOptions(currentSymbol, currentExpiration, perSide);
    await loadOIChange(currentSymbol, currentExpiration);
  });

  expSelect.addEventListener("change", async (e) => {
    currentExpiration = e.target.value;
    await loadOptions(currentSymbol, currentExpiration, perSide);
    await loadOIChange(currentSymbol, currentExpiration);
  });

  sideInput.addEventListener("change", async (e) => {
    perSide = +e.target.value || 12;
    await loadOptions(currentSymbol, currentExpiration, perSide);
  });

  ["toggle-oi", "toggle-vol"].forEach((id) => {
    const el = document.getElementById(id);
    if (el)
      el.addEventListener("change", () =>
        loadOptions(currentSymbol, currentExpiration, perSide)
      );
  });
}

async function refreshExpirations(symbol) {
  const data = await safeFetchJSON(`/api/expirations?symbol=${symbol}`);
  const sel = document.getElementById("expiration-select");
  sel.innerHTML = "";
  if (data.expirations?.length) {
    data.expirations.forEach((d, i) => {
      const o = document.createElement("option");
      o.value = d;
      o.textContent = d;
      sel.appendChild(o);
      if (i === 0) currentExpiration = d;
    });
  }
}

async function loadSpot(symbol) {
  const data = await safeFetchJSON(`/api/spot?symbol=${symbol}`);
  const el = document.getElementById("spot-price");
  if (el) el.textContent = data.spot ?? "—";
}

async function loadHistory(symbol) {
  const data = await safeFetchJSON(`/api/history?symbol=${symbol}&period=90d`);
  const prices = data?.prices || [];
  if (!prices.length) {
    Plotly.purge("history-chart");
    return;
  }
  const dates = prices.map((p) => p.date);
  const open = prices.map((p) => +p.open);
  const high = prices.map((p) => +p.high);
  const low = prices.map((p) => +p.low);
  const close = prices.map((p) => +p.close);
  const ema = (arr, n) => {
    const k = 2 / (n + 1);
    let e = arr[0];
    return arr.map((v, i) => (i ? (e = v * k + e * (1 - k)) : v));
  };
  const ema20 = ema(close, 20),
    ema50 = ema(close, 50);
  Plotly.newPlot(
    "history-chart",
    [
      {
        type: "candlestick",
        x: dates,
        open,
        high,
        low,
        close,
        increasing: { line: { color: "rgba(0, 11, 170, 1)" } },
        decreasing: { line: { color: "#d00" } },
        name: "Price",
      },
      {
        x: dates,
        y: ema20,
        mode: "lines",
        name: "EMA20",
        line: { color: "#007bff" },
      },
      {
        x: dates,
        y: ema50,
        mode: "lines",
        name: "EMA50",
        line: { color: "#ff9900" },
      },
    ],
    { xaxis: { rangeslider: { visible: false } }, yaxis: { title: "Price" } }
  );
}

async function loadOptions(symbol, expiration, sideCount) {
  if (!expiration) return;
  const d = await safeFetchJSON(
    `/api/options?symbol=${symbol}&expiration=${expiration}&per_side=${sideCount}`
  );
  const calls = d?.calls || [],
    puts = d?.puts || [];
  if (!calls.length && !puts.length) {
    Plotly.purge("options-chart");
    return;
  }
  const strikes = [...new Set([...calls, ...puts].map((r) => +r.strike))].sort(
    (a, b) => a - b
  );
  const showOI = document.getElementById("toggle-oi").checked;
  const showVol = document.getElementById("toggle-vol").checked;
  const mk = (t, m, n, c) => ({
    type: "bar",
    x: strikes,
    y: strikes.map((s) => {
      const src = t === "call" ? calls : puts;
      const f = src.find((r) => +r.strike === s) || {};
      return +(f[m] || 0);
    }),
    name: n,
    marker: { color: c },
  });
  const tr = [];
  if (showOI) {
    tr.push(mk("call", "oi", "Call OI", "rgba(43,0,255,0.6)"));
    tr.push(mk("put", "oi", "Put OI", "rgba(220,53,70,1)"));
  }
  if (showVol) {
    tr.push(mk("call", "volume", "Call Vol", "rgba(0,123,255,0.3)"));
    tr.push(mk("put", "volume", "Put Vol", "rgba(220,53,69,0.3)"));
  }
  Plotly.newPlot("options-chart", tr, {
    barmode: "group",
    xaxis: { title: "Strike" },
    yaxis: { title: "Contracts" },
  });
}

async function loadOIChange(symbol, expiration) {
  if (!expiration) return;
  const data = await safeFetchJSON(
    `/api/oi_change?symbol=${symbol}&expiration=${expiration}`
  );
  const rows = data?.change || [];
  if (!rows.length) {
    Plotly.purge("oi-change-chart");
    return;
  }
  const calls = rows.filter((r) => r.type === "call");
  const puts = rows.filter((r) => r.type === "put");
  Plotly.newPlot(
    "oi-change-chart",
    [
      {
        type: "bar",
        x: calls.map((r) => r.strike),
        y: calls.map((r) => r.change),
        name: "ΔOI Calls",
        marker: { color: "rgba(0,123,255,0.6)" },
      },
      {
        type: "bar",
        x: puts.map((r) => r.strike),
        y: puts.map((r) => r.change),
        name: "ΔOI Puts",
        marker: { color: "rgba(220,53,69,0.6)" },
      },
    ],
    { barmode: "group", xaxis: { title: "Strike" }, yaxis: { title: "Δ OI" } }
  );
}

// ===== AGGREGATE =====
async function initAggregateTab() {
  document.getElementById("agg-symbol").value = currentSymbol;
  loadAggregateExpirations(currentSymbol);
  loadPcrSnapshot(currentSymbol);
  loadSpot(currentSymbol);
  const sym = document.getElementById("agg-symbol");
  if (sym)
    sym.addEventListener("change", () => {
      const sym =
        document.getElementById("agg-symbol").value.trim().toUpperCase() ||
        "SPY";
      loadAggregateExpirations(sym);
    });

  const btn = document.getElementById("btn-refresh-aggregate");
  if (btn)
    btn.addEventListener("click", () => {
      const sym =
        document.getElementById("agg-symbol").value.trim().toUpperCase() ||
        "SPY";
      const fromExp = document.getElementById("agg-expiration").value;
      const count = document.getElementById("agg-count").value || 3;
      loadAggregateData(sym, fromExp, count);
      loadPcrSnapshot(sym);
      loadOptions(sym, fromExp, 12);
    });
}

function loadAggregateExpirations(symbol) {
  fetch(`/api/db_expirations?symbol=${symbol}`)
    .then((r) => r.json())
    .then((data) => {
      const sel = document.getElementById("agg-expiration");
      if (!sel) return;
      sel.innerHTML = "";
      (data.expirations || []).forEach((exp) => {
        const o = document.createElement("option");
        o.value = exp;
        o.textContent = exp;
        sel.appendChild(o);
      });
    });
}

async function loadAggregateData(symbol, fromExp, count) {
  const strikes = +(document.getElementById("agg-strikes").value || 10);
  const showOI = document.getElementById("agg-show-oi").checked;
  const showVol = document.getElementById("agg-show-vol").checked;
  const url = `/api/aggregate_strike?symbol=${symbol}&from_expiration=${fromExp}&count=${count}&strikes=${strikes}`;
  const data = await safeFetchJSON(url);
  const xs = data?.strikes || [];
  const call = data?.call_sum || [];
  const put = data?.put_sum || [];
  const cvol = data?.call_vol_sum || [];
  const pvol = data?.put_vol_sum || [];
  const traces = [];
  if (showOI) {
    traces.push({
      type: "bar",
      x: xs,
      y: call,
      name: "Call OI",
      marker: { color: "rgba(55,0,255,0.6)" },
    });
    traces.push({
      type: "bar",
      x: xs,
      y: put,
      name: "Put OI",
      marker: { color: "rgba(220,53,70,1)" },
    });
  }
  if (showVol) {
    traces.push({
      type: "bar",
      x: xs,
      y: cvol,
      name: "Call Vol",
      marker: { color: "rgba(0,123,255,0.3)" },
    });
    traces.push({
      type: "bar",
      x: xs,
      y: pvol,
      name: "Put Vol",
      marker: { color: "rgba(220,53,69,0.3)" },
    });
  }
  Plotly.newPlot("aggregate-chart", traces, {
    barmode: "group",
    xaxis: { title: "Strike" },
    yaxis: { title: "Sum across expirations" },
  });
}

function loadPcrSnapshot(symbol) {
  fetch(`/api/pcr_snapshot?symbol=${symbol}`)
    .then((r) => r.json())
    .then((data) => {
      if (!data.pcr_data?.length) return;
      const exps = data.pcr_data.map((x) => x.expiration);
      const pcrs = data.pcr_data.map((x) => x.pcr);
      const ctx = document.getElementById("pcrChart").getContext("2d");
      if (window.pcrChart && typeof window.pcrChart.destroy === "function") {
        window.pcrChart.destroy();
      }

      window.pcrChart = new Chart(ctx, {
        type: "line",
        data: {
          labels: exps,
          datasets: [
            {
              label: "Put/Call Ratio",
              data: pcrs,
              borderColor: (ctx) => (ctx.raw > 1 ? "red" : "green"),
              fill: false,
              tension: 0.2,
            },
          ],
        },
        options: {
          scales: {
            y: { beginAtZero: true, title: { text: "PCR", display: true } },
          },
        },
      });
    });
}

// ===== SCHEDULER =====
async function updateSchedulerStatus() {
  const el = document.getElementById("scheduler-status");
  if (!el) return;
  const data = await safeFetchJSON("/api/fetch_status").catch(() => ({}));
  let html = "";
  if (data.running)
    html = `<b>Status:</b> 🟢 Running<br>Last Run: ${data.last_run || "-"}<br>${
      data.last_msg || ""
    }`;
  else
    html = `<b>Status:</b> ⚪ Idle<br>Last Run: ${data.last_run || "-"}<br>${
      data.last_msg || ""
    }`;
  el.innerHTML = html;
}

async function loadSchedulerList() {
  const data = await safeFetchJSON("/api/symbols");
  document.getElementById("symbols-list").value = (data.symbols || []).join(
    ", "
  );
}
document.getElementById("save-symbols")?.addEventListener("click", async () => {
  const val = document.getElementById("symbols-list").value;
  const syms = val
    .split(",")
    .map((s) => s.trim().toUpperCase())
    .filter(Boolean);
  await safeFetchJSON("/api/symbols", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbols: syms }),
  });
  document.getElementById("scheduler-status").textContent = "✅ Saved";
});
document.getElementById("run-now")?.addEventListener("click", async () => {
  const res = await safeFetchJSON("/api/fetch_now", { method: "POST" });
  document.getElementById("scheduler-status").textContent =
    "Scheduler triggered.";
});

// ===== SCANNER =====
async function loadScanner() {
  const n = +(document.getElementById("scan-limit")?.value || 10);
  const data = await safeFetchJSON(`/api/scanner?limit=${n}`);
  const body = document.querySelector("#scanner-table tbody");
  if (!body) return;
  body.innerHTML = "";
  (data.results || []).forEach((r) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${r.symbol}</td><td>${r.delta_oi}</td><td>${r.delta_vol}</td><td>${r.signal}</td>`;
    body.appendChild(tr);
  });
}
document.getElementById("scan-refresh")?.addEventListener("click", loadScanner);

// ===== SQL VIEWER =====
async function initSqlViewer() {
  const tableSel = document.getElementById("sql-table");
  const symbolInput = document.getElementById("sql-symbol");
  const expSel = document.getElementById("sql-expiration");
  const limitInput = document.getElementById("sql-limit");
  const btn = document.getElementById("sql-run");
  const reset = document.getElementById("sql-reset");
  if (!tableSel || !symbolInput) return;

  const loadExpirations = async () => {
    const sym = symbolInput.value.trim().toUpperCase();
    if (!sym) {
      expSel.innerHTML = '<option value="">(all)</option>';
      return;
    }
    const data = await safeFetchJSON(`/api/db_expirations?symbol=${sym}`);
    expSel.innerHTML = '<option value="">(all)</option>';
    (data.expirations || []).forEach((e) => {
      const o = document.createElement("option");
      o.value = e;
      o.textContent = e;
      expSel.appendChild(o);
    });
  };

  symbolInput.addEventListener("change", loadExpirations);
  await loadExpirations();

  btn.addEventListener("click", async () => {
    const table = tableSel.value;
    const sym = symbolInput.value.trim().toUpperCase();
    const exp = expSel.value;
    const limit = +(limitInput.value || 100);
    const url = new URL(window.location.origin + `/api/sql_view`);
    url.searchParams.set("table", table);
    url.searchParams.set("limit", String(limit));
    if (table === "options") {
      if (sym) url.searchParams.set("symbol", sym);
      if (exp) url.searchParams.set("expiration", exp);
    }
    const data = await safeFetchJSON(url.toString());
    renderSqlTable(data.columns || [], data.rows || []);
  });

  reset.addEventListener("click", () => {
    tableSel.value = "options";
    symbolInput.value = "";
    expSel.innerHTML = "<option value=''>(all)</option>";
    limitInput.value = "100";
    renderSqlTable([], []);
  });
}

function renderSqlTable(columns, rows) {
  const wrap = document.getElementById("sql-grid");
  if (!wrap) return;
  wrap.innerHTML = "";
  if (!columns.length || !rows.length) {
    wrap.textContent = "No rows.";
    return;
  }
  const table = document.createElement("table");
  const thead = document.createElement("thead");
  const trh = document.createElement("tr");
  columns.forEach((c) => {
    const th = document.createElement("th");
    th.textContent = c;
    trh.appendChild(th);
  });
  thead.appendChild(trh);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  rows.forEach((r) => {
    const tr = document.createElement("tr");
    columns.forEach((c) => {
      const td = document.createElement("td");
      td.textContent = r[c] ?? "";
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

// static/app.js
async function runSQL() {
  const queryBox = document.getElementById("sql-query");
  const status = document.getElementById("sql-status");
  const results = document.getElementById("sql-results");

  const query = queryBox.value.trim();
  if (!query) {
    status.innerHTML =
      "<span class='text-danger'>Enter a SQL query first.</span>";
    return;
  }

  status.innerHTML = "<span class='text-info'>Running query...</span>";
  results.innerHTML = "";

  try {
    const res = await fetch("/scanner/run_sql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });

    const data = await res.json();

    if (!res.ok) {
      status.innerHTML = `<span class='text-danger'>Error: ${data.error}</span>`;
      return;
    }

    if (!data.rows || data.rows.length === 0) {
      status.innerHTML = "<span class='text-warning'>No rows returned.</span>";
      return;
    }

    const cols = data.columns;
    const rows = data.rows;
    let html =
      "<table class='table table-sm table-striped table-bordered table-hover'><thead><tr>";

    for (const c of cols) html += `<th>${c}</th>`;
    html += "</tr></thead><tbody>";

    for (const r of rows) {
      html += "<tr>";
      for (const v of r) html += `<td>${v ?? ""}</td>`;
      html += "</tr>";
    }
    html += "</tbody></table>";
    results.innerHTML = html;
    status.innerHTML = `<span class='text-success'>✅ ${rows.length} row(s) returned</span>`;

    // save last query in localStorage
    localStorage.setItem("last_sql_query", query);
  } catch (err) {
    status.innerHTML = `<span class='text-danger'>Error: ${err.message}</span>`;
  }
}

function clearSQL() {
  document.getElementById("sql-query").value = "";
  document.getElementById("sql-status").innerHTML = "";
  document.getElementById("sql-results").innerHTML = "";
  localStorage.removeItem("last_sql_query");
}

// Auto-load last query if present
window.addEventListener("DOMContentLoaded", () => {
  const last = localStorage.getItem("last_sql_query");
  if (last) document.getElementById("sql-query").value = last;
});

// ========== AUTO LOAD DASHBOARD BASED ON URL PARAMS ==========
// ========== AUTO LOAD DASHBOARD BASED ON URL PARAMS ==========
async function autoLoadFromURL() {
  const params = new URLSearchParams(window.location.search);
  const symbolParam = params.get("symbol");
  const expiryParam = params.get("expiry");

  if (!symbolParam) return;

  const symInput = document.getElementById("symbol-input");
  const expSelect = document.getElementById("expiration-select");
  if (!symInput || !expSelect) return;

  // 1️⃣ Fill symbol field
  symInput.value = symbolParam.toUpperCase();

  try {
    // 2️⃣ Fetch expirations for that symbol
    const r = await fetch(`/api/expirations?symbol=${symbolParam}`);
    let data = await r.json();
    if (data && data.expirations) data = data.expirations;
    if (!Array.isArray(data) || data.length === 0) return;

    // 3️⃣ Populate dropdown
    expSelect.innerHTML = data
      .map((e) => `<option value="${e.trim()}">${e.trim()}</option>`)
      .join("");

    // 4️⃣ Try to match expiry ignoring format quirks
    let normalizedExpiry = expiryParam ? expiryParam.trim() : "";
    const match = data.find((e) => e.trim() === normalizedExpiry);
    if (match) {
      expSelect.value = match;
      console.log(`✅ Matched expiry ${match}`);
    } else {
      // fallback to first if not matched
      expSelect.selectedIndex = 0;
      console.log(
        `⚠️ No exact expiry match for ${expiryParam}; using ${expSelect.value}`
      );
    }

    // 5️⃣ Wait a moment to ensure DOM update, then trigger refresh
    setTimeout(() => {
      if (typeof loadCharts === "function") {
        loadCharts();
      } else if (document.getElementById("scan-refresh")) {
        document.getElementById("scan-refresh").click();
      } else if (document.getElementById("btn-refresh-aggregate")) {
        document.getElementById("btn-refresh-aggregate").click();
      }
    }, 600);
  } catch (err) {
    console.error("Auto-load error:", err);
  }
}

// Run auto-load once the DOM is ready
window.addEventListener("DOMContentLoaded", autoLoadFromURL);
