/* ============================================================
   KLORIZ I18N — account menu language support (English / বাংলা)
   ============================================================
   A deliberately small localisation layer. Kloriz had no i18n before
   this file, so this is NOT a framework: it is one dictionary, one
   piece of state (the chosen language) and one helper that fills in
   elements marked with data-i18n attributes.

   SCOPE (what honours the chosen language today):
     - the account menu opened from the sidebar profile row
     - the Settings, Get Help, Update Plan and Learn More screens
       (static/js/profile-menu.js builds them from this dictionary)
     - the logout confirmation dialog

   NOT translated (on purpose, not by accident):
     - the rest of the app (chat, quizzes, notes, auth and the profile
       editor) - those strings live in app.js / other templates and
       moving them is a separate, larger change
     - anything the student typed (name, email) and every AI answer.
       Kloriz does not translate answers; the AI replies in the
       language it is asked in.

   MARKUP CONTRACT (templates/index.html):
     data-i18n="key"              -> element.textContent
     data-i18n-aria-label="key"   -> aria-label
     data-i18n-title="key"        -> title
   Elements that hold icons keep the icon and mark only the label
   <span>, so applying a language never destroys markup.

   PERSISTENCE: localStorage key "kloriz-language", exactly like the
   theme (static/js/theme.js): a per-device display preference, no
   backend call, no account field. Every storage call is wrapped, so
   private browsing / blocked storage degrades to "English, not
   remembered" instead of throwing.

   Adding a string: add the SAME key to both "en" and "bn". A key
   missing from "bn" falls back to English rather than showing a key.
   ============================================================ */

(function () {
    "use strict";

    var STORAGE_KEY = "kloriz-language";
    var SUPPORTED = ["en", "bn"];
    var DEFAULT_LANGUAGE = "en";

    /* Language names are always shown in their own script and are never
       translated, so a student who cannot read the current language can
       still find theirs. */
    var LANGUAGE_NAMES = {
        en: { native: "English", secondary: "" },
        bn: { native: "বাংলা", secondary: "Bangla" }
    };

    var DICTIONARY = {
        en: {
            "common.close": "Close",
            "common.back": "Back",

            /* ---- account menu ---- */
            "pm.trigger": "Open account menu",
            "pm.menuLabel": "Account menu",
            "pm.viewLearningProfile": "View your learning profile",
            "pm.editProfile": "Edit profile",
            "pm.settings": "Settings",
            "pm.language": "Language",
            "pm.help": "Get help",
            "pm.plan": "Update plan",
            "pm.learnMore": "Learn more",
            "pm.logout": "Logout",
            "pm.languageBack": "Back to account menu",

            /* ---- logout confirmation ---- */
            "logout.title": "Log out of Kloriz?",
            "logout.desc": "You can sign back in anytime. Your chats and learning progress stay saved to your account.",
            "logout.cancel": "Cancel",
            "logout.confirm": "Log out",

            /* ---- settings ---- */
            "settings.title": "Settings",
            "settings.intro": "Choose how Kloriz looks and which language your account menu uses.",
            "settings.appearance": "Appearance",
            "settings.theme": "Theme",
            "settings.themeLight": "Light",
            "settings.themeDark": "Dark",
            "settings.language": "Language",
            "settings.languageHint": "Applies to the account menu and the screens opened from it. Answers from Kloriz are not translated.",
            "settings.deviceNote": "Theme and language are saved on this device only.",

            /* ---- help ---- */
            "help.title": "Get help",
            "help.intro": "Quick answers about using Kloriz.",
            "help.t1.q": "Asking a question",
            "help.t1.a": "Type your question in the box at the bottom and send it. Choose Direct for a full answer, or Socratic to be guided step by step instead of being handed the answer.",
            "help.t2.q": "Using a PDF or a photo",
            "help.t2.a": "Tap the + button next to the message box, then choose Upload File for a PDF or Upload Image for a photo of a question or diagram. PDFs up to 15 MB and images up to 8 MB are accepted. Scanned PDFs without selectable text cannot be read.",
            "help.t3.q": "Practice quizzes",
            "help.t3.a": "Open the Quizzes tab to generate a practice test on a topic. When you submit, you see your score with an explanation for each question. Your quiz results help build your learning profile.",
            "help.t4.q": "Pocket Notes",
            "help.t4.a": "Save a helpful answer as a note, then find it in the Pocket Notes tab. Saved notes can be read offline once they have loaded on this device.",
            "help.t5.q": "Your profile and class",
            "help.t5.a": "Use Edit profile in the account menu to change your name, class or stream. Kloriz uses your class and stream to match answers to your syllabus. Tap your name at the top of the account menu to see your learning profile.",
            "help.t6.q": "Something isn't working",
            "help.t6.a": "If an answer stops or fails, send your question again after a moment. If a PDF or image is rejected, check its size and type. If the problem continues, log out and log back in.",
            "help.t7.q": "Plans and payments",
            "help.t7.a": "Kloriz is currently on the Free plan. Paid plans are not available yet, so no payment is needed or accepted.",

            /* ---- plan ---- */
            "plan.title": "Choose your plan",
            "plan.intro": "You're on the Free plan. Paid plans aren't available yet.",
            "plan.free": "Free",
            "plan.current": "Current plan",
            "plan.free.f1": "Direct answers and step-by-step Socratic help",
            "plan.free.f2": "Questions about your own PDFs and photos",
            "plan.free.f3": "AI-generated practice quizzes with explanations",
            "plan.free.f4": "Pocket Notes",
            "plan.free.f5": "A learning profile with strengths and topics to practise",
            "plan.pro": "Pro",
            "plan.notAvailable": "Not available yet",
            "plan.proBody": "Pro isn't available yet. Plan details and pricing haven't been announced.",
            "plan.proButton": "Pro isn't available yet",

            /* ---- learn more ---- */
            "learn.title": "Learn more",
            "learn.kloriz.title": "About Kloriz",
            "learn.kloriz.desc": "What Kloriz is and what it can do",
            "learn.altruvis.title": "About Altruvis",
            "learn.altruvis.desc": "The company behind Kloriz",
            "learn.usage.title": "Usage Policy",
            "learn.usage.desc": "Rules for using Kloriz",
            "learn.privacy.title": "Privacy Policy",
            "learn.privacy.desc": "How your information is handled",
            "learn.external": "Opens in a new tab",

            "about.kloriz.p1": "Kloriz is an AI academic assistant for students in Bangladesh following the NCTB curriculum, from Class 6 to Class 12. It is meant to help you understand your lessons, not just hand you answers.",
            "about.kloriz.p2": "You can ask questions in Direct or Socratic mode, ask about your own PDFs and photos, practise with AI-generated quizzes, keep Pocket Notes, and follow your progress in your learning profile.",
            "about.kloriz.p3": "Kloriz can make mistakes. Check important information against your textbook or your teacher.",
            "about.altruvis.p1": "Altruvis is the company behind Kloriz.",
            "policy.usage.body": "The Usage Policy hasn't been published yet.",
            "policy.privacy.body": "The Privacy Policy hasn't been published yet.",
            "policy.later": "It will be available here once it is ready."
        },

        bn: {
            "common.close": "বন্ধ করুন",
            "common.back": "পেছনে",

            /* ---- account menu ---- */
            "pm.trigger": "অ্যাকাউন্ট মেনু খুলুন",
            "pm.menuLabel": "অ্যাকাউন্ট মেনু",
            "pm.viewLearningProfile": "আপনার লার্নিং প্রোফাইল দেখুন",
            "pm.editProfile": "প্রোফাইল সম্পাদনা",
            "pm.settings": "সেটিংস",
            "pm.language": "ভাষা",
            "pm.help": "সহায়তা নিন",
            "pm.plan": "প্ল্যান আপডেট",
            "pm.learnMore": "আরও জানুন",
            "pm.logout": "লগ আউট",
            "pm.languageBack": "অ্যাকাউন্ট মেনুতে ফিরুন",

            /* ---- logout confirmation ---- */
            "logout.title": "Kloriz থেকে লগ আউট করবেন?",
            "logout.desc": "আপনি যেকোনো সময় আবার সাইন ইন করতে পারবেন। আপনার চ্যাট ও লার্নিং অগ্রগতি অ্যাকাউন্টে সংরক্ষিত থাকবে।",
            "logout.cancel": "বাতিল",
            "logout.confirm": "লগ আউট",

            /* ---- settings ---- */
            "settings.title": "সেটিংস",
            "settings.intro": "Kloriz কেমন দেখাবে এবং অ্যাকাউন্ট মেনুতে কোন ভাষা ব্যবহার হবে তা বেছে নিন।",
            "settings.appearance": "চেহারা",
            "settings.theme": "থিম",
            "settings.themeLight": "লাইট",
            "settings.themeDark": "ডার্ক",
            "settings.language": "ভাষা",
            "settings.languageHint": "এটি অ্যাকাউন্ট মেনু ও তা থেকে খোলা স্ক্রিনগুলোতে প্রযোজ্য। Kloriz-এর উত্তর স্বয়ংক্রিয়ভাবে অনুবাদ হয় না।",
            "settings.deviceNote": "থিম ও ভাষা শুধু এই ডিভাইসে সংরক্ষিত থাকে।",

            /* ---- help ---- */
            "help.title": "সহায়তা নিন",
            "help.intro": "Kloriz ব্যবহার সম্পর্কে দ্রুত উত্তর।",
            "help.t1.q": "প্রশ্ন করা",
            "help.t1.a": "নিচের বক্সে আপনার প্রশ্ন লিখে পাঠান। সম্পূর্ণ উত্তরের জন্য Direct, আর সরাসরি উত্তর না দিয়ে ধাপে ধাপে বুঝতে সাহায্য পেতে Socratic বেছে নিন।",
            "help.t2.q": "PDF বা ছবি ব্যবহার করা",
            "help.t2.a": "মেসেজ বক্সের পাশের + বোতামে চাপ দিন, তারপর PDF-এর জন্য Upload File অথবা প্রশ্ন বা ডায়াগ্রামের ছবির জন্য Upload Image বেছে নিন। 15 MB পর্যন্ত PDF এবং 8 MB পর্যন্ত ছবি গ্রহণ করা হয়। স্ক্যান করা যে PDF-এ লেখা নির্বাচন করা যায় না, সেগুলো পড়া সম্ভব নয়।",
            "help.t3.q": "অনুশীলনের কুইজ",
            "help.t3.a": "কোনো টপিকের উপর অনুশীলন পরীক্ষা তৈরি করতে Quizzes ট্যাব খুলুন। জমা দেওয়ার পর স্কোর ও প্রতিটি প্রশ্নের ব্যাখ্যা দেখতে পাবেন। কুইজের ফলাফল আপনার লার্নিং প্রোফাইল গড়তে সাহায্য করে।",
            "help.t4.q": "Pocket Notes",
            "help.t4.a": "কোনো কাজের উত্তর নোট হিসেবে সংরক্ষণ করুন, তারপর Pocket Notes ট্যাবে পাবেন। এই ডিভাইসে লোড হওয়া সংরক্ষিত নোট অফলাইনেও পড়া যাবে।",
            "help.t5.q": "আপনার প্রোফাইল ও শ্রেণি",
            "help.t5.a": "নাম, শ্রেণি বা স্ট্রিম বদলাতে অ্যাকাউন্ট মেনুর প্রোফাইল সম্পাদনা ব্যবহার করুন। আপনার সিলেবাসের সঙ্গে উত্তর মিলিয়ে দিতে Kloriz আপনার শ্রেণি ও স্ট্রিম ব্যবহার করে। লার্নিং প্রোফাইল দেখতে অ্যাকাউন্ট মেনুর উপরে আপনার নামে চাপ দিন।",
            "help.t6.q": "কিছু কাজ করছে না",
            "help.t6.a": "কোনো উত্তর থেমে গেলে বা ব্যর্থ হলে কিছুক্ষণ পর আবার প্রশ্নটি পাঠান। PDF বা ছবি গ্রহণ না হলে এর সাইজ ও ধরন যাচাই করুন। সমস্যা থাকলে লগ আউট করে আবার লগ ইন করুন।",
            "help.t7.q": "প্ল্যান ও পেমেন্ট",
            "help.t7.a": "Kloriz এখন ফ্রি প্ল্যানে চলছে। পেইড প্ল্যান এখনও চালু হয়নি, তাই কোনো পেমেন্ট লাগে না এবং গ্রহণও করা হয় না।",

            /* ---- plan ---- */
            "plan.title": "আপনার প্ল্যান বেছে নিন",
            "plan.intro": "আপনি এখন ফ্রি প্ল্যানে আছেন। পেইড প্ল্যান এখনও চালু হয়নি।",
            "plan.free": "ফ্রি",
            "plan.current": "বর্তমান প্ল্যান",
            "plan.free.f1": "সরাসরি উত্তর ও ধাপে ধাপে Socratic সাহায্য",
            "plan.free.f2": "নিজের PDF ও ছবি নিয়ে প্রশ্ন",
            "plan.free.f3": "ব্যাখ্যাসহ AI-তৈরি অনুশীলন কুইজ",
            "plan.free.f4": "Pocket Notes",
            "plan.free.f5": "শক্তি ও অনুশীলনের টপিকসহ লার্নিং প্রোফাইল",
            "plan.pro": "Pro",
            "plan.notAvailable": "এখনও পাওয়া যাচ্ছে না",
            "plan.proBody": "Pro এখনও চালু হয়নি। প্ল্যানের বিস্তারিত ও মূল্য এখনও ঘোষণা করা হয়নি।",
            "plan.proButton": "Pro এখনও চালু হয়নি",

            /* ---- learn more ---- */
            "learn.title": "আরও জানুন",
            "learn.kloriz.title": "Kloriz সম্পর্কে",
            "learn.kloriz.desc": "Kloriz কী এবং কী করতে পারে",
            "learn.altruvis.title": "Altruvis সম্পর্কে",
            "learn.altruvis.desc": "Kloriz-এর পেছনের প্রতিষ্ঠান",
            "learn.usage.title": "ব্যবহারনীতি",
            "learn.usage.desc": "Kloriz ব্যবহারের নিয়ম",
            "learn.privacy.title": "গোপনীয়তা নীতি",
            "learn.privacy.desc": "আপনার তথ্য কীভাবে ব্যবহৃত হয়",
            "learn.external": "নতুন ট্যাবে খুলবে",

            "about.kloriz.p1": "Kloriz বাংলাদেশের NCTB পাঠ্যক্রম অনুসরণকারী ষষ্ঠ থেকে দ্বাদশ শ্রেণির শিক্ষার্থীদের জন্য একটি AI একাডেমিক সহকারী। এটি শুধু উত্তর দেওয়ার জন্য নয়, আপনার পাঠ বুঝতে সাহায্য করার জন্য।",
            "about.kloriz.p2": "আপনি Direct বা Socratic মোডে প্রশ্ন করতে পারেন, নিজের PDF ও ছবি নিয়ে জানতে চাইতে পারেন, AI-তৈরি কুইজে অনুশীলন করতে পারেন, Pocket Notes রাখতে পারেন এবং লার্নিং প্রোফাইলে নিজের অগ্রগতি দেখতে পারেন।",
            "about.kloriz.p3": "Kloriz ভুল করতে পারে। গুরুত্বপূর্ণ তথ্য পাঠ্যবই বা শিক্ষকের সঙ্গে মিলিয়ে নিন।",
            "about.altruvis.p1": "Altruvis হলো Kloriz-এর পেছনের প্রতিষ্ঠান।",
            "policy.usage.body": "ব্যবহারনীতি এখনও প্রকাশ করা হয়নি।",
            "policy.privacy.body": "গোপনীয়তা নীতি এখনও প্রকাশ করা হয়নি।",
            "policy.later": "প্রস্তুত হলে এখানে পাওয়া যাবে।"
        }
    };

    var current = DEFAULT_LANGUAGE;

    function isSupported(lang) {
        return SUPPORTED.indexOf(lang) !== -1;
    }

    function readStored() {
        try {
            var saved = window.localStorage.getItem(STORAGE_KEY);
            return isSupported(saved) ? saved : null;
        } catch (e) {
            return null;
        }
    }

    function writeStored(lang) {
        try {
            window.localStorage.setItem(STORAGE_KEY, lang);
        } catch (e) {
            // Storage unavailable: the choice still applies for this page
            // view, it just is not remembered on reload. Not fatal.
        }
    }

    /* t(key): the current language's string, then English, then the key
       itself (so a missing translation is visible in development rather
       than rendering as an empty element). */
    function t(key) {
        var table = DICTIONARY[current] || DICTIONARY[DEFAULT_LANGUAGE];
        if (Object.prototype.hasOwnProperty.call(table, key)) return table[key];
        var fallback = DICTIONARY[DEFAULT_LANGUAGE];
        if (Object.prototype.hasOwnProperty.call(fallback, key)) return fallback[key];
        return key;
    }

    /* Fills every marked element under `root` (default: the whole
       document). Idempotent; safe to call repeatedly. */
    function applyTranslations(root) {
        var scope = root || document;
        if (!scope || !scope.querySelectorAll) return;

        var nodes = scope.querySelectorAll("[data-i18n]");
        Array.prototype.forEach.call(nodes, function (el) {
            el.textContent = t(el.getAttribute("data-i18n"));
        });
        nodes = scope.querySelectorAll("[data-i18n-aria-label]");
        Array.prototype.forEach.call(nodes, function (el) {
            el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria-label")));
        });
        nodes = scope.querySelectorAll("[data-i18n-title]");
        Array.prototype.forEach.call(nodes, function (el) {
            el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
        });
    }

    function announce(lang) {
        var event;
        try {
            event = new window.CustomEvent("kloriz:languagechange", { detail: { language: lang } });
        } catch (e) {
            event = document.createEvent("CustomEvent");
            event.initCustomEvent("kloriz:languagechange", false, false, { language: lang });
        }
        document.dispatchEvent(event);
    }

    function activate(lang, persist) {
        if (!isSupported(lang)) return false;
        var changed = lang !== current;
        current = lang;
        if (persist) writeStored(lang);
        // <html lang> tells screen readers and the browser's font/
        // hyphenation logic which language the page now speaks.
        document.documentElement.setAttribute("lang", lang);
        applyTranslations(document);
        if (changed) announce(lang);
        return true;
    }

    function setLanguage(lang) {
        return activate(lang, true);
    }

    function getLanguage() {
        return current;
    }

    function init() {
        var stored = readStored();
        // Always apply once (even for the default) so <html lang> and every
        // data-i18n element reflect the real state from the first paint
        // after DOM ready.
        activate(stored || DEFAULT_LANGUAGE, false);

        // Keep two open tabs in step, like the theme does.
        window.addEventListener("storage", function (event) {
            if (event.key !== STORAGE_KEY) return;
            if (isSupported(event.newValue)) activate(event.newValue, false);
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }

    window.KlorizI18n = {
        t: t,
        get: getLanguage,
        set: setLanguage,
        apply: applyTranslations,
        names: LANGUAGE_NAMES,
        supported: SUPPORTED.slice(),
        STORAGE_KEY: STORAGE_KEY
    };
})();