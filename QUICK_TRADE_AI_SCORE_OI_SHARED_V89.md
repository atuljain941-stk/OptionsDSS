# Quick Trade AI score + OI chart shared-code update v89

The global Quick Trade modal now exposes the same analysis users see on the full Journal Add Trade page without duplicating scoring or chart logic.

## Shared scoring

Quick Trade calls the existing Journal endpoint:

```text
POST /journal/draft_health_score
```

The Quick Trade score panel renders through the same frontend renderer used by the full Add Trade panel:

```text
_jrnRenderEntryScorePreview(...)
```

So the modal and Journal Add Trade page remain aligned for:

- Journal Health Score
- grade
- OPEN / HOLD / AVOID / ROLL-style recommendation text
- AI Trade Analyst summary
- best strategy and alternatives
- PNR / DTE / spot / current mark
- signal factors, notes, and suggestions

## Shared OI / IV chart

Quick Trade uses the same reusable OI/IV chart helper as Journal Add Trade:

```text
_renderOptionOiIvContext(...)
```

It uses the current Quick Trade symbol, first option expiry, first option strike, and chart-expiry selector to render the same OI + IV by strike chart inside the modal.

## Maintenance rule

Do not add separate Quick Trade scoring or OI calculation logic. Any future Journal Add Trade scoring/chart change should flow through the shared endpoint/helper functions above.
