# Candle Context Scanner — V2

Fixes three real problems reported from a live run: 0 signals / 198
scanned / 60.5s / 131 errors, plus two explicit design corrections.

## 1. Fixed: the 131-error / 60.5s timeout bug (real bug, not a tuning issue)

**Root cause:** V1 built its context via the heavy `_symbol_ctx()` --
which fetches live options history, earnings info, sector data, and a
flow snapshot for every symbol, none of which this scanner's scoring
actually uses. With a 60s timeout and 198 symbols each needing several
live fetches, the majority of symbols simply couldn't finish in time
(60.5s elapsed is the tell -- that's the timeout firing, not the scan
finishing naturally).

**Fix:** rewrote the context builder to read OHLCV directly from
`price_cache`, the same pattern `institutional_scanner.py` already
uses. RSI is now computed locally (standard Wilder's smoothing) instead
of depending on the heavy ctx for it. Every other scoring dimension
(S/R, TouchCount, compression, prior trend) only ever needed
`ctx["timeframes"]["1d"]["series"]` in the first place -- confirmed by
testing -- so nothing else had to change.

**Verified:** built a synthetic 45-symbol `price_cache` (40 random-walk
symbols expected to show nothing, 5 engineered breakout symbols) and ran
the actual scan through a real Flask `test_client()`. V1's equivalent
would have taken 60+ seconds and errored on a majority of symbols; V2
completed in **0.83 seconds with zero errors**.

## 2. Fixed: `min_body_pct` was a hard filter, contradicting the explicit design rule

Re-reading the instruction closely: "strong candle and volume are the
only filter and rest are scoring." V1's base filter also required
body-to-range ratio >= 50%, which is a THIRD filter condition, not just
the two (move + volume). Removed it from the gate entirely -- **the
only two gate conditions now are `move_mult` and `vol_mult`.** Body
strength is still useful information (a big move with a small real body
is a different, weaker signal than a big move that closes near its
high/low), so it's now folded into the "candle" scoring dimension
instead: a bonus for >=70% body, a smaller bonus for >=50%, and an
honest "more wick than conviction" note otherwise -- informational, never
exclusionary.

This was caught directly during testing, not just from re-reading the
instruction: five engineered breakout candles in the synthetic test data
were being rejected by V1 even though their move (8-11x normal) and
volume (3-5x normal) both cleared their thresholds by a wide margin --
the synthetic OHLC shape happened to have a 20% body-to-range ratio,
correctly triggering the old hard filter. All five now correctly appear
in results with the honest "small body" note attached, at whatever score
that combination of factors produces.

## 3. `min_score` default changed from 5.0 to 0.0

Per "I think system shall always return a stock if it had strong candle
and good volume": `min_score` is now purely a post-scoring DISPLAY
filter (applied after every dimension has already been scored), and its
default no longer hides anything. Every symbol with a qualifying candle
now shows up by default, sorted by score, with the full breakdown
visible -- raise `min_score` yourself if you want to see only the
higher-scoring subset.

## 4. Full per-dimension point transparency added

Added a `points` object to every result (`candle`, `rsi`, `sr`, `touch`,
`compression`, `trend` -- each a 0-to-max point value) alongside the raw
metric values (RSI number, S/R distance/strength, touch count,
compression average, prior trend) that were already there. The results
table's hover tooltip now shows the exact point breakdown per dimension
plus the plain-English reasons, e.g.:

```
Points -- Candle:1.5 RSI:1.2 S/R:1.5 Touch:1.5 Compression:0.0 Trend:0.0 = 5.7/10
Very large move (8.11x normal) · Small body relative to range (20.0%) --
more wick than conviction · RSI neutral (63) · Right at a resistance
zone (moderate strength 42) · Level tested 3x -- validated, not
exhausted · No meaningful consolidation beforehand · Prior action was
sideways/choppy
```

## Files changed
- `oiapp/scanners/candle_context_scanner.py` -- rewritten context
  builder (price_cache instead of `_symbol_ctx`), `min_body_pct` moved
  from filter to scoring, `min_score` default 0.0, `points` dict added
  to every result, timeout reduced from 60s to 25s (appropriate now that
  each symbol is a fast local query instead of several live fetches),
  worker count raised from 6 to 12 (no external rate limit to respect
  anymore).
- `templates/index.html` -- removed the now-unused "Min body %" field,
  updated the tab description to state the filter/scoring split
  explicitly, `Min score` field default changed to 0.0.
- `oiapp/static/app.js` -- removed `min_body_pct` from params/reset
  defaults, `min_score` default 0.0, richer hover tooltip showing the
  per-dimension point breakdown.

## Verification performed
- Full `py_compile` on all touched files; `node --check` on `app.js`;
  HTML `<div>` balance (855/855).
- Full Flask `test_client()` scan against a real (synthetic)
  `price_cache`: 45 symbols, 0.83s, 0 errors, 6 matches (all 5 engineered
  breakouts + 1 naturally-occurring qualifying move among the random-walk
  symbols -- correctly caught, not a false negative).
- Confirmed `min_score: 0.0` is applied when no override is sent at all
  (true default path, not just the explicitly-passed-0.0 path tested
  earlier).

## How to use TouchCount() (asked again -- concise version)
`TouchCount(level, tolerance, bars[, timeframe])` counts how many of the
last `bars` candles had a high/low range overlapping a price band around
`level` (the band width is `level * tolerance`, so `tolerance=0.01` = a
1% band). In this scanner: `level` is the nearest resistance (bull
candles) or support (bear candles) from `Resistance()`/`Support()`,
`tolerance` comes from the "Touch tolerance %" input (default 1.0%), and
`bars` from "Touch bars" (default 60 -- roughly 3 months of daily data).
The result feeds the freshness scoring: 2-4 touches scores highest
(genuinely validated, not yet worn out), 0-1 is unproven, 6+ suggests a
heavily faded zone. It's purely informational here -- never a filter,
same as everything else past the base candle+volume check.

## Known gaps
- Verified against synthetic `price_cache` data, not your real database
  -- the timeout fix should be dramatic (price_cache reads are always
  fast local queries regardless of watchlist size), but the real test is
  your next live run.
- Scoring breakpoints (the 2.0/1.4/0.7 move-multiple bands, the
  70%/50% body bands, the 2-4 touch "sweet spot", etc.) are reasonable
  starting points, not empirically tuned against your historical data --
  now that you'll actually see every qualifying candle with full point
  transparency, that's exactly the data you'd want to calibrate them
  against.
