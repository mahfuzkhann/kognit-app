/* Shared jsdom boot for the UI/UX tests: the REAL templates/index.html with the REAL
   shipped scripts, and only the network + Supabase faked. See test_uiux_improvements.mjs. */
import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");   // tests_frontend/
const ROOT = path.resolve(HERE, "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const MARKED_SRC = fs.readFileSync(path.join(HERE, "node_modules", "marked", "lib", "marked.umd.js"), "utf8");
const SRC = {
    stream: read("static/js/stream-render.js"), purify: read("static/js/vendor/purify.min.js"),
    feedback: read("static/js/feedback.js"), app: read("static/js/app.js"),
    store: read("static/js/notes-store.js"), notes: read("static/js/notes.js"), nav: read("static/js/nav-views.js"),
    uicore: read("static/js/ui-core.js"), shell: read("static/js/shell.js"), share: read("static/js/share.js"),
};
const TEMPLATE = read("templates/index.html");

const assert = (c, m) => { if (!c) throw new Error(m || "assertion failed"); };
const eq = (a, b, m) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------- fakes ---------- */
const R = (status, body, headers = {}) => ({
    ok: status >= 200 && status < 300, status,
    headers: { get: (k) => headers[k] ?? headers[String(k).toLowerCase()] ?? null },
    json: async () => body, clone() { return R(status, body, headers); }, text: async () => JSON.stringify(body),
});
const QUOTA = (over = {}) => ({ detail: { code: "QUOTA_EXCEEDED", kind: "chat", plan: "free", resets_at: "2026-11-03T00:00:00Z", reason: "quota_exceeded", message: "english", ...over } });

async function boot(opts = {}) {
    const html = TEMPLATE.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kognit.test/" });
    const w = dom.window;
    w.matchMedia = (q) => ({ matches: false, media: q, addEventListener() {}, removeEventListener() {} });
    w.TextEncoder = TextEncoder; w.TextDecoder = TextDecoder; w.MessageChannel = MessageChannel;
    const env = { alerts: 0, calls: [], session: opts.guest ? null : { access_token: "tok", user: { id: "user-1", email: "s@kloriz.test" } } };
    w.alert = () => { env.alerts++; }; w.confirm = () => true;
    Object.defineProperty(w.navigator, "onLine", { get: () => true, configurable: true });
    w.indexedDB = undefined;
    w.Element.prototype.scrollIntoView = function () {};

    // routing: tests set env.route = (url, init, method) => response
    env.route = opts.route || (() => R(404, { detail: "nope" }));
    w.fetch = (url, init = {}) => {
        const method = (init.method || "GET").toUpperCase();
        env.calls.push({ url, method, body: init.body, headers: init.headers || {} });
        if (url === "/api/profile" && method === "GET") return Promise.resolve(R(200, { profile: { name: "Tester", user_class: "Class 9-10 (SSC)", stream: "Science" } }));
        return Promise.resolve(env.route(url, init, method));
    };

    const query = { select: () => query, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => query, eq: async () => ({ error: null }), then: undefined };
    env.auth = {
        getSession: async () => ({ data: { session: env.session } }),
        onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
        signOut: async () => ({}),
        signInWithPassword: async () => ({ data: { user: { id: "user-1", email: "s@kloriz.test" }, session: {} }, error: null }),
        signUp: async () => ({ data: { user: { id: "u2", identities: [{}] }, session: null }, error: null }),
        signInWithOAuth: async () => ({ error: null }),
    };
    w.supabase = { createClient: () => ({ auth: env.auth, from: () => query }) };
    w.eval(MARKED_SRC);
    w.Element.prototype.getClientRects = function () { return this.isConnected ? [{}] : []; };
    w.MathJax = Object.assign(w.MathJax || {}, { typesetPromise: async () => {}, typesetClear: () => {}, startup: { promise: Promise.resolve() } });
    w.KlorizProfileMenu = { openAccount: (pane) => env.calls.push({ openAccount: pane }) };

    // Let jsdom finish parsing first. Otherwise ui-core.js (which wires itself immediately OR on
    // DOMContentLoaded) would be wired twice - once by the synthetic event below and once by the real
    // one - and a single Escape would be handled twice. A real page loads it exactly once.
    if (w.document.readyState === "loading") await new Promise((r) => w.document.addEventListener("DOMContentLoaded", r, { once: true }));
    const add = (src) => { const s = w.document.createElement("script"); s.textContent = src; w.document.body.appendChild(s); };
    add(SRC.stream); add(SRC.purify); add(SRC.feedback); add(SRC.app); add(SRC.store); add(SRC.notes); add(SRC.nav); add(SRC.uicore); add(SRC.shell); add(SRC.share);
    w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
    await sleep(80);

    const doc = w.document;
    const api = {
        w, doc, env,
        $: (id) => doc.getElementById(id),
        hidden: (id) => doc.getElementById(id).classList.contains("hidden"),
        chatBox: doc.getElementById("chat-box"), input: doc.getElementById("user-input"),
    };
    api.seed = (messages = []) => {
        const projects = w.getProjectsSnapshot(); projects.length = 0;
        projects.push({ id: "p1", title: "Physics", chats: [{ id: "c1", title: "Chat 1", messages }] });
        w.loadChat("p1", "c1");
        return projects[0].chats[0];
    };
    api.signIn = () => w.eval("currentUser = { id: 'user-1', email: 's@kloriz.test' }");
    api.key = (target, key, init = {}) => {
        const e = new w.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
        target.dispatchEvent(e); return e;
    };
    return api;
}


export { boot, R, QUOTA, sleep, assert, eq, ROOT, read, SRC, TEMPLATE, MARKED_SRC };
