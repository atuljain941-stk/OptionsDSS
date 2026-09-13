"""Systematic capitulation / reversal scanner built from measurable rules."""
from __future__ import annotations
import concurrent.futures, math, sqlite3
from typing import Any, Dict, List, Optional
from flask import Blueprint, jsonify, render_template, request
from ..config import DB_PATH
from ..services.technical_snapshot import get_technical_snapshot

systematic_reversal_bp = Blueprint("systematic_reversal", __name__, url_prefix="/systematic-reversal")
MODES = {"off", "score", "filter"}

def _conn():
    con = sqlite3.connect(DB_PATH, timeout=15); con.row_factory = sqlite3.Row; return con
def _num(value: Any) -> Optional[float]:
    try: return float(value) if value is not None else None
    except (TypeError, ValueError): return None
def _mode(payload: Dict[str, Any], name: str) -> str:
    value = str(payload.get(name, "score")).lower(); return value if value in MODES else "score"
def _symbols(watchlist_id: int) -> List[str]:
    con = _conn()
    try:
        return [str(r["symbol"]).upper() for r in con.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",(watchlist_id,)).fetchall() if r["symbol"]]
    finally: con.close()
def _bars(symbol: str) -> List[Dict[str, Any]]:
    con = _conn()
    try: rows=con.execute("SELECT date,high,low,close,volume FROM price_cache WHERE symbol=? ORDER BY date DESC LIMIT 80",(symbol,)).fetchall()
    except sqlite3.Error: rows=[]
    finally: con.close()
    out=[]
    for r in reversed(rows):
        row={k:_num(r[k]) for k in ("high","low","close","volume")}
        if all(row[k] is not None for k in ("high","low","close")): out.append({"date":str(r["date"])[:10],**row})
    return out
def _mean(v): return sum(v)/len(v) if v else 0.0
def _std(v):
    m=_mean(v); return math.sqrt(sum((x-m)**2 for x in v)/len(v)) if len(v)>1 else 0.0
def _streak(c):
    if len(c)<2 or c[-1]==c[-2]: return "neutral",0
    side="bull" if c[-1]>c[-2] else "bear"; n=0
    for i in range(len(c)-1,0,-1):
        if (side=="bull" and c[i]>c[i-1]) or (side=="bear" and c[i]<c[i-1]): n+=1
        else: break
    return side,n

def _evaluate(symbol: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    bars=_bars(symbol)
    if len(bars)<25: return {"symbol":symbol,"included":False,"score":0,"max_score":0,"error":"Insufficient daily history"}
    c=[r["close"] for r in bars]; p=c[-1]; move,streak=_streak(c)
    requested=str(payload.get("direction","both")).lower()
    target=requested if requested in {"bull","bear"} else ("bear" if move=="bull" else "bull" if move=="bear" else "neutral")
    move="bear" if target=="bull" else "bull" if target=="bear" else move
    rets=[abs(c[i]/c[i-1]-1) for i in range(1,len(c))]; rate=rets[-1]/max(_mean(rets[-21:-1]),.0001)
    acceleration={"status":"ok","pass":rate>=2,"score":2 if rate>=3 else 1 if rate>=2 else 0,"reason":f"Last move {rets[-1]*100:.2f}% · {rate:.1f}× 20-day average"}
    streak_stage={"status":"ok","pass":streak>=3,"score":2 if streak>=5 else 1 if streak>=3 else 0,"reason":f"{streak} consecutive {move} closes"}
    basis=_mean(c[-20:]); band=2*_std(c[-20:]); upper,lower=basis+band,basis-band
    ext=(p-upper)/max(band,.0001) if move=="bull" else (lower-p)/max(band,.0001)
    boll={"status":"ok","pass":ext>=0,"score":2 if ext>=.5 else 1 if ext>=0 else 0,"reason":f"Price {p:.2f}; band {upper if move=='bull' else lower:.2f}"}
    vols=[r["volume"] for r in bars if r["volume"] is not None]; avg=_mean(vols[-21:-1]) if len(vols)>=21 else 0; ratio=(bars[-1]["volume"] or 0)/avg if avg else None
    volume={"status":"ok" if ratio is not None else "unavailable","pass":ratio is not None and ratio>=1.5,"score":2 if ratio is not None and ratio>=3 else 1 if ratio is not None and ratio>=1.5 else 0,"reason":f"Volume {ratio:.1f}× average" if ratio is not None else "Volume unavailable"}
    dirs=[1 if c[i]>c[i-1] else -1 if c[i]<c[i-1] else 0 for i in range(1,len(c))][-12:]; sign=1 if move=="bull" else -1
    legs=sum(1 for i in range(1,len(dirs)) if dirs[i]==sign and dirs[i-1]!=sign); flats=sum(x==0 for x in dirs)/max(len(dirs),1)
    structure={"status":"ok","pass":legs>=2 and flats<=.25,"score":2 if legs>=3 and flats<=.17 else 1 if legs>=2 and flats<=.25 else 0,"reason":f"{legs} directional legs; {flats*100:.0f}% flat bars"}
    snap=get_technical_snapshot(symbol,"1d") or {}; diff=_num(snap.get("rsidiff90")); support,resistance=_num(snap.get("sr_support")),_num(snap.get("sr_resistance"))
    if diff is None: rsi={"status":"unavailable","pass":True,"score":0,"reason":"RSIDiff90 unavailable"}
    else:
        good=diff<=-20 if target=="bull" else diff>=20
        rsi={"status":"ok","pass":good,"score":2 if good and abs(diff)>=30 else 1 if good else 0,"reason":f"RSIDiff90 {diff:+.1f}; threshold {'≤ -20' if target=='bull' else '≥ +20'}"}
    level=support if target=="bull" else resistance; name="support" if target=="bull" else "resistance"; near=level is not None and (p<=level*1.02 if target=="bull" else p>=level*.98)
    sr={"status":"ok" if level is not None else "unavailable","pass":near if level is not None else True,"score":1 if near else 0,"reason":f"Price {p:.2f}; {name} {level:.2f}" if level is not None else f"{name.title()} unavailable"}
    stages={"acceleration":acceleration,"streak":streak_stage,"bollinger":boll,"volume":volume,"structure":structure,"rsi_extreme":rsi,"sr_location":sr}
    included=target!="neutral"
    for name,stage in stages.items():
        if _mode(payload,name)=="filter" and stage["status"]!="unavailable" and not stage["pass"]: included=False
    enabled=[name for name in stages if _mode(payload,name)!="off"]; total=sum(stages[n]["score"] for n in enabled); maximum=sum(1 if n=="sr_location" else 2 for n in enabled)
    return {"symbol":symbol,"direction":target,"move_direction":move,"price":p,"score":total,"max_score":maximum,"included":included,"stages":stages,"rsidiff90":diff,"support":support,"resistance":resistance}

@systematic_reversal_bp.route("/")
def page(): return render_template("systematic_reversal.html")
@systematic_reversal_bp.route("/api/run",methods=["POST"])
def run():
    payload=request.get_json(silent=True) or {}
    try: symbols=_symbols(int(payload.get("watchlist_id")))
    except (TypeError,ValueError): return jsonify({"error":"Choose a watchlist."}),400
    if not symbols: return jsonify({"error":"Selected watchlist has no symbols."}),400
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8,len(symbols))) as pool: rows=[f.result() for f in concurrent.futures.as_completed([pool.submit(_evaluate,s,payload) for s in symbols])]
    minimum=max(0,float(payload.get("min_score",0) or 0)); results=[r for r in rows if r.get("included") and r.get("score",0)>=minimum]; results.sort(key=lambda r:r["score"],reverse=True)
    return jsonify({"results":results,"scanned":len(symbols),"min_score":minimum})
