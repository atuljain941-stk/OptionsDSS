# V67 - AI Hub Weekly IC Selection and Seller Flow Recommendation Fix

## Fixed AI Hub weekly best-strategy behavior

Natural-language questions such as:

```text
What is the best strategy for SPY expiring Jun 26?
```

now use the fast weekly-plan path but no longer default to a one-sided bull put
spread just because PCR/put OI is high.  The fast fallback now evaluates:

- Bull put spread near actionable put support
- Bear call spread near actionable call resistance
- Iron condor using both actionable walls

For liquid weekly underlyings, the fallback limits OI-wall selection to the
weekly actionable range around spot/expected move so stale deep OI walls do not
win.  Example: a deep SPY 565P wall will not dominate a 5-DTE plan when the
price is near 746 and actionable walls are near 720P and 760C.

## IC selection improvements

The fallback now computes:

- local ATM straddle expected move when prices exist
- price-cache realized-vol fallback when straddle pricing is missing
- historical max pain from local strike-level OI
- actionable put and call walls around the weekly range
- PS, CS and IC credit/RR from local option prices

When both sides have usable walls and the IC score is near or above the best
one-sided spread, the IC can be selected as the preferred weekly strategy.

## Weekly Plan invocation bug fixed

The newer AI Hub weekly path referenced `current_app` without importing it,
which caused Weekly Plan execution to fail immediately and always use fallback.
`current_app` is now imported properly.

## Seller Flow card recommendation fix

Saved/cached OI Buildup rows can now backfill a missing strategy suggestion from
ST/MT/LT seller-flow alignment.  If ST, MT and LT all show Bearish Call Selling,
the card will show a bear-call spread framework even if the saved row was created
before strategy fields existed.

## No database files in package

The release ZIP is built without `.db`, `.sqlite`, `.sqlite3`, `.pyc` or
`__pycache__` files.
