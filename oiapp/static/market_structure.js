const $ = (id) => document.getElementById(id);

function fmt(v, dec = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (typeof v === 'number') return dec === 0 ? Math.round(v).toLocaleString() : v.toFixed(dec);
  return String(v);
}

function regimeBadge(label) {
  const map = {
    'Strong Uptrend': 'pill-good',
    'Uptrend': 'pill-good',
    'Range': 'pill-warn',
    'Downtrend': 'pill-bad',
    'Strong Downtrend': 'pill-bad',
  };
  return `<span class="badge ${map[label] || ''}">${label || '—'}</span>`;
}

function zoneHtml(title, zones) {
  if (!Array.isArray(zones) || !zones.length) return `<div class="mini">No levels identified.</div>`;
  return `<div class="levels">${zones.map(z => `
    <div class="level">
      <div class="row"><b>${fmt(z.level)}</b><span>${z.type || 'Zone'}</span></div>
      <div class="row"><span>Score ${fmt(z.score, 0)} · Conf ${fmt(z.confidence, 0)}%</span><span>${fmt(z.distance_pct, 2)}%</span></div>
      <div class="mini">${(z.reasons || []).join(' · ') || 'Confluence zone'}</div>
    </div>`).join('')}</div>`;
}

function hvnHtml(vp) {
  const hvn = vp?.hvn || [];
  if (!hvn.length) return '<div class="mini">No HVNs identified.</div>';
  return `<div class="levels">${hvn.map(h => `
    <div class="level">
      <div class="row"><b>${fmt(h.level)}</b><span>${h.rank}</span></div>
      <div class="row"><span>Volume ${fmt(h.volume, 0)}</span><span>Strength ${fmt(h.strength, 1)}%</span></div>
    </div>`).join('')}</div>`;
}

function lvnHtml(vp) {
  const lvn = vp?.lvn || [];
  if (!lvn.length) return '<div class="mini">No LVNs identified.</div>';
  return `<div class="levels">${lvn.map(h => `
    <div class="level">
      <div class="row"><b>${fmt(h.level)}</b><span>LVN</span></div>
      <div class="row"><span>Volume ${fmt(h.volume, 0)}</span><span>Strength ${fmt(h.strength, 1)}%</span></div>
    </div>`).join('')}</div>`;
}

function renderSummary(data) {
  $('sum-results').textContent = data.count ?? 0;
  $('sum-confidence').textContent = fmt(data.summary?.average_confidence, 1);
  $('sum-ob').textContent = data.summary?.monthly_overbought ?? '—';
  $('sum-os').textContent = data.summary?.monthly_oversold ?? '—';
  const rc = data.summary?.regime_counts || {};
  $('sum-regime').textContent = `${rc['Strong Uptrend'] || 0} / ${rc['Strong Downtrend'] || 0}`;
  $('sum-time').textContent = data.completed_at || '—';
}

function renderCard(r) {
  const regime = r.regime || {};
  const vp = r.volume_profile || {};
  const mr = r.mean_reversion || {};
  const rev = r.reversal || {};
  const bt = r.best_trade || {};
  const conf = r.confidence || {};

  return `
    <div class="card">
      <div class="card-head">
        <div>
          <div class="sym">${r.symbol}</div>
          <div class="meta">Spot ${fmt(r.spot)} · Benchmark ${r.benchmark || 'SPY'} · Monthly RSI diff ${fmt(r.monthly_rsidiff90, 1)}</div>
          <div class="meta">Monthly state: ${r.monthly_momentum_state || '—'}</div>
        </div>
        <div style="display:flex;flex-direction:column;gap:6px;align-items:flex-end">
          ${regimeBadge(regime.overall)}
          <span class="badge">Trade: ${bt.setup || '—'}</span>
          <span class="badge">Conf ${fmt(conf.overall, 0)}%</span>
        </div>
      </div>

      <div class="grid">
        <div class="panel">
          <h4>Market Regime</h4>
          <div class="mini"><b>Monthly:</b> ${regime.monthly?.label || '—'} (${fmt(regime.monthly?.confidence, 0)}%)</div>
          <div class="mini"><b>Weekly:</b> ${regime.weekly?.label || '—'} (${fmt(regime.weekly?.confidence, 0)}%)</div>
          <div class="mini"><b>Daily:</b> ${regime.daily?.label || '—'} (${fmt(regime.daily?.confidence, 0)}%)</div>
          <div class="mini"><b>Overall:</b> ${regime.overall || '—'}</div>
          <div class="mini"><b>RS strength:</b> ${fmt(regime.monthly?.relative_strength, 4)} / slope ${fmt(regime.monthly?.relative_strength_slope, 2)}%</div>
        </div>

        <div class="panel">
          <h4>Volume Profile</h4>
          <div class="mini"><b>POC:</b> ${fmt(vp.poc)}</div>
          <div class="mini"><b>VAH:</b> ${fmt(vp.vah)}</div>
          <div class="mini"><b>VAL:</b> ${fmt(vp.val)}</div>
          <div class="mini"><b>State:</b> ${vp.value_area_state || '—'}</div>
          <div class="mini"><b>Profile:</b> ${vp.profile_window ? `last ${fmt(vp.profile_window, 0)} bars` : '—'}</div>
          <div class="mini"><b>Anchor:</b> ${vp.profile_anchor || '—'}</div>
          <div class="mini"><b>Major HVN:</b> ${fmt(vp.major_hvn_level)}</div>
        </div>

        <div class="panel">
          <h4>Mean Reversion</h4>
          <div class="mini"><b>Score:</b> ${fmt(mr.score, 0)} / 100</div>
          <div class="mini"><b>Direction:</b> ${mr.direction || '—'}</div>
          <div class="mini"><b>RSI:</b> ${fmt(mr.rsi, 1)} · <b>BB pos:</b> ${fmt(mr.bb_position, 1)}%</div>
          <div class="mini"><b>Dist MA20:</b> ${fmt(mr.distance_from_ma20_pct, 2)}%</div>
          <div class="mini"><b>MACD hist:</b> ${fmt(mr.macd_hist, 3)}</div>
        </div>

        <div class="panel">
          <h4>Reversal / Trade</h4>
          <div class="mini"><b>Bullish prob:</b> ${fmt(rev.bullish_probability, 0)}%</div>
          <div class="mini"><b>Bearish prob:</b> ${fmt(rev.bearish_probability, 0)}%</div>
          <div class="mini"><b>Entry:</b> ${fmt(bt.entry_zone)} · <b>Stop:</b> ${fmt(bt.stop)}</div>
          <div class="mini"><b>T1:</b> ${fmt(bt.target1)} · <b>T2:</b> ${fmt(bt.target2)}</div>
          <div class="mini"><b>R/R:</b> ${fmt(bt.risk_reward, 2)} · <b>Bias:</b> ${bt.bias || '—'}</div>
        </div>
      </div>

      <div class="grid" style="margin-top:10px">
        <div class="panel">
          <h4>Support Zones</h4>
          ${zoneHtml('Support', r.support_zones)}
        </div>
        <div class="panel">
          <h4>Resistance Zones</h4>
          ${zoneHtml('Resistance', r.resistance_zones)}
        </div>
        <div class="panel">
          <h4>Major HVNs</h4>
          ${hvnHtml(vp)}
        </div>
        <div class="panel">
          <h4>LVNs</h4>
          ${lvnHtml(vp)}
        </div>
      </div>

      <div class="panel" style="margin-top:10px">
        <h4>Thesis</h4>
        <div class="summary-text">${r.summary || '—'}</div>
        <div class="mini" style="margin-top:8px"><b>Bull trigger:</b> ${rev.bullish_trigger || '—'}</div>
        <div class="mini"><b>Bear trigger:</b> ${rev.bearish_trigger || '—'}</div>
      </div>

      <div class="panel" style="margin-top:10px">
        <h4>Confidence</h4>
        <div class="mini">Regime ${fmt(conf.regime, 0)}% · Volume Profile ${fmt(conf.volume_profile, 0)}% · Support ${fmt(conf.support, 0)}% · Resistance ${fmt(conf.resistance, 0)}%</div>
        <div class="mini">Mean Reversion ${fmt(conf.mean_reversion, 0)}% · Reversal ${fmt(conf.reversal, 0)}% · Trade Setup ${fmt(conf.trade_setup, 0)}%</div>
      </div>
    </div>`;
}

async function loadWatchlists() {
  const sel = $('watchlist');
  sel.innerHTML = '<option>Loading...</option>';
  const r = await fetch('/scanner/market-structure/watchlists');
  const d = await r.json();
  const list = d.watchlists || [];
  sel.innerHTML = list.length ? list.map((w, i) => `<option value="${w.id}" ${i===0 ? 'selected' : ''}>${w.name} (${w.symbol_count || 0})</option>`).join('') : '<option value="">No watchlists</option>';
  if (!sel.value && list.length) sel.value = String(list[0].id);
}

async function run() {
  const watchlist_id = $('watchlist').value;
  const benchmark = $('benchmark').value.trim().toUpperCase() || 'SPY';
  const monthly_filter = $('monthly-filter').value;
  const threshold = $('threshold').value || 20;
  const max_symbols = $('max-symbols').value || 60;
  const status = $('status');
  const results = $('results');
  status.textContent = 'Running analysis...';
  results.innerHTML = '';

  const qs = new URLSearchParams({ watchlist_id, benchmark, monthly_filter, threshold, max_symbols, limit: max_symbols });
  const r = await fetch(`/scanner/market-structure/scan?${qs.toString()}`);
  const d = await r.json();
  if (d.error) {
    status.textContent = d.error;
    results.innerHTML = `<div class="card">${d.error}</div>`;
    return;
  }
  renderSummary(d);
  status.textContent = `Completed ${d.count || 0} symbols${d.filters?.monthly_filter && d.filters.monthly_filter !== 'all' ? ` · Filtered by ${d.filters.monthly_filter}` : ''}`;
  const rows = d.results || [];
  results.innerHTML = rows.map(renderCard).join('') || '<div class="card">No results. Try lowering the RSI threshold or increasing the symbol limit.</div>';
  if (Array.isArray(d.errors) && d.errors.length) {
    const errCard = `<div class="card"><div class="card-head"><div><div class="sym">Scan warnings</div><div class="meta">${d.errors.length} symbols could not be analyzed</div></div></div><div class="mini"><pre>${d.errors.slice(0,8).map(e => `${e.symbol || 'UNK'}: ${e.error || 'unknown error'}`).join('\n')}</pre></div></div>`;
    results.insertAdjacentHTML('beforeend', errCard);
  }
}

window.addEventListener('DOMContentLoaded', async () => {
  $('run').addEventListener('click', run);
  $('refresh').addEventListener('click', loadWatchlists);
  await loadWatchlists();
});
