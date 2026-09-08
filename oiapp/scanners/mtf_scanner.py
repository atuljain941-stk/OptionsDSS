"""Multi-Timeframe Alignment Scanner."""
from flask import Blueprint, jsonify, request, current_app

mtf_scanner_bp = Blueprint("mtf_scanner_bp", __name__, url_prefix="/mtf-scanner")
TIMEFRAME_OPTIONS = ["1m","1w","1d","4h","2h","1h"]
_TF_STEP_UP = {"1h":"4h","2h":"1d","4h":"1d","1d":"1w","1w":"1m","1m":"1m"}
SCENARIOS={"misalign_rejection":{"label":"Counter-trend"},"align_continuation":{"label":"Continuation"},"ltf_base_mtf_relevant":{"label":"Base"}}

def _build_query(k,h,l,b,t,m,unwind=True,pct=3):
    if k=="misalign_rejection":
      gate=f" and oi_change_pct <= -{pct}" if unwind else ""
      return f'(RegSlopeDeg(close,{b},"{h}")>{t} and RegSlopeDeg(close,{m},"{l}")<-.25{gate}) or (RegSlopeDeg(close,{b},"{h}")<-{t} and RegSlopeDeg(close,{m},"{l}")>.25{gate})'
    if k=="align_continuation": return f'(RegSlopeDeg(close,{b},"{h}")>{t} and RegSlopeDeg(close,{m},"{l}")>.25) or (RegSlopeDeg(close,{b},"{h}")<-{t} and RegSlopeDeg(close,{m},"{l}")<-.25)'
    return f'SqueezeOn(20,2.0,20,10,1.5,"{l}") and VolumeDryup(20,"{l}")<70 and ATRCompression(14,"{_TF_STEP_UP.get(l,l)}")<80'

def _cols(l): return [{"expr":"close","label":"Close"},{"expr":f"close[{l}] / ema13[{l}]","label":"Close / EMA13"},{"expr":f"ema13[{l}] / ema50[{l}]","label":"EMA13 / EMA50"},{"expr":f'rsidiff90(90,"{l}")',"label":"RSI Diff 90"},{"expr":f'Support(60,"{l}")',"label":"Major Support"},{"expr":f'Resistance(60,"{l}")',"label":"Major Resistance"},{"expr":"PutWallStrike()","label":"Put Wall"},{"expr":"CallWallStrike()","label":"Call Wall"}]
def _trade(row):
 m=row.get("metrics",{}); p=float(m.get("Close") or row.get("price") or 0); call=float(m.get("Call Wall") or 0); put=float(m.get("Put Wall") or 0); r=float(m.get("Major Resistance") or 0); s=float(m.get("Major Support") or 0); e=float(m.get("Close / EMA13") or 1); q=float(m.get("RSI Diff 90") or 0); names=" ".join(x["key"] for x in row["scenarios"])
 up=(call or r)>p and ((call-p)/p if call else (r-p)/p)<.025; down=(put or s)<p and ((p-put)/p if put else (p-s)/p)<.025
 if "align_continuation" in names: typ="Trending Bull" if e>=1 else "Trending Bear"
 elif up and down: typ="MRT Range";
 elif q>=10 or e>=1.06: typ="MRT Short"
 elif q<=-10 or e<=.94: typ="MRT Long"
 else: typ="Signal only"
 rec="Iron Condor" if typ=="MRT Range" else ("Call Credit Vertical" if typ=="MRT Short" else ("Put Credit Vertical" if typ=="MRT Long" else typ))
 row["trade"]={"setup":typ,"recommendation":rec,"score":min(100,50+10*sum([bool(up),bool(down),abs(q)>=10,abs(e-1)>=.04])),"comment":f"{typ} · call wall {call or '—'} · put wall {put or '—'}"}

def run_mtf_scan(watchlist_id,keys,h,l,b=10,t=3,m=3,unwind=True,pct=3):
 out={}; cols=_cols(l)
 for k in keys:
  with current_app.test_client() as c: data=(c.post("/scanner-builder/api/run",json={"query_text":_build_query(k,h,l,b,t,m,unwind,pct),"watchlist_id":watchlist_id,"result_columns":cols}).get_json() or {})
  for r in data.get("results",[]):
   x=out.setdefault(r["symbol"],{"symbol":r["symbol"],"price":r.get("price"),"metrics":r.get("_result_columns") or {},"scenarios":[]});x["scenarios"].append({"key":k,"label":SCENARIOS[k]["label"]})
 for x in out.values(): _trade(x)
 return {"results":list(out.values()),"result_columns":cols}
@mtf_scanner_bp.route("/run",methods=["POST"])
def run_route():
 p=request.get_json(force=True) or {}; keys=p.get("scenarios") or []; keys=list(SCENARIOS) if keys=="all" or keys==["all"] else keys
 if not p.get("watchlist_id") or not keys:return jsonify({"error":"watchlist_id and scenarios required"}),400
 try:return jsonify(run_mtf_scan(p["watchlist_id"],keys,str(p.get("htf") or "1m"),str(p.get("ltf") or "1d"),int(p.get("trend_bars") or 10),float(p.get("trend_threshold_deg") or 3),max(2,min(10,int(p.get("maturity_bars") or 3))),bool(p.get("require_oi_unwind",True)),float(p.get("min_oi_unwind_pct") or 3)))
 except Exception as e:return jsonify({"error":str(e)}),500
