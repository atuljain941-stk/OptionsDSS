# Journal Partial Close / Side Close / Roll Split v91

This build enhances the Journal close and roll workflows.

## Partial quantity close

A trade with quantity 5 can now close quantity 3 and keep quantity 2 open.

Behavior:
- a CLOSED child row records the realized portion and P&L;
- the original OPEN row is reduced to the remaining quantity;
- leg quantities and net premium are recalculated from `legs_json`.

## One-side close for IC

The close dialog now lets selected legs/sides be closed while unchecked legs remain open.

Example:
- original: NOW IC C135/140 and P110/105;
- close only the put side;
- original trade becomes a remaining call spread C135/140;
- a CLOSED child row records the put-side close P&L.

## Structured side roll

The roll dialog can roll only the tested side of an IC.

Example:
- original: NOW IC C135/140 + P110/105 expiring 2026-06-26;
- roll put side to P105/100 expiring 2026-07-17;
- original row becomes the untested call side;
- closed child row records the old put-side close;
- new OPEN row records the new rolled put spread.

Roll notes store:
- old side basis;
- close side net;
- new entry net;
- roll adjustment;
- cumulative side basis.

## Compatibility

The implementation uses runtime `PRAGMA table_info(trades)` before insert/update so it remains compatible with existing user databases.
