/* ============================================================
   KLORIZ ACCOUNT MENU — dropdown, language submenu, account dialog
   ============================================================
   Drives the account menu that opens from the sidebar profile row
   (templates/index.html: #profile-trigger-btn / #profile-menu) and the
   one dialog (#account-modal) that hosts Settings, Get Help, Update
   Plan and Learn More.

   Like shell.js and ui-core.js, this file OBSERVES app.js from the
   outside. It never edits or re-implements anything app.js owns:

     Edit profile   -> window.openProfileModal()        (the existing editor)
     Learning panel -> window.openStudentProfilePanel() (the existing panel)
     Logout         -> window.requestLogout()           (ui-core.js confirmation,
                                                         which calls the unchanged
                                                         handleLogout())
     Theme          -> window.KognitTheme.set()         (theme.js)
     Name           -> window.getCurrentProfile()       (app.js accessor)
     Email          -> supabaseClient.auth.getSession() (same lookup pattern
                                                         notes.js uses)

   Every app.js / theme.js function is looked up lazily at event time,
   so script load order can never break this file.

   WHAT IS REAL AND WHAT IS NOT (kept honest on purpose):
     - Settings only offers things the app genuinely honours: theme
       (theme.js) and language (i18n.js). No placeholder toggles.
     - Update Plan describes the Free plan from features that exist in
       the app and says plainly that Pro is not available. There is no
       billing backend, no pricing and no plan entitlement in this
       repository, so nothing here can purchase or unlock anything.
       PLAN_CONFIG below is the single place to change once that exists.
     - Usage Policy / Privacy Policy / About Altruvis: no such content
       exists in the repository. The screens say so rather than inventing
       legal text. LEARN_MORE_LINKS below is the single place to point
       a row at a real published page.
   ============================================================ */

(function () {
    "use strict";

    /* ========================================================
       CONFIGURATION (edit here when real content/plans exist)
       ======================================================== */

    // A string URL turns that Learn More row into an external link that
    // opens in a new tab. null = show the in-app screen instead.
    var LEARN_MORE_LINKS = {
        kloriz: null,
        altruvis: null,
        usage: null,
        privacy: null
    };

    // Display-only. Entitlements must come from trusted backend logic;
    // this object never grants anything. There is no plan endpoint yet,
    // so every account is shown as Free and Pro as unavailable.
    var PLAN_CONFIG = {
        currentPlan: "free",
        proAvailable: false
    };

    /* ========================================================
       SMALL HELPERS
       ======================================================== */

    function $(id) { return document.getElementById(id); }

    function t(key) {
        var i18n = window.KlorizI18n;
        return i18n ? i18n.t(key) : key;
    }

    function call(name) {
        var fn = window[name];
        if (typeof fn === "function") {
            return fn.apply(window, Array.prototype.slice.call(arguments, 1));
        }
        return undefined;
    }

    // Built from fixed strings only (never student data), via
    // textContent, so nothing here can become markup.
    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    var ICONS = {
        down: '<polyline points="6 9 12 15 18 9"></polyline>',
        right: '<polyline points="9 6 15 12 9 18"></polyline>',
        check: '<polyline points="20 6 9 17 4 12"></polyline>',
        external: '<path d="M15 3h6v6"></path><path d="M10 14 21 3"></path><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>',
        info: '<circle cx="12" cy="12" r="10"></circle><path d="M12 16v-4"></path><path d="M12 8h.01"></path>',
        building: '<path d="M6 22V4a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v18Z"></path><path d="M6 12H4a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"></path><path d="M18 9h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-2"></path><path d="M10 6h4"></path><path d="M10 10h4"></path><path d="M10 14h4"></path><path d="M10 18h4"></path>',
        file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line>',
        shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"></path>'
    };

    function icon(name, size) {
        var wrap = document.createElement("span");
        wrap.style.display = "inline-flex";
        // ICONS values are constants defined above - never user data.
        wrap.innerHTML = '<svg width="' + size + '" height="' + size +
            '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ' +
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            ICONS[name] + "</svg>";
        wrap.setAttribute("aria-hidden", "true");
        return wrap;
    }

    function dismissDrawer() {
        // On phones the sidebar is a drawer. A modal opened from inside it
        // should not leave the drawer open underneath (the old profile row
        // did the same through ui-core's close-on-navigate rule).
        var ui = window.KognitUI;
        if (ui && typeof ui.isDrawerMode === "function" && ui.isDrawerMode() &&
            typeof ui.closeDrawer === "function") {
            ui.closeDrawer({ silent: true });
        }
    }

    /* ========================================================
       ACCOUNT MENU (dropdown)
       ======================================================== */

    var wrapper, trigger, menu, paneMain, paneLanguage;
    var identityGeneration = 0;
    var cachedEmail = "";

    function menuIsOpen() {
        return !!menu && !menu.classList.contains("hidden");
    }

    function activePane() {
        return paneLanguage && !paneLanguage.classList.contains("hidden") ? paneLanguage : paneMain;
    }

    function paneItems(pane) {
        // Every item in the ACTIVE pane is visible (inactive panes are
        // display:none and never passed in), so no visibility filter needed.
        return Array.prototype.slice.call(pane.querySelectorAll('[role^="menuitem"]'));
    }

    function showPane(which, focusTarget) {
        var language = which === "language";
        paneMain.classList.toggle("hidden", language);
        paneLanguage.classList.toggle("hidden", !language);
        if (focusTarget) focusTarget();
    }

    function focusLanguagePaneEntry() {
        var checked = paneLanguage.querySelector('[aria-checked="true"]') ||
                      paneLanguage.querySelector("[data-pm-language]");
        if (checked) checked.focus();
    }

    function focusLanguageRow() {
        var row = paneMain.querySelector('[data-pm-action="language"]');
        if (row) row.focus();
    }

    function syncLanguageUI() {
        var i18n = window.KlorizI18n;
        if (!i18n) return;
        var lang = i18n.get();
        var valueEl = $("pm-language-value");
        if (valueEl) {
            valueEl.textContent = i18n.names[lang].native;
            valueEl.setAttribute("lang", lang);
        }
        Array.prototype.forEach.call(
            menu ? menu.querySelectorAll("[data-pm-language]") : [],
            function (opt) {
                opt.setAttribute("aria-checked", opt.getAttribute("data-pm-language") === lang ? "true" : "false");
            }
        );
    }

    /* Name comes from the loaded profile (the same source the sidebar
       row uses); when no profile exists yet the sidebar row already shows
       the account email as its name, so mirror that fallback. The email
       line is the account's own email, read from the current session. */
    function readSessionEmail() {
        var client = null;
        try {
            if (typeof supabaseClient !== "undefined" && supabaseClient) client = supabaseClient;
        } catch (e) { /* not defined */ }
        if (!client && window.supabaseClient) client = window.supabaseClient;
        if (!client || !client.auth || typeof client.auth.getSession !== "function") {
            return Promise.resolve("");
        }
        return client.auth.getSession().then(
            function (result) {
                var session = result && result.data && result.data.session;
                return (session && session.user && session.user.email) || "";
            },
            function () { return ""; }
        );
    }

    function refreshIdentity() {
        var nameEl = $("pm-name");
        var emailEl = $("pm-email");
        if (!nameEl || !emailEl) return;

        var profile = call("getCurrentProfile");
        var sidebarName = $("user-display-name");
        var name = (profile && profile.name) || (sidebarName ? sidebarName.textContent : "") || "";
        nameEl.textContent = name;

        function paintEmail(email) {
            // Do not repeat the email on both lines (the no-profile fallback
            // already uses it as the display name).
            emailEl.textContent = email && email !== name ? email : "";
        }
        paintEmail(cachedEmail);

        var myGeneration = ++identityGeneration;
        readSessionEmail().then(function (email) {
            if (myGeneration !== identityGeneration) return;
            cachedEmail = email;
            paintEmail(email);
        });
    }

    function openMenu(focus) {
        if (!menu || menuIsOpen()) return;
        showPane("main");
        syncLanguageUI();
        refreshIdentity();
        menu.classList.remove("hidden");
        trigger.setAttribute("aria-expanded", "true");
        if (focus) {
            var items = paneItems(paneMain);
            var target = focus === "last" ? items[items.length - 1] : items[0];
            if (target) target.focus();
        }
    }

    function closeMenu(restoreFocus) {
        if (!menu) return;
        var wasOpen = menuIsOpen();
        menu.classList.add("hidden");
        trigger.setAttribute("aria-expanded", "false");
        showPane("main");
        if (wasOpen && restoreFocus) trigger.focus();
    }

    function moveFocus(delta, absolute) {
        var items = paneItems(activePane());
        if (!items.length) return;
        var index = items.indexOf(document.activeElement);
        var next;
        if (absolute === "first") next = 0;
        else if (absolute === "last") next = items.length - 1;
        else if (index === -1) next = delta > 0 ? 0 : items.length - 1;
        else next = (index + delta + items.length) % items.length;
        items[next].focus();
    }

    function onWrapperKeydown(event) {
        var key = event.key;
        var open = menuIsOpen();

        if (!open) {
            if (event.target === trigger && (key === "ArrowDown" || key === "ArrowUp")) {
                event.preventDefault();
                openMenu(key === "ArrowUp" ? "last" : "first");
            }
            return;
        }

        if (key === "Escape") {
            // Handled here (and stopped) so ui-core's global Escape rule does
            // not ALSO close the mobile drawer behind the menu.
            event.preventDefault();
            event.stopPropagation();
            if (activePane() === paneLanguage) {
                showPane("main", focusLanguageRow);
            } else {
                closeMenu(true);
            }
            return;
        }
        if (key === "Tab") {
            closeMenu(false);
            return;
        }
        if (key === "ArrowDown") { event.preventDefault(); moveFocus(1); return; }
        if (key === "ArrowUp") { event.preventDefault(); moveFocus(-1); return; }
        if (key === "Home") { event.preventDefault(); moveFocus(0, "first"); return; }
        if (key === "End") { event.preventDefault(); moveFocus(0, "last"); return; }

        if (key === "ArrowLeft" && activePane() === paneLanguage) {
            event.preventDefault();
            showPane("main", focusLanguageRow);
            return;
        }
        if (key === "ArrowRight" && activePane() === paneMain) {
            var focused = document.activeElement;
            if (focused && focused.getAttribute &&
                focused.getAttribute("data-pm-action") === "language") {
                event.preventDefault();
                showPane("language", focusLanguagePaneEntry);
            }
        }
    }

    function onDocumentPointerDown(event) {
        if (!menuIsOpen()) return;
        if (wrapper.contains(event.target)) return;
        // Clicked elsewhere: close, and leave focus where the student put it.
        closeMenu(false);
    }

    function runAction(action) {
        if (action === "language") {
            showPane("language", focusLanguagePaneEntry);
            return;
        }
        if (action === "language-back") {
            showPane("main", focusLanguageRow);
            return;
        }

        // Everything else leaves the menu. Focus returns to the trigger first
        // so the dialog opened next remembers it as the place to return to.
        closeMenu(true);
        dismissDrawer();

        switch (action) {
            case "learning-profile": call("openStudentProfilePanel"); break;
            case "edit-profile": call("openProfileModal"); break;
            case "logout": call("requestLogout"); break;
            case "settings":
            case "help":
            case "plan":
            case "learn-more":
                openAccountModal(action);
                break;
            default: break;
        }
    }

    function onMenuClick(event) {
        var langBtn = event.target.closest("[data-pm-language]");
        if (langBtn && menu.contains(langBtn)) {
            var i18n = window.KlorizI18n;
            if (i18n) i18n.set(langBtn.getAttribute("data-pm-language"));
            syncLanguageUI();
            // Selection done: back to the main pane (it now shows the new
            // language), keeping focus inside the menu.
            showPane("main", focusLanguageRow);
            return;
        }
        var actionBtn = event.target.closest("[data-pm-action]");
        if (actionBtn && menu.contains(actionBtn)) {
            runAction(actionBtn.getAttribute("data-pm-action"));
        }
    }

    function onTriggerClick(event) {
        if (menuIsOpen()) {
            closeMenu(false);
        } else {
            // event.detail === 0 means keyboard activation (Enter/Space):
            // move focus into the menu. A mouse/touch click keeps focus on
            // the trigger.
            openMenu(event.detail === 0 ? "first" : null);
        }
    }

    function initMenu() {
        wrapper = $("auth-logged-view");
        trigger = $("profile-trigger-btn");
        menu = $("profile-menu");
        paneMain = $("pm-main");
        paneLanguage = $("pm-language-pane");
        if (!wrapper || !trigger || !menu || !paneMain || !paneLanguage) return;

        trigger.addEventListener("click", onTriggerClick);
        menu.addEventListener("click", onMenuClick);
        wrapper.addEventListener("keydown", onWrapperKeydown);
        document.addEventListener("pointerdown", onDocumentPointerDown, true);
        document.addEventListener("mousedown", onDocumentPointerDown, true);

        // updateAuthUI() hides this wrapper on logout. Close the menu and
        // forget the cached email so one student's address can never show
        // for the next person using this browser.
        new MutationObserver(function () {
            if (wrapper.classList.contains("hidden")) {
                closeMenu(false);
                cachedEmail = "";
                identityGeneration++;
                var nameEl = $("pm-name");
                var emailEl = $("pm-email");
                if (nameEl) nameEl.textContent = "";
                if (emailEl) emailEl.textContent = "";
            }
        }).observe(wrapper, { attributes: true, attributeFilter: ["class"] });

        // Keep the open menu's name current if the profile finishes loading
        // or is edited while it is open.
        var sidebarName = $("user-display-name");
        if (sidebarName) {
            new MutationObserver(function () {
                if (menuIsOpen()) refreshIdentity();
            }).observe(sidebarName, { childList: true, characterData: true, subtree: true });
        }

        syncLanguageUI();
    }

    /* ========================================================
       ACCOUNT MODAL (Settings / Help / Plan / Learn more)
       ======================================================== */

    var modal, modalTitle, modalBody, modalBack;
    var currentScreen = null;

    // Which screen the Back arrow returns to (none = no arrow).
    var SCREEN_PARENT = {
        "about-kloriz": "learn-more",
        "about-altruvis": "learn-more",
        "usage-policy": "learn-more",
        "privacy-policy": "learn-more"
    };

    var SCREEN_TITLE_KEY = {
        "settings": "settings.title",
        "help": "help.title",
        "plan": "plan.title",
        "learn-more": "learn.title",
        "about-kloriz": "learn.kloriz.title",
        "about-altruvis": "learn.altruvis.title",
        "usage-policy": "learn.usage.title",
        "privacy-policy": "learn.privacy.title"
    };

    function modalIsOpen() {
        return !!modal && !modal.classList.contains("hidden");
    }

    /* ---- Settings ---- */
    function segmented(name, options, checkedValue, onChange) {
        var group = el("div", "acct-seg");
        group.setAttribute("role", "radiogroup");
        options.forEach(function (opt) {
            var label = el("label", "acct-seg-opt");
            var input = document.createElement("input");
            input.type = "radio";
            input.name = name;
            input.value = opt.value;
            input.id = name + "-" + opt.value;
            input.checked = opt.value === checkedValue;
            input.addEventListener("change", function () {
                if (input.checked) onChange(opt.value);
            });
            var text = el("span", null, opt.label);
            if (opt.lang) text.setAttribute("lang", opt.lang);
            label.appendChild(input);
            label.appendChild(text);
            group.appendChild(label);
        });
        return group;
    }

    function currentTheme() {
        var theme = window.KognitTheme;
        if (theme && typeof theme.current === "function") return theme.current();
        return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    function buildSettings(body) {
        body.appendChild(el("p", "acct-lead", t("settings.intro")));

        var appearance = el("section", "acct-group");
        appearance.appendChild(el("h3", "acct-group-title", t("settings.appearance")));
        var themeRow = el("div", "acct-row");
        themeRow.appendChild(el("span", "acct-row-label", t("settings.theme")));
        themeRow.appendChild(segmented("acct-theme", [
            { value: "light", label: t("settings.themeLight") },
            { value: "dark", label: t("settings.themeDark") }
        ], currentTheme(), function (value) {
            var theme = window.KognitTheme;
            if (theme && typeof theme.set === "function") theme.set(value);
        }));
        appearance.appendChild(themeRow);
        body.appendChild(appearance);

        var language = el("section", "acct-group");
        language.appendChild(el("h3", "acct-group-title", t("settings.language")));
        var langRow = el("div", "acct-row");
        langRow.appendChild(el("span", "acct-row-label", t("settings.language")));
        var i18n = window.KlorizI18n;
        langRow.appendChild(segmented("acct-language", [
            { value: "en", label: i18n.names.en.native, lang: "en" },
            { value: "bn", label: i18n.names.bn.native, lang: "bn" }
        ], i18n.get(), function (value) { i18n.set(value); }));
        language.appendChild(langRow);
        language.appendChild(el("p", "acct-hint", t("settings.languageHint")));
        body.appendChild(language);

        body.appendChild(el("p", "acct-note", t("settings.deviceNote")));
    }

    function syncThemeRadios() {
        if (!modalIsOpen() || currentScreen !== "settings") return;
        var theme = currentTheme();
        Array.prototype.forEach.call(modalBody.querySelectorAll('input[name="acct-theme"]'), function (input) {
            input.checked = input.value === theme;
        });
    }

    /* ---- Help ---- */
    function buildHelp(body) {
        body.appendChild(el("p", "acct-lead", t("help.intro")));
        var list = el("div", "acct-faq-list");
        for (var n = 1; n <= 7; n++) {
            var details = el("details", "acct-faq");
            var summary = el("summary");
            summary.appendChild(el("span", null, t("help.t" + n + ".q")));
            summary.appendChild(icon("down", 18));
            details.appendChild(summary);
            details.appendChild(el("div", "acct-faq-answer", t("help.t" + n + ".a")));
            list.appendChild(details);
        }
        body.appendChild(list);
    }

    /* ---- Update plan ---- */
    function buildPlan(body) {
        body.appendChild(el("p", "acct-lead", t("plan.intro")));
        var grid = el("div", "acct-plan-grid");

        var freeIsCurrent = PLAN_CONFIG.currentPlan === "free";
        var free = el("section", "acct-plan-card" + (freeIsCurrent ? " is-current" : ""));
        var freeHead = el("div", "acct-plan-head");
        freeHead.appendChild(el("h3", "acct-plan-name", t("plan.free")));
        if (freeIsCurrent) freeHead.appendChild(el("span", "acct-badge", t("plan.current")));
        free.appendChild(freeHead);
        var features = el("ul", "acct-feature-list");
        for (var n = 1; n <= 5; n++) {
            var li = el("li");
            li.appendChild(icon("check", 16));
            li.appendChild(el("span", null, t("plan.free.f" + n)));
            features.appendChild(li);
        }
        free.appendChild(features);
        grid.appendChild(free);

        var pro = el("section", "acct-plan-card");
        var proHead = el("div", "acct-plan-head");
        proHead.appendChild(el("h3", "acct-plan-name", t("plan.pro")));
        if (!PLAN_CONFIG.proAvailable) proHead.appendChild(el("span", "acct-badge", t("plan.notAvailable")));
        pro.appendChild(proHead);
        pro.appendChild(el("p", "acct-plan-body", t("plan.proBody")));
        var button = el("button", "acct-btn", t("plan.proButton"));
        button.type = "button";
        // There is no checkout, so this can never be enabled from here.
        button.disabled = true;
        button.setAttribute("aria-disabled", "true");
        pro.appendChild(button);
        grid.appendChild(pro);

        body.appendChild(grid);
    }

    /* ---- Learn more ---- */
    var LEARN_ROWS = [
        { id: "kloriz", screen: "about-kloriz", iconName: "info", title: "learn.kloriz.title", desc: "learn.kloriz.desc" },
        { id: "altruvis", screen: "about-altruvis", iconName: "building", title: "learn.altruvis.title", desc: "learn.altruvis.desc" },
        { id: "usage", screen: "usage-policy", iconName: "file", title: "learn.usage.title", desc: "learn.usage.desc" },
        { id: "privacy", screen: "privacy-policy", iconName: "shield", title: "learn.privacy.title", desc: "learn.privacy.desc" }
    ];

    function isSafeExternalUrl(url) {
        return typeof url === "string" && /^https:\/\//i.test(url);
    }

    function buildLearnMore(body) {
        var list = el("div", "acct-link-list");
        LEARN_ROWS.forEach(function (row) {
            var href = LEARN_MORE_LINKS[row.id];
            var external = isSafeExternalUrl(href);
            var node;
            if (external) {
                node = el("a", "acct-link-row");
                node.href = href;
                node.target = "_blank";
                node.rel = "noopener noreferrer";
                node.setAttribute("aria-label", t(row.title) + " (" + t("learn.external") + ")");
            } else {
                node = el("button", "acct-link-row");
                node.type = "button";
                node.addEventListener("click", function () { showScreen(row.screen, true); });
            }
            var iconBox = el("span", "acct-link-icon");
            iconBox.appendChild(icon(row.iconName, 22));
            var text = el("span", "acct-link-text");
            text.appendChild(el("span", "acct-link-title", t(row.title)));
            text.appendChild(el("span", "acct-link-desc", t(row.desc)));
            var trail = el("span", "acct-link-trail");
            trail.appendChild(icon(external ? "external" : "right", 18));
            node.appendChild(iconBox);
            node.appendChild(text);
            node.appendChild(trail);
            list.appendChild(node);
        });
        body.appendChild(list);
    }

    function prose(body, keys, mutedLast) {
        var wrap = el("div", "acct-prose");
        keys.forEach(function (key, i) {
            var p = el("p", mutedLast && i === keys.length - 1 ? "is-muted" : null, t(key));
            wrap.appendChild(p);
        });
        body.appendChild(wrap);
    }

    var BUILDERS = {
        "settings": buildSettings,
        "help": buildHelp,
        "plan": buildPlan,
        "learn-more": buildLearnMore,
        "about-kloriz": function (b) { prose(b, ["about.kloriz.p1", "about.kloriz.p2", "about.kloriz.p3"]); },
        "about-altruvis": function (b) { prose(b, ["about.altruvis.p1"]); },
        "usage-policy": function (b) { prose(b, ["policy.usage.body", "policy.later"], true); },
        "privacy-policy": function (b) { prose(b, ["policy.privacy.body", "policy.later"], true); }
    };

    function renderScreen() {
        var build = BUILDERS[currentScreen];
        if (!build) return;
        modalBody.textContent = "";
        modalTitle.textContent = t(SCREEN_TITLE_KEY[currentScreen]);
        modalBack.classList.toggle("hidden", !SCREEN_PARENT[currentScreen]);
        build(modalBody);
        modalBody.scrollTop = 0;
    }

    function showScreen(name, moveFocus) {
        if (!BUILDERS[name]) return;
        currentScreen = name;
        renderScreen();
        // In-dialog navigation: put focus on the new heading so assistive
        // tech announces where the student is (the initial open is handled
        // by ui-core's modal focus management instead).
        if (moveFocus) modalTitle.focus();
    }

    function openAccountModal(name) {
        if (!modal || !BUILDERS[name]) return;
        if (menuIsOpen()) closeMenu(false);
        showScreen(name, false);
        modal.classList.remove("hidden");
    }

    window.openAccountModal = openAccountModal;

    window.closeAccountModal = function () {
        if (modal) modal.classList.add("hidden");
        currentScreen = null;
    };

    function onLanguageChange() {
        syncLanguageUI();
        if (!modalIsOpen() || !currentScreen) return;
        // Re-render in place, and put keyboard focus back on the same
        // control (the language radio the student just used).
        var focusedId = document.activeElement && modal.contains(document.activeElement)
            ? document.activeElement.id : "";
        renderScreen();
        if (focusedId) {
            var again = document.getElementById(focusedId);
            if (again) again.focus();
        }
    }

    function initModal() {
        modal = $("account-modal");
        modalTitle = $("account-modal-title");
        modalBody = $("account-body");
        modalBack = $("account-back-btn");
        if (!modal || !modalTitle || !modalBody || !modalBack) return;

        modalBack.addEventListener("click", function () {
            var parent = SCREEN_PARENT[currentScreen];
            if (parent) showScreen(parent, true);
        });

        // Theme can also be toggled by the sidebar/top-bar buttons while
        // Settings is open; keep its radios truthful.
        new MutationObserver(syncThemeRadios).observe(document.documentElement, {
            attributes: true,
            attributeFilter: ["data-theme"]
        });
    }

    /* ========================================================
       BOOT
       ======================================================== */

    function init() {
        initMenu();
        initModal();
        document.addEventListener("kloriz:languagechange", onLanguageChange);
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    // Exposed for tests and for any later module that needs to drive the menu.
    window.KlorizProfileMenu = {
        open: function (focus) { openMenu(focus || null); },
        close: function (restoreFocus) { closeMenu(!!restoreFocus); },
        isOpen: menuIsOpen,
        openAccount: openAccountModal,
        config: { LEARN_MORE_LINKS: LEARN_MORE_LINKS, PLAN_CONFIG: PLAN_CONFIG }
    };
})();