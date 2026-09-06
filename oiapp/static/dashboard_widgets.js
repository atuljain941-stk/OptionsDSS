/* ============================================================
   dashboard_widgets.js  —  UI-uplift widget layer for the Dashboard tab
   Progressive enhancement: wraps the EXISTING dashboard panels (which keep
   rendering live data via app.js / Plotly) into a 12-column widget grid with
   drag-reorder, collapse, resize, remove/add, and unlimited saved layouts.
   Layout state persists in localStorage and restores on reload.
   Backend, routes, data and app.js are untouched.
   ============================================================ */
(function () {
  "use strict";
  var LS = "oi-dash-widgets-v1";
  var SPANS = [4, 6, 8, 12];
  var WIDGETS = [
    { id: "oivol",   title: "OI & Volume by Strike",        span: 6 },
    { id: "doi",     title: "\u0394OI Change vs Prior Day",  span: 6 },
    { id: "futures", title: "Futures OI",                    span: 6 },
    { id: "oiintel", title: "OI Intelligence",               span: 12 },
    { id: "price",   title: "90-Day Price \u00b7 EMA20 \u00b7 EMA50", span: 12 }
  ];

  function dash() { return document.getElementById("tab-dashboard"); }
  function resolve(id) {
    var d = dash(); if (!d) return null;
    if (id === "oivol")   { var a = d.querySelector("#options-chart");  return a ? a.closest(".col-half") : null; }
    if (id === "doi")     { var b = d.querySelector("#oiChangeChart");  return b ? b.closest(".col-half") : null; }
    if (id === "futures") { return document.getElementById("dash-futures-card"); }
    if (id === "oiintel") { return document.getElementById("oi-intel-card"); }
    if (id === "price")   { var c = d.querySelector("#history-chart");  return c ? c.closest(".card") : null; }
    return null;
  }

  function loadState() {
    try { return JSON.parse(localStorage.getItem(LS)) || {}; } catch (e) { return {}; }
  }
  function saveState(extra) {
    var st = current();
    if (extra) { for (var k in extra) st[k] = extra[k]; }
    try { localStorage.setItem(LS, JSON.stringify(st)); } catch (e) {}
  }
  function current() {
    var prev = loadState();
    var grid = document.getElementById("dw-grid");
    var order = [], state = {};
    if (grid) {
      [].forEach.call(grid.children, function (w) {
        var id = w.getAttribute("data-dw"); if (!id) return;
        order.push(id);
        state[id] = { span: parseInt(w.getAttribute("data-span"), 10) || 6,
                      collapsed: w.getAttribute("data-collapsed") === "1",
                      hidden: w.style.display === "none" };
      });
    }
    return { order: order, state: state, layouts: prev.layouts || [], active: prev.active || null };
  }

  function plotResize() {
    if (!window.Plotly) return;
    ["options-chart", "oiChangeChart", "history-chart"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { try { window.Plotly.Plots.resize(el); } catch (e) {} }
    });
  }

  function css() {
    if (document.getElementById("dw-style")) return;
    var s = document.createElement("style"); s.id = "dw-style";
    s.textContent =
      "#dw-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px;position:relative}" +
      "#dw-grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;align-items:start}" +
      ".dw-widget{grid-column:span 6;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;min-width:0}" +
      ".dw-head{display:flex;align-items:center;gap:8px;padding:7px 11px;border-bottom:1px solid var(--border);background:var(--surface);cursor:grab}" +
      ".dw-head.drag{opacity:.5}" +
      ".dw-title{font-size:12px;font-weight:700;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".dw-grip{color:var(--muted);font-size:12px}" +
      ".dw-ctrls{margin-left:auto;display:flex;gap:12px;align-items:center;color:var(--muted);font-size:14px}" +
      ".dw-ctrls span{cursor:pointer;line-height:1}" +
      ".dw-ctrls span:hover{color:var(--text)}" +
      ".dw-body{min-width:0}" +
      ".dw-widget .card,.dw-widget .col-half{margin:0!important;border:0!important;border-radius:0!important;background:transparent!important}" +
      ".dw-widget .card-title{display:none!important}" +
      ".dw-btn{background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}" +
      ".dw-btn:hover{border-color:var(--accent)}" +
      ".dw-pop{position:absolute;top:40px;z-index:60;background:var(--card);border:1px solid var(--border);border-radius:10px;box-shadow:0 16px 40px rgba(0,0,0,.5);padding:8px;min-width:230px;display:none}" +
      ".dw-pop.open{display:block}" +
      ".dw-pop .item{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:7px;cursor:pointer;font-size:13px;color:var(--text)}" +
      ".dw-pop .item:hover{background:var(--surface)}" +
      ".dw-pop .muted{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:4px 6px 6px}" +
      ".dw-chip{display:inline-flex;align-items:center;gap:7px;padding:7px 11px;border-radius:9px;background:var(--surface);border:1px solid var(--border);font-size:12.5px}" +
      ".dw-chip .nm{cursor:pointer;font-weight:600;color:var(--text)}" +
      ".dw-chip .del{cursor:pointer;color:var(--muted)} .dw-chip .del:hover{color:var(--red)}" +
      ".dw-input{background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:12.5px;padding:7px 10px;outline:none}";
    document.head.appendChild(s);
  }

  function build() {
    var d = dash();
    if (!d || d.getAttribute("data-dw-init") === "1") return true;
    // all panels present?
    for (var i = 0; i < WIDGETS.length; i++) { if (!resolve(WIDGETS[i].id)) return false; }
    d.setAttribute("data-dw-init", "1");
    css();

    var st = loadState();
    var order = (st.order && st.order.length) ? st.order.slice() : WIDGETS.map(function (w) { return w.id; });
    // include any new widgets not in saved order
    WIDGETS.forEach(function (w) { if (order.indexOf(w.id) < 0) order.push(w.id); });
    var pstate = st.state || {};

    // control bar
    var bar = document.createElement("div"); bar.id = "dw-bar";
    bar.innerHTML =
      '<div style="position:relative">' +
        '<button class="dw-btn" id="dw-add">\uFF0B Add Widget <span style="color:var(--muted);font-size:10px">\u25be</span></button>' +
        '<div class="dw-pop" id="dw-add-pop"></div>' +
      '</div>' +
      '<div style="position:relative">' +
        '<button class="dw-btn" id="dw-lay">\u25a6 Layouts <span style="color:var(--muted);font-size:10px">\u25be</span></button>' +
        '<div class="dw-pop" id="dw-lay-pop"></div>' +
      '</div>' +
      '<span style="font-size:11px;color:var(--muted);margin-left:4px">Drag \u2630 to reorder \u00b7 layout auto-saves &amp; restores on reload</span>';

    var grid = document.createElement("div"); grid.id = "dw-grid";

    // insert after the ctrl-bar (keep symbol/expiry controls on top)
    var ctrl = d.querySelector(".ctrl-bar");
    if (ctrl && ctrl.nextSibling) { d.insertBefore(bar, ctrl.nextSibling); }
    else if (ctrl) { d.appendChild(bar); }
    else { d.insertBefore(bar, d.firstChild); }
    d.insertBefore(grid, bar.nextSibling);

    // wrap each panel in declared order
    order.forEach(function (id) {
      var meta = WIDGETS.filter(function (w) { return w.id === id; })[0]; if (!meta) return;
      var el = resolve(id); if (!el) return;
      var w = document.createElement("div"); w.className = "dw-widget"; w.setAttribute("data-dw", id);
      var span = (pstate[id] && pstate[id].span) || meta.span; w.setAttribute("data-span", span);
      w.style.gridColumn = "span " + span;
      var head = document.createElement("div"); head.className = "dw-head"; head.setAttribute("draggable", "true");
      head.innerHTML = '<span class="dw-grip">\u2630</span><span class="dw-title">' + meta.title + '</span>' +
        '<span class="dw-ctrls"><span data-act="collapse" title="Collapse">\u25be</span>' +
        '<span data-act="resize" title="Resize">\u2922</span>' +
        '<span data-act="remove" title="Remove">\u00d7</span></span>';
      var body = document.createElement("div"); body.className = "dw-body";
      el.parentNode.removeChild(el); body.appendChild(el);
      w.appendChild(head); w.appendChild(body); grid.appendChild(w);

      if (pstate[id] && pstate[id].collapsed) { w.setAttribute("data-collapsed", "1"); body.style.display = "none"; head.querySelector('[data-act=collapse]').textContent = "\u25b8"; }
      if (pstate[id] && pstate[id].hidden) { w.style.display = "none"; }

      head.addEventListener("click", function (ev) {
        var act = ev.target.getAttribute("data-act"); if (!act) return;
        if (act === "collapse") {
          var col = w.getAttribute("data-collapsed") === "1";
          w.setAttribute("data-collapsed", col ? "0" : "1");
          body.style.display = col ? "" : "none";
          ev.target.textContent = col ? "\u25be" : "\u25b8";
        } else if (act === "resize") {
          var cur = parseInt(w.getAttribute("data-span"), 10) || 6;
          var nx = SPANS[(SPANS.indexOf(cur) + 1) % SPANS.length];
          w.setAttribute("data-span", nx); w.style.gridColumn = "span " + nx;
        } else if (act === "remove") {
          w.style.display = "none";
        }
        saveState({ active: null }); plotResize(); renderAddPop();
      });

      // drag reorder
      head.addEventListener("dragstart", function (e) { w.__drag = true; head.classList.add("drag"); try { e.dataTransfer.setData("text/plain", id); } catch (x) {} });
      head.addEventListener("dragend", function () { w.__drag = false; head.classList.remove("drag"); saveState({ active: null }); });
      w.addEventListener("dragover", function (e) { e.preventDefault(); });
      w.addEventListener("drop", function (e) {
        e.preventDefault();
        var dragging = grid.querySelector(".dw-head.drag"); if (!dragging) return;
        var dw = dragging.parentNode; if (dw === w) return;
        var kids = [].slice.call(grid.children);
        if (kids.indexOf(dw) < kids.indexOf(w)) { grid.insertBefore(dw, w.nextSibling); }
        else { grid.insertBefore(dw, w); }
        saveState({ active: null }); plotResize();
      });
    });

    // apply saved order strictly (in case resolve order differed)
    order.forEach(function (id) { var w = grid.querySelector('[data-dw="' + id + '"]'); if (w) grid.appendChild(w); });

    wireBar();
    renderAddPop(); renderLayPop();
    setTimeout(plotResize, 200);
    return true;
  }

  function wireBar() {
    var add = document.getElementById("dw-add"), addPop = document.getElementById("dw-add-pop");
    var lay = document.getElementById("dw-lay"), layPop = document.getElementById("dw-lay-pop");
    add.addEventListener("click", function (e) { e.stopPropagation(); layPop.classList.remove("open"); addPop.classList.toggle("open"); });
    lay.addEventListener("click", function (e) { e.stopPropagation(); addPop.classList.remove("open"); layPop.classList.toggle("open"); });
    document.addEventListener("click", function () { addPop.classList.remove("open"); layPop.classList.remove("open"); });
    addPop.addEventListener("click", function (e) { e.stopPropagation(); });
    layPop.addEventListener("click", function (e) { e.stopPropagation(); });
  }

  function renderAddPop() {
    var pop = document.getElementById("dw-add-pop"); if (!pop) return;
    var grid = document.getElementById("dw-grid");
    var hidden = WIDGETS.filter(function (m) { var w = grid.querySelector('[data-dw="' + m.id + '"]'); return w && w.style.display === "none"; });
    var html = '<div class="muted">Add a widget</div>';
    if (!hidden.length) { html += '<div style="padding:10px;color:var(--muted);font-size:12px">All widgets shown \uD83C\uDF89</div>'; }
    hidden.forEach(function (m) { html += '<div class="item" data-add="' + m.id + '">' + m.title + '<span style="margin-left:auto;color:var(--accent)">\uFF0B</span></div>'; });
    pop.innerHTML = html;
    [].forEach.call(pop.querySelectorAll("[data-add]"), function (it) {
      it.addEventListener("click", function () {
        var w = grid.querySelector('[data-dw="' + it.getAttribute("data-add") + '"]');
        if (w) { w.style.display = ""; }
        pop.classList.remove("open"); saveState({ active: null }); renderAddPop(); plotResize();
      });
    });
  }

  function renderLayPop() {
    var pop = document.getElementById("dw-lay-pop"); if (!pop) return;
    var st = loadState(); var layouts = st.layouts || [];
    var html = '<div class="muted">Dashboard layouts</div>' +
      '<div style="display:flex;gap:6px;margin:2px 4px 10px"><input class="dw-input" id="dw-lay-name" placeholder="Layout name\u2026" style="flex:1"/><button class="dw-btn" id="dw-lay-save" style="padding:7px 10px">\uD83D\uDCBE</button></div>';
    if (!layouts.length) { html += '<div style="padding:2px 6px 6px;color:var(--muted);font-size:12px">No saved layouts yet</div>'; }
    html += '<div style="display:flex;flex-direction:column;gap:6px">';
    layouts.forEach(function (l) {
      var active = st.active === l.id;
      html += '<div class="dw-chip" style="' + (active ? "border-color:var(--accent)" : "") + '"><span class="nm" data-load="' + l.id + '">' + (active ? "\u25cf " : "") + l.name + '</span><span class="del" data-del="' + l.id + '">\u00d7</span></div>';
    });
    html += '</div>';
    pop.innerHTML = html;

    pop.querySelector("#dw-lay-save").addEventListener("click", function () {
      var nm = (pop.querySelector("#dw-lay-name").value || "").trim() || ("Layout " + (layouts.length + 1));
      var snap = current(); var id = "ly" + Date.now();
      var next = (loadState().layouts || []).slice();
      next.push({ id: id, name: nm, order: snap.order, state: snap.state });
      saveState({ layouts: next, active: id });
      renderLayPop();
    });
    [].forEach.call(pop.querySelectorAll("[data-load]"), function (n) {
      n.addEventListener("click", function () { applyLayout(n.getAttribute("data-load")); });
    });
    [].forEach.call(pop.querySelectorAll("[data-del]"), function (n) {
      n.addEventListener("click", function () {
        var id = n.getAttribute("data-del");
        var next = (loadState().layouts || []).filter(function (l) { return l.id !== id; });
        var st2 = loadState();
        saveState({ layouts: next, active: st2.active === id ? null : st2.active });
        renderLayPop();
      });
    });
  }

  function applyLayout(id) {
    var st = loadState(); var l = (st.layouts || []).filter(function (x) { return x.id === id; })[0]; if (!l) return;
    var grid = document.getElementById("dw-grid");
    // order
    l.order.forEach(function (wid) { var w = grid.querySelector('[data-dw="' + wid + '"]'); if (w) grid.appendChild(w); });
    // per-widget state
    Object.keys(l.state).forEach(function (wid) {
      var w = grid.querySelector('[data-dw="' + wid + '"]'); if (!w) return;
      var s = l.state[wid];
      w.setAttribute("data-span", s.span); w.style.gridColumn = "span " + s.span;
      w.style.display = s.hidden ? "none" : "";
      w.setAttribute("data-collapsed", s.collapsed ? "1" : "0");
      var body = w.querySelector(".dw-body"); var cb = w.querySelector('[data-act=collapse]');
      if (body) body.style.display = s.collapsed ? "none" : "";
      if (cb) cb.textContent = s.collapsed ? "\u25b8" : "\u25be";
    });
    saveState({ active: id });
    renderAddPop(); renderLayPop(); plotResize();
  }

  function boot() {
    if (build()) return;
    var tries = 0;
    var iv = setInterval(function () { tries++; if (build() || tries > 40) clearInterval(iv); }, 250);
  }
  if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", boot); }
  else { boot(); }
  window.addEventListener("load", boot);
})();
