# OI Buildup Seller-Side PCR/OI Update (V61)

This patch changes the OI Buildup scanner from a simple aggregate-OI/price interpretation to a seller-side PCR plus OI model.

## New interpretation

The scanner now evaluates each ST/MT/LT window using:

- total OI percent change
- call OI percent change
- put OI percent change
- PCR current value
- PCR point change
- PCR percent change
- price proxy confirmation

Seller-side assumptions used by the scanner:

- Rising put OI plus rising PCR = bullish seller support / put writing.
- Rising call OI plus falling PCR = bearish seller resistance / call writing.
- Falling OI is treated as unwind. Direction comes from which side is being removed:
  - call-side unwind / PCR rising = bullish resistance release
  - put-side unwind / PCR falling = bearish support removal

## New signal labels

The ST, MT, and LT signal columns now show seller-flow labels:

- Bullish Put Selling
- Bearish Call Selling
- Bullish Call Unwind
- Bearish Put Unwind
- Bullish PCR Shift
- Bearish PCR Shift
- Mixed OI Buildup
- Neutral OI Unwind
- Neutral

Legacy price/OI labels are still retained in the row payload as `st_price_oi_signal`, `mt_price_oi_signal`, and `lt_price_oi_signal` for compatibility.

## UI changes

The OI Buildup dashboard was updated to match the seller-side model:

- default windows changed to ST 3 days, MT 10 days, LT 30 days
- Volume columns were removed from the ST/MT/LT table
- PCR change columns were added for ST/MT/LT
- Filters now focus on seller flow:
  - Bullish Seller Flow
  - Bearish Seller Flow
  - Put Sellers Building
  - Call Sellers Building
  - Seller Unwind
  - Mixed / Neutral
- Put OI is colored as support-side flow, and Call OI is colored as resistance-side flow.

## API/AI Hub alignment

The shared scanner primitive remains:

`oiapp/scanners/oi_buildup_scanner.py::run_oi_buildup_screener`

The API endpoint `/api/oi_buildup_screener` and AI Hub OI buildup answers now use the same seller-side scanner output. AI Hub responses include OI change and PCR change for ST/MT/LT instead of relying only on rising/falling aggregate OI.

## Added row fields

Each row now includes seller-side fields such as:

- `pcr_st_chg`, `pcr_st_chg_pct`
- `pcr_mt_chg`, `pcr_mt_chg_pct`
- `pcr_lt_chg`, `pcr_lt_chg_pct`
- `call_oi_st_pct`, `put_oi_st_pct`
- `call_oi_mt_pct`, `put_oi_mt_pct`
- `call_oi_lt_pct`, `put_oi_lt_pct`
- `st_seller_signal`, `mt_seller_signal`, `lt_seller_signal`
- `st_seller_bias`, `mt_seller_bias`, `lt_seller_bias`
- `seller_thesis`

Legacy aliases such as `oi_1d_pct`, `oi_5d_pct`, `oi_15d_pct`, and the volume fields remain in JSON for compatibility, but the dashboard no longer displays volume.

## Validation

Validated with a synthetic options-history database:

- a symbol with rising put OI and rising PCR is classified as `Bullish Put Selling`
- a symbol with rising call OI and falling PCR is classified as `Bearish Call Selling`
- scanner output includes all new PCR-change fields
