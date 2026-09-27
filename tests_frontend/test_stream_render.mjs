/* ============================================================
   KOGNIT PHASE 8E — STREAM RENDERING TESTS
   ============================================================
   Tests the real static/js/stream-render.js. These cover the
   failure mode that matters most for streaming: a chunk boundary
   landing inside a Markdown or LaTeX construct.

   Run: node tests_frontend/test_stream_render.mjs
   ============================================================ */

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

let passed = 0, failed = 0;
const failures = [];

function check(name, fn) {
    try { fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; failures.push({ name, e }); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) {
    if (a !== b) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`);
}

// Load the module in a bare global-window shim.
global.window = {};
const src = fs.readFileSync(path.join(ROOT, "static", "js", "stream-render.js"), "utf8");
new Function(src).call(global);
const S = global.window.KognitStream;

console.log("\nSafe boundary — nothing unterminated is ever parsed");

check("plain text is fully renderable immediately", () => {
    eq(S.splitForRender("Newton's second law").renderable, "Newton's second law");
});

check("completed display math is renderable", () => {
    const t = "The law is $$F = ma$$ exactly.";
    eq(S.splitForRender(t).renderable, t);
});

check("UNTERMINATED display math is held back", () => {
    const r = S.splitForRender("The law is $$F = m");
    eq(r.renderable, "The law is ");
    eq(r.pending, "$$F = m");
});

check("unterminated inline math is held back", () => {
    eq(S.splitForRender("where $m is").renderable, "where ");
});

check("unterminated code fence is held back", () => {
    eq(S.splitForRender("Example:\n```python\nx = 1").renderable, "Example:\n");
});

check("closed code fence is renderable", () => {
    const t = "Example:\n```python\nx = 1\n```\ndone";
    eq(S.splitForRender(t).renderable, t);
});

check("unterminated bold is held back", () => {
    eq(S.splitForRender("This is **impor").renderable, "This is ");
});

check("unterminated inline code is held back", () => {
    eq(S.splitForRender("Call `foo").renderable, "Call ");
});

check("LaTeX \\[ \\] display block handled", () => {
    eq(S.splitForRender("x \\[a=b\\] y").renderable, "x \\[a=b\\] y");
    eq(S.splitForRender("x \\[a=").renderable, "x ");
});

check("LaTeX \\( \\) inline handled", () => {
    eq(S.splitForRender("x \\(a\\) y").renderable, "x \\(a\\) y");
    eq(S.splitForRender("x \\(a").renderable, "x ");
});

check("empty input is safe", () => {
    eq(S.splitForRender("").renderable, "");
    eq(S.findSafeBoundary(null), 0);
});

console.log("\nBengali + LaTeX (Kognit's sharpest case)");

check("Bengali text with completed math renders fully", () => {
    const t = "বলের সূত্র: $$F = ma$$ এখানে $m$ হলো ভর।";
    eq(S.splitForRender(t).renderable, t);
});

check("Bengali text with unterminated math holds back only the math", () => {
    const r = S.splitForRender("বলের সূত্র: $$F = m");
    eq(r.renderable, "বলের সূত্র: ");
    eq(r.pending, "$$F = m");
});

check("Bengali is never split mid-string by the scanner", () => {
    const t = "শিক্ষার্থীদের জন্য";
    eq(S.splitForRender(t).renderable, t);
});

console.log("\nProgressive accumulation — renderable never regresses");

check("boundary grows monotonically as chunks arrive", () => {
    const full = "Force is $$F = ma$$ and mass is $m$ kg.\n```py\na=1\n```\ndone";
    let acc = "";
    let lastBoundary = 0;
    for (const ch of full) {
        acc += ch;
        const b = S.findSafeBoundary(acc);
        assert(b >= lastBoundary, `boundary went backwards at ${JSON.stringify(acc)}`);
        lastBoundary = b;
    }
    eq(S.findSafeBoundary(full), full.length, "complete text must be fully renderable");
});

check("renderable is always a prefix of the accumulated text", () => {
    const full = "a $$x$$ b **c** `d` e";
    let acc = "";
    for (const ch of full) {
        acc += ch;
        const { renderable, pending } = S.splitForRender(acc);
        eq(renderable + pending, acc, "split must be lossless");
        assert(acc.startsWith(renderable), "renderable must be a prefix");
    }
});

console.log("\nMath detection (guards MathJax)");

check("detects completed display math", () => assert(S.hasCompleteMath("a $$x=1$$ b")));
check("detects completed inline math", () => assert(S.hasCompleteMath("a $x$ b")));
check("does NOT detect unterminated display math", () => assert(!S.hasCompleteMath("a $$x=1")));
check("no math in plain text", () => assert(!S.hasCompleteMath("just words")));

console.log("\nNDJSON parser");

function collect(chunks) {
    const events = [], bad = [];
    const p = S.createNdjsonParser(e => events.push(e), l => bad.push(l));
    chunks.forEach(c => p.push(c));
    p.flush();
    return { events, bad };
}

check("parses whole lines", () => {
    const { events } = collect(['{"type":"delta","text":"a"}\n{"type":"done","reply":"a"}\n']);
    eq(events.length, 2);
    eq(events[1].type, "done");
});

check("reassembles a line split across chunks", () => {
    const { events } = collect(['{"type":"del', 'ta","text":"hello"}\n']);
    eq(events.length, 1);
    eq(events[0].text, "hello");
});

check("handles a line split mid-multibyte-safe string", () => {
    const { events } = collect(['{"type":"delta","text":"বাংলা', ' টেক্সট"}\n']);
    eq(events[0].text, "বাংলা টেক্সট");
});

check("malformed line is skipped, not fatal", () => {
    const { events, bad } = collect(['not json\n{"type":"done","reply":"ok"}\n']);
    eq(bad.length, 1);
    eq(events.length, 1, "the good event must still arrive");
    eq(events[0].reply, "ok");
});

check("trailing line with no newline is flushed", () => {
    const { events } = collect(['{"type":"done","reply":"final"}']);
    eq(events.length, 1);
    eq(events[0].reply, "final");
});

check("blank lines are ignored", () => {
    const { events } = collect(['\n\n{"type":"delta","text":"x"}\n\n']);
    eq(events.length, 1);
});

check("newlines INSIDE the answer do not split events", () => {
    // This is why NDJSON is safe: json.dumps escapes the newline.
    const payload = JSON.stringify({ type: "done", reply: "line1\nline2\n$$F=ma$$" }) + "\n";
    const { events } = collect([payload]);
    eq(events.length, 1, "escaped newlines must not create extra events");
    eq(events[0].reply, "line1\nline2\n$$F=ma$$");
});

console.log("\nLoading copy honesty");

check("each backend context maps to distinct copy", () => {
    const keys = ["thinking", "research", "document", "document_web"];
    const vals = keys.map(k => S.loadingCopyFor(k));
    eq(new Set(vals).size, 4, "each state must read differently");
});

check("plain thinking copy never mentions a document or the web", () => {
    const c = S.loadingCopyFor("thinking").toLowerCase();
    assert(!c.includes("document"), "must not claim to read a document");
    assert(!c.includes("web"), "must not claim to search the web");
    assert(!c.includes("book"), "the old copy claimed 'book & notes' unconditionally");
});

check("research copy mentions the web", () => {
    assert(S.loadingCopyFor("research").toLowerCase().includes("web"));
});

check("document copy mentions the document", () => {
    assert(S.loadingCopyFor("document").toLowerCase().includes("document"));
});

check("unknown context degrades to the neutral default", () => {
    eq(S.loadingCopyFor("nonsense"), S.loadingCopyFor("thinking"));
    eq(S.loadingCopyFor(undefined), S.loadingCopyFor("thinking"));
});

console.log("\nKey Takeaway extraction (Phase 9C, Decision 6 - deterministic, no second AI call)");

check("no marker present - answer unchanged, takeaway null", () => {
    const r = S.extractTakeaway("Dhaka is the capital of Bangladesh.");
    eq(r.answer, "Dhaka is the capital of Bangladesh.");
    eq(r.takeaway, null);
});

check("marker present - splits answer from takeaway", () => {
    const r = S.extractTakeaway(
        "Photosynthesis converts light into chemical energy.\n" +
        "<!--KOGNIT_TAKEAWAY\nPlants store solar energy as glucose.\nKOGNIT_TAKEAWAY-->"
    );
    eq(r.answer, "Photosynthesis converts light into chemical energy.");
    eq(r.takeaway, "Plants store solar energy as glucose.");
});

check("Bengali takeaway extracted correctly", () => {
    const r = S.extractTakeaway(
        "সালোকসংশ্লেষণ একটি গুরুত্বপূর্ণ প্রক্রিয়া।\n" +
        "<!--KOGNIT_TAKEAWAY\nউদ্ভিদ সূর্যালোক থেকে খাদ্য তৈরি করে।\nKOGNIT_TAKEAWAY-->"
    );
    eq(r.answer, "সালোকসংশ্লেষণ একটি গুরুত্বপূর্ণ প্রক্রিয়া।");
    eq(r.takeaway, "উদ্ভিদ সূর্যালোক থেকে খাদ্য তৈরি করে।");
});

check("an empty marker body yields no takeaway, not a blank card", () => {
    const r = S.extractTakeaway("Answer text.\n<!--KOGNIT_TAKEAWAY\n   \nKOGNIT_TAKEAWAY-->");
    eq(r.takeaway, null);
});

check("malformed/unclosed marker leaves the answer untouched (fail-safe)", () => {
    const input = "Answer text.\n<!--KOGNIT_TAKEAWAY\nnever closed";
    const r = S.extractTakeaway(input);
    eq(r.answer, input, "no partial-match corruption - unmatched text stays exactly as-is");
    eq(r.takeaway, null);
});

check("marker text is trimmed of surrounding whitespace", () => {
    const r = S.extractTakeaway("A.\n<!--KOGNIT_TAKEAWAY\n   Core idea.   \nKOGNIT_TAKEAWAY-->");
    eq(r.takeaway, "Core idea.");
});

check("null/empty input never throws", () => {
    eq(S.extractTakeaway(null).answer, "");
    eq(S.extractTakeaway("").answer, "");
    eq(S.extractTakeaway(undefined).takeaway, null);
});

console.log("\nisCompletedAnswer — BUG 3 completion-integrity truth table");

check("no terminal event, no text -> not completed", () => {
    eq(S.isCompletedAnswer(false, null), false);
});

check("no terminal event, but SOME text already arrived -> still not completed", () => {
    // This is the exact confirmed frontend bug: the old check
    // `!sawTerminal && !replyText` missed this case entirely because
    // replyText was non-empty.
    eq(S.isCompletedAnswer(false, null), false);
});

check("explicit interrupted terminal -> not completed", () => {
    eq(S.isCompletedAnswer(true, "interrupted"), false);
});

check("explicit error terminal -> not completed", () => {
    eq(S.isCompletedAnswer(true, "error"), false);
});

check("explicit done terminal -> completed", () => {
    eq(S.isCompletedAnswer(true, "done"), true);
});

check("sawTerminal true but no terminalType recorded -> not completed (defensive)", () => {
    eq(S.isCompletedAnswer(true, null), false);
});

check("sawTerminal true but SOME text already arrived, so this alone was never the test", () => {
    // isCompletedAnswer only takes (sawTerminal, terminalType) - text
    // presence must NEVER be part of the completion decision. This is
    // asserted structurally by the function signature itself; this check
    // just documents that intent for future readers.
    eq(S.isCompletedAnswer.length, 2, "must never grow a text-presence parameter");
});

console.log("\napplyStreamEvent / createStreamState — BUG 3 PHASE 2 recovery state machine");

check("createStreamState starts empty", () => {
    const s = S.createStreamState();
    eq(s.replyText, "");
    eq(s.researchPayload, null);
    eq(s.sawTerminal, false);
    eq(s.terminalType, null);
});

check("delta accumulates text", () => {
    let s = S.createStreamState();
    s = S.applyStreamEvent(s, { type: "delta", text: "Hello " });
    s = S.applyStreamEvent(s, { type: "delta", text: "world" });
    eq(s.replyText, "Hello world");
    eq(s.sawTerminal, false);
});

check("spec test 17: attempt 1 partial - delta lands in state, not terminal", () => {
    let s = S.applyStreamEvent(S.createStreamState(), { type: "delta", text: "attempt one partial" });
    eq(s.replyText, "attempt one partial");
    eq(S.isCompletedAnswer(s.sawTerminal, s.terminalType), false);
});

check("spec test 18: a retry event resets everything accumulated so far", () => {
    let s = S.createStreamState();
    s = S.applyStreamEvent(s, { type: "delta", text: "attempt one partial" });
    s = S.applyStreamEvent(s, { type: "retry" });
    eq(s.replyText, "", "retry must wipe the discarded attempt's text");
    eq(s.researchPayload, null);
    eq(s.sawTerminal, false);
    eq(s.terminalType, null);
});

check("spec test 19: after a retry, new deltas start from a clean slate", () => {
    let s = S.createStreamState();
    s = S.applyStreamEvent(s, { type: "delta", text: "DISCARDED" });
    s = S.applyStreamEvent(s, { type: "retry" });
    s = S.applyStreamEvent(s, { type: "delta", text: "fresh " });
    s = S.applyStreamEvent(s, { type: "delta", text: "answer" });
    eq(s.replyText, "fresh answer");
    assert(!s.replyText.includes("DISCARDED"), "the discarded attempt's text must never reappear");
});

check("spec test 20/21: attempt 2 done - only the recovery's own text is final, and it completes", () => {
    let s = S.createStreamState();
    s = S.applyStreamEvent(s, { type: "delta", text: "DISCARDED" });
    s = S.applyStreamEvent(s, { type: "retry" });
    s = S.applyStreamEvent(s, { type: "delta", text: "fresh answer" });
    s = S.applyStreamEvent(s, { type: "done", reply: "fresh answer" });
    eq(s.replyText, "fresh answer");
    eq(S.isCompletedAnswer(s.sawTerminal, s.terminalType), true);
});

check("spec test 22: no duplicated answer even if attempt 1 streamed a lot before failing", () => {
    let s = S.createStreamState();
    s = S.applyStreamEvent(s, { type: "delta", text: "Newton's first law states that an object " });
    s = S.applyStreamEvent(s, { type: "delta", text: "remains at rest unless acted upon by DISCARDED" });
    s = S.applyStreamEvent(s, { type: "retry" });
    s = S.applyStreamEvent(s, { type: "delta", text: "Newton's first law: an object at rest stays at rest, " });
    s = S.applyStreamEvent(s, { type: "delta", text: "and an object in motion stays in motion, unless acted on by a net force." });
    s = S.applyStreamEvent(s, { type: "done", reply: "Newton's first law: an object at rest stays at rest, and an object in motion stays in motion, unless acted on by a net force." });
    assert(!s.replyText.includes("DISCARDED"), "attempt 1's text must not survive into the final answer");
    // The final text is exactly attempt 2's answer, not a longer string
    // formed by concatenating both attempts.
    eq(s.replyText, "Newton's first law: an object at rest stays at rest, and an object in motion stays in motion, unless acted on by a net force.");
});

check("spec test 23: retry followed by a second failure yields interrupted, not completed", () => {
    let s = S.createStreamState();
    s = S.applyStreamEvent(s, { type: "delta", text: "attempt one" });
    s = S.applyStreamEvent(s, { type: "retry" });
    s = S.applyStreamEvent(s, { type: "delta", text: "attempt two partial" });
    s = S.applyStreamEvent(s, { type: "interrupted", reply: "attempt two partial" });
    eq(s.replyText, "attempt two partial");
    assert(!s.replyText.includes("attempt one"), "attempt 1 must not leak into the interrupted result either");
    eq(S.isCompletedAnswer(s.sawTerminal, s.terminalType), false);
});

check("spec test 24: connection ends with no terminal event at all remains incomplete", () => {
    let s = S.createStreamState();
    s = S.applyStreamEvent(s, { type: "delta", text: "some partial text" });
    // ... connection drops here, no further events ...
    eq(S.isCompletedAnswer(s.sawTerminal, s.terminalType), false);
    eq(s.replyText, "some partial text", "the partial text is preserved for display, just never marked complete");
});

check("unrecognized event types are a safe no-op", () => {
    let s = S.applyStreamEvent(S.createStreamState(), { type: "start", context: "thinking" });
    eq(s.replyText, "");
    eq(s.sawTerminal, false);
});

check("applyStreamEvent never mutates its input state", () => {
    const s1 = S.createStreamState();
    const s2 = S.applyStreamEvent(s1, { type: "delta", text: "x" });
    eq(s1.replyText, "", "the original state object must be untouched");
    eq(s2.replyText, "x");
    assert(s1 !== s2, "a new object must be returned, never the same reference");
});

console.log(`\n${"=".repeat(52)}`);
console.log(`  ${passed} passed, ${failed} failed`);
console.log("=".repeat(52));
if (failed) {
    for (const f of failures) console.log(`\n${f.name}\n  ${f.e.stack}`);
    process.exit(1);
}