/* Saved OI day-to-day change bubble matrix.  Loads only on Refresh. */
(function () {
  "use strict";
  function byId(id) { return document.getElementById(id); }
  function n(v) { return Number(v || 0); }
  function selectedSide() { var el=document.querySelector('input[name="oi-trend-side"]:checked'); return el ? el.value : "both"; }
  function comma(v) { return n(v).toLocaleString(); }
  var cached=null;

  function render(data) {
    var chart=byId("oiBuildupTrendChart"), table=byId("oiBuildupTrendTable");
    if (!chart || !window.Plotly) return;
    var side=selectedSide(), points=[], maxAbs=1, dates=data.dates || [];
    ["call","put"].forEach(function(kind) {
      if (side!=="both" && side!==kind) return;
      (data[kind+"_matrix"] || []).forEach(function(row) {
        var values=row.values || [];
        for (var i=1; i<Math.min(values.length,dates.length); i++) {
          var change=n(values[i])-n(values[i-1]);
          maxAbs=Math.max(maxAbs,Math.abs(change));
          points.push({side:kind,strike:n(row.strike),from:dates[i-1],date:dates[i],prior:n(values[i-1]),current:n(values[i]),change:change});
        }
      });
    });
    if (!points.length) { chart.innerHTML='<div style="height:300px;display:grid;place-items:center;color:var(--muted)">No consecutive saved OI snapshots are available.</div>'; return; }
    var calls=points.filter(function(p){return p.side==="call";}), puts=points.filter(function(p){return p.side==="put";});
    function trace(rows, kind) {
      return {
        type:"scatter", mode:"markers", name:kind==="call"?"Calls":"Puts",
        x:rows.map(function(p){return p.date;}),
        y:rows.map(function(p){return kind==="call"?p.strike:-p.strike;}),
        customdata:rows.map(function(p){return [p.from,p.date,p.side,p.strike,p.change,p.prior,p.current];}),
        marker:{color:rows.map(function(p){return p.change>=0?"#3b82f6":"#ef4444";}),size:rows.map(function(p){return Math.max(7,Math.sqrt(Math.abs(p.change)/maxAbs)*32);}),symbol:kind==="call"?"circle":"diamond",opacity:.9,line:{color:"#dbeafe",width:.3}},
        hovertemplate:"%{customdata[2]} · Strike %{customdata[3]}<br>%{customdata[0]} → %{customdata[1]}<br>ΔOI: %{customdata[4]:,.0f}<br>Prior OI: %{customdata[5]:,.0f}<br>Current OI: %{customdata[6]:,.0f}<extra></extra>"
      };
    }
    var strikes=Array.from(new Set(points.map(function(p){return p.strike;}))).sort(function(a,b){return a-b;});
    var shown=strikes.length>14?strikes.filter(function(_,i){return i%Math.ceil(strikes.length/14)===0;}):strikes;
    var tickvals=shown.concat(shown.map(function(v){return -v;}));
    var ticktext=shown.map(function(v){return "Call "+v;}).concat(shown.map(function(v){return "Put "+v;}));
    Plotly.react(chart,[trace(calls,"call"),trace(puts,"put")],{
      title:(data.symbol||"")+" "+(data.expiration||"")+" — Saved day-to-day signed OI change",
      paper_bgcolor:"#0b1018",plot_bgcolor:"#0b1018",font:{color:"#d1d5db"},
      margin:{l:85,r:25,t:48,b:54},
      xaxis:{title:"Snapshot date",type:"category",gridcolor:"#263243"},
      yaxis:{title:"Strike — calls above centre / puts below",tickvals:tickvals,ticktext:ticktext,gridcolor:"#263243",zeroline:true,zerolinecolor:"#aab6c6",zerolinewidth:2},
      legend:{orientation:"h",y:-.2},hovermode:"closest"
    },{responsive:true,displaylogo:false});
    if (table) {
      var byStrike={};
      points.forEach(function(p){
        var key=p.side+"|"+p.strike, item=byStrike[key];
        if(!item) item=byStrike[key]={side:p.side,strike:p.strike,from:p.from,date:p.date,prior:p.prior,current:p.current,change:0};
        item.change+=p.change; item.current=p.current; item.date=p.date;
      });
      var rows=Object.keys(byStrike).map(function(key){return byStrike[key];}).sort(function(a,b){
        return a.side===b.side ? a.strike-b.strike : (a.side==="call" ? -1 : 1);
      });
      table.innerHTML='<table style="width:100%;font-size:11px;border-collapse:collapse"><thead><tr><th>Window</th><th>Side</th><th>Strike</th><th>Net ΔOI</th><th>Start OI</th><th>End OI</th></tr></thead><tbody>'+
        rows.map(function(p){var c=p.change>=0?"#3b82f6":"#ef4444";return '<tr><td>'+p.from+' → '+p.date+'</td><td>'+p.side+'</td><td>'+p.strike+'</td><td style="color:'+c+'">'+(p.change>=0?"+":"")+comma(p.change)+'</td><td>'+comma(p.prior)+'</td><td>'+comma(p.current)+'</td></tr>';}).join("")+'</tbody></table>';
    }
  }
  function load() {
    var chart=byId("oiBuildupTrendChart"),symbol=(byId("symbol-input")||{}).value||"",expiration=(byId("expiration-select")||{}).value||"",days=Math.max(2,Math.min(30,n((byId("oi-trend-days")||{}).value)||5));
    if (!symbol || !expiration || expiration.toLowerCase()==="all") { chart.innerHTML='<div style="height:220px;display:grid;place-items:center;color:var(--muted)">Choose one symbol and a specific expiry, then Refresh.</div>'; return; }
    chart.innerHTML='<div style="height:300px;display:grid;place-items:center;color:var(--muted)">Loading saved OI changes…</div>';
    fetch("/api/oi_buildup_trend?symbol="+encodeURIComponent(symbol)+"&expiration="+encodeURIComponent(expiration)+"&days="+days,{credentials:"same-origin",cache:"no-store"})
      .then(function(r){return r.json().then(function(d){if(!r.ok||d.ok===false)throw new Error(d.error||"Unable to load OI history");return d;});})
      .then(function(data){cached=data;render(data);})
      .catch(function(e){chart.innerHTML='<div style="height:220px;display:grid;place-items:center;color:#f87171">'+e.message+'</div>';});
  }
  function boot() {
    var old=byId("oi-trend-refresh-btn"); if (!old) return;
    var button=old.cloneNode(true); old.parentNode.replaceChild(button,old);
    button.addEventListener("click",load);
    document.querySelectorAll('input[name="oi-trend-side"]').forEach(function(node){node.addEventListener("change",function(){if(cached)render(cached);});});
    // The legacy OI viewer also paints this container on load.  Render last so this
    // day-to-day bubble matrix is the sole owner of the panel and raw-data table.
    window.setTimeout(load, 0);
  }
  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",boot);else boot();
})();