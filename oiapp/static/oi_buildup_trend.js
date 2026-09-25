/* Saved OI Buildup Trend panel. Runs only when its Refresh button is clicked. */
(function () {
  "use strict";
  function byId(id) { return document.getElementById(id); }
  function esc(v) { return String(v == null ? "" : v).replace(/[&<>"']/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
  function n(v) { return Number(v || 0); }
  function num(v) { return n(v).toLocaleString(); }

  function selectedSide() {
    var x = document.querySelector('input[name="oi-trend-side"]:checked');
    return x ? x.value : "both";
  }

  function changes(data, side) {
    var out = [], dates = data.dates || [];
    ["call", "put"].forEach(function(kind) {
      if (side !== "both" && side !== kind) return;
      (data[kind + "_matrix"] || []).forEach(function(row) {
        var values = row.values || [];
        for (var i = 1; i < Math.min(dates.length, values.length); i++) {
          var delta = n(values[i]) - n(values[i - 1]);
          if (delta) out.push({date:dates[i], from:dates[i-1], side:kind, strike:n(row.strike), change:delta, oi:n(values[i])});
        }
      });
    });
    return out;
  }

  function render(data) {
    var chart = byId("oiBuildupTrendChart"), table = byId("oiBuildupTrendTable");
    if (!chart || !table) return;
    var side = selectedSide(), pts = changes(data, side), dates = (data.dates || []).slice(1);
    if (!pts.length) {
      chart.style.height = "auto";
      chart.innerHTML = '<div style="height:260px;display:grid;place-items:center;color:var(--muted)">No non-zero OI changes in the selected saved snapshots.</div>';
      table.innerHTML = "";
      return;
    }
    var strikes = Array.from(new Set(pts.map(function(p){return p.strike;}))).sort(function(a,b){return a-b;});
    var maxAbs = Math.max.apply(null, pts.map(function(p){return Math.abs(p.change);})); 
    var width = Math.max(840, strikes.length * 62 + 120), rowH = 112, height = Math.max(300, dates.length * rowH + 60);
    function x(strike) { return 64 + strikes.indexOf(strike) * ((width - 105) / Math.max(1, strikes.length - 1)); }
    function mid(date) { return 28 + dates.indexOf(date) * rowH + rowH / 2; }
    var svg = '<svg viewBox="0 0 '+width+' '+height+'" width="'+width+'" height="'+height+'" role="img" aria-label="Saved signed OI change by strike and date">';
    dates.forEach(function(date) {
      var m=mid(date);
      svg += '<text x="6" y="'+(m+4)+'" fill="#aab6c6" font-size="11">'+esc(date)+'</text>'+
        '<line x1="58" y1="'+m+'" x2="'+(width-18)+'" y2="'+m+'" stroke="#64748b" stroke-width="1"/>'+
        '<text x="'+(width-20)+'" y="'+(m-19)+'" fill="#6ea8fe" font-size="9" text-anchor="end">CALLS</text>'+
        '<text x="'+(width-20)+'" y="'+(m+34)+'" fill="#d78a8a" font-size="9" text-anchor="end">PUTS</text>';
    });
    strikes.forEach(function(strike) {
      var xx=x(strike);
      svg += '<line x1="'+xx+'" y1="18" x2="'+xx+'" y2="'+(height-28)+'" stroke="#243247" stroke-width="1"/>'+
        '<text x="'+xx+'" y="'+(height-8)+'" fill="#aab6c6" font-size="10" text-anchor="middle">'+strike+'</text>';
    });
    pts.forEach(function(p) {
      var radius=4+17*Math.sqrt(Math.abs(p.change)/maxAbs), yy=mid(p.date)+(p.side==="call"?-27:27);
      var color=p.change>0 ? "#3b82f6" : "#ef4444";
      var label=(p.side==="call"?"Call":"Put")+" "+p.strike+" · "+p.from+" → "+p.date+" · ΔOI "+(p.change>0?"+":"")+num(p.change)+" · OI "+num(p.oi);
      svg += '<circle cx="'+x(p.strike)+'" cy="'+yy+'" r="'+radius.toFixed(1)+'" fill="'+color+'" fill-opacity=".88" stroke="#dbeafe" stroke-opacity=".35"><title>'+esc(label)+'</title></circle>';
    });
    svg += '</svg>';
    chart.style.height = "auto";
    chart.innerHTML = '<div style="font-size:11px;color:var(--muted);margin:2px 0 7px">X = strike · each row = consecutive snapshot comparison · <span style="color:#60a5fa">blue: OI added</span> · <span style="color:#f87171">red: OI reduced</span> · calls above / puts below centre line</div><div style="overflow-x:auto">'+svg+'</div>';
    var rows=pts.slice().sort(function(a,b){return b.date.localeCompare(a.date)||Math.abs(b.change)-Math.abs(a.change);});
    table.innerHTML='<table style="width:100%;border-collapse:collapse;font-size:11px"><thead><tr><th style="text-align:left">Date</th><th>Side</th><th>Strike</th><th>ΔOI</th><th>Current OI</th></tr></thead><tbody>'+
      rows.map(function(p){return '<tr><td style="padding:5px;border-top:1px solid var(--border)">'+esc(p.from)+' → '+esc(p.date)+'</td><td style="text-align:center">'+(p.side==="call"?"Call":"Put")+'</td><td style="text-align:right">'+p.strike+'</td><td style="text-align:right;color:'+(p.change>0?"#60a5fa":"#f87171")+'">'+(p.change>0?"+":"")+num(p.change)+'</td><td style="text-align:right">'+num(p.oi)+'</td></tr>';}).join("")+'</tbody></table>';
  }

  function load() {
    var chart=byId("oiBuildupTrendChart"), symbol=(byId("symbol-input")||{}).value||"", expiration=(byId("expiration-select")||{}).value||"";
    var days=Math.max(2,Math.min(30,n((byId("oi-trend-days")||{}).value)||10));
    if (!symbol || !expiration || expiration.toLowerCase()==="all") {
      chart.innerHTML='<div style="height:220px;display:grid;place-items:center;color:var(--muted)">Choose one symbol and a specific expiry, then Refresh.</div>'; return;
    }
    chart.style.height="340px"; chart.innerHTML='<div style="height:100%;display:grid;place-items:center;color:var(--muted)">Loading saved OI snapshots…</div>';
    fetch("/api/oi_buildup_trend?symbol="+encodeURIComponent(symbol)+"&expiration="+encodeURIComponent(expiration)+"&days="+days,{credentials:"same-origin"})
      .then(function(r){return r.json().then(function(d){if(!r.ok||d.ok===false)throw new Error(d.error||"Unable to load OI history");return d;});})
      .then(render).catch(function(e){chart.style.height="auto";chart.innerHTML='<div style="height:220px;display:grid;place-items:center;color:#f87171">'+esc(e.message)+'</div>';});
  }

  function boot() {
    var button=byId("oi-trend-refresh-btn"); if (!button || button.dataset.oiTrendBound) return;
    button.dataset.oiTrendBound="1"; button.addEventListener("click",load);
    document.querySelectorAll('input[name="oi-trend-side"]').forEach(function(node){node.addEventListener("change",function(){ if (byId("oiBuildupTrendTable").innerHTML) load(); });});
  }
  if (document.readyState==="loading") document.addEventListener("DOMContentLoaded",boot); else boot();
})();