/* ============================================================
   KOGNIT UI CORE — PHASE 8C / 8D / 8G
   ============================================================
   Shared interaction infrastructure: modal focus management, the
   mobile navigation drawer, the logout confirmation step, and the
   empty-state layout toggle.

   DESIGN DECISION — why this is a separate file that OBSERVES
   app.js rather than editing it:

   app.js contains several pieces of hard-won, non-obvious
   correctness (the CHAT-05 race-condition guard in sendMessage,
   the full logout state-clearing sequence, the Bengali/LaTeX math
   protection pipeline). Every one of those is a fix for a real bug
   that was hit in production. Reaching into those functions to add
   UI concerns is exactly how such fixes get silently reverted.

   So this module attaches from the outside:
     - modal focus trapping via MutationObserver on .modal-overlay,
       so NO existing open/close function needed to change
     - logout confirmation by wrapping window.handleLogout, leaving
       its cleanup body completely untouched
     - empty-state detection by observing #chat-box

   Loaded BEFORE app.js (see templates/index.html). Everything here
   defers its DOM wiring to DOMContentLoaded, so load order only
   matters for the wrapper below, which re-reads window.handleLogout
   lazily at click time rather than capturing it at load time.
   ============================================================ */

(function () {
    "use strict";

    /* ========================================================
       FOCUS MANAGEMENT (Phase 8G)
       ======================================================== */

    var FOCUSABLE = [
        "a[href]",
        "button:not([disabled])",
        "input:not([disabled]):not([type=hidden])",
        "select:not([disabled])",
        "textarea:not([disabled])",
        '[tabindex]:not([tabindex="-1"])'
    ].join(",");

    function getFocusable(container) {
        if (!container) return [];
        return Array.prototype.slice
            .call(container.querySelectorAll(FOCUSABLE))
            .filter(function (el) {
                // offsetParent is null for display:none subtrees. Also
                // skip anything explicitly hidden from assistive tech.
                return el.offsetParent !== null &&
                       el.getAttribute("aria-hidden") !== "true";
            });
    }

    function isVisible(el) {
        return el && !el.classList.contains("hidden");
    }

    // The element that had focus before the topmost layer opened, per
    // layer, so nested cases (drawer -> modal) restore in the right
    // order instead of all restoring to the same element.
    var focusReturnStack = [];

    function captureFocusOrigin() {
        focusReturnStack.push(document.activeElement);
    }

    function restoreFocusOrigin() {
        var origin = focusReturnStack.pop();
        if (origin && typeof origin.focus === "function" &&
            document.contains(origin)) {
            origin.focus();
        }
    }

    function focusFirstIn(container) {
        // Prefer an explicitly marked initial target, then the first
        // natural focusable, then the container itself as a last resort
        // so focus never stays stranded behind the overlay.
        var preferred = container.querySelector("[data-autofocus]");
        var target = preferred || getFocusable(container)[0];
        if (target) {
            target.focus();
            return;
        }
        if (!container.hasAttribute("tabindex")) {
            container.setAttribute("tabindex", "-1");
        }
        container.focus();
    }

    /* ========================================================
       MODAL LAYER
       ======================================================== */

    // Maps each modal's element id to the existing global close
    // function already defined in app.js. Escape uses these so the
    // modal's own teardown (state resets, generation counters) runs
    // exactly as it does when the student clicks the X - we never
    // just hide the element behind app.js's back.
    var MODAL_CLOSERS = {
        "auth-modal": "closeAuthModal",
        "profile-modal": "closeProfileModal",
        "student-profile-modal": "closeStudentProfilePanel",
        "quiz-modal": "closeQuizModal",
        "logout-confirm-modal": "closeLogoutConfirm"
    };

    // Modals that must NOT close on Escape or on overlay click,
    // because dismissing them mid-flow would lose student work or
    // leave the app in a state the student cannot recover from.
    var NON_DISMISSIBLE = {
        // An in-progress quiz: Escape here would discard answers the
        // student has already entered with no way back.
        "quiz-modal": function (modal) {
            var active = modal.querySelector("#quiz-active-view");
            return isVisible(active);
        },
        // First-run profile onboarding: app.js opens this
        // automatically when a logged-in student has no profile yet.
        // Escaping it leaves an account with no class/stream, which
        // every downstream academic-context call depends on.
        "profile-modal": function (modal) {
            return modal.getAttribute("data-required") === "true";
        }
    };

    function isDismissible(modal) {
        var guard = NON_DISMISSIBLE[modal.id];
        if (!guard) return true;
        try {
            return !guard(modal);
        } catch (e) {
            return true;
        }
    }

    function closeModal(modal) {
        var fnName = MODAL_CLOSERS[modal.id];
        var fn = fnName && window[fnName];
        if (typeof fn === "function") {
            fn();
        } else {
            // No registered closer (a modal added later without
            // updating MODAL_CLOSERS) - hide it rather than trapping
            // the student inside it.
            modal.classList.add("hidden");
        }
    }

    function openModalLayer(modal) {
        if (modal.dataset.k8Open === "true") return;
        modal.dataset.k8Open = "true";
        captureFocusOrigin();
        document.body.classList.add("has-open-modal");
        // Defer focus one frame: app.js's own open functions sometimes
        // populate/reset fields immediately after removing .hidden, and
        // focusing before that runs can land on an element it then
        // rewrites.
        window.requestAnimationFrame(function () {
            if (isVisible(modal)) focusFirstIn(modal);
        });
    }

    function closeModalLayer(modal) {
        if (modal.dataset.k8Open !== "true") return;
        delete modal.dataset.k8Open;
        if (!document.querySelector(".modal-overlay[data-k8-open='true']")) {
            document.body.classList.remove("has-open-modal");
        }
        restoreFocusOrigin();
    }

    function topmostOpenModal() {
        var open = Array.prototype.slice
            .call(document.querySelectorAll(".modal-overlay"))
            .filter(isVisible);
        return open.length ? open[open.length - 1] : null;
    }

    function trapTab(event) {
        if (event.key !== "Tab") return;
        var modal = topmostOpenModal();
        if (!modal) return;

        var focusable = getFocusable(modal);
        if (focusable.length === 0) {
            event.preventDefault();
            return;
        }

        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        var active = document.activeElement;

        if (!modal.contains(active)) {
            event.preventDefault();
            first.focus();
            return;
        }
        if (event.shiftKey && active === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && active === last) {
            event.preventDefault();
            first.focus();
        }
    }

    function observeModals() {
        var modals = document.querySelectorAll(".modal-overlay");
        Array.prototype.forEach.call(modals, function (modal) {
            // Sync any modal that is somehow already visible at boot.
            if (isVisible(modal)) openModalLayer(modal);

            new MutationObserver(function () {
                if (isVisible(modal)) {
                    openModalLayer(modal);
                } else {
                    closeModalLayer(modal);
                }
            }).observe(modal, {
                attributes: true,
                attributeFilter: ["class"]
            });

            // Overlay-click dismissal, only on the overlay itself (not
            // on the card inside it) and only when dismissible.
            modal.addEventListener("mousedown", function (event) {
                if (event.target !== modal) return;
                if (!isDismissible(modal)) return;
                closeModal(modal);
            });
        });
    }

    /* ========================================================
       MOBILE NAVIGATION DRAWER (Phase 8C)
       ======================================================== */

    var DRAWER_BREAKPOINT = 768;

    function isDrawerMode() {
        return window.innerWidth <= DRAWER_BREAKPOINT;
    }

    function drawerIsOpen() {
        return document.body.classList.contains("sidebar-open");
    }

    function openDrawer() {
        if (drawerIsOpen()) return;
        captureFocusOrigin();
        document.body.classList.add("sidebar-open");
        var toggle = document.getElementById("sidebar-toggle-btn");
        if (toggle) toggle.setAttribute("aria-expanded", "true");
        var sidebar = document.querySelector(".sidebar");
        if (sidebar) {
            sidebar.removeAttribute("aria-hidden");
            window.requestAnimationFrame(function () {
                focusFirstIn(sidebar);
            });
        }
    }

    function closeDrawer(options) {
        if (!drawerIsOpen()) return;
        document.body.classList.remove("sidebar-open");
        var toggle = document.getElementById("sidebar-toggle-btn");
        if (toggle) toggle.setAttribute("aria-expanded", "false");
        if (options && options.silent) {
            // Navigating away (student picked a chat): focus belongs in
            // the conversation they just opened, not back on the
            // hamburger they tapped to get here.
            focusReturnStack.pop();
        } else {
            restoreFocusOrigin();
        }
    }

    function toggleDrawer() {
        if (drawerIsOpen()) closeDrawer();
        else openDrawer();
    }

    function syncDrawerForViewport() {
        // Leaving drawer mode (rotate / resize to desktop) must not
        // strand the body in the open state, where the desktop sidebar
        // would render with a drawer transform applied.
        if (!isDrawerMode() && drawerIsOpen()) {
            document.body.classList.remove("sidebar-open");
            var toggle = document.getElementById("sidebar-toggle-btn");
            if (toggle) toggle.setAttribute("aria-expanded", "false");
            focusReturnStack.length = 0;
        }
    }

    function wireDrawer() {
        var toggle = document.getElementById("sidebar-toggle-btn");
        if (toggle) toggle.addEventListener("click", toggleDrawer);

        var overlay = document.getElementById("sidebar-overlay");
        if (overlay) overlay.addEventListener("click", function () {
            closeDrawer();
        });

        // Close on navigation. Uses a capturing listener on the sidebar
        // so it runs regardless of the inline onclick handlers app.js
        // attaches to dynamically-created history items - we never have
        // to know which specific element was clicked.
        var sidebar = document.querySelector(".sidebar");
        if (sidebar) {
            sidebar.addEventListener("click", function (event) {
                if (!isDrawerMode() || !drawerIsOpen()) return;
                var navTarget = event.target.closest(
                    ".chat-title-text, .project-title-wrapper, .btn-primary, " +
                    ".btn-secondary, .btn-quiz, .profile-trigger-btn"
                );
                if (!navTarget) return;
                // Renaming/deleting happens inside the drawer; only
                // actions that change what the main area shows close it.
                if (event.target.closest(".item-actions")) return;
                closeDrawer({ silent: true });
            }, true);
        }

        var resizeTimer = null;
        window.addEventListener("resize", function () {
            window.clearTimeout(resizeTimer);
            resizeTimer = window.setTimeout(syncDrawerForViewport, 120);
        });
        syncDrawerForViewport();
    }

    /* ========================================================
       LOGOUT CONFIRMATION (Phase 8G)
       ========================================================
       Wraps app.js's window.handleLogout instead of editing it.
       handleLogout contains the full session-cleanup sequence
       (Supabase signOut, localStorage purge, profile + student-panel
       state reset). That body is security-relevant and stays exactly
       as written; this only puts a confirmation step in front of it.
       ======================================================== */

    window.requestLogout = function () {
        var modal = document.getElementById("logout-confirm-modal");
        if (!modal) {
            // Confirmation UI missing for any reason - fall back to the
            // original immediate behavior rather than making logout
            // impossible.
            if (typeof window.handleLogout === "function") window.handleLogout();
            return;
        }
        modal.classList.remove("hidden");
    };

    window.closeLogoutConfirm = function () {
        var modal = document.getElementById("logout-confirm-modal");
        if (modal) modal.classList.add("hidden");
    };

    window.confirmLogout = function () {
        window.closeLogoutConfirm();
        if (typeof window.handleLogout === "function") {
            window.handleLogout();
        }
    };

    /* ========================================================
       EMPTY-STATE LAYOUT (Phase 8D)
       ========================================================
       Adds .is-empty-chat to .main-content whenever the conversation
       area holds only the empty-state greeting, which is what lets
       CSS center the composer. Observed rather than called from
       app.js so loadChat/sendMessage keep their exact current logic -
       app.js already creates and removes .empty-chat-greeting at
       precisely the right moments.
       ======================================================== */

    function syncEmptyState() {
        var chatBox = document.getElementById("chat-box");
        var main = document.querySelector(".main-content");
        if (!chatBox || !main) return;

        var hasGreeting = !!chatBox.querySelector(".empty-chat-greeting");
        var hasMessages = !!chatBox.querySelector(
            ".bot-message, .user-message"
        );
        main.classList.toggle("is-empty-chat", hasGreeting && !hasMessages);
    }

    function observeEmptyState() {
        var chatBox = document.getElementById("chat-box");
        if (!chatBox) return;
        new MutationObserver(syncEmptyState).observe(chatBox, {
            childList: true
        });
        syncEmptyState();
    }

    /* ========================================================
       GLOBAL KEYBOARD
       ======================================================== */

    function wireKeyboard() {
        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
                var modal = topmostOpenModal();
                if (modal) {
                    if (isDismissible(modal)) {
                        event.preventDefault();
                        closeModal(modal);
                    }
                    return;
                }
                if (drawerIsOpen()) {
                    event.preventDefault();
                    closeDrawer();
                    return;
                }
                // The composer "+" menu is a lightweight popup owned by
                // app.js; close it through its own function so its
                // aria-expanded bookkeeping stays correct.
                if (typeof window.closeComposerMenuIfOpen === "function") {
                    window.closeComposerMenuIfOpen();
                }
                return;
            }
            trapTab(event);
        });
    }

    /* ========================================================
       BOOT
       ======================================================== */

    function init() {
        observeModals();
        wireDrawer();
        observeEmptyState();
        wireKeyboard();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    // Exposed for tests and for any later module that needs to drive
    // these layers directly.
    window.KognitUI = {
        openDrawer: openDrawer,
        closeDrawer: closeDrawer,
        toggleDrawer: toggleDrawer,
        isDrawerMode: isDrawerMode,
        syncEmptyState: syncEmptyState,
        getFocusable: getFocusable,
        DRAWER_BREAKPOINT: DRAWER_BREAKPOINT
    };
})();