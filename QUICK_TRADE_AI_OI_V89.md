# Quick Trade AI scoring + OI context v89

This build upgrades the global **➕ Trade** modal so it is not a separate lightweight form only.

## What changed

- The modal now includes a **Journal AI score / recommendation** panel.
- The panel calls the same backend endpoint used by the full Journal Add Trade screen: `/journal/draft_health_score`.
- The rendering has been refactored into a shared client-side renderer, so the full Journal Add Trade page and the Quick Trade modal show the same score card, recommendation, factors, AI summary, best strategy, risk notes, and next actions.
- The modal now includes the same **OI / IV by expiry** chart context used by the Journal Add Trade page.
- The OI/IV chart code has been refactored into a shared helper so the Journal Add Trade page and Quick Trade modal use the same API calls, expiry selector behavior, Plotly chart rendering, selected-strike marker, and live IV overlay logic.

## Reused code paths

- Journal score endpoint: `/journal/draft_health_score`
- Journal save endpoint: `/journal/trade/add`
- OI expiration API: `/api/db_expirations`
- OI rows API: `/api/options`
- live option chain / IV overlay API: `/strategy/live_prices`
- Shared JS renderer: `_jrnRenderEntryScorePreview(panel, d)`
- Shared OI/IV chart renderer: `_renderOptionOiIvContext(opts)`

## No duplicate-maintenance intent

The Quick Trade modal now reuses the same scoring and OI chart primitives as the Journal Add Trade page. Future changes to the Journal score-card rendering or OI/IV chart helper should flow through both places.
