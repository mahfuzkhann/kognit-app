/* ============================================================
   KOGNIT POCKET NOTES - FRONTEND TESTS
   ============================================================
   Runs the REAL templates/index.html, REAL static/js/app.js,
   notes-store.js, notes.js, nav-views.js, the REAL vendored DOMPurify
   and the real `marked` package in jsdom. Stubbed: Supabase, fetch (an
   in-memory notes API that behaves like backend/main.py) and
   IndexedDB (the `fake-indexeddb` package).

   WHAT THIS FILE CANNOT PROVE: real-browser service-worker behaviour
   (install, scope, cold-start offline, update flow). Part G tests the
   service worker's LOGIC in isolation with a fake Cache Storage; the
   real-browser checks are manual - see the manual verification steps in
   the Pocket Notes delivery notes.

   Run: node tests_frontend/test_notes.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
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
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (a !== b) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
const rawSleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* Fixed sleeps are flaky under load, so every `sleep(n)` in this file first waits n ms and then waits
   until the booted page is SETTLED: no API request in flight and no DOM mutation for QUIET_MS. That makes
   "click, then assert" deterministic without hard-coding how long IndexedDB/jsdom happen to take. */
const QUIET_MS = 70;
let activeDom = null;
async function sleep(ms) {
    await rawSleep(ms);
    if (!activeDom) return;
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
        const quietFor = Date.now() - activeDom.lastMutation;
        if (activeDom.inflight === 0 && quietFor >= QUIET_MS) return;
        await rawSleep(Math.max(5, QUIET_MS - quietFor));
    }
}

const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const MARKED_SRC = fs.readFileSync(path.join(HERE, "node_modules", "marked", "lib", "marked.umd.js"), "utf8");
const SRC = {
    stream: read("static/js/stream-render.js"),
    purify: read("static/js/vendor/purify.min.js"),
    app: read("static/js/app.js"),
    store: read("static/js/notes-store.js"),
    notes: read("static/js/notes.js"),
    nav: read("static/js/nav-views.js"),
};
const TEMPLATE = read("templates/index.html");

/* ------------------------------------------------------------
   In-memory notes API (mirrors backend/main.py semantics)
   ------------------------------------------------------------ */
function makeServer() {
    const server = {
        notes: [], calls: [], down: false, failNext: null, seq: 0,
        flash: [{ front: "What is F?", back: "ma" }, { front: "SI unit of force?", back: "newton" }, { front: "Third card", back: "c" }],
        quiz: [
            { question: "F = ?", options: ["ma", "mv", "m/a", "a/m"], correct_index: 0, explanation: "Newton's 2nd law" },
            { question: "Unit of force?", options: ["joule", "newton", "watt", "pascal"], correct_index: 1, explanation: "SI" },
        ],
        suggestions: [{ label: "Newton Laws", kind: "topic", origin: "ai" }],
    };
    const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
    server.add = (over = {}) => {
        const id = `00000000-0000-4000-8000-${String(++server.seq).padStart(12, "0")}`;
        const n = { id, client_id: `n_seed${server.seq}xxxx`, content_md: "Seed note body", takeaway: null, question_md: null,
            sources: [], tags: [], needs_review: false, source_ref: null, flashcards: null, mini_quiz: null,
            created_at: `2026-10-0${(server.seq % 9) + 1}T10:00:00+00:00`, updated_at: "2026-10-10T10:00:00+00:00", ...over };
        server.notes.push(n);
        return n;
    };
    server.fetch = async (url, opts = {}) => {
        const method = (opts.method || "GET").toUpperCase();
        const body = opts.body ? JSON.parse(opts.body) : undefined;
        server.calls.push({ method, url, body, headers: opts.headers || {} });
        if (server.down) throw new TypeError("Failed to fetch");
        if (server.failNext) { const f = server.failNext; server.failNext = null; return json(f.status, { detail: f.detail }); }
        if (!/^Bearer tok/.test((opts.headers || {}).Authorization || "")) return json(401, { detail: "Not authenticated" });

        const u = new URL(url, "https://kognit.test");
        const m = u.pathname.match(/^\/api\/notes(?:\/([^/]+))?(?:\/(flashcards|mini-quiz|suggest-tags))?$/);
        if (!m) return json(404, { detail: "nope" });
        const [, id, action] = m;
        const sorted = () => server.notes.slice().sort((a, b) => b.created_at.localeCompare(a.created_at));

        if (!id && method === "GET") {
            const limit = Number(u.searchParams.get("limit") || 20), offset = Number(u.searchParams.get("offset") || 0);
            const rows = sorted();
            return json(200, { notes: rows.slice(offset, offset + limit), limit, offset, has_more: rows.length > offset + limit });
        }
        if (!id && method === "POST") {
            const existing = server.notes.find((n) => n.client_id === body.client_id);
            if (existing) return json(200, { note: existing, created: false });
            const note = server.add({ client_id: body.client_id, content_md: body.content_md, takeaway: body.takeaway || null,
                question_md: body.question_md || null, sources: body.sources || [], source_ref: body.source_ref || null,
                tags: [{ label: "Class 9-10 (SSC)", kind: "class", origin: "known" }], needs_review: true });
            return json(201, { note, created: true });
        }
        const note = server.notes.find((n) => n.id === id);
        if (!note) return json(404, { detail: "Note not found." });
        if (!action && method === "GET") return json(200, { note });
        if (!action && method === "PATCH") {
            note.tags = body.tags.map((t) => ({ label: t.label, kind: t.kind, origin: t.origin === "ai" ? "ai" : (note.tags.find((x) => x.label === t.label)?.origin || "student") }));
            note.needs_review = !note.tags.some((t) => t.kind === "subject");
            return json(200, { note });
        }
        if (!action && method === "DELETE") { server.notes = server.notes.filter((n) => n !== note); return json(200, { deleted: true }); }
        if (action === "suggest-tags") return json(200, { suggestions: server.suggestions });
        const col = action === "flashcards" ? "flashcards" : "mini_quiz";
        if (note[col] && !(body && body.regenerate === true)) return json(200, { [col]: note[col], note, regenerated: false });
        note[col] = action === "flashcards" ? server.flash : server.quiz;
        return json(200, { [col]: note[col], note, regenerated: true });
    };
    return server;
}

/* ------------------------------------------------------------
   Boot the real app + notes in jsdom
   ------------------------------------------------------------ */
async function boot(opts = {}) {
    const html = TEMPLATE.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kognit.test/" });
    const w = dom.window;
    w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    w.TextEncoder = TextEncoder; w.TextDecoder = TextDecoder;
    w.MessageChannel = MessageChannel;     // jsdom lacks it; real browsers have it (used for the SW status query)

    const env = { online: true, confirmAnswer: true, confirms: 0, authModalOpened: 0, signOutCallbacks: [], session: opts.session === undefined ? { access_token: "tok", user: { id: "user-1" } } : opts.session };
    const server = opts.server || makeServer();
    const tracker = { inflight: 0, lastMutation: Date.now() };
    const innerFetch = server.fetch;
    server.fetch = async (...args) => { tracker.inflight++; try { return await innerFetch(...args); } finally { tracker.inflight--; } };
    w.alert = () => {};
    w.confirm = () => { env.confirms++; return env.confirmAnswer; };
    Object.defineProperty(w.navigator, "onLine", { get: () => env.online, configurable: true });
    w.fetch = server.fetch;
    w.indexedDB = opts.indexedDB === undefined ? new IDBFactory() : opts.indexedDB;
    if (opts.serviceWorker) w.navigator.serviceWorker = opts.serviceWorker;

    const query = { select: () => query, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => query, eq: async () => ({ error: null }) };
    w.supabase = {
        createClient: () => ({
            auth: {
                getSession: async () => ({ data: { session: env.session } }),
                onAuthStateChange: (cb) => { env.signOutCallbacks.push(cb); return { data: { subscription: { unsubscribe() {} } } }; },
                signOut: async () => ({}),
            },
            from: () => query,
        }),
    };
    w.eval(MARKED_SRC);
    // MathJax itself is exercised by test_security_hardening.mjs; here it is a no-op so app.js's
    // bounded "wait for MathJax" poll does not spam the log.
    // jsdom has no layout engine. Stub the one layout fact notes.js depends on (an element is "laid out" when it is
    // attached and has no .hidden ancestor) and RECORD every element MathJax is asked to typeset, so the tests can
    // assert the root-cause rule: MathJax measures what it typesets, so it must never get a detached/hidden node.
    const typesetCalls = [];
    w.Element.prototype.getClientRects = function () {
        if (!this.isConnected) return [];
        for (let n = this; n; n = n.parentElement) if (n.classList && n.classList.contains("hidden")) return [];
        return [{}];
    };
    w.MathJax = Object.assign(w.MathJax || {}, {
        typesetPromise: async (els) => { (els || []).forEach((e) => typesetCalls.push({ connected: e.isConnected, laidOut: e.getClientRects().length > 0, cls: e.className || e.id || e.tagName })); },
        typesetClear: () => {},
        startup: { promise: Promise.resolve() },
    });
    if (opts.beforeScripts) opts.beforeScripts(w);
    const add = (src) => { const s = w.document.createElement("script"); s.textContent = src; w.document.body.appendChild(s); };
    add(SRC.stream); add(SRC.purify); add(SRC.app);
    w.openAuthModal = () => { env.authModalOpened++; };
    add(SRC.store);
    if (opts.noMarked) delete w.marked;
    add(SRC.notes);
    add(SRC.nav);
    new w.MutationObserver(() => { tracker.lastMutation = Date.now(); })
        .observe(w.document, { subtree: true, childList: true, attributes: true, characterData: true });
    activeDom = tracker;
    w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
    await sleep(40);
    return { w, doc: w.document, env, server, dom, typesetCalls };
}

function seedProject(w, messages) {
    const projects = w.getProjectsSnapshot();
    projects.length = 0;
    projects.push({ id: "p1", title: "Physics", chats: [{ id: "c1", title: "Newton", messages }] });
    return projects[0].chats[0];
}

const BOT_TEXT = "**Newton's second law**: $F = ma$.\n\nDefinition: force is mass times acceleration.";
const completedChat = () => [
    { role: "user", text: "Explain F=ma in physics" },
    { role: "bot", text: BOT_TEXT, takeaway: "Force depends on mass and acceleration.", research: null },
];

function toolbarFor(w, msgIndex = 1, extraMeta = {}) {
    const el = w.createBotMessageElement(BOT_TEXT, { projId: "p1", chatId: "c1", msgIndex, ...extraMeta });
    return el.querySelector(".bot-toolbar");
}
const saveBtnOf = (toolbar) => toolbar.querySelector(".toolbar-btn-save");
const click = (w, node) => node.dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
const labelOf = (btn) => btn.querySelector("span").textContent;
async function openNotesTab(h) {
    click(h.w, h.doc.getElementById("nav-tab-notes"));
    await sleep(60);
}
const buttonByText = (root, text) => [...root.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
const notesCalls = (server) => server.calls.filter((c) => /^\/api\/notes/.test(c.url));
const noteCards = (doc) => [...doc.querySelectorAll("#notes-view .note-card")];

/* =============================================================
   PART A - Save to Pocket Notes (answer toolbar)
   ============================================================= */
console.log("\nA. Save button");
await check("Save appears on a completed answer and does not remove Copy/Like/Dislike/Regenerate", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const tb = toolbarFor(h.w);
    assert(saveBtnOf(tb), "Save missing");
    eq(tb.querySelectorAll(".toolbar-btn").length, 5, "Copy + Like + Dislike + Regenerate + Save");
    assert(/Copy/.test(tb.textContent) && /Regenerate/.test(tb.textContent));
    eq(saveBtnOf(tb).title, "Save to Pocket Notes");
    eq(saveBtnOf(tb).dataset.state, "idle");
});

await check("Save is NOT offered on an interrupted/partial answer", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    assert(!saveBtnOf(toolbarFor(h.w, 1, { interrupted: true })));
});

await check("Save is NOT offered without a stable message reference", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    assert(!saveBtnOf(h.w.createBotMessageElement("x", {}).querySelector(".bot-toolbar")));
    assert(!saveBtnOf(toolbarFor(h.w, 7)), "msgIndex pointing at no message");
    assert(!saveBtnOf(toolbarFor(h.w, 0)), "msgIndex pointing at a user message");
});

await check("one click POSTs the original Markdown/LaTeX, takeaway, question and source reference - never a user_id", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    click(h.w, btn); await sleep(30);
    const post = h.server.calls.find((c) => c.method === "POST" && c.url === "/api/notes");
    assert(post, "no POST");
    eq(post.body.content_md, BOT_TEXT);
    eq(post.body.takeaway, "Force depends on mass and acceleration.");
    eq(post.body.question_md, "Explain F=ma in physics");
    assert(JSON.stringify(post.body.source_ref) === JSON.stringify({ projectId: "p1", chatId: "c1", msgIndex: 1 }));
    assert(/^[A-Za-z0-9_-]{8,64}$/.test(post.body.client_id), "client_id format");
    assert(!("user_id" in post.body), "user_id must never be sent");
    eq(post.headers.Authorization, "Bearer tok");
    eq(btn.dataset.state, "saved"); eq(labelOf(btn), "Saved"); assert(btn.disabled);
});

await check("double click while saving sends exactly one request", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    click(h.w, btn); click(h.w, btn); click(h.w, btn);
    await sleep(40);
    eq(h.server.calls.filter((c) => c.method === "POST").length, 1);
});

await check("saving again from a fresh toolbar is idempotent: 'Already saved', one note on the server", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    click(h.w, saveBtnOf(toolbarFor(h.w))); await sleep(30);
    const second = saveBtnOf(toolbarFor(h.w));
    // the first save was recorded, so a rebuilt toolbar already shows it as saved
    eq(second.dataset.state, "saved");
    eq(h.server.notes.length, 1);
});

await check("server 'created:false' (existing note) shows 'Already saved'", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    const client = JSON.parse(JSON.stringify({}));
    // pre-create the same logical note on the server, as another device would have
    click(h.w, btn); await sleep(30);
    h.w.KognitNotes._state.savedClientIds = {};           // forget locally
    const fresh = saveBtnOf(toolbarFor(h.w));
    eq(fresh.dataset.state, "idle");
    click(h.w, fresh); await sleep(30);
    eq(fresh.dataset.state, "already"); eq(labelOf(fresh), "Already saved");
    eq(h.server.notes.length, 1);
});

await check("a failed save shows Retry, keeps the content, and the retry reuses the SAME client_id", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    h.server.failNext = { status: 500, detail: "Could not reach your notes right now. Please try again." };
    click(h.w, btn); await sleep(30);
    eq(btn.dataset.state, "error"); eq(labelOf(btn), "Retry"); assert(!btn.disabled);
    assert(/Could not reach your notes/.test(btn.title), "error detail surfaced");
    click(h.w, btn); await sleep(30);
    const posts = h.server.calls.filter((c) => c.method === "POST");
    eq(posts.length, 2); eq(posts[0].body.client_id, posts[1].body.client_id, "idempotency key must survive the retry");
    eq(btn.dataset.state, "saved");
    eq(h.server.notes.length, 1);
});

await check("offline: no request is sent, state 'Offline', and a later click works after reconnecting", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    h.env.online = false;
    click(h.w, btn); await sleep(20);
    eq(notesCalls(h.server).length, 0); eq(btn.dataset.state, "offline"); assert(!btn.disabled);
    h.env.online = true;
    click(h.w, btn); await sleep(30);
    eq(btn.dataset.state, "saved");
});

await check("network failure mid-request shows Offline, not a fake success", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    h.server.down = true;
    click(h.w, btn); await sleep(30);
    eq(btn.dataset.state, "offline"); eq(h.server.notes.length, 0);
});

await check("not signed in: 'Sign in' state and the auth modal opens; nothing is sent", async () => {
    const h = await boot({ session: null }); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    click(h.w, btn); await sleep(20);
    eq(btn.dataset.state, "auth"); eq(h.env.authModalOpened, 1); eq(notesCalls(h.server).length, 0);
});

await check("a regenerated answer (different text, same position) is a DIFFERENT note; same text is the SAME id", async () => {
    const h = await boot();
    const id = h.w.KognitNotes._makeClientId;
    eq(id("p", "c", 1, "answer A"), id("p", "c", 1, "answer A"));
    assert(id("p", "c", 1, "answer A") !== id("p", "c", 1, "answer B"));
    assert(id("p", "c", 1, "a") !== id("p", "c", 2, "a"));
    assert(/^[A-Za-z0-9_-]{8,64}$/.test(id("p", "c", 1, "Bangla: \u09AC\u09C7\u0997")));
});

await check("research sources are saved only when grounding was actually used", async () => {
    const h = await boot();
    const chat = seedProject(h.w, completedChat());
    chat.messages[1].research = { grounding_status: "used", sources: [{ title: "NCERT", url: "https://ncert.example/x", domain: "ncert.example" }] };
    click(h.w, saveBtnOf(toolbarFor(h.w))); await sleep(30);
    eq(h.server.calls.find((c) => c.method === "POST").body.sources.length, 1);

    const h2 = await boot();
    const chat2 = seedProject(h2.w, completedChat());
    chat2.messages[1].research = { grounding_status: "not_used", sources: [{ title: "X", url: "https://x.example" }] };
    click(h2.w, saveBtnOf(toolbarFor(h2.w))); await sleep(30);
    eq(h2.server.calls.find((c) => c.method === "POST").body.sources.length, 0);
});

await check("after a reload, an INTERRUPTED answer still shows the notice and is NOT offered Save; a complete one is", async () => {
    const h = await boot();
    seedProject(h.w, [
        { role: "user", text: "q1" }, { role: "bot", text: "partial ans", interrupted: true },
        { role: "user", text: "q2" }, { role: "bot", text: "complete answer" },
    ]);
    h.w.loadChat("p1", "c1"); await sleep(20);
    const bots = [...h.doc.querySelectorAll("#chat-box .bot-message")];
    const partial = bots.find((b) => /partial ans/.test(b.textContent));
    const complete = bots.find((b) => /complete answer/.test(b.textContent));
    assert(partial.querySelector(".answer-interrupted-notice"), "notice restored after reload");
    assert(!partial.querySelector(".toolbar-btn-save"), "no Save on a partial answer");
    assert(complete.querySelector(".toolbar-btn-save"), "Save on the complete answer");
});

await check("the button's label is announced politely and busy state is exposed to assistive tech", async () => {
    const h = await boot(); seedProject(h.w, completedChat());
    const btn = saveBtnOf(toolbarFor(h.w));
    assert(btn.getAttribute("aria-label"), "aria-label");
    eq(btn.querySelector("span").getAttribute("aria-live"), "polite");
    eq(btn.getAttribute("aria-busy"), "false");
});

/* =============================================================
   PART B - the Pocket Notes view
   ============================================================= */
console.log("\nB. Notes view");
await check("empty state explains how to save the first note", async () => {
    const h = await boot(); await openNotesTab(h);
    assert(/No notes yet/.test(h.doc.getElementById("notes-view").textContent));
    assert(/choose Save/.test(h.doc.getElementById("notes-view").textContent));
});

await check("loading state is shown while the list is fetched", async () => {
    const h = await boot();
    const slow = h.server.fetch;
    let release; const gate = new Promise((r) => { release = r; });
    h.w.fetch = async (...a) => { await gate; return slow(...a); };
    click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(20);
    assert(/Loading your notes/.test(h.doc.getElementById("notes-view").textContent));
    release(); await sleep(40);
    assert(!/Loading your notes/.test(h.doc.getElementById("notes-view").textContent));
});

await check("lists notes with title, tags, 'Needs review' and date", async () => {
    const h = await boot();
    h.server.add({ content_md: "# Newton's Laws\nBody", tags: [{ label: "Physics", kind: "subject", origin: "inferred" }] });
    h.server.add({ content_md: "Mystery note", needs_review: true });
    await openNotesTab(h);
    const cards = noteCards(h.doc);
    eq(cards.length, 2);
    assert(cards.some((c) => /Newton's Laws/.test(c.textContent) && /Physics/.test(c.textContent)));
    assert(cards.some((c) => /Mystery note/.test(c.textContent) && /Needs review/.test(c.textContent)));
});

await check("search filters by title, body and tag text", async () => {
    const h = await boot();
    h.server.add({ content_md: "Photosynthesis in leaves" });
    h.server.add({ content_md: "Ohm's law", tags: [{ label: "Electricity", kind: "topic", origin: "student" }] });
    await openNotesTab(h);
    const search = h.doc.querySelector(".notes-search");
    const type = (v) => { search.value = v; search.dispatchEvent(new h.w.Event("input", { bubbles: true })); };
    type("photo"); eq(noteCards(h.doc).length, 1);
    type("electricity"); eq(noteCards(h.doc).length, 1);
    type("zzz"); eq(noteCards(h.doc).length, 0);
    assert(/No matching notes/.test(h.doc.getElementById("notes-view").textContent));
    type(""); eq(noteCards(h.doc).length, 2);
});

await check("tag filter chips toggle with aria-pressed and filter the list", async () => {
    const h = await boot();
    h.server.add({ content_md: "A", tags: [{ label: "Physics", kind: "subject", origin: "inferred" }] });
    h.server.add({ content_md: "B", tags: [{ label: "Biology", kind: "subject", origin: "inferred" }] });
    await openNotesTab(h);
    const chip = () => [...h.doc.querySelectorAll(".notes-chip")].find((c) => /^Physics/.test(c.textContent));
    eq(chip().getAttribute("aria-pressed"), "false");
    click(h.w, chip());
    eq(chip().getAttribute("aria-pressed"), "true"); eq(noteCards(h.doc).length, 1);
    click(h.w, chip());
    eq(noteCards(h.doc).length, 2);
});

await check("opening a note renders Markdown and math, and strips scripts/event handlers (XSS)", async () => {
    const h = await boot();
    h.server.add({ content_md: "**bold** and $E=mc^2$\n\n<script>window.__pwned=1</script><img src=x onerror=\"window.__pwned=2\">[x](javascript:alert(1))" });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    const body = h.doc.querySelector(".notes-body");
    assert(body.querySelector("strong"), "markdown rendered");
    assert(body.textContent.includes("E=mc^2"), "math text kept for MathJax");
    assert(!body.querySelector("script"), "script survived");
    assert(!body.querySelector("[onerror]"), "event handler survived");
    assert(![...body.querySelectorAll("a")].some((a) => /^javascript:/i.test(a.getAttribute("href") || "")), "javascript: link survived");
    assert(!h.w.__pwned, "payload executed");
});

await check("if marked/DOMPurify are unavailable the note falls back to PLAIN TEXT (never raw HTML)", async () => {
    const h = await boot({ noMarked: true });
    h.server.add({ content_md: "plain <b>text</b>" });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    const body = h.doc.querySelector(".notes-body");
    assert(body.classList.contains("notes-body-plain"));
    assert(!body.querySelector("b"), "must not interpret HTML");
    assert(body.textContent.includes("<b>text</b>"));
    assert(/shown as plain text/.test(h.doc.getElementById("notes-view").textContent));
});

await check("detail view shows the question, key takeaway and sources with safe links only", async () => {
    const h = await boot();
    h.server.add({ content_md: "Answer", question_md: "Why?", takeaway: "Short idea",
        sources: [{ title: "Good", url: "https://ok.example/p", domain: "ok.example" }, { title: "Bad", url: "javascript:alert(1)" }] });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    const txt = h.doc.querySelector(".notes-detail-pane").textContent;
    assert(/Why\?/.test(txt) && /Short idea/.test(txt) && /Sources/.test(txt));
    const links = [...h.doc.querySelectorAll(".notes-sources a")];
    eq(links.length, 1); eq(links[0].getAttribute("rel"), "noopener noreferrer");
    assert(/Bad/.test(h.doc.querySelector(".notes-sources").textContent), "unsafe source still listed, as text only");
});

await check("removing a tag PATCHes the remaining tags; provenance is the server's decision", async () => {
    const h = await boot();
    h.server.add({ tags: [{ label: "Physics", kind: "subject", origin: "inferred" }, { label: "Vector", kind: "topic", origin: "student" }] });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    click(h.w, h.doc.querySelector('[aria-label="Remove tag Vector"]')); await sleep(40);
    const patch = h.server.calls.find((c) => c.method === "PATCH");
    eq(patch.body.tags.length, 1); eq(patch.body.tags[0].label, "Physics");
    eq(h.server.notes[0].tags.length, 1);
});

await check("adding a tag works via button and Enter; invalid/empty input is not sent", async () => {
    const h = await boot();
    h.server.add({ tags: [{ label: "Physics", kind: "subject", origin: "inferred" }] });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    const input = () => h.doc.querySelector(".notes-tag-input");
    click(h.w, buttonByText(h.doc, "Add")); await sleep(10);
    eq(h.server.calls.filter((c) => c.method === "PATCH").length, 0, "empty tag must not be sent");
    input().value = "#Momentum";
    input().dispatchEvent(new h.w.KeyboardEvent("keydown", { key: "Enter", bubbles: true })); await sleep(40);
    const patch = h.server.calls.find((c) => c.method === "PATCH");
    assert(patch.body.tags.some((t) => t.label === "Momentum" && t.kind === "topic"), "added with # stripped");
});

await check("'Needs review' notes offer subject quick-picks; choosing one fixes the tags", async () => {
    const h = await boot();
    h.server.add({ content_md: "No subject keyword here", needs_review: true });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    assert(/couldn't tell the subject/.test(h.doc.querySelector(".notes-tags").textContent));
    click(h.w, buttonByText(h.doc.querySelector(".notes-subject-picks"), "Physics")); await sleep(40);
    assert(h.server.notes[0].tags.some((t) => t.label === "Physics" && t.kind === "subject"));
    eq(h.server.notes[0].needs_review, false);
    assert(!/couldn't tell the subject/.test(h.doc.querySelector(".notes-tags").textContent), "review prompt cleared");
});

await check("a tag-edit failure is reported and the original tags stay", async () => {
    const h = await boot();
    h.server.add({ tags: [{ label: "Vector", kind: "topic", origin: "student" }] });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    h.server.failNext = { status: 400, detail: "Tag labels must be 1-40 characters of letters, numbers or simple punctuation." };
    click(h.w, h.doc.querySelector('[aria-label="Remove tag Vector"]')); await sleep(40);
    assert(/Tag labels must be/.test(h.doc.querySelector(".notes-tags").textContent));
    eq(h.server.notes[0].tags.length, 1);
});

await check("AI topic suggestions are explicit, labelled as not official, and only saved when clicked", async () => {
    const h = await boot();
    h.server.add({ content_md: "Body" });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    eq(h.server.calls.filter((c) => /suggest-tags/.test(c.url)).length, 0, "never automatic");
    click(h.w, buttonByText(h.doc, "Suggest topics (AI)")); await sleep(40);
    assert(/not official curriculum chapters/.test(h.doc.querySelector(".notes-tags").textContent));
    eq(h.server.calls.filter((c) => c.method === "PATCH").length, 0, "suggestion alone must not save");
    click(h.w, buttonByText(h.doc.querySelector(".notes-suggestions"), "+ Newton Laws")); await sleep(40);
    const patch = h.server.calls.find((c) => c.method === "PATCH");
    assert(patch.body.tags.some((t) => t.label === "Newton Laws" && t.origin === "ai"));
});

await check("delete asks for confirmation, removes the note, and cancelling sends nothing", async () => {
    const h = await boot();
    h.server.add({ content_md: "Doomed" });
    await openNotesTab(h);
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    h.env.confirmAnswer = false;
    click(h.w, buttonByText(h.doc, "Delete note")); await sleep(20);
    eq(h.server.calls.filter((c) => c.method === "DELETE").length, 0);
    h.env.confirmAnswer = true;
    click(h.w, buttonByText(h.doc, "Delete note")); await sleep(50);
    eq(h.server.notes.length, 0);
    assert(/No notes yet/.test(h.doc.getElementById("notes-view").textContent));
});

await check("a load failure shows an accessible error with a working Try again", async () => {
    const h = await boot();
    h.server.add({ content_md: "Ok" });
    h.server.failNext = { status: 502, detail: "Could not reach your notes right now. Please try again." };
    await openNotesTab(h);
    const alert = h.doc.querySelector('#notes-view [role="alert"]');
    assert(alert && /Could not reach your notes/.test(alert.textContent));
    click(h.w, buttonByText(h.doc, "Try again")); await sleep(50);
    eq(noteCards(h.doc).length, 1);
});

await check("signed-out visitors see a sign-in message, not an empty-notes lie", async () => {
    const h = await boot({ session: null });
    await openNotesTab(h);
    assert(/sign in/i.test(h.doc.getElementById("notes-view").textContent));
    eq(notesCalls(h.server).length, 0);
});

await check("accessibility: real buttons, labelled search, tabpanel semantics, keyboard-reachable controls", async () => {
    const h = await boot();
    h.server.add({ content_md: "Body", needs_review: true });
    await openNotesTab(h);
    eq(h.doc.querySelector(".notes-search").getAttribute("aria-label"), "Search your notes");
    eq(h.doc.getElementById("notes-view").getAttribute("role"), "tabpanel");
    assert(noteCards(h.doc).every((c) => c.tagName === "BUTTON"), "cards must be buttons (keyboard + SR)");
    assert(h.doc.querySelector('.notes-chips[role="group"]'), "chip group labelled");
    click(h.w, noteCards(h.doc)[0]); await sleep(30);
    eq(h.doc.activeElement.className.includes("notes-back"), true, "focus moves into the detail view");
    assert([...h.doc.querySelectorAll("#notes-view button")].every((b) => b.getAttribute("type") === "button"), "no implicit submit buttons");
    assert(h.doc.querySelector('#notes-view [aria-live="polite"]'), "polite live region present");
});

await check("pagination: more than one server page is fetched and all notes are listed", async () => {
    const h = await boot();
    for (let i = 0; i < 60; i++) h.server.add({ content_md: `Note ${i}`, created_at: `2026-09-${String((i % 28) + 1).padStart(2, "0")}T10:${String(i % 60).padStart(2, "0")}:00+00:00` });
    await openNotesTab(h); await sleep(40);
    eq(noteCards(h.doc).length, 60);
    assert(h.server.calls.filter((c) => c.method === "GET").length >= 2, "followed has_more");
});

await check("concurrent loads do not stack requests", async () => {
    const h = await boot();
    h.server.add({ content_md: "x" });
    h.server.calls.length = 0;
    await Promise.all([h.w.KognitNotes.loadNotes(), h.w.KognitNotes.loadNotes(), h.w.KognitNotes.loadNotes()]);
    eq(h.server.calls.filter((c) => c.method === "GET").length, 1);
});

/* =============================================================
   PART C - flashcards and mini-quiz
   ============================================================= */
console.log("\nC. Study tools");
async function openFirstNote(h) { await openNotesTab(h); click(h.w, noteCards(h.doc)[0]); await sleep(30); }

await check("nothing is generated until the student asks; the AI-generated/not-verified notice is shown", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await openFirstNote(h);
    eq(h.server.calls.filter((c) => /flashcards|mini-quiz/.test(c.url)).length, 0);
    const txt = h.doc.querySelector(".notes-study").textContent;
    assert(/AI-generated/.test(txt) && /isn't independently verified/.test(txt) && /not added to your progress/.test(txt));
});

await check("flashcards: create, reveal answer, navigate; bounds are enforced", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await openFirstNote(h);
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    const face = () => h.doc.querySelector(".notes-flash-text").textContent;
    eq(h.doc.querySelector(".notes-flash-count").textContent, "Card 1 of 3");
    eq(face(), "What is F?");
    assert(buttonByText(h.doc, "Previous").disabled, "no previous on first card");
    click(h.w, buttonByText(h.doc, "Show answer")); eq(face(), "ma");
    click(h.w, buttonByText(h.doc, "Next")); eq(face(), "SI unit of force?", "next card shows the question side again");
    click(h.w, buttonByText(h.doc, "Next"));
    assert(buttonByText(h.doc, "Next").disabled, "no next on last card");
    click(h.w, buttonByText(h.doc, "Previous")); eq(h.doc.querySelector(".notes-flash-count").textContent, "Card 2 of 3");
});

await check("stored flashcards open with NO request; regenerate is explicit and sends regenerate:true", async () => {
    const h = await boot(); h.server.add({ content_md: "Body", flashcards: h.server.flash });
    await openFirstNote(h);
    h.server.calls.length = 0;
    click(h.w, buttonByText(h.doc, "Open flashcards")); await sleep(20);
    eq(h.server.calls.length, 0);
    click(h.w, buttonByText(h.doc, "Regenerate flashcards")); await sleep(40);
    const post = h.server.calls.find((c) => /flashcards/.test(c.url));
    eq(post.body.regenerate, true);
});

await check("generation failure shows the server's safe message and a retry path; the note is untouched", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await openFirstNote(h);
    h.server.failNext = { status: 502, detail: "Couldn't create valid study material this time. Please try again." };
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    assert(/Couldn't create valid study material/.test(h.doc.querySelector(".notes-study").textContent));
    assert(h.doc.querySelector('.notes-study [role="alert"]'));
    assert(buttonByText(h.doc, "Create flashcards"), "can retry");
    eq(h.server.notes.length, 1);
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    assert(h.doc.querySelector(".notes-flash"), "retry succeeded");
});

await check("create buttons are disabled while a generation is in flight (no duplicate requests)", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await openFirstNote(h);
    const slow = h.server.fetch; let release; const gate = new Promise((r) => { release = r; });
    h.w.fetch = async (...a) => { await gate; return slow(...a); };
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(10);
    assert(buttonByText(h.doc, "Create flashcards").disabled && buttonByText(h.doc, "Create mini-quiz").disabled);
    release(); await sleep(50);
    eq(h.server.calls.filter((c) => /flashcards/.test(c.url)).length, 1);
});

async function startQuiz(h) {
    await openFirstNote(h);
    click(h.w, buttonByText(h.doc, "Create mini-quiz")); await sleep(40);
}
const pick = (h, q, o) => click(h.w, h.doc.querySelectorAll(".notes-quiz-q")[q].querySelectorAll(".notes-quiz-opt")[o]);

await check("mini-quiz: options are radios, 'Check answers' needs every question answered, then score + correct answers + explanations", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await startQuiz(h);
    const opts = h.doc.querySelectorAll(".notes-quiz-opt");
    assert([...opts].every((o) => o.getAttribute("role") === "radio" && o.getAttribute("aria-checked") === "false"));
    eq(h.doc.querySelector(".notes-quiz-q .notes-quiz-options").getAttribute("role"), "radiogroup");
    assert(buttonByText(h.doc, "Check answers").disabled, "disabled until all answered");
    pick(h, 0, 0);
    eq(h.doc.querySelectorAll(".notes-quiz-q")[0].querySelector('[aria-checked="true"]').textContent.startsWith("A."), true);
    assert(buttonByText(h.doc, "Check answers").disabled);
    pick(h, 1, 0);                                                  // wrong on purpose (correct is B)
    click(h.w, buttonByText(h.doc, "Check answers"));
    eq(h.doc.querySelector(".notes-quiz-score").textContent, "Score: 1 / 2");
    const fb = [...h.doc.querySelectorAll(".notes-quiz-feedback")];
    assert(/^Correct/.test(fb[0].textContent) && /Newton's 2nd law/.test(fb[0].textContent));
    assert(/The answer is B/.test(fb[1].textContent) && /SI/.test(fb[1].textContent));
    assert(h.doc.querySelectorAll(".notes-quiz-opt.correct").length === 2 && h.doc.querySelectorAll(".notes-quiz-opt.wrong").length === 1);
    assert([...h.doc.querySelectorAll(".notes-quiz-opt")].every((o) => o.disabled), "locked after checking");
});

await check("mini-quiz is SELF-GRADED: checking answers sends no request and never touches the quiz/evidence endpoints", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await startQuiz(h);
    const before = h.server.calls.length;
    pick(h, 0, 0); pick(h, 1, 1);
    click(h.w, buttonByText(h.doc, "Check answers"));
    await sleep(30);
    eq(h.server.calls.length, before, "grading must be client-side only");
    assert(!h.server.calls.some((c) => /\/api\/quiz/.test(c.url)), "must never use the quiz endpoints");
    eq(h.doc.querySelector(".notes-quiz-score").textContent, "Score: 2 / 2");
});

await check("retry practice offers only the missed questions and can be retaken from the start", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await startQuiz(h);
    pick(h, 0, 0); pick(h, 1, 0);
    click(h.w, buttonByText(h.doc, "Check answers"));
    click(h.w, buttonByText(h.doc, "Practise the 1 you missed"));
    eq(h.doc.querySelectorAll(".notes-quiz-q").length, 1);
    assert(/Practice: questions you missed/.test(h.doc.querySelector(".notes-quiz").textContent));
    pick(h, 0, 1); click(h.w, buttonByText(h.doc, "Check answers"));
    eq(h.doc.querySelector(".notes-quiz-score").textContent, "Score: 1 / 1");
    assert(!buttonByText(h.doc, "Practise the 0 you missed"), "no practice offer when nothing missed");
    click(h.w, buttonByText(h.doc, "Retake from the start"));
    eq(h.doc.querySelectorAll(".notes-quiz-q").length, 2);
});

await check("leaving a note clears study state (no stale quiz carried to another note)", async () => {
    const h = await boot(); h.server.add({ content_md: "One" }); h.server.add({ content_md: "Two" });
    await openNotesTab(h); click(h.w, noteCards(h.doc)[0]); await sleep(30);
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    assert(h.doc.querySelector(".notes-flash"));
    click(h.w, buttonByText(h.doc, "\u2190 All notes"));
    click(h.w, noteCards(h.doc)[1]); await sleep(30);
    assert(!h.doc.querySelector(".notes-flash"), "flashcards must not leak across notes");
});

/* =============================================================
   PART C2 - typesetting rules (root cause of the giant-glyph bug)
   -------------------------------------------------------------
   MathJax MEASURES the element it typesets. Typeset a detached or
   display:none element and it emits <svg width="NaNex" ...>, which
   renders as enormous shapes. These tests pin the rules that prevent
   it; test_notes_browser.mjs proves the visual result in a real browser.
   ============================================================= */
console.log("\nC2. Typesetting rules");
await check("MathJax is never asked to typeset a detached or hidden element (flashcards, quiz, every interaction)", async () => {
    const h = await boot(); h.server.add({ content_md: "Body $x$" });
    await openFirstNote(h);
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    click(h.w, buttonByText(h.doc, "Show answer")); click(h.w, buttonByText(h.doc, "Next")); await sleep(20);
    click(h.w, buttonByText(h.doc, "Create mini-quiz")); await sleep(40);
    pick(h, 0, 0); pick(h, 1, 1); click(h.w, buttonByText(h.doc, "Check answers")); await sleep(20);
    assert(h.typesetCalls.length > 0, "nothing was typeset");
    const bad = h.typesetCalls.filter((c) => !c.connected || !c.laidOut);
    eq(bad.length, 0, "typeset requested for detached/hidden element(s): " + JSON.stringify(bad.slice(0, 3)));
});

await check("a quiz/flashcard click re-renders ONLY the study section: the note body node survives and is not re-typeset", async () => {
    const h = await boot(); h.server.add({ content_md: "Body $x$" });
    await openFirstNote(h);
    click(h.w, buttonByText(h.doc, "Create mini-quiz")); await sleep(40);
    const body = h.doc.querySelector(".notes-body");
    const study = h.doc.querySelector(".notes-study");
    h.typesetCalls.length = 0;
    pick(h, 0, 2); await sleep(20);
    assert(h.doc.querySelector(".notes-body") === body, "note body must be the SAME node (not rebuilt)");
    assert(h.doc.querySelector(".notes-study") !== study, "the study section is what gets replaced");
    eq(h.typesetCalls.length, 1, "exactly one typeset call per click");
    assert(/notes-study/.test(h.typesetCalls[0].cls), "and it targets the study section only, not the whole pane");
    h.typesetCalls.length = 0;
    click(h.w, buttonByText(h.doc, "Check answers")); await sleep(20);
    assert(!h.typesetCalls.some((c) => /notes-body|notes-detail-pane/.test(c.cls)), "never re-typeset the note body");
});

await check("keyboard focus stays on the same control after a click re-renders the study section", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await openFirstNote(h);
    click(h.w, buttonByText(h.doc, "Create mini-quiz")); await sleep(40);
    const opt = h.doc.querySelector('[data-fk="q1o2"]'); opt.focus();
    click(h.w, opt); await sleep(20);
    eq(h.doc.activeElement.dataset.fk, "q1o2", "focus must not drop to <body> after choosing an answer");
    eq(h.doc.activeElement.getAttribute("aria-checked"), "true");

    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    const reveal = h.doc.querySelector('[data-fk="reveal"]'); reveal.focus();
    click(h.w, reveal); await sleep(20);
    eq(h.doc.activeElement.dataset.fk, "reveal");
    // Next on the last card disables itself; focus must fall back to an enabled control, not vanish.
    h.doc.querySelector('[data-fk="next"]').focus(); click(h.w, h.doc.querySelector('[data-fk="next"]')); await sleep(10);
    h.doc.querySelector('[data-fk="next"]').focus(); click(h.w, h.doc.querySelector('[data-fk="next"]')); await sleep(10);
    assert(h.doc.querySelector('[data-fk="next"]').disabled, "last card reached");
    assert(h.doc.activeElement && h.doc.activeElement.tagName === "BUTTON", "focus fell back to a button");
});

await check("one click = one step: no duplicate handlers after regenerate or after reopening the note", async () => {
    const h = await boot(); h.server.add({ content_md: "Body" });
    await openFirstNote(h);
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    click(h.w, buttonByText(h.doc, "Regenerate flashcards")); await sleep(40);
    eq(h.doc.querySelectorAll(".notes-flash").length, 1, "one flashcard component");
    click(h.w, buttonByText(h.doc, "Next")); await sleep(10);
    eq(h.doc.querySelector(".notes-flash-count").textContent, "Card 2 of 3");
    click(h.w, buttonByText(h.doc, "\u2190 All notes")); click(h.w, noteCards(h.doc)[0]); await sleep(30);
    click(h.w, buttonByText(h.doc, "Open flashcards")); await sleep(20);
    eq(h.doc.querySelectorAll(".notes-flash").length, 1);
    click(h.w, buttonByText(h.doc, "Next")); await sleep(10);
    eq(h.doc.querySelector(".notes-flash-count").textContent, "Card 2 of 3", "reopened note: Next advances exactly one card");
    eq(h.doc.querySelectorAll(".notes-quiz").length, 0);
});

await check("a render while the Notes view is HIDDEN typesets nothing; showing the tab typesets the visible pane once", async () => {
    const h = await boot(); h.server.add({ content_md: "Body $x$" });
    await openFirstNote(h);
    click(h.w, h.doc.getElementById("nav-tab-chat")); await sleep(20);          // Notes view is now .hidden
    h.typesetCalls.length = 0;
    h.w.dispatchEvent(new h.w.Event("online")); await sleep(60);                // background reload re-renders the pane
    eq(h.typesetCalls.length, 0, "must not typeset while hidden (MathJax would measure NaN)");
    click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(80);
    assert(h.typesetCalls.length >= 1, "typeset after the view became visible");
    assert(h.typesetCalls.every((c) => c.connected && c.laidOut), "and only on a visible, attached element");
});

await check("the render helpers return UNATTACHED elements and never typeset them themselves (source-level guard)", () => {
    const src = read("static/js/notes.js");
    const calls = [...src.matchAll(/\btypeset\(([^)]*)\)/g)].map((m) => m[1]);
    assert(!/function renderFlashcards[\s\S]*?typeset\(wrap\)[\s\S]*?\n    function renderQuiz/.test(src), "renderFlashcards must not typeset its own detached wrapper");
    assert(!/function renderQuiz[\s\S]*?typeset\(wrap\)[\s\S]*?\n    \/\/ -{10,}/.test(src), "renderQuiz must not typeset its own detached wrapper");
    assert(calls.filter((c) => c === "wrap").length === 0, "no typeset(wrap) anywhere");
});

await check("generated Bangla + math + long content is inserted as TEXT (no HTML), long options carry wrapping CSS", async () => {
    const h = await boot();
    const note = h.server.add({ content_md: "Body" });
    h.server.flash = [{ front: "<img src=x onerror=\"window.__pwned=1\"> \u09AC\u09BE\u0982\u09B2\u09BE $x^2$", back: "b".repeat(500) }, { front: "f2", back: "b2" }, { front: "f3", back: "b3" }];
    await openFirstNote(h);
    click(h.w, buttonByText(h.doc, "Create flashcards")); await sleep(40);
    assert(!h.doc.querySelector(".notes-flash img"), "no element created from generated text");
    assert(h.doc.querySelector(".notes-flash-text").textContent.includes("<img src=x"), "shown literally");
    assert(!h.w.__pwned);
    const css = read("static/css/notes.css");
    for (const needle of [/\.notes-quiz-q \{[^}]*min-width: 0/, /\.notes-quiz-opt \{[^}]*white-space: normal/, /\.notes-study mjx-container \{[^}]*max-width: 100%/]) {
        assert(needle.test(css), "missing containment rule " + needle);
    }
});

/* =============================================================
   PART D - IndexedDB cache (notes-store.js)
   ============================================================= */
console.log("\nD. Offline cache store");
function newStore() {
    const w = new JSDOM("", { runScripts: "outside-only" }).window;
    w.indexedDB = new IDBFactory();
    w.eval(SRC.store);
    return { store: w.KognitNotesStore, w };
}
const N = (id, over = {}) => ({ id, client_id: "c_" + id + "xxxxxxx", content_md: "note " + id, created_at: `2026-10-0${id}T00:00:00Z`, tags: [], ...over });

await check("replaceAll then getAll round-trips, newest first, with a sync marker", async () => {
    const { store } = newStore();
    await store.replaceAll("u1", [N("1"), N("3"), N("2")], "2026-10-10T00:00:00Z");
    const got = await store.getAll("u1");
    eq(got.notes.map((n) => n.note.id).join(","), "3,2,1");
    eq(got.syncedAt, "2026-10-10T00:00:00Z");
    assert(got.notes[0].cachedAt);
});

await check("cache is scoped per user", async () => {
    const { store } = newStore();
    await store.replaceAll("u1", [N("1")], "t"); await store.replaceAll("u2", [N("2")], "t");
    eq((await store.getAll("u1")).notes.length, 1); eq((await store.getAll("u1")).notes[0].note.id, "1");
    eq((await store.getAll("u2")).notes[0].note.id, "2");
    eq((await store.getAll("nobody")).notes.length, 0);
});

await check("replaceAll removes notes the server no longer has (stale cache cannot resurrect deleted notes)", async () => {
    const { store } = newStore();
    await store.replaceAll("u1", [N("1"), N("2"), N("3")], "t1");
    await store.replaceAll("u1", [N("2")], "t2");
    eq((await store.getAll("u1")).notes.map((n) => n.note.id).join(","), "2");
});

await check("server wins: replaceAll overwrites an older cached copy", async () => {
    const { store } = newStore();
    await store.replaceAll("u1", [N("1", { tags: [{ label: "old" }] })], "t1");
    await store.replaceAll("u1", [N("1", { tags: [{ label: "new" }] })], "t2");
    eq((await store.getAll("u1")).notes[0].note.tags[0].label, "new");
});

await check("put/remove and clearUser (sign-out wipe) work and leave other users alone", async () => {
    const { store } = newStore();
    await store.replaceAll("u1", [N("1")], "t"); await store.replaceAll("u2", [N("9")], "t");
    await store.put("u1", N("2")); eq((await store.getAll("u1")).notes.length, 2);
    await store.remove("u1", "1"); eq((await store.getAll("u1")).notes.map((n) => n.note.id).join(","), "2");
    await store.clearUser("u1");
    const after = await store.getAll("u1");
    eq(after.notes.length, 0); eq(after.syncedAt, null);
    eq((await store.getAll("u2")).notes.length, 1, "other user untouched");
});

await check("remembers the last user (used only for the offline cold-start fallback)", async () => {
    const { store } = newStore();
    eq(await store.getLastUserId(), null);
    await store.replaceAll("u7", [N("1")], "t");
    eq(await store.getLastUserId(), "u7");
    await store.clearUser("u7");
    eq(await store.getLastUserId(), null, "sign-out forgets the user");
});

await check("quota errors reject with code 'quota' instead of throwing synchronously or hanging", async () => {
    const quotaFactory = {
        open() {
            const req = {};
            setTimeout(() => {
                const db = {
                    objectStoreNames: { contains: () => true },
                    transaction() { const e = new Error("full"); e.name = "QuotaExceededError"; throw e; },
                };
                req.result = db; req.onsuccess && req.onsuccess();
            }, 0);
            return req;
        },
    };
    const w = new JSDOM("", { runScripts: "outside-only" }).window;
    w.eval(SRC.store);
    const store = w.createNotesStore(quotaFactory);
    let code = null;
    try { await store.replaceAll("u1", [N("1")], "t"); } catch (e) { code = e.code; }
    eq(code, "quota");
});

await check("no IndexedDB at all: store reports unsupported and rejects with 'unavailable'", async () => {
    const w = new JSDOM("", { runScripts: "outside-only" }).window;
    w.eval(SRC.store);
    const store = w.createNotesStore(null);
    eq(store.isSupported(), false);
    let code = null;
    try { await store.getAll("u1"); } catch (e) { code = e.code; }
    eq(code, "unavailable");
});

/* =============================================================
   PART E - offline behaviour in the UI
   ============================================================= */
console.log("\nE. Offline reading");
async function bootWithSyncedNotes(extra = {}) {
    const h = await boot(extra);
    h.server.add({ content_md: "# Cached note\nBody $x$", tags: [{ label: "Physics", kind: "subject", origin: "inferred" }], flashcards: h.server.flash });
    h.server.add({ content_md: "Second cached note" });
    await openNotesTab(h); await sleep(60);                 // online load => cache filled
    return h;
}
function goOffline(h) { h.env.online = false; h.server.down = true; }

await check("online load fills the IndexedDB cache with the server's notes", async () => {
    const h = await bootWithSyncedNotes();
    const cached = await h.w.KognitNotesStore.getAll("user-1");
    eq(cached.notes.length, 2);
    assert(cached.syncedAt, "sync marker recorded");
    eq(Object.keys(h.w.KognitNotes._state.cachedIds).length, 2);
});

await check("offline: previously synchronized notes are readable from the cache, flagged as OFFLINE COPIES", async () => {
    const h = await bootWithSyncedNotes();
    goOffline(h);
    h.server.calls.length = 0;
    click(h.w, h.doc.getElementById("nav-tab-chat")); click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(80);
    eq(noteCards(h.doc).length, 2, "cached notes listed");
    assert(h.doc.querySelector(".note-badge-copy"), "marked as offline copy");
    assert(/offline copies/.test(h.doc.querySelector(".notes-status").textContent));
    assert(/Last synced/.test(h.doc.querySelector(".notes-status").textContent));
    assert(!/Available offline/.test(h.doc.getElementById("notes-view").textContent), "an offline copy must not be labelled current/available");
    click(h.w, noteCards(h.doc).find((c) => /Cached note/.test(c.textContent))); await sleep(30);
    assert(h.doc.querySelector(".notes-body").textContent.includes("Body"), "content readable");
});

await check("offline: every server-dependent action is disabled with an explanation; nothing is sent", async () => {
    const h = await bootWithSyncedNotes();
    goOffline(h);
    click(h.w, h.doc.getElementById("nav-tab-chat")); click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(80);
    h.server.calls.length = 0;
    click(h.w, noteCards(h.doc).find((c) => /Cached note/.test(c.textContent))); await sleep(30);
    const del = buttonByText(h.doc, "Delete note");
    assert(del.disabled && /offline/i.test(del.title));
    assert(buttonByText(h.doc, "Open flashcards") && !buttonByText(h.doc, "Open flashcards").disabled, "STORED flashcards can still be opened offline");
    assert(buttonByText(h.doc, "Create mini-quiz").disabled, "new generation needs the network");
    assert(!h.doc.querySelector(".note-tag-remove"), "no tag editing offline");
    assert(!h.doc.querySelector(".notes-tag-input"), "no tag adding offline");
    click(h.w, del); click(h.w, buttonByText(h.doc, "Create mini-quiz"));
    eq(notesCalls(h.server).length, 0);
});

await check("offline with nothing cached: honest empty message, no crash", async () => {
    const h = await boot();
    goOffline(h);
    await openNotesTab(h);
    assert(/offline and no notes have been saved on this device/.test(h.doc.getElementById("notes-view").textContent));
});

await check("reconnecting refreshes from the server: edits made elsewhere appear and deleted notes disappear (server wins)", async () => {
    const h = await bootWithSyncedNotes();
    goOffline(h);
    click(h.w, h.doc.getElementById("nav-tab-chat")); click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(80);
    eq(noteCards(h.doc).length, 2);
    h.server.notes = h.server.notes.filter((n) => !/Second/.test(n.content_md));            // deleted on another device
    h.server.notes[0].tags.push({ label: "NewFromServer", kind: "topic", origin: "student" });
    h.env.online = true; h.server.down = false;
    h.w.dispatchEvent(new h.w.Event("online")); await sleep(100);
    eq(noteCards(h.doc).length, 1, "deleted note gone");
    assert(/NewFromServer/.test(h.doc.getElementById("notes-view").textContent), "server edit shown");
    assert(!h.doc.querySelector(".note-badge-copy"), "no longer an offline copy");
    const cached = await h.w.KognitNotesStore.getAll("user-1");
    eq(cached.notes.length, 1, "cache rebuilt from server, stale entry removed");
});

await check("a server error (not offline) does NOT silently fall back to stale cached data as if current", async () => {
    const h = await bootWithSyncedNotes();
    h.server.failNext = { status: 502, detail: "Could not reach your notes right now. Please try again." };
    click(h.w, h.doc.getElementById("nav-tab-chat")); click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(80);
    assert(h.doc.querySelector('#notes-view [role="alert"]'), "error surfaced");
    assert(!h.doc.querySelector(".note-badge-copy"), "not presented as an offline copy while online");
});

await check("a PARTIAL server listing never replaces the cache (an unfetched note must not be deleted from the offline copy)", async () => {
    const h = await boot();
    for (let i = 0; i < 620; i++) h.server.add({ content_md: `Bulk ${i}`, created_at: `2026-08-${String((i % 27) + 1).padStart(2, "0")}T10:00:${String(i % 60).padStart(2, "0")}+00:00` });
    await h.w.KognitNotesStore.put("user-1", { id: "stale-keep-me", client_id: "c_staleXXXXXXX", content_md: "only in cache", created_at: "2026-01-01T00:00:00Z", tags: [] });
    await openNotesTab(h); await sleep(200);
    assert(noteCards(h.doc).length >= 600, "UI shows what was fetched");
    const cached = await h.w.KognitNotesStore.getAll("user-1");
    assert(cached.notes.some((n) => n.note.id === "stale-keep-me"), "partial listing must not wipe cached notes");
    eq(Object.keys(h.w.KognitNotes._state.cachedIds).length, 0, "and nothing is claimed as freshly cached");
});

await check("storage failure (no IndexedDB): notes still work online, and NOTHING claims offline availability", async () => {
    const h = await boot({ indexedDB: null });
    h.server.add({ content_md: "Body" });
    await openNotesTab(h); await sleep(40);
    eq(noteCards(h.doc).length, 1);
    assert(/couldn't store your notes on this device/.test(h.doc.querySelector(".notes-status").textContent));
    assert(!/Available offline/.test(h.doc.getElementById("notes-view").textContent));
});

await check("quota failure while caching is non-fatal and disables the offline claim", async () => {
    const h = await boot({ beforeScripts: (w) => { w.indexedDB = {
        open() { const req = {}; setTimeout(() => { req.result = { objectStoreNames: { contains: () => true }, transaction() { const e = new Error("q"); e.name = "QuotaExceededError"; throw e; } }; req.onsuccess && req.onsuccess(); }, 0); return req; } }; } });
    h.w.indexedDB = h.w.indexedDB;                                        // (override applied before notes-store ran)
    h.server.add({ content_md: "Body" });
    await openNotesTab(h); await sleep(60);
    eq(noteCards(h.doc).length, 1, "list still renders");
    eq(h.w.KognitNotes._state.storageOk, false);
    assert(/couldn't store/.test(h.doc.querySelector(".notes-status").textContent));
});

await check("signing out wipes the cached notes (shared-device safety) and clears the screen", async () => {
    const h = await bootWithSyncedNotes();
    eq((await h.w.KognitNotesStore.getAll("user-1")).notes.length, 2);
    h.env.signOutCallbacks.forEach((cb) => cb("SIGNED_OUT", null)); await sleep(60);
    eq((await h.w.KognitNotesStore.getAll("user-1")).notes.length, 0);
    eq(noteCards(h.doc).length, 0);
});

await check("offline cold start with no readable session falls back to the last signed-in user's cache", async () => {
    const h = await bootWithSyncedNotes();
    h.env.session = null; goOffline(h);
    h.w.KognitNotes._state.userId = null; h.w.KognitNotes._state.loaded = false;
    click(h.w, h.doc.getElementById("nav-tab-chat")); click(h.w, h.doc.getElementById("nav-tab-notes")); await sleep(80);
    eq(noteCards(h.doc).length, 2);
});

/* "Available offline" is only claimed when the service worker confirms the shell is cached */
function fakeServiceWorker(statusReply) {
    const controller = { postMessage(msg, ports) { if (msg.type === "GET_STATUS" && statusReply) ports[0].postMessage(statusReply); } };
    const listeners = {};
    return { controller, addEventListener: (n, f) => { listeners[n] = f; }, ready: Promise.resolve({}), register: async () => ({}), _calls: [] };
}

await check("'Available offline' badge appears ONLY when the service worker reports the app shell cached", async () => {
    const ready = await bootWithSyncedNotes({ serviceWorker: fakeServiceWorker({ version: "t", ready: true, mathReady: true }) });
    await ready.w.KognitNotes.loadNotes(); await sleep(40);
    assert(ready.doc.querySelector(".note-badge:not(.note-badge-copy)"), "badge shown when ready");
    assert(/available to read offline/.test(ready.doc.querySelector(".notes-status").textContent));

    const notReady = await bootWithSyncedNotes({ serviceWorker: fakeServiceWorker({ version: "t", ready: false }) });
    await notReady.w.KognitNotes.loadNotes(); await sleep(40);
    assert(!notReady.doc.querySelector(".note-badge:not(.note-badge-copy)"), "no badge when shell not cached");
    assert(/still being set up/.test(notReady.doc.querySelector(".notes-status").textContent));
});

await check("no service worker support: honest message, no offline badge", async () => {
    const h = await bootWithSyncedNotes();
    assert(!h.doc.querySelector(".note-badge:not(.note-badge-copy)"));
    assert(/can't keep Kognit open offline|still being set up/.test(h.doc.querySelector(".notes-status").textContent));
});

await check("first-time visitor (worker registered but not yet controlling): not told offline reading is available", async () => {
    const sw = fakeServiceWorker({ ready: true }); sw.controller = null;
    const h = await bootWithSyncedNotes({ serviceWorker: sw });
    assert(!h.doc.querySelector(".note-badge:not(.note-badge-copy)"));
    assert(!/available to read offline/.test(h.doc.querySelector(".notes-status").textContent));
});

await check("service worker registration: /sw.js at scope '/', only on a secure context", async () => {
    const regs = [];
    const sw = { controller: null, addEventListener() {}, ready: new Promise(() => {}), register: async (url, opts) => { regs.push([url, opts]); return {}; } };
    await boot({ serviceWorker: sw });
    eq(regs.length, 1); eq(regs[0][0], "/sw.js"); eq(regs[0][1].scope, "/");
});

/* =============================================================
   PART F - wiring / dependencies in the real template
   ============================================================= */
console.log("\nF. Template wiring");
await check("supabase-js is pinned to an exact version (no floating @2) and matches the service worker's URL", () => {
    const m = TEMPLATE.match(/<script src="(https:\/\/cdn\.jsdelivr\.net\/npm\/@supabase\/supabase-js@[^"]+)"/);
    assert(m, "supabase script tag missing");
    assert(/@\d+\.\d+\.\d+$/.test(m[1]), "must be an exact x.y.z version, got " + m[1]);
    assert(read("static/sw.js").includes(`"${m[1]}"`), "static/sw.js REQUIRED_CDN must list the identical URL");
});

await check("notes scripts load after app.js and BEFORE nav-views.js; stylesheet is linked", () => {
    const order = [...TEMPLATE.matchAll(/<script src="\/static\/js\/([a-z-]+\.js)"/g)].map((m) => m[1]);
    assert(order.indexOf("app.js") < order.indexOf("notes-store.js"));
    assert(order.indexOf("notes-store.js") < order.indexOf("notes.js"));
    assert(order.indexOf("notes.js") < order.indexOf("nav-views.js"), "nav-views may open the Notes tab at init");
    assert(TEMPLATE.includes('href="/static/css/notes.css"'));
});

await check("the service worker's precache list matches the real static files and the template's references", () => {
    const sw = read("static/sw.js");
    const core = [...sw.match(/const CORE_URLS = \[([\s\S]*?)\];/)[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
    for (const url of core) {
        if (url === "/") continue;
        assert(fs.existsSync(path.join(ROOT, url.replace(/^\//, ""))), `precached file does not exist: ${url}`);
    }
    for (const m of TEMPLATE.matchAll(/(?:src|href)="(\/static\/[^"]+)"/g)) {
        assert(core.includes(m[1]), `template references ${m[1]} but the service worker does not precache it`);
    }
});

await check("the template's CDN URLs that the app needs offline are listed in the service worker", () => {
    const sw = read("static/sw.js");
    assert(sw.includes("https://cdn.jsdelivr.net/npm/marked/marked.min.js"));
    assert(sw.includes("https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js"));
    assert(TEMPLATE.includes("https://cdn.jsdelivr.net/npm/marked/marked.min.js") && TEMPLATE.includes("mathjax@3/es5/tex-svg.js"));
});

/* =============================================================
   PART G - service worker logic (fake Cache Storage, isolated)
   ============================================================= */
console.log("\nG. Service worker logic");
activeDom = null;
function makeCaches() {
    const stores = new Map();
    const keyOf = (r) => (typeof r === "string" ? r : r.url);
    const open = async (name) => {
        if (!stores.has(name)) stores.set(name, new Map());
        const m = stores.get(name);
        return { match: async (r) => { const hit = m.get(keyOf(r)); return hit ? hit.clone() : undefined; }, put: async (r, res) => { m.set(keyOf(r), res.clone()); }, _m: m };
    };
    return { stores, open, keys: async () => [...stores.keys()], delete: async (n) => stores.delete(n), match: async () => undefined };
}
const basic = (body, status = 200) => { const r = new Response(body, { status }); Object.defineProperty(r, "type", { value: "basic" }); return r; };

function loadSW({ network }) {
    const handlers = {};
    const caches = makeCaches();
    const swSelf = {
        location: { origin: "https://kognit.test" },
        addEventListener: (n, f) => { handlers[n] = f; },
        skipWaiting: async () => { swSelf.skippedWaiting = true; },
        clients: { claim: async () => { swSelf.claimed = true; } },
    };
    // In a real worker a relative URL resolves against the worker's own origin.
    class SwRequest extends Request {
        constructor(input, init) { super(typeof input === "string" && input.startsWith("/") ? "https://kognit.test" + input : input, init); }
    }
    const sandbox = { self: swSelf, caches, fetch: (req, init) => network(req, init), Request: SwRequest, Response, URL, AbortController, setTimeout, clearTimeout, Promise, console };
    vm.runInNewContext(SRC_SW, sandbox);
    return { handlers, caches, swSelf };
}
const SRC_SW = read("static/sw.js");
const allOk = (req) => { const url = typeof req === "string" ? req : req.url; return basic("ok:" + url); };
const waitUntil = async (handler, event = {}) => { let p; handler({ ...event, waitUntil: (x) => { p = x; } }); await p; };

function fetchEvent(url, { method = "GET", mode = "no-cors" } = {}) {
    let responded = null;
    const event = { request: { method, url, mode }, respondWith: (p) => { responded = p; }, waitUntil: () => {} };
    return { event, result: () => responded };
}

await check("install precaches every core asset and the CDN libraries, then activates immediately", async () => {
    const { handlers, caches, swSelf } = loadSW({ network: async (r) => allOk(r) });
    await waitUntil(handlers.install);
    const shell = [...caches.stores.keys()].find((k) => k.startsWith("kognit-shell-"));
    assert(shell && caches.stores.get(shell).has("/") && caches.stores.get(shell).has("/static/js/notes.js"));
    const cdn = [...caches.stores.keys()].find((k) => k.startsWith("kognit-cdn-"));
    assert(caches.stores.get(cdn).has("https://cdn.jsdelivr.net/npm/marked/marked.min.js"));
    assert(swSelf.skippedWaiting);
});

await check("install FAILS if a core asset cannot be cached (no half-working offline shell)", async () => {
    const { handlers } = loadSW({ network: async (r) => (/app\.js/.test(r.url || r) ? basic("x", 404) : allOk(r)) });
    let rejected = false;
    try { await waitUntil(handlers.install); } catch (e) { rejected = true; }
    assert(rejected, "install must reject");
});

await check("install still succeeds when a CDN is unreachable; status then reports NOT ready", async () => {
    const { handlers, caches } = loadSW({ network: async (r) => { const u = r.url || r; if (/jsdelivr/.test(u)) throw new TypeError("cdn down"); return allOk(r); } });
    await waitUntil(handlers.install);
    let reply = null;
    await waitUntil(handlers.message, { data: { type: "GET_STATUS" }, ports: [{ postMessage: (m) => { reply = m; } }] });
    eq(reply.ready, false); assert(reply.requiredCdnMissing > 0);
});

await check("GET_STATUS reports ready only when every core asset and required CDN library is cached", async () => {
    const { handlers } = loadSW({ network: async (r) => allOk(r) });
    await waitUntil(handlers.install);
    let reply = null;
    await waitUntil(handlers.message, { data: { type: "GET_STATUS" }, ports: [{ postMessage: (m) => { reply = m; } }] });
    eq(reply.ready, true); eq(reply.coreMissing, 0); eq(reply.requiredCdnMissing, 0); eq(reply.mathReady, true);
});

await check("GET_STATUS ignores unrelated messages", async () => {
    const { handlers } = loadSW({ network: async (r) => allOk(r) });
    let called = false;
    handlers.message({ data: { type: "SOMETHING_ELSE" }, ports: [{ postMessage: () => { called = true; } }], waitUntil() {} });
    handlers.message({ data: null, ports: [], waitUntil() {} });
    assert(!called);
});

await check("activate deletes OLD kognit caches but leaves caches it does not own", async () => {
    const { handlers, caches } = loadSW({ network: async (r) => allOk(r) });
    await waitUntil(handlers.install);
    await caches.open("kognit-shell-OLD"); await caches.open("kognit-cdn-OLD"); await caches.open("someone-elses-cache");
    await waitUntil(handlers.activate);
    const names = [...caches.stores.keys()];
    assert(!names.includes("kognit-shell-OLD") && !names.includes("kognit-cdn-OLD"), "old caches removed");
    assert(names.includes("someone-elses-cache"), "foreign cache preserved");
    assert(names.some((n) => n.startsWith("kognit-shell-") && n !== "kognit-shell-OLD"), "current caches kept");
});

await check("NEVER intercepts /api/*, non-GET requests, /sw.js, Supabase or any unknown origin", async () => {
    const { handlers } = loadSW({ network: async (r) => allOk(r) });
    const passthrough = [
        fetchEvent("https://kognit.test/api/notes"),
        fetchEvent("https://kognit.test/api/notes/123/flashcards", { method: "POST" }),
        fetchEvent("https://kognit.test/api/chat", { method: "POST" }),
        fetchEvent("https://kognit.test/static/js/app.js", { method: "POST" }),
        fetchEvent("https://kognit.test/sw.js"),
        fetchEvent("https://ywxsxhzlienovbbakvcg.supabase.co/rest/v1/user_projects"),
        fetchEvent("https://www.gstatic.com/firebasejs/ui/2.0.0/images/auth/google.svg"),
        fetchEvent("https://kognit.test/some/other/path"),
    ];
    for (const fe of passthrough) { handlers.fetch(fe.event); assert(fe.result() === null, "must not respondWith: " + fe.event.request.url); }
});

await check("authenticated API responses are never written to any cache", async () => {
    const { handlers, caches } = loadSW({ network: async (r) => allOk(r) });
    await waitUntil(handlers.install);
    const snapshot = JSON.stringify([...caches.stores].map(([n, m]) => [n, [...m.keys()]]));
    handlers.fetch(fetchEvent("https://kognit.test/api/notes").event);
    handlers.fetch(fetchEvent("https://kognit.test/api/notes?limit=50").event);
    await sleep(10);
    eq(JSON.stringify([...caches.stores].map(([n, m]) => [n, [...m.keys()]])), snapshot);
    for (const m of caches.stores.values()) for (const k of m.keys()) assert(!/\/api\//.test(k), "api URL cached: " + k);
});

await check("navigation is NETWORK-FIRST: fresh when online, cached shell when offline", async () => {
    let online = true;
    const { handlers } = loadSW({ network: async (r) => { if (!online) throw new TypeError("offline"); return basic("html:" + (r.url || r)); } });
    await waitUntil(handlers.install);
    let fe = fetchEvent("https://kognit.test/?code=abc", { mode: "navigate" });
    handlers.fetch(fe.event);
    eq(await (await fe.result()).text(), "html:https://kognit.test/?code=abc", "online => network response");
    online = false;
    fe = fetchEvent("https://kognit.test/", { mode: "navigate" });
    handlers.fetch(fe.event);
    assert((await (await fe.result()).text()).startsWith("html:"), "offline => cached shell served");
});

await check("static files are network-first with cache fallback; an error response never replaces the cached copy", async () => {
    let mode = "ok";
    const { handlers } = loadSW({ network: async (r) => { const u = r.url || r; if (mode === "down") throw new TypeError("offline"); if (mode === "500") return basic("boom", 500); return basic("v1:" + u); } });
    await waitUntil(handlers.install);
    let fe = fetchEvent("https://kognit.test/static/js/app.js"); handlers.fetch(fe.event); await fe.result();
    mode = "500";
    fe = fetchEvent("https://kognit.test/static/js/app.js"); handlers.fetch(fe.event);
    eq((await fe.result()).status, 500, "online error is passed through");
    mode = "down";
    fe = fetchEvent("https://kognit.test/static/js/app.js"); handlers.fetch(fe.event);
    eq(await (await fe.result()).text(), "v1:https://kognit.test/static/js/app.js", "offline => last GOOD copy, not the 500");
});

await check("offline with nothing cached for a file: the request fails normally (no fabricated response)", async () => {
    const { handlers } = loadSW({ network: async () => { throw new TypeError("offline"); } });
    const fe = fetchEvent("https://kognit.test/static/js/never-cached.js"); handlers.fetch(fe.event);
    let failed2 = false; try { await fe.result(); } catch (e) { failed2 = true; }
    assert(failed2);
});

await check("CDN libraries: cached copy served instantly, refreshed in the background; CORS re-request so only verified-ok responses are stored", async () => {
    const seen = [];
    const { handlers, caches } = loadSW({ network: async (r, init) => { seen.push({ url: r.url || r, mode: r.mode }); return basic("lib:" + (r.url || r)); } });
    await waitUntil(handlers.install);
    seen.length = 0;
    let fe = fetchEvent("https://cdn.jsdelivr.net/npm/marked/marked.min.js"); let waited;
    fe.event.waitUntil = (p) => { waited = p; };
    handlers.fetch(fe.event);
    assert((await (await fe.result()).text()).startsWith("lib:"));
    await waited;
    assert(seen.length === 1 && seen[0].mode === "cors", "background refresh uses cors mode");
    // a broken CDN response must not poison the cache
    const { handlers: h2, caches: c2 } = loadSW({ network: async (r) => basic("404 page", 404) });
    const fe2 = fetchEvent("https://cdn.jsdelivr.net/npm/some/new.js"); h2.fetch(fe2.event);
    eq((await fe2.result()).status, 404);
    const cdn = [...c2.stores.keys()].find((k) => k.startsWith("kognit-cdn-"));
    assert(!cdn || !c2.stores.get(cdn).has("https://cdn.jsdelivr.net/npm/some/new.js"), "404 must not be cached");
});

await check("only '/' is treated as the app shell: other pages (e.g. /docs) pass through and can never be cached as the shell", async () => {
    const { handlers, caches } = loadSW({ network: async (r) => basic("page:" + (r.url || r)) });
    await waitUntil(handlers.install);
    const shellName = [...caches.stores.keys()].find((k) => k.startsWith("kognit-shell-"));
    const before = caches.stores.get(shellName).get("/") && await caches.stores.get(shellName).get("/").clone().text();
    for (const url of ["https://kognit.test/docs", "https://kognit.test/openapi.json", "https://kognit.test/anything"]) {
        const fe = fetchEvent(url, { mode: "navigate" });
        handlers.fetch(fe.event);
        assert(fe.result() === null, "must pass through: " + url);
    }
    await sleep(10);
    eq(await caches.stores.get(shellName).get("/").clone().text(), before, "shell entry untouched");
});

await check("the cache names are versioned with SW_VERSION", () => {
    assert(/const SHELL_CACHE = "kognit-shell-" \+ SW_VERSION;/.test(SRC_SW) && /const CDN_CACHE = "kognit-cdn-" \+ SW_VERSION;/.test(SRC_SW));
    assert(/const SW_VERSION = "[^"]+";/.test(SRC_SW));
});

/* ============================================================ */
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) {
    console.log("\nFailures:");
    for (const f of failures) console.log(` - ${f.name}\n   ${f.e.stack.split("\n").slice(0, 3).join("\n   ")}`);
    process.exit(1);
}
