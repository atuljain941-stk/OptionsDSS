# AI Hub Weekly Fast Path and Timeout Guard V62

## Problem fixed

Question:

```text
can you suggest best strategy for SPY for 6/26/2026
```

was being handled like a generic best-strategy request.  That path evaluates PS, CS, IC, CALL, and PUT independently and can repeatedly resolve expiries, build regime context, fetch option chains, and score each structure.  For SPY or other highly liquid weekly names with a near-term Friday expiry, this is not the desired workflow and can leave the chat waiting too long.

## New behavior

AI Hub now detects near-term liquid weekly strategy questions and routes them to the Weekly Plan path first.

A request is routed to the Weekly Plan when all of these are true:

- intent is `best_strategy`
- no explicit manual structure/strikes were supplied
- expiry DTE is inside the weekly-plan window
- symbol is SPY/QQQ/IWM/DIA or a configured liquid weekly options symbol

For the sample question above, the parser now returns:

- symbol: `SPY`
- expiry: `2026-06-26`
- DTE: `5`
- strikes: `[]`
- route: Weekly Plan

## Date no longer becomes fake strikes

The prior strike parser could treat dates like `6/26/2026` as strikes `[6, 26, 2026]`.  That made the router think the user had supplied a manual structure.  The parser now ignores slash-date fragments as strikes unless the user explicitly specifies a strategy type such as `PS 95/90` or uses clear strike wording.

## Response-time guard

AI Hub now has response budgets:

- `AI_HUB_WEEKLY_PLAN_TIMEOUT_SECONDS`, default `12`
- `AI_HUB_STRATEGY_TIMEOUT_SECONDS`, default `25`

If the dashboard Weekly Plan does not return within the weekly budget, AI Hub falls back to a fast local OI/PCR weekly model instead of leaving the conversation hanging.

## Fast local fallback

The fallback is DB-only where possible.  It uses:

- local option OI snapshot for the requested expiry
- local `price_cache` spot if available
- put/call OI walls
- PCR
- seller-side support/resistance logic
- local option prices stored in the `options` table when available

It does not guess.  If it cannot find local OI and spot data, it returns a no-trade/data-missing response.

## Generic best-strategy path optimized

For non-weekly requests, the generic all-strategy search is now bounded per structure, and it reuses cached agentic regime context.  Option-chain selection in the UAE trade scanner is now DB-first and cached, falling back to yfinance only when local chain snapshots are missing.

## Files changed

- `oiapp/ai/ai_hub.py`
- `oiapp/scanners/uae_trade_scanner.py`
