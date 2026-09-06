# Journal Integrated Roll / Close v92

This build moves the roll/split workflow into the existing Close Trade modal.

## Changes

- The Open Trades **Roll** button now opens the same Close Trade modal with the roll panel expanded.
- The roll panel uses the currently selected close legs/sides.
- Close one side of an IC and keep the other side open.
- Roll selected put/call/custom legs to a future expiry/structure from the same dialog.
- New premium accepts signed values:
  - positive = credit collected
  - negative = debit paid
- The preview shows:
  - selected legs
  - close quantity
  - old side net premium
  - close cost/net
  - new signed premium
  - roll adjustment
  - effective side basis

## Accounting

The backend keeps the clean accounting model:

1. Selected old side is closed and recorded as a closed child event with realized P&L.
2. Untested side remains open in the original trade.
3. Rolled side opens as a new trade linked to the original.
4. The roll note records the effective side basis:

```
effective side basis = old side net + (new premium - close cost)
```

This keeps journal totals correct while still showing how the debit/credit adjusted the original premium.
