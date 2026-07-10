# GEX Wall Significance v30

This update ranks option walls by more than total OI.

Wall score uses:

- 40% current OI rank
- 30% positive day-over-day OI change rank
- 20% proximity / z-score to spot using the expected daily move
- 10% per-strike GEX contribution

The goal is to distinguish:

- Major Fresh Wall: large current OI plus fresh positive OI change
- Major Existing Wall: large current OI but less fresh change
- Fresh Developing Wall: new positive OI change that may be building into a wall
- Unwinding Wall: current OI is shrinking materially
- Weak/Stale Wall: lower score or far from spot

VIX is included as a reliability overlay, not as a wall selector. High VIX or a VIX spike can make GEX levels less reliable even if the wall itself is large.

Updated areas:

- GEX Plan /spy/daily_plan payload
- GEX Plan UI wall quality table
- GEX Pine JSON endpoint /gex/pine?fmt=json
- GEX Live page wall chips and wall detail panel

The CSV/Pine string remains backward compatible with the existing first 20 fields.
