# GEX Composite VIX/DTE Action Guide v82

This build updates the GEX Daily Plan so the intraday composite grid includes the original five core factors plus VIX Regime and DTE Effect.

## Composite grid

The grid now scores:

1. Skew RR 25D
2. Put-Call Ratio
3. GEX Regime / Strength percent of gross GEX
4. Gamma Flip
5. Wing Premium
6. VIX Regime
7. DTE Effect

GEX strength uses the trader-facing rule:

- More than 20 percent of gross GEX: very strong pin / fade-first while levels hold
- 10 to 20 percent: moderate pin / current reading
- Less than 10 percent: weak pin / breakouts are more valid

VIX scoring:

- VIX under 15: +1
- VIX 15 to 20: 0
- VIX over 20: -1
- VIX over 30: -2

DTE scoring:

- 0DTE in positive/moderate GEX: stronger pin
- 0DTE in negative GEX: fast-break risk
- 1-2DTE: walls are less sticky, breakouts get more respect
- 3-5DTE: weekly structure
- Longer DTE: context only, not a precise intraday pin

## How To Trade This Today

The page now renders a concrete action guide from the same GEX/PCR/VIX/DTE inputs:

- Range to watch
- Range-fade setup
- Breakout-long setup
- Breakdown-short setup
- PCR vs GEX tension
- VIX note
- DTE note
- bottom-line trade actions and sizing note

The action guide is deterministic and uses only the current GEX levels, PCR, VIX, DTE, wall context, and spot.
