# OptionsDSS Codex handoff

- Repository: `atuljain941-stk/OptionsDSS`; work branch: `fix/tastytrade-summary-collection`.
- The SQLite path is configured by `OIAPP_DB_PATH`; never overwrite the live database. Use `oiapp.config.DB_PATH` and short-lived read connections for scans.
- MTF page: `templates/index.html`; API: `oiapp/scanners/mtf_scanner.py`; Scanner Builder API supplies technical primitives and context.
- Existing MTF behavior must remain intact. Trade recommendations must use only the newest `options.fetch_ts` chain per symbol and actual saved prices/Greeks—never modeled prices.
- Product constraints: 14–45 DTE; OI/liquidity required; RR >= 0.60; otherwise Signal only. Flag stale/invalid/illiquid chains and elevated test risk.
- Before committing, inspect `git status`: this checkout may contain unrelated user work. Stage only files changed for the requested task.
