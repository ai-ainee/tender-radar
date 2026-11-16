# Radar Scout V5 — Offline-Tested Candidate

Source: consolidated V4 repository snapshot. The V5 additions are `qualification_v5.py`, changes in `radar.py`, and `tests/test_qualification_v5.py`.

## Changes
- Conservative, source-grounded pre-AI qualification for procurement, named corporate projects/expansion, filings, and employer-specific hiring.
- Rejects generic tutorials, marketing, irrelevant news, and unnamed events before selection.
- Infers buyer organization only when the title contains a recognizable subject; inferred names are **unverified** and must be confirmed by downstream review.
- Prioritizes candidate leads by evidence score before the run's lead cap; preserves source order on equal score.
- Adds V5 columns to dry-run audit while preserving V4 column names for compatibility.

## Validation
- `python -m unittest discover -s tests -q`: 204 tests passed locally.
- `v5_replay_audit.csv`: deterministic reclassification of the previous V4 30-lead CSV. This is not a live crawl.

## Safety / limitations
- This is an **offline-tested candidate**, NOT a production-verified release.
- A PROVISIONAL record is NOT a verified active tender, confirmed purchasing intent, or verified buyer.
- This package does not add a separate production CRM quarantine or test external credentials, live GeM/CPPP, Google Apps Script, Telegram, or GitHub Actions.
- The quality gate uses conservative heuristics and can reject valid opportunities, especially unnamed events, non-English headlines, or unfamiliar headline formats. Review replay audit before adoption.
- Keep `RADAR_DRY_RUN=1` for the first integration run and keep production CRM writes disabled. Do not replace the production branch until integration tests pass.
- Never commit real `.env`, API keys, caches, or operational state files.
