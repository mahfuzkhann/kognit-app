# Kognit frontend tests (Phase 8 & 9)

Behaviour tests for Kognit's frontend interaction layer. All seven suites
load the **real** `templates/index.html` and the **real** source files
into jsdom — there is no rebuilt fixture markup, so if the template or a
source file drifts from what these expect, they fail.

## Run

```bash
cd tests_frontend
npm install      # installs jsdom, marked, mathjax, fake-indexeddb and puppeteer-core (test-only); no build step, no bundler,
                 # and NO browser download (puppeteer-core drives a browser you already have)
npm test         # runs all seven suites (the real-browser one prints SKIPPED if no Chrome/Edge is found)
npm run test:jsdom    # the six jsdom suites only
npm run test:browser  # the real-browser Pocket Notes test only
```

Expected: `38 passed` (ui-core) + `57 passed` (stream-render) + `14 passed` (theme) +
`33 passed` (nav-views) + `62 passed` (security-hardening) + `91 passed` (notes, jsdom) +
`10 passed` (notes-browser, real Chrome) = **305 passed, 0 failed**. Without a browser the last
suite prints `SKIPPED` and the total is 295. The older "120" figure that used to be printed here
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