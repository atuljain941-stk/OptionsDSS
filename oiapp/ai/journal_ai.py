from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..journal.journal_snapshot import preview_entry_score


_SIDE_LABELS = {
    "PS": "Bullish credit",
    "PB": "Bullish debit",
    "CB": "Bullish debit",
    "CS": "Bearish credit",
    "IC": "Neutral income",
    "Stock": "Directional stock",
}

_MISSING_LABELS = [
    ("spot", "spot price"),
    ("rs_vs_spy", "relative strength vs SPY"),
    ("iv_rank", "IV rank"),
    ("pcr", "put/call ratio"),
    ("call_wall", "call wall"),
    ("put_wall", "put wall"),
    ("gamma_flip", "gamma flip"),
]


def _safe_num(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if num != num:  # NaN
        return None
    return num


def _fmt_pct(value: Optional[float], digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}%"


def _fmt_money(value: Optional[float], digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"${value:.{digits}f}"


def _trade_family(trade_type: str) -> str:
    tt = (trade_type or "").strip().upper()
    if tt in ("PS", "PB", "CB"):
        return "bull"
    if tt == "CS":
        return "bear"
    if tt == "IC":
        return "ic"
    return "directional"


def _grade_from_score(score: Optional[float]) -> str:
    s = float(score or 0)
    if s >= 80:
        return "A"
    if s >= 65:
        return "B"
    if s >= 50:
        return "C"
    if s >= 35:
        return "D"
    return "F"


def _recommendation_from_score(score: Optional[float], trade_family: str = "directional", *, context: str = "entry") -> str:
    s = float(score or 0)
    fam = (trade_family or "directional").lower().strip()
    context = (context or "entry").lower().strip()

    if context == "alert":
        if s >= 80:
            return "HOLD"
        if s >= 65:
            return "HOLD"
        if s >= 50:
            return "WATCH"
        if s >= 35:
            return "TRIM"
        return "HEDGE" if fam in {"bull", "bear", "directional"} else "EXIT"

    # Entry / preview context
    if s >= 80:
        return "OPEN"
    if s >= 65:
        return "OPEN"
    if s >= 50:
        return "OPEN_SMALL"
    if s >= 35:
        return "OPEN_SMALL"
    return "AVOID"


def _decision_phrase(score: Optional[float], recommendation: str, *, context: str = "entry", trade_family: str = "directional") -> str:
    rec = str(recommendation or "HOLD").upper().strip()
    s = float(score or 0)
    fam = (trade_family or "directional").lower().strip()
    if context == "alert":
        if rec == "HOLD":
            return "Trade holding up. Stay with the position unless the key level fails."
        if rec == "WATCH":
            return "Conditions are softening. Watch the nearest critical level closely."
        if rec == "TRIM":
            return "Risk is rising. Trim or reduce size before the next adverse move."
        if rec == "HEDGE":
            return "Protection is needed now. Hedge or reduce risk immediately."
        if rec == "EXIT":
            return "Critical risk. Exit the trade or close on the next orderly opportunity."
    else:
        if rec == "OPEN":
            return "Strong setup. Proceed with standard size if liquidity is acceptable."
        if rec == "OPEN_SMALL":
            return "Marginal setup. Open small and keep risk defined."
        if rec == "AVOID":
            return "Weak setup. Skip it unless one of the risks clears."
        if rec == "WATCH":
            return "Watch for confirmation before committing size."
        if rec == "TRIM":
            return "Trim or protect gains before the next key level."
        if rec == "HEDGE":
            return "Hedge or reduce size before the next adverse move."
        if rec == "EXIT":
            return "Exit rather than forcing a weak setup."
    if fam == "ic":
        return "Keep the structure inside the intended range and manage the short strikes carefully."
    return f"Score {s:.0f}/100 suggests the trade should be handled with caution."


def _next_actions_for_decision(base: Dict[str, Any], recommendation: str, *, context: str = "entry") -> List[str]:
    rec = str(recommendation or "HOLD").upper().strip()
    score = _safe_num(base.get("score")) or 0.0
    alerts = _critical_price_alerts(base)
    price_action = alerts[0]["action"] if alerts else "Keep monitoring the key levels."
    if context == "alert":
        if rec == "HOLD":
            return ["Hold the trade while the key level remains intact.", "Keep alerts active on the nearest support/resistance zone.", price_action]
        if rec == "WATCH":
            return ["Watch closely; the setup is losing quality.", "Do not add size.", price_action]
        if rec == "TRIM":
            return ["Trim risk now.", "Protect gains before the next adverse move.", price_action]
        if rec == "HEDGE":
            return ["Hedge or reduce size now.", "Use defined-risk protection if possible.", price_action]
        if rec == "EXIT":
            return ["Exit or close on the next orderly opportunity.", "Do not add to a broken setup.", price_action]
    if rec == "OPEN_SMALL":
        size = "25-50% size" if score >= 45 else "25% size"
        return [f"Open with {size} only.", "Keep risk defined and avoid over-sizing.", price_action]
    if rec == "OPEN":
        return ["Proceed with standard size if liquidity and spreads are acceptable.", "Set alerts on the nearest critical level.", price_action]
    if rec == "AVOID":
        return ["Skip this setup unless a clearer trigger appears.", "Wait for a better regime or price location.", price_action]
    if rec == "TRIM":
        return ["Trim risk now and protect gains.", "Do not wait for a full reversal.", price_action]
    if rec == "HEDGE":
        return ["Hedge or reduce size before the next adverse move.", "Prefer defined-risk protection.", price_action]
    if rec == "EXIT":
        return ["Exit or close on the next orderly opportunity.", "Avoid fighting the move.", price_action]
    return ["Hold and wait for confirmation.", "Monitor the nearest critical price alert.", price_action]


def _apply_decision_fields(base: Dict[str, Any], *, context: str = "entry") -> Dict[str, Any]:
    base = dict(base or {})
    score = _safe_num(base.get("score")) or 0.0
    trade_family = str(base.get("trade_family") or "directional")
    recommendation = _recommendation_from_score(score, trade_family, context=context)
    base["score"] = round(score, 1) if score % 1 else int(score)
    base["grade"] = _grade_from_score(score)
    base["recommendation"] = recommendation
    base["decision_phrase"] = _decision_phrase(score, recommendation, context=context, trade_family=trade_family)
    base["headline"] = f"{base.get('grade', '?')} / {int(round(score))} — {base['decision_phrase']}"
    base["explicit_action"] = _explicit_recommendation_text(base)
    base["critical_price_alerts"] = _critical_price_alerts(base)
    base["action_steps"] = _next_actions_for_decision(base, recommendation, context=context)
    base["next_actions"] = list(base["action_steps"])
    return base


def _confidence_from_analysis(base: Dict[str, Any]) -> float:
    score = _safe_num(base.get("score")) or 50.0
    filled = 0
    for key, _label in _MISSING_LABELS:
        if base.get(key) not in (None, "", [], {}):
            filled += 1
    completeness = filled / max(len(_MISSING_LABELS), 1)
    distance = min(abs(score - 50.0), 40.0)
    confidence = 36.0 + completeness * 34.0 + distance * 0.9
    return max(35.0, min(96.0, round(confidence, 1)))


def _thesis_bullets(base: Dict[str, Any]) -> List[str]:
    bullets: List[str] = []
    regime_m = base.get("regime_monthly") or ""
    regime_w = base.get("regime_weekly") or ""
    regime_d = base.get("regime_daily") or ""
    regime_txt = ", ".join([x for x in [regime_m, regime_w, regime_d] if x])
    if regime_txt:
        bullets.append(f"Regime: {regime_txt}")

    rs = _safe_num(base.get("rs_vs_spy"))
    if rs is not None:
        bullets.append(f"RS vs SPY: {rs:+.1f}%")

    ivr = _safe_num(base.get("iv_rank"))
    if ivr is not None:
        bullets.append(f"IV rank: {ivr:.0f}%")

    pcr = _safe_num(base.get("pcr"))
    if pcr is not None:
        bullets.append(f"PCR: {pcr:.2f}")

    put_wall = _safe_num(base.get("put_wall"))
    call_wall = _safe_num(base.get("call_wall"))
    if put_wall is not None or call_wall is not None:
        left = f"Put wall {_fmt_money(put_wall, 0)}" if put_wall is not None else None
        right = f"Call wall {_fmt_money(call_wall, 0)}" if call_wall is not None else None
        bullets.append("Walls: " + " / ".join([x for x in [left, right] if x]))

    gamma_flip = _safe_num(base.get("gamma_flip"))
    if gamma_flip is not None:
        bullets.append(f"Gamma flip: {_fmt_money(gamma_flip, 0)}")

    if base.get("earn_days") is not None:
        bullets.append(f"Earnings in {base.get('earn_days')} days")
    return bullets


def _risk_bullets(base: Dict[str, Any]) -> List[str]:
    risks = list(base.get("cons") or [])
    extra = []
    if base.get("earn_days") is not None and _safe_num(base.get("earn_days")) is not None:
        ed = int(_safe_num(base.get("earn_days")) or 0)
        if ed < 14:
            extra.append(f"Earnings in {ed} days")
    if base.get("gamma_flip") is not None and base.get("spot") is not None:
        spot = _safe_num(base.get("spot"))
        gf = _safe_num(base.get("gamma_flip"))
        if spot is not None and gf is not None and abs(gf - spot) / max(spot, 1e-9) < 0.01:
            extra.append("Spot is near gamma flip")
    return (risks + extra)[:5]


def _score_meaning(score: Optional[float]) -> str:
    if score is None:
        return "Score unavailable"
    s = float(score)
    if s >= 80:
        return "Strong setup. Favor hold/add behavior if risk is controlled."
    if s >= 65:
        return "Good setup. Hold unless a risk level is breached."
    if s >= 50:
        return "Mixed setup. Watch closely and avoid adding size."
    if s >= 35:
        return "Weak setup. Reduce risk and protect capital."
    return "Critical setup. Exit or hedge is usually the prudent path."


def _confidence_meaning(confidence: Optional[float]) -> str:
    if confidence is None:
        return "Confidence reflects how much context is available."
    c = float(confidence)
    if c >= 85:
        return "Most key inputs are present, so the read is well-supported."
    if c >= 70:
        return "Useful context is present, but a few important inputs may be missing."
    if c >= 55:
        return "Some inputs are missing; treat the read as directional, not definitive."
    return "Many inputs are missing; use this as a rough guide only."


def _explicit_recommendation_text(base: Dict[str, Any]) -> str:
    rec = str(base.get("recommendation") or "HOLD").upper().strip()
    score = _safe_num(base.get("score")) or 0.0
    fam = str(base.get("trade_family") or "").lower().strip()
    if rec == "OPEN_SMALL":
        size = "25-50% of planned size" if score >= 45 else "25% of planned size"
        return (
            f"Open small: use {size}, keep defined risk, and place alerts at the nearest critical level."
        )
    if rec == "ADD":
        return "Add only if the setup still respects the key level and liquidity remains acceptable."
    if rec == "WATCH":
        return "Watch closely and wait for confirmation before acting."
    if rec == "TRIM":
        return "Trim size now and protect gains before the next key level or event."
    if rec == "HEDGE":
        return "Add a hedge or reduce size before the next adverse move can expand."
    if rec == "EXIT":
        return "Exit now or on the next orderly opportunity; the risk is outweighing the edge."
    if fam == "ic":
        return "Hold the iron condor only while both sides remain inside the intended range."
    return "Hold and monitor the nearest critical level for confirmation or failure."


def _critical_price_alerts(base: Dict[str, Any]) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []
    spot = _safe_num(base.get("spot"))
    pnr = _safe_num(base.get("pnr"))
    pnr_upper = _safe_num(base.get("pnr_upper"))
    call_wall = _safe_num(base.get("call_wall"))
    put_wall = _safe_num(base.get("put_wall"))
    gamma_flip = _safe_num(base.get("gamma_flip"))
    max_pain = _safe_num(base.get("max_pain"))
    exp_move = _safe_num(base.get("expected_move"))
    trade_family = str(base.get("trade_family") or "").lower().strip()

    def add(level: Optional[float], label: str, reason: str, action: str, *, direction: str = ""):
        if level is None:
            return
        alerts.append({
            "label": label,
            "level": round(level, 2),
            "reason": reason,
            "action": action,
            "direction": direction,
        })

    if pnr is not None:
        add(
            pnr,
            "PNR",
            "Price at or beyond the planned no-recovery level for this trade.",
            "Exit or hedge immediately if this level is breached.",
            direction="down" if trade_family in {"bull", "directional"} else "up",
        )
    if pnr_upper is not None and pnr_upper != pnr:
        add(
            pnr_upper,
            "PNR upper",
            "Upper risk boundary for this trade.",
            "Reduce risk if price pushes through this level.",
            direction="up",
        )
    if put_wall is not None:
        add(
            put_wall,
            "Put wall",
            "A major downside level where buyers may defend or step in.",
            "If this fails, bearish pressure is increasing.",
            direction="down",
        )
    if call_wall is not None:
        add(
            call_wall,
            "Call wall",
            "A major upside level where sellers may defend or cap price.",
            "If this breaks, upside continuation is improving.",
            direction="up",
        )
    if gamma_flip is not None:
        add(
            gamma_flip,
            "Gamma flip",
            "Dealer positioning can change meaningfully near this level.",
            "Treat a break of this level as a regime shift alert.",
            direction="up" if spot is None or gamma_flip >= (spot or gamma_flip) else "down",
        )
    if max_pain is not None:
        add(
            max_pain,
            "Max pain",
            "Option pain may pull price toward this level into expiry.",
            "Use it as a magnet/mean-reversion alert rather than a hard stop.",
        )
    if spot is not None and exp_move is not None:
        add(
            max(0.0, spot - exp_move),
            "Expected move low",
            "Lower edge of the current expected move from implied volatility.",
            "A break below this zone suggests the move is larger than expected.",
            direction="down",
        )
        add(
            spot + exp_move,
            "Expected move high",
            "Upper edge of the current expected move from implied volatility.",
            "A break above this zone suggests the move is larger than expected.",
            direction="up",
        )
    alerts.sort(key=lambda x: (0 if x.get("label") == "PNR" else 1, abs((x.get("level") or 0) - (spot or 0))))
    return alerts[:4]


def _practical_trade_plan(base: Dict[str, Any], *, context: str = "entry") -> Dict[str, Any]:
    """Build a practical hold / roll / exit plan from the current trade context.

    This stays deterministic and explainable: score, DTE, PNR, IV, regime, OI,
    and the optional roll candidates from the journal route decide the action.
    """
    d = dict(base or {})
    context = (context or "entry").lower().strip()
    score = _safe_num(d.get("score")) or 0.0
    dte = int(round(_safe_num(d.get("dte")) or 0))
    pct_of_max = _safe_num(d.get("pct_of_max_profit"))
    pnr_breached = bool(d.get("pnr_breached"))
    trade_type = str(d.get("trade_type") or "").upper().strip()
    trade_family = str(d.get("trade_family") or _trade_family(trade_type)).lower().strip()
    iv_rank = _safe_num(d.get("iv_rank"))
    oi_signal = str(d.get("oi_signal") or "").upper().strip()
    regime_bias = str(d.get("regime_bias") or "").lower().strip()
    regime_name = str(d.get("regime_name") or "").upper().strip()
    spot = _safe_num(d.get("spot"))
    pnr = _safe_num(d.get("pnr"))
    pnr_upper = _safe_num(d.get("pnr_upper"))

    roll_candidates = list(d.get("roll_candidates") or [])
    roll_best = roll_candidates[0] if roll_candidates else {}
    roll_expiry = roll_best.get("expiry") or d.get("roll_expiry") or ""
    roll_name = roll_best.get("name") or roll_best.get("strategy") or ""
    roll_rr = _safe_num(roll_best.get("rr"))
    roll_pop = _safe_num(roll_best.get("pop"))

    # Infer which side is under pressure so the user gets a real action.
    threat_side = str(d.get("threat_side") or "").lower().strip()
    if not threat_side:
        pnr_status = str(d.get("pnr_status") or "").upper()
        if "PUT" in pnr_status:
            threat_side = "put"
        elif "CALL" in pnr_status:
            threat_side = "call"
        elif trade_family == "ic" and spot is not None and pnr is not None and pnr_upper is not None:
            width = max(pnr_upper - pnr, 0.01)
            if spot <= pnr + width * 0.30:
                threat_side = "put"
            elif spot >= pnr_upper - width * 0.30:
                threat_side = "call"

    # Determine action mode.
    action_mode = "HOLD"
    if context == "alert":
        if pnr_breached and dte <= 3:
            action_mode = "EXIT"
        elif trade_family == "ic" and threat_side and roll_expiry:
            action_mode = "ROLL_SIDE"
        elif pnr_breached or score < 35:
            action_mode = "EXIT"
        elif score < 50 and dte <= 21:
            action_mode = "ROLL"
        elif pct_of_max is not None and pct_of_max >= 50 and dte <= 21:
            action_mode = "TRIM"
        elif dte <= 7 and trade_family in {"bull", "bear", "directional"}:
            action_mode = "ROLL"
        elif score >= 65:
            action_mode = "HOLD"
        else:
            action_mode = "WATCH"
    else:
        if score >= 80:
            action_mode = "OPEN"
        elif score >= 50:
            action_mode = "OPEN_SMALL"
        else:
            action_mode = "AVOID"

    # Human readable recommendation text.
    if context == "alert":
        if action_mode == "EXIT":
            action_text = "No clean roll is available. Exit the trade and reset risk."
        elif action_mode == "ROLL_SIDE":
            side = "put" if threat_side == "put" else "call"
            if roll_expiry:
                if trade_family == "ic":
                    action_text = (
                        f"Roll the {side} side to {roll_expiry} ({roll_name or 'best available structure'}" 
                        f"{f' · RR {roll_rr:.2f}' if roll_rr is not None else ''}"
                        f"{f' · PoP {roll_pop:.0f}%' if roll_pop is not None else ''}). "
                        f"Leave the safe side in place if it still has room."
                    )
                else:
                    action_text = (
                        f"Roll to {roll_expiry} ({roll_name or 'best available structure'}"
                        f"{f' · RR {roll_rr:.2f}' if roll_rr is not None else ''}"
                        f"{f' · PoP {roll_pop:.0f}%' if roll_pop is not None else ''}) and keep risk defined."
                    )
            else:
                action_text = "Roll the threatened side only if a clean later expiry exists; otherwise exit."
        elif action_mode == "ROLL":
            if roll_expiry:
                action_text = (
                    f"Roll out to {roll_expiry} ({roll_name or 'best available structure'}"
                    f"{f' · RR {roll_rr:.2f}' if roll_rr is not None else ''}"
                    f"{f' · PoP {roll_pop:.0f}%' if roll_pop is not None else ''}) if liquidity is acceptable."
                )
            else:
                action_text = "Roll out to the next liquid expiry if IV / OI / regime stay supportive; otherwise exit."
        elif action_mode == "TRIM":
            action_text = "Trim or reduce risk now; the trade has already captured enough profit."
        elif action_mode == "WATCH":
            action_text = "Watch closely; the setup is deteriorating but not broken yet."
        else:
            action_text = "Trade is holding; stay with it unless the next key level fails."
    else:
        if action_mode == "OPEN":
            action_text = "Strong setup. Proceed with standard size if liquidity is acceptable."
        elif action_mode == "OPEN_SMALL":
            action_text = "Marginal setup. Open small and keep risk defined."
        elif action_mode == "AVOID":
            action_text = "Weak setup. Skip it unless one of the risks clears."
        else:
            action_text = "Hold and wait for confirmation."

    # What the user should watch next.
    watch: List[str] = []
    if trade_family == "ic":
        if threat_side:
            watch.append(f"{threat_side.title()} side is the one under pressure")
        watch.append("Keep the safe side untouched if it still has room")
    elif trade_family in {"bull", "bear"}:
        watch.append("Use the nearest PNR / support / resistance zone as the trigger")
        watch.append("Roll only if the thesis is still valid and the next expiry has better carry")
    else:
        watch.append("Stay aligned with the prevailing trend and regime")
        watch.append("Exit if the trade is no longer supported by price action")

    reasons: List[str] = []
    if score < 50:
        reasons.append(f"Score {score:.0f}/100 is only moderate or weak")
    if pnr_breached:
        reasons.append("PNR is breached or under immediate pressure")
    if dte <= 7:
        reasons.append(f"Only {dte} DTE left, so gamma risk is rising")
    elif dte <= 21:
        reasons.append(f"{dte} DTE left — manage before time decay accelerates")
    if iv_rank is not None:
        if trade_family in {"bull", "bear", "ic"} and iv_rank >= 55:
            reasons.append(f"IV rank {iv_rank:.0f}% still supports premium selling")
        elif trade_family in {"bull", "bear", "ic"} and iv_rank < 30:
            reasons.append(f"IV rank {iv_rank:.0f}% is thin for credit rolls")
    if oi_signal:
        if "SHORT" in oi_signal:
            reasons.append("OI trend is still bearish / short-heavy")
        elif "LONG" in oi_signal:
            reasons.append("OI trend is still constructive / long-heavy")
    if regime_bias:
        if "bear" in regime_bias:
            reasons.append(f"Regime bias is bearish ({regime_name or 'unknown'})")
        elif "bull" in regime_bias:
            reasons.append(f"Regime bias is bullish ({regime_name or 'unknown'})")
    if pct_of_max is not None:
        reasons.append(f"{pct_of_max:.0f}% of max profit has already been captured")

    if action_mode in {"ROLL", "ROLL_SIDE"} and not roll_expiry and roll_candidates:
        action_mode = "EXIT" if pnr_breached or score < 45 else "WATCH"
        action_text = "No clean roll candidate is available. Exit or wait for a better entry."

    return {
        "action_mode": action_mode,
        "action_text": action_text,
        "roll_side": threat_side,
        "roll_expiry": roll_expiry,
        "roll_strategy": roll_name,
        "roll_rr": roll_rr,
        "roll_pop": roll_pop,
        "roll_candidates": roll_candidates[:3],
        "reasons": reasons[:6],
        "watch": watch[:4],
    }


def _explicit_action_items(base: Dict[str, Any]) -> List[str]:
    rec = str(base.get("recommendation") or "HOLD").upper().strip()
    score = _safe_num(base.get("score")) or 0.0
    alerts = _critical_price_alerts(base)
    price_action = alerts[0]["action"] if alerts else "Keep monitoring the trade."
    if rec == "OPEN_SMALL":
        if score >= 45:
            size = "25-50% size"
        elif score >= 35:
            size = "25% size"
        else:
            size = "very small size only"
        return [
            f"Open with {size} only.",
            "Use defined risk, not unlimited risk.",
            price_action,
        ]
    if rec == "ADD":
        return [
            "Add only if the key level still holds.",
            "Keep the same thesis and avoid over-sizing.",
            price_action,
        ]
    if rec == "TRIM":
        return [
            "Trim risk now and protect gains.",
            "Do not wait for a full reversal.",
            price_action,
        ]
    if rec == "HEDGE":
        return [
            "Hedge or reduce size before the next adverse move.",
            "Use a defined-risk hedge if possible.",
            price_action,
        ]
    if rec == "EXIT":
        return [
            "Exit or close on the next orderly opportunity.",
            "Do not add to a trade that is already broken.",
            price_action,
        ]
    return [
        "Hold and wait for confirmation.",
        "Monitor the nearest critical price alert.",
        price_action,
    ]


def _augment_analysis(base: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(base or {})
    base["score"] = _safe_num(base.get("score")) or 0.0
    base["confidence"] = _confidence_from_analysis(base)
    base["score_meaning"] = _score_meaning(base.get("score"))
    base["confidence_meaning"] = _confidence_meaning(base.get("confidence"))
    base["explicit_action"] = _explicit_recommendation_text(base)
    base["critical_price_alerts"] = _critical_price_alerts(base)
    base["action_steps"] = _explicit_action_items(base)
    return base


def build_trade_alert_analysis(symbol: str, trade_type: str, live: Optional[Dict[str, Any]] = None, trade: Optional[Dict[str, Any]] = None, strike_summary: str = "—") -> Dict[str, Any]:
    """Fast AI-style analysis for alerts using already-computed live data."""
    live = dict(live or {})
    trade = dict(trade or {})
    raw_score = _safe_num(live.get("trade_health_score") or live.get("probability_score") or trade.get("score") or 0) or 0.0
    base: Dict[str, Any] = {
        "symbol": (symbol or trade.get("symbol") or "").upper(),
        "trade_type": trade_type or trade.get("trade_type") or "",
        "trade_family": _trade_family(trade_type or trade.get("trade_type") or ""),
        "score": raw_score,
        "grade": _grade_from_score(raw_score),
        "recommendation": live.get("trade_action") or live.get("recommendation") or trade.get("recommendation") or "HOLD",
        "summary": live.get("rec_reason") or live.get("action_reason") or "",
        "spot": live.get("spot") or trade.get("spot"),
        "dte": live.get("dte") or trade.get("dte"),
        "pnr": live.get("pnr") or trade.get("pnr"),
        "pnr_upper": live.get("pnr_upper") or trade.get("pnr_upper"),
        "pnr_status": live.get("pnr_status") or trade.get("pnr_status") or "",
        "pnl": live.get("unrealised_pnl") or trade.get("unrealised_pnl"),
        "iv_rank": live.get("iv_rank") or trade.get("iv_rank"),
        "oi_signal": live.get("oi_signal") or trade.get("oi_signal") or "",
        "regime_name": live.get("regime_name") or trade.get("regime_name") or "",
        "regime_bias": live.get("regime_bias") or trade.get("regime_bias") or "",
        "expiry": trade.get("expiry") or live.get("expiry") or "",
        "strike_summary": strike_summary,
        "call_wall": trade.get("call_wall") or live.get("call_wall"),
        "put_wall": trade.get("put_wall") or live.get("put_wall"),
        "gamma_flip": trade.get("gamma_flip") or live.get("gamma_flip"),
        "max_pain": trade.get("max_pain") or live.get("max_pain"),
        "expected_move": trade.get("expected_move") or live.get("expected_move"),
        "roll_candidates": list(live.get("roll_candidates") or trade.get("roll_candidates") or []),
        "roll_expiry": live.get("roll_expiry") or trade.get("roll_expiry") or "",
        "threat_side": live.get("threat_side") or trade.get("threat_side") or "",
    }
    base = _augment_analysis(base)
    base = _apply_decision_fields(base, context="alert")
    practical = _practical_trade_plan(base, context="alert")
    if practical:
        base["action_mode"] = practical.get("action_mode")
        base["roll_side"] = practical.get("roll_side")
        base["roll_expiry"] = practical.get("roll_expiry")
        base["roll_strategy"] = practical.get("roll_strategy")
        base["roll_rr"] = practical.get("roll_rr")
        base["roll_pop"] = practical.get("roll_pop")
        base["roll_candidates"] = practical.get("roll_candidates") or base.get("roll_candidates") or []
        base["action_text"] = practical.get("action_text") or base.get("explicit_action")
        base["watch_items"] = practical.get("watch") or []
        if practical.get("reasons"):
            base["decision_reasons"] = practical.get("reasons")
        # Make the visible recommendation and action text agree.
        if practical.get("action_mode") in {"ROLL", "ROLL_SIDE", "EXIT", "TRIM", "WATCH", "HOLD"}:
            base["recommendation"] = practical.get("action_mode")
            base["decision_phrase"] = practical.get("action_text") or base.get("decision_phrase")
            base["explicit_action"] = practical.get("action_text") or base.get("explicit_action")
            base["action_steps"] = [practical.get("action_text")] + practical.get("watch", [])
            base["next_actions"] = base["action_steps"]
    base["thesis"] = [x for x in [
        ('Regime: ' + str(base.get('regime_name') or '—') + (f" ({base.get('regime_bias')})" if base.get('regime_bias') else '')),
        f"RS vs SPY: {base.get('rs_vs_spy'):+.1f}%" if base.get("rs_vs_spy") is not None else None,
        f"IV rank: {float(base['iv_rank']):.0f}%" if base.get("iv_rank") is not None else None,
        f"Strikes: {strike_summary}" if strike_summary and strike_summary != "—" else None,
        f"PNR: {base.get('pnr')}" if base.get("pnr") is not None else None,
    ] if x]
    base["missing_inputs"] = []
    base["monitor_items"] = [
        "support / resistance zone",
        "PNR / strike boundary",
        "relative strength vs SPY",
        "IV rank / premium regime",
        "earnings proximity",
    ]
    return base

def _missing_inputs(base: Dict[str, Any]) -> List[str]:
    missing: List[str] = []
    for key, label in _MISSING_LABELS:
        val = base.get(key)
        if val in (None, "", [], {}):
            missing.append(label)
    return missing


def _next_actions(base: Dict[str, Any]) -> List[str]:
    rec = (base.get("recommendation") or "").upper()
    grade = (base.get("grade") or "").upper()
    actions: List[str] = []
    if rec == "AVOID":
        actions.append("Skip the trade unless one of the risks clears.")
        actions.append("Wait for a cleaner regime / RS / wall alignment.")
        actions.append("Only revisit if the setup improves on the next pullback.")
    elif rec == "OPEN_SMALL":
        actions.append("Use reduced size or tighter risk.")
        actions.append("Prefer defined-risk structure and avoid over-sizing.")
        actions.append("Place alerts on the nearest support/resistance zone.")
    else:
        actions.append("Proceed with normal size if liquidity and spread are acceptable.")
        actions.append("Set alerts around the key wall / support zone.")
        actions.append("Record the thesis so the journal can learn from the outcome.")
    if grade in ("A", "B"):
        actions.append("This is a candidate to watch for add-on strength or retest confirmation.")
    return actions[:4]


def build_entry_analysis(symbol: str, trade_type: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return an AI-style trade entry analysis from the existing journal snapshot logic."""
    try:
        base = preview_entry_score(symbol, trade_type)
    except Exception as exc:
        base = {
            "symbol": symbol,
            "trade_type": trade_type,
            "score": 50,
            "grade": "C",
            "recommendation": "OPEN_SMALL",
            "summary": f"AI snapshot unavailable: {exc}",
            "pros": [],
            "cons": ["Snapshot fetch failed"],
        }
    base = dict(base or {})
    base["symbol"] = symbol
    base["trade_type"] = trade_type
    base["trade_family"] = _trade_family(trade_type)
    base = _augment_analysis(base)
    base = _apply_decision_fields(base, context="entry")
    base["thesis"] = _thesis_bullets(base)
    base["risks"] = _risk_bullets(base)
    base["missing_inputs"] = _missing_inputs(base)
    base["monitor_items"] = [
        "support / resistance zone",
        "relative strength vs SPY",
        "IV rank / premium regime",
        "PCR and wall shifts",
        "earnings proximity",
    ]
    if payload:
        base["input_snapshot"] = {
            k: payload.get(k) for k in (
                "symbol", "trade_type", "entry_date", "expiry", "quantity",
                "long_strike", "short_strike", "entry_price"
            ) if k in payload
        }
    return base


def _score_to_state(score: float) -> str:
    if score >= 80:
        return "strong"
    if score >= 65:
        return "good"
    if score >= 50:
        return "watch"
    if score >= 35:
        return "weak"
    return "avoid"


def summarize_portfolio_review(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarise open trades with an AI-style coaching layer."""
    rows: List[Dict[str, Any]] = []
    for t in trades or []:
        score = _safe_num(t.get("trade_health_score"))
        if score is None:
            score = _safe_num(t.get("probability_score"))
        if score is None:
            score = _safe_num(t.get("score")) or 0.0
        action = (t.get("action") or t.get("trade_action") or t.get("recommendation") or "HOLD").upper()
        pnl = _safe_num(t.get("unrealised_pnl"))
        if pnl is None:
            pnl = _safe_num(t.get("pnl"))
        rows.append({
            "id": t.get("id"),
            "symbol": (t.get("symbol") or "").upper(),
            "trade_type": t.get("trade_type") or "",
            "score": round(score, 1),
            "state": _score_to_state(score),
            "action": action,
            "pnl": pnl,
            "dte": t.get("dte"),
            "reason": t.get("action_reason") or t.get("rec_reason") or "",
            "spot": _safe_num(t.get("spot")),
            "notes": list(t.get("probability_notes") or []),
            "signals": list(t.get("signal_factors") or []),
            "severity": t.get("severity") or t.get("health_severity") or "",
        })

    rows.sort(key=lambda x: (x["score"], x["pnl"] or -9999))
    count = len(rows)
    avg_score = round(mean([r["score"] for r in rows]), 1) if rows else 0.0
    attention = [r for r in rows if r["score"] < 50 or r["action"] in {"EXIT", "TRIM", "HEDGE"}]
    best = sorted(rows, key=lambda x: x["score"], reverse=True)[:3]
    worst = rows[:3]

    by_type = Counter(r["trade_type"] or "?" for r in rows)
    by_state = Counter(r["state"] for r in rows)
    by_action = Counter(r["action"] for r in rows)
    by_symbol = Counter(r["symbol"] for r in rows)
    top_symbols = [s for s, _ in by_symbol.most_common(5)]

    recommendation: List[str] = []
    if attention:
        recommendation.append(f"{len(attention)} open trades need attention based on current health score or action state.")
    if worst:
        recommendation.append(f"Most fragile name: {worst[0]['symbol']} ({worst[0]['score']:.0f}).")
    if best:
        recommendation.append(f"Strongest current position: {best[0]['symbol']} ({best[0]['score']:.0f}).")
    if by_action.get("EXIT") or by_action.get("TRIM") or by_action.get("HEDGE"):
        recommendation.append("Reduce risk on trades that are already triggering exit or hedge logic.")
    if len(top_symbols) >= 3:
        recommendation.append(f"Exposure is concentrated in {', '.join(top_symbols[:3])}.")

    learning = []
    for state in ("strong", "good", "watch", "weak", "avoid"):
        n = by_state.get(state, 0)
        if n:
            learning.append(f"{state.title()}: {n}")

    return {
        "open_count": count,
        "avg_score": avg_score,
        "best": best,
        "worst": worst,
        "attention": attention[:6],
        "by_type": dict(by_type),
        "by_state": dict(by_state),
        "by_action": dict(by_action),
        "top_symbols": top_symbols,
        "recommendation": recommendation,
        "learning": learning,
        "summary": (
            f"{count} open trades. {len(attention)} need attention. "
            f"Average health score {avg_score:.1f}/100."
        ),
    }


def rank_scanner_results(results: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Rank scanner candidates using a simple AI-style composite score."""
    ranked: List[Dict[str, Any]] = []
    for row in results or []:
        try:
            score = _safe_num(row.get("score"))
            if score is None:
                score = _safe_num(row.get("trade_health_score"))
            if score is None:
                score = _safe_num(row.get("probability_score"))
            if score is None:
                score = 0.0
        except Exception:
            score = 0.0
        rs = _safe_num(row.get("rs_vs_spy") or row.get("rs_rank")) or 0.0
        ivr = _safe_num(row.get("iv_rank")) or 0.0
        flow = _safe_num(row.get("flow_score")) or 0.0
        bonus = 0.0
        bonus += max(min(rs, 20.0), -20.0) * 0.5
        bonus += (50.0 - abs(ivr - 50.0)) * 0.08
        bonus += max(min(flow, 100.0), 0.0) * 0.12
        ai_score = max(0.0, min(100.0, score * 0.7 + bonus))
        ranked.append({
            **dict(row),
            "ai_score": round(ai_score, 1),
            "ai_rank_reason": (
                f"Base {score:.0f} + RS/IV/flow adjustments -> {ai_score:.1f}"
            ),
        })
    ranked.sort(key=lambda x: x["ai_score"], reverse=True)
    return {
        "count": len(ranked),
        "top": ranked[:15],
        "summary": f"Ranked {len(ranked)} scanner results using score, RS, IV, and flow context.",
    }
