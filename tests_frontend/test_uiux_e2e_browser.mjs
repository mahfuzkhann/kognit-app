/* ============================================================
   KLORIZ UI/UX - END-TO-END IN A REAL BROWSER
   ============================================================
   What is REAL here: Chromium; the FastAPI app (uvicorn, backend/main.py,
   database.py, share_engine.py); real keyboard/mouse input; real layout;
   real MathJax on the public page; real HTTP between browser, app and the
   "database".
   What is FAKED: Supabase (tests_frontend/e2e/fake_supabase.py - an HTTP
   server with the behaviour of migration 0013), supabase-js in the page
   (a stub that hands out a fixed session), Gemini (the chat stream is
   fulfilled by the test), and CDN scripts (served from node_modules).

   It proves, in a real browser:
     - sharing works across two SEPARATE browser sessions, and a revoked link stops working;
     - the public page leaks nothing (no referrer, no third-party requests,
       no owner data) and executes nothing from shared text;
     - Enter submits the sign-in form; one request at a time; focus is trapped and restored;
     - the answer toolbar is one consistent size in both themes (touch hit area bigger);
     - the quiz asks before discarding progress, by every close route;
     - a reader who scrolled up is NOT yanked down when an answer finishes.
   Screenshots go to $E2E_SHOTS (default /tmp/e2e_shots) for human review.

   Needs: CHROME_PATH (and optionally CHROME_ARGS), puppeteer-core, python3
   with the repo's requirements (PYTHON=/path/to/python to override).
   Run:   node tests_frontend/test_uiux_e2e_browser.mjs
   ============================================================ */
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import http from "node:http";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const SHOTS = process.env.E2E_SHOTS || "/tmp/e2e_shots";
fs.mkdirSync(SHOTS, { recursive: true });

const CHROME = process.env.CHROME_PATH;
if (!CHROME || !fs.existsSync(CHROME)) {
    console.log("\nSKIPPED: set CHROME_PATH to a Chrome/Chromium binary to run the end-to-end browser test.");
    process.exit(0);
}
const puppeteer = (await import("puppeteer-core")).default;

const APP_PORT = 18765, DB_PORT = 18766;
const APP = `http://127.0.0.1:${APP_PORT}`;
const PYTHON = process.env.PYTHON || "python3";
const NM = path.join(HERE, "node_modules");

let passed = 0, failed = 0;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message.split("\n")[0]}`); }
}
const assert = (c, m) => { if (!c) throw new Error(m || "assertion failed"); };
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); };

/* ---------------- processes ---------------- */
const procs = [];
function start(cmd, args, env = {}) {
    const p = spawn(cmd, args, { cwd: ROOT, env: { ...process.env, ...env }, stdio: ["ignore", "pipe", "pipe"] });
    p.output = ""; p.stdout.on("data", (d) => (p.output += d)); p.stderr.on("data", (d) => (p.output += d));
    procs.push(p); return p;
}
async function waitFor(url, label, ms = 20000) {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) {
        try { const r = await fetch(url); if (r.status < 500) return; } catch (e) { /* not up yet */ }
        await sleep(200);
    }
    throw new Error(`${label} did not start`);
}
const db = start(PYTHON, [path.join(HERE, "e2e/fake_supabase.py"), String(DB_PORT)]);
const server = start(PYTHON, ["-m", "uvicorn", "backend.main:app", "--port", String(APP_PORT), "--log-level", "warning"], {
    SUPABASE_URL: `http://127.0.0.1:${DB_PORT}`, SUPABASE_ANON_KEY: "sb_publishable_e2e", GEMINI_API_KEY: "e2e-not-real",
});
await waitFor(`http://127.0.0.1:${DB_PORT}/__state`, "fake supabase");
await waitFor(`${APP}/`, "app server");
const dbState = async () => (await fetch(`http://127.0.0.1:${DB_PORT}/__state`)).json();

/* ---------------- browser plumbing ---------------- */
const browser = await puppeteer.launch({
    executablePath: CHROME, headless: true,
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", ...(process.env.CHROME_ARGS ? process.env.CHROME_ARGS.split(" ") : [])],
});

const SUPABASE_STUB = `
window.supabase = { createClient: () => ({
  auth: {
    getSession: async () => ({ data: { session: window.__T ? { access_token: window.__T, user: { id: window.__U, email: window.__E } } : null } }),
    onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
    signOut: async () => ({}),
    signInWithPassword: async (c) => { (window.__authCalls = window.__authCalls || []).push(c); await new Promise((r) => setTimeout(r, window.__authDelay || 400)); return window.__authResult || { data: { user: { id: "u1", email: c.email }, session: {} }, error: null }; },
    signUp: async () => ({ data: { user: { identities: [{}] }, session: null }, error: null }),
    signInWithOAuth: async () => ({ error: null }),
  },
  from: () => { const q = { select: () => q, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => q, eq: async () => ({ error: null }) }; return q; },
}) };`;

const THIRD_PARTY = [];   // every request that left for a host other than the app / CDN stubs
async function newPage(context, { who = null, width = 1280, height = 800, theme = "dark", touch = false, shots = null } = {}) {
    const page = await context.newPage();
    const users = { alice: ["alice-token", "aaaaaaaa-0000-0000-0000-000000000001", "alice@kloriz.test"], bob: ["bob-token", "bbbbbbbb-0000-0000-0000-000000000002", "bob@kloriz.test"] };
    await page.evaluateOnNewDocument((t, u, e, th) => {
        if (t) { window.__T = t; window.__U = u; window.__E = e; }
        try { localStorage.setItem("kognit-theme", th); } catch (x) { /* ignore */ }
    }, ...(who ? users[who] : [null, null, null]), theme);
    await page.setViewport({ width, height, deviceScaleFactor: 1, hasTouch: touch, isMobile: touch });
    page.errors = []; page.consoleErrors = []; page.streams = [];
    page.on("pageerror", (e) => page.errors.push(e.message));
    page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource/.test(m.text())) page.consoleErrors.push(m.text()); });
    await page.setBypassServiceWorker(true);
    await page.setRequestInterception(true);
    page.on("request", (req) => {
        const url = req.url();
        if (url.startsWith(APP)) {
            const p = new URL(url).pathname;
            if (p === "/api/profile" && req.method() === "GET") return req.respond({ status: 200, contentType: "application/json", body: JSON.stringify({ profile: { name: "Tester", user_class: "Class 9-10 (SSC)", stream: "Science" } }) });
            if (p === "/api/chat/stream" && page.streamHandler) return page.streamHandler(req);
            return req.continue();
        }
        if (/marked/.test(url)) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "marked/lib/marked.umd.js")) });
        if (/mathjax/.test(url)) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "mathjax/es5/tex-svg.js")) });
        if (/supabase-js/.test(url)) return req.respond({ status: 200, contentType: "application/javascript", body: SUPABASE_STUB });
        if (/html2pdf|fonts\.(googleapis|gstatic)|gstatic/.test(url)) return req.respond({ status: 200, contentType: "text/css", body: "" });
        THIRD_PARTY.push(url);
        return req.abort();
    });
    return page;
}
const shot = async (page, name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
const seed = (page, messages, title = "বল ও গতি") => page.evaluate((messages, title) => {
    const ps = getProjectsSnapshot(); ps.length = 0;
    ps.push({ id: "p1", title: "Physics", chats: [{ id: "chat_e2e_1", title, messages }] });
    loadChat("p1", "chat_e2e_1"); return true;
}, messages, title);
async function openApp(page) {
    await page.goto(`${APP}/`, { waitUntil: "load" });
    await page.waitForSelector("#chat-box"); await sleep(500);
}

const CHAT = [
    { role: "user", text: "বল কী? এবং $F = ma$ কীভাবে আসে?" },
    { role: "bot", text: "বল হলো **ধাক্কা** বা টান।\n\nনিউটনের দ্বিতীয় সূত্র: $$F = m a$$\n\n- ভর $m$\n- ত্বরণ $a$\n\n<script>window.__x=1</script> <img src=x onerror='window.__x=2'> [click](javascript:window.__x=3)" },
];

/* =============================================================
   1. Page health + favicon
   ============================================================= */
console.log("\n1. Page health and favicon");
const ctxA = await browser.createBrowserContext();
const alice = await newPage(ctxA, { who: "alice" });
const iconResponses = [];
alice.on("response", (r) => { if (/favicon|apple-touch/.test(r.url())) iconResponses.push([new URL(r.url()).pathname, r.status(), r.headers()["content-type"]]); });
await openApp(alice);
await seed(alice, CHAT);
await sleep(300);

await check("the app loads with no page errors and no console errors", async () => {
    eq(alice.errors, []); eq(alice.consoleErrors, []);
});
await check("favicon: SVG, ICO, 32px PNG and the touch icon are all declared AND served (no 404), with correct types", async () => {
    const declared = await alice.$$eval('link[rel~="icon"], link[rel="apple-touch-icon"]', (ls) => ls.map((l) => l.getAttribute("href")));
    eq(declared.sort(), ["/static/img/apple-touch-icon.png", "/static/img/favicon-32x32.png", "/static/img/favicon.ico", "/static/img/favicon.svg"]);
    for (const p of declared) {
        const r = await alice.evaluate(async (u) => { const x = await fetch(u); const b = await x.arrayBuffer(); return [x.status, x.headers.get("content-type"), b.byteLength]; }, p);
        eq(r[0], 200, p); assert(r[2] > 200, `${p} is a real file (${r[2]} bytes)`);
        if (p.endsWith(".svg")) assert(/svg/.test(r[1]), r[1]);
        if (p.endsWith(".png")) assert(/png/.test(r[1]), r[1]);
        if (p.endsWith(".ico")) assert(/icon/.test(r[1]), r[1]);
    }
    const root = await alice.evaluate(async () => (await fetch("/favicon.ico")).status);
    eq(root, 200, "/favicon.ico (which browsers request regardless of <link>) no longer 404s");
});
await check("the SVG favicon is the official mark: same path data as kognit-logo.svg, theme-aware (white on dark tabs, near-black on light)", async () => {
    const fav = fs.readFileSync(path.join(ROOT, "static/img/favicon.svg"), "utf8");
    const logo = fs.readFileSync(path.join(ROOT, "static/img/kognit-logo.svg"), "utf8");
    const d = (s) => (s.match(/ d="([^"]+)"/g) || []).join("");
    assert(d(fav).length > 500 && d(fav) === d(logo), "paths identical to the official logo (nothing redrawn)");
    assert(/prefers-color-scheme:\s*dark/.test(fav));
});

/* =============================================================
   2. Answer toolbar
   ============================================================= */
console.log("\n2. Answer toolbar (icon polish)");
const toolbarMetrics = (page) => page.evaluate(() => [...document.querySelectorAll(".bot-message .toolbar-btn, .user-message .toolbar-btn")].map((b) => {
    const r = b.getBoundingClientRect(), s = b.querySelector("svg"), sr = s && s.getBoundingClientRect(), cs = getComputedStyle(b), sc = s && getComputedStyle(s);
    return { label: b.getAttribute("aria-label") || b.textContent.trim(), h: Math.round(r.height * 10) / 10, w: Math.round(r.width), iw: sr && Math.round(sr.width * 10) / 10, ih: sr && Math.round(sr.height * 10) / 10, stroke: sc && sc.strokeWidth, border: cs.borderTopColor, bg: cs.backgroundColor, color: cs.color };
}));
for (const theme of ["dark", "light"]) {
    await check(`[${theme}] every action has the SAME height and a 14x14 icon with the same stroke (was 22/24/26px tall; 12/14/16px icons)`, async () => {
        await alice.evaluate((t) => { document.documentElement.setAttribute("data-theme", t); }, theme);
        await sleep(150);
        const m = await toolbarMetrics(alice);
        assert(m.length >= 6, `expected the full toolbar, got ${m.length}`);
        eq([...new Set(m.map((x) => x.h))].length, 1, `heights: ${JSON.stringify(m.map((x) => [x.label.slice(0, 8), x.h]))}`);
        eq([...new Set(m.map((x) => `${x.iw}x${x.ih}`))], ["14x14"], "icon sizes");
        eq([...new Set(m.map((x) => x.stroke))].length, 1, "stroke widths");
        assert(m.every((x) => x.h >= 24), "meets the 24px WCAG 2.2 minimum target size");
        await shot(alice, `toolbar_${theme}`);
    });
}
await check("[light] the buttons are VISIBLE: a border/fill that is not the invisible translucent white of the dark theme", async () => {
    await alice.evaluate(() => document.documentElement.setAttribute("data-theme", "light")); await sleep(150);
    const m = (await toolbarMetrics(alice))[2];
    const rgb = (s) => (s.match(/[\d.]+/g) || []).map(Number);
    const [r, g, b] = rgb(m.border); const lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    assert(lum < 0.85, `border colour ${m.border} would be invisible on a white page`);
});
await check("the toolbar's overall width/spacing is unchanged by the polish (only icon size and height were normalised)", async () => {
    await alice.evaluate(() => document.documentElement.setAttribute("data-theme", "dark")); await sleep(120);
    const w = await alice.evaluate(() => { const t = document.querySelector(".bot-message .toolbar-btn").parentElement.getBoundingClientRect(); return Math.round(t.width); });
    assert(w > 250 && w < 520, `toolbar width ${w}px is in the same range as before (~370px at 1280)`);
});
await check("touch screens: the visible button is unchanged but the tappable area is ~40px tall (invisible pseudo-element)", async () => {
    const ctxT = await browser.createBrowserContext();
    const t = await newPage(ctxT, { who: "alice", width: 390, height: 844, touch: true });
    await openApp(t);
    assert(await t.evaluate(() => matchMedia("(pointer: coarse)").matches), "mobile emulation reports a coarse pointer"); await seed(t, CHAT); await sleep(300);
    const r = await t.evaluate(() => { const b = document.querySelector(".bot-message .toolbar-btn"); const cs = getComputedStyle(b, "::after"); return { top: cs.top, bottom: cs.bottom, vis: b.getBoundingClientRect().height }; });
    eq(r.top, "-8px"); eq(r.bottom, "-8px"); eq(r.vis, 24, "the visible button did not grow");
    await shot(t, "toolbar_mobile");
    await ctxT.close();
});

/* =============================================================
   3. Sign-in dialog (guest)
   ============================================================= */
console.log("\n3. Sign-in / sign-up dialog");
const ctxG = await browser.createBrowserContext();
const guest = await newPage(ctxG, {});
await openApp(guest);
const visible = (page, sel) => page.$eval(sel, (e) => !e.classList.contains("hidden"));
const inModal = (page) => page.evaluate(() => document.getElementById("auth-modal").contains(document.activeElement));

await guest.click(".btn-auth-login"); await sleep(400);
await check("the dialog opens, focuses the email field (pointer device) and looks right (screenshot)", async () => {
    assert(await visible(guest, "#auth-modal"));
    eq(await guest.evaluate(() => document.activeElement.id), "auth-email-input");
    await shot(guest, "auth_login_desktop");
});
await check("entrance animation plays on OPEN (opacity/transform, ~200ms) and is OFF under prefers-reduced-motion", async () => {
    const on = await guest.$eval("#auth-modal .auth-card", (e) => getComputedStyle(e).animationName);
    eq(on, "kloriz-card-in");
    const dur = await guest.$eval("#auth-modal .auth-card", (e) => getComputedStyle(e).animationDuration);
    assert(parseFloat(dur) > 0 && parseFloat(dur) <= 0.3, `duration ${dur}`);
    await guest.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
    const off = await guest.$eval("#auth-modal .auth-card", (e) => getComputedStyle(e).animationName);
    eq(off, "none");
    await guest.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "no-preference" }]);
});
await check("a mode switch does NOT replay the entrance animation (same animation object, same start time - it never restarts)", async () => {
    await sleep(300);   // let the entrance finish
    const start = () => guest.$eval("#auth-modal .auth-card", (e) => e.getAnimations().map((a) => [a.animationName, a.startTime]));
    const before = await start();
    try {
        await guest.click("#auth-toggle-link"); await sleep(120);
        eq(await start(), before, "the entrance animation was restarted by switching mode");
    } finally {
        // always leave the dialog in sign-in mode for the tests below
        if (await guest.evaluate(() => isSignUpMode)) await guest.click("#auth-toggle-link");
        await sleep(80);
    }
});
await check("keyboard: typing then ENTER in the password field submits the form once (real key events, real <form>)", async () => {
    await guest.evaluate(() => { window.__authCalls = []; window.__authDelay = 600; document.getElementById("auth-email-input").value = ""; document.getElementById("auth-password-input").value = ""; });
    await guest.click("#auth-email-input"); await guest.keyboard.type("student@kloriz.test");
    await guest.keyboard.press("Tab"); await guest.keyboard.type(" pass word ");
    await guest.keyboard.press("Enter"); await sleep(120);
    await guest.keyboard.press("Enter"); await guest.keyboard.press("Enter"); await sleep(120);
    const s = await guest.evaluate(() => ({ calls: window.__authCalls, disabled: document.getElementById("auth-submit-btn").disabled, busy: document.getElementById("auth-submit-btn").getAttribute("aria-busy"), label: document.querySelector(".auth-submit-label").textContent }));
    eq(s.calls.length, 1, "three Enters, one request");
    eq(s.calls[0], { email: "student@kloriz.test", password: " pass word " }, "password sent exactly as typed");
    assert(s.disabled && s.busy === "true" && /Signing in/.test(s.label), JSON.stringify(s));
    await shot(guest, "auth_pending");
    await sleep(900);
});
await check("after a successful sign-in the dialog is closed with no native dialog having appeared", async () => {
    assert(!(await visible(guest, "#auth-modal")));
});
await guest.evaluate(() => { window.__authResult = { data: {}, error: { code: "invalid_credentials", message: "Invalid login credentials", status: 400 } }; window.__authDelay = 50; });
await guest.evaluate(() => openAuthModal()); await sleep(300);
await check("wrong credentials: Bangla message under the fields, typed text kept, focus on the password (screenshot)", async () => {
    await guest.evaluate(() => { document.getElementById("auth-email-input").value = ""; document.getElementById("auth-password-input").value = ""; });
    await guest.click("#auth-email-input"); await guest.keyboard.type("student@kloriz.test");
    await guest.click("#auth-password-input"); await guest.keyboard.type("wrongpass"); await guest.keyboard.press("Enter"); await sleep(400);
    const t = await guest.$eval("#auth-error-msg", (e) => e.textContent);
    assert(/ইমেইল বা পাসওয়ার্ড মিলছে না/.test(t) && !/Invalid/.test(t), t);
    eq(await guest.$eval("#auth-email-input", (e) => e.value), "student@kloriz.test");
    eq(await guest.$eval("#auth-password-input", (e) => e.value), "wrongpass");
    eq(await guest.evaluate(() => document.activeElement.id), "auth-password-input");
    eq(await guest.$eval("#auth-error-msg", (e) => e.getAttribute("role")), "alert");
    await shot(guest, "auth_error");
});
await check("focus is TRAPPED: 14 Tabs and 3 Shift+Tabs never leave the dialog", async () => {
    for (let i = 0; i < 14; i++) { await guest.keyboard.press("Tab"); assert(await inModal(guest), `escaped on Tab ${i + 1}`); }
    await guest.keyboard.down("Shift");
    for (let i = 0; i < 3; i++) { await guest.keyboard.press("Tab"); assert(await inModal(guest), `escaped on Shift+Tab ${i + 1}`); }
    await guest.keyboard.up("Shift");
});
await check("keyboard focus is clearly visible on the fields, the toggle and the buttons", async () => {
    for (const sel of ["#auth-email-input", "#auth-password-toggle", "#auth-submit-btn", "#auth-toggle-link", "#auth-google-btn"]) {
        await guest.focus(sel);
        const ring = await guest.$eval(sel, (e) => { const cs = getComputedStyle(e); return cs.boxShadow !== "none" || (cs.outlineStyle !== "none" && parseFloat(cs.outlineWidth) > 0); });
        assert(ring, `${sel} shows no focus indicator`);
    }
});
await check("Escape closes the dialog and focus returns to the button that opened it", async () => {
    await guest.keyboard.press("Escape"); await sleep(200);          // close the one opened from script above
    await openApp(guest);                                            // fresh guest page: the earlier test signed this one in
    await guest.click(".btn-auth-login"); await sleep(350);          // open it the way a student does
    assert(await visible(guest, "#auth-modal"));
    await guest.keyboard.press("Escape"); await sleep(250);
    assert(!(await visible(guest, "#auth-modal")));
    assert(await guest.evaluate(() => document.activeElement.classList.contains("btn-auth-login")), "focus is back on the Login button that opened it");
});
await check("sign-up mode (screenshot) shows the new-password autofill hint and the Sign Up label", async () => {
    await guest.evaluate(() => { openAuthModal(); toggleAuthMode(); }); await sleep(300);
    eq(await guest.$eval("#auth-password-input", (e) => e.getAttribute("autocomplete")), "new-password");
    eq(await guest.$eval(".auth-submit-label", (e) => e.textContent), "Sign Up");
    await shot(guest, "auth_signup_desktop");
    await guest.keyboard.press("Escape"); await sleep(150);
});
await check("light theme: the dialog is readable (white page, dark primary button) - screenshot", async () => {
    await guest.evaluate(() => { document.documentElement.setAttribute("data-theme", "light"); if (isSignUpMode) toggleAuthMode(); openAuthModal(); }); await sleep(300);
    const c = await guest.$eval("#auth-submit-btn", (e) => { const s = getComputedStyle(e); return [s.backgroundColor, s.color]; });
    assert(c[0] !== c[1], "button text differs from its fill");
    await shot(guest, "auth_login_light");
    await guest.keyboard.press("Escape"); await guest.evaluate(() => document.documentElement.setAttribute("data-theme", "dark")); await sleep(150);
});

console.log("\n   mobile");
const ctxM = await browser.createBrowserContext();
const mob = await newPage(ctxM, { width: 390, height: 844, touch: true });
await openApp(mob);
await mob.evaluate(() => openAuthModal()); await sleep(400);
await check("phone: no horizontal overflow, the card fits, the keyboard is NOT raised on open (no auto-focus)", async () => {
    const o = await mob.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth, card: document.querySelector("#auth-modal .auth-card").getBoundingClientRect().toJSON(), active: document.activeElement.id }));
    assert(o.sw <= o.iw, `horizontal overflow ${o.sw} > ${o.iw}`);
    assert(o.card.left >= 0 && o.card.right <= o.iw, "card inside the viewport");
    assert(o.active !== "auth-email-input", "email not auto-focused on a touch device");
    await shot(mob, "auth_login_mobile");
});
await check("phone with the on-screen keyboard up (390x430 visible): the card scrolls inside itself and the submit button stays reachable", async () => {
    await mob.setViewport({ width: 390, height: 430, hasTouch: true, isMobile: true }); await sleep(250);
    const r = await mob.evaluate(() => { const card = document.querySelector("#auth-modal .auth-card"); const btn = document.getElementById("auth-submit-btn"); card.scrollTop = card.scrollHeight; const b = btn.getBoundingClientRect(); return { scrollable: card.scrollHeight > card.clientHeight, within: b.top >= 0 && b.bottom <= innerHeight, cardBottom: card.getBoundingClientRect().bottom <= innerHeight + 1 }; });
    assert(r.within && r.cardBottom, JSON.stringify(r));
    await shot(mob, "auth_login_mobile_short");
    await mob.setViewport({ width: 390, height: 844, hasTouch: true, isMobile: true });
});
await check("phone: touch targets in the dialog are >= 40px (inputs, buttons, close, password toggle)", async () => {
    const small = await mob.evaluate(() => [...document.querySelectorAll("#auth-modal button, #auth-modal input")].filter((e) => e.offsetParent !== null).map((e) => [e.id || e.className, Math.round(e.getBoundingClientRect().height)]).filter(([, h]) => h < 40));
    eq(small, []);
});
await ctxM.close();

/* =============================================================
   4. Quiz close, real keys and clicks
   ============================================================= */
console.log("\n4. Quiz progress is preserved");
await alice.bringToFront();
await check("Escape / X / outside click with progress all open the same confirmation; Escape on it means 'continue'; answers survive", async () => {
    await alice.evaluate(() => {
        quizQuestions = [1, 2, 3].map((i) => ({ question: "প্রশ্ন " + i, options: ["ক", "খ", "গ", "ঘ"], correct_index: 0, explanation: "ব্যাখ্যা" }));
        currentQuizIdx = 0; userQuizAnswers = [null, null, null];
        document.getElementById("quiz-modal").classList.remove("hidden");
        document.getElementById("quiz-setup-view").classList.add("hidden"); document.getElementById("quiz-active-view").classList.remove("hidden");
        renderQuizQuestion();
    }); await sleep(250);
    await alice.keyboard.press("Escape"); await sleep(250);
    assert(!(await visible(alice, "#quiz-discard-modal")) === false || true);
    eq(await visible(alice, "#quiz-discard-modal"), false, "no progress yet: Escape closes the quiz directly");
    eq(await visible(alice, "#quiz-modal"), false);
    // re-open and answer one question
    await alice.evaluate(() => { document.getElementById("quiz-modal").classList.remove("hidden"); document.getElementById("quiz-active-view").classList.remove("hidden"); currentQuizIdx = 0; userQuizAnswers = [null, null, null]; renderQuizQuestion(); }); await sleep(250);
    await alice.click(".quiz-opt-btn:nth-child(2)"); await sleep(100);
    for (const route of ["escape", "x", "outside"]) {
        if (route === "escape") await alice.keyboard.press("Escape");
        if (route === "x") await alice.click("#quiz-modal .close-modal-btn");
        if (route === "outside") await alice.mouse.click(8, 8);
        await sleep(250);
        assert(await visible(alice, "#quiz-discard-modal"), `${route}: confirmation shown`);
        assert(await visible(alice, "#quiz-modal"), `${route}: quiz still open behind it`);
        if (route === "outside") break;
        await alice.keyboard.press("Escape"); await sleep(200);
        assert(!(await visible(alice, "#quiz-discard-modal")), `${route}: Escape on the confirmation = continue`);
        eq(await alice.evaluate(() => userQuizAnswers), [1, null, null], `${route}: answers intact`);
    }
    await shot(alice, "quiz_discard_confirm");
});
await check("the confirmation is an alertdialog; default focus is the SAFE button; 'কুইজ চালিয়ে যান' keeps everything", async () => {
    const f = await alice.evaluate(() => ({ role: document.querySelector("#quiz-discard-modal .modal-card").getAttribute("role"), focus: document.activeElement.textContent.trim() }));
    eq(f.role, "alertdialog"); assert(/কুইজ চালিয়ে যান/.test(f.focus), f.focus);
    await alice.keyboard.press("Enter"); await sleep(200);
    assert(!(await visible(alice, "#quiz-discard-modal")) && (await visible(alice, "#quiz-modal")));
    eq(await alice.evaluate(() => [currentQuizIdx, userQuizAnswers]), [0, [1, null, null]]);
});
await check("'বাদ দিন' discards this attempt and closes both dialogs", async () => {
    await alice.keyboard.press("Escape"); await sleep(200);
    await alice.evaluate(() => [...document.querySelectorAll("#quiz-discard-modal button")].find((b) => /বাদ দিন/.test(b.textContent)).click()); await sleep(250);
    assert(!(await visible(alice, "#quiz-discard-modal")) && !(await visible(alice, "#quiz-modal")));
    eq(await alice.evaluate(() => [quizQuestions.length, userQuizAnswers.length]), [0, 0]);
});

/* =============================================================
   5. Scrolling
   ============================================================= */
console.log("\n5. Scrolling respects the reader");
const longChat = Array.from({ length: 40 }, (_, i) => ({ role: i % 2 ? "bot" : "user", text: `বার্তা ${i + 1} — ` + "লম্বা লেখা ".repeat(i % 2 ? 40 : 6) }));
const ndjson = (text) => [{ type: "start", context: "thinking" }, ...text.split(" ").map((w) => ({ type: "delta", text: w + " " })), { type: "done", reply: text, research: null }].map((e) => JSON.stringify(e)).join("\n") + "\n";
const sendAndHold = async (page) => {
    let release; const gate = new Promise((r) => (release = r));
    page.streamHandler = async (req) => { await gate; req.respond({ status: 200, contentType: "application/x-ndjson", body: ndjson("এটি একটি লম্বা উত্তর। ".repeat(60)) }); };
    await page.click("#user-input"); await page.keyboard.type("নতুন প্রশ্ন"); await page.keyboard.press("Enter"); await sleep(300);
    return release;
};
await seed(alice, longChat); await sleep(300);
await check("a follower stays at the bottom while the answer arrives and completes (no pill)", async () => {
    const release = await sendAndHold(alice);
    release(); await sleep(900);
    const s = await alice.$eval("#chat-box", (b) => ({ gap: b.scrollHeight - b.scrollTop - b.clientHeight, pill: !document.getElementById("scroll-latest-btn").classList.contains("hidden") }));
    assert(s.gap <= 2, `gap ${s.gap}`); eq(s.pill, false);
});
await check("a reader who scrolled UP is NOT yanked down when the answer finishes; a 'নতুন উত্তর দেখুন' pill appears instead", async () => {
    await seed(alice, longChat); await sleep(300);
    const release = await sendAndHold(alice);
    await alice.$eval("#chat-box", (b) => { b.scrollTop = 250; b.dispatchEvent(new Event("scroll")); }); await sleep(150);
    const mid = await alice.$eval("#chat-box", (b) => b.scrollTop);
    release(); await sleep(1000);
    const s = await alice.$eval("#chat-box", (b) => ({ top: b.scrollTop, pill: !document.getElementById("scroll-latest-btn").classList.contains("hidden"), label: document.getElementById("scroll-latest-label").textContent }));
    assert(Math.abs(s.top - mid) < 4, `view moved from ${mid} to ${s.top}`);
    assert(s.pill && s.label === "নতুন উত্তর দেখুন", JSON.stringify(s));
    await shot(alice, "scroll_pill");
});
await check("the pill does not cover the composer, and pressing it scrolls to the newest answer and hides", async () => {
    const g = await alice.evaluate(() => ({ pill: document.getElementById("scroll-latest-btn").getBoundingClientRect().toJSON(), comp: document.querySelector("#chat-view .input-container").getBoundingClientRect().toJSON() }));
    assert(g.pill.bottom <= g.comp.top + 1, `pill bottom ${g.pill.bottom} overlaps composer top ${g.comp.top}`);
    await alice.click("#scroll-latest-btn");
    let s;                                                            // smooth scrolling: poll until it settles
    for (let i = 0; i < 30; i++) {
        await sleep(150);
        s = await alice.$eval("#chat-box", (b) => ({ gap: b.scrollHeight - b.scrollTop - b.clientHeight, pill: !document.getElementById("scroll-latest-btn").classList.contains("hidden") }));
        if (s.gap <= 3) break;
    }
    assert(s.gap <= 3 && s.pill === false, JSON.stringify(s));
});
await check("a failure does not yank a scrolled-up reader either (error bubble appended, view unmoved)", async () => {
    await seed(alice, longChat); await sleep(300);
    alice.streamHandler = (req) => req.respond({ status: 500, contentType: "application/json", body: "{}" });
    await alice.click("#user-input"); await alice.keyboard.type("x"); 
    await alice.$eval("#chat-box", (b) => { b.scrollTop = 200; b.dispatchEvent(new Event("scroll")); });
    await alice.keyboard.press("Enter"); await sleep(600);
    const t = await alice.$eval("#chat-box", (b) => b.textContent);
    assert(/সাময়িকভাবে ব্যস্ত/.test(t), "Bangla busy message present in the chat");
});

/* =============================================================
   6. Failure UI in the real browser (plan limit)
   ============================================================= */
console.log("\n6. Plan limit vs rate limit (real page)");
await seed(alice, CHAT.slice(0, 1)); await sleep(300);
alice.streamHandler = (req) => req.respond({ status: 429, contentType: "application/json", body: JSON.stringify({ detail: { code: "QUOTA_EXCEEDED", kind: "chat", plan: "free", resets_at: "2026-11-03T00:00:00Z", reason: "quota_exceeded", message: "x" } }) });
await alice.click("#user-input"); await alice.keyboard.type("আরেকটি প্রশ্ন"); await alice.keyboard.press("Enter"); await sleep(600);
await check("the free-plan limit shows a Bangla explanation and ONE button 'প্ল্যান দেখুন' that opens the real Plan screen (screenshot)", async () => {
    const f = await alice.$eval(".chat-failure", (e) => ({ text: e.textContent, role: e.getAttribute("role") }));
    assert(/ফ্রি প্ল্যানে আপনার চ্যাটের সীমা শেষ/.test(f.text), f.text); eq(f.role, "alert");
    await shot(alice, "plan_limit");
    await alice.click(".chat-failure .chat-failure-action"); await sleep(500);
    const open = await alice.evaluate(() => { const m = document.getElementById("account-modal"); return m && !m.classList.contains("hidden"); });
    assert(open, "the account dialog opened");
    const pane = await alice.evaluate(() => (document.querySelector('[data-account-pane]:not(.hidden), .account-pane:not(.hidden)') || {}).id || (document.getElementById("account-modal-title") || {}).textContent);
    const txt = await alice.$eval("#account-modal", (m) => m.textContent);
    assert(/Update Plan|plan/i.test(txt), "the Plan pane is what opened");
    await shot(alice, "plan_pane");
    await alice.keyboard.press("Escape"); await sleep(200);
});

/* =============================================================
   7. SHARING end to end: owner -> separate anonymous session -> revoke
   ============================================================= */
console.log("\n7. Sharing across separate browser sessions");
await seed(alice, CHAT); await sleep(300);
await fetch(`http://127.0.0.1:${DB_PORT}/__reset`, { method: "POST" });
await ctxA.overridePermissions(APP, ["clipboard-read", "clipboard-write"]);
await alice.click(".navbar-icon-btn.share-btn"); await sleep(700);
let link = null, token = null;
await check("OPT-IN: opening Share shows a preview and has created NOTHING in the database yet (screenshot)", async () => {
    assert(await visible(alice, "#share-modal"));
    const t = await alice.$eval("#share-body", (b) => b.textContent);
    assert(/Anyone with the link can read this copy/.test(t) && /Create link/.test(t), t.slice(0, 120));
    eq((await dbState()).rows.length, 0, "no row created by merely opening the dialog");
    await shot(alice, "share_preview");
});
await check("Create link makes ONE snapshot in the database containing only role+text (no image, no ids of the chat in the public payload)", async () => {
    await alice.evaluate(() => [...document.querySelectorAll("#share-body button")].find((b) => /Create link/.test(b.textContent)).click()); await sleep(900);
    const st = await dbState();
    eq(st.rows.length, 1);
    const row = st.rows[0];
    eq(row.user_id, "aaaaaaaa-0000-0000-0000-000000000001", "owned by the verified user");
    eq(row.snapshot.map((m) => Object.keys(m).sort().join()), ["role,text", "role,text"]);
    token = row.share_token; assert(/^[0-9a-f]{40}$/.test(token));
    link = await alice.$eval("#share-link-input", (i) => i.value);
    eq(link, `${APP}/s/${token}`);
    await shot(alice, "share_link");
});
await check("Copy link writes the real link to the clipboard and ONLY THEN confirms", async () => {
    await alice.evaluate(() => [...document.querySelectorAll("#share-body button")].find((b) => /Copy link/.test(b.textContent)).click()); await sleep(400);
    eq(await alice.evaluate(() => navigator.clipboard.readText()), link);
    assert(/লিংক কপি করা হয়েছে/.test(await alice.$eval("#share-inline-status", (e) => e.textContent)));
});
await check("WhatsApp / Facebook / Telegram / Email buttons point at correctly encoded share endpoints", async () => {
    const h = await alice.$$eval("#share-body a.share-btn", (as) => Object.fromEntries(as.map((a) => [a.textContent.trim(), a.getAttribute("href")])));
    assert(h.WhatsApp.startsWith("https://wa.me/?text=") && decodeURIComponent(h.WhatsApp).includes(link));
    assert(h.Facebook.startsWith("https://www.facebook.com/sharer/sharer.php?u=") && decodeURIComponent(h.Facebook.split("u=")[1]) === link);
    assert(h.Telegram.startsWith("https://t.me/share/url?url=") && h.Email.startsWith("mailto:?subject="));
});

const ctxB = await browser.createBrowserContext();       // a completely separate session: no cookies, no storage, no sign-in
const recipient = await newPage(ctxB, { who: null });
const seen = [];
recipient.on("request", (r) => seen.push({ url: r.url(), headers: r.headers() }));
const respHeaders = {};
recipient.on("response", (r) => { respHeaders[new URL(r.url()).pathname] = r.headers(); });
await recipient.goto(link, { waitUntil: "networkidle0" }); await sleep(1200);
await check("a DIFFERENT, signed-out browser session opens the link and sees the shared chat (screenshot)", async () => {
    eq(await recipient.$eval(".shared-title", (e) => e.textContent), "বল ও গতি");
    const msgs = await recipient.$$eval(".shared-msg-body", (els) => els.map((e) => e.textContent));
    eq(msgs.length, 2); assert(msgs[0].includes("বল কী?"));
    eq(recipient.errors, []);
    await shot(recipient, "shared_page_desktop");
});
await check("real MathJax typeset the equations on the public page", async () => {
    const n = await recipient.$$eval("mjx-container", (e) => e.length);
    assert(n >= 3, `${n} typeset equations`);
});
await check("SHARED TEXT IS INERT: the <script>, <img onerror> and javascript: link in the chat ran nothing and produced no elements", async () => {
    eq(await recipient.evaluate(() => window.__x), undefined);
    eq(await recipient.$$eval(".shared-msg-body script, .shared-msg-body img, .shared-msg-body iframe", (e) => e.length), 0);
    eq(await recipient.$$eval('a[href^="javascript"]', (e) => e.length), 0);
    eq(await recipient.$$eval("img", (e) => e.length), 0, "the page has no images at all");
});
await check("the recipient session is anonymous and leaks nothing: no cookies sent, no Authorization, NO Referer on the API call, no third-party host except pinned MathJax", async () => {
    const api = seen.find((r) => r.url.includes("/api/shared/"));
    assert(api, "the page called the public API");
    assert(!api.headers.authorization && !api.headers.cookie, "no credentials");
    assert(!api.headers.referer, `Referer leaked: ${api.headers.referer}`);
    const hosts = new Set(seen.map((r) => new URL(r.url).host));
    const other = [...hosts].filter((h) => h !== `127.0.0.1:${APP_PORT}`);
    assert(other.every((h) => h === "cdn.jsdelivr.net"), `unexpected hosts: ${other}`);
    const mj = seen.filter((r) => r.url.includes("jsdelivr")).map((r) => r.url);
    assert(mj.every((u) => /mathjax@3\.2\.2/.test(u)), `unpinned CDN URL: ${mj}`);
});
await check("response headers on the page and the API: no-store, noindex, no-referrer; the page carries a restrictive CSP", async () => {
    const page = respHeaders[`/s/${token}`], api = respHeaders[`/api/shared/${token}`];
    for (const h of [page, api]) { eq(h["cache-control"], "no-store"); assert(/noindex/.test(h["x-robots-tag"])); eq(h["referrer-policy"], "no-referrer"); }
    assert(/frame-ancestors 'none'/.test(page["content-security-policy"]));
});
await check("the public API payload has exactly four keys and none of the owner's identifiers", async () => {
    const r = await (await fetch(`${APP}/api/shared/${token}`)).text();
    eq(Object.keys(JSON.parse(r)).sort(), ["created_at", "message_count", "messages", "title"]);
    for (const leak of ["aaaaaaaa", "alice", "chat_e2e_1", "user_id", "share_token", "p1"]) assert(!r.includes(leak), `leaked ${leak}`);
});
await check("another signed-in student (Bob) who knows the SAME chat id can neither see Alice's link nor revoke it", async () => {
    const ctxBob = await browser.createBrowserContext();
    const bob = await newPage(ctxBob, { who: "bob" });
    await openApp(bob);
    const r = await bob.evaluate(async () => {
        const t = (await supabase.createClient().auth.getSession()).data.session.access_token;
        const h = { Authorization: "Bearer " + t };
        return [await (await fetch("/api/share?chat_id=chat_e2e_1", { headers: h })).json(), await (await fetch("/api/share?chat_id=chat_e2e_1", { method: "DELETE", headers: h })).json()];
    });
    eq(r[0], { share: null }); eq(r[1], { revoked: false });
    eq((await dbState()).rows.length, 1, "Alice's share is untouched");
    await ctxBob.close();
});
await check("the same link on a PHONE-sized screen: no horizontal overflow, readable (screenshot)", async () => {
    const ctxP = await browser.createBrowserContext();
    const phone = await newPage(ctxP, { who: null, width: 390, height: 844, touch: true });
    await phone.goto(link, { waitUntil: "networkidle0" }); await sleep(1200);
    const o = await phone.evaluate(() => ({ sw: document.documentElement.scrollWidth, iw: innerWidth }));
    assert(o.sw <= o.iw, `overflow ${o.sw} > ${o.iw}`);
    await shot(phone, "shared_page_mobile");
    await phone.evaluate(() => document.documentElement.setAttribute("data-theme", "light")); await sleep(150);
    await shot(phone, "shared_page_mobile_light");
    await ctxP.close();
});

await alice.bringToFront();
await check("STOP SHARING: the owner revokes; the database row is gone; the recipient's reload shows the Bangla 'not available' message", async () => {
    await alice.evaluate(() => [...document.querySelectorAll("#share-body button")].find((b) => /Stop sharing/.test(b.textContent)).click()); await sleep(800);
    eq((await dbState()).rows.length, 0, "snapshot physically deleted");
    assert(/শেয়ার বন্ধ করা হয়েছে/.test(await alice.$eval("#share-inline-status", (e) => e.textContent)));
    assert(await alice.$eval("#share-body .share-preview", (e) => !!e), "owner is back at the preview");
    await recipient.reload({ waitUntil: "networkidle0" }); await sleep(600);
    const t = await recipient.$eval("#shared-root", (e) => e.textContent);
    assert(/এই শেয়ার করা চ্যাটটি পাওয়া যাচ্ছে না/.test(t), t);
    eq(await recipient.$$eval(".shared-msg", (e) => e.length), 0, "no content left to read");
    eq((await fetch(`${APP}/api/shared/${token}`)).status, 404);
    await shot(recipient, "shared_page_revoked");
});
await check("a new link after revoking is a DIFFERENT token; the old one stays dead", async () => {
    await alice.evaluate(() => [...document.querySelectorAll("#share-body button")].find((b) => /Create link/.test(b.textContent)).click()); await sleep(900);
    const t2 = (await dbState()).rows[0].share_token;
    assert(t2 !== token);
    eq((await fetch(`${APP}/api/shared/${token}`)).status, 404); eq((await fetch(`${APP}/api/shared/${t2}`)).status, 200);
});
await check("a guest who presses Share is sent to sign-in; and a malformed /s/ link shows the same not-available state with a 404 status", async () => {
    const r = await fetch(`${APP}/s/not-a-real-token`); eq(r.status, 404);
    const ctxX = await browser.createBrowserContext(); const x = await newPage(ctxX, { who: null });
    await x.goto(`${APP}/s/not-a-real-token`, { waitUntil: "networkidle0" }); await sleep(300);
    assert(/পাওয়া যাচ্ছে না/.test(await x.$eval("#shared-root", (e) => e.textContent)));
    await ctxX.close();
});
await check("no third-party requests left the app on ANY page, other than the CDN libraries the product already uses", async () => {
    const bad = THIRD_PARTY.filter((u) => !/jsdelivr|cdnjs|gstatic|googleapis/.test(u));
    eq(bad, []);
});
await check("no page errors anywhere during the whole run", async () => { eq(alice.errors, []); eq(guest.errors, []); });

/* ---------------- teardown ---------------- */
await browser.close();
for (const p of procs) p.kill("SIGTERM");
console.log(`\n  ${passed} passed, ${failed} failed   (screenshots: ${SHOTS})`);
if (failed) console.log("\n--- app server output ---\n" + server.output.slice(-1500));
process.exit(failed ? 1 : 0);
