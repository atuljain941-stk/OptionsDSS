'use strict';

function $(id) { return document.getElementById(id); }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.ok === false) throw new Error(d.error || r.statusText);
  return d;
}

let jobs = [];

function fmtRelative(iso) {
  if (!iso) return 'never run';
  try {
    const d = new Date(iso);
    const diffSec = Math.round((Date.now() - d.getTime()) / 1000);
    if (diffSec < 60) return `${diffSec}s ago`;
    if (diffSec < 3600) return `${Math.round(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.round(diffSec / 3600)}h ago`;
    return `${Math.round(diffSec / 86400)}d ago`;
  } catch { return iso; }
}

function scheduleText(job) {
  if (job.kind === 'interval') {
    const min = job.schedule.interval_min || job.default_schedule.interval_min || '?';
    return `every ${min} min`;
  }
  const times = (job.schedule.times || job.default_schedule.times || []).join(', ');
  const wd = job.schedule.weekdays !== undefined ? job.schedule.weekdays : job.default_schedule.weekdays;
  const wdNames = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
  const wdText = wd ? ` (${wd.map(w => wdNames[w]).join('/')})` : ' (daily)';
  return `${times}${wdText}`;
}

function jobEditorHtml(job) {
  if (!job.editable) {
    return `<div class="sh-readonly-note">⚠ Schedule is managed elsewhere for this job — this switch and "Run now" still work here.</div>`;
  }
  if (job.kind === 'interval') {
    const min = job.schedule.interval_min || job.default_schedule.interval_min || 15;
    return `
      <div class="sh-schedule-row">
        <span>Every</span>
        <input type="number" class="sh-input" style="width:70px" min="1" value="${min}" data-role="interval-input" data-key="${job.key}" />
        <span>minutes</span>
        <button class="sh-btn" data-action="save-interval" data-key="${job.key}">Save</button>
      </div>`;
  }
  const times = job.schedule.times || job.default_schedule.times || [];
  const chips = times.map((t, i) => `<span class="sh-times-chip">${esc(t)}<button data-action="remove-time" data-key="${job.key}" data-idx="${i}">✕</button></span>`).join('');
  return `
    <div class="sh-schedule-row" data-times-row="${job.key}">
      ${chips}
      <input type="time" class="sh-input" data-role="time-input" data-key="${job.key}" />
      <button class="sh-btn" data-action="add-time" data-key="${job.key}">+ Add time</button>
      <button class="sh-btn" data-action="save-times" data-key="${job.key}">Save</button>
    </div>`;
}

function jobHtml(job) {
  const statusColor = job.last_status === 'ok' ? 'var(--sh-good)' : (job.last_status === 'error' ? 'var(--sh-bad)' : 'var(--sh-muted)');
  return `
    <div class="sh-job" data-key="${job.key}">
      <div class="sh-job-head">
        <div class="sh-job-title">
          <label class="sh-switch"><input type="checkbox" data-action="toggle-enabled" data-key="${job.key}" ${job.enabled ? 'checked' : ''}><span class="sh-slider"></span></label>
          ${esc(job.label)}
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          ${job.is_running ? `<span style="color:#fbbf24;font-weight:800;font-size:11px;display:inline-flex;align-items:center;gap:5px"><span style="width:7px;height:7px;border-radius:50%;background:#fbbf24;display:inline-block;animation:sh-pulse 1.2s infinite"></span>Running now (${Math.round(job.running_seconds)}s)</span>` : `<span style="font-size:11px;color:${statusColor}">${job.last_run_at ? fmtRelative(job.last_run_at) : 'never run'}</span>`}
          ${job.can_run_now ? `<button class="sh-btn" data-action="run-now" data-key="${job.key}" ${job.is_running ? 'disabled' : ''}>▶ Run now</button>` : ''}
        </div>
      </div>
      <div class="sh-job-desc">${esc(job.description)}</div>
      <div class="sh-job-meta">
        <span>Schedule: <b>${esc(scheduleText(job))}</b></span>
        ${job.last_note ? `<span>Last: ${esc(job.last_note)}</span>` : ''}
      </div>
      ${jobEditorHtml(job)}
    </div>`;
}

function render() {
  const groups = {};
  for (const j of jobs) {
    (groups[j.group] = groups[j.group] || []).push(j);
  }
  const wrap = $('sh-groups');
  if (!jobs.length) {
    wrap.innerHTML = '<div class="sh-empty">No background jobs registered yet — they show up here once the app has been running for a moment.</div>';
    return;
  }
  wrap.innerHTML = Object.keys(groups).sort().map(g => `
    <div class="sh-group-title">${esc(g)}</div>
    ${groups[g].map(jobHtml).join('')}
  `).join('');
}

async function refresh() {
  const d = await api('/scheduler-hub/api/jobs');
  jobs = d.jobs || [];
  render();
}

function bindEvents() {
  document.body.addEventListener('click', async e => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;
    const key = btn.dataset.key;
    const action = btn.dataset.action;

    if (action === 'run-now') {
      btn.disabled = true;
      const prev = btn.textContent;
      btn.textContent = '⏳';
      try {
        const r = await api(`/scheduler-hub/api/jobs/${key}/run`, { method: 'POST' });
        alert(r.message || 'Started.');
      } catch (err) {
        alert(`Failed: ${err.message || err}`);
      } finally {
        btn.disabled = false;
        btn.textContent = prev;
        setTimeout(refresh, 1500);
      }
    } else if (action === 'save-interval') {
      const input = document.querySelector(`[data-role="interval-input"][data-key="${key}"]`);
      const minutes = parseInt(input?.value || '15', 10) || 15;
      try {
        await api(`/scheduler-hub/api/jobs/${key}/schedule`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: 'interval', interval_min: minutes }),
        });
        await refresh();
      } catch (err) {
        alert(`Failed to save: ${err.message || err}`);
      }
    } else if (action === 'add-time') {
      const input = document.querySelector(`[data-role="time-input"][data-key="${key}"]`);
      const val = input?.value;
      if (!val) return;
      const job = jobs.find(j => j.key === key);
      const times = job.schedule.times || job.default_schedule.times || [];
      if (!times.includes(val)) times.push(val);
      job.schedule = { ...job.schedule, times };
      render();
    } else if (action === 'remove-time') {
      const idx = Number(btn.dataset.idx);
      const job = jobs.find(j => j.key === key);
      const times = (job.schedule.times || job.default_schedule.times || []).slice();
      times.splice(idx, 1);
      job.schedule = { ...job.schedule, times };
      render();
    } else if (action === 'save-times') {
      const job = jobs.find(j => j.key === key);
      const times = job.schedule.times || job.default_schedule.times || [];
      if (!times.length) { alert('Add at least one time first.'); return; }
      try {
        await api(`/scheduler-hub/api/jobs/${key}/schedule`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ kind: 'time', times, weekdays: job.schedule.weekdays ?? job.default_schedule.weekdays ?? null }),
        });
        await refresh();
      } catch (err) {
        alert(`Failed to save: ${err.message || err}`);
      }
    }
  });

  document.body.addEventListener('change', async e => {
    const el = e.target.closest('[data-action="toggle-enabled"]');
    if (!el) return;
    const key = el.dataset.key;
    try {
      await api(`/scheduler-hub/api/jobs/${key}/enabled`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: el.checked }),
      });
    } catch (err) {
      alert(`Failed to toggle: ${err.message || err}`);
      el.checked = !el.checked;
    }
  });
}

async function init() {
  bindEvents();
  await refresh();
  setInterval(refresh, 30000);
}

window.addEventListener('DOMContentLoaded', init);
