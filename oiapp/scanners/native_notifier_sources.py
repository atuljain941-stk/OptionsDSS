"""Native Signal Notifier adapters for the institutional and reversal scanners."""
from __future__ import annotations
import json
from concurrent.futures import as_completed
from typing import Any, Dict, List

def _payload(source: Dict[str, Any], defaults: Dict[str, Any]) -> Dict[str, Any]:
    try:
        saved=json.loads(source.get("query_text") or "{}")
    except (TypeError, ValueError):
        saved={}
    return {**defaults, **(saved if isinstance(saved, dict) else {})}

def run_native_source(source: Dict[str, Any], dry_run: bool=False) -> Dict[str, Any]:
    from .signal_notifier import _format_message, _already_alerted_today, _log_alert
    from ..services.telegram_alerts import send_telegram_message, telegram_configured
    kind=source["kind"]; watchlist_id=source.get("watchlist_id")
    try: watchlist_id=int(watchlist_id)
    except (TypeError, ValueError): return {"ok":False,"error":"Select a watchlist for this source."}
    if kind=="institutional_confluence":
        from .institutional_confluence import _watchlist_symbols, _evaluate_symbol
        defaults={"direction":"both","sector_regime":"score","mtf_confluence":"filter","price_structure":"score","options_positioning":"score","volatility":"score","earnings_risk":"filter","min_earnings_days":30,"volatility_dte":30,"min_confluences":3}
        evaluator=_evaluate_symbol
    elif kind=="systematic_reversal":
        from .reversal_scanner import _symbols as _watchlist_symbols, _evaluate as evaluator
        defaults={"direction":"both","acceleration":"score","streak":"score","bollinger":"score","volume":"score","structure":"score","rsi_extreme":"score","iv_fade":"score","sr_location":"score","earnings_risk":"filter","min_earnings_days":30,"volatility_dte":30}
    else: return {"ok":False,"error":f"Unsupported native scanner: {kind}"}
    symbols=_watchlist_symbols(watchlist_id)
    if not symbols: return {"ok":False,"error":"Selected watchlist has no symbols."}
    payload=_payload(source, defaults); minimum=float(source.get("min_score") or payload.get("min_score") or 0)
    from ..services.task_executor import get_background_executor
    ex=get_background_executor(); futures={ex.submit(evaluator,s,payload):s for s in symbols}; rows=[]
    for future in as_completed(futures):
        try:
            row=future.result()
            if row and row.get("included") and float(row.get("score") or 0)>=minimum: rows.append(row)
        except Exception: pass
    rows.sort(key=lambda r:float(r.get("score") or 0),reverse=True)
    sent=[]; skipped=[]; can_send=telegram_configured() and not dry_run
    for row in rows:
        symbol=row["symbol"]
        if _already_alerted_today(symbol): skipped.append(symbol); continue
        direction=str(row.get("direction") or "").upper()
        stages=row.get("stages") or {}
        reasons=[f"{name}: {stage.get('reason','')}" for name,stage in stages.items() if stage.get("score",0)>0][:4]
        opp={"trade_type":"SCAN","grade":"A" if row.get("score",0)>=minimum+3 else "B","score":round(float(row.get("score") or 0)),"price":row.get("price"),"pros":reasons,"rationale":json.dumps({"direction":direction,"stages":stages,"support":row.get("support"),"resistance":row.get("resistance"),"walls":row.get("walls"),"iv_context":row.get("iv_context")},default=str)}
        msg=_format_message(symbol,"SCANNER",opp,source_label=source.get("label") or kind)
        ok=False
        if can_send: ok=bool(send_telegram_message(msg).get("ok"))
        if not dry_run: _log_alert(symbol,"SCANNER",opp,ok,source_id=source.get("id"),source_kind=kind,source_label=source.get("label") or kind,message=msg)
        sent.append({"symbol":symbol,"direction":row.get("direction"),"score":row.get("score"),"max_score":row.get("max_score"),"telegram_ok":ok,"message":msg})
    return {"ok":True,"scanned":len(symbols),"candidates":len(rows),"sent":len(sent),"skipped_duplicate":len(skipped),"alerts":sent,"telegram_configured":telegram_configured(),"dry_run":dry_run}
