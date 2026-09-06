# Aggregate Strike PCR Charts V15

Aggregate tab PCR has been split into two strike-specific charts on the same row:

1. **Strike PCR History**
   - Uses the selected symbol, selected expiry, and selected PCR strike.
   - Shows how PCR for that one strike has changed across stored OI snapshot dates.
   - PCR = selected-strike put OI / selected-strike call OI.

2. **Current Strike PCR by Expiry**
   - Uses the selected symbol, selected PCR strike, selected starting expiry, and Count.
   - Shows current/latest snapshot PCR for the selected expiry and the next N future expiries.
   - This is intentionally not historical; it uses the latest OI snapshot for each expiry.

The Aggregate controls now include **PCR Strike**. If left blank, the UI/API choose the nearest available strike around spot/ATM and fill it in.

New APIs:

- `GET /api/pcr_strike_timeseries?symbol=SPY&expiry=YYYY-MM-DD&strike=740`
- `GET /api/pcr_strike_expiries?symbol=SPY&from_expiration=YYYY-MM-DD&count=3&strike=740`

The package is source-only and should not include SQLite/MongoDB/user data files.
