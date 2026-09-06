# oiapp/services/scanner_primitives_guide.py
"""
Scanner primitives reference page -- reads the LIVE registry
(scanner_builder.py's FUNCTION_CATALOG, via its own existing
/scanner-builder/api/catalog endpoint) rather than maintaining a
separate, hand-written copy.

WHY THIS EXISTS: this app already has many scattered, version-specific
dev-changelog docs (SCANNER_BUILDER_MACD_SPREAD_PRIMITIVES.md,
SCANNER_BUILDER_UAE_PRIMITIVES.md, etc.) -- each one a snapshot of
primitives added at a specific point in time, none of them a single,
current, complete reference, and every one of them destined to go
stale the next time a primitive gets added (this session alone added
several: ATR as a bare series, CrossAbove/CrossBelow, wall-strength
primitives, the Lookback fixes...). A hand-maintained guide has the
exact same problem -- it's accurate the day it's written and wrong
forever after.

This page has no primitive data of its own at all. It's a thin
presentation layer over scanner_builder.py's existing FUNCTION_CATALOG,
fetched fresh from /scanner-builder/api/catalog on every page load.
When a new primitive gets added to that list, it appears here
automatically, with zero changes needed to this file. That's the only
way to actually solve "the guide may be old now" rather than just
resetting the clock on when it goes stale again.

Categorization: FUNCTION_CATALOG has no category field, so grouping is
done client-side by keyword-matching each primitive's own name --
stated as an organizational aid for browsing 255 primitives, not a
claim of official categories. Anything that doesn't match a known
pattern lands in "Other" rather than being force-fit somewhere wrong.

Example usage per primitive is derived mechanically from that
primitive's own verified `signature` string (substituting the default
values already shown in the signature itself), not hand-written from
memory -- 255 individually-composed examples would carry real risk of
inventing syntax that doesn't match what the interpreter actually
accepts. Where a signature doesn't parse cleanly into a usable example,
the raw signature is shown instead of guessing.
"""

from __future__ import annotations

from flask import Blueprint, render_template

scanner_primitives_guide_bp = Blueprint("scanner_primitives_guide", __name__, url_prefix="/scanner-primitives-guide")


@scanner_primitives_guide_bp.route("/")
def page():
    return render_template("scanner_primitives_guide.html")
