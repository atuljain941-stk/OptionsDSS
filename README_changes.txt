Version: 2026-06-20

Phase 1 AI updates:
1. Added an AI-style Trade Analyst layer for Add Trade preview.
2. Added AI portfolio coaching for open trades.
3. Synchronized score / recommendation / headline / next steps so they come from one decision bundle.
4. Added best-strategy and strike suggestions for Add Trade when symbol + expiry are available.
5. Kept the implementation modular in oiapp/ai/journal_ai.py for long-term maintenance.

How to use:
- Open Journal -> Add Trade and type a symbol.
- Select an expiry to load the best strategy, strikes, RR, PoP, and risk summary.
- The preview panel shows the AI Trade Analyst, confidence, thesis bullets, risks, and next actions.
- Open Journal -> Analytics to see the AI Portfolio Coach summary for all open trades.
- Use the health score plus the AI thesis and strategy block to decide whether to open, size down, or skip.

Packaging:
- No database files included.
- No cache / __pycache__ / .pyc files included.
