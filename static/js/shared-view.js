/* ============================================================
   KLORIZ SHARED-CHAT PAGE (public, read-only)
   ============================================================
   Runs on /s/{token} (templates/shared.html). No account, no Supabase,
   no app.js. It fetches ONE snapshot from /api/shared/{token} and renders
   it.

   The content was written by someone else (a student and an AI model), so
   it is treated as untrusted exactly like model output in the main app:

     1. math is lifted out into inert placeholders (so Markdown can not
        mangle LaTeX, and LaTeX can not inject markup),
     2. the text is parsed as Markdown,
     3. the HTML is sanitised by DOMPurify with a strict allow-list
        (NO <img>, no forms, no scripts, no styles, https/mailto links only,
        every link rel="noopener noreferrer nofollow" + target=_blank),
     4. only then is the math put back - HTML-escaped, so it is text,
     5. MathJax typesets it (its \href/\class/\style/\cssId/\bbox macros are
        neutralised in shared.html).

   User messages are shown with textContent (no Markdown at all).
   If DOMPurify or marked failed to load, everything is shown as plain
   escaped text - it fails closed, never open.
   ============================================================ */

(function () {
    "use strict";

    var FB = window.KlorizFeedback || null;
    function say(key) {
        return FB ? FB.msg(key) : "কিছু একটা সমস্যা হয়েছে। আবার চেষ্টা করুন।";
    }

    var root = document.getElementById("shared-root");
    var TOKEN_PATH = /^\/s\/([0-9a-f]{40})\/?$/;

    /* ---------------- math protection ---------------- */

    // Walks the text once, leaving code spans/fences untouched and replacing every
    // math segment with an alphanumeric placeholder Markdown can not alter.
    function protectMath(text) {
        var stash = [];
        var out = "";
        var i = 0;
        var n = text.length;

        function keep(segment) {
            stash.push(segment);
            return "KLRZMATH" + (stash.length - 1) + "X";
        }

        while (i < n) {
            var ch = text.charAt(i);

            // fenced code: copy through untouched
            if (text.substr(i, 3) === "```") {
                var endFence = text.indexOf("```", i + 3);
                var stop = endFence === -1 ? n : endFence + 3;
                out += text.slice(i, stop);
                i = stop;
                continue;
            }
            // inline code span
            if (ch === "`") {
                var endTick = text.indexOf("`", i + 1);
                if (endTick !== -1 && text.slice(i + 1, endTick).indexOf("\n") === -1) {
                    out += text.slice(i, endTick + 1);
                    i = endTick + 1;
                    continue;
                }
            }
            // escaped dollar / backslash pair: not a delimiter
            if (ch === "\\" && text.charAt(i + 1) === "$") {
                out += "\\$";
                i += 2;
                continue;
            }
            // $$ ... $$
            if (text.substr(i, 2) === "$$") {
                var endD = text.indexOf("$$", i + 2);
                if (endD !== -1) {
                    out += keep(text.slice(i, endD + 2));
                    i = endD + 2;
                    continue;
                }
            }
            // \[ ... \]  and  \( ... \)
            if (text.substr(i, 2) === "\\[" || text.substr(i, 2) === "\\(") {
                var close = text.charAt(i + 1) === "[" ? "\\]" : "\\)";
                var endB = text.indexOf(close, i + 2);
                if (endB !== -1) {
                    out += keep(text.slice(i, endB + 2));
                    i = endB + 2;
                    continue;
                }
            }
            // $ ... $ : same line, no space just inside, so "$5 and $10" stays text
            if (ch === "$") {
                var endI = i + 1;
                var found = -1;
                while (endI < n) {
                    var c = text.charAt(endI);
                    if (c === "\n") break;
                    if (c === "\\") { endI += 2; continue; }
                    if (c === "$") { found = endI; break; }
                    endI++;
                }
                if (found > i + 1 && !/\s/.test(text.charAt(i + 1)) && !/\s/.test(text.charAt(found - 1))) {
                    out += keep(text.slice(i, found + 1));
                    i = found + 1;
                    continue;
                }
            }
            out += ch;
            i++;
        }
        return { text: out, stash: stash };
    }

    function escapeHtml(value) {
        return String(value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    /* ---------------- sanitiser ---------------- */

    var ALLOWED_TAGS = [
        "p", "br", "hr", "strong", "b", "em", "i", "del", "s", "code", "pre", "blockquote",
        "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6",
        "table", "thead", "tbody", "tr", "th", "td", "a", "span", "sup", "sub"
    ];
    var ALLOWED_ATTR = ["href", "title", "colspan", "rowspan", "align"];

    var hookInstalled = false;
    function installHook() {
        if (hookInstalled || !window.DOMPurify) return;
        hookInstalled = true;
        window.DOMPurify.addHook("afterSanitizeAttributes", function (node) {
            if (node.tagName === "A") {
                node.setAttribute("target", "_blank");
                node.setAttribute("rel", "noopener noreferrer nofollow");
            }
        });
    }

    function renderBotHtml(text) {
        var purifier = window.DOMPurify;
        var parser = window.marked;
        if (!purifier || typeof purifier.sanitize !== "function" || !parser || typeof parser.parse !== "function") {
            return null; // caller shows plain text instead
        }
        installHook();
        var protectedText = protectMath(text);
        var html = parser.parse(protectedText.text, { gfm: true, breaks: true, async: false });
        var clean = purifier.sanitize(html, {
            ALLOWED_TAGS: ALLOWED_TAGS,
            ALLOWED_ATTR: ALLOWED_ATTR,
            ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
            ALLOW_DATA_ATTR: false,
            ALLOW_ARIA_ATTR: false,
            KEEP_CONTENT: true,
            FORCE_BODY: true
        });
        // Math goes back only now, escaped, so it can never become markup.
        return clean.replace(/KLRZMATH(\d+)X/g, function (_m, idx) {
            var seg = protectedText.stash[Number(idx)];
            return seg === undefined ? "" : escapeHtml(seg);
        });
    }

    /* ---------------- MathJax (best effort) ---------------- */

    function typeset(el) {
        var tries = 0;
        (function attempt() {
            var mj = window.MathJax;
            if (mj && typeof mj.typesetPromise === "function") {
                mj.typesetPromise([el]).catch(function () { /* equations stay as text */ });
                return;
            }
            if (++tries < 50) setTimeout(attempt, 200); // up to ~10 s, then give up quietly
        })();
    }

    /* ---------------- views ---------------- */

    function setBusy(busy) {
        root.setAttribute("aria-busy", busy ? "true" : "false");
    }

    function showMessage(text, withRetry) {
        setBusy(false);
        root.textContent = "";
        var p = document.createElement("p");
        p.className = "shared-status shared-status-error";
        p.setAttribute("role", "alert");
        p.textContent = text;
        root.appendChild(p);
        if (withRetry) {
            var b = document.createElement("button");
            b.type = "button";
            b.className = "shared-retry";
            b.textContent = say("retry");
            b.addEventListener("click", load);
            root.appendChild(b);
        }
    }

    function formatDate(iso) {
        if (typeof iso !== "string") return "";
        var d = new Date(iso);
        if (isNaN(d.getTime())) return "";
        try {
            return new Intl.DateTimeFormat("bn-BD", { year: "numeric", month: "long", day: "numeric" }).format(d);
        } catch (e) { return ""; }
    }

    function bn(n) {
        try { return Number(n).toLocaleString("bn-BD"); } catch (e) { return String(n); }
    }

    function render(data) {
        setBusy(false);
        root.textContent = "";

        var title = typeof data.title === "string" && data.title ? data.title : "Shared chat";
        document.title = title + " · Kloriz";

        var h1 = document.createElement("h1");
        h1.className = "shared-title";
        h1.textContent = title;
        root.appendChild(h1);

        var meta = document.createElement("p");
        meta.className = "shared-meta";
        var when = formatDate(data.created_at);
        meta.textContent = bn(data.message_count) + "টি বার্তা" + (when ? " · শেয়ার করা হয়েছে " + when : "");
        root.appendChild(meta);

        var list = document.createElement("div");
        list.className = "shared-messages";
        data.messages.forEach(function (m) {
            var row = document.createElement("article");
            row.className = "shared-msg shared-msg-" + (m.role === "user" ? "user" : "bot");

            var who = document.createElement("h2");
            who.className = "shared-msg-who";
            who.textContent = m.role === "user" ? "ব্যবহারকারী" : "Kloriz";
            row.appendChild(who);

            var body = document.createElement("div");
            body.className = "shared-msg-body";
            if (m.role === "user") {
                body.textContent = m.text; // no Markdown, no HTML: just text
            } else {
                var html = renderBotHtml(m.text);
                if (html === null) {
                    var pre = document.createElement("div");
                    pre.className = "shared-plain";
                    pre.textContent = m.text;
                    body.appendChild(pre);
                } else {
                    body.innerHTML = html; // sanitised above; math placeholders restored as escaped text
                }
            }
            row.appendChild(body);
            list.appendChild(row);
        });
        root.appendChild(list);
        typeset(list);
    }

    /* ---------------- load ---------------- */

    function load() {
        var match = TOKEN_PATH.exec(window.location.pathname);
        if (!match) { showMessage(say("shareUnavailable"), false); return; }

        setBusy(true);
        root.textContent = "";
        var status = document.createElement("p");
        status.className = "shared-status";
        status.textContent = "লোড হচ্ছে…";
        root.appendChild(status);

        fetch("/api/shared/" + match[1], {
            method: "GET",
            headers: { Accept: "application/json" },
            credentials: "omit",
            cache: "no-store",
            referrerPolicy: "no-referrer"
        }).then(function (res) {
            if (res.status === 404) { showMessage(say("shareUnavailable"), false); return null; }
            if (!res.ok) { showMessage(say("shareLoadFailed"), true); return null; }
            return res.json();
        }).then(function (data) {
            if (!data) return;
            if (!data || !Array.isArray(data.messages) || !data.messages.length) {
                showMessage(say("shareUnavailable"), false);
                return;
            }
            render(data);
        }).catch(function () {
            showMessage(say("network"), true);
        });
    }

    load();
})();