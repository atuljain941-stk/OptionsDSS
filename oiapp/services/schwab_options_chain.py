"""schwab_options_chain.py -- V108 addition.

Live (uncached) options-chain fetch directly from Schwab's real-money
data feed -- the same feed thinkorswim/Schwab's own platform shows,
which is why it matches broker screenshots (Schwab acquired TD
Ameritrade/thinkorswim) while yfinance's option_chain() does not.

Root cause this fixes: the existing daily options ingestion
(`market.fetch_store_for` -> `tk.option_chain(e)` in
`futures_oi_schwab`'s sibling module `market.py`) is yfinance-based.
Yahoo's options volume/OI is frequently delayed or substantially
understated versus a real broker feed -- confirmed directly against a
Schwab/thinkorswim screenshot showing ~5-8x higher volume at the same
strikes than what the app was displaying.

Deliberate scope: THIS MODULE IS FOR VOLUME ONLY, fetched live/on
every request, never written to a table or cached. Open interest
keeps coming from the existing DB-cached pipeline (`options` table /
`oi_intraday_dte_cache`) -- OI only settles once a day, so there's no
benefit to a live fetch there and it would just add unnecessary Schwab
API load. This intentionally mirrors the instruction that prompted it:
volume = live fetch, OI = DB.

Reuses the existing Schwab OAuth session (`_schwab_headers`/
`_schwab_get` from `futures_oi_schwab.py`) rather than standing up a
second auth flow -- same Schwab connection already used for futures OI.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

SCHWAB_CHAINS_URL = "https://api.schwabapi.com/marketdata/v1/chains"


def fetch_live_volume_schwab(symbol: str, expiration: Optional[str] = None, strike_count: int = 60) -> Dict[str, Any]:
    """Live per-strike call/put volume for `symbol`, optionally scoped
    to one `expiration` (YYYY-MM-DD). No DB write, no cache -- this is
    meant to be called fresh on every page load/refresh, since the
    whole point is that volume changes continuously through the day.
    """
    from .futures_oi_schwab import _schwab_get

    params: Dict[str, Any] = {
        "symbol": symbol.upper(),
        "contractType": "ALL",
        "strikeCount": strike_count,
        "includeUnderlyingQuote": "true",
    }
    if expiration:
        params["fromDate"] = expiration
        params["toDate"] = expiration

    data, err = _schwab_get(SCHWAB_CHAINS_URL, params=params)
    if err:
        return {"ok": False, "error": err, "rows": [], "underlying": None}

    rows: List[Dict[str, Any]] = []
    for map_key, typ in (("callExpDateMap", "call"), ("putExpDateMap", "put")):
        exp_map = (data or {}).get(map_key) or {}
        for exp_key, by_strike in exp_map.items():
            # Schwab keys this map as "YYYY-MM-DD:<dte>" -- split off the DTE suffix.
            exp_date = exp_key.split(":")[0]
            if expiration and exp_date != expiration:
                continue
            for strike_str, contracts in (by_strike or {}).items():
                for c in (contracts or []):
                    try:
                        rows.append({
                            "type": typ,
                            "expiration": exp_date,
                            "strike": float(strike_str),
                            "live_volume": int(c.get("totalVolume") or 0),
                            "oi_schwab_live": int(c.get("openInterest") or 0),  # informational only -- OI display uses the DB cache, not this
                            "bid": c.get("bid"),
                            "ask": c.get("ask"),
                            "iv": c.get("volatility"),
                            "delta": c.get("delta"),
                        })
                    except Exception:
                        continue

    underlying = (data or {}).get("underlyingPrice")
    return {"ok": True, "rows": rows, "underlying": underlying, "error": None}
