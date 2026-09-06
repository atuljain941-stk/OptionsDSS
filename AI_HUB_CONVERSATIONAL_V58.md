# Conversational AI Hub

Adds an in-app conversational layer at `/ai-hub` and a Dashboard tab named **AI Hub**.

The AI Hub is a deterministic router over existing app engines. It does not generate trade answers from free-form guesses. A question is parsed into an intent, the corresponding scanner/strategy/journal engine is run, and the response includes data-quality notes, evidence, rules used, and row-level results when available.

## Supported natural-language workflows

Examples:

- `Show me top momentum retests under $50`
- `What is the best bull put spread for NVDA expiring July 19?`
- `Which open trades should I roll today?`
- `Go analyze all my open trades and suggest health and appropriate actions`
- `Is it okay to open a new trade NOW PS 95/90 for 6/26/26 expiry?`
- `Run the Agentic scanner for 45 DTE and show top trade ideas`
- `Show latest AI scanner finds`

## What it uses

- Momentum retest scanner plus the unified scanner ranking.
- UAE trade scanner option-chain helpers, exact listed expiry/strike validation, and OI/GEX pressure helpers.
- Agentic AI scanner market/sector regime, RS, futures OI, sector context, and option-chain enrichment.
- Journal live P&L, Trade Health Score, PNR, AI alert, and roll-candidate logic for open trades.
- Local option OI snapshots and stored Agentic AI findings.

## Routes

- Page: `GET /ai-hub`
- Status/examples/watchlists: `GET /ai-hub/api/status`
- History: `GET /ai-hub/api/history?limit=30`
- Ask: `POST /ai-hub/api/ask`

Example ask payload:

```json
{
  "question": "Is it okay to open a new trade NOW PS 95/90 for 6/26/26 expiry?",
  "watchlist_id": "",
  "max_symbols": 120
}
```

## Database table added

`ai_hub_queries`

Stores the question, parsed intent, parsed parameters, answer text, structured response JSON, error text if any, and timestamp.

## No-guess behavior

If a required input is missing, a chain/price/OI row is unavailable, an expiry is not listed, or the query cannot be routed to an existing engine, AI Hub returns an explicit failure/data-quality response rather than creating a speculative answer.
