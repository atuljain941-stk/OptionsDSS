/* ============================================================
   page_widgets.js — free-form "Home" dashboard canvas
   Each PAGE becomes a widget: a live iframe of that view (embed mode),
   freely DRAGGABLE and RESIZABLE, add/remove from a catalog built from
   the app's own nav. Named layouts (dropdown) + localStorage persistence.
   Embed mode (?embed=<tab>) hides ALL chrome + inner widget toolbars.
   No backend changes; every widget is the real page with real data.
   ============================================================ */
(function () {
  "use strict";
  var LS = "oi-home-widgets-v2";

  // ---------- EMBED MODE (runs inside each widget's iframe) ----------
  var params = new URLSearchParams(location.search);
  var embedTab = params.get("embed");
  if (embedTab) {
    document.documentElement.classList.add("wz-embed-html");
    function activateEmbed() {
      document.body.classList.add("wz-embed");
      var btn = document.querySelector('[data-tab="' + embedTab + '"]');
      if (btn) { try { btn.click(); } catch (e) {} }
      [].forEach.call(document.querySelectorAll("#content .section"), function (s) {
        s.classList.toggle("active", s.id === "tab-" + embedTab);
      });
    }
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", activateEmbed);
    else activateEmbed();
    window.addEventListener("load", function () { setTimeout(activateEmbed, 300); });
    return;
  }

  // ---------- HOME CANVAS (top-level app) ----------
  function catalog() {
    var seen = {}, out = [];
    [].forEach.call(document.querySelectorAll('#nav-wrap [data-tab]'), function (b) {
      var t = b.getAttribute("data-tab"); if (!t || t === "home" || seen[t]) return; seen[t] = 1;
      out.push({ tab: t, label: (b.textContent || t).replace(/^[^A-Za-z0-9]+/, "").trim() });
    });
    return out;
  }
  function load() {
    try { var s = JSON.parse(localStorage.getItem(LS)); if (s && s.widgets) return s; } catch (e) {}
    return { widgets: [], layouts: [], active: null };
  }
  function save() { try { localStorage.setItem(LS, JSON.stringify(state)); } catch (e) {} }

  var state, canvas, plane, catalogList, bar;

  function css() {
    if (document.getElementById("pw-style")) return;
    var s = document.createElement("style"); s.id = "pw-style";
    s.textContent =
      "#tab-home{padding-top:6px}" +
      "#tab-home .pw-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;position:relative}" +
      "#tab-home .pw-canvas{position:relative;height:calc(100vh - 150px);overflow:auto;" +
        "background:linear-gradient(var(--border,#1c2836) 1px,transparent 1px),linear-gradient(90deg,var(--border,#1c2836) 1px,transparent 1px);" +
        "background-size:40px 40px;background-color:var(--bg,#05080c);border:1px solid var(--border,#1c2836);border-radius:10px}" +
      "body.chrome-collapsed #tab-home .pw-canvas{height:calc(100vh - 96px)}" +
      "#tab-home .pw-plane{position:relative;min-width:100%;min-height:100%}" +
      ".pw-w{position:absolute;background:var(--card,#0b0f14);border:1px solid var(--border,#1c2836);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.45);display:flex;flex-direction:column;overflow:hidden;min-width:280px;min-height:180px}" +
      ".pw-w.drag,.pw-w.resizing{outline:2px solid var(--accent,#5aa2ff);z-index:50}" +
      ".pw-head{display:flex;align-items:center;gap:8px;padding:6px 10px;background:var(--surface,#0e141b);border-bottom:1px solid var(--border,#1c2836);cursor:grab;user-select:none}" +
      ".pw-head .t{font-size:12.5px;font-weight:700;color:var(--text,#e7edf5);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".pw-head .sp{margin-left:auto;display:flex;gap:11px;align-items:center;color:var(--muted,#93a1b3);font-size:14px}" +
      ".pw-head .sp span{cursor:pointer;line-height:1}.pw-head .sp span:hover{color:var(--text,#e7edf5)}" +
      ".pw-body{flex:1;position:relative;min-height:0;background:var(--bg,#05080c)}" +
      ".pw-body iframe{position:absolute;inset:0;width:100%;height:100%;border:0;display:block}" +
      ".pw-shield{position:absolute;inset:0;display:none;z-index:2}" +
      ".pw-w.drag .pw-shield,.pw-w.resizing .pw-shield{display:block}" +
      ".pw-resize{position:absolute;right:1px;bottom:1px;width:20px;height:20px;cursor:nwse-resize;z-index:3;background:linear-gradient(135deg,transparent 42%,var(--muted,#93a1b3) 42%,var(--muted,#93a1b3) 52%,transparent 52%,transparent 66%,var(--muted,#93a1b3) 66%,var(--muted,#93a1b3) 76%,transparent 76%)}" +
      ".pw-btn{background:var(--card,#0b0f14);border:1px solid var(--border,#1c2836);color:var(--text,#e7edf5);border-radius:8px;padding:7px 12px;font-size:12.5px;font-weight:600;cursor:pointer;font-family:inherit}" +
      ".pw-btn:hover{border-color:var(--accent,#5aa2ff)}" +
      ".pw-sel,.pw-inp{background:var(--surface,#0e141b);border:1px solid var(--border,#1c2836);color:var(--text,#e7edf5);border-radius:8px;padding:7px 9px;font-size:12.5px;font-family:inherit;outline:none}" +
      ".pw-pop{position:absolute;top:42px;left:0;z-index:80;background:var(--card,#0b0f14);border:1px solid var(--border,#1c2836);border-radius:10px;box-shadow:0 16px 40px rgba(0,0,0,.5);padding:8px;width:320px;max-height:60vh;overflow:auto;display:none}" +
      ".pw-pop.open{display:block}.pw-pop .muted{color:var(--muted,#93a1b3);font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:4px 6px 6px}" +
      ".pw-pop .grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}" +
      ".pw-pop .it{display:flex;align-items:center;gap:6px;padding:8px 9px;border-radius:8px;cursor:pointer;font-size:12.5px;color:var(--text,#e7edf5);border:1px solid var(--border,#1c2836);background:var(--surface,#0e141b)}" +
      ".pw-pop .it:hover{border-color:var(--accent,#5aa2ff)}" +
      ".pw-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:var(--muted,#93a1b3);text-align:center;pointer-events:none}";
    document.head.appendChild(s);
  }

  function embedUrl(tab) { return location.pathname + "?embed=" + encodeURIComponent(tab); }

  function fitPlane() {
    var maxR = canvas.clientWidth, maxB = canvas.clientHeight;
    state.widgets.forEach(function (w) { maxR = Math.max(maxR, (w.x || 0) + (w.w || 0) + 40); maxB = Math.max(maxB, (w.y || 0) + (w.h || 0) + 40); });
    plane.style.width = maxR + "px"; plane.style.height = maxB + "px";
  }

  function renderEmpty() {
    var e = plane.querySelector(".pw-empty");
    var has = state.widgets.length > 0;
    if (has && e) e.remove();
    if (!has && !e) {
      var d = document.createElement("div"); d.className = "pw-empty";
      d.innerHTML = '<div style="font-size:34px;opacity:.4">\uD83E\uDDE9</div><div style="font-size:14px;font-weight:600;color:var(--text,#e7edf5)">Your Home canvas is empty</div><div style="font-size:12.5px">Click <b>\uFF0B Add Page</b> to drop any page here as a movable, resizable widget.</div>';
      plane.appendChild(d);
    }
  }

  function makeWidget(w) {
    var el = document.createElement("div"); el.className = "pw-w"; el.setAttribute("data-id", w.id);
    el.style.left = (w.x || 24) + "px"; el.style.top = (w.y || 24) + "px";
    el.style.width = (w.w || 640) + "px"; el.style.height = (w.h || 460) + "px";
    var item = catalogList.filter(function (c) { return c.tab === w.tab; })[0] || { label: w.tab };
    el.innerHTML =
      '<div class="pw-head"><span class="t">' + item.label + '</span>' +
      '<span class="sp"><span data-a="open" title="Open full page">\u2197</span><span data-a="reload" title="Reload">\u21bb</span><span data-a="remove" title="Remove">\u00d7</span></span></div>' +
      '<div class="pw-body"><iframe loading="lazy" src="' + embedUrl(w.tab) + '"></iframe><div class="pw-shield"></div></div>' +
      '<div class="pw-resize"></div>';
    plane.appendChild(el);

    var head = el.querySelector(".pw-head"), iframe = el.querySelector("iframe"), handle = el.querySelector(".pw-resize");
    head.querySelector('[data-a=open]').addEventListener("click", function (e) { e.stopPropagation(); var b = document.querySelector('[data-tab="' + w.tab + '"]'); if (b) b.click(); });
    head.querySelector('[data-a=reload]').addEventListener("click", function (e) { e.stopPropagation(); iframe.src = embedUrl(w.tab); });
    head.querySelector('[data-a=remove]').addEventListener("click", function (e) { e.stopPropagation(); el.remove(); state.widgets = state.widgets.filter(function (x) { return x.id !== w.id; }); save(); fitPlane(); renderEmpty(); });

    head.addEventListener("mousedown", function (ev) {
      if (ev.target.closest(".sp")) return;
      ev.preventDefault(); el.classList.add("drag");
      var sx = ev.clientX, sy = ev.clientY, ox = el.offsetLeft, oy = el.offsetTop;
      function mm(e) { el.style.left = Math.max(0, ox + e.clientX - sx) + "px"; el.style.top = Math.max(0, oy + e.clientY - sy) + "px"; }
      function mu() { document.removeEventListener("mousemove", mm); document.removeEventListener("mouseup", mu); el.classList.remove("drag"); w.x = el.offsetLeft; w.y = el.offsetTop; save(); fitPlane(); }
      document.addEventListener("mousemove", mm); document.addEventListener("mouseup", mu);
    });
    handle.addEventListener("mousedown", function (ev) {
      ev.preventDefault(); ev.stopPropagation(); el.classList.add("resizing");
      var sx = ev.clientX, sy = ev.clientY, ow = el.offsetWidth, oh = el.offsetHeight;
      function mm(e) { el.style.width = Math.max(280, ow + e.clientX - sx) + "px"; el.style.height = Math.max(180, oh + e.clientY - sy) + "px"; fitPlane(); }
      function mu() { document.removeEventListener("mousemove", mm); document.removeEventListener("mouseup", mu); el.classList.remove("resizing"); w.w = el.offsetWidth; w.h = el.offsetHeight; save(); fitPlane(); }
      document.addEventListener("mousemove", mm); document.addEventListener("mouseup", mu);
    });
    el.addEventListener("mousedown", function () { [].forEach.call(plane.querySelectorAll(".pw-w"), function (x) { x.style.zIndex = ""; }); el.style.zIndex = 40; });
  }

  function addWidget(tab) {
    var n = state.widgets.length;
    var w = { id: "w" + Date.now() + Math.floor(Math.random() * 99), tab: tab, x: 20 + (n % 3) * 34, y: 20 + (n % 3) * 34, w: 680, h: 480 };
    state.widgets.push(w); save(); renderEmpty(); makeWidget(w); fitPlane();
  }

  function renderPop() {
    var pop = document.getElementById("pw-add-pop");
    var html = '<div class="muted">Add any page as a widget</div><div class="grid">';
    catalogList.forEach(function (c) { html += '<div class="it" data-t="' + c.tab + '">' + c.label + '<span style="margin-left:auto;color:var(--accent,#5aa2ff)">\uFF0B</span></div>'; });
    html += '</div>'; pop.innerHTML = html;
    [].forEach.call(pop.querySelectorAll("[data-t]"), function (it) { it.addEventListener("click", function () { addWidget(it.getAttribute("data-t")); pop.classList.remove("open"); }); });
  }

  function snapshot() { return state.widgets.map(function (w) { return { tab: w.tab, x: w.x, y: w.y, w: w.w, h: w.h }; }); }
  function clearWidgets() { [].forEach.call(plane.querySelectorAll(".pw-w"), function (x) { x.remove(); }); }
  function applyLayout(id) {
    var l = state.layouts.filter(function (x) { return x.id === id; })[0]; if (!l) return;
    clearWidgets();
    state.widgets = l.widgets.map(function (w, i) { return { id: "w" + Date.now() + i, tab: w.tab, x: w.x, y: w.y, w: w.w, h: w.h }; });
    state.active = id; save(); renderEmpty(); state.widgets.forEach(makeWidget); fitPlane(); renderLayoutSelect();
  }
  function renderLayoutSelect() {
    var sel = document.getElementById("pw-layouts"); if (!sel) return;
    var html = '<option value="">Layouts\u2026</option>';
    state.layouts.forEach(function (l) { html += '<option value="' + l.id + '"' + (state.active === l.id ? " selected" : "") + '>' + l.name + '</option>'; });
    sel.innerHTML = html;
  }

  function build() {
    var home = document.getElementById("tab-home"); if (!home || home.getAttribute("data-pw") === "1") return;
    home.setAttribute("data-pw", "1"); css();
    catalogList = catalog(); state = load();

    bar = document.createElement("div"); bar.className = "pw-toolbar";
    bar.innerHTML =
      '<button class="pw-btn" id="pw-min" title="Minimize / expand this toolbar" style="padding:7px 10px">\u25be</button>' +
      '<span class="pw-tools" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">' +
      '<div style="position:relative"><button class="pw-btn" id="pw-add">\uFF0B Add Page <span style="color:var(--muted,#93a1b3);font-size:10px">\u25be</span></button><div class="pw-pop" id="pw-add-pop"></div></div>' +
      '<button class="pw-btn" id="pw-tile" title="Auto-arrange into a tidy grid">\u25a6 Tile</button>' +
      '<span style="width:1px;height:22px;background:var(--border,#1c2836)"></span>' +
      '<input class="pw-inp" id="pw-lname" placeholder="Layout name\u2026" style="width:130px"/>' +
      '<button class="pw-btn" id="pw-save">\uD83D\uDCBE Save</button>' +
      '<select class="pw-sel" id="pw-layouts" title="Switch saved layout"></select>' +
      '<button class="pw-btn" id="pw-del" title="Delete selected layout">\uD83D\uDDD1</button>' +
      '<span style="width:1px;height:22px;background:var(--border,#1c2836)"></span>' +
      '<button class="pw-btn" id="pw-chrome" title="Hide/show the top menus for more space">\u2921 Hide menus</button>' +
      '<button class="pw-btn" id="pw-clear" title="Remove all widgets">\u2715 Clear</button>' +
      '<span style="font-size:11.5px;color:var(--muted,#93a1b3);margin-left:2px">Drag headers to move \u00b7 drag corner to resize \u00b7 scroll for off-screen widgets \u00b7 auto-saves</span>' +
      '</span>';
    canvas = document.createElement("div"); canvas.className = "pw-canvas";
    plane = document.createElement("div"); plane.className = "pw-plane";
    canvas.appendChild(plane);
    home.appendChild(bar); home.appendChild(canvas);

    renderPop(); renderEmpty(); renderLayoutSelect();
    state.widgets.forEach(makeWidget); fitPlane();

    var add = bar.querySelector("#pw-add"), pop = bar.querySelector("#pw-add-pop");
    add.addEventListener("click", function (e) { e.stopPropagation(); pop.classList.toggle("open"); });
    document.addEventListener("click", function () { pop.classList.remove("open"); });
    pop.addEventListener("click", function (e) { e.stopPropagation(); });

    bar.querySelector("#pw-tile").addEventListener("click", function () {
      var cw = canvas.clientWidth - 16, cols = Math.max(1, Math.round(cw / 640)), gw = Math.floor(cw / cols) - 12, gh = 460, i = 0;
      state.widgets.forEach(function (w) { var r = Math.floor(i / cols), c = i % cols; w.x = 8 + c * (gw + 12); w.y = 8 + r * (gh + 12); w.w = gw; w.h = gh; i++;
        var el = plane.querySelector('[data-id="' + w.id + '"]'); if (el) { el.style.left = w.x + "px"; el.style.top = w.y + "px"; el.style.width = w.w + "px"; el.style.height = w.h + "px"; } });
      save(); fitPlane();
    });
    bar.querySelector("#pw-save").addEventListener("click", function () {
      var inp = bar.querySelector("#pw-lname"); var nm = (inp.value || "").trim() || ("Layout " + (state.layouts.length + 1));
      var id = "ly" + Date.now(); state.layouts.push({ id: id, name: nm, widgets: snapshot() }); state.active = id; inp.value = ""; save(); renderLayoutSelect();
    });
    bar.querySelector("#pw-layouts").addEventListener("change", function () { if (this.value) applyLayout(this.value); });
    bar.querySelector("#pw-del").addEventListener("click", function () {
      var sel = bar.querySelector("#pw-layouts"); var id = sel.value; if (!id) return;
      state.layouts = state.layouts.filter(function (l) { return l.id !== id; }); if (state.active === id) state.active = null; save(); renderLayoutSelect();
    });
    bar.querySelector("#pw-chrome").addEventListener("click", function () {
      var on = document.body.classList.toggle("chrome-collapsed");
      this.textContent = on ? "\u2921 Show menus" : "\u2921 Hide menus";
      try { localStorage.setItem("oi-chrome-collapsed", on ? "1" : "0"); } catch (e) {}
    });
    if (localStorage.getItem("oi-chrome-collapsed") === "1") { document.body.classList.add("chrome-collapsed"); bar.querySelector("#pw-chrome").textContent = "\u2921 Show menus"; }

    bar.querySelector("#pw-clear").addEventListener("click", function () { if (!confirm("Remove all widgets from Home?")) return; state.widgets = []; state.active = null; save(); clearWidgets(); fitPlane(); renderEmpty(); });

    // minimize / expand the Home toolbar
    bar.querySelector("#pw-min").addEventListener("click", function () {
      var on = bar.classList.toggle("pw-min"); this.textContent = on ? "\u25b8" : "\u25be";
      try { localStorage.setItem("oi-home-toolbar-min", on ? "1" : "0"); } catch (e) {}
    });
    if (localStorage.getItem("oi-home-toolbar-min") === "1") { bar.classList.add("pw-min"); bar.querySelector("#pw-min").textContent = "\u25b8"; }

    // brand → Home navigation
    var brand = document.querySelector("#topbar-brand .brand") || document.querySelector("#topbar-brand");
    if (brand) { brand.title = "Go to Home dashboard"; brand.addEventListener("click", function () { var b = document.querySelector('[data-tab="home"]'); if (b) b.click(); }); }

    // global ☰ toggle for the top nav menus (works on every page)
    var topbar = document.getElementById("topbar");
    if (topbar && !document.getElementById("pw-menu-toggle")) {
      var mt = document.createElement("button");
      mt.id = "pw-menu-toggle"; mt.type = "button"; mt.title = "Show / hide the navigation menus"; mt.textContent = "\u2630";
      topbar.insertBefore(mt, topbar.firstChild);
      mt.addEventListener("click", function () {
        var on = document.body.classList.toggle("nav-collapsed");
        try { localStorage.setItem("oi-nav-collapsed", on ? "1" : "0"); } catch (e) {}
      });
      if (localStorage.getItem("oi-nav-collapsed") === "1") document.body.classList.add("nav-collapsed");
    }

    window.addEventListener("resize", fitPlane);
  }

  function boot() { if (document.getElementById("tab-home")) build(); else setTimeout(boot, 200); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
