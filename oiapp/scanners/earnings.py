# oiapp/scanners/earnings.py  v3 — no API key required
"""
Earnings Analysis Engine — fully self-contained, no external AI API needed.
Data sources: yfinance only (free)
- EPS history:        earnings_dates  (est, actual, surprise%)
- Revenue actuals:    quarterly_income_stmt
- Revenue estimates:  revenue_estimate (analyst avg/low/high, yoy growth)
- EPS estimates:      earnings_estimate (analyst avg/low/high)
- Analyst consensus:  info (recommendationKey, targetMeanPrice, etc.)
- Post-earnings move: computed from price history around past earnings dates
Rule-based outlook:   computed from beat rate, rev trend, analyst score,
                      price vs 52w high, margin, surprise streak — no LLM needed
"""
import sqlite3, math
from pathlib import Path
from datetime import date, datetime
from flask import Blueprint, jsonify, request
import yfinance as yf
try:
    from ..services.yf_session import safe_history, safe_calendar, safe_earnings_dates
except Exception:
    safe_history        = lambda sym, period="1y", interval="1d": yf.Ticker(sym).history(period=period, interval=interval)
    safe_calendar       = lambda sym: yf.Ticker(sym).calendar
    safe_earnings_dates = lambda sym: yf.Ticker(sym).earnings_dates

earnings_bp = Blueprint("earnings_bp", __name__, url_prefix="/earnings")

def _save_scan_timestamp(key):
    """Save scan completion time to app_config."""
    try:
        import datetime as _dt, sqlite3 as _sq
        from pathlib import Path as _P
        ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db = str(_P(__file__).resolve().parents[2] / "options_data.db")
        c  = _sq.connect(db)
        c.execute("CREATE TABLE IF NOT EXISTS app_config (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT OR REPLACE INTO app_config VALUES (?,?)", (key, ts))
        c.commit(); c.close()
        return ts
    except: return ""



@earnings_bp.errorhandler(Exception)
def _handle_err(e):
    import traceback
    return __import__("flask").jsonify({"error": str(e), "trace": traceback.format_exc()[-600:], "results": []}), 500

# ETFs and funds that do not report earnings - skip in all earnings scans
_NON_EQUITY = set([
    "SPY","QQQ","IWM","DIA","VOO","VTI","TQQQ","SQQQ","SPXL","SPXS","UPRO",
    "XLE","XLF","XLK","XLV","XLI","XLP","XLU","XLY","XLC","XLRE","XLB",
    "SMH","SOXX","GDX","GDXJ","TLT","IEF","SHY","HYG","LQD","JNK","BND",
    "AGG","BNDX","EMB","GLD","SLV","IAU","USO","UCO","IBIT","FBTC","GBTC",
    "EEM","EFA","VWO","EWJ","FXI","MCHI","EWZ","IVV","VXX","ARKK","ARKG",
    "SOXL","SOXS","UVXY","SVXY","JEPI","JEPQ","XYLD","QYLD","TBT","TMF",
    "TMV","BITO","UUP","RSP","IBB","KRE","KBE","EWG","EWU","VEA","XBI",
    "XHB","ARKW","ARKF","ARKQ","BITB","ETHE","COPX","GDXJ","SCHD","DVY",
    "VYM","TBF","SSO","SDS","QLD","QID","UDOW","PSQ","LABD","LABU",
])

def _is_equity(sym):
    """True if symbol is an individual stock that reports earnings."""
    return sym.upper() not in _NON_EQUITY


DB_PATH = str(Path(__file__).resolve().parents[2] / "options_data.db")


# ── helpers ──────────────────────────────────────────────────────────────────
def _get_symbols_from_db():
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT symbol FROM symbols ORDER BY symbol").fetchall()
        con.close()
        syms = [r[0] for r in rows]
        return syms if syms else [
            "SPY","AAPL","MSFT","AMZN","GOOGL","META","NVDA","TSLA","JPM","V"]
    except:
        return ["SPY","AAPL","MSFT","AMZN","GOOGL","META","NVDA","TSLA","JPM"]


def _get_watchlist_symbols(watchlist_id):
    """Return symbols for a selected watchlist, or None if unavailable."""
    if not watchlist_id:
        return None
    try:
        con = sqlite3.connect(DB_PATH)
        rows = con.execute(
            "SELECT symbol FROM watchlist_symbols WHERE watchlist_id=? ORDER BY symbol",
            (int(watchlist_id),)
        ).fetchall()
        con.close()
        return [r[0] for r in rows] if rows else []
    except:
        return None


def _earn_cache_key(base: str, watchlist_id=None) -> str:
    return f"{base}_{int(watchlist_id)}" if watchlist_id not in (None, "", 0) else base


def _safe(v, dec=2):
    if v is None:
        return None
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, dec)
    except:
        return None


# ── Rule-based analysis (no API key needed) ───────────────────────────────────
def _rule_based_analysis(data: dict) -> dict:
    """
    Derive market outlook, beat streaks, revenue trend, options implication
    purely from the numbers — no LLM required.
    """
    eps_hist   = data.get("eps_history") or []
    rev_hist   = data.get("rev_history") or []
    price_now  = data.get("price_now") or 0
    price_52h  = data.get("price_52h") or price_now
    price_52l  = data.get("price_52l") or price_now
    rec_mean   = data.get("recommend_mean")       # 1=Strong Buy … 5=Strong Sell
    rec_key    = (data.get("recommend_key") or "").lower()
    rev_growth = data.get("revenue_growth") or 0  # % YoY
    eps_growth = data.get("earnings_growth") or 0
    margin     = data.get("profit_margin") or 0
    analyst_t  = data.get("analyst_target") or price_now
    next_rev_avg = data.get("next_rev_est_avg")
    next_rev_yago= data.get("next_rev_yago")
    post_moves = data.get("post_earnings_moves") or []

    # ── Beat / miss streak ─────────────────────────────────────────────────
    surprises = [h.get("surprise_pct") for h in eps_hist
                 if h.get("surprise_pct") is not None]
    beat_flags = [s > 0 for s in surprises]
    beats = sum(beat_flags)
    total = len(beat_flags)
    beat_rate = round(beats / total * 100) if total else None

    streak = 0
    if beat_flags:
        streak_dir = beat_flags[0]
        for f in beat_flags:
            if f == streak_dir:
                streak += 1
            else:
                break
        streak_word = f"{streak} consecutive {'beat' if streak_dir else 'miss'}{'s' if streak > 1 else ''}"
    else:
        streak_word = "insufficient data"

    avg_surprise = round(sum(surprises[:4]) / len(surprises[:4]), 1) if surprises else None

    # ── Revenue trend ──────────────────────────────────────────────────────
    rev_vals = [r.get("revenue") for r in rev_hist if r.get("revenue")]
    if len(rev_vals) >= 3:
        # Compare avg of last 2 quarters vs 2 before that
        recent = sum(rev_vals[:2]) / 2
        older  = sum(rev_vals[2:4]) / 2 if len(rev_vals) >= 4 else rev_vals[2]
        rev_chg = (recent - older) / older * 100 if older else 0
        if rev_chg > 8:   rev_trend = "ACCELERATING"
        elif rev_chg > 2: rev_trend = "STABLE"
        elif rev_chg > -3: rev_trend = "DECELERATING"
        else:              rev_trend = "DECLINING"
    elif rev_growth > 5:  rev_trend = "ACCELERATING"
    elif rev_growth > 0:  rev_trend = "STABLE"
    elif rev_growth > -5: rev_trend = "DECELERATING"
    else:                  rev_trend = "DECLINING"

    # ── Earnings quality ───────────────────────────────────────────────────
    q_score = 0
    q_reasons = []
    if beat_rate and beat_rate >= 75:
        q_score += 2; q_reasons.append(f"{beat_rate}% EPS beat rate")
    elif beat_rate and beat_rate >= 50:
        q_score += 1; q_reasons.append(f"{beat_rate}% EPS beat rate")
    if avg_surprise and avg_surprise > 5:
        q_score += 2; q_reasons.append(f"avg EPS surprise +{avg_surprise}%")
    elif avg_surprise and avg_surprise > 0:
        q_score += 1
    if rev_trend == "ACCELERATING":
        q_score += 2; q_reasons.append("accelerating revenue")
    elif rev_trend == "STABLE":
        q_score += 1
    if margin and margin > 15:
        q_score += 1; q_reasons.append(f"{round(margin)}% profit margin")

    if q_score >= 5:   eq = "HIGH"
    elif q_score >= 3: eq = "MEDIUM"
    else:              eq = "LOW"

    eq_reason = (", ".join(q_reasons[:3]) if q_reasons
                 else "mixed results based on recent history")

    # ── Market outlook scoring ─────────────────────────────────────────────
    score = 0  # -10 to +10

    # Analyst consensus (1=Strong Buy, 3=Hold, 5=Strong Sell)
    if rec_mean:
        if rec_mean <= 1.5:   score += 3
        elif rec_mean <= 2.2: score += 2
        elif rec_mean <= 2.8: score += 1
        elif rec_mean <= 3.5: score -= 1
        else:                 score -= 2

    # Analyst price target vs current price
    if price_now and analyst_t:
        upside = (analyst_t - price_now) / price_now * 100
        if upside > 20:   score += 2
        elif upside > 10: score += 1
        elif upside > 0:  score += 0
        elif upside > -5: score -= 1
        else:             score -= 2

    # EPS beat streak
    if beat_flags and beat_flags[0]:
        if streak >= 4: score += 2
        elif streak >= 2: score += 1
    elif beat_flags and not beat_flags[0]:
        if streak >= 2: score -= 2
        else: score -= 1

    # Revenue trend
    if rev_trend == "ACCELERATING":  score += 2
    elif rev_trend == "STABLE":       score += 1
    elif rev_trend == "DECELERATING": score -= 1
    elif rev_trend == "DECLINING":    score -= 2

    # Price position vs 52-week range
    if price_52h > price_52l and price_now:
        pct_range = (price_now - price_52l) / (price_52h - price_52l) * 100
        if pct_range > 85:   score -= 1   # near high, caution
        elif pct_range > 60: score += 1
        elif pct_range < 20: score += 1   # beaten down, potential
    else:
        pct_range = 50

    # Revenue growth
    if rev_growth > 15:   score += 1
    elif rev_growth < -5: score -= 1

    # Map score to outlook
    if score >= 6:    outlook = "BULLISH"
    elif score >= 3:  outlook = "MILDLY_BULLISH"
    elif score >= -1: outlook = "NEUTRAL"
    elif score >= -4: outlook = "MILDLY_BEARISH"
    else:             outlook = "BEARISH"

    # ── Outlook reason (data-driven sentences) ────────────────────────────
    reasons = []
    if beat_rate is not None:
        reasons.append(
            f"{symbol_from_data(data)} has beaten EPS estimates in {beat_rate}% of the last "
            f"{total} quarters with an average surprise of "
            f"{'+'if (avg_surprise or 0)>=0 else ''}{avg_surprise}%."
        )
    if analyst_t and price_now:
        upside_str = f"+{upside:.1f}%" if upside >= 0 else f"{upside:.1f}%"
        reasons.append(
            f"Analyst consensus is {rec_key or 'hold'} with a mean price target of "
            f"${analyst_t} ({upside_str} from current ${price_now})."
        )
    if rev_trend:
        rev_desc = {"ACCELERATING":"accelerating","STABLE":"stable",
                    "DECELERATING":"decelerating","DECLINING":"declining"}.get(rev_trend,"")
        reasons.append(
            f"Revenue is {rev_desc} "
            f"(TTM growth {'+' if rev_growth>=0 else ''}{round(rev_growth,1)}%, "
            f"margin {round(margin,1)}%)."
        )
    outlook_reason = " ".join(reasons[:3]) if reasons else \
        "Outlook based on earnings beat rate, analyst consensus, and revenue trend."

    # ── Post-earnings move estimate ────────────────────────────────────────
    if post_moves:
        avg_abs = round(sum(abs(m) for m in post_moves) / len(post_moves), 1)
        max_abs = round(max(abs(m) for m in post_moves), 1)
        move_est = f"±{avg_abs}% avg (max ±{max_abs}% in last {len(post_moves)} quarters)"
    else:
        # Fallback: use ATR-based heuristic from IV proxy
        iv_est = data.get("iv_est_pct") or 30
        move_est = f"±{round(iv_est * 0.15, 1)}–{round(iv_est * 0.25, 1)}% (IV-based estimate)"

    # ── My EPS / Rev estimates (conservative analyst avg with adjustment) ──
    next_eps_avg = data.get("next_eps_est_avg")
    next_rev_avg_v = data.get("next_rev_est_avg")
    my_eps = round(next_eps_avg * (1 + (avg_surprise or 0) / 100 * 0.5), 2) \
        if next_eps_avg and avg_surprise else next_eps_avg
    my_rev = round(next_rev_avg_v * 1.01, 2) if next_rev_avg_v else None

    # ── Options implication (rule-based) ──────────────────────────────────
    dte_str = ""
    nd = data.get("next_earnings_date")
    if nd:
        try:
            dd = (datetime.strptime(nd, "%Y-%m-%d").date() - date.today()).days
            dte_str = f" with {dd} days to earnings"
        except:
            pass

    if outlook in ("BULLISH","MILDLY_BULLISH"):
        opt_impl = (
            f"Bullish setup{dte_str}. Consider bull put spreads below key support "
            f"or long calls/call spreads. IV typically expands into earnings — "
            f"sell premium after the print if vol remains elevated."
        )
    elif outlook in ("BEARISH","MILDLY_BEARISH"):
        opt_impl = (
            f"Bearish setup{dte_str}. Bear call spreads above resistance or "
            f"protective puts on long positions. "
            f"Watch for IV crush post-earnings if holding long premium."
        )
    else:
        opt_impl = (
            f"Neutral setup{dte_str}. Iron condors or short strangles work well "
            f"when IV is elevated heading into earnings. "
            f"Target the OI walls as your short strikes, buy wings 1 ATR further out."
        )

    # ── Key catalysts & risks (derived from data) ─────────────────────────
    catalysts = []
    risks = []

    if beat_flags and beat_flags[0] and streak >= 2:
        catalysts.append(f"{streak}-quarter EPS beat streak — management execution track record")
    if rev_trend == "ACCELERATING":
        catalysts.append("Accelerating revenue growth signals demand momentum")
    if analyst_t and price_now and (analyst_t - price_now) / price_now > 0.1:
        catalysts.append(f"Analyst consensus target ${analyst_t} implies "
                         f"{round((analyst_t-price_now)/price_now*100)}% upside")
    if margin and margin > 20:
        catalysts.append(f"High profit margin ({round(margin)}%) provides earnings buffer")
    if next_rev_avg and next_rev_yago and next_rev_avg > next_rev_yago:
        growth_pct = round((next_rev_avg - next_rev_yago) / next_rev_yago * 100, 1)
        catalysts.append(f"Next quarter revenue est. implies +{growth_pct}% YoY growth")

    if beat_flags and not beat_flags[0]:
        risks.append(f"Recent EPS miss — {streak} consecutive miss(es) raises execution concern")
    if rev_trend in ("DECELERATING","DECLINING"):
        risks.append("Revenue trend decelerating — growth story under pressure")
    if price_now and price_52h and (price_52h - price_now) / price_52h < 0.05:
        risks.append("Stock near 52-week high — limited upside room, elevated sell pressure")
    if rec_mean and rec_mean > 3:
        risks.append(f"Analyst consensus trending negative (mean score {rec_mean:.1f}/5)")
    if margin and margin < 5:
        risks.append(f"Thin profit margin ({round(margin,1)}%) leaves little room for error")
    risks.append("Macro rate/tariff environment could compress multiples broadly")

    return {
        # Beat/miss
        "beat_miss_streak":      streak_word,
        "beat_rate_pct":         beat_rate,
        "avg_eps_surprise_pct":  avg_surprise,
        "earnings_quality":      eq,
        "earnings_quality_reason": eq_reason,
        # Estimates
        "next_eps_my_est":       my_eps,
        "next_eps_direction":    "BEAT" if (my_eps and next_eps_avg and my_eps > next_eps_avg) else "IN_LINE",
        "next_rev_my_est":       my_rev,
        "next_rev_direction":    "BEAT" if (my_rev and next_rev_avg_v and my_rev > next_rev_avg_v) else "IN_LINE",
        # Trends
        "revenue_trend":         rev_trend,
        "margin_trend":          "EXPANDING" if eps_growth > rev_growth else
                                 ("STABLE" if abs(eps_growth-rev_growth)<3 else "CONTRACTING"),
        # Outlook
        "market_outlook":        outlook,
        "market_outlook_horizon":"1-3 months",
        "market_outlook_reason": outlook_reason,
        "outlook_score":         score,
        # Price target
        "price_target_my":       round(analyst_t * 0.95, 2) if analyst_t else None,
        "upside_downside_pct":   round((analyst_t*0.95 - price_now)/price_now*100, 1)
                                 if analyst_t and price_now else None,
        # Options
        "post_earnings_move_est": move_est,
        "options_implication":    opt_impl,
        # Catalysts / risks
        "key_catalysts": catalysts[:4] or ["Monitor next earnings print for confirmation"],
        "key_risks":     risks[:4],
        # Summary
        "summary": _build_summary(data, outlook, streak_word, rev_trend,
                                  avg_surprise, beat_rate, analyst_t, price_now),
        "data_source": "yfinance + rule-based analysis (no AI API required)",
    }


def symbol_from_data(data):
    return data.get("company_name") or data.get("symbol") or "This company"


def _build_summary(data, outlook, streak_word, rev_trend,
                   avg_surprise, beat_rate, analyst_t, price_now):
    sym   = data.get("company_name") or data.get("symbol")
    sec   = data.get("sector") or ""
    nd    = data.get("next_earnings_date") or "upcoming"
    rec   = (data.get("recommend_key") or "hold").lower()

    parts = []
    parts.append(
        f"{sym} ({sec}) has delivered {streak_word} "
        f"{'with an average EPS surprise of +'+str(avg_surprise)+'%' if avg_surprise and avg_surprise>0 else 'recently'}."
    )
    parts.append(
        f"Revenue growth is {rev_trend.lower()}, "
        f"with TTM revenue of ${_safe(data.get('revenue_ttm'))}B "
        f"and {round(data.get('profit_margin') or 0, 1)}% profit margin."
    )
    if analyst_t and price_now:
        upside = round((analyst_t - price_now) / price_now * 100, 1)
        parts.append(
            f"Analyst consensus is {rec} with a mean price target of "
            f"${analyst_t} ({'+' if upside>=0 else ''}{upside}% upside)."
        )
    parts.append(
        f"The overall setup into {nd} earnings looks "
        f"{'constructive' if 'BULL' in outlook else 'cautious' if 'BEAR' in outlook else 'balanced'} "
        f"based on earnings quality, analyst positioning, and revenue trajectory."
    )
    return " ".join(parts)


# ── Post-earnings move calculation from price history ─────────────────────────
def _post_earnings_moves(symbol: str, eps_dates: list) -> list:
    """Calculate actual % move on day after each earnings print."""
    if not eps_dates:
        return []
    try:
        ph = yf.Ticker(symbol).history(period="2y")
        if ph.empty:
            return []
        moves = []
        for item in eps_dates[:6]:
            try:
                dt = datetime.strptime(item["date"], "%Y-%m-%d").date()
                # Find close on earnings day and next trading day
                ph_dates = [d.date() for d in ph.index]
                if dt not in ph_dates:
                    continue
                idx = ph_dates.index(dt)
                if idx + 1 >= len(ph_dates):
                    continue
                c0 = float(ph.iloc[idx]["Close"])
                c1 = float(ph.iloc[idx + 1]["Close"])
                if c0 > 0:
                    moves.append(round((c1 - c0) / c0 * 100, 2))
            except:
                continue
        return moves
    except:
        return []


# ── Main data fetch ───────────────────────────────────────────────────────────
def _fetch_earnings_data(symbol: str) -> dict:
    try:
        tk   = yf.Ticker(symbol)
        info = tk.info or {}

        # ── 1. EPS history (earnings_dates) ───────────────────────────────
        eps_history = []
        try:
            ed = safe_earnings_dates(symbol)
            if ed is not None and not ed.empty:
                actuals = ed[ed["Reported EPS"].notna()].copy()
                for idx, row in actuals.head(8).iterrows():
                    try:
                        dt = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
                    except:
                        dt = str(idx)[:10]
                    eps_history.append({
                        "date":         dt,
                        "eps_expected": _safe(row.get("EPS Estimate")),
                        "eps_actual":   _safe(row.get("Reported EPS")),
                        "surprise_pct": _safe(row.get("Surprise(%)") or row.get("Surprise (%)")),
                    })
        except Exception as e:
            print(f"[eps_history] {e}")

        # ── 2. Calendar + forward estimates ───────────────────────────────
        next_date = next_eps_low = next_eps_high = next_eps_avg = None
        try:
            cal = safe_calendar(symbol)
            if isinstance(cal, dict):
                nd = cal.get("Earnings Date") or cal.get("earningsDate")
                if nd:
                    next_date = str(list(nd)[0])[:10] \
                        if (hasattr(nd, "__iter__") and not isinstance(nd, str)) \
                        else str(nd)[:10]
                next_eps_low  = _safe(cal.get("Earnings Low")  or cal.get("earningsLow"))
                next_eps_high = _safe(cal.get("Earnings High") or cal.get("earningsHigh"))
                if next_eps_low and next_eps_high:
                    next_eps_avg = round((next_eps_low + next_eps_high) / 2, 2)
        except Exception as e:
            print(f"[calendar] {e}")

        # revenue_estimate and earnings_estimate
        next_rev_avg = next_rev_low = next_rev_high = next_rev_yago = None
        try:
            re_df = tk.revenue_estimate
            ee_df = tk.earnings_estimate
            if re_df is not None and not re_df.empty and "0q" in re_df.index:
                r0 = re_df.loc["0q"]
                next_rev_avg  = _safe((r0.get("avg")  or 0) / 1e9)
                next_rev_low  = _safe((r0.get("low")  or 0) / 1e9)
                next_rev_high = _safe((r0.get("high") or 0) / 1e9)
                next_rev_yago = _safe((r0.get("yearAgoRevenue") or 0) / 1e9)
            if ee_df is not None and not ee_df.empty and "0q" in ee_df.index and not next_eps_avg:
                e0 = ee_df.loc["0q"]
                next_eps_avg  = _safe(e0.get("avg"))
                next_eps_low  = _safe(e0.get("low"))
                next_eps_high = _safe(e0.get("high"))
        except Exception as e:
            print(f"[estimates] {e}")

        # ── 3. Revenue actuals (quarterly_income_stmt) ─────────────────────
        rev_history = []
        try:
            qi = tk.quarterly_income_stmt
            if qi is not None and not qi.empty:
                rev_label = next(
                    (l for l in qi.index if "Revenue" in str(l) and "Cost" not in str(l)), None)
                if rev_label:
                    rev_row = qi.loc[rev_label]
                    for col in list(rev_row.index)[:8]:
                        v = rev_row[col]
                        try:
                            col_dt = col.strftime("%Y-%m-%d") \
                                if hasattr(col, "strftime") else str(col)[:10]
                        except:
                            col_dt = str(col)[:10]
                        fv = _safe(float(v) / 1e9, 2) if v is not None else None
                        rev_history.append({"period": col_dt, "revenue": fv})
        except Exception as e:
            print(f"[revenue actuals] {e}")

        # ── 4. Analyst recommendations ──────────────────────────────────────
        latest_rec = None
        try:
            recs = tk.recommendations
            if recs is not None and not recs.empty:
                last = recs.iloc[-1]
                latest_rec = {
                    "firm":   str(last.get("Firm",   "") or ""),
                    "action": str(last.get("Action", "") or ""),
                    "grade":  str(last.get("To Grade","") or ""),
                }
        except:
            pass

        # ── 5. Price + 52-week range ────────────────────────────────────────
        price_now = prev_close = price_52h = price_52l = day_chg_pct = None
        try:
            ph = tk.history(period="1y")
            if not ph.empty:
                price_now  = float(ph["Close"].iloc[-1])
                prev_close = float(ph["Close"].iloc[-2]) if len(ph) > 1 else None
                price_52h  = float(ph["High"].max())
                price_52l  = float(ph["Low"].min())
                if prev_close and prev_close > 0:
                    day_chg_pct = round((price_now - prev_close) / prev_close * 100, 2)
        except:
            pass

        # Post-earnings moves
        post_moves = _post_earnings_moves(symbol, eps_history)

        # IV proxy (30-day realized vol)
        iv_est_pct = None
        try:
            ph2 = tk.history(period="60d")
            if not ph2.empty and len(ph2) >= 20:
                closes = ph2["Close"].tolist()
                rets = [math.log(closes[i]/closes[i-1])
                        for i in range(1, len(closes)) if closes[i-1] > 0]
                iv_est_pct = round(math.sqrt(sum(x**2 for x in rets) /
                                             len(rets) * 252) * 100, 1) if rets else None
        except:
            pass

        result = {
            "symbol":           symbol,
            "company_name":     info.get("longName", symbol),
            "sector":           info.get("sector", ""),
            "industry":         info.get("industry", ""),
            "market_cap":       _safe((info.get("marketCap") or 0) / 1e9),
            "pe_ratio":         _safe(info.get("forwardPE") or info.get("trailingPE")),
            "eps_ttm":          _safe(info.get("trailingEps")),
            "revenue_ttm":      _safe((info.get("totalRevenue") or 0) / 1e9),
            "profit_margin":    _safe((info.get("profitMargins") or 0) * 100),
            "revenue_growth":   _safe((info.get("revenueGrowth") or 0) * 100),
            "earnings_growth":  _safe((info.get("earningsGrowth") or 0) * 100),
            "analyst_target":   _safe(info.get("targetMeanPrice")),
            "analyst_low":      _safe(info.get("targetLowPrice")),
            "analyst_high":     _safe(info.get("targetHighPrice")),
            "recommend_mean":   _safe(info.get("recommendationMean")),
            "recommend_key":    info.get("recommendationKey", ""),
            "price_now":        _safe(price_now),
            "prev_close":       _safe(prev_close),
            "day_chg_pct":      day_chg_pct,
            "price_52h":        _safe(price_52h),
            "price_52l":        _safe(price_52l),
            "iv_est_pct":       iv_est_pct,
            # Next quarter
            "next_earnings_date":    next_date,
            "next_eps_est_avg":      next_eps_avg,
            "next_eps_est_low":      next_eps_low,
            "next_eps_est_high":     next_eps_high,
            "next_rev_est_avg":      next_rev_avg,
            "next_rev_est_low":      next_rev_low,
            "next_rev_est_high":     next_rev_high,
            "next_rev_yago":         next_rev_yago,
            # History
            "eps_history":           eps_history,
            "rev_history":           rev_history,
            "post_earnings_moves":   post_moves,
            "latest_analyst_action": latest_rec,
        }

        # Run rule-based analysis inline
        result["analysis"] = _rule_based_analysis(result)
        return result

    except Exception as e:
        print(f"[fetch_earnings] {symbol}: {e}")
        return {"symbol": symbol, "error": str(e)}



def _suggest_earnings_strategy(sym_data, spy_regime=None):
    """
    Suggest an options strategy for earnings play.
    Based on: IV, expected move vs historical, direction signal, market regime.
    Returns: strategy name, rationale, score (0-100), estimated PoP, R:R tag.
    """
    import math as _m

    iv_atm      = sym_data.get("iv_atm") or 30
    exp_move    = sym_data.get("exp_move_pct") or 0
    avg_hist    = sym_data.get("avg_hist_move") or 0
    direction   = sym_data.get("direction", "NEUTRAL")
    signal_conf = sym_data.get("signal_conf", 50)
    pcr         = sym_data.get("pcr") or 1.0
    move_5d     = sym_data.get("move_5d") or 0
    hist_bull   = sym_data.get("hist_bull") or 0
    hist_bear   = sym_data.get("hist_bear") or 0
    earn_days   = sym_data.get("earn_days") or 0
    straddle    = sym_data.get("straddle") or 0
    spot        = sym_data.get("spot") or 100

    # IV regime
    iv_high = iv_atm >= 60
    iv_mod  = 30 <= iv_atm < 60
    iv_low  = iv_atm < 30

    # Is the straddle expensive vs historical move?
    overpriced = exp_move > avg_hist * 1.25 if avg_hist > 0 else False
    underpriced= exp_move < avg_hist * 0.75 if avg_hist > 0 else False

    market_bull = spy_regime and "BULL" in (spy_regime or "").upper()
    market_bear = spy_regime and "BEAR" in (spy_regime or "").upper()

    strategies = []

    # ── Strategy 1: Short Straddle / Strangle (if IV very high + options overpriced) ──
    if iv_high and overpriced and abs(move_5d) < 3:
        width_pct = round(exp_move * 0.6, 1)
        pop = min(72, 55 + (iv_atm - 60) * 0.3)
        rr_tag = "1:2" if pop > 65 else "1:1.5"
        strategies.append({
            "strategy": "Short Strangle",
            "timing": "Sell 1-2 days BEFORE earnings",
            "rationale": f"IV {iv_atm:.0f}% is elevated. Straddle (${straddle:.2f}) implies ±{exp_move:.1f}% but hist avg is ±{avg_hist:.1f}%. Sell OTM puts/calls ~{width_pct:.1f}% OTM.",
            "type": "credit",
            "score": min(88, 60 + (iv_atm-60)*0.5),
            "pop": round(pop, 0),
            "rr": rr_tag,
            "risk": "Unlimited risk if large gap — size small.",
        })

    # ── Strategy 2: Iron Condor (if IV high, neutral direction, expensive straddle) ──
    if iv_high and overpriced and direction == "NEUTRAL":
        pop = min(68, 52 + (iv_atm - 60) * 0.2)
        strategies.append({
            "strategy": "Iron Condor",
            "timing": "Sell 2-5 days BEFORE earnings, close after",
            "rationale": f"Neutral signal + high IV. Use wings at ±{round(exp_move*0.8,1)}% (put) and +{round(exp_move*0.8,1)}% (call) from spot.",
            "type": "credit",
            "score": min(82, 55 + (iv_atm-55)*0.4),
            "pop": round(pop, 0),
            "rr": "1:2.5",
            "risk": "Max loss if stock gaps beyond wings.",
        })

    # ── Strategy 3: Long Straddle (if IV low + underpriced vs historical) ──
    if (iv_low or underpriced) and avg_hist > 0 and avg_hist > exp_move * 1.2:
        pop = min(65, 45 + (avg_hist - exp_move) * 2)
        strategies.append({
            "strategy": "Long Straddle",
            "timing": "Buy 3-7 days BEFORE earnings",
            "rationale": f"Options cheap (IV {iv_atm:.0f}%). Historical avg move ±{avg_hist:.1f}% > implied ±{exp_move:.1f}%. Buy ATM straddle, profit if stock moves big.",
            "type": "debit",
            "score": min(78, 45 + (avg_hist - exp_move) * 3),
            "pop": round(pop, 0),
            "rr": f"1:{round(avg_hist/max(0.1,exp_move)*1.2, 1)}",
            "risk": "Lose full premium if stock pins near ATM.",
        })

    # ── Strategy 4: Directional Debit Spread (strong directional signal) ──
    if signal_conf >= 60 and direction != "NEUTRAL" and iv_mod:
        opt_type = "Call" if direction == "BULLISH" else "Put"
        target_move = round(exp_move * 1.2, 1)
        pop = min(72, signal_conf * 0.7)
        strategies.append({
            "strategy": f"{direction.title()} {opt_type} Spread",
            "timing": "Buy 1-3 days BEFORE earnings, sell day-of",
            "rationale": f"{direction} signal ({signal_conf}% conf). {'PCR '+str(pcr)+' supports puts.' if direction=='BEARISH' else 'Momentum +'+str(move_5d)+'% supports calls.'} Target +{target_move}% move.",
            "type": "debit",
            "score": min(80, signal_conf * 0.75 + (5 if (market_bull and direction=='BULLISH') or (market_bear and direction=='BEARISH') else 0)),
            "pop": round(pop, 0),
            "rr": "1:1.8",
            "risk": "Full debit lost if direction wrong.",
        })

    # ── Strategy 5: Post-earnings premium sell (if high IV expected to crush) ──
    if iv_high and earn_days <= 3:
        strategies.append({
            "strategy": "Post-Earnings Bull/Bear Spread",
            "timing": "SELL immediately AFTER earnings announcement",
            "rationale": f"IV crush after earnings. Sell credit spread in reaction direction. IV {iv_atm:.0f}% will drop sharply post-announcement.",
            "type": "credit",
            "score": 70,
            "pop": 62,
            "rr": "1:2",
            "risk": "Gap risk if move continues beyond spread width.",
        })

    # Sort by score and return top 2
    strategies.sort(key=lambda x: -x["score"])
    return strategies[:2]

# ── Routes ────────────────────────────────────────────────────────────────────
@earnings_bp.route("/symbols")
def api_symbols():
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    symbols = _get_watchlist_symbols(watchlist_id)
    if symbols is None:
        symbols = _get_symbols_from_db()
    return jsonify({"symbols": symbols, "watchlist_id": watchlist_id})

@earnings_bp.route("/data")
def api_data():
    sym = (request.args.get("symbol") or "SPY").upper().strip()
    return jsonify(_fetch_earnings_data(sym))

@earnings_bp.route("/analysis")
def api_analysis():
    """Same as /data — analysis is always included now (no API key needed)."""
    sym = (request.args.get("symbol") or "SPY").upper().strip()
    data = _fetch_earnings_data(sym)
    if "error" in data:
        return jsonify(data), 500
    return jsonify(data)


# ════════════════════════════════════════════════════════════════════════════
# PRE-EARNINGS SCREENER
# ════════════════════════════════════════════════════════════════════════════

@earnings_bp.route("/pre_earnings_scan")
def pre_earnings_scan():
    """
    Pre-earnings scan: filter using earnings_calendar DB, then enrich with yfinance.
    Much faster: only fetches detailed data for symbols with upcoming earnings.
    """
    import math
    from datetime import date, datetime, timedelta
    from .earnings_calendar import get_upcoming_symbols, _ensure_calendar_table

    days_ahead = request.args.get("days", 30, type=int)
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    watchlist_syms = _get_watchlist_symbols(watchlist_id)

    # ── Step 1: Use calendar DB to get symbols with upcoming earnings ─────
    _ensure_calendar_table()
    cal_rows = get_upcoming_symbols(days_ahead=days_ahead)
    if watchlist_syms is not None:
        cal_rows = [r for r in cal_rows if r.get("symbol") in set(watchlist_syms)]

    if not cal_rows:
        # Calendar is empty — fall back to selected watchlist or all equity symbols
        if watchlist_syms is not None:
            symbols = [s for s in watchlist_syms if _is_equity(s)]
        else:
            symbols = [s for s in (_get_symbols_from_db() or []) if _is_equity(s)][:150]
        use_calendar = False
    else:
        symbols  = [r["symbol"] for r in cal_rows]
        cal_dict = {r["symbol"]: r for r in cal_rows}
        use_calendar = True

    results = []
    cached_syms = {}

    for sym in symbols:
        try:
            tk = yf.Ticker(sym)

            # ── Earnings date (from calendar DB first, then yfinance) ────────
            earn_date = None; earn_days = None
            if use_calendar and sym in cal_dict:
                ed_str = cal_dict[sym].get("next_earn_date")
                if ed_str:
                    earn_date = date.fromisoformat(ed_str)
                    earn_days = (earn_date - date.today()).days

            if earn_date is None:
                try:
                    cal = safe_calendar(sym)
                    if cal is not None and not (hasattr(cal,"empty") and cal.empty):
                        if isinstance(cal, dict):
                            ed = cal.get("Earnings Date") or cal.get("earningsDate")
                            if ed:
                                earn_date = (ed[0] if isinstance(ed, list) else ed)
                                if hasattr(earn_date,"date"): earn_date = earn_date.date()
                        elif hasattr(cal,"iloc"):
                            for col in cal.columns:
                                if "earn" in str(col).lower():
                                    v = cal[col].iloc[0]
                                    if v:
                                        earn_date = v.date() if hasattr(v,"date") else v
                                        break
                except: pass
                if earn_date:
                    earn_days = (earn_date - date.today()).days
                    if earn_days < 0 or earn_days > days_ahead:
                        continue

            if earn_date is None: continue
            if earn_days is None: earn_days = (earn_date - date.today()).days
            if earn_days < 0 or earn_days > days_ahead: continue

            earn_date_str = str(earn_date)[:10]

            # ── Historical price data ────────────────────────────────────────
            hist = tk.history(period="6mo")
            if hist is None or hist.empty: continue
            spot    = round(float(hist["Close"].iloc[-1]), 2)
            vol_20  = float(hist["Volume"].tail(20).mean()) if len(hist) >= 20 else 0
            vol_1   = float(hist["Volume"].iloc[-1])
            vol_rat = round(vol_1 / max(1, vol_20), 2)
            hi52    = round(float(hist["Close"].max()), 2)
            lo52    = round(float(hist["Close"].min()), 2)
            pct_hi  = round((spot - hi52) / hi52 * 100, 1) if hi52 else 0
            mom_10  = round((spot - float(hist["Close"].iloc[-11])) / float(hist["Close"].iloc[-11]) * 100, 2) if len(hist) >= 11 else 0
            atr_14  = float((hist["High"]-hist["Low"]).tail(14).mean()) if len(hist) >= 14 else 0
            atr_pct = round(atr_14 / max(0.01, spot) * 100, 2)

            # ── EPS history (from calendar DB if available) ──────────────────
            cal_data        = cal_dict.get(sym,{}) if use_calendar else {}
            last_actual     = cal_data.get("last_eps_actual")
            last_est        = cal_data.get("last_eps_estimate")
            last_surp_pct   = cal_data.get("last_surprise_pct")
            surp_streak     = cal_data.get("surprise_streak", 0)
            earn_react      = cal_data.get("earn_reaction_pct")

            # Fetch if not in calendar
            if last_actual is None:
                try:
                    eh = tk.quarterly_earnings or tk.earnings_history
                    if eh is not None and not eh.empty:
                        r0 = eh.iloc[-1]
                        last_actual  = float(r0.get("Reported EPS", r0.get("epsActual", 0)) or 0)
                        last_est_v   = float(r0.get("EPS Estimate",  r0.get("epsEstimate", 0)) or 0)
                        last_est     = last_est_v
                        if last_est_v:
                            last_surp_pct = round((last_actual-last_est_v)/abs(last_est_v)*100, 2)
                        surp_streak = 0
                        for _, row in eh.iloc[::-1].iterrows():
                            a = float(row.get("Reported EPS", row.get("epsActual",0)) or 0)
                            e = float(row.get("EPS Estimate", row.get("epsEstimate",0)) or 0)
                            if e and a >= e: surp_streak += 1
                            else: break
                except: pass

            # ── IV + expected move ────────────────────────────────────────────
            iv_atm = 0; exp_move_pct = 0
            try:
                exps = tk.options
                near_exp = min((e for e in exps if e > earn_date_str), default=None) if exps else None
                if near_exp:
                    chain    = tk.option_chain(near_exp)
                    atm_call = chain.calls[chain.calls["strike"]>=spot].head(1)
                    atm_put  = chain.puts[chain.puts["strike"]<=spot].tail(1)
                    iv_c     = float(atm_call["impliedVolatility"].iloc[0]) if not atm_call.empty else 0
                    iv_p     = float(atm_put["impliedVolatility"].iloc[0])  if not atm_put.empty  else 0
                    iv_atm   = round((iv_c + iv_p) / 2 * 100, 1) if iv_c and iv_p else 0
                    dte      = max(1, (date.fromisoformat(near_exp) - date.today()).days)
                    exp_move_pct = round(iv_atm * math.sqrt(dte / 365) * 0.68, 2) if iv_atm else 0
            except: pass

            # ── Analyst consensus ────────────────────────────────────────────
            analyst_rating = ""; analyst_target = 0
            try:
                info = tk.info or {}
                analyst_rating = info.get("recommendationKey","").replace("_"," ").title()
                analyst_target = round(float(info.get("targetMeanPrice",0) or 0), 2)
            except: pass

            results.append({
                "symbol":        sym,
                "spot":          spot,
                "earn_date":     earn_date_str,
                "earn_days":     earn_days,
                "earn_confirmed":cal_data.get("next_earn_confirmed",0),
                "vol_ratio":     vol_rat,
                "pct_52hi":      pct_hi,
                "mom_10d":       mom_10,
                "atr_pct":       atr_pct,
                "iv_atm":        iv_atm,
                "exp_move_pct":  exp_move_pct,
                "last_eps_act":  last_actual,
                "last_eps_est":  last_est,
                "last_surp_pct": last_surp_pct,
                "surp_streak":   surp_streak,
                "earn_react":    earn_react,
                "analyst_rating":analyst_rating,
                "analyst_target":analyst_target,
            })
        except Exception as e:
            continue

    results.sort(key=lambda x: x["earn_days"])

    # Save to cache + timestamp
    _ts_pre = _save_scan_timestamp("pre_earnings_completed_at")
    try:
        import json as _jj, sqlite3 as _ss
        from pathlib import Path as _PP
        _db = str(_PP(__file__).resolve().parents[2] / "options_data.db")
        _c  = _ss.connect(_db)
        _c.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        _c.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                   (_earn_cache_key('pre_earnings_scan', watchlist_id), _jj.dumps(results), _ts_pre))
        _c.commit(); _c.close()
    except: pass

    return jsonify({"results": results, "count": len(results), "completed_at": _ts_pre,
                    "days_ahead": days_ahead, "used_calendar": use_calendar,
                    "calendar_symbols": len(cal_rows) if use_calendar else 0,
                    "new_fetched": len(results), "from_cache": 0})

@earnings_bp.route("/post_earnings_scan")
def post_earnings_scan():
    """
    Scan watchlist for stocks that had earnings in last N days.
    fetch_all=true  → re-scan all
    fetch_all=false → only fetch symbols not in cache
    """
    import math as _m, json as _json
    from datetime import date, datetime, timedelta
    import sqlite3
    from pathlib import Path as _P
    from .earnings_calendar import get_recent_symbols, _ensure_calendar_table

    days_back = request.args.get("days", 10, type=int)
    fetch_all = request.args.get("fetch_all", "false").lower() == "true"
    watchlist_id = request.args.get("watchlist_id", None, type=int)
    watchlist_syms = _get_watchlist_symbols(watchlist_id)
    cutoff    = (date.today() - timedelta(days=days_back)).isoformat()

    # Step 1: Use calendar DB to pre-filter to symbols with recent earnings
    _ensure_calendar_table()
    cal_rows = get_recent_symbols(days_back=days_back)
    if cal_rows:
        symbols = [r["symbol"] for r in cal_rows if _is_equity(r["symbol"])]
        _cal_data = {r["symbol"]: r for r in cal_rows}
    else:
        # Fallback: scan selected watchlist or all equity symbols (slow)
        if watchlist_syms is not None:
            symbols = [s for s in watchlist_syms if _is_equity(s)]
        else:
            symbols = [s for s in (_get_symbols_from_db() or []) if _is_equity(s)]
        _cal_data = {}

    # Step 2: Check cached results for incremental mode
    cached_syms = {}
    DB_PATH3 = str(_P(__file__).resolve().parents[2] / "options_data.db")
    if not fetch_all:
        try:
            _cc2 = sqlite3.connect(DB_PATH3)
            row2 = _cc2.execute("SELECT value FROM app_cache WHERE key=?", (_earn_cache_key('post_earnings_scan', watchlist_id),)).fetchone()
            if row2: cached_syms = {r["symbol"]: r for r in _json.loads(row2[0])}
            _cc2.close()
        except: pass
        symbols = [s for s in symbols if s not in cached_syms]

    results = []

    for sym in symbols:
        try:
            tk = yf.Ticker(sym)

            # ── Find last earnings date ────────────────────────────────────
            earn_date = None
            try:
                ed = safe_earnings_dates(sym)
                if ed is not None and not ed.empty:
                    past = [i for i in ed.index if i.date() <= date.today()]
                    if past:
                        earn_date = max(past).date()
            except: pass

            if earn_date is None or earn_date.isoformat() < cutoff:
                continue

            days_since = (date.today() - earn_date).days

            # ── Price data ─────────────────────────────────────────────────
            hist = tk.history(period="60d")
            if hist.empty: continue
            closes = hist["Close"].tolist()
            vols   = hist["Volume"].tolist()
            spot   = round(closes[-1], 2)

            # Find pre-earnings close (day before)
            earn_idx = None
            for i, idx in enumerate(hist.index):
                if idx.date() >= earn_date:
                    earn_idx = i; break

            pre_earn_close  = round(float(hist["Close"].iloc[earn_idx - 1]), 2) if earn_idx and earn_idx > 0 else spot
            post_earn_close = round(float(hist["Close"].iloc[earn_idx]),     2) if earn_idx is not None else spot
            earn_reaction   = round((post_earn_close - pre_earn_close) / pre_earn_close * 100, 2)
            post_move       = round((spot - post_earn_close) / post_earn_close * 100, 2)
            total_move      = round((spot - pre_earn_close) / pre_earn_close * 100, 2)

            # ── Momentum post-earnings ─────────────────────────────────────
            post_closes = closes[earn_idx:] if earn_idx is not None else closes[-5:]
            if len(post_closes) >= 3:
                recent_trend = "UP" if post_closes[-1] > post_closes[-3] else "DOWN"
            else:
                recent_trend = "NEUTRAL"

            # ── Volume analysis ─────────────────────────────────────────────
            avg_vol_20  = sum(vols[-20:])/20 if len(vols)>=20 else sum(vols)/len(vols)
            earn_vol    = float(hist["Volume"].iloc[earn_idx]) if earn_idx is not None else avg_vol_20
            post_vol_3d = sum(vols[earn_idx:earn_idx+3]) / 3 if earn_idx and earn_idx+3 <= len(vols) else avg_vol_20
            vol_ratio   = round(earn_vol / max(1, avg_vol_20) * 100, 0)
            post_vol_ratio = round(post_vol_3d / max(1, avg_vol_20) * 100, 0)

            # ── EPS / Revenue ─────────────────────────────────────────────
            eps_beat = None; rev_beat = None; guidance = "N/A"
            eps_actual = None; eps_est = None; rev_actual = None; rev_est = None
            try:
                cal = safe_calendar(sym)
                if isinstance(cal, dict):
                    eps_actual = _safe(cal.get("EPS Actual") or cal.get("epsActual"))
                    eps_est    = _safe(cal.get("EPS Estimate") or cal.get("epsEstimate"))
                    rev_actual = _safe(cal.get("Revenue Actual") or cal.get("revenueActual"))
                    rev_est    = _safe(cal.get("Revenue Estimate") or cal.get("revenueEstimate"))
            except: pass
            try:
                fi = tk.fast_info
                if eps_actual is None and hasattr(fi, 'last_fiscal_year_end'):
                    qe = tk.quarterly_earnings
                    if qe is not None and not qe.empty:
                        r = qe.iloc[0]
                        eps_actual = float(r.get("Reported EPS", r.get("EPS",0)) or 0)
                        eps_est    = float(r.get("EPS Estimate", 0) or 0)
            except: pass

            if eps_actual is not None and eps_est is not None and eps_est != 0:
                eps_beat = eps_actual >= eps_est
            if rev_actual is not None and rev_est is not None and rev_est != 0:
                rev_beat = rev_actual >= rev_est

            # ── Technical levels (S/R) ─────────────────────────────────────
            recent_closes = closes[-20:]
            high_20  = round(max(recent_closes), 2)
            low_20   = round(min(recent_closes), 2)
            sma_20   = round(sum(recent_closes)/len(recent_closes), 2)
            # Key S/R: pre-earnings close, post-earnings open, 20d high/low
            support    = round(min(pre_earn_close, low_20), 2)
            resistance = round(max(pre_earn_close, high_20), 2)
            gap_fill   = pre_earn_close  # gap fill target

            # ── OI from DB ────────────────────────────────────────────────
            pcr = None; put_wall = None; call_wall = None; total_oi = 0
            try:
                con = sqlite3.connect(DB_PATH3)
                today_s = date.today().isoformat()
                exp_row = con.execute("SELECT MIN(expiration) FROM options WHERE symbol=? AND expiration>=?",
                    (sym, today_s)).fetchone()
                if exp_row and exp_row[0]:
                    oi_exp = exp_row[0]
                    agg = con.execute("""SELECT type, SUM(oi) FROM options
                        WHERE symbol=? AND expiration=?
                        AND date=(SELECT MAX(date) FROM options WHERE symbol=?)
                        GROUP BY type""", (sym, oi_exp, sym)).fetchall()
                    oi_map = {r[0]: r[1] or 0 for r in agg}
                    c_oi = oi_map.get("call",0); p_oi = oi_map.get("put",0)
                    total_oi = c_oi + p_oi
                    if c_oi > 0: pcr = round(p_oi/c_oi, 2)
                    pw = con.execute("""SELECT strike FROM options
                        WHERE symbol=? AND expiration=? AND type='put'
                        AND date=(SELECT MAX(date) FROM options WHERE symbol=?)
                        ORDER BY oi DESC LIMIT 1""", (sym,oi_exp,sym)).fetchone()
                    cw = con.execute("""SELECT strike FROM options
                        WHERE symbol=? AND expiration=? AND type='call'
                        AND date=(SELECT MAX(date) FROM options WHERE symbol=?)
                        ORDER BY oi DESC LIMIT 1""", (sym,oi_exp,sym)).fetchone()
                    if pw: put_wall = float(pw[0])
                    if cw: call_wall = float(cw[0])
                con.close()
            except: pass

            # ── IV ────────────────────────────────────────────────────────
            iv_atm = None
            try:
                exps = tk.options
                if exps:
                    chain = tk.option_chain(exps[0])
                    all_k = sorted(set(chain.calls["strike"].tolist()))
                    atm = min(all_k, key=lambda k: abs(k-spot))
                    iv_row = chain.calls[chain.calls["strike"]==atm]["impliedVolatility"].values
                    if len(iv_row): iv_atm = round(float(iv_row[0])*100, 1)
            except: pass

            # ── SPY regime ────────────────────────────────────────────────
            spy_regime = None
            try:
                con2 = sqlite3.connect(DB_PATH3)
                sr = con2.execute("SELECT bias FROM regime_scan WHERE symbol='SPY' ORDER BY scan_date DESC LIMIT 1").fetchone()
                if sr: spy_regime = sr[0]
                con2.close()
            except: pass

            # ── Post-Earnings Trade Suggestions ───────────────────────────
            trades = _suggest_post_earnings_trades(
                sym=sym, spot=spot, earn_reaction=earn_reaction, post_move=post_move,
                recent_trend=recent_trend, eps_beat=eps_beat, rev_beat=rev_beat,
                support=support, resistance=resistance, gap_fill=gap_fill,
                put_wall=put_wall, call_wall=call_wall, pcr=pcr,
                iv_atm=iv_atm, vol_ratio=vol_ratio, post_vol_ratio=post_vol_ratio,
                sma_20=sma_20, days_since=days_since, spy_regime=spy_regime
            )

            # ── Market/Earnings verdict ────────────────────────────────────
            verdict = "BEAT" if (eps_beat and rev_beat) else                       "MISS" if (eps_beat==False and rev_beat==False) else                       "MIXED" if eps_beat is not None else "UNKNOWN"
            reaction_label = "GAP UP" if earn_reaction>3 else "GAP DOWN" if earn_reaction<-3 else                              "SLIGHT UP" if earn_reaction>0 else "SLIGHT DOWN"

            results.append({
                "symbol": sym, "earn_date": earn_date.isoformat(),
                "days_since": days_since,
                "verdict": verdict, "reaction_label": reaction_label,
                "earn_reaction": earn_reaction, "post_move": post_move,
                "total_move": total_move, "spot": spot,
                "pre_earn_close": pre_earn_close, "recent_trend": recent_trend,
                "eps_beat": eps_beat, "rev_beat": rev_beat,
                "eps_actual": eps_actual, "eps_est": eps_est,
                "vol_ratio": vol_ratio, "post_vol_ratio": post_vol_ratio,
                "support": support, "resistance": resistance,
                "gap_fill": gap_fill, "sma_20": sma_20,
                "put_wall": put_wall, "call_wall": call_wall,
                "pcr": pcr, "total_oi": total_oi, "iv_atm": iv_atm,
                "spy_regime": spy_regime,
                "trades": trades,
            })
        except Exception as e:
            continue

    # Merge with cached results
    if cached_syms:
        from datetime import date as _date3, timedelta as _td3
        cutoff3 = (_date3.today() - _td3(days=days_back)).isoformat()
        existing_valid3 = [r for r in cached_syms.values()
                          if r.get("earn_date","") >= cutoff3
                          and r["symbol"] not in {x["symbol"] for x in results}]
        results = results + existing_valid3

    # Save merged to DB
    try:
        import json as _json3, sqlite3 as _sq3
        from pathlib import Path as _P3b
        _db3 = str(_P3b(__file__).resolve().parents[2] / "options_data.db")
        _c3 = _sq3.connect(_db3)
        _c3.execute("CREATE TABLE IF NOT EXISTS app_cache (key TEXT PRIMARY KEY, value TEXT, updated TEXT)")
        _c3.execute("INSERT OR REPLACE INTO app_cache VALUES (?,?,?)",
                    (_earn_cache_key('post_earnings_scan', watchlist_id), _json3.dumps(results), __import__('datetime').date.today().isoformat()))
        _c3.commit(); _c3.close()
    except: pass

    results.sort(key=lambda x: x["days_since"])
    _now_post = _save_scan_timestamp("post_earnings_completed_at")
    return jsonify({"results": results, "completed_at": _now_post, "count": len(results), "days_back": days_back,
                    "new_fetched": len([r for r in results if r["symbol"] not in cached_syms]),
                    "from_cache": len(cached_syms)})


def _suggest_post_earnings_trades(sym, spot, earn_reaction, post_move, recent_trend,
    eps_beat, rev_beat, support, resistance, gap_fill, put_wall, call_wall,
    pcr, iv_atm, vol_ratio, post_vol_ratio, sma_20, days_since, spy_regime):
    """Suggest post-earnings option trades with rationale, S/R, score, PoP."""
    import math as _m

    iv = iv_atm or 30
    market_bull = "BULL" in (spy_regime or "").upper()
    market_bear = "BEAR" in (spy_regime or "").upper()
    big_gap_up   = earn_reaction > 5
    big_gap_down = earn_reaction < -5
    consolidating = abs(post_move) < 2 and days_since >= 3
    fading   = earn_reaction > 3 and post_move < -1
    rallying = earn_reaction < -3 and post_move > 1  # gap-fill rally

    trades = []

    # ── 1. Fade the Gap (if big gap up + IV still high + fading) ──
    if big_gap_up and fading and iv > 35:
        gap_target = round(spot * 0.95, 2)
        conf  = min(82, 55 + abs(post_move)*3)
        pop   = min(70, 50 + (iv - 35)*0.4)
        trades.append({
            "strategy": "Bear Put Spread (Gap Fade)",
            "type": "debit", "direction": "bearish",
            "entry": f"Buy ${round(spot,0):.0f}P / Sell ${round(gap_target,0):.0f}P",
            "target": f"${gap_fill:.2f} (gap fill) then ${support:.2f} support",
            "stop": f"${resistance:.2f} (above gap high)",
            "rationale": f"Gap up {earn_reaction}% is fading ({post_move}% back). Gap fill target ${gap_fill:.2f}. IV {iv}% still elevated — debit cheap.",
            "sr_key": f"S: ${support:.2f} | R: ${resistance:.2f} | Gap: ${gap_fill:.2f}",
            "confidence": round(conf), "pop": round(pop),
            "rr": f"1:{round(abs(gap_fill-spot)/max(0.1,spot-gap_target)*0.8,1)}",
        })

    # ── 2. Gap Fill Buy (if big gap down + rallying / oversold bounce) ──
    if big_gap_down and (rallying or recent_trend == "UP"):
        target = min(gap_fill, resistance)
        conf = min(78, 50 + abs(earn_reaction)*1.5)
        pop  = min(65, 45 + (abs(earn_reaction)-5)*1.5)
        trades.append({
            "strategy": "Bull Call Spread (Gap Fill)",
            "type": "debit", "direction": "bullish",
            "entry": f"Buy ${round(spot,0):.0f}C / Sell ${round(target,0):.0f}C",
            "target": f"${target:.2f} (gap fill / resistance)",
            "stop": f"${support:.2f} (below post-earn low)",
            "rationale": f"Gap down {earn_reaction}% with bounce. Gap fill to ${gap_fill:.2f} ({round((gap_fill-spot)/spot*100,1)}% upside). Post-move: {post_move}%.",
            "sr_key": f"S: ${support:.2f} | R: ${target:.2f} | Gap: ${gap_fill:.2f}",
            "confidence": round(conf), "pop": round(pop),
            "rr": f"1:{round(abs(target-spot)/max(0.1,spot-support)*0.8,1)}",
        })

    # ── 3. Continuation Bull Spread (beat + trending up) ──
    if (eps_beat or rev_beat) and recent_trend == "UP" and earn_reaction > 0:
        tgt = call_wall if call_wall and call_wall > spot else round(spot * 1.08, 2)
        conf = min(80, 50 + (10 if eps_beat else 0) + (5 if rev_beat else 0) + (5 if market_bull else 0))
        pop  = min(68, 50 + conf*0.15)
        trades.append({
            "strategy": "Bull Call Spread (Continuation)",
            "type": "debit", "direction": "bullish",
            "entry": f"Buy ${round(spot,0):.0f}C / Sell ${round(tgt,0):.0f}C",
            "target": f"${tgt:.2f} {'(call wall)' if call_wall and abs(call_wall-tgt)<2 else '(+8%)'}",
            "stop": f"${round(spot*0.96,2):.2f} (4% stop)",
            "rationale": f"{'EPS beat. ' if eps_beat else ''}{'Rev beat. ' if rev_beat else ''}Trending up post-earnings. {'Market bullish.' if market_bull else ''}",
            "sr_key": f"S: ${support:.2f} / ${sma_20:.2f} SMA | R: ${tgt:.2f}",
            "confidence": round(conf), "pop": round(pop),
            "rr": f"1:{round((tgt-spot)/max(0.1,spot-spot*0.96)*0.8,1)}",
        })

    # ── 4. IV Crush Sell (if IV still elevated post-earnings + consolidating) ──
    if iv > 40 and consolidating:
        c_sell = call_wall if call_wall and call_wall > spot else round(spot * 1.05, 2)
        p_sell = put_wall  if put_wall  and put_wall  < spot else round(spot * 0.95, 2)
        conf = min(75, 50 + (iv-40)*0.5)
        pop  = min(70, 55 + (iv-40)*0.35)
        trades.append({
            "strategy": "Iron Condor (IV Crush)",
            "type": "credit", "direction": "neutral",
            "entry": f"Sell ${round(p_sell,0):.0f}P–${round(c_sell,0):.0f}C strangle",
            "target": "Expire worthless / close at 50% profit",
            "stop": "Close if price breaches either short strike",
            "rationale": f"IV {iv}% still elevated {days_since}d post-earnings. Stock consolidating (±{abs(post_move):.1f}%). Sell premium with IV crush tailwind.",
            "sr_key": f"P-Wall: ${p_sell:.2f} | C-Wall: ${c_sell:.2f} | PCR: {pcr or '—'}",
            "confidence": round(conf), "pop": round(pop),
            "rr": "1:2.5",
        })

    # ── 5. Put Spread (miss + trending down + market bear) ──
    if (eps_beat==False or rev_beat==False) and recent_trend == "DOWN":
        tgt = put_wall if put_wall and put_wall < spot else round(spot * 0.93, 2)
        conf = min(78, 50 + (8 if not eps_beat else 0) + (5 if not rev_beat else 0) + (5 if market_bear else 0))
        pop  = min(65, 48 + conf*0.15)
        trades.append({
            "strategy": "Bear Put Spread (Miss + Trend)",
            "type": "debit", "direction": "bearish",
            "entry": f"Buy ${round(spot,0):.0f}P / Sell ${round(tgt,0):.0f}P",
            "target": f"${tgt:.2f} {'(put wall)' if put_wall and abs(put_wall-tgt)<2 else '(-7%)'}",
            "stop": f"${round(spot*1.04,2):.2f} (4% stop)",
            "rationale": f"{'EPS miss. ' if eps_beat==False else ''}{'Rev miss. ' if rev_beat==False else ''}Downtrend continuing. {'Market bearish.' if market_bear else ''}",
            "sr_key": f"S: ${tgt:.2f} | R: ${support:.2f} | SMA20: ${sma_20:.2f}",
            "confidence": round(conf), "pop": round(pop),
            "rr": f"1:{round((spot-tgt)/max(0.1,spot*0.04)*0.8,1)}",
        })

    trades.sort(key=lambda x: -x["confidence"])
    return trades[:3]
