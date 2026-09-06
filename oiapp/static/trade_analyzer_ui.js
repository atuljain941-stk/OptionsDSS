// trade_analyzer_ui.js
// Extracted from app.js -- the Trade Analyzer page (P&L diagram, Greeks,
// step-through-time sliders, client-side Black-Scholes pricing engine).
// The scoring MODEL itself (specific point weights) was already moved
// server-side to /strategy/strategy_score earlier this session -- this
// file keeps only the pricing/P&L math, which is standard public
// finance math with no real secrecy value and needs to feel instant
// while dragging sliders.
// Two originally non-adjacent regions of app.js combined here (the main
// P&L/Greeks engine, and the separate IV Rank panel that had ended up
// physically separated by ~700 lines of unrelated Backtest/OI-charting
// code) -- grouped by what they actually ARE, not by where they
// happened to sit in the original file.
// Plain global-scope script (not an ES module), loaded via its own
// <script> tag -- order relative to app.js doesn't matter for these
// function-to-function calls (hoisted declarations, nothing here runs
// until a user interacts with the page).

// TRADE ANALYSIS PAGE — P&L Diagram + Greeks + Step-through Time

// State vars declared here so they are initialized before any function uses them
let _taLegs=[], _taData=null, _taSpotOffset=0, _taIvOffset=0, _taDaysOffset=0;
let _taBaseSpot=0, _taBaseIv=0.20, _taBaseDte=28;
let _taActiveGreek='delta';

// ─────────────────────────────────────────────────────────────────────────
// Client-side Black-Scholes (fully self-contained, no server needed for sliders)
// ─────────────────────────────────────────────────────────────────────────
function _bsNormCDF(x){
  if(x<0)return 1-_bsNormCDF(-x);
  const t=1/(1+0.2316419*x);
  const p=t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))));
  return 1-(1/Math.sqrt(2*Math.PI))*Math.exp(-0.5*x*x)*p;
}
function _bsNormPDF(x){ return Math.exp(-0.5*x*x)/Math.sqrt(2*Math.PI); }
function _bsPrice(S,K,T,iv,isCall,r=0.043){
  if(S<=0||K<=0||iv<=0) return isCall?Math.max(0,S-K):Math.max(0,K-S);
  if(T<=0) return isCall?Math.max(0,S-K):Math.max(0,K-S);
  const d1=(Math.log(S/K)+(r+0.5*iv*iv)*T)/(iv*Math.sqrt(T));
  const d2=d1-iv*Math.sqrt(T);
  if(isCall) return Math.max(0, S*_bsNormCDF(d1) - K*Math.exp(-r*T)*_bsNormCDF(d2));
  return Math.max(0, K*Math.exp(-r*T)*_bsNormCDF(-d2) - S*_bsNormCDF(-d1));
}
function _bsGreeks(S,K,T,iv,isCall,r=0.043){
  if(T<=0.5/365){
    return{price:isCall?Math.max(0,S-K):Math.max(0,K-S),delta:isCall?(S>K?1:0):(S<K?-1:0),gamma:0,theta:0,vega:0};
  }
  const sqT=Math.sqrt(T);
  const d1=(Math.log(S/K)+(r+0.5*iv*iv)*T)/(iv*sqT);
  const d2=d1-iv*sqT;
  const pdf1=_bsNormPDF(d1);
  const price=_bsPrice(S,K,T,iv,isCall,r);
  const delta=isCall?_bsNormCDF(d1):(_bsNormCDF(d1)-1);
  const gamma=pdf1/(S*iv*sqT);
  const theta=(-(S*pdf1*iv)/(2*sqT) - r*K*Math.exp(-r*T)*(isCall?_bsNormCDF(d2):_bsNormCDF(-d2)))/365;
  const vega=S*pdf1*sqT/100;
  const rho=K*T*Math.exp(-r*T)*(isCall?_bsNormCDF(d2):-_bsNormCDF(-d2))/100;
  return{price,delta,gamma,theta,vega,rho};
}


// Implied IV from market price (bisection method)
function _bsImpliedIV(S, K, T, marketPrice, isCall, r=0.043) {
  if (T <= 0 || marketPrice <= 0 || S <= 0 || K <= 0) return 0.20;
  const intrinsic = isCall ? Math.max(0, S - K) : Math.max(0, K - S);
  if (marketPrice <= intrinsic) return 0.01;
  let lo = 0.01, hi = 3.0;
  for (let iter = 0; iter < 50; iter++) {
    const mid = (lo + hi) / 2;
    const px = _bsPrice(S, K, T, mid, isCall, r);
    if (Math.abs(px - marketPrice) < 0.001) return mid;
    if (px > marketPrice) hi = mid;
    else lo = mid;
  }
  return (lo + hi) / 2;
}

// Compute total strategy P&L at a given spot/DTE/IV
function _taStratPnL(legs, S, dte, iv){
  let pnl=0;
  legs.forEach(leg=>{
    const K=parseFloat(leg.strike||0);
    const ep=parseFloat(leg.entry_price||leg.price||0);
    const q=parseInt(leg.qty||1);
    const sign=leg.side==='sell'?-1:1;
    if(leg.option_type==='stock'){
      pnl+=sign*(S-ep)*q;
    } else if(K>0){
      const isCall=leg.option_type==='call';
      const T=Math.max(0,dte/365);
      // Use per-leg implied IV if available (calibrated from entry price)
      // Then apply the IV OFFSET from the slider on top
      const baseIv = leg._impliedIv || parseFloat(leg.leg_iv||0)/100 || iv;
      const ivShift = iv - _taBaseIv;  // how much IV changed from base
      const legIv = Math.max(0.01, baseIv + ivShift);
      const curr = _bsPrice(S, K, T, legIv, isCall);
      pnl += sign * (curr - ep) * q * 100;
    }
  });
  return pnl;
}

// Compute aggregate greeks at spot
function _taStratGreeks(legs, S, dte, iv){
  let delta=0,gamma=0,theta=0,vega=0;
  legs.forEach(leg=>{
    if(leg.option_type==='stock'){
      delta+=parseInt(leg.qty||1)*(leg.side==='sell'?-1:1);
      return;
    }
    const K=parseFloat(leg.strike||0); if(!K) return;
    const isCall=leg.option_type==='call';
    const sign=leg.side==='sell'?-1:1;
    const q=parseInt(leg.qty||1);
    const T=Math.max(0.001,dte/365);
    const baseIv=leg._impliedIv||parseFloat(leg.leg_iv||0)/100||iv;
    const ivShift=iv-_taBaseIv;
    const legIv=Math.max(0.01,baseIv+ivShift);
    const g=_bsGreeks(S,K,T,legIv,isCall);
    delta+=sign*g.delta*q*100;
    gamma+=sign*g.gamma*q*100;
    theta+=sign*g.theta*q*100;
    vega +=sign*g.vega *q*100;
  });
  return{delta,gamma,theta,vega};
}

// ─────────────────────────────────────────────────────────────────────────
// BUILD PAGE
// ─────────────────────────────────────────────────────────────────────────
function _loadTradeAnalysis(preloadTrade){
  // HTML is now static in index.html. Just wire events on first load.
  const cv = document.getElementById('ta-canvas');
  if(cv && !cv.dataset.wired){
    cv.dataset.wired='1';
    cv.addEventListener('mousemove', _taHover);
    cv.addEventListener('mouseleave', ()=>{const t=document.getElementById('ta-tip');if(t)t.style.display='none';});
  }
  // Auto-fetch IV rank when symbol changes
  const symInput = document.getElementById('ta-sym');
  if (symInput && !symInput.dataset.ivWired) {
    symInput.dataset.ivWired = '1';
    symInput.addEventListener('blur', () => { if(symInput.value) _taFetchIVRank(symInput.value.toUpperCase()); });
  }

  if(preloadTrade){
    _taLoadFromTrade(preloadTrade);
  } else if(!cv?.dataset.loaded){
    cv.dataset.loaded='1';
    _taTemplate('Iron Condor');
    setTimeout(() => { try { _taAnalyze(); } catch (e) { console.warn(e); } }, 120);
  }
}

// ─────────────────────────────────────────────────────────────────────────
// Leg management
// ─────────────────────────────────────────────────────────────────────────
function _taAddLeg(){
  _taLegs.push({side:'buy',option_type:'call',strike:'',entry_price:'',qty:1,leg_iv:''});
  _taRenderLegs();
}
function _taRemoveLeg(i){_taLegs.splice(i,1);_taRenderLegs();}

// Update leg value without re-rendering (prevents focus loss)
function _taLegVal(i, field, val) {
  if (_taLegs[i]) {
    _taLegs[i][field] = val;
    // Just update net display
    const netEl = document.getElementById('ta-net');
    if (netEl) {
      let net = 0;
      _taLegs.forEach(l => {
        const px = parseFloat(l.entry_price || l.price || 0), q = parseInt(l.qty || 1);
        net += l.side === 'sell' ? px * q * 100 : -px * q * 100;
      });
      netEl.textContent = (net >= 0 ? '+' : '') + '$' + net.toFixed(2) + (net > 0 ? ' credit' : net < 0 ? ' debit' : '');
      netEl.style.color = net > 0 ? '#22c55e' : net < 0 ? '#ef4444' : 'var(--muted)';
    }
  }
}

function _taLegChange(i,f,v){if(_taLegs[i]){_taLegs[i][f]=v;_taRenderLegs();}}

function _taRenderLegs(){
  const tbody=document.getElementById('ta-legs');
  const netEl=document.getElementById('ta-net');
  if(!tbody) return;
  let net=0;
  _taLegs.forEach(l=>{
    const px=parseFloat(l.entry_price||l.price||0),q=parseInt(l.qty||1);
    net+=l.side==='sell'?px*q*100:-px*q*100;
  });
  if(netEl){
    netEl.textContent=(net>=0?'+':'')+'$'+net.toFixed(2)+(net>0?' credit':net<0?' debit':'');
    netEl.style.color=net>0?'#22c55e':net<0?'#ef4444':'var(--muted)';
  }
  const ns='appearance:textfield;-moz-appearance:textfield;-webkit-appearance:none;';
  tbody.innerHTML=_taLegs.map((leg,i)=>{
    const sC=leg.side==='sell'?'#f97316':'#3b82f6';
    const ep=parseFloat(leg.entry_price||leg.price||0),q=parseInt(leg.qty||1);
    const lpnl=(leg.side==='sell'?ep:-ep)*(leg.option_type==='stock'?1:100)*q;
    const lC=lpnl>0?'#22c55e':lpnl<0?'#ef4444':'#64748b';
    const dlt=leg._delta!=null?leg._delta.toFixed(3):'—';
    const dltC=(leg._delta||0)>0?'#22c55e':(leg._delta||0)<0?'#ef4444':'#64748b';
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:3px 4px">
        <select onchange="_taLegChange(${i},'side',this.value)" style="font-size:11px;color:${sC};background:${sC}18;border:1px solid ${sC}44;border-radius:3px;padding:2px">
          <option value="buy" ${leg.side==='buy'?'selected':''}>🔵 BUY</option>
          <option value="sell" ${leg.side==='sell'?'selected':''}>🟠 SELL</option>
        </select>
      </td>
      <td style="padding:3px 4px">
        <select onchange="_taLegChange(${i},'option_type',this.value)" style="font-size:11px;padding:2px">
          <option value="call"  ${leg.option_type==='call' ?'selected':''}>CALL</option>
          <option value="put"   ${leg.option_type==='put'  ?'selected':''}>PUT</option>
          <option value="stock" ${leg.option_type==='stock'?'selected':''}>STOCK</option>
        </select>
      </td>
      <td style="padding:3px 4px"><input type="text" inputmode="decimal" value="${leg.strike||''}" placeholder="Strike"
        onblur="_taLegStrikeBlur(${i},this.value)"
        style="width:70px;font-size:11px;${ns}${leg.option_type==='stock'?'opacity:.4':''}"/></td>
      <td style="padding:3px 4px"><input type="date" value="${leg.expiry||''}"
        onchange="_taLegExpiryChange(${i},this.value)"
        style="width:110px;font-size:10px;${leg.option_type==='stock'?'opacity:.4':''}"/></td>
      <td style="padding:3px 4px"><input type="text" inputmode="decimal" value="${leg.entry_price||leg.price||''}" placeholder="$"
        oninput="_taLegVal(${i},'entry_price',this.value)"
        onblur="_taLegChange(${i},'entry_price',this.value)"
        style="width:62px;font-size:11px;${ns}"/></td>
      <td style="padding:3px 4px"><input type="text" inputmode="numeric" value="${leg.qty||1}"
        oninput="_taLegVal(${i},'qty',this.value)"
        onblur="_taLegChange(${i},'qty',this.value)"
        style="width:38px;font-size:11px;${ns}"/></td>
      <td style="padding:3px 6px;font-weight:700;color:${lC};font-size:11px;white-space:nowrap">
        ${lpnl>=0?'+':''}$${lpnl.toFixed(0)}</td>
      <td style="padding:3px 4px;font-size:10px;color:${dltC};font-weight:700;text-align:center;min-width:44px"
        title="Delta for this leg">${dlt}</td>
      <td style="padding:3px 4px"><input type="text" inputmode="decimal"
        value="${leg._impliedIv ? (leg._impliedIv*100).toFixed(1) : (leg.leg_iv||'')}" placeholder="auto"
        oninput="_taLegVal(${i},'leg_iv',this.value)"
        onblur="_taLegChange(${i},'leg_iv',this.value)"
        style="width:44px;font-size:10px;${ns}${leg._impliedIv?'color:#a78bfa':''}"
        title="${leg._impliedIv ? 'Implied IV from entry price' : 'Per-leg IV override'}"/></td>
      <td style="padding:3px 2px"><button onclick="_taRemoveLeg(${i})"
        style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:13px;padding:0">✕</button></td>
    </tr>`;
  }).join('');
}


// Auto-fetch spot when symbol changes
async function _taSymbolChange(){
  const sym=(document.getElementById('ta-sym')?.value||'').toUpperCase().trim();
  if(!sym||sym.length<1) return;
  const spotEl=document.getElementById('ta-spot');
  const msg=document.getElementById('ta-msg');
  if(msg) msg.textContent='📡 Fetching spot…';
  try{
    const d=await api(`/analysis/spot/${sym}`);
    if(d.spot&&spotEl){spotEl.value=d.spot; _taBaseSpot=d.spot;}
    if(msg) msg.textContent=`✅ ${sym} @ $${d.spot}`;
  }catch(e){if(msg) msg.textContent=`⚠ ${e.message}`;}
}

// Auto-fetch premium + IV + delta when strike/expiry changes
async function _taLegStrikeBlur(i, val){
  if(!_taLegs[i]) return;
  _taLegs[i].strike = val;
  _taRenderLegs();
  await _taFetchLegData(i);
}

async function _taLegExpiryChange(i, val){
  if(!_taLegs[i]) return;
  _taLegs[i].expiry = val;
  _taRenderLegs();
  await _taFetchLegData(i);
}

async function _taFetchLegData(i){
  const leg=_taLegs[i];
  if(!leg||!leg.strike||!leg.expiry||leg.option_type==='stock') return;
  const sym=(document.getElementById('ta-sym')?.value||'SPY').toUpperCase();
  const msg=document.getElementById('ta-msg');
  try{
    const d=await api(`/analysis/iv/${sym}/${leg.expiry}/${leg.strike}/${leg.option_type}`);
    if(d.iv&&d.iv>0){
      leg.leg_iv = (d.iv*100).toFixed(1);
      if((leg.entry_price===''||leg.entry_price===undefined||leg.entry_price===null) && d.price) leg.entry_price = d.price.toFixed(2);
      // Compute delta
      const spot=parseFloat(document.getElementById('ta-spot')?.value||0)||_taBaseSpot;
      const dte=parseInt(document.getElementById('ta-dte')?.value||28);
      if(spot>0){
        const g=_bsGreeks(spot, parseFloat(leg.strike), Math.max(0.001,dte/365), d.iv, leg.option_type==='call');
        leg._delta = g.delta;
      }
      _taRenderLegs();
      if(msg) msg.textContent=`✅ ${leg.option_type.toUpperCase()} $${leg.strike}: IV=${leg.leg_iv}% price=$${d.price?.toFixed(2)||'?'}`;
    }
  }catch{}
}

function _taTemplate(type){
  const S=parseFloat(document.getElementById('ta-spot')?.value||0)||500;
  const exp=new Date(Date.now()+28*864e5).toISOString().slice(0,10);
  const exp2=new Date(Date.now()+56*864e5).toISOString().slice(0,10);
  const T={
    'Bull Put Spread':  [{side:'sell',option_type:'put', strike:Math.round(S*0.97),expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'buy', option_type:'put', strike:Math.round(S*0.95),expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Bear Call Spread': [{side:'sell',option_type:'call',strike:Math.round(S*1.03),expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'buy', option_type:'call',strike:Math.round(S*1.05),expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Iron Condor':      [{side:'sell',option_type:'put', strike:Math.round(S*0.97),expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'buy', option_type:'put', strike:Math.round(S*0.95),expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'sell',option_type:'call',strike:Math.round(S*1.03),expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'buy', option_type:'call',strike:Math.round(S*1.05),expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Long Call':        [{side:'buy', option_type:'call',strike:Math.round(S),      expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Long Put':         [{side:'buy', option_type:'put', strike:Math.round(S),      expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Straddle':         [{side:'buy', option_type:'call',strike:Math.round(S),      expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'buy', option_type:'put', strike:Math.round(S),      expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Strangle':         [{side:'sell',option_type:'call',strike:Math.round(S*1.05), expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'sell',option_type:'put', strike:Math.round(S*0.95), expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Call Debit Spread':[{side:'buy', option_type:'call',strike:Math.round(S),      expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'sell',option_type:'call',strike:Math.round(S*1.05), expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Put Debit Spread': [{side:'buy', option_type:'put', strike:Math.round(S),      expiry:exp,entry_price:'',qty:1,leg_iv:''},
                         {side:'sell',option_type:'put', strike:Math.round(S*0.95), expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Covered Call':     [{side:'buy', option_type:'stock',strike:'',               expiry:'',  entry_price:S,qty:100,leg_iv:''},
                         {side:'sell',option_type:'call', strike:Math.round(S*1.03),expiry:exp,entry_price:'',qty:1,leg_iv:''}],
    'Calendar Spread':  [{side:'buy', option_type:'call',strike:Math.round(S),      expiry:exp2,entry_price:'',qty:1,leg_iv:''},
                         {side:'sell',option_type:'call',strike:Math.round(S),      expiry:exp, entry_price:'',qty:1,leg_iv:''}],
  };
  if(T[type]){_taLegs=T[type].map(l=>({...l}));_taRenderLegs();}
}

// ─────────────────────────────────────────────────────────────────────────
// Analyze / Fetch IV
// ─────────────────────────────────────────────────────────────────────────
async function _taAnalyze(){
  const msg=document.getElementById('ta-msg');
  const sym=(document.getElementById('ta-sym')?.value||'SPY').toUpperCase();
  let spot=parseFloat(document.getElementById('ta-spot')?.value||0);
  let iv=parseFloat(document.getElementById('ta-iv')?.value||20)/100;
  const dte=parseInt(document.getElementById('ta-dte')?.value||28);
  const legs=_taLegs.filter(l=>l.strike||l.option_type==='stock');
  if(!legs.length){if(msg)msg.textContent='⚠ Add at least one leg';return;}
  // Auto-fetch spot
  if(!spot||spot<1){
    if(msg)msg.textContent='📡 Fetching spot…';
    try{const d=await api(`/analysis/spot/${sym}`);spot=d.spot||500;
      document.getElementById('ta-spot').value=spot;}
    catch{spot=500;}
  }
  // Fetch IV Rank (non-blocking, shows above graph)
  _taFetchIVRank(sym).catch(()=>{});
  // For journal trades: auto-fetch CURRENT IV for each leg
  const hasOriginal = legs.some(l=>l._isOriginal);
  if(hasOriginal){
    if(msg)msg.textContent='📡 Fetching live IV for existing position…';
    for(const leg of legs){
      if(leg.option_type==='stock'||!leg.strike||!leg.expiry) continue;
      try{
        const d=await api(`/analysis/iv/${sym}/${leg.expiry}/${leg.strike}/${leg.option_type}`);
        if(d.iv>0) leg.leg_iv=(d.iv*100).toFixed(1);
      }catch{}
    }
    // Update global IV to avg of fetched
    const fetchedIvs=legs.filter(l=>l.leg_iv).map(l=>parseFloat(l.leg_iv)/100);
    if(fetchedIvs.length){
      iv=fetchedIvs.reduce((a,b)=>a+b,0)/fetchedIvs.length;
      document.getElementById('ta-iv').value=(iv*100).toFixed(1);
    }
  }
  // For each leg: if entry price exists, compute implied IV from it
  // This ensures P&L=0 at entry conditions (spot/DTE haven't changed)
  const T_entry = Math.max(0.001, dte/365);
  legs.forEach(leg=>{
    if(leg.option_type==='stock') return;
    const K=parseFloat(leg.strike); if(!K) return;
    const isCall=leg.option_type==='call';
    const ep=parseFloat(leg.entry_price||leg.price||0);
    if(ep > 0){
      // Compute implied IV from the entry price — this calibrates the model
      const impliedIv = _bsImpliedIV(spot, K, T_entry, ep, isCall);
      leg._impliedIv = impliedIv;
      // Also compute delta at entry
      const g = _bsGreeks(spot, K, T_entry, impliedIv, isCall);
      leg._delta = g.delta;
    } else {
      // No entry price — estimate using global IV
      const legIv=parseFloat(leg.leg_iv||0)/100||iv;
      leg.entry_price=_bsPrice(spot,K,T_entry,legIv,isCall).toFixed(2);
      leg._impliedIv = legIv;
      const g = _bsGreeks(spot, K, T_entry, legIv, isCall);
      leg._delta = g.delta;
    }
  });
  _taLegs=legs;
  _taRenderLegs();
  _taBaseSpot=spot; _taBaseIv=iv; _taBaseDte=dte;
  _taSpotOffset=0; _taIvOffset=0; _taDaysOffset=0;
  // Reset sliders
  ['ta-s','ta-v','ta-d'].forEach(id=>{const el=document.getElementById(id);if(el)el.value=0;});
  // Update days slider max
  const dsl=document.getElementById('ta-d');
  if(dsl)dsl.max=dte;
  const emptyEl = document.getElementById('ta-empty');
  if(emptyEl) emptyEl.style.display='none';
  _taDraw();
  _taDrawGreekChart('delta');
  _taShowStockInfo(sym, spot, dte, iv);
  _taShowStrikesBar(legs, spot);
  if(msg)msg.textContent=`✅ ${legs.length} legs · $${spot} · IV ${(iv*100).toFixed(0)}%`;
}

async function _taFetchIV(){
  const msg=document.getElementById('ta-msg');
  const sym=(document.getElementById('ta-sym')?.value||'SPY').toUpperCase();
  if(msg) msg.textContent='📡 Fetching IV from options chain…';
  try{
    const legs=_taLegs.filter(l=>l.option_type!=='stock'&&l.strike&&l.expiry);
    if(!legs.length){if(msg)msg.textContent='⚠ Set strikes and expiry first';return;}
    let totalIv=0,cnt=0;
    for(const leg of legs){
      try{
        const d=await api(`/analysis/iv/${sym}/${leg.expiry}/${leg.strike}/${leg.option_type}`);
        if(d.iv&&d.iv>0){
          totalIv+=d.iv; cnt++;
          if(!leg.entry_price&&d.price) leg.entry_price=d.price.toFixed(2);
          leg.leg_iv=(d.iv*100).toFixed(1);
        }
      }catch{}
    }
    if(cnt>0){
      const avgIv=totalIv/cnt;
      document.getElementById('ta-iv').value=(avgIv*100).toFixed(1);
      _taBaseIv=avgIv;
      _taRenderLegs();
      if(msg)msg.textContent=`✅ IV fetched: ${(avgIv*100).toFixed(1)}% avg`;
    } else {
      if(msg)msg.textContent='⚠ Could not fetch IV — using manual value';
    }
  }catch(e){if(msg)msg.textContent=`❌ ${e.message}`;}
}

// ─────────────────────────────────────────────────────────────────────────
// Sliders
// ─────────────────────────────────────────────────────────────────────────
function _taSlide(){
  _taSpotOffset=parseFloat(document.getElementById('ta-s')?.value||0);
  _taIvOffset  =parseFloat(document.getElementById('ta-v')?.value||0);
  _taDaysOffset=parseInt  (document.getElementById('ta-d')?.value||0);
  _taUpdatePins();
  _taDraw();
}

function _taUpdatePins(){
  const W=el=>document.getElementById(el)?.offsetWidth||300;
  const pin=(id,val,min,max,fmt,c)=>{
    const pct=(val-min)/(max-min)*100;
    const p=document.getElementById(id+'-pin');
    const f=document.getElementById(id+'-fill');
    if(p){p.style.left=pct+'%';p.textContent=fmt(val);p.style.background=c;}
    if(f){
      const mid=(0-min)/(max-min)*100;
      const l=Math.min(pct,mid),w=Math.abs(pct-mid);
      f.style.left=l+'%';f.style.width=w+'%';
      f.style.background=c;
    }
  };
  const sv=_taSpotOffset, vv=_taIvOffset, dv=_taDaysOffset;
  pin('ta-s',sv,-20,20,v=>(v>0?'+':'')+v+'%', sv>0?'#22c55e':sv<0?'#ef4444':'#3b82f6');
  pin('ta-v',vv,-50,50,v=>(v>0?'+':'')+v+'%', vv>0?'#ef4444':'#22c55e');
  const dMax=parseInt(document.getElementById('ta-d')?.max||60);
  pin('ta-d',dv,0,dMax,v=>v+'d','#64748b');

  // Scenario P&L labels
  const S2=_taBaseSpot*(1+sv/100);
  const iv2=Math.max(0.01,_taBaseIv+vv/100);
  const dte2=Math.max(0,_taBaseDte-dv);
  const legs=_taLegs.filter(l=>l.strike||l.option_type==='stock');
  const pnlS=_taStratPnL(legs,S2,_taBaseDte,_taBaseIv);
  const pnlV=_taStratPnL(legs,_taBaseSpot,_taBaseDte,iv2);
  const pnlD=_taStratPnL(legs,_taBaseSpot,dte2,_taBaseIv);
  const fmt=v=>(v>=0?'+':'')+'$'+v.toFixed(0);
  const fC=v=>v>=0?'#22c55e':'#ef4444';
  const set=(id,v)=>{const el=document.getElementById(id);if(el){el.textContent=fmt(v);el.style.color=fC(v);}};
  set('ta-s-pnl',pnlS);set('ta-v-pnl',pnlV);set('ta-d-pnl',pnlD);
}

// ─────────────────────────────────────────────────────────────────────────
// Draw payoff canvas
// ─────────────────────────────────────────────────────────────────────────
function _taDraw(){
  const cv=document.getElementById('ta-canvas');
  if(!cv||!_taBaseSpot) return;
  const ctx=cv.getContext('2d');
  const W=cv.offsetWidth||800; const H=cv.height||320;
  cv.width=W;

  const spot =_taBaseSpot*(1+_taSpotOffset/100);
  const iv   =Math.max(0.01,_taBaseIv+_taIvOffset/100);
  const dte  =Math.max(0,_taBaseDte-_taDaysOffset);
  const legs =_taLegs.filter(l=>l.strike||l.option_type==='stock');
  if(!legs.length) return;

  const PAD={t:28,r:16,b:28,l:58};
  const cW=W-PAD.l-PAD.r, cH=H-PAD.t-PAD.b;

  // Spot range: ±25% around BASE spot, 160 points
  const lo=_taBaseSpot*0.75, hi=_taBaseSpot*1.25;
  const spotArr=Array.from({length:160},(_,i)=>lo+i*(hi-lo)/159);

  // Scenario line and expiry line
  const scenLine=spotArr.map(S=>({S,pnl:_taStratPnL(legs,S,dte,iv)}));
  const expiryLine=spotArr.map(S=>({S,pnl:_taStratPnL(legs,S,0,iv)}));

  const allPnls=[...scenLine,...expiryLine].map(p=>p.pnl);
  const maxP=Math.max(...allPnls,0); const minP=Math.min(...allPnls,0);
  const range=Math.max(Math.abs(maxP),Math.abs(minP))||100;
  const padded=range*1.12;

  const toX=S=>PAD.l+(S-lo)/(hi-lo)*cW;
  const toY=v=>PAD.t+cH/2-v/padded*(cH/2);
  const y0=toY(0);

  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#0d1117';ctx.fillRect(0,0,W,H);

  // ── Sigma bands ──────────────────────────────────────────────────────
  const annIv=iv*Math.sqrt(Math.max(1,dte)/365);
  const sigma=[-2,-1,0,1,2].map(n=>_taBaseSpot*Math.exp(n*annIv));
  const bandC=['rgba(239,68,68,.07)','rgba(251,191,36,.05)','rgba(34,197,94,.05)','rgba(251,191,36,.05)','rgba(239,68,68,.07)'];
  for(let i=0;i<4;i++){
    const x1=Math.max(PAD.l,toX(sigma[i]));
    const x2=Math.min(W-PAD.r,toX(sigma[i+1]));
    if(x2>x1){ctx.fillStyle=bandC[i];ctx.fillRect(x1,PAD.t,x2-x1,cH);}
  }

  // ── Grid ─────────────────────────────────────────────────────────────
  ctx.strokeStyle='rgba(255,255,255,.04)';ctx.lineWidth=1;
  for(let i=1;i<8;i++){
    const y=PAD.t+i*cH/8;
    ctx.beginPath();ctx.moveTo(PAD.l,y);ctx.lineTo(W-PAD.r,y);ctx.stroke();
  }

  // ── Sigma lines ──────────────────────────────────────────────────────
  ctx.strokeStyle='rgba(255,255,255,.10)';ctx.lineWidth=1;ctx.setLineDash([2,4]);
  [-2,-1,1,2].forEach(n=>{
    const sx=toX(_taBaseSpot*Math.exp(n*annIv));
    if(sx>PAD.l&&sx<W-PAD.r){
      ctx.beginPath();ctx.moveTo(sx,PAD.t);ctx.lineTo(sx,PAD.t+cH);ctx.stroke();
      ctx.fillStyle='rgba(255,255,255,.3)';ctx.font='9px sans-serif';ctx.textAlign='center';
      ctx.fillText((n>0?'+':'')+n+'σ',sx,PAD.t-5);
    }
  });
  ctx.setLineDash([]);

  // ── Zero line ────────────────────────────────────────────────────────
  ctx.strokeStyle='rgba(255,255,255,.2)';ctx.lineWidth=1.5;ctx.setLineDash([5,4]);
  ctx.beginPath();ctx.moveTo(PAD.l,y0);ctx.lineTo(W-PAD.r,y0);ctx.stroke();
  ctx.setLineDash([]);

  // ── Expiry line (orange dashed, like Opstra) ─────────────────────────
  if(dte>0){
    ctx.strokeStyle='rgba(249,115,22,.7)';ctx.lineWidth=1.8;ctx.setLineDash([4,3]);
    ctx.beginPath();
    expiryLine.forEach((p,i)=>i===0?ctx.moveTo(toX(p.S),toY(p.pnl)):ctx.lineTo(toX(p.S),toY(p.pnl)));
    ctx.stroke();ctx.setLineDash([]);
  }

  // ── Fill zones for scenario line ─────────────────────────────────────
  // Green above zero
  ctx.save();ctx.beginPath();ctx.rect(PAD.l,PAD.t,cW,Math.max(0,y0-PAD.t));ctx.clip();
  const gG=ctx.createLinearGradient(0,PAD.t,0,y0);
  gG.addColorStop(0,'rgba(34,197,94,.25)');gG.addColorStop(1,'rgba(34,197,94,.04)');
  ctx.fillStyle=gG;ctx.beginPath();
  scenLine.forEach((p,i)=>i===0?ctx.moveTo(toX(p.S),toY(p.pnl)):ctx.lineTo(toX(p.S),toY(p.pnl)));
  ctx.lineTo(toX(scenLine[scenLine.length-1].S),y0);ctx.lineTo(toX(scenLine[0].S),y0);
  ctx.closePath();ctx.fill();ctx.restore();
  // Red below zero
  ctx.save();ctx.beginPath();ctx.rect(PAD.l,y0,cW,cH-(y0-PAD.t));ctx.clip();
  const gR=ctx.createLinearGradient(0,y0,0,PAD.t+cH);
  gR.addColorStop(0,'rgba(239,68,68,.04)');gR.addColorStop(1,'rgba(239,68,68,.22)');
  ctx.fillStyle=gR;ctx.beginPath();
  scenLine.forEach((p,i)=>i===0?ctx.moveTo(toX(p.S),toY(p.pnl)):ctx.lineTo(toX(p.S),toY(p.pnl)));
  ctx.lineTo(toX(scenLine[scenLine.length-1].S),y0);ctx.lineTo(toX(scenLine[0].S),y0);
  ctx.closePath();ctx.fill();ctx.restore();

  // ── Scenario P&L line (blue solid) ──────────────────────────────────
  ctx.strokeStyle='#3b82f6';ctx.lineWidth=2.5;
  ctx.beginPath();
  scenLine.forEach((p,i)=>i===0?ctx.moveTo(toX(p.S),toY(p.pnl)):ctx.lineTo(toX(p.S),toY(p.pnl)));
  ctx.stroke();

  // ── Breakeven markers ────────────────────────────────────────────────
  let prevPnl=expiryLine[0].pnl, beArr=[];
  expiryLine.forEach((p,i)=>{
    if(i>0&&((prevPnl<0&&p.pnl>=0)||(prevPnl>=0&&p.pnl<0)))
      beArr.push((p.S+expiryLine[i-1].S)/2);
    prevPnl=p.pnl;
  });
  beArr.forEach(be=>{
    const bx=toX(be);
    ctx.fillStyle='#fbbf24';ctx.beginPath();ctx.arc(bx,y0,5,0,Math.PI*2);ctx.fill();
    ctx.fillStyle='rgba(0,0,0,.7)';ctx.fillRect(bx-24,y0-20,48,14);
    ctx.fillStyle='#fbbf24';ctx.font='bold 9px monospace';ctx.textAlign='center';
    ctx.fillText('$'+be.toFixed(0),bx,y0-9);
  });

  // ── Current spot line ────────────────────────────────────────────────
  const sx=toX(spot);
  ctx.strokeStyle='rgba(251,191,36,.6)';ctx.lineWidth=1.5;ctx.setLineDash([3,3]);
  ctx.beginPath();ctx.moveTo(sx,PAD.t);ctx.lineTo(sx,PAD.t+cH);ctx.stroke();
  ctx.setLineDash([]);

  // P&L dot at scenario spot
  const nearestPnl=_taStratPnL(legs,spot,dte,iv);
  const py=toY(nearestPnl);
  ctx.fillStyle=nearestPnl>=0?'#22c55e':'#ef4444';
  ctx.beginPath();ctx.arc(sx,py,6,0,Math.PI*2);ctx.fill();
  // Label
  ctx.fillStyle='rgba(0,0,0,.75)';ctx.fillRect(sx-28,py-20,56,15);
  ctx.fillStyle=nearestPnl>=0?'#22c55e':'#ef4444';
  ctx.font='bold 10px monospace';ctx.textAlign='center';
  ctx.fillText((nearestPnl>=0?'+':'')+'$'+nearestPnl.toFixed(0),sx,py-9);

  // ── Y-axis labels ────────────────────────────────────────────────────
  ctx.textAlign='right';ctx.font='10px monospace';
  const step=Math.pow(10,Math.floor(Math.log10(padded)));
  for(let v=-padded;v<=padded+step*0.1;v+=step){
    const vy=toY(v);if(vy<PAD.t-2||vy>PAD.t+cH+2)continue;
    ctx.fillStyle=v>0?'rgba(34,197,94,.7)':v<0?'rgba(239,68,68,.7)':'rgba(255,255,255,.5)';
    ctx.fillText((v>=0?'+':'')+v.toFixed(0),PAD.l-4,vy+3);
  }

  // ── X-axis spot labels ───────────────────────────────────────────────
  ctx.textAlign='center';ctx.fillStyle='rgba(255,255,255,.35)';ctx.font='9px monospace';
  [0,40,79,119,159].forEach(i=>{if(spotArr[i])ctx.fillText(spotArr[i].toFixed(0),toX(spotArr[i]),PAD.t+cH+12);});

  // ── Key levels ───────────────────────────────────────────────────────
  const levEl=document.getElementById('ta-levels');
  if(levEl){
    const exPnls=expiryLine.map(p=>p.pnl);
    const mp=Math.max(...exPnls),ml=Math.min(...exPnls);
    // Compute PoP for levels bar
    const _avgIv2 = legs.reduce((s,l)=>s+(l._impliedIv||parseFloat(l.leg_iv||0)/100||iv),0)/legs.length;
    const _sig2 = _avgIv2*Math.sqrt(Math.max(1,_taBaseDte)/365);
    const _mu2 = Math.log(_taBaseSpot)+(-0.5*_avgIv2*_avgIv2)*(_taBaseDte/365);
    let _pop2 = 50;
    if(beArr.length===1){
      const z=(Math.log(beArr[0])-_mu2)/_sig2;
      const midPnl2=expiryLine[Math.floor(expiryLine.length/2)]?.pnl||0;
      _pop2=(midPnl2>0?_bsNormCDF(z):1-_bsNormCDF(z))*100;
    } else if(beArr.length===2){
      const midPnl2=_taStratPnL(legs,(beArr[0]+beArr[1])/2,0,iv);
      const pB0=_bsNormCDF((Math.log(beArr[0])-_mu2)/_sig2);
      const pB1=_bsNormCDF((Math.log(beArr[1])-_mu2)/_sig2);
      _pop2=midPnl2>0?(pB1-pB0)*100:(pB0+(1-pB1))*100;
    } else if(beArr.length===0){ _pop2=expiryLine[80]?.pnl>=0?95:5; }
    _pop2=Math.min(99,Math.max(1,Math.round(_pop2*10)/10));
    const _popC2=_pop2>=65?'#22c55e':_pop2>=45?'#f59e0b':'#ef4444';

    levEl.innerHTML=[
      {l:'Max Profit',v:'$'+mp.toFixed(0),c:'#22c55e'},
      {l:'Max Loss',  v:'$'+Math.abs(ml).toFixed(0),c:'#ef4444'},
      {l:'Breakevens',v:beArr.length?beArr.map(b=>'$'+b.toFixed(0)).join(' / '):'—',c:'#fbbf24'},
      {l:'P&L now',   v:(nearestPnl>=0?'+':'')+'$'+nearestPnl.toFixed(0),c:nearestPnl>=0?'#22c55e':'#ef4444'},
      {l:'PoP',       v:_pop2.toFixed(1)+'%',c:_popC2},
      {l:'DTE',       v:dte+'d',c:'#3b82f6'},
    ].map(k=>`<span><span style="color:var(--muted)">${k.l}: </span><b style="color:${k.c}">${k.v}</b></span>`)
     .join('<span style="color:var(--border);margin:0 6px">|</span>');
  }

  // ── Greeks bar ───────────────────────────────────────────────────────
  const g=_taStratGreeks(legs,spot,dte,iv);
  const gbEl=document.getElementById('ta-greeks-bar');
  if(gbEl) gbEl.innerHTML=[
    {n:'Δ Delta',v:g.delta,c:g.delta>=0?'#22c55e':'#ef4444'},
    {n:'Γ Gamma', v:g.gamma,c:'#3b82f6'},
    {n:'Θ Theta', v:g.theta,c:g.theta<0?'#ef4444':'#22c55e'},
    {n:'V Vega',  v:g.vega, c:'#a855f7'},
  ].map(x=>`<span style="background:var(--surface);border:1px solid var(--border);
    padding:3px 9px;border-radius:4px;font-size:11px">
    <span style="color:var(--muted)">${x.n}: </span>
    <b style="color:${x.c}">${x.v>=0?'+':''}${x.v.toFixed(x.v>10||x.v<-10?1:3)}</b>
  </span>`).join('');

  // ── P&L at spot display ──────────────────────────────────────────────
  const pnlSEl=document.getElementById('ta-pnl-spot');
  if(pnlSEl){
    pnlSEl.textContent=(nearestPnl>=0?'+':'')+'$'+nearestPnl.toFixed(2);
    pnlSEl.style.color=nearestPnl>=0?'#22c55e':'#ef4444';
  }

  // Update slider scenario labels
  _taUpdatePins();
}

// ─────────────────────────────────────────────────────────────────────────
// Hover tooltip
// ─────────────────────────────────────────────────────────────────────────
function _taHover(e){
  if(!_taBaseSpot) return;
  const cv=e.currentTarget;
  const rect=cv.getBoundingClientRect();
  const mx=e.clientX-rect.left;
  const W=cv.width||800;
  const PAD_L=58,cW=W-PAD_L-16;
  const lo=_taBaseSpot*0.75, hi=_taBaseSpot*1.25;
  const S=lo+(mx-PAD_L)/cW*(hi-lo);
  if(S<lo||S>hi) return;
  const legs=_taLegs.filter(l=>l.strike||l.option_type==='stock');
  const dte=Math.max(0,_taBaseDte-_taDaysOffset);
  const iv=Math.max(0.01,_taBaseIv+_taIvOffset/100);
  const pnl=_taStratPnL(legs,S,dte,iv);
  const tip=document.getElementById('ta-tip');
  if(!tip) return;
  const c=pnl>=0?'#22c55e':'#ef4444';
  tip.innerHTML=`<div style="color:var(--muted);font-size:9px">Spot: $${S.toFixed(1)}</div>
    <div style="font-weight:800;font-size:13px;color:${c}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</div>`;
  tip.style.display='';
  tip.style.left=Math.min(mx+10,cv.offsetWidth-90)+'px';
  tip.style.top='30px';
}

// ─────────────────────────────────────────────────────────────────────────
// Greeks chart
// ─────────────────────────────────────────────────────────────────────────
function _taShowGreek(g){
  _taActiveGreek=g;
  ['delta','gamma','theta','vega'].forEach(k=>{
    const b=document.getElementById('ta-gb-'+k);
    if(b){b.style.background=k===g?'#6366f1':'';b.style.color=k===g?'#fff':'';}
  });
  _taDrawGreekChart(g);
}

function _taDrawGreekChart(metric){
  const cv=document.getElementById('ta-gcanvas');
  if(!cv||!_taBaseSpot) return;
  const ctx=cv.getContext('2d');
  const W=cv.offsetWidth||800; const H=cv.height||120;
  cv.width=W; ctx.clearRect(0,0,W,H);
  const legs=_taLegs.filter(l=>l.strike||l.option_type==='stock');
  if(!legs.length) return;
  const spot=_taBaseSpot*(1+_taSpotOffset/100);
  const iv=Math.max(0.01,_taBaseIv+_taIvOffset/100);
  // Compute greek at spot for each DTE from max down to 1
  const dtes=Array.from({length:_taBaseDte},(_,i)=>_taBaseDte-i);
  const vals=dtes.map(d=>_taStratGreeks(legs,spot,d,iv)[metric]||0);
  const maxV=Math.max(...vals,0),minV=Math.min(...vals,0);
  const rng=Math.max(Math.abs(maxV),Math.abs(minV))||1;
  const PAD={t:14,r:12,b:18,l:48};
  const cW=W-PAD.l-PAD.r,cH=H-PAD.t-PAD.b;
  const toX=(_,i)=>PAD.l+i/Math.max(1,dtes.length-1)*cW;
  const toY=v=>PAD.t+cH/2-v/rng*(cH/2);
  // Zero
  ctx.strokeStyle='rgba(255,255,255,.15)';ctx.lineWidth=1;ctx.setLineDash([3,3]);
  const yz=toY(0);ctx.beginPath();ctx.moveTo(PAD.l,yz);ctx.lineTo(W-PAD.r,yz);ctx.stroke();ctx.setLineDash([]);
  const colors={delta:'#3b82f6',gamma:'#22c55e',theta:'#ef4444',vega:'#a855f7'};
  const c=colors[metric]||'#64748b';
  // Fill
  ctx.fillStyle=c+'28';ctx.beginPath();ctx.moveTo(toX(null,0),toY(vals[0]));
  vals.forEach((v,i)=>ctx.lineTo(toX(null,i),toY(v)));
  ctx.lineTo(toX(null,vals.length-1),yz);ctx.lineTo(toX(null,0),yz);ctx.closePath();ctx.fill();
  // Line
  ctx.strokeStyle=c;ctx.lineWidth=2;ctx.beginPath();
  vals.forEach((v,i)=>i===0?ctx.moveTo(toX(null,i),toY(v)):ctx.lineTo(toX(null,i),toY(v)));
  ctx.stroke();
  // Labels
  ctx.fillStyle='rgba(255,255,255,.4)';ctx.font='9px monospace';ctx.textAlign='left';
  ctx.fillText(metric.toUpperCase()+' @ $'+spot.toFixed(0),PAD.l+2,12);
  ctx.textAlign='right';
  [maxV,0,minV].forEach(v=>{
    const vy=toY(v);if(vy>PAD.t&&vy<PAD.t+cH){
      ctx.fillStyle=v>0?'rgba(34,197,94,.7)':v<0?'rgba(239,68,68,.7)':'rgba(255,255,255,.4)';
      ctx.fillText(v.toFixed(2),PAD.l-3,vy+3);
    }
  });
  ctx.textAlign='center';ctx.fillStyle='rgba(255,255,255,.35)';
  [0,Math.floor(dtes.length/2),dtes.length-1].forEach(i=>{
    if(dtes[i]) ctx.fillText(dtes[i]+'d',toX(null,i),H-2);
  });
}

// ─────────────────────────────────────────────────────────────────────────
// Load from journal trade
// ─────────────────────────────────────────────────────────────────────────
function _taLoadFromTrade(trade){
  if(!trade) return;
  const symEl=document.getElementById('ta-sym');
  if(symEl) symEl.value=trade.symbol||'SPY';
  const spotEl=document.getElementById('ta-spot');
  if(spotEl&&trade.spot_price) spotEl.value=trade.spot_price;
  const dteEl=document.getElementById('ta-dte');
  if(dteEl&&trade.expiry){
    const days=Math.max(1,Math.round((new Date(trade.expiry)-new Date())/86400000));
    dteEl.value=days;
    const dsl=document.getElementById('ta-d');if(dsl) dsl.max=days;
  }
  // Build legs
  try{
    const legs=JSON.parse(trade.legs_json||'[]');
    if(legs.length){
      _taLegs=legs.map(l=>{
        // Preserve original entry price — don't let 0 become empty
        const origPrice = l.price!=null&&l.price!=='' ? String(l.price)
                        : l.entry_price!=null&&l.entry_price!=='' ? String(l.entry_price) : '';
        return {side:l.side||'buy', option_type:l.option_type||'call',
          strike:l.strike||'', expiry:l.expiry||trade.expiry||'',
          entry_price:origPrice, qty:l.qty||1, leg_iv:'', _isOriginal:true};
      });
    } else { _taLegs=_taLegsLegacy(trade); }
  }catch{ _taLegs=_taLegsLegacy(trade); }
  _taRenderLegs();
  setTimeout(_taAnalyze,100);
}

function _taLegsLegacy(t){
  const exp=t.expiry||'';const tt=t.trade_type;
  if(tt==='PS')  return [{side:'sell',option_type:'put', strike:t.short_strike,expiry:exp,entry_price:t.entry_price,qty:t.quantity||1,leg_iv:''},
                          {side:'buy', option_type:'put', strike:t.long_strike, expiry:exp,entry_price:'',qty:t.quantity||1,leg_iv:''}];
  if(tt==='CS')  return [{side:'sell',option_type:'call',strike:t.short_strike,expiry:exp,entry_price:t.entry_price,qty:t.quantity||1,leg_iv:''},
                          {side:'buy', option_type:'call',strike:t.long_strike, expiry:exp,entry_price:'',qty:t.quantity||1,leg_iv:''}];
  if(tt==='IC')  return [{side:'sell',option_type:'put', strike:t.put_sell,  expiry:exp,entry_price:t.put_credit||'',qty:1,leg_iv:''},
                          {side:'buy', option_type:'put', strike:t.put_buy,   expiry:exp,entry_price:'',qty:1,leg_iv:''},
                          {side:'sell',option_type:'call',strike:t.call_sell, expiry:exp,entry_price:t.call_credit||'',qty:1,leg_iv:''},
                          {side:'buy', option_type:'call',strike:t.call_buy,  expiry:exp,entry_price:'',qty:1,leg_iv:''}];
  if(tt==='Stock') return [{side:'buy',option_type:'stock',strike:'',expiry:'',entry_price:t.entry_price,qty:t.quantity||100,leg_iv:''}];
  return [];
}

async function _jrnOpenAnalysis(tid){
  let trade=window._jrnTrades?.find(t=>t.id===tid);
  if(!trade){
    try{const all=await api('/journal/trades?status=ALL');window._jrnTrades=all;trade=all.find(t=>t.id===tid);}catch{}
  }
  // Switch tab
  _setActiveTab('tradeanalysis');
  // Reset dataset.built so it rebuilds with trade data
  const root=document.getElementById('trade-analysis-root');
  if(root) root.dataset.built='';
  setTimeout(()=>_loadTradeAnalysis(trade||null),80);
}



let _taScoreDebounceTimer = null;
async function _taFetchStrategyScore(boxId, popPct, rrRatio, theta, dte, isCredit, delta, vega) {
  // Debounced -- _taShowStockInfo fires repeatedly while dragging a
  // slider, and only the LAST call after dragging settles should
  // actually hit the server. Since each call re-renders the whole
  // panel from scratch, any earlier (now-orphaned) score box is already
  // gone from the DOM by the time a stale fetch could resolve, so
  // there's no risk of a leftover placeholder stuck on "…" forever.
  if (_taScoreDebounceTimer) clearTimeout(_taScoreDebounceTimer);
  _taScoreDebounceTimer = setTimeout(async () => {
    try {
      const d = await api('/strategy/strategy_score', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ pop_pct: popPct, rr_ratio: rrRatio, theta, dte, is_credit: isCredit, delta, vega }),
      });
      const box = document.getElementById(boxId);
      if (!box || d.error) return;
      const score = d.score;
      const notes = d.score_notes || [];
      const scoreC = score >= 75 ? '#22c55e' : score >= 55 ? '#4ade80' : score >= 40 ? '#f59e0b' : '#ef4444';
      box.style.borderColor = scoreC;
      box.title = notes.join(' · ');
      box.innerHTML = `
        <div style="font-size:9px;color:var(--muted);margin-bottom:2px">Score</div>
        <div style="font-size:20px;font-weight:900;color:${scoreC}">${score}</div>
        <div style="font-size:9px;color:${scoreC}">/100</div>
      `;
    } catch (e) {
      console.warn('strategy score fetch failed', e);
    }
  }, 250);
}

function _taShowStockInfo(sym, spot, dte, iv) {
  const el = document.getElementById('ta-stock-info');
  if (!el) return;
  el.style.display = '';
  const legs = _taLegs.filter(l => l.strike || l.option_type === 'stock');
  if (!legs.length) return;

  // ── Expiry P&L curve for max profit / max loss / breakevens ────────
  const expiryLine = Array.from({length:200}, (_, i) => {
    const S = spot * 0.70 + i * (spot * 0.60) / 199;
    return {S, pnl: _taStratPnL(legs, S, 0, iv)};
  });
  const pnls = expiryLine.map(p => p.pnl);
  const maxP = Math.max(...pnls), minP = Math.min(...pnls);
  let prevP = expiryLine[0].pnl;
  const bes = [];
  expiryLine.forEach((p, i) => {
    if (i > 0 && ((prevP < 0 && p.pnl >= 0) || (prevP >= 0 && p.pnl < 0)))
      bes.push((p.S + expiryLine[i - 1].S) / 2);
    prevP = p.pnl;
  });

  // ── Probability of Profit (PoP) using log-normal distribution ──────
  // Uses per-leg implied IVs for accurate sigma
  const avgIv = legs.reduce((sum, l) => sum + (l._impliedIv || parseFloat(l.leg_iv||0)/100 || iv), 0) / legs.length;
  const sigma = avgIv * Math.sqrt(Math.max(1, dte) / 365);
  const mu = Math.log(spot) + (-0.5 * avgIv * avgIv) * (dte / 365);  // log-normal drift

  function probBelow(S) {
    // P(S_T < S) under log-normal
    if (S <= 0) return 0;
    const z = (Math.log(S) - mu) / sigma;
    return _bsNormCDF(z);
  }
  function probAbove(S) { return 1 - probBelow(S); }

  // Determine profit zones from expiry P&L
  // Sample at fine granularity to find where P&L > 0
  let popPct = 0;
  if (bes.length === 0) {
    // No breakevens: either all profit or all loss
    popPct = expiryLine[Math.floor(expiryLine.length/2)].pnl >= 0 ? 95 : 5;
  } else if (bes.length === 1) {
    // One breakeven: profit is on one side
    const be = bes[0];
    const profitAbove = expiryLine[expiryLine.length - 1].pnl > 0;
    popPct = profitAbove ? probAbove(be) * 100 : probBelow(be) * 100;
  } else if (bes.length === 2) {
    // Two breakevens (e.g. iron condor, straddle)
    const midPnl = _taStratPnL(legs, (bes[0] + bes[1]) / 2, 0, iv);
    if (midPnl > 0) {
      // Profit is BETWEEN breakevens (iron condor / credit spread)
      popPct = (probBelow(bes[1]) - probBelow(bes[0])) * 100;
    } else {
      // Profit is OUTSIDE breakevens (long straddle/strangle)
      popPct = (probBelow(bes[0]) + probAbove(bes[1])) * 100;
    }
  } else {
    // Multiple breakevens: integrate profit zones
    let pop = 0;
    for (let i = 0; i < expiryLine.length - 1; i++) {
      if (expiryLine[i].pnl >= 0) {
        const w = (expiryLine[i + 1].S - expiryLine[i].S);
        pop += probBelow(expiryLine[i].S + w/2) - probBelow(expiryLine[i].S - w/2);
      }
    }
    popPct = Math.min(99, Math.max(1, pop * 100));
  }
  popPct = Math.min(99, Math.max(1, Math.round(popPct * 10) / 10));

  // ── Score (0-100) ─────────────────────────────────────────────────
  // Factors: PoP, R:R, theta advantage, DTE sweet spot, delta neutrality
  // The actual weighted scoring judgment now lives server-side
  // (/strategy/strategy_score) -- only the specific point weights and
  // thresholds moved; the underlying PoP/R:R/Greeks math above stays
  // here since it's standard, public options math and needs to update
  // instantly while dragging the sliders, not round-trip to the server
  // on every drag tick.
  const g = _taStratGreeks(legs, spot, dte, iv);
  const rrRatio = Math.abs(minP) > 0 ? maxP / Math.abs(minP) : 0;
  const netCredit = legs.reduce((s, l) => {
    const ep = parseFloat(l.entry_price || l.price || 0), q = parseInt(l.qty || 1);
    return s + (l.side === 'sell' ? ep * q : -ep * q);
  }, 0);
  const isCredit = netCredit > 0;

  const scoreBoxId = 'ta-score-box-' + Math.random().toString(36).slice(2, 8);
  _taFetchStrategyScore(scoreBoxId, popPct, rrRatio, g.theta, dte, isCredit, g.delta, g.vega);

  const popC = popPct >= 65 ? '#22c55e' : popPct >= 50 ? '#4ade80' : popPct >= 35 ? '#f59e0b' : '#ef4444';

  // ── Strategy name detection ────────────────────────────────────────
  const puts = legs.filter(l => l.option_type === 'put');
  const calls = legs.filter(l => l.option_type === 'call');
  const stocks = legs.filter(l => l.option_type === 'stock');
  let stratName = legs.length + '-Leg Custom';
  if (puts.length===2 && !calls.length) stratName = puts.some(l=>l.side==='sell')?'Bull Put Spread':'Put Debit Spread';
  if (calls.length===2 && !puts.length) stratName = calls.some(l=>l.side==='sell')?'Bear Call Spread':'Call Debit Spread';
  if (puts.length===2 && calls.length===2) stratName = 'Iron Condor';
  if (puts.length===1 && calls.length===1 && puts[0].strike===calls[0].strike) stratName = 'Straddle';
  if (puts.length===1 && calls.length===1 && puts[0].strike!==calls[0].strike) stratName = (puts[0].side==='sell'&&calls[0].side==='sell')?'Strangle':'Custom';
  if (stocks.length && calls.length===1 && calls[0].side==='sell') stratName = 'Covered Call';
  if (legs.length===1 && legs[0].option_type==='call') stratName = legs[0].side==='buy'?'Long Call':'Short Call';
  if (legs.length===1 && legs[0].option_type==='put') stratName = legs[0].side==='buy'?'Long Put':'Short Put';
  if (stocks.length===1 && !calls.length && !puts.length) stratName = 'Stock Position';

  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">
      <div>
        <div style="margin-bottom:4px">
          <span style="font-size:15px;font-weight:800;color:var(--accent)">${sym}</span>
          <span style="font-size:12px;color:var(--muted);margin-left:8px">Spot: <b style="color:var(--text)">$${spot.toFixed(2)}</b></span>
          <span style="font-size:12px;color:var(--muted);margin-left:8px">IV: <b style="color:#a855f7">${(avgIv*100).toFixed(1)}%</b></span>
          <span style="font-size:12px;color:var(--muted);margin-left:8px">DTE: <b style="color:#3b82f6">${dte}d</b></span>
        </div>
        <div style="display:flex;gap:16px;font-size:11px;flex-wrap:wrap">
          <span>Max Profit: <b style="color:#22c55e">$${maxP.toFixed(0)}</b></span>
          <span>Max Loss: <b style="color:#ef4444">$${Math.abs(minP).toFixed(0)}</b></span>
          <span>Breakevens: <b style="color:#fbbf24">${bes.length?bes.map(b=>'$'+b.toFixed(1)).join(' / '):'—'}</b></span>
          <span>R:R: <b style="color:${rrRatio>=1?'#22c55e':'#f59e0b'}">${rrRatio.toFixed(2)}:1</b></span>
          <span>Net ${isCredit?'Credit':'Debit'}: <b style="color:${isCredit?'#22c55e':'#ef4444'}">$${Math.abs(netCredit*100).toFixed(0)}</b></span>
        </div>
      </div>
      <div style="display:flex;gap:10px;align-items:center">
        <!-- PoP -->
        <div style="text-align:center;background:var(--card);border:2px solid ${popC};border-radius:8px;padding:6px 12px;min-width:70px">
          <div style="font-size:9px;color:var(--muted);margin-bottom:2px">Prob. of Profit</div>
          <div style="font-size:20px;font-weight:900;color:${popC}">${popPct.toFixed(1)}%</div>
        </div>
        <!-- Score -->
        <div id="${scoreBoxId}" style="text-align:center;background:var(--card);border:2px solid #64748b;border-radius:8px;padding:6px 12px;min-width:70px">
          <div style="font-size:9px;color:var(--muted);margin-bottom:2px">Score</div>
          <div style="font-size:20px;font-weight:900;color:#64748b">…</div>
          <div style="font-size:9px;color:#64748b">/100</div>
        </div>
        <!-- Strategy badge -->
        <div style="background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.3);color:#a5b4fc;
          padding:6px 14px;border-radius:6px;font-size:12px;font-weight:700;white-space:nowrap">${stratName}</div>
      </div>
    </div>`;
}

function _taShowStrikesBar(legs, spot) {
  const el = document.getElementById('ta-strikes-bar');
  if (!el) return;
  const optLegs = legs.filter(l => l.option_type !== 'stock' && l.strike);
  if (!optLegs.length) { el.style.display = 'none'; return; }
  el.style.display = '';
  const strikes = optLegs.map(l => parseFloat(l.strike));
  const minK = Math.min(...strikes, spot * 0.95);
  const maxK = Math.max(...strikes, spot * 1.05);
  const range = maxK - minK || 1;
  const toP = v => ((v - minK) / range * 90 + 5);
  el.innerHTML = `
    <div style="position:relative;height:32px;background:var(--surface);border:1px solid var(--border);border-radius:4px;overflow:hidden">
      <!-- Spot marker -->
      <div style="position:absolute;top:0;bottom:0;left:${toP(spot)}%;width:2px;background:rgba(251,191,36,.5)"></div>
      <div style="position:absolute;top:2px;left:${toP(spot)}%;transform:translateX(-50%);
        font-size:8px;color:#fbbf24;font-weight:700;white-space:nowrap">S: $${spot.toFixed(0)}</div>
      <!-- Strike markers -->
      ${optLegs.map(l => {
        const K = parseFloat(l.strike);
        const c = l.option_type === 'call' ? '#ef4444' : '#22c55e';
        const icon = l.side === 'sell' ? '▼' : '▲';
        const typeChar = l.option_type === 'call' ? 'C' : 'P';
        return `<div style="position:absolute;bottom:2px;left:${toP(K)}%;transform:translateX(-50%);
          text-align:center">
          <div style="font-size:10px;color:${c};font-weight:700">${icon}</div>
          <div style="font-size:8px;color:${c};white-space:nowrap">${l.side === 'sell' ? 'S' : 'B'}${typeChar}$${K}</div>
        </div>`;
      }).join('')}
    </div>`;
}


// TRADE ANALYSIS — IV RANK / PERCENTILE

async function _taFetchIVRank(sym) {
  const el = document.getElementById('ta-iv-panel');
  if (!el) return;
  el.style.display = '';
  el.innerHTML = '<div style="padding:6px 0;font-size:11px;color:var(--muted)">⏳ Loading IV Rank…</div>';
  try {
    const d = await api(`/api/iv_rank/${sym}`);
    _taRenderIVPanel(el, d);
  } catch(e) {
    el.innerHTML = `<div style="font-size:11px;color:var(--muted)">IV Rank unavailable: ${e.message}</div>`;
  }
}

function _taRenderIVPanel(el, d) {
  const regC = d.regime==='HIGH' ? '#ef4444' : d.regime==='LOW' ? '#22c55e' : '#f59e0b';
  const recC = d.recommendation==='SELL PREMIUM' ? '#a855f7' : d.recommendation==='BUY OPTIONS' ? '#3b82f6' : '#f59e0b';
  const trendArrow = d.iv_trend==='RISING' ? '↑' : d.iv_trend==='FALLING' ? '↓' : '→';
  const trendC = d.iv_trend==='RISING' ? '#ef4444' : d.iv_trend==='FALLING' ? '#22c55e' : '#94a3b8';

  // IV Rank bar
  const rankPct = d.iv_rank || 0;
  const pctPct  = d.iv_percentile || 0;
  const barC = rankPct >= 70 ? '#ef4444' : rankPct >= 40 ? '#f59e0b' : '#22c55e';

  // Preferred strategies as tags
  const strats = (d.preferred_strategies || []).slice(0,4).map(s =>
    `<span style="font-size:10px;padding:2px 8px;border-radius:3px;background:${recC}18;color:${recC};border:1px solid ${recC}33">${s}</span>`
  ).join('');

  el.innerHTML = `
    <div style="background:var(--card2);border:1px solid var(--border);border-radius:6px;padding:10px 14px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:8px">

        <!-- Left: regime badge + meters -->
        <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:center">
          <div style="text-align:center">
            <div style="font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">IV Regime</div>
            <div style="font-size:18px;font-weight:900;color:${regC}">${d.regime}</div>
            <div style="font-size:10px;font-weight:700;color:${recC}">${d.recommendation}</div>
          </div>

          <!-- IV Rank meter -->
          <div style="min-width:140px">
            <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:3px">
              <span>IV Rank</span><b style="color:${barC}">${rankPct}%</b>
            </div>
            <div style="height:6px;background:var(--border2);border-radius:3px;overflow:hidden">
              <div style="width:${rankPct}%;height:100%;background:${barC};border-radius:3px;transition:width .3s"></div>
            </div>
            <div style="font-size:9px;color:var(--muted);margin-top:2px">Low: ${d.hv_low}% · High: ${d.hv_high}%</div>
          </div>

          <!-- IV Percentile meter -->
          <div style="min-width:140px">
            <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--muted);margin-bottom:3px">
              <span>IV Percentile</span><b style="color:${barC}">${pctPct}%</b>
            </div>
            <div style="height:6px;background:var(--border2);border-radius:3px;overflow:hidden">
              <div style="width:${pctPct}%;height:100%;background:${barC}aa;border-radius:3px;transition:width .3s"></div>
            </div>
            <div style="font-size:9px;color:var(--muted);margin-top:2px">Days below current: ${pctPct}% of past year</div>
          </div>

          <!-- HV term structure -->
          <div>
            <div style="font-size:9px;color:var(--muted);margin-bottom:3px;text-transform:uppercase;letter-spacing:.5px">HV Term Structure <span style="color:${trendC}">${trendArrow} ${d.iv_trend}</span></div>
            <div style="display:flex;gap:8px;font-size:11px;font-family:monospace">
              <span style="color:var(--muted)">5d:</span><b>${d.hv_5}%</b>
              <span style="color:var(--muted)">10d:</span><b>${d.hv_10}%</b>
              <span style="color:var(--muted)">21d:</span><b style="color:var(--accent)">${d.hv_21}%</b>
              <span style="color:var(--muted)">63d:</span><b>${d.hv_63}%</b>
            </div>
          </div>
        </div>

        <!-- Right: advice + strategies -->
        <div style="max-width:320px">
          <div style="font-size:11px;color:var(--text2);margin-bottom:6px;line-height:1.4">${d.advice}</div>
          <div style="display:flex;gap:4px;flex-wrap:wrap">${strats}</div>
        </div>

      </div>
    </div>`;
}


window._taAddLeg = _taAddLeg;
window._taTemplate = _taTemplate;
window._taAnalyze = _taAnalyze;

// Purpose-built entry point for other pages (e.g. Greeks Strategy
// Scanner, running in a same-origin iframe) to hand off a full strategy
// into Trade Analysis. _taLegs is `let`-scoped to this script, not a
// `var`, so it was never actually reachable as window._taLegs from
// outside -- an external assignment to that name would silently create
// an unrelated property with no effect on the real state this file's
// own functions close over. This function runs INSIDE this script's own
// scope, so it correctly touches the real _taLegs and can call
// _taRenderLegs()/_taAnalyze() directly, same as this file's own code.
window._taLoadLegs = function(legs, opts) {
  opts = opts || {};
  _taLegs = (legs || []).map(l => ({
    side: l.side, option_type: l.option_type, strike: l.strike,
    entry_price: l.entry_price, qty: l.qty || 1, leg_iv: l.leg_iv || ''
  }));
  const symEl = document.getElementById('ta-sym');
  const spotEl = document.getElementById('ta-spot');
  const ivEl = document.getElementById('ta-iv');
  const dteEl = document.getElementById('ta-dte');
  if (symEl && opts.symbol) symEl.value = opts.symbol;
  if (spotEl && opts.spot != null) spotEl.value = opts.spot;
  if (ivEl && opts.iv_pct != null) ivEl.value = opts.iv_pct;
  if (dteEl && opts.dte != null) dteEl.value = opts.dte;
  _taRenderLegs();
  if (opts.autoAnalyze !== false) _taAnalyze();
};


