# Journal Close P&L Fix v32

This patch fixes multi-leg spread close P&L to use option points consistently:

- Entry net is calculated from legs first: sell leg price - buy leg price.
- Net close is treated as the opposite transaction in option points.
- Dollar P&L is calculated by multiplying by quantity and 100 exactly once.

Example:

CAT CS
- Sell call entry: 44.01
- Buy call entry: 41.51
- Entry credit: 2.50
- Net close debit: 3.80
- Quantity: 1

P&L = (2.50 - 3.80) * 1 * 100 = -130.00

The backend now recomputes net-close P&L server-side instead of trusting a frontend override.
No database files are included in this package.
