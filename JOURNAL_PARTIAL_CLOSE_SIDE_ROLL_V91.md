# Journal Partial Close / Side Close / Roll v91

## What changed

The Journal close flow now supports three adjustment types without losing journal history:

1. Partial quantity close
   - Example: original trade quantity is 5 contracts; close 3 and keep 2 open.
   - The original open row is reduced to the remaining 2 contracts.
   - A new closed child row is inserted for the realized 3-contract close.

2. One-side close for multi-leg positions
   - Example: close only the put side of an iron condor and keep the call side open.
   - The original row is rewritten to the remaining open side and its strategy type is inferred from remaining legs, such as CS or PS.
   - A closed child row records the closed side and realized P&L.

3. Side roll / tested-side roll
   - Example: roll only the tested put side of an IC to a future expiry with different strikes.
   - The selected old legs are closed and journaled with realized P&L.
   - The untouched side remains open as its own adjusted trade.
   - The new rolled side is inserted as a new open trade linked by `roll_from_id`.

## Accounting model

Roll economics are preserved as two journal events:

- Realized P&L from closing the old side.
- New open entry credit/debit for the rolled side.

This avoids hiding the roll cost inside a single adjusted basis number and keeps open/closed journal analytics clean.

## UI behavior

The Close modal now includes:

- Position quantity and close quantity.
- Per-leg close quantity.
- Per-leg exit price.
- Quick selectors: All, Put side, Call side, None.
- Roll selected button for rolling a selected side.

## Files changed

- `oiapp/journal/journal_routes.py`
- `oiapp/static/app.js`
- `templates/index.html`
