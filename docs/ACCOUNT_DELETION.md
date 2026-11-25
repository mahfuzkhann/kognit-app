# Account deletion — inventory, behaviour and open legal questions

Status: implemented in `backend/account_deletion.py` + `POST /api/account/delete`.
**Not yet verified against a live Supabase project** (see "Staging check" below).
Nothing here is legal advice: items under "Open legal questions" need qualified counsel.

## What happens when a student deletes their account

1. The app asks the student to type `DELETE` and to have **signed in** within the last 10 minutes
   (otherwise the server answers `403 REAUTH_REQUIRED` and the app asks them to sign in again).
   "Signed in" is proven only by an interactive sign-in entry in the token's `amr` claim. A token
   refresh, the token's issue time (`iat`) and anything unreadable are **not** evidence: the check
   fails closed.
2. `POST /api/account/delete` with `{"confirm": "DELETE"}` and the student's normal access token.
   The account deleted is **always** the token's user. The body can not name anyone else.
3. The server calls Supabase's admin API once (`DELETE /auth/v1/admin/users/<id>`, hard delete).
   Every table below references `auth.users(id) ON DELETE CASCADE`, so PostgreSQL removes the
   student's rows **in the same transaction** as the auth user: all of it is gone, or none of it.
4. The server forgets what it still holds in memory for that student (uploaded PDF text, quiz
   answer keys, rate-limit counters).
5. The app signs out and clears the student's data stored in the browser (see "Browser data").

Retrying is safe. After success the old token is rejected by the normal login check (`401`),
which the app must treat as "already deleted"; a `404` from Supabase is also treated as success.

## Inventory — every place student data lives

### Database (all deleted by the cascade; proven by `supabase/tests/account_deletion_cascade_test.sql`)

| Table | What it holds | Removed by |
|---|---|---|
| `student_profiles` | name, class, stream | cascade from `auth.users` |
| `user_projects` | projects, chats, messages (JSON), images attached to messages | cascade |
| `pocket_notes` | saved notes, takeaways, flashcards, mini-quizzes | cascade |
| `quiz_attempts` | quiz topic, score | cascade |
| `quiz_answers` | per-question answers | cascade from `quiz_attempts` |
| `learning_evidence` | learning signals per topic | cascade |
| `conversation_index` | short chat labels per topic | cascade |
| `shared_chats` | public snapshots of chats the student shared | cascade (every share link stops working) |
| `content_reports` | reports the student filed about AI answers | cascade (policy from B02, unchanged) |
| `subscriptions` | plan, dates | cascade |
| `payments` | provider payment id, amount, status | cascade — **see legal question 1** |
| `quota_ledger` | usage counting for plan limits | cascade |
| `ai_usage_events` | per-request model/token/latency telemetry | cascade |

Reference data with no student rows (not touched): `plan_limits`, `plan_prices`, `usage_write_keys`.

A guard keeps this list true: `test_account_deletion.py` and the database test **fail** if any
future table with a `user_id` column is not tied to `auth.users` with `ON DELETE CASCADE`,
and fail if a table with a `user_id` column is missing from this document.

### Supabase Auth (deleted by the same admin call)
User row, identities (email / Google), sessions, refresh tokens. A still-unexpired access token
stops working at once because every request is validated against Supabase Auth.

### Server memory (purged by the endpoint; also expires on its own)
Uploaded PDF text (max 2 h), quiz answer keys (max 2 h), rate-limit counters.

### Browser data (cleared by the app — frontend work, not yet done)
`localStorage` project/chat cache and any cached API responses in the service worker.
The server can not do this; the consolidated `app.js` must.

### Not deleted by this feature — needs a decision or is outside our control
| Place | Why it is not (fully) deleted | Action |
|---|---|---|
| Supabase backups | Backups are point-in-time copies; deleted rows remain in them until the backup ages out | Counsel + Supabase plan: state the backup retention in the Privacy Policy |
| Google Gemini API | Prompts (and attached images / PDF text) were sent to the AI provider to generate answers; we can not delete them from there | Counsel: document the provider's retention terms; check the paid-tier data-use setting |
| Email provider logs | Sign-up / reset emails | Counsel: document |
| Hosting / server logs | Error log lines contain the student's pseudonymous user id (a UUID) - never email, token, password, chat or document text. The deletion itself logs only a one-way fingerprint. httpx request URLs (which carry ids) are no longer logged. Log retention is set by the hosting provider | Counsel: set a log retention period and state it in the Privacy Policy |
| Content already shared with others | A share link is removed, but anyone who copied the text keeps it | State in the Privacy Policy |

## Setting up the privileged key (the Founder does this, never in chat)

1. Supabase dashboard → Project Settings → API → copy the **service_role** key.
2. Put it ONLY in the hosting provider's secret/environment settings as `SUPABASE_SERVICE_ROLE_KEY`.
   For local testing, put it in your git-ignored `.env`. Never paste it in chat, tickets or files in the repo.
3. It bypasses Row Level Security on every table. It is read in exactly one file
   (`backend/account_deletion.py`) and tests fail if any other backend file reads it or if it
   appears in `static/` or `templates/`.
4. Without it, the endpoint answers `503 ACCOUNT_DELETION_UNAVAILABLE` and nothing else is affected.
5. If it is ever exposed: rotate it in the Supabase dashboard immediately and update the secret.

## Staging check (required before deletion is enabled for real students)

Use a **disposable account on a staging Supabase project**, never production data.

**A. Prove the re-authentication evidence (the part the code cannot prove offline)**
1. Set `ACCOUNT_DELETE_REAUTH_WINDOW_SECONDS=60` on the staging server (allowed 30-900; default 600).
2. Sign in as the test user in the web app. Copy the access token from DevTools -> Application ->
   Local Storage -> `sb-...-auth-token` -> `access_token`.
3. Run `python scripts/check_jwt_claims.py` and paste the token (input hidden; nothing is stored or
   printed back). Expected: `amr present: yes`, an entry such as `method='password'` marked `COUNTS`
   with an age of a few seconds, and `Verdict: RECENT`.
4. Wait more than 60 seconds. In the app force a session refresh (or wait for the automatic one),
   copy the NEW token and run the script again. **Expected: the sign-in age keeps growing** (not reset
   to ~0) and the verdict becomes `NOT RECENT`. If the age resets to ~0 after a refresh, Supabase is
   re-stamping the sign-in time on refresh: **stop - do not enable deletion** and tell the engineer.
5. Call `POST /api/account/delete` with `{"confirm":"DELETE"}` and the refreshed token. Expected:
   `403 REAUTH_REQUIRED`, and nothing deleted.
6. Repeat steps 2-3 for a Google (OAuth) test user. Expected: an `oauth` entry that `COUNTS`.
7. If `amr` is missing or has no timestamps for either path, deletion can not be enabled safely;
   the endpoint keeps answering `REAUTH_REQUIRED` (fail closed) and a different re-authentication
   design is needed.

**B. Prove the deletion itself**
1. Sign up a test user; create a project/chat, a quiz, a note, a share link, a report.
2. Sign in again (fresh login), call `POST /api/account/delete` with `{"confirm":"DELETE"}`.
3. In the Supabase SQL editor confirm zero rows for that user id in every table above and that
   the user is gone from Authentication -> Users; confirm the share link now shows "not found".
4. Confirm the same token now returns 401 and that signing in with the old password fails.
5. Confirm a second user's data is untouched.

Record the date, the Supabase project, and the observed results in the decision log.

## API contract (for the frontend)

`POST /api/account/delete`  body `{"confirm":"DELETE"}`  header `Authorization: Bearer <token>`

| HTTP | `detail.code` | Meaning / what the app does |
|---|---|---|
| 200 | — (`{"status":"deleted"}`) | Done. Sign out, clear browser data, show the goodbye screen |
| 400 | `ACCOUNT_DELETE_CONFIRMATION_REQUIRED` | The typed word was wrong |
| 401 | — | Not signed in, or **already deleted** after an earlier success — treat as done if a deletion was in progress |
| 403 | `REAUTH_REQUIRED` | No proof of a recent interactive sign-in (or the proof is unreadable). Ask the student to sign in again, then retry |
| 429 | — | More than 3 attempts in 10 minutes |
| 502 | `ACCOUNT_DELETE_FAILED` | Nothing was deleted; safe to retry |
| 503 | `ACCOUNT_DELETION_UNAVAILABLE` | Server not configured; show "contact support" |

## Open legal questions (for counsel — do not treat any answer here as decided)

1. **Payment records.** `payments` and `subscriptions` are deleted with the account. Bangladeshi tax /
   accounting rules may require keeping transaction records for a number of years. If so, the
   design must change (keep the financial record, detach it from the person) **before real payments
   go live**. Payments are not live yet, so none exist today.
2. **Reports.** `content_reports` are deleted with the account (B02 decision, unchanged). If safety
   reports must outlive the account, switch to `ON DELETE SET NULL` in a new migration.
3. **Minors.** Whether a parent/guardian can request deletion, and whether a minor's own request is
   sufficient.
4. **Backups and AI-provider copies** (table above): what must be disclosed, and for how long.
5. **Deletion timing.** Deletion here is immediate and total; counsel may require a grace period
   or a confirmation email — that would be a product change, not a bug.