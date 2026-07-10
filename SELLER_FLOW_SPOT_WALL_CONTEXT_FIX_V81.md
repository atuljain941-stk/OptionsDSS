# Seller Flow Spot / Wall Context Fix v81

This patch fixes the Seller Flow Scanner card fields that could show an incorrect top-right price or blank support/resistance/max-pain fields.

## Fixes

- The card top-right value is now explicitly labelled as **Spot**.
- Seller Flow no longer uses an OI-weighted strike proxy as the displayed underlying price.
- Spot is resolved from `price_cache` first, then option-chain `underlying` only when needed.
- If price-cache and underlying are missing, spot shows `Spot n/a` instead of a misleading strike-derived value.
- Latest support/resistance walls now use aggregate strike-level OI through the next 45 days.
- Deep stale OI walls are kept as context but no longer dominate the card's actionable support/resistance anchors.
- Cached `Load Saved` rows are repaired on read so old saved rows do not keep stale spot/wall/max-pain fields.
- Skew/max-pain context is backfilled from the latest aggregate snapshot when older cached rows have blanks.

## Why this matters

For SPY, a prior build could show a value around `$560` on the card even though actual SPY spot was around `$747`. That value came from an OI-weighted strike proxy, not the underlying price. This patch prevents that class of error.
