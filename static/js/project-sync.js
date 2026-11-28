/* ==========================================================================
   Kloriz / Kognit - notebook synchronisation core (browser + Node-testable)

   Implements the client side of docs/SYNC_PROTOCOL.md:
     - UUID ids and explicit createdAt values for NEW notebooks/chats
     - the server-authoritative merge rule (never silently drops a message)
     - a version-checked (compare-and-set) saver around the
       save_user_project RPC from migration 0019, with:
         * one in-flight save per notebook (later changes are coalesced)
         * the SAME expected version on network retries (the RPC is replay-safe)
         * at most MAX_ATTEMPTS conflict-merge rounds, then "conflict_unresolved"
           (the working copy is kept; nothing is discarded)

   This file contains NO network code of its own: the caller injects `rpc`,
   a function (args) => Promise<{data, error}> (the Supabase client's
   rpc("save_user_project", args)). That keeps it testable without a browser.
   ========================================================================== */
(function (root) {
    "use strict";

    var MAX_ATTEMPTS = 3;
    var COPY_SUFFIX = " (copy from this device)";
    // Database error codes that retrying cannot fix (SYNC_PROTOCOL.md).
    var PERMANENT_CODES = { "28000": "not_signed_in", "22023": "invalid", "54000": "too_large" };

    // ---------------------------------------------------------------- ids
    function hex(n) {
        var s = "";
        for (var i = 0; i < n; i++) s += Math.floor(Math.random() * 16).toString(16);
        return s;
    }

    // RFC 4122 v4. Uses crypto.randomUUID / getRandomValues when present; the
    // Math.random fallback exists only for very old WebViews and is still
    // collision-safe for per-user ids (uniqueness is enforced by the database).
    function newId() {
        var c = root.crypto;
        if (c && typeof c.randomUUID === "function") return c.randomUUID();
        if (c && typeof c.getRandomValues === "function") {
            var b = new Uint8Array(16);
            c.getRandomValues(b);
            b[6] = (b[6] & 0x0f) | 0x40;
            b[8] = (b[8] & 0x3f) | 0x80;
            var h = Array.prototype.map.call(b, function (x) { return (x + 256).toString(16).slice(1); }).join("");
            return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
        }
        return hex(8) + "-" + hex(4) + "-4" + hex(3) + "-" + "89ab"[Math.floor(Math.random() * 4)] + hex(3) + "-" + hex(12);
    }

    // Creation time: an explicit createdAt wins; legacy "<prefix>_<ms>" ids
    // still carry theirs; anything else has none (never guessed).
    function createdAtOf(item, legacyPrefix) {
        if (!item) return null;
        if (typeof item.createdAt === "number" && isFinite(item.createdAt) && item.createdAt > 0) return item.createdAt;
        var m = new RegExp("^" + legacyPrefix + "_(\\d{10,})$").exec(item.id || "");
        if (!m) return null;
        var ms = Number(m[1]);
        return isFinite(ms) ? ms : null;
    }

    // -------------------------------------------------------------- merge
    function msgKey(m) {
        return JSON.stringify([m && m.role, m && m.text, (m && m.image) || null]);
    }
    function isPrefix(shorter, longer) {
        if (shorter.length > longer.length) return false;
        for (var i = 0; i < shorter.length; i++) {
            if (msgKey(shorter[i]) !== msgKey(longer[i])) return false;
        }
        return true;
    }
    function clone(x) { return JSON.parse(JSON.stringify(x)); }

    // Merge chat arrays. server = authoritative copy, local = working copy.
    //   opts.deletedChatIds : ids the student deleted in this session (never resurrected)
    //   opts.busyChatIds    : chats with a response being generated right now; their
    //                         local messages are never replaced by the server's
    //   opts.makeId         : id factory (tests)
    function mergeChats(localChats, serverChats, opts) {
        opts = opts || {};
        var deleted = opts.deletedChatIds || new Set();
        var busy = opts.busyChatIds || new Set();
        var makeId = opts.makeId || newId;
        var local = Array.isArray(localChats) ? localChats : [];
        var server = Array.isArray(serverChats) ? serverChats : [];
        var localById = {};
        var serverIds = {};
        local.forEach(function (c) { if (c && c.id) localById[c.id] = c; });
        server.forEach(function (c) { if (c && c.id) serverIds[c.id] = true; });

        var out = [];
        var copies = [];
        server.forEach(function (sc) {
            if (!sc || !sc.id) return;
            if (deleted.has(sc.id)) return;                 // deleted here: stays deleted
            var lc = localById[sc.id];
            if (!lc) { out.push(sc); return; }              // only on the server: keep
            var lm = Array.isArray(lc.messages) ? lc.messages : [];
            var sm = Array.isArray(sc.messages) ? sc.messages : [];
            if (isPrefix(sm, lm)) { out.push(lc); return; } // server is a prefix of local (or equal): local wins
            if (isPrefix(lm, sm) && !busy.has(lc.id)) {     // local is a prefix of server: server wins
                out.push(clone(sc));
                return;
            }
            if (isPrefix(lm, sm) && busy.has(lc.id)) { out.push(lc); return; }
            // Diverged: keep the server's chat under its id, keep ours as a NEW chat.
            out.push(sc);
            var copy = clone(lc);
            copy.id = makeId();
            copy.title = String(lc.title || "Chat") + COPY_SUFFIX;
            copy.createdAt = Date.now();
            copies.push(copy);
        });
        // Only local, not deleted: keep (union). They are newer than anything the
        // server knows, so they go to the front like a normal "new chat".
        var localOnly = local.filter(function (c) { return c && c.id && !serverIds[c.id]; });
        return localOnly.concat(copies, out);
    }

    // Merge a whole notebook. Title: the server's, unless renamed locally in this session.
    function mergeProject(local, server, opts) {
        opts = opts || {};
        return {
            id: server.id || local.id,
            title: opts.titleRenamedLocally ? local.title : (server.title || local.title),
            chats: mergeChats(local.chats, server.chats, opts),
            version: server.version,
            updated_at: server.updated_at || null
        };
    }

    // Apply a merged notebook to the live object IN PLACE. Chat objects that
    // already exist keep their identity (the streaming code holds references to
    // them), so a merge can never orphan a response that is being written.
    function applyMerged(project, merged) {
        var existing = {};
        (project.chats || []).forEach(function (c) { if (c && c.id) existing[c.id] = c; });
        var next = merged.chats.map(function (mc) {
            var live = existing[mc.id];
            if (!live || live === mc) return mc;
            Object.keys(live).forEach(function (k) { if (!(k in mc)) delete live[k]; });
            Object.keys(mc).forEach(function (k) {
                if (k === "messages" && Array.isArray(live.messages)) {
                    if (live.messages !== mc.messages) live.messages.splice.apply(live.messages, [0, live.messages.length].concat(mc.messages));
                } else {
                    live[k] = mc[k];
                }
            });
            return live;
        });
        if (!Array.isArray(project.chats)) project.chats = [];
        project.chats.splice.apply(project.chats, [0, project.chats.length].concat(next));
        project.title = merged.title;
        project.version = merged.version;
        project.updated_at = merged.updated_at;
        return project;
    }

    // -------------------------------------------------------------- saver
    // createSaver({ rpc, getBusyChatIds, onIdChanged })
    //   saveProject(project) -> Promise<{
    //     status: "saved" | "merged" | "recreated" | "conflict_unresolved" |
    //             "rejected" | "offline",
    //     reason?: "not_signed_in" | "invalid" | "too_large" | "network",
    //     idChanged?: {from, to}
    //   }>
    // "saved"/"merged"/"recreated" mean the server holds the project (ok = true).
    function createSaver(deps) {
        var rpc = deps.rpc;
        var getBusy = deps.getBusyChatIds || function () { return new Set(); };
        var inflight = {};            // project id -> { dirty, promise }
        var deletedChats = {};        // project id -> Set(chat id), until a save confirms
        var renamed = {};             // project id -> true, until a save confirms

        function noteChatDeleted(projectId, chatId) {
            (deletedChats[projectId] = deletedChats[projectId] || new Set()).add(chatId);
        }
        function noteRenamed(projectId) { renamed[projectId] = true; }
        function clearRenamed(projectId) { delete renamed[projectId]; }

        async function attemptOnce(project) {
            var outcome = "saved";
            var idChanged = null;
            var conflicts = 0;
            for (;;) {
                var res;
                try {
                    res = await rpc({
                        p_id: project.id,
                        p_title: project.title,
                        p_chats: project.chats,
                        p_expected_version: typeof project.version === "number" ? project.version : null
                    });
                } catch (e) {
                    return { status: "offline", reason: "network" };
                }
                if (!res || res.error) {
                    var code = res && res.error && String(res.error.code || "");
                    if (code && PERMANENT_CODES[code]) return { status: "rejected", reason: PERMANENT_CODES[code] };
                    return { status: "offline", reason: "network" };
                }
                var d = res.data;
                if (!d || typeof d.status !== "string") return { status: "offline", reason: "network" };

                if (d.status === "ok") {
                    project.version = d.version;
                    project.updated_at = d.updated_at || project.updated_at || null;
                    delete deletedChats[project.id];
                    delete renamed[project.id];
                    var r = { status: outcome };
                    if (idChanged) r.idChanged = idChanged;
                    return r;
                }
                if (d.status === "conflict" && d.server) {
                    conflicts++;
                    if (conflicts > MAX_ATTEMPTS) return { status: "conflict_unresolved" };
                    var merged = mergeProject(project, d.server, {
                        deletedChatIds: deletedChats[project.id],
                        busyChatIds: getBusy(),
                        titleRenamedLocally: !!renamed[project.id]
                    });
                    applyMerged(project, merged);
                    outcome = "merged";
                    continue;
                }
                if (d.status === "not_found") {
                    // Deleted on another device: keep our copy and re-create it.
                    project.version = null;
                    outcome = "recreated";
                    continue;
                }
                if (d.status === "id_unavailable") {
                    var from = project.id;
                    project.id = newId();
                    project.version = null;
                    idChanged = { from: idChanged ? idChanged.from : from, to: project.id };
                    if (deps.onIdChanged) deps.onIdChanged(idChanged.from, project.id);
                    continue;
                }
                return { status: "offline", reason: "network" };
            }
        }

        // One in-flight save per notebook. A change that arrives meanwhile marks it
        // dirty and is saved right after (always from the live object, so the
        // newest state is what gets sent). Duplicate rapid saves collapse into one.
        function saveProject(project) {
            var key = project.id;
            var state = inflight[key];
            if (state) { state.dirty = true; return state.promise; }
            state = { dirty: false, promise: null };
            inflight[key] = state;
            state.promise = (async function () {
                var result;
                try {
                    do {
                        state.dirty = false;
                        result = await attemptOnce(project);
                    } while (state.dirty && (result.status === "saved" || result.status === "merged" || result.status === "recreated"));
                } finally {
                    delete inflight[key];
                }
                return result;
            })();
            return state.promise;
        }

        return { saveProject: saveProject, noteChatDeleted: noteChatDeleted, noteRenamed: noteRenamed, clearRenamed: clearRenamed };
    }

    var api = {
        MAX_ATTEMPTS: MAX_ATTEMPTS,
        COPY_SUFFIX: COPY_SUFFIX,
        newId: newId,
        createdAtOf: createdAtOf,
        mergeChats: mergeChats,
        mergeProject: mergeProject,
        applyMerged: applyMerged,
        createSaver: createSaver
    };
    root.KognitSync = api;
    if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof window !== "undefined" ? window : globalThis);