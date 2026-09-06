'use strict';

function $(id) { return document.getElementById(id); }

// Global fetch timeout, shared across Scanner Builder/Dashboard/Signal
// Notifier/AI Copilot. Configured once from Scanner Builder → Settings.
let _globalApiTimeoutMs = 120000;
async function loadGlobalApiTimeout() {
  try {
    const r = await fetch('/scanner-builder/api/settings/timeout');
    const d = await r.json();
    if (d && Number.isFinite(Number(d.timeout_sec))) {
      _globalApiTimeoutMs = Math.max(10, Number(d.timeout_sec)) * 1000;
    }
  } catch (e) {
    console.warn('Could not load global API timeout setting, using default', e);
  }
}

function api(url, opts = {}) {
  const controller = new AbortController();
  const timeoutMs = opts.timeoutMs || _globalApiTimeoutMs;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const finalOpts = { ...opts, signal: controller.signal };
  return fetch(url, finalOpts).then(async r => {
    clearTimeout(timer);
    if (!r.ok) {
      let msg = '';
      try { const j = await r.clone().json(); msg = j.error || j.message || ''; } catch {}
      throw new Error(msg || `${r.status} ${r.statusText}`);
    }
    return r.json();
  }).catch(err => {
    clearTimeout(timer);
    if (err && err.name === 'AbortError') throw new Error(`Request timed out after ${Math.round(timeoutMs/1000)}s — you can raise this in Scanner Builder → Settings.`);
    throw err;
  });
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}

const state = {
  watchlists: [],
  dashboards: [],
  scanners: [],
  sources: [],
  editingSourceId: null,
  editingKind: 'trade_scanner',
  saving: false,
  selectedSourceIds: new Set(),
  alertSourceLabels: [],
  backtestResults: [],
  backtestSort: { key: null, dir: 1 },
};

function showModalError(msg) {
  const el = $('sn-modal-error');
  if (!el) return;
  if (!msg) { el.style.display = 'none'; el.textContent = ''; return; }
  el.textContent = `⚠ ${msg}`;
  el.style.display = 'block';
}

function watchlistOptionsHtml(selected) {
  const opts = ['<option value="">All symbols</option>'];
  for (const w of state.watchlists) {
    const sel = String(w.id) === String(selected) ? 'selected' : '';
    opts.push(`<option value="${w.id}" ${sel}>${esc(w.name)} (${w.symbol_count || 0})</option>`);
  }
  return opts.join('');
}

async function loadWatchlists() {
  const d = await api('/signal-notifier/picker/watchlists');
  state.watchlists = d.watchlists || [];
}

async function loadDashboards() {
  const d = await api('/signal-notifier/picker/dashboards');
  state.dashboards = d.dashboards || [];
}

async function loadScanners() {
  const d = await api('/signal-notifier/picker/scanners');
  state.scanners = d.scanners || [];
}

async function loadSources() {
  const d = await api('/signal-notifier/sources');
  state.sources = d.sources || [];
}

async function loadGlobalConfig() {
  const cfg = await api('/signal-notifier/config');
  $('sn-global-enabled').checked = !!cfg.enabled;
  $('sn-ai-gate-enabled').checked = !!cfg.ai_gate_enabled;
  $('sn-ai-gate-legend').style.display = cfg.ai_gate_enabled ? 'block' : 'none';
  $('sn-default-watchlist').innerHTML = watchlistOptionsHtml(cfg.watchlist_id || '');
  $('sn-default-min-score').value = cfg.min_score ?? 70;
  $('sn-default-interval').value = Math.max(1, Math.round((cfg.interval_sec ?? 900) / 60));
  $('sn-schedule-kind').value = cfg.schedule_kind === 'time' ? 'time' : 'interval';
  state.scheduleTimes = Array.isArray(cfg.schedule_times) ? [...cfg.schedule_times] : [];
  renderScheduleTimes();
  updateScheduleModeVisibility();

  $('sn-journal-pnl-enabled').checked = cfg.journal_pnl_alerts_enabled !== false;
  $('sn-journal-pnl-interval').value = Math.max(1, Math.round((cfg.journal_pnl_alerts_interval_sec ?? 14400) / 60));
  $('sn-journal-deep-loss-enabled').checked = cfg.journal_deep_loss_alerts_enabled !== false;
  $('sn-journal-deep-loss-threshold').value = cfg.journal_deep_loss_threshold_pct ?? 75;
  $('sn-journal-health-enabled').checked = cfg.journal_health_alerts_enabled !== false;
  $('sn-journal-health-interval').value = Math.max(1, Math.round((cfg.journal_health_alerts_interval_sec ?? 300) / 60));
  $('sn-telegram-price-enabled').checked = cfg.telegram_price_alerts_enabled !== false;
  $('sn-telegram-price-interval').value = Math.max(1, Math.round((cfg.telegram_price_alerts_interval_sec ?? 60) / 60));
}

function renderScheduleTimes() {
  const list = $('sn-schedule-times-list');
  const times = (state.scheduleTimes || []).slice().sort();
  list.innerHTML = times.map(t => `
    <span class="sn-time-chip">${t}<button type="button" data-time="${t}" class="sn-time-remove">×</button></span>
  `).join('') || '<span class="sn-sub">No times added yet.</span>';
  list.querySelectorAll('.sn-time-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      state.scheduleTimes = (state.scheduleTimes || []).filter(t => t !== btn.dataset.time);
      renderScheduleTimes();
    });
  });
}

function updateScheduleModeVisibility() {
  const isTime = $('sn-schedule-kind').value === 'time';
  $('sn-schedule-interval-row').style.display = isTime ? 'none' : '';
  $('sn-schedule-times-row').style.display = isTime ? '' : 'none';
}

// Per-source counterparts to renderScheduleTimes()/updateScheduleModeVisibility()
// above -- same pattern, separate state (state.sourceFormScheduleTimes)
// since the add/edit form is reused across many different sources, each
// with its own independent set of times, unlike the single global default
// schedule those functions manage.
function renderSourceScheduleTimes() {
  const list = $('sn-f-schedule-times-list');
  const times = (state.sourceFormScheduleTimes || []).slice().sort();
  list.innerHTML = times.map(t => `
    <span class="sn-time-chip">${t}<button type="button" data-time="${t}" class="sn-time-remove">×</button></span>
  `).join('') || '<span class="sn-sub">No times added yet.</span>';
  list.querySelectorAll('.sn-time-remove').forEach(btn => {
    btn.addEventListener('click', () => {
      state.sourceFormScheduleTimes = (state.sourceFormScheduleTimes || []).filter(t => t !== btn.dataset.time);
      renderSourceScheduleTimes();
    });
  });
}

function updateSourceScheduleModeVisibility() {
  const isTime = $('sn-f-schedule-kind').value === 'time';
  $('sn-f-schedule-times-row').style.display = isTime ? '' : 'none';
}

function fmtSourceMeta(s) {
  const parts = [];
  if (s.schedule_kind === 'time') {
    const times = (s.schedule_times || []).slice().sort();
    parts.push(times.length ? `at ${times.join(', ')}` : 'no times configured -- never runs');
  } else {
    const mins = Math.max(1, Math.round((s.interval_sec || 900) / 60));
    parts.push(`every ${mins} min${mins === 1 ? '' : 's'}`);
  }
  if (s.kind === 'trade_scanner') {
    parts.push(`min score ${s.min_score}`);
    const wl = state.watchlists.find(w => String(w.id) === String(s.watchlist_id));
    parts.push(wl ? `watchlist: ${wl.name}` : 'watchlist: all symbols');
  } else if (s.kind === 'dashboard_tile') {
    const dash = state.dashboards.find(d => String(d.id) === String(s.dashboard_id));
    const tile = dash?.tiles?.find(t => String(t.id) === String(s.tile_id));
    parts.push(dash ? `dashboard: ${dash.name}` : `dashboard #${s.dashboard_id}`);
    parts.push(tile ? `tile: ${tile.title}` : `tile: ${s.tile_id || '?'}`);
    if (s.min_conviction_score) parts.push(`min score ${s.min_conviction_score}`);
    if ((s.direction_tags || []).length) parts.push(`🎯 ${s.direction_tags.join('/')}`);
  } else if (s.kind === 'scanner_query') {
    if (s.definition_id) {
      const def = state.scanners.find(sc => String(sc.id) === String(s.definition_id));
      parts.push(def ? `scanner: ${def.name}` : `scanner #${s.definition_id}`);
    } else {
      parts.push('custom query');
    }
    const wl = state.watchlists.find(w => String(w.id) === String(s.watchlist_id));
    parts.push(wl ? `watchlist: ${wl.name}` : 'watchlist: all symbols');
    if (s.min_conviction_score) parts.push(`min score ${s.min_conviction_score}`);
    if ((s.direction_tags || []).length) parts.push(`🎯 ${s.direction_tags.join('/')}`);
  } else if (s.kind === 'swing_positioning') {
    const wl = state.watchlists.find(w => String(w.id) === String(s.watchlist_id));
    parts.push(wl ? `watchlist: ${wl.name}` : 'watchlist: all with options OI');
    parts.push(`min confidence ${s.min_confidence ?? 40}%`);
  }
  if (s.last_run_at) parts.push(`last run: ${s.last_run_at}`);
  if (s.last_error) parts.push(`⚠ ${s.last_error}`);
  return parts;
}

const KIND_LABELS = { trade_scanner: 'Trade Scanner', dashboard_tile: 'Dashboard Tile', scanner_query: 'Scanner Query', swing_positioning: 'Swing Positioning Scanner' };

function renderSources() {
  const wrap = $('sn-sources');
  // Selection set may reference sources that no longer exist (deleted
  // since the last render) -- prune before rendering so the count/bar
  // never shows a stale number.
  const liveIds = new Set(state.sources.map(s => s.id));
  state.selectedSourceIds.forEach(id => { if (!liveIds.has(id)) state.selectedSourceIds.delete(id); });

  if (!state.sources.length) {
    wrap.innerHTML = '<div class="sn-empty">No extra sources yet. Click "+ Add source" to alert on a Scanner Dashboard tile, a saved Scanner Builder query, or another Trade Opportunity Scanner sweep.</div>';
    updateBulkBar();
    return;
  }
  wrap.innerHTML = state.sources.map(s => `
    <div class="sn-source-card" data-id="${s.id}">
      <div class="sn-source-head">
        <div class="sn-source-title">
          <input type="checkbox" class="sn-select-cb" data-id="${s.id}" ${state.selectedSourceIds.has(s.id) ? 'checked' : ''} title="Select for bulk enable/disable" />
          <label class="sn-switch"><input type="checkbox" data-action="toggle" data-id="${s.id}" ${s.enabled ? 'checked' : ''}><span class="sn-slider"></span></label>
          ${esc(s.label)}
          <span class="sn-kind-tag">${esc(KIND_LABELS[s.kind] || s.kind)}</span>
        </div>
        <div class="sn-source-actions">
          <button class="sn-btn sm" data-action="dry-run" data-id="${s.id}">🧪 Test</button>
          <button class="sn-btn sm" data-action="edit" data-id="${s.id}">✎ Edit</button>
          <button class="sn-btn sm bad" data-action="delete" data-id="${s.id}">🗑</button>
        </div>
      </div>
      <div class="sn-source-meta">${fmtSourceMeta(s).map(p => `<span>${esc(p)}</span>`).join('')}</div>
    </div>
  `).join('');
  updateBulkBar();
}

function updateBulkBar() {
  const n = state.selectedSourceIds.size;
  const countEl = $('sn-select-count');
  if (countEl) countEl.textContent = n ? `${n} selected` : '';
  const enableBtn = $('sn-bulk-enable');
  const disableBtn = $('sn-bulk-disable');
  if (enableBtn) enableBtn.disabled = n === 0;
  if (disableBtn) disableBtn.disabled = n === 0;
  const selectAllCb = $('sn-select-all');
  if (selectAllCb) {
    selectAllCb.checked = n > 0 && n === state.sources.length;
    selectAllCb.indeterminate = n > 0 && n < state.sources.length;
  }
}

async function bulkSetEnabled(enabled) {
  const ids = Array.from(state.selectedSourceIds);
  if (!ids.length) return;
  const label = enabled ? 'Enable selected' : 'Disable selected';
  const btn = $(enabled ? 'sn-bulk-enable' : 'sn-bulk-disable');
  if (btn) { btn.disabled = true; btn.textContent = `${label}…`; }
  // Sequential, not Promise.all -- keeps the bulk action from hammering
  // the backend with N simultaneous requests for a large selection, and
  // means a single failure is easy to attribute to a specific source
  // rather than an ambiguous batch rejection.
  let failed = 0;
  for (const id of ids) {
    try {
      await api(`/signal-notifier/sources/${id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }),
      });
    } catch (err) {
      failed++;
    }
  }
  if (btn) { btn.textContent = label; }
  state.selectedSourceIds.clear();
  await loadSources();
  renderSources();
  if (failed) {
    alert(`${label}: ${ids.length - failed} succeeded, ${failed} failed. Check individual sources.`);
  }
}

async function refreshAll() {
  await Promise.all([loadWatchlists(), loadDashboards(), loadScanners()]);
  await Promise.all([loadGlobalConfig(), loadSources()]);
  renderSources();
  await refreshTelegramStatus();
  await refreshHistory().catch(err => console.warn('history refresh failed', err));
}

async function refreshTelegramStatus() {
  try {
    const d = await api('/signal-notifier/telegram-status');
    $('sn-telegram-status').textContent = `Telegram: ${d.telegram_configured ? 'connected' : 'not configured'}`;
  } catch {
    $('sn-telegram-status').textContent = 'Telegram: unknown';
  }
}

function _fmtDateInput(d) {
  return d.toISOString().slice(0, 10);
}

function _defaultHistoryRange() {
  const to = new Date();
  const from = new Date();
  from.setDate(from.getDate() - 4); // "last 5 days" inclusive of today
  return { from: _fmtDateInput(from), to: _fmtDateInput(to) };
}

async function _loadAlertSourceLabels() {
  try {
    const d = await api('/signal-notifier/history/sources');
    state.alertSourceLabels = d.sources || [];
    const opts = ['<option value="">All scanners</option>'].concat(
      state.alertSourceLabels.map(s => `<option value="${esc(s)}">${esc(s)}</option>`)
    ).join('');
    ['sn-history-source', 'sn-backtest-source'].forEach(id => {
      const el = $(id);
      if (el) el.innerHTML = opts;
    });
  } catch (e) { /* non-fatal, filters just show empty dropdown */ }
}

async function refreshHistory() {
  const from = $('sn-history-from')?.value || '';
  const to = $('sn-history-to')?.value || '';
  const symbol = $('sn-history-symbol')?.value.trim().toUpperCase() || '';
  const sourceLabel = $('sn-history-source')?.value || '';
  const params = new URLSearchParams({ limit: '300' });
  if (from) params.set('from', from);
  if (to) params.set('to', to);
  if (symbol) params.set('symbol', symbol);
  if (sourceLabel) params.set('source_label', sourceLabel);
  const d = await api(`/signal-notifier/history?${params.toString()}`);
  const rows = d.alerts || [];
  state.historyRows = rows;
  const aiBadge = (v) => {
    if (!v) return '<span style="color:var(--sn-muted)">—</span>';
    const color = v === 'REJECT' ? '#fca5a5' : v === 'CAUTION' ? '#fcd34d' : '#86efac';
    const bg = v === 'REJECT' ? 'rgba(239,68,68,.14)' : v === 'CAUTION' ? 'rgba(245,158,11,.14)' : 'rgba(34,197,94,.14)';
    return `<span style="color:${color};background:${bg};padding:2px 7px;border-radius:999px;font-size:11px;font-weight:700">${esc(v)}</span>`;
  };
  $('sn-history-body').innerHTML = rows.length ? rows.map(r => `
    <tr>
      <td>${esc(r.sent_at || '')}</td>
      <td><b>${esc(r.symbol || '')}</b></td>
      <td>${esc(r.source_label || '—')}${r.source_kind ? ` <span style="color:var(--sn-muted);font-size:11px">(${esc(r.source_kind)})</span>` : ''}</td>
      <td>${esc(r.bucket || '')}</td>
      <td>${esc(r.grade || '—')}</td>
      <td>${esc(r.score ?? '—')}</td>
      <td>${r.symbol_price != null ? '$' + esc(r.symbol_price) : '—'}</td>
      <td>${esc(r.expiry || '—')}</td>
      <td>${r.dte != null ? esc(r.dte) : '—'}</td>
      <td style="max-width:220px;white-space:normal;font-size:11.5px">${esc(r.legs || '—')}</td>
      <td>${aiBadge(r.ai_verdict)}</td>
      <td>${r.telegram_ok ? '✅' : '⚠️'}</td>
      <td><button class="sn-btn sm" data-action="view-alert" data-id="${r.id}">View</button></td>
    </tr>
  `).join('') : `<tr><td colspan="13" class="sn-empty">No alerts sent in this date range.</td></tr>`;

  const exportParams = new URLSearchParams();
  if (from) exportParams.set('from', from);
  if (to) exportParams.set('to', to);
  const exportLink = $('sn-history-export-csv');
  if (exportLink) exportLink.href = `/signal-notifier/history/export.csv?${exportParams.toString()}`;
}

function openAlertDetail(id) {
  const r = (state.historyRows || []).find(x => String(x.id) === String(id));
  if (!r) return;
  $('sn-detail-title').textContent = `${r.symbol} — ${r.source_label || r.bucket || 'Alert'}`;
  const metaParts = [
    `Sent: ${r.sent_at || '—'}`,
    `Source: ${r.source_label || '—'}${r.source_kind ? ` (${r.source_kind})` : ''}`,
    `Bucket: ${r.bucket || '—'}`,
  ];
  if (r.grade) metaParts.push(`Grade: ${r.grade} (${r.score ?? '—'}/100)`);
  if (r.expiry) metaParts.push(`Expiry: ${r.expiry}${r.dte != null ? ` (${r.dte} DTE)` : ''}`);
  if (r.legs) metaParts.push(`Strikes / Legs: ${r.legs}`);
  if (r.symbol_price != null) metaParts.push(`Spot price at alert: $${r.symbol_price}`);
  metaParts.push(`Telegram delivered: ${r.telegram_ok ? 'yes' : 'no'}`);
  if (r.ai_verdict) {
    metaParts.push(`AI verdict: ${r.ai_verdict}`);
    if (r.ai_reasoning) metaParts.push(`AI reasoning: ${r.ai_reasoning}`);
    try {
      const flags = JSON.parse(r.ai_risk_flags || '[]');
      if (flags.length) metaParts.push(`AI risk flags: ${flags.join('; ')}`);
    } catch {}
    if (r.ai_sizing_note) metaParts.push(`AI sizing note: ${r.ai_sizing_note}`);
  }
  $('sn-detail-meta').innerHTML = metaParts.map(p => esc(p)).join('<br>');
  $('sn-detail-message').value = r.message || '(no message text stored for this alert — it predates the message-logging update)';
  $('sn-detail-backdrop').style.display = 'flex';
}

function closeAlertDetail() {
  $('sn-detail-backdrop').style.display = 'none';
}

function setKindPanel(kind) {
  state.editingKind = kind;
  document.querySelectorAll('.sn-kind-tab').forEach(el => el.classList.toggle('active', el.dataset.kind === kind));
  document.querySelectorAll('.sn-kind-panel').forEach(el => { el.style.display = el.id === `sn-kind-${kind}` ? '' : 'none'; });
}

function dashboardOptionsHtml() {
  return state.dashboards.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('');
}

function tileOptionsHtml(dashboardId) {
  const dash = state.dashboards.find(d => String(d.id) === String(dashboardId));
  const tiles = dash?.tiles || [];
  if (!tiles.length) return '<option value="">No tiles on this dashboard</option>';
  return tiles.map(t => `<option value="${esc(t.id)}">${esc(t.title || t.id)}</option>`).join('');
}

function scannerOptionsHtml() {
  const opts = ['<option value="">— Use custom query below —</option>'];
  for (const s of state.scanners) opts.push(`<option value="${s.id}">${esc(s.name)}</option>`);
  return opts.join('');
}

function openSourceModal(source = null) {
  showModalError('');
  state.editingSourceId = source ? source.id : null;
  $('sn-modal-title-text').textContent = source ? 'Edit alert source' : 'Add alert source';
  $('sn-f-label').value = source?.label || '';
  $('sn-f-enabled').checked = source ? !!source.enabled : true;
  $('sn-f-interval').value = Math.max(1, Math.round((source?.interval_sec || 900) / 60));
  $('sn-f-schedule-kind').value = source?.schedule_kind === 'time' ? 'time' : 'interval';
  state.sourceFormScheduleTimes = Array.isArray(source?.schedule_times) ? [...source.schedule_times] : [];
  renderSourceScheduleTimes();
  updateSourceScheduleModeVisibility();

  $('sn-f-ts-watchlist').innerHTML = watchlistOptionsHtml(source?.watchlist_id || '');
  $('sn-f-ts-min-score').value = source?.min_score ?? 70;

  $('sn-f-dt-dashboard').innerHTML = dashboardOptionsHtml();
  if (source?.kind === 'dashboard_tile' && source.dashboard_id) {
    $('sn-f-dt-dashboard').value = String(source.dashboard_id);
  }
  $('sn-f-dt-tile').innerHTML = tileOptionsHtml($('sn-f-dt-dashboard').value);
  if (source?.tile_id) $('sn-f-dt-tile').value = String(source.tile_id);
  $('sn-f-dt-min-conviction').value = source?.min_conviction_score ?? 0;
  const dtTags = new Set(source?.direction_tags || []);
  document.querySelectorAll('.sn-dt-direction').forEach(cb => { cb.checked = dtTags.has(cb.value); });

  $('sn-f-sq-definition').innerHTML = scannerOptionsHtml();
  $('sn-f-sq-definition').value = source?.definition_id ? String(source.definition_id) : '';
  $('sn-f-sq-watchlist').innerHTML = watchlistOptionsHtml(source?.watchlist_id || '');
  $('sn-f-sq-benchmark').value = source?.benchmark || 'SPY';
  $('sn-f-sq-query').value = source?.query_text || '';
  $('sn-f-sq-min-conviction').value = source?.min_conviction_score ?? 0;
  const sqTags = new Set(source?.direction_tags || []);
  document.querySelectorAll('.sn-sq-direction').forEach(cb => { cb.checked = sqTags.has(cb.value); });
  $('sn-f-sq-combine').checked = !!source?.combine_alerts;
  $('sn-f-sq-score-expr').value = source?.score_expr || 'Score()';
  $('sn-f-sq-score-expr-wrap').style.display = $('sn-f-sq-combine').checked ? '' : 'none';

  $('sn-f-sp-watchlist').innerHTML = watchlistOptionsHtml(source?.watchlist_id || '');
  $('sn-f-sp-min-confidence').value = source?.min_confidence ?? 40;
  $('sn-f-sp-min-oi').value = source?.min_total_oi ?? 50000;
  $('sn-f-sp-min-volume').value = source?.min_avg_daily_volume ?? 1000000;

  const kind = source?.kind || 'trade_scanner';
  setKindPanel(kind);
  $('sn-modal-backdrop').style.display = 'flex';
}

function closeSourceModal() {
  $('sn-modal-backdrop').style.display = 'none';
  showModalError('');
  state.editingSourceId = null;
}

function buildSourcePayload() {
  const kind = state.editingKind;
  const payload = {
    kind,
    label: $('sn-f-label').value.trim(),
    enabled: $('sn-f-enabled').checked,
    interval_sec: Math.max(60, (parseInt($('sn-f-interval').value || '15', 10) || 15) * 60),
    schedule_kind: $('sn-f-schedule-kind').value === 'time' ? 'time' : 'interval',
    schedule_times: state.sourceFormScheduleTimes || [],
  };
  if (kind === 'trade_scanner') {
    payload.watchlist_id = $('sn-f-ts-watchlist').value || '';
    payload.min_score = parseInt($('sn-f-ts-min-score').value || '70', 10) || 70;
    if (!payload.label) payload.label = 'Trade Opportunity Scanner';
  } else if (kind === 'dashboard_tile') {
    payload.dashboard_id = parseInt($('sn-f-dt-dashboard').value || '0', 10) || null;
    payload.tile_id = $('sn-f-dt-tile').value || '';
    payload.min_conviction_score = Math.max(0, parseInt($('sn-f-dt-min-conviction').value || '0', 10) || 0);
    payload.direction_tags = [...document.querySelectorAll('.sn-dt-direction:checked')].map(cb => cb.value);
    if (!payload.label) {
      const dash = state.dashboards.find(d => String(d.id) === String(payload.dashboard_id));
      const tile = dash?.tiles?.find(t => String(t.id) === String(payload.tile_id));
      payload.label = tile ? `${dash.name} — ${tile.title}` : 'Dashboard tile';
    }
  } else if (kind === 'scanner_query') {
    payload.definition_id = $('sn-f-sq-definition').value || null;
    payload.watchlist_id = $('sn-f-sq-watchlist').value || '';
    payload.benchmark = $('sn-f-sq-benchmark').value.trim() || 'SPY';
    payload.query_text = $('sn-f-sq-query').value.trim();
    payload.min_conviction_score = Math.max(0, parseInt($('sn-f-sq-min-conviction').value || '0', 10) || 0);
    payload.direction_tags = [...document.querySelectorAll('.sn-sq-direction:checked')].map(cb => cb.value);
    payload.combine_alerts = $('sn-f-sq-combine').checked;
    payload.score_expr = $('sn-f-sq-score-expr').value.trim() || 'Score()';
    if (!payload.label) {
      const def = state.scanners.find(s => String(s.id) === String(payload.definition_id));
      payload.label = def ? def.name : 'Custom scanner query';
    }
    if (!payload.definition_id && !payload.query_text) {
      throw new Error('Enter a query, or pick a saved scanner.');
    }
  } else if (kind === 'swing_positioning') {
    payload.watchlist_id = $('sn-f-sp-watchlist').value || '';
    payload.min_confidence = Math.max(0, Math.min(100, parseInt($('sn-f-sp-min-confidence').value || '40', 10) || 40));
    payload.min_total_oi = Math.max(0, parseInt($('sn-f-sp-min-oi').value || '50000', 10) || 50000);
    payload.min_avg_daily_volume = Math.max(0, parseFloat($('sn-f-sp-min-volume').value || '1000000') || 1000000);
    if (!payload.label) payload.label = 'Swing Positioning Scanner';
  }
  return payload;
}

async function saveSourceFromModal() {
  if (state.saving) return;
  const btn = $('sn-modal-save');
  state.saving = true;
  showModalError('');
  const prevLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const payload = buildSourcePayload();
    if (payload.schedule_kind === 'time' && !payload.schedule_times.length) {
      showModalError('Add at least one time, or switch back to "Every N minutes".');
      return;
    }
    if (state.editingSourceId) {
      await api(`/signal-notifier/sources/${state.editingSourceId}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
    } else {
      await api('/signal-notifier/sources', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
    }
    await loadSources();
    renderSources();
    closeSourceModal();
  } catch (err) {
    showModalError(err?.message || String(err));
  } finally {
    state.saving = false;
    btn.disabled = false;
    btn.textContent = prevLabel;
  }
}

function bindCollapsibleSections() {
  const pairs = [
    ['sn-default-title', 'sn-default-body'],
    ['sn-journal-title', 'sn-journal-body'],
    ['sn-sources-title', 'sn-sources-body'],
    ['sn-history-title', 'sn-history-body-wrap'],
    ['sn-backtest-title', 'sn-backtest-body-wrap'],
  ];
  for (const [titleId, bodyId] of pairs) {
    const titleEl = $(titleId);
    const bodyEl = $(bodyId);
    if (!titleEl || !bodyEl) continue;
    // Only the text/caret span toggles collapse — buttons inside the header
    // (e.g. "+ Add source") should not also trigger it.
    const clickTarget = titleEl.querySelector('span');
    (clickTarget || titleEl).addEventListener('click', () => {
      const collapsed = bodyEl.classList.toggle('collapsed');
      titleEl.classList.toggle('collapsed', collapsed);
    });
  }
}

function bindEvents() {
  document.querySelectorAll('#sn-backtest-results-table th.sn-sortable').forEach(th => {
    th.addEventListener('click', () => _sortBacktestResults(th.dataset.sort));
  });
  $('sn-backtest-run').addEventListener('click', runBacktest);
  $('sn-global-enabled').addEventListener('change', async () => {
    try {
      await api('/signal-notifier/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: $('sn-global-enabled').checked }),
      });
    } catch (err) {
      alert(`Failed to update global switch: ${err.message || err}`);
    }
  });

  $('sn-ai-gate-enabled').addEventListener('change', async () => {
    try {
      await api('/signal-notifier/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ai_gate_enabled: $('sn-ai-gate-enabled').checked }),
      });
      $('sn-ai-gate-legend').style.display = $('sn-ai-gate-enabled').checked ? 'block' : 'none';
    } catch (err) {
      alert(`Failed to update AI gate: ${err.message || err}`);
      $('sn-ai-gate-enabled').checked = !$('sn-ai-gate-enabled').checked;
    }
  });

  $('sn-refresh-all').addEventListener('click', () => refreshAll().catch(err => alert(err.message || err)));
  $('sn-refresh-history').addEventListener('click', () => refreshHistory().catch(err => alert(err.message || err)));
  $('sn-history-apply-range').addEventListener('click', () => refreshHistory().catch(err => alert(err.message || err)));
  $('sn-history-reset-range').addEventListener('click', () => {
    const range = _defaultHistoryRange();
    $('sn-history-from').value = range.from;
    $('sn-history-to').value = range.to;
    refreshHistory().catch(err => alert(err.message || err));
  });
  $('sn-history-body').addEventListener('click', e => {
    const btn = e.target.closest('[data-action="view-alert"]');
    if (!btn) return;
    openAlertDetail(btn.dataset.id);
  });
  $('sn-detail-close').addEventListener('click', closeAlertDetail);
  $('sn-detail-close2').addEventListener('click', closeAlertDetail);
  $('sn-detail-backdrop').addEventListener('click', e => { if (e.target === $('sn-detail-backdrop')) closeAlertDetail(); });

  $('sn-schedule-kind').addEventListener('change', updateScheduleModeVisibility);

  $('sn-schedule-time-add').addEventListener('click', () => {
    const val = $('sn-schedule-time-input').value;
    if (!val) return;
    state.scheduleTimes = state.scheduleTimes || [];
    if (!state.scheduleTimes.includes(val)) state.scheduleTimes.push(val);
    $('sn-schedule-time-input').value = '';
    renderScheduleTimes();
  });

  $('sn-f-schedule-kind').addEventListener('change', updateSourceScheduleModeVisibility);

  $('sn-f-schedule-time-add').addEventListener('click', () => {
    const val = $('sn-f-schedule-time-input').value;
    if (!val) return;
    state.sourceFormScheduleTimes = state.sourceFormScheduleTimes || [];
    if (!state.sourceFormScheduleTimes.includes(val)) state.sourceFormScheduleTimes.push(val);
    $('sn-f-schedule-time-input').value = '';
    renderSourceScheduleTimes();
  });

  $('sn-save-default').addEventListener('click', async () => {
    const note = $('sn-default-note');
    note.textContent = 'Saving…';
    const scheduleKind = $('sn-schedule-kind').value;
    try {
      if (scheduleKind === 'time' && (!state.scheduleTimes || !state.scheduleTimes.length)) {
        note.textContent = '❌ Add at least one time, or switch back to "Every N minutes".';
        return;
      }
      await api('/signal-notifier/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          watchlist_id: $('sn-default-watchlist').value || '',
          min_score: parseInt($('sn-default-min-score').value || '70', 10) || 70,
          schedule_kind: scheduleKind,
          interval_sec: Math.max(60, (parseInt($('sn-default-interval').value || '15', 10) || 15) * 60),
          schedule_times: state.scheduleTimes || [],
        }),
      });
      note.textContent = '✅ Saved.';
    } catch (err) {
      note.textContent = `❌ Failed to save: ${err.message || err}`;
    }
  });

  $('sn-run-default-dry').addEventListener('click', () => runDefault(true));
  $('sn-run-default-live').addEventListener('click', () => runDefault(false));

  $('sn-save-journal').addEventListener('click', async () => {
    const note = $('sn-journal-note');
    note.textContent = 'Saving…';
    try {
      await api('/signal-notifier/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          journal_pnl_alerts_enabled: $('sn-journal-pnl-enabled').checked,
          journal_pnl_alerts_interval_sec: Math.max(60, (parseInt($('sn-journal-pnl-interval').value || '1', 10) || 1) * 60),
          journal_deep_loss_alerts_enabled: $('sn-journal-deep-loss-enabled').checked,
          journal_deep_loss_threshold_pct: Math.min(99, Math.max(1, parseFloat($('sn-journal-deep-loss-threshold').value || '75') || 75)),
          journal_health_alerts_enabled: $('sn-journal-health-enabled').checked,
          journal_health_alerts_interval_sec: Math.max(60, (parseInt($('sn-journal-health-interval').value || '5', 10) || 5) * 60),
          telegram_price_alerts_enabled: $('sn-telegram-price-enabled').checked,
          telegram_price_alerts_interval_sec: Math.max(60, (parseInt($('sn-telegram-price-interval').value || '1', 10) || 1) * 60),
        }),
      });
      note.textContent = '✅ Saved.';
    } catch (err) {
      note.textContent = `❌ Failed to save: ${err.message || err}`;
    }
  });

  $('sn-add-source').addEventListener('click', () => openSourceModal(null));
  $('sn-modal-close').addEventListener('click', closeSourceModal);
  $('sn-modal-cancel').addEventListener('click', closeSourceModal);
  $('sn-modal-backdrop').addEventListener('click', e => { if (e.target === $('sn-modal-backdrop')) closeSourceModal(); });
  $('sn-modal-save').addEventListener('click', saveSourceFromModal);

  document.querySelectorAll('.sn-kind-tab').forEach(tab => {
    tab.addEventListener('click', () => setKindPanel(tab.dataset.kind));
  });
  $('sn-f-dt-dashboard').addEventListener('change', () => {
    $('sn-f-dt-tile').innerHTML = tileOptionsHtml($('sn-f-dt-dashboard').value);
  });
  $('sn-f-sq-definition').addEventListener('change', () => {
    const id = $('sn-f-sq-definition').value;
    const def = state.scanners.find(s => String(s.id) === String(id));
    if (def) {
      $('sn-f-sq-query').value = def.query_text || '';
      if (def.watchlist_id) $('sn-f-sq-watchlist').value = String(def.watchlist_id);
      if (def.benchmark) $('sn-f-sq-benchmark').value = def.benchmark;
    }
  });

  $('sn-sources').addEventListener('click', async e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const action = btn.dataset.action;
    const source = state.sources.find(s => s.id === id);
    if (!source) return;
    if (action === 'edit') {
      openSourceModal(source);
    } else if (action === 'delete') {
      if (!confirm(`Delete source "${source.label}"?`)) return;
      try {
        await api(`/signal-notifier/sources/${id}`, { method: 'DELETE' });
        await loadSources();
        renderSources();
      } catch (err) {
        alert(`Failed to delete: ${err.message || err}`);
      }
    } else if (action === 'dry-run') {
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = '…';
      try {
        const result = await api(`/signal-notifier/sources/${id}/run`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dry_run: true }),
        });
        if (result.ok) {
          alert(`Dry run: ${result.candidates} candidate(s), ${result.sent} would be sent (${result.skipped_duplicate} already alerted today).`);
        } else {
          alert(`Dry run failed: ${result.error || 'unknown error'}`);
        }
      } catch (err) {
        alert(`Dry run failed: ${err.message || err}`);
      } finally {
        btn.disabled = false;
        btn.textContent = prev;
      }
    }
  });

  $('sn-sources').addEventListener('change', async e => {
    const selCb = e.target.closest('.sn-select-cb');
    if (selCb) {
      const id = Number(selCb.dataset.id);
      if (selCb.checked) state.selectedSourceIds.add(id); else state.selectedSourceIds.delete(id);
      updateBulkBar();
      return;
    }
    const el = e.target.closest('[data-action="toggle"]');
    if (!el) return;
    const id = Number(el.dataset.id);
    try {
      await api(`/signal-notifier/sources/${id}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: el.checked }),
      });
      await loadSources();
      renderSources();
    } catch (err) {
      alert(`Failed to toggle: ${err.message || err}`);
      el.checked = !el.checked;
    }
  });

  $('sn-select-all').addEventListener('change', e => {
    if (e.target.checked) {
      state.sources.forEach(s => state.selectedSourceIds.add(s.id));
    } else {
      state.selectedSourceIds.clear();
    }
    renderSources();
  });
  $('sn-bulk-enable').addEventListener('click', () => bulkSetEnabled(true));
  $('sn-bulk-disable').addEventListener('click', () => bulkSetEnabled(false));
}

async function runDefault(dryRun) {
  const note = $('sn-default-note');
  note.textContent = dryRun ? 'Running dry run…' : 'Running…';
  try {
    const result = await api('/signal-notifier/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dry_run: dryRun }),
    });
    if (result.ok) {
      note.textContent = `✅ Scanned ${result.scanned}, ${result.candidates} candidate(s), ${result.sent} sent, ${result.skipped_duplicate} already alerted today.`;
      await refreshHistory();
    } else {
      note.textContent = `❌ ${result.error || 'Failed'}`;
    }
  } catch (err) {
    note.textContent = `❌ ${err.message || err}`;
  }
}

function _outcomeBadge(outcome) {
  const map = {
    max_profit: ['#86efac', 'rgba(34,197,94,.14)', 'Max profit'],
    partial_win: ['#86efac', 'rgba(34,197,94,.10)', 'Partial win'],
    partial_loss: ['#fcd34d', 'rgba(245,158,11,.12)', 'Partial loss'],
    max_loss: ['#fca5a5', 'rgba(239,68,68,.14)', 'Max loss'],
  };
  const [color, bg, label] = map[outcome] || ['var(--sn-muted)', 'transparent', outcome || '—'];
  return `<span style="color:${color};background:${bg};padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700">${esc(label)}</span>`;
}

function _statCard(title, value, sub) {
  return `<div style="background:rgba(8,12,24,.4);border:1px solid var(--sn-border);border-radius:10px;padding:10px 12px">
    <div style="font-size:11px;color:var(--sn-muted);text-transform:uppercase;letter-spacing:.03em">${esc(title)}</div>
    <div style="font-size:20px;font-weight:900;margin-top:2px">${value}</div>
    ${sub ? `<div style="font-size:11px;color:var(--sn-muted);margin-top:2px">${esc(sub)}</div>` : ''}
  </div>`;
}

function _bucketRow(label, s) {
  if (!s || !s.count) return `<tr><td>${esc(label)}</td><td colspan="6" class="sn-empty">No data</td></tr>`;
  const gapColor = s.calibration_gap_pts == null ? 'var(--sn-muted)' : (s.calibration_gap_pts < -10 ? '#fca5a5' : s.calibration_gap_pts > 10 ? '#86efac' : 'var(--sn-muted)');
  return `<tr>
    <td><b>${esc(label)}</b></td>
    <td>${s.count}</td>
    <td>${s.realized_win_rate_pct}%</td>
    <td>${s.avg_predicted_pop_pct ?? '—'}%</td>
    <td style="color:${gapColor}">${s.calibration_gap_pts != null ? (s.calibration_gap_pts > 0 ? '+' : '') + s.calibration_gap_pts + ' pts' : '—'}</td>
    <td>${s.total_pnl_per_contract}</td>
    <td>${s.avg_pnl_per_contract}</td>
  </tr>`;
}

function _renderBacktestResultsBody() {
  const rows = state.backtestResults || [];
  $('sn-backtest-results-body').innerHTML = rows.length ? rows.map(r => `
    <tr>
      <td>${esc(r.alert_date || '')}</td>
      <td><b>${esc(r.symbol || '')}</b></td>
      <td>${esc(r.trade_type || '')}</td>
      <td>${esc(r.grade || '—')}</td>
      <td>${esc(r.score ?? '—')}</td>
      <td>${r.pop != null ? r.pop + '%' : '—'}</td>
      <td>${esc(r.expiry || '')}</td>
      <td style="max-width:200px;white-space:normal;font-size:11.5px">${esc(r.legs || '')}</td>
      <td>${r.est_credit ?? '—'}</td>
      <td>${r.max_loss_amt ?? '—'}</td>
      <td>${r.bt_expiry_price ?? '—'}</td>
      <td>${_outcomeBadge(r.bt_outcome)}</td>
      <td style="color:${(r.bt_pnl || 0) >= 0 ? '#86efac' : '#fca5a5'}">${r.bt_pnl ?? '—'}</td>
    </tr>
  `).join('') : '<tr><td colspan="13" class="sn-empty">No results.</td></tr>';
}

function _sortBacktestResults(key) {
  const s = state.backtestSort;
  s.dir = (s.key === key) ? -s.dir : 1;
  s.key = key;
  state.backtestResults.sort((a, b) => {
    let av = a[key], bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    if (typeof av === 'string') { av = av.toLowerCase(); bv = String(bv).toLowerCase(); }
    if (av < bv) return -1 * s.dir;
    if (av > bv) return 1 * s.dir;
    return 0;
  });
  document.querySelectorAll('#sn-backtest-results-table th.sn-sortable').forEach(th => {
    th.classList.toggle('sn-sort-active', th.dataset.sort === key);
  });
  _renderBacktestResultsBody();
}

function _renderCalibrationSuggestions(suggestions) {
  if (!suggestions || !suggestions.length) {
    $('sn-calibration-suggestions').innerHTML = '<div class="sn-empty">No suggestions yet — run a wider date range for more sample size.</div>';
    return;
  }
  $('sn-calibration-suggestions').innerHTML = suggestions.map(s => {
    const color = s.severity === 'high' ? (s.gap_pts > 0 ? '#86efac' : '#fca5a5') : '#94a3b8';
    return `<div style="padding:8px 10px;border-left:3px solid ${color};background:rgba(148,163,184,.05);margin-bottom:6px;font-size:12.5px;line-height:1.5">${esc(s.text)}</div>`;
  }).join('');
}

function _renderFactorCalibration(factors) {
  const rows = factors || [];
  $('sn-factor-calibration-body').innerHTML = rows.length ? rows.map(f => `
    <tr>
      <td><b>${esc(f.factor)}</b></td>
      <td>${f.as_pro_count}</td>
      <td>${f.as_pro_win_rate != null ? f.as_pro_win_rate + '%' : '—'}</td>
      <td>${f.absent_win_rate != null ? f.absent_win_rate + '%' : '—'}</td>
      <td style="color:${f.separation_pts == null ? '#94a3b8' : f.separation_pts >= 8 ? '#86efac' : f.separation_pts < 3 ? '#fca5a5' : '#fcd34d'}">${f.separation_pts != null ? (f.separation_pts > 0 ? '+' : '') + f.separation_pts + 'pts' : '—'}</td>
      <td style="max-width:280px;white-space:normal;font-size:11.5px;color:#94a3b8">${esc(f.note)}</td>
    </tr>
  `).join('') : '<tr><td colspan="6" class="sn-empty">No data</td></tr>';
}

function _renderSpecificParamSuggestions(suggestions) {
  const el = $('sn-specific-param-suggestions');
  if (!el) return;
  const rows = suggestions || [];
  if (!rows.length) {
    el.innerHTML = '<div class="sn-empty">No specific parameter changes suggested yet — factors need enough alerts (10+) with a clear separation before a concrete number is suggested.</div>';
    return;
  }
  el.innerHTML = rows.map(s => {
    const isObj = typeof s.current_value === 'object';
    const valStr = isObj
      ? Object.entries(s.suggested_value).map(([k, v]) => `${k}: ${s.current_value[k]} → ${v}`).join(', ')
      : `${s.current_value} → ${s.suggested_value}`;
    const applyPayload = isObj
      ? `${s.param_key}=${encodeURIComponent(JSON.stringify(s.suggested_value))}`
      : `${s.param_key}=${s.suggested_value}`;
    return `
      <div style="padding:9px 11px;border-left:3px solid ${s.scale_pct > 0 ? '#4ade80' : '#f87171'};background:rgba(148,163,184,.05);margin-bottom:6px;font-size:12.5px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">
          <b>${esc(s.param_key)}</b>
          <span style="color:${s.scale_pct > 0 ? '#4ade80' : '#f87171'};font-weight:800">${s.scale_pct > 0 ? '+' : ''}${s.scale_pct}%</span>
        </div>
        <div style="color:#94a3b8;font-size:11.5px;margin-bottom:4px">${valStr}</div>
        <div style="color:#94a3b8;font-size:11px;line-height:1.5;margin-bottom:6px">${esc(s.reason)}</div>
        <a href="/scoring-params?${applyPayload}" target="_blank" style="font-size:11px;color:#818cf8;text-decoration:none;font-weight:700">→ Open in Scoring Parameters, pre-filled</a>
      </div>`;
  }).join('');
}

function renderBacktestResults(d) {
  $('sn-backtest-summary').style.display = d.total_backtested ? 'block' : 'none';
  if (!d.total_backtested) return;

  const o = d.overall || {};
  $('sn-backtest-overall-cards').innerHTML = [
    _statCard('Alerts backtested', d.total_backtested),
    _statCard('Realized win rate', (o.realized_win_rate_pct ?? '—') + '%', `predicted POP avg: ${o.avg_predicted_pop_pct ?? '—'}%`),
    _statCard('Calibration gap', o.calibration_gap_pts != null ? ((o.calibration_gap_pts > 0 ? '+' : '') + o.calibration_gap_pts + ' pts') : '—', o.calibration_gap_pts < 0 ? 'scanner is overconfident' : o.calibration_gap_pts > 0 ? 'scanner is underconfident' : ''),
    _statCard('Total PnL / contract', o.total_pnl_per_contract ?? '—', `avg ${o.avg_pnl_per_contract ?? '—'} per alert`),
  ].join('');

  const grades = Object.keys(d.by_grade || {}).sort();
  $('sn-backtest-grade-body').innerHTML = grades.map(g => _bucketRow(g, d.by_grade[g])).join('') || '<tr><td colspan="7" class="sn-empty">No data</td></tr>';

  const types = Object.keys(d.by_trade_type || {}).sort();
  $('sn-backtest-type-body').innerHTML = types.map(t => _bucketRow(t, d.by_trade_type[t])).join('') || '<tr><td colspan="7" class="sn-empty">No data</td></tr>';

  _renderCalibrationSuggestions(d.calibration_suggestions);
  _renderFactorCalibration(d.factor_calibration);
  _renderSpecificParamSuggestions(d.specific_param_suggestions);

  state.backtestResults = d.results || [];
  state.backtestSort = { key: null, dir: 1 };
  _renderBacktestResultsBody();
}

async function runBacktest() {
  const btn = $('sn-backtest-run');
  const from = $('sn-backtest-from').value || '';
  const to = $('sn-backtest-to').value || '';
  const symbol = $('sn-backtest-symbol')?.value.trim().toUpperCase() || '';
  const sourceLabel = $('sn-backtest-source')?.value || '';
  const forceRecompute = $('sn-backtest-force').checked;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '⏳ Running…';
  $('sn-backtest-note').textContent = '';
  try {
    const d = await api('/signal-notifier/backtest/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from, to, force_recompute: forceRecompute, symbol, source_label: sourceLabel }),
      timeoutMs: 120000,
    });
    if (d.ok) {
      $('sn-backtest-note').textContent = `Computed ${d.computed_this_run} new outcome(s) this run. ${d.total_backtested} alert(s) have a computed outcome in this range.`;
      renderBacktestResults(d);
    } else {
      $('sn-backtest-note').textContent = `Failed: ${d.error || 'unknown error'}`;
    }
  } catch (err) {
    $('sn-backtest-note').textContent = `Failed: ${err.message || err}`;
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
    const params = new URLSearchParams();
    if (from) params.set('from', from);
    if (to) params.set('to', to);
    $('sn-backtest-export-csv').href = `/signal-notifier/backtest/export.csv?${params.toString()}`;
  }
}


async function init() {
  await loadGlobalApiTimeout();
  bindEvents();
  bindCollapsibleSections();
  const range = _defaultHistoryRange();
  if ($('sn-history-from')) $('sn-history-from').value = range.from;
  if ($('sn-history-to')) $('sn-history-to').value = range.to;
  try {
    await refreshAll();
  } catch (err) {
    console.error('init failed', err);
  }
  try {
    await refreshHistory();
  } catch (err) {
    console.warn('history load failed', err);
  }
  _loadAlertSourceLabels().catch(() => null);
  // Alerts can be sent by the background watcher at any time, not just when
  // this button is clicked — poll so newly-sent alerts show up without the
  // user having to remember to hit refresh.
  setInterval(() => { refreshHistory().catch(() => null); }, 60000);
}

window.addEventListener('DOMContentLoaded', init);
