
const INDICATORS = [
  'close','open','high','low','volume',
  'rsi3','rsi14','ema5','ema9','ema20','ema50','ema200',
  'ema_rsi14_13','ema_rsi14_90','macd','macd_signal','macd_hist',
  'relative_strength','leadership'
];
const TIMEFRAMES = ['15m','1h','2h','4h','1d','1w','1m'];
const OPERATORS = ['>','>=','<','<=','=','crosses above','crosses below'];
let watchlists = [];
let savedScanners = [];
let selectedScannerId = null;
let rules = [];

function esc(s){return String(s ?? '').replace(/[&<>\"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[m]));}
function tfLabel(tf){ return tf === '1m' ? '1m (monthly)' : tf; }

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
  updateActionButtons();
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

  // Split on newlines/semicolons first, then keep AND / OR connectors between clauses.
  const tokens = text
    .replace(/[\n;]+/g, ' ')
    .split(/\b(AND|OR)\b/i)
    .map(x => x.trim())
    .filter(Boolean);

  const parsed = [];
  let pendingConnector = 'AND';

  const parseTerm = (t) => {
    const term = t.trim();
    const br = term.match(/^(.+)\[(.+)\]$/);
    if (br) return {name: br[1].trim().toLowerCase(), tf: br[2].trim().toLowerCase()};
    if (/^-?\d+(?:\.\d+)?$/.test(term)) return {value: term, type: 'value'};
    const pref = term.match(/^(15m|1h|2h|4h|1d|1w|1m|1mo|daily|weekly|monthly)\s+(.+)$/i);
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
  updateActionButtons();
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

function updateActionButtons() {
  const query = document.getElementById('queryText')?.value.trim() || '';
  const name  = document.getElementById('scannerName')?.value.trim() || '';
  const hasSelected = !!selectedScannerId;
  const runBtn   = document.getElementById('runBtn');
  const saveBtn  = document.getElementById('saveBtn');
  const updateBtn = document.getElementById('updateBtn');
  if (runBtn) runBtn.disabled = !query;
  if (saveBtn) saveBtn.disabled = !query;
  if (updateBtn) updateBtn.disabled = !(query && hasSelected);
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
}

async function loadDefinitions() {
  const r = await fetch('/scanner-builder/api/definitions');
  const d = await r.json();
  savedScanners = d.definitions || [];
  const body = document.getElementById('savedBody');
  if (!savedScanners.length) {
    body.innerHTML = '<tr><td colspan="5" class="small">No saved scanners yet.</td></tr>';
    return;
  }
  const wlMap = Object.fromEntries(watchlists.map(w => [String(w.id), w.name]));
  body.innerHTML = savedScanners.map(s => `
    <tr>
      <td><b>${esc(s.name)}</b><div class="mini">${esc(s.description || '')}</div></td>
      <td>${esc(wlMap[String(s.watchlist_id)] || 'All symbols')}</td>
      <td>${esc(s.benchmark || 'SPY')}</td>
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
  updateActionButtons();
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
  updateActionButtons();
}

async function init() {
  rules = [ruleTemplate()];
  renderRules();
  await loadWatchlists();
  await loadDefinitions();
  document.getElementById('rules').addEventListener('change', () => syncQueryFromBuilder());
  document.getElementById('queryText').addEventListener('input', () => {
    setStatus('Text updated');
    updateActionButtons();
  });
  document.getElementById('scannerName').addEventListener('input', updateActionButtons);
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
  document.getElementById('runBtn').addEventListener('click', () => runScanner());
  document.getElementById('saveBtn').addEventListener('click', () => saveScanner(false).catch(e => alert(e.message)));
  document.getElementById('updateBtn').addEventListener('click', () => saveScanner(true).catch(e => alert(e.message)));
  document.getElementById('clearBtn').addEventListener('click', clearPage);
  document.getElementById('refreshSavedBtn').addEventListener('click', () => loadDefinitions());
  document.getElementById('logic-chips').innerHTML = [
    ['Text-first', 'g'], ['Saved scanners', 'y'], ['Watchlist execution', 'g'], ['AND / OR', 'y'], ['Crossovers', 'r']
  ].map(([t,c]) => `<span class="chip ${c}">${t}</span>`).join('');
  loadExample();
  updateActionButtons();
}

init().catch(e => {
  console.error(e);
  setStatus(e.message || 'Failed to initialize');
});
