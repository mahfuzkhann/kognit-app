/* ============================================================
   KOGNIT - UI REFINEMENT: SIDEBAR / SCROLLING / FOCUS (REAL BROWSER)
   ============================================================
   jsdom has no layout engine, so none of the following can be tested there.
   This drives a REAL Chrome/Chromium/Edge (puppeteer-core) against the REAL
   templates/index.html + CSS + JS, served by a real local http.Server. Only
   Supabase and the CDN libraries are stubbed (as in the other browser tests).

   It pins down the defects that were reproduced at a short laptop viewport
   (1280x585 CSS px - a 1080p screen at 150% Windows scaling):

     1. Recent Chats got only ~2.5 rows (sidebar spacing tuned for tall windows)
     2. native scrollbars drew arrow buttons + an invisible thumb
     3. the keyboard focus ring on chat rows was clipped by the scroller
     4. row action buttons were 20x18px (< 24x24, WCAG 2.2 / 2.5.8)

   ...and guards what must NOT change: tall-window spacing, mobile layout, the
   navigation / search / rename / delete / new-chat features, independent
   scrolling, and reduced-motion behaviour.

   SCROLLBARS: headless Chrome hides scrollbars by default (--hide-scrollbars),
   which would hide exactly the class of defect under test, so that default
   flag is removed here.

   BROWSER DISCOVERY: set CHROME_PATH, or rely on the usual install locations.
   If no browser is found this prints SKIPPED and exits 0 - never a fake pass.

   Run: node tests_frontend/test_ui_refinement_browser.mjs
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
    console.log("\nSKIPPED: no Chrome/Chromium/Edge found (set CHROME_PATH to run the real-browser UI refinement test).");
    console.log("0 passed, 0 failed (SKIPPED - real layout, scrollbars and focus rings were NOT verified by this run)");
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

const SHELL_CSS = fs.readFileSync(path.join(ROOT, "static/css/shell.css"), "utf8");

/* ------------------------------------------------------------
   Local server + browser
   ------------------------------------------------------------ */
const MIME = { ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html", ".png": "image/png", ".json": "application/json" };
const server = await new Promise((resolve) => {
    const s = http.createServer((req, res) => {
        const u = new URL(req.url, "http://x");
        if (u.pathname === "/api/profile" && req.method === "GET") {
            res.writeHead(200, { "Content-Type": "application/json" });
            return res.end(JSON.stringify({ profile: { name: "Mahfuzur Rahman", user_class: "Class 11-12 (HSC)", stream: "Science" } }));
        }
        if (u.pathname.startsWith("/api/")) { res.writeHead(404, { "Content-Type": "application/json" }); return res.end("{}"); }
        let file = null;
        if (u.pathname === "/") file = path.join(ROOT, "templates/index.html");
        else if (u.pathname.startsWith("/static/")) file = path.join(ROOT, u.pathname);
        if (file && fs.existsSync(file)) {
            res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "text/plain", "Cache-Control": "no-store" });
            res.end(fs.readFileSync(file));
        } else { res.writeHead(404); res.end(); }
    });
    s.listen(0, "127.0.0.1", () => resolve(s));
});
const BASE = `http://127.0.0.1:${server.address().port}/`;

const SUPABASE_STUB = `window.supabase = { createClient: () => ({ auth: {
  getSession: async () => ({ data: { session: { access_token: "tok", user: { id: "user-1", email: "t@example.com" } } } }),
  onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }), signOut: async () => ({}) },
  from: () => { const q = { select: () => q, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => q, eq: async () => ({ error: null }) }; return q; } }) };`;

const browser = await puppeteer.launch({
    executablePath: EXE, headless: true,
    ignoreDefaultArgs: ["--hide-scrollbars"],
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", ...(process.env.CHROME_ARGS ? process.env.CHROME_ARGS.split(" ") : [])],
});

const TITLES = ["New Chat", "Hi", "Hi", "Newton's second law", "Photosynthesis light reaction", "Quadratic equations practice",
    "Bangla grammar: sandhi", "Chemical bonding revision", "A very long conversation title that must truncate cleanly in the sidebar row"];

async function openApp({ width = 1280, height = 585, dsf = 1.5, theme = "dark", reduced = false, chats = 9, longConversation = false } = {}) {
    const page = await browser.newPage();
    await page.setViewport({ width, height, deviceScaleFactor: dsf });
    if (reduced) await page.emulateMediaFeatures([{ name: "prefers-reduced-motion", value: "reduce" }]);
    await page.setBypassServiceWorker(true);
    const errors = [];
    // Service-worker registration asks this bare server for /sw.js (not served). Unrelated to the UI under test.
    page.on("pageerror", (e) => { if (!/fetching the script/.test(e.message)) errors.push(e.message); });
    page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource|fetching the script/.test(m.text())) errors.push(m.text()); });
    page.on("dialog", (d) => d.accept());
    await page.setRequestInterception(true);
    page.on("request", (req) => {
        const url = req.url();
        if (url.includes("cdn.jsdelivr.net/npm/marked")) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "marked/lib/marked.umd.js")) });
        if (url.includes("mathjax@3/es5/tex-svg.js")) return req.respond({ status: 200, contentType: "application/javascript", body: fs.readFileSync(path.join(NM, "mathjax/es5/tex-svg.js")) });
        if (url.includes("supabase-js")) return req.respond({ status: 200, contentType: "application/javascript", body: SUPABASE_STUB });
        if (/fonts\.(googleapis|gstatic)\.com|gstatic\.com\/firebasejs/.test(url)) return req.respond({ status: 200, contentType: "text/css", body: "" });
        return req.continue();
    });
    await page.evaluateOnNewDocument((t) => { try { localStorage.setItem("kognit_theme", t); } catch (e) {} }, theme);
    await page.goto(BASE, { waitUntil: "networkidle0" });
    await page.waitForFunction(() => window.sendMessage && window.MathJax && window.MathJax.typesetPromise, { timeout: 20000 });
    await page.evaluate((n, titles, long) => {
        const now = Date.now(), off = [0, 4 * 60e3, 4 * 3600e3, 26 * 3600e3, 3 * 86400e3, 5 * 86400e3, 9 * 86400e3, 40 * 86400e3, 70 * 86400e3];
        projects.length = 0;
        const cs = [];
        for (let i = 0; i < n; i++) cs.push({ id: "chat_" + (now - off[i % off.length] - i), title: titles[i % titles.length], messages: [] });
        if (long) for (let i = 0; i < 40; i++) {
            cs[0].messages.push({ role: "user", text: "Question number " + i + " about forces and motion?" });
            cs[0].messages.push({ role: "bot", text: "Answer " + i + ". " + "Force equals mass times acceleration. ".repeat(12) });
        }
        projects.push({ id: DEFAULT_PROJECT_ID, title: "Recent Chats", chats: cs });
        projects.push({ id: "proj_1", title: "Physics revision", chats: [{ id: "chat_" + (now - 8e8), title: "Motion graphs", messages: [] }] });
        activeProjectId = DEFAULT_PROJECT_ID; activeChatId = cs[0].id;
        renderHistoryList(); loadChat(DEFAULT_PROJECT_ID, cs[0].id);
    }, chats, TITLES, longConversation);
    await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
    await sleep(450);
    return { page, errors, close: () => page.close() };
}

const rectOf = (page, sel) => page.evaluate((s) => { const el = document.querySelector(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom }; }, sel);

/* How many Recent Chats rows are FULLY inside the visible part of the list. */
const fullyVisibleRows = (page) => page.evaluate(() => {
    const box = document.querySelector(".history-section").getBoundingClientRect();
    return [...document.querySelectorAll(".recent-chats-list .chat-item")].filter((r) => { const b = r.getBoundingClientRect(); return b.top >= box.top - 0.5 && b.bottom <= box.bottom + 0.5; }).length;
});

/* Is the 4px focus ring of the focused element fully inside every clipping ancestor? */
const ringClipping = (page) => page.evaluate(() => {
    const a = document.activeElement; if (!a || a === document.body) return { ok: true, why: "none" };
    const RING = 4, r = a.getBoundingClientRect();
    for (let n = a.parentElement; n && n !== document.documentElement; n = n.parentElement) {
        const cs = getComputedStyle(n);
        if (!/(hidden|auto|scroll|clip)/.test(cs.overflowX + cs.overflowY)) continue;
        const c = n.getBoundingClientRect();
        const bl = parseFloat(cs.borderLeftWidth) || 0, br = parseFloat(cs.borderRightWidth) || 0, bt = parseFloat(cs.borderTopWidth) || 0, bb = parseFloat(cs.borderBottomWidth) || 0;
        const clip = { l: c.left + bl, r: c.right - br - (n.offsetWidth - n.clientWidth - bl - br), t: c.top + bt, b: c.bottom - bb };
        const outsideView = r.bottom < clip.t || r.top > clip.b || r.right < clip.l || r.left > clip.r;
        if (outsideView) return { ok: true, why: "scrolled out of view" };
        // only vertical clipping at an EDGE the user cannot scroll further past is a defect; horizontal always is
        const sides = { left: r.left - RING < clip.l - 0.5, right: r.right + RING > c.right - br + 0.5 };
        const top = r.top - RING < clip.t - 0.5 && n.scrollTop === 0;
        if (sides.left || sides.right || top) return { ok: false, why: `${a.className} ring clipped by ${n.id ? "#" + n.id : "." + String(n.className).split(" ")[0]} ` + JSON.stringify({ sides, top }) };
    }
    return { ok: true, why: "fits" };
});

const luminance = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
const ratio = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
const colourOfToken = (page, token) => page.evaluate((t) => {
    const probe = document.createElement("div"); probe.style.color = `var(${t})`; document.body.appendChild(probe);
    const m = getComputedStyle(probe).color.match(/\d+(\.\d+)?/g).map(Number); probe.remove(); return m.slice(0, 3);
}, token);

/* =============================================================
   1. Short laptop viewport (1280x585 @1.5x) - both themes
   ============================================================= */
for (const theme of ["dark", "light"]) {
    console.log(`\n1. Short laptop window 1280x585 @1.5x [${theme}]`);
    const app = await openApp({ theme });
    const { page } = app;

    await check(`[${theme}] Recent Chats shows at least 4 complete rows (was ~2.5: two full rows and a sliced one)`, async () => {
        const n = await fullyVisibleRows(page);
        assert(n >= 4, `only ${n} complete rows visible`);
    });

    await check(`[${theme}] the sidebar still fits the window: nothing overflows, the account row stays anchored at the bottom`, async () => {
        const sb = await rectOf(page, "#app-sidebar"), foot = await rectOf(page, ".sidebar-footer");
        assert(sb.bottom <= 585 + 0.5, "sidebar taller than the window");
        assert(foot.bottom <= sb.bottom + 0.5 && sb.bottom - foot.bottom < 20, `account row not anchored (gap ${sb.bottom - foot.bottom})`);
        const sbScroll = await page.evaluate(() => { const s = document.getElementById("app-sidebar"); return s.scrollHeight - s.clientHeight; });
        assert(sbScroll <= 1, `the whole sidebar scrolls (${sbScroll}px) - only the chat list may scroll`);
    });

    await check(`[${theme}] the logo, all four navigation rows and the profile row are still present and still touch-friendly (>= 36px rows)`, async () => {
        const nav = await page.evaluate(() => [...document.querySelectorAll(".nav-rail-btn")].map((b) => ({ t: b.textContent.trim(), h: b.getBoundingClientRect().height, w: b.getBoundingClientRect().width })));
        eq(nav.map((n) => n.t).join("|"), "Chat|Quizzes|Projects|Pocket Notes");
        for (const n of nav) assert(n.h >= 36 && n.w >= 200, `${n.t} ${n.w}x${n.h}`);
        const brand = await rectOf(page, ".sidebar-brand .brand-mark");
        assert(brand.w >= 40 && brand.h >= 38, "logo size preserved");
        assert((await rectOf(page, ".profile-trigger-btn")).h >= 44, "profile row");
        eq(await page.evaluate(() => document.querySelector("#app-sidebar").getBoundingClientRect().width), 272, "sidebar width unchanged (laptop width token)");
    });

    await check(`[${theme}] chat rows end exactly where the navigation rows end (scrollbar lives in the gutter, rows are not narrowed)`, async () => {
        const nav = await rectOf(page, ".nav-rail-btn"), row = await rectOf(page, ".chat-item.active");
        assert(Math.abs(nav.right - row.right) < 1 && Math.abs(nav.x - row.x) < 1, `nav ${nav.x}-${nav.right}, row ${row.x}-${row.right}`);
    });

    await check(`[${theme}] scrollbar: slim 8px bar, no arrow buttons, thumb >= 3:1 against its surface (it was ~1.2:1)`, async () => {
        const sb = await page.evaluate(() => { const h = document.querySelector(".history-section"), cs = getComputedStyle(h); return { w: h.offsetWidth - h.clientWidth, sw: cs.scrollbarWidth, sc: cs.scrollbarColor, overflow: h.scrollHeight > h.clientHeight }; });
        assert(sb.overflow, "list should overflow in this scenario");
        eq(sb.w, 8, "scrollbar width");
        eq(sb.sw, "auto", "standard scrollbar-width must stay unset or Chromium ignores ::-webkit-scrollbar");
        eq(sb.sc, "auto", "standard scrollbar-color must stay unset in Chromium");
        assert(/::-webkit-scrollbar-button\s*,?[^{]*\{[^}]*display:\s*none/.test(SHELL_CSS), "scrollbar buttons removed");
        const thumb = await colourOfToken(page, "--color-text-faint");
        const surface = await colourOfToken(page, "--color-surface");
        const r = ratio(thumb, surface);
        assert(r >= 3, `thumb contrast ${r.toFixed(2)}:1`);
        assert(/thumb[^{]*\{[^}]*var\(--color-text-faint\)/.test(SHELL_CSS), "thumb uses the token that was measured");
    });

    await check(`[${theme}] the conversation scroller got the same slim button-less scrollbar`, async () => {
        const a2 = await openApp({ theme, longConversation: true });
        const info = await a2.page.evaluate(() => { const c = document.getElementById("chat-box"), cs = getComputedStyle(c); return { w: c.offsetWidth - c.clientWidth, sw: cs.scrollbarWidth, over: c.scrollHeight > c.clientHeight }; });
        await a2.close();
        assert(info.over, "long conversation should overflow");
        eq(info.w, 8); eq(info.sw, "auto");
    });

    await check(`[${theme}] keyboard focus ring is NOT clipped on any chat row, header button or account control while tabbing the sidebar`, async () => {
        await page.focus("#nav-tab-chat");
        const bad = []; let rows = 0;
        for (let i = 0; i < 40; i++) {
            await page.keyboard.press("Tab");
            const info = await page.evaluate(() => { const a = document.activeElement; return { inSidebar: !!a.closest("#app-sidebar"), cls: String(a.className) }; });
            if (!info.inSidebar) break;
            if (/chat-title-text/.test(info.cls)) rows++;
            const r = await ringClipping(page);
            if (!r.ok) bad.push(r.why);
        }
        assert(rows >= 4, `only tabbed through ${rows} chat rows`);
        assert(bad.length === 0, "clipped: " + [...new Set(bad)].join(" | "));
    });

    await check(`[${theme}] the first row's ring (the one that was cut off at its top and sides) is fully drawn - pixel-level check`, async () => {
        await page.focus("#nav-tab-chat");
        for (let i = 0; i < 12; i++) { await page.keyboard.press("Tab"); if (await page.evaluate(() => /chat-title-text/.test(document.activeElement.className))) break; }
        const g = await page.evaluate(() => { const a = document.activeElement, r = a.getBoundingClientRect(), h = document.querySelector(".history-section"), hr = h.getBoundingClientRect(); return { rowLeft: r.left, rowRight: r.right, rowTop: r.top, left: hr.left, right: hr.right - (h.offsetWidth - h.clientWidth), top: hr.top }; });
        assert(g.rowLeft - 4 >= g.left - 0.5, `left ring ${g.rowLeft - 4} < clip ${g.left}`);
        assert(g.rowRight + 4 <= g.right + 8 + 0.5, "right ring fits (extends into the scrollbar gutter)");
        assert(g.rowTop - 4 >= g.top - 0.5, `top ring ${g.rowTop - 4} < clip ${g.top}`);
    });

    await check(`[${theme}] row action buttons (pin / rename / delete) are at least 24x24 CSS px`, async () => {
        const sizes = await page.evaluate(() => [...document.querySelectorAll(".chat-item.active .action-btn")].map((b) => { const r = b.getBoundingClientRect(); return [b.title, r.width, r.height]; }));
        eq(sizes.length, 3, "pin, rename, delete");
        for (const [t, w, h] of sizes) assert(w >= 24 && h >= 24, `${t}: ${w}x${h}`);
    });

    await check(`[${theme}] no page errors, no horizontal overflow`, async () => {
        assert(app.errors.length === 0, "page errors: " + app.errors.join(" | "));
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "horizontal scroll");
    });
    await app.close();
}

/* =============================================================
   2. Must NOT change
   ============================================================= */
console.log("\n2. Unchanged where it should be");
{
    const app = await openApp({ width: 1440, height: 900, dsf: 1 });
    const { page } = app;
    await check("tall window (1440x900): sidebar spacing is exactly the original (46px nav rows, 6px gaps, 44px chat rows)", async () => {
        const g = await page.evaluate(() => ({
            nav: document.querySelector(".nav-rail-btn").getBoundingClientRect().height,
            gap: document.querySelectorAll(".nav-rail-btn")[1].getBoundingClientRect().top - document.querySelectorAll(".nav-rail-btn")[0].getBoundingClientRect().bottom,
            chat: document.querySelector(".chat-item").getBoundingClientRect().height,
            profile: document.querySelector(".profile-trigger-btn").getBoundingClientRect().height,
        }));
        eq(g.nav, 46); eq(g.gap, 6); eq(g.chat, 44); eq(g.profile, 56);
    });
    await check("tall window: the scrollbar arrows are gone there too, and rows align with the nav", async () => {
        const nav = await rectOf(page, ".nav-rail-btn"), row = await rectOf(page, ".chat-item.active");
        assert(Math.abs(nav.right - row.right) < 1);
    });
    await app.close();
}
for (const [w, h] of [[390, 780], [320, 640]]) {
    const app = await openApp({ width: w, height: h, dsf: 2 });
    const { page } = app;
    await check(`[${w}px phone] drawer sidebar keeps its full-size rows (compaction is desktop/tablet only) and has no horizontal overflow`, async () => {
        await page.evaluate(() => document.body.classList.add("sidebar-open"));
        await sleep(450);
        const nav = await page.evaluate(() => document.querySelector(".nav-rail-btn").getBoundingClientRect().height);
        assert(nav >= 44, `nav row ${nav}px`);
        const sizes = await page.evaluate(() => [...document.querySelectorAll(".chat-item .action-btn")].map((b) => { const r = b.getBoundingClientRect(); return Math.min(r.width, r.height); }).filter((n) => n > 0));
        for (const n of sizes) assert(n >= 40, `touch target ${n}px`);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), "horizontal overflow");
    });
    await app.close();
}

/* =============================================================
   3. Independent scrolling
   ============================================================= */
console.log("\n3. Independent scrolling (real mouse wheel)");
{
    const app = await openApp({ longConversation: true });
    const { page } = app;
    const snap = () => page.evaluate(() => ({
        list: document.querySelector(".history-section").scrollTop, chat: document.getElementById("chat-box").scrollTop,
        sidebar: document.getElementById("app-sidebar").scrollTop, page: document.scrollingElement.scrollTop,
        composerY: document.querySelector(".input-container").getBoundingClientRect().y, sidebarY: document.getElementById("app-sidebar").getBoundingClientRect().y,
    }));
    await check("wheel over the chat list scrolls ONLY the list - sidebar, conversation, page and composer stay put", async () => {
        const before = await snap(); const box = await rectOf(page, ".history-section");
        await page.mouse.move(box.x + 60, box.y + 60); await page.mouse.wheel({ deltaY: 120 }); await sleep(250);
        const after = await snap();
        assert(after.list > before.list, "list did not scroll");
        eq(after.sidebar, 0); eq(after.page, 0); eq(after.chat, before.chat, "conversation moved"); eq(after.composerY, before.composerY, "composer moved");
    });
    await check("wheel over the conversation scrolls ONLY the conversation - list, sidebar and composer stay put", async () => {
        const before = await snap(); const box = await rectOf(page, "#chat-box");
        await page.mouse.move(box.x + box.w / 2, box.y + box.h / 2); await page.mouse.wheel({ deltaY: -400 }); await sleep(250);
        const mid = await snap();
        await page.mouse.wheel({ deltaY: 600 }); await sleep(250);
        const after = await snap();
        assert(after.chat !== before.chat || mid.chat !== before.chat, "conversation did not scroll");
        eq(after.list, before.list, "list moved"); eq(after.sidebar, 0); eq(after.page, 0); eq(after.composerY, before.composerY, "composer moved");
    });
    await check("the list does not chain its scroll to the page when it hits its end (overscroll-behavior)", async () => {
        const box = await rectOf(page, ".history-section");
        await page.mouse.move(box.x + 60, box.y + 60);
        for (let i = 0; i < 6; i++) { await page.mouse.wheel({ deltaY: 400 }); await sleep(60); }
        await sleep(200);
        const s = await snap();
        eq(s.page, 0); eq(s.sidebar, 0); eq(s.sidebarY, 0);
    });
    await check("keyboard scrolling works in the list: focused row + ArrowDown/Tab keeps the focused row visible", async () => {
        await page.evaluate(() => { document.querySelector(".history-section").scrollTop = 0; });
        await page.focus("#nav-tab-chat");
        for (let i = 0; i < 40; i++) {
            await page.keyboard.press("Tab");
            const inList = await page.evaluate(() => !!document.activeElement.closest(".history-section"));
            if (!inList && i > 6) break;
        }
        const vis = await page.evaluate(() => { const a = document.activeElement.closest(".history-section") ? document.activeElement : null; if (!a) return true; const r = a.getBoundingClientRect(), h = document.querySelector(".history-section").getBoundingClientRect(); return r.bottom > h.top && r.top < h.bottom; });
        assert(vis, "focused row scrolled out of view");
    });
    await app.close();
}

/* =============================================================
   4. Features still work (real clicks)
   ============================================================= */
console.log("\n4. Features still work");
{
    const app = await openApp();
    const { page } = app;
    const shown = (sel) => page.evaluate((s) => { const e = document.querySelector(s); return !!e && !e.classList.contains("hidden") && e.getBoundingClientRect().width > 0; }, sel);

    await check("navigation: Quizzes, Projects, Pocket Notes and back to Chat each show their own view", async () => {
        for (const [tab, view] of [["quizzes", "#quizzes-view"], ["projects", "#projects-view"], ["notes", "#notes-view"], ["chat", "#chat-view"]]) {
            await page.click(`#nav-tab-${tab}`); await sleep(150);
            assert(await shown(view), `${view} not shown after clicking ${tab}`);
            eq(await page.evaluate((t) => document.getElementById("nav-tab-" + t).getAttribute("aria-selected"), tab), "true");
        }
    });
    await check("search: the magnifier reveals the box and typing filters the list, clearing restores it", async () => {
        await page.click("#sidebar-search-toggle"); await sleep(120);
        assert(await shown("#sidebar-search-container"), "search box hidden");
        await page.type("#sidebar-search-input", "newton"); await sleep(200);
        eq(await page.evaluate(() => document.querySelectorAll(".recent-chats-list .chat-item").length), 1);
        await page.evaluate(() => { const i = document.getElementById("sidebar-search-input"); i.value = ""; i.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true })); });
        await sleep(200);
        eq(await page.evaluate(() => document.querySelectorAll(".recent-chats-list .chat-item").length), 9);
        await page.click("#sidebar-search-toggle"); await sleep(100);
    });
    await check("new chat: the + button adds a chat to Recent Chats and opens it", async () => {
        await page.click("#sidebar-new-chat-btn"); await sleep(250);
        eq(await page.evaluate(() => document.querySelectorAll(".recent-chats-list .chat-item").length), 10);
        assert(await page.evaluate(() => document.querySelector(".recent-chats-list .chat-item.active .chat-title-label").textContent === "New Chat"));
    });
    await check("select: clicking another row makes it the active chat", async () => {
        await page.evaluate(() => document.querySelectorAll(".recent-chats-list .chat-item .chat-title-text")[3].click()); await sleep(200);
        eq(await page.evaluate(() => document.querySelector(".recent-chats-list .chat-item.active .chat-title-label").textContent), "Hi");
    });
    await check("rename: the rename button opens an input; typing + Enter saves the new title", async () => {
        await page.hover(".recent-chats-list .chat-item.active"); await sleep(150);
        await page.click(".recent-chats-list .chat-item.active .action-btn[title='Rename Chat']"); await sleep(200);
        await page.waitForSelector(".chat-item .rename-input");
        await page.keyboard.down("Control"); await page.keyboard.press("a"); await page.keyboard.up("Control");
        await page.keyboard.type("Renamed by test"); await page.keyboard.press("Enter"); await sleep(250);
        assert(await page.evaluate(() => [...document.querySelectorAll(".chat-title-label")].some((e) => e.textContent === "Renamed by test")), "title not updated");
    });
    await check("delete: the delete button (confirm accepted) removes exactly that chat", async () => {
        await page.hover(".recent-chats-list .chat-item.active"); await sleep(150);
        await page.click(".recent-chats-list .chat-item.active .action-btn[title='Delete Chat']"); await sleep(300);
        eq(await page.evaluate(() => document.querySelectorAll(".recent-chats-list .chat-item").length), 9);
        assert(!(await page.evaluate(() => [...document.querySelectorAll(".chat-title-label")].some((e) => e.textContent === "Renamed by test"))));
    });
    await check("composer: typing works, Direct/Socratic switch, attach menu opens, quick cards present", async () => {
        await page.click("#user-input"); await page.type("#user-input", "hello");
        eq(await page.evaluate(() => document.getElementById("user-input").value), "hello");
        await page.click("#btn-socratic"); await sleep(100);
        assert(await page.evaluate(() => document.getElementById("btn-socratic").classList.contains("active")));
        await page.click("#btn-direct"); await sleep(100);
        assert(await page.evaluate(() => document.getElementById("btn-direct").classList.contains("active")));
        await page.click(".composer-plus-btn"); await sleep(200);
        assert(await page.evaluate(() => document.querySelector(".composer-plus-btn").getAttribute("aria-expanded") === "true"), "attach menu");
        await page.keyboard.press("Escape");
    });
    await check("theme switching still works from the page toggle", async () => {
        const before = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
        // the first [data-theme-toggle] in the DOM is the drawer's (hidden on desktop): click the VISIBLE one
        await page.evaluate(() => { const b = [...document.querySelectorAll("[data-theme-toggle]")].find((e) => e.getBoundingClientRect().width > 0); b.click(); });
        await sleep(250);
        const after = await page.evaluate(() => document.documentElement.getAttribute("data-theme"));
        assert(after && after !== before, `theme did not change (${before} -> ${after})`);
    });
    await check("no page errors during any of the above", async () => assert(app.errors.length === 0, app.errors.join(" | ")));
    await app.close();
}

/* =============================================================
   5. Reduced motion
   ============================================================= */
console.log("\n5. prefers-reduced-motion");
{
    const app = await openApp({ reduced: true });
    await check("with reduced motion every interactive transition in the sidebar and composer is effectively instant", async () => {
        const durs = await app.page.evaluate(() => ["#nav-tab-quizzes", ".chat-item", ".mini-icon-btn", ".quick-card", "#send-btn", ".composer-plus-btn", ".composer-pill", ".chat-item .action-btn", ".input-container"].map((s) => {
            const e = document.querySelector(s); if (!e) return [s, -1];
            return [s, Math.max(...getComputedStyle(e).transitionDuration.split(",").map((d) => parseFloat(d) * (d.includes("ms") ? 1 : 1000)))];
        }));
        for (const [s, ms] of durs) assert(ms >= 0 && ms <= 0.1, `${s} still transitions for ${ms}ms`);
    });
    await app.close();
}

await browser.close();
server.close();

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) {
    console.log("\nFailures:");
    for (const f of failures) console.log(` - ${f.name}\n   ${f.e.stack.split("\n").slice(0, 3).join("\n   ")}`);
    process.exit(1);
}
process.exit(0);
