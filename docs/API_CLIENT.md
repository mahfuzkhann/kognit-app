# API client contract (`static/js/api.js`)

Status: **written and tested, not yet loaded by any page.** It is wired into `index.html` / `app.js` in the
consolidated frontend batch. Until then the existing `fetch()` calls are unchanged and nothing in the product
behaves differently.

`api.js` is the one place that talks to the backend. It exposes `window.KognitApi`.
Tests: `tests_frontend/test_api_client.mjs` (behaviour) and `test_api_contract.py` (agreement with the backend).

## Inventory of what it replaces (verified in the repository at `10d2085`)

`static/js/app.js` has **13** `fetch` call sites to the API (12 literal `fetch('/api/...')` plus one dynamic
`fetch(url, ...)`), each repeating "get the Supabase session, build an `Authorization` header, parse the error".
`notes.js`, `share.js` and `shared-view.js` have their own small wrappers. None sends a client version, none
reads the request id, and none has a timeout. Migrating them is part of the consolidated `app.js` delivery.

## Configuration

```js
window.KOGNIT_CONFIG = {               // define BEFORE api.js loads; all optional on the website
  apiBase:       "https://app.example.com",   // REQUIRED in the Android app (page origin is https://localhost)
  publicOrigin:  "https://app.example.com",   // share links, password-reset redirects
  clientVersion: "android/1.0.0"              // sent as X-Client-Version
};
KognitApi.configure({
  getAccessToken: async ({ forceRefresh }) => token | null,  // app.js maps this to Supabase
  onAuthExpired:   (err) => {...},     // once per expired-session episode
  onUpdateRequired:(err) => {...}      // once per page load
});
```

| Setting | Website | Android (Capacitor) |
|---|---|---|
| `apiBase` | `""` = same origin | **required**, an `https://` origin (`http://` only for localhost) |
| `publicOrigin` | the page's own origin | **required** to build public URLs; never falls back to `https://localhost` |
| `clientVersion` | not sent | `android/MAJOR.MINOR.PATCH`, sent on every request |

Invalid values are rejected (userinfo, paths, query strings, non-https hosts, malformed versions): the client then
refuses to send requests (`ApiError.kind === "config"`) instead of guessing.

## Wire contract with the backend

| Topic | Rule | Verified by |
|---|---|---|
| Version header | `X-Client-Version: android/1.2.3` (regex identical to `backend/client_access.py`) | `test_api_contract.py` |
| Update required | HTTP **400** AND `detail.code == "APP_UPDATE_REQUIRED"`. The **code** decides; an ordinary 400 is not an update requirement | both test files + the real gate response |
| Request id | **Read** from the response header `X-Request-ID` (exposed by CORS). **Never sent**: the server ignores inbound ids and Android's CORS allow-list is `Authorization, Content-Type, X-Client-Version` only | contract test sends a real preflight with `x-request-id` and sees it rejected |
| Headers sent | only `Authorization`, `Content-Type`, `X-Client-Version`, `Accept` | contract test compares with `CORS_ALLOWED_HEADERS` |
| App config | `GET /api/app-config` -> `{"api_version":1,"android":{"min_version","latest_version"},"features":{"account_deletion":bool}}`, public, no token. `features` are server-side switches (booleans only); the client turns a feature on only for a literal `true` | fixture generated from the backend, compared in both languages |
| Auth | `Authorization: Bearer <JWT>`; credentials are never cookies (`credentials: "omit"`) | tests |
| Errors | `{"detail": "text"}` or `{"detail": {"code","message",...}}`; validation lists are never echoed | tests |

## Behaviour

- **Paths** must start with a single `/`. Absolute URLs, `//host`, backslashes, whitespace are refused before any
  network call, so a bug can never send a student's token to another site. There is exactly one `fetch()` in the file.
- **Auth modes**: `required` (default; no token -> `AUTH_REQUIRED`, no network call), `optional`, `none`.
- **401**: one silent refresh (shared by all concurrent requests), then one retry with the new token (safe: a 401
  means the request was rejected before any work ran). Still 401 -> `ApiError(kind "auth", code "AUTH_EXPIRED")` and
  `onAuthExpired` fires once per episode. No token at all -> `AUTH_REQUIRED` and the hook stays quiet.
- **Retries**: only `GET`/`HEAD` (2 retries on network error, timeout, 502/503/504, exponential backoff). `POST`, `PUT`,
  `PATCH`, `DELETE` are never retried unless the caller passes `idempotent: true` (then once). `429` is never retried.
  A `Retry-After` above 10 s is not waited out.
- **Timeouts**: 30 s default; 120 s for `upload()`; streaming waits up to 120 s for the response to *start* and the
  timer then stops (a long answer is never cut off). The caller's `AbortSignal` (e.g. the Stop button) always wins and
  is reported as `kind "aborted"`, never a timeout. Works on old WebViews (no `AbortSignal.any`).
- **Offline** (`navigator.onLine === false`): fails at once as `kind "network"`.
- **Errors**: one `ApiError` with `kind` (`network|timeout|aborted|auth|update_required|http|parse|config`), `status`, `code`,
  `message` (server-supplied, clipped, student-safe, may be empty), `detail`, `body`, `retryAfterSeconds`, `requestId`,
  and `toUserMessage(fallback)`.
- **Privacy**: no `console` output, no storage access, no logging of tokens, bodies or responses; a token that is not
  JWT-shaped is never put in a header.
- **Update check**: `checkForUpdate()` compares `clientVersion` with `/api/app-config`, numerically (`1.10.0 > 1.2.0`),
  and **fails open** like the server gate: if the policy cannot be read, nobody is locked out.

## Usage

```js
const { data, requestId } = await KognitApi.get("/api/profile");
await KognitApi.post("/api/report", { json: { category: "inaccurate", ai_response: text, request_id: rid } });
const fd = new FormData(); fd.append("file", pdf);
await KognitApi.upload("/api/pdf/upload", fd);
const { response } = await KognitApi.stream("/api/chat/stream", { formData: fd, signal: stopController.signal });
try { ... } catch (e) { if (KognitApi.isApiError(e)) show(e.toUserMessage()); }
```

## Not covered here (later batches)

- Loading `api.js` in `index.html`, caching it in `sw.js`, and replacing the 13 call sites (consolidated `app.js`).
- Real-device checks of CORS preflight, aborts and streaming inside a Capacitor WebView.
- Request-id on every response: today the server attaches `X-Request-ID` to chat responses only (streamed and not), so `requestId` is `null`
  elsewhere. That is a backend decision, not an `api.js` limit.