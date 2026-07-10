# v48 Futures OI for Commodities, FX, Rates, and Indexes

This update extends the existing Schwab Futures OI module into a broader futures positioning module.

## Data layers

1. **Schwab futures quotes** remain the primary source for daily real open-interest snapshots.
2. **CME settlement/VOI fallback** is wired as a fallback layer where a product mapping exists. It never stores yfinance volume proxy as real OI.
3. **CFTC COT overlay** is used as weekly positioning context for index, commodity, rate, and FX futures roots.

## Supported roots

Equity indexes: `/ES`, `/NQ`, `/RTY`, `/YM`

Metals: `/GC`, `/SI`, `/HG`, `/PL`

Energy: `/CL`, `/NG`, `/RB`, `/HO`

Rates: `/ZB`, `/ZN`, `/ZF`, `/ZT`

FX futures: `/6E`, `/6J`, `/6B`, `/6A`, `/6C`, `/6S`, `/6N`, `/6M`

## History and cumulative OI

The app continues to store daily rows in `futures_oi_daily` and now stores/returns:

- root
- asset class
- display name
- expiry
- all active contracts
- active contract by highest current OI
- front contract by nearest configured expiry
- cumulative OI history across active contracts
- contract ladder table

The Dashboard Futures OI panel now supports commodity, FX, rate, and equity futures and still shows two charts:

1. Immediate/front contract OI history
2. Cumulative OI history across active contracts

## Interpretation

The signal classification uses price + cumulative OI change:

- Price up + cumulative OI up = long buildup
- Price down + cumulative OI up = short buildup
- Price up + cumulative OI down = short covering
- Price down + cumulative OI down = long unwinding
- Front OI falling while next/active OI rises and cumulative OI holds = roll/position transfer

## Notes

For FX, the module uses CME FX futures OI, not spot-FX broker positioning.

For commodities and rates, active contract selection uses highest OI when data exists, because nearest calendar month is not always the most liquid contract during roll periods.
