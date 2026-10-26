# Pre-Phase 10 finalization - implementation report

Baseline: `main` at `2807af9`. Working tree changes only; **nothing committed, pushed, merged or deployed.**

**Important:** `docs/QUOTA_SECURITY_AUDIT_REPORT.md` is **not in the repository** (no file of that name in the tree
or in git history) and was not attached. Q-01/03/04/05/06 were implemented from the task description and
confirmed by reproducing each issue on the real code; Q-02 is not described anywhere I could read and was not
assessed; Q-07..Q-15 were re-derived by inspecting the code, so their numbering follows the task's titles.

## Verified results (what was actually run)
| Check | Result |
|---|---|
| Baseline `pytest` (before changes) | 1874 passed, 1 skipped |
| Baseline `kognit_smoke_test.py` | 72 passed |
| Final `pytest` (incl. real PostgreSQL 16 integration) | **1956 passed** |
| Final smoke | 72 passed |
| New Python tests | `test_quota_settlement.py` 35, `test_notes_title_quota.py` 30, `test_usage_telemetry_integrity.py` 16 |
| New tests vs ORIGINAL backend | 15/35 and 23/30 fail (they detect the bugs); the rest pass (unchanged behaviour) |
| Real PostgreSQL 16: migrations 0001-0011 apply, 0008-0011 re-apply twice | passed |
| SQL behaviour + hardening suites | passed |
| Concurrency: 24 parallel chat consumes (limit 1) / 30 parallel notes_ai (limit 6) | exactly 1 / exactly 6 allowed |
| `py_compile`, `node --check` (all JS), `git diff --check` | passed |
| Fresh clone of `origin/main` + only the delivered files | 1956 passed, smoke 72 passed |
| `pip-audit` | 72 advisories before; 4 packages upgraded; Starlette (14) remains |

NOT verified (no access): live Supabase, live Gemini, production, Supabase dashboard settings.

## Findings matrix
| ID | Status | Evidence / remaining risk / follow-up |
|---|---|---|
| Q-01 | FIXED | Reproduced: partial text then failure, and `MAX_TOKENS`, both refunded. `UsageCollector.text_delivered` is set immediately before the first non-empty delta is yielded; `refund_due()` returns False once set, above every other signal. Tests incl. real-ASGI disconnect. **Policy consequence:** two failed attempts after a first partial still charge the unit. |
| Q-02 | DEFERRED | Not described in anything provided; audit report absent. Owner: CEO to supply the report. |
| Q-03 | FIXED | Reproduced: quiz exception after consume = 500, no refund. Post-consume work now runs under `settle_on_exception`; exception is re-raised (not swallowed); refund is idempotent. Stream, quiz, notes, title covered. |
| Q-04 | FIXED (residual) | Lost consume response now triggers one compensating refund (safe no-op if nothing recorded; SQL cannot create a credit). Fail-closed kept. **Residual:** if the database is down for BOTH calls one unit can stay counted. |
| Q-05 | FIXED locally | Migration 0011 adds kind `notes_ai`; 4 routes metered before the provider call; separate from chat; direct/concurrent bypass tests; AST test fails if an AI route lacks a meter. **Limit 6/day Free is a proposal needing CEO approval.** |
| Q-06 | FIXED locally | Reproduced: student inserted a forged `pro` row + 50,000 rows. Direct INSERT revoked; writes only via `record_ai_usage_events` (server secret, derives user/plan/time, NULL cost, validation, 20 per call, 1000/user/24h). **Needs the one-time `USAGE_WRITE_KEY` setup.** Legacy rows may be forged: review with query 99. |
| Q-07 | VERIFIED | `payments.py` is not wired to any route; `activate_pro_subscription` is service_role only. Payment is NOT ready (no bKash verifier exists). |
| Q-08 | FIXED | A Pro user without limits is denied (fail closed); activation now refuses until chat, quiz and notes_ai Pro limits exist. Pro limits are still not configured. |
| Q-09 | VERIFIED + DEFERRED | `(user, kind, request_id)` unique; replay never double-counts; a refunded id cannot be reused; new meter uses 128-bit ids (chat keeps 48-bit, collision negligible). **Deferred:** a student can call consume/refund directly in a loop and grow `quota_ledger`; it gives no free AI, but is a storage-abuse path. Recommend HMAC-gating those RPCs. Owner: Claude, after approval. |
| Q-10 | FIXED / DEFERRED | Undecodable image on the stream now writes a record and is refunded. Cancelled streams write no usage row (cost under-count, bounded by quota). |
| Q-11 | FIXED | Non-stream `MAX_TOKENS` is labelled `interrupted` + `code`, recorded as interrupted. The frontend does not yet display the label on the non-stream fallback path. |
| Q-12 | DEFERRED (partly fixed) | jinja2 3.1.6, pypdf 6.21.2, python-dotenv 1.2.2, Pillow 13.0.0 pinned; full suite passes. Pillow is a major bump: smoke-test real image uploads. **Starlette 0.37.2 has 14 advisories; the fix needs a FastAPI upgrade - separate task, recommend before 18 Nov.** Per-advisory applicability was not analysed. |
| Q-13 | FIXED | Two trigger functions had no search_path and were callable by API roles; fixed. SQL test asserts every function in `public` has a fixed search_path and security-definer grants are minimal. Live grants: run `verify_policies.sql` queries 4-5. |
| Q-14 | UNVERIFIED | App code enforces no email confirmation or CAPTCHA; per-user quotas mean new accounts get fresh Free quota (3 chats on day one). Everything depends on Supabase dashboard settings I could not see: Confirm email, CAPTCHA, auth rate limits, disposable-email policy. |
| Q-15 | FIXED / residual | Refund and telemetry no longer depend only on background tasks (exception path settles immediately). Residual: a process crash mid-request loses the refund. |

## Readiness: READY WITH CONDITIONS
1. Apply 0011, set `USAGE_WRITE_KEY` (docs/DEPLOYMENT.md 2a), run `verify_schema.sql` + `verify_policies.sql`.
2. CEO approves or changes the `notes_ai` Free limit.
3. Decide on Starlette/FastAPI upgrade before public launch.
4. Verify Supabase Auth settings (Q-14) and live-test streaming, PDF upload and image upload against real Gemini after the dependency bumps.
5. Supply the audit report so Q-02 and the Q numbering can be reconciled.