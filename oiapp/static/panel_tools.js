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
    // Explicit data-pt-group always wins if a page sets it. Otherwise,
    // default to the containing tab's own id (the nearest ancestor
    // .section, e.g. "tab-scheduler", "tab-sql") rather than a single
    // shared "default" string.
    //
    // This was a real, app-wide bug: with only one panel anywhere in
    // the app setting an explicit group, virtually every other panel on
    // every tab fell back to the exact same "default" group -- meaning
    // the drag-reorder and cross-row layout-persistence system had no
    // concept of tab boundaries whatsoever. Two ungrouped panels on
    // completely different tabs were, as far as this system was
    // concerned, just two panels in the same list, fully eligible to
    // swap positions with each other. That's what actually explains a
    // panel from one page appearing to have moved onto a different,
    // unrelated page: it wasn't teleporting, it was being reordered
    // within what the system believed was one single, page-spanning
    // group the whole time.
    if (card.dataset.ptGroup) return card.dataset.ptGroup;
    var section = card.closest(".section[id]");
    return (section && section.id) || "default";
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

    // Pinned panels (data-pt-pin="top") are the primary controls for a
    // page -- symbol selectors, "Analyze"/"Run" buttons, the things you
    // need just to operate the page at all. These used to be draggable
    // like every other panel, which meant an accidental drag could move
    // the ONE panel a page can't function without somewhere else
    // entirely, with no way to tell where it went. Pinned panels get no
    // drag handle at all, can't be a drop target's "insert before" spot,
    // and get force-corrected back to position 0 if anything ever
    // manages to displace them (e.g. a stale saved layout from before
    // this was added).
    var isPinned = card.hasAttribute("data-pt-pin");

    // Grid-based panels (a .pt-grid container's direct children) use a
    // completely different sizing model from regular flex-row panels:
    // instead of a pixel flex-basis, they declare how many of the 12
    // grid columns they span. This is what actually fixes "some panels
    // only resize height, not width" -- every panel in a .pt-grid
    // container resizes through the exact same column-span mechanism,
    // instead of some panels being plain .card (no width resize) and
    // others being .col-half (flex-basis resize that fights its
    // sibling). Initial span comes from data-pt-cols in the HTML (or
    // 12 -- full width -- if that's not set), then gets overridden by
    // whatever the user last resized it to, if anything.
    var isGridChild = card.parentNode && card.parentNode.classList.contains("pt-grid");
    var gridColsKey = isGridChild ? storeKey("gridcols:" + group + ":" + id) : null;
    if (isGridChild) {
      var savedCols = null;
      try { savedCols = parseInt(localStorage.getItem(gridColsKey), 10); } catch (e) {}
      var initialCols = (savedCols >= 1 && savedCols <= 12) ? savedCols
        : (parseInt(card.dataset.ptCols, 10) || 12);
      card.style.setProperty("--pt-cols", initialCols);
    }

    var bar = document.createElement("div"); bar.className = "pt-bar";
    bar.innerHTML = isPinned
      ? '<span class="pt-h" title="Pinned in place -- always stays first" style="cursor:default;opacity:.5">\u{1F4CC}</span>' +
        '<span class="pt-t">' + titleOf(card) + '</span>' +
        '<span class="pt-c" title="Collapse / expand">\u25be</span>'
      : '<span class="pt-h" title="Drag to reorder" draggable="true">\u2630</span>' +
        '<span class="pt-t">' + titleOf(card) + '</span>' +
        '<span class="pt-c" title="Collapse / expand">\u25be</span>';
    card.insertBefore(bar, card.firstChild);
    if (!isPinned) {
      // Grid children get vertical-only native resize -- width isn't a
      // free-pixel thing for them at all, it's the column-span logic
      // below, so the native horizontal resize handle would just fight
      // that (same class of conflict flex-basis used to have).
      card.style.resize = isGridChild ? "vertical" : "both";
      card.style.overflow = "hidden";
    }
    // Pinned panels get neither. This is the piece the size-restore fix
    // and the collapse fix didn't actually cover: resize:both paired
    // with overflow:hidden is what makes the native browser resize
    // handle work at all, but that pairing can itself constrain an
    // element to a small intrinsic size in some layout situations, with
    // no localStorage or user interaction involved whatsoever. A
    // control panel (buttons, a symbol picker) has no reason to be
    // resizable in the first place -- it should always just naturally
    // size to fit its content, full stop.

    // Width resize alone didn't work before this: most panels sit inside
    // a flex row (e.g. ".col-half { flex: 1 1 0 }"), and that flex-basis
    // gets recalculated on every layout pass -- so even though the
    // native resize handle supports both directions and you could drag
    // it wider, flexbox immediately overrides the width back to its
    // computed share of the row. Height isn't fought the same way by a
    // row-direction flex container, which is why only height ever
    // visibly worked. Once a panel is manually resized, pin it to a
    // fixed flex-basis so the manual size actually sticks, and persist
    // it the same way collapse/order state already is.
    // Bumped from "size:" to "size2:" deliberately: the previous version
    // pinned+persisted a panel's width on ANY ResizeObserver-detected
    // change, including the initial layout settling that happens right
    // after this header bar gets inserted (before the flex container has
    // finished laying siblings out) -- so panels could get permanently
    // stuck at whatever narrow width they happened to measure at during
    // that split second, well before any user touched a resize handle.
    // This key-name bump orphans all of that previously-broken persisted
    // data so everyone gets a clean slate; the new logic below only
    // pins/persists on an actual user-driven drag.
    var sizeKey = storeKey("size2:" + group + ":" + id);
    if (!isPinned) {
      // Pinned panels never participate in saved sizing at all -- they're
      // control bars (buttons, a symbol picker), not resizable content,
      // and a stale/bad persisted size here is pure downside: it can
      // silently collapse the ONE panel a page needs just to be usable
      // down to a sliver, with overflow:hidden clipping everything
      // inside it -- which is exactly what happened on the Earnings
      // page. Non-pinned panels still get the floor check below, so
      // this same failure mode can't happen to them either.
      try {
        var savedSize = JSON.parse(localStorage.getItem(sizeKey) || "null");
        // This floor was already enforced when a size gets SAVED (see
        // the mouseup handler below), but was missing here on RESTORE --
        // so anything persisted before that save-side floor existed (or
        // by any other path) got reapplied with no protection at all.
        if (savedSize && savedSize.w >= 120) card.style.flex = "0 0 " + savedSize.w + "px";
        if (savedSize && savedSize.h >= 60) card.style.height = savedSize.h + "px";
      } catch (e) {}
    }

    // Only pin the manually-chosen size when the user actually dragged
    // the native resize handle (bottom-right corner) -- track that
    // explicitly instead of trusting every ResizeObserver firing, since
    // plenty of those are just the surrounding layout settling and have
    // nothing to do with user intent.
    // This custom mousedown-proximity detector is what actually drives
    // resizing (see the flex-pinning comment below) -- it's completely
    // independent of the native CSS `resize` property, so removing
    // resize:both above does NOT stop this from firing. Gating it
    // behind !isPinned is what actually makes a pinned panel fully
    // non-resizable: without this, any click landing within 22px of the
    // panel's corner -- not even a deliberate resize attempt -- would
    // still pin its flex-basis to whatever width it happened to be at
    // that instant, which is exactly the kind of "gets stuck small for
    // no visible reason" failure this whole pinning mechanism exists to
    // prevent.
    if (!isPinned) {
      var RESIZE_CORNER_PX = 22;
      var resizing = false;
      var resizeStartW = 0;
      card.addEventListener("mousedown", function (e) {
        var r = card.getBoundingClientRect();
        var nearRight = e.clientX >= r.right - RESIZE_CORNER_PX && e.clientX <= r.right + 4;
        var nearBottom = e.clientY >= r.bottom - RESIZE_CORNER_PX && e.clientY <= r.bottom + 4;
        resizing = nearRight && nearBottom;
        if (resizing) {
          resizeStartW = card.offsetWidth;
          if (!isGridChild) {
            // This is the actual fix for "width resize only works when a
            // panel is alone in its row": with a sibling still on
            // flex:1 1 0, the flex algorithm recalculates and overrides
            // this panel's width on every single frame of the drag (not
            // just after releasing the mouse) -- so the native resize
            // handle was fighting flexbox in real time and visually doing
            // nothing for the whole drag. Pinning to the CURRENT size
            // right when the drag starts takes this panel out of flex's
            // grow/shrink accounting for the duration of the drag, so the
            // native handle actually has control of the box.
            card.style.flex = "0 0 " + card.offsetWidth + "px";
          }
          // Grid children don't need this pin-to-current-width step at
          // all: grid-column: span N already takes a panel out of any
          // "shrink to fit siblings" fight the way flex does, so the
          // drag can go straight to snapping by column on mouseup.
        }
      });
      document.addEventListener("mouseup", function (e) {
        if (!resizing) return;
        resizing = false;
        if (isGridChild) {
          // Snap the resized width to the nearest whole column rather
          // than keeping an arbitrary pixel width -- this is what keeps
          // every panel in a grid row resizing in the same consistent
          // units, instead of ending up with odd, unaligned widths that
          // don't line up with anything else in the row.
          var gridEl = card.parentNode;
          var gridW = gridEl.getBoundingClientRect().width;
          var colW = gridW / 12;
          var draggedW = card.offsetWidth;
          var newCols = Math.round(draggedW / colW);
          newCols = Math.max(1, Math.min(12, newCols));
          card.style.setProperty("--pt-cols", newCols);
          try { localStorage.setItem(gridColsKey, String(newCols)); } catch (err) {}
          return;
        }
        var w = Math.round(card.offsetWidth), h = Math.round(card.offsetHeight);
        // Sanity floor -- never pin to something so small the panel becomes
        // useless; a stray click near the corner without an actual drag
        // shouldn't be able to shrink-and-lock a panel either.
        if (w < 120 || h < 60) return;
        card.style.flex = "0 0 " + w + "px";
        try { localStorage.setItem(sizeKey, JSON.stringify({ w: w, h: h })); } catch (e) {}
      });
    }

    // Decide initial collapsed state: a previous explicit user choice
    // wins; otherwise fall back to the page author's data-pt-collapsed
    // default (useful for de-cluttering settings/helper panels the
    // first time someone opens a busy page).
    //
    // Pinned panels are exempt entirely -- same reasoning as the drag
    // and size fixes above: a control panel (buttons, a symbol picker)
    // collapsing down to a 34px sliver via .pt-collapsed's
    // `max-height: 34px !important; overflow: hidden !important` and
    // getting stuck there from one stray click, with the state
    // persisted forever, is exactly the kind of "page becomes
    // unusable" failure this whole pinning mechanism exists to prevent.
    // No collapse button at all for these, and any stale collapsed
    // state already sitting in localStorage from before this fix is
    // simply ignored rather than requiring a migration.
    if (isPinned) {
      var collapseBtn = bar.querySelector(".pt-c");
      if (collapseBtn) collapseBtn.remove();
    } else {
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
    }

    var h = bar.querySelector(".pt-h");
    h.addEventListener("dragstart", function (e) { card.classList.add("pt-drag"); try { e.dataTransfer.setData("text", "1"); e.dataTransfer.effectAllowed = "move"; } catch (x) {} });
    h.addEventListener("dragend", function () {
      card.classList.remove("pt-drag");
      persistLayout(group);
    });
    // No same-parent restriction here anymore -- a panel can be dropped
    // into ANY row in its group, not just reordered within the one it
    // started in. That's what makes "drag this panel up into the row
    // above, which has spare width" actually possible; before this, the
    // drop handler silently refused (via the parentNode check) unless
    // source and target already shared the same row.
    card.addEventListener("dragover", function (e) {
      var d = document.querySelector(".pt-drag");
      if (d && d !== card && groupOf(d) === group) e.preventDefault();
    });
    card.addEventListener("drop", function (e) {
      var d = document.querySelector(".pt-drag");
      if (!d || d === card || groupOf(d) !== group) return;
      e.preventDefault();
      var targetParent = card.parentNode;
      var sibs = [].slice.call(targetParent.children);
      var wasSameRow = d.parentNode === targetParent;
      if (isPinned) {
        // Never let anything land before a pinned panel -- always after
        // it, regardless of drag direction. This is what actually keeps
        // it at position 0: without this, dropping another panel "onto"
        // the pinned one from below could still push it to position 1.
        targetParent.insertBefore(d, card.nextSibling);
      } else if (wasSameRow && sibs.indexOf(d) < sibs.indexOf(card)) {
        targetParent.insertBefore(d, card.nextSibling);
      } else {
        targetParent.insertBefore(d, card);
      }
      persistLayout(group);
    });
  }

  function persistLayout(group) {
    // Was persistOrder(parent, group): only ever recorded order WITHIN
    // whichever single row it was called on, keyed by group alone -- on
    // a page with multiple rows sharing a group, each row's save would
    // silently overwrite the previous one's, and there was no way to
    // remember which row a panel belonged to at all. This records every
    // panel's row + position in one shot, which is what makes moving a
    // panel to a *different* row actually stick across reloads.
    try {
      var rows = rowsOf(group);
      var layout = [];
      rows.forEach(function (row, rowIdx) {
        [].slice.call(row.children).forEach(function (el, pos) {
          if (el.__pt) layout.push({ id: el.__ptId, row: rowIdx, pos: pos });
        });
      });
      localStorage.setItem(storeKey("layout:" + group), JSON.stringify(layout));
    } catch (e) {}
  }

  function rowsOf(group) {
    // Stable per-row identity within a group: the DOM order of each
    // row's first appearance, captured once and kept (via a WeakMap) so
    // "row 0", "row 1" etc. mean the same thing across a save and a
    // later restore even if rows themselves get scanned in a different
    // order on a re-scan.
    if (!rowsOf._registry) rowsOf._registry = {};
    var reg = rowsOf._registry[group] || (rowsOf._registry[group] = []);
    document.querySelectorAll(SEL).forEach(function (card) {
      if (groupOf(card) !== group || !card.parentNode) return;
      if (reg.indexOf(card.parentNode) === -1) reg.push(card.parentNode);
    });
    return reg;
  }

  function restoreLayout(group) {
    var saved = null;
    try { saved = JSON.parse(localStorage.getItem(storeKey("layout:" + group)) || "null"); } catch (e) {}
    if (!Array.isArray(saved) || !saved.length) return;
    var rows = rowsOf(group);
    var byId = {};
    document.querySelectorAll(SEL).forEach(function (el) {
      if (el.__pt && groupOf(el) === group) byId[el.__ptId] = el;
    });
    saved
      .slice()
      .sort(function (a, b) { return a.row - b.row || a.pos - b.pos; })
      .forEach(function (entry) {
        var el = byId[entry.id];
        var row = rows[entry.row];
        if (el && row) row.appendChild(el);
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
        if (!scan.__restored) scan.__restored = {};
        if (!scan.__restored[group]) {
          scan.__restored[group] = true;
          restoreLayout(group);
        }
      });
      enforcePins();
    } catch (e) { /* never let panel enhancement break the page */ }
  }

  function enforcePins() {
    // Unconditional, every scan -- fixes the immediate problem (a pinned
    // panel already stuck somewhere else from a layout saved before
    // pinning existed) and guarantees it can never drift again, without
    // needing a one-time migration or clearing anyone's saved layout.
    document.querySelectorAll("[data-pt-pin]").forEach(function (card) {
      var parent = card.parentNode;
      if (parent && parent.firstElementChild !== card) {
        parent.insertBefore(card, parent.firstElementChild);
      }
    });
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
