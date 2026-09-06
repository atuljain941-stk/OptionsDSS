// candidate_board_ui.js
// Extracted from app.js -- the Candidate Board page. Includes
// runCandidateBoard() itself, which despite not having a "_cb" prefix
// is clearly part of this same feature (references cb-status/
// cb-summary/cb-longs/cb-shorts DOM elements, builds the candidate
// board) -- caught by checking actual DOM-element references, not just
// function name prefixes, which a naive prefix-only search would have
// missed entirely.

// ── Candidate Board ────────────────────────────────────────────────────────
function _cbNormalizeDir(v) {
  const s = String(v || '').toUpperCase();
  if (/(BULL|LONG|CALL|BUY)/.test(s)) return 'BULLISH';
  if (/(BEAR|SHORT|PUT|SELL)/.test(s)) return 'BEARISH';
  return 'NEUTRAL';
}
function _cbScore(v) {
  const n = Number.parseFloat(v);
  return Number.isFinite(n) ? n : 0;
}
function _cbBestLabel(r) {
  const label = r.setup_type || r.setup || r.source || 'Setup';
  return String(label).replace(/_/g, ' ');
}
function _cbScoreChip(label, value, color) {
  return `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;border:1px solid ${color}44;background:${color}18;color:${color};font-size:11px;font-weight:700;white-space:nowrap">${label} ${value}</span>`;
}
function _cbRow(a) {
  const best = a.bestRow || {};
  const dirColor = a.direction === 'BULLISH' ? '#22c55e' : '#ef4444';
  const dirIcon  = a.direction === 'BULLISH' ? '↑' : '↓';
  const setups = [...(a.setups || [])].slice(0, 4).map(s => _cbScoreChip(s, '', '#818cf8')).join(' ');
  const scanners = [...(a.scanners || [])].slice(0, 4).join(' · ');
  const bestSetup = _cbBestLabel(best);
  const age = best.trend_age ?? a.trend_age ?? '—';
  return `<tr>
    <td style="padding:7px 10px;font-weight:800;color:var(--accent)">${a.symbol}</td>
    <td style="padding:7px 10px;white-space:nowrap"><span style="color:${dirColor};font-weight:800">${dirIcon} ${a.direction}</span></td>
    <td style="padding:7px 10px;font-size:12px;font-weight:800;color:${a.conviction>=85?'#22c55e':a.conviction>=70?'#f59e0b':'#ef4444'}">${a.conviction}</td>
    <td style="padding:7px 10px;font-size:12px">${a.consensus}/${a.scanners.size || 0}</td>
    <td style="padding:7px 10px;font-size:11px;white-space:nowrap;max-width:220px;overflow:hidden;text-overflow:ellipsis">${scanners || '—'}</td>
    <td style="padding:7px 10px;font-size:11px;white-space:normal;min-width:220px;line-height:1.45">${setups || '—'}</td>
    <td style="padding:7px 10px;font-size:11px;white-space:nowrap">${best.source || '—'} · ${bestSetup}</td>
    <td style="padding:7px 10px;font-size:11px;min-width:260px">${_edgeScoreCell(best)}</td>
    <td style="padding:7px 10px;font-size:11px;white-space:nowrap">${age ?? '—'}</td>
  </tr>`;
}
function _cbTable(title, rows, emptyText) {
  if (!rows.length) return `<div style="color:var(--muted);font-size:12px;padding:12px">${emptyText || 'No candidates found.'}</div>`;
  return `<div class="tbl-wrap" style="margin-top:0"><table class="data-tbl"><thead><tr>
    <th>Symbol</th><th>Direction</th><th>Conviction</th><th>Consensus</th><th>Scanners</th><th>Setups</th><th>Best Setup</th><th>Edge Scores</th><th>Age</th>
  </tr></thead><tbody>${rows.map(_cbRow).join('')}</tbody></table></div>`;
}
async function _cbLoadWatchlists() {
  const sel = document.getElementById('cb-watchlist');
  if (!sel) return;
  try {
    const d = await api('/watchlists/');
    const wls = (d && d.watchlists) ? d.watchlists : [];
    const stored = _getScannerWatchlistId();
    sel.innerHTML = '<option value="">Default (symbols table)</option>' +
      wls.map(w => `<option value="${w.id}"${w.is_default?' selected':''}>${w.is_default?'⭐ ':''}${w.name} (${w.symbol_count})</option>`).join('');
    if (stored && [...sel.options].some(o => String(o.value) === String(stored))) sel.value = String(stored);
    await _cbWatchlistChange(false);
  } catch (e) { console.warn('candidate board watchlist load:', e.message); }
}
async function _cbWatchlistChange(save=true) {
  const sel = document.getElementById('cb-watchlist');
  const cnt = document.getElementById('cb-wl-count');
  const wlId = sel ? sel.value : '';
  if (save) _setScannerWatchlistId(wlId);
  if (!wlId) { if (cnt) cnt.textContent = ''; return; }
  try {
    const d = await api(`/watchlists/${wlId}/symbols?prices=0`);
    if (cnt) cnt.textContent = `→ ${d.count} symbols`;
  } catch (e) { if (cnt) cnt.textContent = ''; }
}
async function runCandidateBoard() {
  const st = document.getElementById('cb-status');
  const sumEl = document.getElementById('cb-summary');
  const longsEl = document.getElementById('cb-longs');
  const shortsEl = document.getElementById('cb-shorts');
  const wlId = document.getElementById('cb-watchlist')?.value || _getScannerWatchlistId() || '';
  const minConv = Number.parseFloat(document.getElementById('cb-min-score')?.value || '70') || 70;
  const minCons = Number.parseInt(document.getElementById('cb-min-consensus')?.value || '2', 10) || 2;
  if (st) st.innerHTML = '⏳ Building candidate board…';
  if (sumEl) sumEl.innerHTML = '';
  if (longsEl) longsEl.innerHTML = '<div style="color:var(--muted);padding:12px">Loading…</div>';
  if (shortsEl) shortsEl.innerHTML = '<div style="color:var(--muted);padding:12px">Loading…</div>';
  _setScannerWatchlistId(wlId);
  const mk = (path) => {
    const u = new URL(path, location.origin);
    if (wlId) u.searchParams.set('watchlist_id', wlId);
    return u.pathname + '?' + u.searchParams.toString();
  };
  const fetches = [
    ['Maya', api(mk('/maya/api/scanner') + '&mode=core&min_score=55&trade_type=any&bias=any&strictness=2&use_rsi=1&use_dmi=1&use_ema=1&use_macd=1&use_squeeze=0')],
    ['S/R Breakout', api(mk('/scanner/sr/scan') + '&min_strength=35&require_momentum=true')],
    ['Momentum Retrace', api(mk('/api/scanner/momentum_retrace') + '&lookback=15&move_pct=6&rsi_peak=68&rsi_trough=32&delta_rsi=18&retrace_min=3&retrace_max=8&recovery_days=5')],
    ['RSI MTF', api(mk('/api/scanner/rsi_mtf'))],
    ['Trend Exhaustion', api(mk('/api/scanner/momentum_retrace_exhaustion') + '&lookback_days=20&rsi_hi=68&rsi_lo=32&diff_thr=20&min_bounce_pct=4&min_second_bars=1&exhaust_tf=1d&pullback_tf=1h')],
  ];
  const settled = await Promise.allSettled(fetches.map(x => x[1]));
  const named = settled.map((s, i) => ({ name: fetches[i][0], s }));
  const flat = [];
  const addRows = (rows, source) => {
    (rows || []).forEach(r => {
      const dir = _cbNormalizeDir(r.direction || r.bias || r.trade_side || r.setup || r.signal || '');
      if (dir === 'NEUTRAL') return;
      flat.push({
        symbol: String(r.symbol || '').toUpperCase(),
        direction: dir,
        source,
        setup: r.setup_type || r.setup || source,
        final_score: _cbScore(r.final_score ?? r.score ?? r.native_score ?? r.composite_score),
        native_score: _cbScore(r.native_score ?? r.score),
        composite_score: _cbScore(r.composite_score ?? r.edge_score),
        rs_score: _cbScore(r.rs_score),
        vol_score: _cbScore(r.vol_score),
        sector_score: _cbScore(r.sector_score),
        institutional_score: _cbScore(r.institutional_score),
        expected_move_score: _cbScore(r.expected_move_score),
        trend_age: r.trend_age ?? r.days_ago ?? null,
        raw: r,
      });
    });
  };
  named.forEach(({name,s}) => {
    if (s.status !== 'fulfilled') return;
    const d = s.value || {};
    if (name === 'Maya') addRows(d.results || [], name);
    else if (name === 'S/R Breakout') addRows(d.results || [], name);
    else if (name === 'Momentum Retrace') addRows((d.bulls || []).concat(d.bears || []), name);
    else if (name === 'RSI MTF') addRows((d.bulls || []).concat(d.bears || []), name);
    else if (name === 'Trend Exhaustion') addRows((d.bulls || []).concat(d.bears || []), name);
  });
  const groups = new Map();
  for (const r of flat) {
    const key = `${r.symbol}|${r.direction}`;
    if (!groups.has(key)) groups.set(key, {
      symbol: r.symbol,
      direction: r.direction,
      hits: 0,
      scanners: new Set(),
      setups: new Set(),
      scoreSum: 0,
      scoreMax: 0,
      rsSum: 0,
      volSum: 0,
      secSum: 0,
      instSum: 0,
      emSum: 0,
      ageSum: 0,
      ageCount: 0,
      rows: [],
      bestRow: null,
    });
    const g = groups.get(key);
    g.hits += 1;
    g.scanners.add(r.source);
    g.setups.add(r.setup);
    g.scoreSum += r.final_score;
    g.scoreMax = Math.max(g.scoreMax, r.final_score);
    g.rsSum += r.rs_score || 0;
    g.volSum += r.vol_score || 0;
    g.secSum += r.sector_score || 0;
    g.instSum += r.institutional_score || 0;
    g.emSum += r.expected_move_score || 0;
    if (r.trend_age != null && !Number.isNaN(Number(r.trend_age))) { g.ageSum += Number(r.trend_age); g.ageCount += 1; }
    g.rows.push(r);
    if (!g.bestRow || r.final_score > g.bestRow.final_score) g.bestRow = r;
  }
  const agg = [...groups.values()].map(g => {
    const consensus = g.scanners.size;
    const avg = g.hits ? g.scoreSum / g.hits : 0;
    const conviction = Math.min(100, Math.round(avg + (consensus - 1) * 6 + (g.setups.size - 1) * 2 + Math.min(8, g.hits - 1)));
    return {
      ...g,
      consensus,
      conviction,
      avgScore: avg,
      rs: g.hits ? g.rsSum / g.hits : 0,
      vol: g.hits ? g.volSum / g.hits : 0,
      sector: g.hits ? g.secSum / g.hits : 0,
      inst: g.hits ? g.instSum / g.hits : 0,
      em: g.hits ? g.emSum / g.hits : 0,
      trend_age: g.ageCount ? Math.round(g.ageSum / g.ageCount) : (g.bestRow?.trend_age ?? '—'),
    };
  }).filter(g => g.conviction >= minConv && g.consensus >= minCons && g.symbol);
  const longs = agg.filter(g => g.direction === 'BULLISH').sort((a,b)=>b.conviction-a.conviction || b.consensus-a.consensus || b.avgScore-a.avgScore).slice(0, 20);
  const shorts = agg.filter(g => g.direction === 'BEARISH').sort((a,b)=>b.conviction-a.conviction || b.consensus-a.consensus || b.avgScore-a.avgScore).slice(0, 20);
  const okCount = named.filter(x => x.s.status === 'fulfilled').length;
  const failCount = named.length - okCount;
  if (st) st.innerHTML = `✅ ${longs.length} longs · ${shorts.length} shorts · ${okCount}/${named.length} sources` + (failCount ? ` · ⚠ ${failCount} source(s) failed` : '');
  if (sumEl) {
    const total = agg.length;
    const avgConv = total ? Math.round(agg.reduce((a,b)=>a+b.conviction,0) / total) : 0;
    sumEl.innerHTML = [
      {l:'Candidates', v: total},
      {l:'Longs', v: longs.length},
      {l:'Shorts', v: shorts.length},
      {l:'Avg Conviction', v: avgConv},
      {l:'Consensus ≥'+minCons, v: agg.filter(g => g.consensus >= minCons).length},
    ].map(x => `<div style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:8px 10px"><div style="font-size:10px;color:var(--muted)">${x.l}</div><div style="font-size:16px;font-weight:800">${x.v}</div></div>`).join('');
  }
  if (longsEl) longsEl.innerHTML = _cbTable('Top Longs', longs, 'No long candidates matched the current filters.');
  if (shortsEl) shortsEl.innerHTML = _cbTable('Top Shorts', shorts, 'No short candidates matched the current filters.');
}


window.runCandidateBoard = runCandidateBoard;
window._cbLoadWatchlists = _cbLoadWatchlists;
window._cbWatchlistChange = _cbWatchlistChange;


