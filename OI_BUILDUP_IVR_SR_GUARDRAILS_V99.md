# OI Buildup / Seller Flow IVR + S/R Guardrails v99

Changes:

- Added quality-aware IV Rank handling.
  - Uses option-IV history first.
  - Uses HV proxy from local price/underlying history when option IV history is missing.
  - Short IV histories are blended toward neutral and labelled as estimates instead of showing misleading n/a or extreme 0/100 values.
- Added daily RSI and RSIDiff90 directly to Seller Flow cards.
- Added distance from daily and weekly rolling support/resistance:
  - Daily S/R = distance from 20 daily-bar support/resistance.
  - Weekly S/R = distance from 20 weekly-bar support/resistance.
- Added price-location guardrails.
  - Bearish seller flow near support / downside exhaustion becomes WAIT / conditional CS only after bounce/rejection or clean breakdown.
  - Bullish seller flow near resistance / upside exhaustion becomes WAIT / conditional PS only after pullback/support confirmation.
- Added these fields to scanner rows:
  - distance_from_support_20_pct
  - distance_from_resistance_20_pct
  - weekly_distance_from_support_20_pct
  - weekly_distance_from_resistance_20_pct
  - weekly_rsi14
  - weekly_rsidiff90
  - price_location_guard
  - price_location_note
  - price_location_action_note
  - iv_rank_estimated
  - iv_history_points
- Updated UI to show Daily RSI, Daily S/R, Weekly S/R and Guard chips on each Seller Flow card.
