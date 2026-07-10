/* ============================================================
   widgetizer.js  —  reusable page-to-widgets engine (UI uplift)
   Turns any page's panels into a draggable widget grid with
   collapse / resize / remove / add + unlimited saved layouts,
   persisted per-page in localStorage and restored on reload.

   USAGE (add near end of a template, after the page's own scripts):
     <script src="/static/widgetizer.js"></script>
     <script>
       Widgetizer.init({
         page: "journal",                 // unique key for localStorage
         container: "#journal-root",       // element whose children become widgets
         mode: "heading",                  // "heading" | "selectors"
         heading: "h2",                    // (heading mode) split boundary
         // OR: panels:[{id,title,selector,span}]  (selectors mode)
         spans: [4,6,8,12],
         defaultSpan: 12
       });
     </script>

   Progressive enhancement only — never changes data, forms, routes or
   existing scripts. If the container isn't found it silently no-ops.
   ============================================================ */
(function (root) {
  "use strict";
  var SPANS_DEFAULT = [4, 6, 8, 12];

  function css() {
    if (document.getElementById("wz-style")) return;
    var s = document.createElement("style"); s.id = "wz-style";
    s.textContent =
      ".wz-bar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 12px;position:relative}" +
      ".wz-grid{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;align-items:start}" +
      ".wz-w{grid-column:span 12;background:var(--card,#0b0f14);border:1px solid var(--border,#1c2836);border-radius:var(--radius,12px);overflow:hidden;min-width:0}" +
      ".wz-head{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--border,#1c2836);background:var(--surface,#0e141b);cursor:grab}" +
      ".wz-head.drag{opacity:.5}.wz-grip{color:var(--muted,#93a1b3);font-size:12px}" +
      ".wz-title{font-size:12.5px;font-weight:700;color:var(--text,#e7edf5);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}" +
      ".wz-ctrls{margin-left:auto;display:flex;gap:12px;align-items:center;color:var(--muted,#93a1b3);font-size:14px}" +
      ".wz-ctrls span{cursor:pointer;line-height:1}.wz-ctrls span:hover{color:var(--text,#e7edf5)}" +
      ".wz-body{min-width:0;padding:2px 2px 4px}" +
      ".wz-btn{background:var(--card,#0b0f14);border:1px solid var(--border,#1c2836);color:var(--text,#e7edf5);border-radius:8px;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit}" +
      ".wz-btn:hover{border-color:var(--accent,#5aa2ff)}" +
      ".wz-pop{position:absolute;top:40px;z-index:60;background:var(--card,#0b0f14);border:1px solid var(--border,#1c2836);border-radius:10px;box-shadow:0 16px 40px rgba(0,0,0,.5);padding:8px;min-width:240px;display:none}" +
      ".wz-pop.open{display:block}.wz-pop .muted{color:var(--muted,#93a1b3);font-size:11px;text-transform:uppercase;letter-spacing:.4px;padding:4px 6px 6px}" +
      ".wz-pop .item{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:7px;cursor:pointer;font-size:13px;color:var(--text,#e7edf5)}" +
      ".wz-pop .item:hover{background:var(--surface,#0e141b)}" +
      ".wz-chip{display:inline-flex;align-items:center;gap:7px;padding:7px 11px;border-radius:9px;background:var(--surface,#0e141b);border:1px solid var(--border,#1c2836);font-size:12.5px}" +
      ".wz-chip .nm{cursor:pointer;font-weight:600;color:var(--text,#e7edf5)}" +
      ".wz-chip .del{cursor:pointer;color:var(--muted,#93a1b3)}.wz-chip .del:hover{color:var(--red,#ff7a45)}" +
      ".wz-input{background:var(--surface,#0e141b);border:1px solid var(--border,#1c2836);border-radius:8px;color:var(--text,#e7edf5);font-size:12.5px;padding:7px 10px;outline:none}";
    document.head.appendChild(s);
  }

  function slug(s){ return (s||"").toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"").slice(0,40)||("w"+Math.random().toString(36).slice(2,7)); }

  function W(cfg) {
    this.cfg = cfg;
    this.LS = "wz-" + cfg.page + "-v1";
    this.spans = cfg.spans || SPANS_DEFAULT;
    this.defaultSpan = cfg.defaultSpan || 12;
    this.panels = [];   // {id,title,span,node}
  }
  W.prototype.load = function(){ try { return JSON.parse(localStorage.getItem(this.LS))||{}; } catch(e){ return {}; } };
  W.prototype.save = function(extra){ var st=this.current(); if(extra){for(var k in extra)st[k]=extra[k];} try{ localStorage.setItem(this.LS, JSON.stringify(st)); }catch(e){} };
  W.prototype.current = function(){
    var prev=this.load(), order=[], state={};
    if(this.grid){ [].forEach.call(this.grid.children,function(w){ var id=w.getAttribute("data-wz"); if(!id)return;
      order.push(id); state[id]={span:parseInt(w.getAttribute("data-span"),10)||12, collapsed:w.getAttribute("data-collapsed")==="1", hidden:w.style.display==="none"}; }); }
    return { order:order, state:state, layouts:prev.layouts||[], active:prev.active||null };
  };

  // ---- discover panels ----
  W.prototype.discover = function(){
    var c = document.querySelector(this.cfg.container); if(!c) return false;
    this.container = c;
    var panels = [];
    if (this.cfg.mode === "selectors") {
      (this.cfg.panels||[]).forEach(function(p){
        var node = document.querySelector(p.selector);
        if(node) panels.push({ id:p.id||slug(p.title), title:p.title||"Widget", span:p.span, node:node });
      });
    } else {
      // heading mode: group each heading + following siblings until next heading
      var hs = this.cfg.heading || "h2";
      var kids = [].slice.call(c.children);
      var cur = null;
      kids.forEach(function(el){
        if (el.matches && el.matches(hs)) {
          cur = { id:slug(el.textContent), title:(el.textContent||"").trim(), nodes:[el] };
          panels.push(cur);
        } else if (cur) { cur.nodes.push(el); }
      });
    }
    this._raw = panels;
    return panels.length > 0;
  };

  W.prototype.build = function(){
    if (!this.discover()) return;
    if (this.container.getAttribute("data-wz-init")==="1") return;
    this.container.setAttribute("data-wz-init","1");
    css();
    var self=this, st=this.load();
    var order = (st.order&&st.order.length)? st.order.slice() : this._raw.map(function(p){return p.id;});
    this._raw.forEach(function(p){ if(order.indexOf(p.id)<0) order.push(p.id); });
    var pstate = st.state||{};

    var bar=document.createElement("div"); bar.className="wz-bar";
    bar.innerHTML =
      '<div style="position:relative"><button class="wz-btn wz-add">\uFF0B Add Widget <span style="color:var(--muted,#93a1b3);font-size:10px">\u25be</span></button><div class="wz-pop wz-add-pop"></div></div>'+
      '<div style="position:relative"><button class="wz-btn wz-lay">\u25a6 Layouts <span style="color:var(--muted,#93a1b3);font-size:10px">\u25be</span></button><div class="wz-pop wz-lay-pop"></div></div>'+
      '<span style="font-size:11px;color:var(--muted,#93a1b3);margin-left:4px">Drag \u2630 to reorder \u00b7 auto-saves &amp; restores on reload</span>';
    var grid=document.createElement("div"); grid.className="wz-grid";
    this.grid=grid; this.bar=bar;
    var anchor = this.cfg.after ? this.container.querySelector(this.cfg.after) : null;
    if (anchor && anchor.parentNode === this.container) {
      this.container.insertBefore(bar, anchor.nextSibling);
      this.container.insertBefore(grid, bar.nextSibling);
    } else {
      this.container.insertBefore(bar, this.container.firstChild);
      this.container.insertBefore(grid, bar.nextSibling);
    }

    var byId={}; this._raw.forEach(function(p){ byId[p.id]=p; });
    order.forEach(function(id){
      var p=byId[id]; if(!p) return;
      var w=document.createElement("div"); w.className="wz-w"; w.setAttribute("data-wz",id);
      var span=(pstate[id]&&pstate[id].span)||p.span||self.defaultSpan; w.setAttribute("data-span",span); w.style.gridColumn="span "+span;
      var head=document.createElement("div"); head.className="wz-head"; head.setAttribute("draggable","true");
      head.innerHTML='<span class="wz-grip">\u2630</span><span class="wz-title">'+p.title+'</span>'+
        '<span class="wz-ctrls"><span data-act="collapse" title="Collapse">\u25be</span><span data-act="resize" title="Resize">\u2922</span><span data-act="remove" title="Remove">\u00d7</span></span>';
      var body=document.createElement("div"); body.className="wz-body";
      // move nodes into body
      if (p.node) { p.node.parentNode.removeChild(p.node); body.appendChild(p.node); }
      else { p.nodes.forEach(function(n){ n.parentNode && n.parentNode.removeChild(n); body.appendChild(n); });
             var h=body.querySelector(self.cfg.heading||"h2"); if(h) h.style.display="none"; }
      w.appendChild(head); w.appendChild(body); grid.appendChild(w);

      if(pstate[id]&&pstate[id].collapsed){ w.setAttribute("data-collapsed","1"); body.style.display="none"; head.querySelector('[data-act=collapse]').textContent="\u25b8"; }
      if(pstate[id]&&pstate[id].hidden){ w.style.display="none"; }

      head.addEventListener("click",function(ev){
        var act=ev.target.getAttribute("data-act"); if(!act)return;
        if(act==="collapse"){ var col=w.getAttribute("data-collapsed")==="1"; w.setAttribute("data-collapsed",col?"0":"1"); body.style.display=col?"":"none"; ev.target.textContent=col?"\u25be":"\u25b8"; }
        else if(act==="resize"){ var cur=parseInt(w.getAttribute("data-span"),10)||12; var nx=self.spans[(self.spans.indexOf(cur)+1)%self.spans.length]; w.setAttribute("data-span",nx); w.style.gridColumn="span "+nx; }
        else if(act==="remove"){ w.style.display="none"; }
        self.save({active:null}); self.renderAdd(); self._resize();
      });
      head.addEventListener("dragstart",function(e){ head.classList.add("drag"); try{e.dataTransfer.setData("text/plain",id);}catch(x){} });
      head.addEventListener("dragend",function(){ head.classList.remove("drag"); self.save({active:null}); });
      w.addEventListener("dragover",function(e){ e.preventDefault(); });
      w.addEventListener("drop",function(e){ e.preventDefault(); var dh=grid.querySelector(".wz-head.drag"); if(!dh)return; var dw=dh.parentNode; if(dw===w)return;
        var kids=[].slice.call(grid.children); if(kids.indexOf(dw)<kids.indexOf(w)) grid.insertBefore(dw,w.nextSibling); else grid.insertBefore(dw,w);
        self.save({active:null}); self._resize(); });
    });
    order.forEach(function(id){ var w=grid.querySelector('[data-wz="'+id+'"]'); if(w) grid.appendChild(w); });

    this.wireBar(); this.renderAdd(); this.renderLay();
    setTimeout(function(){ self._resize(); },200);
  };

  W.prototype._resize = function(){
    if(window.Plotly){ var self=this; this.grid && [].forEach.call(this.grid.querySelectorAll(".js-plotly-plot,[id]"),function(el){ try{ window.Plotly.Plots.resize(el);}catch(e){} }); }
    window.dispatchEvent(new Event("resize"));
  };

  W.prototype.wireBar = function(){
    var self=this, b=this.bar;
    var add=b.querySelector(".wz-add"), addP=b.querySelector(".wz-add-pop"), lay=b.querySelector(".wz-lay"), layP=b.querySelector(".wz-lay-pop");
    add.addEventListener("click",function(e){ e.stopPropagation(); layP.classList.remove("open"); addP.classList.toggle("open"); });
    lay.addEventListener("click",function(e){ e.stopPropagation(); addP.classList.remove("open"); layP.classList.toggle("open"); });
    document.addEventListener("click",function(){ addP.classList.remove("open"); layP.classList.remove("open"); });
    addP.addEventListener("click",function(e){e.stopPropagation();}); layP.addEventListener("click",function(e){e.stopPropagation();});
  };

  W.prototype.renderAdd = function(){
    var self=this, pop=this.bar.querySelector(".wz-add-pop"), grid=this.grid;
    var hidden=[].filter.call(grid.children,function(w){ return w.style.display==="none"; });
    var html='<div class="muted">Add a widget</div>';
    if(!hidden.length) html+='<div style="padding:10px;color:var(--muted,#93a1b3);font-size:12px">All widgets shown \uD83C\uDF89</div>';
    hidden.forEach(function(w){ var t=w.querySelector(".wz-title").textContent; html+='<div class="item" data-add="'+w.getAttribute("data-wz")+'">'+t+'<span style="margin-left:auto;color:var(--accent,#5aa2ff)">\uFF0B</span></div>'; });
    pop.innerHTML=html;
    [].forEach.call(pop.querySelectorAll("[data-add]"),function(it){ it.addEventListener("click",function(){ var w=grid.querySelector('[data-wz="'+it.getAttribute("data-add")+'"]'); if(w)w.style.display=""; pop.classList.remove("open"); self.save({active:null}); self.renderAdd(); self._resize(); }); });
  };

  W.prototype.renderLay = function(){
    var self=this, pop=this.bar.querySelector(".wz-lay-pop"), st=this.load(), layouts=st.layouts||[];
    var html='<div class="muted">Saved layouts</div><div style="display:flex;gap:6px;margin:2px 4px 10px"><input class="wz-input wz-lname" placeholder="Layout name\u2026" style="flex:1"/><button class="wz-btn wz-lsave" style="padding:7px 10px">\uD83D\uDCBE</button></div>';
    if(!layouts.length) html+='<div style="padding:2px 6px 6px;color:var(--muted,#93a1b3);font-size:12px">No saved layouts yet</div>';
    html+='<div style="display:flex;flex-direction:column;gap:6px">';
    layouts.forEach(function(l){ var a=st.active===l.id; html+='<div class="wz-chip" style="'+(a?"border-color:var(--accent,#5aa2ff)":"")+'"><span class="nm" data-load="'+l.id+'">'+(a?"\u25cf ":"")+l.name+'</span><span class="del" data-del="'+l.id+'">\u00d7</span></div>'; });
    html+='</div>'; pop.innerHTML=html;
    pop.querySelector(".wz-lsave").addEventListener("click",function(){ var nm=(pop.querySelector(".wz-lname").value||"").trim()||("Layout "+(layouts.length+1)); var snap=self.current(); var id="ly"+Date.now(); var next=(self.load().layouts||[]).slice(); next.push({id:id,name:nm,order:snap.order,state:snap.state}); self.save({layouts:next,active:id}); self.renderLay(); });
    [].forEach.call(pop.querySelectorAll("[data-load]"),function(n){ n.addEventListener("click",function(){ self.apply(n.getAttribute("data-load")); }); });
    [].forEach.call(pop.querySelectorAll("[data-del]"),function(n){ n.addEventListener("click",function(){ var id=n.getAttribute("data-del"); var next=(self.load().layouts||[]).filter(function(l){return l.id!==id;}); var s2=self.load(); self.save({layouts:next,active:s2.active===id?null:s2.active}); self.renderLay(); }); });
  };

  W.prototype.apply = function(id){
    var self=this, st=this.load(), l=(st.layouts||[]).filter(function(x){return x.id===id;})[0]; if(!l)return; var grid=this.grid;
    l.order.forEach(function(wid){ var w=grid.querySelector('[data-wz="'+wid+'"]'); if(w)grid.appendChild(w); });
    Object.keys(l.state).forEach(function(wid){ var w=grid.querySelector('[data-wz="'+wid+'"]'); if(!w)return; var s=l.state[wid];
      w.setAttribute("data-span",s.span); w.style.gridColumn="span "+s.span; w.style.display=s.hidden?"none":"";
      w.setAttribute("data-collapsed",s.collapsed?"1":"0"); var body=w.querySelector(".wz-body"), cb=w.querySelector('[data-act=collapse]');
      if(body)body.style.display=s.collapsed?"none":""; if(cb)cb.textContent=s.collapsed?"\u25b8":"\u25be"; });
    this.save({active:id}); this.renderAdd(); this.renderLay(); this._resize();
  };

  var api = {
    init: function(cfg){
      var w=new W(cfg);
      function boot(){ if(document.querySelector(cfg.container)){ w.build(); if(w.container&&w.container.getAttribute("data-wz-init")==="1") return true; } return false; }
      if(!boot()){ var t=0, iv=setInterval(function(){ t++; if(boot()||t>40) clearInterval(iv); },250); }
      return w;
    }
  };
  root.Widgetizer = api;
})(window);
