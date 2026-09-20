# Kognit frontend tests (Phase 8)

Behaviour tests for the Phase 8C/8D/8G interaction layer. They load the
**real** `templates/index.html` and the **real** `static/js/ui-core.js`
into jsdom — there is no rebuilt fixture markup, so if the template loses
the drawer trigger or `ui-core.js` stops trapping focus, these fail.

## Run

```bash
cd tests_frontend
npm install      # installs jsdom only; no build step, no bundler
npm test
```

Expected: `37 passed, 0 failed`.

## What is and is not covered

Covered: modal focus trap/restore, Escape semantics (including the two
deliberately non-dismissible cases), the logout confirmation gate, the
mobile drawer state machine, and the empty-state layout toggle.

**Not** covered: `app.js`, which needs a live Supabase session and Gemini
key to boot. The handful of globals `ui-core.js` delegates to are stubbed,
and the tests assert on the delegation (e.g. that confirming logout calls
`handleLogout` exactly once) rather than on `handleLogout`'s own body.

Also not covered: anything requiring real layout. jsdom does not compute
styles, so "is the composer actually centered" and "is contrast
sufficient" still need a browser. See the Phase 8 report's Visual QA
section.
