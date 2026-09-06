const INDICATORS = [
  'close','open','high','low','volume',
  'rsi3','rsi14','ema5','ema9','ema20','ema50','ema200',
  'ema_rsi14_13','ema_rsi14_90','rsi_diff_90','macd','macd_signal','macd_hist',
  'relative_strength','leadership','mansfield_rs','rs_rank',
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
const DEFAULT_QUERY_TEXT = 'rsi14[1d] > 50 AND rsi14[1w] > 50';

function looksCorruptedQuery(text) {
  const t = String(text || '');
  return t.length > 300 && (
    /\bconst\s+INDICATORS\b/.test(t) ||
    /\bfunction\s+[A-Za-z_$][\w$]*\s*\(/.test(t) ||
    /\bdocument\.getElementById\(\'queryText\'\)/.test(t) ||
    /\bscanner_builder\.js\b/.test(t) ||
    /\b@scanner_builder_bp\b/.test(t) ||
    /\bloadExample\s*\(/.test(t)
  );
}

function resetQueryText(reason) {
  const ta = currentQueryText();
  if (!ta) return;
  ta.value = DEFAULT_QUERY_TEXT;
  if (reason) setStatus(reason);
}

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
  return /\bscan\s*\(|\bexpand\s*\(|\blookback\s*\(|\bpriorDay\s*\(|\bShift\s*\(|\bSMA\s*\(|\bEMA\s*\(|\bAverage\s*\(|\bHighest\s*\(|\bLowest\s*\(|\bStdDev\s*\(|\bChangePct\s*\(|\bAbs\s*\(|\bBetween\s*\(|\bNotBetween\s*\(|\bRegSlopeATRDeg\s*\(|\bRegSlopeATR\s*\(|\bRegSlopePct\s*\(|\bRegSlopeDeg\s*\(|\bSlopeDegPerBar\s*\(|\bSlopePctPerBar\s*\(|\bSlopeDegRaw\s*\(|\bSlopeATRDeg\s*\(|\bSlopeATR\s*\(|\bSlopePct\s*\(|\bSlopeDeg\s*\(|\bSlope\s*\(|\bCrossAbove\s*\(|\bCrossBelow\s*\(|\bLastSwingHigh\s*\(|\bLastSwingHighClose\s*\(|\bLastSwingLow\s*\(|\bLastSwingLowClose\s*\(|\bDaysSinceSwingHigh\s*\(|\bDaysSinceSwingLow\s*\(|\bPullbackFromSwingHighPct\s*\(|\bPullbackFromSwingHighATR\s*\(|\bBounceFromSwingLowPct\s*\(|\bBounceFromSwingLowATR\s*\(|\bMin\s*\(|\bMax\s*\(|\bRetest\s*\(|\bResistance\s*\(|\bSupport\s*\(|\bTouchCount\s*\(|\bBreakoutStrength\s*\(|\bBreakoutAge\s*\(|\bDistanceFromResistance\s*\(|\bDistanceFromSupport\s*\(|\bATRCompression\s*\(|\bRangeCompression\s*\(|\bVolumeDryup\s*\(|\bResistanceStrength\s*\(|\bSupportStrength\s*\(|\bFailedBreakoutStrength\s*\(|\bBreakoutFailedStrength\s*\(|\bFailedBreakdownStrength\s*\(|\bVolumeAtLevel\s*\(|\bOIChange\s*\(|\bPCRChange\s*\(|\bEarningsDays\s*\(|\bEarningsScore\s*\(|\bRSIDiff\s*\(|\bSectorRS\s*\(|\bSector\s*\(|\bSectorName\s*\(|\bSectorETF\s*\(|\bRelativeStrength\s*\(|\bMansfieldRS\s*\(|\bRSRank\s*\(|\bIVRank\s*\(|\bIVChange\s*\(|\bFlowScore\s*\(|\bFlowBias\s*\(|\bPCRShift\s*\(|\bIsATH\s*\(|\bIsATL\s*\(|\bATHDistance\s*\(|\bATLDistance\s*\(|\bFibResistance\s*\(|\bFibSupport\s*\(|\bBeta\s*\(/i.test(t);
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
function currentAutocompleteRange(ta) {
  const cursor = ta.selectionStart ?? ta.value.length;
  const end = ta.selectionEnd ?? cursor;
  if (end !== cursor) return { mode: 'selection', start: cursor, end };
  const before = ta.value.slice(0, cursor);
  const scanMatch = before.match(/scan\(\s*([A-Za-z0-9_\- ]*)$/i);
  if (scanMatch) {
    return { mode: 'scan', start: before.length - scanMatch[0].length, end: cursor };
  }
  const fnParen = before.match(/([A-Za-z_][A-Za-z0-9_]*)\($/);
  if (fnParen) {
    return { mode: 'function', start: before.length - fnParen[0].length, end: cursor };
  }
  const token = before.match(/([A-Za-z_][A-Za-z0-9_]*)$/);
  if (token) {
    return { mode: 'function', start: before.length - token[0].length, end: cursor };
  }
  return { mode: 'insert', start: cursor, end: cursor };
}
function replaceAutocompleteFragment(text, statusMessage = 'Inserted', cursorOffset = null) {
  const ta = currentQueryText();
  const range = currentAutocompleteRange(ta);
  ta.value = ta.value.slice(0, range.start) + text + ta.value.slice(range.end);
  const pos = range.start + (cursorOffset == null ? text.length : cursorOffset);
  ta.setSelectionRange(pos, pos);
  ta.focus();
  setStatus(statusMessage);
  showAutocomplete();
}
function replaceScanFragment(text) {
  replaceAutocompleteFragment(text, 'Inserted scanner code block');
}
function insertScannerCall(name) {
  const call = `scan(${name})`;
  replaceAutocompleteFragment(call, 'Inserted ' + call);
}

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

async function fetchJsonOrText(url, options = {}) {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs || _globalApiTimeoutMs;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const { timeoutMs: _drop, ...fetchOptions } = options;
  let resp;
  try {
    resp = await fetch(url, { ...fetchOptions, signal: controller.signal });
  } catch (e) {
    clearTimeout(timer);
    if (e && e.name === 'AbortError') {
      return { resp: null, data: { error: `Request timed out after ${Math.round(timeoutMs/1000)}s — you can raise this in Settings.` } };
    }
    return { resp: null, data: { error: e.message || String(e) } };
  }
  clearTimeout(timer);
  const text = await resp.text();
  let data = {};
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (e) {
      data = { error: text.trim() || resp.statusText || 'Request failed', raw: text };
    }
  }
  if (!resp.ok && !data.error) {
    data.error = resp.statusText || 'Request failed';
  }
  return { resp, data };
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
  const call = `${name}()`;
  replaceAutocompleteFragment(call, 'Inserted ' + call, name.length + 1);
  updateFunctionHint(name);
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
  if (item.kind === 'function') insertFunctionCall(item.name);
  else replaceScanFragment(scannerSnippet(item));
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
    ruleTemplate({lhs:'rsi14', lhs_tf:'1d', op:'>', rhs_type:'value', rhs:'50'}),
    ruleTemplate({connector:'AND', lhs:'rsi14', lhs_tf:'1w', op:'>', rhs_type:'value', rhs:'50'}),
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


// ─── Trade-scored results renderer ───────────────────────────────────────
let _tradeScores = {};
let _scanQueryText = '';
let _scanClauses = [];
let _scanBias = 'neutral';

function _scoreRingSmall(score, size) {
  size = size || 38;
  const r = size/2 - 4;
  const circ = 2*Math.PI*r;
  const dash = (circ*score/100).toFixed(1);
  const gap  = (circ*(1-score/100)).toFixed(1);
  const c = score>=80?'#22c55e':score>=65?'#4ade80':score>=50?'#f59e0b':score>=35?'#f97316':'#ef4444';
  return '<svg width="'+size+'" height="'+size+'" viewBox="0 0 '+size+' '+size+'" style="flex-shrink:0">' +
    '<circle cx="'+size/2+'" cy="'+size/2+'" r="'+r+'" fill="none" stroke="#1e293b" stroke-width="3.5"/>' +
    '<circle cx="'+size/2+'" cy="'+size/2+'" r="'+r+'" fill="none" stroke="'+c+'" stroke-width="3.5"' +
    ' stroke-dasharray="'+dash+' '+gap+'" stroke-linecap="round" transform="rotate(-90 '+size/2+' '+size/2+')"/>' +
    '<text x="'+size/2+'" y="'+(size/2+4)+'" text-anchor="middle" font-size="10" font-weight="800" fill="'+c+'">'+score+'</text>' +
    '</svg>';
}

function _dualBar(ms, ts) {
  const mc = ms>=75?'#6366f1':ms>=55?'#a78bfa':ms>=40?'#f59e0b':'#ef4444';
  const tc = ts>=80?'#22c55e':ts>=65?'#4ade80':ts>=50?'#f59e0b':ts>=35?'#f97316':'#ef4444';
  return '<div style="font-size:9px;display:flex;flex-direction:column;gap:2px;min-width:90px">' +
    '<div style="display:flex;align-items:center;gap:4px">' +
      '<span style="color:#64748b;width:42px">Match</span>' +
      '<div style="flex:1;height:5px;background:#1e293b;border-radius:3px;overflow:hidden">' +
        '<div style="width:'+ms+'%;height:100%;background:'+mc+';border-radius:3px"></div></div>' +
      '<span style="color:'+mc+';font-weight:700;min-width:22px;text-align:right">'+ms+'</span></div>' +
    '<div style="display:flex;align-items:center;gap:4px">' +
      '<span style="color:#64748b;width:42px">Trade</span>' +
      '<div style="flex:1;height:5px;background:#1e293b;border-radius:3px;overflow:hidden">' +
        '<div style="width:'+ts+'%;height:100%;background:'+tc+';border-radius:3px"></div></div>' +
      '<span style="color:'+tc+';font-weight:700;min-width:22px;text-align:right">'+ts+'</span></div>' +
    '</div>';
}

function _tradeBadge(trec) {
  if (!trec) return '<span style="color:var(--muted);font-size:10px">—</span>';
  const tt = trec.trade_type || '';
  const tc = tt==='PS'?'#22c55e':tt==='CS'?'#ef4444':tt==='IC'?'#a78bfa':tt==='PB'?'#3b82f6':'#f59e0b';
  return '<span style="background:'+tc+'22;border:1px solid '+tc+'66;color:'+tc+';font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px">'+esc(tt)+' '+esc(trec.trade_name||'')+'</span>';
}

function _recBadge(rec, grade) {
  const c = rec==='OPEN'?'#22c55e':rec==='OPEN_SMALL'?'#f59e0b':'#ef4444';
  return '<span style="background:'+c+'22;border:1px solid '+c+'66;color:'+c+';font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px">'+esc(grade)+' · '+esc(rec)+'</span>';
}

function _renderSymCard(r, scored) {
  const sym = esc(r.symbol || '');
  const price = r.price != null ? Number(r.price).toFixed(2) : '—';
  const rsi = r.rsi14 != null ? Number(r.rsi14).toFixed(1) : '—';
  const rsiDiff = r.rsi_diff_90 != null ? Number(r.rsi_diff_90).toFixed(1) : '—';
  const rsiDiffN = r.rsi_diff_90 || 0;
  const rsiDiffC = rsiDiffN > 0 ? '#22c55e' : '#ef4444';
  const lead = r.leadership != null ? r.leadership+'%' : '—';
  const rsN = r.relative_strength || 0;
  const rs = r.relative_strength != null ? (rsN >= 0 ? '+' : '') + Number(r.relative_strength).toFixed(1) : '—';
  const rsC = rsN >= 0 ? '#22c55e' : '#ef4444';
  const iv = r.iv_rank != null ? Number(r.iv_rank).toFixed(0)+'%' : '—';
  const flowBias = r.flow_bias || 'NEUTRAL';
  const flowC = flowBias==='BULL'?'#22c55e':flowBias==='BEAR'?'#ef4444':'#64748b';
  const earnD = (r.earn_days && r.earn_days < 999) ? r.earn_days+'d' : 'safe';
  const earnC = r.earn_days < 14 ? '#ef4444' : r.earn_days < 21 ? '#f59e0b' : '#22c55e';
  const clauses = (r.reason || []).join(' | ');

  const ms    = scored ? scored.match_score : null;
  const ts    = scored ? scored.trade_score : null;
  const grade = scored ? (scored.grade || '?') : null;
  const rec   = scored ? (scored.recommendation || '—') : null;
  const trec  = scored ? scored.trade_rec : null;
  const pros  = scored ? (scored.pros || []) : [];
  const cons  = scored ? (scored.cons || []) : [];
  const mnotes= scored ? (scored.match_notes || []) : [];

  const scoreSection = (ms != null && ts != null)
    ? _dualBar(ms, ts) + '<div style="display:flex;flex-direction:column;gap:3px;margin-top:3px">' +
        (rec && grade ? _recBadge(rec, grade) : '') + (trec ? _tradeBadge(trec) : '') + '</div>'
    : '<div style="color:var(--muted);font-size:10px;cursor:pointer;padding:4px" onclick="event.stopPropagation();_scoreOne(\''+sym+'\',this)">📊 Score</div>';

  const trecDetail = trec ? (
    '<div style="background:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:8px 10px;margin-top:6px;font-size:10px">' +
    '<div style="font-weight:700;color:#94a3b8;margin-bottom:3px">RECOMMENDED TRADE</div>' +
    '<div style="font-weight:700;color:#e2e8f0;margin-bottom:2px">' + esc(trec.legs||'—') + '</div>' +
    '<div style="display:flex;gap:10px;flex-wrap:wrap;margin:4px 0;font-size:10px">' +
      (trec.est_credit!=null?'<span>Credit <b style="color:#22c55e">$'+trec.est_credit+'</b>/contract</span>':'') +
      (trec.est_debit!=null?'<span>Debit <b style="color:#f59e0b">$'+trec.est_debit+'</b>/contract</span>':'') +
      (trec.max_loss!=null?'<span>Max Loss <b style="color:#ef4444">$'+trec.max_loss+'/contract</b></span>':'') +
      (trec.rr?'<span>R:R <b>'+trec.rr+'</b></span>':'') +
      (trec.pop?'<span>POP <b>'+trec.pop+'%</b></span>':'') +
      (trec.pnr?'<span>PNR <b style="color:#f59e0b">$'+trec.pnr+'</b></span>':'') +
      '<span style="color:#64748b">~'+(trec.dte_hint||17)+' DTE</span></div>' +
    '<div style="color:#64748b;font-size:9px;font-style:italic">' + esc(trec.rationale_hint||'') + '</div>' +
    '<div style="color:#475569;font-size:9px;margin-top:3px">📋 ' + esc(trec.manage||'') + '</div></div>'
  ) : '';

  const prosConsHtml = (pros.length || cons.length) ?
    '<div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px;font-size:9px">' +
    '<div>' + pros.map(function(p){return '<div style="color:#22c55e;padding:1px 0">✓ '+esc(p)+'</div>';}).join('') +
             mnotes.slice(0,2).map(function(n){return '<div style="color:#6366f1;padding:1px 0">◉ '+esc(n)+'</div>';}).join('') + '</div>' +
    '<div>' + cons.map(function(c){return '<div style="color:#ef4444;padding:1px 0">✗ '+esc(c)+'</div>';}).join('') + '</div>' +
    '</div>' : '';

  return '<tr id="row-'+sym+'" style="border-bottom:1px solid var(--border);vertical-align:top;cursor:pointer" onclick="_toggleRowDetail(\''+sym+'\')">' +
    '<td style="padding:8px 6px;vertical-align:middle">' +
      '<div style="font-weight:800;font-size:14px;color:var(--accent)">'+sym+'</div>' +
      '<div style="font-size:10px;color:var(--muted)">$'+price+'</div>' +
      '<div style="font-size:9px;color:'+earnC+';margin-top:1px">Earn: '+earnD+'</div></td>' +
    '<td style="padding:8px 6px;vertical-align:middle">' + scoreSection + '</td>' +
    '<td style="padding:8px 6px;vertical-align:middle">' +
      '<div style="font-size:10px;display:flex;flex-direction:column;gap:2px">' +
        '<div>RSI <b>'+rsi+'</b> · diff <b style="color:'+rsiDiffC+'">'+(rsiDiffN>0?'+':'')+rsiDiff+'</b></div>' +
        '<div>RS <b style="color:'+rsC+'">'+rs+'</b> · Lead <b>'+lead+'</b></div>' +
        '<div>IVR <b>'+iv+'</b> · Flow <b style="color:'+flowC+'">'+flowBias+'</b></div></div></td>' +
    '<td style="padding:8px 6px;vertical-align:middle">' +
      '<div style="font-size:9px;color:var(--muted);max-width:200px;white-space:normal;line-height:1.4">'+esc(clauses)+'</div></td></tr>' +
    '<tr id="detail-row-'+sym+'" style="display:none;background:rgba(255,255,255,.015)">' +
      '<td colspan="4" style="padding:0 8px 10px 8px">' + trecDetail + prosConsHtml + '</td></tr>';
}

function _toggleRowDetail(sym) {
  const dr = document.getElementById('detail-row-'+sym);
  if (!dr) return;
  dr.style.display = dr.style.display === 'none' ? 'table-row' : 'none';
}

async function _scoreOne(sym, el) {
  if (el) { el.textContent = '⏳'; el.style.pointerEvents='none'; }
  try {
    const row = (window._lastScanResults||[]).find(function(r){return r.symbol===sym;});
    if (!row) return;
    const { resp, data: d } = await fetchJsonOrText('/scanner-builder/api/trade_score', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({row: row, query_text: _scanQueryText, clauses: _scanClauses})
    });
    if (!resp.ok) throw new Error(d.error || d.message || 'Trade score request failed');
    _tradeScores[sym] = d;
    const trs = document.querySelectorAll('#row-'+sym+', #detail-row-'+sym);
    trs.forEach(function(t){t.remove();});
    const tbody = document.getElementById('resultsBody');
    if (tbody) {
      const tmp = document.createElement('tbody');
      tmp.innerHTML = _renderSymCard(row, d);
      Array.from(tmp.children).forEach(function(c){tbody.appendChild(c);});
    }
  } catch(e) {
    if (el) { el.textContent = 'Err'; el.style.pointerEvents='auto'; }
  }
}

async function _scoreBatch(rows) {
  if (!rows || !rows.length) return;
  try {
    const { resp, data: d } = await fetchJsonOrText('/scanner-builder/api/trade_score', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({rows: rows, query_text: _scanQueryText, clauses: _scanClauses})
    });
    if (!resp.ok) throw new Error(d.error || d.message || 'Trade score request failed');
    _scanBias = d.bias || 'neutral';
    (d.results || []).forEach(function(s){ if(s.symbol) _tradeScores[s.symbol]=s; });
    const rows2 = window._lastScanResults || [];
    const body = document.getElementById('resultsBody');
    if (body) body.innerHTML = rows2.map(function(r){return _renderSymCard(r, _tradeScores[r.symbol]);}).join('');
    const scored = Object.values(_tradeScores).filter(function(s){return s.trade_score!=null;});
    const avgT = scored.length ? Math.round(scored.reduce(function(a,b){return a+(b.trade_score||0);},0)/scored.length) : null;
    const avgM = scored.length ? Math.round(scored.reduce(function(a,b){return a+(b.match_score||0);},0)/scored.length) : null;
    const gradeA = scored.filter(function(s){return s.grade==='A';}).length;
    const opens  = scored.filter(function(s){return s.recommendation==='OPEN';}).length;
    const el = document.getElementById('scoreSummaryChips');
    if (el) {
      el.innerHTML = [
        avgM!=null?'<span class="chip y">Avg Match '+avgM+'</span>':'',
        avgT!=null?'<span class="chip g">Avg Trade '+avgT+'</span>':'',
        gradeA?'<span class="chip g">Grade A: '+gradeA+'</span>':'',
        opens?'<span class="chip g">OPEN: '+opens+'</span>':'',
        '<span class="chip" style="background:rgba(99,102,241,.1);color:#a78bfa">Bias: '+_scanBias.toUpperCase()+'</span>',
      ].filter(Boolean).join('');
    }
  } catch(e) { console.error('batch score:', e); }
}

function renderResults(d) {
  const body = document.getElementById('resultsBody');
  const summary = document.getElementById('resultsSummary');
  const items = d.results || [];
  const market = d.summary || {};
  _scanQueryText = d.query_text || '';
  _scanClauses   = d.clauses || [];
  _tradeScores   = {};
  window._lastScanResults = items;

  if (!items.length) {
    const errCount = Number((d && d.error_count) != null ? d.error_count : ((d && Array.isArray(d.errors)) ? d.errors.length : 0));
    const loadedCount = Number((d && d.loaded_count) != null ? d.loaded_count : 0);
    const firstErr = d && Array.isArray(d.errors) && d.errors.length ? d.errors[0] : null;
    const errText = firstErr ? (' First error: ' + esc(firstErr.symbol || 'symbol') + ' - ' + esc(firstErr.error || firstErr)) : '';
    body.innerHTML = '<tr><td colspan="4" class="small">' + ((errCount && loadedCount === 0) ? 'No symbols could be evaluated due to data-load errors.' : 'No matches.') + errText + '</td></tr>';
    summary.style.display = 'block';
    summary.innerHTML = (errCount && loadedCount === 0) ? ('Scanner could not evaluate the selected watchlist. ' + errCount + ' symbol load error(s).' + errText) : 'No symbols matched the current query.';
    setSummary((errCount && loadedCount === 0) ? 'Data load error' : 'No matches');
    return;
  }

  body.innerHTML = items.map(function(r){return _renderSymCard(r, null);}).join('');

  summary.style.display = 'block';
  const marketView = market.market_view || 'Balanced';
  const bullets = [
    'Bull '+(market.bullish ?? 0),
    'Bear '+(market.bearish ?? 0),
    'Neutral '+(market.neutral ?? 0),
    'Avg Flow '+(market.avg_flow_score != null ? Number(market.avg_flow_score).toFixed(1) : '—'),
    'Avg IVR '+(market.avg_iv_rank != null ? Number(market.avg_iv_rank).toFixed(1) : '—'),
  ];
  summary.innerHTML = '<b>'+esc(marketView)+' market view</b> · '+bullets.map(function(x){return esc(x);}).join(' · ')+' · Benchmark: <b>'+esc(d.benchmark||'SPY')+'</b>.<div id="scoreSummaryChips" class="chips" style="margin-top:6px"></div>';
  setSummary(marketView+' · '+items.length+' matches');

  setTimeout(function(){ _scoreBatch(items.slice(0, 60)); }, 80);
}


async function loadWatchlists() {
  const sel = document.getElementById('watchlistSel');
  sel.innerHTML = '<option value="">Loading...</option>';
  const { resp: r, data: d } = await fetchJsonOrText('/scanner-builder/api/watchlists');
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
  const { resp: r, data: d } = await fetchJsonOrText('/scanner-builder/api/definitions');
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
  const q2 = currentQueryText();
  if (q2 && looksCorruptedQuery(q2.value)) {
    resetQueryText('Reset corrupted scanner text');
  }
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
  const ordered = Object.entries(groups).sort((a, b) => {
    if (a[0] === 'Weekly') return -1;
    if (b[0] === 'Weekly') return 1;
    return a[0].localeCompare(b[0]);
  });
  for (const [cat, items] of ordered) {
    const sortedItems = [...items].sort((a, b) => String(a.name || '').localeCompare(String(b.name || '')));
    sections.push(`<div><div class="mini" style="margin:6px 0 4px;font-weight:700;color:var(--text)">${esc(cat)}</div>${sortedItems.map(x => `
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
  const { resp: r, data: d } = await fetchJsonOrText(`/scanner-builder/api/definitions/${defId}`, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ alert_enabled: enabled, alert_mode })
  });
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
  const { resp: r, data: d } = await fetchJsonOrText(url, {
    method,
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  if (!r.ok || d.error) throw new Error(d.error || r.statusText);
  selectedScannerId = d.definition ? d.definition.id : selectedScannerId;
  setStatus(updateExisting ? 'Scanner updated' : 'Scanner saved');
  await loadDefinitions();
}

async function deleteScanner(id) {
  const s = savedScanners.find(x => Number(x.id) === Number(id));
  if (!s) return;
  if (!confirm(`Delete scanner '${s.name}'?`)) return;
  const { resp: r, data: d } = await fetchJsonOrText(`/scanner-builder/api/definitions/${id}`, { method:'DELETE' });
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
  const { resp: r, data: d } = await fetchJsonOrText('/scanner-builder/api/run', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
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
  const { resp: r, data: d } = await fetchJsonOrText('/scanner-builder/api/run', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ ...payload, limit: 1 }),
  });
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
  const { resp: r, data: d } = await fetchJsonOrText('/scanner-builder/api/catalog');
  scannerCatalog = d || scannerCatalog;
  functionCatalog = scannerCatalog.function_meta || (scannerCatalog.functions || []).map(s => ({name: String(s).split('(')[0], signature: s, description: 'Built-in function'}));
  renderCatalog();
  showAutocomplete();
}

async function init() {
  await loadGlobalApiTimeout();
  rules = [ruleTemplate()];
  renderRules();
  const q0 = currentQueryText();
  if (q0) q0.value = '';
  await loadWatchlists();
  await loadDefinitions();
  await loadCatalog();
  document.getElementById('rules').addEventListener('change', () => syncQueryFromBuilder());
  document.getElementById('watchlistSel').addEventListener('change', (e) => {
    localStorage.setItem('scanner_builder_watchlist_id', e.target.value || '');
  });
  const q = document.getElementById('queryText');
  if (q && (!q.value.trim() || looksCorruptedQuery(q.value))) {
    q.value = DEFAULT_QUERY_TEXT;
  }
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
  const q3 = currentQueryText();
  if (q3 && (!q3.value.trim() || looksCorruptedQuery(q3.value))) {
    q3.value = DEFAULT_QUERY_TEXT;
  }
  loadExample();
}

init().catch(e => {
  console.error(e);
  setStatus(e.message || 'Failed to initialize');
});
