// schwab_ui.js
// Extracted from app.js -- Schwab broker OAuth/credentials integration
// (auth status, save credentials, get auth URL, exchange code, refresh
// token). Fully self-contained, zero external references either
// direction. Plain global-scope script, same as every other file here.

function _schwabAuthLabel(d) {
  const hasToken = !!(d && d.access_token);
  if (!hasToken) return {ok:false, color:'#f59e0b', html:'⚠ Not connected — connect Schwab below to get real futures OI'};
  const exp = Number(d.expires_in_seconds);
  if (Number.isFinite(exp) && exp <= 0) {
    return {ok:false, color:'#f59e0b', html:'⚠ Schwab access token is expired — click Refresh Token or re-authorize if refresh fails'};
  }
  if (!d.has_refresh_token) {
    return {ok:false, color:'#f59e0b', html:'⚠ Schwab access token exists but no refresh token is stored — re-authorize soon'};
  }
  const min = Number.isFinite(exp) ? Math.max(0, Math.round(exp / 60)) : null;
  return {ok:true, color:'#22c55e', html:'✅ Schwab connected — real exchange OI active' + (min !== null ? ` · token ${min}m` : '')};
}

async function _loadFuturesConfig() {
  const authEl = document.getElementById('futures-auth-status');
  try {
    const d = await api('/schwab/config');
    const lbl = _schwabAuthLabel(d);
    if (authEl) authEl.innerHTML = '<span style="color:' + lbl.color + '">' + lbl.html + '</span>';
  } catch {
    if (authEl) authEl.innerHTML = '<span style="color:var(--muted)">⚠ Could not check Schwab status</span>';
  }
}

// ════════════════════════════════════════════════════════════════════════════
// SCHWAB CONNECT — Scheduler Tab
// ════════════════════════════════════════════════════════════════════════════
async function _schwabCheckStatus() {
  const badge = document.getElementById('schwab-conn-badge');
  const authEl= document.getElementById('futures-auth-status');
  try {
    const d = await api('/schwab/config');
    const lbl = _schwabAuthLabel(d);
    if (badge) {
      badge.textContent    = lbl.ok ? '✅ Connected' : '⚠ Needs auth';
      badge.style.background = lbl.ok ? 'rgba(34,197,94,.15)'  : 'rgba(245,158,11,.15)';
      badge.style.color      = lbl.color;
    }
    if (authEl) authEl.innerHTML = '<span style="color:' + lbl.color + '">' + lbl.html + '</span>';
    // Pre-fill keys if already saved (show hint only)
    if (d && d.app_key) {
      const keyEl = document.getElementById('schwab-app-key');
      if (keyEl && !keyEl.value) keyEl.placeholder = d.app_key.slice(0,8) + '... (already saved)';
    }
  } catch(e) {
    if (badge) { badge.textContent = '❌ Error'; badge.style.color = '#ef4444'; }
  }
}

async function _schwabSaveCreds() {
  const key    = document.getElementById('schwab-app-key')?.value?.trim();
  const secret = document.getElementById('schwab-app-secret')?.value?.trim();
  const st     = document.getElementById('schwab-status');
  if (!key || !secret) {
    if (st) st.innerHTML = '<span style="color:#ef4444">Enter both App Key and App Secret</span>';
    return;
  }
  try {
    await api('/schwab/config', { method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ app_key: key, app_secret: secret })
    });
    if (st) st.innerHTML = '<span style="color:#22c55e">✅ Credentials saved — now click � Get Auth URL</span>';
    document.getElementById('schwab-app-secret').value = '';
  } catch(e) {
    if (st) st.innerHTML = '<span style="color:#ef4444">❌ ' + e.message + '</span>';
  }
}

async function _schwabGetAuthUrl() {
  const st = document.getElementById('schwab-status');
  try {
    const d = await api('/schwab/auth_url');
    if (d.url) {
      window.open(d.url, '_blank');
      const row = document.getElementById('schwab-redirect-row');
      if (row) row.style.display = '';
      if (st) st.innerHTML = '<span style="color:#f59e0b">� Browser opened — log in to Schwab, authorize, then paste the redirect URL below</span>';
    } else {
      if (st) st.innerHTML = '<span style="color:#ef4444">❌ ' + (d.error||'Save credentials first') + '</span>';
    }
  } catch(e) {
    if (st) st.innerHTML = '<span style="color:#ef4444">❌ ' + e.message + ' — save credentials first</span>';
  }
}

async function _schwabExchangeCode() {
  const url = document.getElementById('schwab-redirect-url')?.value?.trim();
  const st  = document.getElementById('schwab-status');
  if (!url) { if (st) st.innerHTML = '<span style="color:#ef4444">Paste the redirect URL first</span>'; return; }
  // Extract the code from the URL
  let code = '';
  try { code = new URL(url).searchParams.get('code') || ''; } catch {}
  if (!code) code = url.match(/code=([^&]+)/)?.[1] || '';
  if (!code) {
    if (st) st.innerHTML = '<span style="color:#ef4444">❌ No code found in URL. Copy the full URL from browser.</span>';
    return;
  }
  try {
    const d = await api('/schwab/exchange_code', { method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ code })
    });
    if (d.ok || d.access_token) {
      if (st) st.innerHTML = '<span style="color:#22c55e">✅ Connected! Schwab will now provide real futures OI at 8 AM.</span>';
      document.getElementById('schwab-redirect-row').style.display = 'none';
      document.getElementById('schwab-redirect-url').value = '';
      setTimeout(_schwabCheckStatus, 500);
    } else {
      if (st) st.innerHTML = '<span style="color:#ef4444">❌ ' + (d.error||'Exchange failed') + '</span>';
    }
  } catch(e) {
    if (st) st.innerHTML = '<span style="color:#ef4444">❌ ' + e.message + '</span>';
  }
}

async function _schwabRefreshToken() {
  const st = document.getElementById('schwab-status');
  if (st) st.innerHTML = '<span style="color:var(--muted)">⏳ Refreshing token…</span>';
  try {
    const d = await api('/schwab/refresh', {method:'POST'});
    if (d.ok || d.access_token) {
      if (st) st.innerHTML = '<span style="color:#22c55e">✅ Token refreshed</span>';
      setTimeout(_schwabCheckStatus, 300);
    } else {
      if (st) st.innerHTML = '<span style="color:#ef4444">❌ ' + (d.error||'Refresh failed — re-authorize') + '</span>';
    }
  } catch(e) {
    if (st) st.innerHTML = '<span style="color:#ef4444">❌ ' + e.message + '</span>';
  }
}


