# AI Hub Conversational Assistant (v5.5)

This build adds a deterministic Conversational AI Hub at `/ai-hub` and embeds it in the main dashboard as the **AI Hub** tab.

## What it does

The AI Hub accepts natural-language trading questions and routes them into the app's existing data-backed engines instead of guessing. It can answer questions such as:

- `Show me top momentum retests under $50`
- `What is the best bull put spread for NVDA expiring July 19?`
- `Which open trades should I roll today?`
- `Go analyze all my open trades and suggest their health and appropriate actions`
- `Is it okay to open a new trade NOW PS 95/90 for 6/26/26 expiry?`
- `Run the Agentic scanner for 45 DTE and show top trade ideas`

## Data/rules used

The hub uses existing app components:

- Momentum retrace scanner and `rank_scanner_results` unified ranking.
- UAE trade scanner option-chain helpers and OI/GEX pressure helpers.
- Agentic AI scanner market/sector regime, RS, futures OI, sector context, and exact option-chain enrichment logic.
- Journal health engine for live P&L, PNR, AI alert analysis, roll review, and roll candidate generation.
- Stored Agentic AI scanner findings from `agentic_ai_findings`.

## Supported intent routing

The router is deterministic and conservative:

| Intent | Example | Behavior |
| --- | --- | --- |
| `momentum_retests` | `Show me top momentum retests under $50` | Runs the momentum retrace scanner, applies price cap/top-N filters, ranks candidates with unified scanner scoring, and returns reasons/signals. |
| `specific_trade` | `Is it okay to open NOW PS 95/90 6/26/26?` | Resolves symbol, trade type, expiry, and strikes; loads the real option chain; evaluates pricing/OI; then scores using Agentic regime + UAE + RS + price/volume + OI/futures weighting. |
| `specific_trade` | `Best bull put spread for NVDA expiring July 19` | Selects a chain-backed vertical when exact strikes are not supplied, then returns the best available setup and rationale. |
| `roll_review` | `Which open trades should I roll today?` | Reviews open trades using the journal health/roll rules and returns only trades needing roll attention. |
| `open_trades_health` | `Analyze all open trades` | Runs the journal health engine across open trades and returns score, action, risks, and roll candidates where relevant. |
| `agentic_scan` | `Run the Agentic scanner for 45 DTE and show top trade ideas` | Runs the Agentic AI scanner with the parsed DTE/watchlist overrides, ranks trade ideas, persists/dedupes findings through the existing scanner rules, and returns the top ideas. |
| `agentic_findings` | `Show latest AI scanner finds` | Reads stored Agentic scanner findings from SQLite. |

## Persistence

Each AI Hub query is saved in SQLite table `ai_hub_queries` with:

- timestamp
- original question
- detected intent
- parsed parameters
- answer text
- complete response JSON
- ok/error status

This creates an auditable history of what the assistant answered and the data/rules it used.

## Important behavior

The hub is intentionally not a free-form hallucinating chatbot. If required data is unavailable, such as missing option-chain rows for a requested expiry/strike, it returns an `AVOID / DATA MISSING` style response and identifies the missing piece. It does not invent strikes, credits, open interest, P&L, or trade health.

## Files added/changed

Added:

- `oiapp/ai/ai_hub.py`
- `templates/ai_hub.html`
- `AI_HUB_V55.md`

Changed:

- `oiapp/app_factory.py` registers `ai_hub_bp` at `/ai-hub`.
- `templates/index.html` adds an AI Hub dashboard tab/iframe and navigation entries.
- `oiapp/static/app.js` adds `ai-hub` to the default favorites.
