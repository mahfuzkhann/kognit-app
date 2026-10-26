#!/usr/bin/env python3
"""
Bounded, LOCAL concurrency probe for the streaming chat endpoint.

WHAT THIS MEASURES (and what it does not)
-----------------------------------------
It runs the real FastAPI app IN-PROCESS with:
  * a scripted mock provider that blocks for GENERATION_SECONDS per answer
    (a blocking sleep inside the worker thread, the same way the real SDK call
    blocks), so thread-pool queueing is realistic;
  * authentication, quota and usage persistence bypassed (they are covered by
    their own tests and depend on Supabase, which this script never contacts).

It therefore measures the application's OWN concurrency behaviour: request
queueing in the thread pool, latency percentiles, errors and memory growth under
N simultaneous streamed answers. It does NOT measure Gemini quota, Supabase,
network, TLS, a reverse proxy, or multiple worker processes. Treat the numbers
as a floor for framework overhead and a picture of the thread-pool ceiling, NOT
as "the system supports N users". A staging test against real infrastructure,
with an approved provider budget, is still required (docs/DEPLOYMENT.md).

SAFE BY CONSTRUCTION: no network calls to Gemini, Supabase or anything else; no
credentials needed (dummy values are set below only so imports succeed).

Usage:  python tools/local_load_test.py [--stages 10,50,100,250] [--generation-seconds 0.5]
"""
import argparse
import asyncio
import logging
import os
import statistics
import sys
import time

try:
    import resource   # Unix only (Linux/macOS). Absent on Windows.
except ImportError:   # pragma: no cover - exercised by tests that block the module
    resource = None

os.environ.setdefault("GEMINI_API_KEY", "dummy-not-a-real-key")
os.environ.setdefault("SUPABASE_URL", "https://example.invalid")
os.environ.setdefault("SUPABASE_ANON_KEY", "dummy")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

import backend.main as main  # noqa: E402
from backend import usage_telemetry  # noqa: E402
from backend.providers import set_provider  # noqa: E402
from backend.providers.base import ProviderEvent  # noqa: E402
from backend.providers.mock import MockProvider, text_attempt  # noqa: E402


class SlowMock(MockProvider):
    """Blocks like a real SDK call for `seconds` before yielding the answer."""

    def __init__(self, seconds):
        super().__init__([text_attempt("A short scripted answer.")])
        self.seconds = seconds

    def stream_chat(self, request):
        inner = super().stream_chat(request)

        def gen():
            time.sleep(self.seconds)
            yield from inner

        return gen()


def configure_for_cli():
    """Process-wide settings this tool needs ONLY when it is run as a script.
    They used to run at import time, which meant that merely letting pytest
    collect this file (its name matches pytest's default `*_test.py` pattern)
    switched quotas off and disabled INFO logging for the whole test session."""
    os.environ["QUOTA_MODE"] = "off"
    logging.disable(logging.INFO)   # one log line per request would drown the results table


def rss_mb():
    """Peak resident memory of this process in MiB, or None when the platform
    offers no such measurement through the standard library (Windows has no
    `resource` module). None means "unavailable" - it is never replaced by a
    guess or by 0."""
    if resource is None:
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return peak / (1024.0 * 1024.0)   # macOS reports bytes
    return peak / 1024.0                  # Linux reports KiB


def format_rss(value, width=13):
    """Right-aligned table cell for a peak-RSS value; 'n/a' when unavailable."""
    return f"{value:>{width}.1f}" if value is not None else f"{'n/a':>{width}}"


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


async def run_stage(client, concurrency, total):
    sem, latencies, errors = asyncio.Semaphore(concurrency), [], 0

    async def one(i):
        nonlocal errors
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.post("/api/chat/stream", data={"prompt": "What is force?", "chat_id": f"c{i % 50}"})
                ok = r.status_code == 200 and '"type": "done"' in r.text.replace('":"', '": "')
            except Exception:
                ok = False
            latencies.append(time.perf_counter() - t0)
            if not ok:
                errors += 1

    t0 = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(total)))
    wall = time.perf_counter() - t0
    return latencies, errors, wall


async def main_async(stages, generation_seconds):
    async def _user():
        return ("load-user", "load-token")

    async def _ctx(**kw):
        return ("Class 9-10 (SSC)", "Science")

    async def _name(**kw):
        return None

    async def _no_save(user_token, user_id, rows):
        return None

    main.app.dependency_overrides[main._rate_limited_chat] = _user
    main._get_academic_context = _ctx
    main._get_student_display_name = _name
    usage_telemetry.save_ai_usage_events = _no_save
    set_provider(SlowMock(generation_seconds))

    print(f"in-process, mock provider blocks {generation_seconds}s per answer; no external calls")
    if rss_mb() is None:
        print("peak RSS: unavailable on this platform (the standard-library 'resource' module does not exist here)")
    print()
    print(f"{'concurrent':>10} {'requests':>8} {'errors':>6} {'wall s':>7} {'req/s':>6} {'p50 s':>6} {'p95 s':>6} {'p99 s':>6} {'peak RSS MiB':>13}")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=120) as client:
        for conc in stages:
            total = conc * 2
            lat, err, wall = await run_stage(client, conc, total)
            print(f"{conc:>10} {total:>8} {err:>6} {wall:>7.2f} {total / wall:>6.1f} "
                  f"{statistics.median(lat):>6.2f} {percentile(lat, 95):>6.2f} {percentile(lat, 99):>6.2f} {format_rss(rss_mb())}")
    set_provider(None)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="10,50,100,250")
    ap.add_argument("--generation-seconds", type=float, default=0.5)
    args = ap.parse_args()
    configure_for_cli()
    asyncio.run(main_async([int(s) for s in args.stages.split(",")], args.generation_seconds))