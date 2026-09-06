# Scanner Builder: `let` Bindings — V1

Adds lightweight local variables within a single scan query, e.g.:

```
let highCls = shift(Highest(close[1w],5),6)
shift(Highest(close[1w],5),1) < highCls and CrossAbove(close,highCls)
```

Motivated by a real query where `shift(Highest(close[1w],5),6)` appeared
twice — both a readability problem (repeated subexpression) and a genuine
performance one (no memoization existed anywhere in the evaluator, so
identical subexpressions really were recomputed once per occurrence).

## Design: zero new AST node types

The tempting implementation is a new `LetExprNode`/`LetRefNode` pair — but
this codebase has **~20 separate functions** that pattern-match on the
existing `Node` subclasses (`_node_to_text`, `_flatten_atoms`,
`_collect_function_periods`, `_required_timeframes`, `optimize_query`,
`_estimate_node_cost`, `_eval`, and more) for things like history-window
sizing, timeframe discovery, cost estimation, and the "reason" breakdown
shown in scan results. New node types would mean touching some subset of
all twenty, with real risk of missing one and causing a silent bug (e.g.
under-fetching history for anything hidden inside a `let` binding).

Instead:
1. Each `let NAME = EXPR` binding is parsed with the **existing** `_Parser`
   — no grammar/parser changes at all.
2. Every later reference to `NAME` is replaced with a **shared reference**
   to that same parsed `Node` object (not a text copy, not a deep clone).
3. `_eval` gained a thin memoizing wrapper (`_eval` now delegates to the
   renamed `_eval_inner`), keyed by `(id(node), shift, tf_default)`, stored
   in `ctx["_eval_memo"]` (`ctx` is already rebuilt fresh per symbol, so
   the cache is correctly scoped and never leaks across symbols).

Net effect: the final AST is built entirely out of the same `Node`
subclasses every existing function already understands — a `let`
reference is just an ordinary node that happens to be shared by
reference from two places in the tree. All ~20 existing walkers work
completely unmodified. The memoization is what actually achieves
"evaluated once instead of once per occurrence" — object identity is
what makes the cache key match at both reference sites.

## Files changed
- `oiapp/scanners/scanner_builder.py`:
  - `_eval` renamed to `_eval_inner` (pure rename, zero logic changes);
    new thin `_eval` wrapper added with the memoization described above.
  - New: `_LET_RESERVED_NAMES`, `_LET_STMT_RE`, `_extract_let_bindings()`,
    `_substitute_let_refs()`.
  - `_parse_query()`: if the query text contains no `let` statement, it
    takes the **exact same code path as before** (`_Parser(text).parse()`)
    — confirmed via a strict dataclass-equality test against the old
    path, not just eyeballed.

## Syntax
- `let NAME = EXPR` — terminated by `;` or a newline (nesting-aware, so
  an `EXPR` containing its own function calls/commas still terminates
  correctly at the real end of the statement).
- Multiple bindings allowed, one per line/semicolon; later bindings can
  reference earlier ones (chained lets).
- The final (non-`let`) statement is the query's actual condition.
- Binding names cannot shadow a reserved identifier (`close`, `open`,
  `high`, `low`, `volume`, `oi`, `pcr`, `beta`, all position-alert
  context keys, etc.) — raises a clear `ValueError` naming the conflict
  rather than silently producing a wrong result.
- Duplicate binding names, and a query with `let` bindings but no final
  expression, both raise clear errors.

## Verification performed before packaging
- Full `py_compile` on the modified file.
- Loaded the module in a sandbox (stubbed `oiapp.config`) and confirmed
  all new functions are present and importable.
- **Correctness**: built a synthetic weekly close-price series with a
  genuine dip-then-breakout, parsed both the manually-duplicated query
  and the `let`-based equivalent, and confirmed **identical results
  across 20+ shift values**, including the one shift where the condition
  is actually `True` (not just agreement on all-`False`, which would
  prove little).
- **Backward compatibility**: strict `dataclasses.asdict()` equality
  check between `_parse_query(plain_query)` and the old
  `_Parser(plain_query).parse()` path for a query with no `let` — proven
  byte-identical, not just "looks the same."
- **Performance**: instrumented `_eval_inner` with a call counter at the
  actual match point (where an `AND` doesn't short-circuit, so both
  reference sites of the shared subexpression genuinely get evaluated) —
  measured **156 → 107 calls, a real 31% reduction**, with identical
  results (not just fewer calls with a different answer).
- **Error paths**: confirmed reserved-name collision, duplicate binding
  name, and missing-final-body all raise the intended `ValueError`.
- **Downstream walkers**: confirmed `_node_to_text`, `_flatten_atoms`
  (used for the "reason" per-clause breakdown in the scan results UI),
  `_required_timeframes`, and `_collect_function_periods` (history-window
  sizing) all correctly re-expand the shared reference back into full
  text/structure with zero code changes to any of them.

## Known gaps
- I don't have your real DB or live price data, so this was verified
  against synthetic data matching the confirmed schema/context shape
  (`ctx["timeframes"][tf]["series"][name]` as a pandas Series), not a
  real end-to-end scan through the Flask route. Recommend running one
  real `let`-based query through the actual Scanner Builder UI first —
  the exact query from this conversation is a good first test:
  `let highCls = shift(Highest(close[1w],5),6)` /
  `shift(Highest(close[1w],5),1) < highCls and CrossAbove(close,highCls)`.
- The memoization cache is a general-purpose addition (keyed on object
  identity), not `let`-specific — it's a no-op for the ~99% of existing
  queries with no shared node objects, but worth knowing it's now part
  of every single scan evaluation, not just `let`-based ones.
