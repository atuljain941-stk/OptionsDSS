# oiapp/services/news_service.py
"""
Market news + pre-market digest service.
Fetches headlines from yfinance (free, no API key).
Also generates morning OI-change digest from DB.
"""
import sqlite3, time, json, math, os
from pathlib import Path
from datetime import date, datetime, timedelta

from ..config import DB_PATH as _OIAPP_DB_PATH  # centralized DB location
DB_PATH = _OIAPP_DB_PATH
_news_cache = {}  # symbol → (data, expires_at)
_NEWS_TTL = 900   # 15 min

def _conn():
    c = sqlite3.connect(DB_PATH); c.row_factory = sqlite3.Row; return c

# ── News fetch ─────────────────────────────────────────────────────────────
def _sentiment(title):
    title = (title or "").lower()
    pos = ["beat","surge","rally","record","strong","upgrade","raises","growth","profit","jumps","optimism","higher","gain"]
    neg = ["miss","fall","drop","weak","downgrade","cuts","loss","plunge","decline","warns","risk","lower","selloff"]
    if any(w in title for w in pos): return "positive"
    if any(w in title for w in neg): return "negative"
    return "neutral"


def _news_ttl_seconds(ttl_seconds=None):
    """Return a safe cache TTL for live news fetches."""
    try:
        val = int(ttl_seconds if ttl_seconds is not None else _NEWS_TTL)
    except Exception:
        val = _NEWS_TTL
    # Do not hammer free endpoints.  Five minutes is the minimum supported cadence.
    return max(300, min(val, 24 * 60 * 60))


def _cache_get(key, ttl_seconds=None):
    cached = _news_cache.get(key)
    if not cached:
        return None
    now = time.time()
    ttl = _news_ttl_seconds(ttl_seconds)
    try:
        if isinstance(cached, dict):
            fetched_at = float(cached.get("fetched_at") or 0)
            if fetched_at and now - fetched_at < ttl:
                return list(cached.get("items") or [])
        elif isinstance(cached, tuple) and len(cached) >= 2:
            items, expires_at = cached[0], float(cached[1])
            if now < expires_at:
                return list(items or [])
    except Exception:
        return None
    return None


def _cache_set(key, items):
    _news_cache[key] = {"items": list(items or []), "fetched_at": time.time()}


def _parse_pub_timestamp(value):
    if not value:
        return 0
    try:
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            raw = value.strip()
            # yfinance sometimes returns ISO strings; RSS returns RFC-822 strings.
            try:
                return int(datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S").timestamp())
            except Exception:
                pass
            try:
                from email.utils import parsedate_to_datetime
                return int(parsedate_to_datetime(raw).timestamp())
            except Exception:
                pass
    except Exception:
        pass
    return 0


def _pub_label(ts):
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M") if ts else date.today().isoformat()
    except Exception:
        return date.today().isoformat()


def fetch_market_news(symbols=None, max_per_symbol=5, ttl_seconds=None, force=False):
    """Fetch news from yfinance for watchlist symbols + general market.

    ttl_seconds is dynamic so the top-bar message board can refresh on the
    interval selected by the user without changing code.
    """
    try:
        import yfinance as yf
    except Exception:
        return []

    all_news = []

    # Always fetch SPY/QQQ/VIX market proxies plus selected symbols.
    requested = []
    for sym in ["SPY", "QQQ", "^VIX"] + list(symbols or [])[:15]:
        sym = str(sym or "").strip().upper()
        if sym and sym not in requested:
            requested.append(sym)

    for sym in requested:
        if not force:
            cached = _cache_get(f"yf:{sym}", ttl_seconds)
            if cached is not None:
                all_news.extend(cached)
                continue
        items = []
        try:
            tk = yf.Ticker(sym)
            raw_news = tk.news or []
            for n in raw_news[:max_per_symbol]:
                try:
                    # yfinance news format changed — handle both old and new formats.
                    content = n.get("content", {}) if isinstance(n, dict) else {}
                    ts = n.get("providerPublishTime") or content.get("pubDate", 0)
                    pub_ts = _parse_pub_timestamp(ts)
                    headline = (n.get("title") or content.get("title", "") or "").strip()
                    if not headline:
                        continue
                    summary = (n.get("summary") or content.get("summary", "") or "").strip()
                    url = n.get("link") or (content.get("canonicalUrl", {}) or {}).get("url", "")
                    source = n.get("publisher") or (content.get("provider", {}) or {}).get("displayName", "Yahoo Finance")
                    items.append({
                        "symbol": sym,
                        "headline": headline,
                        "summary": summary[:300] if summary else "",
                        "url": url,
                        "source": source or "Yahoo Finance",
                        "published": _pub_label(pub_ts),
                        "published_ts": pub_ts,
                        "sentiment": _sentiment(headline),
                        "category": "market" if sym in ("SPY", "QQQ", "^VIX", "DIA", "IWM") else "stock",
                    })
                except Exception:
                    continue
        except Exception:
            items = []
        _cache_set(f"yf:{sym}", items)
        all_news.extend(items)

    all_news.sort(key=lambda x: x.get("published_ts") or 0, reverse=True)
    return _dedupe_news(all_news)[:60]


def _default_rss_feeds():
    raw = os.environ.get("OIAPP_NEWS_RSS_FEEDS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ,DIA,IWM&region=US&lang=en-US",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.marketwatch.com/rss/topstories",
    ]


def _fetch_rss_feed(url, limit=12):
    import urllib.request
    import xml.etree.ElementTree as ET
    out = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 OI-Dashboard-News"})
        with urllib.request.urlopen(req, timeout=7) as resp:
            raw = resp.read(1024 * 1024)
        root = ET.fromstring(raw)
        channel_title = "RSS"
        ch = root.find("channel")
        if ch is not None:
            title_el = ch.find("title")
            if title_el is not None and title_el.text:
                channel_title = title_el.text.strip()
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            link = (item.findtext("link") or "").strip()
            pub_raw = (item.findtext("pubDate") or item.findtext("published") or "").strip()
            pub_ts = _parse_pub_timestamp(pub_raw)
            source = channel_title
            src_el = item.find("source")
            if src_el is not None and src_el.text:
                source = src_el.text.strip()
            out.append({
                "symbol": "MARKET",
                "headline": title,
                "summary": (item.findtext("description") or "")[:300],
                "url": link,
                "source": source,
                "published": _pub_label(pub_ts),
                "published_ts": pub_ts,
                "sentiment": _sentiment(title),
                "category": "market",
            })
    except Exception:
        return []
    return out


def fetch_rss_market_news(ttl_seconds=None, force=False, max_items=24):
    """Fetch no-key RSS market headlines.  Used by the topbar as a live supplement."""
    cache_key = "rss:market"
    if not force:
        cached = _cache_get(cache_key, ttl_seconds)
        if cached is not None:
            return cached[:max_items]
    items = []
    for url in _default_rss_feeds():
        items.extend(_fetch_rss_feed(url, limit=10))
    items = _dedupe_news(items)
    items.sort(key=lambda x: x.get("published_ts") or 0, reverse=True)
    _cache_set(cache_key, items)
    return items[:max_items]


def _dedupe_news(items):
    seen = set()
    out = []
    for n in items or []:
        headline = str(n.get("headline") or "").strip()
        if not headline:
            continue
        key = " ".join(headline.lower().split())[:160]
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def fetch_topbar_news(symbols=None, max_items=10, ttl_seconds=None, force=False):
    """Return latest market headlines for the scrolling topbar.

    Combines yfinance ticker headlines with no-key RSS market headlines.  Results
    are cached for the selected refresh cadence to avoid repeated network calls.
    """
    yf_items = fetch_market_news(symbols=symbols, max_per_symbol=3, ttl_seconds=ttl_seconds, force=force)
    rss_items = fetch_rss_market_news(ttl_seconds=ttl_seconds, force=force, max_items=max_items * 2)
    items = _dedupe_news((yf_items or []) + (rss_items or []))
    items.sort(key=lambda x: x.get("published_ts") or 0, reverse=True)
    return items[:max_items]


def build_topbar_news_payload(news_items, refresh_minutes=15, max_items=8):
    now_label = datetime.now().strftime("%H:%M")
    messages = []
    cleaned = []
    for n in _dedupe_news(news_items or [])[:max_items]:
        headline = str(n.get("headline") or "").strip()
        if not headline:
            continue
        source = str(n.get("source") or "News").strip()
        sym = str(n.get("symbol") or "").strip().upper()
        sym_tag = f" · {sym}" if sym and sym not in ("MARKET", "SPY", "QQQ", "^VIX") else ""
        pub = str(n.get("published") or "")[:16]
        messages.append(f"📰 {headline}{sym_tag} · {source}{' · ' + pub if pub else ''}")
        cleaned.append(n)
    if not messages:
        messages.append("📰 Latest market news unavailable. Check the Market News tab or refresh later.")
    return {
        "ok": True,
        "refresh_minutes": int(refresh_minutes or 15),
        "last_checked": now_label,
        "items": cleaned,
        "messages": messages,
        "source_note": "Latest yfinance ticker headlines plus no-key RSS market feeds",
    }


def save_news_to_db(news_items):
    con = _conn()
    today = date.today().isoformat()
    # Clear today's news first
    con.execute("DELETE FROM market_news WHERE fetch_date=?", (today,))
    for n in news_items:
        con.execute("""
            INSERT INTO market_news (fetch_date,source,headline,summary,url,symbol,sentiment,category)
            VALUES (?,?,?,?,?,?,?,?)
        """, (today, n.get("source",""), n["headline"], n.get("summary",""),
              n.get("url",""), n.get("symbol",""), n.get("sentiment","neutral"),
              n.get("category","market")))
    con.commit(); con.close()

def get_saved_news(fetch_date=None):
    con = _conn()
    d = fetch_date or date.today().isoformat()
    rows = con.execute(
        "SELECT * FROM market_news WHERE fetch_date=? ORDER BY id DESC", (d,)
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]

# ── Pre-market digest ──────────────────────────────────────────────────────
def generate_morning_digest(symbols=None):
    """
    Build a morning briefing from overnight OI changes.
    Compares last two DB snapshots across all symbols.
    """
    con = _conn()
    today_str = date.today().isoformat()

    # Get all symbols
    if not symbols:
        symbols = [r["symbol"] for r in con.execute("SELECT symbol FROM symbols").fetchall()]

    oi_signals = []
    for sym in symbols:
        # Get two latest dates for ANY expiry
        dates = [r["date"] for r in con.execute("""
            SELECT DISTINCT date FROM options WHERE symbol=?
            ORDER BY date DESC LIMIT 2
        """, (sym,)).fetchall()]
        if len(dates) < 2: continue
        d1, d2 = dates[0], dates[1]

        # Aggregate OI change by type
        changes = con.execute("""
            SELECT o1.type, SUM(o1.oi) as oi_new, SUM(o2.oi) as oi_old,
                   SUM(o1.oi - o2.oi) as delta
            FROM options o1
            JOIN options o2 ON o1.symbol=o2.symbol AND o1.expiration=o2.expiration
                AND o1.type=o2.type AND o1.strike=o2.strike
            WHERE o1.symbol=? AND o1.date=? AND o2.date=?
            GROUP BY o1.type
        """, (sym, d1, d2)).fetchall()

        put_delta = call_delta = 0
        for c in changes:
            if c["type"] == "put":  put_delta  = c["delta"] or 0
            if c["type"] == "call": call_delta = c["delta"] or 0

        total_abs = abs(put_delta) + abs(call_delta)
        if total_abs < 500: continue  # too small, skip

        pcr_direction = "neutral"
        if put_delta > 500 and put_delta > call_delta * 1.5:
            pcr_direction = "bearish_buildup"
        elif call_delta > 500 and call_delta > put_delta * 1.5:
            pcr_direction = "bullish_buildup"
        elif put_delta < -500:
            pcr_direction = "put_unwind_bullish"
        elif call_delta < -500:
            pcr_direction = "call_unwind_bearish"

        if pcr_direction == "neutral": continue

        signal_map = {
            "bearish_buildup":    ("🔴", "BEARISH", f"Put OI +{put_delta:,} vs Call OI {call_delta:+,}"),
            "bullish_buildup":    ("🟢", "BULLISH", f"Call OI +{call_delta:,} vs Put OI {put_delta:+,}"),
            "put_unwind_bullish": ("🟢", "BULLISH", f"Put OI reduced by {abs(put_delta):,} — hedges unwinding"),
            "call_unwind_bearish":("🔴", "BEARISH", f"Call OI reduced by {abs(call_delta):,}"),
        }
        icon, signal, detail = signal_map[pcr_direction]
        oi_signals.append({
            "symbol": sym, "icon": icon, "signal": signal,
            "detail": detail, "put_delta": put_delta, "call_delta": call_delta,
            "total_change": total_abs
        })

    # Sort by magnitude
    oi_signals.sort(key=lambda x: -x["total_change"])

    # Market summary from news sentiment
    news = get_saved_news()
    pos = sum(1 for n in news if n.get("sentiment") == "positive")
    neg = sum(1 for n in news if n.get("sentiment") == "negative")
    if pos > neg * 1.5:   mkt_tone = "🟢 Generally positive overnight news flow"
    elif neg > pos * 1.5: mkt_tone = "🔴 Generally negative overnight news flow"
    else:                  mkt_tone = "⚪ Mixed overnight news sentiment"

    top_bullish = [s for s in oi_signals if s["signal"]=="BULLISH"][:3]
    top_bearish = [s for s in oi_signals if s["signal"]=="BEARISH"][:3]

    digest = {
        "date": today_str,
        "market_tone": mkt_tone,
        "news_positive": pos,
        "news_negative": neg,
        "oi_signals": oi_signals[:20],
        "top_bullish_oi": top_bullish,
        "top_bearish_oi": top_bearish,
        "total_signals": len(oi_signals),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

    # Save to DB
    con.execute("""
        INSERT OR REPLACE INTO morning_digest
        (digest_date, oi_changes, market_summary, top_signals, generated_at)
        VALUES (?,?,?,?,?)
    """, (today_str, json.dumps(oi_signals[:20]),
          mkt_tone, json.dumps(top_bullish + top_bearish),
          digest["generated_at"]))
    con.commit(); con.close()

    return digest

def get_saved_digest(digest_date=None):
    con = _conn()
    d = digest_date or date.today().isoformat()
    row = con.execute("SELECT * FROM morning_digest WHERE digest_date=?", (d,)).fetchone()
    con.close()
    if not row: return None
    return {
        "date": row["digest_date"],
        "market_summary": row["market_summary"],
        "oi_changes": json.loads(row["oi_changes"] or "[]"),
        "top_signals": json.loads(row["top_signals"] or "[]"),
        "generated_at": row["generated_at"]
    }
