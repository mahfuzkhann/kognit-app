/* ============================================================
   KOGNIT NAV VIEWS — PHASE 9B
   ============================================================
   Top-level navigation: Chat / Quizzes / Projects (Decision 3 -
   no Home). Loaded AFTER app.js (see templates/index.html), and
   depends only on the explicit accessor functions app.js exposes
   for this purpose (window.getProjectsSnapshot, getActiveIds,
   startRenamingProject, openProjectAndChat - see the PHASE 9B
   comment in app.js) plus functions that were already global:
   openQuizModal, createNewProject, deleteProject, renderHistoryList,
   _spAuthedGet, _spGroupBySubject.

   Nothing here duplicates app.js's data model, quiz logic, or
   project logic - every action delegates to the existing function
   that already does it.
   ============================================================ */

(function () {
    "use strict";

    var VIEWS = ["chat", "quizzes", "projects"];
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
        // Projects tab isn't showing the same list twice in two places.
        var chatSidebar = document.getElementById("chat-sidebar-section");
        if (chatSidebar) chatSidebar.classList.toggle("hidden", view !== "chat");

        try {
            window.localStorage.setItem(STORAGE_KEY, view);
        } catch (e) {
            // Not persisted this session - not fatal, defaults to Chat.
        }

        if (view === "quizzes") renderQuizzesView();
        if (view === "projects") renderProjectsView();

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

        topicsEl.innerHTML = '<div class="nav-view-loading">Loading your topics\u2026</div>';

        try {
            var session = await getSessionSafely();
            if (myToken !== quizzesLoadToken) return; // a newer switch superseded this

            if (!session) {
                topicsEl.innerHTML =
                    '<div class="sp-empty-state">Log in to see topics from your quiz history.</div>';
                return;
            }

            var snapshot = await window._spAuthedGet("/api/profile/snapshot", session.access_token);
            if (myToken !== quizzesLoadToken) return;

            var strengths = snapshot.strengths || [];
            var needsPractice = snapshot.needs_practice || [];

            if (strengths.length === 0 && needsPractice.length === 0) {
                topicsEl.innerHTML =
                    '<div class="sp-empty-state">Take a quiz to start building your topic history here.</div>';
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
            topicsEl.innerHTML =
                '<div class="sp-empty-state">Couldn\u2019t load your topics right now.</div>';
        }
    }

    async function getSessionSafely() {
        try {
            var result = await window.supabaseClient.auth.getSession();
            return result && result.data ? result.data.session : null;
        } catch (e) {
            return null;
        }
    }

    /* ==========================================================
       PROJECTS VIEW
       ==========================================================
       A dedicated view (Decision 4), built entirely from the
       SAME `projects` array app.js maintains. Only title and a
       derived chat count are shown - no timestamps, no activity
       metrics, because the data model has none and Decision 4 is
       explicit: don't invent metadata that doesn't exist.
       ========================================================== */
    function escapeText(s) {
        var div = document.createElement("div");
        div.textContent = s == null ? "" : String(s);
        return div.innerHTML;
    }
    function escapeAttr(s) {
        return escapeText(s).replace(/"/g, "&quot;");
    }

    function renderProjectsView() {
        var panel = viewPanel("projects");
        if (!panel) return;

        var projects = window.getProjectsSnapshot() || [];
        var active = window.getActiveIds() || {};

        var header =
            '<div class="nav-view-inner">' +
              '<div class="nav-view-header">' +
                '<h2 class="nav-view-title">Projects</h2>' +
                '<p class="nav-view-subtitle">Group related chats together.</p>' +
              '</div>' +
              '<button type="button" class="quizzes-start-btn" id="projects-view-new-btn">+ New Project</button>' +
              '<div class="projects-grid" id="projects-view-grid"></div>' +
            '</div>';
        panel.innerHTML = header;

        var newBtn = document.getElementById("projects-view-new-btn");
        if (newBtn) newBtn.addEventListener("click", function () {
            window.createNewProject();
            switchTo("chat"); // the new project opens as an active chat
        });

        var grid = document.getElementById("projects-view-grid");
        if (!grid) return;

        if (projects.length === 0) {
            grid.innerHTML = '<div class="sp-empty-state">No projects yet. Create one to get started.</div>';
            return;
        }

        grid.innerHTML = projects.map(function (proj) {
            var chatCount = (proj.chats || []).length;
            var isActive = proj.id === active.activeProjectId;
            return (
                '<div class="project-card-tile' + (isActive ? ' project-card-tile--active' : '') + '" ' +
                'data-project-id="' + escapeAttr(proj.id) + '">' +
                  '<div class="project-card-tile-title">' + escapeText(proj.title) + '</div>' +
                  '<div class="project-card-tile-meta">' +
                    chatCount + (chatCount === 1 ? ' chat' : ' chats') +
                  '</div>' +
                  '<div class="project-card-tile-actions">' +
                    '<button type="button" class="project-card-tile-open">Open</button>' +
                    '<button type="button" class="project-card-tile-rename" aria-label="Rename project">' +
                      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"></path></svg>' +
                    '</button>' +
                    '<button type="button" class="project-card-tile-delete" aria-label="Delete project">' +
                      '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path></svg>' +
                    '</button>' +
                  '</div>' +
                '</div>'
            );
        }).join("");

        Array.prototype.forEach.call(grid.querySelectorAll(".project-card-tile"), function (card) {
            var projId = card.dataset.projectId;
            var proj = projects.find(function (p) { return p.id === projId; });
            if (!proj) return;

            var openTo = function () {
                var firstChat = proj.chats && proj.chats[0];
                if (firstChat) {
                    window.openProjectAndChat(proj.id, firstChat.id);
                } else {
                    window.createNewChat(proj.id);
                }
                switchTo("chat");
            };

            card.querySelector(".project-card-tile-open").addEventListener("click", openTo);

            card.querySelector(".project-card-tile-rename").addEventListener("click", function (e) {
                e.stopPropagation();
                window.startRenamingProject(proj.id);
                switchTo("chat"); // the existing rename UI lives in the sidebar list
            });

            card.querySelector(".project-card-tile-delete").addEventListener("click", function (e) {
                e.stopPropagation();
                // deleteProject() already contains its own confirm() guard
                // and its own re-render of the sidebar - reused as-is.
                window.deleteProject(proj.id).then(function () {
                    renderProjectsView();
                });
            });
        });
    }

    /* --------------------------------------------------------
       Boot
       -------------------------------------------------------- */
    function init() {
        wireClicks();
        wireKeyboard();
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
        renderProjectsView: renderProjectsView
    };
})();
