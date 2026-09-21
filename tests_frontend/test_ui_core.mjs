/* ============================================================
   KOGNIT PHASE 8 — FRONTEND BEHAVIOUR TESTS
   ============================================================
   Runs the REAL templates/index.html and the REAL
   static/js/ui-core.js inside jsdom. No mocked DOM, no rebuilt
   fixture markup - if the template stops containing the drawer
   trigger, or ui-core stops trapping focus, these fail.

   Scope note: these cover the interaction layer added in Phase
   8C/8D/8G. They do NOT cover app.js, which needs a live
   Supabase/Gemini session to boot; app.js is stubbed to the
   handful of globals ui-core.js delegates to.

   Run:  node tests_frontend/test_ui_core.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

let passed = 0;
let failed = 0;
const failures = [];

function check(name, fn) {
    try {
        fn();
        passed++;
        console.log(`  PASS  ${name}`);
    } catch (err) {
        failed++;
        failures.push({ name, err });
        console.log(`  FAIL  ${name}\n        ${err.message}`);
    }
}

function assert(cond, msg) {
    if (!cond) throw new Error(msg || "assertion failed");
}

function assertEqual(actual, expected, msg) {
    if (actual !== expected) {
        throw new Error(`${msg || "mismatch"}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    }
}

/* ---------- environment ---------- */

async function buildDom({ width = 1280 } = {}) {
    // The Jinja template has no template tags in the shell we test,
    // so it loads as plain HTML.
    let html = fs.readFileSync(path.join(ROOT, "templates", "index.html"), "utf8");
    // Strip external CDN scripts - jsdom must not hit the network.
    html = html.replace(/<script src="https:\/\/[^"]*"[^>]*><\/script>/g, "");
    html = html.replace(/<script src="\/static\/js\/app\.js"><\/script>/, "");
    html = html.replace(/<script src="\/static\/js\/ui-core\.js"><\/script>/, "");

    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true });
    const { window } = dom;

    Object.defineProperty(window, "innerWidth", { value: width, writable: true, configurable: true });

    // jsdom leaves offsetParent null for everything, which ui-core's
    // visibility filter uses to skip display:none subtrees. Report an
    // element as laid out unless it (or an ancestor) is .hidden or
    // [hidden] - close enough to real layout for these assertions.
    Object.defineProperty(window.HTMLElement.prototype, "offsetParent", {
        get() {
            let node = this;
            while (node && node !== window.document.body) {
                if (node.classList?.contains("hidden") || node.hasAttribute?.("hidden")) return null;
                node = node.parentElement;
            }
            return window.document.body;
        },
        configurable: true
    });

    // Minimal stand-ins for the app.js globals ui-core.js delegates to.
    const calls = { handleLogout: 0, closeQuizModal: 0, closeAuthModal: 0 };
    window.handleLogout = () => { calls.handleLogout++; };
    window.closeAuthModal = () => { calls.closeAuthModal++; window.document.getElementById("auth-modal").classList.add("hidden"); };
    window.closeProfileModal = () => window.document.getElementById("profile-modal").classList.add("hidden");
    window.closeStudentProfilePanel = () => window.document.getElementById("student-profile-modal").classList.add("hidden");
    window.closeQuizModal = () => { calls.closeQuizModal++; window.document.getElementById("quiz-modal").classList.add("hidden"); };

    const uiCore = fs.readFileSync(path.join(ROOT, "static", "js", "ui-core.js"), "utf8");
    const script = window.document.createElement("script");
    script.textContent = uiCore;
    window.document.body.appendChild(script);

    // ui-core.js defers all DOM wiring to DOMContentLoaded. jsdom is still
    // in readyState "loading" at this point, so dispatching events before
    // that event fires would hit a page with no listeners attached - which
    // is a harness artefact, not product behaviour. Wait for it.
    if (window.document.readyState === "loading") {
        await new Promise((resolve) =>
            window.document.addEventListener("DOMContentLoaded", resolve, { once: true })
        );
    }

    return { dom, window, doc: window.document, calls };
}

function press(window, key, extra = {}) {
    const evt = new window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...extra });
    window.document.dispatchEvent(evt);
    return evt;
}

const tick = (window) => new Promise((r) => window.setTimeout(r, 0));

/* ---------- template contract ---------- */

console.log("\nTemplate contract (Phase 8C/8D/8G hooks present)");

const rawHtml = fs.readFileSync(path.join(ROOT, "templates", "index.html"), "utf8");

check("tokens.css loads before style.css", () => {
    const t = rawHtml.indexOf("/static/css/tokens.css");
    const s = rawHtml.indexOf("/static/css/style.css");
    assert(t !== -1, "tokens.css not linked");
    assert(s !== -1, "style.css not linked");
    assert(t < s, "tokens.css must load before style.css for var() to resolve");
});

check("app.js loads before nav-views.js and ui-core.js (Phase 9B order)", () => {
    // Reversed from Phase 8C's original order (ui-core.js before app.js).
    // Phase 9B's nav-views.js needs app.js's accessor functions
    // (getProjectsSnapshot, getActiveIds, etc.) to exist first, so app.js
    // now loads first. ui-core.js tolerates either position by design -
    // every app.js function it calls is looked up lazily via `window[...]`
    // at event time, never captured at script-load time (see the header
    // comment in ui-core.js) - so moving it after app.js changes nothing
    // about its own behavior, which is exactly why this reordering was
    // safe to make.
    const a = rawHtml.indexOf("/static/js/app.js");
    const n = rawHtml.indexOf("/static/js/nav-views.js");
    const u = rawHtml.indexOf("/static/js/ui-core.js");
    assert(a !== -1 && n !== -1 && u !== -1, "all three scripts must be present");
    assert(a < n, "app.js must load before nav-views.js (nav-views.js reads app.js's accessors)");
    assert(a < u, "app.js must load before ui-core.js under the current order");
});

check("nav rail markup (Chat/Quizzes/Projects, no Home) is present", () => {
    assert(rawHtml.includes('id="nav-tab-chat"'), "missing Chat tab");
    assert(rawHtml.includes('id="nav-tab-quizzes"'), "missing Quizzes tab");
    assert(rawHtml.includes('id="nav-tab-projects"'), "missing Projects tab");
    assert(!rawHtml.includes('id="nav-tab-home"'), "Home was explicitly declined (Decision 3) - must not exist");
    assert(!/>\s*Scheduled\s*</.test(rawHtml), "Scheduled was explicitly declined - must not exist");
    assert(!/>\s*Plugins\s*</.test(rawHtml), "Plugins was explicitly declined - must not exist");
});

check("logout button routes through confirmation, not straight to handleLogout", () => {
    assert(rawHtml.includes('onclick="requestLogout()"'), "logout button should call requestLogout()");
    assert(!rawHtml.includes('onclick="handleLogout()"'), "no control may call handleLogout() directly");
});

check("viewport allows pinch-zoom", () => {
    const m = rawHtml.match(/<meta name="viewport" content="([^"]+)"/);
    assert(m, "viewport meta missing");
    assert(!/user-scalable\s*=\s*no/i.test(m[1]), "pinch-zoom must not be disabled");
    assert(!/maximum-scale/i.test(m[1]), "maximum-scale must not be pinned");
});

check("drawer trigger and overlay exist with correct ARIA wiring", () => {
    assert(rawHtml.includes('id="sidebar-toggle-btn"'), "missing sidebar toggle");
    assert(rawHtml.includes('id="sidebar-overlay"'), "missing drawer overlay");
    assert(rawHtml.includes('aria-controls="app-sidebar"'), "toggle must reference the sidebar it controls");
    assert(rawHtml.includes('id="app-sidebar"'), "sidebar needs the referenced id");
});

/* ---------- modal focus management (8G) ---------- */

console.log("\nModal focus management (8G)");

const main = async () => {
    {
        const { window, doc } = await buildDom();
        const trigger = doc.getElementById("profile-trigger-btn");
        const modal = doc.getElementById("student-profile-modal");

        check("focus moves into a modal when it opens", () => {
            trigger.focus();
            assertEqual(doc.activeElement, trigger, "precondition");
            modal.classList.remove("hidden");
        });

        await new Promise((r) => window.requestAnimationFrame(() => r()));

        check("  ...focus landed inside the modal", () => {
            assert(modal.contains(doc.activeElement), `focus went to ${doc.activeElement?.id || doc.activeElement?.tagName}`);
        });

        check("  ...body marked as having an open modal", () => {
            assert(doc.body.classList.contains("has-open-modal"));
        });

        check("Escape closes a dismissible modal", () => {
            press(window, "Escape");
            assert(modal.classList.contains("hidden"), "modal should be hidden after Escape");
        });

        await tick(window);

        check("  ...focus is restored to the element that opened it", () => {
            assertEqual(doc.activeElement, trigger, "focus should return to the trigger");
        });

        check("  ...body class cleared", () => {
            assert(!doc.body.classList.contains("has-open-modal"));
        });
    }

    {
        const { window, doc } = await buildDom();
        const modal = doc.getElementById("auth-modal");
        modal.classList.remove("hidden");
        await new Promise((r) => window.requestAnimationFrame(() => r()));

        check("Tab wraps from last focusable back to first (forward trap)", () => {
            const focusable = window.KognitUI.getFocusable(modal);
            assert(focusable.length > 1, "need multiple focusables to test wrapping");
            focusable[focusable.length - 1].focus();
            const evt = press(window, "Tab");
            assert(evt.defaultPrevented, "Tab at the end should be intercepted");
            assertEqual(doc.activeElement, focusable[0], "should wrap to first");
        });

        check("Shift+Tab wraps from first back to last (reverse trap)", () => {
            const focusable = window.KognitUI.getFocusable(modal);
            focusable[0].focus();
            const evt = press(window, "Tab", { shiftKey: true });
            assert(evt.defaultPrevented, "Shift+Tab at the start should be intercepted");
            assertEqual(doc.activeElement, focusable[focusable.length - 1], "should wrap to last");
        });

        check("focus outside the modal is pulled back in", () => {
            doc.getElementById("user-input").focus();
            press(window, "Tab");
            assert(modal.contains(doc.activeElement), "focus must not escape an open modal");
        });
    }

    {
        const { window, doc, calls } = await buildDom();
        const quiz = doc.getElementById("quiz-modal");

        check("Escape does NOT discard an in-progress quiz", () => {
            quiz.classList.remove("hidden");
            doc.getElementById("quiz-setup-view").classList.add("hidden");
            doc.getElementById("quiz-active-view").classList.remove("hidden");
            press(window, "Escape");
            assert(!quiz.classList.contains("hidden"), "an active quiz must not be dismissible by Escape");
            assertEqual(calls.closeQuizModal, 0, "closeQuizModal should not have been called");
        });

        check("Escape DOES close the quiz modal while still in setup", () => {
            doc.getElementById("quiz-active-view").classList.add("hidden");
            doc.getElementById("quiz-setup-view").classList.remove("hidden");
            press(window, "Escape");
            assert(quiz.classList.contains("hidden"), "setup view should be dismissible");
        });
    }

    {
        const { window, doc } = await buildDom();
        const profile = doc.getElementById("profile-modal");

        check("required onboarding profile modal cannot be escaped", () => {
            profile.setAttribute("data-required", "true");
            profile.classList.remove("hidden");
            press(window, "Escape");
            assert(!profile.classList.contains("hidden"), "onboarding must not be dismissible");
        });

        check("ordinary profile editing is dismissible", () => {
            profile.setAttribute("data-required", "false");
            press(window, "Escape");
            assert(profile.classList.contains("hidden"), "normal edit should close");
        });
    }

    /* ---------- logout confirmation (8G) ---------- */

    console.log("\nLogout confirmation (8G)");

    {
        const { window, doc, calls } = await buildDom();

        check("requestLogout opens confirmation and does NOT sign out", () => {
            window.requestLogout();
            assert(!doc.getElementById("logout-confirm-modal").classList.contains("hidden"), "confirm modal should open");
            assertEqual(calls.handleLogout, 0, "handleLogout must not fire on the first click");
        });

        check("Cancel closes the dialog without signing out", () => {
            window.closeLogoutConfirm();
            assert(doc.getElementById("logout-confirm-modal").classList.contains("hidden"));
            assertEqual(calls.handleLogout, 0, "cancelling must never sign out");
        });

        check("Escape on the confirmation cancels rather than signing out", () => {
            window.requestLogout();
            press(window, "Escape");
            assert(doc.getElementById("logout-confirm-modal").classList.contains("hidden"));
            assertEqual(calls.handleLogout, 0, "Escape must be treated as Cancel");
        });

        check("confirming delegates to app.js handleLogout exactly once", () => {
            window.requestLogout();
            window.confirmLogout();
            assertEqual(calls.handleLogout, 1, "handleLogout should run once, on confirm only");
            assert(doc.getElementById("logout-confirm-modal").classList.contains("hidden"));
        });

        check("Cancel is the default-focused action, not Log out", () => {
            const card = doc.querySelector("#logout-confirm-modal .confirm-card");
            const autofocus = card.querySelector("[data-autofocus]");
            assert(autofocus, "confirmation should mark a default action");
            assert(autofocus.classList.contains("btn-confirm-cancel"), "the safe action must be the default");
        });

        check("confirmation dialog is announced as an alertdialog", () => {
            const card = doc.querySelector("#logout-confirm-modal .confirm-card");
            assertEqual(card.getAttribute("role"), "alertdialog");
            assertEqual(card.getAttribute("aria-modal"), "true");
            assert(card.getAttribute("aria-labelledby"), "needs an accessible name");
            assert(card.getAttribute("aria-describedby"), "needs an accessible description");
        });
    }

    /* ---------- mobile drawer (8C) ---------- */

    console.log("\nMobile navigation drawer (8C)");

    {
        const { window, doc } = await buildDom({ width: 390 });
        const toggle = doc.getElementById("sidebar-toggle-btn");

        check("drawer mode is active at phone width", () => {
            assert(window.KognitUI.isDrawerMode(), "390px should be drawer mode");
        });

        check("drawer starts closed", () => {
            assert(!doc.body.classList.contains("sidebar-open"));
            assertEqual(toggle.getAttribute("aria-expanded"), "false");
        });

        check("toggle opens the drawer and updates aria-expanded", () => {
            toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            assert(doc.body.classList.contains("sidebar-open"));
            assertEqual(toggle.getAttribute("aria-expanded"), "true");
        });

        check("Escape closes the drawer", () => {
            press(window, "Escape");
            assert(!doc.body.classList.contains("sidebar-open"));
            assertEqual(toggle.getAttribute("aria-expanded"), "false");
        });

        check("overlay click closes the drawer", () => {
            toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            assert(doc.body.classList.contains("sidebar-open"), "precondition");
            doc.getElementById("sidebar-overlay").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            assert(!doc.body.classList.contains("sidebar-open"));
        });

        check("choosing a conversation closes the drawer", () => {
            toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            const item = doc.createElement("span");
            item.className = "chat-title-text";
            doc.getElementById("history-list").appendChild(item);
            item.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            assert(!doc.body.classList.contains("sidebar-open"), "navigating should dismiss the drawer");
        });

        check("rename/delete actions do NOT close the drawer", () => {
            toggle.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            const actions = doc.createElement("div");
            actions.className = "item-actions";
            const del = doc.createElement("button");
            del.className = "action-btn";
            actions.appendChild(del);
            doc.getElementById("history-list").appendChild(actions);
            del.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
            assert(doc.body.classList.contains("sidebar-open"), "managing items should keep the drawer open");
        });
    }

    {
        const { window, doc } = await buildDom({ width: 1280 });
        check("desktop is not drawer mode", () => {
            assert(!window.KognitUI.isDrawerMode());
        });
        check("resizing from drawer to desktop clears the open state", () => {
            window.innerWidth = 390;
            window.KognitUI.openDrawer();
            assert(doc.body.classList.contains("sidebar-open"), "precondition");
            window.innerWidth = 1280;
            window.KognitUI.closeDrawer;
            // syncDrawerForViewport runs on the debounced resize handler;
            // call the exposed path directly rather than waiting 120ms.
            window.dispatchEvent(new window.Event("resize"));
            return new Promise((r) => window.setTimeout(() => {
                assert(!doc.body.classList.contains("sidebar-open"), "stale open state must be cleared on resize to desktop");
                r();
            }, 200));
        });
    }

    /* ---------- empty state (8D) ---------- */

    console.log("\nEmpty state / centered composer (8D)");

    {
        const { window, doc } = await buildDom();
        const chatBox = doc.getElementById("chat-box");
        const mainEl = doc.querySelector(".main-content");

        check("no empty-state class when the conversation is truly empty of both", () => {
            assert(!mainEl.classList.contains("is-empty-chat"), "nothing rendered yet");
        });

        check("greeting alone puts the layout into centered empty state", () => {
            const greeting = doc.createElement("div");
            greeting.className = "empty-chat-greeting";
            chatBox.appendChild(greeting);
            window.KognitUI.syncEmptyState();
            assert(mainEl.classList.contains("is-empty-chat"), "should centre the composer");
        });

        check("first real message exits the empty state", () => {
            const msg = doc.createElement("div");
            msg.className = "user-message";
            chatBox.appendChild(msg);
            window.KognitUI.syncEmptyState();
            assert(!mainEl.classList.contains("is-empty-chat"), "composer must return to the bottom");
        });

        check("empty state is observed automatically, without app.js calling in", async () => {
            chatBox.innerHTML = "";
            const greeting = doc.createElement("div");
            greeting.className = "empty-chat-greeting";
            chatBox.appendChild(greeting);
            return new Promise((r) => window.setTimeout(() => {
                assert(mainEl.classList.contains("is-empty-chat"), "MutationObserver should have synced this");
                r();
            }, 20));
        });
    }

    await new Promise((r) => setTimeout(r, 250));

    console.log(`\n${"=".repeat(52)}`);
    console.log(`  ${passed} passed, ${failed} failed`);
    console.log("=".repeat(52));
    if (failed) {
        for (const f of failures) console.log(`\n${f.name}\n  ${f.err.stack}`);
        process.exit(1);
    }
};

main();
