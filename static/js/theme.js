/* ============================================================
   KOGNIT THEME — PHASE 9A
   ============================================================
   The actual theme DECISION and its application to <html> happens
   in the tiny inline script in templates/index.html's <head> - that
   has to run before first paint, which an external file can't do.
   This file reuses that exact same decision function
   (window.__kognitResolveTheme) rather than re-implementing it, and
   is responsible for everything that happens AFTER first paint:

     - the toggle button's icon/label and click handling
     - persisting an explicit choice to localStorage
     - staying in sync if the OS theme changes while Kognit is open
       AND the student has never made an explicit choice here
     - re-syncing across browser tabs if the student changes theme
       in one tab while another is open

   Persistence is localStorage-only, per Decision 5 - no backend
   call, no profile field, no auth requirement.
   ============================================================ */

(function () {
    "use strict";

    var STORAGE_KEY = "kognit-theme";

    function currentTheme() {
        return document.documentElement.getAttribute("data-theme") === "light"
            ? "light" : "dark";
    }

    function applyTheme(theme) {
        document.documentElement.setAttribute("data-theme", theme);
        updateToggleUI(theme);
    }

    function hasExplicitChoice() {
        try {
            var v = window.localStorage.getItem(STORAGE_KEY);
            return v === "light" || v === "dark";
        } catch (e) {
            return false;
        }
    }

    function setExplicitTheme(theme) {
        applyTheme(theme);
        try {
            window.localStorage.setItem(STORAGE_KEY, theme);
        } catch (e) {
            // Storage unavailable - the toggle still works for this page
            // view, it just won't be remembered on reload. Not fatal.
        }
    }

    function toggleTheme() {
        setExplicitTheme(currentTheme() === "light" ? "dark" : "light");
    }

    function updateToggleUI(theme) {
        var btn = document.getElementById("theme-toggle-btn");
        if (!btn) return;
        var isLight = theme === "light";
        btn.setAttribute("aria-pressed", isLight ? "true" : "false");
        btn.setAttribute(
            "aria-label",
            isLight ? "Switch to dark theme" : "Switch to light theme"
        );
        btn.title = isLight ? "Switch to dark theme" : "Switch to light theme";
        var sunIcon = btn.querySelector(".theme-icon-sun");
        var moonIcon = btn.querySelector(".theme-icon-moon");
        if (sunIcon) sunIcon.classList.toggle("hidden", !isLight);
        if (moonIcon) moonIcon.classList.toggle("hidden", isLight);
    }

    function wireToggleButton() {
        var btn = document.getElementById("theme-toggle-btn");
        if (!btn) return;
        btn.addEventListener("click", toggleTheme);
        updateToggleUI(currentTheme());
    }

    function wireSystemPreferenceSync() {
        if (!window.matchMedia) return;
        var mql = window.matchMedia("(prefers-color-scheme: light)");
        var handler = function (event) {
            // Only follow the OS if the student has never explicitly
            // chosen a theme in Kognit - an explicit choice always wins
            // and must not be silently overridden by an OS-level change.
            if (hasExplicitChoice()) return;
            applyTheme(event.matches ? "light" : "dark");
        };
        if (mql.addEventListener) mql.addEventListener("change", handler);
        else if (mql.addListener) mql.addListener(handler); // older Safari
    }

    function wireCrossTabSync() {
        window.addEventListener("storage", function (event) {
            if (event.key !== STORAGE_KEY) return;
            if (event.newValue === "light" || event.newValue === "dark") {
                applyTheme(event.newValue);
            }
        });
    }

    function init() {
        // The inline head script already applied the correct theme
        // before paint; this just brings the toggle button's own UI
        // (icon, aria-pressed) into sync with whatever was decided.
        updateToggleUI(currentTheme());
        wireToggleButton();
        wireSystemPreferenceSync();
        wireCrossTabSync();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    window.KognitTheme = {
        current: currentTheme,
        set: setExplicitTheme,
        toggle: toggleTheme,
        hasExplicitChoice: hasExplicitChoice
    };
})();
