"""Multi-Timeframe Alignment Scanner.  Concrete saved-chain trade selection lives here."""
import sqlite3
from datetime import date
from ..config import DB_PATH
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
def _chain_trade(row, setup):
 """Use only the newest stored chain and real leg prices/Greeks."""
 con=sqlite3.connect(DB_PATH,timeout=20);con.row_factory=sqlite3.Row
 try:
  stamp=con.execute("select max(fetch_ts) from options where symbol=?",(row['symbol'],)).fetchone()[0]
  if not stamp:return None,['No saved option chain']
  rows=[dict(x) for x in con.execute("select * from options where symbol=? and fetch_ts=? and oi>0",(row['symbol'],stamp))]
 finally: con.close()
 p=float((row.get('metrics') or {}).get('Close') or row.get('price') or 0);bull=setup in ('Trending Bull','MRT Long');typ='P' if bull else 'C'
 def dte(x):
  try:return (date.fromisoformat(str(x['expiration'])[:10])-date.today()).days
  except:return -1
 def px(x):
  b,a,l,q=[float(x.get(k) or 0) for k in ('bid','ask','last','price')];return (b+a)/2 if b>0 and a>0 else l or q
 legs=[x for x in rows if str(x.get('type','')).upper().startswith(typ) and 14<=dte(x)<=45 and px(x)>0]
 for short in sorted([x for x in legs if (float(x['strike'])<p if bull else float(x['strike'])>p) and .12<=abs(float(x.get('delta') or 0))<=.42],key=lambda x:abs(abs(float(x.get('delta') or 0))-.25)):
  long=[x for x in legs if x['expiration']==short['expiration'] and (float(x['strike'])<float(short['strike']) if bull else float(x['strike'])>float(short['strike']))]
  if not long:continue
  buy=min(long,key=lambda x:abs(abs(float(x['strike'])-float(short['strike']))-5));credit=px(short)-px(buy);width=abs(float(short['strike'])-float(buy['strike']));loss=width-credit
  if credit>0 and loss>0 and credit/loss>=.6:return {'recommendation':'Put Credit Vertical' if bull else 'Call Credit Vertical','expiry':str(short['expiration'])[:10],'dte':dte(short),'legs':f"Sell {short['strike']}{typ} / Buy {buy['strike']}{typ}",'max_profit':round(credit*100,2),'max_loss':round(loss*100,2),'breakevens':[round(float(short['strike'])-credit if bull else float(short['strike'])+credit,2)],'rr':round(credit/loss,2),'pop_proxy':round((1-abs(float(short.get('delta') or 0)))*100,1),'iv':float(short.get('iv') or 0),'delta':float(short.get('delta') or 0)},[]
 return None,['No liquid defined-risk legs meeting RR floor']

def _trade(row):
 m=row.get("metrics",{}); p=float(m.get("Close") or row.get("price") or 0); call=float(m.get("Call Wall") or 0); put=float(m.get("Put Wall") or 0); r=float(m.get("Major Resistance") or 0); s=float(m.get("Major Support") or 0); e=float(m.get("Close / EMA13") or 1); q=float(m.get("RSI Diff 90") or 0); names=" ".join(x["key"] for x in row["scenarios"])
 up=(call or r)>p and ((call-p)/p if call else (r-p)/p)<.025; down=(put or s)<p and ((p-put)/p if put else (p-s)/p)<.025
 if "align_continuation" in names: typ="Trending Bull" if e>=1 else "Trending Bear"
 elif up and down: typ="MRT Range";
 elif q>=10 or e>=1.06: typ="MRT Short"
 elif q<=-10 or e<=.94: typ="MRT Long"
 else: typ="Signal only"
 rec,flags=_chain_trade(row,typ) if typ not in ('MRT Range','Signal only') else (None,[])
 if not rec:rec={'recommendation':'Signal only','expiry':None,'dte':None,'legs':'—','max_profit':None,'max_loss':None,'breakevens':[],'rr':None,'pop_proxy':None,'iv':None,'delta':None}
 score=min(100,50+10*sum([bool(up),bool(down),abs(q)>=10,abs(e-1)>=.04])+(10 if rec['recommendation']!='Signal only' else 0))
 rec.update({'setup':typ,'score':score,'flags':flags,'comment':f"{typ} · walls {abs(call-put)/p*100:.1f}% apart" if p and call and put else f"{typ} · saved-chain check"})
 row['trade']=rec

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


@mtf_scanner_bp.route("/options")
def options_route():
    """UI metadata; keeps the MTF scenario picker independent of a scan."""
    return jsonify({"scenarios":[{"key":key, "label":value["label"]} for key, value in SCENARIOS.items()], "timeframes":TIMEFRAME_OPTIONS})


@mtf_scanner_bp.route("/mw-run", methods=["POST"])
def mw_run_route():
    p = request.get_json(force=True) or {}
    watchlist_id = p.get("watchlist_id")
    side = str(p.get("pattern") or "both").lower()
    if not watchlist_id: return jsonify({"error":"watchlist_id required"}), 400
    timeframe = str(p.get("timeframe") or "1d")
    lookback = max(20, min(120, int(p.get("lookback") or 40)))
    tolerance = max(.25, min(8, float(p.get("tolerance_pct") or 2)))
    # M/W definition intentionally uses the Scanner Builder touch/bounce
    # primitives: a second/third structural test and a recent rejection/bounce.
    checks = []
    if side in ("m", "both"):
        checks.append(("M Top", f'TouchCount(Resistance(60,"{timeframe}"),{tolerance},60,"{timeframe}") >= 2 and lookback(BounceOffSwingHigh({tolerance},60,2,2,"{timeframe}"),3) and close < ema5'))
    if side in ("w", "both"):
        checks.append(("W Bottom", f'TouchCount(Support(60,"{timeframe}"),{tolerance},60,"{timeframe}") >= 2 and lookback(BounceOffSwingLow({tolerance},60,2,2,"{timeframe}"),3) and close > ema5'))
    results = []
    for label, query in checks:
        with current_app.test_client() as c:
            data = c.post("/scanner-builder/api/run", json={"query_text":query,"watchlist_id":watchlist_id,"result_columns":_cols(timeframe)}).get_json() or {}
        for row in data.get("results", []):
            item={"symbol":row.get("symbol"),"pattern":label,"price":row.get("price"),"metrics":row.get("_result_columns") or {},"scenarios":[{"key":"mw","label":label}]}
            _trade(item); results.append(item)
    return jsonify({"results":results,"pattern":side,"timeframe":timeframe})
