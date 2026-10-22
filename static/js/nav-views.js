/* ============================================================
   KOGNIT NAV VIEWS — PHASE 9B (+ NOTEBOOKS REDESIGN)
   ============================================================
   Top-level navigation: Chat / Quizzes / Notebooks / Pocket Notes.
   Loaded AFTER app.js (see templates/index.html), and depends only
   on the explicit accessor functions app.js exposes for this
   purpose (window.getProjectsSnapshot, getActiveIds,
   getDefaultProjectId, getProjectsLoadState, openProjectAndChat,
   renameProject, reloadProjectsFromDatabase - see the PHASE 9B and
   NOTEBOOKS comments in app.js) plus functions that were already
   global: openQuizModal, createNewProject, createNewChat,
   deleteProject, _spAuthedGet, _spGroupBySubject.

   "Notebooks" is the student-facing name of what the data model
   still calls a project. Internal identifiers are unchanged on
   purpose: #projects-view, #nav-tab-projects, the `projects`
   array in app.js, the user_projects table.

   Nothing here duplicates app.js's data model, quiz logic, or
   project logic - every action delegates to the existing function
   that already does it.
   ============================================================ */

(function () {
    "use strict";

    var VIEWS = ["chat", "quizzes", "projects", "notes"];
    var STORAGE_KEY = "kognit-active-nav-tab";
    var quizzesLoadToken = 0;   // guards against a stale fetch finishing
    var projectsRendered = false;

    function tabButton(view) {
        return document.getElementById("nav-tab-" + view);
    }
    function viewPanel(view) {
        return document.getElementById(view + "-view");
    }

    /* --------------------------------------------------------
       Switching
       -------------------------------------------------------- */
    function switchTo(view, options) {
        if (VIEWS.indexOf(view) === -1) view = "chat";
        var focusTab = !(options && options.silent);

        VIEWS.forEach(function (v) {
            var isActive = v === view;
            var btn = tabButton(v);
            if (btn) {
                btn.classList.toggle("active", isActive);
                btn.setAttribute("aria-selected", isActive ? "true" : "false");
                btn.tabIndex = isActive ? 0 : -1;
            }
            var panel = viewPanel(v);
            if (panel) panel.classList.toggle("hidden", !isActive);
        });

        // The Chat destination also includes the sidebar's project/chat
        // history (Decision 9B.1) - hide it alongside #chat-view so the
        // Notebooks tab isn't showing the same list twice in two places.
        var chatSidebar = document.getElementById("chat-sidebar-section");
        if (chatSidebar) chatSidebar.classList.toggle("hidden", view !== "chat");

        try {
            window.localStorage.setItem(STORAGE_KEY, view);
        } catch (e) {
            // Not persisted this session - not fatal, defaults to Chat.
        }

        if (view === "quizzes") renderQuizzesView();
        if (view === "projects") renderProjectsView();
        if (view === "notes" && window.KognitNotes) window.KognitNotes.onShow();

        if (focusTab) {
            var activeBtn = tabButton(view);
            if (activeBtn) activeBtn.focus();
        }
    }

    function initialTab() {
        try {
            var saved = window.localStorage.getItem(STORAGE_KEY);
            if (VIEWS.indexOf(saved) !== -1) return saved;
        } catch (e) { /* fall through */ }
        return "chat";
    }

    /* --------------------------------------------------------
       Keyboard: standard ARIA tablist roving-tabindex pattern -
       Left/Right (and Home/End) move focus AND activate, matching
       how a tab list is expected to behave for screen-reader users.
       -------------------------------------------------------- */
    function wireKeyboard() {
        var rail = document.querySelector(".nav-rail");
        if (!rail) return;
        rail.addEventListener("keydown", function (event) {
            var currentIndex = VIEWS.findIndex(function (v) {
                return tabButton(v) === document.activeElement;
            });
            if (currentIndex === -1) return;

            var nextIndex = null;
            if (event.key === "ArrowRight" || event.key === "ArrowDown") {
                nextIndex = (currentIndex + 1) % VIEWS.length;
            } else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
                nextIndex = (currentIndex - 1 + VIEWS.length) % VIEWS.length;
            } else if (event.key === "Home") {
                nextIndex = 0;
            } else if (event.key === "End") {
                nextIndex = VIEWS.length - 1;
            }
            if (nextIndex !== null) {
                event.preventDefault();
                switchTo(VIEWS[nextIndex]);
            }
        });
    }

    function wireClicks() {
        VIEWS.forEach(function (view) {
            var btn = tabButton(view);
            if (btn) btn.addEventListener("click", function () { switchTo(view); });
        });
    }

    /* ==========================================================
       QUIZZES VIEW
       ==========================================================
       Two parts: a primary "Start a New Quiz" action that opens
       the EXISTING #quiz-modal unchanged, and a "Your Topics"
       section reusing the SAME /api/profile/snapshot data and the
       SAME grouping helper (_spGroupBySubject) the Student Profile
       panel already uses - not a second data source, not invented
       quiz history.
       ========================================================== */
    function quizzesViewShell() {
        return (
            '<div class="nav-view-inner">' +
              '<div class="nav-view-header">' +
                '<h2 class="nav-view-title">Quizzes</h2>' +
                '<p class="nav-view-subtitle">Test yourself on any topic, any time.</p>' +
              '</div>' +
              '<button type="button" class="quizzes-start-btn" id="quizzes-view-start-btn">' +
                'Start a New Quiz' +
              '</button>' +
              '<div class="nav-view-section">' +
                '<div class="nav-view-section-title">Your Topics</div>' +
                '<div id="quizzes-view-topics"></div>' +
              '</div>' +
            '</div>'
        );
    }

    // The auth client lives in app.js as a top-level `const supabaseClient`.
    // A top-level const is visible to every classic script by its bare name
    // but is NOT a property of `window`, so reading only `window.supabaseClient`
    // (what this view used to do) is always undefined - every signed-in student
    // was told to log in. notes.js and profile-menu.js already use this exact
    // lookup; this is the same one.
    function authClient() {
        try { if (typeof supabaseClient !== "undefined" && supabaseClient) return supabaseClient; } catch (e) { /* not defined */ }
        return window.supabaseClient || null;
    }

    // Distinguishes "no session" (logged out) from "could not find out"
    // (client missing / getSession threw): the second must never be shown as
    // a login prompt.
    async function readSession() {
        var client = authClient();
        if (!client || !client.auth || typeof client.auth.getSession !== "function") return { ok: false };
        try {
            var result = await client.auth.getSession();
            return { ok: true, session: result && result.data ? result.data.session : null };
        } catch (e) {
            return { ok: false };
        }
    }

    var lastQuizzesSignedIn = null;

    function quizzesPanelVisible() {
        var panel = viewPanel("quizzes");
        return !!panel && !panel.classList.contains("hidden");
    }

    function showTopicsMessage(topicsEl, text, retryLabel) {
        topicsEl.textContent = "";
        var box = document.createElement("div");
        box.className = "sp-empty-state";
        box.setAttribute("role", "status");
        box.appendChild(document.createTextNode(text));
        if (retryLabel) {
            var retry = document.createElement("button");
            retry.type = "button";
            retry.className = "quizzes-retry-btn";
            retry.textContent = retryLabel;
            retry.addEventListener("click", function () { renderQuizzesView(); });
            box.appendChild(retry);
        }
        topicsEl.appendChild(box);
    }

    async function renderQuizzesView() {
        var panel = viewPanel("quizzes");
        if (!panel) return;

        var myToken = ++quizzesLoadToken;
        panel.innerHTML = quizzesViewShell();

        var startBtn = document.getElementById("quizzes-view-start-btn");
        if (startBtn) startBtn.addEventListener("click", function () {
            window.openQuizModal();
        });

        var topicsEl = document.getElementById("quizzes-view-topics");
        if (!topicsEl) return;

        topicsEl.innerHTML = '<div class="nav-view-loading" role="status">Loading your topics\u2026</div>';
        topicsEl.setAttribute("aria-busy", "true");

        try {
            var auth = await readSession();
            if (myToken !== quizzesLoadToken) return; // a newer switch superseded this

            if (!auth.ok) {
                topicsEl.removeAttribute("aria-busy");
                showTopicsMessage(topicsEl, "Couldn\u2019t check your sign-in status right now.", "Try again");
                return;
            }

            lastQuizzesSignedIn = !!auth.session;
            if (!auth.session) {
                topicsEl.removeAttribute("aria-busy");
                topicsEl.innerHTML =
                    '<div class="sp-empty-state">Log in to see topics from your quiz history.</div>';
                return;
            }

            var snapshot = await window._spAuthedGet("/api/profile/snapshot", auth.session.access_token);
            if (myToken !== quizzesLoadToken) return;
            topicsEl.removeAttribute("aria-busy");

            var strengths = snapshot.strengths || [];
            var needsPractice = snapshot.needs_practice || [];

            if (strengths.length === 0 && needsPractice.length === 0) {
                // Two different truths share this empty list: no quiz has ever
                // been taken, or quizzes exist but no topic has enough
                // attempts yet to show a clear strength / area to practice
                // (the backend's own rule - at least 2 attempts on a topic).
                var summary = snapshot.evidence_summary || {};
                showTopicsMessage(topicsEl, summary.has_quiz_evidence
                    ? "Your quiz results are saved. A topic appears here once you have taken enough quizzes on it to show a clear strength or an area to practice."
                    : "Take a quiz to start building your topic history here.");
                return;
            }

            var html = "";
            if (needsPractice.length > 0) {
                html += '<div class="quizzes-topic-group-label">Needs practice</div>';
                html += needsPractice.slice(0, 5).map(function (entry) {
                    var label = entry.subject + (entry.topic ? " \u2014 " + entry.topic : "");
                    return (
                        '<button type="button" class="quizzes-topic-chip quizzes-topic-chip--needs-practice" ' +
                        'data-subject="' + escapeAttr(entry.subject || "") + '" ' +
                        'data-topic="' + escapeAttr(entry.topic || "") + '">' +
                        escapeText(label) + '</button>'
                    );
                }).join("");
            }
            if (strengths.length > 0) {
                html += '<div class="quizzes-topic-group-label">Strong topics</div>';
                var groups = window._spGroupBySubject(strengths);
                Array.from(groups.keys()).slice(0, 3).forEach(function (subject) {
                    Array.from(groups.get(subject)).slice(0, 4).forEach(function (topic) {
                        var label = subject + " \u2014 " + topic;
                        html +=
                            '<button type="button" class="quizzes-topic-chip" ' +
                            'data-subject="' + escapeAttr(subject) + '" ' +
                            'data-topic="' + escapeAttr(topic) + '">' +
                            escapeText(label) + '</button>';
                    });
                });
            }
            topicsEl.innerHTML = html;

            Array.prototype.forEach.call(
                topicsEl.querySelectorAll(".quizzes-topic-chip"),
                function (chip) {
                    chip.addEventListener("click", function () {
                        // Reuses the EXISTING topic-quiz launcher (already
                        // wired to the same #quiz-modal) rather than a new
                        // quiz-start path.
                        if (typeof window._spOpenQuizForTopic === "function") {
                            window._spOpenQuizForTopic(chip.dataset.subject, chip.dataset.topic);
                        } else {
                            window.openQuizModal();
                        }
                    });
                }
            );
        } catch (e) {
            if (myToken !== quizzesLoadToken) return;
            topicsEl.removeAttribute("aria-busy");
            // A failed request is an error, never an empty history.
            showTopicsMessage(topicsEl, "Couldn\u2019t load your topics right now.", "Try again");
        }
    }

    var quizzesWiringDone = false;
    function wireQuizzes() {
        if (quizzesWiringDone) return;
        quizzesWiringDone = true;

        // A quiz result that was really saved changes "Your Topics".
        document.addEventListener("kloriz:quiz-saved", function () {
            if (quizzesPanelVisible()) renderQuizzesView();
        });

        // Logging in / out while this page is open must not leave the old
        // message on screen. Only a real change of signed-in state re-renders
        // (Supabase also fires SIGNED_IN on tab refocus), and it is deferred a
        // tick because supabase-js must not be re-entered from its own
        // auth-state callback.
        var client = authClient();
        if (client && client.auth && typeof client.auth.onAuthStateChange === "function") {
            client.auth.onAuthStateChange(function (_event, session) {
                var signedIn = !!session;
                setTimeout(function () {
                    if (quizzesPanelVisible() && lastQuizzesSignedIn !== null && lastQuizzesSignedIn !== signedIn) {
                        renderQuizzesView();
                    }
                }, 0);
            });
        }
    }

    /* ==========================================================
       NOTEBOOKS VIEW (internal ids still say "projects")
       ==========================================================
       Built entirely from the SAME `projects` array app.js maintains
       (window.getProjectsSnapshot). Only data that really exists is
       shown: the title, the chat count, and a time derived from the
       server's updated_at (or, failing that, from the "proj_<ms>" id).
       No descriptions, progress or activity metrics are invented -
       the data model has none.

       Two screens share #projects-view:
         list   - search / sort / grid of notebook cards
         detail - one notebook: its chats, new chat, rename, delete
       ========================================================== */
    var NB_TITLE_MAX = 80;

    var nb = {
        query: "",
        sort: "updated",      // "updated" | "name" | "chats"
        openId: null,         // notebook shown in the detail screen
        menuFor: null,        // notebook id whose overflow menu is open
        mode: null,           // "create" | "rename" while the form dialog is open
        targetId: null,       // notebook being renamed
        deleteId: null,       // notebook awaiting delete confirmation
        busy: false,          // a create/rename/delete request is in flight
        toastTimer: null
    };

    function escapeText(s) {
        var div = document.createElement("div");
        div.textContent = s == null ? "" : String(s);
        return div.innerHTML;
    }
    function escapeAttr(s) {
        return escapeText(s).replace(/"/g, "&quot;");
    }

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function $(id) { return document.getElementById(id); }

    /* ---- icons (static markup, never built from user data) ---- */
    function svg(inner, size, fill) {
        return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="' + (fill ? "currentColor" : "none") +
            '" stroke="' + (fill ? "none" : "currentColor") + '" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
            inner + "</svg>";
    }
    var ICON = {
        notebook: '<path d="M6 3h12a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"></path><line x1="9" y1="3" x2="9" y2="21"></line><line x1="13" y1="8" x2="16" y2="8"></line><line x1="13" y1="12" x2="16" y2="12"></line>',
        chat: '<path d="M21 11.5a8.5 8.5 0 0 1-12.4 7.5L3 20.5l1.6-5.2A8.5 8.5 0 1 1 21 11.5z"></path>',
        calendar: '<rect x="3" y="4" width="18" height="18" rx="2"></rect><line x1="16" y1="2" x2="16" y2="6"></line><line x1="8" y1="2" x2="8" y2="6"></line><line x1="3" y1="10" x2="21" y2="10"></line>',
        arrowRight: '<line x1="5" y1="12" x2="19" y2="12"></line><polyline points="12 5 19 12 12 19"></polyline>',
        arrowLeft: '<line x1="19" y1="12" x2="5" y2="12"></line><polyline points="12 19 5 12 12 5"></polyline>',
        more: '<circle cx="12" cy="5" r="1.6"></circle><circle cx="12" cy="12" r="1.6"></circle><circle cx="12" cy="19" r="1.6"></circle>',
        plus: '<line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line>',
        search: '<circle cx="11" cy="11" r="7"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line>',
        sort: '<path d="M7 4v16"></path><polyline points="3 8 7 4 11 8"></polyline><path d="M17 20V4"></path><polyline points="13 16 17 20 21 16"></polyline>',
        chevron: '<polyline points="6 9 12 15 18 9"></polyline>',
        pencil: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"></path>',
        trash: '<polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path>',
        alert: '<circle cx="12" cy="12" r="9"></circle><line x1="12" y1="8" x2="12" y2="12.5"></line><line x1="12" y1="16" x2="12.01" y2="16"></line>',
        spark: '<path d="M19 3v4"></path><path d="M17 5h4"></path><path d="M5 17v3"></path><path d="M3.5 18.5h3"></path>'
    };
    function icon(name, size) {
        var span = el("span", "nb-icon");
        span.innerHTML = svg(ICON[name] || "", size || 18, name === "more");
        return span;
    }
    function iconText(name, size, label, className) {
        var span = el("span", className || "");
        var ic = icon(name, size);
        span.appendChild(ic);
        span.appendChild(document.createTextNode(label));
        return span;
    }

    /* ---- data helpers ---- */
    function defaultProjectId() {
        return typeof window.getDefaultProjectId === "function" ? window.getDefaultProjectId() : null;
    }

    // Real notebooks only: the reserved "Recent Chats" bucket is stored as a
    // project too, but it is not a notebook and must never be listed or deleted.
    function allNotebooks() {
        var reserved = defaultProjectId();
        return (window.getProjectsSnapshot() || []).filter(function (p) {
            return p && p.id !== reserved;
        });
    }
    function notebookById(id) {
        var list = allNotebooks();
        for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i];
        return null;
    }
    function chatCount(p) { return (p.chats || []).length; }
    function chatsLabel(n) { return n + (n === 1 ? " chat" : " chats"); }

    function idTimestamp(id, prefix) {
        var m = new RegExp("^" + prefix + "_(\\d{10,})$").exec(id || "");
        if (!m) return null;
        var ms = Number(m[1]);
        return isFinite(ms) ? ms : null;
    }
    function updatedMs(p) {
        if (!p.updated_at) return null;
        var ms = Date.parse(p.updated_at);
        return isFinite(ms) ? ms : null;
    }
    function createdMs(p) { return idTimestamp(p.id, "proj"); }
    function activityMs(p) { return updatedMs(p) || createdMs(p) || 0; }

    function relativeTime(ms, now) {
        var seconds = Math.max(0, Math.floor(((now || Date.now()) - ms) / 1000));
        if (seconds < 60) return "just now";
        var minutes = Math.floor(seconds / 60);
        if (minutes < 60) return minutes + (minutes === 1 ? " minute ago" : " minutes ago");
        var hours = Math.floor(minutes / 60);
        if (hours < 24) return hours + (hours === 1 ? " hour ago" : " hours ago");
        var days = Math.floor(hours / 24);
        if (days === 1) return "yesterday";
        if (days < 7) return days + " days ago";
        if (days < 30) { var w = Math.floor(days / 7); return w + (w === 1 ? " week ago" : " weeks ago"); }
        if (days < 365) { var mo = Math.floor(days / 30); return mo + (mo === 1 ? " month ago" : " months ago"); }
        var y = Math.floor(days / 365);
        return y + (y === 1 ? " year ago" : " years ago");
    }

    // "Updated 2 days ago" when the server timestamp exists, otherwise
    // "Created 2 days ago" from the id, otherwise nothing at all.
    function timeInfo(p) {
        var u = updatedMs(p);
        if (u) return { text: "Updated " + relativeTime(u), title: "Updated " + new Date(u).toLocaleString() };
        var c = createdMs(p);
        if (c) return { text: "Created " + relativeTime(c), title: "Created " + new Date(c).toLocaleString() };
        return null;
    }

    function byTitle(a, b) {
        return String(a.title).localeCompare(String(b.title), undefined, { sensitivity: "base", numeric: true });
    }
    function sortNotebooks(list, mode) {
        var copy = list.slice();
        copy.sort(function (a, b) {
            if (mode === "name") return byTitle(a, b);
            if (mode === "chats") return (chatCount(b) - chatCount(a)) || byTitle(a, b);
            return (activityMs(b) - activityMs(a)) || byTitle(a, b);
        });
        return copy;
    }
    function filterNotebooks(list, query) {
        var q = String(query || "").trim().toLowerCase();
        if (!q) return list;
        return list.filter(function (p) { return String(p.title).toLowerCase().indexOf(q) !== -1; });
    }

    function currentLoadState() {
        return typeof window.getProjectsLoadState === "function" ? window.getProjectsLoadState() : "ready";
    }
    function projectsPanelVisible() {
        var panel = viewPanel("projects");
        return !!panel && !panel.classList.contains("hidden");
    }

    /* ---- toast (one live region, outside the re-rendered panel) ---- */
    function showToast(message, kind) {
        var toast = $("nb-toast");
        if (!toast) {
            toast = el("div", "nb-toast");
            toast.id = "nb-toast";
            toast.setAttribute("role", "status");
            toast.setAttribute("aria-live", "polite");
            document.body.appendChild(toast);
        }
        toast.textContent = message;
        toast.className = "nb-toast nb-toast--visible" + (kind === "warning" ? " nb-toast--warning" : "");
        window.clearTimeout(nb.toastTimer);
        nb.toastTimer = window.setTimeout(function () {
            toast.className = "nb-toast";
        }, kind === "warning" ? 7000 : 3500);
    }

    /* ---- overflow menu ---- */
    function closeMenu(restoreFocus) {
        if (nb.menuFor == null) return;
        var panel = viewPanel("projects");
        var openBtn = panel && panel.querySelector('.nb-menu-btn[aria-expanded="true"]');
        var menu = panel && panel.querySelector(".nb-menu:not([hidden])");
        if (menu) menu.hidden = true;
        if (openBtn) {
            openBtn.setAttribute("aria-expanded", "false");
            if (restoreFocus) openBtn.focus();
        }
        var card = openBtn && openBtn.closest(".nb-card");
        if (card) card.classList.remove("nb-card--menu-open");
        nb.menuFor = null;
    }

    function openMenu(btn, menu, id) {
        closeMenu(false);
        menu.hidden = false;
        btn.setAttribute("aria-expanded", "true");
        var card = btn.closest(".nb-card");
        if (card) card.classList.add("nb-card--menu-open");
        nb.menuFor = id;
        var first = menu.querySelector('[role="menuitem"]');
        if (first) first.focus();
    }

    function wireMenuKeys(wrapper, btn, menu) {
        wrapper.addEventListener("keydown", function (event) {
            var items = Array.prototype.slice.call(menu.querySelectorAll('[role="menuitem"]'));
            var open = !menu.hidden;
            if (!open) {
                if (event.target === btn && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
                    event.preventDefault();
                    btn.click();
                }
                return;
            }
            var index = items.indexOf(document.activeElement);
            if (event.key === "Escape") {
                event.preventDefault();
                event.stopPropagation();
                closeMenu(true);
            } else if (event.key === "Tab") {
                closeMenu(false);
            } else if (event.key === "ArrowDown") {
                event.preventDefault();
                items[(index + 1 + items.length) % items.length].focus();
            } else if (event.key === "ArrowUp") {
                event.preventDefault();
                items[(index - 1 + items.length) % items.length].focus();
            } else if (event.key === "Home") {
                event.preventDefault();
                items[0].focus();
            } else if (event.key === "End") {
                event.preventDefault();
                items[items.length - 1].focus();
            }
        });
    }

    function buildMenu(p) {
        var wrapper = el("div", "nb-menu-wrap");

        var btn = el("button", "nb-menu-btn");
        btn.type = "button";
        btn.setAttribute("aria-haspopup", "menu");
        btn.setAttribute("aria-expanded", "false");
        btn.setAttribute("aria-label", "More options for " + p.title);
        btn.appendChild(icon("more", 18));

        var menu = el("div", "nb-menu");
        menu.setAttribute("role", "menu");
        menu.setAttribute("aria-label", "Options for " + p.title);
        menu.hidden = true;

        var rename = el("button", "nb-menu-item");
        rename.type = "button";
        rename.setAttribute("role", "menuitem");
        rename.appendChild(icon("pencil", 16));
        rename.appendChild(document.createTextNode("Rename"));
        rename.addEventListener("click", function () {
            closeMenu(false);
            openFormDialog("rename", p.id, btn);
        });

        var del = el("button", "nb-menu-item nb-menu-item--danger");
        del.type = "button";
        del.setAttribute("role", "menuitem");
        del.appendChild(icon("trash", 16));
        del.appendChild(document.createTextNode("Delete"));
        del.addEventListener("click", function () {
            closeMenu(false);
            openDeleteDialog(p.id, btn);
        });

        menu.appendChild(rename);
        menu.appendChild(del);

        btn.addEventListener("click", function () {
            if (menu.hidden) openMenu(btn, menu, p.id);
            else closeMenu(true);
        });
        wireMenuKeys(wrapper, btn, menu);

        wrapper.appendChild(btn);
        wrapper.appendChild(menu);
        return wrapper;
    }

    /* ---- screens ---- */
    function startChatingIn(projId, chatId) {
        if (chatId) window.openProjectAndChat(projId, chatId);
        else window.createNewChat(projId);
        switchTo("chat");
    }

    function openNotebookDetail(id) {
        nb.openId = id;
        renderProjectsView();
        focusHeading();
    }

    function closeNotebookDetail() {
        var leaving = nb.openId;
        nb.openId = null;
        renderProjectsView();
        var panel = viewPanel("projects");
        var back = panel && leaving && panel.querySelector('.nb-card[data-notebook-id="' + leaving + '"] .nb-open-btn');
        if (back) back.focus(); else focusHeading();
    }

    function focusHeading() {
        var panel = viewPanel("projects");
        var h = panel && panel.querySelector(".nb-title");
        if (h) h.focus();
    }

    function buildHeader(title, subtitle, opts) {
        var header = el("div", "nb-header");
        var lead = el("div", "nb-header-lead");
        lead.appendChild(icon("notebook", 26));
        var text = el("div", "nb-header-text");
        var h = el("h2", "nb-title", title);
        h.tabIndex = -1;
        text.appendChild(h);
        if (subtitle) text.appendChild(el("p", "nb-subtitle", subtitle));
        lead.appendChild(text);
        header.appendChild(lead);
        return { header: header, text: text };
    }

    function buildNewButton(label, disabled) {
        var btn = el("button", "nb-primary-btn");
        btn.type = "button";
        btn.id = "projects-view-new-btn";
        btn.appendChild(icon("plus", 16));
        btn.appendChild(document.createTextNode(label));
        if (disabled) {
            btn.disabled = true;
            btn.setAttribute("aria-disabled", "true");
        }
        btn.addEventListener("click", function () { openFormDialog("create", null, btn); });
        return btn;
    }

    function buildSkeleton() {
        var wrap = el("div", "nb-loading");
        wrap.setAttribute("role", "status");
        wrap.setAttribute("aria-live", "polite");
        wrap.appendChild(el("span", "nb-sr-only", "Loading your notebooks\u2026"));
        var grid = el("div", "nb-grid nb-grid--skeleton");
        grid.setAttribute("aria-hidden", "true");
        for (var i = 0; i < 4; i++) {
            var card = el("div", "nb-skel-card");
            card.appendChild(el("div", "nb-skel nb-skel-icon"));
            card.appendChild(el("div", "nb-skel nb-skel-line"));
            card.appendChild(el("div", "nb-skel nb-skel-line nb-skel-line--short"));
            card.appendChild(el("div", "nb-skel nb-skel-btn"));
            grid.appendChild(card);
        }
        wrap.appendChild(grid);
        return wrap;
    }

    function buildEmptyState() {
        var box = el("div", "nb-empty");
        var art = el("div", "nb-empty-art");
        art.innerHTML = svg(ICON.notebook + ICON.spark, 40);
        box.appendChild(art);
        box.appendChild(el("h3", "nb-empty-title", "Create your first notebook"));
        box.appendChild(el("p", "nb-empty-text", "Organize your study chats by subject, chapter or learning goal."));
        var btn = el("button", "nb-primary-btn");
        btn.type = "button";
        btn.id = "projects-view-empty-create-btn";
        btn.appendChild(icon("plus", 16));
        btn.appendChild(document.createTextNode("Create notebook"));
        btn.addEventListener("click", function () { openFormDialog("create", null, btn); });
        box.appendChild(btn);
        return box;
    }

    function buildDegradedBanner() {
        var banner = el("div", "nb-banner");
        banner.setAttribute("role", "alert");
        banner.appendChild(icon("alert", 18));
        banner.appendChild(el("span", "nb-banner-text",
            "We couldn\u2019t load your notebooks from your account. Showing the copy saved on this device."));
        var retry = el("button", "nb-ghost-btn", "Retry");
        retry.type = "button";
        retry.addEventListener("click", function () {
            retry.disabled = true;
            retry.textContent = "Retrying\u2026";
            Promise.resolve(window.reloadProjectsFromDatabase && window.reloadProjectsFromDatabase())
                .then(function () { if (projectsPanelVisible()) renderProjectsView(); })
                .catch(function () { if (projectsPanelVisible()) renderProjectsView(); });
        });
        banner.appendChild(retry);
        return banner;
    }

    function buildToolbar() {
        var bar = el("div", "nb-toolbar");

        var search = el("div", "nb-search");
        search.appendChild(icon("search", 18));
        var input = el("input", "nb-search-input");
        input.type = "search";
        input.id = "notebook-search-input";
        input.placeholder = "Search notebooks\u2026";
        input.setAttribute("aria-label", "Search notebooks");
        input.autocomplete = "off";
        input.value = nb.query;
        input.addEventListener("input", function () {
            nb.query = input.value;
            closeMenu(false);
            renderResults();
        });
        search.appendChild(input);
        bar.appendChild(search);

        var sortWrap = el("label", "nb-sort");
        sortWrap.appendChild(el("span", "nb-sr-only", "Sort notebooks"));
        sortWrap.appendChild(icon("sort", 16));
        var select = el("select", "nb-sort-select");
        select.id = "notebook-sort-select";
        [["updated", "Recently updated"], ["name", "Name (A\u2013Z)"], ["chats", "Most chats"]].forEach(function (opt) {
            var o = el("option", "", opt[1]);
            o.value = opt[0];
            if (opt[0] === nb.sort) o.selected = true;
            select.appendChild(o);
        });
        select.addEventListener("change", function () {
            nb.sort = select.value;
            closeMenu(false);
            renderResults();
        });
        sortWrap.appendChild(select);
        sortWrap.appendChild(icon("chevron", 14));
        bar.appendChild(sortWrap);
        return bar;
    }

    function buildCard(p, isCurrent) {
        var item = el("li", "nb-card" + (isCurrent ? " nb-card--current" : ""));
        item.setAttribute("data-notebook-id", p.id);

        var top = el("div", "nb-card-top");
        var tile = el("span", "nb-card-icon");
        tile.appendChild(icon("notebook", 22));
        top.appendChild(tile);
        if (isCurrent) top.appendChild(el("span", "nb-current-pill", "Current"));
        top.appendChild(buildMenu(p));
        item.appendChild(top);

        var title = el("h3", "nb-card-title", p.title);
        title.title = p.title;
        item.appendChild(title);

        var meta = el("div", "nb-card-meta");
        meta.appendChild(iconText("chat", 14, chatsLabel(chatCount(p)), "nb-meta-item"));
        var t = timeInfo(p);
        if (t) {
            var when = iconText("calendar", 14, t.text, "nb-meta-item");
            when.title = t.title;
            meta.appendChild(when);
        }
        item.appendChild(meta);

        var open = el("button", "nb-open-btn");
        open.type = "button";
        open.setAttribute("aria-label", "Open " + p.title);
        open.appendChild(document.createTextNode("Open"));
        open.appendChild(icon("arrowRight", 16));
        open.addEventListener("click", function () { openNotebookDetail(p.id); });
        item.appendChild(open);

        return item;
    }

    // Only the results area is rebuilt while typing, so the search box keeps focus.
    function renderResults() {
        var host = $("nb-results");
        if (!host) return;
        host.textContent = "";

        var all = allNotebooks();
        var visible = sortNotebooks(filterNotebooks(all, nb.query), nb.sort);

        if (visible.length === 0) {
            var none = el("div", "nb-no-results");
            none.setAttribute("role", "status");
            none.appendChild(el("p", "nb-no-results-text", "No notebooks match \u201c" + String(nb.query).trim() + "\u201d."));
            var clear = el("button", "nb-ghost-btn", "Clear search");
            clear.type = "button";
            clear.addEventListener("click", function () {
                nb.query = "";
                var input = $("notebook-search-input");
                if (input) { input.value = ""; input.focus(); }
                renderResults();
            });
            none.appendChild(clear);
            host.appendChild(none);
            return;
        }

        var ids = (window.getActiveIds && window.getActiveIds()) || {};
        var grid = el("ul", "nb-grid");
        grid.setAttribute("aria-label", "Your notebooks");
        visible.forEach(function (p) {
            grid.appendChild(buildCard(p, p.id === ids.activeProjectId));
        });
        host.appendChild(grid);
    }

    function buildList() {
        var page = el("div", "nb-page");
        var state = currentLoadState();
        var all = allNotebooks();

        var parts = buildHeader("Notebooks", "Keep related chats and study materials organized in one place.");
        // Creating while the account's data is still arriving would be
        // overwritten by that load, so the button waits for it.
        parts.header.appendChild(buildNewButton("New notebook", state === "loading"));
        page.appendChild(parts.header);

        if (state === "loading") {
            page.appendChild(buildSkeleton());
            return page;
        }
        if (state === "degraded") page.appendChild(buildDegradedBanner());

        if (all.length === 0) {
            page.appendChild(buildEmptyState());
            return page;
        }

        page.appendChild(buildToolbar());
        var results = el("div", "nb-results");
        results.id = "nb-results";
        page.appendChild(results);
        return page;
    }

    function buildDetail(p) {
        var page = el("div", "nb-page");

        var back = el("button", "nb-back-btn");
        back.type = "button";
        back.appendChild(icon("arrowLeft", 16));
        back.appendChild(document.createTextNode("Back to Notebooks"));
        back.addEventListener("click", closeNotebookDetail);
        page.appendChild(back);

        var count = chatCount(p);
        var t = timeInfo(p);
        var subtitle = chatsLabel(count) + (t ? " \u00b7 " + t.text : "");
        var parts = buildHeader(p.title, subtitle);
        var actions = el("div", "nb-header-actions");

        var newChat = el("button", "nb-primary-btn");
        newChat.type = "button";
        newChat.id = "notebook-detail-new-chat-btn";
        newChat.appendChild(icon("plus", 16));
        newChat.appendChild(document.createTextNode("New chat"));
        newChat.addEventListener("click", function () { startChatingIn(p.id, null); });
        actions.appendChild(newChat);

        var rename = el("button", "nb-ghost-btn");
        rename.type = "button";
        rename.appendChild(icon("pencil", 15));
        rename.appendChild(document.createTextNode("Rename"));
        rename.addEventListener("click", function () { openFormDialog("rename", p.id, rename); });
        actions.appendChild(rename);

        var del = el("button", "nb-ghost-btn nb-ghost-btn--danger");
        del.type = "button";
        del.appendChild(icon("trash", 15));
        del.appendChild(document.createTextNode("Delete"));
        del.addEventListener("click", function () { openDeleteDialog(p.id, del); });
        actions.appendChild(del);

        parts.header.appendChild(actions);
        page.appendChild(parts.header);

        page.appendChild(el("h3", "nb-section-title", "Chats in this notebook"));

        var chats = p.chats || [];
        if (chats.length === 0) {
            var empty = el("div", "nb-empty nb-empty--inline");
            empty.appendChild(el("p", "nb-empty-text", "No chats in this notebook yet. Start one to keep your questions together."));
            page.appendChild(empty);
            return page;
        }

        var list = el("ul", "nb-chat-list");
        chats.forEach(function (chat) {
            var li = el("li", "nb-chat-item");
            var row = el("button", "nb-chat-row");
            row.type = "button";
            row.appendChild(icon("chat", 16));
            var text = el("span", "nb-chat-text");
            text.appendChild(el("span", "nb-chat-title", chat.title || "Untitled chat"));
            var bits = [];
            var started = idTimestamp(chat.id, "chat");
            if (started) bits.push("Started " + relativeTime(started));
            var msgs = (chat.messages || []).length;
            bits.push(msgs === 0 ? "No messages yet" : msgs + (msgs === 1 ? " message" : " messages"));
            text.appendChild(el("span", "nb-chat-meta", bits.join(" \u00b7 ")));
            row.appendChild(text);
            row.appendChild(icon("arrowRight", 16));
            row.addEventListener("click", function () { startChatingIn(p.id, chat.id); });
            li.appendChild(row);
            list.appendChild(li);
        });
        page.appendChild(list);
        return page;
    }

    function renderProjectsView() {
        var panel = viewPanel("projects");
        if (!panel) return;

        nb.menuFor = null;
        var notebook = nb.openId ? notebookById(nb.openId) : null;
        if (nb.openId && !notebook) nb.openId = null;

        panel.textContent = "";
        panel.appendChild(notebook ? buildDetail(notebook) : buildList());
        if (!notebook) renderResults();
    }

    /* ==========================================================
       DIALOGS - create / rename, and delete confirmation
       ==========================================================
       The markup lives in templates/index.html as ordinary
       .modal-overlay elements, so ui-core.js already gives them
       focus trapping, Escape and overlay-click dismissal. While a
       request is in flight the element carries data-busy="true",
       which ui-core's NON_DISMISSIBLE guard reads to block both.
       ========================================================== */
    function setFieldError(message) {
        var err = $("notebook-name-error");
        var input = $("notebook-name-input");
        if (!err || !input) return;
        if (message) {
            err.textContent = message;
            err.classList.remove("hidden");
            input.setAttribute("aria-invalid", "true");
        } else {
            err.textContent = "";
            err.classList.add("hidden");
            input.removeAttribute("aria-invalid");
        }
    }

    function setFormBusy(busy) {
        nb.busy = busy;
        var modal = $("notebook-modal");
        var submit = $("notebook-submit");
        if (modal) {
            if (busy) modal.setAttribute("data-busy", "true"); else modal.removeAttribute("data-busy");
        }
        ["notebook-name-input", "notebook-cancel", "notebook-modal-close"].forEach(function (id) {
            var node = $(id);
            if (node) node.disabled = busy;
        });
        if (submit) {
            submit.disabled = busy;
            submit.setAttribute("aria-busy", busy ? "true" : "false");
            submit.textContent = busy
                ? (nb.mode === "rename" ? "Saving\u2026" : "Creating\u2026")
                : (nb.mode === "rename" ? "Save" : "Create");
        }
    }

    var dialogOpener = null;

    function openFormDialog(mode, id, opener) {
        var modal = $("notebook-modal");
        var input = $("notebook-name-input");
        if (!modal || !input) return;
        if (mode === "create" && currentLoadState() === "loading") return;

        var target = id ? notebookById(id) : null;
        if (mode === "rename" && !target) return;

        nb.mode = mode;
        nb.targetId = id || null;
        dialogOpener = opener || null;

        $("notebook-modal-title").textContent = mode === "rename" ? "Rename notebook" : "Create notebook";
        input.value = mode === "rename" ? target.title : "";
        input.maxLength = NB_TITLE_MAX;
        setFieldError("");
        setFormBusy(false);

        modal.classList.remove("hidden");
        window.setTimeout(function () {
            input.focus();
            if (mode === "rename") input.select();
        }, 30);
    }

    window.closeNotebookModal = function () {
        var modal = $("notebook-modal");
        if (!modal || nb.busy) return;
        modal.classList.add("hidden");
        nb.mode = null;
        nb.targetId = null;
        restoreFocusTo(dialogOpener);
        dialogOpener = null;
    };

    function restoreFocusTo(node) {
        window.setTimeout(function () {
            if (node && document.body.contains(node) && typeof node.focus === "function") {
                node.focus();
            } else if (projectsPanelVisible()) {
                focusHeading();
            }
        }, 0);
    }

    function validateName(raw) {
        var value = String(raw || "").replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim();
        if (!value) return { error: "Enter a name for your notebook." };
        if (value.length > NB_TITLE_MAX) return { error: "Use " + NB_TITLE_MAX + " characters or fewer." };
        return { value: value };
    }

    async function submitForm(event) {
        event.preventDefault();
        if (nb.busy) return;

        var input = $("notebook-name-input");
        var checked = validateName(input.value);
        if (checked.error) {
            setFieldError(checked.error);
            input.focus();
            return;
        }
        setFieldError("");

        var mode = nb.mode;
        var targetId = nb.targetId;

        if (mode === "rename") {
            var current = notebookById(targetId);
            if (!current) { forceCloseForm(); renderProjectsView(); return; }
            if (current.title === checked.value) { window.closeNotebookModal(); return; }
        }

        setFormBusy(true);
        try {
            if (mode === "create") {
                var pending = window.createNewProject(checked.value);
                // createNewProject() makes the new notebook the active one
                // synchronously, before its save has finished.
                var newId = (window.getActiveIds() || {}).activeProjectId;
                var saved = await pending;

                forceCloseForm();
                nb.query = "";
                nb.openId = newId;
                renderProjectsView();
                focusHeading();
                if (saved && saved.signedIn && !saved.savedRemotely) {
                    showToast("Notebook created on this device, but it couldn\u2019t sync to your account yet.", "warning");
                } else {
                    showToast("Notebook created.");
                }
            } else {
                var result = await window.renameProject(targetId, checked.value);
                if (result && result.ok) {
                    forceCloseForm();
                    renderProjectsView();
                    showToast("Notebook renamed.");
                } else if (result && result.reason === "missing") {
                    forceCloseForm();
                    renderProjectsView();
                } else {
                    setFormBusy(false);
                    setFieldError("Couldn\u2019t save the new name. Check your connection and try again.");
                    input.focus();
                }
            }
        } catch (e) {
            console.error("Notebook form error:", e);
            setFormBusy(false);
            setFieldError(mode === "rename"
                ? "Couldn\u2019t save the new name. Please try again."
                : "Couldn\u2019t create the notebook. Please try again.");
            input.focus();
        }
    }

    // Closes the form after a request finished (busy is cleared first, since
    // closeNotebookModal refuses to close while a request is in flight).
    function forceCloseForm() {
        nb.busy = false;
        var modal = $("notebook-modal");
        if (modal) modal.removeAttribute("data-busy");
        window.closeNotebookModal();
    }

    /* ---- delete confirmation ---- */
    var deleteOpener = null;

    function setDeleteError(message) {
        var err = $("notebook-delete-error");
        if (!err) return;
        if (message) { err.textContent = message; err.classList.remove("hidden"); }
        else { err.textContent = ""; err.classList.add("hidden"); }
    }

    function setDeleteBusy(busy) {
        nb.busy = busy;
        var modal = $("notebook-delete-modal");
        if (modal) {
            if (busy) modal.setAttribute("data-busy", "true"); else modal.removeAttribute("data-busy");
        }
        var confirmBtn = $("notebook-delete-confirm");
        var cancelBtn = $("notebook-delete-cancel");
        if (confirmBtn) {
            confirmBtn.disabled = busy;
            confirmBtn.setAttribute("aria-busy", busy ? "true" : "false");
            confirmBtn.textContent = busy ? "Deleting\u2026" : "Delete notebook";
        }
        if (cancelBtn) cancelBtn.disabled = busy;
    }

    function openDeleteDialog(id, opener) {
        var modal = $("notebook-delete-modal");
        var p = notebookById(id);
        if (!modal || !p) return;

        nb.deleteId = id;
        deleteOpener = opener || null;

        var n = chatCount(p);
        // Accurate to the app's real behavior: a notebook's chats are stored
        // inside the notebook's own record, so deleting it deletes them.
        var desc = $("notebook-delete-desc");
        desc.textContent = "\u201c" + p.title + "\u201d" +
            (n > 0 ? " and its " + chatsLabel(n) + " will be permanently deleted. " : " will be permanently deleted. ") +
            "This can\u2019t be undone. Pocket Notes you saved from these chats are kept, but their \u201cOpen original chat\u201d link will stop working.";

        setDeleteError("");
        setDeleteBusy(false);
        modal.classList.remove("hidden");
    }

    window.closeNotebookDeleteModal = function () {
        var modal = $("notebook-delete-modal");
        if (!modal || nb.busy) return;
        modal.classList.add("hidden");
        nb.deleteId = null;
        restoreFocusTo(deleteOpener);
        deleteOpener = null;
    };

    async function confirmDelete() {
        if (nb.busy || !nb.deleteId) return;
        var id = nb.deleteId;
        setDeleteError("");
        setDeleteBusy(true);
        try {
            var ok = await window.deleteProject(id, { confirmed: true });
            if (ok) {
                nb.busy = false;
                var modal = $("notebook-delete-modal");
                if (modal) { modal.removeAttribute("data-busy"); modal.classList.add("hidden"); }
                nb.deleteId = null;
                deleteOpener = null;
                if (nb.openId === id) nb.openId = null;
                renderProjectsView();
                focusHeading();
                showToast("Notebook deleted.");
            } else {
                setDeleteBusy(false);
                setDeleteError("Couldn\u2019t delete this notebook. Check your connection and try again.");
            }
        } catch (e) {
            console.error("Notebook delete error:", e);
            setDeleteBusy(false);
            setDeleteError("Couldn\u2019t delete this notebook. Please try again.");
        }
    }

    var notebookWiringDone = false;
    function wireNotebooks() {
        if (notebookWiringDone) return;
        notebookWiringDone = true;

        var form = $("notebook-form");
        if (form) form.addEventListener("submit", submitForm);

        var cancel = $("notebook-cancel");
        if (cancel) cancel.addEventListener("click", function () { window.closeNotebookModal(); });
        var closeX = $("notebook-modal-close");
        if (closeX) closeX.addEventListener("click", function () { window.closeNotebookModal(); });
        var input = $("notebook-name-input");
        if (input) input.addEventListener("input", function () { setFieldError(""); });

        var delCancel = $("notebook-delete-cancel");
        if (delCancel) delCancel.addEventListener("click", function () { window.closeNotebookDeleteModal(); });
        var delConfirm = $("notebook-delete-confirm");
        if (delConfirm) delConfirm.addEventListener("click", confirmDelete);

        // Click outside an open overflow menu closes it. Registered once.
        document.addEventListener("pointerdown", function (event) {
            if (nb.menuFor == null) return;
            if (event.target.closest && event.target.closest(".nb-menu-wrap")) return;
            closeMenu(false);
        });

        // The first project load (and any retry) finishes after this module
        // has already drawn, so the page repaints when the state changes.
        document.addEventListener("kloriz:projects-state", function () {
            if (projectsPanelVisible()) renderProjectsView();
        });
    }

    /* --------------------------------------------------------
       Boot
       -------------------------------------------------------- */
    function init() {
        wireClicks();
        wireKeyboard();
        wireNotebooks();
        wireQuizzes();
        switchTo(initialTab(), { silent: true });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    window.KognitNav = {
        switchTo: switchTo,
        renderQuizzesView: renderQuizzesView,
        renderProjectsView: renderProjectsView,
        renderNotebooksView: renderProjectsView
    };
})();