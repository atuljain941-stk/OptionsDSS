"""
trade_planner.py — Single-Symbol Deep Scan
Runs all scanners fresh for one symbol. OI Buildup is DB-only.
All other scanners run live yfinance. Conviction score adapts to available data.
"""
import sqlite3
from pathlib import Path
from datetime import datetime, date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Blueprint, jsonify, request

planner_bp = Blueprint("planner_bp", __name__, url_prefix="/planner")
DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")   # prevent DB locks from concurrent threads
    c.execute("PRAGMA busy_timeout=5000")
    return c


# ── 1. OI Buildup (DB only — no live fetch) ────────────────────────────────
def _run_oi_buildup(sym):
    try:
        con = _conn()
        cutoff = (date.today() - timedelta(days=45)).isoformat()
        rows = con.execute(
            "SELECT date,"
            " SUM(CASE WHEN type=\'call\' THEN oi ELSE 0 END) call_oi,"
            " SUM(CASE WHEN type=\'put\'  THEN oi ELSE 0 END) put_oi,"
            " SUM(CASE WHEN type=\'call\' THEN volume ELSE 0 END) call_vol,"
            " SUM(CASE WHEN type=\'put\'  THEN volume ELSE 0 END) put_vol"
            " FROM options WHERE symbol=? AND date>=? AND expiration>=date"
            " GROUP BY date ORDER BY date",
            (sym, cutoff)).fetchall()
        con.close()
        if len(rows) < 3:
            return {"status": "no_db_data", "skipped": True,
                    "detail": f"No OI data in DB for {sym} (fetch OI for this watchlist first)"}
        first, last = rows[0], rows[-1]
        t0 = (first["call_oi"] or 0) + (first["put_oi"] or 0)
        t1 = (last["call_oi"]  or 0) + (last["put_oi"]  or 0)
        oi_growth = round((t1 - t0) / max(1, t0) * 100, 1)
        call_pct  = round((last["call_oi"] or 0) / max(1, t1) * 100, 1)
        pcr       = round((last["put_oi"] or 0) / max(1, last["call_oi"] or 1), 3)
        score = 0
        if oi_growth > 50: score += 4
        elif oi_growth > 20: score += 3
        elif oi_growth > 5: score += 2
        elif oi_growth > 0: score += 1
        if call_pct > 60: score += 2
        elif call_pct > 50: score += 1
        if pcr < 0.6: score += 2
        elif pcr < 0.8: score += 1
        bias = "Bullish" if call_pct > 55 and pcr < 0.8 else "Bearish" if call_pct < 45 and pcr > 1.2 else "Neutral"
        return {"status": "found", "score": min(10, score), "max": 10,
                "oi_growth_pct": oi_growth, "call_pct": call_pct, "pcr": pcr, "bias": bias,
                "days_of_data": len(rows),
                "detail": f"{bias} | OI {oi_growth:+.1f}% over {len(rows)}d | calls {call_pct}% | PCR {pcr}"}
    except Exception as e:
        return {"status": "error", "skipped": True, "detail": str(e)[:80]}


# ── 2. Regime (live yfinance) ──────────────────────────────────────────────
def _run_regime(sym):
    try:
        import yfinance as yf, pandas as pd, numpy as np
        h = yf.Ticker(sym).history(period="6mo")
        if h is None or len(h) < 30:
            return {"status": "error", "detail": "Not enough price history"}
        closes = h["Close"].values.astype(float)
        s = pd.Series(closes)
        def ema(x, n): return x.ewm(span=n, adjust=False).mean()
        ema20 = float(ema(s, 20).iloc[-1])
        ema50 = float(ema(s, 50).iloc[-1])
        ema200= float(ema(s, 200).iloc[-1]) if len(closes) >= 200 else None
        price = float(closes[-1])
        # 14-day RSI
        delta = s.diff(); gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
        rs = gain.ewm(span=14,adjust=False).mean() / loss.ewm(span=14,adjust=False).mean().replace(0,1e-9)
        rsi = float((100 - 100/(1+rs)).iloc[-1])
        # ADX proxy: directional movement
        hi = h["High"].values.astype(float); lo = h["Low"].values.astype(float)
        p20_hi = float(np.mean(hi[-20:])); p20_lo = float(np.mean(lo[-20:]))
        range20 = round((p20_hi - p20_lo) / p20_lo * 100, 1)
        p5_chg  = round((price - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 else 0
        # Determine regime
        above_ema20  = price > ema20
        above_ema50  = price > ema50
        above_ema200 = price > ema200 if ema200 else None
        if above_ema20 and above_ema50 and rsi > 55:
            regime = "Trending Bullish"; score = 9 if (above_ema200 or above_ema200 is None) else 7
        elif above_ema50 and rsi > 50:
            regime = "Mild Bullish"; score = 6
        elif not above_ema20 and not above_ema50 and rsi < 45:
            regime = "Trending Bearish"; score = 2
        elif not above_ema50 and rsi < 50:
            regime = "Mild Bearish"; score = 3
        else:
            regime = "Consolidating"; score = 5
        bias = "Bullish" if score >= 6 else "Bearish" if score <= 3 else "Neutral"
        signals = []
        if above_ema20: signals.append("Above EMA20")
        if above_ema50: signals.append("Above EMA50")
        if above_ema200 is True: signals.append("Above EMA200")
        if rsi > 60: signals.append(f"RSI strong {rsi:.0f}")
        elif rsi < 40: signals.append(f"RSI weak {rsi:.0f}")
        return {"status": "found", "score": score, "max": 9, "regime": regime, "bias": bias,
                "rsi": round(rsi, 1), "price": round(price, 2),
                "ema20": round(ema20, 2), "ema50": round(ema50, 2),
                "ema200": round(ema200, 2) if ema200 else None,
                "above_ema20": above_ema20, "above_ema50": above_ema50, "above_ema200": above_ema200,
                "price_5d_chg": p5_chg, "range20": range20, "signals": signals,
                "detail": f"{regime} ({bias}) | RSI {rsi:.0f} | 5d {p5_chg:+.1f}%"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:100]}


# ── 3. RSI MTF (live yfinance) ─────────────────────────────────────────────
def _run_rsi_mtf(sym):
    try:
        from .rsi_mtf_scanner import _scan, load_params
        p = load_params()
        r = _scan(sym, p)
        if r is None:
            # No divergence setup — but still show RSI state using raw yfinance
            import yfinance as yf, pandas as pd
            h_d = yf.Ticker(sym).history(period="3mo")
            if h_d is None or len(h_d) < 20:
                return {"status": "no_data", "detail": "Could not fetch price data"}
            s = pd.Series(h_d["Close"].values.astype(float))
            delta = s.diff(); gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
            rsi_d = float((100-100/(1+(gain.ewm(span=14,adjust=False).mean()/
                           loss.ewm(span=14,adjust=False).mean().replace(0,1e-9)))).iloc[-1])
            rsi_ema = float(pd.Series([rsi_d]*len(s)).ewm(span=90,adjust=False).mean().iloc[-1])
            rsi_delta = round(rsi_d - rsi_ema, 1)
            return {"status": "no_setup", "score": 0, "max": 8,
                    "setup": "No Divergence", "daily_rsi": round(rsi_d, 1),
                    "daily_rsi_delta": rsi_delta,
                    "detail": f"No MTF divergence | Daily RSI {rsi_d:.0f} (delta vs EMA90: {rsi_delta:+.1f})"}
        setup   = r.get("setup", "")
        d_delta = r.get("daily_rsi_delta", 0) or 0
        i_delta = r.get("intra_rsi_delta", 0) or 0
        score   = 8 if "bull" in str(setup).lower() else 6 if "bear" in str(setup).lower() else 2
        return {"status": "found", "score": score, "max": 8, "setup": setup,
                "daily_rsi_delta": round(d_delta, 1), "intra_rsi_delta": round(i_delta, 1),
                "detail": f"{setup} | daily RSI delta {d_delta:+.1f} | intra RSI delta {i_delta:+.1f}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:100]}


# ── 4. S/R Breakout + Proximity (live yfinance) ────────────────────────────
def _run_sr(sym):
    try:
        from .sr_breakout_scanner import analyze_sr_breakouts, scan_sr_proximity
        br       = analyze_sr_breakouts(sym) or {}
        breakouts = br.get("breakouts") or br.get("results") or []
        channels  = br.get("channels") or []
        prox      = scan_sr_proximity([sym], proximity_pct=5.0) or []
        sym_prox  = next((r for r in prox if r.get("symbol") == sym), None)
        nearby    = sym_prox.get("all_nearby", []) if sym_prox else []
        score = 0
        if breakouts:       score = 8
        elif nearby:        score = 4 + min(3, len(nearby))
        elif channels:      score = 3
        parts = []
        if breakouts:
            b = breakouts[0]
            parts.append(f"Breaking {b.get('direction','?')} — strength {b.get('strength','?')} at ${b.get('level',b.get('hi','?'))}")
        if nearby:
            n = nearby[0]
            parts.append(f"{len(nearby)} S/R levels | {n.get('sr_type','?')} @ ${n.get('mid','?'):.2f} ({n.get('dist_pct',0):.1f}% away)")
        elif channels:
            parts.append(f"{len(channels)} S/R channels identified")
        return {"status": "found" if (breakouts or nearby or channels) else "no_data",
                "score": score, "max": 8,
                "breakouts": len(breakouts), "nearby_levels": len(nearby),
                "channels": len(channels),
                "top_breakout": breakouts[0] if breakouts else None,
                "nearest_level": nearby[0] if nearby else None,
                "detail": " | ".join(parts) if parts else "No S/R levels found"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:100]}


# ── 5. Trend Exhaustion (live yfinance) ───────────────────────────────────
def _run_trend_exhaustion(sym):
    try:
        from .edge_factors import trend_exhaustion_snapshot, merge_edge_fields
        r = trend_exhaustion_snapshot(sym)
        if r.get("status") not in ("found", "watch"):
            return {"status": "no_setup", "score": 0, "max": 100, "detail": "No exhaustion setup"}
        score = r.get("score", 0)
        detail = r.get("detail") or ""
        result = {
            "status": "found" if r.get("status") == "found" else "watch",
            "score": score,
            "max": 100,
            "direction": r.get("direction"),
            "trade_bias": r.get("contrarian_trade"),
            "price": r.get("price"),
            "rsi": r.get("rsi"),
            "atr_pct": r.get("atr_pct"),
            "trend_age": r.get("trend_age"),
            "stretch_atr": r.get("stretch_atr"),
            "stretch_pct": r.get("stretch_pct"),
            "climax_vol": r.get("climax_vol"),
            "macd_hist": r.get("macd_hist"),
            "macd_roll": r.get("macd_roll"),
            "detail": detail,
            "notes": r.get("notes", []),
        }
        return merge_edge_fields(result, sym)
    except Exception as e:
        return {"status": "error", "detail": str(e)[:100]}


# ── 6. Institutional (live yfinance) ──────────────────────────────────────
def _run_institutional(sym):
    try:
        import yfinance as yf, pandas as pd, numpy as np
        h = yf.Ticker(sym).history(period="1y")
        if h is None or len(h) < 60:
            return {"status": "error", "detail": "Not enough price history"}
        closes = h["Close"].values.astype(float)
        highs  = h["High"].values.astype(float)
        lows   = h["Low"].values.astype(float)
        vols   = h["Volume"].values.astype(float)
        price  = float(closes[-1])
        s = pd.Series(closes)
        def ema(x, n): return x.ewm(span=n, adjust=False).mean()
        ema20  = float(ema(s, 20).iloc[-1])
        ema50  = float(ema(s, 50).iloc[-1])
        ema200 = float(ema(s, 200).iloc[-1]) if len(closes) >= 200 else float(ema(s, min(len(closes)-1, 200)).iloc[-1])
        # RSI
        delta = s.diff(); gain = delta.clip(lower=0); loss = (-delta).clip(lower=0)
        rsi = float((100-100/(1+(gain.ewm(span=14,adjust=False).mean()/
                     loss.ewm(span=14,adjust=False).mean().replace(0,1e-9)))).iloc[-1])
        # Vol surge
        avg_vol_20 = float(np.mean(vols[-25:-5]))
        vol_surge  = round(float(vols[-1]) / max(1, avg_vol_20), 2)
        # 5d change
        p5d = round((price - closes[-6]) / closes[-6] * 100, 2) if len(closes) >= 6 else 0
        # Base analysis
        base_high = float(max(highs[-35:-5]))
        base_low  = float(min(lows[-35:-5]))
        base_mid  = (base_high + base_low) / 2
        base_tight = round((base_high - base_low) / max(0.01, base_mid) * 100, 1)
        is_above_base = price > base_high
        # 52W
        hi52 = float(max(highs[-252:])) if len(highs) >= 252 else float(max(highs))
        pct_from_52wh = round((price - hi52) / hi52 * 100, 1)
        # Score
        score = 0
        if price > ema200: score += 2
        if price > ema50:  score += 1.5
        if price > ema20:  score += 1
        if vol_surge > 2:  score += 2
        elif vol_surge > 1.5: score += 1.5
        elif vol_surge > 1.2: score += 1
        if rsi >= 50 and rsi <= 75: score += 1.5
        elif rsi >= 45: score += 0.75
        if base_tight < 10: score += 2
        elif base_tight < 20: score += 1.5
        elif base_tight < 30: score += 1
        if is_above_base: score += 1
        score = round(min(10, score), 1)
        if is_above_base and vol_surge > 1.5: btype = "Base Breakout"
        elif price > ema50 and p5d > 2: btype = "EMA50 Reclaim"
        elif abs(price - ema200) / ema200 < 0.03: btype = "200 EMA Hold"
        elif vol_surge > 2: btype = "Vol Surge"
        else: btype = "Watching"
        return {"status": "found", "score": score, "max": 10,
                "breakout_type": btype, "vol_surge": vol_surge, "base_tight_pct": base_tight,
                "rsi": round(rsi, 1), "price_5d_chg": p5d,
                "ema_stack": {"above20": price > ema20, "above50": price > ema50, "above200": price > ema200},
                "is_above_base": is_above_base, "pct_from_52wh": pct_from_52wh,
                "detail": f"{btype} | score {score} | vol {vol_surge:.1f}x | RSI {rsi:.0f} | base {base_tight:.1f}% | 5d {p5d:+.1f}%"}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:100]}


# ── 6. Earnings (DB cache with live fallback) ──────────────────────────────
def _run_earnings(sym):
    try:
        con = _conn()
        con.execute("""CREATE TABLE IF NOT EXISTS earnings_calendar (
            symbol TEXT PRIMARY KEY, next_earn_date TEXT, last_earn_date TEXT,
            last_eps_actual REAL, last_eps_estimate REAL, last_surprise_pct REAL,
            earn_reaction_pct REAL, surprise_streak INTEGER DEFAULT 0, fetch_date TEXT)""")
        row = con.execute("SELECT * FROM earnings_calendar WHERE symbol=?", (sym,)).fetchone()
        con.close()
        needs_fetch = True
        data = {}
        if row:
            fd = row["fetch_date"] or ""
            if fd >= (date.today() - timedelta(days=7)).isoformat():
                needs_fetch = False
                data = dict(row)
        if needs_fetch:
            from .earnings_calendar import _fetch_one_symbol
            fresh = _fetch_one_symbol(sym)
            if fresh:
                data = fresh
                con2 = _conn()
                con2.execute("""INSERT OR REPLACE INTO earnings_calendar
                    (symbol,next_earn_date,last_earn_date,last_eps_actual,last_eps_estimate,
                     last_surprise_pct,earn_reaction_pct,surprise_streak,fetch_date) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (sym, data.get("next_earn_date"), data.get("last_earn_date"),
                     data.get("last_eps_actual"), data.get("last_eps_estimate"),
                     data.get("last_surprise_pct"), data.get("earn_reaction_pct"),
                     data.get("surprise_streak", 0), date.today().isoformat()))
                con2.commit(); con2.close()
        if not data:
            return {"status": "no_data", "detail": "No earnings data available"}
        next_date    = data.get("next_earn_date")
        surprise_pct = data.get("last_surprise_pct")
        reaction_pct = data.get("earn_reaction_pct")
        streak       = int(data.get("surprise_streak") or 0)
        eps_actual   = data.get("last_eps_actual")
        eps_est      = data.get("last_eps_estimate")
        days_to_earn = None
        if next_date:
            try:
                nd = date.fromisoformat(str(next_date)[:10])
                days_to_earn = (nd - date.today()).days
            except: pass
        score = 0; reasons = []
        if surprise_pct is not None:
            if surprise_pct > 10: score += 3; reasons.append(f"Beat EPS {surprise_pct:+.1f}%")
            elif surprise_pct > 0: score += 2; reasons.append(f"Beat EPS {surprise_pct:+.1f}%")
            elif surprise_pct < -5: score -= 1; reasons.append(f"Missed EPS {surprise_pct:.1f}%")
        if streak >= 3: score += 2; reasons.append(f"{streak} consecutive beats")
        elif streak >= 2: score += 1; reasons.append(f"{streak} consecutive beats")
        if reaction_pct and abs(reaction_pct) > 8: score += 2; reasons.append(f"Big mover {abs(reaction_pct):.1f}%")
        elif reaction_pct and abs(reaction_pct) > 4: score += 1
        if days_to_earn is not None:
            if 0 <= days_to_earn <= 7: score += 2
            elif 0 <= days_to_earn <= 21: score += 1
        score = max(0, min(10, score))
        timing = (f"⚠ Earnings in {days_to_earn}d" if days_to_earn is not None and 0 <= days_to_earn <= 7
                  else f"Earnings in {days_to_earn}d" if days_to_earn is not None and days_to_earn > 0
                  else f"Earnings {abs(days_to_earn)}d ago" if days_to_earn is not None and days_to_earn < 0
                  else "Earnings date unknown")
        parts = [timing]
        if surprise_pct is not None: parts.append(f"Surprise {surprise_pct:+.1f}%")
        if streak: parts.append(f"{streak}x beat streak")
        if reaction_pct: parts.append(f"avg move {reaction_pct:+.1f}%")
        return {"status": "found", "score": score, "max": 10, "next_earn_date": str(next_date) if next_date else None,
                "days_to_earnings": days_to_earn, "last_surprise_pct": round(surprise_pct,1) if surprise_pct is not None else None,
                "earn_reaction_pct": round(reaction_pct,1) if reaction_pct is not None else None,
                "surprise_streak": streak, "eps_actual": eps_actual, "eps_estimate": eps_est,
                "reasons": reasons, "detail": " | ".join(parts)}
    except Exception as e:
        return {"status": "error", "detail": str(e)[:100]}


# ── Main scan ──────────────────────────────────────────────────────────────
def run_planner_scan(symbol):
    sym = symbol.upper().strip()
    started = datetime.now()
    tasks = {
        "oi_buildup":    lambda: _run_oi_buildup(sym),
        "regime":        lambda: _run_regime(sym),
        "rsi_mtf":       lambda: _run_rsi_mtf(sym),
        "sr":            lambda: _run_sr(sym),
        "trend_exhaustion": lambda: _run_trend_exhaustion(sym),
        "institutional": lambda: _run_institutional(sym),
        "earnings":      lambda: _run_earnings(sym),
    }
    results = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fn): name for name, fn in tasks.items()}
        for fut in as_completed(futs):
            name = futs[fut]
            try:    results[name] = fut.result()
            except Exception as e: results[name] = {"status": "error", "detail": str(e)[:80]}

    # Conviction: sum scores normalized over scanners that have data
    # OI buildup skipped if no DB data. Others score 0 if no setup (but not skipped).
    total_score = 0.0; total_max = 0.0
    for key, r in results.items():
        if r.get("skipped") or r.get("status") in ("error",):
            continue    # don't count errors or explicitly skipped
        sc  = r.get("score") or 0
        mx  = r.get("max") or 10
        total_score += sc
        total_max   += mx

    if total_max > 0:
        conviction = round((total_score / total_max) * 10, 1)  # normalize to 0-10
    else:
        conviction = 0.0

    label = ("🔥 Strong" if conviction >= 7 else "✅ Good" if conviction >= 5
             else "👀 Moderate" if conviction >= 3 else "⚠ Weak")
    elapsed = round((datetime.now() - started).total_seconds(), 1)
    scanners_with_data = sum(1 for r in results.values()
                             if not r.get("skipped") and r.get("status") not in ("error",))
    return {
        "symbol":     sym,
        "conviction": {"score": conviction, "label": label,
                       "scanners_scored": scanners_with_data,
                       "raw_score": round(total_score, 1), "raw_max": round(total_max, 1)},
        "scanners":   results,
        "elapsed_s":  elapsed,
        "scanned_at": started.strftime("%Y-%m-%d %H:%M:%S"),
    }


@planner_bp.route("/scan/<symbol>")
def planner_scan(symbol):
    return jsonify(run_planner_scan(symbol))


@planner_bp.route("/symbols")
def get_planner_symbols():
    wl_id = request.args.get("watchlist_id", None, type=int)
    con = _conn()
    if wl_id:
        rows = con.execute("SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",(wl_id,)).fetchall()
    else:
        rows = con.execute("SELECT symbol FROM symbols ORDER BY symbol").fetchall()
    con.close()
    return jsonify({"symbols": [r[0] for r in rows]})
