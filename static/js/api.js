/* ============================================================
   KOGNIT - CENTRAL API CLIENT (api.js)
   ============================================================
   ONE place that talks to the Kloriz backend. It replaces the token-fetching,
   header-building and error-parsing code that is repeated around every fetch() in
   the app. Contract with the backend: docs/API_CLIENT.md (and test_api_contract.py,
   which fails if this file and backend/client_access.py ever disagree).

   WHAT IT DOES
     * builds the URL:  API_BASE + path     (web: same origin; Android: the real server)
     * attaches  Authorization: Bearer <token>  (token comes from a provider that
       app.js supplies - this file never imports Supabase and never stores a token)
     * attaches  X-Client-Version: android/1.2.3  when configured (Android only)
     * timeouts + caller cancellation (AbortSignal) that also work on old WebViews
     * ONE structured error type (ApiError) for every failure
     * reads X-Request-ID from the response (the B02 report endpoint needs it)
     * on 401: one silent token refresh + one retry, otherwise "session expired"
     * on APP_UPDATE_REQUIRED: tells the app to show the update screen
     * safe retries: ONLY idempotent reads (GET) retry automatically

   WHAT IT DELIBERATELY DOES NOT DO
     * It never SENDS an X-Request-ID header. The server generates its own and
       ignores any inbound one, and Android's CORS allow-list is exactly
       Authorization, Content-Type, X-Client-Version: an extra header would make the
       browser's preflight fail and break EVERY Android request.
     * It never sends a request to anything but API_BASE: a path must start with a
       single "/", so a bug can not make it post the student's token to another site.
     * It never retries POST/PUT/PATCH/DELETE on its own (a repeated "submit quiz"
       or "send report" must be the student's decision). A caller that knows an
       operation is safe to repeat can say so with  idempotent: true.
     * It never retries 429 (a quota / rate limit is an answer, not a glitch).
     * It never logs, stores or echoes a token, a request body or a response body.

   CONFIGURATION (all optional on the website)
     window.KOGNIT_CONFIG = {                     // set BEFORE this file loads
        apiBase:       "https://app.example.com", // REQUIRED inside the Android app
        publicOrigin:  "https://app.example.com", // used for share links / reset redirects
        clientVersion: "android/1.0.0"            // sent as X-Client-Version
     }
   On the website none of it is needed: apiBase is "" (same origin) and publicOrigin
   is the page's own origin. Inside the Capacitor app the page lives on
   https://localhost, which has NO API and is NOT the public site, so apiBase must be
   set and publicOrigin must never fall back to the page origin.

   Exposed as  window.KognitApi.   No dependencies. ES2017. Works in Node (tests).
   ============================================================ */
(function (global) {
    "use strict";

    var MODULE_VERSION = 1;
    var SUPPORTED_API_VERSION = 1;                        // GET /api/app-config -> api_version

    var HEADER_CLIENT_VERSION = "X-Client-Version";
    var HEADER_REQUEST_ID = "X-Request-ID";               // RESPONSE header only
    var ERROR_UPDATE_REQUIRED = "APP_UPDATE_REQUIRED";    // detail.code, HTTP 400

    var CLIENT_VERSION_RE = /^[a-z]{2,16}\/\d{1,4}\.\d{1,4}\.\d{1,4}$/;
    var VERSION_RE = /^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$/;
    var REQUEST_ID_RE = /^[A-Za-z0-9_.-]{1,64}$/;
    var ERROR_CODE_RE = /^[A-Za-z0-9_]{2,64}$/;
    var TOKEN_RE = /^[A-Za-z0-9._~+\/=-]{10,4096}$/;     // a JWT; anything else is not sent

    var DEFAULTS = {
        timeoutMs: 30000,                // ordinary JSON calls
        uploadTimeoutMs: 120000,         // PDF / image uploads (slow mobile networks)
        streamHeadersTimeoutMs: 120000,  // streaming: time allowed until the response STARTS
        getRetries: 2,                   // extra attempts for GET only
        retryBaseMs: 400,
        retryMaxWaitMs: 10000,           // a longer Retry-After is NOT waited out automatically
        appConfigTtlMs: 5 * 60 * 1000
    };

    var MAX_MESSAGE_CHARS = 300;
    var MAX_ERROR_BODY_CHARS = 100000;

    /* ---------------------------------------------------------------- errors */

    // kind: "network" | "timeout" | "aborted" | "auth" | "update_required" | "http" | "parse" | "config"
    function ApiError(kind, init) {
        init = init || {};
        this.name = "ApiError";
        this.kind = kind;
        this.status = typeof init.status === "number" ? init.status : 0;
        this.code = init.code || null;                       // stable machine code, e.g. "REAUTH_REQUIRED"
        this.detail = init.detail === undefined ? null : init.detail;
        this.body = init.body === undefined ? null : init.body;
        this.retryAfterSeconds = init.retryAfterSeconds === undefined ? null : init.retryAfterSeconds;
        this.requestId = init.requestId || null;
        // Empty unless the SERVER gave a student-safe message: toUserMessage() supplies the friendly default.
        this.message = init.message || "";
        if (typeof Error.captureStackTrace === "function") Error.captureStackTrace(this, ApiError);
    }
    ApiError.prototype = Object.create(Error.prototype);
    ApiError.prototype.constructor = ApiError;

    ApiError.prototype.toUserMessage = function (fallback) {
        switch (this.kind) {
            case "network": return "You're offline or Kloriz can't be reached.";
            case "timeout": return "That took too long. Please try again.";
            case "aborted": return "";
            case "auth": return "Your session has expired. Please sign in again.";
            case "update_required": return this.message || "Please update the app to continue.";
            case "config": return "The app is not set up correctly. Please update it or contact support.";
            case "parse": return "Kloriz sent an unexpected response. Please try again.";
            default: break;
        }
        if (this.status === 429 && !this.message) {
            return this.retryAfterSeconds
                ? "Too many requests. Please try again in " + this.retryAfterSeconds + " seconds."
                : "Too many requests. Please try again in a moment.";
        }
        return this.message || fallback || "Something went wrong. Please try again.";
    };

    function isApiError(e) { return !!e && e.name === "ApiError"; }

    /* ------------------------------------------------------------- versions */

    function parseVersion(value) {
        if (typeof value !== "string") return null;
        var m = VERSION_RE.exec(value.trim());
        return m ? [parseInt(m[1], 10), parseInt(m[2], 10), parseInt(m[3], 10)] : null;
    }

    // -1 / 0 / 1, or null when either side is unreadable. Numeric: 1.10.0 > 1.2.0.
    function compareVersions(a, b) {
        var x = parseVersion(a), y = parseVersion(b);
        if (!x || !y) return null;
        for (var i = 0; i < 3; i++) { if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1; }
        return 0;
    }

    /* --------------------------------------------------------------- config */

    // "https://app.example.com" -> same string; anything that is not a bare origin -> null.
    // http is allowed only for local development hosts.
    function normalizeOrigin(value) {
        if (typeof value !== "string" || !value || value.length > 253) return null;
        var u;
        try { u = new URL(value.trim()); } catch (e) { return null; }
        if (u.username || u.password || u.search || u.hash) return null;
        if (u.pathname !== "/" ) return null;
        if (u.protocol === "https:") return u.origin;
        if (u.protocol === "http:" && /^(localhost|127\.0\.0\.1|\[::1\])$/.test(u.hostname)) return u.origin;
        return null;
    }

    // Pure: raw settings in, safe settings out. Exported so the rules are testable.
    function resolveConfig(raw, env) {
        raw = raw || {};
        env = env || {};
        var native = !!env.native;
        var apiBase = null, publicOrigin = null, clientVersion = null;
        var problems = [];

        if (raw.apiBase === undefined || raw.apiBase === null || raw.apiBase === "") {
            apiBase = native ? null : "";                    // same origin is only valid on the website
            if (native) problems.push("apiBase is required in the app");
        } else {
            apiBase = normalizeOrigin(raw.apiBase);
            if (apiBase === null) problems.push("apiBase is not a valid origin");
        }

        if (raw.publicOrigin !== undefined && raw.publicOrigin !== null && raw.publicOrigin !== "") {
            publicOrigin = normalizeOrigin(raw.publicOrigin);
            if (publicOrigin === null) problems.push("publicOrigin is not a valid origin");
        } else if (!native && env.locationOrigin) {
            publicOrigin = normalizeOrigin(env.locationOrigin);   // the website's own address
        }
        // Inside the app the page origin (https://localhost) is NOT the public site: never fall back to it.

        if (raw.clientVersion !== undefined && raw.clientVersion !== null && raw.clientVersion !== "") {
            if (typeof raw.clientVersion === "string" && CLIENT_VERSION_RE.test(raw.clientVersion)) {
                clientVersion = raw.clientVersion;
            } else {
                problems.push("clientVersion must look like android/1.2.3");
            }
        }
        return { apiBase: apiBase, publicOrigin: publicOrigin, clientVersion: clientVersion, native: native, problems: problems };
    }

    var state = {
        raw: null,                 // explicit configure() settings
        getAccessToken: null,      // async ({forceRefresh}) => string | null
        onAuthExpired: null,
        onUpdateRequired: null,
        refreshPromise: null,      // single flight: many 401s trigger ONE refresh
        authExpiredFired: false,
        updateFired: false,
        appConfig: null,
        appConfigAt: 0
    };

    function detectEnv() {
        var native = false;
        try { native = !!(global.Capacitor && typeof global.Capacitor.isNativePlatform === "function" && global.Capacitor.isNativePlatform()); } catch (e) { native = false; }
        var locationOrigin = null;
        try { if (global.location && /^https?:$/.test(global.location.protocol)) locationOrigin = global.location.origin; } catch (e) { locationOrigin = null; }
        return { native: native, locationOrigin: locationOrigin };
    }

    function config() {
        var raw = state.raw || global.KOGNIT_CONFIG || {};
        return resolveConfig(raw, detectEnv());
    }

    // Absolute URL on the PUBLIC website (share links, password-reset redirects), or null when unknown.
    function publicUrl(path) {
        var origin = config().publicOrigin;
        if (!origin) return null;
        if (typeof path !== "string" || path.charAt(0) !== "/" || path.indexOf("//") === 0 || path.indexOf("\\") !== -1) return null;
        return origin + path;
    }

    function configure(options) {
        options = options || {};
        if (options.getAccessToken !== undefined) {
            if (options.getAccessToken !== null && typeof options.getAccessToken !== "function") throw new TypeError("getAccessToken must be a function");
            state.getAccessToken = options.getAccessToken;
        }
        if (options.onAuthExpired !== undefined) state.onAuthExpired = options.onAuthExpired;
        if (options.onUpdateRequired !== undefined) state.onUpdateRequired = options.onUpdateRequired;
        if (options.apiBase !== undefined || options.publicOrigin !== undefined || options.clientVersion !== undefined) {
            state.raw = Object.assign({}, state.raw || global.KOGNIT_CONFIG || {}, {
                apiBase: options.apiBase !== undefined ? options.apiBase : (state.raw || global.KOGNIT_CONFIG || {}).apiBase,
                publicOrigin: options.publicOrigin !== undefined ? options.publicOrigin : (state.raw || global.KOGNIT_CONFIG || {}).publicOrigin,
                clientVersion: options.clientVersion !== undefined ? options.clientVersion : (state.raw || global.KOGNIT_CONFIG || {}).clientVersion
            });
        }
        state.appConfig = null;
        return config();
    }

    function resetForTests() {
        state.raw = null; state.getAccessToken = null; state.onAuthExpired = null; state.onUpdateRequired = null;
        state.refreshPromise = null; state.authExpiredFired = false; state.updateFired = false;
        state.appConfig = null; state.appConfigAt = 0;
    }

    /* ----------------------------------------------------------- small utils */

    function safeCall(fn, arg) { if (typeof fn !== "function") return; try { fn(arg); } catch (e) { /* a hook must never break a request */ } }

    function cleanToken(value) { return typeof value === "string" && TOKEN_RE.test(value) ? value : null; }

    function sleep(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

    function clip(text) {
        if (typeof text !== "string") return "";
        var t = text.replace(/[\u0000-\u001f\u007f]/g, " ").trim();
        return t.length > MAX_MESSAGE_CHARS ? t.slice(0, MAX_MESSAGE_CHARS).trim() : t;
    }

    function parseRetryAfter(headerValue) {
        if (typeof headerValue !== "string" || !headerValue.trim()) return null;
        var v = headerValue.trim();
        if (/^\d{1,6}$/.test(v)) return Math.min(parseInt(v, 10), 3600);
        var t = Date.parse(v);
        if (!isNaN(t)) { var s = Math.ceil((t - Date.now()) / 1000); return s > 0 ? Math.min(s, 3600) : 0; }
        return null;
    }

    function headerValue(response, name) {
        try { return response.headers && typeof response.headers.get === "function" ? response.headers.get(name) : null; } catch (e) { return null; }
    }

    function readRequestId(response) {
        var v = headerValue(response, HEADER_REQUEST_ID);
        return typeof v === "string" && REQUEST_ID_RE.test(v) ? v : null;
    }

    // FastAPI/our errors: {detail: "text"} | {detail: {code, message, ...}} | {detail: [validation items]}.
    function readDetail(data) {
        var out = { message: "", code: null, detail: null };
        if (!data || typeof data !== "object") return out;
        var d = data.detail;
        out.detail = d === undefined ? null : d;
        if (typeof d === "string") { out.message = clip(d); }
        else if (d && typeof d === "object" && !Array.isArray(d)) {
            if (typeof d.code === "string" && ERROR_CODE_RE.test(d.code)) out.code = d.code;
            if (typeof d.message === "string") out.message = clip(d.message);
        } else if (Array.isArray(d)) {
            out.message = "Some of the information sent was not valid.";   // never echo validation internals
        }
        if (!out.code && typeof data.code === "string" && ERROR_CODE_RE.test(data.code)) out.code = data.code;
        return out;
    }

    function isIdempotentMethod(method) { return method === "GET" || method === "HEAD"; }

    /* -------------------------------------------- timeout + caller cancellation */

    // Old Android WebViews lack AbortSignal.any / AbortSignal.timeout, so combine by hand.
    function makeAbort(userSignal, timeoutMs) {
        var controller = new AbortController();
        var timedOut = false, timer = null, onUserAbort = null;
        if (userSignal) {
            if (userSignal.aborted) { controller.abort(); }
            else {
                onUserAbort = function () { controller.abort(); };
                userSignal.addEventListener("abort", onUserAbort, { once: true });
            }
        }
        function armTimer() {
            if (timer) clearTimeout(timer);
            if (timeoutMs > 0) timer = setTimeout(function () { timedOut = true; controller.abort(); }, timeoutMs);
        }
        armTimer();
        return {
            signal: controller.signal,
            timedOut: function () { return timedOut; },
            userAborted: function () { return !!(userSignal && userSignal.aborted); },
            stopTimer: function () { if (timer) { clearTimeout(timer); timer = null; } },
            cleanup: function () {
                if (timer) { clearTimeout(timer); timer = null; }
                if (userSignal && onUserAbort) userSignal.removeEventListener("abort", onUserAbort);
            }
        };
    }

    /* ------------------------------------------------------------------ auth */

    async function currentToken(forceRefresh) {
        if (typeof state.getAccessToken !== "function") return null;
        try { return cleanToken(await state.getAccessToken({ forceRefresh: !!forceRefresh })); } catch (e) { return null; }
    }

    // Many requests can hit 401 at once; they must share ONE refresh, not stampede Supabase.
    function refreshTokenOnce() {
        if (!state.refreshPromise) {
            state.refreshPromise = currentToken(true).then(
                function (t) { state.refreshPromise = null; return t; },
                function () { state.refreshPromise = null; return null; }
            );
        }
        return state.refreshPromise;
    }

    // No token at all (never signed in / signed out): NOT an expiry, so the "session expired" hook stays quiet.
    function authRequiredError() {
        return new ApiError("auth", { status: 401, code: "AUTH_REQUIRED", message: "Please sign in to continue." });
    }

    function raiseAuthExpired(requestId) {
        var err = new ApiError("auth", { status: 401, code: "AUTH_EXPIRED", requestId: requestId, message: "Your session has expired. Please sign in again." });
        if (!state.authExpiredFired) { state.authExpiredFired = true; safeCall(state.onAuthExpired, err); }
        return err;
    }

    /* --------------------------------------------------------------- request */

    function validatePath(path) {
        return typeof path === "string" && path.charAt(0) === "/" && path.charAt(1) !== "/" && path.indexOf("\\") === -1 &&
            !/[\u0000-\u001f\u007f\s]/.test(path) && path.length <= 2048;
    }

    async function readErrorBody(response) {
        var text = "";
        try { text = await response.text(); } catch (e) { return null; }
        if (text.length > MAX_ERROR_BODY_CHARS) return null;
        try { return JSON.parse(text); } catch (e) { return null; }
    }

    async function parseSuccess(response, parse) {
        if (parse === "response") return response;
        if (parse === "blob") return response.blob();
        if (parse === "text") return response.text();
        if (response.status === 204 || response.status === 205) return null;
        var text = await response.text();
        if (!text.trim()) return null;
        try { return JSON.parse(text); }
        catch (e) { throw new ApiError("parse", { status: response.status, requestId: readRequestId(response), message: "Unexpected response from the server." }); }
    }

    function buildBody(opts, headers) {
        var given = (opts.json !== undefined ? 1 : 0) + (opts.formData !== undefined ? 1 : 0) + (opts.body !== undefined ? 1 : 0);
        if (given > 1) throw new ApiError("config", { message: "Use only one of json, formData or body." });
        if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; return JSON.stringify(opts.json); }
        if (opts.formData !== undefined) return opts.formData;     // the browser sets the multipart boundary itself
        if (opts.body !== undefined) return opts.body;
        return undefined;
    }

    // request(path, {method, json|formData|body, auth, parse, stream, signal, timeoutMs, retries, retryBaseMs, idempotent, headers})
    //   auth:  "required" (default) | "optional" | "none"
    //   parse: "json" (default) | "text" | "blob" | "response"
    // Resolves { status, data, requestId, response }; rejects with ApiError.
    async function request(path, opts) {
        opts = opts || {};
        var method = (opts.method || "GET").toUpperCase();
        var authMode = opts.auth || "required";
        var parse = opts.stream ? "response" : (opts.parse || "json");

        if (!validatePath(path)) throw new ApiError("config", { message: "Invalid request path." });
        if (["required", "optional", "none"].indexOf(authMode) === -1) throw new ApiError("config", { message: "Invalid auth mode." });

        var cfg = config();
        if (cfg.apiBase === null || cfg.problems.length) {
            throw new ApiError("config", { message: "The app is not set up correctly." });
        }
        if (global.navigator && global.navigator.onLine === false) throw new ApiError("network", {});

        var extra = Object.assign({}, opts.headers || {});
        var body;
        var staticHeaders = {};
        body = buildBody(opts, staticHeaders);

        var token = null;
        if (authMode !== "none") {
            token = await currentToken(false);
            if (!token && authMode === "required") throw authRequiredError();
        }

        var timeoutMs = opts.timeoutMs !== undefined ? opts.timeoutMs : (opts.stream ? DEFAULTS.streamHeadersTimeoutMs : DEFAULTS.timeoutMs);
        // Only GET/HEAD, or a call the CALLER marked idempotent, may ever be repeated automatically.
        // An explicit `retries` can lower or raise the count but can NOT make an unsafe call retryable.
        var canRetry = isIdempotentMethod(method) || opts.idempotent === true;
        var maxRetries = opts.retries !== undefined ? opts.retries : (isIdempotentMethod(method) ? DEFAULTS.getRetries : 1);
        var retryBaseMs = typeof opts.retryBaseMs === "number" ? opts.retryBaseMs : DEFAULTS.retryBaseMs;
        var attempt = 0, refreshedOnce = false;

        for (;;) {
            var headers = Object.assign({ "Accept": parse === "json" ? "application/json" : "*/*" }, extra, staticHeaders);
            if (token) headers["Authorization"] = "Bearer " + token;
            if (cfg.clientVersion) headers[HEADER_CLIENT_VERSION] = cfg.clientVersion;

            var abort = makeAbort(opts.signal, timeoutMs);
            var response;
            try {
                response = await fetch(cfg.apiBase + path, {
                    method: method, headers: headers, body: body, signal: abort.signal,
                    cache: "no-store", credentials: "omit"
                });
            } catch (e) {
                var timedOut = abort.timedOut(), userAborted = abort.userAborted();
                abort.cleanup();
                if (userAborted) throw new ApiError("aborted", {});
                var failure = timedOut ? new ApiError("timeout", {}) : new ApiError("network", {});
                if (canRetry && attempt < maxRetries) { attempt++; await sleep(backoff(attempt, null, retryBaseMs)); continue; }
                throw failure;
            }
            abort.stopTimer();   // headers arrived: the "until the response starts" timer is done

            var requestId = readRequestId(response);

            if (response.status === 401 && authMode !== "none") {
                abort.cleanup();
                try { await response.text(); } catch (e) { /* drain */ }
                if (!refreshedOnce) {
                    refreshedOnce = true;
                    var fresh = await refreshTokenOnce();
                    if (fresh && fresh !== token) { token = fresh; continue; }   // 401 = rejected before any work ran, so one retry is safe
                }
                throw raiseAuthExpired(requestId);
            }

            if (!response.ok) {
                abort.cleanup();
                var errBody = await readErrorBody(response);
                var info = readDetail(errBody);
                var retryAfter = parseRetryAfter(headerValue(response, "Retry-After"));

                if (response.status === 400 && info.code === ERROR_UPDATE_REQUIRED) {
                    var upd = new ApiError("update_required", {
                        status: 400, code: ERROR_UPDATE_REQUIRED, detail: info.detail, body: errBody, requestId: requestId,
                        message: info.message || "Please update the app to continue."
                    });
                    if (!state.updateFired) { state.updateFired = true; safeCall(state.onUpdateRequired, upd); }
                    throw upd;
                }
                if (canRetry && attempt < maxRetries && (response.status === 502 || response.status === 503 || response.status === 504) &&
                    (retryAfter === null || retryAfter * 1000 <= DEFAULTS.retryMaxWaitMs)) {
                    attempt++; await sleep(backoff(attempt, retryAfter, retryBaseMs)); continue;
                }
                throw new ApiError("http", {
                    status: response.status, code: info.code, detail: info.detail, body: errBody,
                    retryAfterSeconds: retryAfter, requestId: requestId, message: info.message
                });
            }

            state.authExpiredFired = false;       // a successful authenticated call ends the "expired" episode
            try {
                if (opts.stream) {
                    // The caller reads response.body. Its signal stays attached, so aborting it cancels the stream.
                    return { status: response.status, data: null, requestId: requestId, response: response };
                }
                var data = await parseSuccess(response, parse);
                return { status: response.status, data: data, requestId: requestId, response: parse === "response" ? response : null };
            } catch (e) {
                if (isApiError(e)) throw e;
                if (abort.userAborted()) throw new ApiError("aborted", {});
                throw new ApiError("network", { requestId: requestId });
            } finally {
                if (!opts.stream) abort.cleanup();
            }
        }
    }

    function backoff(attempt, retryAfterSeconds, baseMs) {
        if (retryAfterSeconds !== null && retryAfterSeconds !== undefined) return Math.min(retryAfterSeconds * 1000, DEFAULTS.retryMaxWaitMs);
        var exp = baseMs * Math.pow(2, attempt - 1);
        return Math.min(exp + Math.floor(Math.random() * Math.max(1, baseMs / 2)), DEFAULTS.retryMaxWaitMs);
    }

    /* ----------------------------------------------------------- conveniences */

    function withMethod(method) { return function (path, opts) { return request(path, Object.assign({}, opts || {}, { method: method })); }; }
    function upload(path, formData, opts) {
        return request(path, Object.assign({ timeoutMs: DEFAULTS.uploadTimeoutMs }, opts || {}, { method: "POST", formData: formData }));
    }
    function stream(path, opts) {
        return request(path, Object.assign({ method: "POST" }, opts || {}, { stream: true }));
    }

    /* ------------------------------------------------------------ app config */

    function normalizeAppConfig(data) {
        if (!data || typeof data !== "object") return null;
        var android = data.android;
        if (!android || typeof android !== "object" || !parseVersion(android.min_version)) return null;
        var latest = android.latest_version === null || android.latest_version === undefined ? null
            : (parseVersion(android.latest_version) ? android.latest_version : null);
        return {
            apiVersion: typeof data.api_version === "number" ? data.api_version : null,
            apiVersionSupported: data.api_version === SUPPORTED_API_VERSION,
            android: { minVersion: android.min_version, latestVersion: latest }
        };
    }

    // GET /api/app-config (public, secret-free). Cached; never throws on a bad shape - returns null.
    async function getAppConfig(options) {
        options = options || {};
        var now = Date.now();
        if (!options.force && state.appConfig && now - state.appConfigAt < DEFAULTS.appConfigTtlMs) return state.appConfig;
        var res = await request("/api/app-config", { auth: "none", timeoutMs: 8000, retries: 1, signal: options.signal });
        var normalized = normalizeAppConfig(res.data);
        if (!normalized) return null;
        state.appConfig = normalized; state.appConfigAt = now;
        return normalized;
    }

    // Fails OPEN like the server gate: if the policy can not be read, nobody is locked out.
    async function checkForUpdate(options) {
        var result = { updateRequired: false, updateAvailable: false, minVersion: null, latestVersion: null, checked: false };
        var mine = config().clientVersion;
        var parts = mine ? mine.split("/") : null;
        if (!parts || parts[0] !== "android") return result;
        var policy;
        try { policy = await getAppConfig(options); } catch (e) { return result; }
        if (!policy) return result;
        result.checked = true;
        result.minVersion = policy.android.minVersion;
        result.latestVersion = policy.android.latestVersion;
        result.updateRequired = compareVersions(parts[1], policy.android.minVersion) === -1;
        result.updateAvailable = policy.android.latestVersion ? compareVersions(parts[1], policy.android.latestVersion) === -1 : false;
        return result;
    }

    /* ---------------------------------------------------------------- export */

    var api = {
        moduleVersion: MODULE_VERSION,
        configure: configure,
        config: config,
        publicUrl: publicUrl,
        request: request,
        get: withMethod("GET"),
        post: withMethod("POST"),
        put: withMethod("PUT"),
        patch: withMethod("PATCH"),
        del: withMethod("DELETE"),
        upload: upload,
        stream: stream,
        getAppConfig: getAppConfig,
        checkForUpdate: checkForUpdate,
        ApiError: ApiError,
        isApiError: isApiError,
        compareVersions: compareVersions,
        parseVersion: parseVersion,
        resolveConfig: resolveConfig,
        constants: {
            HEADER_CLIENT_VERSION: HEADER_CLIENT_VERSION,
            HEADER_REQUEST_ID: HEADER_REQUEST_ID,
            ERROR_UPDATE_REQUIRED: ERROR_UPDATE_REQUIRED,
            SUPPORTED_API_VERSION: SUPPORTED_API_VERSION,
            DEFAULTS: Object.freeze(Object.assign({}, DEFAULTS))
        },
        _resetForTests: resetForTests
    };

    global.KognitApi = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : (typeof window !== "undefined" ? window : this));