# OI Buildup / Seller Flow UI Compact Cards v66

Changes in this patch:

- The Seller Flow card was redesigned to reduce height and avoid horizontal scrolling.
- The top snapshot metrics now render as a horizontal row of compact chips instead of tall metric boxes.
- Suggested setup now appears immediately below the final seller-flow read.
- Suggested setup includes strategy code and strike anchors when available, using latest strike-level OI support/resistance walls.
- Earnings date and days-to-earnings are shown next to the suggested setup so earnings conflicts are visible without opening details.
- ST / MT / LT sections are collapsed by default. Their summary rows show signal, OI change, PCR change, skew change, and max-pain shift.
- Rationale remains collapsed by default.
- Backend now exposes put/call wall strike anchors and spread-leg hints for the Seller Flow UI and AI Hub.

No database files are included in the package.
