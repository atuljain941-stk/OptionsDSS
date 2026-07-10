"""Strike-level OI-change significance helpers.

These helpers are intentionally small and dependency-free so the same liquidity
and threshold rules can be reused by Dashboard, Aggregate, GEX Plan, and Weekly
Plan without adding Scanner Builder primitives.
"""
from __future__ import annotations

import math
import os
from typing import Any, Dict, Iterable, Optional


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        f = float(str(value).replace(",", "").strip())
        return f if math.isfinite(f) else default
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    f = _safe_float(value, None)
    return default if f is None else int(f)


def _env_int(name: str, default: int) -> int:
    return _safe_int(os.environ.get(name), default)


def _env_float(name: str, default: float) -> float:
    v = _safe_float(os.environ.get(name), None)
    return float(default if v is None else v)


def rows_total_oi(rows: Iterable[Dict[str, Any]] | None) -> int:
    total = 0
    for row in rows or []:
        total += _safe_int((row or {}).get("oi"), 0)
    return int(total)


def threshold_from_args(args: Any, default: float = 30.0) -> float:
    """Return user-entered ΔOI percent threshold from Flask request args."""
    for key in ("min_change_pct", "oi_sig_pct", "oi_change_pct_threshold", "threshold_pct"):
        try:
            if args.get(key) not in (None, ""):
                val = _safe_float(args.get(key), default)
                return max(0.0, float(default if val is None else val))
        except Exception:
            pass
    return float(default)


def build_oi_change_filter_context(
    symbol: str,
    rows: Iterable[Dict[str, Any]] | None,
    expiry: Optional[str] = None,
    source: str = "",
    total_oi: Optional[int] = None,
    min_change_pct: Optional[float] = None,
    min_expiry_total_oi: Optional[int] = None,
    min_strike_oi: Optional[int] = None,
    min_abs_change: Optional[int] = None,
) -> Dict[str, Any]:
    """Build liquidity gate + user threshold context for strike ΔOI signals."""
    sym = str(symbol or "").upper().strip()
    expiry_total = int(total_oi if total_oi is not None else rows_total_oi(rows))
    min_expiry_total = int(min_expiry_total_oi if min_expiry_total_oi is not None else _env_int("OI_SIGNIFICANCE_MIN_EXPIRY_TOTAL_OI", 50000))
    strike_oi = int(min_strike_oi if min_strike_oi is not None else _env_int("OI_SIGNIFICANCE_MIN_STRIKE_OI", 1000))
    abs_change = int(min_abs_change if min_abs_change is not None else _env_int("OI_SIGNIFICANCE_MIN_ABS_CHANGE", 500))
    change_pct = float(min_change_pct if min_change_pct is not None else _env_float("OI_SIGNIFICANCE_MIN_CHANGE_PCT", 30.0))
    change_pct = max(0.0, change_pct)
    qualified = bool(expiry_total >= min_expiry_total)
    reason = (
        f"qualified: total expiry OI {expiry_total:,} >= {min_expiry_total:,}"
        if qualified else
        f"not qualified: total expiry OI {expiry_total:,} < {min_expiry_total:,}; OI change ignored"
    )
    return {
        "enabled": True,
        "qualified": qualified,
        "symbol": sym,
        "expiry": str(expiry or "")[:10] if expiry else None,
        "source": str(source or ""),
        "expiry_total_oi": expiry_total,
        "min_expiry_total_oi": min_expiry_total,
        "min_strike_oi": strike_oi,
        "min_abs_change": abs_change,
        "min_change_pct": change_pct,
        "reason": reason,
        "method": (
            f"Strike-level OI change counts only when expiry total OI >= {min_expiry_total:,}, "
            f"abs(Delta OI) >= {abs_change:,}, abs(Delta OI%) >= {change_pct:g}%, "
            f"and max(prev OI,current OI) >= {strike_oi:,}."
        ),
    }


def oi_change_sig_flags(oi: Any, prev_oi: Any, oi_change: Any, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return strike-level OI-change significance flags for a strike row."""
    oi_i = _safe_int(oi, 0)
    prev_i = _safe_int(prev_oi, 0)
    change_i = _safe_int(oi_change, 0)
    base_oi = max(oi_i, prev_i)
    # Percent change is normally measured from previous OI.  A newly built
    # strike can have prev_oi=0, where the mathematical percent change is
    # undefined/infinite.  For significance filtering we should still allow a
    # large new strike build to pass when it also passes the absolute-change
    # and strike-size liquidity gates.  Treat that as a 100% build for display
    # and threshold comparison; tiny new strikes are still rejected by the size
    # gates below.
    if prev_i:
        pct = change_i / max(1, prev_i) * 100.0
    elif change_i > 0 and oi_i > 0:
        pct = 100.0
    else:
        pct = None
    if not ctx:
        sig_build = change_i > 0
        sig_remove = change_i < -max(500, oi_i * 0.10)
        reason = "legacy wall scoring"
    else:
        qualified = bool(ctx.get("qualified"))
        min_strike = _safe_int(ctx.get("min_strike_oi"), 0)
        min_abs = _safe_int(ctx.get("min_abs_change"), 0)
        min_pct = float(_safe_float(ctx.get("min_change_pct"), 0.0) or 0.0)
        pct_abs_ok = pct is not None and abs(pct) >= min_pct
        abs_ok = abs(change_i) >= min_abs
        size_ok = base_oi >= min_strike
        sig_build = bool(qualified and change_i > 0 and pct_abs_ok and abs_ok and size_ok)
        sig_remove = bool(qualified and change_i < 0 and pct_abs_ok and abs_ok and size_ok)
        if not qualified:
            reason = str(ctx.get("reason") or "expiry total OI below threshold")
        elif not size_ok:
            reason = f"strike OI {base_oi:,} below {min_strike:,}"
        elif not abs_ok:
            reason = f"abs Delta OI {abs(change_i):,} below {min_abs:,}"
        elif not pct_abs_ok:
            reason = f"abs Delta OI% {abs(pct or 0):.1f}% below {min_pct:g}%"
        else:
            reason = "significant OI build" if sig_build else "significant OI removal" if sig_remove else "OI change not directional"
    return {
        "pct": round(pct, 2) if pct is not None else None,
        "base_oi": int(base_oi),
        "significant_build": bool(sig_build),
        "significant_removal": bool(sig_remove),
        "significant": bool(sig_build or sig_remove),
        "direction": "build" if sig_build else "removal" if sig_remove else "none",
        "reason": reason,
    }


def annotate_oi_change_row(row: Dict[str, Any], ctx: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = dict(row or {})
    flags = oi_change_sig_flags(out.get("oi", out.get("latest_oi")), out.get("prev_oi"), out.get("oi_change"), ctx)
    out["oi_change_pct"] = flags.get("pct") if out.get("oi_change_pct") is None else out.get("oi_change_pct")
    out["oi_change_significant"] = flags.get("significant")
    out["oi_change_significant_build"] = flags.get("significant_build")
    out["oi_change_significant_removal"] = flags.get("significant_removal")
    out["oi_change_significance_reason"] = flags.get("reason")
    out["oi_change_base_oi"] = flags.get("base_oi")
    return out
