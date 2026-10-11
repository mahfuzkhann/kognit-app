/* ============================================================
   KOGNIT SERVICE WORKER - Pocket Notes offline reading
   ============================================================
   Served from the site root by GET /sw.js (backend/main.py) so its
   scope covers "/". Its ONLY job is to let the app shell open while
   the network is down, so the student can read notes that were
   already synchronized into IndexedDB (static/js/notes-store.js).

   CACHE POLICY (explicit, and deliberately narrow):

     CACHED
       * "/"                 - the HTML shell. Identical for every user
                               (the template contains no per-user data).
       * /static/*           - Kognit's own CSS/JS/images (GET only).
       * public CDN libraries and fonts: jsDelivr, cdnjs, Google Fonts.

     NEVER CACHED - never even intercepted (the request goes straight to
     the network, exactly as if no service worker existed):
       * /api/*              - every API response. Private note data is
                               NOT stored in Cache Storage; it lives only in
                               the per-user IndexedDB cache, which is cleared
                               on sign-out.
       * every non-GET request.
       * Supabase (auth/REST) and any other origin not listed above.
       * /sw.js itself.

   STRATEGIES
       * shell + /static/*   NETWORK-FIRST with a short timeout, falling
                             back to the cache. A deploy is therefore picked
                             up as soon as the student is online - the cache
                             is only ever a fallback, never the primary
                             source, so stale code cannot linger while online.
       * CDN libraries       STALE-WHILE-REVALIDATE (they are public, and the
                             supabase-js URL is pinned to an exact version).
       * Only successful (ok) responses are stored, so an error page can
         never become the "offline copy".

   INSTALL: the core same-origin assets must ALL cache or installation
   fails (an incomplete shell is worse than no service worker - the page
   would then never claim offline support it does not have). The CDN
   libraries are cached best-effort; whether each one is present is
   reported by GET_STATUS so the UI never claims offline support that
   is not actually there.

   UPDATES: bump SW_VERSION on any change to this file or to the asset
   list. activate() deletes every older "kognit-" cache.
   ============================================================ */
"use strict";

const SW_VERSION = "2026-10-10.1";
const SHELL_CACHE = "kognit-shell-" + SW_VERSION;
const CDN_CACHE = "kognit-cdn-" + SW_VERSION;
const CACHE_PREFIX = "kognit-";

const NETWORK_TIMEOUT_MS = 4000;

// Same-origin assets that MUST be cached for the shell to open offline.
const CORE_URLS = [
    "/",
    "/static/css/tokens.css",
    "/static/css/style.css",
    "/static/css/shell.css",
    "/static/css/notes.css",
    "/static/img/kognit-logo.svg",
    "/static/js/theme.js",
    "/static/js/stream-render.js",
    "/static/js/vendor/purify.min.js",
    "/static/js/app.js",
    "/static/js/notes-store.js",
    "/static/js/notes.js",
    "/static/js/nav-views.js",
    "/static/js/ui-core.js",
    "/static/js/shell.js",
];

// Must match the <script>/<link> URLs in templates/index.html exactly.
// REQUIRED_CDN: without these the app cannot boot or render notes offline.
const REQUIRED_CDN = [
    "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.2",
    "https://cdn.jsdelivr.net/npm/marked/marked.min.js",
];
// OPTIONAL_CDN: nice to have; notes still read without them (formulas
// would show as plain text / system font).
const OPTIONAL_CDN = [
    "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js",
    "https://fonts.googleapis.com/css2?family=Noto+Sans+Bengali:wght@400;500;600;700&display=swap",
];

const CDN_HOSTS = new Set([
    "cdn.jsdelivr.net",
    "cdnjs.cloudflare.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
]);


// ---------------------------------------------------------------
// install / activate
// ---------------------------------------------------------------

self.addEventListener("install", (event) => {
    event.waitUntil((async () => {
        const shell = await caches.open(SHELL_CACHE);
        // All-or-nothing for the core shell. `cache: "reload"` bypasses the
        // HTTP cache so a new version never precaches stale bytes.
        for (const url of CORE_URLS) {
            const response = await fetch(new Request(url, { cache: "reload" }));
            if (!response.ok) {
                throw new Error("core asset failed to cache: " + url + " (" + response.status + ")");
            }
            await shell.put(url, response);
        }

        // Best-effort for CDN libraries: one failing CDN must not block the
        // install. GET_STATUS reports what is actually present.
        const cdn = await caches.open(CDN_CACHE);
        await Promise.allSettled(REQUIRED_CDN.concat(OPTIONAL_CDN).map(async (url) => {
            const response = await fetch(new Request(url, { mode: "cors", credentials: "omit" }));
            if (response.ok) await cdn.put(url, response);
        }));

        await self.skipWaiting();
    })());
});

self.addEventListener("activate", (event) => {
    event.waitUntil((async () => {
        const keep = new Set([SHELL_CACHE, CDN_CACHE]);
        const names = await caches.keys();
        await Promise.all(
            names
                .filter((name) => name.startsWith(CACHE_PREFIX) && !keep.has(name))
                .map((name) => caches.delete(name))
        );
        await self.clients.claim();
    })());
});


// ---------------------------------------------------------------
// fetch
// ---------------------------------------------------------------

function fetchWithTimeout(request, ms) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), ms);
    return fetch(request, { signal: controller.signal }).finally(() => clearTimeout(timer));
}

async function networkFirst(request, cacheKey) {
    const cache = await caches.open(SHELL_CACHE);
    const cached = await cache.match(cacheKey);
    try {
        // With a cached fallback, don't let a hanging connection ("lie-fi")
        // block the page; with no fallback, wait for the real network.
        const response = cached ? await fetchWithTimeout(request, NETWORK_TIMEOUT_MS) : await fetch(request);
        if (response && response.ok && response.type === "basic") {
            await cache.put(cacheKey, response.clone());
        }
        return response;
    } catch (err) {
        if (cached) return cached;
        throw err;
    }
}

async function staleWhileRevalidate(event, request) {
    const cache = await caches.open(CDN_CACHE);
    const cached = await cache.match(request.url);

    // Subresource tags carry no crossorigin attribute, so the page's own
    // request is "no-cors" (opaque - its status is unreadable and a 404 would
    // look identical to success). Re-request in CORS mode (all four hosts
    // support it) so only a verified-ok response is ever stored.
    const refresh = fetch(new Request(request.url, { mode: "cors", credentials: "omit" }))
        .then(async (response) => {
            if (response && response.ok) await cache.put(request.url, response.clone());
            return response;
        });

    if (cached) {
        event.waitUntil(refresh.catch(() => undefined));
        return cached;
    }
    return refresh;
}

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET") return;

    const url = new URL(request.url);

    if (url.origin === self.location.origin) {
        if (url.pathname.startsWith("/api/") || url.pathname === "/sw.js") return;

        if (url.pathname === "/") {
            // One shell for every query string (OAuth redirects add ?code=...).
            // ONLY "/" is the app shell: any other page (e.g. FastAPI's /docs)
            // passes straight through and can never be cached AS the shell.
            event.respondWith(networkFirst(request, "/"));
            return;
        }
        if (url.pathname.startsWith("/static/")) {
            event.respondWith(networkFirst(request, url.pathname));
            return;
        }
        return;
    }

    if (CDN_HOSTS.has(url.hostname)) {
        event.respondWith(staleWhileRevalidate(event, request));
    }
    // Everything else (Supabase, Google avatars, ...) passes straight through.
});


// ---------------------------------------------------------------
// status (so the UI never over-claims offline support)
// ---------------------------------------------------------------

async function computeStatus() {
    const shell = await caches.open(SHELL_CACHE);
    const cdn = await caches.open(CDN_CACHE);

    const coreMissing = [];
    for (const url of CORE_URLS) {
        if (!(await shell.match(url))) coreMissing.push(url);
    }
    const requiredCdnMissing = [];
    for (const url of REQUIRED_CDN) {
        if (!(await cdn.match(url))) requiredCdnMissing.push(url);
    }
    const optionalCdnMissing = [];
    for (const url of OPTIONAL_CDN) {
        if (!(await cdn.match(url))) optionalCdnMissing.push(url);
    }
    return {
        version: SW_VERSION,
        // "ready" = the app can open and render notes with no network.
        ready: coreMissing.length === 0 && requiredCdnMissing.length === 0,
        mathReady: !optionalCdnMissing.some((u) => u.indexOf("mathjax") !== -1),
        coreMissing: coreMissing.length,
        requiredCdnMissing: requiredCdnMissing.length,
    };
}

self.addEventListener("message", (event) => {
    if (!event.data || event.data.type !== "GET_STATUS" || !event.ports || !event.ports[0]) return;
    const port = event.ports[0];
    event.waitUntil(
        computeStatus()
            .then((status) => port.postMessage(status))
            .catch(() => port.postMessage({ version: SW_VERSION, ready: false, mathReady: false, error: true }))
    );
});