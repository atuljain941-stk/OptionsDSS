# Agentic AI Scanner V54

Adds a background trade scanner at `/agentic-ai-scanner` and an embedded Dashboard tab named **Agentic AI Scanner**.

## What it does

The scanner follows the requested workflow:

1. **Market regime agent**
   - Validates SPY, QQQ and IWM across 1H, Daily and Weekly UAE trend states.
   - Adds futures OI buildup/short buildup/long unwind/short covering context when stored futures OI data exists.
   - Adds index options pressure and GEX/wall context where local option OI exists.

2. **Sector regime agent**
   - Maps each stock to its sector ETF proxy using the existing sector service.
   - Scores sector ETF 1H/Daily/Weekly regime and sector relative strength versus SPY.

3. **Stock agent**
   - Uses UAE trend analyzer states.
   - Scores price action, volume ratio, RSI14, RSIDiff90, MACD histogram and relative strength versus both market and sector.

4. **Options/OI agent**
   - Looks at local option OI snapshots inside the next target DTE window, default 45 DTE.
   - Computes call/put OI, OI changes, PCR and pressure notes.
   - Uses OI wall context and GEX context where available.

5. **Decision/risk agent**
   - Produces a combined confidence score.
   - Builds strategy, expiry, strikes and action plan.
   - Uses exact option-chain enrichment when available, with fast UAE preview fallback.

## Alerts and de-duplication

- Findings are stored in `agentic_ai_findings`.
- Scanner runs are stored in `agentic_ai_scanner_runs`.
- Each setup gets a stable signature from symbol, direction, strategy type, expiry, strikes, market regime and sector regime.
- New signatures alert once only.
- Re-seen signatures update `last_seen_at` and `seen_count` without sending another alert.
- Alerts use the existing Telegram configuration if present and also write to the existing alert notification history for the notification bell / Alerts Hub.

## Controls

The page at `/agentic-ai-scanner` lets you configure:

- enabled/disabled background scanner
- watchlist
- interval seconds
- max symbols
- target DTE
- minimum confidence
- minimum UAE score
- forced trade type or AUTO
- regime alignment requirement
- max alerts per run
- spread width
- short delta

Environment override:

```bash
AGENTIC_AI_SCANNER_AUTOSTART=0
```

Set that to disable automatic watcher startup while keeping the page/API available.

## Files added or changed

Added:

- `oiapp/scanners/agentic_ai_scanner.py`
- `templates/agentic_ai_scanner.html`
- `AGENTIC_AI_SCANNER_V54.md`

Changed:

- `oiapp/app_factory.py` registers the blueprint and starts the watcher.
- `templates/index.html` adds navigation and iframe tab.
- `oiapp/static/app.js` adds the scanner to default smart favorites.
