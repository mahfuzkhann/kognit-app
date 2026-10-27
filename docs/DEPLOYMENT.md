# Deployment, configuration and operations

**What this document is.** The repository contains no deployment artifacts (no Dockerfile,
Procfile, CI or hosting config), so the hosting provider, worker count, proxy and timeouts are
**unknown to the code** and are not assumed here. Everything below is what the *application*
requires, what it does under restart or multiple workers, and how to roll changes back.

## 1. Required configuration

Set these as server-side environment variables (never in the browser, HTML, git or chat):

| Variable | Required | Notes |
|---|---|---|
| `GEMINI_API_KEY` | yes | server-side only |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY` | yes | the anon/publishable key is public by design; protection comes from RLS |
| `GEMINI_MODEL` | no | defaults to the built-in model id |
| `QUOTA_MODE` | no | `enforce` (default) or `off` |
| `USAGE_WRITE_KEY` | for usage telemetry | server-side secret that lets the backend write usage rows through `record_ai_usage_events()` (migration 0011). Without it chat/quiz/quotas work, but usage rows are **not recorded** (one warning is logged). Setup in section 2a |
| `PDF_CONTEXT_*`, `QUIZ_DEFINITION_*` | no | in-memory limits, see section 4 |
| `THREADPOOL_MAX_THREADS` | no | ceiling on simultaneous streamed answers per process, see section 5 |

No service-role key exists anywhere in the application, and none should be added to the request
path. Check readiness (booleans only, never values): `GET /readyz` (503 when Supabase/Gemini are not
configured). Liveness: `GET /healthz`.

## 2. Database migrations

Apply in numeric order in the Supabase SQL editor. Run each file once, except `0002, 0004, 0006,
0008, 0009, 0010, 0011, 0012`, which are safe to re-run.

1. `0001`–`0007` — existing schema.
2. `0008_plans_quota_subscriptions.sql` — plans, limits, prices, payments, subscriptions, quota
   ledger, and the `consume_quota` / `refund_quota` / `activate_pro_subscription` functions.
3. `0009_ai_usage_events.sql` — usage telemetry table.
4. `0010_user_projects_baseline.sql` — makes `user_projects` reproducible (additive; no-op on a
   database where the table already exists).
5. `0011_quota_notes_meter_and_telemetry_integrity.sql` — adds the separate `notes_ai` quota meter
   (chat titles + Pocket Notes AI), **closes the student-writable `ai_usage_events` hole**, adds the
   Pro-limits guard on `activate_pro_subscription`, and fixes function search paths. Deletes nothing.
6. `0012_usage_privileges_and_ledger_cap.sql` — revokes the last unneeded privileges (`REFERENCES`,
   `TRIGGER`) that API roles held on `ai_usage_events`, so `supabase/verify_policies.sql` query 4 returns
   zero rows, and caps `quota_ledger` growth per user (500 rows per rolling 24 h, refunded rows included)
   so a signed-in student cannot grow the ledger without bound by looping the quota functions. Adds an
   index. Deletes nothing; the backend works with or without it. **If you ever set Pro daily limits,
   keep a Pro student's legitimate rows per day well under 500, or raise the cap in the function first.**

After applying, run `supabase/verify_schema.sql` (expect **zero rows**) and
`supabase/verify_policies.sql` (query 1 must return **zero rows**; review any policy it lists — a
hand-made policy such as `using (true)` on `user_projects` would still be in force and must be
removed deliberately).

**Deploy order matters:** apply `0008`–`0012` **before** deploying code running with
`QUOTA_MODE=enforce`. If the migration is missing, chat and quiz requests fail closed with a safe
503 (`QUOTA_UNAVAILABLE`) rather than running unmetered.

**Pro limits are not seeded** (the CEO has not fixed them). Until you insert them, a Pro subscriber
is refused with `PLAN_NOT_CONFIGURED`, and since 0011 `activate_pro_subscription()` itself refuses to
record a payment until all three exist, so nobody can pay for a plan that would then be denied.
Insert when decided:

```sql
insert into public.plan_limits (plan, kind, period, limit_count)
values ('pro','chat','day', <N>), ('pro','quiz','day', <M>), ('pro','notes_ai','day', <K>);
```

### 2a. One-time setup of the trusted usage writer (after applying 0011)

Students can no longer write `ai_usage_events`. The backend writes through
`public.record_ai_usage_events()`, which needs a server secret. Do this once per environment:

1. Generate a long random value locally (e.g. `python3 -c "import secrets;print(secrets.token_urlsafe(48))"`)
   and set it as `USAGE_WRITE_KEY` in the **server** environment (not in git, chat or the browser).
2. Compute its SHA-256 locally and store **only the hash** in the database (the key itself is never
   typed into the SQL editor):
   ```
   python3 -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" '<your key>'
   ```
   ```sql
   insert into public.usage_write_keys (key_hash) values ('<the 64-character hash>');
   ```
3. Restart the server. Rotation: insert the new hash, switch the env var, delete the old hash.

It is **not** a service-role key. It only lets a request that already carries a valid student token add
usage rows for *that* student; the database derives user, plan and time itself, forces cost columns to
NULL, validates every field, and accepts at most 1000 rows per user per 24 h (a tunable hypothesis).
Rows written before 0011 may have been forged by a student: run query 99 in `docs/USAGE_REPORTS.sql`
and decide deliberately; nothing is deleted for you.

**Configuring the Notes/title limit.** Limits are data, not code. The 0011 seed is a **proposal awaiting
CEO approval**: Free = 6 AI note/title operations per Dhaka day. Change it with
`update public.plan_limits set limit_count = <N> where plan='free' and kind='notes_ai';` (no deploy
needed). If a (plan, kind) row is missing, the API answers 403 `PLAN_NOT_CONFIGURED` and the provider is
never called.

**Rollback.** Code rollback = redeploy the previous commit; set `QUOTA_MODE=off` first if the new
tables are not yet present. Schema rollback: the new objects are additive and unused by old code, so
leaving them in place is safe. Dropping them destroys ledger/subscription/payment history — export
first. There is no automated down-migration.

## 3. Plans and quotas (current policy)

* **Free:** 3 chats on the first (Asia/Dhaka) day the account exists, then 1 chat per day; 3 quizzes
  per week. Days reset at midnight Dhaka time, weeks on Monday 00:00 Dhaka.
* **Pro:** ৳299 monthly / ৳3,199 yearly, manual renewal (no auto-renew). Limits: **not yet decided**.
* A "chat" is one request to `/api/chat` or `/api/chat/stream`; a "quiz" is one `/api/quiz/generate`.
  Counting requests (not chat ids) means opening new chats cannot bypass a limit.
* A request is counted when accepted, before the provider is called. A request rejected for quota
  is not counted. If generation then fails and the student received nothing usable, the unit is
  given back (only the server holds the refund token). Re-sending after a failure is a new request.
  Pressing Stop on a delivered answer is **not** a refund.
* **Refund policy (CEO decision A).** Once even one piece of generated text has been delivered, the
  request is final and is **never** refunded - not for a later provider failure, `MAX_TOKENS`, a failed
  recovery, a timeout or a client disconnect. If no text was delivered, a refund is allowed. A student
  who closes the tab or presses Stop is never refunded (even before the first word: otherwise anyone
  could cancel for free credits). Consequence worth knowing: if Gemini streams a few words, fails, and
  the one bounded recovery attempt also fails, the student keeps the error and is still charged one unit.
* If the server fails *after* consuming a unit but before any text exists (profile lookup, quiz
  generation, an unexpected exception), the unit is returned immediately - not via a background task that
  FastAPI would skip when an exception escapes.
* **Notes/title AI is a separate meter** (`notes_ai`, Free: 6 per Dhaka day - proposed): chat titles and
  Pocket Notes topic suggestions, flashcards and mini-quizzes. Opening stored study material and
  ordinary note create/read/edit/delete are free. The existing per-minute limiter remains as an extra
  layer only.

**Payments.** No payment integration exists. `backend/payments.py` and `activate_pro_subscription()`
define the rules (provider-verified, right user, BDT, exact price, idempotent, service-role-only
activation). A real bKash Merchant verifier must be written against bKash's published API with
credentials the founder supplies, plus a small privileged component that calls the activation
function — kept out of the request path.

## 4. In-memory state and restarts

These live in process memory and are **lost on restart and not shared between workers**:

| State | Bound (default; assumption, tune from measurements) |
|---|---|
| PDF text per chat | idle TTL 2 h; 5 documents per student; 1.5 M characters per student; 40 M characters per process (about 80 MB of Bangla text) |
| Pending quiz definitions | TTL 2 h; 10 per student; 5,000 per process |
| Per-minute rate-limit counters | sliding windows, per process |

Behaviour when state is gone: the browser sends `pdf_expected=1` for chats showing a PDF, and the
server answers `409 PDF_CONTEXT_EXPIRED` (before any quota is spent) so the app tells the student to
re-upload; a submit for an expired/lost quiz returns the existing "quiz not found" response.

**Multiple workers.** Run **one worker** unless you accept that a student's second request may land
on a worker that does not hold their PDF/quiz, and that per-minute limits multiply by the worker
count. Quotas, subscriptions and usage records are in the database and are correct across workers.
Sticky sessions would mitigate the in-memory state; moving PDF context to durable storage is a
planned Phase 10 item, not done.

## 5. Capacity: what is and is not known

Measured locally with `tools/local_load_test.py` (in-process, mock provider that blocks 0.5 s per
answer, no external services; **a floor for framework overhead, not a capacity claim**):

| Simultaneous requests | Throughput (default pool) | Throughput with `THREADPOOL_MAX_THREADS=200` |
|---|---|---|
| 250 | ~68 req/s, p95 ≈ 4.0 s | ~196 req/s, p95 ≈ 1.6 s |
| 500 | ~70 req/s, p95 ≈ 7.1 s | ~256 req/s, p95 ≈ 2.8 s |

Throughput plateaus near (thread-pool size ÷ generation time). With the default 40 threads and real
generations of several seconds, one process completes only a handful of answers per second;
beyond that, requests queue. **Unmeasured:** real Gemini latency and quota, Supabase latency (each
request makes at least one Auth call), multi-process behaviour, proxy timeouts. Registered users,
daily-active users, concurrent open streams and provider quota are different numbers; none of them
has been load-tested against real infrastructure. Do that in staging, with an approved provider
budget, before quoting any user capacity.

## 6. Credential hygiene (exposed-key remediation)

A project-knowledge file named `API` held a key-shaped string. Its live status is unknown. In order:

1. In Google AI Studio / Cloud Console, **revoke** that key and create a replacement.
2. Set the replacement only as `GEMINI_API_KEY` in the server environment (and the local `.env`).
3. Delete the `API` file from the project knowledge and any chat attachments.
4. Review the old key's usage in the Google console for the exposure period.

Deleting the file does not revoke the key. Never paste keys into chats or documents.

## 7. Operating checklist for a release

`pytest -q` · `python -m py_compile backend/*.py` · `node --check static/js/*.js` ·
`git diff --check` · (staging) apply migrations, `verify_schema.sql`, `verify_policies.sql`,
`/readyz`, a signed-in chat, a quiz, a PDF upload, and a deliberate over-quota request.

## 8. Data and retention (founder / legal decisions — not settled by code)

`ai_usage_events` stores no prompt, document or answer text, but it does link a student id to usage
over time. A retention period, deletion-on-request procedure, and any parental-consent requirement
for minors are open decisions; this repository makes no legal-compliance claim.

## Python dependencies (changed in the pre-Phase 10 remediation)

`requirements.txt` now pins **fastapi 0.133.0 / starlette 1.3.1** (previously 0.110.1 / 0.37.2),
jinja2 3.1.6, Pillow 13.0.0, pypdf 6.21.2 and python-dotenv 1.2.2. Why: `pip-audit` reported 7 distinct
Starlette advisories (14 database entries) on 0.37.2, and two of them were reproduced against this app (a 2 MB text form field and a
2,500-field form were accepted instead of refused: CVE-2024-47874, CVE-2026-54283). FastAPI 0.133.0 is the
oldest release whose declared Starlette range includes a fixed version; `pip check` is clean and `pip-audit`
reports no known vulnerabilities for the full resolved set. There is no Python lockfile, so transitive
packages (pydantic, anyio, ...) float: re-run `pip-audit` before each deploy. Install with
`python -m pip install -r requirements.txt` in a **fresh virtual environment**, then run the test suite.
`test_real_server_uploads.py` runs the app under a real uvicorn server and pins the upload and form limits.
Live Gemini behaviour after the upgrade is not verified: see `docs/PRE_PHASE10_LIVE_VERIFICATION_CHECKLIST.md`.