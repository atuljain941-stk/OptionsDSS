
const LAST_QUERY_KEY = 'scanner_builder_last_executed_query_v3';
let watchlists = [];
let savedScanners = [];
let scannerCatalog = {saved_scanners: [], builtin_scanners: [], functions: [], function_meta: [], timeframes: [], indicators: []};
let functionCatalog = [];
let selectedScannerId = null;
let autocompleteItems = [];
let autocompleteIndex = -1;
let columnTemplates = [];
let resultColumns = [];
let selectedColumnTemplateId = '';
let resultSort = { index: null, dir: 1 };
let columnValuesStale = false;
let autoRefreshColumns = false;
const BUILTIN_DEFAULT_COLUMNS = [
  {label:'Symbol',expr:'symbol',format:'text',locked:true},
  {label:'Price',expr:'close[1d]',format:'price'},
  {label:'RS',expr:'RelativeStrength(20, "1d")',format:'number'},
  {label:'Leadership',expr:'leadership',format:'pct0'},
  {label:'RSI',expr:'rsi14[1d]',format:'number'},
  {label:'RSIDiff90',expr:'RSIDiff90(90, "1d")',format:'number'},
  {label:'UAE 1D',expr:'UAERegime("1d")',format:'text'},
  {label:'Reason',expr:'reason',format:'text',locked:true},
];

function esc(s){return String(s ?? '').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
function currentQueryText(){return document.getElementById('queryText');}
function setStatus(msg){const el=document.getElementById('status'); if(el) el.textContent = msg || '';}
function setSummary(text){const el=document.getElementById('scanSummary'); if(el) el.textContent = text || '';}
function scannerSnippet(x){const raw=(x.query_text||'').trim(); return raw || `scan(${x.name})`;}
function functionSnippet(x){const sig=String(x.signature||x.name||'').trim(); return sig || `${x.name || ''}()`;}
function getSelectedWatchlistId(){const v=document.getElementById('watchlistSel').value; return v ? Number(v) : null;}
function defaultColumns(){
  return normalizeColumns(BUILTIN_DEFAULT_COLUMNS);
}
function defaultTemplateColumns(){
  const templ = columnTemplates.find(t=>Number(t.is_default||0)===1) || columnTemplates[0];
  if(templ && Array.isArray(templ.columns) && templ.columns.length) return normalizeColumns(templ.columns);
  return defaultColumns();
}
function normalizeColumns(cols){
  if(typeof cols === 'string') { try { cols = JSON.parse(cols); } catch(e) { cols = []; } }
  if(!Array.isArray(cols)) cols=[];
  const seen=new Set();
  return cols.map((c,i)=>{
    if(typeof c==='string') c={expr:c};
    const expr=String(c.expr||c.name||c.key||'').trim();
    if(!expr) return null;
    const label=String(c.label||c.title||formatColumnLabel(expr)).trim() || `Column ${i+1}`;
    const key=label.toLowerCase();
    if(seen.has(key)) return null;
    seen.add(key);
    let fmt=String(c.format||'auto').toLowerCase();
    if(!fmt || fmt==='auto' || (fmt==='text' && inferFormat(expr)!=='text')) fmt=inferFormat(expr);
    return {label, expr, format:fmt, locked:!!c.locked};
  }).filter(Boolean).slice(0,24);
}
function formatColumnLabel(expr){
  const e=String(expr||'').trim();
  if(e.toLowerCase()==='symbol') return 'Symbol';
  if(e.toLowerCase()==='reason') return 'Reason';
  if(/^close\[/i.test(e)) return 'Price';
  const m=e.match(/^([A-Za-z_][A-Za-z0-9_]*)/);
  return m ? m[1].replace(/_/g,' ') : e;
}
function inferFormat(expr){
  const e=String(expr||'').toLowerCase().trim();
  if(['symbol','reason','scanner_reason','match_reason'].includes(e)) return 'text';
  if(e.includes('uaeregime')||e.includes('uae_regime')||e.includes('flowbias')||e.includes('flow_bias')||e.includes('sector')||e.includes('regime')||e.includes('signal')||e.includes('bias')) return 'text';
  if(e.includes('price')||e.includes('close')||e.includes('open')||e.includes('high')||e.includes('low')||e.includes('ema')||e.includes('support')||e.includes('resistance')||e.includes('fib')) return 'price';
  if(e.includes('pct')||e.includes('percent')||e.includes('leadership')||e.includes('rank')) return 'pct1';
  if(e.includes('days')||e.includes('count')||e.includes('age')||e==='oi') return 'integer';
  return 'number';
}
function getResultColumns(){
  if(!resultColumns.length) resultColumns = defaultColumns();
  return normalizeColumns(resultColumns);
}
function getPayload(definitionId=null){return {query_text:currentQueryText().value.trim(), watchlist_id:getSelectedWatchlistId(), benchmark:document.getElementById('benchmarkSel').value, limit:Number(document.getElementById('limitSel').value||200), definition_id:definitionId, columns:getResultColumns(), result_template_id:selectedColumnTemplateId || null};}

function sortValue(v){
  if(v===null||v===undefined||v==='') return {kind:'empty', value:''};
  if(Array.isArray(v)) v=v.join(', ');
  if(typeof v==='object') v=JSON.stringify(v);
  const raw=String(v).replace(/[$,%]/g,'').trim();
  const n=Number(raw);
  if(!Number.isNaN(n)&&Number.isFinite(n)&&raw!=='') return {kind:'number', value:n};
  return {kind:'text', value:String(v).toLowerCase()};
}
function compareValues(a,b){
  const av=sortValue(a), bv=sortValue(b);
  if(av.kind==='empty' && bv.kind==='empty') return 0;
  if(av.kind==='empty') return 1;
  if(bv.kind==='empty') return -1;
  if(av.kind==='number' && bv.kind==='number') return av.value-bv.value;
  return String(av.value).localeCompare(String(bv.value), undefined, {numeric:true, sensitivity:'base'});
}
function sortedRows(items, cols){
  const rows=Array.isArray(items)?items.slice():[];
  if(resultSort.index===null || resultSort.index===undefined || resultSort.index==='') return rows;
  const idx=Number(resultSort.index);
  if(!Number.isInteger(idx)||idx<0||idx>=cols.length) return rows;
  const col=cols[idx]; const dir=resultSort.dir<0?-1:1;
  return rows.sort((a,b)=>dir*compareValues(getRowValue(a,col), getRowValue(b,col)));
}
function resultHeaderHtml(cols){
  return '<tr>'+cols.map((c,i)=>`<th class="sortable" data-sort-col="${i}" title="Sort by ${esc(c.label)}">${esc(c.label)}${resultSort.index===i?`<span class="sort-mark">${resultSort.dir<0?'▼':'▲'}</span>`:''}</th>`).join('')+'</tr>';
}
function setResultSort(idx){
  idx=Number(idx);
  if(!Number.isInteger(idx)) return;
  if(resultSort.index===idx) resultSort.dir = resultSort.dir<0 ? 1 : -1;
  else resultSort = {index: idx, dir: 1};
  if(window._lastScanData) renderResults(window._lastScanData); else renderResultShell();
}
function allFunctionEntries(){const seen=new Set(); return (functionCatalog||[]).map(x=>({name:x.name,signature:x.signature||`${x.name}()`,description:x.description||'',category:'Primitive'})).filter(x=>{const k=String(x.name||'').toLowerCase(); if(!k||seen.has(k)) return false; seen.add(k); return true;});}
function allColumnPrimitiveEntries(){
  const out=[]; const seen=new Set();
  const add=(label,expr,fmt,cat,desc)=>{expr=String(expr||'').trim(); if(!expr) return; const k=expr.toLowerCase(); if(seen.has(k)) return; seen.add(k); out.push({label:label||formatColumnLabel(expr),expr,format:fmt||inferFormat(expr),category:cat||'Primitive',description:desc||''});};
  (scannerCatalog.column_primitives||[]).forEach(x=>add(x.label||x.name,x.expr||x.signature||x.name,x.format,x.category,x.description));
  allFunctionEntries().forEach(x=>add(x.name,functionSnippet(x),inferFormat(functionSnippet(x)),x.category,x.description));
  BUILTIN_DEFAULT_COLUMNS.forEach(x=>add(x.label,x.expr,x.format,'Default','Built-in default result column'));
  return out;
}
function fillColumnPrimitiveDatalist(){
  const dl=document.getElementById('columnPrimitiveDatalist'); if(!dl) return;
  dl.innerHTML=allColumnPrimitiveEntries().map(x=>`<option value="${esc(x.expr)}" label="${esc(x.label)}"></option>`).join('');
}
function canonicalColumnMeta(expr,label){
  const raw=String(expr||'').trim(); const key=raw.replace(/\s+/g,'').toLowerCase();
  const found=allColumnPrimitiveEntries().find(x=>String(x.expr||'').replace(/\s+/g,'').toLowerCase()===key || String(x.label||'').replace(/\s+/g,'').toLowerCase()===key);
  if(found) return {expr:found.expr,label:label||found.label,format:found.format||inferFormat(found.expr)};
  const fn=allFunctionEntries().find(x=>{const nm=String(x.name||'').toLowerCase(); const rx=new RegExp('^'+nm+'\\s*\\(', 'i'); return rx.test(raw);});
  if(fn){const fixed=raw.replace(new RegExp('^'+fn.name,'i'),fn.name); return {expr:fixed,label:label||fn.name,format:inferFormat(fixed)};}
  return {expr:raw,label:label||formatColumnLabel(raw),format:inferFormat(raw)};
}
function allScannerEntries(){const saved=(scannerCatalog.saved_scanners||[]).map(s=>({name:s.name,category:'Saved',description:s.description||s.query_text||'',query_text:s.query_text||'',id:s.id})); const builtins=(scannerCatalog.builtin_scanners||[]).map(s=>({...s,query_text:s.query_text||''})); const seen=new Set(); return [...saved,...builtins].filter(x=>{const k=String(x.name||'').toLowerCase(); if(!k||seen.has(k)) return false; seen.add(k); return true;});}
function setSelectOptions(sel, entries, placeholder){sel.innerHTML = `<option value="">${esc(placeholder)}</option>` + entries.map((x, idx)=>`<option value="${idx}">${esc((x.category ? x.category + ' — ' : '') + x.name)}</option>`).join('');}

function currentAutocompleteRange(ta){const cursor=ta.selectionStart??ta.value.length; const end=ta.selectionEnd??cursor; if(end!==cursor) return {start:cursor,end}; const before=ta.value.slice(0,cursor); const scanMatch=before.match(/scan\(\s*([A-Za-z0-9_\- ]*)$/i); if(scanMatch) return {start:before.length-scanMatch[0].length,end:cursor}; const fnParen=before.match(/([A-Za-z_][A-Za-z0-9_]*)\($/); if(fnParen) return {start:before.length-fnParen[0].length,end:cursor}; const token=before.match(/([A-Za-z_][A-Za-z0-9_]*)$/); if(token) return {start:before.length-token[0].length,end:cursor}; return {start:cursor,end:cursor};}
function replaceAutocompleteFragment(text, statusMessage='Inserted', cursorOffset=null){const ta=currentQueryText(); const range=currentAutocompleteRange(ta); ta.value=ta.value.slice(0,range.start)+text+ta.value.slice(range.end); const pos=range.start+(cursorOffset==null?text.length:cursorOffset); ta.setSelectionRange(pos,pos); ta.focus(); setStatus(statusMessage); showAutocomplete(); updateFunctionHelp();}
function insertScannerCall(name){replaceAutocompleteFragment(`scan(${name})`, `Inserted scan(${name})`);}
function insertFunctionCall(name){const call=`${name}()`; replaceAutocompleteFragment(call, `Inserted ${call}`, name.length+1);}
function currentTokenBeforeCursor(text,cursor){const before=text.slice(0,cursor); const scanMatch=before.match(/scan\(\s*([A-Za-z0-9_\- ]*)$/i); if(scanMatch) return {mode:'scan',typed:(scanMatch[1]||'').trim().toLowerCase()}; const fnParen=before.match(/([A-Za-z_][A-Za-z0-9_]*)\($/); if(fnParen) return {mode:'function',typed:fnParen[1].toLowerCase()}; const token=before.match(/([A-Za-z_][A-Za-z0-9_]*)$/); if(token) return {mode:'function',typed:token[1].toLowerCase()}; return {mode:'none',typed:''};}
function renderAutocompleteSelection(){const panel=document.getElementById('scanAutocomplete'); if(!panel) return; panel.querySelectorAll('.autocomplete-item').forEach(el=>el.classList.toggle('selected',Number(el.dataset.idx||-1)===autocompleteIndex));}
function chooseAutocomplete(delta=0){if(!autocompleteItems.length) return false; autocompleteIndex=Math.max(0,Math.min(autocompleteItems.length-1,autocompleteIndex+delta)); renderAutocompleteSelection(); return true;}
function commitAutocomplete(){if(!autocompleteItems.length) return false; const item=autocompleteItems[Math.max(0,autocompleteIndex)]||autocompleteItems[0]; if(!item) return false; if(item.kind==='function') insertFunctionCall(item.name); else replaceAutocompleteFragment(scannerSnippet(item),'Inserted scanner code block'); const panel=document.getElementById('scanAutocomplete'); if(panel) panel.style.display='none'; return true;}
function showAutocomplete(forceAll=false){const ta=currentQueryText(); const panel=document.getElementById('scanAutocomplete'); if(!ta||!panel) return; const token=currentTokenBeforeCursor(ta.value,ta.selectionStart??ta.value.length); const mode=token.mode; const typed=token.typed||''; if(mode!=='scan'&&mode!=='function'&&!forceAll){panel.style.display='none'; panel.innerHTML=''; autocompleteItems=[]; autocompleteIndex=-1; updateFunctionHelp(); return;} let entries=[]; if(mode==='scan'||forceAll){entries=allScannerEntries().filter(x=>!typed||String(x.name||'').toLowerCase().includes(typed)).slice(0,20).map(x=>({...x,kind:'scanner'}));} if(mode==='function'||(forceAll&&!entries.length)){const fnEntries=allFunctionEntries().filter(x=>!typed||String(x.name||'').toLowerCase().startsWith(typed)).slice(0,24).map(x=>({...x,kind:'function'})); entries=mode==='function'?fnEntries:entries.concat(fnEntries);} if(!entries.length){panel.style.display='none'; panel.innerHTML=''; autocompleteItems=[]; autocompleteIndex=-1; updateFunctionHelp(); return;} autocompleteItems=entries; autocompleteIndex=0; panel.innerHTML=entries.map((x,idx)=>x.kind==='function'?`
    <div class="autocomplete-item ${idx===autocompleteIndex?'selected':''}" data-idx="${idx}"><div style="min-width:0"><b>${esc(x.name)}</b> <span class="mini">${esc(x.signature||'')}</span><div class="mini">${esc(x.description||'Primitive')}</div></div><button class="btn secondary smallbtn" data-action="insert-fn" data-name="${esc(x.name)}">fn()</button></div>`:`
    <div class="autocomplete-item ${idx===autocompleteIndex?'selected':''}" data-idx="${idx}"><div style="min-width:0"><b>${esc(x.name)}</b><div class="mini">${esc(x.category||'Scanner')}</div><pre class="scanner-code">${esc(scannerSnippet(x))}</pre></div><div class="row tight" style="flex-shrink:0"><button class="btn secondary smallbtn" data-action="code" data-name="${esc(x.name)}">code</button><button class="btn secondary smallbtn" data-action="ref" data-name="${esc(x.name)}">scan()</button></div></div>`).join(''); panel.style.display='block'; panel.querySelectorAll('.autocomplete-item').forEach(el=>{el.addEventListener('mouseenter',()=>{autocompleteIndex=Number(el.dataset.idx||0); renderAutocompleteSelection();}); el.addEventListener('mousedown',(e)=>{const btn=e.target.closest('button[data-action]'); if(btn) return; e.preventDefault(); const item=autocompleteItems[Number(el.dataset.idx||0)]; if(!item) return; if(item.kind==='function') insertFunctionCall(item.name); else replaceAutocompleteFragment(scannerSnippet(item),'Inserted scanner code block'); panel.style.display='none';});}); panel.querySelectorAll('button[data-action]').forEach(btn=>btn.addEventListener('mousedown',(e)=>{e.preventDefault(); const item=autocompleteItems.find(x=>String(x.name)===String(btn.dataset.name)); if(!item) return; if(item.kind==='function'||btn.dataset.action==='insert-fn') insertFunctionCall(item.name); else if(btn.dataset.action==='ref') insertScannerCall(item.name); else replaceAutocompleteFragment(scannerSnippet(item),'Inserted scanner code block'); panel.style.display='none';})); updateFunctionHelp();}

function signatureParams(signature){const sig=String(signature||''); const m=sig.match(/^[^(]+\((.*)\)$/); if(!m) return []; let inside=m[1].replace(/[\[\]]/g,''); if(!inside.trim()) return []; return inside.split(',').map(p=>p.trim()).filter(Boolean);}
function findActiveFunction(text,cursor){let depth=0, quote=null; for(let i=cursor-1;i>=0;i--){const ch=text[i]; if(quote){if(ch===quote && text[i-1] !== '\\') quote=null; continue;} if(ch==='"'||ch==="'"){quote=ch; continue;} if(ch===')'){depth++; continue;} if(ch==='('){if(depth>0){depth--; continue;} let j=i-1; while(j>=0 && /\s/.test(text[j])) j--; let end=j+1; while(j>=0 && /[A-Za-z0-9_]/.test(text[j])) j--; const name=text.slice(j+1,end); if(!name) return null; const inner=text.slice(i+1,cursor); let paramIndex=0, d=0, q=null; for(let k=0;k<inner.length;k++){const c=inner[k]; if(q){if(c===q && inner[k-1] !== '\\') q=null; continue;} if(c==='"'||c==="'"){q=c; continue;} if(c==='('||c==='[') d++; else if(c===')'||c===']') d=Math.max(0,d-1); else if(c===','&&d===0) paramIndex++;} return {name,paramIndex,inner,start:j+1,open:i};}} return null;}
function updateFunctionHelp(){const hint=document.getElementById('funcHint'); const ta=currentQueryText(); if(!hint||!ta) return; const text=ta.value; const cursor=ta.selectionStart??text.length; const active=findActiveFunction(text,cursor); let meta=null, paramIndex=0; if(active){paramIndex=active.paramIndex; meta=allFunctionEntries().find(x=>String(x.name||'').toLowerCase()===String(active.name||'').toLowerCase());} else {const tok=currentTokenBeforeCursor(text,cursor); if(tok.mode==='function'&&tok.typed) meta=allFunctionEntries().find(x=>String(x.name||'').toLowerCase().startsWith(tok.typed));}
  if(!meta){hint.innerHTML='Type a primitive name, choose one from autocomplete, or place your cursor inside a function to see parameter help.'; return;}
  const params=signatureParams(meta.signature); const paramHtml=params.length?`<div style="margin-top:3px">${params.map((p,i)=>`<span class="param ${active&&i===Math.min(paramIndex,params.length-1)?'active':''}">${i+1}. ${esc(p)}</span>`).join('')}</div>`:'';
  const current=params.length?params[Math.min(paramIndex,params.length-1)]:'value';
  hint.innerHTML=`<b>${esc(meta.name)}</b> <span class="dim">${esc(meta.signature||'')}</span><br><span>${esc(meta.description||'')}</span>${active?`<br><span class="warn">Current parameter: ${esc(current)}</span>`:''}${paramHtml}`;
}

function renderColumnTemplates(){
  const sel=document.getElementById('columnTemplateSelect');
  if(!sel) return;
  sel.innerHTML='<option value="">Custom columns</option>'+columnTemplates.map(t=>`<option value="${esc(t.id)}">${Number(t.is_default||0)?'★ ':''}${esc(t.name)}</option>`).join('');
  sel.value=selectedColumnTemplateId || '';
}
function updateColumnRefreshUi(){
  const queryReady=!!String(currentQueryText()?.value||'').trim();
  const refreshBtn=document.getElementById('refreshColumnValuesBtn');
  const realignBtn=document.getElementById('realignColumnsBtn');
  const note=document.getElementById('columnRefreshNote');
  if(refreshBtn){
    refreshBtn.disabled=!queryReady;
    refreshBtn.classList.toggle('danger', !!columnValuesStale);
    refreshBtn.textContent=columnValuesStale?'Refresh values *':'Refresh values';
  }
  if(realignBtn){
    realignBtn.disabled=!window._lastScanData;
  }
  if(note){
    note.textContent=columnValuesStale
      ? 'Columns changed. Table was realigned from the last scan; click Refresh values to recompute new primitive columns.'
      : 'Tip: drag chips to reorder. Use Realign table after layout-only changes; use Refresh values after adding/changing primitive columns.';
  }
}
function refreshCurrentResultLayout(statusMsg='Column layout updated.', needsValueRefresh=false){
  const cols=getResultColumns();
  if(resultSort.index!==null && resultSort.index!==undefined && Number(resultSort.index)>=cols.length) resultSort={index:null,dir:1};
  if(window._lastScanData){
    window._lastScanData={...window._lastScanData,result_columns:cols,result_template_id:selectedColumnTemplateId||null};
    renderResults(window._lastScanData);
  } else {
    renderResultShell();
  }
  if(needsValueRefresh && window._lastScanData){
    columnValuesStale=true;
    updateColumnRefreshUi();
    if(autoRefreshColumns){
      setStatus(`${statusMsg} Refreshing values…`);
      setTimeout(()=>refreshColumnValues().catch(e=>alert(e.message||e)),0);
      return;
    }
    setStatus(`${statusMsg} Click Refresh values to recompute primitive values.`);
  } else {
    updateColumnRefreshUi();
    setStatus(statusMsg);
  }
}
async function refreshColumnValues(){
  const payload=getPayload();
  if(!payload.query_text){alert('Enter or load a scanner query first.'); return;}
  const btn=document.getElementById('refreshColumnValuesBtn');
  if(btn){btn.disabled=true; btn.textContent='Refreshing…';}
  setStatus('Refreshing result values for selected columns…');
  try{
    await runScanner();
    columnValuesStale=false;
    updateColumnRefreshUi();
  } finally {
    if(btn){btn.disabled=false; updateColumnRefreshUi();}
  }
}
function moveColumn(idx,delta){
  resultColumns=getResultColumns();
  const next=idx+delta;
  if(idx<0||next<0||idx>=resultColumns.length||next>=resultColumns.length) return;
  const tmp=resultColumns[idx]; resultColumns[idx]=resultColumns[next]; resultColumns[next]=tmp;
  selectedColumnTemplateId=''; renderColumnTemplates(); renderColumnList();
  refreshCurrentResultLayout('Column order changed. Click Refresh values if any columns were newly added. Save template to keep this order.');
}
function renderColumnList(){
  const list=document.getElementById('columnList'); if(!list) return;
  const cols=getResultColumns();
  list.innerHTML=cols.map((c,i)=>`<span class="column-chip" draggable="true" data-col-index="${i}" title="Drag to reorder · ${esc(c.expr)}"><span class="col-grip" aria-hidden="true">⋮⋮</span><span class="col-meta"><b>${esc(c.label)}</b> <code>${esc(c.expr)}</code></span><span class="col-actions">${c.locked?'<span class="mini">locked</span>':`<button class="col-del" data-col-del="${i}" title="Remove">×</button>`}</span></span>`).join('');
  list.querySelectorAll('button[data-col-del]').forEach(btn=>btn.addEventListener('click',()=>{const idx=Number(btn.dataset.colDel); resultColumns.splice(idx,1); selectedColumnTemplateId=''; renderColumnTemplates(); renderColumnList(); refreshCurrentResultLayout('Column removed. Save template to keep this change.');}));
  list.querySelectorAll('.column-chip[draggable="true"]').forEach(chip=>{
    chip.addEventListener('dragstart',e=>{e.dataTransfer.setData('text/plain', chip.dataset.colIndex||''); e.dataTransfer.effectAllowed='move';});
    chip.addEventListener('dragover',e=>{e.preventDefault(); chip.classList.add('drag-over');});
    chip.addEventListener('dragleave',()=>chip.classList.remove('drag-over'));
    chip.addEventListener('drop',e=>{
      e.preventDefault(); chip.classList.remove('drag-over');
      const from=Number(e.dataTransfer.getData('text/plain'));
      const to=Number(chip.dataset.colIndex||-1);
      if(Number.isFinite(from)&&Number.isFinite(to)&&from!==to){
        resultColumns=getResultColumns();
        const moved=resultColumns.splice(from,1)[0];
        resultColumns.splice(to,0,moved);
        selectedColumnTemplateId=''; renderColumnTemplates(); renderColumnList();
        refreshCurrentResultLayout('Column order changed. Click Refresh values if any columns were newly added. Save template to keep this order.');
      }
    });
  });
  renderResultShell();
}
function renderResultShell(){
  const head=document.getElementById('resultsHead'); const body=document.getElementById('resultsBody');
  const cols=getResultColumns();
  if(head) head.innerHTML=resultHeaderHtml(cols);
  const hasRows=Array.isArray(window._lastScanResults)&&window._lastScanResults.length;
  if(body && !hasRows) body.innerHTML=`<tr><td colspan="${cols.length}" class="small">Run a scan to evaluate the selected primitive columns.</td></tr>`;
}
async function loadColumnTemplates(){
  try{const r=await fetch('/scanner-builder/api/column-templates'); const d=await r.json(); columnTemplates=d.templates||[]; const def=columnTemplates.find(t=>Number(t.is_default||0)===1)||columnTemplates[0]; if(!resultColumns.length){resultColumns=def?normalizeColumns(def.columns):defaultColumns();} selectedColumnTemplateId=def?String(def.id):''; const inp=document.getElementById('columnTemplateNameInput'); if(inp&&def) inp.value=def.name||''; renderColumnTemplates(); renderColumnList();}
  catch(e){console.warn('column templates failed',e); resultColumns=defaultColumns(); renderColumnTemplates(); renderColumnList();}
}
function applyColumnTemplate(id){
  const t=columnTemplates.find(x=>String(x.id)===String(id));
  if(!t){selectedColumnTemplateId=''; resultColumns=defaultTemplateColumns();}
  else{selectedColumnTemplateId=String(t.id); resultColumns=normalizeColumns(t.columns); const inp=document.getElementById('columnTemplateNameInput'); if(inp) inp.value=t.name||'';}
  renderColumnTemplates(); renderColumnList(); refreshCurrentResultLayout(t?`Applied columns: ${t.name}.`:'Using default columns.', true);
}
function addColumn(label,expr,fmt){
  const meta=canonicalColumnMeta(expr,label);
  expr=String(meta.expr||'').trim(); if(!expr) return alert('Enter a primitive/expression first.');
  resultColumns=getResultColumns();
  const exists=resultColumns.some(c=>String(c.expr||'').replace(/\s+/g,'').toLowerCase()===expr.replace(/\s+/g,'').toLowerCase() || String(c.label||'').toLowerCase()===String(meta.label||'').toLowerCase());
  if(exists) return alert('That column is already in the result columns.');
  let effectiveFmt=String(fmt||'auto').toLowerCase();
  if(!effectiveFmt || effectiveFmt==='auto') effectiveFmt=meta.format||inferFormat(expr)||'number';
  if(effectiveFmt==='text' && inferFormat(expr)!=='text') effectiveFmt=inferFormat(expr);
  resultColumns.push({label:String(meta.label||formatColumnLabel(expr)).trim(), expr, format:effectiveFmt});
  selectedColumnTemplateId=''; renderColumnTemplates(); renderColumnList(); refreshCurrentResultLayout(`Added column: ${meta.label||expr}.`, true);
}
function addSelectedPrimitiveColumn(){
  const typedExpr=String(document.getElementById('columnExprInput')?.value||'').trim();
  const typedLabel=String(document.getElementById('columnLabelInput')?.value||'').trim();
  if(typedExpr){ addColumn(typedLabel, typedExpr, document.getElementById('columnFormatSelect').value); return; }
  const item=selectedPrimitive();
  if(item){ const expr=functionSnippet(item); addColumn(item.name, expr, inferFormat(expr)); return; }
  alert('Type a primitive/expression first, for example RSIDiff90() or RSIDiff90(90, "1d").');
}
async function saveColumnTemplate(){
  const input=document.getElementById('columnTemplateNameInput');
  const name=String(input?.value||'').trim();
  if(!name){ if(input) input.focus(); return alert('Enter a template name first.'); }
  const desc='Scanner result primitive columns';
  const r=await fetch('/scanner-builder/api/column-templates',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,description:desc,columns:getResultColumns()})});
  const d=await r.json(); if(!r.ok||d.error) throw new Error(d.error||r.statusText);
  columnTemplates=d.templates||[]; const t=columnTemplates.find(x=>String(x.name).toLowerCase()===name.toLowerCase()); selectedColumnTemplateId=t?String(t.id):''; renderColumnTemplates(); renderColumnList(); setStatus(`Saved column template: ${name}`);
}
async function deleteColumnTemplate(){
  const id=document.getElementById('columnTemplateSelect').value; if(!id) return alert('Select a saved template to delete.');
  const t=columnTemplates.find(x=>String(x.id)===String(id)); if(t&&Number(t.is_default||0)===1) return alert('Default template cannot be deleted.');
  if(!confirm(`Delete column template '${t?t.name:id}'?`)) return;
  const r=await fetch(`/scanner-builder/api/column-templates/${id}`,{method:'DELETE'}); const d=await r.json(); if(!r.ok||d.error) throw new Error(d.error||r.statusText);
  columnTemplates=d.templates||[]; selectedColumnTemplateId=''; resultColumns=defaultColumns(); renderColumnTemplates(); renderColumnList(); refreshCurrentResultLayout('Column template deleted. Default columns restored.', true);
}
function fmtCell(v,fmt){
  if(v===null||v===undefined||v==='') return '—';
  if(Array.isArray(v)) return v.join(', ');
  if(typeof v==='object') return JSON.stringify(v).slice(0,160);
  const n=Number(v);
  const fmtName=String(fmt||'auto').toLowerCase();
  if(fmtName==='raw') return String(v);
  if(!Number.isNaN(n) && Number.isFinite(n) && String(v).trim()!==''){
    if(fmtName==='price') return '$'+n.toFixed(2);
    if(fmtName==='integer'||fmtName==='number0') return Math.round(n).toLocaleString();
    if(fmtName==='pct0') return n.toFixed(0)+'%';
    if(fmtName==='pct1') return n.toFixed(1)+'%';
    if(fmtName==='pct2') return n.toFixed(2)+'%';
    if(fmtName==='number1') return n.toFixed(1).replace(/\.0$/,'');
    if(fmtName==='number3') return n.toFixed(3).replace(/\.000$/,'');
    if(fmtName==='number4') return n.toFixed(4).replace(/\.0000$/,'');
    if(fmtName==='number'||fmtName==='number2'||fmtName==='auto'||fmtName==='text'||!fmtName) return n.toFixed(Number.isInteger(n)?0:2).replace(/\.00$/,'');
  }
  return String(v);
}
function getRowValue(row,col){
  const vals=row._result_columns||{};
  if(Object.prototype.hasOwnProperty.call(vals,col.label)) return vals[col.label];
  if(Object.prototype.hasOwnProperty.call(vals,col.expr)) return vals[col.expr];
  const expr=String(col.expr||'').toLowerCase();
  if(expr==='symbol') return row.symbol;
  if(expr==='reason') return (row.reason||[]).join(' | ');
  if(expr==='close[1d]'||expr==='price') return row.price ?? row.close;
  return row[expr] ?? row[col.expr] ?? null;
}
function renderResults(d){
  window._lastScanData = d || {results: []};
  const body=document.getElementById('resultsBody'); const head=document.getElementById('resultsHead'); const summary=document.getElementById('resultsSummary');
  const rawItems=(d&&Array.isArray(d.results))?d.results:[];
  window._lastScanResults=rawItems;
  resultColumns=normalizeColumns((d&&d.result_columns)||resultColumns||defaultColumns());
  selectedColumnTemplateId=d&&d.result_template_id?String(d.result_template_id):selectedColumnTemplateId;
  renderColumnTemplates();
  renderColumnList();
  const cols=getResultColumns();
  const items=sortedRows(rawItems, cols);
  if(head) head.innerHTML=resultHeaderHtml(cols);
  if(!items.length){
    const errCount=Number((d&&d.error_count)!=null?d.error_count:((d&&Array.isArray(d.errors))?d.errors.length:0));
    const loadedCount=Number((d&&d.loaded_count)!=null?d.loaded_count:0);
    const firstErr=(d&&Array.isArray(d.errors)&&d.errors.length)?d.errors[0]:null;
    const errText=firstErr?` First error: ${esc(firstErr.symbol||'symbol')} - ${esc(firstErr.error||firstErr)}`:'';
    if(body) body.innerHTML=`<tr><td colspan="${cols.length}" class="small">${errCount&&loadedCount===0?'No symbols could be evaluated due to data-load errors.':'No matches.'}${errText}</td></tr>`;
    if(summary){summary.style.display='block'; summary.innerHTML=errCount&&loadedCount===0?`Scanner could not evaluate the selected watchlist. ${errCount} symbol load error(s).${errText}`:'No symbols matched the current query.';}
    return;
  }
  if(body) body.innerHTML=items.map(r=>`<tr>${cols.map(c=>{const v=getRowValue(r,c); const n=Number(v); const cls=!Number.isNaN(n)&&String(v).trim()!==''?(n<0?'neg':(n>0?'pos':'')):''; const extra=String(c.expr||'').toLowerCase()==='reason'?' reason-cell small':''; return `<td class="${cls}${extra}">${esc(fmtCell(v,c.format))}</td>`;}).join('')}</tr>`).join('');
  if(summary){summary.style.display='block'; summary.innerHTML=`${rawItems.length} match(es) across ${d&&d.symbols?d.symbols.length:'selected'} symbols. Benchmark: <b>${esc(d&&d.benchmark?d.benchmark:'SPY')}</b>. Scanned: ${esc(d&&d.scanned_at?d.scanned_at:'now')}`;}
}
function renderSavedDropdown(){const sel=document.getElementById('savedScannerSelect'); if(!savedScanners.length){sel.innerHTML='<option value="">No saved scanners</option>'; updateSavedPreview(); return;} sel.innerHTML='<option value="">Select saved scanner…</option>'+savedScanners.map(s=>`<option value="${s.id}">${esc(s.name)}${s.last_run_count!=null?` · ${s.last_run_count} matches`:''}</option>`).join(''); if(selectedScannerId && savedScanners.some(s=>Number(s.id)===Number(selectedScannerId))) sel.value=String(selectedScannerId); updateSavedPreview();}
function updateSavedPreview(){const id=document.getElementById('savedScannerSelect').value; const s=savedScanners.find(x=>String(x.id)===String(id)); const prev=document.getElementById('savedPreview'); const alert=document.getElementById('savedAlertEnabled'); const mode=document.getElementById('savedAlertMode'); if(!s){prev.textContent=''; alert.checked=false; mode.value='enter_exit'; return;} prev.textContent=(s.description?`${s.description}\n`:'')+(s.query_text||''); alert.checked=!!s.alert_enabled; mode.value=s.alert_mode||'enter_exit';}
function renderCatalogDropdown(){const entries=allScannerEntries(); const sel=document.getElementById('catalogSelect'); setSelectOptions(sel, entries, 'Select catalog scanner…'); updateCatalogPreview();}
function selectedCatalogItem(){const entries=allScannerEntries(); const idx=document.getElementById('catalogSelect').value; return idx===''?null:entries[Number(idx)];}
function updateCatalogPreview(){const item=selectedCatalogItem(); document.getElementById('catalogPreview').textContent=item?((item.description?item.description+'\n':'')+scannerSnippet(item)):'';}
function renderPrimitiveDropdown(){const entries=allFunctionEntries(); const sel=document.getElementById('primitiveSelect'); setSelectOptions(sel, entries, 'Select primitive…'); updatePrimitivePreview();}
function selectedPrimitive(){const entries=allFunctionEntries(); const idx=document.getElementById('primitiveSelect').value; return idx===''?null:entries[Number(idx)];}
function updatePrimitivePreview(){const item=selectedPrimitive(); const prev=document.getElementById('primitivePreview'); if(!item){prev.textContent=''; return;} prev.textContent=(item.signature||item.name+'()')+'\n'+(item.description||''); document.getElementById('columnLabelInput').value=item.name||''; document.getElementById('columnExprInput').value=functionSnippet(item); document.getElementById('columnFormatSelect').value=inferFormat(functionSnippet(item)); updateFunctionHelp();}

async function loadWatchlists(){const sel=document.getElementById('watchlistSel'); sel.innerHTML='<option value="">Loading…</option>'; const r=await fetch('/scanner-builder/api/watchlists'); const d=await r.json(); watchlists=d.watchlists||[]; sel.innerHTML='<option value="">All symbols table</option>'+watchlists.map(w=>`<option value="${w.id}" ${w.is_default?'selected':''}>${esc(`${w.name} (${w.symbol_count||0})`)}</option>`).join(''); const saved=localStorage.getItem('scanner_builder_watchlist_id')||''; const savedOk=saved&&watchlists.some(w=>String(w.id)===String(saved)); let preferred=savedOk?saved:''; if(!preferred&&watchlists.length){const defaultOne=watchlists.find(w=>Number(w.is_default||0)===1&&Number(w.symbol_count||0)>0); const withSymbols=watchlists.find(w=>Number(w.symbol_count||0)>0); preferred=String((defaultOne||withSymbols||watchlists[0]).id||'');} if(preferred) sel.value=String(preferred); localStorage.setItem('scanner_builder_watchlist_id',sel.value||'');}
async function loadDefinitions(){const r=await fetch('/scanner-builder/api/definitions'); const d=await r.json(); savedScanners=d.definitions||[]; scannerCatalog.saved_scanners=savedScanners; renderSavedDropdown(); renderCatalogDropdown(); showAutocomplete();}
async function loadCatalog(){const r=await fetch('/scanner-builder/api/catalog'); const d=await r.json(); scannerCatalog=d||scannerCatalog; scannerCatalog.saved_scanners=savedScanners; functionCatalog=scannerCatalog.function_meta || (scannerCatalog.functions||[]).map(s=>({name:String(s).split('(')[0],signature:s,description:'Built-in function'})); renderCatalogDropdown(); renderPrimitiveDropdown(); fillColumnPrimitiveDatalist(); showAutocomplete(); updateFunctionHelp();}
async function loadScanner(id){const s=savedScanners.find(x=>Number(x.id)===Number(id)); if(!s) return; selectedScannerId=Number(id); document.getElementById('savedScannerSelect').value=String(id); document.getElementById('scannerName').value=s.name||''; document.getElementById('scannerDesc').value=s.description||''; currentQueryText().value=s.query_text||''; document.getElementById('benchmarkSel').value=s.benchmark||'SPY'; if(s.watchlist_id) document.getElementById('watchlistSel').value=String(s.watchlist_id); const cols=normalizeColumns(s.result_columns_json||[]); if(cols.length){resultColumns=cols; selectedColumnTemplateId=s.result_template_id?String(s.result_template_id):'';} else if(s.result_template_id){applyColumnTemplate(s.result_template_id);} else {resultColumns=defaultColumns(); selectedColumnTemplateId=''; renderColumnTemplates(); renderColumnList();} updateSavedPreview(); updateFunctionHelp(); setStatus(`Loaded scanner: ${s.name}`);}
async function updateSelectedScannerAlert(){const id=Number(document.getElementById('savedScannerSelect').value||0); if(!id) return; const enabled=!!document.getElementById('savedAlertEnabled').checked; const alert_mode=document.getElementById('savedAlertMode').value||'enter_exit'; const r=await fetch(`/scanner-builder/api/definitions/${id}`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({alert_enabled:enabled,alert_mode})}); const d=await r.json(); if(!r.ok||d.error) throw new Error(d.error||r.statusText); setStatus(`Scanner alert ${enabled?'enabled':'disabled'} (${alert_mode})`); await loadDefinitions();}
async function saveScanner(updateExisting){const query=currentQueryText().value.trim(); if(!query){alert('Enter a query first.'); return;} const payload={name:document.getElementById('scannerName').value.trim(),description:document.getElementById('scannerDesc').value.trim(),query_text:query,builder_json:[],watchlist_id:getSelectedWatchlistId(),benchmark:document.getElementById('benchmarkSel').value,result_columns_json:getResultColumns(),result_template_id:selectedColumnTemplateId||null}; if(!payload.name){payload.name=prompt('Scanner name','My Scanner'); if(!payload.name) return;} let url='/scanner-builder/api/definitions'; let method='POST'; if(updateExisting&&selectedScannerId){url=`/scanner-builder/api/definitions/${selectedScannerId}`; method='PUT';} const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const d=await r.json(); if(!r.ok||d.error) throw new Error(d.error||r.statusText); selectedScannerId=d.definition?d.definition.id:selectedScannerId; setStatus(updateExisting?'Scanner updated':'Scanner saved'); await loadDefinitions();}
async function deleteScanner(id){const s=savedScanners.find(x=>Number(x.id)===Number(id)); if(!s) return; if(!confirm(`Delete scanner '${s.name}'?`)) return; const r=await fetch(`/scanner-builder/api/definitions/${id}`,{method:'DELETE'}); const d=await r.json(); if(!r.ok||d.error) throw new Error(d.error||r.statusText); if(selectedScannerId===id) selectedScannerId=null; await loadDefinitions(); setStatus('Scanner deleted');}
function saveLastExecuted(payload,d){try{localStorage.setItem(LAST_QUERY_KEY,JSON.stringify({query_text:payload.query_text,watchlist_id:payload.watchlist_id,benchmark:payload.benchmark,limit:payload.limit,columns:payload.columns,result_template_id:payload.result_template_id,scanner_name:document.getElementById('scannerName').value.trim(),scanner_desc:document.getElementById('scannerDesc').value.trim(),saved_at:new Date().toISOString(),count:d.count||0})); document.getElementById('lastQueryNote').textContent='Last executed query cached';}catch(e){}}
function restoreLastExecuted(){try{const raw=localStorage.getItem(LAST_QUERY_KEY); if(!raw) return false; const x=JSON.parse(raw); if(!x||!x.query_text) return false; currentQueryText().value=x.query_text||''; if(x.watchlist_id) document.getElementById('watchlistSel').value=String(x.watchlist_id); if(x.benchmark) document.getElementById('benchmarkSel').value=x.benchmark; if(x.limit) document.getElementById('limitSel').value=String(x.limit); if(x.scanner_name) document.getElementById('scannerName').value=x.scanner_name; if(x.scanner_desc) document.getElementById('scannerDesc').value=x.scanner_desc; if(x.columns){resultColumns=normalizeColumns(x.columns); selectedColumnTemplateId=x.result_template_id?String(x.result_template_id):''; renderColumnTemplates(); renderColumnList();} document.getElementById('lastQueryNote').textContent=`Restored last run${x.count!=null?' · '+x.count+' matches':''}`; updateFunctionHelp(); return true;}catch(e){return false;}}
async function runScanner(defId=null){let payload=getPayload(defId); if(defId){const s=savedScanners.find(x=>Number(x.id)===Number(defId)); if(s){const savedCols=normalizeColumns(s.result_columns_json||[]); if(savedCols.length){resultColumns=savedCols; selectedColumnTemplateId=s.result_template_id?String(s.result_template_id):'';} payload={...payload,definition_id:defId,query_text:s.query_text||'',watchlist_id:s.watchlist_id||payload.watchlist_id,benchmark:s.benchmark||payload.benchmark,columns:getResultColumns(),result_template_id:selectedColumnTemplateId||null}; currentQueryText().value=payload.query_text; document.getElementById('benchmarkSel').value=payload.benchmark||'SPY'; if(payload.watchlist_id) document.getElementById('watchlistSel').value=String(payload.watchlist_id); selectedScannerId=Number(defId); document.getElementById('savedScannerSelect').value=String(defId);}} if(!payload.query_text){alert('Enter a query first.'); return;} setStatus('Scanning…'); setSummary(''); const r=await fetch('/scanner-builder/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}); const d=await r.json(); if(!r.ok||d.error){setStatus('Error'); document.getElementById('resultsBody').innerHTML=`<tr><td colspan="${getResultColumns().length}" class="small">${esc(d.error||r.statusText)}</td></tr>`; return;} columnValuesStale=false; updateColumnRefreshUi(); setStatus(`Done · ${d.count} match(es)`); renderResults(d); saveLastExecuted(payload,d); await loadDefinitions();}
async function validateQuery(){const payload=getPayload(); if(!payload.query_text) return alert('Enter a query first.'); const r=await fetch('/scanner-builder/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,limit:1})}); const d=await r.json(); if(!r.ok){alert(d.error||r.statusText); return;} setStatus(`Valid query · ${d.clauses?d.clauses.length:0} clause(s)`);}
function copyQuery(){const txt=currentQueryText().value; navigator.clipboard.writeText(txt).then(()=>setStatus('Query copied'));}
function clearPage(){selectedScannerId=null; document.getElementById('scannerName').value=''; document.getElementById('scannerDesc').value=''; currentQueryText().value=''; const cols=getResultColumns(); renderResultShell(); document.getElementById('resultsBody').innerHTML=`<tr><td colspan="${cols.length}" class="small">Run a scan to see results.</td></tr>`; document.getElementById('resultsSummary').style.display='none'; setSummary(''); setStatus('Cleared'); updateFunctionHelp();}

async function init(){await loadWatchlists(); await loadDefinitions(); await loadCatalog(); await loadColumnTemplates(); restoreLastExecuted(); document.getElementById('watchlistSel').addEventListener('change',e=>localStorage.setItem('scanner_builder_watchlist_id',e.target.value||'')); const q=currentQueryText(); q.addEventListener('input',()=>{setStatus('Text updated'); showAutocomplete(); updateFunctionHelp(); updateColumnRefreshUi();}); q.addEventListener('keyup',()=>{showAutocomplete(); updateFunctionHelp();}); q.addEventListener('click',()=>{showAutocomplete(); updateFunctionHelp();}); q.addEventListener('keydown',(e)=>{const panel=document.getElementById('scanAutocomplete'); const visible=panel&&panel.style.display==='block'; if(e.shiftKey&&e.key==='Enter'){e.preventDefault(); showAutocomplete(true); return;} if(visible&&e.key==='ArrowDown'){e.preventDefault(); chooseAutocomplete(1); return;} if(visible&&e.key==='ArrowUp'){e.preventDefault(); chooseAutocomplete(-1); return;} if(visible&&e.key==='Enter'){e.preventDefault(); commitAutocomplete(); return;} if(visible&&e.key==='Escape'){e.preventDefault(); panel.style.display='none'; return;}}); q.addEventListener('blur',()=>setTimeout(()=>{const panel=document.getElementById('scanAutocomplete'); if(panel) panel.style.display='none'; updateFunctionHelp();},150)); document.getElementById('savedScannerSelect').addEventListener('change',updateSavedPreview); document.getElementById('loadSavedBtn').addEventListener('click',()=>{const id=Number(document.getElementById('savedScannerSelect').value||0); if(id) loadScanner(id);}); document.getElementById('runSavedBtn').addEventListener('click',()=>{const id=Number(document.getElementById('savedScannerSelect').value||0); if(id) runScanner(id);}); document.getElementById('deleteSavedBtn').addEventListener('click',()=>{const id=Number(document.getElementById('savedScannerSelect').value||0); if(id) deleteScanner(id);}); document.getElementById('refreshSavedBtn').addEventListener('click',()=>loadDefinitions()); document.getElementById('savedAlertEnabled').addEventListener('change',()=>updateSelectedScannerAlert().catch(e=>alert(e.message))); document.getElementById('savedAlertMode').addEventListener('change',()=>updateSelectedScannerAlert().catch(e=>alert(e.message))); document.getElementById('catalogSelect').addEventListener('change',updateCatalogPreview); document.getElementById('insertCatalogCodeBtn').addEventListener('click',()=>{const item=selectedCatalogItem(); if(item) replaceAutocompleteFragment(scannerSnippet(item),'Inserted scanner code block');}); document.getElementById('insertCatalogRefBtn').addEventListener('click',()=>{const item=selectedCatalogItem(); if(item) insertScannerCall(item.name);}); document.getElementById('refreshCatalogBtn').addEventListener('click',()=>loadCatalog()); document.getElementById('primitiveSelect').addEventListener('change',updatePrimitivePreview); document.getElementById('insertPrimitiveBtn').addEventListener('click',()=>{const item=selectedPrimitive(); if(item) insertFunctionCall(item.name);}); document.getElementById('openScannerPickerBtn').addEventListener('click',()=>{q.focus(); showAutocomplete(true);}); document.getElementById('copyQueryBtn').addEventListener('click',copyQuery); document.getElementById('validateBtn').addEventListener('click',validateQuery); document.getElementById('runBtn').addEventListener('click',()=>runScanner()); document.getElementById('saveBtn').addEventListener('click',()=>saveScanner(false).catch(e=>alert(e.message))); document.getElementById('updateBtn').addEventListener('click',()=>saveScanner(true).catch(e=>alert(e.message))); document.getElementById('clearBtn').addEventListener('click',clearPage); document.getElementById('resultsHead').addEventListener('click',e=>{const btn=e.target.closest('[data-sort-col]'); if(btn) setResultSort(Number(btn.dataset.sortCol));}); document.getElementById('columnTemplateSelect').addEventListener('change',e=>applyColumnTemplate(e.target.value)); document.getElementById('applyColumnTemplateBtn').addEventListener('click',()=>applyColumnTemplate(document.getElementById('columnTemplateSelect').value)); document.getElementById('saveColumnTemplateBtn').addEventListener('click',()=>saveColumnTemplate().catch(e=>alert(e.message))); document.getElementById('deleteColumnTemplateBtn').addEventListener('click',()=>deleteColumnTemplate().catch(e=>alert(e.message))); document.getElementById('addColumnBtn').addEventListener('click',()=>addColumn(document.getElementById('columnLabelInput').value,document.getElementById('columnExprInput').value,document.getElementById('columnFormatSelect').value)); document.getElementById('addSelectedPrimitiveColumnBtn').addEventListener('click',addSelectedPrimitiveColumn); document.getElementById('realignColumnsBtn').addEventListener('click',()=>refreshCurrentResultLayout('Table realigned using the current column order.', false)); document.getElementById('refreshColumnValuesBtn').addEventListener('click',()=>refreshColumnValues().catch(e=>alert(e.message||e))); document.getElementById('autoRefreshColumnsChk').addEventListener('change',e=>{autoRefreshColumns=!!e.target.checked; localStorage.setItem('scanner_builder_auto_refresh_columns',autoRefreshColumns?'1':'0'); setStatus(autoRefreshColumns?'Auto-refresh values enabled.':'Auto-refresh values disabled.');}); autoRefreshColumns=localStorage.getItem('scanner_builder_auto_refresh_columns')==='1'; document.getElementById('autoRefreshColumnsChk').checked=autoRefreshColumns; updateColumnRefreshUi(); document.getElementById('resetColumnsBtn').addEventListener('click',()=>{selectedColumnTemplateId=''; resultColumns=defaultColumns(); resultSort={index:null,dir:1}; renderColumnTemplates(); renderColumnList(); refreshCurrentResultLayout('Reset to built-in default primitive columns.', true);}); document.getElementById('logic-chips').innerHTML=[['Text-first','g'],['Primitive columns','g'],['Column templates','y'],['Last query cache','y'],['Dashboard reuse','g']].map(([t,c])=>`<span class="chip ${c}">${t}</span>`).join(''); renderColumnList(); updateFunctionHelp();}
init().catch(e=>{console.error(e); setStatus(e.message||'Failed to initialize');});
