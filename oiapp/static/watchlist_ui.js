// watchlist_ui.js
// Extracted from app.js (the watchlist management page's UI logic --
// symbol detail panel, table rendering, add/edit/delete, per-watchlist
// schedule controls). Plain global-scope script (not an ES module),
// same as app.js and every other file here -- loaded via a second
// <script> tag, not import/export, so load order relative to app.js
// doesn't matter for these function-to-function calls (function
// declarations are hoisted and don't actually run until a user
// interacts with the page, by which point every script has loaded).
// Split out as part of breaking up app.js's 22k-line monolith into
// smaller, independently-verifiable files.

// ════════════════════════════════════════════════════════════════════════════
// WATCHLIST SYMBOL DETAIL PANEL
// ---------------------------------------------------------------------------
async function _wlLoadSymbolsPanel(wlId, wlName) {
  const panel = document.getElementById('wl-symbol-panel');
  if (!panel || !wlId) return;
  panel.style.display = 'block';
  panel.dataset.wlId = String(wlId);
  panel.dataset.wlName = wlName || '';
  panel.innerHTML = '<div style="color:var(--muted);padding:12px">Loading symbols...</div>';
  try {
    const d = await api(`/watchlists/${wlId}/symbols`);
    const rows = d.symbols || [];
    if (!rows.length) {
      panel.innerHTML = '<div style="color:var(--muted);padding:12px">No symbols in this watchlist.</div>';
      return;
    }
    panel.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;gap:10px;flex-wrap:wrap">
        <div><b>${wlName || 'Watchlist'} symbols</b> <span style="color:var(--muted)">(${rows.length})</span></div>
        <div style="font-size:11px;color:var(--muted)">Set an alert price and choose above / below / both.</div>
      </div>
      <div style="overflow:auto;border:1px solid var(--border);border-radius:8px">
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr style="background:rgba(255,255,255,.05)">
            <th style="padding:8px;text-align:left">Symbol</th>
            <th style="padding:8px;text-align:left">Added</th>
            <th style="padding:8px;text-align:left">Alert Price</th>
            <th style="padding:8px;text-align:left">Direction</th>
            <th style="padding:8px;text-align:left">Mode</th>
            <th style="padding:8px;text-align:left">Last Price</th>
            <th style="padding:8px;text-align:left">Last Alert</th>
            <th style="padding:8px;text-align:left">Action</th>
          </tr></thead>
          <tbody>
            ${rows.map(r => `
              <tr data-wl-symbol-row="${_priceAlertRowKey(wlId, r.symbol)}" style="border-top:1px solid var(--border)">
                <td style="padding:8px;font-weight:700">${r.symbol}</td>
                <td style="padding:8px;color:var(--muted)">${r.added_at || '—'}</td>
                <td style="padding:8px"><input id="wl-ap-${wlId}-${r.symbol}" type="number" step="0.01" value="${r.alert_price ?? ''}" style="width:100px;padding:5px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text)"/></td>
                <td style="padding:8px">
                  <select id="wl-ad-${wlId}-${r.symbol}" style="padding:5px;background:var(--bg);border:1px solid var(--border);border-radius:5px;color:var(--text)">
                    <option value="both" ${((r.alert_direction||'both')==='both')?'selected':''}>Both</option>
                    <option value="above" ${(r.alert_direction==='above')?'selected':''}>Above</option>
                    <option value="below" ${(r.alert_direction==='below')?'selected':''}>Below</option>
                  </select>
                </td>
                <td style="padding:8px"><input id="wl-ae-${wlId}-${r.symbol}" type="checkbox" ${r.alert_enabled ? 'checked' : ''}/></td>
                <td style="padding:8px;color:var(--muted)">${fmt(r.current_price, 2)}</td>
                <td style="padding:8px;color:var(--muted)">${r.alert_last_sent_at || '—'}</td>
                <td style="padding:8px;display:flex;gap:6px;flex-wrap:wrap"><button type="button" class="btn btn-ghost btn-sm" onclick="_wlSaveAlert(${wlId}, '${r.symbol}')">Save</button><button type="button" class="btn btn-ghost btn-sm" onclick="_deleteSymbolPriceAlert(${wlId}, '${r.symbol}')">Delete</button></td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch(e) {
    panel.innerHTML = '<div style="color:#ef4444;padding:12px">' + e.message + '</div>';
  }
}

async function _wlSaveAlert(wlId, sym) {
  const priceEl = document.getElementById(`wl-ap-${wlId}-${sym}`);
  const dirEl   = document.getElementById(`wl-ad-${wlId}-${sym}`);
  const enEl    = document.getElementById(`wl-ae-${wlId}-${sym}`);
  const payload = {
    alert_price: priceEl?.value || '',
    alert_direction: dirEl?.value || 'both',
    alert_enabled: !!enEl?.checked,
  };
  try {
    const d = await api(`/watchlists/${wlId}/symbols/${encodeURIComponent(sym)}/alert`, {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    addNotif('ok', 'Alert saved', `${sym} -> ${(d.alert_price ?? payload.alert_price) || 'off'}`, 'Watchlists');
    const panel = document.getElementById('wl-symbol-panel');
    if (panel?.dataset?.wlId) {
      await _wlLoadSymbolsPanel(panel.dataset.wlId, panel.dataset.wlName || 'Watchlist');
    }
    try { await _loadSymbolAlertsHub(); } catch {}
  } catch (e) {
    addNotif('err', 'Alert save failed', e.message || String(e), 'Watchlists');
  }
}

async function _deleteSymbolPriceAlert(wlId, sym) {
  const watchlistId = Number(wlId);
  const symbol = String(sym || '').trim().toUpperCase();
  if (!Number.isFinite(watchlistId) || watchlistId <= 0 || !symbol) {
    addNotif('err', 'Delete failed', 'Invalid symbol alert target', 'Alerts');
    return;
  }
  if (!confirm(`Delete price alert for ${symbol}?`)) return;

  try {
    let d;
    try {
      d = await api(`/watchlists/${watchlistId}/symbols/${encodeURIComponent(symbol)}/alert`, {method:'DELETE'});
    } catch (firstErr) {
      d = await api(`/watchlists/${watchlistId}/symbols/${encodeURIComponent(symbol)}/alert`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({alert_price:'', alert_direction:'both', alert_enabled:false})
      });
    }
    if (!d || d.ok === false) throw new Error((d && (d.error || d.message)) || 'Delete failed');

    // Optimistically remove/clear visible rows immediately, then force no-cache
    // reloads.  This fixes the stale grid issue after a confirmed delete.
    _removeDeletedSymbolAlertFromGrids(watchlistId, symbol);
    const panel = document.getElementById('wl-symbol-panel');
    const refreshes = [];
    if (panel?.dataset?.wlId) {
      refreshes.push(_wlLoadSymbolsPanel(panel.dataset.wlId, panel.dataset.wlName || 'Watchlist'));
    }
    refreshes.push(_loadSymbolAlertsHub(true));
    if (typeof _loadAlertRulesHub === 'function') refreshes.push(_loadAlertRulesHub());
    await Promise.allSettled(refreshes);
    addNotif('ok', 'Price alert deleted', symbol, 'Alerts');
  } catch (e) {
    addNotif('err', 'Price alert delete failed', e.message || String(e), 'Alerts');
  }
}
// WATCHLIST MANAGER  — form-based create/edit, table display
// ════════════════════════════════════════════════════════════════════════════
var _wlEditingId = null;
var _wlData = [];

async function _wlLoad() {
  const wrap = document.getElementById('wl-table-wrap');
  if (!wrap) return;
  try {
    const d = await api('/watchlists/');
    _wlData = (d && d.watchlists) ? d.watchlists : [];
    _wlRenderTable();
    _loadSchedulerWatchlists();
    _loadWatchlistDropdown('wl-fetch-watchlist', 'wl-fetch-count', 'Default watchlist');
  } catch(e) {
    if (wrap) wrap.innerHTML = '<div style="color:#ef4444;padding:16px">❌ '+e.message+'</div>';
    addNotif('error', 'Watchlist load failed', e.message, 'Watchlists');
  }
}

function _wlRenderTable() {
  const wrap = document.getElementById('wl-table-wrap');
  if (!wrap) return;
  if (!_wlData.length) {
    wrap.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:10px"><div style="color:var(--muted);font-size:12px">No watchlists yet — click + New Watchlist.</div><button class="btn btn-secondary" onclick="_wlLoad()" style="font-size:11px;padding:4px 9px">⟳ Refresh</button></div>';
    return;
  }

  function _lastRunBadge(wl) {
    if (!wl.last_fetch_at) return '<span style="color:#ef4444;font-size:10px">⚠ Never fetched</span>';
    // Parse timestamp and compute staleness
    const fetchDate = new Date(wl.last_fetch_at.replace(' ', 'T'));
    const hrs = Math.round((Date.now() - fetchDate) / 3600000);
    const label = hrs < 1 ? 'just now' : hrs < 24 ? hrs+'h ago' : Math.round(hrs/24)+'d ago';
    const color = hrs < 12 ? '#22c55e' : hrs < 48 ? '#f59e0b' : '#ef4444';
    return `<span style="color:${color};font-size:10px;font-weight:700" title="${wl.last_fetch_at}">
      ✅ ${label} &nbsp;<span style="color:var(--muted);font-weight:400">${wl.last_fetch_mode||''} · ${wl.last_fetch_count||0} syms</span>
    </span>`;
  }

  // Renders one row of the Schedule column: a time input + a small
  // status dot showing whether that step has already run TODAY
  // (green), is scheduled but hasn't fired yet today (amber), or has no
  // time set at all (gray dash, matches "if there is no time it shall
  // not execute" -- this step simply never runs). Earnings/Indicators
  // note the price/OI dependency in their title tooltip since that's
  // not otherwise visible from the UI alone.
  function _scheduleStepRow(wl, field, dateField, label, id, dependsOnPrice) {
    const timeVal = wl[field] || '';
    const today = new Date().toISOString().slice(0, 10);
    const ranToday = wl[dateField] === today;
    let dot, dotTitle;
    if (!timeVal) { dot = '#475569'; dotTitle = 'Not scheduled -- this step never runs automatically'; }
    else if (ranToday) { dot = '#22c55e'; dotTitle = `Ran today (${wl[dateField]})`; }
    else { dot = '#f59e0b'; dotTitle = 'Scheduled, hasn\'t run yet today'; }
    const depNote = dependsOnPrice ? ' -- waits for Price/OI to complete first, even if its own time has passed' : '';
    return `<div style="display:flex;align-items:center;gap:5px;margin-bottom:3px">
      <span style="width:7px;height:7px;border-radius:50%;background:${dot};flex-shrink:0" title="${dotTitle}${depNote}"></span>
      <span style="font-size:10px;color:var(--muted);width:64px">${label}</span>
      <input type="time" value="${timeVal}" id="wl-sched-${field}-${id}"
        onchange="_wlSetSchedule(${id}, '${field}', this.value)"
        style="font-size:10px;padding:1px 3px;width:78px;background:var(--bg,#0b1020);border:1px solid var(--border,#334155);color:var(--text,#e2e8f0);border-radius:3px" />
    </div>`;
  }

  const futuresOiPanel = `<div style="background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.25);border-radius:6px;padding:10px;margin-bottom:12px">
    <div style="font-size:12px;font-weight:700;color:#818cf8;margin-bottom:6px">📊 Futures OI Data Source</div>
    <div id="futures-source-info" style="font-size:11px;color:var(--muted);margin-bottom:8px;line-height:1.6">
      <span id="futures-auth-status">⏳ Checking…</span><br>
      <b>tastytrade</b> → tried first (live snapshot, no daily-fetch dependency)<br>
      <b>Schwab connected</b> → fallback, real daily OI from exchange<br>
      <b>Not connected</b> → no real futures OI; connect Schwab or tastytrade. CFTC COT remains separate.
    </div>
    <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
      <button class="btn btn-primary" onclick="_fetchFuturesNow()" style="font-size:11px">⚡ Fetch OI Now</button>
      <button class="btn btn-ghost" onclick="_checkFuturesData()" style="font-size:11px">🔍 Check Data</button>
      <button class="btn btn-ghost" onclick="_clearFuturesData()" style="font-size:11px;color:#ef4444;border-color:#ef4444">🗑 Clear Data</button>
      <div style="width:1px;height:22px;background:var(--border)"></div>
      <button class="btn btn-ghost" onclick="_fetchCOTNow()" style="font-size:11px;color:#818cf8;border-color:#818cf8">📊 Fetch CFTC COT</button>
      <span id="cot-fetch-status" style="font-size:11px;color:var(--muted)"></span>
      <span id="futures-config-status" style="font-size:11px;color:var(--muted)"></span>
    </div>
  </div>`;

  const toolbar = `<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
    <div style="color:var(--muted);font-size:12px">Manage watchlists and fetch data.</div>
    <div style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)" title="Global -- not tied to any one watchlist. Blank means it never runs on a schedule.">
      🛢 Futures OI schedule:
      <input type="time" id="wl-future-oi-time" onchange="_wlSetFutureOiSchedule(this.value)"
        style="font-size:10px;padding:2px 4px;background:var(--bg,#0b1020);border:1px solid var(--border,#334155);color:var(--text,#e2e8f0);border-radius:3px" />
    </div>
    <button class="btn btn-secondary" onclick="_wlLoad()" style="font-size:11px;padding:4px 9px">⟳ Refresh</button>
  </div>`;

  const rows = _wlData.map(wl => {
    const oi  = wl.fetch_options_oi
      ? '<span style="font-size:11px;color:#22c55e;font-weight:600">📊 Options OI<br><span style="font-size:9px;font-weight:400;color:var(--muted)">→ options table</span></span>'
      : '<span style="font-size:11px;color:#818cf8;font-weight:600">💰 Price/Vol<br><span style="font-size:9px;font-weight:400;color:var(--muted)">→ price_cache</span></span>';
    const def = wl.is_default
      ? '<span style="color:#f59e0b;font-size:11px;font-weight:700">⭐ Yes</span>'
      : `<button onclick="_wlSetDefault(${wl.id})" style="font-size:10px;padding:2px 7px;border-radius:4px;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);color:#f59e0b;cursor:pointer">Set Default</button>`;
    const nm = wl.name.replace(/'/g, "\'");
    return `<tr style="border-bottom:1px solid var(--border);vertical-align:top">
      <td style="padding:10px 10px">
        <div style="font-weight:700;color:${wl.color||'#818cf8'};font-size:13px">${wl.name}</div>
        <div style="font-size:11px;font-weight:700;color:var(--text2);margin-top:2px">
          ${wl.symbol_count} symbols
          ${wl.sector_coverage > 0
            ? '<span style="color:var(--muted);font-weight:400;margin-left:4px">· '+wl.sector_coverage+' with sector</span>'
            : '<span style="color:#f59e0b;font-size:10px;margin-left:4px">· run 🗂 Sectors</span>'}
        </div>
        ${(wl.sectors||[]).length > 0 ? '<div style="margin-top:4px;display:flex;gap:3px;flex-wrap:wrap">' +
          (wl.sectors||[]).map(s => `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(99,102,241,.1);color:#818cf8">${s.sector} ${s.count}</span>`).join('') +
          '</div>' : ''}
      </td>
      <td style="padding:10px 10px;vertical-align:top">${oi}</td>
      <td style="padding:10px 10px;text-align:center;vertical-align:top">${def}</td>
      <td style="padding:10px 10px;vertical-align:top">
        ${_lastRunBadge(wl)}
        <span onclick="_wlShowFetchHistory(${wl.id},'${nm}')" title="See when each action last ran"
          style="cursor:pointer;color:#818cf8;font-size:10px;margin-left:6px;text-decoration:underline">ⓘ details</span>
        <div id="wl-status-${wl.id}" style="font-size:10px;color:var(--muted);margin-top:4px"></div>
      </td>
      <td style="padding:8px 10px;vertical-align:top;min-width:150px">
        ${_scheduleStepRow(wl, 'schedule_price_oi_time', 'schedule_price_oi_last_date', wl.fetch_options_oi ? 'OI' : 'Price', wl.id, false)}
        ${_scheduleStepRow(wl, 'schedule_earnings_time', 'schedule_earnings_last_date', 'Earnings', wl.id, true)}
        ${_scheduleStepRow(wl, 'schedule_indicators_time', 'schedule_indicators_last_date', 'Indicators', wl.id, true)}
        ${_scheduleStepRow(wl, 'schedule_corporate_events_time', 'schedule_corporate_events_last_date', 'Corp Events', wl.id, true)}
        ${_scheduleStepRow(wl, 'schedule_volume_profile_time', 'schedule_volume_profile_last_date', 'Vol Profile', wl.id, true)}
        ${_scheduleStepRow(wl, 'schedule_intraday_price_time', 'schedule_intraday_price_last_date', 'Intraday 2m', wl.id, false)}
      </td>
      <td style="padding:8px 10px;vertical-align:top">
        <div style="display:flex;gap:5px;flex-wrap:wrap;align-items:center">
          <button onclick="_wlRunFetch(${wl.id},'${nm}')" id="wl-run-${wl.id}"
            title="${wl.fetch_options_oi?'Fetch Options OI chain → options table':'Fetch OHLCV → price_cache table'}"
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.3);color:#22c55e;cursor:pointer;white-space:nowrap">
            ${wl.fetch_options_oi?'📊 Fetch OI':'💰 Fetch Price'}
          </button>
          <button onclick="_wlRunIntradayPrice(${wl.id},'${nm}')" id="wl-intraday-price-${wl.id}"
            title="Fetch Tastytrade extended-hours candles once, then store compact two-minute bars plus premarket high/low for backtests. It does not poll every minute."
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(14,165,233,.12);border:1px solid rgba(14,165,233,.3);color:#38bdf8;cursor:pointer;white-space:nowrap">
            ⏱ Fetch Intraday (2m)
          </button>
          ${wl.fetch_options_oi ? `
          <select id="wl-oi-source-${wl.id}" title="yfinance: fast, no real Greeks. tastytrade: real broker Greeks (delta/gamma/theta/vega), ~20s/symbol -- meant for long, unattended off-hours runs, not a quick daytime check."
            style="font-size:10px;padding:2px 4px;border-radius:4px;background:var(--card,#111827);border:1px solid var(--border,#1f2937);color:var(--muted,#94a3b8)">
            <option value="yfinance">yfinance (fast)</option>
            <option value="tastytrade">tastytrade (real Greeks, slow -- off-hours)</option>
          </select>` : ''}
          <button onclick="_wlRunSectors(${wl.id},'${nm}')"
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.25);color:#818cf8;cursor:pointer">
            🗂 Sectors
          </button>
          <button onclick="_wlRunEarnings(${wl.id},'${nm}')"
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.25);color:#f59e0b;cursor:pointer">
            📅 Earnings
          </button>
          <button onclick="_wlRunBulkBackfill(${wl.id},'${nm}')" id="wl-backfill-${wl.id}"
            title="Backfill months of daily price+volume history into price_cache -- for scanner indicators (rsidiff90 etc.) that need long history, run once, not part of the regular daily fetch"
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(56,189,248,.1);border:1px solid rgba(56,189,248,.25);color:#38bdf8;cursor:pointer">
            📈 Backfill History
          </button>
          <button onclick="_wlRunBulkBackfillIntraday(${wl.id},'${nm}')" id="wl-backfill-intraday-${wl.id}"
            title="Backfill ~2 years of hourly price+volume history into intraday_price_cache -- base data for 1h/2h/4h scanner queries (2h/4h are derived from this by resampling, not fetched separately). Run once, useful for swing-trade scans."
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.25);color:#22c55e;cursor:pointer">
            ⏱ Backfill Intraday (1h/2h/4h)
          </button>
          <button onclick="_wlRunComputeIndicators(${wl.id},'${nm}')" id="wl-compute-${wl.id}"
            title="Precompute RSI/EMA/rsidiff90/MACD/ADX/DI+-/S-R for this watchlist (daily + weekly) into the technical_snapshot cache -- speeds up scanners/scoring that read from it instead of recomputing live"
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(168,85,247,.1);border:1px solid rgba(168,85,247,.25);color:#a855f7;cursor:pointer">
            🧮 Compute Indicators
          </button>
          <button onclick="_wlRunCorpEvents(${wl.id},'${nm}')" id="wl-corpevents-${wl.id}"
            title="Fetch insider activity, debt snapshot, volume signal, and material events (SEC EDGAR) for every symbol in this watchlist -- same fetch the scheduled Corp Events step runs"
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(236,72,153,.1);border:1px solid rgba(236,72,153,.25);color:#ec4899;cursor:pointer">
            📋 Corp Events
          </button>
          <button onclick="_wlRunVolumeProfile(${wl.id},'${nm}')" id="wl-volprofile-${wl.id}"
            title="Precompute today's Volume Profile balance/imbalance scan (POC/VAH/VAL, Trend/Reversion thesis) for every symbol in this watchlist -- same fetch the scheduled Vol Profile step runs, speeds up the live Volume Profile Scanner page"
            style="font-size:11px;padding:3px 10px;border-radius:4px;background:rgba(20,184,166,.1);border:1px solid rgba(20,184,166,.25);color:#14b8a6;cursor:pointer">
            📊 Vol Profile
          </button>
        </div>
        <div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:5px">
          <button onclick="_wlLoadSymbolsPanel(${wl.id}, '${nm}')"
            style="font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25);color:#22c55e;cursor:pointer">
            Symbols
          </button>
          <button onclick="_wlEdit(${wl.id})"
            style="font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(148,163,184,.08);border:1px solid var(--border);color:var(--text2);cursor:pointer">
            ✏ Edit
          </button>
          <button onclick="_wlDelete(${wl.id},'${nm}')"
            style="font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);color:#ef4444;cursor:pointer">
            🗑 Delete
          </button>
        </div>
      </td>
    </tr>`;
  }).join('');

  wrap.innerHTML = futuresOiPanel + toolbar + `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="background:rgba(255,255,255,.05);border-bottom:2px solid var(--border)">
      <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:600">Watchlist</th>
      <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:600">Data Mode</th>
      <th style="padding:8px 10px;text-align:center;font-size:11px;color:var(--muted);font-weight:600">Default</th>
      <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:600">Last Run</th>
      <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:600" title="Each step only runs if it has a time set -- blank means it never runs on a schedule. Earnings and Indicators additionally wait for Price/OI to actually complete that day before running, regardless of their own configured time.">Schedule</th>
      <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:600">Actions</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
  _wlLoadFutureOiSchedule();
}


async function _wlRunFetch(id, name) {
  const st = document.getElementById('wl-status-'+id);
  const btn = document.getElementById('wl-run-'+id);
  const sourceEl = document.getElementById('wl-oi-source-'+id);
  const oiSource = sourceEl ? sourceEl.value : 'yfinance';
  if (btn) btn.disabled = true;
  if (st) st.textContent = oiSource === 'tastytrade' ? '⏳ Starting tastytrade fetch (this runs long -- off-hours use)…' : '⏳ Starting fetch…';
  try {
    const d = await api(`/watchlists/${id}/fetch_data`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({oi_source: oiSource}),
    });
    const msg = `⏳ ${d.mode} running for ${d.symbols} symbols → ${d.table} table`;
    if (st) st.textContent = msg;
    addNotif('ok', `${name}: ${d.mode} started`, `${d.symbols} symbols → ${d.table}`, 'Watchlists');
    // Poll status
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api(`/watchlists/${id}/fetch_status`);
        const lr = s.last_run;
        // Real error visibility -- previously this only ever showed the
        // fetched count, so a run where every symbol was failing looked
        // identical to one still quietly in progress. If a completed run
        // shows up with errors, say so explicitly instead of just "0
        // fetched" until the poll loop times out on its own.
        if (!s.running && lr && (lr.fetched + lr.errored + lr.no_options + lr.timed_out) >= lr.total) {
          const errPart = lr.errored > 0 ? ` — ⚠ ${lr.errored} errored` : '';
          const noOptPart = lr.no_options > 0 ? ` — ${lr.no_options} had no options chain` : '';
          // timed_out symbols aren't failures -- their fetches keep
          // running on the shared background pool after this request
          // stopped waiting for them, so they may still land shortly.
          const toPart = lr.timed_out > 0 ? ` — ⏳ ${lr.timed_out} still finishing in the background (check again shortly)` : '';
          if (st) st.textContent = `📊 ${s.fetched_today}/${s.symbols_total} fetched today → ${s.table}${errPart}${noOptPart}${toPart}`;
          if (lr.sample_errors && lr.sample_errors.length) {
            addNotif('error', `${name}: ${lr.errored} symbol(s) failed`, lr.sample_errors.join(' | '), 'Watchlists');
          }
          clearInterval(poll);
          if (btn) btn.disabled = false;
          await _wlLoad();
          return;
        }
        const live = s.fetch_progress || {};
        const liveSuffix = live.message ? ` — ${live.message}` : '';
        if (st) st.textContent = `${s.running ? '⏳' : '📊'} ${s.fetched_today}/${s.symbols_total} fetched today → ${s.table}${liveSuffix}`;
        // tastytrade runs far longer than yfinance (~20s/symbol at 3-way
        // concurrency vs yfinance's much faster per-symbol cost) -- the
        // original 60-poll (5 min) cutoff was tuned for yfinance and
        // would have made this polling loop give up and re-enable the
        // button well before a tastytrade fetch actually finishes,
        // making a legitimately-still-running fetch look abandoned.
        const maxPolls = oiSource === 'tastytrade' ? 400 : 60;  // ~33min vs ~5min
        if (!s.running || polls > maxPolls) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          addNotif('ok', `${name}: fetch done`, `${s.fetched_today}/${s.symbols_total} → ${s.table}`, 'Watchlists');
          // Reload table to show updated Last Run timestamp
          await _wlLoad();
        }
      } catch { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 5000);
  } catch(e) {
    if (st) st.textContent = '❌ '+e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name+': fetch failed', e.message, 'Watchlists');
  }
}

async function _wlRunComputeIndicators(id, name) {
  const st = document.getElementById('wl-status-'+id);
  const btn = document.getElementById('wl-compute-'+id);
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ Starting indicator compute…';
  try {
    const d = await api('/technical-snapshot/api/bulk-compute', {
      method: 'POST',
      body: JSON.stringify({ watchlist_id: id, timeframes: ['1d', '1w'] }),
    });
    if (d.error) throw new Error(d.error);
    addNotif('ok', `${name}: indicator compute started`, `${d.symbol_count} symbols × daily+weekly → technical_snapshot`, 'Watchlists');
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api('/technical-snapshot/api/bulk-compute/status');
        if (st) st.textContent = `🧮 Indicators: ${s.processed}/${s.total} (${s.succeeded} ok, ${s.failed} failed)`;
        if (!s.running || polls > 720) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (!s.running) {
            addNotif('ok', `${name}: indicators computed`, `${s.succeeded}/${s.total} symbol×timeframe snapshots stored`, 'Watchlists');
          }
        }
      } catch { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 5000);
  } catch (e) {
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name + ': indicator compute failed', e.message, 'Watchlists');
  }
}


async function _wlRunCorpEvents(id, name) {
  const st = document.getElementById('wl-status-'+id);
  const btn = document.getElementById('wl-corpevents-'+id);
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ Starting corporate events fetch…';
  try {
    const d = await api(`/watchlists/${id}/run-corporate-events`, {method: 'POST'});
    if (d.error) throw new Error(d.error);
    addNotif('ok', `${name}: corporate events fetch started`, `${d.symbol_count} symbols → SEC EDGAR`, 'Watchlists');
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api('/watchlists/api/corporate-events-status');
        if (st) st.textContent = `📋 Corp Events: ${s.processed}/${s.total} (${s.fetched} fetched, ${s.skipped_no_cik} no CIK, ${s.errors} errors)`;
        if (!s.running || polls > 720) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (!s.running) {
            addNotif('ok', `${name}: corporate events updated`, `${s.fetched} fetched, ${s.skipped_no_cik} skipped (no CIK), ${s.errors} error(s)`, 'Watchlists');
          }
        }
      } catch { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 3000);
  } catch (e) {
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name + ': corporate events fetch failed', e.message, 'Watchlists');
  }
}


async function _wlRunVolumeProfile(id, name) {
  const st = document.getElementById('wl-status-'+id);
  const btn = document.getElementById('wl-volprofile-'+id);
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ Starting volume profile precompute…';
  try {
    const d = await api('/scanner/volume-profile/api/precompute', {
      method: 'POST',
      body: JSON.stringify({ watchlist_id: id }),
    });
    if (d.error) throw new Error(d.error);
    addNotif('ok', `${name}: volume profile precompute started`, `${d.symbol_count} symbols → vp_scan_results cache`, 'Watchlists');
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api('/scanner/volume-profile/api/precompute/status');
        if (st) st.textContent = `📊 Vol Profile: ${s.processed}/${s.total} (${s.computed} computed, ${s.errors} errors)`;
        if (!s.running || polls > 720) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (!s.running) {
            addNotif('ok', `${name}: volume profile computed`, `${s.computed}/${s.total} symbols cached for today`, 'Watchlists');
          }
        }
      } catch { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 3000);
  } catch (e) {
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name + ': volume profile precompute failed', e.message, 'Watchlists');
  }
}


async function _wlRunBulkBackfillIntraday(id, name) {
  const days = prompt(`Backfill how many days of hourly price+volume history for "${name}"?\n\nBase data for 1h scanner queries -- 2h and 4h are derived from this same data by resampling, not fetched separately. Default 729 is yfinance's own max for hourly data (~2 years), which comfortably covers the convergence threshold rsidiff90 needs even on 4h.`, '729');
  if (!days) return;
  const daysNum = parseInt(days, 10);
  if (!daysNum || daysNum < 1) { alert('Enter a number of days, e.g. 729'); return; }

  const st = document.getElementById('wl-status-'+id);
  const btn = document.getElementById('wl-backfill-intraday-'+id);
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ Starting intraday backfill…';
  try {
    const d = await api('/scanner-builder/api/bulk-backfill-intraday', {
      method: 'POST',
      body: JSON.stringify({ watchlist_id: id, days: daysNum }),
    });
    if (d.error) throw new Error(d.error);
    addNotif('ok', `${name}: intraday backfill started`, `${d.symbol_count} symbols, ${d.days} days → intraday_price_cache`, 'Watchlists');
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api('/scanner-builder/api/bulk-backfill-intraday/status');
        if (st) st.textContent = `⏱ Intraday: ${s.processed}/${s.total} (${s.succeeded} ok, ${s.failed} failed)${s.current_symbol ? ' — ' + s.current_symbol : ''}`;
        if (!s.running || polls > 720) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (!s.running) {
            addNotif('ok', `${name}: intraday backfill done`, `${s.succeeded}/${s.total} symbols backfilled`, 'Watchlists');
          }
        }
      } catch { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 5000);
  } catch (e) {
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name + ': intraday backfill failed', e.message, 'Watchlists');
  }
}

async function _wlRunBulkBackfill(id, name) {
  const months = prompt(`Backfill how many months of daily price+volume history for "${name}"?\n\nThis is separate from the regular daily fetch above -- run this once (e.g. over a weekend) to give scanner indicators that need long history enough data to compute correctly. Default is 132 (11 years): rsidiff90() on a WEEKLY timeframe specifically needs about 10.4 years of history to converge (same math as daily, just measured in weekly bars) -- less than that and rsidiff90("1w") will show no value at all, not just a less-accurate one.`, '132');
  if (!months) return;
  const monthsNum = parseInt(months, 10);
  if (!monthsNum || monthsNum < 1) { alert('Enter a number of months, e.g. 36'); return; }

  const st = document.getElementById('wl-status-'+id);
  const btn = document.getElementById('wl-backfill-'+id);
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ Starting backfill…';
  try {
    const d = await api('/scanner-builder/api/bulk-backfill', {
      method: 'POST',
      body: JSON.stringify({ watchlist_id: id, months: monthsNum }),
    });
    if (d.error) throw new Error(d.error);
    addNotif('ok', `${name}: backfill started`, `${d.symbol_count} symbols, ${d.months} months → price_cache`, 'Watchlists');
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api('/scanner-builder/api/bulk-backfill/status');
        if (st) st.textContent = `📈 Backfill: ${s.processed}/${s.total} (${s.succeeded} ok, ${s.failed} failed)${s.current_symbol ? ' — ' + s.current_symbol : ''}`;
        if (!s.running || polls > 720) {  // 720 * 5s = up to 1hr of polling before giving up watching
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (!s.running) {
            addNotif('ok', `${name}: backfill done`, `${s.succeeded}/${s.total} symbols backfilled`, 'Watchlists');
          }
        }
      } catch { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 5000);
  } catch (e) {
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name + ': backfill failed', e.message, 'Watchlists');
  }
}


async function _wlFetchSelected() {
  const sel = document.getElementById('wl-fetch-watchlist');
  const wlId = sel ? (sel.value || '') : '';
  const st = document.getElementById('wl-fetch-status');
  if (st) st.textContent = '⏳ Starting selected watchlist fetch…';
  try {
    await api(`/api/fetch_now${wlId ? '?watchlist_id='+wlId : ''}`, {method:'POST'});
    if (st) st.textContent = '✅ Fetch started';
    _pollFetchStatus(st, 'selected');
  } catch(e) {
    if (st) st.textContent = '❌ '+e.message;
  }
}

async function _wlFetchAll() {
  const st = document.getElementById('wl-fetch-status');
  if (st) st.textContent = '⏳ Starting full watchlist sweep…';
  try {
    await api('/api/fetch_now', {method:'POST'});
    if (st) st.textContent = '✅ Full sweep started';
    _pollFetchStatus(st, 'all');
  } catch(e) {
    if (st) st.textContent = '❌ '+e.message;
  }
}

function _fmtAgo(iso) {
  if (!iso) return null;
  const d = new Date(iso.replace(' ', 'T'));
  const hrs = Math.round((Date.now() - d) / 3600000);
  if (hrs < 1) return 'just now';
  if (hrs < 24) return hrs + 'h ago';
  return Math.round(hrs / 24) + 'd ago';
}

async function _wlShowFetchHistory(wlId, wlName) {
  const existing = document.getElementById('wl-history-popover');
  if (existing) existing.remove();

  const overlay = document.createElement('div');
  overlay.id = 'wl-history-popover';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center';
  overlay.innerHTML = `<div style="background:#111827;color:#e5eefc;border-radius:12px;padding:20px;width:420px;max-width:92vw;box-shadow:0 12px 40px rgba(0,0,0,.5);border:1px solid rgba(255,255,255,.1)">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h3 style="margin:0;font-size:15px">Fetch history — ${wlName}</h3>
      <button id="wl-history-close" style="background:transparent;border:none;color:#9ca3af;font-size:18px;cursor:pointer;padding:2px 6px">✕</button>
    </div>
    <div id="wl-history-body" style="font-size:12.5px;color:var(--muted)">Loading…</div>
  </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  document.getElementById('wl-history-close').addEventListener('click', close);
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

  try {
    const d = await api(`/watchlists/${wlId}/fetch_history`);
    const labels = {
      fetch_price: '💰 Fetch Price', fetch_oi: '📊 Fetch OI',
      backfill_history: '📈 Backfill History', backfill_intraday: '⏱ Backfill Intraday',
      fetch_intraday_price: '⏱ Fetch Intraday (2m)',
    };
    const rows = Object.entries(labels).map(([key, label]) => {
      const a = d.actions[key];
      if (!a) return `<div style="display:flex;justify-content:space-between;padding:7px 0;border-top:1px solid rgba(255,255,255,.08)">
        <span>${label}</span><span style="color:#6b7280">never run</span></div>`;
      const ok = a.status === 'ok';
      const ago = _fmtAgo(a.started_at);
      return `<div style="display:flex;justify-content:space-between;padding:7px 0;border-top:1px solid rgba(255,255,255,.08)">
        <span>${label}</span>
        <span style="color:${ok ? '#22c55e' : '#ef4444'}" title="${a.note || ''}">${ok ? '✅' : '❌'} ${ago}${a.duration_sec != null ? ` · ${a.duration_sec}s` : ''}</span>
      </div>`;
    }).join('');
    document.getElementById('wl-history-body').innerHTML = rows;
  } catch (e) {
    document.getElementById('wl-history-body').innerHTML = `<span style="color:#ef4444">Failed to load: ${e.message}</span>`;
  }
}

function _pollFetchStatus(st, label) {
  let ticks = 0;
  const timer = setInterval(async () => {
    ticks++;
    try {
      const s = await api('/api/fetch_status');
      if (st) st.textContent = s.running ? `⏳ ${s.last_msg || 'running'}` : `✅ ${s.last_msg || 'done'}`;
      if (!s.running || ticks > 120) {
        clearInterval(timer);
        if (st) st.textContent = s.last_msg || 'done';
        // This was the actual bug: the button looked like it worked (status
        // text said "done"), but the table's "Last Run" column never
        // refreshed, so it kept showing stale data until you manually
        // reloaded the whole page. Reload the table now that the fetch is
        // actually finished.
        if (typeof _wlLoad === 'function') _wlLoad();
      }
    } catch {
      if (ticks > 10) clearInterval(timer);
    }
  }, 3000);
}

async function _wlRunSectors(id, name) {
  const st  = document.getElementById('wl-status-'+id);
  const btn = document.getElementById('wl-sec-'+id);
  if (btn) btn.disabled = true;
  if (st)  st.textContent = '⏳ Starting sector update…';
  try {
    const d = await api(`/api/update_sectors?force=false&watchlist_id=${id}`, {method:'POST'});
    if (!d.ok) throw new Error(d.error || 'Start failed');
    if (st) st.textContent = `⏳ Updating sectors for ${d.total} symbols (background)…`;
    addNotif('info', name+': sector update started', d.total+' symbols', 'Watchlists');
    // Poll every 5s until done
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api('/api/update_sectors_status');
        if (st) st.textContent = `⏳ Sectors: ${s.updated} updated · ${s.skipped} skipped · ${s.failed} failed / ${s.total}`;
        if (s.done || polls > 120) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (st) st.textContent = `✅ Sectors done: ${s.updated} updated · ${s.skipped} skipped · ${s.failed} no-sector`;
          addNotif('ok', name+': sectors updated', `${s.updated}/${s.total} · ${s.failed} ETFs/unknown`, 'Watchlists');
          await _wlLoad(); // refresh to show new sector badges
        }
      } catch { clearInterval(poll); if (btn) btn.disabled = false; }
    }, 5000);
  } catch(e) {
    if (st) st.textContent = '❌ '+e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name+': sectors failed', e.message, 'Watchlists');
  }
}

const _WL_SCHEDULE_FIELD_LABELS = {
  schedule_price_oi_time: 'Price/OI',
  schedule_earnings_time: 'Earnings',
  schedule_indicators_time: 'Indicators',
  schedule_corporate_events_time: 'Corp Events',
  schedule_volume_profile_time: 'Vol Profile',
  schedule_intraday_price_time: 'Intraday 2m',
};

async function _wlSetSchedule(id, field, value) {
  // PUT /watchlists/<id> already accepts these 3 fields (extended in the
  // backend refactor) -- an empty string clears the schedule for that
  // step (matches "if there is no time it shall not execute": the
  // scheduler's `if t_price:`-style checks treat '' the same as null).
  const fieldLabel = _WL_SCHEDULE_FIELD_LABELS[field] || field;
  const wl = (_wlData || []).find(w => w.id === id);
  const wlName = wl ? wl.name : `#${id}`;
  try {
    await api(`/watchlists/${id}`, {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[field]: value})});
    const dotInput = document.getElementById(`wl-sched-${field}-${id}`);
    if (dotInput) dotInput.style.borderColor = '#22c55e';
    setTimeout(() => { if (dotInput) dotInput.style.borderColor = ''; }, 1200);
    // Explicit, persistent confirmation via the same bell/notification
    // system everything else in the app already uses -- the border
    // flash alone was too easy to miss and vanished after 1.2s with no
    // way to confirm later whether it actually saved.
    addNotif('ok', 'Schedule saved',
      value ? `"${wlName}" — ${fieldLabel} will run at ${value} daily` : `"${wlName}" — ${fieldLabel} schedule cleared (won't run automatically)`,
      'Watchlists');
  } catch (e) {
    addNotif('error', 'Schedule update failed', `"${wlName}" — ${fieldLabel}: ${e.message}`, 'Watchlists');
    _wlLoad();  // revert the input to whatever's actually saved
  }
}

async function _wlLoadFutureOiSchedule() {
  try {
    const d = await api('/watchlists/future-oi-schedule');
    const el = document.getElementById('wl-future-oi-time');
    if (el) el.value = d.time || '';
  } catch (e) { /* non-critical */ }
}

async function _wlSetFutureOiSchedule(value) {
  try {
    await api('/watchlists/future-oi-schedule', {method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({time: value})});
    const el = document.getElementById('wl-future-oi-time');
    if (el) { el.style.borderColor = '#22c55e'; setTimeout(() => { el.style.borderColor = ''; }, 1200); }
    addNotif('ok', 'Schedule saved',
      value ? `Futures OI fetch will run at ${value} daily` : `Futures OI schedule cleared (won't run automatically)`,
      'Watchlists');
  } catch (e) {
    addNotif('error', 'Futures OI schedule update failed', e.message, 'Watchlists');
  }
}


async function _wlRunEarnings(id, name) {
  const st = document.getElementById('wl-status-'+id);
  if (st) st.textContent = '⏳ Starting earnings + fundamentals fetch…';
  const t0 = Date.now();
  try {
    const start = await api(`/earnings/fetch_calendar?force=false&watchlist_id=${id}`, {method:'POST'});
    if (start.error) throw new Error(start.error);
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api('/earnings/fetch_status');
        if (st) st.textContent = `⏳ Earnings: ${s.processed}/${s.total} (${s.calendar_updated} updated, ${s.calendar_skipped} already current) · Fundamentals: ${s.fundamentals_fetched} fetched`;
        if (!s.running || polls > 720) {
          clearInterval(poll);
          const elapsed = ((Date.now()-t0)/1000).toFixed(0);
          const fundBits = ` · Fundamentals: ${s.fundamentals_fetched} fetched, ${s.fundamentals_skipped} unchanged (skipped)${s.fundamentals_errored ? `, ${s.fundamentals_errored} failed` : ''}`;
          if (st) st.textContent = `✅ Earnings: ${s.calendar_updated} updated · ${s.calendar_skipped} already current · ${s.calendar_failed} failed${fundBits} · ${elapsed}s`;
          addNotif('ok', name+': earnings + fundamentals updated', `${s.calendar_updated} calendar updated, ${s.fundamentals_fetched||0} fundamentals fetched, ${s.calendar_skipped} already current`, 'Watchlists');
        }
      } catch(e) {
        clearInterval(poll);
        if (st) st.textContent = '❌ '+e.message;
      }
    }, 2000);
  } catch(e) {
    if (st) st.textContent = '❌ '+e.message;
    addNotif('error', name+': earnings failed', e.message, 'Watchlists');
  }
}

function _wlParseSymbols(rawSymbols) {
  const allowed = /^[A-Z0-9.$=^_-]{1,40}$/;
  const seen = new Set();
  const symbols = [];
  const rejected = [];
  String(rawSymbols || '')
    .split(/[,\n\r\t ]+/)
    .map(s => String(s || '').trim().toUpperCase().replace(/^[,;]+|[,;]+$/g, ''))
    .filter(Boolean)
    .forEach(sym => {
      if (!allowed.test(sym)) { rejected.push(sym); return; }
      if (!seen.has(sym)) { seen.add(sym); symbols.push(sym); }
    });
  symbols.sort();
  return {symbols, rejected};
}

function _wlShowAddForm() {
  _wlEditingId = null;
  document.getElementById('wl-form-title').textContent = 'New Watchlist';
  document.getElementById('wl-f-id').value = '';
  document.getElementById('wl-f-name').value = '';
  document.getElementById('wl-f-symbols').value = '';
  document.getElementById('wl-f-oi').checked = false;
  document.getElementById('wl-f-default').checked = false;
  document.getElementById('wl-form-status').textContent = '';
  document.getElementById('wl-form-panel').style.display = 'block';
  document.getElementById('wl-f-name').focus();
}

async function _wlEdit(id) {
  _wlEditingId = id;
  const wl = _wlData.find(w => w.id === id);
  if (!wl) return;
  document.getElementById('wl-form-title').textContent = 'Edit: '+wl.name;
  document.getElementById('wl-f-id').value = id;
  document.getElementById('wl-f-name').value = wl.name;
  document.getElementById('wl-f-oi').checked = !!wl.fetch_options_oi;
  document.getElementById('wl-f-default').checked = !!wl.is_default;
  document.getElementById('wl-form-status').textContent = '⏳ Loading symbols…';
  document.getElementById('wl-form-panel').style.display = 'block';
  try {
    const d = await api(`/watchlists/${id}/symbols?prices=0`);
    document.getElementById('wl-f-symbols').value = (d.symbols||[]).map(s=>s.symbol).join(', ');
    document.getElementById('wl-form-status').textContent = `${d.count} symbols`;
  } catch(e) {
    document.getElementById('wl-form-status').textContent = '❌ '+e.message;
  }
  document.getElementById('wl-form-panel').scrollIntoView({behavior:'smooth',block:'start'});
}

function _wlFormCancel() {
  document.getElementById('wl-form-panel').style.display = 'none';
  _wlEditingId = null;
}

async function _wlFormSave() {
  const st   = document.getElementById('wl-form-status');
  const name = (document.getElementById('wl-f-name')?.value || '').trim();
  const rawSymbols = document.getElementById('wl-f-symbols')?.value || '';
  const fetchOI    = document.getElementById('wl-f-oi')?.checked ? 1 : 0;
  const isDefault  = document.getElementById('wl-f-default')?.checked ? 1 : 0;
  const editId     = document.getElementById('wl-f-id')?.value;

  if (!name) { alert('Watchlist name is required'); return; }

  // Parse symbols. Keep yfinance futures/FX/index suffixes such as GC=F, EURUSD=X, ^VIX.
  const parsed = _wlParseSymbols(rawSymbols);
  const symbols = parsed.symbols;
  const rejected = parsed.rejected;
  if (rejected.length && st) {
    st.textContent = `⚠ Ignoring ${rejected.length} invalid symbol(s): ${rejected.slice(0,8).join(', ')}`;
    await new Promise(r => setTimeout(r, 350));
  }

  if (st) st.textContent = '⏳ Saving…';

  try {
    if (editId) {
      // Update metadata
      await api(`/watchlists/${editId}`, {method:'PUT',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name, fetch_options_oi:fetchOI, description:''})});
      // Update symbols
      await api(`/watchlists/${editId}/symbols/replace`, {method:'POST',
        headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbols})});
      // Set default
      if (isDefault) await api(`/watchlists/${editId}/set_default`, {method:'POST'});
    } else {
      // Create
      const cr = await api('/watchlists/', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({name, fetch_options_oi:fetchOI, description:''})});
      const newId = cr.id;
      if (symbols.length) {
        await api(`/watchlists/${newId}/symbols/replace`, {method:'POST',
          headers:{'Content-Type':'application/json'}, body: JSON.stringify({symbols})});
      }
      if (isDefault) await api(`/watchlists/${newId}/set_default`, {method:'POST'});
    }
    if (st) st.textContent = `✅ Saved: ${symbols.length} symbols${rejected.length ? ` · ${rejected.length} ignored` : ''}`;
    setTimeout(() => { document.getElementById('wl-form-panel').style.display='none'; _wlEditingId=null; }, 800);
    await _wlLoad();
    addNotif('ok', editId ? 'Watchlist updated' : 'Watchlist created', name+' ('+symbols.length+' symbols)', 'Watchlists');
  } catch(e) {
    if (st) st.textContent = '❌ '+e.message;
    addNotif('error', 'Save failed', e.message, 'Watchlists');
  }
}

async function _wlDelete(id, name) {
  if (!confirm(`Delete watchlist "${name}"?\nAll ${(_wlData.find(w=>w.id===id)||{}).symbol_count||0} symbols will be removed from this list.`)) return;
  try {
    await api(`/watchlists/${id}`, {method:'DELETE'});
    await _wlLoad();
    addNotif('ok', 'Deleted: '+name, '', 'Watchlists');
  } catch(e) { addNotif('error', 'Delete failed', e.message, 'Watchlists'); }
}


async function _wlRunIntradayPrice(id, name) {
  const st = document.getElementById('wl-status-' + id);
  const btn = document.getElementById('wl-intraday-price-' + id);
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ Starting extended-hours 2m fetch…';
  try {
    const result = await api(`/watchlists/${id}/fetch_intraday_price`, { method: 'POST' });
    if (!result.ok) throw new Error(result.error || 'Start failed');
    addNotif('info', name + ': intraday fetch started',
      'One end-of-day Tastytrade request per symbol; compact 2m bars + premarket high/low will be saved.', 'Watchlists');
    // Keep the row state visible for the whole broker operation. The old
    // three-second cooldown made a still-running job look finished.
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      try {
        const s = await api(`/watchlists/${id}/fetch_status`);
        const p = s.intraday_progress || {};
        const count = p.symbols ?? s.symbols_total;
        const bars = p.bars != null ? ` · ${p.bars} 2m bars` : '';
        if (st) st.textContent = `${s.intraday_running ? '⏳' : '✅'} Intraday 2m: ${p.message || (s.intraday_running ? 'Running' : 'Completed')} · ${count} symbol(s)${bars}`;
        if (!s.intraday_running || polls > 720) {
          clearInterval(poll);
          if (btn) btn.disabled = false;
          if (!s.intraday_running) {
            const failed = /^Failed:/.test(String(p.message || ''));
            addNotif(failed ? 'error' : 'ok', name + (failed ? ': intraday fetch failed' : ': intraday fetch done'),
              p.message || `${count} symbol(s)${bars}`, 'Watchlists');
            await _wlLoad();
          }
        }
      } catch (e) {
        clearInterval(poll);
        if (st) st.textContent = '❌ Live progress unavailable: ' + e.message;
        if (btn) btn.disabled = false;
      }
    }, 2000);
  } catch (e) {
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', name + ': intraday fetch failed', e.message, 'Watchlists');
  }
}

