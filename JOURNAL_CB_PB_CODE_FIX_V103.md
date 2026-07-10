# Journal CB/PB Code Fix v103

## Canonical trade-code meaning

This build makes the Journal code mapping consistent everywhere:

- `CB` = Call Buy / Bull Call Debit spread
- `PB` = Put Buy / Bear Put Debit spread
- `PS` = Bull Put credit spread
- `CS` = Bear Call credit spread

## What was fixed

1. Add Trade and Quick Trade templates now create CB as call-debit legs and PB as put-debit legs.
2. Journal edit mode no longer blanks strikes when changing a vertical trade code between PS/CS/CB/PB. It preserves strikes, expiry, quantity, and leg prices where possible.
3. Legacy rows created during the temporary PB/CB mismatch are repaired on read: if a stored `PB` row clearly has only call legs, it is displayed/calculated as `CB`; if a stored `CB` row clearly has only put legs, it is displayed/calculated as `PB`.
4. Journal health, PNR, portfolio alignment, position alerts, and scanner recommendations now use CB as bullish call-debit and PB as bearish put-debit.
5. Scanner Builder trade recommendations now return CB for bullish call debit and PB for bearish put debit.

No database migration is required. Existing rows are not destructively modified; the correction is applied from `legs_json` when the leg structure clearly identifies the intended code.
