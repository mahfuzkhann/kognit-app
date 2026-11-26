/* ============================================================
   KOGNIT - API CLIENT TESTS (api.js)
   ============================================================
   Loads the REAL static/js/api.js into an isolated Node vm context with a controllable
   fake fetch. No jsdom and no network are needed. The fake fetch behaves like a browser's:
   it rejects with AbortError when the request's signal fires.

   WHAT THIS FILE CANNOT PROVE: how a real WebView / Android Chrome handles CORS preflights
   and aborts. test_api_contract.py proves the headers api.js sends are inside the server's CORS
   allow-list; a real device test belongs to the Android batch.

   Run: node tests_frontend/test_api_client.mjs
   ============================================================ */

import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const SOURCE = fs.readFileSync(path.join(ROOT, "static/js/api.js"), "utf8");
const APP_CONFIG_FIXTURE = JSON.parse(fs.readFileSync(path.join(HERE, "fixtures/app_config.contract.json"), "utf8"));

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
async function rejects(promise, test, label) {
    let err = null;
    try { await promise; } catch (e) { err = e; }
    if (!err) throw new Error(`${label || "expected a rejection"}, but it resolved`);
    test(err);
    return err;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signaturesignature";
const JWT2 = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMiJ9.othersignature2";

/* ------------------------------------------------------------ harness */
function jsonResponse(status, body, headers = {}) {
    return new Response(body === undefined ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}

function load({ config, native = false, origin = "https://kloriz.example", online = true } = {}) {
    const calls = [];
    const queue = [];
    const world = {
        fetch: (url, init) => {
            calls.push({ url, init, headers: { ...(init.headers || {}) } });
            const next = queue.length ? queue.shift() : () => jsonResponse(200, {});
            return new Promise((resolve, reject) => {
                const sig = init.signal;
                const onAbort = () => { const e = new Error("aborted"); e.name = "AbortError"; reject(e); };
                if (sig && sig.aborted) return onAbort();
                if (sig) sig.addEventListener("abort", onAbort, { once: true });
                Promise.resolve().then(() => next(url, init, sig)).then(resolve, reject);
            });
        },
        URL, AbortController, setTimeout, clearTimeout, Date, Math, Promise, JSON, Error, TypeError, Object, Array, Number, String, RegExp, Response, Headers, FormData, Blob,
        navigator: { onLine: online },
        location: { protocol: new URL(origin).protocol, origin },
        console: { log() {}, warn() {}, error() {} },
    };
    if (native) world.Capacitor = { isNativePlatform: () => true };
    if (config) world.KOGNIT_CONFIG = config;
    const ctx = vm.createContext(world);
    vm.runInContext(SOURCE, ctx, { filename: "api.js" });
    const api = ctx.KognitApi;
    return {
        api, calls, world,
        reply: (...fns) => queue.push(...fns.map((f) => (typeof f === "function" ? f : () => f))),
        withToken(token = JWT, onRefresh) {
            api.configure({ getAccessToken: async ({ forceRefresh } = {}) => (forceRefresh && onRefresh ? onRefresh() : token) });
        },
    };
}

/* ============================================================ CONFIG */
console.log("\n== configuration ==");

await check("website defaults: same-origin API, public origin = the page", () => {
    const { api } = load();
    const c = api.config();
    eq(c.apiBase, ""); eq(c.publicOrigin, "https://kloriz.example"); eq(c.clientVersion, null); eq(c.problems, []);
});
await check("Android app REQUIRES apiBase and never falls back to https://localhost for the public origin", () => {
    const { api } = load({ native: true, origin: "https://localhost" });
    const c = api.config();
    eq(c.apiBase, null); eq(c.publicOrigin, null); assert(c.problems.length === 1, "one problem reported");
    eq(api.publicUrl("/share/abc"), null, "no public URL when the public origin is unknown");
});
await check("Android app with full configuration", () => {
    const { api } = load({ native: true, origin: "https://localhost", config: { apiBase: "https://app.kloriz.example", publicOrigin: "https://app.kloriz.example", clientVersion: "android/1.2.3" } });
    const c = api.config();
    eq(c.apiBase, "https://app.kloriz.example"); eq(c.publicOrigin, "https://app.kloriz.example"); eq(c.clientVersion, "android/1.2.3"); eq(c.problems, []);
    eq(api.publicUrl("/s/token"), "https://app.kloriz.example/s/token");
});
for (const bad of ["http://evil.example", "https://app.example/path", "https://user:pw@app.example", "https://app.example?x=1", "https://app.example#f",
                   "javascript:alert(1)", "//app.example", "app.example", "ftp://app.example", "https://", " ", 5, {}, "https://" + "a".repeat(260) + ".com"]) {
    await check(`apiBase ${JSON.stringify(bad).slice(0, 40)} is rejected (request refused before any network call)`, async () => {
        const h = load({ config: { apiBase: bad } });
        assert(h.api.config().problems.length > 0, "problem reported");
        await rejects(h.api.get("/api/x", { auth: "none" }), (e) => eq(e.kind, "config"));
        eq(h.calls.length, 0, "no fetch");
    });
}
await check("http is accepted only for local development hosts", () => {
    for (const ok of ["http://localhost:8000", "http://127.0.0.1:8000"]) eq(load({ config: { apiBase: ok } }).api.config().apiBase, ok);
    eq(load({ config: { apiBase: "http://192.168.1.5:8000" } }).api.config().apiBase, null);
});
await check("clientVersion must look like android/1.2.3, otherwise it is reported and not sent", async () => {
    for (const bad of ["1.2.3", "android/1.2", "Android/1.2.3", "android/1.2.3; x", "android/1.2.3\r\nX: y", "android/99999.1.1"]) {
        const h = load({ config: { clientVersion: bad } });
        eq(h.api.config().clientVersion, null, bad); assert(h.api.config().problems.length > 0);
    }
});
await check("publicUrl rejects anything that is not a plain path", () => {
    const { api } = load();
    for (const bad of ["https://evil.example/x", "//evil.example", "x", "/a\\b", 5, null]) eq(api.publicUrl(bad), null, String(bad));
    eq(api.publicUrl("/reset"), "https://kloriz.example/reset");
});

/* ============================================================ PATH SAFETY + HEADERS */
console.log("\n== path safety and headers ==");

for (const bad of ["https://evil.example/api/x", "//evil.example/api/x", "api/x", "", "/api/x y", "/api/x\n", "/api\\x", null, undefined, 42, "/" + "a".repeat(2100)]) {
    await check(`path ${JSON.stringify(bad)?.slice(0, 30)} is refused and the token is never sent`, async () => {
        const h = load(); h.withToken();
        await rejects(h.api.get(bad), (e) => eq(e.kind, "config"));
        eq(h.calls.length, 0);
    });
}
await check("URL is apiBase + path; Authorization, Accept, no-store, no cookies", async () => {
    const h = load({ config: { apiBase: "https://app.kloriz.example" } }); h.withToken();
    h.reply(jsonResponse(200, { ok: 1 }));
    await h.api.get("/api/profile");
    const c = h.calls[0];
    eq(c.url, "https://app.kloriz.example/api/profile");
    eq(c.headers.Authorization, `Bearer ${JWT}`); eq(c.headers.Accept, "application/json");
    eq(c.init.cache, "no-store"); eq(c.init.credentials, "omit"); eq(c.init.method, "GET");
});
await check("website: relative same-origin URL and NO X-Client-Version header", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(200, {}));
    await h.api.get("/api/profile");
    eq(h.calls[0].url, "/api/profile"); assert(!("X-Client-Version" in h.calls[0].headers));
});
await check("Android: X-Client-Version is sent on every request, including unauthenticated ones", async () => {
    const h = load({ native: true, config: { apiBase: "https://app.kloriz.example", clientVersion: "android/1.4.0" } });
    h.reply(jsonResponse(200, APP_CONFIG_FIXTURE));
    await h.api.get("/api/app-config", { auth: "none" });
    eq(h.calls[0].headers["X-Client-Version"], "android/1.4.0");
});
await check("X-Request-ID is NEVER sent as a request header (it would break Android CORS preflight)", async () => {
    const h = load({ config: { clientVersion: "android/1.0.0", apiBase: "https://app.kloriz.example" } }); h.withToken();
    h.reply(jsonResponse(200, {}), jsonResponse(200, {}), jsonResponse(200, {}));
    await h.api.get("/api/a"); await h.api.post("/api/b", { json: { a: 1 } }); await h.api.upload("/api/c", new FormData());
    for (const c of h.calls) for (const name of Object.keys(c.headers)) assert(name.toLowerCase() !== "x-request-id", "sent " + name);
});
await check("every header api.js sends is inside the server's CORS allow-list (+ safelisted Accept)", async () => {
    const h = load({ config: { clientVersion: "android/1.0.0" } }); h.withToken();
    h.reply(jsonResponse(200, {}), jsonResponse(200, {}));
    await h.api.post("/api/a", { json: { a: 1 } }); await h.api.upload("/api/b", new FormData());
    const allowed = new Set(["authorization", "content-type", "x-client-version", "accept"]);
    for (const c of h.calls) for (const name of Object.keys(c.headers)) assert(allowed.has(name.toLowerCase()), "not allowed: " + name);
});
await check("json body: Content-Type + stringified; FormData: no Content-Type (browser sets the boundary)", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(200, {}), jsonResponse(200, {}));
    await h.api.post("/api/x", { json: { a: [1, 2], b: "ক" } });
    eq(h.calls[0].headers["Content-Type"], "application/json"); eq(h.calls[0].init.body, JSON.stringify({ a: [1, 2], b: "ক" }));
    const fd = new FormData(); fd.append("k", "v");
    await h.api.upload("/api/y", fd);
    assert(!("Content-Type" in h.calls[1].headers)); assert(h.calls[1].init.body === fd); eq(h.calls[1].init.method, "POST");
});
await check("two body kinds at once are a programming error, refused before any request", async () => {
    const h = load(); h.withToken();
    await rejects(h.api.post("/api/x", { json: {}, formData: new FormData() }), (e) => eq(e.kind, "config"));
    eq(h.calls.length, 0);
});
await check("caller headers are merged, but cannot replace the access token with another one by accident of ordering", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(200, {}));
    await h.api.get("/api/x", { headers: { "X-Custom": "1", Authorization: "Bearer attacker" } });
    eq(h.calls[0].headers.Authorization, `Bearer ${JWT}`); eq(h.calls[0].headers["X-Custom"], "1");
});

/* ============================================================ AUTH */
console.log("\n== authentication ==");

await check("auth required (default) and no token: AUTH_REQUIRED, no network call, expiry hook stays quiet", async () => {
    const h = load(); let fired = 0; h.api.configure({ getAccessToken: async () => null, onAuthExpired: () => fired++ });
    const e = await rejects(h.api.get("/api/x"), (e) => { eq(e.kind, "auth"); eq(e.code, "AUTH_REQUIRED"); });
    eq(h.calls.length, 0); eq(fired, 0); assert(e.toUserMessage().length > 0);
});
await check("no provider configured at all behaves like no token", async () => {
    const h = load();
    await rejects(h.api.get("/api/x"), (e) => eq(e.code, "AUTH_REQUIRED")); eq(h.calls.length, 0);
});
await check('auth "none": never attaches a token, even when one exists', async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(200, {}));
    await h.api.get("/api/app-config", { auth: "none" });
    assert(!("Authorization" in h.calls[0].headers));
});
await check('auth "optional": attaches the token when there is one, proceeds without when there is not', async () => {
    const a = load(); a.withToken(); a.reply(jsonResponse(200, {})); await a.api.get("/api/x", { auth: "optional" });
    eq(a.calls[0].headers.Authorization, `Bearer ${JWT}`);
    const b = load(); b.api.configure({ getAccessToken: async () => null }); b.reply(jsonResponse(200, {})); await b.api.get("/api/x", { auth: "optional" });
    assert(!("Authorization" in b.calls[0].headers));
});
await check("a malformed 'token' (spaces, newlines, too short) is never put in a header", async () => {
    for (const bad of ["abc", "token with spaces and more", "tok\r\nX-Injected: 1" + "a".repeat(20), "", "a".repeat(5000), 5, {}]) {
        const h = load(); h.api.configure({ getAccessToken: async () => bad });
        await rejects(h.api.get("/api/x"), (e) => eq(e.code, "AUTH_REQUIRED")); eq(h.calls.length, 0);
    }
});
await check("a provider that throws is treated as 'no token', never as a crash", async () => {
    const h = load(); h.api.configure({ getAccessToken: async () => { throw new Error("boom"); } });
    await rejects(h.api.get("/api/x"), (e) => eq(e.code, "AUTH_REQUIRED"));
});
await check("401 -> ONE silent refresh -> retry with the new token -> success", async () => {
    const h = load(); const refresh = []; h.withToken(JWT, () => { refresh.push(1); return JWT2; });
    h.reply(jsonResponse(401, { detail: "expired" }), jsonResponse(200, { ok: true }));
    const r = await h.api.post("/api/chat", { json: { a: 1 } });
    eq(r.data, { ok: true }); eq(h.calls.length, 2); eq(refresh.length, 1);
    eq(h.calls[0].headers.Authorization, `Bearer ${JWT}`); eq(h.calls[1].headers.Authorization, `Bearer ${JWT2}`);
});
await check("401 with a refresh that yields the SAME token: session-expired error + hook once, no loop", async () => {
    const h = load(); let fired = 0; h.withToken(JWT, () => JWT); h.api.configure({ onAuthExpired: () => fired++ });
    h.reply(jsonResponse(401, {}), jsonResponse(401, {}));
    await rejects(h.api.get("/api/x"), (e) => { eq(e.kind, "auth"); eq(e.code, "AUTH_EXPIRED"); eq(e.status, 401); });
    eq(h.calls.length, 1, "no retry with an identical token");
    h.reply(jsonResponse(401, {}));
    await rejects(h.api.get("/api/x"), (e) => eq(e.kind, "auth")); eq(fired, 1, "hook fires once per episode");
});
await check("401 again AFTER the refreshed retry: expired, exactly 2 requests, never a third", async () => {
    const h = load(); let fired = 0; h.withToken(JWT, () => JWT2); h.api.configure({ onAuthExpired: () => fired++ });
    h.reply(jsonResponse(401, {}), jsonResponse(401, {}), jsonResponse(200, {}));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => eq(e.code, "AUTH_EXPIRED"));
    eq(h.calls.length, 2); eq(fired, 1);
});
await check("no refresh possible (provider returns null on refresh): expired", async () => {
    const h = load(); h.withToken(JWT, () => null);
    h.reply(jsonResponse(401, {}));
    await rejects(h.api.get("/api/x"), (e) => eq(e.code, "AUTH_EXPIRED"));
});
await check("many concurrent 401s share ONE refresh (no stampede on Supabase)", async () => {
    const h = load(); let refreshes = 0;
    h.withToken(JWT, async () => { refreshes++; await sleep(20); return JWT2; });
    for (let i = 0; i < 5; i++) h.reply(jsonResponse(401, {}));
    for (let i = 0; i < 5; i++) h.reply(jsonResponse(200, { i }));
    const rs = await Promise.all([1, 2, 3, 4, 5].map(() => h.api.get("/api/x", { retries: 0 })));
    eq(rs.length, 5); eq(refreshes, 1);
});
await check("a successful call ends the 'expired' episode so the hook can fire again later", async () => {
    const h = load(); let fired = 0; h.withToken(JWT, () => JWT); h.api.configure({ onAuthExpired: () => fired++ });
    h.reply(jsonResponse(401, {}), jsonResponse(200, {}), jsonResponse(401, {}));
    await rejects(h.api.get("/api/x"), () => {}); await h.api.get("/api/x"); await rejects(h.api.get("/api/x"), () => {});
    eq(fired, 2);
});
await check('401 on an auth:"none" call is just an HTTP error, not a session event', async () => {
    const h = load(); let fired = 0; h.api.configure({ onAuthExpired: () => fired++ });
    h.reply(jsonResponse(401, { detail: "nope" }));
    await rejects(h.api.get("/api/x", { auth: "none" }), (e) => { eq(e.kind, "http"); eq(e.status, 401); }); eq(fired, 0);
});
await check("a hook that throws does not break the request path", async () => {
    const h = load(); h.withToken(JWT, () => JWT); h.api.configure({ onAuthExpired: () => { throw new Error("hook bug"); } });
    h.reply(jsonResponse(401, {}));
    await rejects(h.api.get("/api/x"), (e) => eq(e.kind, "auth"));
});
await check("the FormData body is re-sent intact on the post-refresh retry", async () => {
    const h = load(); h.withToken(JWT, () => JWT2); h.reply(jsonResponse(401, {}), jsonResponse(200, { ok: 1 }));
    const fd = new FormData(); fd.append("prompt", "hello");
    await h.api.post("/api/chat", { formData: fd });
    assert(h.calls[0].init.body === fd && h.calls[1].init.body === fd);
});

/* ============================================================ RESPONSES */
console.log("\n== responses and errors ==");

await check("200 JSON parsed; 204 and empty body give null; X-Request-ID is read from the response", async () => {
    const h = load(); h.withToken();
    h.reply(jsonResponse(200, { a: 1 }, { "X-Request-ID": "req_abc-123" }), new Response(null, { status: 204 }), new Response("  ", { status: 200 }));
    const a = await h.api.get("/api/x"); eq(a.data, { a: 1 }); eq(a.requestId, "req_abc-123"); eq(a.status, 200);
    eq((await h.api.get("/api/x")).data, null); eq((await h.api.get("/api/x")).data, null);
});
await check("a malformed X-Request-ID is dropped (it later goes into a report)", async () => {
    const h = load(); h.withToken();
    h.reply(jsonResponse(200, {}, { "X-Request-ID": "bad id with spaces" }), jsonResponse(200, {}, { "X-Request-ID": "x".repeat(65) }));
    eq((await h.api.get("/api/x")).requestId, null); eq((await h.api.get("/api/x")).requestId, null);
});
await check("a 200 whose body is not JSON is a parse error, never a half-parsed value", async () => {
    const h = load(); h.withToken(); h.reply(new Response("<html>oops</html>", { status: 200 }));
    await rejects(h.api.get("/api/x", { retries: 0 }), (e) => eq(e.kind, "parse"));
});
await check("parse:text and parse:blob", async () => {
    const h = load(); h.withToken(); h.reply(new Response("# md", { status: 200 }), new Response("PDFDATA", { status: 200 }));
    eq((await h.api.post("/api/export/markdown", { parse: "text" })).data, "# md");
    const b = (await h.api.post("/api/x", { parse: "blob" })).data; eq(await b.text(), "PDFDATA");
});
await check("string detail becomes the message; code is null", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(404, { detail: "Chat not found." }));
    await rejects(h.api.get("/api/x"), (e) => { eq(e.kind, "http"); eq(e.status, 404); eq(e.message, "Chat not found."); eq(e.code, null); });
});
await check("object detail gives a stable code + message, and the full body stays available (e.g. PLAN_REQUIRED)", async () => {
    const h = load(); h.withToken();
    h.reply(jsonResponse(403, { detail: { code: "PLAN_REQUIRED", message: "Upgrade to continue.", feature: "pdf" }, extra: 1 }, { "X-Request-ID": "r1" }));
    await rejects(h.api.post("/api/pdf/upload", { formData: new FormData() }), (e) => {
        eq(e.code, "PLAN_REQUIRED"); eq(e.message, "Upgrade to continue."); eq(e.detail.feature, "pdf"); eq(e.body.extra, 1); eq(e.requestId, "r1"); eq(e.status, 403);
    });
});
await check("validation (422) detail lists are never echoed back to the student", async () => {
    const h = load(); h.withToken();
    h.reply(jsonResponse(422, { detail: [{ loc: ["body", "secret_field"], msg: "value is not a valid integer", input: "PRIVATE" }] }));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => { assert(!/secret_field|PRIVATE/.test(e.message)); eq(e.status, 422); assert(e.message.length > 0); });
});
await check("an HTML / plain-text error page from a proxy gives a clean HTTP error", async () => {
    const h = load(); h.withToken(); h.reply(new Response("<html>502 Bad Gateway</html>", { status: 502 }));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => { eq(e.kind, "http"); eq(e.status, 502); eq(e.message, ""); assert(e.toUserMessage().length > 0); });
});
await check("error messages are clipped and stripped of control characters", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(400, { detail: "a\u0000b\nc" + "x".repeat(1000) }));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => { assert(e.message.length <= 300); assert(!/[\u0000-\u001f]/.test(e.message)); });
});
await check("a malformed code in detail is ignored", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(400, { detail: { code: "bad code!<script>", message: "m" } }));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => eq(e.code, null));
});
await check("429: Retry-After seconds exposed, friendly message, NEVER auto-retried", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(429, {}, { "Retry-After": "17" }), jsonResponse(200, {}));
    await rejects(h.api.get("/api/x"), (e) => { eq(e.status, 429); eq(e.retryAfterSeconds, 17); assert(/17 seconds/.test(e.toUserMessage())); });
    eq(h.calls.length, 1);
});
await check("Retry-After as an HTTP date, a huge value, and garbage", async () => {
    const h = load(); h.withToken();
    h.reply(jsonResponse(429, {}, { "Retry-After": new Date(Date.now() + 30000).toUTCString() }), jsonResponse(429, {}, { "Retry-After": "999999" }), jsonResponse(429, {}, { "Retry-After": "soon" }));
    const got = [];
    for (let i = 0; i < 3; i++) await rejects(h.api.get("/api/x"), (e) => got.push(e.retryAfterSeconds));
    assert(got[0] >= 28 && got[0] <= 31, "date form " + got[0]); eq(got[1], 3600, "capped"); eq(got[2], null);
});
await check("APP_UPDATE_REQUIRED (HTTP 400 + detail.code) -> kind update_required, hook fired once", async () => {
    const h = load({ native: true, config: { apiBase: "https://a.example", clientVersion: "android/0.9.0" } }); h.withToken(); let fired = [];
    h.api.configure({ onUpdateRequired: (e) => fired.push(e.detail.min_version) });
    const body = { detail: { code: "APP_UPDATE_REQUIRED", message: "Please update.", min_version: "1.0.0" } };
    h.reply(jsonResponse(400, body), jsonResponse(400, body));
    await rejects(h.api.get("/api/chat"), (e) => { eq(e.kind, "update_required"); eq(e.code, "APP_UPDATE_REQUIRED"); eq(e.message, "Please update."); });
    await rejects(h.api.get("/api/chat"), (e) => eq(e.kind, "update_required"));
    eq(fired, ["1.0.0"], "once");
    eq(h.calls.length, 2, "not retried");
});
await check("an ordinary HTTP 400 is NOT an update requirement (the code decides, never the status)", async () => {
    const h = load(); h.withToken(); let fired = 0; h.api.configure({ onUpdateRequired: () => fired++ });
    h.reply(jsonResponse(400, { detail: "Bad input." }), jsonResponse(400, { detail: { code: "SOMETHING_ELSE", message: "x" } }));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => eq(e.kind, "http"));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => eq(e.kind, "http")); eq(fired, 0);
});
await check("the update code on a non-400 status is not treated as an update requirement either", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(409, { detail: { code: "APP_UPDATE_REQUIRED", message: "x" } }));
    await rejects(h.api.post("/api/x", { json: {} }), (e) => eq(e.kind, "http"));
});
await check("no token, header value or body ever appears in an error's text, JSON or properties", async () => {
    const h = load({ config: { clientVersion: "android/1.0.0" } }); h.withToken();
    h.reply(jsonResponse(500, { detail: "server broke" }));
    await rejects(h.api.post("/api/x", { json: { password: "HUNTER2-SECRET" } }), (e) => {
        const dump = e.stack + e.message + JSON.stringify(e) + String(e);
        assert(!dump.includes(JWT) && !dump.includes("HUNTER2-SECRET"));
    });
});
await check("toUserMessage: friendly text per kind, empty for a student's own cancel", async () => {
    const { api } = load(); const E = api.ApiError;
    assert(/offline/i.test(new E("network").toUserMessage())); assert(/too long/i.test(new E("timeout").toUserMessage()));
    eq(new E("aborted").toUserMessage(), ""); assert(/sign in/i.test(new E("auth").toUserMessage()));
    eq(new E("http", { status: 500 }).toUserMessage("fallback text"), "fallback text");
    assert(api.isApiError(new E("http")) && !api.isApiError(new Error("x")) && !api.isApiError(null));
});

/* ============================================================ RETRIES */
console.log("\n== retry policy ==");
const fast = { retryBaseMs: 1 };

await check("GET retries a 503 and then succeeds", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(503, {}), jsonResponse(502, {}), jsonResponse(200, { ok: 1 }));
    eq((await h.api.get("/api/x", fast)).data, { ok: 1 }); eq(h.calls.length, 3);
});
await check("GET gives up after its retries (3 requests total) with the real status", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(503, {}), jsonResponse(503, {}), jsonResponse(503, {}), jsonResponse(503, {}));
    await rejects(h.api.get("/api/x", fast), (e) => eq(e.status, 503)); eq(h.calls.length, 3);
});
await check("GET retries a network failure, not a 4xx", async () => {
    const h = load(); h.withToken();
    h.reply(() => { throw new TypeError("Failed to fetch"); }, jsonResponse(200, { ok: 1 }));
    eq((await h.api.get("/api/x", fast)).data, { ok: 1 });
    const g = load(); g.withToken(); g.reply(jsonResponse(404, {}), jsonResponse(200, {}));
    await rejects(g.api.get("/api/x", fast), (e) => eq(e.status, 404)); eq(g.calls.length, 1);
});
await check("a long Retry-After (> 10 s) is NOT waited out automatically", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(503, {}, { "Retry-After": "60" }), jsonResponse(200, {}));
    await rejects(h.api.get("/api/x", fast), (e) => { eq(e.status, 503); eq(e.retryAfterSeconds, 60); }); eq(h.calls.length, 1);
});
for (const method of ["post", "put", "patch", "del"]) {
    await check(`${method.toUpperCase()} is NEVER retried: not on 503, not on a network failure`, async () => {
        const h = load(); h.withToken(); h.reply(jsonResponse(503, {}), jsonResponse(200, {}));
        await rejects(h.api[method]("/api/x", { json: {}, ...fast }), (e) => eq(e.status, 503)); eq(h.calls.length, 1);
        const g = load(); g.withToken(); g.reply(() => { throw new TypeError("Failed to fetch"); }, jsonResponse(200, {}));
        await rejects(g.api[method]("/api/x", { json: {}, ...fast }), (e) => eq(e.kind, "network")); eq(g.calls.length, 1);
    });
}
await check("an explicit retries:3 on a POST still does not make it retryable", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(503, {}), jsonResponse(200, {}));
    await rejects(h.api.post("/api/x", { json: {}, retries: 3, ...fast }), (e) => eq(e.status, 503)); eq(h.calls.length, 1);
});
await check("a POST the caller marks idempotent gets exactly one retry", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(503, {}), jsonResponse(200, { ok: 1 }));
    eq((await h.api.post("/api/x", { json: {}, idempotent: true, ...fast })).data, { ok: 1 }); eq(h.calls.length, 2);
    const g = load(); g.withToken(); g.reply(jsonResponse(503, {}), jsonResponse(503, {}), jsonResponse(200, {}));
    await rejects(g.api.post("/api/x", { json: {}, idempotent: true, ...fast }), (e) => eq(e.status, 503)); eq(g.calls.length, 2);
});
await check("retries:0 disables GET retries", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(503, {}), jsonResponse(200, {}));
    await rejects(h.api.get("/api/x", { retries: 0 }), (e) => eq(e.status, 503)); eq(h.calls.length, 1);
});

/* ============================================================ TIMEOUT / CANCEL / OFFLINE */
console.log("\n== timeouts, cancellation, offline ==");
const hang = (_u, _i, sig) => new Promise(() => {});   // never answers; the fake fetch aborts it via the signal

await check("a request that never answers fails with kind timeout after timeoutMs", async () => {
    const h = load(); h.withToken(); h.reply(hang);
    const t0 = Date.now();
    await rejects(h.api.post("/api/x", { json: {}, timeoutMs: 60 }), (e) => eq(e.kind, "timeout"));
    const dt = Date.now() - t0; assert(dt >= 50 && dt < 1000, "took " + dt);
});
await check("the student's own cancel is 'aborted', never 'timeout' and never retried", async () => {
    const h = load(); h.withToken(); h.reply(hang, jsonResponse(200, {}));
    const ctl = new AbortController(); const p = h.api.get("/api/x", { signal: ctl.signal, timeoutMs: 5000, ...fast });
    setTimeout(() => ctl.abort(), 20);
    await rejects(p, (e) => eq(e.kind, "aborted")); eq(h.calls.length, 1);
});
await check("an already-aborted signal fails as 'aborted'", async () => {
    const h = load(); h.withToken(); h.reply(hang);
    const ctl = new AbortController(); ctl.abort();
    await rejects(h.api.get("/api/x", { signal: ctl.signal }), (e) => eq(e.kind, "aborted"));
});
await check("GET timeouts are retried; the retry can succeed", async () => {
    const h = load(); h.withToken(); h.reply(hang, jsonResponse(200, { ok: 1 }));
    eq((await h.api.get("/api/x", { timeoutMs: 40, ...fast })).data, { ok: 1 });
});
await check("offline: fails at once as 'network' without calling fetch", async () => {
    const h = load({ online: false }); h.withToken();
    await rejects(h.api.get("/api/x"), (e) => eq(e.kind, "network")); eq(h.calls.length, 0);
});
await check("stream: returns the live Response; the header timer stops once headers arrive", async () => {
    const h = load(); h.withToken();
    const enc = new TextEncoder();
    h.reply(async () => new Response(new ReadableStream({ async start(c) { c.enqueue(enc.encode('{"a":1}\n')); await sleep(120); c.enqueue(enc.encode('{"b":2}\n')); c.close(); } }), { status: 200, headers: { "X-Request-ID": "stream1" } }));
    const r = await h.api.stream("/api/chat/stream", { formData: new FormData(), timeoutMs: 50 });
    eq(r.requestId, "stream1");
    const text = await r.response.text();     // takes ~120 ms > the 50 ms header timeout: must NOT be aborted
    eq(text, '{"a":1}\n{"b":2}\n');
});
await check("stream: the student's Stop button (signal) cancels the body read", async () => {
    const h = load(); h.withToken(); const enc = new TextEncoder();
    h.reply((_u, init) => new Response(new ReadableStream({
        start(c) { c.enqueue(enc.encode("x\n")); init.signal.addEventListener("abort", () => { try { c.error(Object.assign(new Error("aborted"), { name: "AbortError" })); } catch (e) {} }); }
    }), { status: 200 }));
    const ctl = new AbortController();
    const r = await h.api.stream("/api/chat/stream", { formData: new FormData(), signal: ctl.signal });
    const reader = r.response.body.getReader(); await reader.read();
    setTimeout(() => ctl.abort(), 10);
    let name = null; try { await reader.read(); } catch (e) { name = e.name; }
    eq(name, "AbortError");
});
await check("stream: an error status is still a structured ApiError (body drained, not returned as a stream)", async () => {
    const h = load(); h.withToken(); h.reply(jsonResponse(429, { detail: { code: "QUOTA_EXCEEDED", message: "Daily limit reached." } }, { "Retry-After": "30" }));
    await rejects(h.api.stream("/api/chat/stream", { formData: new FormData() }), (e) => { eq(e.code, "QUOTA_EXCEEDED"); eq(e.retryAfterSeconds, 30); });
});

/* ============================================================ APP CONFIG + VERSION CHECK */
console.log("\n== app-config and version check ==");

await check("compareVersions is numeric and fails safe", () => {
    const { api } = load();
    eq(api.compareVersions("1.2.3", "1.2.4"), -1); eq(api.compareVersions("1.10.0", "1.2.0"), 1); eq(api.compareVersions("2.0.0", "2.0.0"), 0);
    for (const bad of [["1.2", "1.2.3"], ["x", "1.0.0"], [null, "1.0.0"], ["1.2.3.4", "1.0.0"], ["1.2.3", undefined]]) eq(api.compareVersions(...bad), null);
});
await check("the REAL backend app-config fixture is understood (contract)", async () => {
    const h = load(); h.reply(jsonResponse(200, APP_CONFIG_FIXTURE));
    const c = await h.api.getAppConfig();
    eq(c.apiVersion, 1); eq(c.apiVersionSupported, true); eq(c.android.minVersion, APP_CONFIG_FIXTURE.android.min_version); eq(c.android.latestVersion, null);
    eq(h.calls[0].url, "/api/app-config"); assert(!("Authorization" in h.calls[0].headers), "public endpoint: no token");
});
await check("app-config is cached; force refetches", async () => {
    const h = load(); h.reply(jsonResponse(200, APP_CONFIG_FIXTURE), jsonResponse(200, APP_CONFIG_FIXTURE));
    await h.api.getAppConfig(); await h.api.getAppConfig(); eq(h.calls.length, 1);
    await h.api.getAppConfig({ force: true }); eq(h.calls.length, 2);
});
await check("a bad app-config shape returns null instead of crashing or caching junk", async () => {
    for (const bad of [{}, { api_version: 1 }, { api_version: 1, android: {} }, { api_version: 1, android: { min_version: "banana" } }, [], "x"]) {
        const h = load(); h.reply(jsonResponse(200, bad)); eq(await h.api.getAppConfig(), null);
    }
    const h = load(); h.reply(jsonResponse(200, { api_version: 2, android: { min_version: "1.0.0", latest_version: "zzz" } }));
    const c = await h.api.getAppConfig(); eq(c.apiVersionSupported, false); eq(c.android.latestVersion, null);
});
const androidCfg = (v) => ({ native: true, origin: "https://localhost", config: { apiBase: "https://a.example", clientVersion: v } });
await check("checkForUpdate: below the minimum -> updateRequired; at/above -> no", async () => {
    const cfg = (min, latest) => ({ api_version: 1, android: { min_version: min, latest_version: latest } });
    let h = load(androidCfg("android/1.2.0")); h.reply(jsonResponse(200, cfg("1.3.0", "1.5.0")));
    let r = await h.api.checkForUpdate(); eq([r.updateRequired, r.updateAvailable, r.checked, r.minVersion, r.latestVersion], [true, true, true, "1.3.0", "1.5.0"]);
    h = load(androidCfg("android/1.10.0")); h.reply(jsonResponse(200, cfg("1.2.0", "1.10.0")));
    r = await h.api.checkForUpdate(); eq([r.updateRequired, r.updateAvailable], [false, false], "1.10.0 is newer than 1.2.0");
    h = load(androidCfg("android/1.3.0")); h.reply(jsonResponse(200, cfg("1.3.0", null)));
    r = await h.api.checkForUpdate(); eq([r.updateRequired, r.updateAvailable], [false, false]);
});
await check("checkForUpdate fails OPEN: server down, bad shape or not Android -> nobody is locked out", async () => {
    let h = load(androidCfg("android/1.0.0")); h.reply(() => { throw new TypeError("down"); }, () => { throw new TypeError("down"); }, () => { throw new TypeError("down"); });
    let r = await h.api.checkForUpdate(); eq([r.updateRequired, r.checked], [false, false]);
    h = load(androidCfg("android/1.0.0")); h.reply(jsonResponse(200, { junk: true }));
    r = await h.api.checkForUpdate(); eq([r.updateRequired, r.checked], [false, false]);
    h = load(); r = await h.api.checkForUpdate(); eq([r.updateRequired, r.checked], [false, false]); eq(h.calls.length, 0, "the website never asks");
});

/* ============================================================ SOURCE HYGIENE */
console.log("\n== source hygiene ==");
// Lines that are not comments. (A regex comment-stripper is fooled by strings such as "*/*", so filter by line.)
// The file starts with ONE block comment (the header); every other comment is a full-line "//" comment.
const CODE = SOURCE.replace(/^\s*\/\*[\s\S]*?\*\//, "").split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
await check("hygiene filter really keeps the code (guards against a vacuous pass)", () => {
    assert(/await fetch\(/.test(CODE) && /function request\(|async function request/.test(CODE) && CODE.length > SOURCE.length * 0.5);
});
await check("api.js has no console output, no eval/Function, no storage access, no Supabase import, no secrets", () => {
    assert(!/console\./.test(CODE), "console used");
    assert(!/\beval\s*\(|new Function\s*\(/.test(CODE), "eval/Function");
    assert(!/localStorage|sessionStorage|indexedDB|document\.cookie/.test(CODE), "storage access");
    assert(!/supabase/i.test(CODE), "supabase referenced in code");
    assert(!/service_role|SERVICE_ROLE|api[_-]?key/i.test(CODE), "secret-ish word");
});
await check("the only network primitive is fetch to apiBase + path", () => {
    assert(!/XMLHttpRequest|sendBeacon|WebSocket|EventSource|importScripts/.test(CODE));
    eq((CODE.match(/\bfetch\(/g) || []).length, 1, "exactly one fetch call site");
    assert(/fetch\(cfg\.apiBase \+ path,/.test(CODE), "it targets apiBase + path");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);