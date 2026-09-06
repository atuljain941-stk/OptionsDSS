# scanners/symbol_screener.py
"""
Multi-symbol screener — pure DB, no yfinance for OI data.
Spot prices fetched from yfinance cache (_spot_cache).

Key fix: every snapshot aggregates with SUM+GROUP BY and pins to the
latest date per symbol (not a single global date) so symbols updated on
different days are still correctly compared.
"""
import sqlite3
from pathlib import Path
from datetime import date

from ._spot_cache import get_spot

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH


def _conn():
    return sqlite3.connect(DB_PATH)


def _two_global_dates():
    """Two most recent distinct dates across the whole DB."""
    con = _conn()
    rows = con.execute(
        "SELECT DISTINCT date FROM options ORDER BY date DESC LIMIT 2"
    ).fetchall()
    con.close()
    return (rows[0][0], rows[1][0]) if len(rows) >= 2 else (None, None)


def _future_exps_set():
    today = date.today().strftime("%Y-%m-%d")
    con = _conn()
    rows = con.execute(
        "SELECT DISTINCT expiration FROM options WHERE expiration >= ?", (today,)
    ).fetchall()
    con.close()
    return {r[0] for r in rows}


def _snap_for_date(dt: str, future_exps: set) -> dict:
    """
    Aggregate put/call OI+vol per symbol for a specific date.
    Only future expirations. SUM+GROUP BY to deduplicate same-day rows.
    Returns {symbol: {put_oi, call_oi, put_vol, call_vol}}.
    """
    if not future_exps:
        return {}
    ph = ",".join("?" * len(future_exps))
    con = _conn()
    rows = con.execute(f"""
        SELECT symbol,
               SUM(CASE WHEN type='put'  THEN oi     ELSE 0 END) AS put_oi,
               SUM(CASE WHEN type='call' THEN oi     ELSE 0 END) AS call_oi,
               SUM(CASE WHEN type='put'  THEN volume ELSE 0 END) AS put_vol,
               SUM(CASE WHEN type='call' THEN volume ELSE 0 END) AS call_vol
        FROM options
        WHERE date=? AND expiration IN ({ph})
        GROUP BY symbol
    """, [dt] + list(future_exps)).fetchall()
    con.close()
    return {r[0]: {"put_oi":r[1],"call_oi":r[2],"put_vol":r[3],"call_vol":r[4]} for r in rows}


def _avg_daily_vol_per_symbol() -> dict:
    """Average total volume per symbol across all stored dates."""
    con = _conn()
    rows = con.execute("""
        SELECT symbol, date, SUM(volume) AS day_vol
        FROM options
        GROUP BY symbol, date
    """).fetchall()
    con.close()
    from collections import defaultdict
    dv = defaultdict(list)
    for sym, dt, vol in rows:
        dv[sym].append(vol or 0)
    return {sym: sum(vs)/len(vs) for sym, vs in dv.items() if vs}




def _fetch_price_data_batch(symbols, days=100):
    """
    Fetch OHLCV for multiple symbols using yfinance with thread pool.
    Returns {symbol: DataFrame} with Close, Volume columns.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import yfinance as yf
    period = f"{min(days+20, 120)}d"
    result = {}

    def _fetch_one(sym):
        try:
            h = yf.Ticker(sym).history(period=period)
            return sym, h if (h is not None and len(h) >= 20) else None
        except:
            return sym, None

    ex = ThreadPoolExecutor(max_workers=10)
    try:
        from ..services.bounded_wait import bounded_as_completed
        futures = {ex.submit(_fetch_one, s): s for s in symbols}
        for fut, s in bounded_as_completed(futures, timeout=60,
                on_timeout=lambda ks: print(f"[symbol_screener] {len(ks)} symbol(s) timed out: {ks[:20]}")):
            if fut is None:
                continue
            sym, h = fut.result()
            if h is not None:
                result[sym] = h
    finally:
        ex.shutdown(wait=False)
    return result


def _compute_price_metrics(hist_map, vol_days=20, mom_days=90):
    """
    Compute per-symbol price metrics from yfinance history.
    Returns {symbol: {vol_ratio, pct_chg, mom_ratio}}
    """
    import numpy as np
    result = {}
    for sym, h in hist_map.items():
        try:
            closes = h["Close"].values.astype(float)
            vols   = h["Volume"].values.astype(float)
            if len(closes) < 20: continue

            # Today's % change
            pct_chg = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 else 0.0

            # Volume ratio: today / avg(vol, 20 days)
            avg_vol_20 = float(np.mean(vols[-21:-1]))  # 20 days excluding today
            today_vol  = float(vols[-1])
            vol_ratio  = round(today_vol / max(1, avg_vol_20), 2)

            # Momentum ratio: |%chg| / EMA(|%chg|, 90)
            abs_chgs = [abs(closes[i]/closes[i-1]-1)*100 for i in range(1, len(closes))]
            if len(abs_chgs) >= 10:
                alpha = 2.0 / (mom_days + 1)
                ema = abs_chgs[0]
                for v in abs_chgs[1:]: ema = alpha*v + (1-alpha)*ema
                today_abs = abs(pct_chg)
                mom_ratio = round(today_abs / max(0.001, ema), 2)
            else:
                mom_ratio = None

            result[sym] = {
                "pct_chg":   pct_chg,
                "vol_ratio": vol_ratio,
                "mom_ratio": mom_ratio,
            }
        except: pass
    return result



def run_price_screener(mode: str = "high_volume", vol_ratio: float = 1.2,
                       mom_x: float = 2.0, symbols: list = None):
    """
    Price/volume/momentum screener using real yfinance data.
    Works independently of the options DB — can screen any symbol.
    Modes: high_volume, large_momentum
    """
    if not symbols:
        con = _conn()
        symbols = [r[0] for r in con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()]
        con.close()

    if not symbols:
        return []

    hist_map = _fetch_price_data_batch(symbols, days=100)
    metrics  = _compute_price_metrics(hist_map, vol_days=20, mom_days=90)

    results = []
    for sym, pm in metrics.items():
        vol_r   = pm.get("vol_ratio", 0)
        mom_r   = pm.get("mom_ratio")
        pct_chg = pm.get("pct_chg", 0)

        if mode == "high_volume":
            if vol_r < vol_ratio:
                continue
            signal_score = min(100, int(vol_r * 30))
        else:  # large_momentum
            if mom_r is None or mom_r < mom_x:
                continue
            signal_score = min(100, int(mom_r * 20))

        results.append({
            "symbol":       sym,
            "signal_score": signal_score,
            "vol_ratio":    round(vol_r, 2),
            "mom_ratio":    mom_r,
            "pct_chg":      round(pct_chg, 2),
            "vol_vs_avg":   round((vol_r - 1) * 100, 1) if vol_r else None,
            "spot":         None,
            "pcr_chg":      None,
            "oi_chg_pct":   None,
        })

    results.sort(key=lambda x: x["signal_score"], reverse=True)
    return results


def run_screener(mode: str = "pcr_change", threshold: float = 10.0, vol_ratio: float = 1.2, mom_x: float = 2.0):
    today, prev = _two_global_dates()
    if not today or not prev:
        return []

    future_exps = _future_exps_set()
    if not future_exps:
        return []

    now_snap  = _snap_for_date(today, future_exps)
    prev_snap = _snap_for_date(prev,  future_exps)
    # For price-based modes, fetch real OHLCV data via yfinance
    _price_metrics = {}
    if mode in ("high_volume", "large_momentum"):
        _all_syms_list = list(set(now_snap) & set(prev_snap))
        _hist_map = _fetch_price_data_batch(_all_syms_list, days=100)
        _price_metrics = _compute_price_metrics(_hist_map, vol_days=20, mom_days=90)
    avg_vols = {}  # no longer used for these modes

    # fetch real spots for all matched symbols in one batch
    all_syms = set(now_snap) & set(prev_snap)
    spot_cache = {s: get_spot(s) for s in all_syms}

    results = []
    for sym in all_syms:
        n = now_snap[sym]
        p = prev_snap[sym]

        put_oi    = n["put_oi"]   or 0
        call_oi   = n["call_oi"]  or 0
        put_vol   = n["put_vol"]  or 0
        call_vol  = n["call_vol"] or 0
        tot_vol   = put_vol + call_vol

        prev_put_oi  = p["put_oi"]  or 0
        prev_call_oi = p["call_oi"] or 0

        pcr_now  = round(put_oi       / call_oi,       3) if call_oi       else None
        pcr_prev = round(prev_put_oi  / prev_call_oi,  3) if prev_call_oi  else None
        pcr_chg  = round((pcr_now - pcr_prev) / pcr_prev * 100, 1) if (pcr_now and pcr_prev) else None

        tot_oi_now  = put_oi + call_oi
        tot_oi_prev = prev_put_oi + prev_call_oi
        oi_chg_pct  = round((tot_oi_now - tot_oi_prev) / tot_oi_prev * 100, 1) if tot_oi_prev else None

        avg_vol    = avg_vols.get(sym, 0)
        vol_vs_avg = round((tot_vol / avg_vol - 1) * 100, 1) if avg_vol else None

        if mode == "pcr_change":
            if pcr_chg is None or abs(pcr_chg) < threshold:
                continue
            signal_score = min(100, int(abs(pcr_chg)))
        elif mode == "oi_change":
            if oi_chg_pct is None or abs(oi_chg_pct) < threshold:
                continue
            signal_score = min(100, int(abs(oi_chg_pct)))
        else:  # high_volume
            if vol_vs_avg is None or vol_vs_avg < threshold:
                continue
            signal_score = min(100, int(vol_vs_avg / 2))

        spot = spot_cache.get(sym)

        # Price-based metrics (real stock data)
        _pm = _price_metrics.get(sym, {})
        mom_ratio_val = _pm.get("mom_ratio")
        real_vol_ratio = _pm.get("vol_ratio")
        pct_chg_today  = _pm.get("pct_chg")

        results.append({
            "symbol":        sym,
            "spot":          round(spot, 2) if spot else None,
            "pcr_now":       pcr_now,
            "pcr_prev":      pcr_prev,
            "pcr_chg":       pcr_chg,
            "put_oi":        put_oi,
            "call_oi":       call_oi,
            "put_vol":       put_vol,
            "call_vol":      call_vol,
            "tot_vol":       tot_vol,
            "tot_oi":        tot_oi_now,
            "oi_chg_pct":    oi_chg_pct,
            "vol_vs_avg":    vol_vs_avg,
            "signal_score":  signal_score,
            "snapshot_date": today,
        })

    results.sort(key=lambda x: -x["signal_score"])
    return results
