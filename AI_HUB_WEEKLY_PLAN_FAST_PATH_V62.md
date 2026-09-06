# AI Hub Weekly Plan Fast Path V62

This patch fixes slow AI Hub responses for near-expiry strategy questions such as:

```text
can you suggest best strategy for SPY for 6/26/2026
```

## What changed

### 1. Near-expiry best-strategy questions now route to Weekly Plan

When a best-strategy request has a ticker and the requested expiry is inside the weekly-plan window, AI Hub now uses the existing Weekly Plan engine instead of the generic five-structure exact-chain search.

Default weekly window:

```text
AI_HUB_WEEKLY_PLAN_MAX_DTE=10
```

Core weekly symbols also route to Weekly Plan when the user does not specify a longer-dated expiry:

```text
SPY, QQQ, IWM, DIA, SPX, XSP
```

Liquid symbols with an explicit near-weekly expiry also route to Weekly Plan.

### 2. Weekly Plan response timeout guard

AI Hub now has a Weekly Plan time budget so the chat request does not sit indefinitely if a live market or option-chain data source stalls.

Default timeout:

```text
AI_HUB_WEEKLY_PLAN_TIMEOUT_SEC=18
```

If the Weekly Plan does not complete inside the budget, AI Hub returns a no-trade/data-timeout answer instead of guessing or hanging.

### 3. Weekly Plan strategy normalization

Weekly Plan candidates are normalized into AI Hub result rows with:

- strategy name and compact type code
- legs
- entry credit/debit text
- POP
- R/R when available
- max profit / max loss
- recommendation: OPEN, OPEN_SMALL, WATCH, or AVOID
- rationale and action text
- Weekly Plan context: bias, score, IV rank, PCR, expected move, OI/GEX walls, futures context, and timeframe profile data

### 4. UI support

The AI Hub results table now has a dedicated renderer for `weekly_plan_strategy` responses.

### 5. No database files

No `.db`, `.sqlite`, or `.sqlite3` files should be included in this package.
