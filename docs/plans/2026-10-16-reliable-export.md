# Reliable diary export implementation plan

**Goal:** Export the account's own diary text without pagination loops or misleading completion claims.

**Architecture:** Preserve the existing interactive command. Follow server-provided continuation information, count unique IDs as records arrive, checkpoint local output, and compare results with the account's reported diary count. Investigate authenticated native read endpoints if normal web pagination stops early. Never commit real account data or credentials.

## Done criteria
1. Regression tests reproduce repeated terminal pages and false completion before fixes.
2. Full, partial, cancelled and failed runs are distinguishable; output remains recoverable.
3. Every extracted record is represented in JSON/TXT/Markdown, including undated records.
4. Any native data source is tested with synthetic protocol fixtures and verified against owned-account data locally.
5. Live export counts and date range are inspected; limitations are stated, not hidden.
6. Synthetic tests run in CI on Windows and Linux. Independent code review is addressed.
7. Only code, synthetic tests and documentation reach GitHub through Issue #1 and its pull request.

## Tasks
1. Investigate actual load-more requests and native account endpoints; save research outside the repository.
2. Write failing synthetic tests for terminal pages, duplicates, checkpoints, count mismatch and missing dates.
3. Implement the smallest verified pagination and export-status fixes; retain existing CLI compatibility.
4. If necessary, add a separate native read client with explicit authentication and safe session handling; do not touch user data on the service.
5. Run automated tests and a real export; compare profile count, unique IDs, output records and Markdown coverage.
6. Document usage and failure recovery; add CI jobs `ci` and `merge-serial`.
7. Review, commit with (#1), push, open PR with Closes #1, verify checks and integrate when green.
