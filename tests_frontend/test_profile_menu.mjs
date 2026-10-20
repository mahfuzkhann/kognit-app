/* ============================================================
   KLORIZ ACCOUNT MENU TESTS
   ============================================================
   Loads the REAL template and the REAL static/js/{theme,ui-core,i18n,
   profile-menu}.js in jsdom. app.js itself is stubbed (it needs a live
   Supabase session to boot) with the exact functions the menu calls:
   openProfileModal, openStudentProfilePanel, handleLogout and the
   getCurrentProfile accessor.

   Covers: menu structure/order, open/close/keyboard behavior, identity
   header, every action's wiring, the language submenu + persistence,
   Settings / Get Help / Update Plan / Learn More screens, honesty
   guarantees (no invented prices/support/policies), the mobile drawer
   interaction, and dictionary completeness.

   Run: node tests_frontend/test_profile_menu.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (a !== b) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

const TEMPLATE = read("templates/index.html");

async function boot({
    profile = { name: "Mahfuz Rahman Khan", user_class: "Class 10", stream: "Science (বিজ্ঞান)" },
    email = "mahfuz@example.com",
    storedLanguage = null,
    brokenStorage = false,
    loggedIn = true,
} = {}) {
    let html = TEMPLATE.replace(/<script src="https:\/\/[^"]*"[^>]*><\/script>/g, "");
    html = html.replace(/<script src="\/static\/js\/[^"]*"><\/script>/g, "");

    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kloriz.test/" });
    const { window } = dom;
    const doc = window.document;

    if (storedLanguage !== null) window.localStorage.setItem("kloriz-language", storedLanguage);
    if (brokenStorage) {
        const boom = () => { throw new window.DOMException("blocked", "SecurityError"); };
        Object.defineProperty(window, "localStorage", { get: boom, configurable: true });
    }
    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

    const calls = { openProfileModal: 0, openStudentProfilePanel: 0, handleLogout: 0 };
    window.openProfileModal = () => { calls.openProfileModal++; };
    window.closeProfileModal = () => {};
    window.openStudentProfilePanel = () => { calls.openStudentProfilePanel++; };
    window.handleLogout = () => { calls.handleLogout++; };
    window.getCurrentProfile = () => profile;
    window.supabaseClient = { auth: { getSession: async () => ({ data: { session: email ? { user: { email } } : null } }) } };

    if (doc.readyState === "loading") {
        await new Promise((resolve) => doc.addEventListener("DOMContentLoaded", resolve, { once: true }));
    }
    for (const f of ["theme.js", "ui-core.js", "i18n.js", "profile-menu.js"]) {
        const script = doc.createElement("script");
        script.textContent = read(`static/js/${f}`);
        doc.body.appendChild(script);
    }
    if (loggedIn) {
        doc.getElementById("auth-guest-view").classList.add("hidden");
        doc.getElementById("auth-logged-view").classList.remove("hidden");
        doc.getElementById("user-display-name").textContent = profile ? profile.name : "";
    }
    await tick(5);

    const $ = (id) => doc.getElementById(id);
    const click = (node, init = {}) => node.dispatchEvent(new window.MouseEvent("click", { bubbles: true, detail: 1, ...init }));
    const key = (node, k, init = {}) => node.dispatchEvent(new window.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true, ...init }));
    const openMenu = async () => { click($("profile-trigger-btn")); await tick(5); };
    const action = (name) => doc.querySelector(`[data-pm-action="${name}"]`);
    const screenTitle = () => $("account-modal-title").textContent;
    const bodyText = () => $("account-body").textContent;
    return { dom, window, doc, $, calls, click, key, openMenu, action, screenTitle, bodyText };
}

/* =============================================================
   A. Template contract
   ============================================================= */
console.log("\nA. Template contract");

await check("the menu has exactly the seven requested options, in order, with dividers in the right places", async () => {
    const { doc } = await boot();
    const order = [...doc.querySelectorAll("#pm-main > [data-pm-action], #pm-main > .pm-divider")]
        .map((n) => n.classList.contains("pm-divider") ? "|" : n.getAttribute("data-pm-action"));
    // Header (learning-profile) is the first child of the pane, then the divider.
    eq(order.join(","), "learning-profile,|,edit-profile,settings,language,help,|,plan,learn-more,|,logout");
    const labels = [...doc.querySelectorAll("#pm-main .pm-item .pm-item-label")].map((n) => n.textContent);
    eq(labels.join("|"), "Edit profile|Settings|Language|Get help|Update plan|Learn more|Logout");
});

await check("every row has a consistent SVG icon (no emoji), and only navigation rows have a chevron", async () => {
    const { doc } = await boot();
    for (const row of doc.querySelectorAll("#pm-main .pm-item")) {
        assert(row.querySelector(".pm-item-icon svg"), `row ${row.dataset.pmAction} has no svg icon`);
        const hasChevron = !!row.querySelector(".pm-item-chevron");
        eq(hasChevron, row.dataset.pmAction !== "logout", `chevron on ${row.dataset.pmAction}`);
    }
    assert(!/[\u{1F300}-\u{1FAFF}\u2600-\u27BF]/u.test(doc.getElementById("profile-menu").textContent), "no emoji in the menu");
});

await check("the trigger is a menu button with correct ARIA and no inline learning-profile handler", async () => {
    const { $ } = await boot();
    const t = $("profile-trigger-btn");
    eq(t.getAttribute("aria-haspopup"), "menu");
    eq(t.getAttribute("aria-expanded"), "false");
    eq(t.getAttribute("aria-controls"), "profile-menu");
    assert(!t.hasAttribute("onclick"), "opening the learning panel must not be wired inline any more");
});

await check("the account modal is registered with the shared modal layer and the logout dialog is localisable", () => {
    assert(/"account-modal":\s*"closeAccountModal"/.test(read("static/js/ui-core.js")));
    for (const k of ["logout.title", "logout.desc", "logout.cancel", "logout.confirm"]) {
        assert(TEMPLATE.includes(`data-i18n="${k}"`), `logout dialog missing ${k}`);
    }
    // ids/handlers of the existing confirmation are untouched
    assert(TEMPLATE.includes('onclick="confirmLogout()"') && TEMPLATE.includes('onclick="closeLogoutConfirm()"'));
});

await check("new assets are in the service worker precache and the template links them", () => {
    const sw = read("static/sw.js");
    for (const u of ["/static/css/profile-menu.css", "/static/js/i18n.js", "/static/js/profile-menu.js"]) {
        assert(sw.includes(`"${u}"`), `sw.js missing ${u}`);
        assert(TEMPLATE.includes(`"${u}"`), `template missing ${u}`);
        assert(fs.existsSync(path.join(ROOT, u.slice(1))), `${u} missing on disk`);
    }
    assert(TEMPLATE.indexOf("/static/js/i18n.js") < TEMPLATE.indexOf("/static/js/profile-menu.js"), "i18n.js must load first");
});

/* =============================================================
   B. Dropdown behavior
   ============================================================= */
console.log("\nB. Dropdown behavior");

await check("click opens the menu and sets aria-expanded; a second click closes it", async () => {
    const { $, click, openMenu } = await boot();
    await openMenu();
    assert(!$("profile-menu").classList.contains("hidden"));
    eq($("profile-trigger-btn").getAttribute("aria-expanded"), "true");
    click($("profile-trigger-btn"));
    assert($("profile-menu").classList.contains("hidden"));
    eq($("profile-trigger-btn").getAttribute("aria-expanded"), "false");
});

await check("outside click closes the menu; a click inside it does not", async () => {
    const { window, doc, $, openMenu } = await boot();
    await openMenu();
    $("pm-name").dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true }));
    assert(!$("profile-menu").classList.contains("hidden"), "inside click must not close");
    doc.getElementById("chat-box").dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true }));
    assert($("profile-menu").classList.contains("hidden"), "outside click must close");
});

await check("Escape closes the menu and returns focus to the trigger", async () => {
    const { doc, $, key, openMenu, action } = await boot();
    await openMenu();
    action("settings").focus();
    key(action("settings"), "Escape");
    assert($("profile-menu").classList.contains("hidden"));
    eq(doc.activeElement, $("profile-trigger-btn"));
});

await check("keyboard activation (Enter/Space) moves focus to the first item; ArrowUp/Down open from the trigger", async () => {
    const { doc, $, click, key, action } = await boot();
    $("profile-trigger-btn").focus();
    click($("profile-trigger-btn"), { detail: 0 });
    await tick(5);
    eq(doc.activeElement, doc.querySelector('[data-pm-action="learning-profile"]'));
    key(doc.activeElement, "Escape");
    key($("profile-trigger-btn"), "ArrowUp");
    eq(doc.activeElement, action("logout"), "ArrowUp opens on the last item");
});

await check("arrow keys wrap through the items; Home/End jump", async () => {
    const { doc, key, openMenu, action } = await boot();
    await openMenu();
    action("edit-profile").focus();
    key(doc.activeElement, "ArrowDown");
    eq(doc.activeElement, action("settings"));
    key(doc.activeElement, "End");
    eq(doc.activeElement, action("logout"));
    key(doc.activeElement, "ArrowDown");
    eq(doc.activeElement, action("learning-profile"), "wraps to the first item");
    key(doc.activeElement, "ArrowUp");
    eq(doc.activeElement, action("logout"), "wraps to the last item");
    key(doc.activeElement, "Home");
    eq(doc.activeElement, action("learning-profile"));
});

await check("Tab closes the menu; logging out (row hidden) closes it too", async () => {
    const { doc, $, key, openMenu } = await boot();
    await openMenu();
    // (a real browser focuses the clicked button; dispatch on it explicitly)
    key($("profile-trigger-btn"), "Tab");
    assert($("profile-menu").classList.contains("hidden"));
    await openMenu();
    $("auth-logged-view").classList.add("hidden");
    await tick(5);
    assert($("profile-menu").classList.contains("hidden"));
});

/* =============================================================
   C. Identity header
   ============================================================= */
console.log("\nC. Identity header");

await check("shows the real profile name and the session email", async () => {
    const { $, openMenu } = await boot();
    await openMenu();
    await tick(10);
    eq($("pm-name").textContent, "Mahfuz Rahman Khan");
    eq($("pm-email").textContent, "mahfuz@example.com");
});

await check("without a profile it falls back to the sidebar's own fallback and never repeats the email", async () => {
    const { doc, $, openMenu } = await boot({ profile: null });
    doc.getElementById("user-display-name").textContent = "mahfuz@example.com";
    await openMenu();
    await tick(10);
    eq($("pm-name").textContent, "mahfuz@example.com");
    eq($("pm-email").textContent, "", "email must not be shown twice");
});

await check("hostile names are rendered as text, never as markup", async () => {
    const { doc, $, openMenu } = await boot({ profile: { name: '<img src=x onerror="window.__pwned=1">', user_class: "Class 9" } });
    await openMenu();
    await tick(10);
    eq($("pm-name").querySelector("img"), null);
    assert($("pm-name").textContent.includes("<img"));
    eq(doc.defaultView.__pwned, undefined);
});

await check("logging out clears the cached name/email so the next user cannot see them", async () => {
    const { $, openMenu } = await boot();
    await openMenu();
    await tick(10);
    $("auth-logged-view").classList.add("hidden");
    await tick(5);
    eq($("pm-name").textContent, "");
    eq($("pm-email").textContent, "");
});

/* =============================================================
   D. Actions reuse the existing functions
   ============================================================= */
console.log("\nD. Actions");

await check("Edit profile opens the EXISTING profile editor (openProfileModal) and closes the menu", async () => {
    const { $, calls, click, openMenu, action } = await boot();
    await openMenu();
    click(action("edit-profile"));
    eq(calls.openProfileModal, 1);
    assert($("profile-menu").classList.contains("hidden"));
});

await check("the header opens the existing Student Profile panel", async () => {
    const { calls, click, openMenu, action } = await boot();
    await openMenu();
    click(action("learning-profile"));
    eq(calls.openStudentProfilePanel, 1);
});

await check("Logout uses the existing confirmation + handleLogout; the menu never logs out by itself", async () => {
    const { $, calls, click, openMenu, action } = await boot();
    await openMenu();
    click(action("logout"));
    assert(!$("logout-confirm-modal").classList.contains("hidden"), "confirmation dialog must show");
    eq(calls.handleLogout, 0, "must not log out before confirmation");
    click($("logout-confirm-modal").querySelector(".btn-confirm-danger"));
    eq(calls.handleLogout, 1);
});

await check("opening the menu never changes profile data", async () => {
    const { calls, openMenu } = await boot();
    await openMenu();
    eq(calls.openProfileModal + calls.openStudentProfilePanel + calls.handleLogout, 0);
});

/* =============================================================
   E. Language
   ============================================================= */
console.log("\nE. Language");

await check("Language opens an inner submenu with English and বাংলা; the active one is marked", async () => {
    const { $, doc, click, openMenu, action } = await boot();
    await openMenu();
    eq($("pm-language-value").textContent, "English");
    click(action("language"));
    assert(!$("pm-language-pane").classList.contains("hidden"));
    assert($("pm-main").classList.contains("hidden"));
    const opts = [...doc.querySelectorAll("[data-pm-language]")];
    eq(opts.map((o) => o.querySelector(".pm-lang-name").textContent).join("|"), "English|বাংলা");
    eq(opts.map((o) => o.getAttribute("aria-checked")).join("|"), "true|false");
    assert(!$("profile-menu").classList.contains("hidden"), "menu stays open while choosing");
});

await check("choosing বাংলা applies, persists, sets <html lang>, translates the menu and returns to the main pane", async () => {
    const { window, doc, $, click, openMenu, action } = await boot();
    await openMenu();
    click(action("language"));
    click(doc.querySelector('[data-pm-language="bn"]'));
    eq(window.localStorage.getItem("kloriz-language"), "bn");
    eq(doc.documentElement.getAttribute("lang"), "bn");
    assert(!$("pm-main").classList.contains("hidden") && $("pm-language-pane").classList.contains("hidden"));
    eq($("pm-language-value").textContent, "বাংলা");
    eq(action("settings").querySelector(".pm-item-label").textContent, "সেটিংস");
    eq(action("logout").querySelector(".pm-item-label").textContent, "লগ আউট");
    eq($("logout-confirm-title").textContent, "Kloriz থেকে লগ আউট করবেন?");
    eq($("profile-trigger-btn").getAttribute("aria-label"), "অ্যাকাউন্ট মেনু খুলুন");
});

await check("a saved language is applied on boot; invalid or unreadable storage falls back to English", async () => {
    let b = await boot({ storedLanguage: "bn" });
    eq(b.doc.documentElement.getAttribute("lang"), "bn");
    eq(b.action("help").querySelector(".pm-item-label").textContent, "সহায়তা নিন");
    b = await boot({ storedLanguage: "fr" });
    eq(b.doc.documentElement.getAttribute("lang"), "en");
    b = await boot({ brokenStorage: true });
    eq(b.doc.documentElement.getAttribute("lang"), "en");
    // choosing a language with storage blocked must still work for this page view
    await b.openMenu();
    b.click(b.action("language"));
    b.click(b.doc.querySelector('[data-pm-language="bn"]'));
    eq(b.doc.documentElement.getAttribute("lang"), "bn");
});

await check("Escape / ArrowLeft in the submenu return to the main pane without closing the menu", async () => {
    const { doc, $, click, key, openMenu, action } = await boot();
    await openMenu();
    click(action("language"));
    key(doc.activeElement, "Escape");
    assert(!$("profile-menu").classList.contains("hidden"));
    assert(!$("pm-main").classList.contains("hidden"));
    eq(doc.activeElement, action("language"));
    key(action("language"), "ArrowRight");
    assert(!$("pm-language-pane").classList.contains("hidden"));
    key(doc.activeElement, "ArrowLeft");
    assert(!$("pm-main").classList.contains("hidden"));
});

await check("the Settings language control and the menu share ONE state", async () => {
    const { doc, $, click, openMenu, action } = await boot();
    await openMenu();
    click(action("settings"));
    await tick(5);
    const bn = doc.getElementById("acct-language-bn");
    bn.checked = true;
    bn.dispatchEvent(new doc.defaultView.Event("change", { bubbles: true }));
    await tick(5);
    eq(doc.documentElement.getAttribute("lang"), "bn");
    eq($("pm-language-value").textContent, "বাংলা");
    eq($("account-modal-title").textContent, "সেটিংস");
    assert(doc.getElementById("acct-language-bn").checked, "control reflects state after re-render");
});

await check("English and Bangla dictionaries have identical keys, no empty values, and every data-i18n key exists", async () => {
    const { window } = await boot();
    const src = read("static/js/i18n.js");
    const dict = src.indexOf("var DICTIONARY = {");   // skip LANGUAGE_NAMES, which also has en:/bn: keys
    const grab = (lang) => {
        const start = src.indexOf(`        ${lang}: {`, dict);
        const next = lang === "en" ? src.indexOf("        bn: {", dict) : src.indexOf("    var current");
        return [...src.slice(start, next).matchAll(/^\s+"([^"]+)":\s*"((?:[^"\\]|\\.)*)"/gm)].map((m) => [m[1], m[2]]);
    };
    const en = grab("en"), bn = grab("bn");
    eq(en.length, bn.length, "same number of keys");
    eq(en.map((e) => e[0]).sort().join(), bn.map((e) => e[0]).sort().join(), "same key set");
    for (const [k, v] of [...en, ...bn]) assert(v.trim().length > 0, `empty value for ${k}`);
    const keys = new Set(en.map((e) => e[0]));
    for (const m of TEMPLATE.matchAll(/data-i18n(?:-aria-label|-title)?="([^"]+)"/g)) {
        assert(keys.has(m[1]), `template uses unknown key ${m[1]}`);
    }
    // Bangla values really are Bangla (brand-only strings excepted)
    const brandOnly = new Set(["help.t4.q", "plan.free.f4", "plan.pro"]);
    for (const [k, v] of bn) if (!brandOnly.has(k)) assert(/[\u0980-\u09FF]/.test(v), `bn value for ${k} has no Bangla`);
    eq(window.KlorizI18n.t("no.such.key"), "no.such.key", "unknown key is visible, not blank");
});

/* =============================================================
   F. Settings
   ============================================================= */
console.log("\nF. Settings");

await check("Settings opens the dialog with only real controls (theme + language)", async () => {
    const { $, doc, click, openMenu, action, screenTitle } = await boot();
    await openMenu();
    click(action("settings"));
    assert(!$("account-modal").classList.contains("hidden"));
    eq(screenTitle(), "Settings");
    const groups = [...doc.querySelectorAll("#account-body .acct-group-title")].map((n) => n.textContent);
    eq(groups.join("|"), "Appearance|Language");
    eq(doc.querySelectorAll("#account-body input[type=checkbox]").length, 0, "no invented toggles");
    assert(doc.querySelector('input[name="acct-theme"]:checked').value === "dark");
});

await check("changing theme in Settings drives the real theme system, and stays in sync with other toggles", async () => {
    const { window, doc, click, openMenu, action } = await boot();
    await openMenu();
    click(action("settings"));
    const light = doc.getElementById("acct-theme-light");
    light.checked = true;
    light.dispatchEvent(new window.Event("change", { bubbles: true }));
    eq(doc.documentElement.getAttribute("data-theme"), "light");
    eq(window.localStorage.getItem("kognit-theme"), "light");
    // toggled elsewhere (the sidebar/top-bar button) -> radios follow
    window.KognitTheme.set("dark");
    await tick(5);
    assert(doc.getElementById("acct-theme-dark").checked, "radio follows the global theme");
});

/* =============================================================
   G. Get help
   ============================================================= */
console.log("\nG. Get help");

await check("Get help lists real topics and adds no fake contact action", async () => {
    const { doc, click, openMenu, action, screenTitle } = await boot();
    await openMenu();
    click(action("help"));
    eq(screenTitle(), "Get help");
    eq(doc.querySelectorAll("#account-body details.acct-faq").length, 7);
    eq(doc.querySelectorAll("#account-body a[href], #account-body button").length, 0, "no contact links/buttons invented");
    assert(!/@|mailto|whatsapp|ticket/i.test(doc.getElementById("account-body").textContent), "no invented support channel");
});

await check("help limits match the backend constants", () => {
    const main = read("backend/main.py");
    assert(/MAX_PDF_UPLOAD_SIZE_BYTES\s*=\s*15 \* 1024 \* 1024/.test(main));
    assert(/MAX_IMAGE_UPLOAD_SIZE_BYTES\s*=\s*8 \* 1024 \* 1024/.test(main));
    const i18n = read("static/js/i18n.js");
    assert(i18n.includes("PDFs up to 15 MB and images up to 8 MB"));
});

/* =============================================================
   H. Update plan
   ============================================================= */
console.log("\nH. Update plan");

await check("shows Free as the current plan and Pro as not available, with a disabled action", async () => {
    const { doc, click, openMenu, action, screenTitle } = await boot();
    await openMenu();
    click(action("plan"));
    eq(screenTitle(), "Choose your plan");
    const cards = [...doc.querySelectorAll("#account-body .acct-plan-card")];
    eq(cards.length, 2);
    assert(cards[0].classList.contains("is-current") && /Current plan/.test(cards[0].textContent));
    assert(/Not available yet/.test(cards[1].textContent));
    const btn = cards[1].querySelector("button");
    assert(btn.disabled && btn.getAttribute("aria-disabled") === "true");
    eq(doc.querySelectorAll("#account-body a[href]").length, 0, "no checkout link");
});

await check("no price, currency or payment claims are invented; the disabled button cannot do anything", async () => {
    const { window, doc, click, openMenu, action, bodyText } = await boot();
    await openMenu();
    click(action("plan"));
    assert(!/[\$\u09F3\u20B9]|BDT|USD|Tk\b|\/\s*month|per month|bKash|Nagad|card/i.test(bodyText()), "no price/payment text");
    assert(!/\d+\s*(?:৳|taka)/i.test(bodyText()));
    const before = doc.documentElement.outerHTML.length;
    doc.querySelector("#account-body .acct-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(doc.documentElement.outerHTML.length, before, "clicking the disabled button changes nothing");
});

/* =============================================================
   I. Learn more
   ============================================================= */
console.log("\nI. Learn more");

await check("Learn more lists the four destinations in order", async () => {
    const { doc, click, openMenu, action, screenTitle } = await boot();
    await openMenu();
    click(action("learn-more"));
    eq(screenTitle(), "Learn more");
    const titles = [...doc.querySelectorAll("#account-body .acct-link-title")].map((n) => n.textContent);
    eq(titles.join("|"), "About Kloriz|About Altruvis|Usage Policy|Privacy Policy");
});

await check("each destination opens, shows a Back arrow, and Back returns to Learn more", async () => {
    const { doc, $, click, openMenu, action, screenTitle } = await boot();
    await openMenu();
    click(action("learn-more"));
    eq($("account-back-btn").classList.contains("hidden"), true, "no back arrow on the list itself");
    for (const [i, title] of ["About Kloriz", "About Altruvis", "Usage Policy", "Privacy Policy"].entries()) {
        click(doc.querySelectorAll("#account-body .acct-link-row")[i]);
        eq(screenTitle(), title);
        eq($("account-back-btn").classList.contains("hidden"), false);
        click($("account-back-btn"));
        eq(screenTitle(), "Learn more");
    }
});

await check("missing legal content is stated honestly, not invented", async () => {
    const { doc, click, openMenu, action, bodyText } = await boot();
    await openMenu();
    click(action("learn-more"));
    click(doc.querySelectorAll("#account-body .acct-link-row")[2]);
    assert(/hasn't been published yet/.test(bodyText()));
    click(doc.getElementById("account-back-btn"));
    click(doc.querySelectorAll("#account-body .acct-link-row")[3]);
    assert(/hasn't been published yet/.test(bodyText()));
});

await check("a configured https URL becomes a safe external link; a non-https value is ignored", async () => {
    const { window, doc, click, openMenu, action } = await boot();
    const cfg = window.KlorizProfileMenu.config.LEARN_MORE_LINKS;
    cfg.privacy = "https://example.org/privacy";
    cfg.usage = "javascript:alert(1)";
    await openMenu();
    click(action("learn-more"));
    const rows = [...doc.querySelectorAll("#account-body .acct-link-row")];
    eq(rows[3].tagName, "A");
    eq(rows[3].getAttribute("href"), "https://example.org/privacy");
    eq(rows[3].getAttribute("target"), "_blank");
    assert(/noopener/.test(rows[3].getAttribute("rel")) && /noreferrer/.test(rows[3].getAttribute("rel")));
    eq(rows[2].tagName, "BUTTON", "javascript: must never become a link");
});

/* =============================================================
   J. Dialog layer + mobile drawer
   ============================================================= */
console.log("\nJ. Dialog layer and mobile drawer");

await check("the account dialog uses the shared modal layer: Escape closes it and focus returns to the trigger", async () => {
    const { window, doc, $, key, click, openMenu, action } = await boot();
    $("profile-trigger-btn").focus();
    await openMenu();
    click(action("help"));
    await tick(30);
    assert(doc.body.classList.contains("has-open-modal"));
    assert($("account-modal").contains(doc.activeElement), "focus moved into the dialog");
    key(doc.activeElement, "Escape");
    await tick(5);
    assert($("account-modal").classList.contains("hidden"));
    assert(!doc.body.classList.contains("has-open-modal"));
    eq(doc.activeElement, $("profile-trigger-btn"), "focus restored");
    assert(window.KlorizProfileMenu.isOpen() === false);
});

await check("overlay click closes the dialog", async () => {
    const { window, $, click, openMenu, action } = await boot();
    await openMenu();
    click(action("settings"));
    $("account-modal").dispatchEvent(new window.MouseEvent("mousedown", { bubbles: true }));
    assert($("account-modal").classList.contains("hidden"));
});

await check("in drawer mode the trigger keeps the drawer open; an action closes it; Escape closes only the menu", async () => {
    const { window, doc, $, click, key, openMenu, action } = await boot();
    Object.defineProperty(window, "innerWidth", { value: 500, configurable: true });
    doc.body.classList.add("sidebar-open");
    // the drawer's capture-phase "close on navigation" rule must not see the trigger
    click($("profile-trigger-btn"));
    await tick(5);
    assert(doc.body.classList.contains("sidebar-open"), "tapping the profile row must not close the drawer");
    assert(!$("profile-menu").classList.contains("hidden"));
    key(action("settings"), "Escape");
    assert($("profile-menu").classList.contains("hidden"));
    assert(doc.body.classList.contains("sidebar-open"), "Escape closes the menu, not the drawer behind it");
    await openMenu();
    click(action("help"));
    assert(!doc.body.classList.contains("sidebar-open"), "an action that opens a dialog closes the drawer");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
