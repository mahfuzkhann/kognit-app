# Kloriz AI Engine 2.0 — implementation, operations and rollback

Status: **implemented and mock/integration-tested; NOT live-validated, NOT load-certified.**
Baseline audited: `main` @ `b2e7abf` (re-verified unchanged before work began).
Reference: `KLORIZ_AI_ENGINE_2_0_COMPLETE_XRAY_AUDIT.md`.

> Read §12 ("What is NOT done / NOT proven") before launching. Several items below are
> architecture + tests, not measured production behaviour.

---------------------------------------------------------------------------------------------

## 1. What changed (one paragraph per area)

| Area | Change | Default behaviour |
|---|---|---|
| **Model registry** (`backend/ai/registry.py`) | The two approved Gemini models with verified capabilities, limits and a versioned price table. | Present, used for cost + routing. |
| **Router** (`backend/ai/router.py`) | Deterministic, explainable, plan-aware routing. | `AI_ROUTER_MODE=static` → **no change** (legacy model). |
| **Gateway policy** (`backend/ai/gateway.py`) | Failure-policy table, process-wide retry budget, per-request deadlines, in-provider model fallback, bounded admission. Plugged into the existing attempt loop in `ai_engine.py`. | Deadlines + budget **on** (`AI_GATEWAY=v2`); fallback **off**. |
| **Health / breaker** (`backend/ai/health.py`) | Rolling per-model health and circuit breaker (in-process). | On; only influences routing/fallback when those flags are on. |
| **Cost** (`backend/ai/cost.py`) | Cost from provider-reported tokens × verified prices; NULL when unknowable. | On. |
| **Context builder v2** (`backend/ai/prompts.py`) | Layered prompt (curriculum/class/task/Socratic), document as delimited untrusted *user* content, document budget. | `AI_CONTEXT_BUILDER=legacy` → **no change**. |
| **Shadow verification** (`backend/ai/verify.py`) | After the answer is final, an independent check records a verdict. Never visible to the student. | `AI_VERIFY_MODE=off`. |
| **Entitlements** (`backend/entitlements.py`) | One module for Free/Pro rules; plan comes from the database. | `PLAN_GATING=enforce`. |
| **Plans/quotas** (migration 0014) | Pro limits seeded; `get_my_plan()`; note-quizzes spend the quiz allowance. | Applies when migration is applied. |
| **Per-attempt usage** | Every provider attempt (incl. a discarded partial) gets a row with model, attempt kind, route, cost. | On. |
| **Curriculum** | `student_profiles.curriculum` (`nctb_bangla` / `nctb_english`), required in the profile form, passed to the prompt. | Prompt uses it only with `AI_CONTEXT_BUILDER=v2`. |
| **Pooled HTTP** (`backend/http_client.py`) | One keep-alive client per event loop instead of one per call. | On. |
| **Dependency** | `pypdf` 6.21.2 → 6.21.4; `pip-audit`: *no known vulnerabilities*. | On. |
| **CI** | `.github/workflows/ci.yml` (not executed here — see §12). | — |

The NDJSON stream contract (`start`/`delta`/`retry`/`done`/`interrupted`/`error`) is **unchanged**.
`backend/ai_engine.py` keeps every public name; the Bug-3 semantics (explicit completion, one bounded
recovery, partial output discarded, 429 not retried) are preserved and covered by the original tests.

## 2. Model routing (approved models, verified 2026-11-03)

Verified from Google's official documentation (`ai.google.dev/gemini-api/docs/models/...`, `/thinking`, `/pricing`):

| | `gemini-3.1-flash-lite` (general) | `gemini-3.5-flash-lite` (advanced) |
|---|---|---|
| Status | Stable | Stable |
| Inputs | text, image, video, audio, PDF | text, image, video, audio, PDF |
| Context / max output | 1,048,576 / 65,536 | 1,048,576 / 65,536 |
| Streaming, structured output, search grounding, thinking | yes | yes |
| Documented thinking levels | **not enumerated** → engine sends none | minimal, low, medium, high (default minimal) |
| Price per 1M tokens (Standard, paid) in / out / cached-in | $0.25 / $1.50 / $0.025 | $0.30 / $2.50 / $0.03 |
| Sampling params | — | **temperature/top_p/top_k removed** (engine sends none) |

Output price includes thinking tokens. Google Search grounding is billed separately per query and is **not** in `estimated_cost_usd`.

Rules (first match wins; policy `2026-11-03.r1`):

1. Image or uploaded document → **advanced** (`multimodal_or_document`).
2. Class 9–12 mathematics / physics / chemistry / numerical commerce / reasoning-heavy ICT → **advanced** (`advanced_<category>`).
3. Otherwise: **Free → general**; **Pro → 70% advanced / 30% general**, by a stable hash of the request id (`pro_distribution_*`).
4. Task suitability always beats the 70/30 split.
5. If the chosen model's breaker is open and the other is capable → the other (`+health_alternative`).

The subject classifier is a **transparent keyword heuristic** (English + Bangla). Its accuracy is *unmeasured*; run `AI_ROUTER_MODE=shadow` first and compare `route_reason`/`task_category` against reality before `policy`.

## 3. Reliability behaviour

| Failure | Before first token | After partial text |
|---|---|---|
| 400/404/413 | fail, no retry | interrupted |
| 401/403 | fail, no retry, **does not** trip breaker | interrupted |
| 429 | **never retried on the same model.** With `AI_MODEL_FALLBACK=on`: one attempt on the other approved model, then fail | interrupted |
| 5xx / 409 | retry ≤3 attempts, jittered backoff, budget- and deadline-gated | **one** recovery generation (partial discarded, never concatenated); else interrupted |
| timeout / connect | retry ≤3, budget- and deadline-gated | interrupted |
| unclassified | retry **once** (was up to 3) | interrupted |
| deadline | no attempt starts if <0.5 s remain; no retry if <2 s remain | answer ends `interrupted` |

* **Deadlines:** first-token `AI_FIRST_TOKEN_DEADLINE_SECONDS` (75), whole answer `AI_STREAM_DEADLINE_SECONDS` (240). The whole-answer deadline is checked **between events**, so a stalled stream ends at most one SDK read-timeout (30 s) later.
* **Retry budget:** a process-wide token bucket (burst 30, +3/s). An outage can no longer multiply provider load by ≈3×.
* **Admission control:** `AI_MAX_INFLIGHT_GENERATIONS` (32) running, `AI_MAX_QUEUED_GENERATIONS` (64) waiting ≤ `AI_QUEUE_WAIT_SECONDS` (8). Beyond that the student gets the normal "busy" message **and the chat unit is refunded**. Keep in-flight **below** the thread-pool size (default 40).
* **Terminal event:** exactly one of `done|interrupted|error` is asserted by 36 scenario tests (all failure kinds × router modes × fallback on/off).

## 4. Streaming / frontend

No protocol change. Verified unchanged: the stream-render (57), stop-generation (45) and quota-response (12) jsdom suites pass. **Not verified here:** real-browser rendering of MathJax/tables/code during streaming (puppeteer suites were not run).

## 5. Prompt and context architecture (`AI_CONTEXT_BUILDER=v2`)

Layers appended to the unchanged core rules: **curriculum** → **class level** → **task** (maths/physics/chemistry/commerce/image/document) → **Socratic protocol** → **document rule**. Versioned (`PROMPT_LAYERS_VERSION`). Layers contain **no student free text**.

* Curriculum comes only from the verified profile. Unrecorded or unsupported → "medium not recorded"; the AI is told **not** to assume one.
* **Documents** are wrapped in `<<<UNTRUSTED_DOCUMENT>>> … <<<END_UNTRUSTED_DOCUMENT>>>` and sent as **user** content, never in the system instruction; nested markers are neutralised; the only trusted text about them is the document rule.
* **Budget:** `AI_CONTEXT_DOC_MAX_CHARS` (default **120,000** chars, down from 300,000). The 2.0 chars/token estimate is a deliberately conservative **hypothesis**; the provider's own token counts are authoritative.
* **Cost warning:** the document is resent on every turn. A Pro student could send 20 such turns/day. Measure real `input_tokens` in `ai_usage_events` before launch; prefix caching is not explicitly used.
* The non-streaming `/api/chat` path and quiz/title/notes generation still use the legacy single model and prompt (see §12).

## 6. Verification (shadow only)

`AI_VERIFY_MODE=shadow`: after `done` has been sent, an independent task asks the advanced model to recompute the answer. The verdict (`correct|incorrect|unsure|error`) goes **only** to `ai_usage_events` (`operation='verify'`, tokens, latency, cost) — never to the student. Eligible: mathematics, physics, chemistry, numerical commerce. The verifier is an LLM and can be wrong; its output is a **measurement input**. There is **no** student-facing mode in the code.

## 7. Plans and entitlements

| | Free | Pro |
|---|---|---|
| Chat | 3 on first login day, then 1/day | 20/day |
| Quiz | **3 per week** (see ambiguity) | 15/day (note-generated quizzes count here) |
| Notes: save | yes | yes |
| Notes: suggest tags / titles | yes (`notes_ai` 6/day) | yes |
| Notes: flashcards, quiz from notes | **403 `PLAN_REQUIRED`** | flashcards: `notes_ai` guard 200/day; quiz: shares the 15/day |
| Learning insights / snapshot / mistakes / next-steps / topic matrix | **403 `PLAN_REQUIRED`** | yes |
| Models | both, by task | both, 70/30 where suitable |

Enforcement is server-side: the plan comes from `public.get_my_plan()` (reads `subscriptions` for the verified JWT), the client cannot supply it, a Free request is refused **before** any quota unit or provider call, and a plan-lookup failure fails **closed** (503). `PLAN_GATING=off` is the rollback.

**Ambiguities (configurable, need your confirmation):**
1. Free quiz reset period is unspecified; the shipped policy (3 per **week**, Asia/Dhaka) is kept. Change with `update public.plan_limits set period='day' where plan='free' and kind='quiz';`.
2. "Unlimited flashcards" is an integer meter: Pro `notes_ai` = 200/day is an abuse guard.
3. The five gated learning endpoints are my interpretation of "advanced learning profile / matrix / student intelligence". The basic profile, quiz history and note-saving remain free.
4. **The frontend does not yet show an upgrade prompt for `PLAN_REQUIRED`** — Free students will see the generic failure message on those actions/panels. Needs a UX decision.

## 8. Authentication and HTTP clients (decision D-9)

**Decision: keep per-request Supabase Auth verification (Option A); pool the HTTP clients.**
Reasons: (1) it is the only option that is revocation-accurate; (2) local JWKS verification is only valid if the project uses **asymmetric signing keys**, which cannot be verified from the repository or the sandbox, and a wrong assumption would weaken authentication; (3) pooling removes the dominant cost (a fresh TLS handshake per call) without changing the security model. **Trade-off:** each authenticated request still makes one Supabase Auth round trip and inherits Auth's availability/rate limits. **Revisit** with real staging latency numbers and after confirming the project's signing configuration in the Supabase dashboard; if asymmetric, JWKS verification (signature, `iss`, `aud`, `exp`, algorithm allow-list, cached key refresh) becomes a measured option.

Pooled clients: one per (event loop, timeout), limits 100 / 40 keep-alive, no stored credentials, closed on shutdown; a test double replacing `httpx.AsyncClient` is honoured.

## 9. Security and privacy

* No secrets in code/logs; `pip-audit` clean after the `pypdf` bump; secret-pattern scan clean.
* Usage rows hold labels and numbers only — a test proves no prompt/answer text appears in them.
* **Verify your Gemini API key is on the PAID tier before real students use Kloriz.** Google's pricing page states that on the **free tier content may be used to improve Google's products; on the paid tier it is not.** Student prompts and uploaded documents must never go through a free-tier key.
* Cross-provider fallback does not exist. Fallback is same-provider, other approved model only, default off.
* Student data minimisation: `curriculum` is one enumerated column; no new free-text storage.

## 10. Database (migration `0014_ai_engine_2_plans_usage_curriculum.sql`)

Additive and idempotent (re-apply tested). **Apply it BEFORE deploying the application** (the profile read selects the new `curriculum` column).
Contains: Pro `plan_limits` rows; `get_my_plan()`; per-attempt usage columns + widened constraints (`verify` operation); `_usage_numeric()`; the extended `record_ai_usage_events()`; `student_profiles.curriculum` with a CHECK.

Rollback of the schema (usually unnecessary — flags suffice): see the comment block at the top of the migration.

## 11. Rollout, monitoring and rollback

| Stage | Setting | Watch |
|---|---|---|
| 1 | Deploy code + migration. Defaults ⇒ legacy behaviour (plus deadlines/budget/pooling/per-attempt usage) | error codes, `interrupted` rate, TTFT |
| 2 | `AI_ROUTER_MODE=shadow` | `route_reason` / `task_category` distribution vs reality |
| 3 | Founder accounts: `AI_ROUTER_MODE=policy`, `AI_CONTEXT_BUILDER=v2` | answer quality by hand; `input_tokens` |
| 4 | `AI_VERIFY_MODE=shadow` (sampled) | verdict rates, added cost |
| 5 | Beta cohort, then everyone | cost/request, 429/503 rates, breaker trips |
| later | `AI_MODEL_FALLBACK=on` only after the paid-tier quotas are known | fallback rate |

**Rollback triggers:** rising `interrupted`/`error` rate, duplicate terminal events, TTFT regression, cost/request spike, any authentication or privacy finding, quota inconsistency, wrong routing, or quality regression.
**Rollback:** `AI_ROUTER_MODE=static`, `AI_CONTEXT_BUILDER=legacy`, `AI_VERIFY_MODE=off`, `AI_MODEL_FALLBACK=off`, `AI_GATEWAY=legacy`, `PLAN_GATING=off` (no deploy needed for an env-only change on most hosts). Old and new code paths write the same usage table; new columns are nullable, so a code rollback is safe.

## 12. What is NOT done / NOT proven (be strict about this)

* **No live Gemini call was made.** Unproven live: that the final stream chunk always carries `finish_reason=STOP` (a clean close with *no* finish reason is still treated as success — audit F-07), that closing the iterator cancels the upstream HTTP stream, real TTFT/latency, 3.1 Flash-Lite thinking-level behaviour, real 429/503 payloads.
* **No capacity claim.** The only load numbers are an in-process mock-provider run (≈37 req/s ceiling bound by the 40-thread pool). 10,000 simultaneous *users* and in-flight *generations* are different loads and neither has been tested. Gemini quota for the paid tier is unknown.
* **Single worker only.** Rate limiter, PDF contexts, quiz definitions **and now the retry budget, health tracker and admission counters** are per-process. With more than one worker each keeps its own view. A shared store is justified only when you run >1 worker.
* **Evaluation (G4) is not built.** No human-review workflow tables, no verified benchmark items, no calibrated judge; the harness still calls the non-streaming path and `evaluation/judge.py` still imports `google.genai` directly. Without this, router accuracy and verification benefit **cannot be measured** — this is the main remaining pre-launch risk to answer quality.
* Router/verification quality is **unmeasured**; both ship in shadow-capable, default-off form.
* Quizzes, titles, notes AI and the non-streaming `/api/chat` do not use the router/new prompts (legacy single model, legacy retry). Per-attempt usage and cost apply to the streaming chat path and shadow verification only.
* Chat answers are **not** structured-output validated; quiz answer keys are structure-checked only (not academically verified).
* Frontend: no upgrade prompt for `PLAN_REQUIRED`; curriculum selection exists but copy/design were not visually reviewed in a real browser.
* CI workflow is written but **was not executed** (no GitHub runner here); the equivalent commands were run locally.
* Pro limits and Free quiz period need your confirmation (§7).
* English Version / Bangla Medium **prompt quality** is untested against real answers.

## 13. Test status at hand-over

See the engineering report delivered with these files for exact counts and commands.