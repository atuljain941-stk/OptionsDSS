# v42 — Weekly Plan Monday-to-Friday options plan

This update rebuilds the existing Weekly Plan tab (not Weekly Analysis) for SPY/QQQ-style Friday option planning, intended to be run Monday around 10:00 ET.

Inputs:
- Symbol: SPY, QQQ, IWM, DIA
- Target Friday expiry: auto current Friday, or selected expiry

The plan now reports:
- Friday expected move from ATM straddle when available, with IV-sigma fallback
- ATM IV, IV rank, and VIX overlay
- Daily, 4H, and 2H indicator blocks:
  - Bollinger %B / width rank / squeeze
  - RSI and RSIDiff90
  - MACD, MACD signal, histogram, histogram change
  - EMA20 / EMA50 / EMA200
  - ADX and ADX rising/falling
  - strong bull/bear candle with volume using the current strong-candle definition
- Current OI and OI-change wall scoring:
  - current OI
  - positive OI change
  - proximity / z-score
  - GEX contribution
- Wall alignment with recent strong candles
- Composite score components
- Approach scores for PS, CS, IC, CB, and PB
- Candidate structures retained from the existing strategy engine

No database files are included in this package.
