#!/usr/bin/env python3
"""
Kloriz AI Engine 2.0 - bounded LIVE Gemini smoke test.

WHY THIS EXISTS
    The automated suite proves the engine against a deterministic mock provider.
    It cannot prove how the REAL Gemini API behaves. This script exercises the real
    attempt loop (backend.ai_engine.stream_ai_response) against the real Gemini API
    with a small, fixed number of short synthetic prompts, and answers the open
    questions listed in docs/AI_ENGINE_2_0.md section 9:

      * does the final stream chunk carry finish_reason (audit F-07)?
      * real time-to-first-token and total latency per approved model
      * real usage metadata (input / output / thinking tokens) and estimated cost
      * image input works
      * a 'document' (text context) request works
      * a bad model id is a fast, non-retried, safely-reported failure
      * closing a stream mid-way returns control promptly

HOW TO RUN (on your own machine, where .env holds your real key)
      python tools/live_gemini_smoke.py                 # live, 8 requests
      python tools/live_gemini_smoke.py --dry-run       # no network, mock provider (checks the script itself)
      python tools/live_gemini_smoke.py --policy        # also route by policy (3.1 / 3.5 Flash-Lite)

COST AND SAFETY
    * At most 8 short model calls (default). Expect well under US$0.01 in total.
    * Prompts are synthetic. No student data, no Supabase, no network call except Gemini.
    * The API key is read from the environment / .env and is NEVER printed or written.
      Only metadata is reported: status, latency, token counts, finish reasons.
    * Answer text is NOT printed (only its length), so the report is safe to paste into a chat.
    * Exit code 0 = every check passed, 1 = at least one failed, 2 = not configured.
"""
import argparse
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--dry-run", action="store_true", help="use the mock provider; no network, no key needed")
parser.add_argument("--policy", action="store_true", help="set AI_ROUTER_MODE=policy for this process")
parser.add_argument("--out", help="optionally write the metadata-only JSON report to this path")
args = parser.parse_args()

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, ".env"))
except Exception:  # python-dotenv missing is not fatal; the environment may already be set
    pass

if args.dry_run:
    os.environ.setdefault("GEMINI_API_KEY", "dry-run-placeholder-not-a-real-key")
    os.environ.setdefault("SUPABASE_URL", "https://dry-run.invalid")
    os.environ.setdefault("SUPABASE_ANON_KEY", "dry-run-placeholder")
elif not (os.getenv("GEMINI_API_KEY") or "").strip():
    print("NOT CONFIGURED: GEMINI_API_KEY is not set (looked in the environment and .env).")
    sys.exit(2)
if args.policy:
    os.environ["AI_ROUTER_MODE"] = "policy"

from PIL import Image, ImageDraw  # noqa: E402

from backend import ai_engine  # noqa: E402
from backend.ai import cost, registry  # noqa: E402
from backend.providers import (  # noqa: E402
    FinishReason, ProviderError, ProviderErrorKind, ProviderRequest, ProviderUsage, get_provider, set_provider,
)
from backend.providers.mock import MockProvider, failing_attempt, text_attempt  # noqa: E402

TERMINALS = ("done", "interrupted", "error")
RESULTS = []


def mock_usage():
    return ProviderUsage(prompt_tokens=120, output_tokens=80, thoughts_tokens=10, total_tokens=210)


def use_mock(*attempts):
    set_provider(MockProvider(list(attempts)))


def tiny_png(text: str) -> bytes:
    img = Image.new("RGB", (240, 80), "white")
    ImageDraw.Draw(img).text((12, 30), text, fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def record(name, passed, **meta):
    RESULTS.append({"check": name, "passed": bool(passed), **meta})
    print(f"[{'PASS' if passed else 'FAIL'}] {name}  " + json.dumps(meta, default=str))


def run_engine(name, prompt, *, expect_text=True, **kw):
    """Drive the REAL attempt loop and report metadata only."""
    t0 = time.perf_counter()
    first = None
    chunks = []
    try:
        for chunk in ai_engine.stream_ai_response(
            prompt, request_id=f"live-{name}", user_class=kw.pop("user_class", "Class 9-10 (SSC)"),
            stream=kw.pop("stream", "Science"), **kw,
        ):
            if chunk.kind == "text" and first is None:
                first = time.perf_counter() - t0
            chunks.append(chunk)
    except Exception as exc:  # the engine must never raise
        record(name, False, problem="engine raised", exception=type(exc).__name__)
        return None
    total = time.perf_counter() - t0
    terminals = [c for c in chunks if c.kind in TERMINALS]
    term = terminals[-1] if terminals else None
    usage = getattr(term, "usage", None)
    model = ai_engine.MODEL_NAME
    meta = {
        "terminal_events": len(terminals),
        "terminal": term.kind if term else None,
        "ttft_s": round(first, 2) if first is not None else None,
        "total_s": round(total, 2),
        "answer_chars": len(term.text or "") if term else 0,
        "text_chunks": sum(1 for c in chunks if c.kind == "text"),
        "retry_events": sum(1 for c in chunks if c.kind == "retry"),
    }
    if usage is not None:
        meta.update(prompt_tokens=usage.prompt_tokens, output_tokens=usage.output_tokens,
                    thoughts_tokens=usage.thoughts_tokens, total_tokens=usage.total_tokens)
        if args.policy:
            # The routed model is not exposed on the terminal chunk, so pricing it here would be a guess.
            meta["estimated_cost_usd"] = "see ai_usage_events (routed model differs per request)"
        else:
            est = cost.estimate(model, usage)
            meta["estimated_cost_usd"] = est.usd
            meta["cost_basis"] = est.basis
    ok = len(terminals) == 1 and (term.kind == "done" if expect_text else True)
    record(name, ok, **meta)
    return term


def main() -> int:
    print(f"Kloriz live Gemini smoke  dry_run={args.dry_run}  router_mode="
          f"{os.getenv('AI_ROUTER_MODE', 'static')}  legacy_model={ai_engine.MODEL_NAME}")
    general = registry.get_model(registry.ROLE_GENERAL).model_id
    advanced = registry.get_model(registry.ROLE_ADVANCED).model_id
    print(f"approved models: general={general}  advanced={advanced}\n")

    # 1. Simple academic question (general text)
    if args.dry_run:
        use_mock(text_attempt("A short answer. ", "Done.", usage=mock_usage()))
    run_engine("1_simple_question", "In one sentence: what is photosynthesis?")

    # 2. Complex reasoning (routes to the advanced model under --policy)
    run_engine("2_complex_reasoning",
               "A ball is thrown upward at 20 m/s. Take g = 10 m/s^2. Find the maximum height and the total time "
               "of flight, showing each step briefly.", user_class="Class 11-12 (HSC)")

    # 3. Streaming completion semantics: look at the provider's FINAL event directly (resolves audit F-07)
    probe_t0 = time.perf_counter()
    if args.dry_run:
        use_mock(text_attempt("One ", "two ", "three.", usage=mock_usage()))
    events, probe_first = [], None
    try:
        req = ProviderRequest(system_instruction="Answer in one short sentence.",
                              user_parts=["Name the capital of Bangladesh."], history=[], model=ai_engine.MODEL_NAME,
                              timeout_seconds=30, reasoning_effort=None, enable_web_search=False, request_id="live-3")
        for ev in get_provider().stream_chat(req):
            if ev.text and probe_first is None:
                probe_first = time.perf_counter() - probe_t0
            events.append(ev)
        finishes = [ev.finish_reason for ev in events if ev.finish_reason is not None]
        last_finish = finishes[-1] if finishes else None
        record("3_final_chunk_finish_reason", last_finish == FinishReason.STOP,
               events=len(events), last_finish_reason=getattr(last_finish, "value", last_finish),
               note="None here means the model/SDK closes cleanly WITHOUT a finish_reason (audit F-07 stays open)")
    except Exception as exc:
        record("3_final_chunk_finish_reason", False, exception=type(exc).__name__)

    # 4. Image input (the app sends images as bytes to the advanced model under policy routing)
    if args.dry_run:
        use_mock(text_attempt("It reads two plus three.", usage=mock_usage()))
    run_engine("4_image_input", "What does this image say? Answer briefly.", image_bytes=tiny_png("2 + 3 = ?"))

    # 5. Document (PDF text context) input. The app extracts PDF text itself and sends it as context.
    if args.dry_run:
        use_mock(text_attempt("The document says it is about cells.", usage=mock_usage()))
    doc = ("Chapter 2 - The Cell. The cell is the basic unit of life. The nucleus controls the cell's activities. "
           "Mitochondria release energy. ") * 20
    run_engine("5_document_context", "According to the document, what does the nucleus do?", pdf_context=doc)

    # 6. Bad model id: must be a fast, non-retried failure reported through the normal error path
    if args.dry_run:
        use_mock(failing_attempt(ProviderErrorKind.INVALID_REQUEST, http_status=404))
    t0 = time.perf_counter()
    try:
        req = ProviderRequest(system_instruction="x", user_parts=["hi"], history=[], model="gemini-does-not-exist",
                              timeout_seconds=20, reasoning_effort=None, enable_web_search=False, request_id="live-6")
        kind = None
        try:
            list(get_provider().stream_chat(req))
        except ProviderError as err:
            kind = err.kind
        record("6_bad_model_is_classified", kind in (ProviderErrorKind.INVALID_REQUEST, ProviderErrorKind.AUTH),
               kind=getattr(kind, "value", kind), seconds=round(time.perf_counter() - t0, 2))
    except Exception as exc:
        record("6_bad_model_is_classified", False, exception=type(exc).__name__)

    # 7. Closing a stream early returns control promptly (client 'Stop' / disconnect)
    if args.dry_run:
        use_mock(text_attempt("a ", "b ", "c ", "d ", "e ", usage=mock_usage()))
    t0 = time.perf_counter()
    gen = ai_engine.stream_ai_response(
        "Explain Newton's three laws of motion in detail, with one example each.",
        request_id="live-7", user_class="Class 9-10 (SSC)", stream="Science")
    got = 0
    for chunk in gen:
        if chunk.kind == "text":
            got += 1
            if got >= 2:
                break
    gen.close()
    record("7_close_stream_early", got >= 1 and (time.perf_counter() - t0) < 60,
           chunks_read=got, seconds=round(time.perf_counter() - t0, 2),
           note="upstream cancellation itself can only be confirmed in Google AI Studio usage / billing, not here")

    # 8. Research (Google Search grounding) is billed per query beyond the free monthly allowance -> opt-in only.
    print("\n(skipped: Google Search grounding check - it is billed per query; run it manually if needed)")

    failed = [r for r in RESULTS if not r["passed"]]
    print(f"\nSUMMARY: {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed"
          + ("  (DRY RUN - proves the script only, NOT the Gemini API)" if args.dry_run else ""))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump({"dry_run": args.dry_run, "results": RESULTS}, fh, indent=2, default=str)
        print(f"metadata-only report written to {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())