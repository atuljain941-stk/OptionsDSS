# Scanner Builder — V3: Critical Crash Fix + Resample Caching

## Critical bug: every scanner query was crashing (fixed)

Your traceback: `TypeError: keys must be str, int, float, bool or None,
not tuple`, thrown from `jsonify()` inside `api_run`.

**Root cause: my own regression.** The `_eval()` memoization wrapper I
added earlier this session caches results in `ctx["_eval_memo"]`, keyed
by `(id(node), shift, tf_default)` -- a tuple. I didn't realize until
now that `api_run`'s main scan loop passes each symbol's context dict
**directly** as `ctx`:

```python
for r in snapshots:
    ok = bool(_eval(_optimized_root, r, shift=0, tf_default="1d"))
    ...
    passed.append(r)
```

`r` IS `ctx`. So `_eval()`'s memo cache was writing `_eval_memo` (tuple
keys and all) directly onto every result row, which then flows into
`jsonify()`. There's an existing cleanup step that strips a few known
ctx-only keys (`timeframes`, `options_history`, `_uae_cache`) before
serialization -- `_eval_memo` wasn't on that list, so it sailed through
and `json.dumps` correctly refused to serialize a dict with tuple keys.

**Fix, two layers:**
1. Added `_eval_memo` / `_eval_memo_keepalive` to the existing
   pre-serialization cleanup, right alongside the keys that were already
   being stripped there.
2. **Defense in depth:** `_json_safe()` previously sanitized dict
   *values* recursively but never dict *keys* -- a tuple key survived
   untouched even through the "safety" pass. It now stringifies any key
   that isn't already str/int/float/bool/None, so if any *future*
   addition to `ctx` introduces a non-string key, one query crashing
   badly is the worst case, not every single scan silently going down.

**Verified:** reproduced the exact failure mode (a `ctx` dict passed
directly as `r`, `_eval()` called on it, then run through the real
cleanup + `_json_safe` + `json.dumps` sequence) -- confirmed it fails
without the fix and succeeds with it. Separately verified the
defense-in-depth layer catches it even with the explicit `.pop()` calls
skipped entirely (simulating a future oversight).

**I take responsibility for this one** -- the memoization wrapper was
tested thoroughly against the `let`-bindings feature and the id-reuse
bug it was built to fix, but I didn't trace every caller of `_eval()` to
check whether any of them treat `ctx` as something that gets serialized
later. `api_run`'s main scan loop does exactly that, and I missed it.

## Resample caching (from the `[1m]` performance question)

Separately fixed: `close[1m]`/`open[1m]`/etc. (monthly timeframe) was
re-running the pandas daily→monthly resample from scratch on *every*
call, even though the underlying daily data hadn't changed since the
last backfill. Now cached (`_resampled_history_cached`, keyed by
symbol+timeframe), invalidated at the same point the existing daily
cache already gets cleared on backfill. This should measurably help any
query using `[1m]` or `[1w]`, especially when the same symbols get
scanned repeatedly across a session.

Two things I checked and ruled out as NOT the cause of the `[1m]`
slowness, so you don't spend time chasing them: the `[1]` bar-shift
folds into a single `_eval()` call (confirmed in code, not two passes),
and `_required_timeframes()` collects needed timeframes into a `set`,
so `close[1m]`, `open[1m]`, `high[1m]`, `low[1m]` all appearing in one
expression only triggers one `_history(symbol, "1m")` call, not four.

## On "candle context is fast, can the main engine be too"

Investigated before touching anything further, since I'd just found one
regression and didn't want to layer a second large, unverified change
right behind it. What I found: **the main engine is already quite
sophisticated, not naively slow.** `req_tfs` is scoped per-query (only
fetches timeframes the specific query actually references), and there's
a staged "snapshot-free pre-filter" that runs cheap conditions
(`Sector()`, `EarningsDays()`, etc.) across the whole watchlist *before*
loading any price history, so a query combining a cheap filter with an
expensive one only pays the expensive cost for symbols that survive the
cheap filter first.

Candle Context is fast because it's narrowly single-purpose (price data
only, nothing else it could possibly need). The main engine solves a
more general problem -- any query, any primitive, any data source
(options, earnings, sector, etc.) -- so it inherently carries more
machinery. The resample-caching fix above is a genuine, already-shipped
speed contribution that applies to every query using `1w`/`1m`, not just
candle-style ones.

Going further (e.g. a fast-path that skips options/earnings/sector
infrastructure entirely for queries that provably don't reference them)
is a real, valuable idea, but it means touching the core snapshot-
builder used by *every* existing saved scan -- I'd rather scope and
verify that properly in its own pass than rush it immediately after a
crash fix. Let me know if you want that as the next thing to tackle, and
I'll trace the snapshot-builder the same way I just traced this crash --
with actual code citations and a real before/after test, not a guess.

## Files changed
- `oiapp/scanners/scanner_builder.py`: the two-layer crash fix above;
  the resample-caching fix (`_resampled_history_cached`, updated
  `_history_from_local_daily`, cache-clear alongside the existing daily
  cache clear).

## Verification performed
- Full `py_compile`.
- Reproduced the exact crash mode end-to-end and confirmed the fix
  resolves it (both the explicit-cleanup path and the defense-in-depth
  path tested independently).
- Resample caching: confirmed via call-count instrumentation that the
  underlying daily-fetch function is called the expected number of times
  across repeated `_history_from_local_daily(symbol, "1m")` calls, and
  that both calls return identical data.

## Known gaps
- Verified against synthetic data reproducing the exact reported failure
  shape, not your live server. This should be very low-risk since the
  fix is narrowly scoped (strip two keys before serialization + make a
  key-sanitizer actually sanitize keys), but it's still worth confirming
  your next scan run succeeds before considering this fully closed.
