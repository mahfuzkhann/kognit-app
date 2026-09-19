# Kognit Research Capability (Phase 7C)

Documentation for Kognit's Research capability: Gemini Google Search
grounding integration, the normalized research data model, citation
handling, the research benchmark, and known limitations.

## Status disclosure (read first)

- **No live Gemini research call has ever been made.** This environment
  has no usable `GEMINI_API_KEY`, and its sandboxed network has no
  route to Google's API domains regardless of key validity. Every test
  in this subsystem uses mocked model/judge responses or real
  `google.genai.types` objects constructed directly (not live calls).
- **No research judge has been calibrated against real human scores.**
  `evaluation/research_judge.py` and its calibration recording are
  real, tested infrastructure; no calibration run has actually happened.
  Every judge `trust_status` is `'uncalibrated'` by construction.
- **The research benchmark has 20 items, not the 40-60 originally
  targeted**, all `content_status='draft'`, `verification_status=
  'unverified'`, `partition='dev'`. See `evaluation/seed_research_v1.py`'s
  module docstring for the full disclosure.

## Why Kognit uses Gemini Search grounding (and not a custom search engine)

Kognit does not currently need to own crawling, indexing, ranking,
freshness detection, or source discovery. Gemini's built-in Google
Search grounding tool already does this well, is officially supported,
and requires no additional infrastructure. Building any of the above
now would be exactly the kind of premature infrastructure the approved
architecture explicitly warns against. **This is a deliberate deferral,
not an oversight** - the abstraction boundary (see below) is built so a
different search provider could be introduced later without touching
`backend/main.py` or the frontend, if that ever becomes necessary.

## When research is used

`backend/research_decision.py:decide_research(prompt)` is a small,
disclosed, swappable v1 heuristic - explicitly NOT meant to be the
final intelligence layer (per the approved architecture's own
instruction not to treat a keyword list as final intelligence). It
decides only whether to *enable* Gemini's search tool for a given
message (a cost/latency gate); it is never treated as proof that search
actually happened.

**The ground truth for whether search happened is always the model's
own response** - `grounding_metadata.web_search_queries` non-empty means
Gemini itself decided to and did search. A prompt can pass the
heuristic and still not trigger a real search if Gemini itself decides
the question doesn't need one; this is a `not_used` outcome, never
treated as a failure.

## Normalized research architecture

```
research_decision.decide_research(prompt)
        |
        v  (only if research_requested)
backend.ai_engine.generate_ai_response(..., enable_research=True, return_metadata=True)
        |
        v
raw google.genai.types.GroundingMetadata (or None)
        |
        v
backend.research_models.normalize_grounding_metadata(...)
        |
        v
ResearchResult (provider-neutral: research_used, sources, citations, grounding_status)
        |
        v
backend.research_models.research_result_to_dict(...)  -->  JSON in /api/chat response
```

The rest of Kognit (the frontend, any future evaluator) depends **only**
on `ResearchResult`/`ResearchSource`/`ResearchCitation` — never on raw
`google.genai.types.GroundingMetadata` directly. Switching search
providers later means rewriting `normalize_grounding_metadata()` only.

## Citation model

- `ResearchSource`: one per `GroundingChunk.web` the provider returned
  (`title`, `url`, `domain`, plus `is_url_safe`, computed at construction).
- `ResearchCitation`: one per `GroundingSupport`, with `citation_status`
  distinguishing:
  - `cited_with_source` — chunk indices resolved to real sources.
  - `source_unmapped` — chunk indices present but didn't resolve to a
    real (web) source (e.g. a non-web chunk type).
  - `malformed` — no chunk indices at all.

**Never fabricated:** a citation with no source, or a source with no
grounding, is recorded as exactly that state — never silently upgraded
to look more supported than the provider's own metadata shows.

## Failure behavior

`ResearchResult.grounding_status` has four states:
- `not_used` — the tool was enabled but Gemini genuinely chose not to
  search. Not a failure.
- `used` — search happened, real grounding metadata present.
- `failed` — research was requested, the tool was enabled, but no
  grounding metadata came back at all (a real failure, not fabricated
  as `used`).
- `unavailable` — reserved for a future distinct case (e.g. the
  provider explicitly signals grounding is unsupported for this
  request); not currently produced by the normalizer, since the SDK
  does not yet distinguish this from `failed` in practice.

**An error response (quota exhausted, blocked, etc.) never receives a
research payload at all** — `backend/main.py` only normalizes and
attaches `research` when `ai_result.is_error` is `False`. Kognit can
never imply an error reply was web-grounded.

## Security considerations

- Every citation URL is validated by `is_safe_citation_url()` (rejects
  everything except `http`/`https` with a real host) **twice**: once
  server-side (`research_result_to_dict` drops unsafe sources from the
  payload entirely) and once again client-side
  (`isClientSafeCitationUrl` in `static/js/app.js`) before ever setting
  an `<a href>` — defense in depth against a future backend bug alone
  being sufficient to render an unsafe link.
- Titles/domains are rendered via `textContent`, never `innerHTML` —
  the browser auto-escapes them, so a malicious provider-supplied title
  cannot inject markup.
- Raw provider HTML (`grounding_metadata.search_entry_point.rendered_content`,
  which Google's own grounding response can include) is **never
  rendered**. Kognit builds its own normalized "Sources" list instead.
  (Note: Google's terms for Search grounding may separately require
  displaying their own search-suggestion widget in some contexts — this
  is a known, disclosed product/legal question for Mahfuz to weigh, not
  a technical limitation of this implementation.)
- Research metadata is request-scoped only — it flows through the
  `/api/chat` response and into the browser's own per-chat local
  storage, exactly like an existing bot message. It is never persisted
  to the production Supabase database, never mixed into
  `learning_evidence`, and never crosses between users (it's part of
  the same per-request, per-authenticated-user response the existing
  chat pipeline already isolates).

## The research benchmark

A **separate** SQLite database (`evaluation/data/research_v1.sqlite`,
schema `evaluation/schema/research_schema.sql`) — not mixed into the
Phase 7B Answer Quality benchmark, per the approved architecture. Same
architectural discipline as Phase 7B: immutable versioned items, a
separate verification-gate table, immutable dataset-version lockfiles,
append-only results, no forced single score.

**One genuinely research-specific rule:** for any item whose
`freshness_requirement` is not `'timeless'`, `research_authoring.py`
refuses to mark it `verified_correct` without a `verified_as_of_date` —
a current-information fact is only ever true as of a specific date,
never eternally.

### Evaluation dimensions

Deterministic (`evaluation/research_evaluators.py`):
`research_decision_correctness`, `citation_url_validity`,
`citation_mapping_validity`, `citation_coverage`, `source_count`,
`search_used_consistency`.

LLM-judge only (`evaluation/research_judge.py`, uncalibrated —
see Status disclosure): `source_relevance`, `claim_grounding`,
`citation_correctness` (semantic half), `citation_completeness`.

## Known limitations

- No live verification has been performed (see Status disclosure).
- The research-decision heuristic is intentionally simple and will
  produce both false positives and false negatives — this is disclosed,
  expected, and exactly what the benchmark's
  `research_decision_correctness` dimension exists to measure over time.
- No rendered-output/browser-level check of the Sources UI exists (same
  limitation as Phase 7B's formatting evaluator).
- Google's own grounding usage terms (display requirements for their
  search-suggestion widget) have not been legally reviewed here — noted
  above as a disclosed open question for Mahfuz.

## Future migration path

If another search provider is ever needed, only
`backend/research_models.py:normalize_grounding_metadata()` (and a new
provider-specific call site alongside `GeminiAdapter`-style wiring in
`ai_engine.py`) would need to change — `main.py`, the frontend, and the
entire evaluation/benchmark layer depend on `ResearchResult` only, never
on Gemini-specific structures.
