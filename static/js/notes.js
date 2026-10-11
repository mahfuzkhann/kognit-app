/* ============================================================
   KOGNIT POCKET NOTES - frontend
   ============================================================
   Loaded after app.js (it uses app.js globals: getProjectsSnapshot,
   protectMathSegments / restoreProtectedMathSegments /
   sanitizeRenderedHtml, typesetMathJax, isClientSafeCitationUrl,
   openProjectAndChat) and after notes-store.js (the IndexedDB cache).

   WHAT THIS FILE DOES
     1. Save button for the answer toolbar (KognitNotes.buildSaveButton,
        called from app.js:buildBotToolbar).
     2. The "Pocket Notes" view: list, search, tag filter, note reader,
        tag editing, optional AI topic suggestions, flashcards and a
        self-graded mini-quiz.
     3. Read-only offline reading: online loads refresh the IndexedDB cache
        from the server; offline loads read that cache. No offline writes.
     4. Registers the service worker (static/sw.js) and asks it whether the
        app shell is genuinely cached, so "available offline" is only ever
        shown when it is true.

   SECURITY NOTES
     * Every piece of user/AI text is inserted with textContent. The ONLY
       innerHTML is the note body, which goes through the exact same
       marked -> sanitizeRenderedHtml (DOMPurify) pipeline as chat
       answers; if that pipeline is unavailable the body falls back to
       plain text.
     * The access token is read fresh from the Supabase session for every
       request; nothing about the session is cached here.
     * Nothing is written to Cache Storage by this file; note data lives
       only in the per-user IndexedDB cache, wiped on sign-out.
   ============================================================ */
(function () {
    "use strict";

    // Same vocabulary as backend/learning_memory.py:_SUBJECT_KEYWORDS keys.
    // Used only for the one-click "set subject" suggestions when the
    // backend could not establish a subject on its own.
    var SUBJECTS = ["Physics", "Chemistry", "Biology", "Higher Math", "ICT", "Accounting",
        "Business Studies", "Finance & Banking", "Bangla", "English", "History", "Civics"];
    var MAX_LIST_PAGES = 12;          // 12 x 50 covers the 500-note per-user cap
    var PAGE_SIZE = 50;
    var SW_STATUS_TIMEOUT_MS = 2500;

    var state = {
        notes: [],
        loaded: false,
        loading: false,
        error: null,           // { message } shown with a Retry button
        fromCache: false,      // true => what is on screen is an OFFLINE COPY, not server-confirmed
        syncedAt: null,
        userId: null,
        cachedIds: {},         // note id -> true, only for notes confirmed written to IndexedDB
        storageOk: true,
        sw: null,              // last GET_STATUS reply, or null when unknown / no controller
        swSupported: false,
        query: "",
        activeTag: null,
        selectedId: null,
        busy: false,           // a mutation is in flight
        savedClientIds: {},    // client_id -> note id, for pre-marking Save buttons
        study: null            // see openStudy()
    };

    var shell = null;          // persistent DOM refs for #notes-view

    // ------------------------------------------------------------
    // small helpers
    // ------------------------------------------------------------

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function isOffline() {
        return typeof navigator !== "undefined" && navigator.onLine === false;
    }

    function authClient() {
        try { if (typeof supabaseClient !== "undefined" && supabaseClient) return supabaseClient; } catch (e) { /* not defined */ }
        return window.supabaseClient || null;
    }

    function getSession() {
        var client = authClient();
        if (!client) return Promise.resolve(null);
        return client.auth.getSession().then(
            function (result) { return result && result.data ? result.data.session : null; },
            function () { return null; }
        );
    }

    function announce(message) {
        if (shell && shell.live) shell.live.textContent = message;
    }

    function formatDate(iso) {
        if (!iso) return "";
        var d = new Date(iso);
        if (isNaN(d.getTime())) return "";
        try { return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }); }
        catch (e) { return d.toISOString().slice(0, 10); }
    }

    // 2 x 53-bit cyrb53 hashes -> a stable id for one logical save.
    function hash53(str, seed) {
        var h1 = 0xdeadbeef ^ seed, h2 = 0x41c6ce57 ^ seed;
        for (var i = 0, ch; i < str.length; i++) {
            ch = str.charCodeAt(i);
            h1 = Math.imul(h1 ^ ch, 2654435761);
            h2 = Math.imul(h2 ^ ch, 1597334677);
        }
        h1 = Math.imul(h1 ^ (h1 >>> 16), 2246822507) ^ Math.imul(h2 ^ (h2 >>> 13), 3266489909);
        h2 = Math.imul(h2 ^ (h2 >>> 16), 2246822507) ^ Math.imul(h1 ^ (h1 >>> 13), 3266489909);
        return 4294967296 * (2097151 & h2) + (h1 >>> 0);
    }

    // The SAME answer always yields the SAME id (so a retry or double click
    // is idempotent on the server), while a regenerated answer - different
    // text at the same position - yields a different one.
    function makeClientId(projId, chatId, msgIndex, text) {
        var s = projId + "|" + chatId + "|" + msgIndex + "|" + text;
        return "n_" + hash53(s, 1).toString(36) + hash53(s, 2).toString(36);
    }

    function noteTitle(note) {
        var lines = String(note.content_md || "").split("\n");
        for (var i = 0; i < lines.length; i++) {
            var line = lines[i].replace(/^[#>\-*\s]+/, "").replace(/[*_`$]/g, "").trim();
            if (line) return line.length > 80 ? line.slice(0, 80) + "\u2026" : line;
        }
        var q = String(note.question_md || "").trim();
        return q ? (q.length > 80 ? q.slice(0, 80) + "\u2026" : q) : "Untitled note";
    }

    function snippet(note) {
        var text = String(note.content_md || "").replace(/[#>*_`$\\]/g, "").replace(/\s+/g, " ").trim();
        return text.length > 140 ? text.slice(0, 140) + "\u2026" : text;
    }

    function makeError(kind, status, detail) {
        var err = new Error("notes: " + kind);
        err.kind = kind;          // "auth" | "network" | "http"
        err.status = status || 0;
        err.detail = detail || null;
        return err;
    }

    // ------------------------------------------------------------
    // API
    // ------------------------------------------------------------

    function request(method, path, body) {
        if (isOffline()) return Promise.reject(makeError("network"));
        return getSession().then(function (session) {
            if (!session || !session.access_token) throw makeError("auth");
            var options = { method: method, headers: { "Authorization": "Bearer " + session.access_token } };
            if (body !== undefined) {
                options.headers["Content-Type"] = "application/json";
                options.body = JSON.stringify(body);
            }
            return fetch(path, options).then(function (resp) {
                return resp.json().catch(function () { return null; }).then(function (data) {
                    if (!resp.ok) throw makeError("http", resp.status, data && typeof data.detail === "string" ? data.detail : null);
                    return { status: resp.status, data: data };
                });
            }, function () { throw makeError("network"); });
        });
    }

    function friendlyError(err, fallback) {
        if (err && err.kind === "auth") return "Please sign in to use Pocket Notes.";
        if (err && err.kind === "network") return "You're offline or Kognit can't be reached.";
        if (err && err.detail) return err.detail;
        return fallback || "Something went wrong. Please try again.";
    }

    // ------------------------------------------------------------
    // IndexedDB cache (never fatal)
    // ------------------------------------------------------------

    function store() { return window.KognitNotesStore || null; }

    function cacheSafely(operation) {
        var s = store();
        if (!s || !s.isSupported()) { state.storageOk = false; return Promise.resolve(false); }
        return operation(s).then(
            function () { return true; },
            function () { state.storageOk = false; return false; }
        );
    }

    function noteCached(note) {
        return cacheSafely(function (s) { return s.put(state.userId, note); }).then(function (ok) {
            if (ok) state.cachedIds[note.id] = true;
        });
    }

    // ------------------------------------------------------------
    // Loading
    // ------------------------------------------------------------

    function fetchAllPages() {
        var all = [];
        function next(offset, page) {
            return request("GET", "/api/notes?limit=" + PAGE_SIZE + "&offset=" + offset).then(function (res) {
                var batch = (res.data && res.data.notes) || [];
                all = all.concat(batch);
                if (res.data && res.data.has_more && page < MAX_LIST_PAGES) return next(offset + PAGE_SIZE, page + 1);
                return { notes: all, complete: !(res.data && res.data.has_more) };
            });
        }
        return next(0, 1);
    }

    function applyServerList(userId, notes, complete) {
        state.notes = notes;
        state.fromCache = false;
        state.error = null;
        state.syncedAt = new Date().toISOString();
        state.savedClientIds = {};
        notes.forEach(function (n) { state.savedClientIds[n.client_id] = n.id; });
        state.cachedIds = {};
        if (!complete) return Promise.resolve();   // never replace the cache with a partial listing
        return cacheSafely(function (s) { return s.replaceAll(userId, notes, state.syncedAt); }).then(function (ok) {
            if (ok) notes.forEach(function (n) { state.cachedIds[n.id] = true; });
        });
    }

    function loadFromCache() {
        var s = store();
        if (!s || !s.isSupported()) return Promise.resolve(false);
        var idPromise = state.userId ? Promise.resolve(state.userId) : s.getLastUserId().catch(function () { return null; });
        return idPromise.then(function (userId) {
            if (!userId) return false;
            return s.getAll(userId).then(function (cached) {
                if (!cached.syncedAt && cached.notes.length === 0) return false;
                state.userId = userId;
                state.notes = cached.notes.map(function (c) { return c.note; });
                state.cachedIds = {};
                state.notes.forEach(function (n) { state.cachedIds[n.id] = true; });
                state.fromCache = true;
                state.syncedAt = cached.syncedAt;
                state.error = null;
                return true;
            });
        }).catch(function () { state.storageOk = false; return false; });
    }

    function loadNotes() {
        if (state.loading) return Promise.resolve();
        state.loading = true;
        render();
        return getSession().then(function (session) {
            if (session && session.user) {
                if (state.userId && state.userId !== session.user.id) resetForNewUser();
                state.userId = session.user.id;
            }
            if (!session && !isOffline()) throw makeError("auth");
            return fetchAllPages();
        }).then(function (result) {
            return applyServerList(state.userId, result.notes, result.complete);
        }).catch(function (err) {
            if ((err && err.kind === "network") || isOffline()) {
                return loadFromCache().then(function (ok) {
                    if (!ok) state.error = { message: "You're offline and no notes have been saved on this device yet." };
                });
            }
            state.error = { message: friendlyError(err, "Could not load your notes.") };
            if (err && err.kind === "auth") { state.notes = []; state.fromCache = false; }
        }).then(function () {
            state.loaded = true;
            state.loading = false;
            return refreshSwStatus();
        }).then(render);
    }

    function resetForNewUser() {
        state.notes = []; state.loaded = false; state.cachedIds = {}; state.savedClientIds = {};
        state.selectedId = null; state.study = null; state.fromCache = false; state.syncedAt = null;
    }

    function onSignedOut() {
        var userId = state.userId;
        var s = store();
        resetForNewUser();
        state.userId = null;
        state.error = null;
        if (s && s.isSupported()) {
            // Wipe the signed-out student's cached notes (shared-device safety).
            var idPromise = userId ? Promise.resolve(userId) : s.getLastUserId().catch(function () { return null; });
            idPromise.then(function (id) { return id ? s.clearUser(id) : null; }).catch(function () { /* nothing to wipe */ });
        }
        render();
    }

    // ------------------------------------------------------------
    // Service worker
    // ------------------------------------------------------------

    function refreshSwStatus() {
        if (!state.swSupported || !navigator.serviceWorker.controller) { state.sw = null; return Promise.resolve(); }
        return new Promise(function (resolve) {
            var channel = null;
            var done = function (value) {
                clearTimeout(timer);
                state.sw = value;
                if (channel) { try { channel.port1.close(); } catch (e) { /* already closed */ } }
                resolve();
            };
            var timer = setTimeout(function () { done(null); }, SW_STATUS_TIMEOUT_MS);
            try {
                channel = new MessageChannel();
                channel.port1.onmessage = function (event) { done(event.data || null); };
                navigator.serviceWorker.controller.postMessage({ type: "GET_STATUS" }, [channel.port2]);
            } catch (e) { done(null); }
        });
    }

    function registerServiceWorker() {
        state.swSupported = typeof navigator !== "undefined" && "serviceWorker" in navigator;
        if (!state.swSupported) return Promise.resolve(null);
        var secure = location.protocol === "https:" || location.hostname === "localhost" || location.hostname === "127.0.0.1";
        if (!secure) { state.swSupported = false; return Promise.resolve(null); }
        navigator.serviceWorker.addEventListener("controllerchange", function () { refreshSwStatus().then(render); });
        return navigator.serviceWorker.register("/sw.js", { scope: "/" }).then(function (registration) {
            return navigator.serviceWorker.ready.then(function () { return refreshSwStatus(); }).then(render).then(function () { return registration; });
        }).catch(function () { state.sw = null; return null; });
    }

    // Only claims "available offline" when BOTH the app shell and this
    // note's data are genuinely cached.
    function shellReady() { return !!(state.sw && state.sw.ready); }
    function noteAvailableOffline(note) { return shellReady() && !!state.cachedIds[note.id] && state.storageOk; }

    function offlineStatusText() {
        if (state.fromCache) {
            var when = state.syncedAt ? " Last synced " + formatDate(state.syncedAt) + "." : "";
            return { tone: "warn", text: "You're offline, so these are your saved offline copies." + when + " Saving, editing and AI study tools are paused." };
        }
        if (!state.storageOk) return { tone: "warn", text: "This browser couldn't store your notes on this device, so they won't be readable offline." };
        if (!state.swSupported) return { tone: "info", text: "This browser can't keep Kognit open offline, so notes need a connection." };
        if (shellReady()) {
            var count = Object.keys(state.cachedIds).length;
            if (count > 0) return { tone: "ok", text: count + (count === 1 ? " note is" : " notes are") + " available to read offline." };
            return null;
        }
        return { tone: "info", text: "Offline reading is still being set up. Keep this page open while it finishes." };
    }

    // ------------------------------------------------------------
    // Save button (answer toolbar)
    // ------------------------------------------------------------

    var BOOKMARK_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z"></path></svg>';
    var CHECK_ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>';

    var SAVE_STATES = {
        idle:    { label: "Save",          title: "Save to Pocket Notes",                     icon: BOOKMARK_ICON, disabled: false },
        saving:  { label: "Saving\u2026",  title: "Saving to Pocket Notes\u2026",             icon: BOOKMARK_ICON, disabled: true },
        saved:   { label: "Saved",         title: "Saved to Pocket Notes",                    icon: CHECK_ICON,    disabled: true },
        already: { label: "Already saved", title: "This answer is already in Pocket Notes",   icon: CHECK_ICON,    disabled: true },
        auth:    { label: "Sign in",       title: "Sign in to save notes",                    icon: BOOKMARK_ICON, disabled: false },
        offline: { label: "Offline",       title: "You're offline. Reconnect to save, then try again.", icon: BOOKMARK_ICON, disabled: false },
        error:   { label: "Retry",         title: "Couldn't save. Click to retry.",           icon: BOOKMARK_ICON, disabled: false }
    };

    function setSaveState(btn, name, detail) {
        var def = SAVE_STATES[name];
        btn.dataset.state = name;
        btn.disabled = def.disabled;
        btn.setAttribute("aria-busy", name === "saving" ? "true" : "false");
        var title = detail ? def.title + " " + detail : def.title;
        btn.title = title;
        btn.setAttribute("aria-label", title);
        btn.innerHTML = def.icon;                      // static, trusted constants only
        var label = el("span", null, def.label);
        label.setAttribute("aria-live", "polite");
        btn.appendChild(label);
    }

    function locateMessage(meta) {
        if (!meta || !meta.projId || !meta.chatId || !Number.isInteger(meta.msgIndex)) return null;
        if (typeof window.getProjectsSnapshot !== "function") return null;
        var projects = window.getProjectsSnapshot() || [];
        var proj = projects.filter(function (p) { return p.id === meta.projId; })[0];
        var chat = proj ? (proj.chats || []).filter(function (c) { return c.id === meta.chatId; })[0] : null;
        var msg = chat ? chat.messages[meta.msgIndex] : null;
        if (!msg || msg.role !== "bot" || typeof msg.text !== "string" || !msg.text.trim()) return null;
        var question = "";
        for (var i = meta.msgIndex - 1; i >= 0; i--) {
            if (chat.messages[i] && chat.messages[i].role === "user") { question = chat.messages[i].text || ""; break; }
        }
        return { msg: msg, question: question };
    }

    function buildPayload(meta, found) {
        var msg = found.msg;
        var research = msg.research;
        var sources = [];
        if (research && research.grounding_status === "used" && Array.isArray(research.sources)) {
            sources = research.sources.slice(0, 10).map(function (s) {
                return { title: s.title || undefined, url: s.url || undefined, domain: s.domain || undefined };
            });
        }
        return {
            client_id: makeClientId(meta.projId, meta.chatId, meta.msgIndex, msg.text),
            content_md: msg.text,
            takeaway: msg.takeaway || undefined,
            question_md: found.question || undefined,
            sources: sources,
            source_ref: { projectId: meta.projId, chatId: meta.chatId, msgIndex: meta.msgIndex }
        };
    }

    // Returns a toolbar button, or null when this answer must not be savable
    // (an interrupted/partial stream, or no stable message reference).
    function buildSaveButton(meta) {
        if (!meta || meta.interrupted) return null;
        var found = locateMessage(meta);
        if (!found) return null;

        var btn = el("button", "toolbar-btn toolbar-btn-save");
        btn.type = "button";
        var payload = buildPayload(meta, found);          // computed ONCE: a retry reuses the same client_id
        setSaveState(btn, state.savedClientIds[payload.client_id] ? "saved" : "idle");

        btn.addEventListener("click", function () { saveAnswer(btn, payload); });
        return btn;
    }

    function saveAnswer(btn, payload) {
        if (btn.dataset.state === "saving" || btn.dataset.state === "saved" || btn.dataset.state === "already") return;
        if (isOffline()) { setSaveState(btn, "offline"); return; }

        setSaveState(btn, "saving");
        request("POST", "/api/notes", payload).then(function (res) {
            var note = res.data && res.data.note;
            if (note) {
                state.savedClientIds[payload.client_id] = note.id;
                if (state.loaded && !state.notes.some(function (n) { return n.id === note.id; })) state.notes.unshift(note);
                if (state.userId) noteCached(note);
                if (state.loaded) renderIfVisible();
            }
            setSaveState(btn, res.data && res.data.created === false ? "already" : "saved");
        }).catch(function (err) {
            if (err.kind === "auth") {
                setSaveState(btn, "auth");
                if (typeof window.openAuthModal === "function") window.openAuthModal();
            } else if (err.kind === "network") {
                setSaveState(btn, "offline");
            } else {
                setSaveState(btn, "error", err.detail || "");
            }
        });
    }

    // ------------------------------------------------------------
    // View shell (created once; sections re-rendered in place)
    // ------------------------------------------------------------

    function ensureShell() {
        if (shell) return shell;
        var root = document.getElementById("notes-view");
        if (!root) return null;
        root.textContent = "";

        var inner = el("div", "nav-view-inner notes-inner");

        var header = el("div", "nav-view-header");
        header.appendChild(el("h2", "nav-view-title", "Pocket Notes"));
        header.appendChild(el("p", "nav-view-subtitle", "Answers you saved from Kognit \u2014 find them again, revise from them, and read them offline."));
        inner.appendChild(header);

        var status = el("div", "notes-status");
        status.setAttribute("role", "status");
        inner.appendChild(status);

        var live = el("div", "notes-sr-only");
        live.setAttribute("aria-live", "polite");
        inner.appendChild(live);

        var listPane = el("div", "notes-list-pane");
        var controls = el("div", "notes-controls");
        var search = el("input", "notes-search");
        search.type = "search";
        search.placeholder = "Search your notes";
        search.setAttribute("aria-label", "Search your notes");
        search.addEventListener("input", function () { state.query = search.value; renderList(); });
        controls.appendChild(search);
        var chips = el("div", "notes-chips");
        chips.setAttribute("role", "group");
        chips.setAttribute("aria-label", "Filter notes by tag");
        controls.appendChild(chips);
        listPane.appendChild(controls);
        var results = el("div", "notes-results");
        listPane.appendChild(results);
        inner.appendChild(listPane);

        var detailPane = el("div", "notes-detail-pane hidden");
        inner.appendChild(detailPane);

        root.appendChild(inner);
        shell = { root: root, status: status, live: live, listPane: listPane, search: search, chips: chips, results: results, detailPane: detailPane };
        return shell;
    }

    function renderIfVisible() {
        var root = document.getElementById("notes-view");
        if (root && !root.classList.contains("hidden")) render();
    }

    function render() {
        if (!ensureShell()) return;
        renderStatus();
        if (state.selectedId && findNote(state.selectedId)) {
            shell.listPane.classList.add("hidden");
            shell.detailPane.classList.remove("hidden");
            renderDetail();
        } else {
            state.selectedId = null;
            shell.detailPane.classList.add("hidden");
            shell.listPane.classList.remove("hidden");
            renderList();
        }
    }

    function renderStatus() {
        shell.status.textContent = "";
        shell.status.className = "notes-status";
        var info = offlineStatusText();
        if (!info) return;
        shell.status.classList.add("notes-status-" + info.tone);
        shell.status.textContent = info.text;
    }

    function findNote(id) {
        for (var i = 0; i < state.notes.length; i++) if (state.notes[i].id === id) return state.notes[i];
        return null;
    }

    // ------------------------------------------------------------
    // List
    // ------------------------------------------------------------

    function tagCounts() {
        var counts = {};
        state.notes.forEach(function (n) {
            (n.tags || []).forEach(function (t) { counts[t.label] = (counts[t.label] || 0) + 1; });
            if (n.needs_review) counts["Needs review"] = (counts["Needs review"] || 0) + 1;
        });
        return Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a] || a.localeCompare(b); })
            .map(function (label) { return { label: label, count: counts[label] }; });
    }

    function noteMatchesTag(note, label) {
        if (label === "Needs review") return !!note.needs_review;
        return (note.tags || []).some(function (t) { return t.label === label; });
    }

    function filteredNotes() {
        var q = state.query.trim().toLowerCase();
        return state.notes.filter(function (n) {
            if (state.activeTag && !noteMatchesTag(n, state.activeTag)) return false;
            if (!q) return true;
            var hay = (noteTitle(n) + " " + (n.content_md || "") + " " + (n.tags || []).map(function (t) { return t.label; }).join(" ")).toLowerCase();
            return hay.indexOf(q) !== -1;
        });
    }

    function renderChips() {
        shell.chips.textContent = "";
        var counts = tagCounts();
        if (state.activeTag && !counts.some(function (c) { return c.label === state.activeTag; })) state.activeTag = null;
        counts.slice(0, 20).forEach(function (c) {
            var chip = el("button", "notes-chip", c.label + " \u00b7 " + c.count);
            chip.type = "button";
            chip.setAttribute("aria-pressed", state.activeTag === c.label ? "true" : "false");
            if (state.activeTag === c.label) chip.classList.add("active");
            chip.addEventListener("click", function () {
                state.activeTag = state.activeTag === c.label ? null : c.label;
                renderList();
            });
            shell.chips.appendChild(chip);
        });
    }

    function emptyState(title, body) {
        var box = el("div", "notes-empty");
        box.appendChild(el("div", "notes-empty-title", title));
        box.appendChild(el("p", "notes-empty-body", body));
        return box;
    }

    function retryBox(message, onRetry) {
        var box = el("div", "notes-empty notes-error");
        box.setAttribute("role", "alert");
        box.appendChild(el("div", "notes-empty-title", message));
        var btn = el("button", "notes-btn", "Try again");
        btn.type = "button";
        btn.addEventListener("click", onRetry);
        box.appendChild(btn);
        return box;
    }

    function renderList() {
        shell.search.value = state.query;
        renderChips();
        var out = shell.results;
        out.textContent = "";

        if (state.loading && !state.loaded) { out.appendChild(el("div", "nav-view-loading", "Loading your notes\u2026")); return; }
        if (state.error) { out.appendChild(retryBox(state.error.message, function () { loadNotes(); })); return; }
        if (state.notes.length === 0) {
            out.appendChild(emptyState("No notes yet",
                "Open a Kognit answer in Chat and choose Save under it. Saved answers show up here."));
            return;
        }
        var shown = filteredNotes();
        if (shown.length === 0) { out.appendChild(emptyState("No matching notes", "Try a different search or clear the tag filter.")); return; }

        var list = el("ul", "notes-list");
        shown.forEach(function (note) {
            var li = el("li", "notes-list-item");
            var card = el("button", "note-card");
            card.type = "button";
            card.appendChild(el("span", "note-card-title", noteTitle(note)));
            card.appendChild(el("span", "note-card-snippet", snippet(note)));
            var meta = el("span", "note-card-meta");
            (note.tags || []).slice(0, 4).forEach(function (t) { meta.appendChild(el("span", "note-tag note-tag-" + t.kind, t.label)); });
            if (note.needs_review) meta.appendChild(el("span", "note-tag note-tag-review", "Needs review"));
            meta.appendChild(el("span", "note-card-date", formatDate(note.created_at)));
            if (state.fromCache) meta.appendChild(el("span", "note-badge note-badge-copy", "Offline copy"));
            else if (noteAvailableOffline(note)) meta.appendChild(el("span", "note-badge", "Available offline"));
            card.appendChild(meta);
            card.addEventListener("click", function () { openNote(note.id); });
            li.appendChild(card);
            list.appendChild(li);
        });
        out.appendChild(list);
    }

    // ------------------------------------------------------------
    // Detail
    // ------------------------------------------------------------

    function openNote(id) {
        state.selectedId = id;
        state.study = null;
        render();
        var back = shell.detailPane.querySelector(".notes-back");
        if (back) back.focus();
    }

    function closeNote() {
        state.selectedId = null;
        state.study = null;
        render();
        shell.search.focus();     // keep keyboard users in the notes list
    }

    function renderBody(container, raw) {
        var canRender = typeof marked !== "undefined" && typeof protectMathSegments === "function"
            && typeof restoreProtectedMathSegments === "function" && typeof sanitizeRenderedHtml === "function";
        if (!canRender) {
            container.classList.add("notes-body-plain");
            container.textContent = raw;
            return false;
        }
        var protectedResult = protectMathSegments(raw);
        container.innerHTML = sanitizeRenderedHtml(
            restoreProtectedMathSegments(marked.parse(protectedResult.text), protectedResult.segments)
        );
        return true;
    }

    function typeset(node) {
        if (typeof typesetMathJax === "function") { try { typesetMathJax([node]); } catch (e) { /* math stays as text */ } }
    }

    function actionButton(label, onClick, options) {
        var btn = el("button", "notes-btn" + (options && options.danger ? " notes-btn-danger" : ""), label);
        btn.type = "button";
        var blocked = (options && options.needsNetwork) && (state.fromCache || isOffline());
        if (blocked) {
            btn.disabled = true;
            btn.title = "Not available offline";
        } else if (state.busy) {
            btn.disabled = true;
        }
        btn.addEventListener("click", onClick);
        return btn;
    }

    function renderDetail() {
        var note = findNote(state.selectedId);
        var pane = shell.detailPane;
        pane.textContent = "";

        var top = el("div", "notes-detail-top");
        var back = el("button", "notes-btn notes-back", "\u2190 All notes");
        back.type = "button";
        back.addEventListener("click", closeNote);
        top.appendChild(back);
        if (state.fromCache) top.appendChild(el("span", "note-badge note-badge-copy", "Offline copy"));
        else if (noteAvailableOffline(note)) top.appendChild(el("span", "note-badge", "Available offline"));
        pane.appendChild(top);

        pane.appendChild(el("h3", "notes-detail-title", noteTitle(note)));
        pane.appendChild(el("div", "notes-detail-date", "Saved " + formatDate(note.created_at)));

        // ---- tags ----
        pane.appendChild(renderTagSection(note));

        // ---- content ----
        if (note.question_md) {
            var q = el("div", "notes-question");
            q.appendChild(el("span", "notes-question-label", "Your question"));
            q.appendChild(el("span", null, note.question_md));
            pane.appendChild(q);
        }
        var body = el("div", "notes-body bot-message-content");
        var rendered = renderBody(body, note.content_md);
        pane.appendChild(body);
        if (!rendered) pane.appendChild(el("p", "notes-hint", "Formatting couldn't load, so this note is shown as plain text."));

        if (note.takeaway) {
            var take = el("div", "answer-takeaway");
            take.appendChild(el("div", "answer-takeaway-label", "Key takeaway"));
            take.appendChild(el("div", "answer-takeaway-body", note.takeaway));
            pane.appendChild(take);
        }
        pane.appendChild(renderSources(note));
        typeset(pane);

        // ---- study tools ----
        pane.appendChild(renderStudySection(note));

        // ---- actions ----
        var actions = el("div", "notes-actions");
        var srcRef = note.source_ref;
        if (srcRef && typeof window.openProjectAndChat === "function" && sourceChatExists(srcRef)) {
            actions.appendChild(actionButton("Open original chat", function () {
                if (window.KognitNav) window.KognitNav.switchTo("chat");
                window.openProjectAndChat(srcRef.projectId, srcRef.chatId);
            }));
        }
        actions.appendChild(actionButton("Delete note", function () { deleteNote(note); }, { danger: true, needsNetwork: true }));
        pane.appendChild(actions);
    }

    function sourceChatExists(ref) {
        var projects = (typeof window.getProjectsSnapshot === "function" && window.getProjectsSnapshot()) || [];
        var proj = projects.filter(function (p) { return p.id === ref.projectId; })[0];
        return !!(proj && (proj.chats || []).some(function (c) { return c.id === ref.chatId; }));
    }

    function renderSources(note) {
        var wrap = el("div", "notes-sources");
        var sources = Array.isArray(note.sources) ? note.sources : [];
        if (sources.length === 0) return wrap;
        wrap.appendChild(el("div", "notes-section-label", "Sources"));
        var list = el("ul", "notes-sources-list");
        sources.forEach(function (s) {
            var li = el("li");
            var label = s.title || s.domain || s.url || "Source";
            var safe = typeof isClientSafeCitationUrl === "function" ? isClientSafeCitationUrl(s.url) : false;
            if (safe) {
                var a = el("a", null, label);
                a.href = s.url; a.target = "_blank"; a.rel = "noopener noreferrer";
                li.appendChild(a);
            } else {
                li.appendChild(el("span", null, label));
            }
            if (s.domain) li.appendChild(el("span", "notes-source-domain", " (" + s.domain + ")"));
            list.appendChild(li);
        });
        wrap.appendChild(list);
        return wrap;
    }

    // ------------------------------------------------------------
    // Tags
    // ------------------------------------------------------------

    function provenanceLabel(tag) {
        if (tag.origin === "known") return "from your profile";
        if (tag.origin === "inferred") return "detected automatically";
        if (tag.origin === "ai") return "AI suggestion you accepted";
        return "added by you";
    }

    function renderTagSection(note) {
        var section = el("div", "notes-tags");
        var row = el("div", "notes-tag-row");
        (note.tags || []).forEach(function (tag) {
            var chip = el("span", "note-tag note-tag-" + tag.kind);
            chip.title = tag.label + " \u2014 " + provenanceLabel(tag);
            chip.appendChild(el("span", null, tag.label));
            var editable = !(state.fromCache || isOffline());
            if (editable) {
                var rm = el("button", "note-tag-remove", "\u00d7");
                rm.type = "button";
                rm.disabled = state.busy;
                rm.setAttribute("aria-label", "Remove tag " + tag.label);
                rm.addEventListener("click", function () {
                    saveTags(note, (note.tags || []).filter(function (t) { return t !== tag; }));
                });
                chip.appendChild(rm);
            }
            row.appendChild(chip);
        });
        if (note.needs_review) row.appendChild(el("span", "note-tag note-tag-review", "Needs review"));
        if ((note.tags || []).length === 0 && !note.needs_review) row.appendChild(el("span", "notes-hint", "No tags yet."));
        section.appendChild(row);

        if (state.fromCache || isOffline()) return section;

        if (note.needs_review) {
            section.appendChild(el("p", "notes-hint", "Kognit couldn't tell the subject for sure. Pick it to fix this note's tags:"));
            var picks = el("div", "notes-subject-picks");
            SUBJECTS.forEach(function (subject) {
                var b = el("button", "notes-chip", subject);
                b.type = "button";
                b.disabled = state.busy;
                b.addEventListener("click", function () {
                    saveTags(note, (note.tags || []).concat([{ label: subject, kind: "subject", origin: "student" }]));
                });
                picks.appendChild(b);
            });
            section.appendChild(picks);
        }

        var form = el("div", "notes-tag-form");
        var input = el("input", "notes-tag-input");
        input.type = "text";
        input.maxLength = 40;
        input.placeholder = "Add a tag";
        input.setAttribute("aria-label", "Add a tag");
        var add = el("button", "notes-btn", "Add");
        add.type = "button";
        add.disabled = state.busy;
        var submit = function () {
            var label = input.value.trim().replace(/^#/, "");
            if (!label) return;
            saveTags(note, (note.tags || []).concat([{ label: label, kind: "topic", origin: "student" }]));
        };
        add.addEventListener("click", submit);
        input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); submit(); } });
        form.appendChild(input);
        form.appendChild(add);

        var suggest = actionButton("Suggest topics (AI)", function () { suggestTopics(note); }, { needsNetwork: true });
        suggest.classList.add("notes-btn-ghost");
        form.appendChild(suggest);
        section.appendChild(form);

        var s = state.study;
        if (s && s.noteId === note.id && s.suggestions) {
            var box = el("div", "notes-suggestions");
            box.appendChild(el("p", "notes-hint", s.suggestions.length
                ? "AI-suggested topics from this note (not official curriculum chapters). Click to add:"
                : "The AI didn't find a clear topic in this note."));
            s.suggestions.forEach(function (t) {
                var b = el("button", "notes-chip", "+ " + t.label);
                b.type = "button";
                b.addEventListener("click", function () {
                    s.suggestions = s.suggestions.filter(function (x) { return x !== t; });
                    saveTags(note, (note.tags || []).concat([{ label: t.label, kind: "topic", origin: "ai" }]));
                });
                box.appendChild(b);
            });
            section.appendChild(box);
        }
        if (s && s.noteId === note.id && s.suggestError) {
            var err = el("p", "notes-hint notes-hint-error", s.suggestError);
            err.setAttribute("role", "alert");
            section.appendChild(err);
        }
        return section;
    }

    function replaceNote(updated) {
        for (var i = 0; i < state.notes.length; i++) if (state.notes[i].id === updated.id) state.notes[i] = updated;
        return noteCached(updated);
    }

    function saveTags(note, tags) {
        if (state.busy) return;
        state.busy = true;
        render();
        request("PATCH", "/api/notes/" + note.id, { tags: tags.map(function (t) { return { label: t.label, kind: t.kind, origin: t.origin }; }) })
            .then(function (res) { return replaceNote(res.data.note).then(function () { announce("Tags updated."); }); })
            .catch(function (err) { announce(friendlyError(err, "Couldn't update tags.")); setStudyError(note, "suggestError", friendlyError(err, "Couldn't update tags.")); })
            .then(function () { state.busy = false; render(); });
    }

    function setStudyError(note, key, message) {
        if (!state.study || state.study.noteId !== note.id) state.study = { noteId: note.id };
        state.study[key] = message;
    }

    function suggestTopics(note) {
        if (state.busy) return;
        state.busy = true;
        if (!state.study || state.study.noteId !== note.id) state.study = { noteId: note.id };
        state.study.suggestError = null;
        render();
        request("POST", "/api/notes/" + note.id + "/suggest-tags")
            .then(function (res) { state.study.suggestions = (res.data && res.data.suggestions) || []; })
            .catch(function (err) { state.study.suggestError = friendlyError(err, "Couldn't get suggestions."); })
            .then(function () { state.busy = false; render(); });
    }

    function deleteNote(note) {
        if (state.busy) return;
        if (!window.confirm("Delete this note? This can't be undone.")) return;
        state.busy = true;
        render();
        request("DELETE", "/api/notes/" + note.id).then(function () {
            state.notes = state.notes.filter(function (n) { return n.id !== note.id; });
            Object.keys(state.savedClientIds).forEach(function (k) { if (state.savedClientIds[k] === note.id) delete state.savedClientIds[k]; });
            delete state.cachedIds[note.id];
            state.selectedId = null;
            state.study = null;
            announce("Note deleted.");
            return cacheSafely(function (s) { return s.remove(state.userId, note.id); });
        }).catch(function (err) {
            // 404 => already gone on the server; drop the local copy too.
            if (err.kind === "http" && err.status === 404) {
                state.notes = state.notes.filter(function (n) { return n.id !== note.id; });
                state.selectedId = null;
                cacheSafely(function (s) { return s.remove(state.userId, note.id); });
            } else {
                announce(friendlyError(err, "Couldn't delete the note."));
                setStudyError(note, "deleteError", friendlyError(err, "Couldn't delete the note."));
            }
        }).then(function () { state.busy = false; render(); });
    }

    // ------------------------------------------------------------
    // Study: flashcards and mini-quiz
    // ------------------------------------------------------------

    function ensureStudy(note) {
        if (!state.study || state.study.noteId !== note.id) state.study = { noteId: note.id };
        return state.study;
    }

    function generate(note, kind, regenerate) {
        if (state.busy) return;
        var study = ensureStudy(note);
        var column = kind === "flashcards" ? "flashcards" : "mini_quiz";
        var path = "/api/notes/" + note.id + "/" + (kind === "flashcards" ? "flashcards" : "mini-quiz");
        state.busy = true;
        study.error = null;
        study.loadingKind = kind;
        render();
        request("POST", path, regenerate ? { regenerate: true } : {}).then(function (res) {
            var data = res.data || {};
            if (data.note) replaceNote(data.note);
            if (kind === "flashcards") { study.cards = data[column]; study.index = 0; study.revealed = false; study.mode = "flashcards"; }
            else { study.questions = data[column]; study.answers = new Array(study.questions.length).fill(null); study.submitted = false; study.mode = "quiz"; }
        }).catch(function (err) {
            study.error = friendlyError(err, "Couldn't create study material.");
        }).then(function () { study.loadingKind = null; state.busy = false; render(); });
    }

    function openStored(note, kind) {
        // Stored material is opened without any request.
        var study = ensureStudy(note);
        study.error = null;
        if (kind === "flashcards") { study.cards = note.flashcards; study.index = 0; study.revealed = false; study.mode = "flashcards"; }
        else { study.questions = note.mini_quiz; study.answers = new Array(note.mini_quiz.length).fill(null); study.submitted = false; study.mode = "quiz"; }
        render();
    }

    function renderStudySection(note) {
        var section = el("div", "notes-study");
        section.appendChild(el("div", "notes-section-label", "Practise from this note"));
        var study = state.study && state.study.noteId === note.id ? state.study : null;

        var row = el("div", "notes-study-actions");
        var hasCards = Array.isArray(note.flashcards) && note.flashcards.length > 0;
        var hasQuiz = Array.isArray(note.mini_quiz) && note.mini_quiz.length > 0;

        row.appendChild(actionButton(hasCards ? "Open flashcards" : "Create flashcards",
            function () { hasCards ? openStored(note, "flashcards") : generate(note, "flashcards", false); },
            { needsNetwork: !hasCards }));
        row.appendChild(actionButton(hasQuiz ? "Open mini-quiz" : "Create mini-quiz",
            function () { hasQuiz ? openStored(note, "quiz") : generate(note, "quiz", false); },
            { needsNetwork: !hasQuiz }));
        section.appendChild(row);

        section.appendChild(el("p", "notes-hint", "Study material is AI-generated from this note. It isn't independently verified, and quiz results are not added to your progress."));

        if (study && study.loadingKind) section.appendChild(el("div", "nav-view-loading", "Creating your " + (study.loadingKind === "flashcards" ? "flashcards" : "mini-quiz") + "\u2026"));
        if (study && study.error) {
            var box = el("div", "notes-hint notes-hint-error", study.error);
            box.setAttribute("role", "alert");
            section.appendChild(box);
        }
        if (study && study.deleteError) section.appendChild(el("div", "notes-hint notes-hint-error", study.deleteError));

        if (study && study.mode === "flashcards" && study.cards) section.appendChild(renderFlashcards(note, study));
        if (study && study.mode === "quiz" && study.questions) section.appendChild(renderQuiz(note, study));
        return section;
    }

    function renderFlashcards(note, study) {
        var cards = study.cards;
        var card = cards[study.index];
        var wrap = el("div", "notes-flash");
        wrap.setAttribute("aria-label", "Flashcards");

        wrap.appendChild(el("div", "notes-flash-count", "Card " + (study.index + 1) + " of " + cards.length));
        var face = el("div", "notes-flash-card");
        face.setAttribute("aria-live", "polite");
        face.appendChild(el("div", "notes-flash-side", study.revealed ? "Answer" : "Question"));
        face.appendChild(el("div", "notes-flash-text", study.revealed ? card.back : card.front));
        wrap.appendChild(face);

        var controls = el("div", "notes-flash-controls");
        var prev = el("button", "notes-btn", "Previous");
        prev.type = "button";
        prev.disabled = study.index === 0;
        prev.addEventListener("click", function () { study.index--; study.revealed = false; render(); });
        var reveal = el("button", "notes-btn notes-btn-primary", study.revealed ? "Hide answer" : "Show answer");
        reveal.type = "button";
        reveal.addEventListener("click", function () { study.revealed = !study.revealed; render(); });
        var next = el("button", "notes-btn", "Next");
        next.type = "button";
        next.disabled = study.index === cards.length - 1;
        next.addEventListener("click", function () { study.index++; study.revealed = false; render(); });
        controls.appendChild(prev); controls.appendChild(reveal); controls.appendChild(next);
        wrap.appendChild(controls);

        wrap.appendChild(actionButton("Regenerate flashcards", function () { generate(note, "flashcards", true); }, { needsNetwork: true }));
        typeset(wrap);
        return wrap;
    }

    function renderQuiz(note, study) {
        var wrap = el("div", "notes-quiz");
        wrap.appendChild(el("div", "notes-flash-count", study.practice ? "Practice: questions you missed" : "Mini-quiz"));

        study.questions.forEach(function (q, qi) {
            var item = el("fieldset", "notes-quiz-q");
            item.appendChild(el("legend", "notes-quiz-legend", (qi + 1) + ". " + q.question));
            var group = el("div", "notes-quiz-options");
            group.setAttribute("role", "radiogroup");
            group.setAttribute("aria-label", "Question " + (qi + 1));
            q.options.forEach(function (opt, oi) {
                var chosen = study.answers[qi] === oi;
                var btn = el("button", "notes-quiz-opt", String.fromCharCode(65 + oi) + ". " + opt);
                btn.type = "button";
                btn.setAttribute("role", "radio");
                btn.setAttribute("aria-checked", chosen ? "true" : "false");
                if (chosen) btn.classList.add("selected");
                if (study.submitted) {
                    btn.disabled = true;
                    if (oi === q.correct_index) btn.classList.add("correct");
                    else if (chosen) btn.classList.add("wrong");
                }
                btn.addEventListener("click", function () { study.answers[qi] = oi; render(); });
                group.appendChild(btn);
            });
            item.appendChild(group);
            if (study.submitted) {
                var right = study.answers[qi] === q.correct_index;
                var fb = el("div", "notes-quiz-feedback " + (right ? "right" : "wrong"),
                    (right ? "Correct. " : "Not quite. The answer is " + String.fromCharCode(65 + q.correct_index) + ". ") + (q.explanation || ""));
                item.appendChild(fb);
            }
            wrap.appendChild(item);
        });

        if (!study.submitted) {
            var answered = study.answers.every(function (a) { return a !== null; });
            var submit = el("button", "notes-btn notes-btn-primary", "Check answers");
            submit.type = "button";
            submit.disabled = !answered;
            if (!answered) submit.title = "Answer every question first";
            submit.addEventListener("click", function () { study.submitted = true; render(); });
            wrap.appendChild(submit);
        } else {
            var score = 0;
            var missed = [];
            study.questions.forEach(function (q, i) { if (study.answers[i] === q.correct_index) score++; else missed.push(q); });
            var result = el("div", "notes-quiz-score", "Score: " + score + " / " + study.questions.length);
            result.setAttribute("role", "status");
            wrap.appendChild(result);
            var row = el("div", "notes-study-actions");
            if (missed.length) {
                var practice = el("button", "notes-btn notes-btn-primary", "Practise the " + missed.length + " you missed");
                practice.type = "button";
                practice.addEventListener("click", function () {
                    study.questions = missed; study.answers = new Array(missed.length).fill(null); study.submitted = false; study.practice = true; render();
                });
                row.appendChild(practice);
            }
            var again = el("button", "notes-btn", "Retake from the start");
            again.type = "button";
            again.addEventListener("click", function () { openStored(note, "quiz"); });
            row.appendChild(again);
            row.appendChild(actionButton("Regenerate mini-quiz", function () { generate(note, "quiz", true); }, { needsNetwork: true }));
            wrap.appendChild(row);
        }
        typeset(wrap);
        return wrap;
    }

    // ------------------------------------------------------------
    // Lifecycle
    // ------------------------------------------------------------

    function onShow() {
        render();
        return loadNotes();
    }

    var initialized = false;

    function init() {
        if (initialized) return;      // DOMContentLoaded can be observed more than once in some hosts
        initialized = true;
        ensureShell();
        registerServiceWorker();

        window.addEventListener("online", function () { if (isViewVisible()) loadNotes(); else render(); });
        window.addEventListener("offline", function () { renderIfVisible(); });

        var client = authClient();
        if (client && client.auth && typeof client.auth.onAuthStateChange === "function") {
            client.auth.onAuthStateChange(function (event) {
                if (event === "SIGNED_OUT") onSignedOut();
                // Warm the offline cache soon after sign-in without delaying startup.
                if (event === "SIGNED_IN" || event === "INITIAL_SESSION") setTimeout(function () { if (!isOffline()) loadNotes(); }, 1500);
            });
        }
    }

    function isViewVisible() {
        var root = document.getElementById("notes-view");
        return !!root && !root.classList.contains("hidden");
    }

    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
    else init();

    window.KognitNotes = {
        buildSaveButton: buildSaveButton,
        onShow: onShow,
        loadNotes: loadNotes,
        // exposed for tests only
        _state: state,
        _makeClientId: makeClientId,
        _noteTitle: noteTitle,
        _onSignedOut: onSignedOut
    };
})();