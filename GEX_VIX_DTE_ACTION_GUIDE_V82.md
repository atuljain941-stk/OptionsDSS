# GEX Plan V82 - VIX/DTE Scoring and Action Guide

## What changed

The GEX Plan now promotes VIX regime and DTE/expiry effect into the scored composite grid instead of leaving them only as footer/context information.

### VIX Regime factor

Scoring:

- `+1` when VIX is below 15
- `0` when VIX is 15 to 20
- `-1` when VIX is above 20
- `-2` when VIX is above 30

This is shown directly in the GEX composite grid as `VIX Regime`.

### DTE Effect factor

Scoring:

- `0DTE + positive/moderate GEX` => stronger pin factor
- `0DTE + negative GEX` => breakout/acceleration risk factor
- `1-2DTE` => walls less sticky, breakouts more valid
- `3-5DTE` => weekly OI structure useful for range/wing planning
- longer DTE => context only

This is shown directly in the GEX composite grid as `DTE Effect`.

### GEX strength note

The existing `GEX Regime` factor now includes GEX strength as percent of gross GEX:

- `>20%` = very strong pin, fade-first environment
- `10-20%` = moderate pin, current reading
- `<10%` = weak pin, confirmed breakouts are valid

## How To Trade This Today

The Daily/GEX Plan response now includes an `action_guide` payload and UI panel. It converts GEX/PCR/VIX/DTE into:

- Range for the day
- Setup A: Range Fade
- Setup B: Breakout Long
- Setup C: Breakdown Short
- Key tension to watch, especially GEX vs PCR conflict
- VIX note
- DTE note
- Bottom-line action plan and sizing guidance

The guide uses gamma flip, call wall, put wall, GEX strength, PCR, VIX, DTE, and expected move/sigma to create actionable triggers, targets, and stops.
