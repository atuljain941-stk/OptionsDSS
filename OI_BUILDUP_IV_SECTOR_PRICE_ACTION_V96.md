# OI Buildup / Seller Flow IV, Sector, Price-Action Context v96

This build upgrades the Seller Flow / OI Buildup response so the card is no longer just OI/PCR plus UAE.

## Added fields per result row

- `iv_rank`, `iv_percentile`, `current_hv`, `iv_regime`, `iv_trend`
- `should_buy_sell`, `entry_action`, `buy_sell_note`
- `sector`, `sector_etf`, `sector_regime`, `sector_trend`, `sector_note`, `sector_rsi14`
- `price_action_read`, `price_action_bias`, `price_action_note`, `rsi14`, `rsidiff90`, `bb_position`, `bb_kc_state`
- UAE now has a price-cache fallback when the shared Scanner Builder primitive context is unavailable.

## IV rank logic

The scanner uses the existing local price-cache volatility proxy. High IV rank favors premium selling; low IV rank favors debit/buy structures.

- IV rank >= 70: high IV, favor SELL PREMIUM.
- IV rank 40-70: moderate IV, mixed.
- IV rank <= 30: low IV, favor BUY OPTIONS / debit structures.

## Sector check

The scanner maps cached sector to a sector ETF and evaluates that ETF's local price-cache trend.

Examples:

- Consumer Staples / Consumer Defensive -> XLP
- Technology -> XLK
- Energy -> XLE
- Financials -> XLF

The UI shows the ETF/regime on the Seller Flow card.

## Price action

Price action is computed from local `price_cache` OHLCV data and summarizes EMA20/EMA50, RSI14, RSIDiff90, Bollinger position, and BB/Keltner-style expansion/compression context.

## Display fixes

- ST/MT/LT windows now show latest max pain if a historical max-pain shift cannot be computed.
- Skew shows current RR/OI-proxy context when window skew shift is unavailable.
- Fonts on the Seller Flow screen are larger for readability.

## No live provider calls

Seller Flow enrichment remains local-data-first. It does not call yfinance during the scan.
