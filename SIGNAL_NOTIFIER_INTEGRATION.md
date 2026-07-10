# Signal Notifier — Integration Guide

## What this is

A new file, `oiapp/scanners/signal_notifier.py`, that runs your existing
`trade_opportunity_scanner` on a timer and pushes a Telegram message for any
new or upgraded A/B-grade setup. It does not place trades — it only watches
and notifies. This is Phase 1 of what you asked for.

It reuses code you already have:
- `trade_opportunity_scanner._scan_one()` — scoring, grading, POP, legs, exit rules
- `telegram_alerts.send_telegram_message()` — your existing Telegram bot
- `watchlist_manager._get_setting/_set_setting()` — your existing settings table

## Install (3 steps)

1. Drop the file at `oiapp/scanners/signal_notifier.py`.

2. In `oiapp/app_factory.py`, near where `trade_opp_bp` is registered, add:

```python
try:
    from .scanners.signal_notifier import signal_notifier_bp, start_signal_notifier_watcher
    app.register_blueprint(signal_notifier_bp)
    start_signal_notifier_watcher(app)
    print("[app] Signal notifier watcher started")
except Exception as e:
    print(f"[app] WARNING: signal notifier not started — {e}")
```

3. Turn it on (it ships disabled by default so it won't start pinging you
   immediately). Either hit the API or set it from a quick script:

```bash
curl -X POST http://localhost:5000/signal-notifier/config \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "interval_sec": 900, "min_score": 70}'
```

Or trigger one manual pass right away to see what it would send, without
actually sending (dry run):

```bash
curl -X POST http://localhost:5000/signal-notifier/run \
  -H "Content-Type: application/json" \
  -d '{"dry_run": true}'
```

Your Telegram bot token/chat ID are already configured (you're using them
for price alerts), so nothing new is needed there.

## How it decides TRENDING vs. MEAN_REVERSION

It reuses the trade-type your scanner already picks:
- `PS` / `CS` (directional credit spreads) → **TRENDING**
- `IC` (iron condor) or `trend == SIDEWAYS` → **MEAN_REVERSION**

If you'd rather bucket by your UAE framework's regime labels
(BULL/WEAK_BULL/BEAR/WEAK_BEAR/SIDEWAYS from `uae_trade_scanner.py`) instead
of the generic trend scanner, that's a ~10 line swap — say the word and I'll
wire `uae_trade_scanner._scan_one` in as an alternate source, since it's
literally your own framework already coded.

## Dedupe logic (so you don't get spammed)

A setup only re-alerts if:
- it's a new symbol/trade-type combo for the day, **or**
- its score crosses a grade tier (e.g. B→A), **or**
- its score moves by more than `min_regap_pts` (default 8)

Otherwise it's silently skipped on subsequent scans. History of everything
sent is in the new `signal_notifier_alerts` table / `GET /signal-notifier/history`.

## What the Telegram message looks like

```
📈 Trending setup: NVDA
Grade A (84/100) — Bullish
Type: PS | Expiry: 2026-07-24 (21 DTE)
Legs: Sell 168P / Buy 163P
POP: 78% | Credit: $1.35 | Max loss: $3.65 | RR: 0.37
Why: Regime bullish ✓; RS +6.2% vs SPY; IVR 58% — good credit
Exit plan: Take profit at 50% credit ($0.68). Close at <7 DTE.
⏱ 2026-07-03 14:32:01
```

## Roadmap: Phase 2 and 3

You already have the hard part of Phase 3 built:
`oiapp/autotrading/schwab_eod.py` builds real stop/OCO order payloads and is
gated by `OIAPP_AUTOTRADE_LIVE_ORDERS` (0 = preview/dry-run, 1 = live). That's
your kill switch for going from "notify" to "trade."

**Phase 2 (semi-auto, tap-to-approve)** — the natural next build:
- Add inline Telegram buttons ("Approve" / "Skip") to each alert using
  Telegram's `reply_markup` (a small addition to `send_telegram_message`)
- A lightweight webhook endpoint that receives the button tap and calls
  into `schwab_eod`'s existing order-builder in preview mode, then asks you
  to confirm size before it actually submits
- This keeps a human in the loop on every single trade but removes all the
  screen-watching — you just react to a phone notification

**Phase 3 (full auto)** — once you trust Phase 2's signal quality over a
few weeks of live tracking:
- Flip `OIAPP_AUTOTRADE_LIVE_ORDERS=1`
- Wire `run_signal_scan()`'s A-grade hits directly into `schwab_eod`'s order
  submission instead of stopping at Telegram
- Keep a daily/weekly loss circuit-breaker (max trades/day, max loss/day)
  before letting it run unattended — this is the one piece that doesn't
  exist anywhere in the codebase yet and is worth building before Phase 3,
  not after

## One thing worth deciding before Phase 2

Right now `trade_opportunity_scanner` is options-focused (spreads/condors).
Your top-of-mind trading right now is gold futures (MGC) with your UAE
framework — that's a different instrument class than what this scanner
covers. If you want futures signals in the same Telegram feed, that's a
separate small scanner (UAE regime + entry trigger on futures OHLCV, no
options chain needed) rather than a reuse of `trade_opportunity_scanner`.
Worth telling me which one you want first.
