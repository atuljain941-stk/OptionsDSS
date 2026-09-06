"""confluence.py -- centralized confluence scoring, shared by every
multi-dimensional scoring system in the app (Candle Context scanner,
Journal AI health score, and anything added later).

The distinction this exists to capture: a weighted SCORE tells you how
much total evidence fired, but not how many INDEPENDENT things agree.
Two strong dimensions can carry a 7/10 score even while four other
dimensions are silent or actively disagreeing -- that's a strong-but-
narrow signal, not a broad one, and a raw score can't tell the
difference. Confluence count/ratio is the separate number that does:
independent signals agreeing is stronger evidence than one signal
being loud, and this is what makes that visible instead of hidden
inside a sum.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def compute_confluence(dimension_results: Dict[str, Optional[bool]]) -> Dict[str, Any]:
    """dimension_results maps a dimension name to:
      True  -- this dimension meaningfully agrees with the signal
      False -- this dimension was evaluated and does NOT agree (neutral
               or contradicting), still counts in the denominator
      None  -- not applicable / not evaluated for this instance (e.g.
               no OI history for this symbol) -- EXCLUDED from the
               denominator entirely, since "we didn't check" is not
               the same as "we checked and it disagreed"

    Returns agreeing/total/ratio plus a compact "N/M" label and the
    raw per-dimension breakdown, so callers can render either the
    summary or the detail without recomputing anything.
    """
    applicable = {k: v for k, v in dimension_results.items() if v is not None}
    agreeing = sum(1 for v in applicable.values() if v)
    total = len(applicable)
    return {
        "agreeing": agreeing,
        "total": total,
        "ratio": round(agreeing / total, 2) if total else None,
        "label": f"{agreeing}/{total}" if total else "n/a",
        "dimensions": dict(dimension_results),
    }
