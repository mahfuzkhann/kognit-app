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

console.log(`\n${"=".repeat(52)}`);
console.log(`  ${passed} passed, ${failed} failed`);
console.log("=".repeat(52));
if (failed) {
    for (const f of failures) console.log(`\n${f.name}\n  ${f.e.stack}`);
    process.exit(1);
}
