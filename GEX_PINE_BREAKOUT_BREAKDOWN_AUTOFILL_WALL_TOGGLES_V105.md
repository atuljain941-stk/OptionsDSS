# v105 - GEX Pine export parity fix (Breakout/Breakdown) + auto-fill string + wall toggles + table; DTE pages linked into main nav

## 1. Why TradingView was missing Breakout / Breakdown / Balance-Pin

Your in-app "Key Levels" panel (`/spy/daily_plan`, built by
`_build_trade_plan()` in `spy_strategies.py`) has always computed and
shown 8 levels: Call Wall, Gamma Flip, **Breakout**, Balance/Pin, Max
Pain, Spot, **Breakdown**, Put Wall.

The `/gex/pine` export (`gex_pine_export.py`) — the CSV your "Copy Pine
String" button on `/gex/live` copies for pasting into TradingView —
only ever exported 20 fields: spot, gamma_flip, pin, max_pain,
sigma_high/low, 3 call walls, 3 put walls, and GEX/PCR/IV/score stats.
**Breakout and Breakdown were never in that export at all** (Balance/
Pin *was* already there, just under the name "pin"). That's the whole
reason the Pine script/TradingView chart never had those two lines —
not a chart bug, a missing field in the data feed.

**Fix:** `breakout`/`breakdown` now computed in `gex_pine_export.py`
using the exact same formula as the in-app panel (`spot + 0.4×1σ` /
`spot - 0.5×1σ`), so the two are always consistent. CSV grew from 20 →
22 values (new order documented at the top of the file); JSON response
also carries `breakout`/`breakdown`. `/gex/live`'s cards now show them
too, for parity with the in-app Key Levels panel.

## 2. Pine script (`GEX_Levels_TradingView.pine`) — v2

### Also found while in there: the promised auto-fill never existed
The on-page instructions on `/gex/live` have always said "paste into
`Paste GEX string (auto-fill)` → OK → all lines draw instantly." That
input never existed in the script -- it only had 20 separate manual
number fields, meaning the actual workflow was typing each value in by
hand every morning, not what the instructions describe.

**Added:** a single `Paste GEX String (auto-fill)` text input (new
"Auto-Fill from OI App" group, top of Settings). Paste the CSV from
"Copy Pine String" and every line + table cell fills in from it
immediately -- `str.split()` + `str.tonumber()` per field, with the
existing manual fields kept only as a fallback for whichever fields
the pasted string doesn't cover (or if you leave it blank entirely).
Put/Call wall **scores** and the **Bias label** aren't in the CSV, so
those stay manual-only; Bias auto-derives from the 5-Factor Score
whenever the auto-fill string is used, so you don't have to set it by
hand either.

### Breakout / Breakdown now drawn
New amber (Breakout) and rose (Breakdown) lines + labels, same style
as the existing Gamma Flip/Pin/Max Pain lines. Also added as two new
rows in the info table's "KEY LEVELS" section (with distance-from-spot,
same pattern as the existing rows).

### Put/Call wall visibility toggles
Two new booleans in the Style group: `Show Call Walls (lines + table)`,
`Show Put Walls (lines + table)`. Off = the wall's lines and labels
don't draw on the chart at all, and its 3 table rows show "hidden"
instead of a price (rows stay in place so the table doesn't jump size
depending on toggle state).

### Table
Already existed in v1 -- `show_table` toggle unchanged. Row count grew
26 → 28 to fit the two new Breakout/Breakdown rows; every row index
below them was renumbered.

## 3. DTE pages linked into the main nav

`/dte/intraday` and `/dte/positional` (from v104) were reachable only
by typing the URL directly. Added as two link pills in `templates/index.html`,
next to the existing nav-pill row (open in a new tab, same pattern
already used elsewhere in the app for standalone full pages like the
Clean Page link on the AI Scanner tab).

## 4. v104 URL bug also fixed in this package
(Carried forward from the previous message, included here since this
is the full source tree.) `api_bp` already carries `url_prefix="/api"`;
the first v104 cut repeated `/api` in the JSON/refresh route decorators
and put the two page routes on `api_bp` too, so `/dte/intraday` 404'd
and `/api/dte/refresh` doubled to `/api/api/dte/refresh`. Fixed: pages
now live on a new prefix-free `dte_pages_bp` (`/dte/intraday`,
`/dte/positional`); JSON/refresh stay on `api_bp` without the repeated
`/api` (`/api/dte/intraday_data`, `/api/dte/positional_data`,
`/api/dte/refresh`).

No database file included in this package, per standing instruction.
