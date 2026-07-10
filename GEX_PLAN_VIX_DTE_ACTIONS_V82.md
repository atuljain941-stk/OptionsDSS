# GEX Plan VIX/DTE Composite + Intraday Action Guide V82

This build updates the GEX Daily Plan so the composite grid is no longer only the original five GEX factors.  VIX regime and DTE/expiry effect are promoted into scored factors because they change whether OI/GEX walls should be treated as sticky fade levels or breakout levels.

## Composite grid changes

The GEX Plan grid now includes:

1. Skew RR 25D
2. Put-Call Ratio
3. GEX Regime / Strength %
4. Gamma Flip
5. Wing Premium
6. VIX Regime
7. DTE Effect
8. Wall Quality

VIX is scored as:

- VIX below 15: +1, calm / wall pin more trustworthy
- VIX 15-20: 0, normal-cautious
- VIX above 20: -1, elevated / confirmation required
- VIX above 30: -2, panic / avoid blind fades

DTE is scored as:

- 0DTE with positive and meaningful GEX: stronger pin / range fade bias
- 0DTE with negative GEX: breakout/acceleration risk
- 1-2DTE: walls are less sticky; confirmed breakouts are more valid
- 3-5DTE: weekly structure, use OI walls as planning levels
- longer DTE: GEX is context, not an intraday pin map

GEX strength is interpreted as:

- above 20% gross GEX: very strong pin; fade extremes first
- 10-20% gross GEX: moderate pin; current range read, but respect breaks
- below 10% gross GEX: weak pin; breakouts are valid

## How To Trade This Today

The GEX Daily Plan response now includes an `action_guide` section.  The UI renders it directly on the GEX Plan page.

The action guide includes:

- The current actionable range
- Setup A: Range Fade
- Setup B: Breakout Long
- Setup C: Breakdown Short
- PCR versus GEX tension
- VIX note
- DTE note
- Bottom-line action plan and sizing warning

For example, when positive GEX and 0DTE pin conditions exist, the page will explain that fades near the call wall and gamma flip/support are the primary plan, while a confirmed 1H close outside the range changes the plan to breakout/breakdown mode.

## Files changed

- `oiapp/scanners/spy_strategies.py`
- `oiapp/static/app.js`
- `templates/index.html`
- `templates/index_mine.html`
