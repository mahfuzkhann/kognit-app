# Kloriz AI Engine 2.0 — implementation, operations and rollback

**Status (read this first)**

| Dimension | Status |
|---|---|
| Code implementation | **Complete** for the approved scope (see §10 for the items that are deliberately *not* implemented) |
| Automated tests | **Passed** (counts in §11). Mock/integration only. |
| Database integration | **Passed** against a real PostgreSQL 16 (migrations, quota behaviour, 24-way concurrency) |
| Frontend integration | **Passed** in jsdom **and** real Chromium 153 |
| Live Gemini integration | **Pending paid credentials — NOT verified** |
| Security review | Code-level review done; **live Supabase RLS/Auth and a paid-tier key check are pending** |
| Production capacity | **Not certified.** No 10,000-user claim is made or supported. |
| Production readiness | **Not certified** |

Baseline audited: `main` @ `b2e7abf`; AI Engine 2.0 first pushed as `e085e22`; this document describes the final state after the CEO's final decisions.

---------------------------------------------------------------------------------------------

## 1. What exists (one line per area)

| Area | Where | Default |
|---|---|---|
| Model registry, verified metadata + versioned prices | `backend/ai/registry.py` | on |
| Deterministic router (task, plan, capability, health) | `backend/ai/router.py` | `AI_ROUTER_MODE=static` (legacy model) |
| Failure policy, retry budget, deadlines, model fallback, admission | `backend/ai/gateway.py` | deadlines/budget **on**, fallback **off** |
| Model health + circuit breaker (in-process) | `backend/ai/health.py` | on |
| Cost from reported tokens × verified prices | `backend/ai/cost.py` | on |
| Layered prompts, document-as-untrusted-user-content, token budget | `backend/ai/prompts.py` | `AI_CONTEXT_BUILDER=legacy` |
| Shadow verification | `backend/ai/verify.py` | `AI_VERIFY_MODE=off` |
| Free/Pro entitlements + the Pro feature list | `backend/entitlements.py` | `PLAN_GATING=enforce` |
| Pooled HTTP clients | `backend/http_client.py` | on |
| Upgrade UX (explanation + value + "Upgrade to Pro") | `static/js/feedback.js`, `notes.js`, `app.js`, `nav-views.js`, `i18n.js`, `profile-menu.js` | on |
| Migrations | `0014_…plans_usage_curriculum.sql`, `0015_flashcards_meter.sql` | apply before deploy |

The NDJSON stream contract (`start`/`delta`/`retry`/`done`/`interrupted`/`error`) is **unchanged**. `backend/ai_engine.py` keeps every public name; the Bug-3 semantics (explicit completion, one bounded recovery, failed partial output discarded, 429 not retried) are preserved and tested.

## 2. Approved models (verified on Google's official docs, 2026-11-03)

| | `gemini-3.1-flash-lite` (general) | `gemini-3.5-flash-lite` (advanced) |
|---|---|---|
| Status | Stable | Stable |
| Inputs | text, image, video, audio, PDF | text, image, video, audio, PDF |
| Context / max output | 1,048,576 / 65,536 | 1,048,576 / 65,536 |
| Streaming, structured output, search grounding, thinking | yes | yes |
| Documented thinking levels | not enumerated → **engine sends none** | minimal, low, medium, high |
| $/1M tokens (Standard, paid): in / out / cached-in | 0.25 / 1.50 / 0.025 | 0.30 / 2.50 / 0.03 |
| Sampling params | — | **removed** (engine sends none) |

Output price includes thinking tokens. Search grounding is billed per query and is **not** part of `estimated_cost_usd`. Re-verify prices and IDs before launch: they are a snapshot.

Routing (first match; policy `2026-11-03.r1`): image/PDF → advanced · Class 9–12 maths/physics/chemistry/numerical commerce/reasoning ICT → advanced · otherwise Free → general, Pro → ≈70% advanced / 30% general by a stable hash of the request id · suitability always beats the split · an open breaker diverts to the other capable model. The subject classifier is a **keyword heuristic with unmeasured accuracy** — run `shadow` first.

## 3. Plans, quotas and entitlements (final CEO decisions)

| | Free | Pro |
|---|---|---|
| Chat | 3 on the first login day, then 1/day | 20/day |
| **Quiz** | **3 per week, weeks reset Monday 00:00 Asia/Dhaka (+06:00, no DST)** | 15/day (**note-generated quizzes count here**) |
| Notes: save | yes | yes |
| Notes: titles / tag suggestions (`notes_ai`) | 6/day | 200/day generic guard (separate meter) |
| Notes: flashcards | **403 `PLAN_REQUIRED`** | own `flashcards` meter — **200/day temporary abuse guard** |
| Notes: quiz from a note | **403 `PLAN_REQUIRED`** | shares the 15/day quiz pool |
| Insights, snapshot, mistakes, next-steps, topic matrix | **403 `PLAN_REQUIRED`** | yes |

* **What the student is told:** Pro = "20 AI chats every day · 15 quizzes every day · **Unlimited flashcards** · Quizzes from your saved notes · Learning insights and your learning matrix · Personalised next steps and mistake analysis". Tests assert this list is identical in the backend (`entitlements.PRO_FEATURE_LIST`), the upgrade UI (`feedback.js`) and the Plan screen (`i18n.js`). **The 200/day guard appears in no student-facing text** (also asserted).
* **The flashcards guard is TEMPORARY and CONFIGURABLE** — an abuse guard behind "Unlimited flashcards", not a product allowance. Change it with no deploy: `update public.plan_limits set limit_count = <N> where plan='pro' and kind='flashcards';`. It has its own meter so it can never throttle titles, tag suggestions, quizzes or chat.
* **The five Learning-Intelligence endpoints** (verified from `backend/main.py`, asserted by an inventory test): `GET /api/profile/insights`, `/snapshot`, `/mistakes`, `/next-steps`, `/topics`. **Not** gated: `GET/PUT /api/profile` (onboarding) and `GET /api/learning/history`.
* **Quiz entry points** (inventory-tested): `POST /api/quiz/generate` and `POST /api/notes/{note_id}/mini-quiz`. Both spend the `quiz` meter; a new route that generates quizzes fails the inventory test until it is reviewed.
* **Server-side only.** The plan comes from `public.get_my_plan()`; a Free request for Pro material is refused **before** any quota unit or provider call; a plan-lookup failure fails **closed** (503). `PLAN_GATING=off` is the rollback.
* **Refunds:** a generation that fails with no usable output is refunded; a successful one never is; a refunded request id cannot be replayed (SQL-tested).
* **Concurrency:** quota consumption is serialised per student in the database (advisory lock). Proven on real PostgreSQL: **exactly 3 of 24** simultaneous weekly quizzes and **exactly 5 of 24** simultaneous flashcards (guard temporarily 5) are counted.
* **Weekly boundary proof (honest scope):** `consume_quota()` reads `now()`, which a test cannot fake. The boundary is therefore proven by (1) rows from the previous week's key not counting, (2) the allowance being fresh when the week key changes, (3) the reported `resets_at` equalling an independently computed next-Monday-00:00+06:00, and (4) the identical `date_trunc('week', … at time zone 'Asia/Dhaka')` expression evaluated at the instants either side of the boundary (Sunday 23:59:59 vs Monday 00:00:00 Dhaka) — it is **not** a test that travels the real clock across a Monday.

## 4. Upgrade UX

A Free student reaching a Pro-only feature sees an explanation of what is locked, the value of Pro, the Pro list, and a visible **"Upgrade to Pro"** button — never a generic error.

* Surfaces: Pocket Notes (flashcards, note quiz), the Student Profile panel (one card + "Included with Pro." in the other sections), and the Quizzes topic history. Stored study material stays openable after a downgrade.
* **There is no checkout, and none was invented.** The button opens the existing **Account → Plan** screen, which now lists what Pro includes and says plainly that Pro is not available yet; its Pro action is disabled. No request is sent, nothing is charged or unlocked, no success state exists.
* Missing/invalid destination (no profile menu, `openAccount` not a function, it throws, or returns `false`) → the button is shown **disabled** with the plain next step "open your account menu → Plan"; never a dead button, never an exception.
* Verified in jsdom (37 assertions: card, CTA, modal accessibility, focus, destination failures, no HTML injection, no payment/success wording, the three integrations) **and** by rendering in real Chromium in light and dark themes.
* **Not done:** the Plan screen cannot show "You are on Pro" because no endpoint exposes the plan to the browser; this matters only once a purchase flow exists.

## 5. PDF document budget

* Budget **300,000 characters** (legacy builder always; v2 builder restored from my earlier 120,000). Extraction/storage cap is 2× that; upload size, page and time limits are unchanged.
* **300,000 characters is not assumed safe.** The v2 builder also enforces a whole-request input-token budget (`AI_CONTEXT_MAX_INPUT_TOKENS`, default 400,000 estimated tokens, far inside the 1,048,576-token window); the document shrinks to fit. The characters→tokens ratio (2.0) is a conservative **hypothesis**; real `input_tokens` in `ai_usage_events` are authoritative.
* **Truncation is explicit and observable:** (1) the upload response carries `max_context_chars` and `will_be_truncated`; (2) the student is told in the chat ("only its first 300,000 characters can be used…"); (3) a WARNING logs the numbers (never the content); (4) the model is told which part it cannot see.
* **Cost reality:** the document is resent on every turn. At 300,000 characters ≈ 150,000 estimated tokens per request on the advanced model ≈ **$0.045 input per turn**, before output. Watch `input_tokens` and `estimated_cost_usd` per student before launch.
* Malformed, empty, unsigned, oversized, and corrupt uploads return safe 4xx/413 responses, never reach the parser when oversized, and leave the app healthy (tested).

## 6. Reliability behaviour

| Failure | Before first token | After partial text |
|---|---|---|
| 400/404/413 | fail | interrupted |
| 401/403 | fail, never trips the breaker | interrupted |
| 429 | never retried on the same model; with `AI_MODEL_FALLBACK=on` one attempt on the other approved model | interrupted |
| 5xx / 409 | ≤3 attempts, jittered backoff, budget+deadline gated | one recovery generation (partial discarded, never concatenated) |
| timeout / connect | ≤3 attempts, gated | interrupted |
| unclassified | retried once | interrupted |

Deadlines: first token `AI_FIRST_TOKEN_DEADLINE_SECONDS` (75 s), whole answer `AI_STREAM_DEADLINE_SECONDS` (240 s, checked between events, so a stalled stream ends at most one SDK read-timeout later). Retry budget: process-wide token bucket (burst 30, +3/s). Admission: 32 running / 64 queued / 8 s wait, then a calm "busy" message **with the chat unit refunded**. Exactly one terminal event is asserted in 36 scenarios. **Cross-provider fallback does not exist.**

## 7. Deployment

### 7.1 Migration order (the only manual database step)
1. `supabase/migrations/0014_ai_engine_2_plans_usage_curriculum.sql`
2. `supabase/migrations/0015_flashcards_meter.sql`

**Apply both BEFORE deploying the application.** The profile read selects the `curriculum` column (0014) and Pro flashcards use the `flashcards` meter (0015); the new code fails closed (503) on the missing pieces. Both are additive and idempotent. Verify after applying:
```sql
select plan, kind, period, limit_count from public.plan_limits order by plan, kind;   -- pro chat 20/day, quiz 15/day, notes_ai 200/day, flashcards 200/day; free chat 1(3), quiz 3/week, notes_ai 6/day; NO free flashcards row
select public.get_my_plan();                                                           -- as a signed-in student: 'free' (or 'pro')
select column_name from information_schema.columns where table_name='student_profiles' and column_name='curriculum';
```

### 7.2 Settings (all optional; defaults reproduce pre-2.0 behaviour — see `.env.example`)
Required secrets exist already (`GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `USAGE_WRITE_KEY`, `QUOTA_MODE`). **Never** put key values in code, the frontend, logs, docs, commits or chat. Flags: `AI_ROUTER_MODE`, `AI_CONTEXT_BUILDER`, `AI_VERIFY_MODE`, `AI_MODEL_FALLBACK`, `AI_GATEWAY`, `PLAN_GATING`, plus the numeric limits in `.env.example`.

### 7.3 Rollout order, how to verify, how to roll back

| Stage | Action | Verify | Roll back |
|---|---|---|---|
| 0 | Apply 0014 then 0015 | queries in §7.1 | see the comment block at the top of each migration (usually leave the schema; flags suffice) |
| 1 | Deploy code, all flags at default | chat works; `/healthz`, `/readyz` OK; usage rows appear with `attempt_kind`; Free student gets the upgrade card on flashcards | redeploy previous version; or `PLAN_GATING=off`, `AI_GATEWAY=legacy` |
| 2 | `AI_ROUTER_MODE=shadow` | logs show `route_model=… route_reason=…` for every chat while the legacy model is still used; compare `task_category` with reality | `AI_ROUTER_MODE=static` |
| 3 | **After live validation (§9)**: `AI_ROUTER_MODE=policy`, `AI_CONTEXT_BUILDER=v2` for founder accounts only | `model` column shows the routed model; answers read well in Bangla and English; `input_tokens` sane | `static` / `legacy` |
| 4 | `AI_VERIFY_MODE=shadow`, sampled | `operation='verify'` rows with `verification_status`; student answers unchanged | `AI_VERIFY_MODE=off` |
| 5 | Beta cohort, then everyone | cost/request, 429/503 rate, `interrupted` rate, breaker trips | back to the previous stage's flags |
| later | `AI_MODEL_FALLBACK=on` | `fallback_from` populated only during incidents | `off` |

**Rollback triggers:** rising `interrupted`/`error` rate, duplicate terminal events, TTFT regression, cost/request spike, any auth/privacy finding, quota inconsistency, wrong routing, quality regression.

## 8. Rollout safety rules
* Everything below stays **disabled** until live Gemini validation passes: `AI_ROUTER_MODE=policy`, `AI_CONTEXT_BUILDER=v2`, `AI_VERIFY_MODE=shadow`, `AI_MODEL_FALLBACK=on`.
* **Cross-provider fallback stays disabled** pending a separate privacy and contractual review. No other provider exists in the code.
* Keep the Gemini key on the **paid tier**: Google's pricing page states free-tier content may be used to improve its products; paid tier is not.

## 9. Tasks that must wait for the paid Gemini credentials
1. Confirm the key is on the paid tier and note the real per-model quotas (RPM/TPM/RPD) for both models.
2. Live-check the final stream chunk's `finish_reason` (resolves audit F-07: a clean close with no finish reason is still treated as success).
3. Confirm that closing the stream actually cancels the upstream request.
4. Measure real TTFT/latency per model and task; real 429/503 payloads and `Retry-After`.
5. Verify 3.1 Flash-Lite thinking behaviour (no level is sent today) and 3.5 Flash-Lite `minimal`/`medium` levels.
6. 20-request mixed validation (short/medium/long/mixed) for the Bug-3 acceptance rule.
7. Measure real `input_tokens`/cost for PDF-heavy chats; calibrate the characters/token estimate.
8. Shadow-run the router and check classification accuracy; shadow-run verification and measure value.
9. Only then consider `policy`, `v2`, verification and fallback.

## 10. Remaining integration gaps — classified honestly

| # | Item | Status |
|---|---|---|
| 1 | Legacy AI paths (quiz generation, titles, notes AI, non-streaming `/api/chat`) use the legacy single model/prompt/retry; they **share the quota/entitlement logic** but not the router/gateway | **Not implemented** (deferred until shadow routing data exists). Entitlement + quota: **Implemented and tested** |
| 2 | Frontend handling of `PLAN_REQUIRED` | **Implemented and tested** (jsdom + real Chromium render) |
| 3 | Gemini stream completion and cancellation | Mock behaviour **implemented and tested**; real behaviour **Blocked by external dependency** (paid key) |
| 4 | Retry budget, deadlines, circuit breaker, admission control | **Implemented and tested** with the mock provider; not exercised against real Gemini |
| 5 | Usage and cost accounting incl. failed/discarded attempts | **Implemented and tested** for streaming chat + shadow verification; other operations record usage as before without per-attempt cost. Cost resolution is $0.000001 (database `numeric(12,6)`) |
| 6 | Single-process health/admission/retry state | **Documented limitation** (not "fixed"): see §12 |
| 7 | Security and user isolation | Isolation by token identity **tested** (no client `user_id`/plan accepted; DB calls use the token's user); RLS itself **not verified live** (Blocked: live Supabase) |
| 8 | Deployment and rollback instructions | **Implemented** (§7); rehearsal against a staging environment **not done** |
| — | Evaluation framework / human review | **Deferred by CEO decision**; infrastructure untouched; the benchmark has never been run |
| — | Quiz answer-key academic verification | **Not implemented** (structure checks only) |

## 11. Test status at hand-over
Counts and the exact commands are in the engineering report delivered with these files.

## 12. Limitations that remain before beta / production
* **Single worker only.** Rate limiter, PDF contexts, quiz definitions, **the retry budget, model health/breaker and admission counters** are per-process. With more than one worker each keeps its own view (a breaker trips per worker; limits are per worker; PDF context is lost between workers). Before horizontal scaling: move PDF/quiz state to shared storage, and rate-limit/retry-budget/health to a shared store (Redis is justified only then). Do not add it before.
* **No capacity claim.** The only load numbers are an in-process mock-provider run (≈37 req/s, bounded by the 40-thread pool). 10,000 simultaneous users and 10,000 in-flight generations are different loads and neither is tested; Gemini paid-tier quotas are unknown.
* **Auth** still makes one Supabase Auth call per authenticated request (decision D-9: revocation-accurate, JWT signing configuration not verifiable here). Revisit with staging latency data.
* **No payment flow exists**; Pro can only be granted in the database.
* Router and verification quality are **unmeasured**; Bangla/English answer quality under the v2 prompts is untested against real answers.
* The CI workflow is written but **has not run on GitHub**; the equivalent commands were run locally.