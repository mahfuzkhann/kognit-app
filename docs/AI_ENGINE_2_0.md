# Kloriz AI Engine 2.0 — implementation, operations and rollback

**Status (read this first)**

| Dimension | Status |
|---|---|
| Code implementation | **Complete** for the approved scope (see §10 for the items that are deliberately *not* implemented) |
| Automated tests | **Passed** (counts in §11). Mock/integration only. |
| Database integration | **Passed** against a real PostgreSQL 16 (migrations, quota behaviour, 24-way concurrency) |
| Frontend integration | **Passed** in jsdom **and** real Chromium 153 |
| Live Gemini integration | **Paid key now configured by the CEO. NOT yet verified: the live smoke test (`tools/live_gemini_smoke.py`, §13) has not been run against the real API** |
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

**Final plan override (migration 0016, `docs/PLANS_AND_QUOTAS.md`): the advanced model is a Pro entitlement. A Free request is always routed to the general model, whatever the rule below says, and a Free request has no fallback model (the only other model is the advanced one). Non-streaming chat, quizzes, titles and notes AI obey the same rule.** Routing (first match; policy `2026-11-03.r1`): image/PDF → advanced · Class 9–12 maths/physics/chemistry/numerical commerce/reasoning ICT → advanced · otherwise Free → general, Pro → ≈70% advanced / 30% general by a stable hash of the request id · suitability always beats the split · an open breaker diverts to the other capable model. The subject classifier is a **keyword heuristic with unmeasured accuracy** — run `shadow` first.

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

* **What the student is told:** Pro = "20x more usage limit · 35x more quizzes than Free plan · **Unlimited flashcards** · Quizzes and flashcards from your saved notes · Learning insights and your learning matrix · Personalised next steps and mistake analysis". Tests assert this list is identical in the backend (`entitlements.PRO_FEATURE_LIST`), the upgrade UI (`feedback.js`) and the Plan screen (`i18n.js`). **The 200/day guard appears in no student-facing text** (also asserted).
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
select plan, kind, period, limit_count from public.plan_limits order by plan, kind;   -- (after 0016) pro chat 20/day, quiz 5/day, photo_solve 5/day, pdf_upload 2/day, notes_ai 200/day, flashcards 200/day; free chat 1(3), quiz 1/week, photo_solve 1 lifetime, notes_ai 6/day; NO free flashcards / pdf_upload row
select public.get_my_plan();                                                           -- as a signed-in student: 'free' (or 'pro')
select column_name from information_schema.columns where table_name='student_profiles' and column_name='curriculum';
```

### 7.2 Settings (all optional; defaults reproduce pre-2.0 behaviour — see `.env.example`)
Required secrets exist already (`GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `USAGE_WRITE_KEY`, `QUOTA_MODE`). **Never** put key values in code, the frontend, logs, docs, commits or chat. Flags: `AI_ROUTER_MODE`, `AI_CONTEXT_BUILDER`, `AI_VERIFY_MODE`, `AI_MODEL_FALLBACK`, `AI_GATEWAY`, `PLAN_GATING`, plus the numeric limits in `.env.example`.

### 7.3 Rollout order, how to verify, how to roll back

> **Superseded by §14 (CEO decision, 2026-11-17):** Kloriz launches with `policy` routing and approved-model fallback ON as the code defaults, without the staged shadow rollout below. The table is kept as the long-form reference for how each switch is verified and rolled back.

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

> **Updated by §14:** `AI_ROUTER_MODE=policy` and `AI_MODEL_FALLBACK=on` are now the defaults (CEO decision). `AI_CONTEXT_BUILDER=v2` and `AI_VERIFY_MODE=shadow` remain OFF.
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

## 13. Finalization audit — 2026-11-17 (scope: AI Engine 2.0 only; Phase 10 is NOT started)

Baseline: `main` @ `341ac2b`. Official Google Gemini API is the only provider; no OpenRouter/OpenAI/Anthropic code exists or was added.

### 13.1 What was verified today
| Item | Result |
|---|---|
| Model ids `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite` (the only two approved models) | Listed as **Stable** on Google's Models page (page dated 2026-10-28). 3.5 Flash-Lite model page: inputs text/image/video/audio/PDF, 1,048,576 in / 65,536 out, thinking, search grounding and structured output supported |
| Prices (Standard paid tier, per 1M tokens) | **Confirmed 2026-11-17 on Google's official page `aistudio.google.com/docs/pricing`**: `gemini-3.1-flash-lite` input $0.25 (audio $0.50), output $1.50, cached input $0.025; `gemini-3.5-flash-lite` input $0.30, output $2.50, cached input $0.03. Output prices include thinking tokens. Both match `backend/ai/registry.py` exactly. Not modelled (not used by Kloriz): audio input, context-cache storage ($1.00/M tokens/hour), Google Search grounding ($14 per 1,000 searches after 5,000 free per month, shared across Gemini 3+ models) |
| Installed SDK `google-genai==2.20.0` | `ThinkingLevel` has MINIMAL/LOW/MEDIUM/HIGH; the adapter's config (thinking level, search tool, timeout) builds correctly offline for every level; `chats.create(model, config, history)` signature matches the adapter |
| Test suite | See 13.5 (final count after the approved-model correction) |
| Secret scan (working tree + history for key patterns) | No secrets found |

### 13.2 Changes made in this pass (all small, additive, reversible)
1. `backend/providers/gemini.py` — a missing or blank `GEMINI_API_KEY` now raises a clear `RuntimeError` naming the setting (previously the SDK's generic `ValueError`). The value is never echoed.
2. `backend/ai_engine.py` — the per-request `stream_telemetry` log line now prints the model actually used. It always printed the legacy `MODEL_NAME`, which was wrong whenever the router chose another model. Usage rows were already correct.
3. `tools/live_gemini_smoke.py` (new) — bounded live test, 7 short calls, metadata-only output, `--dry-run` for checking the script itself.
4. `test_ai_engine_finalization.py` (new) — 8 tests for the above and for the "exactly one terminal event" contract.

### 13.3 Findings that are NOT fixed here (need a decision or live data)
| # | Finding | Priority |
|---|---|---|
| 1 | ~~Default config prices nothing~~ — **closed in 13.5**: the static default is now an approved, priced model | closed |
| 2 | Audit F-07 still open: a stream that closes with no `finish_reason` counts as success. Only the live smoke (check 3) can tell whether Gemini always sends STOP | P1, needs live run |
| 3 | Upstream cancellation on client disconnect is by garbage collection of the synchronous generator; the provider call can continue until its next chunk and the admission slot is released earlier. Bounded, but unconfirmed live | P2 |
| 4 | ~~Streaming research metadata labelled the provider model with the legacy `MODEL_NAME`~~ — **fixed in §14**: the `done` event now names the model that answered | closed |
| 5 | Quiz generation, titles, notes AI and non-streaming `/api/chat` still use the legacy single-model path (unchanged from §10 item 1) | P2, deferred by design |
| 6 | Models newer than the approved pair exist on Google's list. Not evaluated and not approved; the approved set is exactly two models and changes only on the CEO's decision backed by the benchmark | Info |
| 7 | Everything in §12 (single worker, no capacity claim, no live RLS check) still stands | — |

### 13.4 Readiness assessment
**READY FOR INTERNAL TESTING.** Not yet *controlled beta*: that requires the live smoke test to pass and a staged `shadow` run (§7.3).

### 13.5 Approved-model correction — 2026-11-17 (CEO instruction)
**Kloriz is approved to use exactly two Gemini models: `gemini-3.1-flash-lite` and `gemini-3.5-flash-lite`. `gemini-3.6-flash` is NOT approved.** The first pass of this section wrongly treated 3.6 Flash as the legacy default; that is withdrawn.

Every reference was audited. Runtime references (all removed or changed):
| File | Was | Now |
|---|---|---|
| `backend/ai_engine.py` | `DEFAULT_MODEL_NAME = "gemini-3.6-flash"`; `GEMINI_MODEL` accepted any plausible id | Default `gemini-3.5-flash-lite`; `GEMINI_MODEL` accepts only the two approved ids (anything else is ignored with a warning). Legacy reasoning level is sent only if the registry documents it for the model |
| `backend/ai/registry.py` | `AI_MODEL_GENERAL` / `AI_MODEL_ADVANCED` could name any model | Only the role's own approved id is ever returned; any other value is ignored with a warning. Router, fallback (`other_role`) and gateway all read this registry, so they cannot reach an unapproved model |
| `.env.example` | Documented 3.6 Flash as default/example | Documents the two approved models and the default |
| `docs/DEPLOYMENT.md` | "defaults to the built-in model id" | States the approved set and the default |

Why `gemini-3.5-flash-lite` is the static default: Google documents its thinking levels (minimal/low/medium/high), so the fixed chat level `low` is valid on it; the registry sends no thinking level to `gemini-3.1-flash-lite` because none are documented. If an operator selects 3.1 via `GEMINI_MODEL`, the legacy path therefore sends no thinking level. Changing the default is one constant (`DEFAULT_MODEL_NAME`).

Cost tracking: in static mode every usage row now carries a real `estimated_cost_usd` (both models are priced from the confirmed rates above). It is an estimate from reported token counts, not the billed amount; Google Search grounding fees are not included. No 3.6 Flash price exists anywhere in the code.

Fixture-only mentions of `gemini-3.6-flash` that remain (no runtime effect): dummy `judge_model` / `provider_model` / mocked `model_version` strings in `evaluation/tests/*` and `test_research_*.py`, and one negative cost test that asserts the model is unpriced.

Tests: `test_ai_engine_finalization.py` pins the approved set, the override lock, the default, `GEMINI_MODEL` rejection of 3.6 Flash, routing and fallback staying inside the pair (including with hostile environment overrides), static-mode requests reaching the provider with an approved model, real cost rows, the confirmed per-model prices, and the reasoning-level gate.

## 14. FINAL launch configuration and closure — 2026-11-17 (CEO decision)

**Decision:** Kloriz launches in December with `AI_ROUTER_MODE=policy` and `AI_MODEL_FALLBACK=on` as the built-in defaults. No staged rollout, no `.env` edits required: a deployment with no `AI_*` variables behaves as below. Phase 10 is not started.

### 14.1 Final configuration (all defaults live in `backend/ai/config.py`)
| Setting | Value | Why |
|---|---|---|
| `AI_ROUTER_MODE` | **policy** | Router picks the model per request: `gemini-3.1-flash-lite` for general text, `gemini-3.5-flash-lite` for images, PDFs and demanding subjects; Pro users send 70 % of requests where both models suit to the advanced model |
| `AI_MODEL_FALLBACK` | **on** | One pre-token switch to the OTHER approved model on a 429 or an open circuit breaker. The two models have separate quotas, so this absorbs most quota hits. Never a third model, never another provider |
| `AI_CONTEXT_BUILDER` | legacy (off) | The v2 prompt builder has never been tried on real Bangla/English answers; it is not needed to launch |
| `AI_VERIFY_MODE` | off | Shadow verification adds a second paid model call to every answer and changes nothing the student sees |
| `AI_GATEWAY` | v2 (reliability controls on) | Deadlines, retry budget, circuit breaker, admission control |
| `PLAN_GATING` / `QUOTA_MODE` | enforce | Free/Pro limits enforced in the database |
| `GEMINI_MODEL` | gemini-3.5-flash-lite | Used by quiz generation, titles, notes AI and the non-streaming chat path (which are not routed) |

On startup the log prints one line, `ai_engine config: router_mode=policy model_fallback=on ...`, so the active configuration is visible without reading any file.

### 14.2 Rollback (one variable, no redeploy of code)
* Back to the single-model path: `AI_ROUTER_MODE=static` and `AI_MODEL_FALLBACK=off`.
* Back to the pre-2.0 attempt loop entirely: also `AI_GATEWAY=legacy`.

### 14.3 What this pass changed
1. `backend/ai/config.py` — defaults `policy` / fallback `on`.
2. `backend/ai_engine.py` — `StreamChunk` has a new optional `model` field, set on `done`, naming the model that actually answered (the routed model, or the other approved model after a fallback). The usage log line prints that model. One startup line prints the active AI configuration.
3. `backend/main.py` — streamed research answers are labelled with the model that answered, not the legacy default.
4. `conftest.py` — the many older tests that exercise the single-model loop run with `static` / fallback `off` pinned by an autouse fixture; any test can override it. `TestProductionDefaults` deletes both variables and proves the real defaults (routing per task, 429 recovery on the other approved model, exactly two attempts when both models are limited, one-variable rollback).

### 14.4 Closure status — read this honestly
* **Code complete for the AI Engine 2.0 scope:** routing, two-model-only registry, fallback, reliability controls, usage and cost accounting, entitlement hooks, telemetry. Automated tests: see the delivery report (all passing, including the full suite and the 72-test smoke suite).
* **Not verified against the real Gemini API.** Nothing in this repository has ever been run against it, and the assistant's environment cannot reach it. The router's classification accuracy and answer quality on real Bangla/English questions are unmeasured. This is a limitation, not a claim of failure.
* **One command closes that gap before launch:** `python tools/live_gemini_smoke.py` (7 short calls, well under US$0.01, metadata only). All 7 lines must say PASS. Then run it once more with `--policy`. If any line says FAIL, do not launch; send the output.
* **Known limits that remain by design:** single worker only; no capacity number beyond a mock run; audit F-07 (a stream that closes with no finish reason counts as success); no payment flow yet.