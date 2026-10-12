/* ============================================================
   KOGNIT POCKET NOTES - REAL-BROWSER REGRESSION TEST
   ============================================================
   jsdom has NO layout engine, so it cannot see a "giant glyph" bug. This
   file drives a REAL Chrome/Chromium/Edge (via puppeteer-core, no browser
   download) against the REAL templates/index.html, app.js, notes.js and the
   REAL MathJax tex-svg + marked bundles taken from node_modules (so it needs
   no network). Only Supabase and the notes API are faked.

   THE BUG THIS PINS: MathJax measures the element it typesets. Typeset a
   DETACHED (or display:none) element and the measurement is NaN, so MathJax
   emits <svg width="NaNex" height="NaNex" viewBox="0 -764.8 NaN NaN">; the
   browser then sizes that SVG to its container and scales the 1000-unit glyph
   paths to match: huge white shapes covering the flashcards / mini-quiz.

   BROWSER DISCOVERY: set CHROME_PATH to a Chrome/Chromium/Edge executable, or
   rely on the usual install locations (Windows/macOS/Linux). Optional
   CHROME_ARGS adds launch flags (space separated). If NO browser is found the
   test prints SKIPPED and exits 0 - it never silently reports a pass: the
   skip is loud, and `npm run test:browser` is the explicit way to run it.

   Run: node tests_frontend/test_notes_browser.mjs
   ============================================================ */

import puppeteer from "puppeteer-core";
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const NM = path.join(HERE, "node_modules");

function findBrowser() {
    const candidates = [
        process.env.CHROME_PATH,
        "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
        (process.env.LOCALAPPDATA || "") + "\\Google\\Chrome\\Application\\chrome.exe",
        "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
        "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium", "/usr/bin/chromium-browser",
    ].filter(Boolean);
    return candidates.find((p) => fs.existsSync(p)) || null;
}

const EXE = findBrowser();
if (!EXE) {
    console.log("\nSKIPPED: no Chrome/Chromium/Edge found (set CHROME_PATH to run the real-browser Pocket Notes test).");
    console.log("0 passed, 0 failed (SKIPPED - browser-level behaviour NOT verified by this run)");
    process.exit(0);
}
for (const f of ["mathjax/es5/tex-svg.js", "marked/lib/marked.umd.js"]) {
    if (!fs.existsSync(path.join(NM, f))) { console.log(`\nSKIPPED: ${f} missing - run npm install in tests_frontend.`); process.exit(0); }
}

let passed = 0, failed = 0;
const failures = [];
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; failures.push({ name, e }); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (a !== b) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------------
   Local app server + fake notes API
   ------------------------------------------------------------ */
const MIME = { ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html" };
const server = await new Promise((resolve) => {
    const s = http.createServer((req, res) => {
        const u = new URL(req.url, "http://x");
        let file = null;
        if (u.pathname === "/") file = path.join(ROOT, "templates/index.html");
        else if (u.pathname.startsWith("/static/")) file = path.join(ROOT, u.pathname);
        if (file && fs.existsSync(file)) { res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "text/plain", "Cache-Control": "no-store" }); res.end(fs.readFileSync(file)); }
        else { res.writeHead(404, { "Content-Type": "application/json" }); res.end("{}"); }
    });
    s.listen(0, "127.0.0.1", () => resolve(s));
});
const BASE = `http://127.0.0.1:${server.address().port}/`;

const SUPABASE_STUB = `window.supabase = { createClient: () => ({ auth: {
  getSession: async () => ({ data: { session: { access_token: "tok", user: { id: "user-1", email: "t@example.com" } } } }),
  onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }), signOut: async () => ({}) },
  from: () => { const q = { select: () => q, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => q, eq: async () => ({ error: null }) }; return q; } }) };`;

const NOTE_ID = "00000000-0000-4000-8000-000000000001";
function makeState(over = {}) {
    return {
        requests: [],
        flashSets: 0,
        note: {
            id: NOTE_ID, client_id: "n_abcdef123456", created_at: "2026-10-11T08:00:00Z", updated_at: "2026-10-11T08:00:00Z",
            content_md: "**সমাধান:** বৃষ্টির দিনে ছাতা ধরার কোণ নির্ণয়:\n\n$$\\tan\\theta_3 = \\frac{3}{2} = 1.5$$\n\n**উত্তর:** উলম্বের সাথে $56.31^\\circ$ কোণে ছাতা ধরতে হবে।",
            takeaway: "সূত্রটি হলো $\\tan\\theta = \\frac{v_r}{v_m}$।", question_md: "Eta solve kore daw", sources: [],
            tags: [{ label: "Physics", kind: "subject", origin: "inferred" }], needs_review: false, source_ref: null, flashcards: null, mini_quiz: null,
        },
        flashcards: [
            { front: "বৃষ্টির দিনে ছাতা ধরার কোণ নির্ণয়ে সূত্রটি কী? $\\tan\\theta = \\frac{v_r}{v_m}$", back: "$\\tan\\theta = \\frac{\\text{আনুভূমিক কার্যকর বেগ}}{\\text{বৃষ্টির উলম্ব বেগ}}$ — এখানে $\\theta$ হলো কোণ।" },
            { front: "What is the angle if $\\tan\\theta_3 = 1.5$?", back: "$$\\theta_3 = \\tan^{-1}(1.5) \\approx 56.31^\\circ$$" },
            { front: "সাইকেলের বিপরীতে চললে কার্যকর বেগ কী হয়?", back: "কার্যকর বেগ বাড়ে এবং সাইকেলের দিকে চললে কমে।" },
        ],
        quiz: [
            { question: "বাতাস সাইকেলের দিকে বইলে ছাতা কত কোণে ধরতে হবে? $\\tan\\theta_3 = \\frac{3}{2}$", options: ["$56.31^\\circ$", "$33.69^\\circ$", "$45^\\circ$", "$90^\\circ$"], correct_index: 0, explanation: "$\\theta_3 = \\tan^{-1}(1.5) \\approx 56.31^\\circ$" },
            { question: "কোন সূত্রটি সঠিক?", options: ["$\\tan\\theta = \\frac{v_r}{v_m}$", "$\\sin\\theta = \\frac{v_m}{v_r}$", "বেগ শূন্য হলে বৃষ্টি হতে রক্ষা পাওয়া যায়", "$\\cos\\theta = 1$"], correct_index: 0, explanation: "সূত্র $\\tan\\theta$" },
            { question: "A very long English question about the relative velocity of rain with respect to a moving cyclist, used to test wrapping inside the quiz card?", options: ["An extremely long option text without any math but with many many words to test the wrapping behaviour of the answer buttons on a narrow phone screen", "Short", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "None"], correct_index: 1, explanation: "wrap" },
        ],
        ...over,
    };
}

async function openApp({ width = 1280, height = 900, state = makeState() } = {}) {
    const page = await browser.newPage();
    await page.setViewport({ width, height });
    await page.setBypassServiceWorker(true);                    // service-worker behaviour is covered elsewhere
    const consoleErrors = [];
    page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) consoleErrors.push(m.text()); });
    page.on("pageerror", (e) => consoleErrors.push(e.message));
    await page.setRequestInterception(true);
    page.on("request", (req) => {
        const url = req.url();
        if (url.includes("cdn.jsdelivr.net/npm/marked")) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "marked/lib/marked.umd.js")) });
        if (url.includes("mathjax@3/es5/tex-svg.js")) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "mathjax/es5/tex-svg.js")) });
        if (url.includes("supabase-js")) return req.respond({ status: 200, contentType: "application/javascript", body: SUPABASE_STUB });
        if (/fonts\.(googleapis|gstatic)\.com|gstatic\.com\/firebasejs/.test(url)) return req.respond({ status: 200, contentType: "text/css", body: "" });
        const u = new URL(url);
        if (u.origin === new URL(BASE).origin && u.pathname.startsWith("/api/")) {
            state.requests.push(`${req.method()} ${u.pathname}`);
            const json = (status, body) => req.respond({ status, contentType: "application/json", body: JSON.stringify(body) });
            if (u.pathname === "/api/profile" && req.method() === "GET") return json(200, { profile: { name: "Tester", user_class: "Class 9-10 (SSC)", stream: "Science" } });
            const m = u.pathname.match(/^\/api\/notes(?:\/([^/]+))?(?:\/(flashcards|mini-quiz))?$/);
            if (m) {
                const [, id, action] = m;
                if (!id) return json(200, { notes: [state.note], limit: 50, offset: 0, has_more: false });
                if (action) {
                    const regen = (req.postData() || "").includes("regenerate");
                    const col = action === "flashcards" ? "flashcards" : "mini_quiz";
                    if (!state.note[col] || regen) state.note[col] = action === "flashcards" ? state.flashcards : state.quiz;
                    return json(200, { [col]: state.note[col], note: state.note, regenerated: true });
                }
                return json(200, { note: state.note });
            }
            return json(404, { detail: "nope" });
        }
        return req.continue();
    });
    await page.goto(BASE, { waitUntil: "networkidle0" });
    await page.waitForFunction(() => window.MathJax && window.MathJax.typesetPromise && window.KognitNotes, { timeout: 20000 });
    // Record every element MathJax is asked to typeset and whether it was attached/visible at that moment.
    await page.evaluate(() => {
        window.__typesetCalls = [];
        const orig = window.MathJax.typesetPromise.bind(window.MathJax);
        window.MathJax.typesetPromise = (els) => {
            (els || []).forEach((e) => window.__typesetCalls.push({ connected: e.isConnected, laidOut: e.isConnected && e.getClientRects().length > 0, cls: e.className }));
            return orig(els);
        };
    });
    return { page, state, consoleErrors, close: () => page.close() };
}

const click = (page, sel) => page.evaluate((s) => document.querySelector(s).click(), sel);
const clickButton = (page, text) => page.evaluate((t) => { const b = [...document.querySelectorAll("#notes-view button")].find((x) => x.textContent.trim() === t); if (!b) throw new Error("no button " + t); b.click(); }, text);
async function openNote(page) {
    await click(page, "#nav-tab-notes");
    await page.waitForSelector(".note-card", { timeout: 15000 });
    await click(page, ".note-card");
    await page.waitForSelector(".notes-detail-title");
}
async function settle(page, ms = 700) { await sleep(ms); await page.evaluate(() => window.MathJax.startup.promise); await sleep(150); }

// Layout facts for every MathJax SVG under `sel`.
const svgFacts = (page, sel) => page.evaluate((sel) => {
    const root = document.querySelector(sel);
    const svgs = [...root.querySelectorAll("svg")];
    return svgs.map((s) => {
        const r = s.getBoundingClientRect();
        return { w: r.width, h: r.height, attrW: s.getAttribute("width"), attrH: s.getAttribute("height"), viewBox: s.getAttribute("viewBox"), paths: s.querySelectorAll("path,use,text").length };
    });
}, sel);

function assertSaneSvgs(facts, label, { maxW = 480, maxH = 90 } = {}) {
    assert(facts.length > 0, `${label}: no MathJax SVG found - math did not render at all`);
    for (const f of facts) {
        assert(!/NaN/.test(`${f.attrW} ${f.attrH} ${f.viewBox}`), `${label}: MathJax produced NaN sizes (${f.attrW} x ${f.attrH}, viewBox ${f.viewBox})`);
        assert(f.w > 0 && f.h > 0, `${label}: zero-size math`);
        assert(f.w <= maxW && f.h <= maxH, `${label}: math is ${Math.round(f.w)}x${Math.round(f.h)}px - far larger than ordinary text (the giant-glyph bug)`);
        assert(f.paths > 0, `${label}: SVG has no glyph content`);
    }
}

const noHorizontalOverflow = (page) => page.evaluate(() => ({
    doc: document.documentElement.scrollWidth, win: window.innerWidth,
    pane: (() => { const p = document.querySelector(".notes-detail-pane"); return p ? p.scrollWidth - p.clientWidth : 0; })(),
}));

/* ============================================================ */
console.log(`\nReal-browser test using: ${EXE}`);
const extraArgs = (process.env.CHROME_ARGS || "").split(/\s+/).filter(Boolean);
const browser = await puppeteer.launch({ executablePath: EXE, headless: "shell", args: ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage", ...extraArgs] });
console.log("Browser version:", await browser.version());

console.log("\nFlashcards");
await check("flashcards render at normal size: no NaN, formulas are text-sized, nothing overflows (the reported bug)", async () => {
    const app = await openApp();
    await openNote(app.page);
    await clickButton(app.page, "Create flashcards");
    await app.page.waitForSelector(".notes-flash");
    await settle(app.page);
    assertSaneSvgs(await svgFacts(app.page, ".notes-flash"), "flashcard");
    const card = await app.page.evaluate(() => { const c = document.querySelector(".notes-flash-card").getBoundingClientRect(); return { w: c.width, h: c.height }; });
    assert(card.h < 400, `flashcard is ${Math.round(card.h)}px tall`);
    const o = await noHorizontalOverflow(app.page);
    assert(o.doc <= o.win && o.pane <= 1, `horizontal overflow: ${JSON.stringify(o)}`);
    assert(app.consoleErrors.filter((e) => /NaN/.test(e)).length === 0, "console reported NaN SVG attributes: " + app.consoleErrors.join(" | "));
    await app.close();
});

await check("every card (Bangla+math, display math, plain Bangla) renders sanely as the student navigates, and Next advances exactly ONE card", async () => {
    const app = await openApp();
    await openNote(app.page);
    await clickButton(app.page, "Create flashcards");
    await app.page.waitForSelector(".notes-flash");
    await settle(app.page);
    for (let i = 1; i <= 3; i++) {
        eq(await app.page.$eval(".notes-flash-count", (e) => e.textContent), `Card ${i} of 3`);
        const facts = await svgFacts(app.page, ".notes-flash");
        if (facts.length) assertSaneSvgs(facts, `card ${i} front`);
        await clickButton(app.page, "Show answer"); await settle(app.page, 400);
        const back = await svgFacts(app.page, ".notes-flash");
        if (back.length) assertSaneSvgs(back, `card ${i} back`);
        if (i < 3) { await clickButton(app.page, "Next"); await settle(app.page, 400); }
    }
    await clickButton(app.page, "Previous"); await settle(app.page, 400);
    eq(await app.page.$eval(".notes-flash-count", (e) => e.textContent), "Card 2 of 3", "Previous must move exactly one card");
    await app.close();
});

console.log("\nMini-quiz");
await check("mini-quiz questions and options render at normal size and stay inside their boxes", async () => {
    const app = await openApp();
    await openNote(app.page);
    await clickButton(app.page, "Create mini-quiz");
    await app.page.waitForSelector(".notes-quiz");
    await settle(app.page);
    assertSaneSvgs(await svgFacts(app.page, ".notes-quiz"), "quiz");
    const layout = await app.page.evaluate(() => [...document.querySelectorAll(".notes-quiz-q")].map((q) => {
        const qr = q.getBoundingClientRect();
        return [...q.querySelectorAll(".notes-quiz-opt")].map((o) => {
            const r = o.getBoundingClientRect();
            return { inside: r.left >= qr.left - 1 && r.right <= qr.right + 1, fontPx: parseFloat(getComputedStyle(o).fontSize), overflowX: o.scrollWidth - o.clientWidth, h: r.height };
        });
    }).flat());
    for (const o of layout) {
        assert(o.inside, "an option extends outside its question box");
        assert(o.fontPx <= 20, `option font is ${o.fontPx}px`);
        assert(o.overflowX <= 1, `option content overflows horizontally by ${o.overflowX}px`);
        assert(o.h < 200, `option is ${Math.round(o.h)}px tall`);
    }
    const o = await noHorizontalOverflow(app.page);
    assert(o.doc <= o.win && o.pane <= 1, `horizontal overflow: ${JSON.stringify(o)}`);
    await app.close();
});

await check("selecting options and checking answers keeps math sane, keeps focus, and never re-renders the note body", async () => {
    const app = await openApp();
    await openNote(app.page);
    await clickButton(app.page, "Create mini-quiz");
    await app.page.waitForSelector(".notes-quiz");
    await settle(app.page);
    const bodyMathBefore = await app.page.evaluate(() => { const b = document.querySelector(".notes-body"); b.__marker = "same-node"; return b.querySelectorAll("mjx-container").length; });
    // keyboard: focus the first option and press Space
    await app.page.focus('[data-fk="q0o0"]');
    await app.page.keyboard.press("Space"); await settle(app.page, 500);
    const afterSelect = await app.page.evaluate(() => ({
        focusKey: document.activeElement && document.activeElement.dataset.fk,
        checked: document.querySelector('[data-fk="q0o0"]').getAttribute("aria-checked"),
        bodyMarker: document.querySelector(".notes-body").__marker,
        bodyMath: document.querySelector(".notes-body").querySelectorAll("mjx-container").length,
    }));
    eq(afterSelect.checked, "true"); eq(afterSelect.focusKey, "q0o0", "keyboard focus must stay on the chosen option");
    eq(afterSelect.bodyMarker, "same-node", "the note body must not be rebuilt by a quiz click");
    eq(afterSelect.bodyMath, bodyMathBefore, "the note body's math must not be re-typeset");
    assertSaneSvgs(await svgFacts(app.page, ".notes-quiz"), "quiz after selection");
    await click(app.page, '[data-fk="q1o0"]'); await click(app.page, '[data-fk="q2o1"]'); await settle(app.page, 500);
    await clickButton(app.page, "Check answers"); await settle(app.page, 800);
    eq(await app.page.$eval(".notes-quiz-score", (e) => e.textContent), "Score: 3 / 3");
    assertSaneSvgs(await svgFacts(app.page, ".notes-quiz"), "quiz after checking (explanations)");
    assert(!app.state.requests.some((r) => /\/api\/quiz/.test(r)), "self-graded quiz must never call a quiz endpoint");
    await app.close();
});

console.log("\nLifecycle (no duplicates, no stale typesetting)");
await check("regenerating and reopening never duplicates the component or its handlers", async () => {
    const app = await openApp();
    await openNote(app.page);
    await clickButton(app.page, "Create flashcards"); await app.page.waitForSelector(".notes-flash"); await settle(app.page);
    await clickButton(app.page, "Regenerate flashcards"); await settle(app.page, 1000);
    eq(await app.page.$$eval(".notes-flash", (e) => e.length), 1, "exactly one flashcard component after regenerate");
    await clickButton(app.page, "Next"); await settle(app.page, 400);
    eq(await app.page.$eval(".notes-flash-count", (e) => e.textContent), "Card 2 of 3", "one click = one card (no duplicate handlers)");
    await clickButton(app.page, "Create mini-quiz"); await app.page.waitForSelector(".notes-quiz"); await settle(app.page);
    await clickButton(app.page, "Regenerate mini-quiz").catch(() => {});      // only present after checking answers
    // leave and re-open the note
    await clickButton(app.page, "\u2190 All notes"); await app.page.waitForSelector(".note-card");
    await click(app.page, ".note-card"); await app.page.waitForSelector(".notes-detail-title");
    await clickButton(app.page, "Open flashcards"); await app.page.waitForSelector(".notes-flash"); await settle(app.page);
    eq(await app.page.$$eval(".notes-flash", (e) => e.length), 1);
    eq(await app.page.$$eval(".notes-quiz", (e) => e.length), 0, "quiz from the previous visit must not reappear");
    assertSaneSvgs(await svgFacts(app.page, ".notes-flash"), "reopened flashcard");
    await app.close();
});

await check("MathJax is only ever asked to typeset elements that are ATTACHED and VISIBLE (root cause pinned)", async () => {
    const app = await openApp();
    await openNote(app.page);
    await clickButton(app.page, "Create flashcards"); await app.page.waitForSelector(".notes-flash"); await settle(app.page);
    await clickButton(app.page, "Show answer"); await clickButton(app.page, "Next"); await settle(app.page, 600);
    await clickButton(app.page, "Create mini-quiz"); await app.page.waitForSelector(".notes-quiz"); await settle(app.page);
    await click(app.page, '[data-fk="q0o1"]'); await settle(app.page, 500);
    const calls = await app.page.evaluate(() => window.__typesetCalls);
    assert(calls.length > 0, "nothing was typeset");
    const bad = calls.filter((c) => !c.connected || !c.laidOut);
    assert(bad.length === 0, `MathJax was asked to typeset ${bad.length} detached/hidden element(s): ${JSON.stringify(bad.slice(0, 3))}`);
    await app.close();
});

await check("a re-render while the Notes tab is HIDDEN (e.g. the connection comes back) does not leave NaN math behind", async () => {
    const app = await openApp();
    await openNote(app.page);
    await clickButton(app.page, "Create flashcards"); await app.page.waitForSelector(".notes-flash"); await settle(app.page);
    await click(app.page, "#nav-tab-chat"); await settle(app.page, 300);                 // Notes tab now display:none
    await app.page.evaluate(() => window.dispatchEvent(new Event("online"))); await settle(app.page, 900);
    await click(app.page, "#nav-tab-notes"); await settle(app.page, 1200);
    assertSaneSvgs(await svgFacts(app.page, ".notes-body"), "note body after tab round-trip");
    const study = await svgFacts(app.page, ".notes-study");
    if (study.length) assertSaneSvgs(study, "study area after tab round-trip");
    const bad = (await app.page.evaluate(() => window.__typesetCalls)).filter((c) => !c.laidOut);
    eq(bad.length, 0, "typeset was requested for a hidden element");
    await app.close();
});

console.log("\nPhone width, long content, security");
await check("390px phone: no horizontal overflow, long options/unbreakable tokens stay inside their question box", async () => {
    const app = await openApp({ width: 390, height: 844 });
    await openNote(app.page);
    await clickButton(app.page, "Create mini-quiz"); await app.page.waitForSelector(".notes-quiz"); await settle(app.page);
    assertSaneSvgs(await svgFacts(app.page, ".notes-quiz"), "phone quiz", { maxW: 340, maxH: 90 });
    const fits = await app.page.evaluate(() => [...document.querySelectorAll(".notes-quiz-q")].every((q) => {
        const qr = q.getBoundingClientRect();
        return [...q.querySelectorAll(".notes-quiz-opt")].every((o) => { const r = o.getBoundingClientRect(); return r.left >= qr.left - 1 && r.right <= qr.right + 1; });
    }));
    assert(fits, "an option overflowed its question box on a phone");
    const o = await noHorizontalOverflow(app.page);
    assert(o.doc <= o.win, `page scrolls horizontally on a phone: ${JSON.stringify(o)}`);
    await clickButton(app.page, "Create flashcards").catch(() => {});
    await app.close();
});

await check("390px phone: flashcards with Bangla + math fit the screen", async () => {
    const app = await openApp({ width: 390, height: 844 });
    await openNote(app.page);
    await clickButton(app.page, "Create flashcards"); await app.page.waitForSelector(".notes-flash"); await settle(app.page);
    assertSaneSvgs(await svgFacts(app.page, ".notes-flash"), "phone flashcard", { maxW: 340, maxH: 90 });
    await clickButton(app.page, "Show answer"); await settle(app.page, 500);
    assertSaneSvgs(await svgFacts(app.page, ".notes-flash"), "phone flashcard back", { maxW: 340, maxH: 90 });
    const o = await noHorizontalOverflow(app.page);
    assert(o.doc <= o.win, `page scrolls horizontally on a phone: ${JSON.stringify(o)}`);
    await app.close();
});

await check("security: HTML in generated cards/options is shown as literal text, never executed or inserted as elements", async () => {
    const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=2<\/script><b>bold</b>';
    const app = await openApp({ state: makeState({
        flashcards: [{ front: evil, back: evil }, { front: "f2", back: "b2" }, { front: "f3", back: "b3" }],
        quiz: [{ question: evil, options: [evil, "b", "c", "d"], correct_index: 0, explanation: evil }, { question: "q2", options: ["a", "b"], correct_index: 1, explanation: "" }],
    }) });
    await openNote(app.page);
    await clickButton(app.page, "Create flashcards"); await app.page.waitForSelector(".notes-flash"); await settle(app.page);
    await clickButton(app.page, "Show answer"); await settle(app.page, 300);
    await clickButton(app.page, "Create mini-quiz"); await app.page.waitForSelector(".notes-quiz"); await settle(app.page);
    const r = await app.page.evaluate(() => ({
        pwned: window.__pwned,
        injected: document.querySelectorAll(".notes-study img, .notes-study script, .notes-study b").length,
        shownAsText: document.querySelector(".notes-study").textContent.includes("<img src=x"),
    }));
    eq(r.pwned, undefined, "injected script/handler executed");
    eq(r.injected, 0, "HTML elements were created from generated text");
    assert(r.shownAsText, "the literal text should be visible");
    await app.close();
});

await browser.close();
server.close();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) {
    console.log("\nFailures:");
    for (const f of failures) console.log(` - ${f.name}\n   ${f.e.message}`);
    process.exit(1);
}
