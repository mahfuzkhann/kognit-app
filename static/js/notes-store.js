/* ============================================================
   KOGNIT POCKET NOTES - OFFLINE NOTE CACHE (IndexedDB)
   ============================================================
   Stores a copy of the signed-in student's notes so they can be READ
   with no network. It is a CACHE of server data, never a source of
   truth:

     * Read-only offline by approved design: there is no write queue,
       no offline create/edit/delete, no merge. The only writers are
       the sync helpers below, and they only ever store what the
       SERVER most recently returned (`replaceAll` after a full
       successful list, `put`/`remove` after a confirmed server write).
       Stale local content can therefore never overwrite newer server
       content - the server always wins on the next online load.

     * Scoped per user (`userId` is part of every key) and wiped on
       sign-out (`clearUser`), so a shared device never shows one
       student's notes to the next one.

     * Every operation can fail (private mode, quota, blocked
       upgrade, storage cleared by the browser). Failures reject with
       an Error whose `.code` is "quota" or "unavailable" so the
       caller can fall back to online-only and tell the student, never
       crash. If the browser clears the data, the cache is simply empty
       until the next online visit rebuilds it - nothing is lost,
       because the server copy is authoritative.

   `createNotesStore(idbFactory)` takes the IndexedDB factory as a
   parameter purely so the logic can be tested against a fake in jsdom;
   in the browser `window.KognitNotesStore` is created from
   `window.indexedDB`.
   ============================================================ */
(function (root) {
    "use strict";

    var DB_NAME = "kognit-notes";
    var DB_VERSION = 1;
    var NOTES = "notes";
    var META = "meta";

    function createNotesStore(idbFactory) {
        var dbPromise = null;

        function storageError(code, cause) {
            var err = new Error("notes-store: " + code);
            err.code = code;
            err.cause = cause;
            return err;
        }

        function classify(err) {
            var name = err && err.name;
            if (name === "QuotaExceededError" || (err && err.code === 22)) return "quota";
            return "unavailable";
        }

        function isSupported() {
            return !!idbFactory;
        }

        function open() {
            if (!idbFactory) return Promise.reject(storageError("unavailable"));
            if (dbPromise) return dbPromise;

            dbPromise = new Promise(function (resolve, reject) {
                var request;
                try {
                    request = idbFactory.open(DB_NAME, DB_VERSION);
                } catch (e) {
                    reject(storageError(classify(e), e));
                    return;
                }
                request.onupgradeneeded = function () {
                    var db = request.result;
                    if (!db.objectStoreNames.contains(NOTES)) {
                        var store = db.createObjectStore(NOTES, { keyPath: "key" });
                        store.createIndex("userId", "userId", { unique: false });
                    }
                    if (!db.objectStoreNames.contains(META)) {
                        db.createObjectStore(META, { keyPath: "key" });
                    }
                };
                request.onsuccess = function () {
                    var db = request.result;
                    // If another tab triggers an upgrade/delete, close so it
                    // is not blocked; the next call re-opens.
                    db.onversionchange = function () { db.close(); dbPromise = null; };
                    resolve(db);
                };
                request.onerror = function () { reject(storageError(classify(request.error), request.error)); };
                request.onblocked = function () { reject(storageError("unavailable")); };
            }).catch(function (err) {
                dbPromise = null;   // allow a later retry
                throw err;
            });
            return dbPromise;
        }

        // Runs `work(stores)` inside one transaction and resolves with its
        // return value only AFTER the transaction has committed.
        function run(mode, work) {
            return open().then(function (db) {
                return new Promise(function (resolve, reject) {
                    var tx, result;
                    try {
                        tx = db.transaction([NOTES, META], mode);
                        result = work({ notes: tx.objectStore(NOTES), meta: tx.objectStore(META) });
                    } catch (e) {
                        reject(storageError(classify(e), e));
                        return;
                    }
                    tx.oncomplete = function () { resolve(result && result.__resolve ? result.__resolve() : result); };
                    tx.onerror = function () { reject(storageError(classify(tx.error), tx.error)); };
                    tx.onabort = function () { reject(storageError(classify(tx.error), tx.error)); };
                });
            });
        }

        function keyFor(userId, noteId) { return userId + ":" + noteId; }

        function requestResult(req) {
            // Lazily-read result holder so the value is read after commit.
            return { __resolve: function () { return req.result; } };
        }

        // ----- reads -----

        function getAll(userId) {
            var notesReq, metaReq;
            return run("readonly", function (s) {
                notesReq = s.notes.index("userId").getAll(userId);
                metaReq = s.meta.get("sync:" + userId);
                return { __resolve: function () { return null; } };
            }).then(function () {
                var entries = (notesReq.result || []).slice();
                entries.sort(function (a, b) {
                    return String(b.note.created_at || "").localeCompare(String(a.note.created_at || ""));
                });
                return {
                    notes: entries.map(function (e) { return { note: e.note, cachedAt: e.cachedAt }; }),
                    syncedAt: metaReq.result ? metaReq.result.syncedAt : null
                };
            });
        }

        function getLastUserId() {
            var req;
            return run("readonly", function (s) { req = s.meta.get("lastUser"); return requestResult(req); })
                .then(function (row) { return row ? row.userId : null; });
        }

        // ----- writes (server-confirmed data only) -----

        // Replaces the user's whole cache with a COMPLETE server listing.
        // Only call this after every page of the list succeeded - a partial
        // listing would delete notes that merely were not fetched.
        function replaceAll(userId, notes, syncedAt) {
            return run("readwrite", function (s) {
                var keep = {};
                var now = new Date().toISOString();
                notes.forEach(function (note) {
                    var key = keyFor(userId, note.id);
                    keep[key] = true;
                    s.notes.put({ key: key, userId: userId, id: note.id, note: note, cachedAt: now });
                });
                var cursorReq = s.notes.index("userId").openKeyCursor(userId);
                cursorReq.onsuccess = function () {
                    var cursor = cursorReq.result;
                    if (!cursor) return;
                    if (!keep[cursor.primaryKey]) s.notes.delete(cursor.primaryKey);
                    cursor.continue();
                };
                s.meta.put({ key: "sync:" + userId, syncedAt: syncedAt });
                s.meta.put({ key: "lastUser", userId: userId });
            });
        }

        function put(userId, note) {
            return run("readwrite", function (s) {
                s.notes.put({
                    key: keyFor(userId, note.id), userId: userId, id: note.id,
                    note: note, cachedAt: new Date().toISOString()
                });
                s.meta.put({ key: "lastUser", userId: userId });
            });
        }

        function remove(userId, noteId) {
            return run("readwrite", function (s) { s.notes.delete(keyFor(userId, noteId)); });
        }

        // Sign-out: wipe this user's notes and sync marker.
        function clearUser(userId) {
            return run("readwrite", function (s) {
                var cursorReq = s.notes.index("userId").openKeyCursor(userId);
                cursorReq.onsuccess = function () {
                    var cursor = cursorReq.result;
                    if (!cursor) return;
                    s.notes.delete(cursor.primaryKey);
                    cursor.continue();
                };
                s.meta.delete("sync:" + userId);
                s.meta.delete("lastUser");
            });
        }

        return {
            isSupported: isSupported,
            getAll: getAll,
            getLastUserId: getLastUserId,
            replaceAll: replaceAll,
            put: put,
            remove: remove,
            clearUser: clearUser
        };
    }

    root.createNotesStore = createNotesStore;
    root.KognitNotesStore = createNotesStore(root.indexedDB || null);
})(typeof window !== "undefined" ? window : this);