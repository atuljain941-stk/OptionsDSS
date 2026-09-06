# Quick Trade shared Add Trade behavior v90

The top-bar Add Trade modal now shares the same option leg premium lookup path used by the full Journal Add Trade page.

## Fixes

- First-leg expiry changes now cascade to the other option legs for normal spread structures, matching the full Add Trade form.
- First-leg quantity changes now cascade to the other option legs for normal spread structures, matching the full Add Trade form.
- Calendar and Custom trades keep row-level expiries, matching the full Add Trade behavior.
- Butterfly and Custom trades keep row-level quantities, matching the full Add Trade behavior.
- Quick Trade now pulls live strike prices using the shared Journal option-leg price helper.
- A `Refresh live prices` button was added to the Quick Trade legs toolbar.

## Shared maintenance path

The shared frontend helper is:

```js
_jrnFetchOptionLegPremium(symbol, leg)
```

Both functions now use that same helper:

```js
_jrnFetchLegPremium(i)
_quickTradeFetchLegPremium(i)
```

The helper checks the live option-chain endpoint first and falls back to the existing Journal IV endpoint if needed.
