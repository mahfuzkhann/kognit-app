/* ============================================================
   KLORIZ SHARE DIALOG
   ============================================================
   Opt-in sharing of ONE chat as a frozen, revocable snapshot.

   FLOW
     Share button -> (signed in?) -> is this chat already shared?
       no  -> PREVIEW: exactly which messages would be shared, who can open the
              link, what is NOT included -> student presses "Create link"
       yes -> LINK view: copy / WhatsApp / Facebook / Telegram / Email / the
              device share sheet, and "Stop sharing"

   WHAT THE SERVER GETS
     Only {chat_id, title, messages:[{role,text}]} - the messages captured at the
     moment the preview was shown. What the student approved is what is stored.
     The link is /s/<40-hex token>; the page it opens is read-only, needs no
     account and has no path to the student's real chat or any other data.

   HONESTY RULES
     - "Link copied" is shown only after the browser confirmed the copy.
     - Opening WhatsApp / Facebook / Telegram / an email draft only opens a
       composer in a new tab. Nothing here says a message was sent.
     - If the server or the database migration is not ready, the dialog says
       sharing is not available - it never pretends a link exists.

   Depends on: window.KlorizAppBridge (app.js), window.KlorizFeedback
   (feedback.js, optional - falls back to plain wording).
   ============================================================ */

(function () {
    "use strict";

    var modal = null;
    var body = null;
    var state = {
        chat: null,        // {chatId, title, messages[]} captured when the preview was built
        share: null,       // {token, path, title, message_count, created_at} when a link exists
        busy: false
    };

    var PATH_RE = /^\/s\/[0-9a-f]{40}$/;

    function fb() { return window.KlorizFeedback || null; }
    function say(key) { return fb() ? fb().msg(key) : "Something went wrong. Please try again."; }
    function bridge() { return window.KlorizAppBridge || null; }

    /* ---------------- tiny DOM helpers ---------------- */

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = text;
        return node;
    }

    function button(label, className, onClick) {
        var b = el("button", className, label);
        b.type = "button";
        if (onClick) b.addEventListener("click", onClick);
        return b;
    }

    function setBusy(busy) {
        state.busy = busy;
        if (modal) {
            if (busy) modal.setAttribute("data-busy", "true"); else modal.removeAttribute("data-busy");
        }
        if (body) {
            Array.prototype.forEach.call(body.querySelectorAll("button"), function (b) { b.disabled = busy; });
        }
    }

    function clearBody() {
        body.textContent = "";
    }

    function statusLine(text, isError) {
        var p = el("p", "share-status" + (isError ? " share-status-error" : ""), text);
        p.setAttribute("role", isError ? "alert" : "status");
        return p;
    }

    /* ---------------- network ---------------- */

    async function authedFetch(url, options) {
        var b = bridge();
        var token = b ? await b.getAccessToken() : null;
        if (!token) {
            var e = new Error("no session");
            e.code = "NO_SESSION";
            throw e;
        }
        var opts = Object.assign({ cache: "no-store" }, options || {});
        opts.headers = Object.assign({ Authorization: "Bearer " + token, Accept: "application/json" }, opts.headers || {});
        return fetch(url, opts);
    }

    async function readDetail(res) {
        try {
            var data = await res.json();
            return data && data.detail && typeof data.detail === "object" ? data.detail : null;
        } catch (e) { return null; }
    }

    // One place that turns a failed share response into a Bangla sentence.
    async function messageForFailure(res, fallbackKey) {
        var detail = await readDetail(res);
        var code = detail && detail.code;
        if (code === "SHARING_UNAVAILABLE") return say("shareNotReady");
        if (code === "SHARE_EMPTY") return say("shareEmptyChat");
        if (code === "SHARE_TOO_LONG" || res.status === 413) return say("shareTooLong");
        if (code === "SHARE_LIMIT") return say("shareLimit");
        if (res.status === 429 && fb()) {
            return fb().classifyFailure({ status: 429, retryAfter: res.headers.get("Retry-After") }).text;
        }
        return say(fallbackKey);
    }

    function handleSessionProblem(res) {
        if (res && res.status === 401) {
            window.closeShareModal();
            var b = bridge();
            if (b) b.promptLogin();
            return true;
        }
        return false;
    }

    /* ---------------- views ---------------- */

    function renderLoading() {
        clearBody();
        body.appendChild(statusLine("লোড হচ্ছে…", false));
    }

    function renderMessage(text, isError, withRetry) {
        clearBody();
        body.appendChild(statusLine(text, isError));
        var row = el("div", "share-actions");
        if (withRetry) row.appendChild(button(say("retry"), "share-btn share-btn-primary", function () { loadForCurrentChat(); }));
        row.appendChild(button("Close", "share-btn", function () { window.closeShareModal(); }));
        body.appendChild(row);
    }

    function renderPreview() {
        var chat = state.chat;
        clearBody();

        var note = el("p", "share-note");
        var strong = el("strong", null, "Anyone with the link can read this copy");
        note.appendChild(strong);
        note.appendChild(document.createTextNode(
            " - no account needed. It contains only the " + chat.messages.length + " message" +
            (chat.messages.length === 1 ? "" : "s") + " listed below, as they are now. Your name, email, other chats and any images are not included, " +
            "and new messages will not appear in it. You can stop sharing at any time."
        ));
        var bn = el("span", "share-note-bn");
        bn.setAttribute("lang", "bn");
        bn.textContent = " লিংক যার কাছে থাকবে সে-ই এই কপিটি পড়তে পারবে। নিচের বার্তাগুলোই শেয়ার হবে; আপনার নাম, ইমেইল, অন্য চ্যাট ও ছবি শেয়ার হবে না। যেকোনো সময় শেয়ার বন্ধ করতে পারবেন।";
        note.appendChild(el("br"));
        note.appendChild(bn);
        body.appendChild(note);

        body.appendChild(el("p", "share-preview-head", "Preview - \u201C" + chat.title + "\u201D"));
        var list = el("ul", "share-preview");
        list.setAttribute("aria-label", "Messages that will be shared");
        chat.messages.forEach(function (m) {
            var li = el("li");
            li.appendChild(el("span", "share-preview-role", m.role === "user" ? "You" : "Kloriz"));
            li.appendChild(el("span", "share-preview-text", m.text));
            list.appendChild(li);
        });
        body.appendChild(list);

        var status = statusLine("", false);
        status.id = "share-inline-status";
        body.appendChild(status);

        var actions = el("div", "share-actions");
        var create = button("Create link", "share-btn share-btn-primary", createShare);
        actions.appendChild(create);
        actions.appendChild(button("Cancel", "share-btn", function () { window.closeShareModal(); }));
        body.appendChild(actions);
        create.focus();
    }

    function absoluteUrl(path) {
        if (!PATH_RE.test(path || "")) return null;
        return window.location.origin + path;
    }

    function shareText(share) {
        return (share.title || "Kloriz") + " - Kloriz";
    }

    function renderLink() {
        var share = state.share;
        var url = absoluteUrl(share.path);
        if (!url) { renderMessage(say("shareCreateFailed"), true, false); return; }
        clearBody();

        var when = "";
        if (share.created_at) {
            var d = new Date(share.created_at);
            if (!isNaN(d.getTime())) {
                try { when = " on " + new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(d); } catch (e) { when = ""; }
            }
        }
        body.appendChild(el("p", "share-note",
            "This link is live. Anyone who has it can read a copy of " + (share.message_count || "these") +
            " message" + (share.message_count === 1 ? "" : "s") + " from this chat" + when +
            ". New messages are not included."));

        var row = el("div", "share-link-row");
        var input = el("input", "share-link-input");
        input.type = "text";
        input.readOnly = true;
        input.value = url;
        input.id = "share-link-input";
        input.setAttribute("aria-label", "Share link");
        input.addEventListener("focus", function () { input.select(); });
        row.appendChild(input);
        row.appendChild(button("Copy link", "share-btn", function () { copyLink(input, url); }));
        body.appendChild(row);

        var dest = el("div", "share-actions");
        var text = shareText(share);
        dest.appendChild(linkButton("WhatsApp", "https://wa.me/?text=" + encodeURIComponent(text + "\n" + url)));
        dest.appendChild(linkButton("Facebook", "https://www.facebook.com/sharer/sharer.php?u=" + encodeURIComponent(url)));
        dest.appendChild(linkButton("Telegram", "https://t.me/share/url?url=" + encodeURIComponent(url) + "&text=" + encodeURIComponent(text)));
        dest.appendChild(linkButton("Email", "mailto:?subject=" + encodeURIComponent(text) + "&body=" + encodeURIComponent(url), true));
        if (typeof navigator.share === "function") {
            dest.appendChild(button("More\u2026", "share-btn", function () { nativeShare(share, url); }));
        }
        body.appendChild(dest);

        var status = statusLine("", false);
        status.id = "share-inline-status";
        body.appendChild(status);

        body.appendChild(el("p", "share-fineprint",
            "Buttons above open the app or page you choose so you can send it yourself; Kloriz can not tell whether you send it."));

        var actions = el("div", "share-actions");
        actions.appendChild(button("Stop sharing", "share-btn share-btn-danger", revokeShare));
        actions.appendChild(button("Done", "share-btn", function () { window.closeShareModal(); }));
        body.appendChild(actions);
    }

    function linkButton(label, href, sameTab) {
        var a = el("a", "share-btn", label);
        a.href = href;
        if (!sameTab) {
            a.target = "_blank";
            a.rel = "noopener noreferrer";
        }
        return a;
    }

    function inlineStatus(text, isError) {
        var node = document.getElementById("share-inline-status");
        if (!node) return;
        node.textContent = text;
        node.className = "share-status" + (isError ? " share-status-error" : "");
        node.setAttribute("role", isError ? "alert" : "status");
    }

    /* ---------------- actions ---------------- */

    async function copyLink(input, url) {
        var ok = false;
        if (navigator.clipboard && typeof navigator.clipboard.writeText === "function" && window.isSecureContext !== false) {
            try { await navigator.clipboard.writeText(url); ok = true; } catch (e) { ok = false; }
        }
        if (!ok) {
            // Older browsers / insecure origins: select the field and use the legacy copy.
            try {
                input.focus();
                input.select();
                input.setSelectionRange(0, url.length);
                ok = document.execCommand && document.execCommand("copy") === true;
            } catch (e) { ok = false; }
        }
        // Success is announced ONLY when a copy path actually succeeded; otherwise the
        // link stays selected so the student can copy it by hand.
        if (ok) {
            inlineStatus(say("shareCopied"), false);
        } else {
            try { input.focus(); input.select(); } catch (e) { /* ignore */ }
            inlineStatus(say("shareCopyFailed"), true);
        }
    }

    async function nativeShare(share, url) {
        try {
            await navigator.share({ title: share.title || "Kloriz", text: shareText(share), url: url });
            // Resolved = the student finished the system share sheet. We still do not
            // claim anything was received by anyone.
        } catch (e) {
            // AbortError = the student closed the sheet: not an error. Anything else:
            // the other buttons / the copy field are still there.
            if (e && e.name !== "AbortError") inlineStatus(say("shareCopyFailed"), true);
        }
    }

    async function createShare() {
        if (state.busy || !state.chat) return;
        setBusy(true);
        inlineStatus("লোড হচ্ছে…", false);
        try {
            var res = await authedFetch("/api/share", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    chat_id: state.chat.chatId,
                    title: state.chat.title,
                    messages: state.chat.messages
                })
            });
            if (handleSessionProblem(res)) return;
            if (!res.ok) {
                setBusy(false);
                inlineStatus(await messageForFailure(res, "shareCreateFailed"), true);
                return;
            }
            var data = await res.json();
            if (!data || !data.share) throw new Error("bad shape");
            state.share = data.share;
            setBusy(false);
            renderLink();
        } catch (e) {
            setBusy(false);
            if (e && e.code === "NO_SESSION") { window.closeShareModal(); var b = bridge(); if (b) b.promptLogin("sessionExpired"); return; }
            inlineStatus(fb() ? fb().classifyFailure({ status: null }).text : say("shareCreateFailed"), true);
        }
    }

    async function revokeShare() {
        if (state.busy || !state.chat) return;
        setBusy(true);
        inlineStatus("লোড হচ্ছে…", false);
        try {
            var res = await authedFetch("/api/share?chat_id=" + encodeURIComponent(state.chat.chatId), { method: "DELETE" });
            if (handleSessionProblem(res)) return;
            if (!res.ok) {
                setBusy(false);
                inlineStatus(await messageForFailure(res, "shareRevokeFailed"), true);
                return;
            }
            // The link is gone server-side; go back to the preview (a fresh link can be made).
            state.share = null;
            setBusy(false);
            renderPreview();
            inlineStatus(say("shareStopped"), false);
        } catch (e) {
            setBusy(false);
            inlineStatus(say("shareRevokeFailed"), true);
        }
    }

    async function loadForCurrentChat() {
        if (!state.chat) return;
        renderLoading();
        try {
            var res = await authedFetch("/api/share?chat_id=" + encodeURIComponent(state.chat.chatId));
            if (handleSessionProblem(res)) return;
            if (!res.ok) {
                renderMessage(await messageForFailure(res, "shareLoadFailed"), true, res.status >= 500 || res.status === 429);
                return;
            }
            var data = await res.json();
            if (data && data.share) {
                state.share = data.share;
                renderLink();
            } else {
                state.share = null;
                renderPreview();
            }
        } catch (e) {
            if (e && e.code === "NO_SESSION") { window.closeShareModal(); var b = bridge(); if (b) b.promptLogin("sessionExpired"); return; }
            renderMessage(fb() ? fb().classifyFailure({ status: null }).text : say("shareLoadFailed"), true, true);
        }
    }

    /* ---------------- public API ---------------- */

    window.shareChatLink = function () {
        modal = document.getElementById("share-modal");
        body = document.getElementById("share-body");
        var b = bridge();
        if (!modal || !body || !b) return;

        // Sharing needs an account: the snapshot belongs to someone and can be revoked by them.
        if (!b.isSignedIn()) { b.promptLogin(); return; }

        var chat = b.getShareableChat();
        var messages = chat ? chat.messages.filter(function (m) {
            return (m.role === "user" || m.role === "bot") && typeof m.text === "string" && m.text.trim();
        }) : [];

        state.share = null;
        state.busy = false;
        state.chat = chat ? { chatId: chat.chatId, title: chat.title || "Shared chat", messages: messages } : null;

        modal.removeAttribute("data-busy");
        modal.classList.remove("hidden");

        if (!state.chat || !messages.length) {
            renderMessage(say("shareEmptyChat"), false, false);
            return;
        }
        loadForCurrentChat();
    };

    window.closeShareModal = function () {
        var m = document.getElementById("share-modal");
        if (m) { m.classList.add("hidden"); m.removeAttribute("data-busy"); }
        state.chat = null;
        state.share = null;
        state.busy = false;
    };
})();