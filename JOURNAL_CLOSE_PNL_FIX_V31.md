# Journal Close P&L Fix v31

Fixes the Close Trade modal P&L preview and close calculation for multi-leg option strategies when using the Net close field.

Example fixed:

- Trade type: CS
- Entry credit: 2.50
- Net close debit: 3.80
- Quantity: 1
- P&L: (2.50 - 3.80) * 1 * 100 = -130.00

Changes:

- Net close preview now uses entry credit/debit and updates immediately.
- Credit strategies profit when net close is below entry credit.
- Debit strategies profit when net close is above entry debit.
- Backend close route now also supports robust net-close calculation if the frontend sends a net-close request without a P&L override.
