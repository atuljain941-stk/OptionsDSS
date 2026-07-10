/* ============================================================
   panel_tools.js — universal panel/card controls on EVERY page.
   Adds to each matched panel a header with: drag-to-reorder handle,
   a title, and a collapse/expand chevron; and makes the panel
   resizable (native corner grip). One consistent behaviour across
   all pages, regardless of which CSS naming convention that page's
   cards happen to use.

   Opt-in per element:
     data-pt-collapsed="1"   → starts collapsed the first time a
                                visitor sees this page (their own
                                later toggle is remembered after that)
     data-pt-title="..."     → override the auto-detected title
     data-pt-group="..."     → separate persistence/reorder scope
                                for panels that shouldn't reorder
                                against each other (defaults to "default")
     class="pt-skip"         → opt a specific element out entirely

   Pure enhancement — no data/markup logic touched. Safe to load on
   any page; if nothing matches the selector, it does nothing.
   ============================================================ */
(function () {
  "use strict";
  // Every card/panel naming convention used across the app's pages.
  // Extend this list rather than duplicating the whole file per page.
  var SEL = ".card, .panel, .sn-card, .cp-card, .sh-card, .sd-card";
  var STORE_PREFIX = "pt:v2:";

  function storeKey(suffix) {
    return STORE_PREFIX + (location.pathname || "/") + ":" + suffix;
  }

  function titleOf(card) {
    if (card.dataset.ptTitle) return card.dataset.ptTitle.slice(0, 70);
    var t = card.querySelector(":scope > .card-title") || card.querySelector(".card-title") ||
            card.querySelector(":scope > h1, :scope > h2, :scope > h3, :scope > h4") ||
            card.querySelector("h1,h2,h3,h4") ||
            card.querySelector(".uiPanel-title, .sn-section-title, .cp-section-title");
    var s = t ? (t.textContent || "").trim() : "";
    return (s ? s.slice(0, 70) : "Panel").replace(/[<>&]/g, "");
  }

  function groupOf(card) {
    return card.dataset.ptGroup || "default";
  }

  // Stable ids: based on title identity, NOT DOM position — position
  // changes every time a panel is dragged, which would otherwise break
  // matching between a previously-saved order and the current DOM on
  // the next page load. An occurrence counter only kicks in to
  // disambiguate genuinely duplicate titles within the same group, and
  // is assigned once (at first enhancement) and cached on the element
  // so it never silently drifts as siblings get reordered later.
  function assignIds(cards) {
    var seen = {};
    cards.forEach(function (card) {
      if (card.__ptId) return; // already assigned, keep it stable
      var t = titleOf(card) || "panel";
      seen[t] = (seen[t] || 0) + 1;
      card.__ptId = seen[t] > 1 ? (t + "#" + seen[t]) : t;
    });
  }

  function enhance(card) {
    if (card.__pt || card.classList.contains("pt-on") || card.classList.contains("pt-skip")) return;
    // don't wrap a card that only exists to hold the page-widget canvas
    if (card.closest("#tab-home")) return;
    card.__pt = 1; card.classList.add("pt-on");
    if (getComputedStyle(card).position === "static") card.style.position = "relative";

    var group = groupOf(card);
    var id = card.__ptId || (card.__ptId = titleOf(card) || "panel");
    var collapsedKey = storeKey("collapsed:" + group + ":" + id);

    var bar = document.createElement("div"); bar.className = "pt-bar";
    bar.innerHTML =
      '<span class="pt-h" title="Drag to reorder" draggable="true">\u2630</span>' +
      '<span class="pt-t">' + titleOf(card) + '</span>' +
      '<span class="pt-c" title="Collapse / expand">\u25be</span>';
    card.insertBefore(bar, card.firstChild);
    card.style.resize = "both";
    card.style.overflow = "hidden";

    // Decide initial collapsed state: a previous explicit user choice
    // wins; otherwise fall back to the page author's data-pt-collapsed
    // default (useful for de-cluttering settings/helper panels the
    // first time someone opens a busy page).
    var saved = null;
    try { saved = localStorage.getItem(collapsedKey); } catch (e) {}
    var startCollapsed = saved !== null ? saved === "1" : card.dataset.ptCollapsed === "1";
    if (startCollapsed) {
      card.classList.add("pt-collapsed");
      bar.querySelector(".pt-c").textContent = "\u25b8";
    }

    bar.querySelector(".pt-c").addEventListener("click", function (e) {
      e.stopPropagation();
      var on = card.classList.toggle("pt-collapsed");
      this.textContent = on ? "\u25b8" : "\u25be";
      try { localStorage.setItem(collapsedKey, on ? "1" : "0"); } catch (err) {}
    });

    var h = bar.querySelector(".pt-h");
    h.addEventListener("dragstart", function (e) { card.classList.add("pt-drag"); try { e.dataTransfer.setData("text", "1"); e.dataTransfer.effectAllowed = "move"; } catch (x) {} });
    h.addEventListener("dragend", function () {
      card.classList.remove("pt-drag");
      persistOrder(card.parentNode, group);
    });
    card.addEventListener("dragover", function (e) { var d = document.querySelector(".pt-drag"); if (d && d !== card && d.parentNode === card.parentNode) e.preventDefault(); });
    card.addEventListener("drop", function (e) {
      var d = document.querySelector(".pt-drag"); if (!d || d === card || d.parentNode !== card.parentNode) return;
      e.preventDefault();
      var sibs = [].slice.call(card.parentNode.children);
      if (sibs.indexOf(d) < sibs.indexOf(card)) card.parentNode.insertBefore(d, card.nextSibling);
      else card.parentNode.insertBefore(d, card);
    });
  }

  function persistOrder(parent, group) {
    if (!parent) return;
    try {
      var order = [].slice.call(parent.children)
        .filter(function (el) { return el.__pt; })
        .map(function (el) { return el.__ptId; });
      localStorage.setItem(storeKey("order:" + group), JSON.stringify(order));
    } catch (e) {}
  }

  function restoreOrder(parent, group) {
    if (!parent) return;
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(storeKey("order:" + group)) || "null"); } catch (e) {}
    if (!Array.isArray(saved) || !saved.length) return;
    var byId = {};
    [].slice.call(parent.children).forEach(function (el) {
      if (el.__pt) byId[el.__ptId] = el;
    });
    saved.forEach(function (id) {
      if (byId[id]) parent.appendChild(byId[id]);
    });
  }

  function scan() {
    try {
      var groups = {};
      document.querySelectorAll(SEL).forEach(function (card) {
        var group = groupOf(card);
        (groups[group] = groups[group] || []).push(card);
      });
      Object.keys(groups).forEach(function (group) {
        assignIds(groups[group]);
        groups[group].forEach(function (card) { enhance(card); });
        groups[group].forEach(function (card) {
          if (!card.parentNode || card.parentNode.__ptSeen) return;
          card.parentNode.__ptSeen = true;
          restoreOrder(card.parentNode, group);
        });
      });
    } catch (e) { /* never let panel enhancement break the page */ }
  }

  var to = null;
  function boot() {
    scan();
    try {
      var mo = new MutationObserver(function () { if (to) return; to = setTimeout(function () { to = null; scan(); }, 400); });
      mo.observe(document.body, { childList: true, subtree: true });
    } catch (e) {}
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
