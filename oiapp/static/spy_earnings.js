// spy_earnings.js  v3 — no API key required, rule-based analysis
'use strict';

// ═══════════════════════════════════════════════════════════════
// UTILITIES
// ═══════════════════════════════════════════════════════════════
const _N  = (v,dec=2) => (v==null)?'—': typeof v==='number'?(dec===0?Math.round(v).toLocaleString():v.toFixed(dec)):String(v);
const _$  = (v,dec=2) => v==null?'—':`$${_N(v,dec)}`;
const _pct= (v,dec=1) => v==null?'—':`${v>=0?'+':''}${_N(v,dec)}%`;
const _B  = (v,dec=2) => v==null?'—':`$${_N(v,dec)}B`;

function _chip(label, val, color='var(--text)', bg='var(--card)') {
  return `<div class="stat-chip" style="border-top:2px solid ${color}">
    <div class="s-label">${label}</div>
    <div class="s-val" style="color:${color}">${val}</div>
  </div>`;
}

function _badge(text, color='#3b82f6') {
  return `<span style="background:${color}22;border:1px solid ${color}55;color:${color};
    padding:2px 9px;border-radius:4px;font-size:11px;font-weight:700;
    white-space:nowrap;letter-spacing:.04em">${text||'—'}</span>`;
}

function _dirBadge(dir, val) {
  const cm = {
    BEAT:'#22c55e', MISS:'#ef4444', IN_LINE:'#f59e0b',
    ACCELERATING:'#22c55e', STABLE:'#94a3b8',
    DECELERATING:'#f59e0b', DECLINING:'#ef4444',
    EXPANDING:'#22c55e', CONTRACTING:'#ef4444',
    HIGH:'#22c55e', MEDIUM:'#f59e0b', LOW:'#ef4444',
  };
  const label = (dir||'—').replace(/_/g,' ');
  const c = cm[dir] || '#64748b';
  return _badge(val ? `${label} (${val})` : label, c);
}

const OUTLOOK_COLOR = {
  BULLISH:'#22c55e', MILDLY_BULLISH:'#4ade80',
  NEUTRAL:'#f59e0b', MILDLY_BEARISH:'#f87171', BEARISH:'#ef4444'
};
const OUTLOOK_ICON  = {
  BULLISH:'▲▲', MILDLY_BULLISH:'▲', NEUTRAL:'→',
  MILDLY_BEARISH:'▼', BEARISH:'▼▼'
};

function _outlookCard(a, d) {
  // d = full earnings data object, a = analysis sub-object
  const o  = a.market_outlook || 'NEUTRAL';
  const c  = OUTLOOK_COLOR[o] || '#f59e0b';
  const ic = OUTLOOK_ICON[o]  || '→';
  const tgtC = (a.upside_downside_pct||0)>=0?'#22c55e':'#ef4444';
  const score = a.outlook_score||0;

  // Score bar (−10 to +10, mapped to 0–100%)
  const barPct = Math.round(((score + 10) / 20) * 100);
  const barC   = score>=4?'#22c55e':score>=0?'#f59e0b':'#ef4444';

  // Earnings read-out context from the data
  const lastQ   = (d?.eps_history||[])[0];
  const revLast = (d?.rev_history||[])[0];
  const beatStr = lastQ
    ? `Last quarter: EPS ${lastQ.eps_actual!=null?'$'+lastQ.eps_actual:'—'} vs est ${lastQ.eps_expected!=null?'$'+lastQ.eps_expected:'—'} (${lastQ.surprise_pct!=null?(lastQ.surprise_pct>0?'+':'')+lastQ.surprise_pct+'%':'—'})`
    : '';

  return `
  <div style="background:${c}0d;border:1px solid ${c}55;border-radius:8px;padding:16px 18px;margin-bottom:14px">

    <!-- Header row -->
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:24px;font-weight:800;color:${c}">${ic}</span>
        <div>
          <div style="font-size:16px;font-weight:800;color:${c};letter-spacing:.05em">${o.replace(/_/g,' ')}</div>
          <div style="font-size:10px;color:var(--muted)">${a.market_outlook_horizon||'1-3 months'} horizon</div>
        </div>
      </div>
      <!-- Score gauge -->
      <div style="margin-left:auto;text-align:right">
        <div style="font-size:9px;color:var(--muted);letter-spacing:.1em;margin-bottom:3px">OUTLOOK SCORE</div>
        <div style="font-size:18px;font-weight:800;color:${barC}">${score>0?'+':''}${score}<span style="font-size:11px;font-weight:400;color:var(--muted)">/10</span></div>
        <div style="width:90px;height:4px;background:var(--border);border-radius:2px;margin-top:3px">
          <div style="width:${barPct}%;height:100%;background:${barC};border-radius:2px"></div>
        </div>
      </div>
    </div>

    <!-- Reasoning -->
    <p style="font-size:12px;color:var(--text);line-height:1.8;margin:0 0 10px;padding:10px 12px;
       background:rgba(255,255,255,.04);border-radius:5px;border-left:3px solid ${c}55">
      ${a.market_outlook_reason||''}
    </p>

    <!-- Earnings read-out context -->
    ${beatStr?`<div style="font-size:11px;color:var(--muted);margin-bottom:10px">📋 ${beatStr}</div>`:''}

    <!-- Key metrics row -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
      ${[
        ['My Target', a.price_target_my!=null?'$'+_N(a.price_target_my):'—', tgtC],
        ['Upside/Dwn', a.upside_downside_pct!=null?_pct(a.upside_downside_pct):'—', tgtC],
        ['Post-Earn Move', a.post_earnings_move_est||'—', '#a855f7'],
        ['Beat Rate', a.beat_rate_pct!=null?a.beat_rate_pct+'%':'—',
          a.beat_rate_pct>=75?'#22c55e':a.beat_rate_pct>=50?'#f59e0b':'#ef4444'],
        ['Earn Quality', a.earnings_quality||'—',
          {HIGH:'#22c55e',MEDIUM:'#f59e0b',LOW:'#ef4444'}[a.earnings_quality]||'#64748b'],
      ].map(([l,v,co])=>`<div style="background:var(--surface);border:1px solid ${co}33;border-top:2px solid ${co};border-radius:5px;padding:5px 10px;min-width:100px">
        <div style="font-size:8px;color:var(--muted);letter-spacing:.1em;margin-bottom:2px">${l}</div>
        <div style="font-size:12px;font-weight:700;color:${co}">${v}</div>
      </div>`).join('')}
    </div>

    <!-- Next quarter direction badges -->
    <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px;font-size:11px">
      <span style="color:var(--muted)">NEXT QTR:</span>
      ${[
        ['EPS', a.next_eps_direction],
        ['Revenue', a.next_rev_direction],
        ['Rev Trend', a.revenue_trend],
        ['Margin', a.margin_trend],
      ].map(([lbl,dir])=>{
        const dc={BEAT:'#22c55e',MISS:'#ef4444',IN_LINE:'#f59e0b',
          ACCELERATING:'#22c55e',STABLE:'#94a3b8',DECELERATING:'#f59e0b',DECLINING:'#ef4444',
          EXPANDING:'#22c55e',CONTRACTING:'#ef4444'}[dir]||'#64748b';
        return `<span style="font-size:9px;color:var(--muted)">${lbl}:</span>
          <span style="background:${dc}18;border:1px solid ${dc}44;color:${dc};padding:1px 7px;border-radius:3px;font-size:10px;font-weight:700">${(dir||'—').replace('_',' ')}</span>`;
      }).join('')}
    </div>

    <!-- Summary -->
    <div style="font-size:11px;color:var(--muted);font-style:italic;border-top:1px solid ${c}22;padding-top:10px;line-height:1.7">
      ${a.summary||''}
    </div>
  </div>`;
}

// ═══════════════════════════════════════════════════════════════
// SPY STRATEGIES TAB
// ═══════════════════════════════════════════════════════════════
async function _loadSPYSymbols() {
  const sel = document.getElementById('spy-symbol'); if(!sel) return;
  try {
    const d = await fetch('/api/symbols').then(r=>r.json());
    const syms = ['SPY',...(d.symbols||[]).filter(s=>s!=='SPY')];
    sel.innerHTML = syms.map(s=>`<option value="${s}"${s==='SPY'?'selected':''}>${s}</option>`).join('');
  } catch { sel.innerHTML='<option value="SPY">SPY</option>'; }
}

function _stratCard(s) {
  const bC = s.bias==='Bullish'?'#22c55e':s.bias==='Bearish'?'#ef4444':'#f59e0b';
  const tC = s.type==='credit'?'#22c55e':'#3b82f6';
  const pC = s.pop>=70?'#22c55e':s.pop>=55?'#f59e0b':'#ef4444';
  return `
  <div style="background:var(--card);border:1px solid var(--border);border-left:4px solid ${bC};
      border-radius:8px;padding:14px 16px;margin-bottom:12px">
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px;margin-bottom:10px">
      <div>
        <span style="font-size:15px;font-weight:700;color:var(--text)">${s.name}</span>
        <span style="margin-left:8px;font-size:11px;color:var(--muted)">${s.dte_label} · Exp ${s.expiry}</span>
      </div>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        ${_badge(s.bias,bC)} ${_badge(s.type.toUpperCase(),tC)}
      </div>
    </div>
    <div style="font-family:monospace;font-size:13px;color:#a855f7;font-weight:700;
      background:#a855f720;border:1px solid #a855f744;border-radius:5px;
      padding:8px 12px;margin-bottom:12px">${s.legs}</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
      ${[
        [s.type==='credit'?'EST. CREDIT':'EST. DEBIT', s.est_credit, tC],
        ['MAX GAIN', s.max_gain, '#22c55e'],
        ['MAX LOSS', s.max_loss, '#ef4444'],
        ['PoP',      s.pop+'%',  pC],
        ['R:R',      s.rr,       'var(--text)'],
      ].map(([l,v,c])=>`
        <div style="flex:1;min-width:90px;background:${c}12;border:1px solid ${c}33;border-radius:4px;padding:6px 10px">
          <div style="font-size:8px;color:${c};letter-spacing:.1em;margin-bottom:3px">${l}</div>
          <div style="font-size:13px;font-weight:700;color:var(--text)">${v}</div>
        </div>`).join('')}
    </div>
    <div style="font-size:11px;color:var(--muted);border-left:2px solid ${bC}55;
      padding-left:8px;line-height:1.6;margin-bottom:8px">${s.rationale}</div>
    <div style="font-size:11px;color:#f59e0b;background:#f59e0b10;
      border:1px solid #f59e0b22;border-radius:4px;padding:6px 10px">
      ⊕ <b>MANAGE:</b> ${s.manage}
    </div>
  </div>`;
}

function _taChip(label, val, color) {
  return `<div style="background:var(--card);border:1px solid ${color}44;border-top:2px solid ${color};
      border-radius:5px;padding:6px 10px;min-width:90px">
    <div style="font-size:8px;color:var(--muted);letter-spacing:.1em;margin-bottom:2px">${label}</div>
    <div style="font-size:13px;font-weight:700;color:${color}">${val}</div>
  </div>`;
}

function _wallRow(walls) {
  if(!walls?.length) return '<p style="color:var(--muted);font-size:11px;padding:6px 0">No OI walls — run scheduler first.</p>';
  return walls.map(([s,oi])=>
    `<div style="display:flex;justify-content:space-between;padding:5px 8px;border-bottom:1px solid rgba(255,255,255,.05)">
       <span style="font-weight:700;font-family:monospace">$${s}</span>
       <span style="color:var(--muted);font-size:11px">${oi.toLocaleString()} OI</span>
     </div>`
  ).join('');
}

function _renderTA(ta) {
  const panel=document.getElementById('spy-ta-panel');
  const grid =document.getElementById('spy-ta-grid');
  if(!ta||!panel||!grid) return;
  document.querySelectorAll('.spy-alert').forEach(el=>el.remove());
  const diff=parseFloat(ta.rsi_ema_diff||0);
  const diffC=diff>=20?'#ef4444':diff<=-20?'#22c55e':Math.abs(diff)>=10?'#f59e0b':'#64748b';
  const momC ={OVERBOUGHT:'#ef4444',OVERSOLD:'#22c55e',
               ELEVATED:'#f59e0b',DEPRESSED:'#3b82f6',NEUTRAL:'#64748b'}[ta.momentum]||'#64748b';
  const trendC=ta.trend?.includes('UP')?'#22c55e':ta.trend?.includes('DOWN')?'#ef4444':'#f59e0b';
  const chgC=(ta.chg_pct||0)>=0?'#22c55e':'#ef4444';
  grid.innerHTML=[
    _taChip('PRICE',          `$${_N(ta.price)}`,            '#e2e8f0'),
    _taChip('DAY CHG',        _pct(ta.chg_pct),              chgC),
    _taChip('RSI-14',         _N(ta.rsi,1),                  momC),
    _taChip('EMA90(RSI)',     _N(ta.ema90_rsi,1),            '#64748b'),
    _taChip('RSI−EMA DIFF',  _N(ta.rsi_ema_diff,1),         diffC),
    _taChip('MOMENTUM',       ta.momentum||'—',               momC),
    _taChip('TREND',          ta.trend||'—',                  trendC),
    _taChip('ATR-14',         `$${_N(ta.atr)}`,              '#94a3b8'),
    _taChip('BB%B',           `${_N(ta.bb_pct)}%`,           parseFloat(ta.bb_pct)>80?'#ef4444':parseFloat(ta.bb_pct)<20?'#22c55e':'#f59e0b'),
    _taChip('IV RANK',        `${ta.iv_rank}/100`,            ta.iv_rank>=70?'#ef4444':ta.iv_rank>=50?'#f59e0b':'#3b82f6'),
    _taChip('EMA-20',         `$${_N(ta.ema20)}`,            '#64748b'),
    _taChip('EMA-50',         `$${_N(ta.ema50)}`,            '#64748b'),
  ].join('');
  if(Math.abs(diff)>=20){
    const isOB=diff>0, c=isOB?'#ef4444':'#22c55e';
    const a=document.createElement('div');
    a.className='spy-alert';
    a.style.cssText=`background:${c}20;border:1px solid ${c};border-radius:6px;
      padding:8px 14px;margin-bottom:10px;font-size:12px;font-weight:700;color:${c}`;
    a.innerHTML=`${isOB?'⚠ OVERBOUGHT':'◈ OVERSOLD'} SIGNAL: RSI14(${ta.rsi}) − EMA90(RSI)(${ta.ema90_rsi}) = ${ta.rsi_ema_diff}`;
    grid.parentNode.insertBefore(a, grid);
  }
  panel.style.display='';
}

function _renderWalls(walls) {
  const panel=document.getElementById('spy-walls-panel'); if(!walls||!panel) return;
  document.getElementById('spy-put-walls').innerHTML =_wallRow(walls.top_put_walls||[]);
  document.getElementById('spy-call-walls').innerHTML=_wallRow(walls.top_call_walls||[]);
  const old=panel.querySelector('.spy-wall-note'); if(old) old.remove();
  const n=document.createElement('div');
  n.className='spy-wall-note';
  n.style.cssText='font-size:11px;color:var(--muted);margin-top:8px;padding:0 4px';
  n.innerHTML=`Gamma wall: <b style="color:var(--text)">$${walls.gamma_wall}</b> &nbsp;·&nbsp;
    Support: <b style="color:#22c55e">$${walls.support}</b> &nbsp;·&nbsp;
    Resistance: <b style="color:#ef4444">$${walls.resistance}</b> &nbsp;·&nbsp;
    Strike interval: ${walls.interval}`;
  panel.appendChild(n);
  panel.style.display='';
}

async function runSPYStrategy(mode) {
  const sym=document.getElementById('spy-symbol')?.value||'SPY';
  const statEl=document.getElementById('spy-status');
  ['spy-strategies-daily','spy-strategies-weekly','spy-ta-panel','spy-walls-panel']
    .forEach(id=>{const el=document.getElementById(id);if(el)el.style.display='none';});
  document.querySelectorAll('.spy-alert,.spy-wall-note').forEach(el=>el.remove());
  if(statEl) statEl.textContent=`⏳ Loading ${sym} ${mode} strategy…`;
  try {
    if(mode==='daily'||mode==='both'){
      const d=await fetch(`/spy/daily?symbol=${sym}`).then(r=>r.json());
      if(d.error) throw new Error(d.error);
      _renderTA(d.ta); _renderWalls(d.walls);
      document.getElementById('spy-strat-daily-cards').innerHTML=
        (d.strategies||[]).map(_stratCard).join('')||'<p style="color:var(--muted)">No strategies generated.</p>';
      document.getElementById('spy-strategies-daily').style.display='';
    }
    if(mode==='weekly'||mode==='both'){
      const d=await fetch(`/spy/weekly?symbol=${sym}`).then(r=>r.json());
      if(d.error) throw new Error(d.error);
      if(mode==='weekly'){_renderTA(d.ta);_renderWalls(d.walls);}
      document.getElementById('spy-strat-weekly-cards').innerHTML=
        (d.strategies||[]).map(_stratCard).join('')||'<p style="color:var(--muted)">No strategies generated.</p>';
      document.getElementById('spy-strategies-weekly').style.display='';
    }
    if(statEl) statEl.innerHTML=`<span style="color:var(--green)">✅ ${sym} strategies ready</span>`;
  } catch(e) {
    if(statEl) statEl.innerHTML=`<span style="color:var(--red)">❌ ${e.message}</span>`;
  }
}
window.runSPYStrategy=runSPYStrategy;


// ═══════════════════════════════════════════════════════════════
// EARNINGS TAB
// ═══════════════════════════════════════════════════════════════
async function _loadEarnSymbols(preferredSymbol) {
  const sel = document.getElementById('earn-symbol');
  if (!sel) return;
  const wlId = document.getElementById('earn-watchlist')?.value || '';
  const prev = (preferredSymbol || sel.value || '').toUpperCase();
  try {
    const url = '/earnings/symbols' + (wlId ? `?watchlist_id=${encodeURIComponent(wlId)}` : '');
    const d = await fetch(url).then(r=>r.json());
    const symbols = (d.symbols || []).map(s => String(s).toUpperCase()).filter(Boolean);
    if (!symbols.length) {
      sel.innerHTML = '<option value="">No symbols</option>';
      sel.value = '';
      return;
    }
    sel.innerHTML = symbols.map(s => `<option value="${s}">${s}</option>`).join('');
    sel.value = symbols.includes(prev) ? prev : symbols[0];
  } catch {
    sel.innerHTML = '<option value="SPY">SPY</option>';
    sel.value = 'SPY';
  }
}

function _epsRow(h) {
  const sp=parseFloat(h.surprise_pct||0);
  const beat=sp>=0;
  const spC=sp>5?'#22c55e':sp>0?'#4ade80':sp<-5?'#ef4444':'#f87171';
  return `<tr>
    <td>${h.date||'—'}</td>
    <td class="text-end" style="color:var(--muted)">${_N(h.eps_expected)}</td>
    <td class="text-end" style="font-weight:700;color:var(--text)">${_N(h.eps_actual)}</td>
    <td class="text-end" style="color:${spC};font-weight:700">
      ${h.surprise_pct!=null?(sp>=0?'+':'')+_N(sp,1)+'%':'—'}</td>
    <td class="text-center">
      ${beat
        ? '<span style="color:#22c55e;font-weight:700;font-size:12px">✓ BEAT</span>'
        : '<span style="color:#ef4444;font-weight:700;font-size:12px">✗ MISS</span>'}</td>
  </tr>`;
}

function _revActualRow(r, revEstAvg) {
  // Show actual revenue; if estimate exists for this period compare it
  const rev = r.revenue;
  return `<tr>
    <td style="color:var(--muted)">${r.period?r.period.slice(0,7):'—'}</td>
    <td class="text-end" style="font-weight:700;color:var(--text)">
      ${rev!=null?_B(rev):'—'}</td>
  </tr>`;
}

async function runEarningsAnalysis() {
  const sym=document.getElementById('earn-symbol')?.value; if(!sym) return;
  // Hide pre-earnings screener, show individual analysis
  if (typeof _earnShowSection === 'function') _earnShowSection('detail');
  const statEl=document.getElementById('earn-status');
  const resEl =document.getElementById('earn-results');
  if(statEl) statEl.innerHTML=`<span style="color:var(--muted)">⏳ Fetching ${sym} data…</span>`;
  resEl.style.display='none';

  try {
    const d=await fetch(`/earnings/analysis?symbol=${sym}`).then(r=>r.json());
    if(d.error) throw new Error(d.error);
    const a = d.analysis || {};

    // ── Header ───────────────────────────────────────────────
    const chgC=(d.day_chg_pct||0)>=0?'#22c55e':'#ef4444';
    document.getElementById('earn-company-name').textContent=`${d.company_name} (${d.symbol})`;
    document.getElementById('earn-meta').innerHTML=
      `<span style="color:var(--muted)">${d.sector||''} ${d.industry?'· '+d.industry:''}</span>`;

    document.getElementById('earn-key-metrics').innerHTML=[
      _chip('Price',       `${_$(d.price_now)} ${_pct(d.day_chg_pct)}`, chgC),
      _chip('52W Range',   `${_$(d.price_52l)} – ${_$(d.price_52h)}`,  '#94a3b8'),
      _chip('Market Cap',  _B(d.market_cap),                            '#3b82f6'),
      _chip('P/E (Fwd)',   _N(d.pe_ratio),                              '#94a3b8'),
      _chip('EPS TTM',     _$(d.eps_ttm),                               '#e2e8f0'),
      _chip('Rev TTM',     _B(d.revenue_ttm),                           '#e2e8f0'),
      _chip('Rev Growth',  _pct(d.revenue_growth),   (d.revenue_growth||0)>=0?'#22c55e':'#ef4444'),
      _chip('EPS Growth',  _pct(d.earnings_growth),  (d.earnings_growth||0)>=0?'#22c55e':'#ef4444'),
      _chip('Margin',      _pct(d.profit_margin,1),  (d.profit_margin||0)>15?'#22c55e':'#f59e0b'),
      _chip('IV Est.',     d.iv_est_pct?d.iv_est_pct+'%':'—',           '#a855f7'),
    ].join('');

    // ── Market Outlook card ───────────────────────────────────
    const outlookEl=document.getElementById('earn-outlook-card');
    if(outlookEl) outlookEl.innerHTML=_outlookCard(a, d);

    // ── Earnings quality + streak chips ──────────────────────
    const qEl=document.getElementById('earn-quality-row');
    if(qEl) qEl.innerHTML=[
      _chip('EPS Beat Streak',  a.beat_miss_streak||'—',                                '#94a3b8'),
      _chip('Beat Rate',        a.beat_rate_pct!=null?a.beat_rate_pct+'%':'—',          a.beat_rate_pct>=75?'#22c55e':a.beat_rate_pct>=50?'#f59e0b':'#ef4444'),
      _chip('Avg EPS Surprise', a.avg_eps_surprise_pct!=null?_pct(a.avg_eps_surprise_pct):'—', (a.avg_eps_surprise_pct||0)>=0?'#22c55e':'#ef4444'),
      _chip('Earn. Quality',    a.earnings_quality||'—',                                {HIGH:'#22c55e',MEDIUM:'#f59e0b',LOW:'#ef4444'}[a.earnings_quality]||'#64748b'),
      _chip('Rev Trend',        a.revenue_trend||'—',                                  {ACCELERATING:'#22c55e',STABLE:'#94a3b8',DECELERATING:'#f59e0b',DECLINING:'#ef4444'}[a.revenue_trend]||'#64748b'),
      _chip('Margin Trend',     a.margin_trend||'—',                                   {EXPANDING:'#22c55e',STABLE:'#94a3b8',CONTRACTING:'#ef4444'}[a.margin_trend]||'#64748b'),
      _chip('Post-Earn. Move',  a.post_earnings_move_est||'—',                         '#a855f7'),
      _chip('My Price Target',  a.price_target_my?_$(a.price_target_my):'—',           (a.upside_downside_pct||0)>=0?'#22c55e':'#ef4444'),
      _chip('Upside / Dwn',     a.upside_downside_pct!=null?_pct(a.upside_downside_pct):'—', (a.upside_downside_pct||0)>=0?'#22c55e':'#ef4444'),
    ].join('');

    // ── EPS History table ─────────────────────────────────────
    const eps=d.eps_history||[];
    document.getElementById('earn-eps-tbl').innerHTML=eps.length
      ?`<table class="data-tbl">
          <thead><tr>
            <th>Quarter</th><th class="text-end">EPS Est.</th>
            <th class="text-end">EPS Actual</th><th class="text-end">Surprise</th><th>Result</th>
          </tr></thead>
          <tbody>${eps.map(_epsRow).join('')}</tbody>
        </table>`
      :'<p style="color:var(--muted);font-size:12px;padding:8px">No EPS history from yfinance.</p>';

    // ── Next Quarter Estimates ────────────────────────────────
    const nd=d.next_earnings_date;
    let daysStr='';
    if(nd){try{const dd=Math.round((new Date(nd)-new Date())/86400000);daysStr=` — ${dd} days away`;}catch{}}
    document.getElementById('earn-next-card').innerHTML=`
      <div style="font-size:15px;font-weight:700;color:var(--accent);margin-bottom:12px">
        ${nd||'Date not available'}${daysStr}
      </div>
      <table class="data-tbl">
        <thead><tr>
          <th>Metric</th>
          <th class="text-end">Analyst Low</th>
          <th class="text-end">Analyst Avg</th>
          <th class="text-end">Analyst High</th>
          <th class="text-end" style="color:#a855f7">My Estimate</th>
          <th class="text-end">YoY</th>
        </tr></thead>
        <tbody>
          <tr>
            <td style="font-weight:700">EPS ($)</td>
            <td class="text-end" style="color:var(--muted)">${_N(d.next_eps_est_low)}</td>
            <td class="text-end" style="color:var(--text);font-weight:700">${_N(d.next_eps_est_avg)}</td>
            <td class="text-end" style="color:var(--muted)">${_N(d.next_eps_est_high)}</td>
            <td class="text-end" style="color:#a855f7;font-weight:700">${a.next_eps_my_est!=null?_N(a.next_eps_my_est):'—'}</td>
            <td class="text-end">${_dirBadge(a.next_eps_direction)}</td>
          </tr>
          <tr>
            <td style="font-weight:700">Revenue ($B)</td>
            <td class="text-end" style="color:var(--muted)">${_N(d.next_rev_est_low)}</td>
            <td class="text-end" style="color:var(--text);font-weight:700">${_N(d.next_rev_est_avg)}</td>
            <td class="text-end" style="color:var(--muted)">${_N(d.next_rev_est_high)}</td>
            <td class="text-end" style="color:#a855f7;font-weight:700">${a.next_rev_my_est!=null?_N(a.next_rev_my_est):'—'}</td>
            <td class="text-end">${_dirBadge(a.next_rev_direction)}</td>
          </tr>
          ${d.next_rev_yago!=null?`
          <tr style="opacity:.6">
            <td colspan="2" style="font-size:11px;color:var(--muted)">Year-ago revenue</td>
            <td class="text-end" style="font-size:11px;color:var(--muted)">${_B(d.next_rev_yago)}</td>
            <td colspan="3"></td>
          </tr>`:''}
        </tbody>
      </table>`;

    // ── Revenue actuals table ─────────────────────────────────
    const rev=d.rev_history||[];
    document.getElementById('earn-rev-tbl').innerHTML=rev.length
      ?`<table class="data-tbl">
          <thead><tr>
            <th>Quarter</th><th class="text-end">Revenue ($B)</th>
          </tr></thead>
          <tbody>${rev.map(r=>_revActualRow(r)).join('')}</tbody>
        </table>`
      :'<p style="color:var(--muted);font-size:12px;padding:8px">No revenue data from yfinance.</p>';

    // ── Post-earnings price moves ─────────────────────────────
    const movesEl=document.getElementById('earn-moves-tbl');
    if(movesEl){
      const moves=d.post_earnings_moves||[];
      if(moves.length){
        movesEl.innerHTML=`<table class="data-tbl">
          <thead><tr><th>#</th><th class="text-end">Move</th></tr></thead>
          <tbody>${moves.map((m,i)=>`<tr>
            <td style="color:var(--muted)">Q-${i+1}</td>
            <td class="text-end" style="font-weight:700;color:${m>=0?'#22c55e':'#ef4444'}">
              ${m>=0?'+':''}${m}%</td>
          </tr>`).join('')}</tbody>
        </table>`;
      } else {
        movesEl.innerHTML='<p style="color:var(--muted);font-size:11px">No move data yet.</p>';
      }
    }

    // ── Catalysts + Risks + Options ───────────────────────────
    document.getElementById('earn-catalysts').innerHTML=
      (a.key_catalysts||[]).map(c=>`<li>${c}</li>`).join('')||'<li style="color:var(--muted)">—</li>';
    document.getElementById('earn-risks').innerHTML=
      (a.key_risks||[]).map(r=>`<li>${r}</li>`).join('')||'<li style="color:var(--muted)">—</li>';
    document.getElementById('earn-options-impl').textContent=a.options_implication||'—';

    // ── Recommendation Model: long-term vs short-term ─────────
    const rm = d.recommendation_model;
    const rmCard = document.getElementById('earn-recommendation-card');
    if (rm && rmCard) {
      rmCard.style.display = 'block';
      const factorOrder = ['business_quality','long_term_growth','analyst_sentiment','earnings','guidance','near_term_momentum'];
      const scoreColor = s => s>=7.5?'#22c55e':s>=6?'#84cc16':s>=4.5?'#f59e0b':s>=3?'#f97316':'#ef4444';
      document.getElementById('earn-rec-model-factors').innerHTML = factorOrder.map(k => {
        const f = rm.factors[k]; if(!f) return '';
        return `<tr style="border-bottom:1px solid var(--border)">
          <td style="padding:6px 4px;color:var(--muted)">${f.label}</td>
          <td style="padding:6px 4px;text-align:right;font-weight:700;color:${scoreColor(f.score)}">${f.score.toFixed(1)}/10</td>
        </tr>`;
      }).join('');
      const labelColor = lbl => lbl==='Bullish'?'#22c55e':lbl==='Mildly Bullish'?'#84cc16':lbl==='Neutral'?'#f59e0b':lbl==='Mildly Bearish'?'#f97316':'#ef4444';
      document.getElementById('earn-rec-model-longterm').innerHTML = `
        <div style="background:${labelColor(rm.long_term_label)}18;border:1px solid ${labelColor(rm.long_term_label)}44">
          <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Long-term (Quality, Growth, Analysts)</div>
          <div style="font-size:20px;font-weight:800;color:${labelColor(rm.long_term_label)}">${rm.long_term_score.toFixed(1)}/10 — ${rm.long_term_label}</div>
        </div>`;
      document.getElementById('earn-rec-model-shortterm').innerHTML = `
        <div style="background:${labelColor(rm.short_term_label)}18;border:1px solid ${labelColor(rm.short_term_label)}44">
          <div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">Short-term (Earnings, Guidance, Momentum)</div>
          <div style="font-size:20px;font-weight:800;color:${labelColor(rm.short_term_label)}">${rm.short_term_score.toFixed(1)}/10 — ${rm.short_term_label}</div>
        </div>`;
    } else if (rmCard) {
      rmCard.style.display = 'none';
    }

    // ── Analyst consensus ─────────────────────────────────────
    const recKey=d.recommend_key||'';
    const recC=recKey.includes('buy')?'#22c55e':recKey.includes('sell')?'#ef4444':'#f59e0b';
    const upside=d.analyst_target&&d.price_now
      ? Math.round((d.analyst_target-d.price_now)/d.price_now*100*10)/10 : null;
    document.getElementById('earn-analyst-row').innerHTML=[
      _chip('Consensus',      recKey.toUpperCase()||'—',       recC),
      _chip('Score',          `${_N(d.recommend_mean,2)}/5`,   '#94a3b8'),
      _chip('Analyst Target', _$(d.analyst_target),            '#3b82f6'),
      _chip('Target Upside',  upside!=null?_pct(upside):'—',  upside>0?'#22c55e':'#ef4444'),
      _chip('Target Range',   `${_$(d.analyst_low)} – ${_$(d.analyst_high)}`, '#64748b'),
      d.latest_analyst_action
        ? _chip('Latest Action',
            `${d.latest_analyst_action.firm} → ${d.latest_analyst_action.grade}`,'#94a3b8')
        : '',
    ].join('');

    resEl.style.display='';
    if(statEl) statEl.innerHTML=
      `<span style="color:var(--green)">✅ ${sym} — ${a.data_source||'yfinance'}</span>`;
  } catch(e) {
    if(statEl) statEl.innerHTML=`<span style="color:var(--red)">❌ ${e.message}</span>`;
    console.error(e);
  }
}
window.runEarningsAnalysis=runEarningsAnalysis;

// ═══════════════════════════════════════════════════════════════
// BOOT
// ═══════════════════════════════════════════════════════════════
window.addEventListener('DOMContentLoaded', async ()=>{
  await _loadSPYSymbols();
  await _loadEarnSymbols();
  document.getElementById('earn-symbol')
    ?.addEventListener('keydown', e=>{ if(e.key==='Enter') runEarningsAnalysis(); });
  let _spyRan=false;
  document.querySelectorAll('#tab-bar button[data-tab="spy"]').forEach(btn=>{
    btn.addEventListener('click', ()=>{ if(!_spyRan){_spyRan=true;runSPYStrategy('both');} });
  });
});
