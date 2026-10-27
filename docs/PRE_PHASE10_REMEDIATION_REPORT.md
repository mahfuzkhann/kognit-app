# Pre-Phase 10 remediation report

Date: 2026-10-26. Owner: Claude (AI Lead Engineer). Reviewer: ChatGPT. Decisions: Mahfuz.
**Starting commit:** `8baa2d6` (`main`, working tree clean at start).
**Ending commit:** `8baa2d6` - **nothing was committed, pushed, merged or deployed.** All changes below are uncommitted in the working tree.
No live service (Supabase, Gemini, GitHub write) was contacted. Database tests ran on a throw-away local PostgreSQL 16 inside the sandbox with stubbed Supabase roles.

## 1. Status of every finding

| ID | Priority | Status | Summary |
|---|---|---|---|
| F-01 | P1 | **FIXED AND VERIFIED** (local) | Original chat concurrency test restored byte-for-byte from `2807af9`; Notes test is its own file; runner exits 0 |
| F-02 | P2 | **FIXED LOCALLY - LIVE VERIFICATION REQUIRED** | Migration 0012 revokes the leftover grants; verify query 4 rewritten and returns zero rows on a 0012 database (4 rows on a 0011-only one) |
| F-03 | P2 | **FIXED LOCALLY - LIVE VERIFICATION REQUIRED** | fastapi 0.133.0 / starlette 1.3.1; `pip-audit`: no known vulnerabilities in the full resolved set; real-server tests added; live Gemini not verified |
| F-06 | P2 | **FOUNDER ACTION REQUIRED** | Local real-server upload tests pass on the upgraded stack; real photos/PDFs against live Gemini still need a human run |
| F-07 | P2 | **PARTIALLY MITIGATED** | Reproduced; now bounded to 500 ledger rows per user per 24 h (migration 0012). Many-account farming remains (F-09). Secret-gating the RPCs left as a founder decision |
| F-09 | P2 | **FOUNDER ACTION REQUIRED** | App code enforces no email-confirmation or CAPTCHA; depends on Supabase dashboard settings. Exact steps in the live checklist |
| F-04 | P2 | **FOUNDER ACTION REQUIRED** (unchanged, out of scope) | In-memory PDF/quiz state and rate limits are per process: run a single worker |
| F-15 (new) | P2 | **FIXED LOCALLY** | Supabase Auth outages (5xx/429) were reported to students as "session expired" (401) |
| F-16 (new) | P3 | Observation, not changed | Default Supabase privileges give API roles `TRUNCATE`/`REFERENCES`/`TRIGGER` on user-data tables; not reachable through the REST API |
| F-08 | P3 | Unchanged | Cancelled streams write no usage row |
| F-10 | P3 | Policy, unchanged | Pre-text cancel never refunded; two failed attempts after partial text still charge one unit |
| F-11 | P3 | Unchanged | `.gitignore` covers only `.env`; many files lack a final newline |
| F-12 | P3 | Unchanged (hypothesis) | `_rpc` logs up to 300 chars of an error body; no path to the secret found |
| F-13 | P3 | **PARTIALLY MITIGATED** | New auth/real-server tests added; still no frontend test for the `notes.js` quota message; browser suites not run |
| F-14 | P3 | **PARTIALLY MITIGATED** | `PRE_PHASE10_FINALIZATION_REPORT.md` totals are stale (it describes an earlier state); this report and the audit addendum supersede its numbers |

## 2. F-01 - restore the database concurrency tests (P1)

- **Defect / root cause:** the committed `supabase/tests/concurrency_test.sh` contained the Notes test (identical to the delivered notes script apart from a final newline) and `concurrency_notes_test.sh` did not exist, so `run_local_db_tests.sh` aborted on its second concurrency call and `test_database_behaviour.py` failed whenever a database was available. Most likely the new file was copied over the old file name; that cause is a guess.
- **Fix:** `concurrency_test.sh` restored with `git show 2807af9:...`, verified byte-identical with `cmp`. `concurrency_notes_test.sh` added as a separate file (its header comment corrected: it claimed chat requests ran in parallel, which it never did; default N set to 24 to match the requirement; the runner passes 24).
- **Review notes:** both scripts use `set -euo pipefail` and fail closed (a pipeline that finds no result also fails). They insert test users and ledger rows and **must only run against a throw-away database**; the runner creates its own. Not changed because the chat file had to be restored exactly; recorded as a P3 caution.
- **Negative controls (tests must fail when wrong):** raising the chat limit to 3 made `concurrency_test.sh` fail ("expected exactly 1 allowed, got 3"); raising the Notes limit to 9 made `concurrency_notes_test.sh` fail.
- **Results:** `concurrency_test.sh`: exit 0, "exactly 1 of 24". `concurrency_notes_test.sh`: exit 0, "exactly 6 of 24 ... chat meter untouched". `run_local_db_tests.sh`: exit 0. Pytest with PostgreSQL: 0 failures. Pytest without PostgreSQL: 0 failures, 2 documented skips.

## 3. F-02 - least privilege (P2)

- **Analysis:** the application needs **no** table or sequence privileges on `ai_usage_events` for `anon`/`authenticated`: telemetry is written by the SECURITY DEFINER function `record_ai_usage_events` (owned by the table owner) and read by the founder as project owner. `quota_ledger`, `usage_write_keys` and the sequences already gave API roles nothing (verified from the ACLs). Only `REFERENCES` and `TRIGGER` on `ai_usage_events` remained (default privileges plus earlier targeted revokes). Impact was low (both need DDL rights, which API roles lack) but the documented acceptance query could never return zero.
- **Fix:** `0012_usage_privileges_and_ledger_cap.sql` section 1: `revoke all` on the table and its sequence from `public, anon, authenticated` (idempotent, forward-only; historical migrations untouched). Default privileges for future tables are deliberately **not** altered (a platform-wide setting); each new table's migration must revoke explicitly.
- **Verification query:** `supabase/verify_policies.sql` query 4 now reads the ACLs directly (tables and sequences, including `PUBLIC` and default privileges). Run on a 0011-only database it lists the four leftover rows; on a 0012 database it returns none.
- **Tests:** `supabase/tests/ledger_cap_and_privileges_test.sql` asserts every privilege type on the three server-owned tables and their sequences is absent for both roles, anonymous and authenticated direct inserts are refused, the correct secret still records an event, a wrong secret is refused (42501). The test fails on a 0011-only database ("anon must not hold REFERENCES on public.ai_usage_events").

## 4. F-03 - FastAPI / Starlette (P2)

- **Advisories (pip-audit, 14 database entries = 7 distinct CVEs, two identifiers each) and applicability:**

| Advisory | Component | Applicable here? |
|---|---|---|
| CVE-2024-47874 | multipart fields without a filename buffered without limit | **Yes, reproduced** |
| CVE-2026-54283 | `max_fields`/`max_part_size` ignored for urlencoded forms | **Yes, reproduced** |
| CVE-2025-54121 | large uploads block the event loop when spooling rolls to disk | Yes (PDF/image uploads) |
| CVE-2026-48710, CVE-2026-54282 | host header / path handling in `request.url` | The app never uses `request.url`/`base_url`: not exploitable here; fixed anyway |
| CVE-2026-48817 | `HTTPEndpoint` method dispatch | Not applicable: no `HTTPEndpoint` in the app |
| CVE-2026-48818 | `StaticFiles` UNC-path request forgery on **Windows** | Relevant only if run on Windows; the app mounts `StaticFiles` |

- **Reproduction:** against the old pins under a real uvicorn server, a 2 MB text form field and a 2,500-field urlencoded form were **accepted (HTTP 200)**. On the upgraded stack both are refused (HTTP 400).
- **Version selection:** searched all FastAPI releases for the oldest whose declared Starlette range allows a fixed Starlette (>= 1.3.1, the highest "fix" version among the applicable advisories): **fastapi 0.133.0** (0.132.1 rejects it). Starlette was not upgraded independently. Resolved in a clean virtual environment: `pip check` clean. Other bumps from the earlier audit round (jinja2 3.1.6, Pillow 13.0.0, pypdf 6.21.2, python-dotenv 1.2.2) are kept. New transitive package: `annotated-doc`.
- **Results on the upgraded stack:** pytest without PostgreSQL 2017 passed, 2 skipped; with PostgreSQL 2018 passed, 1 skipped; smoke 72 passed; frontend jsdom 10 suites, 489 passed, 0 failed. `pip-audit` on the full resolved set (transitives included): **no known vulnerabilities**. Remaining advisories: none known.
- **New tests:** `test_real_server_uploads.py` (14 tests) serves the app with real uvicorn and exercises valid PDF/image, 413 limits, corrupt files, streaming events, form-parser limits, auth, quota settlement, a real client disconnect and the post-consumption exception path. Run against the old pins, exactly the three form-parser tests fail, which is the proof that the advisories applied.
- **Not verified:** live Gemini behaviour, real student photos and PDFs, Windows runtime. There is no Python lockfile, so transitive versions float: run `pip-audit` before each deploy. Test-only deprecation warning: Starlette's test client warns that `httpx` support is deprecated in favour of `httpx2`; harmless today, plan to adapt later.

## 5. F-07 - quota ledger growth (P2)

- **Attack (confirmed, isolated local database):** a signed-in student calls `consume_quota` with a refund-token hash they chose, then `refund_quota` with the matching token, repeatedly. 5,000 cycles took 2.3 s database-side and added 5,001 ledger rows (about 2.5 MB, roughly 500 bytes/row with indexes). **No quota is gained**: afterwards the student's real request was allowed once and the next was denied by the plan limit. So: verified storage-growth exposure, no free AI. Over HTTP the rate is lower and cannot be extrapolated reliably; a single account could add on the order of tens of MB per hour (an estimate, not a measurement). Other student-writable tables (notes, projects) have similar growth paths; platform-level storage alerts remain the general control.
- **Options compared:**
  1. *Secret-gate the quota RPCs* (like the usage writer): blocks direct calls completely, but adds a new required secret, changes the function signatures (breaking a rolling deploy), and a misconfigured variable would lock **every** student out. Rejected for now.
  2. *Per-user row cap inside `consume_quota`* (chosen): no new secret, no signature change, refunds unaffected, bounded growth.
  3. *Periodic purge*: bounds age, not rate; offered as an optional founder-run statement in 0012.
- **Implementation:** `consume_quota` refuses to insert when the user already has **500** ledger rows (all kinds, refunded included) in the last 24 h, returning reason `rate_limited` with a reset time. The cap sits after the plan-limit check, so ordinary refusals are unchanged, and before the insert, so a denied loop adds no rows. Adds index `quota_ledger_user_created_idx`. The API maps it to HTTP 429 `QUOTA_EXCEEDED` with the message "Too many requests in a short time..." (the frontend already shows the server's message).
- **Why 500:** a Free student produces roughly 3-12 rows a day; 500 leaves large headroom. **When Pro daily limits are chosen, keep a Pro student's legitimate rows per day well below 500 or raise the cap first** (documented in DEPLOYMENT.md and the migration). It is a tunable hypothesis, not a validated number. Concurrency note: the cap is checked under the per-user-and-kind lock, so different kinds racing at the cap can overshoot by at most 2 rows (number of kinds minus one).
- **Residual:** a motivated abuser keeps 500 rows/day per account (about 250 KB), and many accounts are a separate problem (F-09). **Founder decision:** whether to also secret-gate the RPCs later.
- **Tests (SQL):** legitimate consume/refund unchanged; second request denied by the plan limit, not the cap; duplicate refund no-op; wrong token refused; the loop is stopped at exactly 500 cycles with reason `rate_limited`; 200 further attempts add no rows; all attack rows are refunded (nothing consumed); other users unaffected; rows older than 24 h stop counting; rows of every kind count toward the cap. The existing 24-way concurrency tests still pass (1 of 24 chat, 6 of 24 notes). Python: API mapping tests in `test_quota_settlement.py`.

## 6. Authentication (F-09 area) and the new finding F-15

- **Local review:** `_verify_supabase_token` fails closed on missing configuration (500), rejects malformed headers (401), verifies each token against Supabase Auth, and takes the user id only from Supabase's answer (a body `user_id` is ignored; tested through the real dependency chain).
- **F-15 (confirmed, fixed):** any non-200 answer from Supabase Auth became 401 "session expired", and the frontend shows the login prompt on every 401. During a Supabase outage or throttling students would be told to log in again. Now 5xx and 429 become 503 "try again"; invalid or expired sessions remain 401. Minimal change in `backend/main.py` (one block).
- **Tests:** `test_auth_verification.py` (30 tests) runs a real local HTTP stand-in for `/auth/v1/user`: good/expired/forbidden/odd/non-JSON responses, 500/502/503/429, unreachable server, header edge cases, fail-closed configuration, tokens never logged. Against the old logic the five outage cases fail.
- **Cannot be done from the repository:** email confirmation, CAPTCHA, auth rate limits, redirect URLs, live upload checks -> `docs/PRE_PHASE10_LIVE_VERIFICATION_CHECKLIST.md`.

## 7. Files changed (all uncommitted)

Modified: `backend/main.py`, `backend/quota_service.py`, `requirements.txt`, `docs/DEPLOYMENT.md`, `supabase/verify_policies.sql`, `supabase/tests/concurrency_test.sh` (restored to `2807af9`), `supabase/tests/run_local_db_tests.sh`, `test_quota_settlement.py`, `test_usage_telemetry_integrity.py`.
Added: `supabase/migrations/0012_usage_privileges_and_ledger_cap.sql`, `supabase/tests/concurrency_notes_test.sh`, `supabase/tests/ledger_cap_and_privileges_test.sql`, `test_real_server_uploads.py`, `test_auth_verification.py`, `docs/PRE_PHASE10_REMEDIATION_REPORT.md`, `docs/PRE_PHASE10_LIVE_VERIFICATION_CHECKLIST.md`, `docs/FINAL_PRE_PHASE10_READINESS_AUDIT.md` (the audit report, which had never been committed; now with a remediation addendum that preserves the original text).

## 8. Test results (actually run, Linux, Python 3.12.3)

| Check | Result |
|---|---|
| `concurrency_test.sh` / `concurrency_notes_test.sh` standalone | exit 0 / exit 0 |
| Negative controls (wrong limits) | both exit non-zero as intended |
| `run_local_db_tests.sh` (migrations 0001-0012, re-apply 0008-0012, behaviour, hardening, ledger-cap/privileges, both concurrency tests) | exit 0 |
| Full pytest, upgraded stack, **with** PostgreSQL | exit 0: 2018 passed, 1 skipped (Windows-only test) |
| Full pytest, upgraded stack, **without** PostgreSQL | exit 0: 2017 passed, 2 skipped (PostgreSQL test; Windows-only test) |
| Same, without `pytest-asyncio` installed | 2017 passed, 2 skipped |
| Smoke suite | 72 passed |
| Frontend jsdom (10 suites) | 489 passed, 0 failed |
| `compileall`, `import backend.main`, `node --check` on all JS | passed |
| `git diff --check` (tracked) and trailing-whitespace scan of new files | clean |
| `pip check` / `pip-audit` (full resolved set) | clean / no known vulnerabilities |
| Real-browser frontend suites | **NOT RUN** (no browser available) |
| Anything against live Supabase, live Gemini, production, or Windows | **NOT RUN** |

Skipped tests are not counted as passed.

## 9. Secrets and hygiene

Diff and new files scanned for key/JWT/private-key/database-URL shapes: no matches. `.env` remains git-ignored; `.env.example` unchanged. No generated files are tracked (`__pycache__`, `node_modules` ignored). The test fixtures use obvious dummy values.

## 10. Remaining issues after this mission

P0: none. P1: none known (F-01 closed locally). P2: F-03 live check, F-06 live check, F-07 residual (many accounts / founder decision), F-09 dashboard settings, F-04 single-worker deployment. P3: F-08, F-10, F-11, F-12, F-13, F-14, F-16, plus: no frontend test for the Notes quota message; the non-stream `interrupted` label is not displayed by the UI; no Python lockfile. Dependency advisories remaining: none known.

## 11. Closure decisions

1. **Pre-Phase 10 code readiness: READY (code complete, tested locally).** No known P0/P1 defects; every local acceptance criterion is met. It closes only after you apply 0011 and 0012, configure the usage writer and complete steps 1-5 of the live checklist.
2. **Beta readiness: NOT APPROVED yet.** Requires the live checklist items for migrations, usage writer, Auth settings (email confirmation, CAPTCHA, rate limits, redirect URLs) and real Gemini/PDF/image checks on the upgraded stack.
3. **Production readiness: NOT APPROVED.** Beta evidence, single-worker or shared-state decision, monitoring and a Pro-limits/payment plan are all still open.

**Can Pre-Phase 10 be closed?** Not yet: it can be closed when the founder completes live checklist steps 0-5 and records the evidence. Phase 10 itself must not start merely because this is done: your Phase 10 document requires launch plus 1-2 months of real usage, a readiness gate, funding or provider access for AI Engine 2.0, and a written Phase 10 scope. None of those exists yet, and the repository contains no Phase 10 specification.

## 12. Commands

Windows (PowerShell, repository root, new virtual environment):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt pytest
$env:GEMINI_API_KEY="dummy-test-key"; $env:SUPABASE_URL="https://example.supabase.co"; $env:SUPABASE_ANON_KEY="dummy-anon"   # test values only
python -m pytest -q -p no:cacheprovider -rs
python -m pytest -q -p no:cacheprovider kognit_smoke_test.py
python -m pip install pip-audit; python -m pip_audit -r requirements.txt
```
Expect about 2 skips on Windows (the PostgreSQL test and a Unix-only memory test). If you have a throw-away PostgreSQL (`psql`, `createdb` on PATH, `PGHOST` set), the database suite runs too.

Commands used here (Linux): `bash supabase/tests/concurrency_test.sh`, `bash supabase/tests/concurrency_notes_test.sh`, `bash supabase/tests/run_local_db_tests.sh`, `python -m pytest -q -p no:cacheprovider -rs` (with and without `PGHOST`/`PGUSER`/`PGDATABASE`), `python -m pytest ... kognit_smoke_test.py`, `npm ci && npm run test:jsdom` in `tests_frontend`, `python -m compileall`, `node --check static/js/*.js`, `git diff --check`, `pip check`, `pip-audit -r requirements.txt` and `pip-audit -r <frozen set>`, plus ad-hoc probes of the real-server behaviour and the ledger loop on isolated local databases.

## 13. Founder actions, in order

1. Copy the delivered files into the repository; run the Windows commands above; review the diff; commit if satisfied (your decision).
2. Follow `docs/PRE_PHASE10_LIVE_VERIFICATION_CHECKLIST.md` steps 1-5 (state check, decide the Notes limit, apply 0011 then 0012, configure the usage writer, correlate one chat event).
3. Complete steps 6-8 (Auth settings, live Gemini/PDF/image tests, log inspection). Tell me which results to turn into code (for example the CAPTCHA widget).
4. Decide whether to secret-gate the quota RPCs later (F-07) and keep the app on one worker (F-04).
5. Before inviting beta users, record the sign-off table; then plan launch. Phase 10 waits for your own entry gate (checklist step 9).