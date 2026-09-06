'use strict';

const TF_MS = { '5m': 5*60*1000, '15m': 15*60*1000, '1h': 60*60*1000, '2h': 2*60*60*1000, '4h': 4*60*60*1000, '1d': 24*60*60*1000, '1w': 7*24*60*60*1000, '1m': 30*24*60*60*1000 };
const DEFAULT_TITLE = 'My Scanner Dashboard';
const DEFAULT_QUERY = 'scan(Momentum Retrace)';
const MIN_TILE_WIDTH = 320;
const MIN_TILE_HEIGHT = 240;
const DEFAULT_TILE_WIDTH = 420;
const DEFAULT_TILE_HEIGHT = 380;
const EXCLUDED_KEYS = new Set([
  'symbol','name','direction','source','reason','scan_error','error','query_text','setup','setup_type','bias','signal',
  'final_score','score','native_score','composite_score','edge_score','rs_score','vol_score','sector_score','institutional_score','expected_move_score',
  'leadership','trend_age','days_ago','age','count','count_score','ok','matched','timeframes','options_history',
  'spot','price','close','last','mark','rsi','rsi14','rsi_14','rsi_diff_90','rsi_diff90','rsiDiff90',
  'volume','vol','oi','pcr','open_interest','change_pct','pct_change','pct','delta','gamma','theta'
]);
const TITLE_MAP = {
  rsi_diff_90: 'RSIDiff90',
  rsi_diff90: 'RSIDiff90',
  rsi14: 'RSI',
  rsi_14: 'RSI',
  relative_strength: 'Rel Strength',
  sector: 'Sector',
  sector_name: 'Sector Name',
  sector_etf: 'Sector ETF',
  sector_rs: 'SectorRS',
  sectorrs: 'SectorRS',
  mansfield_rs: 'Mansfield RS',
  breakoutstrength: 'Breakout Strength',
  breakoutage: 'Breakout Age',
  distancefromresistance: 'Dist. from Res',
  distancefromsupport: 'Dist. from Sup',
  atrcompression: 'ATR Compression',
  rangecompression: 'Range Compression',
  volumedryup: 'Volume Dry-up',
  resistancestrength: 'Resistance Strength',
  supportstrength: 'Support Strength',
  failedbreakoutstrength: 'Failed BO Strength',
  failedbreakdownstrength: 'Failed BD Strength',
  volumeatlevel: 'Vol @ Level',
  leadership: 'Leadership',
  beta: 'Beta',
  earn_days: 'Earnings Days',
  earn_score: 'Earnings Score',
};

const state = {
  dashboards: [],
  current: null,
  watchlists: [],
  catalog: null,
  columnTemplates: [],
  dragIndex: null,
  resizeDrag: null,
  autoTimer: null,
  editLayout: false,
  autoRefresh: false,
  modalTileIndex: null,
  autocomplete: { items: [], idx: 0, visible: false },
  busy: false,
  saveTimer: null,
};

const BUILTIN_DEFAULT_COLUMNS = [
  { label: 'Symbol', expr: 'symbol', format: 'text', locked: true },
  { label: 'Price', expr: 'close[1d]', format: 'price' },
  { label: 'Sector', expr: 'Sector()', format: 'text' },
  { label: 'RS', expr: 'RelativeStrength(20, "1d")', format: 'number' },
  { label: 'SectorRS', expr: 'SectorRS(20, "1d")', format: 'number' },
  { label: 'Leadership', expr: 'leadership', format: 'pct0' },
  { label: 'RSI', expr: 'rsi14[1d]', format: 'number' },
  { label: 'RSIDiff90', expr: 'RSIDiff90(90, "1d")', format: 'number' },
  { label: 'UAE 1D', expr: 'UAERegime("1d")', format: 'text' },
  { label: 'Reason', expr: 'reason', format: 'text', locked: true },
];

function $(id) { return document.getElementById(id); }

// Global fetch timeout, shared across Scanner Builder/Dashboard/Signal
// Notifier/AI Copilot. Configured once from Scanner Builder → Settings and
// applied here as the default for every api() call on this page.
let _globalApiTimeoutMs = 120000;
let _globalApiTimeoutLoaded = false;
async function loadGlobalApiTimeout() {
  try {
    const r = await fetch('/scanner-builder/api/settings/timeout');
    const d = await r.json();
    if (d && Number.isFinite(Number(d.timeout_sec))) {
      _globalApiTimeoutMs = Math.max(10, Number(d.timeout_sec)) * 1000;
    }
  } catch (e) {
    console.warn('Could not load global API timeout setting, using default', e);
  } finally {
    _globalApiTimeoutLoaded = true;
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
function fmt(v, key = '') {
  if (v === null || v === undefined || v === '') return '—';
  if (typeof v === 'boolean') return v ? '✔' : '—';
  if (typeof v === 'number') {
    if (/volume|count|age|days|qty/i.test(key)) return Number(v).toLocaleString();
    return Number(v).toFixed(Number.isInteger(v) ? 0 : 2).replace(/\.00$/, '');
  }
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'object') return JSON.stringify(v).slice(0, 140);
  return String(v);
}

function getPrice(row) {
  if (!row) return null;
  const candidates = [
    row.price, row.spot, row.close, row.last, row.mark, row.underlying_price, row.current_price,
    row.close_1d, row.latest_close, row.last_price
  ];
  for (const v of candidates) {
    const n = Number(v);
    if (!Number.isNaN(n) && Number.isFinite(n)) return n;
  }
  const tfClose = row.timeframes?.['1d']?.close;
  if (Array.isArray(tfClose) && tfClose.length) {
    const n = Number(tfClose[tfClose.length - 1]);
    if (!Number.isNaN(n) && Number.isFinite(n)) return n;
  }
  return null;
}
function clone(obj) { return JSON.parse(JSON.stringify(obj)); }
function uid(prefix = 'tile') { return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`; }
function clampTileWidth(v) { return Math.max(MIN_TILE_WIDTH, parseInt(v || DEFAULT_TILE_WIDTH, 10) || DEFAULT_TILE_WIDTH); }
function clampTileHeight(v) { return Math.max(MIN_TILE_HEIGHT, parseInt(v || DEFAULT_TILE_HEIGHT, 10) || DEFAULT_TILE_HEIGHT); }
function humanizeKey(key) {
  const k = String(key || '');
  if (TITLE_MAP[k]) return TITLE_MAP[k];
  if (TITLE_MAP[k.toLowerCase()]) return TITLE_MAP[k.toLowerCase()];
  return k
    .replace(/^_+/, '')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_\-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, m => m.toUpperCase());
}

function formatColumnLabel(expr) {
  const e = String(expr || '').trim();
  if (e.toLowerCase() === 'symbol') return 'Symbol';
  if (e.toLowerCase() === 'reason') return 'Reason';
  if (/^close\[/i.test(e)) return 'Price';
  const m = e.match(/^([A-Za-z_][A-Za-z0-9_]*)/);
  return m ? humanizeKey(m[1]) : e;
}

function inferColumnFormat(expr) {
  const e = String(expr || '').toLowerCase().trim();
  if (['symbol', 'reason', 'scanner_reason', 'match_reason'].includes(e)) return 'text';
  if (e.includes('uaeregime') || e.includes('uae_regime') || e.includes('flowbias') || e.includes('flow_bias') || e.includes('sector') || e.includes('regime') || e.includes('signal') || e.includes('bias')) return 'text';
  if (e.includes('price') || e.includes('close') || e.includes('open') || e.includes('high') || e.includes('low') || e.includes('ema') || e.includes('support') || e.includes('resistance') || e.includes('fib')) return 'price';
  if (e.includes('pct') || e.includes('percent') || e.includes('leadership') || e.includes('rank')) return 'pct1';
  if (e.includes('days') || e.includes('count') || e.includes('age') || e === 'oi') return 'integer';
  return 'number';
}

function normalizeColumns(cols) {
  if (typeof cols === 'string') {
    try { cols = JSON.parse(cols); } catch { cols = []; }
  }
  if (!Array.isArray(cols)) cols = [];
  const seen = new Set();
  return cols.map((c, i) => {
    if (typeof c === 'string') c = { expr: c };
    const expr = String(c?.expr || c?.name || c?.key || '').trim();
    if (!expr) return null;
    const label = String(c.label || c.title || formatColumnLabel(expr) || `Column ${i + 1}`).trim();
    const key = label.toLowerCase();
    if (seen.has(key)) return null;
    seen.add(key);
    let fmt = String(c.format || 'auto').toLowerCase();
    if (!fmt || fmt === 'auto' || (fmt === 'text' && inferColumnFormat(expr) !== 'text')) fmt = inferColumnFormat(expr);
    return { label, expr, format: fmt, locked: !!c.locked };
  }).filter(Boolean).slice(0, 24);
}

function defaultColumns() {
  return normalizeColumns(BUILTIN_DEFAULT_COLUMNS);
}

function defaultTemplateColumns() {
  const templ = (state.columnTemplates || []).find(t => Number(t.is_default || 0) === 1) || (state.columnTemplates || [])[0];
  if (templ && Array.isArray(templ.columns) && templ.columns.length) return normalizeColumns(templ.columns);
  return defaultColumns();
}

function templateById(id) {
  if (!id) return null;
  return (state.columnTemplates || []).find(t => String(t.id) === String(id)) || null;
}

function columnsForTile(tile) {
  const explicit = normalizeColumns(tile?.result_columns_json || []);
  if (explicit.length) return explicit;
  const templ = templateById(tile?.result_template_id);
  if (templ) return normalizeColumns(templ.columns || []);
  return defaultTemplateColumns();
}

function columnTemplateOptionsHtml(selected = '') {
  const opts = ['<option value="">Default columns</option>'];
  for (const t of state.columnTemplates || []) {
    opts.push(`<option value="${esc(t.id)}" ${String(selected) === String(t.id) ? 'selected' : ''}>${Number(t.is_default || 0) ? '★ ' : ''}${esc(t.name)}</option>`);
  }
  return opts.join('');
}

function fmtColumn(v, fmtName = '') {
  if (v === null || v === undefined || v === '') return '—';
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'object') return JSON.stringify(v).slice(0, 140);
  const n = Number(v);
  const fmt = String(fmtName || 'auto').toLowerCase();
  if (fmt === 'raw') return String(v);
  if (!Number.isNaN(n) && Number.isFinite(n) && String(v).trim() !== '') {
    if (fmt === 'price') return '$' + n.toFixed(2);
    if (fmt === 'integer' || fmt === 'number0') return Math.round(n).toLocaleString();
    if (fmt === 'pct0') return n.toFixed(0) + '%';
    if (fmt === 'pct1') return n.toFixed(1) + '%';
    if (fmt === 'pct2') return n.toFixed(2) + '%';
    if (fmt === 'number1') return n.toFixed(1).replace(/\.0$/, '');
    if (fmt === 'number3') return n.toFixed(3).replace(/\.000$/, '');
    if (fmt === 'number4') return n.toFixed(4).replace(/\.0000$/, '');
    if (fmt === 'number' || fmt === 'number2' || fmt === 'auto' || fmt === 'text' || !fmt) return n.toFixed(Number.isInteger(n) ? 0 : 2).replace(/\.00$/, '');
  }
  return String(v);
}


function sortColumnValue(v) {
  if (v === null || v === undefined || v === '') return { kind: 'empty', value: '' };
  if (Array.isArray(v)) v = v.join(', ');
  if (typeof v === 'object') v = JSON.stringify(v);
  const raw = String(v).replace(/[$,%]/g, '').trim();
  const n = Number(raw);
  if (!Number.isNaN(n) && Number.isFinite(n) && raw !== '') return { kind: 'number', value: n };
  return { kind: 'text', value: String(v).toLowerCase() };
}

function compareColumnValues(a, b) {
  const av = sortColumnValue(a);
  const bv = sortColumnValue(b);
  if (av.kind === 'empty' && bv.kind === 'empty') return 0;
  if (av.kind === 'empty') return 1;
  if (bv.kind === 'empty') return -1;
  if (av.kind === 'number' && bv.kind === 'number') return av.value - bv.value;
  return String(av.value).localeCompare(String(bv.value), undefined, { numeric: true, sensitivity: 'base' });
}

function sortedTileRows(tile, rows, cols) {
  const data = Array.isArray(rows) ? rows.slice() : [];
  if (tile?.sort_col === null || tile?.sort_col === undefined || tile?.sort_col === '') return data;
  const idx = Number(tile?.sort_col);
  if (!Number.isInteger(idx) || idx < 0 || idx >= cols.length) return data;
  const col = cols[idx];
  const dir = String(tile?.sort_dir || 'asc') === 'desc' ? -1 : 1;
  return data.sort((a, b) => dir * compareColumnValues(getColumnValue(a, col), getColumnValue(b, col)));
}

function getColumnValue(row, col) {
  const vals = row?._result_columns || {};
  if (Object.prototype.hasOwnProperty.call(vals, col.label)) return vals[col.label];
  if (Object.prototype.hasOwnProperty.call(vals, col.expr)) return vals[col.expr];
  const expr = String(col.expr || '').toLowerCase();
  if (expr === 'symbol') return row.symbol || row.name || '—';
  if (expr === 'reason') return (row.reason || []).join(' | ');
  if (expr === 'close[1d]' || expr === 'price') return getPrice(row);
  return row[expr] ?? row[col.expr] ?? null;
}

function defaultDashboard(name = DEFAULT_TITLE) {
  return {
    id: null,
    name,
    settings: {
      name,
      default_watchlist_id: '',
      timeframe: '1h',
      auto_refresh: false,
      edit_layout: false,
      freeze_tile_size: false,
      tile_width: DEFAULT_TILE_WIDTH,
      tile_height: DEFAULT_TILE_HEIGHT,
      layout_columns: 0,
    },
    tiles: [
      { id: uid(), title: 'RSI_OveBought_Sold', query_text: 'RSIDiff90() >= 12 OR RSIDiff90() <= -12', watchlist_id: '', prior_days: 0, limit: 200, width: DEFAULT_TILE_WIDTH, height: DEFAULT_TILE_HEIGHT, x: 0, y: 0, result_template_id: '', result_columns_json: '', results: [], last_run_at: '', error: '' },
      { id: uid(), title: 'Momentum Retrace', query_text: 'scan(Momentum Retrace)', watchlist_id: '', prior_days: 0, limit: 200, width: DEFAULT_TILE_WIDTH, height: DEFAULT_TILE_HEIGHT, x: 440, y: 0, result_template_id: '', result_columns_json: '', results: [], last_run_at: '', error: '' },
      { id: uid(), title: 'RSI MTF', query_text: 'scan(RSI MTF)', watchlist_id: '', prior_days: 0, limit: 200, width: DEFAULT_TILE_WIDTH, height: DEFAULT_TILE_HEIGHT, x: 880, y: 0, result_template_id: '', result_columns_json: '', results: [], last_run_at: '', error: '' },
    ],
  };
}

function normalizeDashboard(d) {
  const out = clone(d || defaultDashboard());
  out.settings = { ...(out.settings || {}) };
  out.settings.name = out.settings.name || out.name || DEFAULT_TITLE;
  out.settings.default_watchlist_id = out.settings.default_watchlist_id == null ? '' : String(out.settings.default_watchlist_id);
  out.settings.timeframe = out.settings.timeframe || '1h';
  out.settings.auto_refresh = !!out.settings.auto_refresh;
  out.settings.edit_layout = !!out.settings.edit_layout;
  out.settings.freeze_tile_size = !!out.settings.freeze_tile_size;
  out.settings.tile_width = clampTileWidth(out.settings.tile_width);
  out.settings.tile_height = clampTileHeight(out.settings.tile_height);
  const gc = parseInt(out.settings.grid_columns || 3, 10);
  out.settings.grid_columns = (gc === 2 || gc === 3) ? gc : 3;
  out.settings.layout_columns = Math.max(0, parseInt(out.settings.layout_columns || 0, 10) || 0);
  out.name = out.name || out.settings.name || DEFAULT_TITLE;
  out.tiles = (out.tiles || []).map((t, i) => ({
    id: t.id || uid(),
    title: t.title || `Tile ${i + 1}`,
    query_text: String(t.query_text || '').trim() || DEFAULT_QUERY,
    watchlist_id: t.watchlist_id == null ? '' : String(t.watchlist_id),
    prior_days: Math.max(0, parseInt(t.prior_days || 0, 10) || 0),
    limit: Math.max(1, parseInt(t.limit || 200, 10) || 200),
    width: clampTileWidth(t.width || out.settings.tile_width),
    height: clampTileHeight(t.height || out.settings.tile_height),
    x: Math.max(0, parseInt(t.x || (i % 3) * 440, 10) || 0),
    y: Math.max(0, parseInt(t.y || Math.floor(i / 3) * 420, 10) || 0),
    result_template_id: t.result_template_id == null ? '' : String(t.result_template_id),
    result_columns_json: typeof t.result_columns_json === 'string' ? t.result_columns_json : (t.result_columns_json ? JSON.stringify(t.result_columns_json) : ''),
    sort_col: (t.sort_col !== null && t.sort_col !== undefined && t.sort_col !== '' && Number.isInteger(Number(t.sort_col))) ? Number(t.sort_col) : null,
    sort_dir: String(t.sort_dir || 'asc') === 'desc' ? 'desc' : 'asc',
    results: Array.isArray(t.results) ? t.results : [],
    last_run_at: t.last_run_at || '',
    error: t.error || '',
    meta: t.meta || {},
    loading: !!t.loading,
  }));
  return out;
}

function getSelectedDashboardId() {
  return localStorage.getItem('scanner_dashboard_selected_id') || '';
}
function setSelectedDashboardId(id) {
  localStorage.setItem('scanner_dashboard_selected_id', id ? String(id) : '');
}

function currentWatchlistId(tile) {
  if (!state.current) return '';
  const top = String($('sd-watchlist')?.value || state.current.settings.default_watchlist_id || '');
  return String(tile.watchlist_id || top || '');
}

function timeframeMs() {
  const tf = $('sd-timeframe')?.value || state.current?.settings?.timeframe || '1h';
  return TF_MS[tf] || 60 * 60 * 1000;
}

function showNote(txt) {
  const el = $('sd-note');
  if (el) el.textContent = txt;
}

function setBusy(v, msg) {
  state.busy = !!v;
  const note = $('sd-note');
  if (note && msg) note.textContent = msg;
  if ($('sd-refresh')) $('sd-refresh').disabled = !!v;
}

function scheduleDashboardSave(delayMs = 250) {
  if (!state.current) return;
  if (state.saveTimer) clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => {
    state.saveTimer = null;
    if (!state.current) return;
    saveDashboard(false).catch(err => {
      console.error('auto-save failed', err);
      showNote(`❌ Auto-save failed: ${err.message || err}`);
    });
  }, delayMs);
}

async function loadWatchlists() {
  let rows = [];
  const endpoints = [
    '/scanner-builder/dashboard/api/watchlists',
    '/watchlists/',
    '/scanner-builder/api/watchlists',
    '/api/watchlists',
  ];
  for (const url of endpoints) {
    try {
      const d = await api(url);
      const got = d.watchlists || d.rows || d.items || [];
      if (Array.isArray(got) && got.length) {
        rows = got;
        break;
      }
      if (!rows.length) rows = got;
    } catch (err) {
      // keep trying next endpoint
    }
  }
  state.watchlists = Array.isArray(rows) ? rows : [];
  const opts = ['<option value="">Default watchlist</option>'].concat((state.watchlists || []).map(w => {
    const count = w.symbol_count != null ? ` (${w.symbol_count})` : '';
    const mark = w.is_default ? '⭐ ' : '';
    return `<option value="${esc(w.id)}">${mark}${esc(w.name || `Watchlist ${w.id}`)}${count}</option>`;
  }));
  const top = $('sd-watchlist');
  const modal = $('sd-modal-watchlist');
  const optsHtml = opts.join('');
  if (top) top.innerHTML = optsHtml;
  if (modal) modal.innerHTML = optsHtml;
  const selected = String(state.current?.settings?.default_watchlist_id || '').trim();
  const validSelected = selected && (state.watchlists || []).some(w => String(w.id) === selected);
  const fallback = (state.watchlists || [])[0] ? String((state.watchlists || [])[0].id) : '';
  const target = validSelected ? selected : fallback;
  if (top) {
    top.value = target || '';
  }
  if (modal) {
    const tileSelected = String(state.current?.tiles?.[state.modalTileIndex || 0]?.watchlist_id || target || '');
    modal.value = (state.watchlists || []).some(w => String(w.id) === tileSelected) ? tileSelected : (target || '');
  }
  if (state.current && !validSelected && target) {
    state.current.settings.default_watchlist_id = target;
  }
}

async function loadCatalog() {
  try {
    const d = await api('/scanner-builder/api/catalog');
    state.catalog = d || {};
  } catch (e) {
    console.warn('catalog load failed', e);
    state.catalog = { saved_scanners: [], builtin_scanners: [], function_meta: [], functions: [] };
  }
}

async function loadColumnTemplates() {
  try {
    const d = await api('/scanner-builder/api/column-templates');
    state.columnTemplates = d.templates || [];
  } catch (e) {
    console.warn('column template load failed', e);
    state.columnTemplates = [];
  }
  const modal = $('sd-modal-column-template');
  if (modal) modal.innerHTML = columnTemplateOptionsHtml('');
}

function dashboardOptionsHtml() {
  const list = state.dashboards || [];
  return list.map(d => `<option value="${esc(d.id)}">${esc(d.name || `Dashboard ${d.id}`)}</option>`).join('');
}

function applyDashboardToUi() {
  if (!state.current) return;
  $('sd-dashboard-name').value = state.current.name || state.current.settings?.name || DEFAULT_TITLE;
  $('sd-dashboard-select').innerHTML = dashboardOptionsHtml();
  if (state.current.id) $('sd-dashboard-select').value = String(state.current.id);
  $('sd-timeframe').value = state.current.settings.timeframe || '1h';
  $('sd-auto-refresh').checked = !!state.current.settings.auto_refresh;
  $('sd-edit-layout').checked = !!state.current.settings.edit_layout;
  if ($('sd-freeze-size')) $('sd-freeze-size').checked = !!state.current.settings.freeze_tile_size;
  if ($('sd-tile-width')) $('sd-tile-width').value = String(clampTileWidth(state.current.settings.tile_width));
  if ($('sd-tile-height')) $('sd-tile-height').value = String(clampTileHeight(state.current.settings.tile_height));
  state.editLayout = !!state.current.settings.edit_layout;
  state.autoRefresh = !!state.current.settings.auto_refresh;
  if ($('sd-watchlist')) {
    $('sd-watchlist').value = String(state.current.settings.default_watchlist_id || '');
  }
  updateSummary();
  renderTiles();
}

function updateSummary() {
  const sum = $('sd-summary');
  if (!sum || !state.current) return;
  const tiles = state.current.tiles || [];
  const wlid = $('sd-watchlist')?.value || state.current.settings.default_watchlist_id || '';
  const wname = state.watchlists.find(w => String(w.id) === String(wlid))?.name || (wlid ? `#${wlid}` : 'Default');
  const tf = $('sd-timeframe')?.value || state.current.settings.timeframe || '1h';
  sum.innerHTML = [
    { l: 'Tiles', v: tiles.length },
    { l: 'Layout', v: `${state.current.settings.layout_columns || 'Auto'}${state.current.settings.layout_columns ? ' col' : ''}${state.editLayout ? ' · Edit' : ''}` },
    { l: 'Tile size', v: `${clampTileWidth(state.current.settings.tile_width)}×${clampTileHeight(state.current.settings.tile_height)}${state.current.settings.freeze_tile_size ? ' fixed' : ''}` },
    { l: 'Refresh', v: state.autoRefresh ? 'On' : 'Off' },
    { l: 'Watchlist', v: wname },
    { l: 'Timeframe', v: tf },
  ].map(x => `<div class="sd-chip"><div class="l">${esc(x.l)}</div><div class="v">${esc(x.v)}</div></div>`).join('');
}

function makeTileHeader(tile, idx) {
  const q = tile.query_text || '';
  const title = tile.title || `Tile ${idx + 1}`;
  return `
    <div class="sd-tile-head">
      <div style="min-width:0;flex:1">
        <div class="sd-title">${esc(title)} <span class="sd-tag">${esc(q.slice(0, 46) || 'blank query')}</span></div>
        <div class="sd-subtitle">${esc(q || 'Enter a scanner query to start this tile.')}</div>
      </div>
      <div class="sd-actions">
        <span class="sd-drag-handle" data-drag-idx="${idx}" title="Move tile">↕</span>
        <button class="sd-ico" data-action="refresh-tile" data-idx="${idx}" title="Run tile">⟳</button>
        <button class="sd-ico" data-action="edit-tile" data-idx="${idx}" title="Edit query">✎</button>
        <button class="sd-ico" data-action="delete-tile" data-idx="${idx}" title="Delete tile">🗑</button>
      </div>
    </div>`;
}

function dashboardGridClass() {
  const cols = Number(state.current?.settings?.grid_columns || 3);
  return cols === 2 ? 'grid-cols-2' : 'grid-cols-3';
}
function applyQuickLayout(cols) {
  if (!state.current) return;
  const c = cols === 2 ? 2 : 3;
  state.current.settings.grid_columns = c;
  state.current.tiles = state.current.tiles || [];
  state.current.tiles.forEach((tile) => {
    tile.width = c === 2 ? 620 : 420;
    tile.height = c === 2 ? Math.max(360, Number(tile.height || 380)) : Math.max(320, Number(tile.height || 360));
    tile.x = 0;
    tile.y = 0;
  });
  renderTiles();
  showNote(`${c} column quick layout applied. Save dashboard to keep it.`);
}

function tileTableHtml(tile) {
  try {
    const rawRows = Array.isArray(tile.results) ? tile.results : [];
    if (tile.error) {
      return `<div class="sd-empty" style="color:#fca5a5">${esc(tile.error)}</div>`;
    }
    const cols = normalizeColumns(tile.meta?.result_columns || columnsForTile(tile));
    if (!rawRows.length) {
      return `<div class="sd-empty">${tile.loading ? 'Refreshing…' : 'No results.'}</div>`;
    }
    const rows = sortedTileRows(tile, rawRows, cols);
    const header = cols.map((c, i) => `<th class="sd-sortable" data-sort-tile="${esc(tile.id || '')}" data-sort-index="${i}" title="Sort by ${esc(c.label)}">${esc(c.label)}${Number(tile.sort_col) === i ? `<span class="sd-sort-mark">${tile.sort_dir === 'desc' ? '▼' : '▲'}</span>` : ''}</th>`).join('');
    const body = rows.map(row => `<tr>${cols.map(c => {
      const v = getColumnValue(row, c);
      const n = Number(v);
      const cls = !Number.isNaN(n) && String(v).trim() !== '' ? (n < 0 ? 'neg' : (n > 0 ? 'pos' : '')) : '';
      const extra = String(c.expr || '').toLowerCase() === 'reason' ? ' style="white-space:normal;min-width:260px"' : '';
      return `<td class="${cls}"${extra}>${esc(fmtColumn(v, c.format))}</td>`;
    }).join('')}</tr>`).join('');
    return `
      <div class="sd-table-wrap">
        <table class="sd-table">
          <thead><tr>${header}</tr></thead>
          <tbody>${body}</tbody>
        </table>
      </div>`;
  } catch (err) {
    console.error('tile render failed', err, tile);
    return `<div class="sd-empty" style="color:#fca5a5">Tile render error: ${esc(err.message || err)}</div>`;
  }
}

function tileBounds(t) {
  return {
    x: Number(t.x || 0),
    y: Number(t.y || 0),
    w: clampTileWidth(t.width),
    h: clampTileHeight(t.height),
  };
}

function rectsOverlap(a, b, gap = 12) {
  return !(
    a.x + a.w + gap <= b.x ||
    b.x + b.w + gap <= a.x ||
    a.y + a.h + gap <= b.y ||
    b.y + b.h + gap <= a.y
  );
}

function tileContainerWidth() {
  const wrap = $('sd-tiles');
  const rect = wrap?.getBoundingClientRect?.();
  const w = Math.floor(rect?.width || wrap?.clientWidth || (window.innerWidth - 32) || 1200);
  return Math.max(360, w);
}

function needsTilePacking() {
  const tiles = state.current?.tiles || [];
  const maxW = tileContainerWidth();
  const seen = [];
  for (const t of tiles) {
    const r = tileBounds(t);
    if (r.x < 0 || r.y < 0 || r.x + r.w > maxW + 2) return true;
    if (seen.some(x => rectsOverlap(x, r))) return true;
    seen.push(r);
  }
  return false;
}

function packTiles({ columns = 0, resize = false, rowHeight = 380 } = {}) {
  if (!state.current) return;
  const tiles = state.current.tiles || [];
  const gap = 12;
  const maxW = tileContainerWidth();
  let x = 0;
  let y = 0;
  let rowH = 0;
  const cols = Math.max(0, parseInt(columns || 0, 10) || 0);
  if (cols > 0) {
    const w = Math.max(MIN_TILE_WIDTH, Math.floor((maxW - gap * (cols - 1)) / cols));
    tiles.forEach((t, i) => {
      const h = Math.max(MIN_TILE_HEIGHT, resize ? rowHeight : Number(t.height || rowHeight));
      t.width = w;
      t.height = h;
      t.x = (i % cols) * (w + gap);
      t.y = Math.floor(i / cols) * (h + gap);
    });
    state.current.settings.layout_columns = cols;
    return;
  }
  tiles.forEach(t => {
    const w = Math.min(maxW, clampTileWidth(t.width || state.current.settings.tile_width));
    const h = clampTileHeight(t.height || rowHeight);
    if (x > 0 && x + w > maxW) {
      x = 0;
      y += rowH + gap;
      rowH = 0;
    }
    t.width = w;
    t.height = h;
    t.x = x;
    t.y = y;
    x += w + gap;
    rowH = Math.max(rowH, h);
  });
}

function ensureNonOverlappingTiles() {
  if (!state.editLayout || !state.current) return;
  if (needsTilePacking()) {
    packTiles({ columns: 0, resize: false });
    showNote('Layout auto-packed to prevent overlapping or off-screen tiles. Save dashboard to keep it.');
  }
}

function applyTilePreset(columns) {
  if (!state.current) return;
  const cols = Math.max(1, parseInt(columns || 0, 10) || 0);
  state.current.settings.edit_layout = true;
  state.current.settings.layout_columns = cols;
  state.editLayout = true;
  const chk = $('sd-edit-layout');
  if (chk) chk.checked = true;
  packTiles({ columns: cols, resize: true, rowHeight: cols >= 3 ? 330 : 390 });
  renderTiles();
  showNote(`Applied ${cols} column layout. Save dashboard to keep it.`);
}

function renderTiles() {
  const wrap = $('sd-tiles');
  if (!wrap || !state.current) return;
  wrap.classList.toggle('layout-editing', !!state.editLayout);
  const freezeSize = !!state.current.settings?.freeze_tile_size;
  wrap.classList.toggle('fixed-size', freezeSize);
  const layoutCols = Math.max(0, parseInt(state.current.settings?.layout_columns || 0, 10) || 0);
  wrap.style.gridTemplateColumns = freezeSize ? '' : (layoutCols > 0
    ? `repeat(${layoutCols}, minmax(0, 1fr))`
    : 'repeat(auto-fit, minmax(min(380px, 100%), 1fr))');
  wrap.style.minHeight = '';
  wrap.innerHTML = (state.current.tiles || []).map((tile, idx) => {
    const style = [];
    const tileW = clampTileWidth(tile.width || state.current.settings.tile_width);
    const tileH = clampTileHeight(tile.height || state.current.settings.tile_height);
    // Keep the tile body and result grid bound to the tile dimensions. Without an explicit height,
    // large result sets expand the card instead of scrolling inside the grid.
    style.push(`--tile-w:${tileW}px;--tile-h:${tileH}px;height:${tileH}px;min-height:${MIN_TILE_HEIGHT}px;`);
    if (freezeSize) {
      style.push(`width:${tileW}px;min-width:${tileW}px;max-width:${tileW}px;max-height:${tileH}px;flex:0 0 ${tileW}px;`);
    } else if (state.editLayout) {
      style.push(`width:${tileW}px;min-width:${MIN_TILE_WIDTH}px;flex:0 0 ${tileW}px;`);
    }
    const wlOptions = ['<option value="">Default</option>'].concat((state.watchlists || []).map(w => `<option value="${esc(w.id)}">${esc(w.name || `Watchlist ${w.id}`)}</option>`)).join('');
    const templateOptions = columnTemplateOptionsHtml(tile.result_template_id || '');
    const templateName = templateById(tile.result_template_id)?.name || 'Default columns';
    const title = tile.title || `Tile ${idx + 1}`;
    return `
      <div class="sd-tile${state.editLayout ? ' editable' : ''}" data-idx="${idx}" draggable="${state.editLayout ? 'true' : 'false'}" style="${style.join(' ')}">
        ${makeTileHeader(tile, idx)}
        <div class="sd-config">
          <div>
            <label>Watchlist</label>
            <select class="sd-select" data-field="watchlist_id" data-idx="${idx}">${wlOptions}</select>
          </div>
          <div>
            <label>Prior days</label>
            <input class="sd-input" data-field="prior_days" data-idx="${idx}" type="number" min="0" step="1" value="${esc(tile.prior_days || 0)}" />
          </div>
          <div>
            <label>Limit</label>
            <input class="sd-input" data-field="limit" data-idx="${idx}" type="number" min="1" step="1" value="${esc(tile.limit || 200)}" />
          </div>
          <div>
            <label>Width</label>
            <input class="sd-input" data-field="width" data-idx="${idx}" type="number" min="320" step="10" value="${esc(tileW)}" />
          </div>
          <div>
            <label>Height</label>
            <input class="sd-input" data-field="height" data-idx="${idx}" type="number" min="240" step="10" value="${esc(tileH)}" />
          </div>
          <div>
            <label>Columns</label>
            <select class="sd-select" data-field="result_template_id" data-idx="${idx}">${templateOptions}</select>
          </div>
        </div>
        <div class="sd-template-line"><span>Columns: ${esc(templateName)}</span>${state.editLayout?'<span>Drag tile to reorder; drag the bottom-right handle to resize width/height. Use 2x2 / 3x3 for quick column layouts.</span>':''}</div>
        <div class="sd-results">
          <div class="sd-res-meta">
            <span>${esc(tile.loading ? 'Refreshing…' : (tile.last_run_at ? `Last run: ${tile.last_run_at}` : 'Not run yet'))}</span>
            <span>${Array.isArray(tile.results) ? `${tile.results.length} row(s)` : '0 row(s)'}</span>
          </div>
          ${tileTableHtml(tile)}
        </div>
        ${state.editLayout ? `<div class="sd-resize-handle" data-resize-idx="${idx}" title="Drag to resize tile width and height"></div>` : ''}
      </div>`;
  }).join('') || '<div class="sd-empty" style="width:100%">No tiles yet — click + Add tile.</div>';

  // apply selection values after rendering
  (state.current.tiles || []).forEach((tile, idx) => {
    const sel = wrap.querySelector(`select[data-field="watchlist_id"][data-idx="${idx}"]`);
    if (sel) sel.value = String(tile.watchlist_id || '');
    const csel = wrap.querySelector(`select[data-field="result_template_id"][data-idx="${idx}"]`);
    if (csel) csel.value = String(tile.result_template_id || '');
  });
  updateSummary();
  wireTileEvents();
  wireTileDrag();
}

function setTileField(idx, field, value) {
  const tile = state.current?.tiles?.[idx];
  if (!tile) return;
  if (field === 'prior_days' || field === 'limit' || field === 'width' || field === 'height' || field === 'x' || field === 'y') {
    tile[field] = Math.max(0, parseInt(value || 0, 10) || 0);
    if (field === 'limit') tile[field] = Math.max(1, tile[field]);
    if (field === 'width') tile[field] = Math.max(320, tile[field]);
    if (field === 'height') tile[field] = Math.max(240, tile[field]);
    if (field === 'x' || field === 'y') tile[field] = Math.max(0, tile[field]);
  } else if (field === 'result_template_id') {
    tile.result_template_id = String(value || '');
    tile.result_columns_json = '';
    tile.meta = null;
  } else {
    tile[field] = String(value || '');
  }
}


function updateTileSizeInputs(idx, width, height) {
  const wrap = $('sd-tiles');
  const wInput = wrap?.querySelector(`input[data-field="width"][data-idx="${idx}"]`);
  const hInput = wrap?.querySelector(`input[data-field="height"][data-idx="${idx}"]`);
  if (wInput) wInput.value = String(clampTileWidth(width));
  if (hInput) hInput.value = String(clampTileHeight(height));
}

function applyTileElementSize(el, width, height) {
  if (!el) return;
  const w = clampTileWidth(width);
  const h = clampTileHeight(height);
  el.style.setProperty('--tile-w', `${w}px`);
  el.style.setProperty('--tile-h', `${h}px`);
  el.style.width = `${w}px`;
  el.style.flexBasis = `${w}px`;
  el.style.height = `${h}px`;
  el.style.minHeight = `${MIN_TILE_HEIGHT}px`;
  if (state.current?.settings?.freeze_tile_size) {
    el.style.minWidth = `${w}px`;
    el.style.maxWidth = `${w}px`;
    el.style.maxHeight = `${h}px`;
  } else {
    el.style.minWidth = `${MIN_TILE_WIDTH}px`;
    el.style.maxWidth = '';
    el.style.maxHeight = '';
  }
}

function captureTileSizes() {
  const wrap = $('sd-tiles');
  if (!wrap || !state.current) return;
  // Outside edit-layout mode, tiles render at width:100% of their grid cell
  // (see .sd-tile CSS) rather than their real stored pixel size, so
  // measuring the DOM here would silently overwrite a correct saved
  // width/height with the stretched grid width. Only trust the DOM
  // measurement while edit-layout is actually active and tiles are
  // rendered at their real inline width/height.
  if (!state.editLayout) return;
  wrap.querySelectorAll('.sd-tile').forEach(el => {
    const idx = Number(el.dataset.idx);
    const tile = state.current.tiles[idx];
    if (!tile) return;
    const rect = el.getBoundingClientRect();
    tile.width = clampTileWidth(Math.round(rect.width));
    tile.height = clampTileHeight(Math.round(rect.height));
  });
}

async function loadDashboards(selectId = null) {
  let d = null;
  try {
    d = await api('/scanner-builder/dashboard/api/dashboards');
  } catch (err) {
    console.error('dashboard list load failed', err);
    state.current = normalizeDashboard(defaultDashboard(DEFAULT_TITLE));
    state.dashboards = [state.current];
    applyDashboardToUi();
    showNote('Using local starter dashboard because saved dashboard API was unavailable: ' + (err.message || err));
    return;
  }
  state.dashboards = d.dashboards || [];
  const sel = $('sd-dashboard-select');
  if (sel) sel.innerHTML = dashboardOptionsHtml();
  let target = selectId || getSelectedDashboardId() || (state.dashboards[0] && String(state.dashboards[0].id)) || '';
  if (!target && state.dashboards[0]) target = String(state.dashboards[0].id);
  if (!target) {
    // no dashboards yet, create a starter one
    const created = await api('/scanner-builder/dashboard/api/dashboards', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(defaultDashboard(DEFAULT_TITLE)),
    });
    state.current = normalizeDashboard(created.dashboard);
    state.dashboards = [state.current];
    sel.innerHTML = dashboardOptionsHtml();
    sel.value = String(state.current.id || '');
    setSelectedDashboardId(state.current.id);
    applyDashboardToUi();
    return;
  }
  if (sel && target) sel.value = String(target);
  await loadDashboard(target);
}

async function loadDashboard(id) {
  if (!id) return;
  try {
    const d = await api(`/scanner-builder/dashboard/api/dashboards/${id}`);
    state.current = normalizeDashboard(d.dashboard || d);
    setSelectedDashboardId(state.current.id);
  } catch (err) {
    console.error('dashboard load failed', err);
    state.current = normalizeDashboard(defaultDashboard(DEFAULT_TITLE));
    showNote('Using local starter dashboard because selected dashboard could not load: ' + (err.message || err));
  }
  applyDashboardToUi();
  await loadWatchlists().catch(e => console.warn('watchlists reload failed', e));
  renderTiles();
  await refreshDashboard({ silent: true, runTiles: false }).catch(e => { console.warn('initial refresh failed', e); showNote('Dashboard loaded; initial refresh failed: ' + (e.message || e)); });
}

async function saveDashboard(asNew = false) {
  if (!state.current) return;
  if (state.saveTimer) { clearTimeout(state.saveTimer); state.saveTimer = null; }
  captureTileSizes();
  syncStateFromUi();
  const payload = {
    id: asNew ? null : state.current.id,
    name: $('sd-dashboard-name').value.trim() || DEFAULT_TITLE,
    layout: {
      settings: {
        ...(state.current.settings || {}),
        name: $('sd-dashboard-name').value.trim() || DEFAULT_TITLE,
        default_watchlist_id: $('sd-watchlist')?.value || '',
        timeframe: $('sd-timeframe')?.value || '1h',
        auto_refresh: !!$('sd-auto-refresh')?.checked,
        edit_layout: !!$('sd-edit-layout')?.checked,
        freeze_tile_size: !!$('sd-freeze-size')?.checked,
        tile_width: clampTileWidth($('sd-tile-width')?.value || state.current.settings?.tile_width),
        tile_height: clampTileHeight($('sd-tile-height')?.value || state.current.settings?.tile_height),
        layout_columns: Math.max(0, parseInt(state.current.settings?.layout_columns || 0, 10) || 0),
      },
      tiles: state.current.tiles || [],
    },
  };
  const url = asNew || !state.current.id ? '/scanner-builder/dashboard/api/dashboards' : `/scanner-builder/dashboard/api/dashboards/${state.current.id}`;
  const method = asNew || !state.current.id ? 'POST' : 'PUT';
  const d = await api(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  state.current = normalizeDashboard(d.dashboard || payload.layout);
  setSelectedDashboardId(state.current.id);
  state.dashboards = (await api('/scanner-builder/dashboard/api/dashboards')).dashboards || state.dashboards;
  $('sd-dashboard-select').innerHTML = dashboardOptionsHtml();
  $('sd-dashboard-select').value = String(state.current.id || '');
  updateSummary();
  showNote(`Saved dashboard ${state.current.name}`);
}

async function newDashboard() {
  const name = prompt('Dashboard name', 'My Scanner Dashboard') || '';
  if (!name.trim()) return;
  const created = await api('/scanner-builder/dashboard/api/dashboards', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(defaultDashboard(name.trim())),
  });
  state.current = normalizeDashboard(created.dashboard);
  state.dashboards = (await api('/scanner-builder/dashboard/api/dashboards')).dashboards || [];
  $('sd-dashboard-select').innerHTML = dashboardOptionsHtml();
  $('sd-dashboard-select').value = String(state.current.id || '');
  setSelectedDashboardId(state.current.id);
  applyDashboardToUi();
  renderTiles();
  showNote(`Created dashboard ${state.current.name}`);
}

async function copyDashboard() {
  if (!state.current) return;
  const name = prompt('Copy dashboard as', `${state.current.name || DEFAULT_TITLE} Copy`) || '';
  if (!name.trim()) return;
  const copied = clone(state.current);
  copied.id = null;
  copied.name = name.trim();
  copied.settings.name = name.trim();
  copied.tiles = copied.tiles || [];
  copied.tiles.forEach(t => { t.id = uid('tile'); t.results = []; t.last_run_at = ''; t.error = ''; });
  const d = await api('/scanner-builder/dashboard/api/dashboards', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(copied),
  });
  state.current = normalizeDashboard(d.dashboard);
  state.dashboards = (await api('/scanner-builder/dashboard/api/dashboards')).dashboards || [];
  $('sd-dashboard-select').innerHTML = dashboardOptionsHtml();
  $('sd-dashboard-select').value = String(state.current.id || '');
  setSelectedDashboardId(state.current.id);
  applyDashboardToUi();
  showNote(`Copied dashboard to ${state.current.name}`);
}

function addTile() {
  if (!state.current) return;
  state.current.tiles.push({
    id: uid(),
    title: `Tile ${state.current.tiles.length + 1}`,
    query_text: DEFAULT_QUERY,
    watchlist_id: $('sd-watchlist')?.value || state.current.settings.default_watchlist_id || '',
    prior_days: 0,
    limit: 200,
    width: 420,
    height: 380,
    x: ((state.current.tiles.length || 0) % 3) * 440,
    y: Math.floor((state.current.tiles.length || 0) / 3) * 420,
    result_template_id: '',
    result_columns_json: '',
    results: [],
    last_run_at: '',
    error: '',
  });
  renderTiles();
  scheduleDashboardSave();
  showNote('Tile added and saved');
}

function deleteTile(idx) {
  if (!state.current?.tiles?.[idx]) return;
  if (!confirm(`Delete tile "${state.current.tiles[idx].title || `Tile ${idx + 1}`}"?`)) return;
  state.current.tiles.splice(idx, 1);
  renderTiles();
  scheduleDashboardSave();
}

function openTileEditor(idx) {
  const tile = state.current?.tiles?.[idx];
  if (!tile) return;
  state.modalTileIndex = idx;
  showModalError('');
  $('sd-modal-title').value = tile.title || '';
  $('sd-modal-query').value = tile.query_text || '';
  $('sd-modal-watchlist').value = String(tile.watchlist_id || $('sd-watchlist')?.value || '');
  $('sd-modal-prior-days').value = String(tile.prior_days || 0);
  $('sd-modal-limit').value = String(tile.limit || 200);
  if ($('sd-modal-width')) $('sd-modal-width').value = String(clampTileWidth(tile.width || state.current.settings.tile_width));
  if ($('sd-modal-height')) $('sd-modal-height').value = String(clampTileHeight(tile.height || state.current.settings.tile_height));
  if ($('sd-modal-column-template')) {
    $('sd-modal-column-template').innerHTML = columnTemplateOptionsHtml(tile.result_template_id || '');
    $('sd-modal-column-template').value = String(tile.result_template_id || '');
  }
  $('sd-modal-backdrop').style.display = 'flex';
  showAutocomplete(true);
  $('sd-modal-query').focus();
  updateAutocompleteFromTextarea();
}

function showModalError(msg) {
  const el = $('sd-modal-error');
  if (!el) return;
  if (!msg) { el.style.display = 'none'; el.textContent = ''; return; }
  el.textContent = `⚠ ${msg}`;
  el.style.display = 'block';
}

function closeTileEditor() {
  $('sd-modal-backdrop').style.display = 'none';
  $('sd-autocomplete').style.display = 'none';
  showModalError('');
  state.autocomplete = { items: [], idx: 0, visible: false };
  state.modalTileIndex = null;
}

function showAutocomplete(force = false) {
  const panel = $('sd-autocomplete');
  if (!panel) return;
  if (force) {
    updateAutocompleteFromTextarea();
  }
  panel.style.display = state.autocomplete.items.length ? 'block' : 'none';
}

function currentQueryToken(text, cursor) {
  const before = text.slice(0, cursor);
  const scanMatch = before.match(/scan\(\s*([A-Za-z0-9_\- ]*)$/i);
  if (scanMatch) {
    return { mode: 'scan', typed: (scanMatch[1] || '').trim().toLowerCase(), start: before.length - scanMatch[1].length, end: cursor };
  }
  const token = before.match(/([A-Za-z_][A-Za-z0-9_]*)$/);
  if (token) {
    return { mode: 'token', typed: token[1].toLowerCase(), start: before.length - token[1].length, end: cursor };
  }
  return { mode: 'none', typed: '', start: cursor, end: cursor };
}

function allScannerEntries() {
  const saved = (state.catalog?.saved_scanners || []).map(x => ({
    name: x.name,
    signature: x.query_text || `scan(${x.name})`,
    description: x.description || x.query_text || 'Saved scanner',
    kind: 'scanner',
  }));
  const builtins = (state.catalog?.builtin_scanners || []).map(x => ({
    name: x.name,
    signature: x.query_text || `scan(${x.name})`,
    description: x.description || 'Built-in scanner',
    kind: 'scanner',
  }));
  const seen = new Set();
  return saved.concat(builtins).filter(x => {
    const k = String(x.name || '').toLowerCase();
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function allFunctionEntries() {
  return (state.catalog?.function_meta || []).map(x => ({
    name: x.name,
    signature: x.signature || `${x.name}()`,
    description: x.description || 'Function',
    kind: 'function',
  }));
}

function autocompleteSource() {
  return allScannerEntries().concat(allFunctionEntries());
}

function suggestionText(item) {
  if (!item) return '';
  if (item.kind === 'function') return item.signature || `${item.name}()`;
  return `scan(${item.name})`;
}

function renderAutocomplete(items, activeIdx = 0) {
  const panel = $('sd-autocomplete');
  if (!panel) return;
  if (!items.length) {
    panel.style.display = 'none';
    panel.innerHTML = '';
    state.autocomplete = { items: [], idx: 0, visible: false };
    return;
  }
  state.autocomplete = { items, idx: activeIdx, visible: true };
  panel.innerHTML = items.map((x, idx) => {
    const subtitle = x.kind === 'function' ? (x.signature || '') : (x.description || 'Scanner');
    const code = x.kind === 'function' ? (x.signature || '') : (x.signature || `scan(${x.name})`);
    return `
      <div class="sd-ac-item${idx === activeIdx ? ' active' : ''}" data-idx="${idx}">
        <div style="min-width:0;flex:1">
          <b>${esc(x.name)}</b> <span class="mini">${esc(subtitle)}</span>
          <div class="sd-ac-code">${esc(code)}</div>
        </div>
        <div class="mini">${x.kind === 'function' ? 'fn' : 'scan'}</div>
      </div>`;
  }).join('');
  panel.style.display = 'block';
}

function updateAutocompleteFromTextarea() {
  const ta = $('sd-modal-query');
  if (!ta) return;
  const tok = currentQueryToken(ta.value, ta.selectionStart ?? ta.value.length);
  const typed = tok.typed || '';
  const mode = tok.mode;
  let items = autocompleteSource().filter(x => {
    if (!typed) return true;
    const hay = `${x.name || ''} ${x.signature || ''} ${x.description || ''}`.toLowerCase();
    return hay.includes(typed);
  });
  if (mode === 'scan') items = items.filter(x => x.kind === 'scanner');
  if (mode === 'token') items = items.filter(x => x.kind === 'function' || x.kind === 'scanner');
  renderAutocomplete(items.slice(0, 24), 0);
  showAutocomplete(false);
}

function replaceTextInTextarea(ta, start, end, insert) {
  const before = ta.value.slice(0, start);
  const after = ta.value.slice(end);
  ta.value = before + insert + after;
  const pos = start + insert.length;
  ta.setSelectionRange(pos, pos);
  ta.focus();
}

function commitAutocompleteItem(item) {
  const ta = $('sd-modal-query');
  if (!ta || !item) return;
  const tok = currentQueryToken(ta.value, ta.selectionStart ?? ta.value.length);
  const replacement = suggestionText(item);
  if (tok.mode === 'scan') {
    const scanStart = ta.value.slice(0, tok.start).lastIndexOf('scan(');
    const start = scanStart >= 0 ? scanStart : tok.start;
    replaceTextInTextarea(ta, start, tok.end, replacement);
  } else if (tok.mode === 'token') {
    replaceTextInTextarea(ta, tok.start, tok.end, replacement);
  } else {
    replaceTextInTextarea(ta, ta.selectionStart ?? ta.value.length, ta.selectionEnd ?? ta.value.length, replacement);
  }
  updateAutocompleteFromTextarea();
}

async function runTile(idx, { silent = false } = {}) {
  const tile = state.current?.tiles?.[idx];
  if (!tile) return;
  tile.loading = true;
  tile.error = '';
  if (!silent) renderTiles();
  const watchlistId = currentWatchlistId(tile);
  try {
    const payload = {
      query_text: tile.query_text,
      benchmark: 'SPY',
      watchlist_id: watchlistId || null,
      limit: tile.limit || 200,
      prior_days: tile.prior_days || 0,
      columns: columnsForTile(tile),
      result_template_id: tile.result_template_id || null,
    };
    const d = await api('/scanner-builder/api/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    tile.results = d.results || [];
    tile.last_run_at = d.scanned_at || new Date().toISOString().slice(0,19).replace('T',' ');
    tile.error = d.error || '';
    tile.count = d.count || (tile.results || []).length || 0;
    tile.meta = d;
  } catch (e) {
    tile.error = e.message || String(e);
    tile.results = [];
  } finally {
    tile.loading = false;
  }
}

async function kickWatchlistFetches() {
  const ids = new Set();
  const topId = String($('sd-watchlist')?.value || state.current?.settings?.default_watchlist_id || '');
  if (topId) ids.add(topId);
  for (const t of (state.current?.tiles || [])) {
    const w = String(t.watchlist_id || '');
    if (w) ids.add(w);
  }
  if (!ids.size) return;
  const calls = [...ids].map(id => api(`/watchlists/${id}/fetch_data`, { method: 'POST' }).catch(() => null));
  await Promise.allSettled(calls);
}

async function refreshDashboard({ silent = false, fetchFreshData = false, runTiles = true } = {}) {
  if (!state.current) return;
  const tf = $('sd-timeframe')?.value || state.current.settings.timeframe || '1h';
  const refreshMsg = state.autoRefresh ? `Auto refresh (${tf})` : 'Manual refresh';
  setBusy(true, `⏳ ${refreshMsg}…`);
  // This was the actual bug: kickWatchlistFetches() -- which fetches
  // fresh price/OI data for EVERY tile's watchlist, a genuinely
  // expensive operation across potentially hundreds of symbols per
  // watchlist -- fired unconditionally every time this function ran,
  // including on plain dashboard load and on every dropdown change,
  // completely regardless of whether Auto Refresh was even switched on.
  // Now it only runs when actually requested: the manual Refresh button,
  // or a genuine auto-refresh timer tick with the toggle on.
  if (fetchFreshData) {
    await kickWatchlistFetches();
    // small delay so the background fetch has a chance to start writing cache rows
    await new Promise(r => setTimeout(r, 700));
  }
  const tiles = state.current.tiles || [];
  if (runTiles) {
    await Promise.allSettled(tiles.map((_, idx) => runTile(idx, { silent: true })));
  }
  // This was the SECOND half of the same bug -- fixing kickWatchlistFetches
  // alone wasn't enough, since every tile's saved scanner query still ran
  // unconditionally on plain page load regardless of that fix, each one a
  // full POST to /scanner-builder/api/run. That's what was firing within
  // seconds of every app restart if this page happened to reload: not a
  // watchlist fetch, but every tile's actual scan query re-executing.
  // Plain load now just skips runTile() entirely -- renderTiles() already
  // falls back to "Not run yet" for any tile with no last_run_at, so
  // nothing extra is needed to show that state correctly.
  renderTiles();
  setBusy(false, runTiles ? `✅ Refreshed ${tiles.length} tile(s)` : `Loaded ${tiles.length} tile(s) — click Refresh to run`);
  restartAutoRefresh();
}

function restartAutoRefresh() {
  if (state.autoTimer) {
    clearInterval(state.autoTimer);
    state.autoTimer = null;
  }
  if (!$('sd-auto-refresh')?.checked) return;
  const ms = timeframeMs();
  state.autoTimer = setInterval(() => {
    refreshDashboard({ silent: true, fetchFreshData: true }).catch(e => console.warn('auto refresh:', e));
  }, ms);
}

function syncStateFromUi() {
  if (!state.current) return;
  state.current.name = $('sd-dashboard-name')?.value?.trim() || DEFAULT_TITLE;
  state.current.settings.name = state.current.name;
  state.current.settings.default_watchlist_id = $('sd-watchlist')?.value || '';
  state.current.settings.timeframe = $('sd-timeframe')?.value || '1h';
  state.current.settings.auto_refresh = !!$('sd-auto-refresh')?.checked;
  state.current.settings.edit_layout = !!$('sd-edit-layout')?.checked;
  state.current.settings.freeze_tile_size = !!$('sd-freeze-size')?.checked;
  state.current.settings.tile_width = clampTileWidth($('sd-tile-width')?.value || state.current.settings.tile_width);
  state.current.settings.tile_height = clampTileHeight($('sd-tile-height')?.value || state.current.settings.tile_height);
  state.editLayout = state.current.settings.edit_layout;
  state.autoRefresh = state.current.settings.auto_refresh;
}

function adjustEditingCanvasHeight() {
  const wrap = $('sd-tiles');
  if (!wrap) return;
  wrap.style.minHeight = '';
}

function wireTileDrag() {
  // Grid layout handles row wrapping. Tile movement is drag/drop reorder in wireTileEvents.
}

function wireTileEvents() {
  const wrap = $('sd-tiles');
  if (!wrap) return;

  const isTileField = (el) => el && el.matches && el.matches('[data-field]');
  const isTileAction = (el) => el && el.matches && el.matches('[data-action]');

  if (!wrap.dataset.boundTileEvents) {
    wrap.dataset.boundTileEvents = '1';

    wrap.addEventListener('mousedown', ev => {
      const handle = ev.target.closest('[data-resize-idx]');
      if (!handle || !state.editLayout) return;
      ev.preventDefault();
      ev.stopPropagation();
      const tileEl = handle.closest('.sd-tile');
      const idx = Number(handle.dataset.resizeIdx);
      const tile = state.current?.tiles?.[idx];
      if (!tileEl || !tile) return;
      const rect = tileEl.getBoundingClientRect();
      const startX = ev.clientX;
      const startY = ev.clientY;
      const startW = rect.width;
      const startH = rect.height;
      state.resizeDrag = { idx, startX, startY, startW, startH };
      document.body.classList.add('sd-resizing-active');

      const onMove = e => {
        if (!state.resizeDrag) return;
        const w = clampTileWidth(Math.round(startW + e.clientX - startX));
        const h = clampTileHeight(Math.round(startH + e.clientY - startY));
        tile.width = w;
        tile.height = h;
        applyTileElementSize(tileEl, w, h);
        updateTileSizeInputs(idx, w, h);
      };
      const onUp = () => {
        if (state.resizeDrag) {
          const w = clampTileWidth(tile.width);
          const h = clampTileHeight(tile.height);
          updateTileSizeInputs(idx, w, h);
          updateSummary();
          showNote(`Tile resized to ${w}x${h}.`);
          scheduleDashboardSave();
        }
        state.resizeDrag = null;
        document.body.classList.remove('sd-resizing-active');
        window.removeEventListener('mousemove', onMove, true);
        window.removeEventListener('mouseup', onUp, true);
      };
      window.addEventListener('mousemove', onMove, true);
      window.addEventListener('mouseup', onUp, true);
    });

    wrap.addEventListener('click', async e => {
      const sortEl = e.target.closest('th[data-sort-index]');
      if (sortEl) {
        e.preventDefault();
        e.stopPropagation();
        const tileEl = sortEl.closest('.sd-tile');
        const idx = Number(tileEl?.dataset.idx);
        const tile = state.current?.tiles?.[idx];
        if (!tile) return;
        const colIdx = Number(sortEl.dataset.sortIndex);
        if (Number(tile.sort_col) === colIdx) tile.sort_dir = tile.sort_dir === 'desc' ? 'asc' : 'desc';
        else { tile.sort_col = colIdx; tile.sort_dir = 'asc'; }
        renderTiles();
        showNote('Sorted tile results. Save dashboard to remember the sort order.');
        return;
      }
      const actionEl = e.target.closest('[data-action]');
      if (!actionEl) return;
      e.preventDefault();
      e.stopPropagation();
      const idx = Number(actionEl.dataset.idx);
      const action = actionEl.dataset.action;
      if (action === 'edit-tile') {
        openTileEditor(idx);
        return;
      }
      if (action === 'delete-tile') {
        deleteTile(idx);
        return;
      }
      if (action === 'refresh-tile') {
        await runTile(idx, { silent: false });
        renderTiles();
        return;
      }
    });

    wrap.addEventListener('change', async e => {
      const fieldEl = e.target.closest('[data-field]');
      if (!fieldEl) return;
      const idx = Number(fieldEl.dataset.idx);
      const field = fieldEl.dataset.field;
      setTileField(idx, field, fieldEl.value);
      updateSummary();
      // changing watchlist/prior days should immediately rerun the tile
      if (field === 'watchlist_id' || field === 'prior_days' || field === 'result_template_id') {
        await runTile(idx, { silent: false });
        renderTiles();
      } else {
        renderTiles();
      }
      scheduleDashboardSave();
    });

    wrap.addEventListener('dragstart', ev => {
      if (!state.editLayout || ev.target.closest('[data-resize-idx]')) return;
      const tileEl = ev.target.closest('.sd-tile');
      if (!tileEl) return;
      state.dragIndex = Number(tileEl.dataset.idx);
      tileEl.classList.add('sd-dragging');
      ev.dataTransfer.effectAllowed = 'move';
      ev.dataTransfer.setData('text/plain', String(state.dragIndex));
    });

    wrap.addEventListener('dragend', ev => {
      const tileEl = ev.target.closest('.sd-tile');
      if (tileEl) tileEl.classList.remove('sd-dragging');
      state.dragIndex = null;
    });

    wrap.addEventListener('dragover', ev => {
      if (!state.editLayout) return;
      if (ev.target.closest('.sd-tile')) ev.preventDefault();
    });

    wrap.addEventListener('drop', ev => {
      if (!state.editLayout) return;
      const targetEl = ev.target.closest('.sd-tile');
      if (!targetEl) return;
      ev.preventDefault();
      const from = state.dragIndex == null ? Number(ev.dataTransfer.getData('text/plain')) : Number(state.dragIndex);
      const to = Number(targetEl.dataset.idx);
      if (!Number.isFinite(from) || !Number.isFinite(to) || from === to) return;
      const tiles = state.current?.tiles || [];
      if (!tiles[from] || !tiles[to]) return;
      captureTileSizes();
      const moved = tiles.splice(from, 1)[0];
      tiles.splice(to, 0, moved);
      state.dragIndex = null;
      renderTiles();
      showNote('Tile moved and saved.');
      scheduleDashboardSave();
    });

    wrap.addEventListener('dblclick', ev => {
      if (ev.target.closest('button, input, select, textarea')) return;
      const tileEl = ev.target.closest('.sd-tile');
      if (!tileEl) return;
      openTileEditor(Number(tileEl.dataset.idx));
    });
  }
}

function bindModalEvents() {
  $('sd-modal-close')?.addEventListener('click', closeTileEditor);
  $('sd-cancel-tile')?.addEventListener('click', closeTileEditor);
  $('sd-modal-backdrop')?.addEventListener('click', e => {
    if (e.target === $('sd-modal-backdrop')) closeTileEditor();
  });
  $('sd-modal-query')?.addEventListener('input', updateAutocompleteFromTextarea);
  $('sd-modal-query')?.addEventListener('focus', updateAutocompleteFromTextarea);
  $('sd-modal-query')?.addEventListener('keydown', e => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      state.autocomplete.idx = Math.min((state.autocomplete.items.length || 1) - 1, state.autocomplete.idx + 1);
      renderAutocomplete(state.autocomplete.items, state.autocomplete.idx);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      state.autocomplete.idx = Math.max(0, state.autocomplete.idx - 1);
      renderAutocomplete(state.autocomplete.items, state.autocomplete.idx);
    } else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey || e.shiftKey || state.autocomplete.visible)) {
      e.preventDefault();
      const item = state.autocomplete.items[state.autocomplete.idx] || state.autocomplete.items[0];
      if (item) commitAutocompleteItem(item);
    } else if (e.key === 'Tab') {
      if (state.autocomplete.items.length) {
        e.preventDefault();
        const item = state.autocomplete.items[state.autocomplete.idx] || state.autocomplete.items[0];
        if (item) commitAutocompleteItem(item);
      }
    } else if (e.key === 'Escape') {
      $('sd-autocomplete').style.display = 'none';
    }
  });
  $('sd-autocomplete')?.addEventListener('mousedown', e => {
    const itemEl = e.target.closest('.sd-ac-item');
    if (!itemEl) return;
    e.preventDefault();
    const idx = Number(itemEl.dataset.idx || 0);
    const item = state.autocomplete.items[idx];
    if (item) commitAutocompleteItem(item);
  });
  $('sd-save-tile')?.addEventListener('click', async () => {
    const idx = state.modalTileIndex;
    if (idx == null || !state.current?.tiles?.[idx]) return;
    if (state.savingTile) return;
    const btn = $('sd-save-tile');
    const tile = state.current.tiles[idx];
    tile.title = $('sd-modal-title').value.trim() || `Tile ${idx + 1}`;
    tile.query_text = $('sd-modal-query').value.trim() || DEFAULT_QUERY;
    tile.watchlist_id = $('sd-modal-watchlist').value || '';
    tile.prior_days = Math.max(0, parseInt($('sd-modal-prior-days').value || '0', 10) || 0);
    tile.limit = Math.max(1, parseInt($('sd-modal-limit').value || '200', 10) || 200);
    tile.width = clampTileWidth($('sd-modal-width')?.value || tile.width || state.current.settings.tile_width);
    tile.height = clampTileHeight($('sd-modal-height')?.value || tile.height || state.current.settings.tile_height);
    tile.result_template_id = $('sd-modal-column-template')?.value || '';
    tile.result_columns_json = '';
    state.savingTile = true;
    showModalError('');
    const prevLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    try {
      renderTiles();
      await saveDashboard(false);
      closeTileEditor();
      showNote(`Saved tile ${tile.title}`);
    } catch (err) {
      console.error('Save tile failed', err);
      const msg = err?.message || String(err);
      showModalError(`Failed to save tile: ${msg}`);
      showNote(`Failed to save tile: ${msg}`);
    } finally {
      state.savingTile = false;
      if (btn) { btn.disabled = false; btn.textContent = prevLabel || 'Save tile'; }
    }
  });
  $('sd-run-tile')?.addEventListener('click', async () => {
    const idx = state.modalTileIndex;
    if (idx == null) return;
    if (state.savingTile) return;
    const btn = $('sd-run-tile');
    const tile = state.current.tiles[idx];
    tile.title = $('sd-modal-title').value.trim() || `Tile ${idx + 1}`;
    tile.query_text = $('sd-modal-query').value.trim() || DEFAULT_QUERY;
    tile.watchlist_id = $('sd-modal-watchlist').value || '';
    tile.prior_days = Math.max(0, parseInt($('sd-modal-prior-days').value || '0', 10) || 0);
    tile.limit = Math.max(1, parseInt($('sd-modal-limit').value || '200', 10) || 200);
    tile.width = clampTileWidth($('sd-modal-width')?.value || tile.width || state.current.settings.tile_width);
    tile.height = clampTileHeight($('sd-modal-height')?.value || tile.height || state.current.settings.tile_height);
    tile.result_template_id = $('sd-modal-column-template')?.value || '';
    tile.result_columns_json = '';
    state.savingTile = true;
    showModalError('');
    const prevLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Running…'; }
    try {
      await runTile(idx, { silent: true });
      renderTiles();
      await saveDashboard(false);
      closeTileEditor();
    } catch (err) {
      console.error('Run tile failed', err);
      showModalError(`Failed to run tile: ${err?.message || err}`);
    } finally {
      state.savingTile = false;
      if (btn) { btn.disabled = false; btn.textContent = prevLabel || 'Run now'; }
    }
  });
  $('sd-delete-tile')?.addEventListener('click', async () => {
    const idx = state.modalTileIndex;
    if (idx == null) return;
    if (!state.current?.tiles?.[idx]) return;
    if (!confirm(`Delete tile "${state.current.tiles[idx].title || `Tile ${idx + 1}`}"?`)) return;
    state.current.tiles.splice(idx, 1);
    renderTiles();
    try {
      await saveDashboard(false);
      closeTileEditor();
    } catch (err) {
      console.error('Delete tile failed to save', err);
      showModalError(`Tile removed locally, but saving failed: ${err?.message || err}. It will retry automatically.`);
      scheduleDashboardSave();
    }
  });
}


function applyTileSizeToAll({ note = true } = {}) {
  if (!state.current) return;
  const w = clampTileWidth($('sd-tile-width')?.value || state.current.settings.tile_width);
  const h = clampTileHeight($('sd-tile-height')?.value || state.current.settings.tile_height);
  state.current.settings.tile_width = w;
  state.current.settings.tile_height = h;
  state.current.settings.freeze_tile_size = true;
  if ($('sd-freeze-size')) $('sd-freeze-size').checked = true;
  for (const tile of (state.current.tiles || [])) {
    tile.width = w;
    tile.height = h;
  }
  renderTiles();
  updateSummary();
  if (note) showNote(`Applied fixed tile size ${w}x${h}. Save dashboard to keep it.`);
}

async function runButton(label, fn) {
  try {
    await fn();
  } catch (err) {
    console.error(label, err);
    showNote(`❌ ${label} failed: ${err.message || err}`);
  }
}

function bindTopEvents() {
  $('sd-dashboard-select')?.addEventListener('change', async e => {
    const id = e.target.value;
    setSelectedDashboardId(id);
    await loadDashboard(id);
  });
  $('sd-dashboard-name')?.addEventListener('change', () => { syncStateFromUi(); updateSummary(); scheduleDashboardSave(); });
  $('sd-watchlist')?.addEventListener('change', async () => { syncStateFromUi(); updateSummary(); renderTiles(); scheduleDashboardSave(); await refreshDashboard({ silent: true }).catch(() => null); });
  $('sd-timeframe')?.addEventListener('change', async () => { syncStateFromUi(); updateSummary(); restartAutoRefresh(); scheduleDashboardSave(); await refreshDashboard({ silent: true }).catch(() => null); });
  $('sd-auto-refresh')?.addEventListener('change', async () => { syncStateFromUi(); updateSummary(); restartAutoRefresh(); scheduleDashboardSave(); if (state.autoRefresh) await refreshDashboard({ silent: true, fetchFreshData: true }).catch(() => null); });
  $('sd-edit-layout')?.addEventListener('change', () => {
    syncStateFromUi();
    updateSummary();
    renderTiles();
    scheduleDashboardSave();
  });
  $('sd-save-dashboard')?.addEventListener('click', () => runButton('Save dashboard', async () => {
    syncStateFromUi();
    await saveDashboard(false);
  }));
  $('sd-new-dashboard')?.addEventListener('click', () => runButton('New dashboard', newDashboard));
  $('sd-copy-dashboard')?.addEventListener('click', () => runButton('Copy dashboard', copyDashboard));
  $('sd-add-tile')?.addEventListener('click', () => { addTile(); packTiles({ columns: state.current?.settings?.layout_columns || 0, resize: false }); renderTiles(); });
  $('sd-layout-pack')?.addEventListener('click', () => { if(state.current) state.current.settings.layout_columns = 0; packTiles({ columns: 0, resize: false }); renderTiles(); scheduleDashboardSave(); showNote('Tiles auto-packed and saved.'); });
  $('sd-layout-2x2')?.addEventListener('click', () => { applyTilePreset(2); scheduleDashboardSave(); });
  $('sd-layout-3x3')?.addEventListener('click', () => { applyTilePreset(3); scheduleDashboardSave(); });
  $('sd-apply-size')?.addEventListener('click', () => { applyTileSizeToAll(); scheduleDashboardSave(); });
  $('sd-freeze-size')?.addEventListener('change', () => { syncStateFromUi(); renderTiles(); updateSummary(); scheduleDashboardSave(); showNote(state.current?.settings?.freeze_tile_size ? 'Tile size frozen and saved.' : 'Tile size is fluid and saved.'); });
  $('sd-tile-width')?.addEventListener('change', () => { if ($('sd-freeze-size')?.checked) applyTileSizeToAll({ note: false }); else syncStateFromUi(); scheduleDashboardSave(); });
  $('sd-tile-height')?.addEventListener('change', () => { if ($('sd-freeze-size')?.checked) applyTileSizeToAll({ note: false }); else syncStateFromUi(); scheduleDashboardSave(); });
  $('sd-refresh')?.addEventListener('click', () => runButton('Refresh dashboard', () => refreshDashboard({ silent: false, fetchFreshData: true })));
  $('sd-open-editor')?.addEventListener('click', () => {
    // open first tile or create one if none exist
    if (!state.current?.tiles?.length) {
      addTile();
    }
    openTileEditor(0);
  });
}

async function init() {
  await loadGlobalApiTimeout();
  bindTopEvents();
  bindModalEvents();
  await loadCatalog().catch(e => console.warn('catalog init failed', e));
  await loadColumnTemplates().catch(e => console.warn('column template init failed', e));
  await loadDashboards().catch(e => {
    console.error('dashboard init failed', e);
    state.current = normalizeDashboard(defaultDashboard(DEFAULT_TITLE));
    renderTiles();
    showNote('Using local starter dashboard after init error: ' + (e.message || e));
  });
  await loadWatchlists().catch(e => console.warn('watchlist init failed', e));
  renderTiles();
  restartAutoRefresh();
  updateSummary();
  if ($('sd-note') && !$('sd-note').textContent) showNote('Dashboard loaded. Add a tile or pick a saved dashboard.');
}

window.addEventListener('DOMContentLoaded', init);
