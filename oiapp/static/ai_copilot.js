'use strict';

function $(id) { return document.getElementById(id); }

// Global fetch timeout, shared across Scanner Builder/Dashboard/Signal
// Notifier/AI Copilot. Configured once from Scanner Builder → Settings.
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

function showError(id, msg) {
  const el = $(id);
  if (!msg) { el.style.display = 'none'; el.textContent = ''; return; }
  el.textContent = `⚠ ${msg}`;
  el.style.display = 'block';
}

async function refreshLlmStatus() {
  try {
    const cfg = await api('/ai-copilot/settings');
    const pill = $('cp-llm-status');
    if (cfg.configured) {
      pill.textContent = `LLM: connected (${cfg.model})`;
      pill.className = 'cp-status-pill ok';
    } else {
      pill.textContent = 'LLM: not configured';
      pill.className = 'cp-status-pill bad';
    }
    $('cp-model').value = cfg.model || '';
  } catch (err) {
    $('cp-llm-status').textContent = 'LLM: unknown';
  }
}

async function refreshBreakerStatus() {
  try {
    const b = await api('/ai-copilot/circuit-breaker');
    const pill = $('cp-breaker-status');
    $('cp-max-daily-loss').value = b.max_daily_loss || 0;
    $('cp-max-weekly-loss').value = b.max_weekly_loss || 0;
    if (b.halt_new_trades) {
      pill.textContent = `Circuit breaker: BREACHED (day $${b.daily_pnl}, week $${b.weekly_pnl})`;
      pill.className = 'cp-status-pill bad';
    } else {
      pill.textContent = `Circuit breaker: OK (day $${b.daily_pnl}, week $${b.weekly_pnl})`;
      pill.className = 'cp-status-pill ok';
    }
  } catch (err) {
    $('cp-breaker-status').textContent = 'Circuit breaker: unknown';
  }
}

function bindSettings() {
  $('cp-save-settings').addEventListener('click', async () => {
    const btn = $('cp-save-settings');
    btn.disabled = true;
    try {
      await api('/ai-copilot/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: $('cp-api-key').value, model: $('cp-model').value }),
      });
      $('cp-api-key').value = '';
      await refreshLlmStatus();
    } catch (err) {
      alert(`Failed to save: ${err.message || err}`);
    } finally {
      btn.disabled = false;
    }
  });

  $('cp-save-breaker').addEventListener('click', async () => {
    const btn = $('cp-save-breaker');
    btn.disabled = true;
    try {
      await api('/ai-copilot/circuit-breaker', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          max_daily_loss: parseFloat($('cp-max-daily-loss').value || '0') || 0,
          max_weekly_loss: parseFloat($('cp-max-weekly-loss').value || '0') || 0,
        }),
      });
      await refreshBreakerStatus();
    } catch (err) {
      alert(`Failed to save: ${err.message || err}`);
    } finally {
      btn.disabled = false;
    }
  });
}

// ── Post-mortem ────────────────────────────────────────────────────────

function renderPostmortemHistory(reports) {
  const wrap = $('cp-pm-history');
  wrap.innerHTML = reports.length ? reports.map(r => `
    <div class="cp-history-item">
      <div class="meta">${esc(r.created_at)} · ${r.period_days}d · ${r.trades_analyzed} trades · ${r.alerts_analyzed} alerts</div>
      <div class="text">${esc(r.report_text || '')}</div>
    </div>
  `).join('') : '<div class="cp-legend">No past reports yet.</div>';
}

async function runPostmortem() {
  const btn = $('cp-run-postmortem');
  const days = parseInt($('cp-pm-days').value || '90', 10);
  showError('cp-pm-error', '');
  $('cp-pm-output').style.display = 'none';
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '⏳ Analyzing…';
  try {
    const result = await api('/ai-copilot/postmortem/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ days }), timeoutMs: 120000,
    });
    if (result.ok) {
      $('cp-pm-output').textContent = result.report_text;
      $('cp-pm-output').style.display = 'block';
      const hist = await api('/ai-copilot/postmortem/history');
      renderPostmortemHistory(hist.reports || []);
    } else {
      showError('cp-pm-error', result.error || 'Failed to run post-mortem.');
    }
  } catch (err) {
    showError('cp-pm-error', err.message || String(err));
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ── Pre-trade risk check ─────────────────────────────────────────────────

function renderRiskCheckHistory(checks) {
  const wrap = $('cp-rc-history');
  wrap.innerHTML = checks.length ? checks.map(c => `
    <div class="cp-history-item">
      <div class="meta">${esc(c.created_at)} · <b>${esc(c.symbol)}</b> ${esc(c.trade_type || '')} · <span class="cp-verdict ${esc(c.verdict)}" style="padding:2px 8px;font-size:11px;margin:0">${esc(c.verdict)}</span></div>
      <div class="text">${esc(c.reasoning || '')}</div>
    </div>
  `).join('') : '<div class="cp-legend">No past checks yet.</div>';
}

async function runRiskCheck() {
  const btn = $('cp-run-riskcheck');
  const symbol = $('cp-rc-symbol').value.trim();
  if (!symbol) { showError('cp-rc-error', 'Symbol is required.'); return; }
  showError('cp-rc-error', '');
  $('cp-rc-result').style.display = 'none';
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '⏳ Checking…';
  try {
    const result = await api('/ai-copilot/risk-check/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        symbol,
        trade_type: $('cp-rc-type').value.trim(),
        direction: $('cp-rc-direction').value,
        sector: $('cp-rc-sector').value.trim() || null,
        risk_amt: $('cp-rc-risk').value ? parseFloat($('cp-rc-risk').value) : null,
        notes: $('cp-rc-notes').value.trim(),
      }),
    });
    if (result.ok) {
      $('cp-rc-verdict').textContent = result.verdict;
      $('cp-rc-verdict').className = `cp-verdict ${result.verdict}`;
      $('cp-rc-reasoning').textContent = result.reasoning || '';
      $('cp-rc-flags').innerHTML = (result.risk_flags || []).map(f => `<span class="cp-flag">${esc(f)}</span>`).join('');
      $('cp-rc-sizing').textContent = result.sizing_note ? `Sizing: ${result.sizing_note}` : '';
      $('cp-rc-result').style.display = 'block';
      const hist = await api('/ai-copilot/risk-check/history');
      renderRiskCheckHistory(hist.checks || []);
    } else {
      showError('cp-rc-error', result.error || 'Failed to run risk check.');
    }
  } catch (err) {
    showError('cp-rc-error', err.message || String(err));
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

// ── Daily briefing ────────────────────────────────────────────────────────

function renderBriefingHistory(briefings) {
  const wrap = $('cp-briefing-history');
  wrap.innerHTML = briefings.length ? briefings.map(b => `
    <div class="cp-history-item">
      <div class="meta">${esc(b.created_at)} ${b.sent_telegram ? '· sent to Telegram' : ''}</div>
      <div class="text">${esc(b.briefing_text || '')}</div>
    </div>
  `).join('') : '<div class="cp-legend">No past briefings yet.</div>';
}

async function runBriefing() {
  const btn = $('cp-run-briefing');
  showError('cp-briefing-error', '');
  $('cp-briefing-output').style.display = 'none';
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = '⏳ Writing…';
  try {
    const result = await api('/ai-copilot/briefing/run', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ send_telegram: $('cp-briefing-telegram').checked }),
    });
    if (result.ok) {
      $('cp-briefing-output').textContent = result.briefing_text;
      $('cp-briefing-output').style.display = 'block';
      const hist = await api('/ai-copilot/briefing/history');
      renderBriefingHistory(hist.briefings || []);
    } else {
      showError('cp-briefing-error', result.error || 'Failed to generate briefing.');
    }
  } catch (err) {
    showError('cp-briefing-error', err.message || String(err));
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function bindToggle(toggleId, panelId, loadFn) {
  let loaded = false;
  $(toggleId).addEventListener('click', async () => {
    const panel = $(panelId);
    const show = panel.style.display === 'none';
    panel.style.display = show ? 'block' : 'none';
    $(toggleId).textContent = $(toggleId).textContent.replace(show ? '▾' : '▴', show ? '▴' : '▾');
    if (show && !loaded) {
      loaded = true;
      try { await loadFn(); } catch (err) { console.warn(err); }
    }
  });
}

async function init() {
  await loadGlobalApiTimeout();
  bindSettings();
  $('cp-run-postmortem').addEventListener('click', runPostmortem);
  $('cp-run-riskcheck').addEventListener('click', runRiskCheck);
  $('cp-run-briefing').addEventListener('click', runBriefing);

  bindToggle('cp-pm-history-toggle', 'cp-pm-history', async () => {
    const hist = await api('/ai-copilot/postmortem/history');
    renderPostmortemHistory(hist.reports || []);
  });
  bindToggle('cp-rc-history-toggle', 'cp-rc-history', async () => {
    const hist = await api('/ai-copilot/risk-check/history');
    renderRiskCheckHistory(hist.checks || []);
  });
  bindToggle('cp-briefing-history-toggle', 'cp-briefing-history', async () => {
    const hist = await api('/ai-copilot/briefing/history');
    renderBriefingHistory(hist.briefings || []);
  });

  await Promise.all([refreshLlmStatus(), refreshBreakerStatus()]);
}

window.addEventListener('DOMContentLoaded', init);
