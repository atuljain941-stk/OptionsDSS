
const INDICATORS = [
  'close','open','high','low','volume',
  'rsi3','rsi14','ema5','ema9','ema20','ema50','ema200',
  'ema_rsi14_13','ema_rsi14_90','rsi_diff_90','macd','macd_signal','macd_hist',
  'relative_strength','leadership',
  'oi','pcr','oi_change','oi_change_pct','pcr_change','pcr_change_pct',
  'atrcompression','rangecompression','volumedryup'
];
const TIMEFRAMES = ['5m','15m','1h','2h','4h','1d','1w','1m'];
const OPERATORS = ['>','>=','<','<=','=','crosses above','crosses below'];
let watchlists = [];
let savedScanners = [];
let scannerCatalog = {saved_scanners: [], builtin_scanners: [], functions: [], function_meta: [], timeframes: [], indicators: []};
let functionCatalog = [];
let selectedScannerId = null;
let rules = [];
let autocompleteItems = [];
let autocompleteIndex = -1;
let rawCodeMode = false;

function esc(s){return String(s ?? '').replace(/[&<>\"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[m]));}
function tfLabel(tf){ return tf === '1m' ? '1m (monthly)' : tf; }
function allScannerEntries() {
  const saved = (scannerCatalog.saved_scanners || []).map(s => ({
    name: s.name,
    category: 'Saved',
    description: s.description || s.query_text || '',
    query_text: s.query_text || '',
  }));
  const builtins = (scannerCatalog.builtin_scanners || []).map(s => ({...s, query_text: ''}));
  const seen = new Set();
  return [...saved, ...builtins].filter(x => {
    const k = String(x.name || '').toLowerCase();
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
function currentQueryText() {
  return document.getElementById('queryText');
}
function isAdvancedScannerCode(text) {
  const t = String(text || '');
  return /\bscan\s*\(|\bexpand\s*\(|\blookback\s*\(|\bpriorDay\s*\(|\bResistance\s*\(|\bSupport\s*\(|\bTouchCount\s*\(|\bBreakoutStrength\s*\(|\bBreakoutAge\s*\(|\bDistanceFromResistance\s*\(|\bDistanceFromSupport\s*\(|\bATRCompression\s*\(|\bRangeCompression\s*\(|\bVolumeDryup\s*\(|\bResistanceStrength\s*\(|\bSupportStrength\s*\(|\bFailedBreakoutStrength\s*\(|\bBreakoutFailedStrength\s*\(|\bFailedBreakdownStrength\s*\(|\bVolumeAtLevel\s*\(|\bOIChange\s*\(|\bPCRChange\s*\(|\bEarningsDays\s*\(|\bEarningsScore\s*\(|\bRSIDiff\s*\(|\bIsATH\s*\(|\bIsATL\s*\(|\bATHDistance\s*\(|\bATLDistance\s*\(|\bFibResistance\s*\(|\bFibSupport\s*\(|\bBeta\s*\(/i.test(t);
}
function insertIntoQuery(text) {
  const ta = currentQueryText();
  const start = ta.selectionStart ?? ta.value.length;
  const end = ta.selectionEnd ?? ta.value.length;
  const before = ta.value.slice(0, start);
  const after = ta.value.slice(end);
  ta.value = before + text + after;
  const pos = before.length + text.length;
  ta.setSelectionRange(pos, pos);
  ta.focus();
  setStatus('Inserted ' + text);
  showAutocomplete();
}
function replaceScanFragment(text) {
  const ta = currentQueryText();
  const cursor = ta.selectionStart ?? ta.value.length;
  const before = ta.value.slice(0, cursor);
  const after = ta.value.slice(cursor);
  const m = before.match(/scan\(\s*([A-Za-z0-9_\- ]*)$/i);
  if (m) {
    const start = before.length - m[0].length;
    ta.value = ta.value.slice(0, start) + text + after;
    const pos = start + text.length;
    ta.setSelectionRange(pos, pos);
  } else {
    insertIntoQuery(text);
    return;
  }
  ta.focus();
  setStatus('Inserted scanner code block');
  showAutocomplete();
}
function insertScannerCall(name) {
  const ta = currentQueryText();
  const cursor = ta.selectionStart ?? ta.value.length;
  const before = ta.value.slice(0, cursor);
  const after = ta.value.slice(cursor);
  const m = before.match(/scan\(\s*([A-Za-z0-9_\- ]*)$/i);
  const call = `scan(${name})`;
  if (m) {
    const start = before.length - m[0].length;
    ta.value = ta.value.slice(0, start) + call + after;
    const pos = start + call.length;
    ta.setSelectionRange(pos, pos);
  } else {
    insertIntoQuery(call);
    return;
  }
  ta.focus();
  setStatus('Inserted ' + call);
  showAutocomplete();
}
function scannerSnippet(x) {
  const raw = (x.query_text || '').trim();
  return raw || `scan(${x.name})`;
}
function functionSnippet(x) {
  const sig = String(x.signature || x.name || '').trim();
  return sig || `${x.name || ''}()`;
}
function allFunctionEntries() {
  const seen = new Set();
  return (functionCatalog || []).map(x => ({
    name: x.name,
    signature: x.signature || `${x.name}()`,
    description: x.description || '',
    category: 'Function',
  })).filter(x => {
    const k = String(x.name || '').toLowerCase();
    if (!k || seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}
function updateFunctionHint(nameOrSig) {
  const hint = document.getElementById('funcHint');
  if (!hint) return;
  const key = String(nameOrSig || '').trim().toLowerCase().replace(/\(.*/, '');
  const meta = allFunctionEntries().find(x => String(x.name || '').toLowerCase() === key);
  if (!meta) { hint.textContent = ''; return; }
  const ex = meta.signature || `${meta.name}()`;
  hint.innerHTML = `<b>${esc(meta.name)}</b> <span class="mini">${esc(meta.description || '')}</span><br><span class="mini">${esc(ex)}</span>`;
}
function insertFunctionCall(name) {
  const ta = currentQueryText();
  const call = `${name}()`;
  const start = ta.selectionStart ?? ta.value.length;
  const end = ta.selectionEnd ?? ta.value.length;
  ta.value = ta.value.slice(0, start) + call + ta.value.slice(end);
  const pos = start + name.length + 1;
  ta.setSelectionRange(pos, pos);
  ta.focus();
  setStatus('Inserted ' + call);
  updateFunctionHint(name);
  showAutocomplete();
}
function currentTokenBeforeCursor(text, cursor) {
  const before = text.slice(0, cursor);
  const scanMatch = before.match(/scan\(\s*([A-Za-z0-9_\- ]*)$/i);
  if (scanMatch) return { mode: 'scan', typed: (scanMatch[1] || '').trim().toLowerCase() };
  const fnParen = before.match(/([A-Za-z_][A-Za-z0-9_]*)\($/);
  if (fnParen) return { mode: 'function', typed: fnParen[1].toLowerCase(), pending: fnParen[1] };
  const token = before.match(/([A-Za-z_][A-Za-z0-9_]*)$/);
  if (token) return { mode: 'function', typed: token[1].toLowerCase(), pending: token[1] };
  return { mode: 'none', typed: '' };
}
function showAutocomplete(forceAll = false) {
  const ta = currentQueryText();
  const panel = document.getElementById('scanAutocomplete');
  const hint = document.getElementById('funcHint');
  if (!ta || !panel) return;
  const token = currentTokenBeforeCursor(ta.value, ta.selectionStart ?? ta.value.length);
  const mode = token.mode;
  const typed = token.typed || '';
  if (mode !== 'scan' && mode !== 'function' && !forceAll) {
    panel.style.display = 'none';
    panel.innerHTML = '';
    if (hint) hint.textContent = '';
    autocompleteItems = [];
    autocompleteIndex = -1;
    return;
  }
  let entries = [];
  if (mode === 'scan' || forceAll) {
    entries = allScannerEntries().filter(x => !typed || String(x.name || '').toLowerCase().includes(typed)).slice(0, 24)
      .map(x => ({...x, kind: 'scanner'}));
  }
  if (mode === 'function' || (forceAll && !entries.length)) {
    const fnEntries = allFunctionEntries().filter(x => !typed || String(x.name || '').toLowerCase().startsWith(typed)).slice(0, 24)
      .map(x => ({...x, kind: 'function'}));
    entries = mode === 'function' ? fnEntries : entries.concat(fnEntries);
  }
  if (!entries.length) {
    panel.style.display = 'none';
    panel.innerHTML = '';
    if (hint) hint.textContent = '';
    autocompleteItems = [];
    autocompleteIndex = -1;
    return;
  }
  autocompleteItems = entries;
  autocompleteIndex = 0;
  panel.innerHTML = entries.map((x, idx) => {
    if (x.kind === 'function') {
      return `
    <div class="autocomplete-item ${idx === autocompleteIndex ? 'selected' : ''}" data-idx="${idx}">
      <div style="min-width:0">
        <b>${esc(x.name)}</b> <span class="mini">${esc(x.signature || '')}</span>
        <div class="mini">${esc(x.description || 'Function')}</div>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-shrink:0">
        <button class="btn secondary" data-action="insert-fn" data-name="${esc(x.name)}">fn()</button>
      </div>
    </div>`;
    }
    return `
    <div class="autocomplete-item ${idx === autocompleteIndex ? 'selected' : ''}" data-idx="${idx}">
      <div style="min-width:0">
        <b>${esc(x.name)}</b>
        <div class="mini">${esc(x.category || 'Scanner')}</div><pre class="scanner-code" style="margin-top:6px">${esc(scannerSnippet(x))}</pre>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-shrink:0">
        <button class="btn secondary" data-action="code" data-name="${esc(x.name)}">code</button>
        <button class="btn secondary" data-action="ref" data-name="${esc(x.name)}">scan()</button>
      </div>
    </div>`;
  }).join('');
  panel.style.display = 'block';
  if (hint) {
    const fn = entries.find(x => x.kind === 'function' && String(x.name || '').toLowerCase() === typed);
    if (mode === 'function' && fn) hint.innerHTML = `<b>${esc(fn.name)}</b> <span class="mini">${esc(fn.description || '')}</span><br><span class="mini">${esc(fn.signature || `${fn.name}()` )}</span>`;
    else if (mode === 'scan') hint.textContent = 'Enter a saved scanner name or pick one from the list.';
    else hint.textContent = '';
  }
  panel.querySelectorAll('.autocomplete-item').forEach(el => {
    el.addEventListener('mouseenter', () => {
      autocompleteIndex = Number(el.dataset.idx || 0);
      renderAutocompleteSelection();
    });
    el.addEventListener('mousedown', (e) => {
      const btn = e.target.closest('button[data-action]');
      if (btn) return;
      e.preventDefault();
      const item = autocompleteItems[Number(el.dataset.idx || 0)];
      if (!item) return;
      if (item.kind === 'function') insertFunctionCall(item.name);
      else replaceScanFragment(scannerSnippet(item));
      panel.style.display = 'none';
    });
  });
  panel.querySelectorAll('button[data-action]').forEach(btn => btn.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const item = autocompleteItems.find(x => String(x.name) === String(btn.dataset.name));
    if (!item) return;
    if (item.kind === 'function' || btn.dataset.action === 'insert-fn') insertFunctionCall(item.name);
    else if (btn.dataset.action === 'ref') insertScannerCall(item.name);
    else replaceScanFragment(scannerSnippet(item));
    panel.style.display = 'none';
  }));
}
function renderAutocompleteSelection() {
  const panel = document.getElementById('scanAutocomplete');
  if (!panel) return;
  panel.querySelectorAll('.autocomplete-item').forEach(el => {
    el.classList.toggle('selected', Number(el.dataset.idx || -1) === autocompleteIndex);
  });
}
function chooseAutocomplete(delta = 0) {
  if (!autocompleteItems.length) return false;
  autocompleteIndex = Math.max(0, Math.min(autocompleteItems.length - 1, autocompleteIndex + delta));
  renderAutocompleteSelection();
  return true;
}
function commitAutocomplete() {
  if (!autocompleteItems.length) return false;
  const item = autocompleteItems[Math.max(0, autocompleteIndex)] || autocompleteItems[0];
  if (!item) return false;
  replaceScanFragment(scannerSnippet(item));
  const panel = document.getElementById('scanAutocomplete');
  if (panel) panel.style.display = 'none';
  const hint = document.getElementById('funcHint');
  if (hint) hint.textContent = '';
  return true;
}

function ruleTemplate(r = {}) {
  return {
    connector: r.connector || 'AND',
    lhs: r.lhs || 'rsi14',
    lhs_tf: r.lhs_tf || '1d',
    op: r.op || '>',
    rhs_type: r.rhs_type || 'value',
    rhs: r.rhs || '70',
    rhs_tf: r.rhs_tf || '1d',
    rhs_indicator: r.rhs_indicator || 'rsi14',
  };
}

function loadExample() {
  rules = [
    ruleTemplate({lhs:'rsi14', lhs_tf:'1m', op:'>', rhs_type:'value', rhs:'70'}),
    ruleTemplate({connector:'AND', lhs:'rsi14', lhs_tf:'1d', op:'<', rhs_type:'value', rhs:'30'}),
  ];
  renderRules();
  syncQueryFromBuilder();
  setStatus('Loaded example: monthly strength + daily pullback');
}

function renderRules() {
  const el = document.getElementById('rules');
  if (!rules.length) rules = [ruleTemplate()];
  el.innerHTML = rules.map((r, i) => `
    <div class="rule" data-idx="${i}">
      <div class="field">
        <label>${i === 0 ? 'Connector' : 'Join'}</label>
        <select class="connector" ${i === 0 ? 'disabled' : ''}>
          <option value="AND" ${r.connector === 'AND' ? 'selected' : ''}>AND</option>
          <option value="OR" ${r.connector === 'OR' ? 'selected' : ''}>OR</option>
        </select>
      </div>
      <div class="field">
        <label>Left indicator</label>
        <select class="lhs">${INDICATORS.map(x => `<option value="${x}" ${r.lhs === x ? 'selected' : ''}>${x}</option>`).join('')}</select>
      </div>
      <div class="field">
        <label>Timeframe</label>
        <select class="lhs_tf">${TIMEFRAMES.map(x => `<option value="${x}" ${r.lhs_tf === x ? 'selected' : ''}>${tfLabel(x)}</option>`).join('')}</select>
      </div>
      <div class="field">
        <label>Operator</label>
        <select class="op">${OPERATORS.map(x => `<option value="${x}" ${r.op === x ? 'selected' : ''}>${x}</option>`).join('')}</select>
      </div>
      <div class="field rhsTypeWrap">
        <label>Right side</label>
        <select class="rhs_type">
          <option value="value" ${r.rhs_type === 'value' ? 'selected' : ''}>Value</option>
          <option value="indicator" ${r.rhs_type === 'indicator' ? 'selected' : ''}>Indicator</option>
        </select>
      </div>
      <div class="field rhsValueWrap ${r.rhs_type === 'indicator' ? 'hidden' : ''}">
        <label>Value</label>
        <input class="rhs_value" type="number" step="0.01" value="${esc(r.rhs)}" />
      </div>
      <div class="field rhsIndicatorWrap ${r.rhs_type === 'value' ? 'hidden' : ''}">
        <label>Indicator / TF</label>
        <div style="display:flex;gap:6px">
          <select class="rhs_indicator">${INDICATORS.map(x => `<option value="${x}" ${r.rhs_indicator === x ? 'selected' : ''}>${x}</option>`).join('')}</select>
          <select class="rhs_tf">${TIMEFRAMES.map(x => `<option value="${x}" ${r.rhs_tf === x ? 'selected' : ''}>${tfLabel(x)}</option>`).join('')}</select>
        </div>
      </div>
      <div class="field">
        <label>Actions</label>
        <div style="display:flex;gap:6px">
          <button class="btn secondary" data-act="add">+</button>
          <button class="btn danger" data-act="del">x</button>
        </div>
      </div>
    </div>
  `).join('');

  el.querySelectorAll('.rule').forEach(row => {
    const idx = Number(row.dataset.idx);
    const set = (field, value) => {
      rules[idx][field] = value;
      syncQueryFromBuilder();
      renderRules();
    };
    row.querySelector('.connector')?.addEventListener('change', e => set('connector', e.target.value));
    row.querySelector('.lhs')?.addEventListener('change', e => set('lhs', e.target.value));
    row.querySelector('.lhs_tf')?.addEventListener('change', e => set('lhs_tf', e.target.value));
    row.querySelector('.op')?.addEventListener('change', e => set('op', e.target.value));
    row.querySelector('.rhs_type')?.addEventListener('change', e => {
      rules[idx].rhs_type = e.target.value;
      if (e.target.value === 'value' && rules[idx].rhs == null) rules[idx].rhs = '70';
      if (e.target.value === 'indicator' && !rules[idx].rhs_indicator) rules[idx].rhs_indicator = 'rsi14';
      syncQueryFromBuilder();
      renderRules();
    });
    row.querySelector('.rhs_value')?.addEventListener('input', e => set('rhs', e.target.value));
    row.querySelector('.rhs_indicator')?.addEventListener('change', e => set('rhs_indicator', e.target.value));
    row.querySelector('.rhs_tf')?.addEventListener('change', e => set('rhs_tf', e.target.value));
    row.querySelector('[data-act="add"]')?.addEventListener('click', () => {
      rules.splice(idx + 1, 0, ruleTemplate({connector:'AND'}));
      syncQueryFromBuilder();
      renderRules();
    });
    row.querySelector('[data-act="del"]')?.addEventListener('click', () => {
      if (rules.length === 1) return;
      rules.splice(idx, 1);
      if (rules[0]) rules[0].connector = 'AND';
      syncQueryFromBuilder();
      renderRules();
    });
  });
}

function syncQueryFromBuilder() {
  const lines = rules.map((r, i) => {
    const lhs = `${r.lhs}[${r.lhs_tf}]`;
    const rhs = r.rhs_type === 'indicator'
      ? `${r.rhs_indicator}[${r.rhs_tf}]`
      : String(r.rhs);
    const prefix = i === 0 ? '' : `${r.connector || 'AND'} `;
    return `${prefix}${lhs} ${r.op} ${rhs}`;
  });
  document.getElementById('queryText').value = lines.join('\n');
}

function loadBuilderFromText() {
  const text = document.getElementById('queryText').value.trim();
  if (!text) return;
  if (isAdvancedScannerCode(text)) {
    setStatus('Advanced scanner code kept in raw query mode');
    return;
  }
  const tokens = text.replace(/[\n;]+/g, ' ').split(/\b(AND|OR)\b/i).map(x => x.trim()).filter(Boolean);
  const parsed = [];
  let pendingConnector = 'AND';

  const parseTerm = (t) => {
    const term = t.trim();
    const br = term.match(/^(.+)\[(.+)\]$/);
    if (br) return {name: br[1].trim().toLowerCase(), tf: br[2].trim().toLowerCase()};
    if (/^-?\d+(?:\.\d+)?$/.test(term)) return {value: term, type: 'value'};
    const pref = term.match(/^(5m|15m|1h|2h|4h|1d|1w|1m|1mo|daily|weekly|monthly|5min|15min)\s+(.+)$/i);
    if (pref) return {name: pref[2].trim().toLowerCase(), tf: pref[1].trim().toLowerCase()};
    return {name: term.toLowerCase(), tf: '1d'};
  };

  for (const token of tokens) {
    const upper = token.toUpperCase();
    if (upper === 'AND' || upper === 'OR') {
      pendingConnector = upper;
      continue;
    }
    const body = token.trim();
    const opMatch = body.match(/(crosses above|crossed above|cross above|crosses below|crossed below|cross below|>=|<=|>|<|=|equals)/i);
    if (!opMatch) throw new Error(`Could not parse: ${body}`);
    const op = opMatch[1].toLowerCase().replace('equals', '=');
    const lhs = body.slice(0, opMatch.index).trim();
    const rhs = body.slice(opMatch.index + opMatch[1].length).trim();
    const l = parseTerm(lhs);
    const r = parseTerm(rhs);
    parsed.push(ruleTemplate({
      connector: parsed.length ? pendingConnector : 'AND',
      lhs: l.name || 'rsi14',
      lhs_tf: l.tf || '1d',
      op: op === '=' ? '=' : op,
      rhs_type: r.type === 'value' ? 'value' : 'indicator',
      rhs: r.value || '70',
      rhs_indicator: r.name || 'rsi14',
      rhs_tf: r.tf || '1d',
    }));
    pendingConnector = 'AND';
  }
  rules = parsed.length ? parsed : [ruleTemplate()];
  renderRules();
  syncQueryFromBuilder();
  setStatus('Builder synced from text');
}
function parseWatchlistSymbolCount() {
  const s = document.getElementById('watchlistSel');
  return s?.value || '';
}

function getSelectedWatchlistId() {
  const v = document.getElementById('watchlistSel').value;
  return v ? Number(v) : null;
}

function getPayload(definitionId = null) {
  return {
    query_text: document.getElementById('queryText').value.trim(),
    watchlist_id: getSelectedWatchlistId(),
    benchmark: document.getElementById('benchmarkSel').value,
    limit: Number(document.getElementById('limitSel').value || 200),
    definition_id: definitionId,
  };
}

function setStatus(msg) {
  document.getElementById('status').textContent = msg || '';
}

function setSummary(text) {
  const el = document.getElementById('scanSummary');
  el.textContent = text || '';
}

function renderResults(d) {
  const body = document.getElementById('resultsBody');
  const summary = document.getElementById('resultsSummary');
  const items = d.results || [];
  if (!items.length) {
    body.innerHTML = '<tr><td colspan="6" class="small">No matches.</td></tr>';
    summary.style.display = 'block';
    summary.innerHTML = 'No symbols matched the current query.';
    return;
  }
  body.innerHTML = items.map(r => `
    <tr>
      <td><b>${esc(r.symbol)}</b></td>
      <td>${r.price != null ? Number(r.price).toFixed(2) : '—'}</td>
      <td>${r.leadership != null ? r.leadership + '%' : '—'}</td>
      <td>${r.relative_strength != null ? Number(r.relative_strength).toFixed(1) : '—'}</td>
      <td class="small">
        RSI14 ${r.rsi14 != null ? Number(r.rsi14).toFixed(1) : '—'}<br>
        EMA(RSI14,90) ${r.ema_rsi14_90 != null ? Number(r.ema_rsi14_90).toFixed(2) : '—'} · RSIDiff90 ${r.rsi_diff_90 != null ? Number(r.rsi_diff_90).toFixed(2) : '—'}<br>
        EMA20 ${r.ema20 != null ? Number(r.ema20).toFixed(2) : '—'} · EMA50 ${r.ema50 != null ? Number(r.ema50).toFixed(2) : '—'}
      </td>
      <td class="small">${esc((r.reason || []).join(' | '))}</td>
    </tr>
  `).join('');
  summary.style.display = 'block';
  summary.innerHTML = `${items.length} match(es) across ${d.symbols ? d.symbols.length : 'selected'} symbols. Benchmark: <b>${esc(d.benchmark || 'SPY')}</b>.`;
}

async function loadWatchlists() {
  const sel = document.getElementById('watchlistSel');
  sel.innerHTML = '<option value="">Loading...</option>';
  const r = await fetch('/scanner-builder/api/watchlists');
  const d = await r.json();
  watchlists = d.watchlists || [];
  sel.innerHTML = '<option value="">All symbols table</option>' + watchlists.map(w => {
    const label = `${w.name} (${w.symbol_count || 0})`;
    return `<option value="${w.id}" ${w.is_default ? 'selected' : ''}>${esc(label)}</option>`;
  }).join('');

  const saved = localStorage.getItem('scanner_builder_watchlist_id') || '';
  const savedOk = saved && watchlists.some(w => String(w.id) === String(saved));
  let preferred = savedOk ? saved : '';
  if (!preferred && watchlists.length) {
    const defaultOne = watchlists.find(w => Number(w.is_default || 0) === 1 && Number(w.symbol_count || 0) > 0);
    const withSymbols = watchlists.find(w => Number(w.symbol_count || 0) > 0);
    preferred = String((defaultOne || withSymbols || watchlists[0]).id || '');
  }
  if (preferred) sel.value = String(preferred);
  localStorage.setItem('scanner_builder_watchlist_id', sel.value || '');
}

async function loadDefinitions() {
  const r = await fetch('/scanner-builder/api/definitions');
  const d = await r.json();
  savedScanners = d.definitions || [];
  scannerCatalog.saved_scanners = savedScanners;
  const body = document.getElementById('savedBody');
  if (!savedScanners.length) {
    body.innerHTML = '<tr><td colspan="7" class="small">No saved scanners yet.</td></tr>';
  } else {
    const wlMap = Object.fromEntries(watchlists.map(w => [String(w.id), w.name]));
    body.innerHTML = savedScanners.map(s => `
      <tr>
        <td><b>${esc(s.name)}</b><div class="mini">${esc(s.description || '')}</div></td>
        <td>${esc(wlMap[String(s.watchlist_id)] || 'All symbols')}</td>
        <td>${esc(s.benchmark || 'SPY')}</td>
        <td><input type="checkbox" data-alert-enabled="${s.id}" ${s.alert_enabled ? 'checked' : ''} /></td>
        <td>
          <select data-alert-mode="${s.id}" style="max-width:120px">
            <option value="enter_exit" ${((s.alert_mode || 'enter_exit') === 'enter_exit') ? 'selected' : ''}>enter_exit</option>
            <option value="enter" ${((s.alert_mode || '') === 'enter') ? 'selected' : ''}>enter</option>
            <option value="exit" ${((s.alert_mode || '') === 'exit') ? 'selected' : ''}>exit</option>
          </select>
        </td>
        <td>${esc(s.last_run_at || '—')}<div class="mini">${s.last_run_count || 0} matches</div></td>
        <td style="white-space:nowrap">
          <button class="btn secondary" data-load="${s.id}">Load</button>
          <button class="btn secondary" data-run="${s.id}">Run</button>
          <button class="btn secondary" data-del="${s.id}">Delete</button>
        </td>
      </tr>
    `).join('');
    body.querySelectorAll('[data-load]').forEach(btn => btn.addEventListener('click', () => loadScanner(Number(btn.dataset.load))));
    body.querySelectorAll('[data-run]').forEach(btn => btn.addEventListener('click', () => runScanner(Number(btn.dataset.run))));
    body.querySelectorAll('[data-del]').forEach(btn => btn.addEventListener('click', () => deleteScanner(Number(btn.dataset.del))));
    body.querySelectorAll('[data-alert-enabled]').forEach(el => el.addEventListener('change', () => updateScannerAlert(Number(el.dataset.alertEnabled))));
    body.querySelectorAll('[data-alert-mode]').forEach(el => el.addEventListener('change', () => updateScannerAlert(Number(el.dataset.alertMode))));
  }
  renderCatalog();
  showAutocomplete();
}

function renderCatalog() {
  const box = document.getElementById('scannerCatalog');
  if (!box) return;
  const entries = allScannerEntries();
  const sections = [];
  const groups = {};
  for (const x of entries) {
    const key = x.category || 'Other';
    (groups[key] ||= []).push(x);
  }
  for (const [cat, items] of Object.entries(groups)) {
    sections.push(`<div><div class="mini" style="margin:6px 0 4px;font-weight:700;color:var(--text)">${esc(cat)}</div>${items.map(x => `
      <div class="catalog-item">
        <div style="min-width:0">
          <b>${esc(x.name)}</b>
          <div class="mini">${esc(x.description || '')}</div>
          <pre class="scanner-code">${esc(scannerSnippet(x))}</pre>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0">
          <button class="btn secondary" data-catalog-code="${esc(x.name)}">code</button>
          <button class="btn secondary" data-catalog-ref="${esc(x.name)}">scan()</button>
        </div>
      </div>`).join('')}</div>`);
  }
  box.innerHTML = sections.join('') || '<div class="small">No scanners found.</div>';
  box.querySelectorAll('[data-catalog-code]').forEach(btn => btn.addEventListener('click', () => {
    const item = allScannerEntries().find(x => String(x.name) === String(btn.dataset.catalogCode));
    if (!item) return;
    replaceScanFragment(scannerSnippet(item));
  }));
  box.querySelectorAll('[data-catalog-ref]').forEach(btn => btn.addEventListener('click', () => insertScannerCall(btn.dataset.catalogRef)));
}

function selectedDefinition() {
  return savedScanners.find(x => Number(x.id) === Number(selectedScannerId));
}

async function loadScanner(id) {
  const s = savedScanners.find(x => Number(x.id) === Number(id));
  if (!s) return;
  selectedScannerId = Number(id);
  document.getElementById('scannerName').value = s.name || '';
  document.getElementById('scannerDesc').value = s.description || '';
  document.getElementById('queryText').value = s.query_text || '';
  document.getElementById('benchmarkSel').value = s.benchmark || 'SPY';
  if (s.watchlist_id) document.getElementById('watchlistSel').value = String(s.watchlist_id);
  try {
    const rows = JSON.parse(s.builder_json || '[]');
    rules = Array.isArray(rows) && rows.length ? rows.map(ruleTemplate) : [ruleTemplate()];
    renderRules();
  } catch (e) {
    rules = [ruleTemplate()];
    renderRules();
  }
  setStatus(`Loaded scanner: ${s.name}`);
  const hint = document.getElementById('funcHint');
  if (hint) hint.textContent = '';
}

async function updateScannerAlert(defId) {
  const enabled = !!document.querySelector(`[data-alert-enabled="${defId}"]`)?.checked;
  const modeEl = document.querySelector(`[data-alert-mode="${defId}"]`);
  const alert_mode = modeEl ? modeEl.value : 'enter_exit';
  const r = await fetch(`/scanner-builder/api/definitions/${defId}`, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ alert_enabled: enabled, alert_mode })
  });
  const d = await r.json();
  if (!r.ok || d.error) throw new Error(d.error || r.statusText);
  setStatus(`Scanner alert ${enabled ? 'enabled' : 'disabled'} (${alert_mode})`);
  await loadDefinitions();
}

async function saveScanner(updateExisting) {
  const query = document.getElementById('queryText').value.trim();
  if (!query) { alert('Enter a query first.'); return; }
  const payload = {
    name: document.getElementById('scannerName').value.trim(),
    description: document.getElementById('scannerDesc').value.trim(),
    query_text: query,
    builder_json: rules,
    watchlist_id: getSelectedWatchlistId(),
    benchmark: document.getElementById('benchmarkSel').value,
  };
  if (!payload.name) {
    payload.name = prompt('Scanner name', 'My Scanner');
    if (!payload.name) return;
  }
  let url = '/scanner-builder/api/definitions';
  let method = 'POST';
  if (updateExisting && selectedScannerId) {
    url = `/scanner-builder/api/definitions/${selectedScannerId}`;
    method = 'PUT';
  }
  const r = await fetch(url, {
    method,
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  const d = await r.json();
  if (!r.ok || d.error) throw new Error(d.error || r.statusText);
  selectedScannerId = d.definition ? d.definition.id : selectedScannerId;
  setStatus(updateExisting ? 'Scanner updated' : 'Scanner saved');
  await loadDefinitions();
}

async function deleteScanner(id) {
  const s = savedScanners.find(x => Number(x.id) === Number(id));
  if (!s) return;
  if (!confirm(`Delete scanner '${s.name}'?`)) return;
  const r = await fetch(`/scanner-builder/api/definitions/${id}`, { method:'DELETE' });
  const d = await r.json();
  if (!r.ok || d.error) throw new Error(d.error || r.statusText);
  if (selectedScannerId === id) selectedScannerId = null;
  await loadDefinitions();
  setStatus('Scanner deleted');
}

async function runScanner(defId = null) {
  const payload = defId ? { ...getPayload(defId), definition_id: defId } : getPayload();
  if (!payload.query_text) { alert('Enter a query first.'); return; }
  setStatus('Scanning...');
  setSummary('');
  const r = await fetch('/scanner-builder/api/run', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  const d = await r.json();
  if (!r.ok || d.error) {
    setStatus('Error');
    document.getElementById('resultsBody').innerHTML = `<tr><td colspan="6" class="small">${esc(d.error || r.statusText)}</td></tr>`;
    return;
  }
  setStatus(`Done · ${d.count} match(es)`);
  renderResults(d);
  await loadDefinitions();
}

async function validateQuery() {
  const payload = getPayload();
  if (!payload.query_text) return alert('Enter a query first.');
  const r = await fetch('/scanner-builder/api/run', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ ...payload, limit: 1 }),
  });
  const d = await r.json();
  if (!r.ok) {
    alert(d.error || r.statusText);
    return;
  }
  setStatus(`Valid query · ${d.clauses.length} clause(s)`);
}

function copyQuery() {
  const txt = document.getElementById('queryText').value;
  navigator.clipboard.writeText(txt).then(() => setStatus('Query copied'));
}

function clearPage() {
  rules = [ruleTemplate()];
  selectedScannerId = null;
  document.getElementById('scannerName').value = '';
  document.getElementById('scannerDesc').value = '';
  document.getElementById('queryText').value = '';
  document.getElementById('resultsBody').innerHTML = '<tr><td colspan="6" class="small">Run a scan to see results.</td></tr>';
  renderRules();
  setStatus('Cleared');
}

async function loadCatalog() {
  const r = await fetch('/scanner-builder/api/catalog');
  const d = await r.json();
  scannerCatalog = d || scannerCatalog;
  functionCatalog = scannerCatalog.function_meta || (scannerCatalog.functions || []).map(s => ({name: String(s).split('(')[0], signature: s, description: 'Built-in function'}));
  renderCatalog();
  showAutocomplete();
}

async function init() {
  rules = [ruleTemplate()];
  renderRules();
  await loadWatchlists();
  await loadDefinitions();
  await loadCatalog();
  document.getElementById('rules').addEventListener('change', () => syncQueryFromBuilder());
  document.getElementById('watchlistSel').addEventListener('change', (e) => {
    localStorage.setItem('scanner_builder_watchlist_id', e.target.value || '');
  });
  const q = document.getElementById('queryText');
  q.addEventListener('input', () => {
    setStatus('Text updated');
    showAutocomplete();
  });
  q.addEventListener('keyup', () => { showAutocomplete(); });
  q.addEventListener('click', () => { showAutocomplete(); });
  q.addEventListener('keydown', (e) => {
    const panel = document.getElementById('scanAutocomplete');
    const visible = panel && panel.style.display === 'block';
    if (e.shiftKey && e.key === 'Enter') {
      e.preventDefault();
      showAutocomplete(true);
      return;
    }
    if (visible && e.key === 'ArrowDown') { e.preventDefault(); chooseAutocomplete(1); return; }
    if (visible && e.key === 'ArrowUp') { e.preventDefault(); chooseAutocomplete(-1); return; }
    if (visible && e.key === 'Enter') { e.preventDefault(); commitAutocomplete(); return; }
    if (visible && e.key === 'Escape') { e.preventDefault(); panel.style.display = 'none'; return; }
  });
  q.addEventListener('blur', () => setTimeout(() => {
    const panel = document.getElementById('scanAutocomplete');
    if (panel) panel.style.display = 'none';
  }, 150));
  document.getElementById('addRuleBtn').addEventListener('click', () => {
    rules.push(ruleTemplate({connector:'AND'}));
    renderRules();
    syncQueryFromBuilder();
  });
  document.getElementById('addExampleBtn').addEventListener('click', loadExample);
  document.getElementById('syncFromTextBtn').addEventListener('click', () => {
    try { loadBuilderFromText(); }
    catch (e) { alert(e.message); }
  });
  document.getElementById('copyQueryBtn').addEventListener('click', copyQuery);
  document.getElementById('validateBtn').addEventListener('click', validateQuery);
  document.getElementById('openScannerPickerBtn').addEventListener('click', () => { const q = currentQueryText(); if (q) { q.focus(); showAutocomplete(true); } });
  document.getElementById('runBtn').addEventListener('click', () => runScanner());
  document.getElementById('saveBtn').addEventListener('click', () => saveScanner(false).catch(e => alert(e.message)));
  document.getElementById('updateBtn').addEventListener('click', () => saveScanner(true).catch(e => alert(e.message)));
  document.getElementById('clearBtn').addEventListener('click', clearPage);
  document.getElementById('refreshSavedBtn').addEventListener('click', () => loadDefinitions());
  document.getElementById('refreshCatalogBtn')?.addEventListener('click', () => loadCatalog());
  document.getElementById('logic-chips').innerHTML = [
    ['Text-first', 'g'], ['Saved scanners', 'y'], ['Watchlist execution', 'g'], ['AND / OR', 'y'], ['Crossovers', 'r']
  ].map(([t,c]) => `<span class="chip ${c}">${t}</span>`).join('');
  loadExample();
}

init().catch(e => {
  console.error(e);
  setStatus(e.message || 'Failed to initialize');
});
