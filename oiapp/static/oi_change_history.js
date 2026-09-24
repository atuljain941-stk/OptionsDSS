/* On-demand saved OI change history for the OI Viewer.
   No polling, no chain fetches, no background jobs. */
(function () {
  "use strict";
  var CARD_ID = "oi-history-bubbles-card";

  function el(id) { return document.getElementById(id); }
  function val(id) { var n = el(id); return n ? String(n.value || "").trim() : ""; }
  function esc(s) { var d = document.createElement("div"); d.textContent = String(s); return d.innerHTML; }
  function fmt(n) { return Number(n || 0).toLocaleString(); }

  function injectStyle() {
    if (el("oi-history-bubbles-style")) return;
    var s = document.createElement("style"); s.id = "oi-history-bubbles-style";
    s.textContent =
      "#"+CARD_ID+"{margin-top:14px}#"+CARD_ID+" .oh-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap}" +
      "#"+CARD_ID+" .oh-controls{margin-left:auto;display:flex;gap:9px;align-items:center;flex-wrap:wrap}" +
      "#"+CARD_ID+" input,#"+CARD_ID+" select{background:var(--surface,#101827);border:1px solid var(--border,#334155);border-radius:6px;color:var(--text,#e5e7eb);padding:5px 7px}" +
      "#"+CARD_ID+" button{background:var(--accent,#2563eb);color:#fff;border:0;border-radius:6px;padding:6px 10px;font-weight:700;cursor:pointer}" +
      "#"+CARD_ID+" .oh-legend{font-size:12px;color:var(--muted,#94a3b8);margin:8px 0}#"+CARD_ID+" .oh-legend b{color:#6ea8fe}#"+CARD_ID+" .oh-legend i{color:#ff6b6b;font-style:normal}" +
      "#"+CARD_ID+" .oh-chart{overflow-x:auto;min-height:250px}#"+CARD_ID+" .oh-empty{padding:28px;color:var(--muted,#94a3b8);text-align:center}" +
      "#"+CARD_ID+" details{margin-top:10px}#"+CARD_ID+" summary{cursor:pointer;color:var(--muted,#94a3b8);font-size:12px}" +
      "#"+CARD_ID+" table{width:100%;border-collapse:collapse;margin-top:7px;font-size:12px}#"+CARD_ID+" th,#"+CARD_ID+" td{padding:5px 7px;border-bottom:1px solid var(--border,#334155);text-align:right}#"+CARD_ID+" th:first-child,#"+CARD_ID+" td:first-child{text-align:left}";
    document.head.appendChild(s);
  }

  function addCard() {
    if (el(CARD_ID)) return el(CARD_ID);
    var anchor = el("oiChangeChart");
    if (!anchor) return null;
    var host = anchor.closest(".col-half") || anchor.parentElement;
    var card = document.createElement("section"); card.id = CARD_ID; card.className = "card";
    card.innerHTML =
      '<div class="card-title oh-head"><span>OI change by strike · saved history</span>' +
      '<div class="oh-controls"><label>Days <input id="oh-days" type="number" min="2" max="10" value="5" style="width:45px"></label>' +
      '<select id="oh-side"><option value="both">Calls + puts</option><option value="c">Calls only</option><option value="p">Puts only</option></select>' +
      '<button id="oh-refresh" type="button">Refresh</button></div></div>' +
      '<div class="oh-legend">Each row is one comparison date. <b>Blue = OI added</b> · <i>Red = OI reduced</i> · calls are above the centre line; puts below. Hover a dot for the exact OI change.</div>' +
      '<div class="oh-chart" id="oh-chart"><div class="oh-empty">Choose Refresh to load saved OI history.</div></div>' +
      '<details><summary>Show exact OI-change values</summary><div id="oh-table"></div></details>';
    host.parentNode.insertBefore(card, host.nextSibling);
    el("oh-refresh").addEventListener("click", load);
    return card;
  }

  function render(data, side) {
    var chart = el("oh-chart"), table = el("oh-table");
    var points = (data.points || []).filter(function (p) { return side === "both" || p.side === side; });
    if (!points.length) { chart.innerHTML = '<div class="oh-empty">No non-zero signed OI changes were found in the saved snapshots.</div>'; table.innerHTML = ""; return; }
    var strikes = Array.from(new Set(points.map(function(p){return Number(p.strike);}))).sort(function(a,b){return a-b;});
    var dates = data.dates.slice(1);
    var maxAbs = Math.max.apply(null, points.map(function(p){return Math.abs(Number(p.change));})) || 1;
    var width = Math.max(760, strikes.length * 52 + 100), rowH = 116, height = dates.length * rowH + 58;
    function x(strike) { var i = strikes.indexOf(Number(strike)); return 55 + i * ((width - 95) / Math.max(1, strikes.length - 1)); }
    function y(date, isCall) { var row = dates.indexOf(date); var mid = 34 + row * rowH + rowH / 2; return mid + (isCall ? -27 : 27); }
    var svg = '<svg viewBox="0 0 '+width+' '+height+'" width="'+width+'" height="'+height+'" role="img" aria-label="Signed OI change by strike and date">';
    dates.forEach(function(date, i) {
      var mid = 34 + i * rowH + rowH / 2;
      svg += '<text x="5" y="'+(mid+4)+'" fill="#aab6c6" font-size="11">'+esc(date)+'</text>' +
             '<line x1="48" y1="'+mid+'" x2="'+(width-18)+'" y2="'+mid+'" stroke="#60708a" stroke-width="1"/>' +
             '<text x="'+(width-15)+'" y="'+(mid-18)+'" fill="#7e9ac0" font-size="10" text-anchor="end">CALL</text>' +
             '<text x="'+(width-15)+'" y="'+(mid+36)+'" fill="#d18484" font-size="10" text-anchor="end">PUT</text>';
    });
    strikes.forEach(function(strike, i) {
      var xx = x(strike);
      svg += '<line x1="'+xx+'" y1="24" x2="'+xx+'" y2="'+(height-28)+'" stroke="#253348" stroke-width="1"/>' +
             '<text x="'+xx+'" y="'+(height-8)+'" fill="#aab6c6" font-size="10" text-anchor="middle">'+strike+'</text>';
    });
    points.forEach(function(p) {
      var positive = Number(p.change) > 0, r = 4 + 17 * Math.sqrt(Math.abs(Number(p.change)) / maxAbs);
      var fill = positive ? "#3b82f6" : "#ef4444";
      var label = (p.side === "c" ? "Call" : "Put") + " · strike " + p.strike + " · " + p.from_date + " → " + p.date + " · ΔOI " + (positive ? "+" : "") + fmt(p.change) + " · current OI " + fmt(p.oi);
      svg += '<circle cx="'+x(p.strike)+'" cy="'+y(p.date,p.side==="c")+'" r="'+r.toFixed(1)+'" fill="'+fill+'" fill-opacity=".84" stroke="#dbeafe" stroke-opacity=".35"><title>'+esc(label)+'</title></circle>';
    });
    svg += '</svg>'; chart.innerHTML = svg;
    var rows = points.slice().sort(function(a,b){return String(b.date).localeCompare(String(a.date)) || Math.abs(b.change)-Math.abs(a.change);});
    table.innerHTML = '<table><thead><tr><th>Date</th><th>Side</th><th>Strike</th><th>ΔOI</th><th>Current OI</th></tr></thead><tbody>' +
      rows.map(function(p){var sign=p.change>0?"+":""; return '<tr><td>'+esc(p.from_date)+' → '+esc(p.date)+'</td><td>'+(p.side==="c"?"Call":"Put")+'</td><td>'+p.strike+'</td><td style="color:'+(p.change>0?"#60a5fa":"#f87171")+'">'+sign+fmt(p.change)+'</td><td>'+fmt(p.oi)+'</td></tr>';}).join("") + '</tbody></table>';
  }

  function load() {
    var symbol = val("symbol-input").toUpperCase(), expiration = val("expiration-select"), days = Math.max(2, Math.min(10, Number(val("oh-days")) || 5)), side = val("oh-side");
    var chart = el("oh-chart"); if (!symbol) { chart.innerHTML='<div class="oh-empty">Select a symbol first.</div>'; return; }
    chart.innerHTML='<div class="oh-empty">Loading saved snapshots…</div>';
    fetch("/api/oi_change_history?symbol="+encodeURIComponent(symbol)+"&expiration="+encodeURIComponent(expiration)+"&days="+days,{credentials:"same-origin"})
      .then(function(r){return r.json().then(function(d){if(!r.ok) throw new Error(d.error||"Unable to load saved OI history");return d;});})
      .then(function(data){render(data,side);})
      .catch(function(err){chart.innerHTML='<div class="oh-empty">'+esc(err.message)+'</div>';});
  }

  function boot() { injectStyle(); addCard(); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
  window.addEventListener("load", boot, {once:true});
})();