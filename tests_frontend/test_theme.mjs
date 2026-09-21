/* ============================================================
   KOGNIT PHASE 9A — THEME SYSTEM TESTS
   ============================================================
   Loads the REAL template (including its inline flash-prevention
   head script) and the REAL static/js/theme.js in jsdom.

   Run: node tests_frontend/test_theme.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

let passed = 0, failed = 0;
const failures = [];

async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; failures.push({ name, e }); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (a !== b) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }

function buildDom({ prefersLight = false, storedTheme = null } = {}) {
    let html = fs.readFileSync(path.join(ROOT, "templates", "index.html"), "utf8");
    html = html.replace(/<script src="https:\/\/[^"]*"[^>]*><\/script>/g, "");
    for (const f of ["app.js", "ui-core.js", "stream-render.js", "nav-views.js", "theme.js"]) {
        html = html.replace(new RegExp(`<script src="/static/js/${f}"></script>`), "");
    }

    // CRITICAL: the inline flash-prevention script in <head> runs
    // SYNCHRONOUSLY during JSDOM's HTML parsing, inside this constructor
    // call - by the time `new JSDOM()` returns, it has already read
    // localStorage and matchMedia and set data-theme. So both must be
    // seeded via `beforeParse`, which JSDOM guarantees runs before any
    // document content (including that inline script) is parsed. Seeding
    // them AFTER construction, as a naive test would, always observes the
    // real jsdom defaults instead of the scenario under test.
    const dom = new JSDOM(html, {
        runScripts: "dangerously",
        pretendToBeVisual: true,
        url: "https://kognit.test/",
        beforeParse(window) {
            if (storedTheme) {
                window.localStorage.setItem("kognit-theme", storedTheme);
            }
            window.matchMedia = (query) => ({
                matches: query.includes("light") ? prefersLight : !prefersLight,
                media: query,
                addEventListener: () => {},
                removeEventListener: () => {},
                addListener: () => {},
                removeListener: () => {}
            });
        }
    });
    const { window } = dom;

    const themeJs = fs.readFileSync(path.join(ROOT, "static", "js", "theme.js"), "utf8");
    const script = window.document.createElement("script");
    script.textContent = themeJs;
    window.document.body.appendChild(script);

    return { dom, window, doc: window.document };
}

async function ready(window) {
    if (window.document.readyState === "loading") {
        await new Promise((resolve) =>
            window.document.addEventListener("DOMContentLoaded", resolve, { once: true })
        );
    }
}

console.log("\nFlash-prevention: theme resolved BEFORE any external script");

await check("with no saved theme and OS=dark, <html> gets data-theme=dark immediately", async () => {
    const { doc, window } = buildDom({ prefersLight: false, storedTheme: null }); await ready(window); await ready(window);
    eq(doc.documentElement.getAttribute("data-theme"), "dark");
});

await check("with no saved theme and OS=light, <html> gets data-theme=light immediately", async () => {
    const { doc, window } = buildDom({ prefersLight: true, storedTheme: null }); await ready(window); await ready(window);
    eq(doc.documentElement.getAttribute("data-theme"), "light");
});

await check("a saved theme overrides the OS preference", async () => {
    const { doc, window } = buildDom({ prefersLight: true, storedTheme: "dark" }); await ready(window); await ready(window);
    eq(doc.documentElement.getAttribute("data-theme"), "dark",
        "explicit dark choice must win even though the OS prefers light");
});

await check("window.__kognitResolveTheme is exposed for theme.js to reuse", async () => {
    const { window } = buildDom({ prefersLight: false }); await ready(window);
    eq(typeof window.__kognitResolveTheme, "function");
});

console.log("\nToggle button");

await check("toggle button reflects the resolved theme on load", async () => {
    const { doc, window } = buildDom({ storedTheme: "light" }); await ready(window); await ready(window);
    const btn = doc.getElementById("theme-toggle-btn");
    eq(btn.getAttribute("aria-pressed"), "true");
});

await check("clicking the toggle flips the theme", async () => {
    const { doc, window } = buildDom({ storedTheme: "dark" }); await ready(window); await ready(window);
    const btn = doc.getElementById("theme-toggle-btn");
    btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(doc.documentElement.getAttribute("data-theme"), "light");
    eq(btn.getAttribute("aria-pressed"), "true");
});

await check("clicking twice returns to the original theme", async () => {
    const { doc, window } = buildDom({ storedTheme: "dark" }); await ready(window); await ready(window);
    const btn = doc.getElementById("theme-toggle-btn");
    btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    btn.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(doc.documentElement.getAttribute("data-theme"), "dark");
});

await check("toggling persists the explicit choice to localStorage", async () => {
    const { doc, window } = buildDom({ storedTheme: "dark" }); await ready(window); await ready(window);
    doc.getElementById("theme-toggle-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(window.localStorage.getItem("kognit-theme"), "light");
});

await check("sun/moon icon visibility matches the active theme", async () => {
    const { doc, window } = buildDom({ storedTheme: "light" }); await ready(window); await ready(window);
    const sun = doc.querySelector(".theme-icon-sun");
    const moon = doc.querySelector(".theme-icon-moon");
    assert(!sun.classList.contains("hidden"), "sun should show in light theme");
    assert(moon.classList.contains("hidden"), "moon should hide in light theme");
});

await check("accessible label describes the ACTION, not the current state", async () => {
    const { doc, window } = buildDom({ storedTheme: "light" }); await ready(window); await ready(window);
    const btn = doc.getElementById("theme-toggle-btn");
    // In light theme, clicking switches TO dark - the label should say so.
    assert(btn.getAttribute("aria-label").toLowerCase().includes("dark"));
});

console.log("\nKognitTheme public API");

await check("KognitTheme.current() matches the DOM attribute", async () => {
    const { window } = buildDom({ storedTheme: "light" }); await ready(window);
    eq(window.KognitTheme.current(), "light");
});

await check("KognitTheme.set() applies and persists", async () => {
    const { window, doc } = buildDom({ storedTheme: "dark" });
    window.KognitTheme.set("light");
    eq(doc.documentElement.getAttribute("data-theme"), "light");
    eq(window.localStorage.getItem("kognit-theme"), "light");
});

await check("KognitTheme.hasExplicitChoice() is false until a choice is made", async () => {
    const { window } = buildDom({ storedTheme: null }); await ready(window);
    eq(window.KognitTheme.hasExplicitChoice(), false);
    window.KognitTheme.set("dark");
    eq(window.KognitTheme.hasExplicitChoice(), true);
});

console.log("\nGraceful degradation");

await check("a broken localStorage.getItem does not crash the page", async () => {
    const { window, doc } = buildDom({ prefersLight: false });
    // Simulate storage being unavailable (private browsing in some
    // browsers throws on access) AFTER initial load, then toggle.
    const original = window.localStorage.setItem.bind(window.localStorage);
    window.localStorage.setItem = () => { throw new Error("storage disabled"); };
    let threw = false;
    try {
        window.KognitTheme.set("light");
    } catch (e) {
        threw = true;
    }
    assert(!threw, "theme.js must catch storage failures, not propagate them");
    eq(doc.documentElement.getAttribute("data-theme"), "light",
        "the toggle should still work for this page view even if it can't be saved");
    window.localStorage.setItem = original;
});

console.log(`\n${"=".repeat(52)}`);
console.log(`  ${passed} passed, ${failed} failed`);
console.log("=".repeat(52));
if (failed) {
    for (const f of failures) console.log(`\n${f.name}\n  ${f.e.stack}`);
    process.exit(1);
}
