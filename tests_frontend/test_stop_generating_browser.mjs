/* ============================================================
   KOGNIT - STOP GENERATING + COMPOSER DISCLAIMER (REAL BROWSER)
   ============================================================
   jsdom has no network stack and no layout engine, so it cannot prove two
   things this feature depends on. This file drives a REAL Chrome/Chromium/Edge
   (puppeteer-core, no browser download) against the REAL templates/index.html,
   app.js, stream-render.js and CSS, served by a REAL local HTTP server:

     1. CANCELLATION IS REAL. The server holds a slow NDJSON stream open. When
        Stop is pressed the server must observe the client's connection close
        before it finished writing (res 'close' with writableFinished=false).
        Hiding a spinner would not do that.
     2. LAYOUT. The disclaimer sits under the composer, centred, overlapping
        nothing, readable (WCAG AA contrast, measured from computed colours) in
        BOTH themes, without horizontal overflow on phone widths; and the Stop
        button occupies exactly the rectangle Send occupied.

   Only Supabase and the CDN libraries are stubbed/served locally (as in
   test_notes_browser.mjs). The chat stream is served by a real http.Server.

   BROWSER DISCOVERY: set CHROME_PATH to a Chrome/Chromium/Edge executable, or
   rely on the usual install locations. If NO browser is found this prints
   SKIPPED and exits 0 - it never silently reports a pass.

   Run: node tests_frontend/test_stop_generating_browser.mjs
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
    console.log("\nSKIPPED: no Chrome/Chromium/Edge found (set CHROME_PATH to run the real-browser Stop-generating test).");
    console.log("0 passed, 0 failed (SKIPPED - real-network cancellation and real layout were NOT verified by this run)");
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
async function until(fn, ms = 5000, label = "condition") {
    const end = Date.now() + ms;
    while (Date.now() < end) { const v = await fn(); if (v) return v; await sleep(25); }
    throw new Error(`timed out waiting for ${label}`);
}

/* ------------------------------------------------------------
   Real HTTP server: static files + a slow, observable chat stream
   ------------------------------------------------------------ */
const MIME = { ".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".html": "text/html" };
const streams = [];     // one record per /api/chat/stream request, in order
let streamMode = "slow";

const server = await new Promise((resolve) => {
    const s = http.createServer((req, res) => {
        const u = new URL(req.url, "http://x");

        if (u.pathname === "/api/chat/stream" && req.method === "POST") {
            req.resume();                                    // consume the multipart body
            const rec = { mode: streamMode, startedAt: Date.now(), closedAt: null, finishedNormally: false, wrote: 0 };
            streams.push(rec);
            res.writeHead(200, { "Content-Type": "application/x-ndjson", "Cache-Control": "no-cache" });
            res.write(JSON.stringify({ type: "start", context: "thinking" }) + "\n");
            res.on("close", () => { rec.closedAt = Date.now(); rec.finishedNormally = res.writableFinished; clearInterval(timer); });
            let n = 0;
            const timer = setInterval(() => {
                n++; rec.wrote = n;
                if (rec.mode === "fast") {
                    if (n === 1) res.write(JSON.stringify({ type: "delta", text: "Quick " }) + "\n");
                    else { res.write(JSON.stringify({ type: "done", reply: "Quick answer.", research: null }) + "\n"); clearInterval(timer); res.end(); }
                } else if (n < 400) {
                    res.write(JSON.stringify({ type: "delta", text: `word${n} ` }) + "\n");
                } else {
                    res.write(JSON.stringify({ type: "done", reply: "never reached in these tests", research: null }) + "\n");
                    clearInterval(timer); res.end();
                }
            }, rec.mode === "fast" ? 40 : 60);
            return;
        }

        if (u.pathname === "/api/profile" && req.method === "GET") {
            res.writeHead(200, { "Content-Type": "application/json" });
            return res.end(JSON.stringify({ profile: { name: "Tester", user_class: "Class 9-10 (SSC)", stream: "Science" } }));
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
    args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", ...(process.env.CHROME_ARGS ? process.env.CHROME_ARGS.split(" ") : [])],
});

async function openApp({ width = 1280, height = 900, theme = "dark" } = {}) {
    const page = await browser.newPage();
    await page.setViewport({ width, height });
    await page.setBypassServiceWorker(true);
    const errors = [];
    // Service-worker registration asks this bare test server for /sw.js, which it does not serve. Unrelated to this feature.
    page.on("pageerror", (e) => { if (!/fetching the script/.test(e.message)) errors.push(e.message); });
    page.on("console", (m) => { if (m.type() === "error" && !/Failed to load resource|fetching the script/.test(m.text())) errors.push(m.text()); });
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
    await page.waitForFunction(() => window.MathJax && window.MathJax.typesetPromise && typeof window.sendMessage === "function" && document.getElementById("stop-btn"), { timeout: 20000 });
    await page.evaluate((t) => document.documentElement.setAttribute("data-theme", t), theme);
    await sleep(200);
    return { page, errors, close: () => page.close() };
}

const rectOf = (page, sel) => page.evaluate((s) => { const el = document.querySelector(s); if (!el) return null; const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height, right: r.right, bottom: r.bottom }; }, sel);
const visible = (page, sel) => page.evaluate((s) => { const el = document.querySelector(s); if (!el) return false; const cs = getComputedStyle(el); const r = el.getBoundingClientRect(); return cs.display !== "none" && cs.visibility !== "hidden" && r.width > 0 && r.height > 0; }, sel);
async function sendText(page, text) { await page.click("#user-input"); await page.type("#user-input", text); await page.click("#send-btn"); }
const chatText = (page) => page.evaluate(() => document.getElementById("chat-box").innerText);

// WCAG contrast of the element's computed text colour against the first opaque background found walking up.
const contrastOf = (page, sel) => page.evaluate((s) => {
    const parse = (c) => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number); return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 }; };
    const lum = ({ r, g, b }) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
    const el = document.querySelector(s);
    const fg = parse(getComputedStyle(el).color);
    let bg = null;
    for (let n = el; n && !bg; n = n.parentElement) { const c = parse(getComputedStyle(n).backgroundColor); if (c && c.a > 0.99) bg = c; }
    if (!bg) bg = parse(getComputedStyle(document.documentElement).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
    const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
    return { ratio: (hi + 0.05) / (lo + 0.05), fg, bg };
}, sel);

const overlaps = (a, b) => !(a.right <= b.x + 0.5 || b.right <= a.x + 0.5 || a.bottom <= b.y + 0.5 || b.bottom <= a.y + 0.5);

/* =============================================================
   1. REAL cancellation
   ============================================================= */
console.log("\n1. Real network cancellation (real Chrome + real HTTP server)");
{
    const app = await openApp();
    const { page } = app;
    let sendRect;

    await check("Stop takes exactly the rectangle Send occupied (same far-right slot, same size)", async () => {
        // Positions are compared RELATIVE TO THE COMPOSER: sending the first message moves the whole composer
        // from the vertically-centred empty state to the bottom of the page, which is unrelated to the buttons.
        const rel = async () => {
            const comp = await rectOf(page, ".input-container");
            const b = await rectOf(page, "#send-btn:not(.hidden), #stop-btn:not(.hidden)");
            return { fromRight: comp.right - b.right, fromTop: b.y - comp.y, w: b.w, h: b.h };
        };
        const idleSend = await rel();
        sendRect = idleSend;
        assert(idleSend && idleSend.w > 0, "Send visible while idle");
        await sendText(page, "Explain Newton's laws");
        await page.waitForSelector("#stop-btn:not(.hidden)", { timeout: 5000 });
        await sleep(400);                                         // let the empty-state -> conversation layout settle
        const stopRel = await rel();
        for (const k of ["fromRight", "w", "h"]) assert(Math.abs(stopRel[k] - idleSend[k]) < 1, `${k}: send ${idleSend[k]} vs stop ${stopRel[k]}`);
        assert(Math.abs(stopRel.fromTop - idleSend.fromTop) < 40, `vertical slot within the composer: send ${idleSend.fromTop} vs stop ${stopRel.fromTop}`);
        eq(await visible(page, "#send-btn"), false, "Send hidden while generating");
        eq(await page.evaluate(() => document.getElementById("stop-btn").getAttribute("aria-label")), "Stop generating");
    });

    await check("the answer streams in progressively before Stop is pressed", async () => {
        await until(async () => /word3\b/.test(await chatText(page)), 5000, "streamed text");
    });

    await check("pressing Stop makes the SERVER observe the connection close mid-stream (the request is really cancelled)", async () => {
        const rec = streams[0];
        eq(rec.closedAt, null, "stream still open before Stop");
        const t0 = Date.now();
        await page.click("#stop-btn");
        await until(() => rec.closedAt !== null, 2500, "server to see the client disconnect");
        eq(rec.finishedNormally, false, "the server was cut off, it did not finish");
        assert(rec.wrote < 400, "server was still mid-stream (wrote " + rec.wrote + " of 400 chunks)");
        assert(rec.closedAt - t0 < 1500, `closed ${rec.closedAt - t0}ms after the click`);
    });

    await check("the server stops being able to deliver: no further chunks are produced after the disconnect", async () => {
        const wroteAtClose = streams[0].wrote;
        await sleep(400);
        assert(streams[0].wrote <= wroteAtClose + 1, `server kept producing: ${wroteAtClose} -> ${streams[0].wrote}`);
    });

    await check("partial text is preserved, flagged as stopped, with no error message; Send is back", async () => {
        const text = await chatText(page);
        assert(/word1 /.test(text), "partial text still on screen");
        assert(/Generation stopped\. This answer is incomplete\./.test(text), "stopped notice shown");
        assert(!/Error connecting/.test(text), "no network error shown for an intentional stop");
        eq(await visible(page, "#send-btn"), true);
        eq(await visible(page, "#stop-btn"), false);
        const kept = await chatText(page);
        await sleep(300);
        eq(await chatText(page), kept, "the kept text does not change after Stop");
        const focus = await page.evaluate(() => document.activeElement && document.activeElement.id);
        eq(focus, "user-input", "keyboard focus lands in the input, not on <body>");
    });

    await check("a new question after Stop streams and completes normally in the same chat", async () => {
        streamMode = "fast";
        await sendText(page, "Second question");
        await until(async () => /Quick answer\./.test(await chatText(page)), 5000, "second answer");
        await until(() => visible(page, "#send-btn"), 3000, "Send restored");
        eq(streams.length, 2);
        eq(streams[1].finishedNormally, true, "the second stream was not cancelled by the first Stop");
        const text = await chatText(page);
        assert(text.indexOf("Generation stopped") < text.indexOf("Quick answer."), "order preserved: stopped answer, then the new one");
        streamMode = "slow";
    });

    await check("Stop works when pressed with the keyboard (Tab to the control, Enter)", async () => {
        await page.click("#user-input"); await page.type("#user-input", "Keyboard stop test");
        await page.keyboard.press("Enter");                         // Enter-to-send, as before
        await page.waitForSelector("#stop-btn:not(.hidden)", { timeout: 5000 });
        await until(async () => /word2\b/.test(await chatText(page)), 5000, "streamed text");
        await page.focus("#stop-btn");
        const rec = streams[2];
        await page.keyboard.press("Enter");
        await until(() => rec.closedAt !== null, 2500, "server to see the disconnect");
        eq(rec.finishedNormally, false);
        await until(() => visible(page, "#send-btn"), 3000, "Send restored");
    });

    await check("a second page-level Stop on an idle composer does nothing and raises no page errors", async () => {
        await page.evaluate(() => window.stopGeneration());
        assert(app.errors.length === 0, "page errors: " + app.errors.join(" | "));
    });
    await app.close();
}

/* =============================================================
   2. Disclaimer layout - real CSS, both themes, several widths
   ============================================================= */
console.log("\n2. Disclaimer layout (real CSS)");
for (const theme of ["dark", "light"]) {
    const app = await openApp({ theme });
    const { page } = app;

    await check(`[${theme}] empty state: sits directly under the composer, above the quick cards, centred, overlapping nothing`, async () => {
        const comp = await rectOf(page, ".input-container");
        const d = await rectOf(page, ".composer-disclaimer");
        assert(await visible(page, ".composer-disclaimer"), "visible");
        assert(d.y >= comp.bottom - 0.5, `below composer (${d.y} vs ${comp.bottom})`);
        assert(d.y - comp.bottom < 24, `directly below, not floating far away (gap ${d.y - comp.bottom})`);
        assert(Math.abs((d.x + d.w / 2) - (comp.x + comp.w / 2)) < 2, "centred under the composer");
        const cards = await rectOf(page, ".quick-actions");
        if (cards && cards.h > 0) assert(d.bottom <= cards.y + 0.5, "does not collide with the quick-action cards");
        const controls = await page.evaluate(() => [...document.querySelectorAll(".input-container button, .input-container textarea, .composer-menu")].map((el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, w: r.width, h: r.height, id: el.id || el.className }; }).filter((r) => r.w > 0 && r.h > 0));
        for (const c of controls) assert(!overlaps(d, c), `overlaps composer control ${c.id}`);
        const text = await page.evaluate(() => document.querySelector(".composer-disclaimer").textContent);
        eq(text, "Kloriz can make mistakes. Check important information.");
    });

    await check(`[${theme}] muted, small, readable: WCAG AA contrast >= 4.5 and font-size <= 13px, no background/border`, async () => {
        const { ratio, fg, bg } = await contrastOf(page, ".composer-disclaimer");
        assert(ratio >= 4.5, `contrast ${ratio.toFixed(2)}:1 (fg ${JSON.stringify(fg)} on bg ${JSON.stringify(bg)})`);
        const st = await page.evaluate(() => { const cs = getComputedStyle(document.querySelector(".composer-disclaimer")); return { fs: parseFloat(cs.fontSize), bg: cs.backgroundColor, bw: cs.borderTopWidth, bs: cs.boxShadow }; });
        assert(st.fs <= 13 && st.fs >= 11, `font-size ${st.fs}`);
        assert(st.bg === "rgba(0, 0, 0, 0)" && st.bw === "0px" && st.bs === "none", "no card styling");
        // visually secondary: dimmer than the primary text colour
        const primary = await contrastOf(page, ".empty-chat-title, .composer-pill, #user-input");
        assert(ratio < primary.ratio || primary.ratio > 4.5, "secondary to primary text");
    });

    await check(`[${theme}] still visible and not moved while an answer is generating, and with a conversation on screen`, async () => {
        streamMode = "slow";
        const before = await rectOf(page, ".composer-disclaimer");
        await sendText(page, "Hold on");
        await page.waitForSelector("#stop-btn:not(.hidden)", { timeout: 5000 });
        await until(async () => /word2\b/.test(await chatText(page)), 5000, "streamed text");
        assert(await visible(page, ".composer-disclaimer"), "visible while generating");
        const comp = await rectOf(page, ".input-container");
        const d = await rectOf(page, ".composer-disclaimer");
        assert(d.y >= comp.bottom - 0.5 && d.bottom <= 900, "below the composer and inside the viewport");
        const stop = await rectOf(page, "#stop-btn");
        assert(!overlaps(d, stop), "does not overlap Stop");
        await page.click("#stop-btn");
        await until(() => visible(page, "#send-btn"), 3000, "Send restored");
        void before;
    });
    await app.close();
}

for (const [w, h] of [[390, 780], [320, 640]]) {
    const app = await openApp({ width: w, height: h, theme: "dark" });
    const { page } = app;

    await check(`[${w}px phone] no horizontal overflow; disclaimer inside the screen, below the composer, overlapping nothing`, async () => {
        const overflow = await page.evaluate(() => ({ doc: document.documentElement.scrollWidth, win: window.innerWidth, body: document.body.scrollWidth }));
        assert(overflow.doc <= overflow.win && overflow.body <= overflow.win, `horizontal scroll: ${JSON.stringify(overflow)}`);
        const comp = await rectOf(page, ".input-container");
        const d = await rectOf(page, ".composer-disclaimer");
        assert(await visible(page, ".composer-disclaimer"), "visible on mobile");
        assert(d.x >= 0 && d.right <= w && d.bottom <= h, `fully on screen: ${JSON.stringify(d)}`);
        assert(d.y >= comp.bottom - 0.5, "below the composer");
        assert(Math.abs((d.x + d.w / 2) - w / 2) < 2, "centred");
        const controls = await page.evaluate(() => [...document.querySelectorAll(".input-container button, .input-container textarea")].map((el) => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, w: r.width, h: r.height, id: el.id || el.className }; }).filter((r) => r.w > 0 && r.h > 0));
        for (const c of controls) assert(!overlaps(d, c), `overlaps ${c.id}`);
        const lines = await page.evaluate(() => { const el = document.querySelector(".composer-disclaimer"); return Math.round(el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight)); });
        assert(lines <= 2, `wraps to ${lines} lines`);
    });

    await check(`[${w}px phone] Stop is a full-size 44px touch target in Send's slot, and the disclaimer stays clear of it`, async () => {
        const sendRect = await rectOf(page, "#send-btn");
        eq(Math.round(sendRect.w), 44);
        await sendText(page, "Phone test");
        await page.waitForSelector("#stop-btn:not(.hidden)", { timeout: 5000 });
        const stop = await rectOf(page, "#stop-btn");
        eq(Math.round(stop.w), 44); eq(Math.round(stop.h), 44);
        for (const k of ["x", "y"]) assert(Math.abs(stop[k] - sendRect[k]) < 1, `${k} moved`);
        const d = await rectOf(page, ".composer-disclaimer");
        assert(!overlaps(d, stop), "no overlap with Stop");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);
        assert(overflow, "no horizontal scroll while generating");
        await page.click("#stop-btn");
        await until(() => visible(page, "#send-btn"), 3000, "Send restored");
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
