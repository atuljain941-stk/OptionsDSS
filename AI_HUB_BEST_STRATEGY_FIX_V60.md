# AI Hub Best Strategy Router Fix V60

## Problem fixed

A question such as:

```text
What is the best strategy for NOW expiring July 19?
```

was incorrectly routed as a specific trade evaluation and defaulted to a bull put spread (`PS`). That made the hub return an avoided PS candidate instead of searching for the best available strategy.

## New behavior

Unconstrained best-strategy questions now use a dedicated `best_strategy` intent. The hub evaluates all supported structures for the requested symbol and expiry:

- `PS` bull put credit spread
- `CS` bear call credit spread
- `IC` iron condor
- `CALL` long call / call debit
- `PUT` long put / put debit

Each structure is scored with the same rules used elsewhere in the app:

- UAE checklist
- Agentic market and sector regime
- Relative strength versus market and sector
- Price action and volume
- Option OI / GEX / wall context
- Futures OI alignment
- Chain-derived pricing, risk/reward, max loss, and OI where available

The hub returns the highest-ranked candidate that qualifies as `OPEN` or `OPEN_SMALL`. If every candidate is `AVOID`, the answer starts with `NO TRADE` and clearly states that no strategy should be opened now. Avoided candidates are only shown as diagnostic near-misses, not as suggested trades.

## Examples

```text
What is the best strategy for NOW expiring July 19?
```

Routes to `best_strategy` and searches across PS, CS, IC, CALL, and PUT.

```text
What is the best bull put spread for NOW expiring July 19?
```

Routes to `specific_trade` with `trade_type=PS` and finds the best chain-selected bull put spread only.

```text
Is it okay to open a new trade NOW PS 95/90 for 6/26/26 expiry?
```

Routes to `specific_trade` with explicit PS strikes and validates exactly those strikes.

## Safety guard added

The hidden default-to-PS behavior was removed. A plain ticker plus expiry will no longer silently become a bull put spread. The user must either ask for the best strategy or specify a concrete trade type / strikes.

## Packaging note

The patched ZIP is built with database files excluded. Files matching `*.db`, `*.sqlite`, and `*.sqlite3` are intentionally not included.
