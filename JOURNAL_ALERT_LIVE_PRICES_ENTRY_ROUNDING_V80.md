# Journal / Alert live option prices + entry rounding v80

## Live option pricing

Journal health, live P&L, PNR alerts, AI alert review and roll-candidate review now use a live-first option-pricing path.

- Journal/alert live marks call `option_prices.fetch_chain(..., prefer_live=True)`.
- That attempts yfinance bid/ask/last first.
- If yfinance has provider-side NaN issues, the app falls back to the latest local DB option snapshot or estimated pricing.
- The known yfinance error `cannot convert float NaN to integer` is suppressed by default so background alert refreshes do not spam the console.

Strategy/dashboard enrichment remains DB-first by default for speed unless the caller asks for live pricing.

## Entry amount formatting

Journal entry amount display is now formatted to 2 decimals in open and closed trade grids.

Examples:

```text
1.3333333333333 -> 1.33
2 -> 2.00
```

## Config

```bash
# Set to 1 to make all option-price enrichment live-first globally.
OPTION_PRICES_PREFER_LIVE=1

# Journal/alert paths already use live-first without this setting.

# Enable yfinance provider error logging if troubleshooting is needed.
OPTION_PRICES_LOG_YF_ERRORS=1
```
