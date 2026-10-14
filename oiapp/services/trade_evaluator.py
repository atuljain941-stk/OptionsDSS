"""Canonical option-trade evaluation shared by strategy scanners."""
from __future__ import annotations
import math
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

def _finite(value: Any) -> Optional[float]:
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except Exception:
        return None

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _dte(expiry: str, as_of: Optional[str] = None) -> int:
    try:
        anchor = datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date() if as_of else date.today()
        return (datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date() - anchor).days
    except Exception:
        return 0

def _quote(quote_map: Dict[Tuple[str, float], dict], option_type: str, strike: float) -> dict:
    side, k = str(option_type or "").lower(), float(strike)
    for candidate in (k, round(k, 4), round(k, 2), round(k)):
        row = quote_map.get((side, candidate))
        if row:
            return row
    return {}

def _entry_price(row: dict, action: str, supplied: Any = None) -> Tuple[Optional[float], str]:
    preferred = row.get("bid") if action == "sell" else row.get("ask")
    p = _finite(preferred)
    if p is not None and p > 0:
        return p, "bid" if action == "sell" else "ask"
    for key in ("mid", "price", "last"):
        p = _finite(row.get(key))
        if p is not None and p > 0:
            return p, key
    p = _finite(supplied)
    return (p, "supplied") if p is not None and p > 0 else (None, "missing")

def _expiry_payoff(legs: List[dict], spot: float) -> float:
    pnl = 0.0
    for leg in legs:
        k = float(leg["strike"])
        intrinsic = max(spot - k, 0.0) if leg["option_type"] == "call" else max(k - spot, 0.0)
        sign = 1.0 if leg["side"] == "buy" else -1.0
        pnl += sign * (intrinsic - float(leg["entry_price"]))
    return pnl

def _payoff_stats(legs: List[dict], spot: float) -> Dict[str, Any]:
    lo, hi = max(0.01, spot * 0.5), spot * 1.5
    grid = [lo + (hi - lo) * i / 800.0 for i in range(801)]
    vals = [_expiry_payoff(legs, s) for s in grid]
    up, down = [], []
    for i in range(1, len(grid)):
        a, b = vals[i - 1], vals[i]
        if a <= 0 < b:
            f = abs(a) / (abs(a) + abs(b)) if abs(a) + abs(b) else 0.0
            up.append(grid[i - 1] + f * (grid[i] - grid[i - 1]))
        elif a >= 0 > b:
            f = abs(a) / (abs(a) + abs(b)) if abs(a) + abs(b) else 0.0
            down.append(grid[i - 1] + f * (grid[i] - grid[i - 1]))
    max_profit, max_loss = max(vals), min(vals)
    risk, reward = abs(max_loss) if max_loss < 0 else None, max_profit if max_profit > 0 else None
    return {
        "max_profit": round(max_profit, 4), "max_loss": round(max_loss, 4),
        "rr_value": round(reward / risk, 4) if reward is not None and risk not in (None, 0) else None,
        "breakeven_lower": round(min(up), 4) if up else None,
        "breakeven_upper": round(max(down), 4) if down else None,
    }

def _model_pop(spot: float, lower: Optional[float], upper: Optional[float],
               dte: int, iv: Optional[float]) -> Optional[float]:
    sigma = _finite(iv)
    if spot <= 0 or dte <= 0 or sigma is None or sigma <= 0:
        return None
    st = sigma * math.sqrt(dte / 365.0)
    if st <= 0:
        return None
    def z(bound: float) -> float:
        return (math.log(bound / spot) + 0.5 * sigma * sigma * dte / 365.0) / st
    hi = 1.0 if upper is None else _norm_cdf(z(upper))
    lo = 0.0 if lower is None else _norm_cdf(z(lower))
    return round(max(0.0, min(1.0, hi - lo)), 4)

def evaluate_legs(legs: Iterable[dict], quote_map: Dict[Tuple[str, float], dict],
                  spot: float, expiry: str, fallback_iv: Optional[float] = None,
                  as_of: Optional[str] = None) -> Dict[str, Any]:
    raw_legs = list(legs)
    normalized, warnings, ivs = [], [], []
    total_credit = 0.0
    for raw in raw_legs:
        action = str(raw.get("side") or raw.get("action") or "").lower()
        action = "sell" if action.startswith("s") else "buy" if action.startswith("b") else action
        option_type = str(raw.get("option_type") or raw.get("type") or "").lower()
        strike = _finite(raw.get("strike"))
        if action not in ("sell", "buy") or option_type not in ("call", "put") or strike is None:
            warnings.append("invalid leg definition")
            continue
        row = _quote(quote_map, option_type, strike)
        price, source = _entry_price(row, action, raw.get("entry_price"))
        if price is None:
            warnings.append(f"missing quote for {action} {strike:g}{option_type[0].upper()}")
            continue
        iv = _finite(row.get("iv")) or _finite(raw.get("iv")) or _finite(fallback_iv)
        if iv is not None:
            ivs.append(iv)
        normalized.append({
            "side": action, "option_type": option_type, "strike": float(strike),
            "entry_price": round(price, 4), "price_source": source,
            "bid": _finite(row.get("bid")), "ask": _finite(row.get("ask")),
            "iv": iv, "oi": int(_finite(row.get("oi")) or 0),
            "volume": int(_finite(row.get("volume")) or 0),
        })
        total_credit += price if action == "sell" else -price
    total_credit = round(total_credit, 4)
    calls = [l for l in normalized if l["option_type"] == "call"]
    puts = [l for l in normalized if l["option_type"] == "put"]
    call_width = max((l["strike"] for l in calls), default=0) - min((l["strike"] for l in calls), default=0) if len(calls) >= 2 else None
    put_width = max((l["strike"] for l in puts), default=0) - min((l["strike"] for l in puts), default=0) if len(puts) >= 2 else None
    widths = [w for w in (call_width, put_width) if w and w > 0]
    width = min(widths) if widths else None
    stats = _payoff_stats(normalized, float(spot)) if normalized else {
        "max_profit": None, "max_loss": None, "rr_value": None,
        "breakeven_lower": None, "breakeven_upper": None,
    }
    if total_credit > 0 and width is not None and total_credit >= width:
        warnings.append(f"credit {total_credit:.2f} is not below narrowest width {width:.2f}")
    if any(l["price_source"] not in ("bid", "ask") for l in normalized):
        warnings.append("one or more legs lack an executable bid/ask quote")
    avg_iv = sum(ivs) / len(ivs) if ivs else None
    dte = _dte(expiry, as_of)
    pop = _model_pop(float(spot), stats["breakeven_lower"], stats["breakeven_upper"], dte, avg_iv)
    rr_value = stats["rr_value"]
    breakeven_pop = (1.0 / (1.0 + rr_value)) if rr_value is not None and rr_value >= 0 else None
    expectancy_r = (pop * rr_value - (1.0 - pop)) if pop is not None and rr_value is not None else None
    return {
        "dte": dte, "breakeven_pop": round(breakeven_pop, 4) if breakeven_pop is not None else None,
        "pop_edge": round(pop - breakeven_pop, 4) if pop is not None and breakeven_pop is not None else None,
        "expectancy_r": round(expectancy_r, 4) if expectancy_r is not None else None,
        "legs": normalized, "net_credit": total_credit,
        "spread_width": round(width, 4) if width is not None else None,
        "max_profit": round(stats["max_profit"], 4) if stats["max_profit"] is not None else None,
        "max_loss": round(stats["max_loss"], 4) if stats["max_loss"] is not None else None,
        "rr_value": stats["rr_value"], "breakeven_lower": stats["breakeven_lower"],
        "breakeven_upper": stats["breakeven_upper"], "model_pop": pop,
        "avg_iv": round(avg_iv, 6) if avg_iv is not None else None,
        "warnings": warnings, "valid": len(normalized) == len(raw_legs) and bool(normalized) and not any("credit " in w for w in warnings),
    }

def annotate_oi(result: Dict[str, Any], quote_map: Dict[Tuple[str, float], dict],
                min_share_pct: float = 0.0) -> Dict[str, Any]:
    call_total = sum(int(_finite(v.get("oi")) or 0) for (side, _), v in quote_map.items() if side == "call")
    put_total = sum(int(_finite(v.get("oi")) or 0) for (side, _), v in quote_map.items() if side == "put")
    result["expiry_call_oi"], result["expiry_put_oi"] = call_total, put_total
    short_pass = True
    for leg in result.get("legs", []):
        total = call_total if leg["option_type"] == "call" else put_total
        oi = int(leg.get("oi") or 0)
        leg["oi_side_total"] = total
        leg["oi_pct_of_side_total"] = round(oi / total * 100, 2) if total else None
        if leg["side"] == "sell" and total and oi / total * 100 < min_share_pct:
            short_pass = False
    result["strike_oi_filter"] = {"min_share_pct": min_share_pct, "short_legs_pass": short_pass}
    return result
