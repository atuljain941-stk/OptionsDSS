const $ = (id) => document.getElementById(id);

const PRIMITIVES = [
  { key: 'ema20', label: 'EMA20', group: 'price' },
  { key: 'ema50', label: 'EMA50', group: 'price' },
  { key: 'ema200', label: 'EMA200', group: 'price' },
  { key: 'sma20', label: 'SMA20', group: 'price' },
  { key: 'sma50', label: 'SMA50', group: 'price' },
  { key: 'bbands', label: 'Bollinger Bands', group: 'price' },
  { key: 'support', label: 'Support', group: 'price' },
  { key: 'resistance', label: 'Resistance', group: 'price' },
  { key: 'put_walls', label: 'Put Walls', group: 'price' },
  { key: 'call_walls', label: 'Call Walls', group: 'price' },
  { key: 'gamma_wall', label: 'Gamma Wall', group: 'price' },
  { key: 'rsi14', label: 'RSI14', group: 'osc' },
  { key: 'ema_rsi90', label: 'EMA(RSI14,90)', group: 'osc' },
  { key: 'rsi_diff_90', label: 'RSIDiff90', group: 'osc' },
  { key: 'macd', label: 'MACD', group: 'osc' },
  { key: 'macd_signal', label: 'MACD Signal', group: 'osc' },
  { key: 'macd_hist', label: 'MACD Hist', group: 'osc' },
  { key: 'atr14', label: 'ATR14', group: 'osc' },
  { key: 'vol_sma20', label: 'Volume SMA20', group: 'osc' },
];

const PRESETS = {
  'monthly-weekly-daily-intraday': ['1m', '1w', '1d', '4h'],
  'weekly-daily-4h-1h': ['1w', '1d', '4h', '1h'],
  'daily-4h-1h-15m': ['1d', '4h', '1h', '15m'],
};

let selected = ['ema20', 'ema50', 'support', 'resistance', 'put_walls', 'call_walls', 'gamma_wall'];
const panelState = [{tf:'1m'},{tf:'1w'},{tf:'1d'},{tf:'4h'}];

function fmt(v, dec = 2) {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (typeof v === 'number') return dec === 0 ? Math.round(v).toLocaleString() : v.toFixed(dec);
  return String(v);
}

function qs() { return new URLSearchParams(location.search); }
function currentSymbol() { return ($('symbol')?.value || qs().get('symbol') || 'SPY').trim().toUpperCase() || 'SPY'; }
function currentExpiry() { return ($('expiry')?.value || qs().get('expiry') || '').trim(); }

async function api(url) {
  const r = await fetch(url, { headers: { 'Accept': 'application/json' } });
  const text = await r.text();
  let data = {};
  try { data = JSON.parse(text); } catch { data = { error: text || r.statusText }; }
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}

function primitiveMeta(key) { return PRIMITIVES.find(p => p.key === key); }

function renderPrimitiveSelect() {
  const sel = $('primitive-select');
  sel.innerHTML = PRIMITIVES.map(p => `<option value="${p.key}">${p.label}</option>`).join('');
}

function renderChips() {
  const el = $('chips');
  el.innerHTML = selected.map(k => {
    const p = primitiveMeta(k);
    return `<span class="chip">${p ? p.label : k}<span class="x" data-remove="${k}" title="Remove">×</span></span>`;
  }).join('');
  el.querySelectorAll('[data-remove]').forEach(x => x.addEventListener('click', () => {
    selected = selected.filter(k => k !== x.dataset.remove);
    renderChips();
    refreshAll();
  }));
}

function fillTimeframeSelects() {
  document.querySelectorAll('.tf-select').forEach(sel => {
    const panel = +sel.dataset.panel;
    sel.innerHTML = `
      <option value="1m">Monthly</option>
      <option value="1w">Weekly</option>
      <option value="1d">Daily</option>
      <option value="4h">4H</option>
      <option value="1h">1H</option>
      <option value="15m">15M</option>
      <option value="5m">5M</option>
    `;
    sel.value = panelState[panel].tf;
    sel.addEventListener('change', () => {
      panelState[panel].tf = sel.value;
      refreshPanel(panel);
    });
  });
}

function setPreset(name) {
  const tfs = PRESETS[name] || PRESETS['monthly-weekly-daily-intraday'];
  panelState.forEach((p, idx) => p.tf = tfs[idx] || p.tf);
  document.querySelectorAll('.tf-select').forEach(sel => sel.value = panelState[+sel.dataset.panel].tf);
  refreshAll();
}

function lineTrace(x, y, name, color, width = 1.6, yaxis = 'y') {
  return { x, y, mode: 'lines', name, line: { color, width }, yaxis, hoverinfo: 'skip' };
}

function shapesForLevels(levels, x0, x1, fill = 'rgba(16,185,129,0.14)', line = 'rgba(16,185,129,0.35)') {
  return (levels || []).map(z => ({
    type: 'rect', xref: 'x', yref: 'y', x0, x1,
    y0: z.lo ?? z.level, y1: z.hi ?? z.level,
    fillcolor: fill, opacity: 0.28, line: { color: line, width: 1 }, layer: 'below',
  }));
}

function wallLines(walls, x0, x1, color) {
  return (walls || []).map((w, idx) => ({
    type: 'line', xref: 'x', yref: 'y', x0, x1, y0: w.strike || w.level || w.gamma_wall || w, y1: w.strike || w.level || w.gamma_wall || w,
    line: { color, width: 1.4, dash: idx === 0 ? 'solid' : 'dot' },
  }));
}

function oscillatorTrace(data, key, x, color) {
  const s = data.series?.[key];
  if (!Array.isArray(s) || !s.length) return null;
  return { x, y: s, mode: 'lines', name: key, line: { color, width: 1.2 }, yaxis: 'y2', hoverinfo: 'skip' };
}

function buildLegend(data) {
  const w = data.walls || {};
  const v = data.latest || {};
  const p = data.symbol;
  const support = data.levels?.supports?.[0]?.level;
  const resistance = data.levels?.resistances?.[0]?.level;
  return `
    <span class="badge">${p}</span>
    <span class="badge">Spot ${fmt(data.spot)}</span>
    <span class="badge">RSI ${fmt(v.rsi14, 1)}</span>
    <span class="badge">RSIDiff90 ${fmt(v.rsi_diff_90, 1)}</span>
    <span class="badge">Support ${fmt(support)}</span>
    <span class="badge">Resistance ${fmt(resistance)}</span>
    <span class="badge">Bias ${(w.bias || 'NEUTRAL').replaceAll('_',' ')}</span>`;
}

function statHtml(data) {
  const v = data.latest || {};
  const w = data.walls || {};
  const s = data.series || {};
  const volRatio = v.vol_ratio ?? (v.volume && s.vol_sma20?.length ? (v.volume / (s.vol_sma20[s.vol_sma20.length - 1] || 1)) : null);
  return [
    ['Close', data.spot],
    ['Volume', v.volume],
    ['Vol ratio', volRatio],
    ['RSI14', v.rsi14],
    ['RSIDiff90', v.rsi_diff_90],
    ['MACD hist', v.macd_hist],
    ['Gamma wall', w?.gamma_wall || data.levels?.channels?.[0]?.mid],
    ['Walls', (w?.nearest_support?.strike || '—') + ' / ' + (w?.nearest_resistance?.strike || '—')],
  ].map(([k, val]) => `<div class="stat"><div class="k">${k}</div><div class="v">${fmt(val, k === 'Volume' ? 0 : 2)}</div></div>`).join('');
}



function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function toNumber(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function extent(arr) {
  let min = Infinity;
  let max = -Infinity;
  (arr || []).forEach(v => {
    const n = toNumber(v);
    if (n === null) return;
    if (n < min) min = n;
    if (n > max) max = n;
  });
  return [min, max];
}

function makeSvgText(x, y, txt, fill = '#cbd5e1', size = 11, anchor = 'start') {
  return `<text x="${x}" y="${y}" fill="${fill}" font-size="${size}" text-anchor="${anchor}" font-family="Inter,Arial,sans-serif">${String(txt).replace(/[&<>]/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[s]))}</text>`;
}

function lineSeries(arr, xMap, yMap) {
  const pts = [];
  (arr || []).forEach((v, i) => {
    const n = toNumber(v);
    if (n === null) return;
    pts.push(`${xMap(i)},${yMap(n)}`);
  });
  return pts.join(' ');
}

function renderFallbackChart(panel, data) {
  const el = $(`chart-${panel}`);
  if (!el) return;
  const bars = Array.isArray(data.bars) ? data.bars : [];
  if (!bars.length) {
    el.innerHTML = '<div class="loading">No chart data</div>';
    return;
  }

  const W = Math.max(640, el.clientWidth || 900);
  const H = Math.max(420, el.clientHeight || 480);
  const pad = { l: 48, r: 20, t: 20, b: 24 };
  const hasOsc = selected.some(k => ['rsi14','ema_rsi90','rsi_diff_90','macd','macd_signal','macd_hist','atr14','vol_sma20'].includes(k));
  const priceH = Math.round(H * (hasOsc ? 0.72 : 0.92));
  const oscH = hasOsc ? Math.max(90, H - priceH - 18) : 0;
  const priceTop = pad.t;
  const oscTop = priceTop + priceH + 8;
  const x0 = pad.l;
  const x1 = W - pad.r;
  const plotW = x1 - x0;

  let minY = Infinity;
  let maxY = -Infinity;
  bars.forEach(b => {
    ['low','high'].forEach(k => {
      const n = toNumber(b[k]);
      if (n === null) return;
      if (n < minY) minY = n;
      if (n > maxY) maxY = n;
    });
  });

  const priceKeys = ['ema20','ema50','ema200','sma20','sma50','bb_lower','bb_mid','bb_upper'];
  selected.forEach(k => {
    if (!priceKeys.includes(k)) return;
    const arr = data.series?.[k];
    (arr || []).forEach(v => {
      const n = toNumber(v);
      if (n === null) return;
      if (n < minY) minY = n;
      if (n > maxY) maxY = n;
    });
  });

  const wallLevels = [];
  const levels = data.levels || {};
  (levels.supports || []).forEach(z => wallLevels.push(toNumber(z.hi ?? z.level)));
  (levels.resistances || []).forEach(z => wallLevels.push(toNumber(z.lo ?? z.level)));
  (levels.channels || []).forEach(z => {
    wallLevels.push(toNumber(z.lo));
    wallLevels.push(toNumber(z.hi));
  });
  const w = data.walls || {};
  (w.put_walls || []).forEach(z => wallLevels.push(toNumber(z.strike ?? z.level ?? z.gamma_wall ?? z)));
  (w.call_walls || []).forEach(z => wallLevels.push(toNumber(z.strike ?? z.level ?? z.gamma_wall ?? z)));
  if (w.gamma_wall !== undefined && w.gamma_wall !== null) wallLevels.push(toNumber(w.gamma_wall));
  wallLevels.forEach(n => {
    if (n === null) return;
    if (n < minY) minY = n;
    if (n > maxY) maxY = n;
  });

  if (!Number.isFinite(minY) || !Number.isFinite(maxY) || minY === maxY) {
    const last = toNumber(data.spot) ?? toNumber(bars[bars.length - 1].close) ?? 1;
    minY = last * 0.95;
    maxY = last * 1.05;
  }
  const yRange = maxY - minY || 1;
  const xStep = bars.length > 1 ? plotW / (bars.length - 1) : plotW;
  const yMap = (v) => priceTop + (maxY - v) / yRange * priceH;
  const xMap = (i) => x0 + i * xStep;
  const candleW = Math.max(2, Math.min(10, plotW / Math.max(20, bars.length) * 0.7));

  let svg = `
  <svg viewBox="0 0 ${W} ${H}" width="100%" height="100%" preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg" style="display:block;background:rgba(8,12,24,.55)">
    <defs>
      <linearGradient id="bggrad-${panel}" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="rgba(8,12,24,.35)" />
        <stop offset="100%" stop-color="rgba(8,12,24,.08)" />
      </linearGradient>
    </defs>
    <rect x="0" y="0" width="${W}" height="${H}" fill="url(#bggrad-${panel})"/>
  `;

  // grid + price labels
  for (let i = 0; i <= 4; i += 1) {
    const y = priceTop + (priceH * i / 4);
    const val = maxY - (yRange * i / 4);
    svg += `<line x1="${x0}" y1="${y}" x2="${x1}" y2="${y}" stroke="rgba(148,163,184,.12)" stroke-width="1" />`;
    svg += makeSvgText(8, y + 4, fmt(val, 2), '#94a3b8', 10, 'start');
  }

  // support/resistance bands
  (levels.supports || []).slice(0, 3).forEach((z, idx) => {
    const y0 = yMap(toNumber(z.hi ?? z.level) ?? minY);
    const y1 = yMap(toNumber(z.lo ?? z.level) ?? minY);
    svg += `<rect x="${x0}" y="${Math.min(y0, y1)}" width="${plotW}" height="${Math.max(1, Math.abs(y1-y0))}" fill="rgba(34,197,94,0.10)" stroke="rgba(34,197,94,0.22)" />`;
    svg += makeSvgText(x1 - 4, Math.min(y0, y1) + 11, `S ${fmt(z.level ?? z.hi, 2)}`, '#86efac', 10, 'end');
  });
  (levels.resistances || []).slice(0, 3).forEach((z, idx) => {
    const y0 = yMap(toNumber(z.hi ?? z.level) ?? minY);
    const y1 = yMap(toNumber(z.lo ?? z.level) ?? minY);
    svg += `<rect x="${x0}" y="${Math.min(y0, y1)}" width="${plotW}" height="${Math.max(1, Math.abs(y1-y0))}" fill="rgba(239,68,68,0.09)" stroke="rgba(239,68,68,0.2)" />`;
    svg += makeSvgText(x1 - 4, Math.min(y0, y1) + 11, `R ${fmt(z.level ?? z.lo, 2)}`, '#fca5a5', 10, 'end');
  });

  // OI walls and gamma wall
  const drawHLine = (y, color, label) => {
    if (y === null) return;
    const yy = yMap(y);
    svg += `<line x1="${x0}" y1="${yy}" x2="${x1}" y2="${yy}" stroke="${color}" stroke-width="1.4" stroke-dasharray="4 4" />`;
    if (label) svg += makeSvgText(x1 - 4, yy - 4, label, color, 10, 'end');
  };
  (w.put_walls || []).slice(0, 3).forEach((z, i) => drawHLine(toNumber(z.strike ?? z.level ?? z.gamma_wall ?? z), 'rgba(34,197,94,.75)', `Put ${fmt(z.strike ?? z.level, 2)}`));
  (w.call_walls || []).slice(0, 3).forEach((z, i) => drawHLine(toNumber(z.strike ?? z.level ?? z.gamma_wall ?? z), 'rgba(239,68,68,.75)', `Call ${fmt(z.strike ?? z.level, 2)}`));
  if (w.gamma_wall !== undefined && w.gamma_wall !== null) drawHLine(toNumber(w.gamma_wall), 'rgba(168,85,247,.85)', `Gamma ${fmt(w.gamma_wall, 2)}`);

  // moving averages / bands
  const overlays = {
    ema20: '#60a5fa', ema50: '#f59e0b', ema200: '#a855f7', sma20: '#14b8a6', sma50: '#e879f9',
    bb_lower: 'rgba(59,130,246,.55)', bb_mid: 'rgba(148,163,184,.75)', bb_upper: 'rgba(59,130,246,.8)'
  };
  Object.entries(overlays).forEach(([key, color]) => {
    if (!selected.includes(key)) return;
    const series = data.series?.[key];
    if (!Array.isArray(series) || !series.length) return;
    const pts = lineSeries(series, xMap, yMap);
    if (!pts) return;
    svg += `<polyline fill="none" stroke="${color}" stroke-width="1.6" points="${pts}" />`;
  });

  // candles
  bars.forEach((b, i) => {
    const o = toNumber(b.open), h = toNumber(b.high), l = toNumber(b.low), c = toNumber(b.close);
    if ([o,h,l,c].some(v => v === null)) return;
    const x = xMap(i);
    const up = c >= o;
    const color = up ? '#22c55e' : '#ef4444';
    const yH = yMap(h), yL = yMap(l), yO = yMap(o), yC = yMap(c);
    const top = Math.min(yO, yC);
    const bodyH = Math.max(1, Math.abs(yC - yO));
    const bodyY = Math.min(yO, yC);
    svg += `<line x1="${x}" y1="${yH}" x2="${x}" y2="${yL}" stroke="${color}" stroke-width="1" />`;
    svg += `<rect x="${x - candleW / 2}" y="${bodyY}" width="${candleW}" height="${bodyH}" fill="${color}" opacity="0.92" />`;
  });

  // Volume mini-panel.
  const vols = bars.map(b => toNumber(b.volume) ?? 0);
  const [volMin, volMax] = extent(vols);
  if (Number.isFinite(volMax) && volMax > 0) {
    const volTop = oscTop;
    const volH = Math.max(40, oscH || Math.round(H * 0.18));
    svg += `<line x1="${x0}" y1="${volTop}" x2="${x1}" y2="${volTop}" stroke="rgba(148,163,184,.16)" />`;
    svg += makeSvgText(8, volTop + 12, 'Volume', '#94a3b8', 10, 'start');
    bars.forEach((b, i) => {
      const v = toNumber(b.volume) ?? 0;
      if (v <= 0) return;
      const hgt = Math.max(1, (v / volMax) * (volH - 18));
      const x = xMap(i);
      const color = (toNumber(b.close) ?? 0) >= (toNumber(b.open) ?? 0) ? 'rgba(34,197,94,.55)' : 'rgba(239,68,68,.55)';
      svg += `<rect x="${x - candleW / 2}" y="${volTop + volH - hgt}" width="${candleW}" height="${hgt}" fill="${color}" />`;
    });
  }

  // Title / footer.
  svg += makeSvgText(12, 16, `${data.symbol} · ${tf} · ${bars.length} bars`, '#e2e8f0', 12, 'start');
  svg += makeSvgText(12, H - 6, `Spot ${fmt(data.spot)}  |  RSI ${fmt(data.latest?.rsi14, 1)}  |  RSIDiff90 ${fmt(data.latest?.rsi_diff_90, 1)}`, '#94a3b8', 10, 'start');
  svg += '</svg>';

  el.innerHTML = svg;
}

function paint(panel, data) {
  const tf = panelState[panel].tf;
  const id = `chart-${panel}`;
  const sub = $(`sub-${panel}`);
  const legend = $(`legend-${panel}`);
  const stats = $(`stats-${panel}`);
  const x = Array.isArray(data.bars) ? data.bars.map(b => b.ts) : [];
  if (!x.length) {
    const el = $(id);
    if (el) el.innerHTML = '<div class="loading">No chart data</div>';
    return;
  }
  if (sub) sub.textContent = `${data.symbol} · ${tf} · ${data.meta?.bars || x.length} bars`;
  if (legend) legend.innerHTML = buildLegend(data);
  if (stats) stats.innerHTML = statHtml(data);

  const canPlot = typeof Plotly !== 'undefined' && Plotly && typeof Plotly.newPlot === 'function';
  if (!canPlot) {
    renderFallbackChart(panel, data);
    return;
  }

  const traces = [
    { type: 'candlestick', x,
      open: data.bars.map(b => b.open), high: data.bars.map(b => b.high), low: data.bars.map(b => b.low), close: data.bars.map(b => b.close),
      increasing: { line: { color: '#22c55e' } }, decreasing: { line: { color: '#ef4444' } }, name: 'Price',
    }
  ];

  const overlayMap = {
    ema20: ['price', lineTrace(x, data.series.ema20, 'EMA20', '#60a5fa')],
    ema50: ['price', lineTrace(x, data.series.ema50, 'EMA50', '#f59e0b')],
    ema200: ['price', lineTrace(x, data.series.ema200, 'EMA200', '#a855f7')],
    sma20: ['price', lineTrace(x, data.series.sma20, 'SMA20', '#14b8a6')],
    sma50: ['price', lineTrace(x, data.series.sma50, 'SMA50', '#e879f9')],
    bbands: ['price', [lineTrace(x, data.series.bb_upper, 'BB Upper', 'rgba(59,130,246,.8)', 1.0), lineTrace(x, data.series.bb_mid, 'BB Mid', 'rgba(148,163,184,.7)', 1.0), lineTrace(x, data.series.bb_lower, 'BB Lower', 'rgba(59,130,246,.5)', 1.0)]],
    support: ['shape', { color: 'rgba(34,197,94,.18)', line: 'rgba(34,197,94,.3)', levels: data.levels?.supports }],
    resistance: ['shape', { color: 'rgba(239,68,68,.15)', line: 'rgba(239,68,68,.28)', levels: data.levels?.resistances }],
    put_walls: ['shapeLine', { color: 'rgba(34,197,94,.7)', walls: data.walls?.put_walls }],
    call_walls: ['shapeLine', { color: 'rgba(239,68,68,.7)', walls: data.walls?.call_walls }],
    gamma_wall: ['shapeLine', { color: 'rgba(168,85,247,.85)', walls: data.walls?.gamma_wall != null ? [{ strike: data.walls.gamma_wall }] : [] }],
    rsi14: ['osc', oscillatorTrace(data, 'rsi14', x, '#fbbf24')],
    ema_rsi90: ['osc', oscillatorTrace(data, 'ema_rsi90', x, '#22c55e')],
    rsi_diff_90: ['osc', oscillatorTrace(data, 'rsi_diff_90', x, '#fb7185')],
    macd: ['osc', oscillatorTrace(data, 'macd', x, '#60a5fa')],
    macd_signal: ['osc', oscillatorTrace(data, 'macd_signal', x, '#f59e0b')],
    macd_hist: ['osc', oscillatorTrace(data, 'macd_hist', x, '#a855f7')],
    atr14: ['osc', oscillatorTrace(data, 'atr14', x, '#14b8a6')],
    vol_sma20: ['osc', oscillatorTrace(data, 'vol_sma20', x, '#94a3b8')],
  };

  const tracesOsc = [];
  const shapes = [];
  selected.forEach(key => {
    const item = overlayMap[key];
    if (!item) return;
    if (item[0] === 'price') {
      const v = item[1];
      if (Array.isArray(v)) traces.push(...v.filter(Boolean)); else if (v) traces.push(v);
    } else if (item[0] === 'shape') {
      shapes.push(...shapesForLevels(item[1].levels, x[0], x[x.length - 1], item[1].color, item[1].line));
    } else if (item[0] === 'shapeLine') {
      shapes.push(...wallLines(item[1].walls, x[0], x[x.length - 1], item[1].color));
    } else if (item[0] === 'osc' && item[1]) {
      tracesOsc.push(item[1]);
    }
  });
  if (tracesOsc.length) traces.push(...tracesOsc);

  const layout = {
    margin: { l: 55, r: 45, t: 20, b: 30 },
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(8,12,24,.4)',
    font: { color: '#e2e8f0', size: 11 },
    xaxis: { rangeslider: { visible: false }, gridcolor: 'rgba(148,163,184,.12)', zeroline: false },
    yaxis: { gridcolor: 'rgba(148,163,184,.12)', zeroline: false, title: 'Price' },
    yaxis2: { overlaying: 'y', side: 'right', showgrid: false, zeroline: false, title: 'Osc', rangemode: 'tozero' },
    legend: { orientation: 'h', y: 1.08, x: 0, font: { size: 10 } },
    shapes,
    hovermode: 'x unified',
  };

  try {
    Plotly.newPlot(id, traces, layout, { responsive: true, displayModeBar: false });
  } catch (err) {
    console.warn('Plotly render failed, falling back to SVG renderer:', err);
    renderFallbackChart(panel, data);
  }
}

async function refreshPanel
(panel) {
  const tf = panelState[panel].tf;
  const sym = currentSymbol();
  const expiry = currentExpiry();
  const el = $(`chart-${panel}`);
  if (el) el.innerHTML = '<div class="loading">Loading chart…</div>';
  try {
    const q = new URLSearchParams({ symbol: sym, timeframe: tf });
    if (expiry) q.set('expiry', expiry);
    const data = await api(`/charts/api/data?${q.toString()}`);
    if (panel === 0) {
      $('active-spot').textContent = `Spot: ${fmt(data.spot)}`;
      const w = data.walls || {};
      const sup = w?.nearest_support?.strike || data.levels?.supports?.[0]?.level || '—';
      const res = w?.nearest_resistance?.strike || data.levels?.resistances?.[0]?.level || '—';
      $('active-walls').textContent = `Support ${fmt(sup)} · Resistance ${fmt(res)}`;
    }
    paint(panel, data);
  } catch (e) {
    if (el) el.innerHTML = `<div class="loading">${e.message || e}</div>`;
  }
}

async function refreshAll() {
  await Promise.all(panelState.map((_, i) => refreshPanel(i)));
}

function setup() {
  renderPrimitiveSelect();
  renderChips();
  fillTimeframeSelects();
  $('add-primitive').addEventListener('click', () => {
    const val = $('primitive-select').value;
    if (val && !selected.includes(val)) {
      selected.push(val);
      renderChips();
      refreshAll();
    }
  });
  $('refresh').addEventListener('click', refreshAll);
  $('preset-select').addEventListener('change', () => setPreset($('preset-select').value));
  $('symbol').addEventListener('change', refreshAll);
  $('expiry').addEventListener('change', refreshAll);
  if (qs().get('symbol')) $('symbol').value = qs().get('symbol').toUpperCase();
  if (qs().get('expiry')) $('expiry').value = qs().get('expiry');
  if (qs().get('preset')) $('preset-select').value = qs().get('preset');
  if (qs().get('overlays')) {
    const p = qs().get('overlays').split(',').map(s => s.trim()).filter(Boolean);
    selected = p.length ? p : selected;
  }
  renderChips();
  setPreset($('preset-select').value);
  refreshAll();
}

window.addEventListener('DOMContentLoaded', setup);
