# Free and Pro plans, quotas and entitlements (migration 0016)

This is the readable version of the finalized plan. The numbers live in **one place per layer** and tests
keep the layers equal:

| Layer | Where | Kept equal by |
|---|---|---|
| Database (authority) | `public.plan_limits`, `public.plan_prices` | `supabase/tests/plan_final_0016_test.sql` |
| Application mirror | `backend/entitlements.py` (`QUOTA_POLICY`, `PRO_FEATURES`, `PHOTO_MAX_BYTES`, `PDF_MAX_PAGES`), `backend/payments.py` (`PRO_PRICES_BDT`) | `test_engine2_entitlements.py`, `test_free_pro_plan_final.py` |

## 1. The plans

| | Free | Pro (৳399 / month) |
|---|---|---|
| AI chats | 3 on the signup day, then 1 / day | 20 / day |
| Direct Answer + Socratic | share the chat allowance (one meter) | share the chat allowance |
| Photo Solve | **1 per account, for life** | 5 / day |
| Photo size | less than 3 MB | less than 3 MB |
| PDF upload | not available | 2 / day, at most 5 pages each |
| AI practice quizzes | 1 / week | 5 / day |
| Pocket Notes | not available | yes (saved notes: no product-level count limit) |
| Flashcards, quizzes from notes | not available | yes (200 / day internal abuse guard) |
| Learning insights / matrix / next steps / mistake analysis | not available | yes |
| Advanced AI model | not available | yes |

"20x more usage limit" (20 chats vs 1) and "35x more quizzes" (5 a day × 7 vs 1 a week) stay arithmetically true.

## 2. What consumes a quota (exactly)

A unit is taken **atomically in the database** (`consume_quota`, advisory-locked per user and kind) **after
the request is validated and before the provider is called**, then handed back if the student received nothing.

| Request | Meter | Taken when | Given back when |
|---|---|---|---|
| `/api/chat`, `/api/chat/stream` **with an image** | `photo_solve` (not `chat`) | after auth, the 3 MB check and the PDF-context check | no text was delivered (provider failure before the first word, unreadable image, server error) |
| `/api/chat`, `/api/chat/stream` without an image | `chat` | same | same |
| `/api/quiz/generate`, mini-quiz from a note | `quiz` | after validation | generation failed / produced nothing usable |
| `/api/pdf/upload` | `pdf_upload` | after: Pro check, real signature, **real page count ≤ 5** | extraction failed, no readable text, or the text could not be stored |
| flashcards from a note | `flashcards` | after the Pro check | generation failed |
| chat titles | `notes_ai` | after validation | generation failed |

Never consumed: a request refused for plan, size, page count, bad file, bad input, auth, or quota. Taking a
quiz that was already generated, opening notes, flashcards or insights consumes nothing. A retry that
re-sends the same request id is counted once (`request_id` replay is idempotent); a refunded request id can
never be replayed for a second free unit. A response delivered to the student is final and not refunded
(CEO decision A, unchanged).

**Interpretation to confirm (flagged):** a photo question spends Photo Solve *instead of* a chat, so the two
allowances are separate lines of the plan and Free's one trial photo never depends on whether today's single
chat was used. Cost is bounded by the sum of both meters.

## 3. Time rules

* Day = calendar day in `Asia/Dhaka`; resets 12:00 AM Dhaka. Computed in SQL from `now() at time zone 'Asia/Dhaka'`; the server's own timezone is never used.
* Week = Monday 12:00 AM → next Monday 12:00 AM Dhaka.
* Signup day = the Dhaka calendar day of `auth.users.created_at` (a persisted account fact, not client state). Free gets 3 chats that day, then 1/day. Logging out, another device, cleared storage or a new chat thread change nothing.
* Lifetime (`period = 'lifetime'`) = ledger key `lifetime`, no reset time (`resets_at` is `null` in the API). A trigger refuses deletion of a lifetime row while the account exists, so a retention purge cannot hand out a second trial. Deleting the account still cascades.

## 4. Enforcement (server-side only)

* The plan is read from the database (`public.get_my_plan()` for the verified JWT). `plan`, `tier`, `model`, `kind` fields, `X-Plan` headers and query parameters are ignored (tested).
* Pro features: `PRO_FEATURES` in `backend/entitlements.py`, checked by `_require_plan_feature` **before** any body is read, quota unit spent or provider called. 403 `PLAN_REQUIRED` with `feature` and `required_plan`. Features: note flashcards, note quiz, learning insights / snapshot / mistakes / next steps / matrix, **Pocket Notes** (create, edit tags, AI tag suggestions), **PDF upload**.
* Pocket Notes are also Pro-only **in the database**: the INSERT policy on `pocket_notes` requires `get_my_plan() = 'pro'`, so calling PostgREST directly does not work either. Reading, listing and deleting notes a student already owns stays allowed. Nothing is deleted when a subscription ends.
* Uploads, from the real bytes: photo ≥ 3 MiB (3,145,728 bytes) → 413 before any quota call; bad image → refunded; PDF must have a `%PDF-` signature and a parseable, unencrypted body, with the page count taken from the file (a 6-page PDF is refused whatever the client says → 413 `PDF_PAGE_LIMIT_EXCEEDED`). Extraction is additionally capped at 5 pages.
* A quota backend outage fails **closed** (503 `QUOTA_UNAVAILABLE`).
* `QUOTA_MODE=off` (local development) and `PLAN_GATING=off` (documented rollback) keep their pre-existing meaning.

## 5. Model access

`gemini-3.1-flash-lite` (general) and `gemini-3.5-flash-lite` (advanced) are the only approved models; nothing was added.

* **Free never reaches the advanced model**: not for reasoning subjects, images, documents or quizzes, and not through a rate-limit/health fallback (the only alternative model *is* the advanced one, so a Free request has no fallback model).
* **Pro** keeps the existing policy router (≈70 % advanced / 30 % general for plain text, advanced for images, documents and demanding subjects, fallback on 429/health).
* One gate, `backend/ai/router.plan_safe_model_id`, is used by the router, the streaming path (all router modes) and every non-routed call (non-streaming chat, quiz, title, notes AI) through `ai_engine.plan_model_for_current_request()`, which reads the plan the endpoint recorded from the database. With **no** request context (offline evaluation scripts) the configured model is returned unchanged so benchmarks measure what they configure.
* The client cannot choose a model (there is no such parameter; extra fields are ignored — tested).

## 6. Subscription lifecycle and payments

`public.subscriptions` is server-controlled (no client write path). A subscription is Pro while `status = 'active' and starts_at <= now() < expires_at`; when it passes `expires_at` the student is Free again **with no job running**, and keeps every note, flashcard and result. `activate_pro_subscription()` (service-role only) validates the amount against `plan_prices` — **monthly is now 399 BDT**; **yearly is untouched at 3,199 BDT** (the specification gives no yearly price: decide it, then `update public.plan_prices set amount_bdt = <N> where plan='pro' and billing_period='yearly'` and `PRO_PRICES_BDT` in `backend/payments.py`).

**Payment status: unchanged and honest.** No payment provider is integrated; there is no checkout route and no fake success path. The Pro button still says "Pro isn't available yet". Before Pro can be sold: a verified bKash Merchant callback that calls `activate_pro_subscription` from a trusted server context (see `backend/payments.py`), plus an operator process for manual grants.

## 7. API errors

| Case | HTTP | `detail.code` |
|---|---|---|
| not signed in | 401 | (existing) |
| Pro-only feature | 403 | `PLAN_REQUIRED` (+ `feature`, `required_plan`) |
| allowance used up | 429 | `QUOTA_EXCEEDED` (+ `kind`, `plan`, `limit`, `used`, `remaining`, `resets_at`; `resets_at` is `null` for the lifetime trial) |
| photo ≥ 3 MB | 413 | string detail "Image must be smaller than 3MB." (unchanged shape) |
| PDF > 5 pages | 413 | `PDF_PAGE_LIMIT_EXCEEDED` (+ `max_pages`, `pages`) |
| invalid file | 400 | (existing messages) |
| quota / plan lookup down | 503 | `QUOTA_UNAVAILABLE` |

Streaming keeps its existing semantics: quota/plan errors are HTTP responses before the stream starts, so a refusal can never look like an answer.

## 8. Telemetry

Existing `ai_usage_events` rows carry `plan`, model, tokens and estimated cost (`docs/USAGE_REPORTS.sql` has the per-user cost and model-mix queries), and `quota_ledger` holds per-user counts of chats, photo solves, PDF uploads and quizzes by kind. No prompt or document text is logged.

## 9. Deploy, verify, roll back

**Apply order (do not reverse):** 1) apply `supabase/migrations/0016_free_pro_plan_final.sql` in the Supabase SQL editor (idempotent; it resets the numbers above if re-run), 2) deploy the application, 3) verify.

```sql
select plan, kind, period, limit_count, first_day_limit_count from public.plan_limits order by plan, kind;
select plan, billing_period, amount_bdt from public.plan_prices order by 1, 2;     -- pro monthly 399, yearly 3199
select polname from pg_policy where polrelid = 'public.pocket_notes'::regclass;    -- pocket_notes_insert_own present
select tgname from pg_trigger where tgname = 'trg_quota_ledger_protect_lifetime';
```

**Tests**

```bash
export GEMINI_API_KEY=ci-not-a-real-key SUPABASE_URL=https://ci.invalid SUPABASE_ANON_KEY=ci-not-a-real-key
python -m pytest -q                                   # full suite, no network, no Gemini credits
cd tests_frontend && npm ci && npm run test:jsdom     # frontend behaviour tests
bash supabase/tests/run_local_db_tests.sh             # real PostgreSQL: every migration, RLS, concurrency (needs a local Postgres)
```

**Rollback:** application — redeploy the previous version (the old code ignores the new kinds). Data — the commented ROLLBACK block at the top of the migration. Nothing is deleted by either direction.

## 10. Known limitations / decisions for the CEO

1. Monthly price changed 299 → 399 as instructed; the earlier recorded ৳299 / ৳3,199 pair no longer matches (yearly undecided).
2. Free has no PDF upload and no Pocket Notes — read from the plan table (neither is listed for Free); existing data stays readable.
3. The 3 MB photo limit is applied to every plan (a phone camera photo is often 3–8 MB and the frontend does not resize: expect "too large" for many raw photos until client-side resizing is added).
4. A photo spends Photo Solve instead of a chat (section 2).
5. Free is now on the general model for *everything* (maths, images, quizzes): expect lower answer quality on hard questions; the Pro difference is real, not cosmetic.
6. `MAX_NOTES_PER_USER = 500` (`backend/notes_engine.py`) is a pre-existing technical cap on saved notes; "unlimited" in marketing means "no plan limit" — raise the constant if 500 is too low.
7. An expired Pro student's in-memory PDF context (TTL-bounded, per chat) can still be used for chat until it expires; no new upload is possible.
8. Learning Insights / Matrix / Next Steps / Mistake Analysis were already implemented and Pro-gated; this task only verified that.
9. Outdated plan copy was corrected in `static/js/i18n.js` (Free card: photo trial, 1 quiz/week, no Pocket Notes line; help text on file limits) and `static/js/feedback.js` (upgrade copy for the two new Pro features); `static/sw.js` version bumped so returning users get the new files. No layout, style or component changed. `about.kloriz.p2` still describes the whole product ("PDFs … Pocket Notes") and was left alone.