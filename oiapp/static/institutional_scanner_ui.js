// institutional_scanner_ui.js
// Extracted from app.js -- the Institutional scanner pair: Accumulation
// (breakout) and Distribution (breakdown), which the code's own
// original comment described as "mirror of the breakout one" --
// genuinely one feature with two modes, kept together in one file
// rather than split by prefix alone. Fully self-contained.

// ════════════════════════════════════════════════════════════════════════════
// INSTITUTIONAL ACCUMULATION BREAKOUT SCANNER
// ════════════════════════════════════════════════════════════════════════════

async function _instLoadWatchlists() {
  const sel = document.getElementById('inst-watchlist');
  if (!sel) return;
  try {
    const d = await api('/watchlists/');
    const wls = (d && d.watchlists) ? d.watchlists : [];
    sel.innerHTML = '<option value="">⚡ All symbols (symbols table)</option>' +
      wls.map(w => `<option value="${w.id}"${w.is_default?' selected':''}>${w.is_default?'⭐ ':''}${w.name} (${w.symbol_count})</option>`).join('');
  } catch(e) { console.warn('inst watchlist load:', e.message); }
}

function _instRenderTable(results, status) {
  const el = document.getElementById('inst-results');
  if (!el) return;
  if (!results || !results.length) {
    el.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center">No institutional breakout signals found in this watchlist.</div>';
    return;
  }

  const signalColor = s => s.includes('Strong') ? '#22c55e' : s.includes('Moderate') ? '#818cf8' : '#f59e0b';
  const pct = v => {
    if (v == null || isNaN(v)) return '<span style="color:var(--muted)">—</span>';
    const c = v > 0 ? '#22c55e' : v < 0 ? '#ef4444' : '#94a3b8';
    return `<span style="color:${c};font-weight:600">${v>0?'+':''}${Number(v).toFixed(1)}%</span>`;
  };
  const emaStack = r => {
    const a20 = r.above_ema20, a50 = r.above_ema50, a200 = r.above_ema200;
    // 200 EMA is required (always green if here), 20/50 are optional bonuses
    const d200 = `<span title="Above 200 EMA (required)" style="color:#22c55e">●</span>`;
    const d50  = `<span title="Above 50 EMA (bonus)" style="color:${a50?'#22c55e':'#94a3b8'}">${a50?'●':'○'}</span>`;
    const d20  = `<span title="Above 20 EMA (bonus)" style="color:${a20?'#22c55e':'#94a3b8'}">${a20?'●':'○'}</span>`;
    return `<span style="font-size:11px">${d20}${d50}${d200}</span>`;
  };

  const rows = results.map(r => {
    const sc = signalColor(r.signal);
    const sweepBadge = r.liquidity_sweep
      ? `<span title="Liquidity sweep of ${r.sweep_depth}% before move" style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(245,158,11,.15);color:#f59e0b;font-weight:700">💧Sweep</span>`
      : '';
    const oiBar = (r.oi_buildup_pct == null)
      ? '<span style="color:var(--muted);font-size:10px">—</span>'
      : r.oi_buildup_pct > 0
        ? `<span style="color:#818cf8;font-size:10px;font-weight:600">+${r.oi_buildup_pct}%</span>`
        : r.oi_buildup_pct < 0
          ? `<span style="color:#ef4444;font-size:10px">${r.oi_buildup_pct}%</span>`
          : '<span style="color:var(--muted);font-size:10px">0%</span>';
    const bType = ({
      '52W Breakout':    '<span style="color:#22c55e;font-weight:700;font-size:10px">🚀 52W High</span>',
      'Base Breakout':   '<span style="color:#818cf8;font-weight:700;font-size:10px">📦 Base Break</span>',
      'Resistance Break':'<span style="color:#a78bfa;font-weight:700;font-size:10px">⚡ Resistance</span>',
      'EMA50 Reclaim':   '<span style="color:#f59e0b;font-weight:700;font-size:10px">📈 EMA50 Reclaim</span>',
      'EMA20 Reclaim':   '<span style="color:#fb923c;font-weight:700;font-size:10px">📈 EMA20 Reclaim</span>',
      '200 EMA Hold':    '<span style="color:#60a5fa;font-weight:700;font-size:10px">🛡 200 EMA Hold</span>',
      'Vol Momentum':    '<span style="color:#f472b6;font-weight:700;font-size:10px">⚡ Vol Momentum</span>',
    })[r.breakout_type] || `<span style="font-size:10px">${r.breakout_type}</span>`;

    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px 10px;font-weight:800;font-size:13px;color:${sc}">${r.symbol}</td>
      <td style="padding:8px 10px;font-size:10px;color:var(--muted);max-width:90px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${r.watchlist||''}">
        ${r.watchlist||'—'}
      </td>
      <td style="padding:8px 10px;text-align:center">
        <span style="font-size:16px;font-weight:900;color:${sc}">${r.score}</span>
        <div style="font-size:9px;color:${sc}">${r.signal.replace(/[🔥✅👀]/g,'').trim()}</div>
      </td>
      <td style="padding:8px 10px">${bType} ${sweepBadge}</td>
      <td style="padding:8px 10px;font-weight:700">$${r.price}</td>
      <td style="padding:8px 6px;text-align:right">${pct(r.price_5d_chg)}</td>
      <td style="padding:8px 6px;text-align:right">${pct(r.price_20d_chg)}</td>
      <td style="padding:8px 10px;text-align:center">
        <div style="font-size:11px;font-weight:700;color:${r.vol_surge>2?'#22c55e':r.vol_surge>1.5?'#f59e0b':'var(--text2)'}">${r.vol_surge}×</div>
        <div style="font-size:9px;color:var(--muted)">${r.vol_peak_pct}% peak</div>
      </td>
      <td style="padding:8px 10px;text-align:center">
        <div style="font-size:11px;font-weight:700;color:${r.rsi>70?'#ef4444':r.rsi>55?'#22c55e':r.rsi>45?'#818cf8':'#f59e0b'}">${r.rsi}</div>
        <div>${emaStack(r)}</div>
      </td>
      <td style="padding:8px 10px;text-align:center">
        ${r.oi_available === false
          ? '<span style="font-size:10px;color:var(--muted)">📵 No OI</span>'
          : oiBar}
      </td>
      <td style="padding:8px 10px;text-align:center">
        <div style="font-size:10px;color:var(--muted)">${r.base_tight_pct}% tight</div>
        <div style="font-size:10px;color:var(--muted)">${r.base_days}D base</div>
      </td>
      <td style="padding:8px 10px;text-align:center">
        ${r.vcp_detected
          ? `<span title="Contraction legs (oldest to newest): ${(r.vcp_contraction_pcts||[]).map(v=>v+'%').join(' → ')}" style="font-size:10px;padding:1px 5px;border-radius:3px;background:rgba(167,139,250,.15);color:#a78bfa;font-weight:700">🎯 VCP ×${r.vcp_num_contractions}</span>`
          : '<span style="font-size:10px;color:var(--muted)">—</span>'}
      </td>
      <td style="padding:8px 10px;text-align:center;font-size:10px;color:var(--muted)">
        ${r.new_52w_high ? '<span style="color:#f59e0b">🏆 New High</span>' : `${r.pct_from_52wh}% from 52W`}
      </td>
    </tr>`;
  }).join('');

  const hdr = `<tr style="background:rgba(255,255,255,.05);border-bottom:2px solid var(--border)">
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Symbol</th>
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Watchlist</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">Score</th>
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Breakout</th>
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Price</th>
    <th style="padding:7px 10px;text-align:right;font-size:10px;color:var(--muted)">5D%</th>
    <th style="padding:7px 10px;text-align:right;font-size:10px;color:var(--muted)">20D%</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">Vol Surge</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">RSI/EMA</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">OI Buildup</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">Base</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)" title="Volatility Contraction Pattern -- a genuine sequence of progressively shallower pullbacks over the lookback window">VCP</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">52W</th>
  </tr>`;

  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead>${hdr}</thead><tbody>${rows}</tbody></table>`;
}

function _instGetParams() {
  const ids = ['min_score','ema200_pct','min_price_5d','min_vol_surge','base_days','lookback_days','base_tight_max','rsi_min','rsi_max'];
  const p = {};
  ids.forEach(id => {
    const el = document.getElementById('inst-p-'+id);
    if (el) p[id] = parseFloat(el.value) || 0;
  });
  const cb = document.getElementById('inst-p-require_above_base');
  p.require_above_base = cb ? cb.checked : false;
  return p;
}

function _instToggleCriteria() {
  const panel = document.getElementById('inst-criteria-panel');
  if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

function _instResetCriteria() {
  const defaults = {min_score:3.0,ema200_pct:3.0,min_price_5d:1.5,min_vol_surge:1.2,base_days:30,lookback_days:65,base_tight_max:30,rsi_min:40,rsi_max:80};
  Object.entries(defaults).forEach(([k,v]) => { const el=document.getElementById('inst-p-'+k); if(el) el.value=v; });
  const cb=document.getElementById('inst-p-require_above_base'); if(cb) cb.checked=false;
}

async function _instRunScan() {
  const btn = document.getElementById('btn-inst-run');
  const st  = document.getElementById('inst-status');
  const wlId = document.getElementById('inst-watchlist')?.value || '';
  const params = _instGetParams();
  const t0  = Date.now();
  if (btn) btn.disabled = true;
  if (st)  st.textContent = '⏳ Scanning…';
  document.getElementById('inst-results').innerHTML =
    '<div style="color:var(--muted);padding:20px;text-align:center">⏳ Running institutional scan (parallel, ~30–90s)…</div>';

  var _t = setInterval(() => {
    if(st) st.textContent = `⏳ Scanning… ${Math.round((Date.now()-t0)/1000)}s`;
  }, 2000);

  try {
    const url = `/scanner/institutional/scan${wlId?'?watchlist_id='+wlId:''}`;
    const d = await api(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(params)});
    clearInterval(_t);
    const elapsed = ((Date.now()-t0)/1000).toFixed(1);
    const oiNote = d.use_oi ? '📊 OI' : '💰 no OI';
    const errNote = d.errors > 0 ? ` · ⚠ ${d.errors} errors` : '';
    if (st) st.textContent = `✅ ${d.count} signals · ${d.total_scanned} scanned · ${elapsed}s · ${d.watchlist||'All'} · ${oiNote}${errNote}`;
    _instRenderTable(d.results);
    addNotif('ok', `🏦 Inst. Breakout: ${d.count} signals`,
      `${d.count} of ${d.total_scanned} passed · took ${elapsed}s`, 'Inst. Breakout');
  } catch(e) {
    clearInterval(_t);
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', 'Inst. Scan failed', e.message, 'Inst. Breakout');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _instLoadCache() {
  const st = document.getElementById('inst-status');
  try {
    const d = await api('/scanner/institutional/scan_cached');
    if (!d.results.length) {
      if (st) st.textContent = 'No cached results — run scanner first';
      return;
    }
    if (st) st.textContent = `📥 Loaded ${d.count} cached results · ${d.completed_at}`;
    _instRenderTable(d.results);
  } catch(e) {
    if (st) st.textContent = '❌ ' + e.message;
  }
}

// inst-scan tab wiring moved to main tab router

// ════════════════════════════════════════════════════════════════════════════
// INSTITUTIONAL DISTRIBUTION BREAKDOWN SCANNER (mirror of the breakout one)
// ════════════════════════════════════════════════════════════════════════════

async function _ppLoadPatterns() {
  const el = document.getElementById('pp-pattern-picker');
  if (!el || el.dataset.loaded) return;
  el.innerHTML = '<div style="color:var(--muted)">Loading patterns…</div>';
  try {
    const d = await api('/pattern-scanner/patterns');
    const byCategory = {};
    (d.patterns || []).forEach(p => { (byCategory[p.category] = byCategory[p.category] || []).push(p); });
    const dirIcon = (dir) => dir === 'bullish' ? '<span style="color:#22c55e">▲</span>' : dir === 'bearish' ? '<span style="color:#ef4444">▼</span>' : '<span style="color:var(--muted)">◆</span>';
    el.innerHTML = Object.entries(byCategory).map(([cat, pats]) => `
      <div style="min-width:200px">
        <div style="font-weight:700;color:var(--muted);font-size:10px;margin-bottom:4px;text-transform:uppercase">${cat}</div>
        ${pats.map(p => `<label style="display:flex;align-items:center;gap:5px;margin-bottom:3px;cursor:pointer">
          <input type="checkbox" class="pp-pattern-cb" value="${p.key}" checked> ${dirIcon(p.direction)} ${p.label}
        </label>`).join('')}
      </div>`).join('');
    el.dataset.loaded = '1';
  } catch(e) {
    el.innerHTML = `<div style="color:#ef4444">Failed to load patterns: ${e.message}</div>`;
  }
}

function _ppSelectAll(checked) {
  document.querySelectorAll('.pp-pattern-cb').forEach(cb => { cb.checked = checked; });
}

async function _ppRun() {
  const status = document.getElementById('pp-status');
  const resultsEl = document.getElementById('pp-results');
  const watchlistId = document.getElementById('pp-watchlist')?.value;
  const timeframe = document.getElementById('pp-timeframe')?.value || '1d';
  const patterns = Array.from(document.querySelectorAll('.pp-pattern-cb:checked')).map(cb => cb.value);
  if (!watchlistId) { status.textContent = '❌ Select a watchlist first.'; return; }
  if (!patterns.length) { status.textContent = '❌ Select at least one pattern.'; return; }

  status.textContent = `⏳ Running ${patterns.length} pattern(s) against the watchlist (${timeframe})…`;
  resultsEl.innerHTML = '';
  try {
    const d = await api('/pattern-scanner/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      watchlist_id: watchlistId, timeframe, patterns,
    })});
    if (d.error) { status.textContent = `❌ ${d.error}`; return; }
    const results = d.results || [];
    status.textContent = `✅ ${results.length} symbol(s) matched at least one pattern, out of ${d.patterns_run} pattern(s) run · ${timeframe}` +
      (d.errors && d.errors.length ? ` · ⚠ ${d.errors.length} pattern(s) errored (see console)` : '');
    if (d.errors && d.errors.length) console.warn('[price-patterns] pattern errors:', d.errors);
    resultsEl.innerHTML = _ppRenderResults(results);
  } catch(e) {
    status.textContent = `❌ ${e.message}`;
  }
}

function _ppRenderResults(results) {
  if (!results.length) return '<div style="padding:20px;color:var(--muted)">No matches.</div>';
  const dirColor = (dir) => dir === 'bullish' ? '#22c55e' : dir === 'bearish' ? '#ef4444' : '#94a3b8';
  const rows = results.map(r => {
    const tp = r.trade_plan;
    return `
    <tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px;font-weight:700">${r.symbol}</td>
      <td style="padding:8px;text-align:right;font-family:monospace">${r.price!=null?'$'+Number(r.price).toFixed(2):'—'}</td>
      <td style="padding:8px;text-align:center;font-size:11px">${(r.patterns||[]).length}</td>
      <td style="padding:8px;text-align:right;font-family:monospace;font-size:11px;color:#ef4444">${tp?'$'+tp.stop:'—'}</td>
      <td style="padding:8px;text-align:right;font-family:monospace;font-size:11px;color:#22c55e">${tp?'$'+tp.target:'—'}</td>
      <td style="padding:8px;text-align:center;font-size:11px">${tp?tp.rr+'R':'—'}</td>
      <td style="padding:8px;text-align:center;font-size:11px" title="Statistical estimate from where price sits in its own Resistance/Support range -- NOT an options-priced probability (no delta/IV available for a bare stock-level pattern). Rough sanity check, not a precise number.">${tp?tp.pop_pct+'%':'—'}</td>
      <td style="padding:8px;font-size:11px">
        ${(r.patterns||[]).map(p => `<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;border-radius:4px;background:${dirColor(p.direction)}18;color:${dirColor(p.direction)};border:1px solid ${dirColor(p.direction)}44;white-space:nowrap">${p.label}</span>`).join('')}
      </td>
    </tr>`;
  }).join('');
  return `<div style="font-size:10px;color:var(--muted);margin-bottom:6px">Target/Stop/RR use the symbol's own 20-bar Resistance/Support range, oriented by the majority direction of its matched patterns. POP is a statistical range-position estimate, not an options-priced probability -- hover the column for detail. Symbols with mixed/tied bullish+bearish matches show no plan (no defensible single direction to size against).</div>
  <table style="width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed">
    <colgroup>
      <col style="width:9%"><col style="width:9%"><col style="width:7%">
      <col style="width:9%"><col style="width:9%"><col style="width:6%"><col style="width:6%">
      <col>
    </colgroup>
    <thead><tr style="background:#0b1322">
      <th style="padding:8px;text-align:left;font-size:10px;color:var(--muted)">SYMBOL</th>
      <th style="padding:8px;text-align:right;font-size:10px;color:var(--muted)">PRICE</th>
      <th style="padding:8px;text-align:center;font-size:10px;color:var(--muted)">#</th>
      <th style="padding:8px;text-align:right;font-size:10px;color:var(--muted)">STOP</th>
      <th style="padding:8px;text-align:right;font-size:10px;color:var(--muted)">TARGET</th>
      <th style="padding:8px;text-align:center;font-size:10px;color:var(--muted)">RR</th>
      <th style="padding:8px;text-align:center;font-size:10px;color:var(--muted)">POP*</th>
      <th style="padding:8px;text-align:left;font-size:10px;color:var(--muted)">PATTERNS</th>
    </tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function _mtfLoadScenarios() {
  const el = document.getElementById('mtf-scenario-picker');
  if (!el || el.dataset.loaded) return;
  el.innerHTML = '<div style="color:var(--muted)">Loading scenarios…</div>';
  try {
    const d = await api('/mtf-scanner/options');
    el.innerHTML = (d.scenarios || []).map(s => `
      <label style="display:flex;align-items:center;gap:5px;cursor:pointer;max-width:280px">
        <input type="checkbox" class="mtf-scenario-cb" value="${s.key}" checked> ${s.label}
      </label>`).join('');
    el.dataset.loaded = '1';
  } catch(e) {
    el.innerHTML = `<div style="color:#ef4444">Failed to load scenarios: ${e.message}</div>`;
  }
}

async function _mtfRun() {
  const status = document.getElementById('mtf-status');
  const resultsEl = document.getElementById('mtf-results');
  const watchlistId = document.getElementById('mtf-watchlist')?.value;
  const htf = document.getElementById('mtf-htf')?.value || '1m';
  const ltf = document.getElementById('mtf-ltf')?.value || '1d';
  const scenarios = Array.from(document.querySelectorAll('.mtf-scenario-cb:checked')).map(cb => cb.value);
  if (!watchlistId) { status.textContent = '❌ Select a watchlist first.'; return; }
  if (!scenarios.length) { status.textContent = '❌ Select at least one scenario.'; return; }

  status.textContent = `⏳ Running ${scenarios.length} scenario(s) — HTF ${htf} / LTF ${ltf}…`;
  resultsEl.innerHTML = '';
  try {
    const d = await api('/mtf-scanner/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
      watchlist_id: watchlistId, htf, ltf, scenarios,
    })});
    if (d.error) { status.textContent = `❌ ${d.error}`; return; }
    const results = d.results || [];
    status.textContent = `✅ ${results.length} symbol(s) matched at least one scenario, out of ${d.scenarios_run} run · HTF ${htf} / LTF ${ltf}` +
      (d.errors && d.errors.length ? ` · ⚠ ${d.errors.length} scenario(s) errored (see console)` : '');
    if (d.errors && d.errors.length) console.warn('[mtf-scan] scenario errors:', d.errors);
    resultsEl.innerHTML = _mtfRenderResults(results);
  } catch(e) {
    status.textContent = `❌ ${e.message}`;
  }
}

function _mtfRenderResults(results) {
  if (!results.length) return '<div style="padding:20px;color:var(--muted)">No matches.</div>';
  const rows = results.map(r => `
    <tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px;font-weight:700">${r.symbol}</td>
      <td style="padding:8px;text-align:right;font-family:monospace">${r.price!=null?'$'+Number(r.price).toFixed(2):'—'}</td>
      <td style="padding:8px;text-align:center;font-size:11px">${(r.scenarios||[]).length}</td>
      <td style="padding:8px;font-size:11px">
        ${(r.scenarios||[]).map(s => `<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;border-radius:4px;background:#3b82f618;color:#3b82f6;border:1px solid #3b82f644;white-space:nowrap">${s.label}</span>`).join('')}
      </td>
    </tr>`).join('');
  return `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead><tr style="background:#0b1322">
      <th style="padding:8px;text-align:left;font-size:10px;color:var(--muted)">SYMBOL</th>
      <th style="padding:8px;text-align:right;font-size:10px;color:var(--muted)">PRICE</th>
      <th style="padding:8px;text-align:center;font-size:10px;color:var(--muted)">#</th>
      <th style="padding:8px;text-align:left;font-size:10px;color:var(--muted)">SCENARIOS MATCHED</th>
    </tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function _distLoadWatchlists() {
  const sel = document.getElementById('dist-watchlist');
  if (!sel) return;
  try {
    const d = await api('/watchlists/');
    const wls = (d && d.watchlists) ? d.watchlists : [];
    sel.innerHTML = '<option value="">⚡ All symbols (symbols table)</option>' +
      wls.map(w => `<option value="${w.id}"${w.is_default?' selected':''}>${w.is_default?'⭐ ':''}${w.name} (${w.symbol_count})</option>`).join('');
  } catch(e) { console.warn('dist watchlist load:', e.message); }
}

function _distRenderTable(results) {
  const el = document.getElementById('dist-results');
  if (!el) return;
  if (!results || !results.length) {
    el.innerHTML = '<div style="color:var(--muted);padding:20px;text-align:center">No distribution/breakdown signals found in this watchlist.</div>';
    return;
  }

  const signalColor = s => s.includes('Strong') ? '#ef4444' : s.includes('Moderate') ? '#f59e0b' : '#94a3b8';
  const pct = v => {
    if (v == null || isNaN(v)) return '<span style="color:var(--muted)">—</span>';
    const c = v > 0 ? '#22c55e' : v < 0 ? '#ef4444' : '#94a3b8';
    return `<span style="color:${c};font-weight:600">${v>0?'+':''}${Number(v).toFixed(1)}%</span>`;
  };

  const rows = results.map(r => {
    const sc = signalColor(r.signal);
    const bt = ({
      'Distribution Top': '<span style="color:#ef4444;font-weight:700;font-size:10px">🔻 Distribution Top</span>',
      'RS Divergence':    '<span style="color:#f59e0b;font-weight:700;font-size:10px">📉 RS Divergence</span>',
      'Churn/Stall':      '<span style="color:#fb923c;font-weight:700;font-size:10px">🌀 Churn/Stall</span>',
      'Volume Weakening': '<span style="color:#f472b6;font-weight:700;font-size:10px">📊 Vol Weakening</span>',
    })[r.breakdown_type] || `<span style="font-size:10px">${r.breakdown_type}</span>`;
    const elevatedBadge = r.still_elevated
      ? `<span title="Still within ${r.pct_below_20d_high}% of 20D high -- pre-breakdown window, not confirmation after the fact" style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(245,158,11,.15);color:#f59e0b;font-weight:700">⏳Elevated</span>`
      : '';

    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px 10px;font-weight:800;font-size:13px;color:${sc}">${r.symbol}</td>
      <td style="padding:8px 10px;font-size:10px;color:var(--muted);max-width:90px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${r.watchlist||''}">
        ${r.watchlist||'—'}
      </td>
      <td style="padding:8px 10px;text-align:center">
        <span style="font-size:16px;font-weight:900;color:${sc}">${r.score}</span>
        <div style="font-size:9px;color:${sc}">${r.signal.replace(/[🔻⚠️👀]/g,'').trim()}</div>
      </td>
      <td style="padding:8px 10px">${bt} ${elevatedBadge}</td>
      <td style="padding:8px 10px;font-weight:700">$${r.price}</td>
      <td style="padding:8px 10px;text-align:center">
        <div style="font-size:11px;font-weight:700;color:${r.udvr<0.65?'#ef4444':r.udvr<r.udvr_threshold?'#f59e0b':'var(--text2)'}">${r.udvr}×</div>
        <div style="font-size:9px;color:var(--muted)">udvr (thr ${r.udvr_threshold})</div>
      </td>
      <td style="padding:8px 10px;text-align:center">
        <div style="font-size:11px;font-weight:700;color:${r.dist_day_count>=6?'#ef4444':r.dist_day_count>=r.min_dist_days?'#f59e0b':'var(--text2)'}">${r.dist_day_count}</div>
        <div style="font-size:9px;color:var(--muted)">dist days</div>
      </td>
      <td style="padding:8px 10px;text-align:center">
        <div style="font-size:11px;font-weight:700">${r.churn_count}</div>
        <div style="font-size:9px;color:var(--muted)">churn days</div>
      </td>
      <td style="padding:8px 10px;text-align:center;font-size:10px">
        ${r.rs_rolling_over ? '<span style="color:#ef4444">📉 RS rollover</span>' : '<span style="color:var(--muted)">—</span>'}
      </td>
      <td style="padding:8px 10px;text-align:center;font-size:10px;color:var(--muted)">
        ${pct(-r.pct_below_20d_high)} from 20D high
      </td>
    </tr>`;
  }).join('');

  const hdr = `<tr style="background:rgba(255,255,255,.05);border-bottom:2px solid var(--border)">
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Symbol</th>
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Watchlist</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">Score</th>
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Breakdown</th>
    <th style="padding:7px 10px;text-align:left;font-size:10px;color:var(--muted)">Price</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">Up/Down Vol</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">Dist Days</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">Churn</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">RS Line</th>
    <th style="padding:7px 10px;text-align:center;font-size:10px;color:var(--muted)">vs 20D High</th>
  </tr>`;

  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px">
    <thead>${hdr}</thead><tbody>${rows}</tbody></table>`;
}

function _distGetParams() {
  const ids = ['min_score','udvr_lookback','udvr_threshold','dist_lookback','min_dist_days','stall_lookback','min_churn_days','pct_near_high_max'];
  const p = {};
  ids.forEach(id => {
    const el = document.getElementById('dist-p-'+id);
    if (el) p[id] = parseFloat(el.value) || 0;
  });
  return p;
}

function _distToggleCriteria() {
  const panel = document.getElementById('dist-criteria-panel');
  if (panel) panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
}

function _distResetCriteria() {
  const defaults = {min_score:3.0,udvr_lookback:20,udvr_threshold:0.77,dist_lookback:25,min_dist_days:4,stall_lookback:10,min_churn_days:2,pct_near_high_max:5};
  Object.entries(defaults).forEach(([k,v]) => { const el=document.getElementById('dist-p-'+k); if(el) el.value=v; });
}

async function _distRunScan() {
  const btn = document.getElementById('btn-dist-run');
  const st  = document.getElementById('dist-status');
  const wlId = document.getElementById('dist-watchlist')?.value || '';
  const params = _distGetParams();
  const t0  = Date.now();
  if (btn) btn.disabled = true;
  if (st)  st.textContent = '⏳ Scanning…';
  document.getElementById('dist-results').innerHTML =
    '<div style="color:var(--muted);padding:20px;text-align:center">⏳ Running distribution scan…</div>';

  var _t = setInterval(() => {
    if(st) st.textContent = `⏳ Scanning… ${Math.round((Date.now()-t0)/1000)}s`;
  }, 2000);

  try {
    const url = `/scanner/distribution/scan${wlId?'?watchlist_id='+wlId:''}`;
    const d = await api(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(params)});
    clearInterval(_t);
    const elapsed = ((Date.now()-t0)/1000).toFixed(1);
    const errNote = d.errors > 0 ? ` · ⚠ ${d.errors} errors` : '';
    if (st) st.textContent = `✅ ${d.count} signals · ${d.total_scanned} scanned · ${elapsed}s · ${d.watchlist||'All'}${errNote}`;
    _distRenderTable(d.results);
    addNotif('ok', `🔻 Inst. Breakdown: ${d.count} signals`,
      `${d.count} of ${d.total_scanned} passed · took ${elapsed}s`, 'Inst. Breakdown');
  } catch(e) {
    clearInterval(_t);
    if (st) st.textContent = '❌ ' + e.message;
    if (btn) btn.disabled = false;
    addNotif('error', 'Dist. Scan failed', e.message, 'Inst. Breakdown');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function _distLoadCache() {
  const st = document.getElementById('dist-status');
  try {
    const d = await api('/scanner/distribution/scan_cached');
    if (!d.results.length) {
      if (st) st.textContent = 'No cached results — run scanner first';
      return;
    }
    if (st) st.textContent = `📦 Cached: ${d.results.length} signals · ${d.completed_at||''}`;
    _distRenderTable(d.results);
  } catch(e) {
    if (st) st.textContent = '❌ ' + e.message;
  }
}



