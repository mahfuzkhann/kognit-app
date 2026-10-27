# Pre-Phase 10 live verification checklist (founder only)

Everything in this file needs your live Supabase project, your server, or a real Gemini key. None of it could be done or verified from the repository or the test environment, so every item is **FOUNDER ACTION REQUIRED** until you record evidence next to it.

Rules for this checklist:
- **Never paste a secret** (service keys, database password, JWTs, `USAGE_WRITE_KEY`, Gemini key) into chat, a ticket or a file in the repo. Record only "done / not done", counts, status codes and screenshots with secrets hidden.
- Items marked **CHANGES LIVE STATE** modify your real project. Do them in the order given and write down the time you did them.
- Supabase dashboard labels change between versions. The navigation below is the usual path; if a label differs, search the dashboard for the setting name or check the current Supabase documentation. I could not open your dashboard, so the labels are not verified.
- Do NOT push, merge or deploy until you are satisfied with step 0.

## 0. Before anything else: get the code into your repo (local, no live effect)
1. Copy the delivered files into your repository at the same paths, run `python -m pip install -r requirements.txt` in a **new** virtual environment, then run the full suite and smoke suite (commands in section 12 of `docs/PRE_PHASE10_REMEDIATION_REPORT.md`).
2. Review `git status` and `git diff`. Commit only if you are satisfied. *(Committing is your decision; nothing here commits for you.)*
   **Evidence to record:** pytest summary line, smoke summary line.

## 1. Check the live migration state (read-only)
Open Supabase Dashboard -> SQL Editor (these are SELECT statements only).

1. Which migrations exist? Run:
   ```sql
   select to_regclass('public.usage_write_keys') as has_0011_key_table,
          exists (select 1 from pg_proc where proname = 'record_ai_usage_events') as has_0011_writer,
          exists (select 1 from pg_proc where proname = 'consume_quota' and prosrc like '%rate_limited%') as has_0012_cap,
          exists (select 1 from plan_limits where kind = 'notes_ai') as has_notes_limit;
   ```
   Expected today if you have only applied 0011: `true, true, false, true`. After step 3: all true.
2. Constraint names (needed by 0011 on a database that predates it; skip if the first two columns above are already true):
   ```sql
   select conrelid::regclass as tbl, conname from pg_constraint
   where conrelid in ('public.plan_limits'::regclass, 'public.quota_ledger'::regclass) and contype = 'c';
   ```
   Expected: `plan_limits_kind_check` and `quota_ledger_kind_check` are listed. If a differently named constraint restricts `kind`, **stop and send me the output** before applying 0011.
3. Run `supabase/verify_schema.sql` (expected: zero rows).
   **Evidence:** the results of 1-3 (they contain no secrets).

## 2. Approve or change the Notes/title limit (decision)
Migration 0011 seeds Free `notes_ai` = **6 per Dhaka day** (a proposal). Decide before launch. To change it later: `update public.plan_limits set limit_count = <N> where plan = 'free' and kind = 'notes_ai';` (**CHANGES LIVE STATE**, takes effect immediately, no deploy).

## 3. Apply the migrations (CHANGES LIVE STATE)
Take a database backup/snapshot first (Dashboard -> Database -> Backups; the exact option depends on your plan). Then, in the SQL Editor, run in this order, one file at a time, reading any error before continuing:
1. `supabase/migrations/0011_quota_notes_meter_and_telemetry_integrity.sql` (skip if step 1 showed it is already applied; it is safe to re-run)
2. `supabase/migrations/0012_usage_privileges_and_ledger_cap.sql`

Then run `supabase/verify_policies.sql` and compare with its comments:
- Query 4 (grants on server-owned tables): **zero rows**. If rows remain, 0012 has not been applied.
- Query 5 (functions without a fixed search_path / callable by anon): **zero rows**.
- Query 6: shows how many usage-writer key hashes are stored (0 until step 4).
- Query 7 (Pro limits): **zero rows** today. Do not activate Pro until chat, quiz and notes_ai rows exist.
**Evidence:** row counts of queries 4-7.

## 4. Configure the usage writer (CHANGES LIVE STATE; secret handling)
1. Generate a long random value on your own computer, never in a chat window:
   - Python (works on Windows): `python -c "import secrets;print(secrets.token_urlsafe(48))"`
2. Put it in your **server's** environment as `USAGE_WRITE_KEY` (your hosting provider's environment-variable screen, or your local `.env` which is git-ignored). Do not put it in any file that is committed.
3. Compute its SHA-256 locally and store **only the hash**:
   - `python -c "import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())" "<your key>"`
   - SQL Editor: `insert into public.usage_write_keys (key_hash) values ('<the 64-character hash>');`
   The database compares the hash of what the app sends, so the app must send the **original key**, not the hash. (Tested: the hash used as the key is refused.)
4. Restart the server so it reads the variable.
**Evidence:** query 6 now shows 1; the server log at startup shows no "USAGE_WRITE_KEY is not set" warning after your first chat.

## 5. Correlate one real chat with its usage event
1. Send one new chat in the app while logged in. In the browser's developer tools -> Network, find the `/api/chat/stream` request and copy the response header `X-Request-ID` (a short hex string; it is not a secret).
2. In the SQL Editor run (replace the id):
   ```sql
   select created_at, request_id, operation, status, model, attempt, input_tokens, output_tokens,
          total_tokens, duration_ms, ttft_ms, plan, estimated_cost_usd, pricing_version
   from public.ai_usage_events where request_id = '<X-Request-ID value>' order by id;
   ```
   Expected: at least one row for that exact id; `estimated_cost_usd` and `pricing_version` are **NULL** (no verified price list exists); `plan` is `free` (or `pro` only for a real active subscription); no prompt or answer text anywhere in the row. An older row proves nothing; the id must match.
3. If no row appears: check the server log for "usage persistence failed" or the missing-key warning; confirm step 3 and 4; confirm you restarted.
**Evidence:** the single result row (it contains no secrets).

## 6. Supabase Auth settings (CHANGES LIVE STATE only if you change them)
Dashboard -> Authentication. Record the current value of each, then decide:
1. **Email confirmation** (usually Authentication -> Providers -> Email -> "Confirm email"). Recommended ON before public launch. Without it anyone can create unlimited accounts with unverified addresses, and each new account receives fresh Free quota (3 chats on the first day).
2. **CAPTCHA / bot protection** (usually Authentication -> Attack Protection). Recommended ON for sign-up (hCaptcha or Cloudflare Turnstile). Turning it on requires adding the CAPTCHA widget to the sign-up form; tell me and I will implement it as a separate task.
3. **Auth rate limits** (usually Authentication -> Rate Limits): record the sign-up, sign-in and email limits. Lower them if they are generous.
4. **Redirect URLs and Site URL** (Authentication -> URL Configuration): Site URL must be your production address; Redirect URLs must list only addresses you control (no wildcards you do not need; remove test/localhost entries before launch). The Google sign-in redirect must match.
5. **Password policy** (minimum length) if you allow email/password sign-up.
Do not weaken any setting to make a test pass.
**Evidence:** a table of setting -> value -> decision (no screenshots with keys).

## 7. Real Gemini and upload checks (use a test account and a real key on the server)
Run these against your deployed or locally running app, not against the test suite:
1. **Streamed answer:** ask a normal question; text appears progressively; the final answer matches what streamed; no error bar.
2. **Valid PDF:** upload a real chapter PDF (a few MB). Expected: "PDF loaded", and a question about its content is answered from it.
3. **Valid image:** attach a real photo of a handwritten problem. Expected: the answer refers to the image.
4. **Invalid files:** upload a renamed text file as a PDF (expected: a clear refusal), a PDF over 15 MB (expected: refusal mentioning the limit), an image over 8 MB (expected: refusal), a corrupt image (expected: a friendly error and the chat is **not** charged).
5. **Quota behaviour:** with a Free test account use the first-day chats, confirm the next request is refused with the reset time; confirm Notes/title tools show the quota message when their limit is used.
6. **After the dependency upgrade**, repeat 1-4 once on the final deployed build. Pillow and the web framework both changed major versions; the automated tests pass but only a live run proves real photos and PDFs.
**Evidence:** pass/fail per item with the date and the app version/commit.

## 8. Log inspection
After steps 5-7, read the server logs for the period:
1. No student prompts, answers, PDF text, image data, tokens or keys (search for a distinctive phrase you typed in the test chat; it must not appear).
2. No stack traces returned to the browser (check the response bodies in the Network tab for the failed uploads).
3. Count of "usage persistence failed" and "quota refund failed" lines (expected: none under normal operation).
**Evidence:** "searched for X, found 0 matches" notes.

## 9. Phase 10 entry criteria (decision, not a test)
Your own Phase 10 document says Phase 10 begins after launch and 1-2 months of real usage. Before starting, write down: launch date; number of real students and weeks of data; the top recurring problems found; feedback received; funding/provider access for AI Engine 2.0; and the written Phase 10 scope and acceptance criteria (none exists in the repository today).

## Sign-off table
| Step | Done (date) | Result / evidence reference |
|---|---|---|
| 0 Tests green locally | | |
| 1 Migration state checked | | |
| 2 Notes limit decided | | |
| 3 Migrations 0011+0012 applied, verify queries clean | | |
| 4 Usage writer configured | | |
| 5 Event correlated | | |
| 6 Auth settings reviewed | | |
| 7 Live Gemini/PDF/image checks | | |
| 8 Logs inspected | | |
| 9 Phase 10 criteria written | | |