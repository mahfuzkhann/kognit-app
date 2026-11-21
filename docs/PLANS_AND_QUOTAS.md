# Free and Pro plans, quotas and entitlements (migrations 0016 + 0017)

This is the readable version of the finalized plan. The numbers live in **one place per layer** and tests
keep the layers equal:

| Layer | Where | Kept equal by |
|---|---|---|
| Database (authority) | `public.plan_limits`, `public.plan_prices` | `supabase/tests/plan_final_0016_test.sql` |
| Application mirror | `backend/entitlements.py` (`QUOTA_POLICY`, `PRO_FEATURES`, `PHOTO_MAX_BYTES`, `PDF_MAX_PAGES`), `backend/payments.py` (`PRO_PRICES_BDT`) | `test_engine2_entitlements.py`, `test_free_pro_plan_final.py` |

## 1. The plans

| | Free | Pro (৳399 / month, ৳4,548 / year) |
|---|---|---|
| AI chats | 3 on the signup day, then 1 / day | 20 / day |
| Direct Answer + Socratic | share the chat allowance (one meter) | share the chat allowance |
| Photo Solve (**its own, independent quota**) | **1 per account, for life** | 5 / day |
| Photo size | at most 5 MB | at most 5 MB |
| PDF upload | not available | 2 / day, at most 5 pages each |
| AI practice quizzes | 1 / week | 5 / day |
| Pocket Notes | cannot create or edit (existing notes stay readable and deletable) | yes |
| Flashcards, quizzes from notes | not available | yes (200 / day internal abuse guard) |
| Learning insights / matrix / next steps / mistake analysis | not available | yes |
| Advanced AI model | not available | yes |

"20x more usage limit" (20 chats vs 1) and "35x more quizzes" (5 a day × 7 vs 1 a week) stay arithmetically true.

## 2. What consumes a quota (exactly)

A unit is taken **atomically in the database** (`consume_quota`, advisory-locked per user and kind) **after
the request is validated and before the provider is called**, then handed back if the student received nothing.

| Request | Meter | Taken when | Given back when |
|---|---|---|---|
| `/api/chat`, `/api/chat/stream` **with an image** | `photo_solve` — exactly one, **never a chat** | after auth, the 5 MB check and the PDF-context plan check | no text was delivered (provider failure before the first word, unreadable image, server error) |
| `/api/chat`, `/api/chat/stream` without an image | `chat` — exactly one, **never a Photo Solve** | same | same |
| `/api/quiz/generate`, mini-quiz from a note | `quiz` | after validation | generation failed / produced nothing usable |
| `/api/pdf/upload` | `pdf_upload` | after: Pro check, real signature, **real page count ≤ 5** | extraction failed, no readable text, or the text could not be stored |
| flashcards from a note | `flashcards` | after the Pro check | generation failed |
| chat titles | `notes_ai` | after validation | generation failed |

Never consumed: a request refused for plan, size, page count, bad file, bad input, auth, or quota. Taking a
quiz that was already generated, opening notes, flashcards or insights consumes nothing. A retry that
re-sends the same request id is counted once (`request_id` replay is idempotent); a refunded request id can
never be replayed for a second free unit. A response delivered to the student is final and not refunded
(CEO decision A, unchanged).

**Photo Solve is independent (CEO decision).** A photo request spends exactly one Photo Solve and no chat; a text
request spends exactly one chat and no Photo Solve; no request ever spends two units. The decision is made on the
server from the bytes it read (a client field cannot change the meter), and both chat endpoints share one helper
(`_chat_quota_kind`), so streaming and non-streaming cannot disagree. Cost is bounded by the sum of the two meters.

## 3. Time rules

* Day = calendar day in `Asia/Dhaka`; resets 12:00 AM Dhaka. Computed in SQL from `now() at time zone 'Asia/Dhaka'`; the server's own timezone is never used.
* Week = Monday 12:00 AM → next Monday 12:00 AM Dhaka.
* Signup day = the Dhaka calendar day of `auth.users.created_at` (a persisted account fact, not client state). Free gets 3 chats that day, then 1/day. Logging out, another device, cleared storage or a new chat thread change nothing.
* Lifetime (`period = 'lifetime'`) = ledger key `lifetime`, no reset time (`resets_at` is `null` in the API). A trigger refuses deletion of a lifetime row while the account exists, so a retention purge cannot hand out a second trial. Deleting the account still cascades.

## 4. Enforcement (server-side only)

* The plan is read from the database (`public.get_my_plan()` for the verified JWT). `plan`, `tier`, `model`, `kind` fields, `X-Plan` headers and query parameters are ignored (tested).
* Pro features: `PRO_FEATURES` in `backend/entitlements.py`, checked by `_require_plan_feature` **before** any body is read, quota unit spent or provider called. 403 `PLAN_REQUIRED` with `feature` and `required_plan`. Features: note flashcards, note quiz, learning insights / snapshot / mistakes / next steps / matrix, **Pocket Notes** (create, edit tags, AI tag suggestions), **PDF upload**.
* Pocket Notes are also Pro-only **in the database**: the INSERT policy on `pocket_notes` requires `get_my_plan() = 'pro'`, so calling PostgREST directly does not work either. Reading, listing and deleting notes a student already owns stays allowed. Nothing is deleted when a subscription ends.
* Uploads, from the real bytes: photo larger than 5 MiB (5,242,880 bytes; exactly 5 MiB is accepted) → 413 before any quota or plan call (the browser checks the same number first and shows the existing "file too large" message); bad image → refunded; PDF must have a `%PDF-` signature and a parseable, unencrypted body, with the page count taken from the file (a 6-page PDF is refused whatever the client says → 413 `PDF_PAGE_LIMIT_EXCEEDED`). Extraction is additionally capped at 5 pages.
* **PDF context and expiry.** A PDF uploaded while Pro is kept in server memory until its idle TTL. It is only ever served while the student is **currently Pro**: both chat endpoints check the plan (database, never the client) before using it and answer 403 `PLAN_REQUIRED` (`feature: pdf_upload`) otherwise, **before any quota unit is spent**. The text is not deleted (it simply is not served, and is served again if the student renews within the TTL), removing it with `/api/pdf/clear` still works, and a chat without a PDF never pays the extra plan lookup. Regression-tested end to end in `test_free_pro_plan_final.py::TestPdfContextRespectsTheCurrentPlan`.
* A quota backend outage fails **closed** (503 `QUOTA_UNAVAILABLE`).
* `QUOTA_MODE=off` (local development) and `PLAN_GATING=off` (documented rollback) keep their pre-existing meaning.

## 5. Model access

`gemini-3.1-flash-lite` (general) and `gemini-3.5-flash-lite` (advanced) are the only approved models; nothing was added.

* **Free never reaches the advanced model**: not for reasoning subjects, images, documents or quizzes, and not through a rate-limit/health fallback (the only alternative model *is* the advanced one, so a Free request has no fallback model).
* **Pro** keeps the existing policy router (≈70 % advanced / 30 % general for plain text, advanced for images, documents and demanding subjects, fallback on 429/health).
* One gate, `backend/ai/router.plan_safe_model_id`, is used by the router, the streaming path (all router modes) and every non-routed call (non-streaming chat, quiz, title, notes AI) through `ai_engine.plan_model_for_current_request()`, which reads the plan the endpoint recorded from the database. With **no** request context (offline evaluation scripts) the configured model is returned unchanged so benchmarks measure what they configure.
* The client cannot choose a model (there is no such parameter; extra fields are ignored — tested).

## 6. Subscription lifecycle and payments

`public.subscriptions` is server-controlled (no client write path). A subscription is Pro while `status = 'active' and starts_at <= now() < expires_at`; when it passes `expires_at` the student is Free again **with no job running**, and keeps every note, flashcard and result. `activate_pro_subscription()` (service-role only) validates the amount against `plan_prices` — **monthly is 399 BDT** (migration 0016) and **yearly is 4,548 BDT** (migration 0017). No yearly discount is advertised, and no price is shown anywhere in the UI.

> **Open issue for the CEO.** The decision calls the yearly price "exactly 12 × ৳399", but 12 × 399 = **4,788**; ৳4,548 is 12 × 379 (240 BDT, 5%, below twelve months). The explicit figure ৳4,548 is what is implemented. If ৳4,788 was meant, change it in four places that must agree: `supabase/migrations/0017_final_yearly_price.sql`, `PRO_PRICES_BDT` in `backend/payments.py`, `supabase/tests/plan_final_0016_test.sql`, and `test_free_pro_plan_final.py::TestPriceMirror` (plus this paragraph and `docs/DEPLOYMENT.md`).

**Payment status: unchanged and honest.** No payment provider is integrated; there is no checkout route and no fake success path. The Pro button still says "Pro isn't available yet". Before Pro can be sold: a verified bKash Merchant callback that calls `activate_pro_subscription` from a trusted server context (see `backend/payments.py`), plus an operator process for manual grants.

## 7. API errors

| Case | HTTP | `detail.code` |
|---|---|---|
| not signed in | 401 | (existing) |
| Pro-only feature (incl. using a stored PDF after expiry) | 403 | `PLAN_REQUIRED` (+ `feature`, `required_plan`) |
| allowance used up | 429 | `QUOTA_EXCEEDED` (+ `kind`, `plan`, `limit`, `used`, `remaining`, `resets_at`; `resets_at` is `null` for the lifetime trial) |
| photo > 5 MB | 413 | string detail "Image must be 5MB or smaller." (unchanged shape) |
| PDF > 5 pages | 413 | `PDF_PAGE_LIMIT_EXCEEDED` (+ `max_pages`, `pages`) |
| invalid file | 400 | (existing messages) |
| quota / plan lookup down | 503 | `QUOTA_UNAVAILABLE` |

Streaming keeps its existing semantics: quota/plan errors are HTTP responses before the stream starts, so a refusal can never look like an answer.

## 8. Telemetry

Existing `ai_usage_events` rows carry `plan`, model, tokens and estimated cost (`docs/USAGE_REPORTS.sql` has the per-user cost and model-mix queries), and `quota_ledger` holds per-user counts of chats, photo solves, PDF uploads and quizzes by kind. No prompt or document text is logged.

## 9. Deploy, verify, roll back

**Apply order (do not reverse):** 1) apply `0016_free_pro_plan_final.sql`, then `0017_final_yearly_price.sql`, in the Supabase SQL editor (both idempotent; re-running them re-asserts the numbers above), 2) deploy the application, 3) verify. Nothing in this repository applies a migration or deploys by itself.

```sql
select plan, kind, period, limit_count, first_day_limit_count from public.plan_limits order by plan, kind;
select plan, billing_period, amount_bdt from public.plan_prices order by 1, 2;     -- pro monthly 399, yearly 4548
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

**Rollback:** application — redeploy the previous version (the old code ignores the new kinds). Data — the commented ROLLBACK blocks at the top of `0016` and `0017` (prices, limits, notes policy). Nothing is deleted by either direction.

## 10. Known limitations

1. **Yearly price arithmetic** — see the open issue in section 6.
2. Photos up to 5 MB are accepted as they are (no compression). A raw phone photo can still be larger; the student is told before sending and keeps the original quality.
3. Free is on the general model for *everything* (maths, images, quizzes): expect lower answer quality on hard questions than Pro.
4. `MAX_NOTES_PER_USER = 500` (`backend/notes_engine.py`) is a technical safeguard against runaway storage. It is deliberately not part of the plan and is not marketed as a limit.
5. A PDF context that outlives a Pro subscription stays in memory until its idle TTL (it is not served — see section 4); a server restart drops all contexts anyway (known MVP behaviour).
6. Learning Insights / Matrix / Next Steps / Mistake Analysis were already implemented and Pro-gated; they were verified, not changed.
7. Payments are not integrated: no checkout route, no fake success path, no subscription without a verified payment (section 6).
8. Verified only locally (full Python suite, jsdom suite, a local PostgreSQL 16 with every migration). Not verified: production Supabase, live Gemini behaviour, real browsers (puppeteer suites were not run), real devices.