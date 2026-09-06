# Journal Roll Net Cashflow Fix v93

This update fixes the Close Trade roll/split math for vertical and IC side rolls.

## Correct roll premium semantics

The roll input is now **Net roll credit/debit**, meaning the total roll order cashflow:

- `+1.25` = additional credit collected
- `-1.03` = debit paid to roll

This value is added directly to the old side basis.

Example:

```text
Old side credit:       +2.50
Net roll debit:        -1.03
Effective position:    +1.47
```

The app no longer subtracts the close cost a second time.

## Displayed preview

The roll preview now shows:

- selected legs
- roll quantity
- old side net
- close cost/net
- net roll credit/debit
- implied new open
- effective position credit

If the user enters short-leg and long-leg premiums, the app derives:

```text
net roll credit/debit = new spread credit - close cost
```

If the user enters only net roll credit/debit, the app infers the new opening net as:

```text
implied new open = close cost + net roll credit/debit
```

## Journal accounting

The backend now receives the explicit roll cashflow and stores:

- closed selected side event
- remaining unrolled side as the original open trade
- replacement rolled side as a new open trade
- actual/implied new open net
- effective position credit in the roll note and API response

This avoids the prior bad calculation:

```text
new net - close cost = roll adjustment
```

which incorrectly turned a `-1.03` roll debit and `2.50` close cost into `-3.53`.
