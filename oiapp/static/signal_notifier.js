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
}

function fmtSourceMeta(s) {
  const mins = Math.max(1, Math.round((s.interval_sec || 900) / 60));
  const parts = [`every ${mins} min${mins === 1 ? '' : 's'}`];
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
  }
  if (s.last_run_at) parts.push(`last run: ${s.last_run_at}`);
  if (s.last_error) parts.push(`⚠ ${s.last_error}`);
  return parts;
}

const KIND_LABELS = { trade_scanner: 'Trade Scanner', dashboard_tile: 'Dashboard Tile', scanner_query: 'Scanner Query' };

function renderSources() {
  const wrap = $('sn-sources');
  if (!state.sources.length) {
    wrap.innerHTML = '<div class="sn-empty">No extra sources yet. Click "+ Add source" to alert on a Scanner Dashboard tile, a saved Scanner Builder query, or another Trade Opportunity Scanner sweep.</div>';
    return;
  }
  wrap.innerHTML = state.sources.map(s => `
    <div class="sn-source-card" data-id="${s.id}">
      <div class="sn-source-head">
        <div class="sn-source-title">
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

async function refreshHistory() {
  const from = $('sn-history-from')?.value || '';
  const to = $('sn-history-to')?.value || '';
  const params = new URLSearchParams({ limit: '300' });
  if (from) params.set('from', from);
  if (to) params.set('to', to);
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
    if (!payload.label) {
      const def = state.scanners.find(s => String(s.id) === String(payload.definition_id));
      payload.label = def ? def.name : 'Custom scanner query';
    }
    if (!payload.definition_id && !payload.query_text) {
      throw new Error('Enter a query, or pick a saved scanner.');
    }
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

  $('sn-save-default').addEventListener('click', async () => {
    const note = $('sn-default-note');
    note.textContent = 'Saving…';
    try {
      await api('/signal-notifier/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          watchlist_id: $('sn-default-watchlist').value || '',
          min_score: parseInt($('sn-default-min-score').value || '70', 10) || 70,
          interval_sec: Math.max(60, (parseInt($('sn-default-interval').value || '15', 10) || 15) * 60),
        }),
      });
      note.textContent = '✅ Saved.';
    } catch (err) {
      note.textContent = `❌ Failed to save: ${err.message || err}`;
    }
  });

  $('sn-run-default-dry').addEventListener('click', () => runDefault(true));
  $('sn-run-default-live').addEventListener('click', () => runDefault(false));

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

  const rows = d.results || [];
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

async function runBacktest() {
  const btn = $('sn-backtest-run');
  const from = $('sn-backtest-from').value || '';
  const to = $('sn-backtest-to').value || '';
  const forceRecompute = $('sn-backtest-force').checked;
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '⏳ Running…';
  $('sn-backtest-note').textContent = '';
  try {
    const d = await api('/signal-notifier/backtest/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from, to, force_recompute: forceRecompute }),
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
  // Alerts can be sent by the background watcher at any time, not just when
  // this button is clicked — poll so newly-sent alerts show up without the
  // user having to remember to hit refresh.
  setInterval(() => { refreshHistory().catch(() => null); }, 60000);
}

window.addEventListener('DOMContentLoaded', init);
