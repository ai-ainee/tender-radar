# Radar Scout V5.1 — targeted qualification correction

Based on GitHub integration branch commit `1af9501`.

Changes:
- Tender pages require a specific numeric reference or a PDF procurement invitation; generic directories are rejected.
- Specific references remain **PROVISIONAL**, never marked as officially verified by the offline gate.
- Same-run project deduplication is deliberately conservative: named company + project type + location + matching amount. Weakly identified articles are not automatically merged.
- Replayed the previous 12-record V5 dry-run audit in `V51_REPLAY_AUDIT.csv`.
- Added 7 focused tests, alongside existing offline suite.

Limits: no live GeM/CPPP validation; no guarantee of CRM write quarantine. Do not deploy to production until controlled GitHub dry-run and write-path review.
