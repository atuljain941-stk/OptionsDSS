from pathlib import Path
p=Path('/mnt/data/work_v93/oiapp/static/app.js')
s=p.read_text()
s=s.replace("""function _jrnCloseRollUpdateNetFromLegs() {
  const sp = parseFloat(document.getElementById('jrn-close-roll-new-short-price')?.value || '');
  const lp = parseFloat(document.getElementById('jrn-close-roll-new-long-price')?.value || '');
  if (Number.isFinite(sp) && Number.isFinite(lp)) {
    const net = document.getElementById('jrn-close-roll-new-net');
    if (net) net.value = (sp - lp).toFixed(2);
  }
  _jrnCloseRollPreview();
}
""", """function _jrnCloseRollUpdateNetFromLegs() {
  const sp = parseFloat(document.getElementById('jrn-close-roll-new-short-price')?.value || '');
  const lp = parseFloat(document.getElementById('jrn-close-roll-new-long-price')?.value || '');
  if (Number.isFinite(sp) && Number.isFinite(lp)) {
    const actualNewOpen = sp - lp;
    const closeNet = _jrnCloseNetForSelected();
    const netRoll = actualNewOpen - closeNet;
    const net = document.getElementById('jrn-close-roll-new-net');
    if (net) net.value = netRoll.toFixed(2);
  }
  _jrnCloseRollPreview();
}
""")
s=s.replace("""  const newNetRaw = document.getElementById('jrn-close-roll-new-net')?.value;
  const newNet = newNetRaw === '' || newNetRaw == null ? NaN : parseFloat(newNetRaw);
  const adj = Number.isFinite(newNet) ? (newNet - closeNet) : NaN;
  const effective = Number.isFinite(adj) ? (oldNet + adj) : NaN;
  const adjColor = Number.isFinite(adj) && adj >= 0 ? '#22c55e' : '#ef4444';
  const effColor = Number.isFinite(effective) && effective >= 0 ? '#22c55e' : '#ef4444';
""", """  const rollNetRaw = document.getElementById('jrn-close-roll-new-net')?.value;
  const rollNet = rollNetRaw === '' || rollNetRaw == null ? NaN : parseFloat(rollNetRaw);
  const impliedNewOpen = Number.isFinite(rollNet) ? (closeNet + rollNet) : NaN;
  const effective = Number.isFinite(rollNet) ? (oldNet + rollNet) : NaN;
  const adjColor = Number.isFinite(rollNet) && rollNet >= 0 ? '#22c55e' : '#ef4444';
  const effColor = Number.isFinite(effective) && effective >= 0 ? '#22c55e' : '#ef4444';
""")
s=s.replace("""    `<div><b>${closeNet.toFixed(2)}</b><br><span style="color:var(--muted)">close cost/net</span></div>` +
    `<div><b>${Number.isFinite(newNet) ? (newNet>=0?'+':'') + newNet.toFixed(2) : '—'}</b><br><span style="color:var(--muted)">new credit/debit</span></div>` +
    `<div><b style="color:${adjColor}">${Number.isFinite(adj) ? (adj>=0?'+':'') + adj.toFixed(2) : '—'}</b><br><span style="color:var(--muted)">roll adjustment</span></div>` +
    `<div><b style="color:${effColor}">${Number.isFinite(effective) ? (effective>=0?'+':'') + effective.toFixed(2) : '—'}</b><br><span style="color:var(--muted)">effective side basis</span></div>` +
    `</div><div style="margin-top:6px;color:var(--muted);font-size:11px">New premium accepts signed values: positive = credit collected, negative = debit paid. Debit reduces the original collected premium; credit increases it. The new rolled trade is journaled with this carried basis by default.</div>`;
""", """    `<div><b>${closeNet.toFixed(2)}</b><br><span style="color:var(--muted)">close cost/net</span></div>` +
    `<div><b style="color:${adjColor}">${Number.isFinite(rollNet) ? (rollNet>=0?'+':'') + rollNet.toFixed(2) : '—'}</b><br><span style="color:var(--muted)">net roll credit/debit</span></div>` +
    `<div><b>${Number.isFinite(impliedNewOpen) ? (impliedNewOpen>=0?'+':'') + impliedNewOpen.toFixed(2) : '—'}</b><br><span style="color:var(--muted)">implied new open</span></div>` +
    `<div><b style="color:${effColor}">${Number.isFinite(effective) ? (effective>=0?'+':'') + effective.toFixed(2) : '—'}</b><br><span style="color:var(--muted)">effective position credit</span></div>` +
    `</div><div style="margin-top:6px;color:var(--muted);font-size:11px">Enter the total roll order cashflow: positive = credit collected, negative = debit paid. Example: old side +2.50 and roll debit -1.03 gives effective position credit +1.47. If short/long new leg premiums are entered, this net roll value is auto-derived as new spread credit minus close cost.</div>`;
""")
s=s.replace("""  const newShortPx = parseFloat(document.getElementById('jrn-close-roll-new-short-price')?.value || '');
  const newLongPx = parseFloat(document.getElementById('jrn-close-roll-new-long-price')?.value || '');
  const newNet = parseFloat(document.getElementById('jrn-close-roll-new-net')?.value || '');
  if (!newExpiry || !Number.isFinite(newShort) || !Number.isFinite(newLong)) { alert('Fill new expiry, new short strike, and new long strike.'); return; }
  if (!Number.isFinite(newNet) && !(Number.isFinite(newShortPx) && Number.isFinite(newLongPx))) { alert('Enter new net premium. Positive = credit, negative = debit.'); return; }
""", """  const newShortPx = parseFloat(document.getElementById('jrn-close-roll-new-short-price')?.value || '');
  const newLongPx = parseFloat(document.getElementById('jrn-close-roll-new-long-price')?.value || '');
  const rollNet = parseFloat(document.getElementById('jrn-close-roll-new-net')?.value || '');
  if (!newExpiry || !Number.isFinite(newShort) || !Number.isFinite(newLong)) { alert('Fill new expiry, new short strike, and new long strike.'); return; }
  if (!Number.isFinite(rollNet) && !(Number.isFinite(newShortPx) && Number.isFinite(newLongPx))) { alert('Enter net roll credit/debit. Positive = credit, negative = debit.'); return; }
""")
s=s.replace("""  let sellPx, buyPx;
  if (Number.isFinite(newShortPx) && Number.isFinite(newLongPx)) {
    sellPx = newShortPx; buyPx = newLongPx;
  } else if (Number.isFinite(newNet) && newNet >= 0) {
    sellPx = newNet; buyPx = 0;
  } else {
    sellPx = 0; buyPx = Math.abs(newNet || 0);
  }
""", """  const closeNetForRoll = _jrnCloseNetForSelected();
  const hasLegPremiums = Number.isFinite(newShortPx) && Number.isFinite(newLongPx);
  const signedRollNet = Number.isFinite(rollNet) ? rollNet : ((newShortPx - newLongPx) - closeNetForRoll);
  const actualNewOpenNet = hasLegPremiums ? (newShortPx - newLongPx) : (closeNetForRoll + signedRollNet);
  let sellPx, buyPx;
  if (hasLegPremiums) {
    sellPx = newShortPx; buyPx = newLongPx;
  } else if (Number.isFinite(actualNewOpenNet) && actualNewOpenNet >= 0) {
    sellPx = actualNewOpenNet; buyPx = 0;
  } else {
    sellPx = 0; buyPx = Math.abs(actualNewOpenNet || 0);
  }
""")
s=s.replace("""    new_legs: newLegs,
    signed_new_net: Number.isFinite(newNet) ? newNet : (sellPx - buyPx),
    effective_side_basis_preview: _jrnCloseEntryNetForSelected() + ((Number.isFinite(newNet) ? newNet : (sellPx-buyPx)) - _jrnCloseNetForSelected()),
    roll_basis_mode: 'carry_forward',
""", """    new_legs: newLegs,
    signed_roll_adjustment: signedRollNet,
    net_roll_credit_debit: signedRollNet,
    roll_net_is_cashflow: true,
    actual_new_entry_net: actualNewOpenNet,
    new_leg_prices_provided: hasLegPremiums,
    signed_new_net: actualNewOpenNet,
    effective_side_basis_preview: _jrnCloseEntryNetForSelected() + signedRollNet,
    roll_basis_mode: 'realized_plus_actual_new_with_effective_note',
""")
s=s.replace("""    const adj = Number(r.roll_adjustment || 0);
    const basis = Number(r.cumulative_side_basis || 0);
    alert(`Roll complete. Closed P&L: ${r.closed_pnl>=0?'+':''}$${Number(r.closed_pnl||0).toFixed(2)}. Roll adjustment: ${adj>=0?'+':''}${adj.toFixed(2)}. Carried basis: ${basis>=0?'+':''}${basis.toFixed(2)}. New trade #${r.new_trade_id || ''}. Journal premium was adjusted for credit/debit carry-forward.`);
""", """    const adj = Number(r.roll_adjustment || 0);
    const basis = Number(r.cumulative_side_basis || 0);
    const actual = Number(r.actual_new_entry_net || 0);
    alert(`Roll complete. Closed P&L: ${r.closed_pnl>=0?'+':''}$${Number(r.closed_pnl||0).toFixed(2)}. Net roll cashflow: ${adj>=0?'+':''}${adj.toFixed(2)}. Effective position credit: ${basis>=0?'+':''}${basis.toFixed(2)}. New trade #${r.new_trade_id || ''}${actual ? ` (new open net ${actual>=0?'+':''}${actual.toFixed(2)})` : ''}.`);
""")
# Legacy roll modal: make net derived as net roll cashflow too and send explicit flags.
s=s.replace("""function _jrnRollUpdateNetFromLegs(){
  const sp=parseFloat(document.getElementById('jrn-roll-new-short-price')?.value || '');
  const lp=parseFloat(document.getElementById('jrn-roll-new-long-price')?.value || '');
  if(Number.isFinite(sp) && Number.isFinite(lp)){
    const net=document.getElementById('jrn-roll-new-price'); if(net) net.value=(sp-lp).toFixed(2);
  }
  _jrnRollPreview();
}
""", """function _jrnRollUpdateNetFromLegs(){
  const sp=parseFloat(document.getElementById('jrn-roll-new-short-price')?.value || '');
  const lp=parseFloat(document.getElementById('jrn-roll-new-long-price')?.value || '');
  if(Number.isFinite(sp) && Number.isFinite(lp)){
    const closeNet=parseFloat(document.getElementById('jrn-roll-close-price')?.value || '0') || 0;
    const net=document.getElementById('jrn-roll-new-price'); if(net) net.value=((sp-lp)-Math.abs(closeNet)).toFixed(2);
  }
  _jrnRollPreview();
}
""")
s=s.replace("""  const closeNet=parseFloat(document.getElementById('jrn-roll-close-price')?.value || '0') || 0;
  const newNet=parseFloat(document.getElementById('jrn-roll-new-price')?.value || '0') || 0;
  const adjustment=newNet-closeNet;
  const c=adjustment>=0?'#22c55e':'#ef4444';
  host.innerHTML = `${selected.length} leg(s) selected · qty ${q} · roll adjustment <b style="color:${c}">${adjustment>=0?'+':''}${adjustment.toFixed(2)}</b> per spread unit. Positive means extra credit; negative means debit paid.`;
""", """  const closeNet=Math.abs(parseFloat(document.getElementById('jrn-roll-close-price')?.value || '0') || 0);
  const rollNet=parseFloat(document.getElementById('jrn-roll-new-price')?.value || '0') || 0;
  const c=rollNet>=0?'#22c55e':'#ef4444';
  const actualNewOpen = closeNet + rollNet;
  host.innerHTML = `${selected.length} leg(s) selected · qty ${q} · net roll cashflow <b style="color:${c}">${rollNet>=0?'+':''}${rollNet.toFixed(2)}</b> per spread unit · implied new open ${actualNewOpen>=0?'+':''}${actualNewOpen.toFixed(2)}.`;
""")
s=s.replace("""  const newNet=parseFloat(document.getElementById('jrn-roll-new-price')?.value || '');
  if(!newExpiry || !Number.isFinite(newShort) || !Number.isFinite(newLong)) { alert('Fill new expiry, new short strike, and new long strike.'); return; }
  if(!Number.isFinite(newNet) && !(Number.isFinite(newShortPx) && Number.isFinite(newLongPx))) { alert('Fill new net credit/debit or both leg premiums.'); return; }
""", """  const rollNet=parseFloat(document.getElementById('jrn-roll-new-price')?.value || '');
  if(!newExpiry || !Number.isFinite(newShort) || !Number.isFinite(newLong)) { alert('Fill new expiry, new short strike, and new long strike.'); return; }
  if(!Number.isFinite(rollNet) && !(Number.isFinite(newShortPx) && Number.isFinite(newLongPx))) { alert('Fill net roll credit/debit or both leg premiums.'); return; }
""")
s=s.replace("""  let sellPx, buyPx;
  if (Number.isFinite(newShortPx) && Number.isFinite(newLongPx)) {
    sellPx = newShortPx; buyPx = newLongPx;
  } else if (Number.isFinite(newNet) && newNet >= 0) {
    sellPx = newNet; buyPx = 0;
  } else if (Number.isFinite(newNet)) {
    sellPx = 0; buyPx = Math.abs(newNet);
  } else {
    sellPx = 0; buyPx = 0;
  }
""", """  const closeNetForRoll = Math.abs(parseFloat(document.getElementById('jrn-roll-close-price')?.value || '0') || 0);
  const hasLegPremiums = Number.isFinite(newShortPx) && Number.isFinite(newLongPx);
  const signedRollNet = Number.isFinite(rollNet) ? rollNet : ((newShortPx - newLongPx) - closeNetForRoll);
  const actualNewOpenNet = hasLegPremiums ? (newShortPx - newLongPx) : (closeNetForRoll + signedRollNet);
  let sellPx, buyPx;
  if (hasLegPremiums) {
    sellPx = newShortPx; buyPx = newLongPx;
  } else if (Number.isFinite(actualNewOpenNet) && actualNewOpenNet >= 0) {
    sellPx = actualNewOpenNet; buyPx = 0;
  } else if (Number.isFinite(actualNewOpenNet)) {
    sellPx = 0; buyPx = Math.abs(actualNewOpenNet);
  } else {
    sellPx = 0; buyPx = 0;
  }
""")
s=s.replace("""    new_expiry:newExpiry,
    new_legs:newLegs,
    roll_reason: document.getElementById('jrn-roll-reason')?.value || ''
""", """    new_expiry:newExpiry,
    new_legs:newLegs,
    signed_roll_adjustment:signedRollNet,
    net_roll_credit_debit:signedRollNet,
    roll_net_is_cashflow:true,
    actual_new_entry_net:actualNewOpenNet,
    new_leg_prices_provided:hasLegPremiums,
    signed_new_net:actualNewOpenNet,
    roll_reason: document.getElementById('jrn-roll-reason')?.value || ''
""")
p.write_text(s)

# Patch labels in templates
for fp in [Path('/mnt/data/work_v93/templates/index.html'), Path('/mnt/data/work_v93/templates/index_mine.html')]:
    if not fp.exists(): continue
    t=fp.read_text()
    t=t.replace('New net credit/debit', 'Net roll credit/debit')
    t=t.replace('credit + / debit -', 'overall roll +credit / -debit')
    t=t.replace('+credit / -debit', 'overall +credit / -debit')
    t=t.replace('New net premium is signed: positive credit, negative debit. The new rolled trade carries adjusted basis from old premium ± close/roll cashflow.', 'Net roll premium is the total roll order cashflow: positive credit, negative debit. Example: old +2.50 and roll debit -1.03 = effective +1.47. If leg prices are entered, the app derives this from new spread credit minus close cost.')
    t=t.replace('Close selected side at net price', 'Close selected side at net price')
    fp.write_text(t)
