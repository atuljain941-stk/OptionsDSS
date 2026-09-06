# Seller Flow Scanner: OI + PCR + Skew + Max Pain + Earnings Guardrails V63

This build upgrades the OI Buildup Screener into a seller-side flow scanner.

## Why PCR + OI was changed

PCR change and OI buildup are useful, but they are not sufficient by themselves because every option open-interest increase has both a buyer and a seller. Rising put OI can be bullish put selling, but it can also be bearish put buying or hedging. Rising call OI can be bearish call selling, but it can also be bullish call buying.

The scanner now uses a layered model:

1. OI change and call/put OI mix
2. PCR change
3. Skew / risk-reversal shift
4. Max-pain shift
5. Price confirmation
6. Earnings conflict guardrails
7. Strategy and timeframe recommendation

## Historical max pain

Historical max pain can be computed from historical option-chain snapshots if the database has strike-level call and put OI by symbol, expiration, strike, and date. Aggregate total OI is not enough.

For each snapshot date and nearest expiration, the scanner evaluates candidate settlement prices at each strike and finds the strike where total option-holder payout is minimized:

```text
call payout = max(candidate_price - call_strike, 0) * call_oi
put payout  = max(put_strike - candidate_price, 0) * put_oi
max pain    = candidate strike with minimum total payout
```

The scanner compares current max pain against the ST / MT / LT base snapshots and reports max-pain shift percentage.

## Historical skew

True historical skew requires either:

- historical implied volatility by option contract, or
- historical option prices plus underlying price so IV can be back-solved.

Historical OI alone is not enough for true IV skew. If the options table has IV or price/underlying data, the scanner computes:

```text
put_skew       = 25-delta put IV - ATM IV
call_skew      = 25-delta call IV - ATM IV
risk_reversal  = 25-delta call IV - 25-delta put IV
```

If IV and price/underlying data are missing, the scanner falls back to an explicitly labelled OI-skew proxy. Proxy skew is lower confidence and is used only as a pressure indicator, not as a true volatility-skew reading.

## New stored option-chain fields

Going forward, `store_option_chain()` stores these enrichment fields when available:

- bid
- ask
- last
- iv
- underlying
- fetch_ts

Existing databases are migrated automatically with `ALTER TABLE` during startup.

## Seller-flow labels

Examples:

- Bullish Put Selling Confirmed
- Bullish Put Selling But Skew Risk
- Bearish Call Selling Confirmed
- Bearish Call Selling But Upside Skew Risk
- Bullish Call Unwind
- Bearish Put Unwind
- Mixed OI Buildup
- No Clear Seller Edge

## Strategy / timeframe recommendations

The scanner now recommends a strategy and timeframe bucket from the seller-flow read:

- Bullish put-selling confirmation: bull put spread or put credit spread
- Bearish call-selling confirmation: bear call spread or call credit spread
- Balanced / pinning flow: iron condor or no trade
- Mixed flow: no new trade until alignment improves

The timeframe is chosen from ST / MT / LT alignment and earnings guardrails.

## Earnings guardrails

The scanner checks cached `earnings_calendar` data when available.

If earnings falls inside the suggested holding/expiration window, the row is flagged with:

- earnings_date
- earnings_days
- earnings_conflict
- avoid_timeframe
- earnings_note

When earnings conflicts with the proposed window, the suggested strategy is changed to an avoid/wait response and AI Hub will avoid recommending a new position through earnings unless the user explicitly asks for an earnings-specific trade.

## UI changes

The OI Buildup tab is now titled Seller Flow Scanner.

The table now displays:

- OI % change
- PCR change
- Skew / risk-reversal shift
- Max-pain shift
- Seller-flow signal
- Final seller read
- Strategy / timeframe
- Earnings guardrail

The former volume columns were removed from this scanner view.

## AI Hub changes

AI Hub OI buildup responses now include:

- seller-flow read
- skew / risk-reversal evidence
- max-pain evidence
- recommended strategy
- recommended timeframe / expiry window
- earnings conflict warning

AI Hub still refuses to guess when underlying chain history is missing.
