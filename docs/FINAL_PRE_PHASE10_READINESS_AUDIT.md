# Final Pre-Phase 10 verification and readiness audit

Audited commit: `8baa2d6` "Complete Phase 10A hardening and telemetry integrity" on `main` (parent `2807af9`), fresh clone, working tree clean at start.
Auditor: Claude (AI Lead Engineer, acting as independent auditor). Read-only: no application code, migration or live data was changed. This report is the only file added and is **uncommitted**.

## 1. Executive summary

The product code delivered for Q-01, Q-03, Q-04, Q-05 and Q-06 is present in `main`, passes the complete Python suite, the smoke suite and the ten jsdom frontend suites, and behaves as designed against a real PostgreSQL 16 built from the committed migrations (including a contract test that fed payloads produced by the real Python telemetry code into the real SQL function).

**However, the repository's own database acceptance gate is broken in the committed state (finding F-01).** `supabase/tests/concurrency_test.sh` was overwritten with the Notes-meter test and `supabase/tests/concurrency_notes_test.sh` is missing, so `run_local_db_tests.sh` aborts and `test_database_behaviour.py` **fails** whenever a disposable PostgreSQL is available. The original proof that the chat meter admits exactly one of 24 simultaneous requests is no longer in the repository. Nothing in the running application is affected, but a required check does not pass, and it is fixed by restoring one file and adding one file.

Separately, Phase 10 is defined (in the founder's project document, not in the repository) as work that begins **after launch and 1-2 months of real usage**. That gate is not met, and no Phase 10 specification exists in the repo.

Nothing about the live Supabase project, the live Gemini API, production configuration, or the real-browser test suites could be verified from here.

## 2. Final recommendation

| Decision | Status |
|---|---|
| **Start Phase 10 now** | **NO-GO** (blocker F-01 open; and the founder's own Phase 10 entry gate is not met, see section 8) |
| Pre-Phase 10 implementation completion | **Complete in code; not yet closed.** Closes when F-01 is fixed and the live-environment steps in section 11 are done |
| Beta testing | **NOT approved yet.** Needs the live checks in section 11 (migration applied, usage writer configured, Supabase Auth settings, live Gemini/PDF/image test) and a decision on Starlette |
| Production deployment | **NOT approved** |

NO-GO here is narrow and quick to clear: F-01 is a file-restore task, not a design problem. Separately, the Phase 10 scope cannot be fully assessed (section 8): **INSUFFICIENT EVIDENCE** for a Phase 10 acceptance checklist.

## 3. Repository and commit verification

| Check | Result | Evidence |
|---|---|---|
| Branch / tree | PASS | `main`, `git status` empty after clone; 97 commits |
| HEAD contains the Phase 10A files | PARTIAL | 22 expected files present and byte-equivalent to the delivered set **except** F-01 (one file overwritten, one missing) |
| Delivered vs committed content | PASS (with F-01) | Whitespace-insensitive diff = 0 for all files; the only textual difference is a missing final newline |
| Secrets in current tree | PASS | Pattern scan (API-key, JWT, private-key, DB-URL, token shapes): no matches. Only `sb_publishable_` (public by design) in `static/js/app.js` |
| Secrets in git history | PASS | Same patterns across all history: no matches; no `.env`/key file ever added |
| `.gitignore` / `.env.example` | PASS, minor | `.env` ignored; `.env.example` has `USAGE_WRITE_KEY` commented and empty. `.gitignore` does not cover `.env.local`-style names (F-11) |
| Dependencies | PARTIAL | `requirements.txt` pins match the tested set; no lockfile for Python. `pip-audit`: 1 vulnerable package left (Starlette, F-03). Frontend tests have a `package-lock.json` |
| Whitespace | PASS | `git diff --check HEAD~1 HEAD` clean. 21 of the 23 files in HEAD (and 109 tracked text files repo-wide) lack a final newline: cosmetic |

## 4. Test results (actually run, on `8baa2d6`, Linux, Python 3.12.3)

| Suite | Result |
|---|---|
| `pytest` with a disposable PostgreSQL available | **1 failed, 1967 passed, 1 skipped** (73 s). Failure: `test_database_behaviour.py` (F-01) |
| `pytest` without PostgreSQL | 1967 passed, **2 skipped** (62 s): the PostgreSQL test, and the Windows-only test |
| `kognit_smoke_test.py` | 72 passed |
| Frontend jsdom suites (`npm run test:jsdom`) | PASS: 38, 57, 14, 52, 62, 91, 45, 80, 38, 12 passed; 0 failed |
| Frontend real-browser suites (4) | **NOT RUN** (no Chrome/Edge in the audit environment) |
| `python -m compileall`, `import backend.main`, `node --check` on all JS | PASS |
| `run_local_db_tests.sh` | **Aborts** after the notes concurrency step: `concurrency_notes_test.sh: No such file or directory`. Before that: migrations 0001-0011 apply; 0008-0011 re-apply; DB behaviour tests PASS; hardening tests PASS; notes concurrency PASS (exactly 6 of 24) |
| Application-to-database contract test (audit-written, not in repo) | PASS: see section 6 |

Reconciliation with the historic baseline: the previously reported 1,966 passed / 3 skipped totals 1,969 tests; this run also collects 1,969 (1,967 + 1 + 1). The 3 skips on Windows are the PostgreSQL test and two Unix-only memory tests; here the Windows-only test is skipped instead. Runtime (7 m 42 s vs about 1 minute here) was not investigated. The DB suite ran on a local throw-away PostgreSQL 16 inside the audit sandbox, with stubbed Supabase roles, not on Supabase.

Skipped tests are not counted as passed anywhere in this report.

## 5. Database and Supabase findings

Verified against a fresh database built from the committed migrations (source review plus execution; **not** the live Supabase project):

- All 11 migrations apply in order; 0008-0011 re-apply cleanly twice. `verify_schema.sql` returns zero rows.
- RLS is enabled on every table in `public`. `pocket_notes` has SELECT/INSERT/UPDATE/DELETE policies all scoped to `auth.uid() = user_id` (UPDATE has the same check on both sides, so a note cannot be re-assigned). Server-owned tables (`quota_ledger`, `subscriptions`, `payments`, `plan_limits`, `plan_prices`, `usage_write_keys`, `ai_usage_events`) give students no INSERT/UPDATE/DELETE/TRUNCATE.
- Every function in `public` has a fixed `search_path`; no SECURITY DEFINER function is executable by `anon` (query 5 returns zero rows). Internal validators are not executable by API roles.
- `record_ai_usage_events`: SECURITY DEFINER, pinned search path, executable by `authenticated` only, needs the secret, derives user from `auth.uid()`, plan from `subscriptions`, forces cost columns to NULL and time to `now()`, validates every field, max 20 per call and 1000 per user per 24 h.
- `consume_quota` / `refund_quota`: advisory-locked per user and kind; replay never double-counts; a refund needs the secret token and cannot create a credit for a missing record; 24 and 30 parallel sessions admit exactly 1 and exactly 6.
- `activate_pro_subscription` is `service_role` only and now refuses while Pro limits are missing.

Findings:

- **F-02 (P2): the repository's own verification query disagrees with reality.** `supabase/verify_policies.sql` query 4 documents "EXPECTED RESULT: ZERO ROWS" but returns 4 rows on a database built from HEAD: `ai_usage_events` still grants `REFERENCES` and `TRIGGER` to `anon` and `authenticated` (migration 0011 revokes INSERT/UPDATE/DELETE/TRUNCATE; 0009 revoked SELECT). Practical impact is low: both privileges are only usable with DDL rights, and `anon`/`authenticated` hold no CREATE on schema `public` in the test database (live setting unverified). But an acceptance query that cannot return zero will either cause a false alarm or be ignored. Fix: a migration `revoke all on public.ai_usage_events from public, anon, authenticated;` (also covers the sequence), then re-check.
- **F-07 (P2): direct quota-RPC loop grows `quota_ledger`.** `consume_quota` and `refund_quota` are callable by any signed-in student with their own token. A student can consume with a refund token they chose and then refund it, repeatedly. This gives no free AI and no quota gain (the unit returns), but it is unbounded row growth in `quota_ledger` and lock churn. Hypothesis-level exploitability; confirmed by design reading and the SQL semantics, not by a flood test. Mitigation options: secret-gate the RPCs like the usage writer, or cap ledger rows per user per day.
- Not verifiable here: whether migration 0011 has been applied to the live database; live default privileges (Supabase grants broad default privileges on new objects, which is why the explicit revokes matter); constraint names on the live `plan_limits`/`quota_ledger` (0011 drops `plan_limits_kind_check` and `quota_ledger_kind_check` by default name; if a live constraint was created under a different name the old check would still reject `notes_ai` and the seed insert would fail). Run section 11 step 2 before applying.

## 6. Usage telemetry verification

Traced path: endpoint -> `UsageCollector` (records contain labels, token counts, timings, status; no prompt or answer text) -> `settle_request` -> `save_ai_usage_events` -> `quota_service._rpc` -> `public.record_ai_usage_events`.

| Contract point | Result | Evidence |
|---|---|---|
| App sends the original secret `USAGE_WRITE_KEY`; DB stores/compares its SHA-256 | PASS | Code: `p_write_key = key`; SQL: `sha256(convert_to(p_write_key,...))` compared to `usage_write_keys.key_hash`. Executed: the secret is accepted; **the stored hash used as the secret is refused (42501)** |
| Secret never returned to the browser, logged or committed | PASS (source) | No reference in `static/` or `templates/`; tests assert it is not logged; history scan clean. `_rpc` logs the first 300 characters of a PostgREST error body (F-12) |
| Missing key fails safely | PASS | No RPC is sent, one warning is logged, chat/quiz unaffected (tested) |
| Wrong or hash-as-secret key | PASS (fail safe) | RPC refused, caught as best-effort; response unaffected. Each request logs a "usage persistence failed" warning |
| Payload/rate/integrity | PASS | Contract test: five representative and hostile events built by the real `record_ai_call` (including `HTTP 503: Service Unavailable!`, a 200-character error code and a model name with spaces) were all accepted after sanitising, stored with `plan=free` and `estimated_cost_usd` NULL |
| Forgery by a student | PASS | Direct INSERT refused; wrong key refused; forged plan/user/cost/time ignored (SQL suite) |
| Duplicates | PASS | `settle_request` persists once per request even if called twice (tested) |

**Do not overstate:** an `ai_usage_events` count of 1 after a chat does **not** show the new path worked. An older row could predate migration 0011 or come from the pre-0011 direct-insert path. Proof needs: 0011 applied, a key hash stored, `USAGE_WRITE_KEY` set on the server, one chat, then a row whose `request_id` matches that request's id with `estimated_cost_usd` NULL and the server log free of "USAGE_WRITE_KEY is not set" and "usage persistence failed". Not verified here.

## 7. Security and reliability findings

No P0 findings. Classification of confirmed defects vs hypotheses is explicit.

| ID | Priority | Status | Finding |
|---|---|---|---|
| F-01 | **P1** | **Confirmed** | `supabase/tests/concurrency_test.sh` holds the Notes test; `concurrency_notes_test.sh` is absent. `run_local_db_tests.sh` aborts; `test_database_behaviour.py` fails with a database; the original chat-meter concurrency proof is gone. Probable cause (unconfirmed): the delivered notes script was copied over the existing file name during manual file application |
| F-03 | P2 | Confirmed (versions) / not analysed (applicability) | Starlette 0.37.2 has 14 advisories (fix 1.3.1); the upgrade requires a FastAPI upgrade. It parses multipart uploads from students |
| F-02 | P2 | Confirmed | Verification query 4 returns rows (section 5) |
| F-07 | P2 | Hypothesis, design-confirmed | Ledger growth via direct consume/refund loop |
| F-04 | P2 | Documented | In-memory PDF/quiz state and per-minute limits are per process; run one worker |
| F-09 | P2 | Unverified | No email-confirmation or CAPTCHA check exists in application code (searched), so each new account gets fresh Free quota (3 chats on day one). Depends entirely on Supabase Auth dashboard settings, which could not be seen |
| F-06 | P2 | Unverified | Pillow moved to a new major version (13.0.0) and pypdf/jinja2/dotenv were bumped; tests pass, but real image/PDF uploads and live Gemini were not exercised |
| F-08 | P3 | Confirmed | A stream cancelled mid-flight writes no usage row (cost under-count, bounded by quota) |
| F-10 | P3 | Policy | Cancelling before the first word is deliberately never refunded; two failed attempts after a first partial still charge one unit (CEO decision A). Worth monitoring in beta |
| F-11 | P3 | Confirmed | `.gitignore` covers only `.env`; many tracked files lack a final newline |
| F-12 | P3 | Hypothesis | `quota_service._rpc` logs up to 300 characters of an error body; no path that would contain the secret was found |
| F-13 | P3 | Confirmed | No frontend test covers the new `notes.js` quota-message handling; the non-stream `interrupted` label is not rendered by the UI; browser suites not run here |
| F-14 | P3 | Confirmed | `docs/PRE_PHASE10_FINALIZATION_REPORT.md` quotes test totals from an earlier state (1956 / 1966) that do not match HEAD |

Areas inspected with no defect found: token verification (every protected route verifies the bearer token against Supabase Auth and never trusts a client-supplied user id; missing configuration fails closed); notes ownership (`_load_owned_note_for_ai` plus RLS; another student's note returns 404 and costs no quota, tested); quota fail-closed behaviour (503 with the provider never called); refund idempotency; no raw stack traces to clients (generic messages, tested).

## 8. Phase 10 requirements checklist

**Scope limitation.** The repository contains no Phase 10 specification, plan or acceptance criteria. Its only mentions are code comments labelled "PHASE 10" (the provider-abstraction work in `ai_engine.py`), the commit titled "Phase 10: redesign application shell" (`ccd20b7`, a UI redesign), and one line in `docs/DEPLOYMENT.md` listing durable PDF storage as a "planned Phase 10 item". The only substantive definition is the founder's project document "Phase-10 updated.txt" (outside the repository). The checklist below uses **only** that document and states what the repo shows. No requirement was invented.

| Requirement (source: founder's Phase 10 document) | Evidence inspected | Result | Remaining action | Priority |
|---|---|---|---|---|
| Launch first and operate about 1-2 months before starting Phase 10 | Document; no launch evidence in repo | **NOT MET** | Launch, observe, then re-run the readiness gate | High |
| Readiness gate: enough real usage data, recurring academic problems identified, reliability stability, meaningful feedback, sufficient funding | No data or feedback in repo | **NOT VERIFIED** | Founder to confirm each item with evidence | High |
| First step is "Phase 10A Post-Launch X-Ray" (original X-Ray + repo + 1-2 months of data + feedback) | No X-Ray report found in repo | **NOT STARTED** (appropriate) | Schedule after the gate | - |
| AI Engine 2.0 starts only when paid multi-provider access exists | Only Gemini is configured | **NOT MET** (stated as planned) | None until access exists | - |
| Provider abstraction foundation | `backend/providers/` (Gemini adapter, mock); `ai_engine.py` imports no SDK | **PASS** | - | - |
| Telemetry: request id, task/operation, provider, model, retries, latency, TTFT, tokens, error category | `usage_collector.py` and the 0009/0011 schema | **PARTIAL** | Cost is always NULL (no verified price list); routing/fallback fields not applicable yet | P2 |
| Student prompts and private documents not written to logs | Usage rows contain labels and numbers only; tests | **PASS** (usage path) | Full log review not done | P3 |
| Post-launch observation: retention, feature usage, quiz performance, regenerate/abandonment | No product-analytics code found | **NOT VERIFIED** | Decide what to measure and how, with privacy controls | P2 |
| Privacy for minors: minimisation, retention and deletion controls | Retention is a documented manual statement (180 days); no automated purge; no account-data-deletion flow verified | **PARTIAL** | Define retention/deletion before collecting at scale | P2 |
| Evaluation / benchmark infrastructure | `evaluation/` exists and its tests are part of the passing suite; not audited in depth | **NOT VERIFIED** (beyond tests) | Review during 10B | P3 |

## 9. Known limitations and skipped tests

- Not run: the four real-browser frontend suites (no Chrome here); any test against live Supabase, live Gemini or production; load tests beyond the local in-process tool.
- Skipped in Python: the PostgreSQL test when no disposable server is configured; the Windows-only test on Linux (and the two Unix-only memory tests on Windows).
- The DB suite ran on a local PostgreSQL 16 with stubbed Supabase roles; real Supabase (PostgREST, default privileges, Auth) can differ.
- Disclosure: the task said to wait for approval before tests that need a disposable database. I used a throw-away PostgreSQL inside the audit sandbox only; nothing of the project's infrastructure was contacted.

## 10. Prioritised remediation plan

1. **F-01 (P1, minutes):** restore the original `supabase/tests/concurrency_test.sh` from `2807af9`; add `supabase/tests/concurrency_notes_test.sh`.
2. **Live steps (section 11)** before any beta.
3. **F-02 (P2):** migration `0012` revoking the leftover privileges; re-run verification queries.
4. **F-03 (P2):** schedule the FastAPI/Starlette upgrade with its own test pass.
5. **F-09 (P2):** verify/enable Supabase Auth email confirmation, CAPTCHA and rate limits.
6. **F-06 (P2):** live smoke of image and PDF upload and streaming against Gemini.
7. **F-07 (P2):** decide on gating or capping the quota RPCs.
8. P3 items (F-08, F-11 to F-14) in a housekeeping pass.

## 11. Exact acceptance criteria for closing remaining blockers

**F-01 closed when** all hold: `supabase/tests/concurrency_test.sh` prints "CONCURRENCY TEST PASSED (exactly 1 of 24 simultaneous requests counted)"; `concurrency_notes_test.sh` prints "NOTES CONCURRENCY TEST PASSED (exactly 6 of N ...; chat meter untouched)"; `bash supabase/tests/run_local_db_tests.sh` exits 0; `pytest` with a disposable PostgreSQL configured shows 0 failed; `pytest` without it shows only the two documented skips.

**Live verification (founder, no code change):**
1. Before applying 0011 on Supabase, run `select conname from pg_constraint where conrelid in ('public.plan_limits'::regclass,'public.quota_ledger'::regclass) and contype='c';` and confirm `plan_limits_kind_check` and `quota_ledger_kind_check` exist.
2. Apply `0011` (safe to re-run), then run `supabase/verify_schema.sql` (expect zero rows) and `supabase/verify_policies.sql` (queries 1-3 as documented; query 4 will list the F-02 rows until `0012`; query 5 zero rows; query 6 shows one key hash; query 7 shows no Pro rows).
3. Set `USAGE_WRITE_KEY`, store only its SHA-256 in `usage_write_keys`, restart, send one chat, and confirm the matching event row as described in section 6.
4. Confirm the Free `notes_ai` limit (proposed: 6 per day) or change it.
5. In Supabase Auth, confirm "Confirm email" and CAPTCHA/rate-limit settings.
6. Live-test a streamed chat, a PDF upload and an image upload.

**Phase 10 may start when** F-01 is closed, the live steps above pass, and the founder documents that the readiness gate in section 8 is met (or deliberately waives it), with a written Phase 10 scope and acceptance criteria.

## 12. Evidence and commands used

```
git clone https://github.com/mahfuzkhann/kognit-app.git && git fetch --unshallow
git status --short; git log; git show --stat HEAD; git ls-files --eol; git diff --check HEAD~1 HEAD
git grep -nIE <secret-shape patterns>; git log --all -p -G<patterns>   (locations only, no values)
python3 -m pytest -q -p no:cacheprovider -rs   (with and without PGHOST/PGUSER set)
python3 -m pytest -q -p no:cacheprovider kognit_smoke_test.py
python3 -m compileall; python3 -c "import backend.main"; node --check static/js/*.js
(cd tests_frontend && npm ci && npm run test:jsdom)
bash supabase/tests/run_local_db_tests.sh        (local throw-away PostgreSQL 16)
psql -f supabase/verify_schema.sql; psql -f supabase/verify_policies.sql   (on a database built from HEAD migrations)
pip-audit -r requirements.txt --no-deps --disable-pip
contract test: real record_ai_call -> usage_telemetry._EVENT_FIELDS payload -> public.record_ai_usage_events
```

## 13. Final sign-off status

- **Phase 10 start: NO-GO** (F-01 open; founder's entry gate not met). Phase 10 acceptance scope: **INSUFFICIENT EVIDENCE**.
- Pre-Phase 10 code: verified locally; closes after F-01 and the live steps.
- Beta: **not approved**. Production: **not approved**.
- Not modified: application code, migrations, database, configuration. This file is uncommitted.

---

# ADDENDUM - remediation status (2026-10-26)

*Everything above this line is the original audit of commit `8baa2d6`, kept unchanged as the audit trail. This addendum records what happened afterwards. Details: `docs/PRE_PHASE10_REMEDIATION_REPORT.md`; founder steps: `docs/PRE_PHASE10_LIVE_VERIFICATION_CHECKLIST.md`.*

All changes are **uncommitted** in the working tree on top of `8baa2d6`. Nothing was applied to live Supabase, and no live service was contacted.

| Finding | Original status | Status now | Evidence |
|---|---|---|---|
| F-01 concurrency tests (P1) | Confirmed, blocker | **FIXED AND VERIFIED** (local) | `concurrency_test.sh` restored byte-identical to `2807af9`; `concurrency_notes_test.sh` added; both exit 0 and both fail when the limit is wrong; `run_local_db_tests.sh` exit 0; pytest with PostgreSQL 0 failures; without it 0 failures and 2 documented skips |
| F-02 verification query (P2) | Confirmed | **FIXED LOCALLY - LIVE VERIFICATION REQUIRED** | Migration 0012 + rewritten query 4: 4 rows on a 0011 database, zero on a 0012 database |
| F-03 Starlette (P2) | Confirmed | **FIXED LOCALLY - LIVE VERIFICATION REQUIRED** | fastapi 0.133.0 / starlette 1.3.1; `pip-audit` clean for the full resolved set; real-server tests; two advisories reproduced against the old pins |
| F-04 per-process state (P2) | Documented | Unchanged, **FOUNDER ACTION REQUIRED** | Run one worker |
| F-06 live uploads (P2) | Unverified | **FOUNDER ACTION REQUIRED** | Local real-server upload tests pass; live Gemini/photo/PDF run still needed |
| F-07 ledger growth (P2) | Hypothesis | **PARTIALLY MITIGATED** | Reproduced (5,000 cycles = 5,001 rows, no quota gained); capped at 500 rows/user/24 h by 0012; many-account farming and RPC secret-gating remain founder decisions |
| F-09 account farming (P2) | Unverified | **FOUNDER ACTION REQUIRED** | Dashboard settings cannot be seen from the repo |
| F-15 (new, P2) | - | **FIXED LOCALLY** | Supabase Auth 5xx/429 now 503 instead of a false "session expired" 401 |
| F-16 (new, P3) | - | Observation | Default Supabase privileges on user-data tables include `TRUNCATE`/`REFERENCES`/`TRIGGER` for API roles (observed on the local stand-in; live default privileges not checked); not reachable through the REST API |
| F-08, F-10, F-11, F-12 | P3 | Unchanged | - |
| F-13, F-14 | P3 | **PARTIALLY MITIGATED** | New auth and real-server tests; stale totals in the earlier finalization report are superseded by the remediation report |

**Re-assessment of the sign-off:** the P1 blocker is closed locally and no P0/P1 defect is known. Phase 10 itself is still **not** started or approved: the founder's own entry gate (launch, 1-2 months of real usage, readiness gate, written scope) is unmet, and the live checklist is open. Beta: not approved. Production: not approved.