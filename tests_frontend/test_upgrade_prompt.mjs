/* ============================================================
   UPGRADE UX - what a FREE student sees at a Pro-only feature
   ============================================================
   CEO decision (final): a Free student who reaches a Pro-only feature gets
     1. a clear explanation of the locked feature,
     2. the value of Pro, and
     3. a visible "Upgrade to Pro" call to action
   - never a generic error. The CTA may only lead to a destination that really exists
   (the account's Plan screen). There is no checkout, so nothing here may charge, subscribe,
   unlock, or display a "success" state. The product says "Unlimited flashcards"; the internal
   200/day guard must never reach the UI.

   PART 1 runs the REAL feedback.js alone (card, modal, destination safety).
   PART 2 boots the REAL page (index.html + app.js + notes.js + nav-views.js + i18n.js + profile-menu.js)
          and drives the three real integration points: Pocket Notes study material,
          the Student Profile panel, and the Quizzes topic history.

   Run: node tests_frontend/test_upgrade_prompt.mjs
   ============================================================ */
import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { IDBFactory } from "fake-indexeddb";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
let passed = 0, failed = 0;
const failures = [];
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; failures.push({ name, e }); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
const assert = (c, m) => { if (!c) throw new Error(m || "assertion failed"); };
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const FEEDBACK = read("static/js/feedback.js");
const PRO_LIST = ["20 AI chats every day", "15 quizzes every day", "Unlimited flashcards",
    "Quizzes from your saved notes", "Learning insights and your learning matrix",
    "Personalised next steps and mistake analysis"];
const ALL_FEATURES = ["note_flashcards", "note_quiz", "learning_snapshot", "learning_mistakes",
    "learning_next_steps", "learning_insights", "learning_matrix"];
const PAYMENT_WORDS = /[\$\u09F3\u20B9]|BDT|USD|\bTk\b|\/\s*month|per month|bKash|Nagad|\bcards?\b|checkout|subscribe[ds]?\b|payment|\bcharged?\b|\bprice\b/i;
const FAKE_SUCCESS = /\b(subscribed|upgraded|welcome to pro|you(?:'| a)re (?:now )?pro|congratulations|payment (?:received|successful)|success)\b/i;

function loadFeedback({ menu } = {}) {
    const dom = new JSDOM("<!DOCTYPE html><body><div id='host'></div><button id='trigger'>t</button></body>",
        { runScripts: "outside-only", url: "https://kognit.test/", pretendToBeVisual: true });
    if (menu !== undefined) dom.window.KlorizProfileMenu = menu;
    dom.window.eval(FEEDBACK);
    return { w: dom.window, FB: dom.window.KlorizFeedback, doc: dom.window.document };
}
const okMenu = () => { const calls = []; return { calls, openAccount: (s) => { calls.push(s); return true; } }; };
const detail = (feature, extra = {}) => ({ detail: { code: "PLAN_REQUIRED", feature, message: "x", required_plan: "pro", ...extra } });

/* ============================================================== */
console.log("\n1. Classification and helpers");
{
    await check("a PLAN_REQUIRED failure is classified, offers the CTA label \"Upgrade to Pro\", never a generic error", () => {
        const { FB } = loadFeedback();
        const r = FB.classifyFailure({ status: 403, structured: { code: "PLAN_REQUIRED", feature: "note_flashcards" } });
        eq(r.code, "PLAN_REQUIRED"); eq(r.action, "plans"); eq(r.actionLabel, "Upgrade to Pro");
        assert(/Pro/.test(r.text) && r.text !== FB.messages.generic, "specific message: " + r.text);
    });
    await check("isPlanRequired / planRequiredFeature accept the body, the detail object, and reject everything else", () => {
        const { FB } = loadFeedback();
        assert(FB.isPlanRequired(detail("note_quiz")) && FB.isPlanRequired(detail("note_quiz").detail));
        eq(FB.planRequiredFeature(detail("note_quiz")), "note_quiz");
        for (const bad of [null, undefined, "PLAN_REQUIRED", 403, [], {}, { detail: "PLAN_REQUIRED" }, { detail: { code: "QUOTA_EXCEEDED" } }, { code: "plan_required" }]) {
            assert(!FB.isPlanRequired(bad), "rejects " + JSON.stringify(bad));
        }
        eq(FB.planRequiredFeature({ detail: { code: "PLAN_REQUIRED" } }), null, "no feature -> null");
    });
    await check("the Pro feature list says \"Unlimited flashcards\" and matches the agreed list exactly", () => {
        eq(Array.from(loadFeedback().FB.proFeatureList), PRO_LIST);
    });
}

/* ============================================================== */
console.log("\n2. The locked-feature card");
{
    for (const feature of ALL_FEATURES) {
        await check(`${feature}: explanation + value + the full Pro list + a visible Upgrade to Pro button`, () => {
            const { FB, doc } = loadFeedback({ menu: okMenu() });
            const host = doc.getElementById("host");
            FB.renderLocked(host, feature);
            const card = host.querySelector(".kloriz-upgrade");
            assert(card, "card rendered"); eq(card.getAttribute("data-feature"), feature);
            assert(card.querySelector(".kloriz-upgrade-title").textContent.length > 8, "title");
            assert(card.querySelector(".kloriz-upgrade-explanation").textContent.length > 30, "explains what is locked");
            assert(card.querySelector(".kloriz-upgrade-value").textContent.length > 20, "states the value of Pro");
            eq(Array.from(card.querySelectorAll(".kloriz-upgrade-list li")).map((li) => li.textContent), PRO_LIST);
            const cta = card.querySelector('[data-fk="upgrade-cta"]');
            eq(cta.textContent, "Upgrade to Pro"); assert(!cta.disabled, "enabled when the destination exists");
            assert(!/error|failed|couldn.?t|something went wrong/i.test(card.textContent), "not an error message");
        });
    }
    await check("flashcards copy names \"Unlimited flashcards\" as the value; no internal guard number appears anywhere", () => {
        const { FB, doc } = loadFeedback({ menu: okMenu() });
        FB.renderLocked(doc.getElementById("host"), "note_flashcards");
        const text = doc.getElementById("host").textContent;
        assert(/Unlimited flashcards/.test(text));
        assert(!/\b200\b|per day|a day|daily limit|abuse|guard/i.test(text), "no guard/limit language: " + text);
        for (const f of ALL_FEATURES) {
            const c = FB.upgradeCopy[f];
            assert(!/\b200\b|abuse|guard/i.test(c.title + c.explanation + c.value), f);
        }
    });
    await check("an unknown feature id falls back to generic Pro copy and still has the CTA", () => {
        const { FB, doc } = loadFeedback({ menu: okMenu() });
        for (const f of ["made_up", null, undefined, "__proto__", "constructor", "toString"]) {
            FB.renderLocked(doc.getElementById("host"), f);
            const card = doc.querySelector(".kloriz-upgrade");
            eq(card.querySelector(".kloriz-upgrade-title").textContent, "This is a Pro feature", String(f));
            assert(card.querySelector('[data-fk="upgrade-cta"]'));
        }
    });
    await check("nothing from the feature id or any other input can inject HTML", () => {
        const { FB, doc } = loadFeedback({ menu: okMenu() });
        FB.renderLocked(doc.getElementById("host"), '"><img src=x onerror=alert(1)><script>boom()</script>');
        eq(doc.querySelectorAll("img, script").length, 0);
        eq(doc.querySelector(".kloriz-upgrade").getAttribute("data-feature"), "unknown", "only known feature ids reach the DOM");
    });
    await check("rendering twice replaces the card instead of stacking duplicates", () => {
        const { FB, doc } = loadFeedback({ menu: okMenu() });
        const host = doc.getElementById("host");
        FB.renderLocked(host, "note_quiz"); FB.renderLocked(host, "note_flashcards");
        eq(host.querySelectorAll(".kloriz-upgrade").length, 1);
        eq(host.querySelector(".kloriz-upgrade").getAttribute("data-feature"), "note_flashcards");
    });
    await check("no price, payment claim, or fake success text appears on any card", () => {
        const { FB, doc } = loadFeedback({ menu: okMenu() });
        for (const f of ALL_FEATURES) {
            FB.renderLocked(doc.getElementById("host"), f);
            const t = doc.getElementById("host").textContent;
            assert(!PAYMENT_WORDS.test(t), f + ": " + t.match(PAYMENT_WORDS));
            assert(!FAKE_SUCCESS.test(t), f);
        }
    });
}

/* ============================================================== */
console.log("\n3. The CTA only leads somewhere real, and fails safely");
{
    await check("clicking Upgrade to Pro opens the Plan screen (and nothing else happens)", () => {
        const menu = okMenu();
        const { FB, doc, w } = loadFeedback({ menu });
        FB.renderLocked(doc.getElementById("host"), "note_flashcards");
        const before = doc.documentElement.outerHTML;
        doc.querySelector('[data-fk="upgrade-cta"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        eq(menu.calls, ["plan"]);
        assert(!FAKE_SUCCESS.test(doc.body.textContent), "no success state");
        eq(doc.querySelectorAll(".kloriz-upgrade-overlay").length, 0);
        assert(before.length > 0);
    });
    await check("no destination (no profile menu): the button is visible but DISABLED, with a plain next step; clicking is harmless", () => {
        const { FB, doc, w } = loadFeedback();                     // KlorizProfileMenu undefined
        FB.renderLocked(doc.getElementById("host"), "note_quiz");
        const cta = doc.querySelector('[data-fk="upgrade-cta"]');
        assert(cta && cta.disabled && cta.getAttribute("aria-disabled") === "true", "disabled, not missing");
        eq(cta.textContent, "Upgrade to Pro");
        const note = doc.querySelector(".kloriz-upgrade-note");
        assert(!note.hidden && /account menu/i.test(note.textContent) && /Plan/.test(note.textContent), "documented next step");
        cta.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));     // must not throw
        assert(!FAKE_SUCCESS.test(doc.body.textContent));
    });
    await check("an invalid destination (openAccount is not a function) is treated as no destination", () => {
        for (const bad of [{ openAccount: "plan" }, { openAccount: null }, {}, null, 7, "menu"]) {
            const { FB, doc } = loadFeedback({ menu: bad });
            FB.renderLocked(doc.getElementById("host"), "note_quiz");
            assert(doc.querySelector('[data-fk="upgrade-cta"]').disabled, "disabled for " + JSON.stringify(bad));
        }
    });
    await check("a destination that throws never breaks the page; the student is told where to look instead", () => {
        const { FB, doc, w } = loadFeedback({ menu: { openAccount() { throw new Error("boom"); } } });
        FB.renderLocked(doc.getElementById("host"), "note_quiz");
        doc.querySelector('[data-fk="upgrade-cta"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        const note = doc.querySelector(".kloriz-upgrade-note");
        assert(!note.hidden && /account menu/i.test(note.textContent), note.textContent);
    });
    await check("a destination that reports failure (returns false) shows the fallback note and keeps the modal open", () => {
        const { FB, doc, w } = loadFeedback({ menu: { openAccount() { return false; } } });
        FB.openUpgradePrompt("note_flashcards");
        doc.querySelector('[data-fk="upgrade-cta"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        assert(doc.querySelector(".kloriz-upgrade-overlay"), "still open so the note is readable");
        assert(!doc.querySelector(".kloriz-upgrade-note").hidden);
    });
}

/* ============================================================== */
console.log("\n4. The modal");
{
    await check("it is an accessible modal dialog labelled by the feature title", () => {
        const { FB, doc } = loadFeedback({ menu: okMenu() });
        FB.openUpgradePrompt("note_flashcards");
        const dlg = doc.querySelector('[role="dialog"]');
        eq(dlg.getAttribute("aria-modal"), "true");
        const labelled = doc.getElementById(dlg.getAttribute("aria-labelledby"));
        eq(labelled.textContent, "Flashcards are a Pro feature");
        eq(doc.activeElement.getAttribute("data-fk"), "upgrade-cta", "focus moves to the call to action");
    });
    await check("Escape, the backdrop and \"Not now\" each close it and return focus to the trigger", () => {
        const { FB, doc, w } = loadFeedback({ menu: okMenu() });
        const trigger = doc.getElementById("trigger");
        const open = () => { trigger.focus(); FB.openUpgradePrompt("note_quiz"); assert(doc.querySelector(".kloriz-upgrade-overlay")); };
        open(); doc.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
        eq(doc.querySelectorAll(".kloriz-upgrade-overlay").length, 0, "Escape"); eq(doc.activeElement, trigger);
        open(); doc.querySelector(".kloriz-upgrade-overlay").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        eq(doc.querySelectorAll(".kloriz-upgrade-overlay").length, 0, "backdrop"); eq(doc.activeElement, trigger);
        open(); doc.querySelector('[data-fk="upgrade-dismiss"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        eq(doc.querySelectorAll(".kloriz-upgrade-overlay").length, 0, "Not now"); eq(doc.activeElement, trigger);
    });
    await check("a click inside the dialog does not close it; opening again replaces rather than stacks", () => {
        const { FB, doc, w } = loadFeedback({ menu: okMenu() });
        FB.openUpgradePrompt("note_quiz");
        doc.querySelector(".kloriz-upgrade-dialog").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        eq(doc.querySelectorAll(".kloriz-upgrade-overlay").length, 1);
        FB.openUpgradePrompt("learning_mistakes");
        eq(doc.querySelectorAll(".kloriz-upgrade-overlay").length, 1);
        eq(doc.querySelector(".kloriz-upgrade").getAttribute("data-feature"), "learning_mistakes");
    });
    await check("Tab is trapped inside the dialog", () => {
        const { FB, doc, w } = loadFeedback({ menu: okMenu() });
        FB.openUpgradePrompt("note_quiz");
        const buttons = doc.querySelectorAll(".kloriz-upgrade-dialog button");
        buttons[buttons.length - 1].focus();
        doc.dispatchEvent(new w.KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }));
        eq(doc.activeElement, buttons[0], "wraps to the first control");
    });
    await check("a successful hand-off closes the modal and shows no success state", () => {
        const menu = okMenu();
        const { FB, doc, w } = loadFeedback({ menu });
        FB.openUpgradePrompt("note_flashcards");
        doc.querySelector('[data-fk="upgrade-cta"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
        eq(menu.calls, ["plan"]); eq(doc.querySelectorAll(".kloriz-upgrade-overlay").length, 0);
        assert(!FAKE_SUCCESS.test(doc.body.textContent));
    });
}

/* ============================================================== */
/* PART 2 - the real page                                          */
/* ============================================================== */
const MARKED_SRC = fs.readFileSync(path.join(HERE, "node_modules", "marked", "lib", "marked.umd.js"), "utf8");
const SRC = {
    stream: read("static/js/stream-render.js"), purify: read("static/js/vendor/purify.min.js"),
    feedback: FEEDBACK, app: read("static/js/app.js"), store: read("static/js/notes-store.js"),
    notes: read("static/js/notes.js"), nav: read("static/js/nav-views.js"),
    i18n: read("static/js/i18n.js"), menu: read("static/js/profile-menu.js"),
};
const TEMPLATE = read("templates/index.html");
const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body) });
const PLAN_BODY = (feature) => detail(feature, { message: "This is available on the Pro plan." });

/* A fake backend. `plan` decides what a Free vs Pro student gets, mirroring backend/main.py. */
function makeApi(plan) {
    const api = { plan, calls: [], notes: [], flash: [{ front: "What is F?", back: "ma" }],
        quiz: [{ question: "F = ?", options: ["ma", "mv", "m/a", "a/m"], correct_index: 0, explanation: "Newton" }] };
    api.add = (n) => { const note = { id: "n" + (api.notes.length + 1), client_id: "c" + api.notes.length, content_md: "Body", takeaway: null,
        question_md: null, sources: [], tags: [], needs_review: false, source_ref: null, flashcards: null, mini_quiz: null,
        created_at: "2026-11-05T10:00:00Z", updated_at: "2026-11-05T10:00:00Z", ...n }; api.notes.push(note); return note; };
    api.fetch = async (url, opts = {}) => {
        const method = (opts.method || "GET").toUpperCase();
        api.calls.push({ method, url });
        const u = new URL(url, "https://kognit.test");
        let body = {}; try { body = opts.body ? JSON.parse(opts.body) : {}; } catch (e) { /* form data */ }
        if (u.pathname === "/api/profile/snapshot" || u.pathname === "/api/profile/mistakes" || u.pathname === "/api/profile/next-steps") {
            if (api.plan !== "pro") {
                const feature = u.pathname.endsWith("snapshot") ? "learning_snapshot" : u.pathname.endsWith("mistakes") ? "learning_mistakes" : "learning_next_steps";
                if (api.only && !api.only.includes(feature)) return json(503, { detail: "down" });
                return json(403, PLAN_BODY(feature));
            }
            return json(200, api.proPayload ? api.proPayload(u.pathname) : { });
        }
        const m = u.pathname.match(/^\/api\/notes(?:\/([^/]+))?(?:\/(flashcards|mini-quiz|suggest-tags))?$/);
        if (!m) return json(404, { detail: "nope" });
        const [, id, action] = m;
        if (!id && method === "GET") return json(200, { notes: api.notes, limit: 20, offset: 0, has_more: false });
        const note = api.notes.find((n) => n.id === id);
        if (!note) return json(404, { detail: "Note not found." });
        if (!action && method === "GET") return json(200, { note });
        if (action === "flashcards" || action === "mini-quiz") {
            const col = action === "flashcards" ? "flashcards" : "mini_quiz";
            const stored = note[col] && !(body && body.regenerate === true);
            if (stored) return json(200, { [col]: note[col], note, regenerated: false });
            if (api.plan !== "pro") return json(403, PLAN_BODY(action === "flashcards" ? "note_flashcards" : "note_quiz"));
            if (api.generic403) return json(403, { detail: "Forbidden for another reason" });
            note[col] = action === "flashcards" ? api.flash : api.quiz;
            return json(200, { [col]: note[col], note, regenerated: true });
        }
        return json(200, { suggestions: [] });
    };
    return api;
}

async function boot(api) {
    const html = TEMPLATE.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kognit.test/" });
    const w = dom.window;
    w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    w.TextEncoder = TextEncoder; w.TextDecoder = TextDecoder; w.MessageChannel = MessageChannel;
    w.alert = () => {}; w.confirm = () => true;
    Object.defineProperty(w.navigator, "onLine", { get: () => true, configurable: true });
    w.fetch = api.fetch; w.indexedDB = new IDBFactory();
    const query = { select: () => query, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => query, eq: async () => ({ error: null }) };
    w.supabase = { createClient: () => ({ auth: {
        getSession: async () => ({ data: { session: { access_token: "tok", user: { id: "user-1" } } } }),
        onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }), signOut: async () => ({}) }, from: () => query }) };
    w.eval(MARKED_SRC);
    w.Element.prototype.getClientRects = function () { return this.isConnected ? [{}] : []; };
    w.MathJax = Object.assign(w.MathJax || {}, { typesetPromise: async () => {}, typesetClear: () => {}, startup: { promise: Promise.resolve() } });
    const add = (src) => { const s = w.document.createElement("script"); s.textContent = src; w.document.body.appendChild(s); };
    add(SRC.stream); add(SRC.purify); add(SRC.feedback); add(SRC.app);
    w.openAuthModal = () => {};
    add(SRC.store); add(SRC.notes); add(SRC.nav); add(SRC.i18n); add(SRC.menu);
    w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
    await sleep(80);
    return { w, doc: w.document, api };
}
const click = (w, el) => el.dispatchEvent(new w.MouseEvent("click", { bubbles: true, cancelable: true }));
const button = (doc, text) => Array.from(doc.querySelectorAll("button")).find((b) => b.textContent.trim() === text);
async function openFirstNote(h) {
    click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(90);
    const card = h.doc.querySelector(".note-card, [data-note-id]");
    assert(card, "a note card exists"); click(h.w, card); await sleep(70);
}

console.log("\n5. Pocket Notes: Free student asks for study material");
{
    await check("Create flashcards -> explanation card + Upgrade to Pro, NOT the generic error; the note is untouched", async () => {
        const api = makeApi("free"); api.add({ content_md: "Newton note" });
        const h = await boot(api); await openFirstNote(h);
        click(h.w, button(h.doc, "Create flashcards")); await sleep(120);
        const card = h.doc.querySelector(".kloriz-upgrade");
        assert(card, "upgrade card shown"); eq(card.getAttribute("data-feature"), "note_flashcards");
        assert(/Unlimited flashcards/.test(card.textContent));
        eq(card.querySelector('[data-fk="upgrade-cta"]').textContent, "Upgrade to Pro");
        eq(h.doc.querySelectorAll(".notes-hint-error").length, 0, "no generic error box");
        assert(!/Couldn.?t create study material/.test(h.doc.getElementById("notes-view").textContent));
        assert(/Newton note/.test(h.doc.getElementById("notes-view").textContent), "the note itself is still shown");
        assert(button(h.doc, "Create flashcards"), "the student can try again after upgrading");
    });
    await check("Create quiz from a note -> the note_quiz card", async () => {
        const api = makeApi("free"); api.add({ content_md: "Newton note" });
        const h = await boot(api); await openFirstNote(h);
        const quizBtn = Array.from(h.doc.querySelectorAll("button")).find((b) => /quiz/i.test(b.textContent) && /Create|Practice|Make/i.test(b.textContent));
        assert(quizBtn, "a create-quiz button exists"); click(h.w, quizBtn); await sleep(120);
        eq(h.doc.querySelector(".kloriz-upgrade").getAttribute("data-feature"), "note_quiz");
        eq(h.doc.querySelectorAll(".notes-hint-error").length, 0);
    });
    await check("the CTA opens the REAL Plan screen, which lists what Pro includes; no request is sent and no success state appears", async () => {
        const api = makeApi("free"); api.add({ content_md: "Newton note" });
        const h = await boot(api); await openFirstNote(h);
        click(h.w, button(h.doc, "Create flashcards")); await sleep(120);
        const callsBefore = api.calls.length;
        click(h.w, h.doc.querySelector('[data-fk="upgrade-cta"]')); await sleep(80);
        const body = h.doc.getElementById("account-body");
        assert(body && body.textContent.length > 20, "the account screen opened");
        for (const item of PRO_LIST) assert(body.textContent.includes(item), "Plan screen lists: " + item);
        assert(/isn.t available yet/.test(body.textContent), "honest: Pro cannot be bought yet");
        eq(api.calls.length, callsBefore, "the click sent no request at all");
        assert(!FAKE_SUCCESS.test(body.textContent) && !PAYMENT_WORDS.test(body.textContent));
        const btn = body.querySelector(".acct-btn");
        assert(btn && btn.disabled, "the Pro action on the Plan screen is disabled (there is no checkout)");
    });
    await check("already-generated flashcards stay openable for a Free student (opening never needs Pro)", async () => {
        const api = makeApi("free"); api.add({ content_md: "Body", flashcards: api.flash });
        const h = await boot(api); await openFirstNote(h);
        click(h.w, button(h.doc, "Open flashcards")); await sleep(80);
        assert(h.doc.querySelector(".notes-flash-text"), "cards shown");
        eq(h.doc.querySelectorAll(".kloriz-upgrade").length, 0);
    });
    await check("regenerating (a NEW generation) is gated too, and the existing cards are not lost", async () => {
        const api = makeApi("free"); const note = api.add({ content_md: "Body", flashcards: api.flash });
        const h = await boot(api); await openFirstNote(h);
        click(h.w, button(h.doc, "Open flashcards")); await sleep(60);
        click(h.w, button(h.doc, "Regenerate flashcards")); await sleep(120);
        assert(h.doc.querySelector(".kloriz-upgrade"), "upgrade card shown");
        assert(note.flashcards && note.flashcards.length === 1, "stored cards untouched");
    });
    await check("a 403 that is NOT PLAN_REQUIRED still shows the ordinary error (the card is only for locked features)", async () => {
        const api = makeApi("pro"); api.generic403 = true; api.add({ content_md: "Body" });
        const h = await boot(api); await openFirstNote(h);
        click(h.w, button(h.doc, "Create flashcards")); await sleep(120);
        eq(h.doc.querySelectorAll(".kloriz-upgrade").length, 0);
        assert(h.doc.querySelector(".notes-hint-error"), "ordinary error shown");
    });
    await check("a Pro student sees flashcards, never an upgrade card", async () => {
        const api = makeApi("pro"); api.add({ content_md: "Body" });
        const h = await boot(api); await openFirstNote(h);
        click(h.w, button(h.doc, "Create flashcards")); await sleep(120);
        eq(h.doc.querySelectorAll(".kloriz-upgrade").length, 0);
        assert(h.doc.querySelector(".notes-flash-text"), "cards shown");
    });
    await check("the client cannot unlock anything: nothing the page stores or sends carries a plan", async () => {
        const api = makeApi("free"); api.add({ content_md: "Body" });
        const h = await boot(api);
        h.w.localStorage.setItem("plan", "pro"); h.w.localStorage.setItem("kloriz_plan", "pro");
        h.w.__plan = "pro"; h.w.KLORIZ_PLAN = "pro";
        await openFirstNote(h);
        click(h.w, button(h.doc, "Create flashcards")); await sleep(120);
        assert(h.doc.querySelector(".kloriz-upgrade"), "still locked: only the server decides");
    });
}

console.log("\n6. Student Profile panel");
{
    const panel = async (api) => { const h = await boot(api); h.w.openStudentProfilePanel(); await sleep(160); return h; };
    await check("all three requests refused -> ONE explanation card with Upgrade to Pro; the rest say \"Included with Pro.\"", async () => {
        const h = await panel(makeApi("free"));
        eq(h.doc.querySelectorAll("#student-profile-modal .kloriz-upgrade").length, 1, "exactly one card, not six");
        const top = h.doc.querySelector("#sp-overview-body .kloriz-upgrade");
        assert(top, "the card is at the top"); eq(top.querySelector('[data-fk="upgrade-cta"]').textContent, "Upgrade to Pro");
        for (const id of ["sp-strengths-body", "sp-needs-practice-body", "sp-mistakes-body", "sp-progress-body", "sp-next-steps-body"]) {
            eq(h.doc.getElementById(id).textContent.trim(), "Included with Pro.", id);
        }
        assert(!/unavailable|try again|couldn.?t/i.test(h.doc.getElementById("student-profile-modal").textContent), "no generic unavailable text");
    });
    await check("only one section locked (the others failed for another reason): that section gets the card, the rest keep the ordinary message", async () => {
        const api = makeApi("free"); api.only = ["learning_mistakes"];       // snapshot + next-steps answer 503
        const h = await panel(api);
        const locked = h.doc.querySelector("#sp-mistakes-body .kloriz-upgrade");
        assert(locked, "mistakes section locked"); eq(locked.getAttribute("data-feature"), "learning_mistakes");
        eq(h.doc.querySelectorAll("#sp-overview-body .kloriz-upgrade").length, 0, "503 is not 'locked'");
        assert(/unavailable|couldn.?t|try again/i.test(h.doc.getElementById("sp-overview-body").textContent), "503 keeps the ordinary message");
    });
    await check("closing and reopening the panel does not stack cards", async () => {
        const h = await panel(makeApi("free"));
        h.w.closeStudentProfilePanel && h.w.closeStudentProfilePanel();
        h.w.openStudentProfilePanel(); await sleep(160);
        eq(h.doc.querySelectorAll("#student-profile-modal .kloriz-upgrade").length, 1);
    });
}

console.log("\n7. Quizzes view: topic history is part of the learning snapshot");
{
    await check("a Free student sees the explanation card where the topic chips would be, not \"couldn't load\"", async () => {
        const api = makeApi("free"); const h = await boot(api);
        click(h.w, h.doc.getElementById("nav-tab-quizzes")); await sleep(160);
        const card = h.doc.querySelector("#quizzes-view .kloriz-upgrade");
        assert(card, "locked card inside the Quizzes view"); eq(card.getAttribute("data-feature"), "learning_snapshot");
        assert(!/Couldn.?t load your topics/.test(h.doc.getElementById("quizzes-view").textContent));
        assert(h.doc.getElementById("quizzes-view").querySelector("button"), "the rest of the view (start a quiz) still works");
    });
}

/* ============================================================== */
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) {
    console.log("\nFailures:");
    for (const f of failures) console.log(` - ${f.name}\n   ${f.e.stack || f.e.message}`);
    process.exit(1);
}
