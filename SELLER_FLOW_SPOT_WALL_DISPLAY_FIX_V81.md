# Seller Flow Spot/Wall Display Fix v81

Fixes the Seller Flow Scanner cards that could show an OI-weighted strike anchor as the top-right price, for example SPY around 560 while the real cached SPY price was around 747.

## Changes

- Seller Flow cards now use real price sources only for the top-right underlying price:
  - `price_cache` latest close
  - option-chain `underlying` column when available
  - otherwise `Spot n/a`
- The scanner no longer uses OI-weighted strike distribution as a displayed spot price.
- Cached/Load Saved Seller Flow rows are repaired on load:
  - stale spot values are replaced with latest `price_cache` close
  - support/resistance walls are refreshed from latest aggregate strike-level OI
  - max pain is refreshed from strike-level OI
  - skew source falls back to `OI-skew proxy` when true IV skew is unavailable
- The Seller Flow card now shows the price source under the displayed price.
- Support/resistance/max-pain blanks are backfilled from the latest aggregate 45D option context when the nearest-expiry snapshot is incomplete.

## Notes

- If price_cache has no valid close and the options table has no valid underlying column, the card will show `Spot n/a` instead of an invented synthetic price.
- The saved scan API now refreshes display context before returning cached rows, so old cached rows from prior builds should no longer keep showing bad price or n/a wall values.
