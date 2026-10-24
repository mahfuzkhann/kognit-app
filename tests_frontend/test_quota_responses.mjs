/* ============================================================
   KOGNIT - STRUCTURED SERVER RESPONSES (quota / expired PDF)
   ============================================================
   Pre-Phase 10. The backend now answers four situations with a JSON body
   {"detail": {"code", "message", ...}}:  429 QUOTA_EXCEEDED, 503
   QUOTA_UNAVAILABLE, 403 PLAN_NOT_CONFIGURED and 409 PDF_CONTEXT_EXPIRED.

   This file tests the helper functions that read and describe those responses
   by extracting their REAL source from static/js/app.js (so it cannot drift from
   what ships) and running it in a sandbox, plus static checks that the send paths
   actually call them. WHAT IT CANNOT PROVE: the full sendMessage()/quiz network
   flow - that is only checked by reading and by node --check; the existing
   jsdom harness (test_stop_generating.mjs) would be the place to add an end-to-end
   case if the team wants one.

   Run: node tests_frontend/test_quota_responses.mjs
   ============================================================ */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const APP = fs.readFileSync(path.join(ROOT, "static/js/app.js"), "utf8");

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
const assert = (c, m) => { if (!c) throw new Error(m || "assertion failed"); };
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); };

// Extract the real helper block.
const start = APP.indexOf("async function readStructuredError");
const end = APP.indexOf("// BUG 2 FIX: single place that syncs the #pdf-status-bar UI");
assert(start > 0 && end > start, "helper block not found in app.js");
const block = APP.slice(start, end);

function sandbox(projects, extra = {}) {
    const calls = { saved: 0, ui: [] };
    const ctx = {
        projects, activeProjectId: "p1", activeChatId: "c1",
        saveProjectsToStorage: () => { calls.saved++; },
        applyPDFStatusUI: (chat) => { calls.ui.push(chat.pdf.active); },
        Date, isNaN, ...extra,
    };
    vm.createContext(ctx);
    vm.runInContext(block + "\nthis.readStructuredError = readStructuredError; this.describeStructuredError = describeStructuredError; this.markPDFExpiredForChat = markPDFExpiredForChat;", ctx);
    return { ctx, calls };
}

const resp = (body, { bad = false } = {}) => ({
    clone() { return { json: async () => { if (bad) throw new Error("not json"); return body; } }; },
});

await check("a structured error body is returned", async () => {
    const { ctx } = sandbox([]);
    const info = await ctx.readStructuredError(resp({ detail: { code: "QUOTA_EXCEEDED", message: "m" } }));
    eq(info.code, "QUOTA_EXCEEDED");
});

await check("the ordinary rate limiter's plain-string detail is NOT treated as structured", async () => {
    const { ctx } = sandbox([]);
    eq(await ctx.readStructuredError(resp({ detail: "Too many requests." })), null);
});

await check("non-JSON, empty and malformed bodies are null, never a crash", async () => {
    const { ctx } = sandbox([]);
    eq(await ctx.readStructuredError(resp(null, { bad: true })), null);
    eq(await ctx.readStructuredError(resp({})), null);
    eq(await ctx.readStructuredError(resp({ detail: { message: "no code" } })), null);
    eq(await ctx.readStructuredError(resp({ detail: ["array"] })), null);
});

await check("the response body is read via clone() so a fall-through can still read it", async () => {
    assert(/response\.clone\(\)\.json\(\)/.test(block), "must clone before reading");
});

await check("a quota message includes the reset time when given", async () => {
    const { ctx } = sandbox([]);
    const text = ctx.describeStructuredError({ code: "QUOTA_EXCEEDED", message: "You've used all of your chats.", resets_at: "2026-10-23T00:00:00+06:00" });
    assert(text.startsWith("You've used all of your chats."), text);
    assert(text.includes("Resets:"), "reset time missing: " + text);
});

await check("a bad or missing reset time never produces 'Invalid Date'", async () => {
    const { ctx } = sandbox([]);
    for (const resets_at of [undefined, null, "not a date", 12345]) {
        const text = ctx.describeStructuredError({ code: "QUOTA_EXCEEDED", message: "Used up.", resets_at });
        eq(text, "Used up.", "resets_at=" + resets_at);
    }
});

await check("a missing message falls back to a safe generic sentence", async () => {
    const { ctx } = sandbox([]);
    eq(ctx.describeStructuredError({ code: "X" }), "Something went wrong. Please try again.");
    eq(ctx.describeStructuredError(null), "Something went wrong. Please try again.");
});

await check("an expired PDF turns the chat's badge off, saves, and refreshes the UI only if that chat is open", async () => {
    const chat = { id: "c1", pdf: { active: true, filename: "a.pdf" } };
    const { ctx, calls } = sandbox([{ id: "p1", chats: [chat] }]);
    ctx.markPDFExpiredForChat("p1", "c1");
    eq(chat.pdf.active, false);
    eq(calls.saved, 1);
    eq(calls.ui, [false]);
    // another chat is open now: state saved, UI untouched
    const other = { id: "c2", pdf: { active: true } };
    const s2 = sandbox([{ id: "p1", chats: [other] }]);
    s2.ctx.activeChatId = "c1";
    s2.ctx.markPDFExpiredForChat("p1", "c2");
    eq(other.pdf.active, false); eq(s2.calls.ui, []);
});

await check("unknown project or chat ids are ignored safely", async () => {
    const { ctx, calls } = sandbox([{ id: "p1", chats: [] }]);
    ctx.markPDFExpiredForChat("nope", "nope");
    ctx.markPDFExpiredForChat("p1", "nope");
    eq(calls.saved, 0);
});

await check("the chat and regenerate sends tell the server a PDF is expected", async () => {
    const hits = APP.match(/if \(isPDFLoaded\) formData\.append\("pdf_expected", "1"\)/g) || [];
    assert(hits.length === 2, `expected 2 send paths, found ${hits.length}`);
});

await check("the stream path handles every structured status before the generic 429 branch", async () => {
    const i = APP.indexOf("PRE-PHASE 10: plan quota / quota backend / expired PDF");
    const j = APP.indexOf("PHASE 4: a 429 here means the backend rate limiter");
    assert(i > 0 && j > i, "structured handling must come first");
    const seg = APP.slice(i, j);
    for (const code of ["429", "503", "403", "409"]) assert(seg.includes(`response.status === ${code}`), code);
    assert(seg.includes("PDF_CONTEXT_EXPIRED") && seg.includes("markPDFExpiredForChat"));
    assert(/return;\s*\}\s*\}/.test(seg), "must return before touching chat state");
});

await check("quiz generation shows the structured message and keeps the old fast-click message", async () => {
    const i = APP.indexOf("PRE-PHASE 10: weekly quiz quota");
    assert(i > 0);
    const seg = APP.slice(i, i + 900);
    assert(seg.includes("readStructuredError(res)") && seg.includes("alert(describeStructuredError(structured))"));
    assert(seg.includes("You're generating quizzes too quickly"), "ordinary 429 message must remain");
});

console.log(`\n  ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
