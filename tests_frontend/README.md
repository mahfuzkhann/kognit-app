# Kognit frontend tests (Phase 8 & 9)

Behaviour tests for Kognit's frontend interaction layer. All four suites
load the **real** `templates/index.html` and the **real** source files
into jsdom — there is no rebuilt fixture markup, so if the template or a
source file drifts from what these expect, they fail.

## Run

```bash
cd tests_frontend
npm install      # installs jsdom only; no build step, no bundler
npm test         # runs all four suites
```

Expected: `38 passed` (ui-core) + `39 passed` (stream-render) +
`14 passed` (theme) + `29 passed` (nav-views) = **120 passed, 0 failed**.

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

## What is NOT covered

`app.js` itself is stubbed in all four suites (it needs a live Supabase
session and Gemini key to boot) — each suite provides fakes matching the
exact function signatures app.js actually exposes, and asserts on the
*delegation* (e.g. that confirming logout calls `handleLogout` exactly
once) rather than on those functions' own bodies, which the backend test
suite and manual QA cover instead.

Also not covered: anything requiring real layout or rendered pixels.
jsdom computes no styles and no layout, so contrast, spacing, and "does
this actually look right" still need a browser — see the Phase 9 report's
Visual QA section for exactly what remains unverified for that reason.
