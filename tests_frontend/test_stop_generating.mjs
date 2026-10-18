/* ============================================================
   KOGNIT - STOP GENERATING + COMPOSER DISCLAIMER (jsdom)
   ============================================================
   Runs the REAL templates/index.html, REAL static/js/app.js,
   stream-render.js, notes-store.js, notes.js, nav-views.js, the real
   vendored DOMPurify and the real `marked` package in jsdom. Stubbed:
   Supabase and fetch.

   The fake /api/chat/stream is a controllable stream that behaves the way a
   browser's fetch does with an AbortSignal:
     * fetch() rejects with AbortError if the signal fires before the headers,
     * a pending reader.read() rejects with AbortError once the signal fires.
   so these tests prove the request is actually aborted - not merely that a
   spinner disappears.

   WHAT THIS FILE CANNOT PROVE: that a REAL browser closes the real socket and
   that the real CSS lays the disclaimer out correctly. Those are covered by
   test_stop_generating_browser.mjs (real Chrome against a real HTTP server).

   Run: node tests_frontend/test_stop_generating.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
const SHELL_CSS = read("static/css/shell.css");

/* ------------------------------------------------------------
   Fake streaming backend (AbortSignal-aware)
   ------------------------------------------------------------ */
function makeBackend(w) {
    const backend = { sessions: [], calls: [], nextOptions: [] };
    const enc = new TextEncoder();
    const abortError = () => { const e = new w.Error("The operation was aborted."); e.name = "AbortError"; return e; };
    const jsonResponse = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

    backend.fetch = (url, opts = {}) => {
        const method = (opts.method || "GET").toUpperCase();
        backend.calls.push({ url, method, signal: opts.signal, headers: opts.headers || {} });

        // A returning student with a saved profile: without this the first-run onboarding modal opens and
        // legitimately takes focus, which has nothing to do with what is being tested here.
        if (url === "/api/profile" && method === "GET") {
            return Promise.resolve(jsonResponse(200, { profile: { name: "Tester", user_class: "Class 9-10 (SSC)", stream: "Science" } }));
        }
        if (url !== "/api/chat/stream") return Promise.resolve(jsonResponse(404, { detail: "nope" }));

        const options = backend.nextOptions.shift() || {};
        const signal = opts.signal;
        const queue = [];
        let waiter = null, closed = false, failure = null, resolveHeaders, rejectHeaders;
        const sess = {
            url, aborted: false, abortEvents: 0, cancelled: false, deliverAfterAbort: !!options.deliverAfterAbort,
            readsStarted: 0,
        };
        const wake = () => { if (waiter) { const f = waiter; waiter = null; f(); } };

        const headersPromise = new Promise((res, rej) => { resolveHeaders = res; rejectHeaders = rej; });
        if (signal && signal.aborted) {
            sess.aborted = true; sess.abortEvents++;
            rejectHeaders(abortError());
        } else if (signal) {
            signal.addEventListener("abort", () => {
                sess.aborted = true; sess.abortEvents++;
                rejectHeaders(abortError());   // no-op once headers have been delivered
                wake();
            });
        }

        const reader = {
            read() {
                sess.readsStarted++;
                return new Promise((resolve, reject) => {
                    const attempt = () => {
                        if (sess.aborted && !(sess.deliverAfterAbort && queue.length)) return reject(abortError());
                        if (queue.length) return resolve({ value: queue.shift(), done: false });
                        if (failure) return reject(failure);
                        if (closed) return resolve({ value: undefined, done: true });
                        waiter = attempt;
                    };
                    attempt();
                });
            },
            cancel() { sess.cancelled = true; return Promise.resolve(); },
        };
        const response = { ok: true, status: options.status || 200, body: { getReader: () => reader }, json: async () => ({ reply: "x" }) };

        sess.respond = () => resolveHeaders(response);        // deliver the response headers
        sess.push = (obj) => { queue.push(enc.encode(JSON.stringify(obj) + "\n")); wake(); };
        sess.close = () => { closed = true; wake(); };
        sess.fail = (err) => { failure = err; wake(); };
        if (!options.holdHeaders) sess.respond();
        backend.sessions.push(sess);
        return headersPromise;
    };
    return backend;
}

async function boot() {
    const html = TEMPLATE.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kognit.test/" });
    const w = dom.window;
    w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    w.TextEncoder = TextEncoder; w.TextDecoder = TextDecoder; w.MessageChannel = MessageChannel;
    w.alert = () => {}; w.confirm = () => true;
    Object.defineProperty(w.navigator, "onLine", { get: () => true, configurable: true });
    w.indexedDB = undefined;

    const backend = makeBackend(w);
    w.fetch = backend.fetch;

    const env = { session: { access_token: "tok", user: { id: "user-1" } }, loginPrompts: 0 };
    const query = { select: () => query, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => query, eq: async () => ({ error: null }) };
    w.supabase = { createClient: () => ({
        auth: {
            getSession: async () => ({ data: { session: env.session } }),
            onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
            signOut: async () => ({}),
        },
        from: () => query,
    }) };
    w.eval(MARKED_SRC);
    w.Element.prototype.getClientRects = function () { return this.isConnected ? [{}] : []; };
    w.MathJax = Object.assign(w.MathJax || {}, { typesetPromise: async () => {}, typesetClear: () => {}, startup: { promise: Promise.resolve() } });

    const add = (src) => { const s = w.document.createElement("script"); s.textContent = src; w.document.body.appendChild(s); };
    add(SRC.stream); add(SRC.purify); add(SRC.app);
    w.openAuthModal = () => { env.loginPrompts++; };
    add(SRC.store); add(SRC.notes); add(SRC.nav);
    w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
    await sleep(60);

    const doc = w.document;
    const api = {
        w, doc, env, backend,
        input: doc.getElementById("user-input"),
        sendBtn: doc.getElementById("send-btn"),
        stopBtn: doc.getElementById("stop-btn"),
        chatBox: doc.getElementById("chat-box"),
        chats: null,
    };
    api.isHidden = (el) => el.classList.contains("hidden");
    api.seed = (n = 1) => {
        const projects = w.getProjectsSnapshot();
        projects.length = 0;
        const chats = [];
        for (let i = 1; i <= n; i++) chats.push({ id: `c${i}`, title: `Chat ${i}`, messages: [] });
        projects.push({ id: "p1", title: "Physics", chats });
        api.chats = chats;
        w.loadChat("p1", "c1");
        return chats;
    };
    api.type = (text) => { api.input.value = text; };
    // sendMessage() only settles when the whole response has finished, so it must NOT be awaited here.
    api.pending = [];
    api.send = async (text) => { api.type(text); api.pending.push(w.sendMessage()); await sleep(30); };
    api.botBubbles = () => [...api.chatBox.querySelectorAll(".bot-message")].filter((b) => !b.classList.contains("streaming-status"));
    api.lastBotText = () => { const b = api.botBubbles().pop(); return b ? b.textContent : ""; };
    api.session = (i = api.backend.sessions.length - 1) => api.backend.sessions[i];
    api.streamCalls = () => api.backend.calls.filter((c) => c.url === "/api/chat/stream");
    api.stream = async (sess, ...texts) => {
        sess.push({ type: "start", context: "thinking" });
        for (const t of texts) sess.push({ type: "delta", text: t });
        await sleep(60);   // let the rAF paint run
    };
    api.complete = async (sess, reply) => { sess.push({ type: "done", reply, research: null }); sess.close(); await sleep(40); };
    api.state = () => ({ send: !api.isHidden(api.sendBtn), stop: !api.isHidden(api.stopBtn) });
    return api;
}

/* =============================================================
   A. Pure helper: stripIncompleteTakeaway
   ============================================================= */
console.log("\nA. stripIncompleteTakeaway (pure helper)");
{
    const h = await boot();
    const strip = h.w.KognitStream.stripIncompleteTakeaway;
    const FULL = "<!--KOGNIT_TAKEAWAY\nForce depends on mass.\nKOGNIT_TAKEAWAY-->";

    await check("text without any marker is returned byte-for-byte unchanged", () => {
        for (const t of ["Plain answer.", "Line1\n\nLine2  \n", "a < b and c <! d", "x <", "$$F=ma$$", "<!-- ordinary comment -->"])
            eq(strip(t), t, JSON.stringify(t));
    });
    await check("an opened-but-unclosed marker is cut from its start, and trailing whitespace trimmed", () => {
        eq(strip("Answer.\n\n<!--KOGNIT_TAKEAWAY\nForce dep"), "Answer.");
        eq(strip("Answer.<!--KOGNIT_TAKEAWAY"), "Answer.");
        eq(strip("Answer.\n<!--KOGNIT_TAKEAWAY\nbody KOGNIT_TAKE"), "Answer.");
    });
    await check("a COMPLETE marker pair is never touched (extractTakeaway owns it)", () => {
        eq(strip("Answer.\n" + FULL), "Answer.\n" + FULL);
        const ex = h.w.KognitStream.extractTakeaway(strip("Answer.\n" + FULL));
        eq(ex.takeaway, "Force depends on mass."); eq(ex.answer, "Answer.");
    });
    await check("only the dangling second opener is cut when a complete pair precedes it", () => {
        eq(strip("A.\n" + FULL + "\nB.\n<!--KOGNIT_TAKEAWAY\nhalf"), "A.\n" + FULL + "\nB.");
    });
    await check("a trailing prefix of the opener ('<!', '<!--', '<!--KOGN') is removed; a lone '<' is not", () => {
        eq(strip("Answer. <!"), "Answer.");
        eq(strip("Answer. <!--"), "Answer.");
        eq(strip("Answer. <!--KOGN"), "Answer.");
        eq(strip("Answer. <!--KOGNIT_TAKEAWA"), "Answer.");
        eq(strip("Answer. <"), "Answer. <");
    });
    await check("empty / non-string input yields an empty string, never throws", () => {
        eq(strip(""), ""); eq(strip(null), ""); eq(strip(undefined), ""); eq(strip(42), "");
    });
}

/* =============================================================
   B. Markup, accessibility, disclaimer
   ============================================================= */
console.log("\nB. Markup / accessibility / disclaimer");
{
    const h = await boot();
    const doc = h.doc;
    const DISCLAIMER = "Kloriz can make mistakes. Check important information.";

    await check("idle state: Send visible and enabled, Stop hidden", () => {
        eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
        assert(!h.sendBtn.disabled);
    });
    await check("Stop is a real <button type=button> with the accessible name 'Stop generating' and is NOT inside a form", () => {
        eq(h.stopBtn.tagName, "BUTTON"); eq(h.stopBtn.type, "button");
        eq(h.stopBtn.getAttribute("aria-label"), "Stop generating");
        eq(h.stopBtn.title, "Stop generating");
        eq(h.stopBtn.closest("form"), null, "no <form>: Stop can never submit the input");
        eq(h.stopBtn.getAttribute("onclick"), "stopGeneration()");
    });
    await check("Stop sits in the composer row right after Send (same grid slot, far right) and shows a square icon", () => {
        eq(h.sendBtn.nextElementSibling, h.stopBtn);
        eq(h.sendBtn.parentElement, h.stopBtn.parentElement);
        assert(h.sendBtn.closest(".input-container"), "inside the composer");
        const svg = h.stopBtn.querySelector("svg");
        assert(svg && svg.getAttribute("aria-hidden") === "true", "decorative icon hidden from AT");
        const rect = svg.querySelector("rect");
        assert(rect && rect.getAttribute("width") === rect.getAttribute("height"), "square glyph");
    });
    await check("CSS gives Stop the same grid slot, size and mobile size as Send, plus a focus ring", () => {
        assert(/#send-btn,\s*#stop-btn\s*\{[^}]*grid-area:\s*send;/.test(SHELL_CSS), "shared grid-area rule");
        assert(/#stop-btn:focus-visible\s*\{[^}]*--focus-ring/.test(SHELL_CSS), "focus ring");
        assert(/\.composer-plus-btn,\s*#send-btn,\s*#stop-btn\s*\{\s*width:\s*44px/.test(SHELL_CSS), "44px touch target on mobile");
    });

    await check("the disclaimer text is exactly right and appears exactly once", () => {
        const all = [...doc.querySelectorAll(".composer-disclaimer")];
        eq(all.length, 1);
        eq(all[0].textContent, DISCLAIMER);
        eq(doc.body.textContent.split(DISCLAIMER).length - 1, 1, "once in the whole page");
    });
    await check("it sits directly below the composer, outside it, as plain text (no icon/card/button)", () => {
        const d = doc.querySelector(".composer-disclaimer");
        eq(d.previousElementSibling, doc.querySelector(".input-container"));
        eq(d.closest(".input-container"), null, "not inside the composer, so it cannot overlap its controls");
        eq(d.children.length, 0, "no child elements/icons");
        eq(d.tagName, "P");
        assert(d.closest("#chat-view"), "belongs to the chat view only");
    });
    await check("CSS: small, muted token colour, centred, no background/border/animation; narrower margins on mobile", () => {
        const rule = SHELL_CSS.match(/\.composer-disclaimer\s*\{([^}]*)\}/);
        assert(rule, "rule missing");
        const css = rule[1];
        assert(/font-size:\s*(1[0-3])px/.test(css), "small font");
        assert(/color:\s*var\(--color-text-muted\)/.test(css), "muted token (theme-aware, not a hard-coded colour)");
        assert(/text-align:\s*center/.test(css));
        assert(!/background|border|animation|transition|box-shadow/.test(css), "no decoration");
        assert(!/#[0-9a-f]{3,6}\b|rgb\(/i.test(css), "no hard-coded colour that would break a theme");
        assert(/@media[^{]*max-width:\s*768px[^{]*\{[\s\S]*?\.composer-disclaimer\s*\{[^}]*calc\(100%\s*-\s*24px\)/.test(SHELL_CSS), "mobile width");
    });
}

/* =============================================================
   C. Stop behaviour
   ============================================================= */
console.log("\nC. Stop generating - behaviour");

await check("while generating: Stop replaces Send; on normal completion Send returns and the answer is saved as a normal one", async () => {
    const h = await boot(); h.seed();
    await h.send("What is force?");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "Stop shown during generation");
    assert(!h.stopBtn.disabled, "Stop is enabled");
    await h.stream(h.session(), "Force is ", "a push or pull.");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "still generating");
    await h.complete(h.session(), "Force is a push or pull.");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }), "Send restored");
    const saved = h.chats[0].messages;
    eq(saved.length, 2); eq(saved[1].role, "bot"); eq(saved[1].text, "Force is a push or pull.");
    assert(!("stopped" in saved[1]) && !("interrupted" in saved[1]), "a normal completion carries no stop/interrupt flag");
    eq(h.session().aborted, false, "a normal completion never aborts the request");
    assert(!/stopped|interrupted/i.test(h.lastBotText()), "no notice");
});

await check("Stop mid-stream: the request is ABORTED, the partial answer stays, and Send is restored", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain Newton's laws");
    await h.stream(h.session(), "Newton's first law says ", "objects keep moving");
    assert(h.lastBotText().includes("Newton's first law says objects keep moving"), "text is on screen before Stop");

    h.stopBtn.click();
    await sleep(60);

    const s = h.session();
    eq(s.aborted, true, "AbortSignal fired - the underlying fetch was cancelled");
    eq(s.abortEvents, 1);
    eq(s.cancelled, true, "reader released");
    assert(h.lastBotText().includes("Newton's first law says objects keep moving"), "partial text preserved on screen");
    assert(/Generation stopped\. This answer is incomplete\./.test(h.lastBotText()), "honest stopped notice");
    assert(!/Error connecting|interrupted before it finished/i.test(h.chatBox.textContent), "an intentional stop is not reported as an error");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }), "composer restored");
    assert(!h.sendBtn.disabled && !h.input.disabled, "can type and send again");
    eq(h.chatBox.querySelectorAll(".streaming-status").length, 0, "no loading line left behind");
    eq(h.streamCalls().length, 1, "no fallback /api/chat retry fired");
    eq(h.backend.calls.filter((c) => c.url === "/api/chat").length, 0);
});

await check("the stopped partial is SAVED (flagged stopped+interrupted) and survives re-opening the chat", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain Newton's laws");
    await h.stream(h.session(), "Partial answer text");
    h.stopBtn.click(); await sleep(60);

    const m = h.chats[0].messages;
    eq(m.length, 2, "user message + stopped bot message, in order");
    eq(m[0].role, "user"); eq(m[1].role, "bot");
    eq(m[1].text, "Partial answer text");
    eq(m[1].stopped, true); eq(m[1].interrupted, true);

    h.w.loadChat("p1", "c1");                       // e.g. after switching away and back / reload
    assert(h.lastBotText().includes("Partial answer text"));
    assert(/Generation stopped\./.test(h.lastBotText()), "notice is rebuilt from the saved flag");
});

await check("a stopped answer is never offered as a finished one: no 'Save to Pocket Notes' on it, but still has Copy", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain Newton's laws");
    await h.stream(h.session(), "Partial answer text");
    h.stopBtn.click(); await sleep(60);
    const tb = [...h.chatBox.querySelectorAll(".bot-message")].pop().querySelector(".bot-toolbar");
    assert(tb, "toolbar present");
    assert(!tb.querySelector(".toolbar-btn-save"), "no Save button on an incomplete answer");
    assert(/Copy/.test(tb.textContent), "Copy still available");
});

await check("Stop right after sending (response headers not received yet): aborts quietly, nothing saved, no error", async () => {
    const h = await boot(); h.seed();
    h.backend.nextOptions.push({ holdHeaders: true });
    await h.send("Quick question");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "Stop is available immediately");
    h.stopBtn.click(); await sleep(60);
    eq(h.session().aborted, true, "fetch aborted before any response");
    eq(h.chats[0].messages.length, 1, "only the user's message; no bot message invented");
    assert(!/Error connecting/i.test(h.chatBox.textContent), "no misleading network error");
    eq(h.chatBox.querySelectorAll(".streaming-status").length, 0, "loading line removed");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("Stop after the stream opened but before any text: nothing saved, nothing rendered, no error", async () => {
    const h = await boot(); h.seed();
    await h.send("Quick question");
    h.session().push({ type: "start", context: "thinking" }); await sleep(30);
    h.stopBtn.click(); await sleep(60);
    eq(h.session().aborted, true);
    eq(h.chats[0].messages.length, 1);
    eq(h.botBubbles().length, 0, "no empty bubble, no notice");
    assert(!/Error connecting/i.test(h.chatBox.textContent));
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("repeated Stop clicks (button + direct calls) abort exactly once and never throw", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain Newton's laws");
    await h.stream(h.session(), "Some text");
    h.stopBtn.click(); h.stopBtn.click(); h.w.stopGeneration(); h.w.stopGeneration(); h.stopBtn.click();
    await sleep(60);
    eq(h.session().abortEvents, 1, "one abort");
    eq(h.chats[0].messages.length, 2, "one saved bot message, not several");
    h.w.stopGeneration();                             // after everything finished: harmless no-op
    eq(h.chats[0].messages.length, 2);
});

await check("Stop with nothing running is a harmless no-op", async () => {
    const h = await boot(); h.seed();
    h.w.stopGeneration();
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
    eq(h.backend.calls.filter((c) => c.url === "/api/chat/stream").length, 0);
});

await check("chunks and a 'done' event that arrive AFTER Stop are ignored (late events cannot change the kept answer)", async () => {
    const h = await boot(); h.seed();
    h.backend.nextOptions.push({ deliverAfterAbort: true });          // worst case: the stream still hands us queued data
    await h.send("Explain Newton's laws");
    await h.stream(h.session(), "Kept text.");
    h.stopBtn.click();
    h.session().push({ type: "delta", text: " LATE-CHUNK" });
    h.session().push({ type: "done", reply: "Kept text. LATE-CHUNK and the full reply", research: null });
    h.session().close();
    await sleep(80);
    const bot = h.chats[0].messages[1];
    eq(bot.text, "Kept text.");
    eq(bot.stopped, true);
    assert(!h.chatBox.textContent.includes("LATE-CHUNK"), "late data never rendered");
    eq(h.chats[0].messages.length, 2);
});

await check("a new question after Stop works normally, in order, and the stopped answer is untouched", async () => {
    const h = await boot(); h.seed();
    await h.send("First question");
    await h.stream(h.session(), "Half an ans");
    h.stopBtn.click(); await sleep(60);

    await h.send("Second question");
    eq(h.backend.sessions.length, 2, "a brand-new request");
    eq(h.session(1).aborted, false);
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "Stop appears again for the new request");
    await h.stream(h.session(1), "Full ", "answer.");
    await h.complete(h.session(1), "Full answer.");

    const m = h.chats[0].messages;
    eq(m.map((x) => x.role).join(","), "user,bot,user,bot", "message order intact");
    eq(m[1].text, "Half an ans"); eq(m[1].stopped, true);
    eq(m[3].text, "Full answer."); assert(!("stopped" in m[3]));
    eq(h.session(0).abortEvents, 1, "the first request was aborted once and only once");
    eq(h.session(1).abortEvents, 0, "the second request was never aborted by the first one's Stop");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("Stop never submits the text typed in the input, and keeps it for the next message", async () => {
    const h = await boot(); h.seed();
    await h.send("First question");
    await h.stream(h.session(), "text");
    h.type("my next question, half typed");
    h.stopBtn.click(); await sleep(60);
    eq(h.streamCalls().length, 1, "no second request was started");
    eq(h.input.value, "my next question, half typed", "typed text preserved");
    eq(h.chats[0].messages.filter((m) => m.role === "user").length, 1);
});

await check("Enter while a response is generating does NOT start a second request; the typed text is kept", async () => {
    const h = await boot(); h.seed();
    await h.send("First question");
    await h.stream(h.session(), "text");
    h.type("follow-up");
    h.input.dispatchEvent(new h.w.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await sleep(40);
    eq(h.streamCalls().length, 1, "still exactly one request");
    eq(h.input.value, "follow-up");
    eq(h.chats[0].messages.filter((m) => m.role === "user").length, 1, "no duplicate user message");
});

await check("Enter-to-send and the Send button still work when idle (regression)", async () => {
    const h = await boot(); h.seed();
    h.type("via enter");
    h.input.dispatchEvent(new h.w.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await sleep(40);
    eq(h.streamCalls().length, 1, "Enter sent");
    await h.complete(h.session(), "ok");
    h.type("via button");
    h.sendBtn.click(); await sleep(40);
    eq(h.streamCalls().length, 2, "Send button sent");
    eq(h.input.value, "", "input cleared on send, exactly as before");
});

await check("Shift+Enter still only inserts a newline (does not send)", async () => {
    const h = await boot(); h.seed();
    h.type("line");
    h.input.dispatchEvent(new h.w.KeyboardEvent("keydown", { key: "Enter", shiftKey: true, bubbles: true, cancelable: true }));
    await sleep(30);
    eq(h.streamCalls().length, 0);
});

await check("an empty message is still ignored (and registers no generation)", async () => {
    const h = await boot(); h.seed();
    h.type("   ");
    await h.w.sendMessage();
    eq(h.streamCalls().length, 0);
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("a cut inside the Key Takeaway marker never leaks the raw fragment into the kept answer", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain");
    await h.stream(h.session(), "Main answer.\n\n<!--KOGNIT_TAKEAWAY\nForce dep");
    h.stopBtn.click(); await sleep(60);
    const bot = h.chats[0].messages[1];
    eq(bot.text, "Main answer.");
    assert(!/KOGNIT_TAKEAWAY|<!--/.test(bot.text), "stored text is clean");
    assert(!/KOGNIT_TAKEAWAY/.test(h.chatBox.textContent), "and so is the screen");
    eq(bot.takeaway, null);
});

await check("a COMPLETE takeaway received before Stop is still extracted into the Key takeaway card", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain");
    await h.stream(h.session(), "Main answer.\n<!--KOGNIT_TAKEAWAY\nMass matters.\nKOGNIT_TAKEAWAY-->");
    h.stopBtn.click(); await sleep(60);
    const bot = h.chats[0].messages[1];
    eq(bot.takeaway, "Mass matters.");
    eq(bot.text.trim(), "Main answer.");
    assert(h.chatBox.querySelector(".answer-takeaway"), "card rendered");
});

await check("Stop during a recovery 'retry' (the failed partial was already discarded): nothing is kept or saved", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain");
    await h.stream(h.session(), "doomed partial");
    h.session().push({ type: "retry" }); await sleep(40);
    h.stopBtn.click(); await sleep(60);
    eq(h.chats[0].messages.length, 1, "the discarded partial is not resurrected");
    assert(!h.chatBox.textContent.includes("doomed partial"));
});

await check("a terminal event that already arrived wins over a late Stop: 'done' is saved as a normal complete answer", async () => {
    const h = await boot(); h.seed();
    await h.send("Explain");
    await h.stream(h.session(), "Finished text.");
    h.session().push({ type: "done", reply: "Finished text.", research: null });
    await sleep(20);                                  // done is processed, stream not yet closed
    h.stopBtn.click(); await sleep(60);
    const bot = h.chats[0].messages[1];
    eq(bot.text, "Finished text.");
    assert(!("stopped" in bot) && !("interrupted" in bot), "completed answers are not relabelled as stopped");
});

/* ---- genuine failures keep their existing behaviour ---- */
console.log("\nD. Genuine failures are NOT treated as 'stopped' (regression)");

await check("network failure before any response still shows the existing error and restores Send", async () => {
    const h = await boot(); h.seed();
    h.w.fetch = () => Promise.reject(new h.w.TypeError("Failed to fetch"));
    await h.send("Hello");
    assert(/Error connecting to Kloriz Engine\./.test(h.chatBox.textContent));
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("a stream that errors mid-read (no Stop pressed) still shows the existing error and restores Send", async () => {
    const h = await boot(); h.seed();
    await h.send("Hello");
    await h.stream(h.session(), "partial");
    h.session().fail(new h.w.TypeError("network error"));
    await sleep(60);
    assert(/Error connecting to Kloriz Engine\./.test(h.chatBox.textContent), "genuine errors are still surfaced");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
    eq(h.session().aborted, false);
});

await check("a dropped connection with partial text keeps its EXISTING 'interrupted' behaviour (not relabelled as stopped)", async () => {
    const h = await boot(); h.seed();
    await h.send("Hello");
    await h.stream(h.session(), "partial text");
    h.session().close();                               // closes with no terminal event
    await sleep(60);
    assert(/This answer was interrupted before it finished generating\./.test(h.chatBox.textContent));
    assert(!/Generation stopped/.test(h.chatBox.textContent));
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("a server 'error' terminal shows its message and restores Send", async () => {
    const h = await boot(); h.seed();
    await h.send("Hello");
    h.session().push({ type: "error", reply: "Kognit is busy right now.", code: "PROVIDER_UNAVAILABLE" });
    h.session().close(); await sleep(60);
    assert(h.chatBox.textContent.includes("Kognit is busy right now."));
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("401 and 429 responses keep their existing handling and always release the composer", async () => {
    const h = await boot(); h.seed();
    h.backend.nextOptions.push({ status: 401 });
    await h.send("Hello");
    await sleep(40);
    eq(h.env.loginPrompts >= 0, true);
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }), "after 401");

    h.backend.nextOptions.push({ status: 429 });
    await h.send("Hello again");
    await sleep(40);
    assert(/sending messages too quickly/.test(h.chatBox.textContent), "429 message");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }), "after 429");
});

await check("not logged in: the existing login prompt is unchanged and no generation is registered", async () => {
    const h = await boot(); h.seed();
    h.env.session = null;
    h.w.eval("currentUser = null");
    h.type("hello");
    await h.w.sendMessage();
    eq(h.streamCalls().length, 0);
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

/* =============================================================
   E. Chat switching during generation
   ============================================================= */
console.log("\nE. Chat switching while generating");

await check("Send/Stop follows the OPEN chat: Stop only for the chat that is generating", async () => {
    const h = await boot(); h.seed(2);
    await h.send("question in chat 1");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "c1 generating");
    h.w.loadChat("p1", "c2");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }), "c2 idle -> Send");
    h.w.loadChat("p1", "c1");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "back in c1 -> Stop");
});

await check("answer text never paints into a DIFFERENT chat's view, and re-attaches when you come back", async () => {
    const h = await boot(); h.seed(2);
    await h.send("question in chat 1");
    await h.stream(h.session(), "Hello from chat one");
    assert(h.lastBotText().includes("Hello from chat one"));

    h.w.loadChat("p1", "c2");                           // switch away mid-answer
    h.session().push({ type: "delta", text: " AND MORE" }); await sleep(60);
    assert(!h.chatBox.textContent.includes("Hello from chat one"), "c1's answer must not appear in c2's view");
    assert(!h.chatBox.textContent.includes("AND MORE"));

    h.w.loadChat("p1", "c1");                           // come back while it is still generating
    h.session().push({ type: "delta", text: " plus later text" }); await sleep(60);
    assert(h.lastBotText().includes("Hello from chat one AND MORE plus later text"), "live bubble re-attached with the full text so far");

    await h.complete(h.session(), "Hello from chat one AND MORE plus later text.");
    eq(h.botBubbles().length, 1, "exactly one final bubble, no duplicate left from the live one");
    eq(h.chats[0].messages[1].text, "Hello from chat one AND MORE plus later text.");
});

await check("a response that finishes while ANOTHER chat is open is saved into its own chat and not drawn in the open one", async () => {
    const h = await boot(); h.seed(2);
    await h.send("question in chat 1");
    await h.stream(h.session(), "partial");
    h.w.loadChat("p1", "c2");
    await h.complete(h.session(), "The full answer for chat 1.");
    eq(h.chats[0].messages[1].text, "The full answer for chat 1.", "saved into c1");
    eq(h.chats[1].messages.length, 0, "c2 untouched");
    assert(!h.chatBox.textContent.includes("full answer for chat 1"), "not rendered in c2");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
    h.w.loadChat("p1", "c1");
    assert(h.lastBotText().includes("The full answer for chat 1."), "present when reopened");
});

await check("Stop from another chat's composer stops THAT chat only: two chats generate independently", async () => {
    const h = await boot(); h.seed(2);
    await h.send("question in chat 1");
    await h.stream(h.session(0), "chat-one text");

    h.w.loadChat("p1", "c2");
    await h.send("question in chat 2");
    eq(h.backend.sessions.length, 2, "a second concurrent request in the other chat is allowed, as before");
    await h.stream(h.session(1), "chat-two text");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }));

    h.stopBtn.click(); await sleep(60);                 // stop chat 2
    eq(h.session(1).aborted, true, "chat 2's request aborted");
    eq(h.session(0).aborted, false, "chat 1's request untouched");
    eq(h.chats[1].messages[1].text, "chat-two text"); eq(h.chats[1].messages[1].stopped, true);
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }), "c2 composer restored");

    h.w.loadChat("p1", "c1");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "c1 still generating -> Stop");
    await h.complete(h.session(0), "chat-one text finished.");
    eq(h.chats[0].messages[1].text, "chat-one text finished.");
    assert(!("stopped" in h.chats[0].messages[1]), "c1 completed normally");
    eq(JSON.stringify(h.state()), JSON.stringify({ send: true, stop: false }));
});

await check("a finished request in one chat does not clear another chat's running generation (cleanup is per request)", async () => {
    const h = await boot(); h.seed(2);
    await h.send("question in chat 1");
    h.w.loadChat("p1", "c2");
    await h.send("question in chat 2");
    await h.complete(h.session(0), "c1 done");           // c1 finishes while c2 is still generating
    eq(JSON.stringify(h.state()), JSON.stringify({ send: false, stop: true }), "c2 still shows Stop");
    h.stopBtn.click(); await sleep(60);
    eq(h.session(1).aborted, true, "c2 can still be stopped");
});

/* ---- focus ---- */
console.log("\nF. Keyboard focus");

await check("focus is never dropped on <body>: pressing Send moves focus to the input, pressing Stop does too", async () => {
    const h = await boot(); h.seed();
    h.type("question");
    h.sendBtn.focus();
    eq(h.doc.activeElement, h.sendBtn);
    h.sendBtn.click(); await sleep(40);
    eq(h.doc.activeElement, h.input, "Send was hidden -> focus handed to the input");

    h.stopBtn.focus();
    h.stopBtn.click(); await sleep(60);
    eq(h.doc.activeElement, h.input, "Stop was hidden -> focus handed to the input");
});

await check("keyboard activation of the focused Stop control works like a click", async () => {
    const h = await boot(); h.seed();
    await h.send("question");
    await h.stream(h.session(), "text");
    h.stopBtn.focus();
    h.stopBtn.click();                                    // Enter/Space on a <button> dispatches click
    await sleep(60);
    eq(h.session().aborted, true);
});

/* ============================================================ */
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) {
    console.log("\nFailures:");
    for (const f of failures) console.log(` - ${f.name}\n   ${f.e.stack.split("\n").slice(0, 3).join("\n   ")}`);
    process.exit(1);
}
