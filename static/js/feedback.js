/* ============================================================
   KLORIZ FEEDBACK - short Bangla messages, one accessible toast,
   and one place that decides WHICH message a failure deserves.
   ============================================================
   WHY THIS FILE EXISTS
   Errors used to be a mix of English strings, raw server text and
   native alert() boxes, and a daily-limit refusal looked the same as
   a temporary slow-down. This module is the single source for:

     1. the student-facing wording (Bangla, calm, non-technical),
     2. a small toast (role=status / role=alert, never a blocking box),
     3. classifyFailure(): maps an HTTP status / structured server body
        / network failure to { code, text, action }.

   WHAT IT DELIBERATELY DOES NOT DO
     - It never shows raw exception text, stack traces, table names,
       request ids or any other internal detail. Unknown failures get
       the generic message below; the details stay in the console /
       server logs.
     - It never decides entitlement. A "limit reached" message is only
       shown when the SERVER said so (429 QUOTA_EXCEEDED). The UI can
       not grant or restore usage.
     - The "plans" action only opens the existing in-app Plan screen
       (KlorizProfileMenu.openAccount("plan")). There is no checkout
       or payment page in this product yet, so no message here says
       that an upgrade is available.

   LANGUAGE
   The messages requested for this scope are Bangla only. They do not
   follow the account-menu language switch (static/js/i18n.js): that
   dictionary is deliberately limited to the account screens. Moving
   these strings into it later is a mechanical change - every string
   lives in MESSAGES below, keyed by name.

   Pure and DOM-light: loaded before app.js, depends on nothing.
   ============================================================ */

(function () {
    "use strict";

    /* ---------------- wording (single source) ---------------- */

    var MESSAGES = {
        // connection / server
        network: "ইন্টারনেট সংযোগে সমস্যা হচ্ছে। সংযোগ ঠিক আছে কিনা দেখে আবার চেষ্টা করুন।",
        timeout: "উত্তর আসতে অনেক সময় লাগছে। একটু পরে আবার চেষ্টা করুন।",
        serverBusy: "Kloriz এখন সাময়িকভাবে ব্যস্ত। কিছুক্ষণ পর আবার চেষ্টা করুন।",
        unknown: "কিছু একটা সমস্যা হয়েছে। আবার চেষ্টা করুন।",
        badRequest: "অনুরোধটি পাঠানো যায়নি। লেখাটি দেখে আবার চেষ্টা করুন।",
        tooLarge: "ফাইলটি অনেক বড়। ছোট একটি ফাইল দিয়ে আবার চেষ্টা করুন।",

        // generation
        interrupted: "উত্তরটি মাঝপথে থেমে গেছে। উত্তরটি আবার তৈরি করুন।",
        interruptedNoText: "উত্তর তৈরি করার আগেই সংযোগ বিচ্ছিন্ন হয়েছে। আবার চেষ্টা করুন।",
        regenerateFailed: "উত্তরটি আবার তৈরি করা যায়নি। পুরোনো উত্তরটি রাখা হয়েছে। আবার চেষ্টা করুন।",
        imageReattach: "ছবিটি আবার যুক্ত করা যায়নি। ছবিটি নতুন করে দিয়ে চেষ্টা করুন।",
        imageUnreadable: "ছবিটি পড়া যায়নি। অন্য একটি ছবি দিয়ে আবার চেষ্টা করুন।",
        blocked: "এই প্রশ্নের উত্তর তৈরি করা যায়নি। প্রশ্নটি অন্যভাবে লিখে আবার চেষ্টা করুন।",
        tooLongAnswer: "উত্তরটি খুব বড় হয়ে গেছে। প্রশ্নটি ছোট করে বা ভাগ করে করুন।",

        // limits
        rateLimited: "অল্প সময়ে অনেক অনুরোধ হয়ে গেছে। একটু অপেক্ষা করে আবার চেষ্টা করুন।",
        rateLimitedWait: "অল্প সময়ে অনেক অনুরোধ হয়ে গেছে। {n} সেকেন্ড পর আবার চেষ্টা করুন।",
        quotaChat: "আপনার চ্যাটের সীমা শেষ হয়ে গেছে।",
        quotaQuiz: "আপনার কুইজের সীমা শেষ হয়ে গেছে।",
        quotaNotes: "আপনার নোট টুলের সীমা শেষ হয়ে গেছে।",
        quotaGeneric: "আপনার ব্যবহারের সীমা শেষ হয়ে গেছে।",
        quotaFree: "ফ্রি প্ল্যানে ",
        quotaResets: " {when} থেকে আবার ব্যবহার করতে পারবেন।",
        quotaUnavailable: "এখন আপনার ব্যবহারের সীমা যাচাই করা যাচ্ছে না। একটু পরে আবার চেষ্টা করুন।",
        planNotConfigured: "আপনার প্ল্যানটি এখনও চালু হয়নি। পরে আবার চেষ্টা করুন।",

        // session / documents
        sessionExpired: "আপনার সেশনের মেয়াদ শেষ হয়েছে। নতুন করে লগইন করুন।",
        pdfExpired: "এই চ্যাটের পিডিএফটি আর পাওয়া যাচ্ছে না। পিডিএফটি আবার আপলোড করুন।",
        pdfUploadFailed: "পিডিএফটি আপলোড করা যায়নি। ফাইলটি ঠিক আছে কিনা দেখে আবার চেষ্টা করুন।",

        // quiz
        quizFailed: "কুইজ তৈরি করা যায়নি। একটু পরে আবার চেষ্টা করুন।",
        quizNoQuestions: "এই বিষয়ে কুইজ তৈরি করা যায়নি। বিষয়ের নাম বদলে আবার চেষ্টা করুন।",
        quizSaveFailed: "এই ফলাফলটি সংরক্ষণ করা যায়নি। কুইজটি আবার দিয়ে দেখতে পারেন।",
        quizSelectAnswer: "পরের প্রশ্নে যাওয়ার আগে একটি উত্তর বেছে নিন।",
        quizCloseTitle: "কুইজ বন্ধ করবেন?",
        quizCloseBody: "এখন বন্ধ করলে এই কুইজে দেওয়া উত্তরগুলো মুছে যাবে।",
        quizContinue: "কুইজ চালিয়ে যান",
        quizDiscard: "বাদ দিন",

        messageEmpty: "মেসেজটি খালি রাখা যাবে না।",

        // copy
        copyFailed: "কপি করা যায়নি। লেখাটি নিজে সিলেক্ট করে কপি করুন।",
        copied: "কপি করা হয়েছে।",

        // authentication
        authEmpty: "ইমেইল ও পাসওয়ার্ড দুটোই লিখুন।",
        authEmailInvalid: "সঠিক ইমেইল ঠিকানা লিখুন।",
        authWrongCredentials: "ইমেইল বা পাসওয়ার্ড মিলছে না। আবার দেখে চেষ্টা করুন।",
        authEmailNotConfirmed: "আপনার ইমেইলটি এখনও যাচাই করা হয়নি। ইমেইলে পাঠানো লিংকে ক্লিক করে যাচাই করুন।",
        authUserExists: "এই ইমেইল দিয়ে আগেই অ্যাকাউন্ট খোলা হয়েছে। লগইন করুন।",
        authPasswordMin: "পাসওয়ার্ড কমপক্ষে {n} অক্ষরের হতে হবে।",
        loginRequired: "চালিয়ে যেতে লগইন করুন অথবা নতুন অ্যাকাউন্ট খুলুন।",
        authWeakPassword: "পাসওয়ার্ডটি যথেষ্ট শক্ত নয়। আরও বড় বা জটিল পাসওয়ার্ড দিন।",
        authRateLimited: "অল্প সময়ে অনেকবার চেষ্টা করা হয়েছে। কিছুক্ষণ পর আবার চেষ্টা করুন।",
        authFailed: "লগইন করা যায়নি। আবার চেষ্টা করুন।",
        signupFailed: "অ্যাকাউন্ট খোলা যায়নি। আবার চেষ্টা করুন।",
        signupCheckEmail: "অ্যাকাউন্ট খোলা হয়েছে। ইমেইলে পাঠানো লিংকে ক্লিক করে যাচাই করুন, তারপর লগইন করুন।",
        googleFailed: "Google দিয়ে লগইন শুরু করা যায়নি। আবার চেষ্টা করুন।",

        // sharing
        shareUnavailable: "এই শেয়ার করা চ্যাটটি পাওয়া যাচ্ছে না। লিংকটি ভুল হতে পারে অথবা মালিক শেয়ার বন্ধ করেছেন।",
        shareCreateFailed: "শেয়ার লিংক তৈরি করা যায়নি। আবার চেষ্টা করুন।",
        shareRevokeFailed: "শেয়ার বন্ধ করা যায়নি। আবার চেষ্টা করুন।",
        shareEmptyChat: "শেয়ার করার মতো কোনো বার্তা এই চ্যাটে নেই।",
        shareTooLong: "চ্যাটটি শেয়ার করার জন্য অনেক বড়। ছোট একটি চ্যাট শেয়ার করুন।",
        shareLimit: "আপনি একসাথে সর্বোচ্চ ৫০টি চ্যাট শেয়ার করতে পারবেন। আগের কোনো শেয়ার বন্ধ করে আবার চেষ্টা করুন।",
        shareNotReady: "শেয়ার করার সুবিধা এখনও চালু হয়নি। পরে আবার চেষ্টা করুন।",
        shareCopied: "লিংক কপি করা হয়েছে।",
        shareCopyFailed: "লিংক কপি করা যায়নি। লিংকটি নিজে সিলেক্ট করে কপি করুন।",
        shareStopped: "শেয়ার বন্ধ করা হয়েছে। এই লিংক দিয়ে আর চ্যাটটি দেখা যাবে না।",
        shareLoadFailed: "শেয়ার করা চ্যাটটি এখন দেখানো যাচ্ছে না। একটু পরে আবার চেষ্টা করুন।",

        // actions
        retry: "আবার চেষ্টা করুন",
        reconnect: "আবার সংযোগ করুন",
        login: "নতুন করে লগইন করুন",
        regenerate: "উত্তরটি আবার তৈরি করুন",
        viewPlans: "প্ল্যান দেখুন",
        goToLatest: "নিচে যান",
        newAnswerBelow: "নতুন উত্তর দেখুন"
    };

    function msg(key, vars) {
        var text = Object.prototype.hasOwnProperty.call(MESSAGES, key) ? MESSAGES[key] : MESSAGES.unknown;
        if (vars) {
            Object.keys(vars).forEach(function (name) {
                text = text.split("{" + name + "}").join(String(vars[name]));
            });
        }
        return text;
    }

    /* ---------------- number / time formatting ---------------- */

    function bnNumber(n) {
        try { return Number(n).toLocaleString("bn-BD"); } catch (e) { return String(n); }
    }

    // "বৃহস্পতিবার ৯:০০ AM" style. Returns "" for anything that is not a real date,
    // so a malformed server value can never print "Invalid Date".
    function formatResetTime(iso) {
        if (typeof iso !== "string" || !iso) return "";
        var when = new Date(iso);
        if (isNaN(when.getTime())) return "";
        try {
            return new Intl.DateTimeFormat("bn-BD", { weekday: "long", hour: "numeric", minute: "2-digit" }).format(when);
        } catch (e) {
            return when.toLocaleString([], { weekday: "long", hour: "numeric", minute: "2-digit" });
        }
    }

    /* ---------------- classification ---------------- */

    var QUOTA_KIND_KEY = { chat: "quotaChat", quiz: "quotaQuiz", notes_ai: "quotaNotes" };

    // Actions a failure can offer. The caller decides how to render them; an
    // action is only ever returned when it can actually be performed.
    var ACTION_RETRY = "retry";
    var ACTION_LOGIN = "login";
    var ACTION_PLANS = "plans";

    function parseRetryAfter(value) {
        var n = parseInt(value, 10);
        return isFinite(n) && n > 0 && n <= 3600 ? n : null;
    }

    /*
      input: {
        status:     HTTP status, or null/undefined when no response arrived
        structured: the parsed {code,message,...} body (detail object) or null
        retryAfter: Retry-After header value (string/number) or null
        timedOut:   true when the request was aborted by a timeout
        offline:    true when the browser reports no connection
      }
      returns { code, text, action, actionLabel }
    */
    function classifyFailure(input) {
        var i = input || {};
        var s = typeof i.status === "number" ? i.status : null;
        var st = i.structured && typeof i.structured === "object" ? i.structured : null;

        function out(code, key, action, label, vars) {
            return { code: code, text: msg(key, vars), action: action || null, actionLabel: label || null };
        }

        if (st && st.code === "QUOTA_EXCEEDED") {
            // reason "rate_limited" is the server's short-term ledger cap, NOT the
            // plan allowance: it clears on its own, so no plan button is offered.
            if (st.reason === "rate_limited") return out("RATE_LIMITED", "rateLimited", ACTION_RETRY, msg("retry"));
            var kindKey = QUOTA_KIND_KEY[st.kind] || "quotaGeneric";
            var text = msg(kindKey);
            var plan = typeof st.plan === "string" ? st.plan.toLowerCase() : "";
            if (plan === "free") text = msg("quotaFree") + text;
            var when = formatResetTime(st.resets_at);
            if (when) text += msg("quotaResets", { when: when });
            return { code: "QUOTA_EXHAUSTED", text: text, action: ACTION_PLANS, actionLabel: msg("viewPlans") };
        }
        if (st && st.code === "QUOTA_UNAVAILABLE") return out("QUOTA_UNAVAILABLE", "quotaUnavailable", ACTION_RETRY, msg("retry"));
        if (st && st.code === "PLAN_NOT_CONFIGURED") return out("PLAN_NOT_CONFIGURED", "planNotConfigured");
        if (st && st.code === "PDF_CONTEXT_EXPIRED") return out("PDF_EXPIRED", "pdfExpired");

        if (s === 401) return out("SESSION_EXPIRED", "sessionExpired", ACTION_LOGIN, msg("login"));
        if (s === 429) {
            var wait = parseRetryAfter(i.retryAfter);
            return wait
                ? out("RATE_LIMITED", "rateLimitedWait", ACTION_RETRY, msg("retry"), { n: bnNumber(wait) })
                : out("RATE_LIMITED", "rateLimited", ACTION_RETRY, msg("retry"));
        }
        if (s === 413) return out("TOO_LARGE", "tooLarge");
        if (s === 400 || s === 422) return out("BAD_REQUEST", "badRequest");
        if (s === 408 || s === 504 || i.timedOut) return out("TIMEOUT", "timeout", ACTION_RETRY, msg("retry"));
        if (s !== null && s >= 500) return out("SERVER_BUSY", "serverBusy", ACTION_RETRY, msg("retry"));
        if (s === null) {
            return out("NETWORK", "network", ACTION_RETRY, msg("reconnect"));
        }
        return out("UNKNOWN", "unknown", ACTION_RETRY, msg("retry"));
    }

    /* ---------------- plan screen ---------------- */

    // The only real "plan" destination in the product today: the Update Plan
    // screen inside the account dialog. Returns true when it was opened.
    function openPlans() {
        try {
            if (window.KlorizProfileMenu && typeof window.KlorizProfileMenu.openAccount === "function") {
                window.KlorizProfileMenu.openAccount("plan");
                return true;
            }
        } catch (e) { /* fall through */ }
        return false;
    }

    /* ---------------- toast ---------------- */

    var toastTimer = null;
    var region = null;

    function ensureRegion() {
        if (region && document.body.contains(region)) return region;
        region = document.getElementById("kloriz-toast-region");
        if (!region) {
            region = document.createElement("div");
            region.id = "kloriz-toast-region";
            region.className = "kloriz-toast-region";
            document.body.appendChild(region);
        }
        return region;
    }

    function dismissToast() {
        if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
        if (region) region.textContent = "";
    }

    /*
      toast(text, { kind: "info"|"success"|"error", actionLabel, onAction, duration })
      - one toast at a time (a newer one replaces the older)
      - errors use role="alert" (announced at once), others role="status"
      - an error with an action stays until dismissed or acted on; everything
        else clears itself. The student can always dismiss with the close button.
      Returns the toast element.
    */
    function toast(text, opts) {
        var o = opts || {};
        var kind = o.kind === "error" || o.kind === "success" ? o.kind : "info";
        var host = ensureRegion();
        dismissToast();

        var el = document.createElement("div");
        el.className = "kloriz-toast kloriz-toast-" + kind;
        el.setAttribute("role", kind === "error" ? "alert" : "status");

        var span = document.createElement("span");
        span.className = "kloriz-toast-text";
        span.textContent = String(text == null ? "" : text);
        el.appendChild(span);

        if (o.actionLabel && typeof o.onAction === "function") {
            var act = document.createElement("button");
            act.type = "button";
            act.className = "kloriz-toast-action";
            act.textContent = o.actionLabel;
            act.addEventListener("click", function () { dismissToast(); o.onAction(); });
            el.appendChild(act);
        }

        var close = document.createElement("button");
        close.type = "button";
        close.className = "kloriz-toast-close";
        close.setAttribute("aria-label", "বন্ধ করুন");
        close.textContent = "\u00D7";
        close.addEventListener("click", dismissToast);
        el.appendChild(close);

        host.appendChild(el);

        var sticky = kind === "error" && !!(o.actionLabel && typeof o.onAction === "function");
        var duration = typeof o.duration === "number" ? o.duration : (kind === "error" ? 8000 : 4000);
        if (!sticky && duration > 0) toastTimer = setTimeout(dismissToast, duration);
        return el;
    }

    /* ---------------- convenience: failure -> toast ---------------- */

    // actions: { retry: fn, login: fn }  (plans is handled here)
    function toastFailure(info, actions) {
        var a = actions || {};
        var handler = null;
        if (info.action === ACTION_PLANS) handler = openPlans;
        else if (info.action === ACTION_LOGIN) handler = a.login || null;
        else if (info.action === ACTION_RETRY) handler = a.retry || null;
        return toast(info.text, {
            kind: "error",
            actionLabel: handler ? info.actionLabel : null,
            onAction: handler
        });
    }

    window.KlorizFeedback = {
        msg: msg,
        messages: MESSAGES,
        classifyFailure: classifyFailure,
        formatResetTime: formatResetTime,
        parseRetryAfter: parseRetryAfter,
        openPlans: openPlans,
        toast: toast,
        toastFailure: toastFailure,
        dismissToast: dismissToast,
        ACTION_RETRY: ACTION_RETRY,
        ACTION_LOGIN: ACTION_LOGIN,
        ACTION_PLANS: ACTION_PLANS
    };
})();