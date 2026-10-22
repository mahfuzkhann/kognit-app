# Kognit frontend tests (Phase 8 & 9)

Behaviour tests for Kognit's frontend interaction layer. All eleven suites
load the **real** `templates/index.html` and the **real** source files
into jsdom — there is no rebuilt fixture markup, so if the template or a
source file drifts from what these expect, they fail.

## Run

```bash
cd tests_frontend
npm install      # installs jsdom, marked, mathjax, fake-indexeddb and puppeteer-core (test-only); no build step, no bundler,
                 # and NO browser download (puppeteer-core drives a browser you already have)
npm test         # runs all thirteen suites (the four real-browser ones print SKIPPED if no Chrome/Edge is found)
npm run test:jsdom    # the nine jsdom suites only
npm run test:browser  # the four real-browser suites only (Pocket Notes; Stop generating + disclaimer; UI refinement; Quizzes)
```

Expected: `38 passed` (ui-core) + `57 passed` (stream-render) + `14 passed` (theme) +
`52 passed` (nav-views) + `62 passed` (security-hardening) + `91 passed` (notes, jsdom) +
`45 passed` (stop-generating, jsdom) + `80 passed` (profile-form, jsdom) + `38 passed` (profile-menu, jsdom) + `10 passed` (notes-browser, real Chrome) +
`18 passed` (stop-generating-browser, real Chrome) + `38 passed` (ui-refinement-browser, real Chrome) +
`14 passed` (quizzes-browser, real Chrome) = **557 passed, 0 failed**. Without a browser the four real-browser suites print `SKIPPED` and the total is 477. The older "120" figure that used to be printed here
pre-dates the P1 hardening suites and is no longer accurate.

## What each suite covers

`test_ui_core.mjs` (Phase 8C/8D/8G) — modal focus trap/restore, Escape
semantics (including the two deliberately non-dismissible cases: an
in-progress quiz and first-run onboarding), the logout confirmation gate,
the mobile drawer state machine, the empty-state layout toggle, and the
Phase 9B script-load-order and nav-rail-markup contract.

`test_stream_render.mjs` (Phase 8E/9C) — the safe incremental-rendering
boundary scanner (unterminated `$$`, code fences, emphasis, inline code,
LaTeX delimiters, HTML comments), Bengali + LaTeX mixing, the
monotonicity guarantee that the render boundary never moves backwards,
NDJSON parsing across split chunks and malformed lines, loading-copy
honesty, and the Key Takeaway marker extraction (Decision 6).

`test_theme.mjs` (Phase 9A) — the flash-prevention inline script resolves
the correct theme before any external script runs, an explicit choice
always overrides the OS preference, the toggle button's icon/label/
aria-pressed state, persistence to localStorage, and graceful
degradation when storage is unavailable.

`test_nav_views.mjs` (Phase 9B) — the Chat/Quizzes/Projects nav rail's
ARIA tablist semantics and keyboard behaviour, that Quizzes and Projects
reuse existing functionality rather than duplicating it (the quiz modal,
the snapshot endpoint, `createNewProject`/`deleteProject`/
`startRenamingProject`), that no metadata is invented for projects that
don't have it, that the learning-profile trigger and theme toggle stay
reachable from every tab, mobile-drawer integration, and an explicit XSS
test proving project titles are escaped before reaching `innerHTML`.

`test_notes.mjs` (Pocket Notes) — unlike the suites above this one boots the
**real** `app.js`, `notes-store.js`, `notes.js` and `nav-views.js` together,
with the real vendored DOMPurify and the real `marked`. Only Supabase, `fetch`
(an in-memory notes API that mirrors `backend/main.py`) and IndexedDB
(`fake-indexeddb`, a test-only dev dependency) are stubbed. It covers: the Save
button (completed answers only, duplicate-save prevention, retry reusing the same
idempotency key, offline/auth/error states), the notes view (search, tag filter,
safe Markdown + XSS, plain-text fallback, tag editing, delete, error/empty/loading
states, accessibility semantics), flashcards and the self-graded mini-quiz
(including that grading sends no request and never touches the quiz endpoints),
the IndexedDB cache (per-user scoping, server-wins refresh, stale-entry removal,
sign-out wipe, quota/unavailable storage, partial listings never replacing the
cache), offline reading and the honesty of the "available offline" claim, template
wiring (pinned supabase-js, script order, precache list vs real files), and the
service worker's *logic* run in isolation against a fake Cache Storage (install
all-or-nothing, cache policy, never intercepting `/api/*`, versioned cleanup).
Waiting is settle-based (no in-flight request and a quiet DOM) rather than fixed
sleeps, so the suite does not depend on machine speed.

**What neither Pocket Notes suite proves:** real-browser *service-worker* behaviour
(registration, scope, cold-start offline, update flow, Supabase session refresh
while offline). `test_notes_browser.mjs` deliberately bypasses the service worker; see
the manual steps in the Pocket Notes delivery notes.

`test_notes_browser.mjs` (Pocket Notes, REAL BROWSER) - jsdom has no layout engine, so it cannot
see a rendering bug like "giant white glyphs". This suite drives a real Chrome/Chromium/Edge
(puppeteer-core) against the real `index.html`, `app.js`, `notes.js` and the real MathJax `tex-svg`
and `marked` bundles from `node_modules` (no network needed); only Supabase and the notes API are
faked. It pins the root cause of the flashcard / mini-quiz bug: MathJax MEASURES the element it
typesets, so typesetting a detached or `display:none` element yields
`<svg width="NaNex" height="NaNex">`, which the browser stretches to its container (enormous
shapes). The suite asserts, with real layout: no NaN sizes, math is text-sized, options stay inside
their question box, no horizontal overflow at desktop and 390px widths, Bangla + math + very long
tokens, keyboard selection keeps focus, quiz clicks never rebuild the note body, regenerate/reopen
never duplicates components or handlers, a re-render while the Notes tab is hidden leaves no NaN
math, and generated HTML is shown as literal text. Browser discovery: set `CHROME_PATH` to a
Chrome/Chromium/Edge executable (Windows/macOS/Linux install locations are tried automatically);
optional `CHROME_ARGS` adds launch flags. No browser found => it prints SKIPPED and exits 0
(loudly, never as a silent pass).

`test_stop_generating.mjs` (Stop generating + composer disclaimer, jsdom) - boots the real `app.js`
against a fake streaming `fetch` that honours `AbortSignal` the way a browser does (a pending
`reader.read()` rejects with `AbortError`). Covers: the Send/Stop swap and its accessible name; abort
of the request (before headers, before any text, mid-stream); the partial answer being kept, saved with
`stopped`+`interrupted`, restored after reopening the chat and never offered to Pocket Notes; repeated
Stop clicks; late chunks/`done` after Stop being ignored; Stop never submitting typed text; Enter ignored
while generating; a new question working after Stop; genuine failures (network, mid-stream error,
dropped connection, 401/429) keeping their existing behaviour and never being mislabelled as "stopped";
per-chat generations when switching chats; focus hand-off; the cut-inside-takeaway-marker helper; and the
disclaimer's exact text, placement and CSS contract.

`test_stop_generating_browser.mjs` (REAL BROWSER) - jsdom has no network stack and no layout engine.
This suite drives a real Chrome/Chromium/Edge against a real `http.Server` that holds a slow NDJSON
stream open, and asserts the SERVER observes the client's connection close mid-stream when Stop is
pressed (genuine cancellation), that keyboard focus lands back in the input, that Stop occupies exactly
Send's slot, and - with real CSS - that the disclaimer sits under the composer, centred, overlapping
nothing, with WCAG AA contrast (measured from computed colours) in both themes, without horizontal
overflow at 390px and 320px. Same browser discovery rules as the Pocket Notes browser test.

**What neither Stop-generating suite proves:** the real FastAPI backend's behaviour on disconnect.
That was probed separately (real uvicorn + `chat_stream_endpoint`, provider replaced): the provider
generator is closed shortly after the client drops, but uvicorn logs an `ERROR: Exception in ASGI
application ... ClientDisconnected` traceback each time.

`test_ui_refinement_browser.mjs` (sidebar / scrollbars / focus, REAL BROWSER) - layout facts that
jsdom cannot see. Runs at 1280x585 @1.5x (a 1080p laptop at 150% scaling, where the defects were
reproduced), in dark and light: Recent Chats shows >= 4 complete rows; scrollbars are slim with no
arrow buttons and a >= 3:1 thumb (Chromium ignores `::-webkit-scrollbar` if `scrollbar-width` is set,
so that is asserted too); rows align with the nav; the 4px keyboard focus ring is not clipped by the
list scroller on any tab stop; row action buttons are >= 24x24. Guards what must not change: tall-window
spacing, phone drawer sizes, independent wheel/keyboard scrolling (list vs conversation vs page vs
composer), navigation / search / new chat / select / rename / delete / composer / theme toggle, and
`prefers-reduced-motion`. Headless Chrome hides scrollbars by default; the test removes that flag.

`test_profile_form.mjs` (Student Profile form, jsdom) - the REAL template and REAL `app.js` against a
fake `/api/profile`. Seven individual classes (Class 6-12) as single-choice radio cards; the stream
choice shown only for Classes 9-12 and cleared when leaving them; the exact class/stream in the PUT
body (never a grouped band); name/class/stream validation; failed and duplicate saves; success
announced only after the server confirms; legacy grouped profiles open with no class pre-selected and
are never mapped; first-run onboarding; radiogroup/label/live-region semantics; a scope guard that no
Email / Preferred Language / Learning Goals field exists; and a CSS contract (teal accent defined locally,
no blue/indigo, responsive grid, no layout shift). It cannot prove real layout or colour - that needs a
browser.

## What is NOT covered

`app.js` itself is stubbed in the first four suites (it needs a live Supabase
session and Gemini key to boot) — each suite provides fakes matching the
exact function signatures app.js actually exposes, and asserts on the
*delegation* (e.g. that confirming logout calls `handleLogout` exactly
once) rather than on those functions' own bodies, which the backend test
suite and manual QA cover instead.

Also not covered: anything requiring real layout or rendered pixels.
jsdom computes no styles and no layout, so contrast, spacing, and "does
this actually look right" still need a browser — see the Phase 9 report's
Visual QA section for exactly what remains unverified for that reason.

`test_quizzes_browser.mjs` (Quizzes fix) — real Chrome + the real MathJax bundle. Pins the
three Quizzes bugs: a signed-in student must never see the login message (the fake
Supabase goes through the real `supabase.createClient` -> `const supabaseClient` path, which
is NOT a `window` property; a stub that sets `window.supabaseClient` hides this bug), quiz
questions/answers/results containing LaTeX (including a bare `\begin{bmatrix}` and Bengali text)
render as math and stay safe against hostile HTML, and the Quizzes UI uses white/neutral
accents (dark and light theme, hover/focus/selected/results) without touching the shared
indigo token other sections use. `test_nav_views.mjs` additionally covers the Your Topics
states (loading, logged out, error + retry, empty, populated, refresh after a saved quiz).