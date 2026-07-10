# OI Buildup / Seller Flow UAE Context v95

This build adds UAE Trend/Vol Analyzer context to the Seller Flow / OI Buildup response.

## What was added

The Seller Flow scanner now enriches each row with the same Scanner Builder UAE primitives aligned to `AJ - Trend/Vol Analyzer v5`:

- Daily UAE regime
- Weekly UAE regime
- Daily and weekly UAE score
- Daily and weekly RSIDiff
- Daily and weekly latest visible marker
- Marker age for MRT/fade arrows and trend triangles
- OI-vs-UAE confirmation label
- UAE action note

The scanner uses local cached bars only for this enrichment, so it does not trigger live yfinance calls while scanning.

## New result fields

Example fields returned by `/api/oi_buildup_screener` and AI Hub OI buildup scans:

```text
uae_daily_regime
uae_weekly_regime
uae_daily_score
uae_weekly_score
uae_daily_rsidiff
uae_weekly_rsidiff
uae_daily_marker
uae_daily_marker_label
uae_daily_marker_age
uae_weekly_marker
uae_weekly_marker_label
uae_weekly_marker_age
uae_confirmation
uae_alignment_score
uae_action_note
```

## Confirmation labels

```text
UAE confirms
UAE conflicts
UAE mixed
UAE neutral
UAE unavailable
```

## UI changes

The Seller Flow card now shows a compact UAE row directly under the suggested strategy:

```text
UAE | UAE confirms | D BULL · W WEAK_BULL
```

The snapshot chip row also includes a UAE chip.

AI Hub OI buildup answers now include UAE timing in the narrative and in the result table.

## Pine v5 priority model

The UAE enrichment follows the Scanner Builder UAE primitives that were aligned to the Pine v5 signal-priority logic:

1. MRT/fade arrow first.
2. Trend triangle only when no same-bar fade fires.
3. Trend triangle requires MACD zero-cross, trending condition, and strong histogram.

## Maintenance note

This build reuses Scanner Builder UAE primitive evaluation instead of duplicating UAE formulas inside the OI scanner.
