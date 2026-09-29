/* ============================================================
   KOGNIT SHELL BEHAVIOUR — PHASE 10 (UI redesign)
   ============================================================
   Wires the redesigned shell (templates/index.html + shell.css) to
   the state app.js already owns. Like ui-core.js it OBSERVES app.js
   from the outside instead of editing it: nothing here touches the
   streaming pipeline, message rendering, auth, or persistence.

   What lives here:
     - quick-action cards  -> setMode / triggerComposer*Upload
     - class pill + menu   -> the student's REAL profile class,
                              opening the real profile editor
     - greeting refresh    -> once the async profile fetch resolves
     - sidebar search      -> revealed by the magnifier (desktop + mobile)
     - mobile top bar      -> chat title, three-dot menu, drawer close
     - composer            -> auto-grow textarea, responsive placeholder
     - a11y sync           -> aria-pressed on the Direct/Socratic controls

   Every app.js function used is looked up lazily via window[...] at
   event time, so load order can never break this file.
   ============================================================ */

(function () {
    "use strict";

    var DESKTOP_PLACEHOLDER = "Ask a question, upload a file, or get help...";
    var MOBILE_PLACEHOLDER = "Ask Kognit";
    var MOBILE_QUERY = "(max-width: 768px)";
    var TEXTAREA_MAX_PX = 200;

    function $(id) { return document.getElementById(id); }

    function call(name) {
        var fn = window[name];
        if (typeof fn === "function") {
            return fn.apply(window, Array.prototype.slice.call(arguments, 1));
        }
        return undefined;
    }

    function isLoggedIn() {
        var view = $("auth-logged-view");
        return !!view && !view.classList.contains("hidden");
    }

    function getProfile() {
        return typeof window.getCurrentProfile === "function"
            ? window.getCurrentProfile()
            : null;
    }

    /* --------------------------------------------------------
       Quick-action cards
       -------------------------------------------------------- */
    function focusComposer() {
        var input = $("user-input");
        if (input) input.focus();
    }

    function handleQuickAction(action) {
        if (action === "direct") {
            call("setMode", "direct");
            focusComposer();
        } else if (action === "socratic") {
            call("setMode", "socratic");
            focusComposer();
        } else if (action === "pdf") {
            call("triggerComposerPDFUpload");
        } else if (action === "image") {
            call("triggerComposerImageUpload");
        }
    }

    function wireQuickActions() {
        var cards = document.querySelectorAll(".quick-card[data-quick-action]");
        Array.prototype.forEach.call(cards, function (card) {
            card.addEventListener("click", function () {
                handleQuickAction(card.getAttribute("data-quick-action"));
            });
        });
    }

    /* --------------------------------------------------------
       Mode state -> aria-pressed (app.js toggles only .active)
       -------------------------------------------------------- */
    function syncModeAria() {
        var direct = $("btn-direct");
        var socratic = $("btn-socratic");
        if (!direct || !socratic) return;
        var socraticOn = socratic.classList.contains("active");

        direct.setAttribute("aria-pressed", socraticOn ? "false" : "true");
        socratic.setAttribute("aria-pressed", socraticOn ? "true" : "false");

        var directCard = document.querySelector('.quick-card[data-quick-action="direct"]');
        var socraticCard = document.querySelector('.quick-card[data-quick-action="socratic"]');
        if (directCard) directCard.setAttribute("aria-pressed", socraticOn ? "false" : "true");
        if (socraticCard) socraticCard.setAttribute("aria-pressed", socraticOn ? "true" : "false");

        var items = document.querySelectorAll("[data-chat-menu='direct'], [data-chat-menu='socratic']");
        Array.prototype.forEach.call(items, function (item) {
            var isSocratic = item.getAttribute("data-chat-menu") === "socratic";
            item.setAttribute("aria-checked", isSocratic === socraticOn ? "true" : "false");
        });
    }

    function observeMode() {
        ["btn-direct", "btn-socratic"].forEach(function (id) {
            var el = $(id);
            if (!el) return;
            new MutationObserver(syncModeAria).observe(el, {
                attributes: true,
                attributeFilter: ["class"]
            });
        });
        syncModeAria();
    }

    /* --------------------------------------------------------
       Profile-driven UI: class pill, menu value, greeting name
       -------------------------------------------------------- */
    function openClassEditor() {
        if (isLoggedIn()) {
            call("openProfileModal");
        } else {
            call("openAuthModal");
        }
    }

    function refreshProfileUI() {
        var profile = isLoggedIn() ? getProfile() : null;

        var pill = $("composer-class-pill");
        var pillLabel = $("composer-class-label");
        if (pill && pillLabel) {
            if (profile && profile.user_class) {
                pillLabel.textContent = profile.user_class;
                pill.title = "Class: " + profile.user_class + " \u2014 change it in your profile";
                pill.setAttribute("aria-label", "Class " + profile.user_class + ". Change it in your profile.");
                pill.classList.remove("hidden");
            } else {
                pill.classList.add("hidden");
            }
        }

        var menuValue = $("chat-menu-class-value");
        if (menuValue) {
            menuValue.textContent = profile && profile.user_class
                ? profile.user_class
                : (isLoggedIn() ? "Set up profile" : "Log in");
        }

        // The class/stream line is hidden by CSS; keep it as the tooltip.
        var trigger = $("profile-trigger-btn");
        var meta = $("user-display-meta");
        if (trigger && meta && meta.textContent) {
            trigger.title = meta.textContent;
        }

        // The greeting's name comes from the same profile.
        call("refreshEmptyChatGreeting");
    }

    function observeProfile() {
        var logged = $("auth-logged-view");
        if (logged) {
            // renderProfileTrigger() rewrites the name/meta text whenever the
            // profile loads or changes; updateAuthUI() toggles .hidden on
            // login/logout. Either one means the profile UI is stale.
            new MutationObserver(refreshProfileUI).observe(logged, {
                attributes: true,
                attributeFilter: ["class"],
                childList: true,
                subtree: true,
                characterData: true
            });
        }
        refreshProfileUI();
    }

    function wireClassPill() {
        var pill = $("composer-class-pill");
        if (pill) pill.addEventListener("click", openClassEditor);
    }

    /* --------------------------------------------------------
       Navbar title + which view is active
       -------------------------------------------------------- */
    function activeView() {
        var active = document.querySelector(".nav-rail-btn.active");
        var id = active ? active.id : "";
        if (id === "nav-tab-quizzes") return "quizzes";
        if (id === "nav-tab-projects") return "projects";
        return "chat";
    }

    function activeChatTitle() {
        if (typeof window.getProjectsSnapshot !== "function" ||
            typeof window.getActiveIds !== "function") return "New Chat";
        var ids = window.getActiveIds() || {};
        var projects = window.getProjectsSnapshot() || [];
        var proj = projects.filter(function (p) { return p.id === ids.activeProjectId; })[0];
        var chat = proj && (proj.chats || []).filter(function (c) { return c.id === ids.activeChatId; })[0];
        return (chat && chat.title) || "New Chat";
    }

    function syncNavbar() {
        var view = activeView();
        var main = document.querySelector(".main-content");
        if (main) main.setAttribute("data-view", view);

        var title = $("navbar-chat-title");
        if (title) {
            title.textContent = view === "quizzes" ? "Quizzes"
                : view === "projects" ? "Projects"
                : activeChatTitle();
        }

        // Chat-only controls make no sense on the other two destinations.
        var chatOnly = [$("navbar-search-btn"), $("chat-menu-btn")];
        chatOnly.forEach(function (btn) {
            if (btn) btn.disabled = view !== "chat";
            if (btn) btn.style.visibility = view === "chat" ? "" : "hidden";
        });
        if (view !== "chat") closeChatMenu();
    }

    function observeNavbar() {
        var rail = document.querySelector(".nav-rail");
        if (rail) {
            new MutationObserver(syncNavbar).observe(rail, {
                attributes: true,
                attributeFilter: ["class"],
                subtree: true
            });
        }
        // renderHistoryList() re-renders on every chat switch / rename /
        // create, which is exactly when the title can change.
        var history = $("history-list");
        if (history) new MutationObserver(syncNavbar).observe(history, { childList: true });
        syncNavbar();
    }

    /* --------------------------------------------------------
       Sidebar search (magnifier reveals the existing input)
       -------------------------------------------------------- */
    function searchContainer() { return $("sidebar-search-container"); }

    function setSearchOpen(open) {
        var box = searchContainer();
        var toggle = $("sidebar-search-toggle");
        var input = $("sidebar-search-input");
        if (!box) return;
        box.classList.toggle("hidden", !open);
        if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (open) {
            if (input) input.focus();
        } else if (input && input.value) {
            // Closing the box must not leave a hidden filter active.
            input.value = "";
            call("handleSidebarSearch", { target: input });
        }
    }

    function wireSearch() {
        var toggle = $("sidebar-search-toggle");
        if (toggle) {
            toggle.addEventListener("click", function () {
                var box = searchContainer();
                setSearchOpen(!!box && box.classList.contains("hidden"));
            });
        }

        var input = $("sidebar-search-input");
        if (input) {
            input.addEventListener("keydown", function (event) {
                if (event.key === "Escape") {
                    event.preventDefault();
                    setSearchOpen(false);
                    if (toggle) toggle.focus();
                }
            });
        }

        var navSearch = $("navbar-search-btn");
        if (navSearch) {
            navSearch.addEventListener("click", function () {
                if (window.KognitNav && activeView() !== "chat") {
                    window.KognitNav.switchTo("chat", { silent: true });
                }
                if (window.KognitUI) window.KognitUI.openDrawer();
                setSearchOpen(true);
                // openDrawer() focuses the drawer's first control on the next
                // frame; the student asked to search, so put focus there.
                window.setTimeout(function () {
                    var field = $("sidebar-search-input");
                    if (field) field.focus();
                }, 80);
            });
        }
    }

    /* --------------------------------------------------------
       Drawer close button (mobile)
       -------------------------------------------------------- */
    function wireDrawerClose() {
        var btn = $("sidebar-close-btn");
        if (btn) {
            btn.addEventListener("click", function () {
                if (window.KognitUI) window.KognitUI.closeDrawer();
            });
        }
    }

    /* --------------------------------------------------------
       Three-dot chat menu (mobile top bar)
       -------------------------------------------------------- */
    function chatMenu() { return $("chat-menu"); }

    function menuItems() {
        var menu = chatMenu();
        return menu ? Array.prototype.slice.call(menu.querySelectorAll("[role^='menuitem']")) : [];
    }

    function isChatMenuOpen() {
        var menu = chatMenu();
        return !!menu && !menu.classList.contains("hidden");
    }

    function closeChatMenu(options) {
        var menu = chatMenu();
        var btn = $("chat-menu-btn");
        if (!menu || menu.classList.contains("hidden")) return;
        menu.classList.add("hidden");
        if (btn) {
            btn.setAttribute("aria-expanded", "false");
            if (options && options.restoreFocus) btn.focus();
        }
    }

    function openChatMenu() {
        var menu = chatMenu();
        var btn = $("chat-menu-btn");
        if (!menu) return;
        refreshProfileUI();
        syncModeAria();
        menu.classList.remove("hidden");
        if (btn) btn.setAttribute("aria-expanded", "true");
        var checked = menu.querySelector("[aria-checked='true']") || menuItems()[0];
        if (checked) checked.focus();
    }

    function runChatMenuAction(action) {
        if (action === "direct") call("setMode", "direct");
        else if (action === "socratic") call("setMode", "socratic");
        else if (action === "class") openClassEditor();
        else if (action === "share") call("shareChatLink");
        else if (action === "new-chat") call("createStandaloneChat");
    }

    function wireChatMenu() {
        var btn = $("chat-menu-btn");
        var menu = chatMenu();
        if (!btn || !menu) return;

        btn.addEventListener("click", function (event) {
            event.stopPropagation();
            if (isChatMenuOpen()) closeChatMenu();
            else openChatMenu();
        });

        menu.addEventListener("click", function (event) {
            var item = event.target.closest("[data-chat-menu]");
            if (!item) return;
            var action = item.getAttribute("data-chat-menu");
            closeChatMenu();
            runChatMenuAction(action);
        });

        menu.addEventListener("keydown", function (event) {
            var items = menuItems();
            var index = items.indexOf(document.activeElement);
            if (event.key === "ArrowDown") {
                event.preventDefault();
                items[(index + 1) % items.length].focus();
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                items[(index - 1 + items.length) % items.length].focus();
            } else if (event.key === "Home") {
                event.preventDefault();
                items[0].focus();
            } else if (event.key === "End") {
                event.preventDefault();
                items[items.length - 1].focus();
            } else if (event.key === "Tab") {
                closeChatMenu();
            }
        });

        document.addEventListener("click", function (event) {
            if (!isChatMenuOpen()) return;
            var wrapper = document.querySelector(".navbar-menu-wrapper");
            if (wrapper && !wrapper.contains(event.target)) closeChatMenu();
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && isChatMenuOpen()) {
                event.preventDefault();
                closeChatMenu({ restoreFocus: true });
            }
        });
    }

    /* --------------------------------------------------------
       Composer: auto-grow + responsive placeholder
       -------------------------------------------------------- */
    function autosize() {
        var input = $("user-input");
        if (!input) return;
        input.style.height = "auto";
        var next = Math.min(input.scrollHeight, TEXTAREA_MAX_PX);
        // scrollHeight is 0 while the element is display:none (other tabs).
        if (next > 0) input.style.height = next + "px";
    }

    function applyPlaceholder() {
        var input = $("user-input");
        if (!input || !window.matchMedia) return;
        input.placeholder = window.matchMedia(MOBILE_QUERY).matches
            ? MOBILE_PLACEHOLDER
            : DESKTOP_PLACEHOLDER;
    }

    function wireComposer() {
        var input = $("user-input");
        if (input) input.addEventListener("input", autosize);

        applyPlaceholder();
        if (window.matchMedia) {
            var mql = window.matchMedia(MOBILE_QUERY);
            if (mql.addEventListener) mql.addEventListener("change", applyPlaceholder);
            else if (mql.addListener) mql.addListener(applyPlaceholder);
        }
    }

    /* --------------------------------------------------------
       Boot
       -------------------------------------------------------- */
    function init() {
        wireQuickActions();
        wireClassPill();
        wireSearch();
        wireDrawerClose();
        wireChatMenu();
        wireComposer();
        observeMode();
        observeProfile();
        observeNavbar();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    window.KognitShell = {
        handleQuickAction: handleQuickAction,
        refreshProfileUI: refreshProfileUI,
        syncNavbar: syncNavbar,
        setSearchOpen: setSearchOpen,
        openChatMenu: openChatMenu,
        closeChatMenu: closeChatMenu,
        DESKTOP_PLACEHOLDER: DESKTOP_PLACEHOLDER,
        MOBILE_PLACEHOLDER: MOBILE_PLACEHOLDER
    };
})();
