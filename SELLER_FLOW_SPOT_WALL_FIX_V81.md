# Seller Flow spot and wall display fix v81

This patch fixes the Seller Flow Scanner card values reported as blank/n/a and the bad SPY price display.

## Fixes

- The top-right card price now uses real `price_cache` / option-underlying values only.
- The scanner no longer uses an OI-weighted strike proxy as spot. That proxy caused SPY to display values such as 560 while the actual SPY price was around 747.
- Cached / Load Saved Seller Flow rows are repaired on read so older cached rows no longer keep the bad spot value.
- Support, resistance, max-pain, and skew context now fall back to latest aggregate strike-level OI through roughly 45 days when the nearest-expiry snapshot does not have usable analytics.
- Displayed support/resistance walls are actionability-filtered near current spot so massive deep OI walls remain context but do not become the primary card support/strike anchor.
- The UI now shows `Spot n/a` rather than `$0.00` if no true price source exists.

## Data source clarification

- `spot`: actual price from `price_cache` or option-chain underlying, not OI-weighted strike.
- `max_pain`: computed from latest aggregate strike-level OI when the nearest snapshot is incomplete.
- `support/resistance`: selected from near-price aggregate put/call OI walls.
- `skew`: true IV skew when available; otherwise OI-skew proxy when the aggregate strike-level OI can support it.
