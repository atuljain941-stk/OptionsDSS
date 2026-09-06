// weekly_analysis_ui.js
// Extracted from app.js -- the Weekly Market Analysis page (per-symbol
// and per-watchlist runs, section cards, bias filtering). Fully
// self-contained.

// ════════════════════════════════════════════════════════════════════════════
// WEEKLY MARKET ANALYSIS
// ════════════════════════════════════════════════════════════════════════════

async function _waLoadWatchlists() {
  const sel = document.getElementById('wa-watchlist');
  if (!sel) return;
  try {
    await _loadWatchlistDropdown('wa-watchlist', 'wa-wl-count', 'Default watchlist');
    _waUpdateWatchlistCount();
  } catch (e) {
    sel.innerHTML = '<option value="">⚠ ' + e.message + '</option>';
  }
}

function _waUpdateWatchlistCount() {
  _updateWlCount('wa-watchlist', 'wa-wl-count');
}

async function _waRunWatchlist() {
  const sel = document.getElementById('wa-watchlist');
  const wlId = sel ? sel.value : '';
  const st  = document.getElementById('wa-status');
  const res = document.getElementById('wa-result');
  if (!res) return;
  if (st) st.textContent = '⏳ Running watchlist flow scan…';
  res.innerHTML = '<div style="padding:20px;text-align:center;color:var(--muted)">⏳ Running weekly flow scan…</div>';
  const t0 = Date.now();
  const timer = setInterval(() => { if (st) st.textContent = `⏳ Watchlist ${Math.round((Date.now()-t0)/1000)}s…`; }, 3000);
  try {
    const url = wlId ? `/weekly_analysis/watchlist?watchlist_id=${wlId}` : '/weekly_analysis/watchlist';
    const d = await api(url);
    clearInterval(timer);
    if (st) st.textContent = `✅ ${d.watchlist_name || 'Watchlist'} — ${d.elapsed_s || 0}s`;
    res.innerHTML = _waRenderWatchlist(d);
    _waApplyBiasFilter();
  } catch (e) {
    clearInterval(timer);
    if (st) st.textContent = `❌ ${e.message}`;
    res.innerHTML = `<div style="color:#ef4444;padding:16px">Error: ${e.message}</div>`;
  }
}


async function _waRun() {
  const sym = document.getElementById('wa-symbol')?.value || 'SPY';
  const st  = document.getElementById('wa-status');
  const res = document.getElementById('wa-result');
  if (!res) return;
  if (st)  st.textContent = `⏳ Analyzing ${sym}…`;
  res.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted)">⏳ Running 5-factor analysis (30-60s)…</div>';
  const t0 = Date.now();
  const timer = setInterval(() => { if(st) st.textContent = `⏳ ${sym} ${Math.round((Date.now()-t0)/1000)}s…`; }, 3000);
  try {
    const d = await api(`/weekly_analysis/analyze/${sym}`);
    clearInterval(timer);
    if (st) st.textContent = `✅ ${sym} — ${d.elapsed_s}s`;
    res.innerHTML = _waRender(d);
  } catch(e) {
    clearInterval(timer);
    if (st) st.textContent = `❌ ${e.message}`;
    res.innerHTML = `<div style="color:#ef4444;padding:16px">Error: ${e.message}</div>`;
  }
}

async function _waRunAll() {
  const st  = document.getElementById('wa-status');
  const res = document.getElementById('wa-result');
  if (!res) return;
  if (st) st.textContent = '⏳ Analyzing SPY + QQQ + IWM in parallel…';
  res.innerHTML = '<div style="padding:24px;text-align:center;color:var(--muted)">⏳ Running parallel analysis (60-90s)…</div>';
  const t0 = Date.now();
  const timer = setInterval(() => { if(st) st.textContent = `⏳ ${Math.round((Date.now()-t0)/1000)}s…`; }, 3000);
  try {
    const d = await api('/weekly_analysis/multi?symbols=SPY,QQQ,IWM');
    clearInterval(timer);
    if (st) st.textContent = `✅ Done`;
    res.innerHTML = Object.values(d.results||{}).map(r => _waRender(r)).join('<hr style="border:none;border-top:2px solid var(--border);margin:16px 0">');
  } catch(e) {
    clearInterval(timer);
    if (st) st.textContent = `❌ ${e.message}`;
  }
}

function _waScoreBar(score, max=10) {
  const pct   = Math.min(100, score / max * 100);
  const color = score >= 7 ? '#22c55e' : score >= 5.5 ? '#86efac' : score >= 4 ? '#94a3b8' : score >= 2.5 ? '#fca5a5' : '#ef4444';
  return `<div style="display:flex;align-items:center;gap:6px">
    <div style="flex:1;height:6px;background:var(--bg);border-radius:3px">
      <div style="height:6px;width:${pct}%;background:${color};border-radius:3px;transition:width .5s"></div>
    </div>
    <span style="font-size:11px;font-weight:700;color:${color};min-width:28px">${score}</span>
  </div>`;
}

const _GOLD_SILVER_CONTRACTS_JS = ["/GC", "/SI", "/MGC", "/SIL"];

function _waSectionCard(icon, title, r, extraHtml='') {
  if (!r) return '';
  const found = r.found !== false;
  const score = r.score ?? null;
  const sc    = score !== null ? score : null;
  const scColor = sc >= 7 ? '#22c55e' : sc >= 5.5 ? '#86efac' : sc >= 4 ? '#94a3b8' : sc >= 2.5 ? '#fca5a5' : '#ef4444';
  return `<div style="background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px 14px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-weight:700;font-size:13px">${icon} ${title}</span>
      ${sc !== null ? `<span style="font-size:20px;font-weight:900;color:${scColor}">${sc}</span>` : ''}
    </div>
    ${sc !== null ? _waScoreBar(sc) + '<div style="height:4px"></div>' : ''}
    <div style="font-size:11px;color:var(--text2);margin-top:4px">${r.detail || (found ? 'Data available' : '📭 No data')}</div>
    ${extraHtml}
  </div>`;
}

function _waRender(d) {
  if (!d || d.error) return `<div style="color:#ef4444;padding:12px">Analysis failed: ${d?.error||'unknown'}</div>`;

  const ws     = d.weekly_score ?? 5;
  const wsC    = ws >= 7 ? '#22c55e' : ws >= 5.5 ? '#86efac' : ws >= 4 ? '#94a3b8' : ws >= 2.5 ? '#fca5a5' : '#ef4444';
  const s      = d.sections || {};
  const cot    = s.cot     || {};
  const fut    = s.futures || {};
  const opt    = s.options || {};
  const vs     = s.vol_skew|| {};
  const vix    = vs.vix    || {};
  const skew   = vs.skew   || {};
  const macro  = s.macro   || {};
  const breadth= s.breadth || {};
  const earn   = s.earnings|| {};

  // ── Macro + breadth extra detail ──
  const macroExtra = (macro.dxy || macro.yield_10y || breadth.found) ? `
    <div style="margin-top:6px;font-size:10px;line-height:1.6">
      ${macro.yield_10y ? `<div><span style="color:var(--muted)">10Y Yield:</span> <b>${macro.yield_10y.level_pct}%</b>
        <span style="color:${macro.yield_10y.chg_5d_bps>0?'#ef4444':'#22c55e'}">(${macro.yield_10y.chg_5d_bps>0?'+':''}${macro.yield_10y.chg_5d_bps}bps 5d)</span></div>` : ''}
      ${macro.dxy ? `<div><span style="color:var(--muted)">DXY:</span> <b>${macro.dxy.level}</b>
        <span style="color:${macro.dxy.chg_5d_pct>0?'#ef4444':'#22c55e'}">(${macro.dxy.chg_5d_pct>0?'+':''}${macro.dxy.chg_5d_pct}% 5d)</span>
        ${_GOLD_SILVER_CONTRACTS_JS.includes(d.contract) ? '<span style="font-size:9px;color:var(--muted)"> — feeds score (gold/silver)</span>' : '<span style="font-size:9px;color:var(--muted)"> — context only</span>'}</div>` : ''}
      ${breadth.found ? `<div><span style="color:var(--muted)">Breadth:</span> <b>${breadth.pct_above_20ma}%</b> above 20MA
        <span style="color:var(--muted)">(${breadth.symbols_checked} symbols) — ${breadth.label}</span></div>` : ''}
    </div>` : '';

  const earnBadge = earn.in_plan_window
    ? `<div style="margin-top:8px;padding:6px 10px;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.35);border-radius:6px;font-size:10.5px;color:#fca5a5">
        ⚠ Earnings in ${earn.earn_days}d (${earn.earn_date}) — inside this plan's window</div>`
    : (earn.found && earn.earn_date ? `<div style="margin-top:4px;font-size:9px;color:var(--muted)">Next earnings: ${earn.earn_date} (${earn.earn_days}d — outside plan window)</div>` : '');

  // ── Hero ──
  const heroHtml = `
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px;
                background:linear-gradient(135deg,rgba(99,102,241,.1),rgba(0,0,0,0));
                border:2px solid rgba(99,102,241,.25);border-radius:10px;padding:14px 18px;flex-wrap:wrap">
      <div style="text-align:center">
        <div style="font-size:44px;font-weight:900;color:${wsC};line-height:1">${ws.toFixed(1)}</div>
        <div style="font-size:10px;color:var(--muted)">/10</div>
      </div>
      <div style="flex:1;min-width:200px">
        <div style="font-size:20px;font-weight:700;color:${wsC};margin-bottom:3px">${d.bias}</div>
        <div style="font-size:12px;color:var(--text2)">${d.symbol} · ${d.contract} · ${d.generated_at}</div>
      </div>
      <div style="min-width:240px">
        <div style="font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px">This Week's Strategies</div>
        ${(d.strategies||[]).map(s => `<div style="font-size:11px;color:var(--text2);margin-bottom:2px">${s}</div>`).join('')}
        ${earnBadge}
      </div>
    </div>`;

  // ── COT extra detail ──
  const cotIdx = cot.cot_index;
  const cotExtra = cot.found ? `
    <div style="margin-top:6px">
      <div style="display:flex;align-items:center;gap:5px">
        <span style="font-size:9px;color:#ef4444">Bear 0</span>
        <div style="flex:1;height:5px;background:var(--bg);border-radius:3px">
          <div style="height:5px;width:${Math.min(100,cotIdx||0)}%;background:${(cotIdx||0)>=75?'#22c55e':(cotIdx||0)<=25?'#ef4444':'#94a3b8'};border-radius:3px"></div>
        </div>
        <span style="font-size:9px;color:#22c55e">100 Bull</span>
        <b style="font-size:12px;color:var(--text2);margin-left:4px">${(cotIdx||0).toFixed(0)}/100</b>
      </div>
      <div style="display:flex;gap:8px;margin-top:4px;font-size:10px;flex-wrap:wrap">
        <span><span style="color:var(--muted)">Net:</span> <b>${(cot.net||0)>=0?'+':''}${(cot.net||0).toLocaleString()}</b></span>
        <span><span style="color:var(--muted)">WoW:</span> <b style="color:${(cot.wow_change||0)>=0?'#22c55e':'#ef4444'}">${(cot.wow_change||0)>=0?'+':''}${(cot.wow_change||0).toLocaleString()}</b></span>
        <span><span style="color:var(--muted)">Trend:</span> <b>${cot.trend_dir||'—'}</b></span>
        <span style="font-size:9px;font-style:italic;color:${(cotIdx||0)<=10?'#22c55e':(cotIdx||0)>=90?'#f59e0b':'var(--muted)'}">${(cotIdx||0)<=10?'⚡ Extreme short — contrarian buy':(cotIdx||0)>=90?'⚠ Crowded long — fade risk':'Moderate positioning'}</span>
      </div>
    </div>` : '';

  // ── Options extra detail ──
  const callW = (opt.call_walls||[]).slice(0,3).map(w=>`<span style="background:rgba(34,197,94,.1);color:#22c55e;font-size:10px;padding:1px 5px;border-radius:3px">C$${w.strike} (${(w.oi/1000).toFixed(0)}K)</span>`).join(' ');
  const putW  = (opt.put_walls ||[]).slice(0,3).map(w=>`<span style="background:rgba(239,68,68,.1);color:#ef4444;font-size:10px;padding:1px 5px;border-radius:3px">P$${w.strike} (${(w.oi/1000).toFixed(0)}K)</span>`).join(' ');
  const optExtra = opt.found ? `
    <div style="margin-top:6px;font-size:10px">
      <div style="display:flex;gap:8px;margin-bottom:3px;flex-wrap:wrap">
        <span><span style="color:var(--muted)">PCR:</span> <b>${opt.pcr||'—'}</b></span>
        <span><span style="color:var(--muted)">Trend:</span> <b>${opt.pcr_trend||'—'}</b></span>
        <span><span style="color:var(--muted)">Vol PCR:</span> <b>${opt.vol_pcr||'—'}</b></span>
      </div>
      <div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:2px">
        ${callW ? `<span style="font-size:9px;color:#22c55e">🛡 Calls:</span> ${callW}` : ''}
        ${putW  ? `<span style="font-size:9px;color:#ef4444;margin-left:6px">⚡ Puts:</span> ${putW}` : ''}
      </div>
    </div>` : '';

  // ── Vol + Skew extra ──
  const tsColor = vs.term_structure==='Backwardation'?'#f59e0b':'#94a3b8';
  const skewSpread = skew.spread??null;
  const volExtra = vs.found ? `
    <div style="margin-top:6px;font-size:10px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:3px">
        ${vix.current ? `<span><span style="color:var(--muted)">VIX:</span> <b>${vix.current}</b> (${vix.regime||''})</span>` : ''}
        ${vix.rank_52w != null ? `<span><span style="color:var(--muted)">VIX Rank:</span> <b>${vix.rank_52w.toFixed(0)}/100</b></span>` : ''}
        ${vs.atm_iv   != null ? `<span><span style="color:var(--muted)">ATM IV:</span> <b>${vs.atm_iv.toFixed(1)}%</b></span>` : ''}
        ${vs.hv20     != null ? `<span><span style="color:var(--muted)">HV20:</span> <b>${vs.hv20}%</b></span>` : ''}
        ${vs.iv_vs_hv != null ? `<span><span style="color:var(--muted)">IV/HV:</span> <b style="color:${vs.iv_vs_hv>1.3?'#f59e0b':vs.iv_vs_hv<0.8?'#22c55e':'var(--text2)'}">${vs.iv_vs_hv}×</b></span>` : ''}
      </div>
      ${vs.term_structure ? `<div style="margin-bottom:2px"><span style="color:var(--muted)">Term Structure:</span> <b style="color:${tsColor}">${vs.term_structure}</b>${vs.ts_spread!=null?` (${vs.ts_spread>0?'+':''}${vs.ts_spread}%)`:''}</div>` : ''}
      ${skewSpread!=null ? `<div><span style="color:var(--muted)">Put/Call Skew:</span> <b style="color:${skewSpread>6?'#f59e0b':skewSpread<0?'#22c55e':'#94a3b8'}">${skewSpread>0?'+':''}${skewSpread}%</b>
        (Put IV ${skew.put_iv||'—'}% · Call IV ${skew.call_iv||'—'}%) — ${skew.sentiment||''}</div>` : ''}
      ${vs.vix?.chg_5d!=null ? `<div style="font-size:9px;color:${vix.chg_5d>0?'#ef4444':'#22c55e'};margin-top:2px">VIX ${vix.chg_5d>0?'↑ +':'↓ '}${vix.chg_5d} vs 5d ago ${vix.chg_20d!=null?`· ${vix.chg_20d>0?'↑':'↓'}${Math.abs(vix.chg_20d)} vs 20d`:''}</div>` : ''}
    </div>` : '';

  // ── Grid of 5 sections ──
  const gridHtml = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px;margin-bottom:12px">
    ${_waSectionCard('📋', 'COT Positioning', cot, cotExtra)}
    ${_waSectionCard('📊', 'Futures OI Flow', fut)}
    ${_waSectionCard('📈', 'Options Sentiment', opt, optExtra)}
    ${_waSectionCard('🌡', 'Volatility + Skew', vs, volExtra)}
    ${_waSectionCard('🌐', 'Macro + Breadth', macro.found || breadth.found ? {...macro, found: true} : {found: false}, macroExtra)}
  </div>`;

  return heroHtml + gridHtml;
}


let __waBiasFilter = 'all';
function _waSetBiasFilter(filter) {
  __waBiasFilter = filter || 'all';
  _waApplyBiasFilter();
}
function _waApplyBiasFilter() {
  const root = document.getElementById('wa-result');
  if (!root) return;
  const rows = root.querySelectorAll('tr.wa-row');
  rows.forEach(row => {
    const b = (row.dataset.bias || 'neutral').toLowerCase();
    const show = (__waBiasFilter === 'all' || __waBiasFilter === b);
    row.style.display = show ? '' : 'none';
  });
  const badges = root.querySelectorAll('[data-wa-filter]');
  badges.forEach(btn => {
    const val = btn.dataset.waFilter || 'all';
    btn.style.opacity = (__waBiasFilter === val) ? '1' : '.72';
    btn.style.transform = (__waBiasFilter === val) ? 'translateY(-1px)' : 'none';
  });
}


function _waRenderWatchlist(d) {
  if (!d || d.error) {
    return `<div style="color:#ef4444;padding:12px">Analysis failed: ${d?.error||'unknown'}</div>`;
  }
  const s = d.summary || {};
  const rows = Array.isArray(d.rows) ? d.rows : [];
  const curFilter = (__waBiasFilter || 'all');
  const badge = (label, value, color) => `<span style="display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border:1px solid ${color};border-radius:999px;font-size:11px;color:${color};font-weight:700">${label} ${value}</span>`;
  const btn = (label, value, color, count) => `<button type="button" data-wa-filter="${value}" onclick="_waSetBiasFilter('${value}')" style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border:1px solid ${color};border-radius:999px;font-size:11px;color:${color};font-weight:800;background:${curFilter===value?'rgba(99,102,241,.16)':'transparent'};transition:all .15s">${label} ${count}</button>`;
  const fmt = (v, d='—') => (v === null || v === undefined || v === '' || Number.isNaN(v) ? d : (typeof v === 'number' ? (Math.abs(v) >= 10 || Number.isInteger(v) ? String(v) : v.toFixed(2)) : String(v)));
  const esc = (v) => String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  const biasColor = (b) => {
    const x = String(b||'').toLowerCase();
    if (x.includes('strong bull') || x.includes('bullish')) return '#22c55e';
    if (x.includes('strong bear') || x.includes('bearish')) return '#ef4444';
    return '#94a3b8';
  };
  const rowsHtml = rows.map(r => {
    const b = r.bias || 'Neutral';
    const c = biasColor(b);
    const strat = Array.isArray(r.detail) ? r.detail.slice(0,2).join(' · ') : (r.detail || '');
    const expBlocks = Array.isArray(r.expiry_profile) ? r.expiry_profile : [];
    const expHtml = expBlocks.length ? expBlocks.map(e => {
      const bias = esc(e.bias || 'Neutral');
      const pcr = fmt(e.pcr);
      const oiChg = fmt(e.oi_change_pct);
      const dte = e.dte != null ? `${e.dte}d` : '—';
      const pwall = e.put_wall != null ? fmt(e.put_wall) : '—';
      const cwall = e.call_wall != null ? fmt(e.call_wall) : '—';
      const hint  = esc(e.trade_hint || '');
      return `<div style="line-height:1.35;margin-bottom:6px">
        <div style="font-weight:800;color:var(--text);white-space:nowrap">${esc(e.expiry || '—')} · ${dte} · <span style="color:${bias.toLowerCase().includes('bull') ? '#22c55e' : bias.toLowerCase().includes('bear') ? '#ef4444' : '#94a3b8'}">${bias}</span></div>
        <div style="color:var(--text2)">P/W ${pwall} · C/W ${cwall} · PCR ${pcr} · ΔOI ${oiChg}%</div>
        <div style="color:var(--muted)">${hint}</div>
      </div>`;
    }).join('') : '—';
    const sym = esc(r.symbol || '—');
    const symLink = r.symbol ? `<a href="?tab=aggregate&symbol=${encodeURIComponent(String(r.symbol).trim().toUpperCase())}" style="color:${c};font-weight:900;text-decoration:none" title="Open aggregate view">${sym}</a>` : sym;
    return `<tr class="wa-row wa-row-${String(b).toLowerCase().includes('bull') ? 'bull' : String(b).toLowerCase().includes('bear') ? 'bear' : 'neutral'}" data-bias="${String(b).toLowerCase().includes('bull') ? 'bull' : String(b).toLowerCase().includes('bear') ? 'bear' : 'neutral'}" style="border-top:1px solid var(--border)">
      <td style="padding:8px 10px;font-weight:800;color:${c}">${symLink}</td>
      <td style="padding:8px 10px;font-weight:800">${fmt(r.spot)}</td>
      <td style="padding:8px 10px;color:${c};font-weight:700">${b}</td>
      <td style="padding:8px 10px;font-weight:800">${fmt(r.weekly_score)}</td>
      <td style="padding:8px 10px">${fmt(r.options_score)}</td>
      <td style="padding:8px 10px">${fmt(r.vol_score)}</td>
      <td style="padding:8px 10px">${fmt(r.pcr)}</td>
      <td style="padding:8px 10px">${fmt(r.iv_rank)}</td>
      <td style="padding:8px 10px">${fmt(r.iv_change_pct)}%</td>
      <td style="padding:8px 10px">${fmt(r.vix_chg_5d)}</td>
      <td style="padding:8px 10px;white-space:normal;min-width:340px">${expHtml}</td>
      <td style="padding:8px 10px;font-size:11px;color:var(--text2)">${esc(strat)}</td>
    </tr>`;
  }).join('');
  const summaryHtml = `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:12px;padding:12px 14px;border:1px solid var(--border);border-radius:10px;background:linear-gradient(135deg,rgba(99,102,241,.08),rgba(0,0,0,0))">
      <div>
        <div style="font-size:18px;font-weight:900">${d.watchlist_name || 'Watchlist'} <span style="font-size:12px;color:var(--muted)">· ${rows.length} symbols</span></div>
        <div style="font-size:11px;color:var(--muted)">Generated ${d.generated_at || ''}${d.elapsed_s != null ? ` · ${d.elapsed_s}s` : ''}</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button type="button" data-wa-filter="all" onclick="_waSetBiasFilter('all')" style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border:1px solid #818cf8;border-radius:999px;font-size:11px;color:#818cf8;font-weight:800;background:${curFilter==='all'?'rgba(99,102,241,.16)':'transparent'}">All ${s.symbol_count ?? rows.length}</button>
        ${btn('Bull', 'bull', '#22c55e', s.bull_count ?? 0)}
        ${btn('Bear', 'bear', '#ef4444', s.bear_count ?? 0)}
        ${btn('Neutral', 'neutral', '#94a3b8', s.neutral_count ?? 0)}
      </div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">
      ${badge('Avg Score', fmt(s.avg_score), '#818cf8')}
      ${badge('Avg Price', fmt(s.avg_price_score), '#22c55e')}
      ${badge('Avg PCR', fmt(s.avg_pcr), '#f59e0b')}
      ${badge('Avg IV Rank', fmt(s.avg_iv_rank), '#22d3ee')}
      ${badge('Avg IV Δ%', fmt(s.avg_iv_change_pct), '#fb7185')}
      ${badge('Avg IV vs HV', fmt(s.avg_iv_vs_hv), '#a78bfa')}
      ${badge('Avg VIX Δ5D', fmt(s.avg_vix_chg_5d), '#f97316')}
      ${badge('COT', fmt(s.cot_score), '#34d399')}
      ${badge('Futures', fmt(s.futures_score), '#60a5fa')}
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px">${esc(s.cot_signal || '')}${s.cot_signal && s.futures_signal ? ' · ' : ''}${esc(s.futures_signal || '')}</div>`;
  return `${summaryHtml}<div style="overflow:auto;border:1px solid var(--border);border-radius:10px">
    <table style="width:100%;border-collapse:collapse;font-size:12px;min-width:1320px">
      <thead><tr style="background:rgba(255,255,255,.05);border-bottom:2px solid var(--border)">
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Symbol</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Spot</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Bias</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Score</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Options</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Vol</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">PCR</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">IV Rank</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">IV Δ%</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">VIX Δ5D</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Expiries / Walls</th>
        <th style="padding:8px 10px;text-align:left;font-size:11px;color:var(--muted);font-weight:700">Notes</th>
      </tr></thead>
      <tbody>${rowsHtml || '<tr><td colspan="12" style="padding:16px;color:var(--muted)">No results.</td></tr>'}</tbody>
    </table>
  </div>`;
}


