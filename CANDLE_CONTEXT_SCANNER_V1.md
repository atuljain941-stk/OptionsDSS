# Candle Context Scanner — V1

New scanner tab answering: "I see a strong candle with a big volume bar --
is it actually meaningful, and what's the setup?"

## Base filter (hard requirement)
The most recent bar is a strong bull OR bear candle:
- `abs(change%) >= move_mult × EMA(abs(change%), avg_change_bars)` (default
  move_mult=1.5) -- the move is large relative to the stock's own recent
  normal move, not an arbitrary fixed threshold across all stocks.
- `volume >= vol_mult × EMA(volume, 20)` (default vol_mult=1.5).
- Body-to-range ratio >= min_body_pct (default 50%) -- filters out
  doji-ish wick bars that technically moved a lot intrabar but didn't
  actually close with conviction.

**Why this isn't just `StrongBullCandle()`/`StrongCandleAge()`:** those
existing primitives are deliberately designed for retest scans -- they
exclude the current bar and search backward for a prior anchor candle to
test a level against. There is no shift value that makes them detect
"the current/most-recent bar IS the strong candle" -- confirmed this by
direct testing (appending one more bar after a synthetic breakout candle
was required before `StrongCandleAge` would detect it as an anchor at
all). So the base filter is computed fresh in this new module for the
actual last bar, using the same underlying formula those primitives use
internally, just anchored to the opposite end of the window.

## Scoring (0-10), all built on already-tested scanner_builder primitives
1. **Candle strength itself** (0-2) -- how many multiples over its own
   normal move.
2. **RSI zone context** (0-2) -- a bull candle emerging from RSI<35
   (oversold) scores highest; already-overbought bull candles (chase
   risk) score near zero. Mirrored for bear/overbought.
3. **S/R proximity + strength** (0-2.5) -- `DistanceFromResistance`/
   `DistanceFromSupport` + `ResistanceStrength`/`SupportStrength`.
4. **TouchCount / freshness** (0-1.5) -- `Resistance`/`Support` (raw
   level) fed into `TouchCount(level, tolerance, bars)`. 2-4 touches
   scores highest (validated but not exhausted); 0-1 = unproven; 6+ =
   heavily faded zone. This is the "is that support/resistance fresh or
   already tested multiple times" dimension from the original request.
5. **Consolidation-before-move** (0-1.5) -- average of `ATRCompression`,
   `RangeCompression`, `VolumeDryup`; low = broke out of a tight coil.
6. **Prior trend clarity** (0-0.5) -- `RegSlopePct` measured over the
   window before the candle; reports continuation vs. reversal setup as
   context (not a directional score bias -- both are valid, different
   trade types).

## Design: orchestration over existing primitives, not new indicator math
Everything after the base filter (steps 2-6 above) evaluates through the
real `_symbol_ctx` / `_parse_query` / `_eval` pipeline -- the exact same
primitives available in Scanner Builder, called the same way. This
scanner doesn't re-implement S/R detection, touch counting, or
compression scoring; it composes primitives that are already tested and
already in production use in other saved scans (`Tight Consolidation`,
`Strong Weekly Resistance`, `Iron Condor`, etc.).

## Files added
- `oiapp/scanners/candle_context_scanner.py` -- new Flask blueprint
  `candle_ctx_bp` at `/scanner/candle-context/{scan,scan_cached,defaults}`,
  same structure as `institutional_scanner.py`.
- New UI tab "Candle Context" (nav + section + JS), same conventions as
  the Inst. Breakout / Inst. Breakdown tabs.

## Files changed (additive only)
- `oiapp/app_factory.py` -- one new blueprint registration, same
  try/except pattern as every other one.
- `templates/index.html` -- one nav button, one tab section.
- `oiapp/static/app.js` -- one tab-router line, one new JS block.

## Critical bug found and fixed while building this: `_eval` memoization
The `let`-bindings feature (see `SCANNER_LET_BINDINGS_V1.md`) added a
memoizing wrapper around `_eval`, keyed by `(id(node), shift, tf_default)`.
While testing this scanner's sequence of one-off `_parse_query(expr)` +
`_eval(...)` calls against the same `ctx` (a very common pattern -- parse
a short-lived expression, evaluate it once, discard the reference), a
**real, reproducible bug** surfaced: `TouchCount(...)` returned a value
that was actually `DistanceFromResistance`'s result from two calls
earlier, not a touch count.

**Root cause:** `id()` is only guaranteed unique among objects that are
currently alive. Once a transient parsed `Node` is evaluated and its only
reference (a local variable) goes out of scope, CPython can immediately
reuse that exact memory address for the next `Node` allocated -- and
does, in practice, for short-lived objects. The memo cache, keyed purely
on that recycled `id()`, returned the stale cached result for a
completely unrelated expression.

**This was not theoretical** -- it was caught by directly reproducing
the exact call sequence and comparing output to a known-correct direct
test, not by code review.

**Fix:** `ctx["_eval_memo_keepalive"]` now holds a strong reference to
every node that's ever been memoized, for as long as `ctx` itself is
alive, so its `id()` can never be recycled. As defense in depth, the memo
now also stores `(node, result)` pairs and verifies `cached_node is node`
before trusting a hit, so even a hypothetical gap in the keepalive would
degrade to "recompute" rather than "return a wrong answer."

**Re-verified after the fix:** the entire `let`-bindings test suite
(correctness across 20+ shifts, error handling, backward-compatibility
dataclass-equality check, and the 31% call-count reduction) was re-run in
full and still passes byte-for-byte identically. This bug affected the
memoization layer generally, not anything specific to `let` or to this
new scanner -- any code path doing repeated one-off parse+eval (which
includes other existing integrations like `weekly_analysis.py`'s
conviction-score wrapper) benefits from this fix.

## Verification performed before packaging
- Full `py_compile` on all touched files.
- `node --check` on `app.js`; HTML `<div>` balance check (855/855).
- Individual primitive calls (`StrongCandleAge`, `DistanceFromResistance`,
  `ResistanceStrength`, `Resistance`/`Support` raw level, `TouchCount`,
  `ATRCompression`/`RangeCompression`/`VolumeDryup`, `RegSlopePct`) tested
  directly against synthetic OHLCV data before being wired into the
  scanner.
- Full `_score_symbol` pipeline tested end-to-end against a synthetic
  uptrend → pullback → double-retest → breakout series (mimics a
  realistic support-tested-twice-then-broken-out setup) with the
  `_sb_symbol_ctx` dependency monkey-patched to the synthetic context --
  confirmed sane, non-crashing output with correct field values after
  the memoization fix.
- Full Flask blueprint test via a real `test_client()` POST to
  `/scanner/candle-context/scan` -- HTTP 200, well-formed JSON, zero
  errors.

## Known gaps
- I don't have your real DB or live price data, so this was verified
  against synthetic data, not a real end-to-end scan through the actual
  Flask route with real symbols. S/R zone detection in particular
  (`_tv_sr_selected_channel`) legitimately returns "no zone found" for
  some price shapes in testing -- expected on my clean synthetic ramps
  (which lack realistic noise/multi-touch pivot structure), but worth
  watching on your first real run to confirm it fires as expected on
  actual market data with genuine consolidation/retest patterns.
- The scoring weights (2/2/2.5/1.5/1.5/0.5 point allocation across the
  six factors) are a reasonable starting design, not empirically tuned
  against your historical data -- treat `min_score` and the individual
  thresholds as a starting point to calibrate against your own watchlist
  once you've run it for real.
