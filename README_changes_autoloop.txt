Agentic AI auto-loop weight tuning changes
=========================================

- Added manual Auto-loop weights and Stop auto-loop controls to both Agentic AI scanner pages.
- Added full-scan auto-loop trials that test evidence-weight / gate combinations until target candidates and target confidence are met, or max trials is reached.
- Empty/no-result trials are treated as negative feedback and used to relax gates and rebalance evidence weights for the next trial.
- Successful/sparse/weak-confidence trials are used to tighten or rebalance the profile depending on outcome quality.
- Best profile is saved back to app_settings after the loop completes.
- Trial history is stored in agentic_ai_autotune_trials for audit/debugging.
- Auto-loop runs use max_alerts_per_run=0 so tuning trials do not spam alerts; the saved profile keeps the user's original alert setting.

New endpoints:
- POST /agentic-ai-scanner/api/autotune/start
- POST /agentic-ai-scanner/api/autotune/stop
- GET  /agentic-ai-scanner/api/autotune/status

New table:
- agentic_ai_autotune_trials

