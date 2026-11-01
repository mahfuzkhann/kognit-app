/* ============================================================
   KLORIZ FEEDBACK - message catalog + failure classifier (pure)
   ============================================================
   static/js/feedback.js is the single place that decides WHICH Bangla
   sentence a failure gets and WHICH recovery action (if any) is offered.
   This file runs the REAL file in a sandbox and pins the rules that matter:

     - a plan limit, a short-term rate limit, a temporary server problem, an
       expired session and a network failure are five DIFFERENT messages;
     - only a plan limit offers "প্ল্যান দেখুন" (the only real plan screen);
     - no message leaks an exception, a status code, a table name or English;
     - the toast is accessible (role=alert/status) and never blocks.

   Run: node tests_frontend/test_feedback.mjs
   ============================================================ */
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { JSDOM } from "jsdom";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SRC = fs.readFileSync(path.join(ROOT, "static/js/feedback.js"), "utf8");

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
const assert = (c, m) => { if (!c) throw new Error(m || "assertion failed"); };
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); };

function load() {
    const dom = new JSDOM("<!DOCTYPE html><body></body>", { runScripts: "outside-only", url: "https://kognit.test/" });
    dom.window.eval(SRC);
    return { w: dom.window, FB: dom.window.KlorizFeedback, doc: dom.window.document };
}
const hasBangla = (s) => /[\u0980-\u09FF]/.test(s);
const hasLatinWords = (s) => /[A-Za-z]{3,}/.test(s.replace(/\{[a-z]+\}/g, "").replace(/Kloriz|Google|PDF|AI/g, ""));   // {n}/{when} are substitution slots

console.log("\n1. Classification: each situation gets its own message and action");
{
    const { FB } = load();
    const quota = (over = {}) => ({ status: 429, structured: { code: "QUOTA_EXCEEDED", kind: "chat", plan: "free", resets_at: "2026-11-03T00:00:00Z", reason: "quota_exceeded", ...over } });

    await check("plan limit reached (free, chat) -> Bangla, mentions the free plan, offers প্ল্যান দেখুন", () => {
        const r = FB.classifyFailure(quota());
        eq(r.code, "QUOTA_EXHAUSTED");
        assert(hasBangla(r.text) && r.text.includes("ফ্রি প্ল্যান") && r.text.includes("চ্যাটের সীমা শেষ"), r.text);
        eq(r.action, "plans"); eq(r.actionLabel, "প্ল্যান দেখুন");
    });
    await check("the reset time is shown only when the server sent a real date", () => {
        assert(/থেকে আবার ব্যবহার করতে পারবেন/.test(FB.classifyFailure(quota()).text));
        for (const bad of [null, undefined, "", "not a date", 12345]) {
            const t = FB.classifyFailure(quota({ resets_at: bad })).text;
            assert(!/Invalid|NaN/.test(t) && !/আবার ব্যবহার করতে পারবেন/.test(t), `no reset sentence for ${JSON.stringify(bad)}`);
        }
    });
    await check("the wording follows the KIND of limit (chat / quiz / notes tool) and does not assume 'free'", () => {
        assert(FB.classifyFailure(quota({ kind: "quiz" })).text.includes("কুইজের সীমা"));
        assert(FB.classifyFailure(quota({ kind: "notes_ai" })).text.includes("নোট টুলের সীমা"));
        const pro = FB.classifyFailure(quota({ plan: "pro" })).text;
        assert(!pro.includes("ফ্রি প্ল্যান"), "a Pro student is never told about the free plan");
        assert(FB.classifyFailure(quota({ kind: "something_new" })).text.includes("ব্যবহারের সীমা"), "unknown kinds fall back safely");
    });
    await check("short-term rate limit (reason=rate_limited) is NOT a plan limit: no plan button, retry instead", () => {
        const r = FB.classifyFailure(quota({ reason: "rate_limited" }));
        eq(r.code, "RATE_LIMITED"); eq(r.action, "retry");
        assert(!/প্ল্যান/.test(r.text + r.actionLabel), "must not talk about plans");
    });
    await check("plain 429 from the ordinary limiter: rate-limit message; Retry-After becomes Bangla seconds", () => {
        const r = FB.classifyFailure({ status: 429, retryAfter: "12" });
        eq(r.code, "RATE_LIMITED"); assert(r.text.includes("১২ সেকেন্ড"), r.text); eq(r.action, "retry");
        assert(!/[0-9]/.test(FB.classifyFailure({ status: 429, retryAfter: "7" }).text), "Bangla digits");
        for (const bad of ["abc", "-4", "0", "999999", ""]) assert(!/সেকেন্ড/.test(FB.classifyFailure({ status: 429, retryAfter: bad }).text), `ignored: ${bad}`);
    });
    await check("quota backend down (503 QUOTA_UNAVAILABLE) is a temporary problem, not 'limit reached'", () => {
        const r = FB.classifyFailure({ status: 503, structured: { code: "QUOTA_UNAVAILABLE" } });
        eq(r.code, "QUOTA_UNAVAILABLE"); eq(r.action, "retry"); assert(!/সীমা শেষ/.test(r.text));
    });
    await check("plan not configured (403) and expired PDF (409) have their own messages and NO retry/plan button", () => {
        const a = FB.classifyFailure({ status: 403, structured: { code: "PLAN_NOT_CONFIGURED" } });
        eq(a.code, "PLAN_NOT_CONFIGURED"); eq(a.action, null);
        const b = FB.classifyFailure({ status: 409, structured: { code: "PDF_CONTEXT_EXPIRED" } });
        eq(b.code, "PDF_EXPIRED"); eq(b.action, null); assert(b.text.includes("পিডিএফ"));
    });
    await check("401 -> session expired with a LOGIN action; 5xx -> busy; 504/timeout -> slow; offline -> network", () => {
        eq(FB.classifyFailure({ status: 401 }).action, "login");
        eq(FB.classifyFailure({ status: 500 }).code, "SERVER_BUSY");
        eq(FB.classifyFailure({ status: 502 }).code, "SERVER_BUSY");
        eq(FB.classifyFailure({ status: 504 }).code, "TIMEOUT");
        eq(FB.classifyFailure({ status: null, timedOut: true }).code, "TIMEOUT");
        const n = FB.classifyFailure({ status: null });
        eq(n.code, "NETWORK"); eq(n.actionLabel, "আবার সংযোগ করুন");
    });
    await check("user-correctable errors: 413 too large, 400/422 bad request - no retry button (retrying can not help)", () => {
        eq(FB.classifyFailure({ status: 413 }).action, null);
        eq(FB.classifyFailure({ status: 400 }).action, null);
        eq(FB.classifyFailure({ status: 422 }).code, "BAD_REQUEST");
    });
    await check("an unknown status gets the generic message and never throws on odd input", () => {
        eq(FB.classifyFailure({ status: 418 }).code, "UNKNOWN");
        for (const v of [undefined, null, {}, { status: "500" }, { structured: "x" }, { structured: [] }]) {
            const r = FB.classifyFailure(v); assert(typeof r.text === "string" && r.text.length > 0);
        }
    });
}

console.log("\n2. Wording rules");
{
    const { FB } = load();
    await check("every catalog message is Bangla (Latin words only for brand/format names)", () => {
        for (const [k, v] of Object.entries(FB.messages)) {
            assert(hasBangla(v), `${k} has no Bangla: ${v}`);
            assert(!hasLatinWords(v), `${k} contains English words: ${v}`);
        }
    });
    await check("no catalog message leaks internals (status codes, exception names, tables, JSON, stack)", () => {
        const bad = /\b(4\d\d|5\d\d)\b|exception|error|undefined|null|stack|traceback|supabase|postgres|rls|user_id|token|api\/|SELECT|JSON/i;
        for (const [k, v] of Object.entries(FB.messages)) assert(!bad.test(v), `${k}: ${v}`);
    });
    await check("messages are short (a toast must be readable at a glance)", () => {
        for (const [k, v] of Object.entries(FB.messages)) assert(v.length <= 150, `${k} is ${v.length} chars`);
    });
    await check("msg() substitutes variables and falls back to the generic message for an unknown key", () => {
        assert(FB.msg("rateLimitedWait", { n: "৫" }).includes("৫ সেকেন্ড"));
        eq(FB.msg("definitely_not_a_key"), FB.messages.unknown);
    });
    await check("the plan button exists ONLY for a plan limit - never offered for any other code", () => {
        const cases = [{ status: 401 }, { status: 429 }, { status: 500 }, { status: null }, { status: 413 },
            { status: 429, structured: { code: "QUOTA_EXCEEDED", reason: "rate_limited" } },
            { status: 503, structured: { code: "QUOTA_UNAVAILABLE" } }];
        for (const c of cases) assert(FB.classifyFailure(c).action !== "plans", JSON.stringify(c));
    });
}

console.log("\n3. Toast (accessible, non-blocking)");
{
    const { w, FB, doc } = load();
    await check("an error toast is role=alert; an info toast is role=status; text is set via textContent (no HTML injection)", () => {
        const e = FB.toast("<img src=x onerror=alert(1)>", { kind: "error" });
        eq(e.getAttribute("role"), "alert");
        eq(doc.querySelector(".kloriz-toast img"), null, "markup in a message is inert text");
        const i = FB.toast("hello", { kind: "info" });
        eq(i.getAttribute("role"), "status");
    });
    await check("only one toast at a time; a newer one replaces the older", () => {
        FB.toast("one"); FB.toast("two");
        eq(doc.querySelectorAll(".kloriz-toast").length, 1);
        assert(doc.querySelector(".kloriz-toast").textContent.includes("two"));
    });
    await check("an action button runs its handler once and dismisses the toast", () => {
        let n = 0;
        FB.toast("x", { kind: "error", actionLabel: "আবার চেষ্টা করুন", onAction: () => n++ });
        doc.querySelector(".kloriz-toast-action").click();
        eq(n, 1); eq(doc.querySelectorAll(".kloriz-toast").length, 0);
    });
    await check("the close button has an accessible name and dismisses", () => {
        FB.toast("x", { kind: "info" });
        const c = doc.querySelector(".kloriz-toast-close");
        assert(c.getAttribute("aria-label")); c.click();
        eq(doc.querySelectorAll(".kloriz-toast").length, 0);
    });
    await check("an error toast WITH an action is sticky (no auto-dismiss); a plain one clears itself", async () => {
        FB.toast("sticky", { kind: "error", actionLabel: "a", onAction() {}, duration: 10 });
        FB.toast("plain", { kind: "info", duration: 10 });
        await new Promise((r) => setTimeout(r, 40));
        eq(doc.querySelectorAll(".kloriz-toast").length, 0, "plain toast auto-dismissed");
        FB.toast("sticky", { kind: "error", actionLabel: "a", onAction() {}, duration: 10 });
        await new Promise((r) => setTimeout(r, 40));
        eq(doc.querySelectorAll(".kloriz-toast").length, 1, "sticky error still there");
    });
    await check("toastFailure wires 'plans' to the existing Plan screen and 'retry'/'login' to the caller's handlers", () => {
        const opened = [];
        w.KlorizProfileMenu = { openAccount: (pane) => opened.push(pane) };
        FB.toastFailure(FB.classifyFailure({ status: 429, structured: { code: "QUOTA_EXCEEDED", kind: "chat", plan: "free", reason: "quota_exceeded" } }));
        doc.querySelector(".kloriz-toast-action").click();
        eq(opened, ["plan"]);
        let retried = 0;
        FB.toastFailure(FB.classifyFailure({ status: 500 }), { retry: () => retried++ });
        doc.querySelector(".kloriz-toast-action").click();
        eq(retried, 1);
        FB.toastFailure(FB.classifyFailure({ status: 500 }), {});
        eq(doc.querySelector(".kloriz-toast-action"), null, "no button when there is nothing to run");
    });
    await check("openPlans() reports false (instead of pretending) when the Plan screen is not available", () => {
        const { FB: F2, w: w2 } = load();
        eq(F2.openPlans(), false);
        w2.KlorizProfileMenu = { openAccount() {} };
        eq(F2.openPlans(), true);
    });
}

console.log(`\n  ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
