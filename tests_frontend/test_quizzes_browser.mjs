/* ============================================================
   KLORIZ QUIZZES - REAL-BROWSER REGRESSION TEST
   ============================================================
   Drives a REAL Chrome/Chromium/Edge (puppeteer-core) against the REAL
   templates/index.html, app.js, nav-views.js, style.css and the REAL MathJax
   tex-svg bundle from node_modules. Only Supabase and the backend API are
   faked. jsdom cannot see layout or real MathJax output, and it also cannot
   reproduce the history bug: app.js declares `const supabaseClient`, which is
   NOT a property of `window`, so a stub that sets window.supabaseClient hides
   the bug. Here the fake Supabase goes through the real
   supabase.createClient(...) path, exactly like production.

   PINS THREE BUGS
     A. A signed-in student saw "Log in to see topics from your quiz history."
        (nav-views.js read window.supabaseClient, which is always undefined.)
     B. The Quizzes primary buttons / selected answers / progress text used the
        indigo quiz accent; they must be white/neutral (Quizzes only).
     C. Quiz questions/options/explanations containing LaTeX (e.g. a bare
        \begin{bmatrix}...\end{bmatrix}) were shown as raw source: nothing ever
        called MathJax for quiz content.

   BROWSER DISCOVERY: same as test_notes_browser.mjs - set CHROME_PATH, or a
   usual install location is used. With no browser it prints SKIPPED (loudly).

   Run: node tests_frontend/test_quizzes_browser.mjs
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
    console.log("\nSKIPPED: no Chrome/Chromium/Edge found (set CHROME_PATH to run the real-browser Quizzes test).");
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

// Goes through the REAL supabase.createClient(...) -> `const supabaseClient` path.
const supabaseStub = (signedIn) => `window.supabase = { createClient: () => ({ auth: {
  getSession: async () => ({ data: { session: ${signedIn ? '{ access_token: "tok", user: { id: "user-1", email: "t@example.com" } }' : "null"} } }),
  onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }), signOut: async () => ({}) },
  from: () => { const q = { select: () => q, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => q, eq: async () => ({ error: null }) }; return q; } }) };`;

const MATRIX_Q = { id: 3, question: "A = \\begin{bmatrix} 2 & 5 & 8 & -1 \\\\ 3 & -4 & 7 \\\\ 0 & 1 & 6 \\end{bmatrix} ম্যাট্রিক্সটির ট্রেস (Trace) কত?", options: ["4", "12", "8", "-4"], correct_index: 0, explanation: "Trace = sum of the diagonal." };
const QUESTIONS = [
    { id: 1, question: "A = \\begin{bmatrix} x-1 & 2 \\\\ 3 & 6 \\end{bmatrix} একটি ব্যতিক্রমী (Singular) ম্যাট্রিক্স হলে, x এর মান কত?", options: ["1", "2", "0", "3"], correct_index: 3, explanation: "det = 0" },
    { id: 2, question: "যদি $a<b$ এবং $\\frac{x^{2}}{3} + \\sqrt{y_1} = 5$ হয় তবে কোনটি সঠিক?", options: ["$x^2 + y_1$", "\\(\\frac{1}{2}\\)", "প্লেইন টেক্সট", "$$\\sum_{i=1}^{n} i$$"], correct_index: 0, explanation: "Because $a<b$ and $\\alpha+\\beta$." },
    MATRIX_Q,
    { id: 4, question: "ভারতের রাজধানী কোথায়? Plain text question with <b>bold</b> allowed.", options: ["Dhaka", "Delhi", "Kolkata", "Mumbai"], correct_index: 1, explanation: "Plain." },
    { id: 5, question: "<img src=x onerror=\"window.__xss=1\"> $\\href{javascript:alert(1)}{x}$ check", options: ["a", "b", "c", "d"], correct_index: 0, explanation: "x" },
];
const SNAPSHOT = { strengths: [{ source: "quiz", subject: "Math", topic: "Algebra", topic_key: "algebra" }], needs_practice: [{ subject: "Physics", topic: "Vectors", topic_key: "vectors" }], recent_progress: {}, evidence_summary: { has_quiz_evidence: true } };

const browser = await puppeteer.launch({ executablePath: EXE, headless: true, args: ["--no-sandbox", ...(process.env.CHROME_ARGS ? process.env.CHROME_ARGS.split(" ") : [])] });

async function openApp({ signedIn = true, width = 1280, height = 900, theme = "dark", snapshot = () => ({ status: 200, body: SNAPSHOT }), submitStatus = 200 } = {}) {
    const page = await browser.newPage();
    await page.setViewport({ width, height });
    await page.setBypassServiceWorker(true);
    const consoleErrors = [];
    const counts = { snapshot: 0, submit: 0 };
    // "fetching the script" = the service worker registration, which this harness does not serve.
    page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource|fetching the script/.test(m.text())) consoleErrors.push(m.text()); });
    page.on("pageerror", (e) => consoleErrors.push(e.message));
    await page.evaluateOnNewDocument((t) => { try { localStorage.setItem("kognit-theme", t); } catch (e) {} }, theme);
    await page.setRequestInterception(true);
    page.on("request", (req) => {
        const url = req.url();
        if (url.includes("cdn.jsdelivr.net/npm/marked")) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "marked/lib/marked.umd.js")) });
        if (url.includes("mathjax@3/es5/tex-svg.js")) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "mathjax/es5/tex-svg.js")) });
        if (url.includes("supabase-js")) return req.respond({ status: 200, contentType: "application/javascript", body: supabaseStub(signedIn) });
        if (/fonts\.(googleapis|gstatic)\.com|gstatic\.com\/firebasejs/.test(url)) return req.respond({ status: 200, contentType: "text/css", body: "" });
        const u = new URL(url);
        if (u.origin === new URL(BASE).origin && u.pathname.startsWith("/api/")) {
            const json = (status, body) => req.respond({ status, contentType: "application/json", body: JSON.stringify(body) });
            if (u.pathname === "/api/profile" && req.method() === "GET") return json(200, { profile: { name: "Tester", user_class: "Class 9-10 (SSC)", stream: "Science" } });
            if (u.pathname === "/api/profile/snapshot") { counts.snapshot++; const r = snapshot(counts.snapshot); return json(r.status, r.body); }
            if (u.pathname === "/api/quiz/generate") return json(200, { questions: QUESTIONS, quiz_id: "quiz-1" });
            if (u.pathname === "/api/quiz/submit") { counts.submit++; return json(submitStatus, {}); }
            if (u.pathname === "/api/profile/mistakes") return json(200, []);
            if (u.pathname === "/api/profile/next-steps") return json(200, {});
            return json(404, { detail: "nope" });
        }
        return req.continue();
    });
    await page.goto(BASE, { waitUntil: "networkidle0" });
    await page.waitForFunction(() => window.MathJax && window.MathJax.typesetPromise && window.KognitNav, { timeout: 20000 });
    return { page, counts, consoleErrors, close: () => page.close() };
}

const click = (page, sel) => page.evaluate((s) => document.querySelector(s).click(), sel);
const openQuizzes = async (page) => { await click(page, "#nav-tab-quizzes"); await sleep(500); };
async function startQuiz(page) {
    await click(page, "#quizzes-view-start-btn");
    await page.type("#quiz-topic-input", "Matrix");
    await click(page, "#quiz-setup-view .start-quiz-btn");
    await page.waitForSelector(".quiz-q-title", { timeout: 10000 });
    await page.evaluate(() => window.MathJax.startup.promise);
    await sleep(500);
}
const nextQuestion = async (page) => { await page.evaluate(() => document.querySelector(".quiz-opt-btn").click()); await click(page, "#quiz-next-btn"); await sleep(600); };
const questionFacts = (page) => page.evaluate(() => {
    const t = document.querySelector(".quiz-q-title");
    const rest = t.cloneNode(true); rest.querySelectorAll("mjx-container").forEach((m) => m.remove());
    const svgs = [...t.querySelectorAll("mjx-container svg")].map((s) => { const r = s.getBoundingClientRect(); return { w: r.width, h: r.height, attr: `${s.getAttribute("width")} ${s.getAttribute("height")} ${s.getAttribute("viewBox")}` }; });
    return { leftover: rest.textContent, svgs, bolds: t.querySelectorAll("b").length, imgs: document.querySelectorAll("#quiz-modal img").length };
});
const isBlueish = (rgb) => { const m = (rgb || "").match(/[\d.]+/g); if (!m || m.length < 3) return false; const [r, g, b] = m.map(Number); const a = m.length > 3 ? Number(m[3]) : 1; return a > 0.05 && b > r + 35 && b > g + 25; };
const colorAudit = (page, root) => page.evaluate((root) => {
    const out = [];
    document.querySelectorAll(root + " *").forEach((e) => {
        const s = getComputedStyle(e);
        ["color", "backgroundColor", "borderTopColor", "boxShadow"].forEach((k) => { if (k === "borderTopColor" && s.borderTopWidth === "0px") return; out.push([String(e.className || e.tagName), k, s[k]]); });
        if (s.outlineStyle !== "none") out.push([String(e.className || e.tagName), "outlineColor", s.outlineColor]);
    });
    return out;
}, root);

/* ------------------------------------------------------------ A */
console.log("\nA. Quiz history on the Quizzes page");

await check("a SIGNED-IN student never sees the login message and gets their history-derived topics", async () => {
    const app = await openApp({ signedIn: true });
    await openQuizzes(app.page);
    const text = await app.page.$eval("#quizzes-view", (e) => e.innerText);
    assert(!text.includes("Log in to see topics"), "false login message shown to a signed-in student: " + text);
    assert(text.includes("Physics") && text.includes("Algebra"), "history-derived chips missing: " + text);
    await app.close();
});

await check("a genuinely logged-out visitor still gets the login guidance (and no snapshot request is made)", async () => {
    const app = await openApp({ signedIn: false });
    await openQuizzes(app.page);
    assert((await app.page.$eval("#quizzes-view", (e) => e.innerText)).includes("Log in to see topics from your quiz history."));
    eq(app.counts.snapshot, 0);
    await app.close();
});

await check("a failed history request is an ERROR with Try again (not empty, not login), and retry recovers", async () => {
    let failing = true;
    const app = await openApp({ snapshot: () => failing ? { status: 502, body: {} } : { status: 200, body: SNAPSHOT } });
    await openQuizzes(app.page);
    const text = await app.page.$eval("#quizzes-view", (e) => e.innerText);
    assert(/couldn.t load your topics/i.test(text) && text.includes("Try again"), text);
    assert(!text.includes("Log in") && !text.includes("Take a quiz"), "error was shown as logged-out/empty: " + text);
    failing = false;
    await click(app.page, ".quizzes-retry-btn"); await sleep(500);
    eq(await app.page.$$eval(".quizzes-topic-chip", (e) => e.length), 2);
    await app.close();
});

await check("saved quizzes but no qualifying topic -> honest message; truly no history -> 'Take a quiz'", async () => {
    let app = await openApp({ snapshot: () => ({ status: 200, body: { strengths: [], needs_practice: [], evidence_summary: { has_quiz_evidence: true } } }) });
    await openQuizzes(app.page);
    let text = await app.page.$eval("#quizzes-view", (e) => e.innerText);
    assert(text.includes("Your quiz results are saved") && !text.includes("Log in"), text);
    eq(await app.page.$$eval(".quizzes-topic-chip", (e) => e.length), 0, "no invented topics");
    await app.close();
    app = await openApp({ snapshot: () => ({ status: 200, body: { strengths: [], needs_practice: [], evidence_summary: { has_quiz_evidence: false } } }) });
    await openQuizzes(app.page);
    assert((await app.page.$eval("#quizzes-view", (e) => e.innerText)).includes("Take a quiz to start building your topic history here."));
    await app.close();
});

/* ------------------------------------------------------------ C */
console.log("\nC. Math in quiz questions, answers and results (real MathJax)");

await check("the screenshot's matrix question renders as a real matrix, surrounding Bengali/English text intact", async () => {
    const app = await openApp();
    await openQuizzes(app.page); await startQuiz(app.page);
    const q1 = await questionFacts(app.page);
    assert(q1.svgs.length >= 1, "no MathJax output: the raw LaTeX would still be on screen");
    assert(!q1.leftover.includes("\\begin") && !q1.leftover.includes("bmatrix") && !q1.leftover.includes("\\\\"), "raw LaTeX left in the text: " + q1.leftover);
    assert(q1.leftover.includes("ব্যতিক্রমী") && q1.leftover.includes("(Singular)"), "surrounding text lost: " + q1.leftover);
    for (const s of q1.svgs) assert(!/NaN/.test(s.attr) && s.w > 0 && s.h > 0, "bad SVG size " + s.attr);
    assert(q1.svgs[0].h > 40, "matrix is a single line, not a multi-row layout (h=" + q1.svgs[0].h + ")");
    await app.close();
});

await check("selecting an answer updates in place: math stays rendered, focus kept, aria-pressed set", async () => {
    const app = await openApp();
    await openQuizzes(app.page); await startQuiz(app.page);
    const before = await app.page.evaluate(() => document.querySelector(".quiz-q-title mjx-container svg"));
    await app.page.evaluate(() => { window.__mj = document.querySelector(".quiz-q-title mjx-container"); document.querySelectorAll(".quiz-opt-btn")[1].focus(); document.querySelectorAll(".quiz-opt-btn")[1].click(); });
    const st = await app.page.evaluate(() => ({
        same: window.__mj === document.querySelector(".quiz-q-title mjx-container"),
        sel: [...document.querySelectorAll(".quiz-opt-btn")].map((b) => b.classList.contains("selected") + ":" + b.getAttribute("aria-pressed")),
        focus: document.activeElement.className,
    }));
    assert(st.same, "the typeset math node was thrown away and rebuilt");
    eq(st.sel.join(","), "false:false,true:true,false:false,false:false");
    assert(st.focus.includes("quiz-opt-btn"), "keyboard focus was lost");
    await app.close();
});

await check("inline $a<b$ is math (not swallowed as an HTML tag); math in answer choices renders; plain choice untouched", async () => {
    const app = await openApp();
    await openQuizzes(app.page); await startQuiz(app.page);
    await nextQuestion(app.page);
    const q2 = await questionFacts(app.page);
    eq(q2.bolds, 1, "an <b> tag appeared inside the question: $a<b$ was parsed as HTML");
    assert(q2.svgs.length >= 2 && !q2.leftover.includes("$") && !q2.leftover.includes("\\frac"), JSON.stringify(q2));
    const opts = await app.page.evaluate(() => [...document.querySelectorAll(".quiz-opt-btn")].map((b) => { const c = b.cloneNode(true); c.querySelectorAll("mjx-container").forEach((m) => m.remove()); return { mjx: b.querySelectorAll("mjx-container svg").length, rest: c.textContent.trim() }; }));
    assert(opts[0].mjx >= 1 && opts[1].mjx >= 1 && opts[3].mjx >= 1, JSON.stringify(opts));
    assert(opts[2].mjx === 0 && opts[2].rest === "C. প্লেইন টেক্সট", JSON.stringify(opts[2]));
    await app.close();
});

await check("moving between questions re-typesets; plain questions are unchanged; hostile HTML/LaTeX stays inert", async () => {
    const app = await openApp();
    await openQuizzes(app.page); await startQuiz(app.page);
    await nextQuestion(app.page);                    // -> Q2
    await nextQuestion(app.page);                    // -> Q3 (trace matrix)
    const q3 = await questionFacts(app.page);
    assert(q3.svgs.length >= 1 && !q3.leftover.includes("\\begin") && q3.leftover.includes("ম্যাট্রিক্সটির ট্রেস"), JSON.stringify(q3));
    await nextQuestion(app.page);                    // -> Q4 (plain)
    const q4 = await questionFacts(app.page);
    assert(q4.svgs.length === 0 && q4.leftover.includes("Plain text question") && q4.bolds === 2, JSON.stringify(q4));
    await nextQuestion(app.page);                    // -> Q5 (hostile)
    const hostile = await app.page.evaluate(() => ({ xss: window.__xss === 1, imgs: document.querySelectorAll("#quiz-modal img").length, handlers: document.querySelectorAll("#quiz-modal [onerror]").length, links: document.querySelectorAll("#quiz-modal a[href]").length }));
    assert(!hostile.xss && hostile.imgs === 0 && hostile.handlers === 0 && hostile.links === 0, JSON.stringify(hostile));
    await app.close();
});

await check("results screen typesets math in questions, answers and explanations; the attempt is saved once and Your Topics refreshes", async () => {
    const app = await openApp();
    await openQuizzes(app.page); await startQuiz(app.page);
    const snapshotsBefore = app.counts.snapshot;
    for (let i = 0; i < 5; i++) await nextQuestion(app.page);
    await sleep(900);
    const res = await app.page.evaluate(() => { const l = document.getElementById("result-details-list"); const c = l.cloneNode(true); c.querySelectorAll("mjx-container").forEach((m) => m.remove()); return { mjx: l.querySelectorAll("mjx-container svg").length, raw: /\\begin|\\frac|\$/.test(c.textContent) }; });
    assert(res.mjx >= 4 && !res.raw, JSON.stringify(res));
    eq(app.counts.submit, 1, "attempt must be submitted exactly once");
    assert((await app.page.$eval("#quiz-save-status", (e) => e.textContent)).includes("Result saved"));
    assert(app.counts.snapshot > snapshotsBefore, "Your Topics was not refreshed after the save");
    assert(app.consoleErrors.length === 0, "console errors: " + app.consoleErrors.join(" | "));
    await app.close();
});

await check("a FAILED save is reported as not saved and does not refresh Your Topics", async () => {
    const app = await openApp({ submitStatus: 500 });
    await openQuizzes(app.page); await startQuiz(app.page);
    const before = app.counts.snapshot;
    for (let i = 0; i < 5; i++) await nextQuestion(app.page);
    await sleep(900);
    const status = await app.page.$eval("#quiz-save-status", (e) => e.textContent);
    assert(!status.includes("Result saved") && status.includes("Could not save"), status);
    eq(app.counts.snapshot, before);
    await app.close();
});

/* ------------------------------------------------------------ B */
console.log("\nB. White accent inside Quizzes only");

for (const theme of ["dark", "light"]) {
    await check(`${theme} theme: no blue/indigo on the Quizzes page, modal, question, hover, selected, focus and results`, async () => {
        const app = await openApp({ theme });
        await openQuizzes(app.page);
        let bad = (await colorAudit(app.page, "#quizzes-view")).filter((x) => isBlueish(x[2]));
        assert(bad.length === 0, "Quizzes page: " + JSON.stringify(bad.slice(0, 4)));
        await app.page.hover("#quizzes-view-start-btn");
        await app.page.focus("#quizzes-view-start-btn"); await app.page.keyboard.press("Tab"); await app.page.keyboard.down("Shift"); await app.page.keyboard.press("Tab"); await app.page.keyboard.up("Shift");
        const btn = await app.page.$eval("#quizzes-view-start-btn", (e) => { const s = getComputedStyle(e); return { bg: s.backgroundColor, fg: s.color, outline: s.outlineStyle + " " + s.outlineColor }; });
        assert(!isBlueish(btn.bg) && !isBlueish(btn.outline), JSON.stringify(btn));
        // dark theme: light button, dark label. light theme: dark button, light label.
        const lum = (rgb) => { const [r, g, b] = rgb.match(/[\d.]+/g).map(Number); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
        if (theme === "dark") assert(lum(btn.bg) > 200 && lum(btn.fg) < 80, JSON.stringify(btn)); else assert(lum(btn.bg) < 80 && lum(btn.fg) > 200, JSON.stringify(btn));
        await startQuiz(app.page);
        await app.page.hover(".quiz-opt-btn:nth-child(3)");
        await app.page.evaluate(() => { document.querySelectorAll(".quiz-opt-btn")[1].focus(); document.querySelectorAll(".quiz-opt-btn")[1].click(); });
        await sleep(400);   // .quiz-opt-btn has a 0.2s transition; read settled styles
        bad = (await colorAudit(app.page, "#quiz-modal")).filter((x) => isBlueish(x[2]));
        assert(bad.length === 0, "quiz question: " + JSON.stringify(bad.slice(0, 5)));
        const sel = await app.page.evaluate(() => { const s = getComputedStyle(document.querySelector(".quiz-opt-btn.selected")); const u = getComputedStyle(document.querySelector(".quiz-opt-btn:not(.selected)")); return { sb: s.borderTopColor, ub: u.borderTopColor, fw: Number(s.fontWeight) }; });
        assert(sel.sb !== sel.ub && sel.fw >= 600, "selected answer is not distinguishable: " + JSON.stringify(sel));
        for (let i = 0; i < 5; i++) await nextQuestion(app.page);
        await sleep(700);
        bad = (await colorAudit(app.page, "#quiz-modal")).filter((x) => isBlueish(x[2]));
        assert(bad.length === 0, "results: " + JSON.stringify(bad.slice(0, 4)));
        const fb = await app.page.$eval(".result-item", (e) => e.getAttribute("style"));
        assert(/#4ade80|#f87171/.test(fb), "semantic correct/incorrect colours were lost");
        await app.close();
    });
}

await check("scope: the shared indigo token is untouched, so other sections keep their look", async () => {
    const tokens = fs.readFileSync(path.join(ROOT, "static/css/tokens.css"), "utf8");
    assert(/--color-accent-quiz:\s+var\(--indigo-600\)/.test(tokens), "tokens.css must not be edited for this fix");
    const css = fs.readFileSync(path.join(ROOT, "static/css/style.css"), "utf8");
    const block = (sel) => { const i = css.indexOf("\n" + sel + " {"); assert(i >= 0, sel + " block missing"); return css.slice(i, css.indexOf("}", i)); };
    for (const sel of [".start-quiz-btn", ".quizzes-start-btn", ".quiz-opt-btn.selected", ".quiz-progress-bar"]) {
        assert(!/accent-quiz|indigo/.test(block(sel)), sel + " still references the indigo accent");
    }
    assert(/\.pill-btn:hover\s*\{[^}]*accent-quiz/.test(css), "non-Quizzes consumers of the token must be unchanged");
});

/* ------------------------------------------------------------ layout */
console.log("\nLayout");

await check("375px mobile: no horizontal overflow on the Quizzes page or inside the modal; the matrix fits", async () => {
    const app = await openApp({ width: 375, height: 740 });
    await app.page.evaluate(() => window.KognitNav.switchTo("quizzes")); await sleep(500);
    await startQuiz(app.page);
    await nextQuestion(app.page); await nextQuestion(app.page);   // Q3: the matrix
    const f = await app.page.evaluate(() => {
        const m = document.querySelector("#quiz-modal .modal-card").getBoundingClientRect();
        const s = document.querySelector(".quiz-q-title mjx-container svg").getBoundingClientRect();
        return { doc: document.documentElement.scrollWidth - document.documentElement.clientWidth, modalX: m.right - m.left, inside: s.left >= m.left - 1 && s.right <= m.right + 1 };
    });
    assert(f.doc <= 0 && f.inside, JSON.stringify(f));
    await app.close();
});

await browser.close();
server.close();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) { for (const f of failures) console.log(`\nFAILED: ${f.name}\n${f.e.stack || f.e.message}`); process.exit(1); }
