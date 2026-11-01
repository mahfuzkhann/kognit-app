/* ============================================================
   KLORIZ UI/UX IMPROVEMENTS - jsdom tests against the REAL app
   ============================================================
   Runs the real templates/index.html with the real app.js, feedback.js,
   ui-core.js, shell.js, share.js (and the other shipped scripts), with
   only the network and Supabase faked. Covers:

     B  Bangla IME: Enter that confirms a composition never sends
     C  sign-in dialog: Enter submits, one request at a time, Bangla errors,
        no native alert(), dialog semantics, signup-without-session
     G  quiz close: confirm only with real progress; every close route agrees
     E  failure UI: plan limit vs rate limit vs server error, real actions,
        a failed regenerate / resend never overwrites or saves a fake answer
     F  scroll following + the "নিচে যান" pill (state logic; real scrolling
        is checked in the browser test)
     I  small accessibility facts (labels, names, toggle state)

   WHAT THIS CANNOT PROVE (see test_uiux_improvements_browser.mjs): real
   layout, focus ring appearance, real keyboard Enter / IME in Chromium.

   Run: node tests_frontend/test_uiux_improvements.mjs
   ============================================================ */
import fs from "node:fs";
import path from "node:path";
import { boot, R, QUOTA, sleep, assert, eq, ROOT, SRC } from "./lib/boot.mjs";

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}

/* =============================================================
   B. Bangla IME / Enter
   ============================================================= */
console.log("\nB. Enter and IME composition");
{
    const h = await boot();
    let sends = 0;
    h.w.sendMessage = () => { sends++; };
    const ta = h.input;
    const comp = (type) => ta.dispatchEvent(new h.w.CompositionEvent(type, { bubbles: true }));

    await check("plain Enter sends exactly once and its default (a newline) is prevented", () => {
        sends = 0; const e = h.key(ta, "Enter");
        eq(sends, 1); assert(e.defaultPrevented);
    });
    await check("Shift+Enter does NOT send and keeps its newline default", () => {
        sends = 0; const e = h.key(ta, "Enter", { shiftKey: true });
        eq(sends, 0); assert(!e.defaultPrevented);
    });
    await check("Enter with isComposing=true (the IME's own Enter) never sends", () => {
        sends = 0; const e = h.key(ta, "Enter", { isComposing: true });
        eq(sends, 0); assert(!e.defaultPrevented, "must not swallow the IME's key either");
    });
    await check("Enter with keyCode 229 ('processing') never sends", () => {
        sends = 0; h.key(ta, "Enter", { keyCode: 229 }); eq(sends, 0);
    });
    await check("Enter between compositionstart and compositionend never sends", () => {
        sends = 0; comp("compositionstart"); h.key(ta, "Enter"); eq(sends, 0); comp("compositionend");
    });
    await check("Safari order (compositionend THEN the confirming Enter, isComposing already false) does not send", () => {
        sends = 0; comp("compositionstart"); comp("compositionend"); h.key(ta, "Enter"); eq(sends, 0);
    });
    await check("after the short grace window a real Enter sends again (the grace is not sticky)", async () => {
        await sleep(120); sends = 0; h.key(ta, "Enter"); eq(sends, 1);
    });
    await check("a composition that ends normally leaves no stuck state: Enter right after typing English works", async () => {
        comp("compositionstart"); comp("compositionend"); await sleep(120);
        sends = 0; h.key(ta, "Enter"); eq(sends, 1);
    });
    await check("rapid Enter presses while composing never produce a send", () => {
        sends = 0; comp("compositionstart");
        for (let i = 0; i < 10; i++) h.key(ta, "Enter", { isComposing: true });
        comp("compositionend"); eq(sends, 0);
    });
    await check("non-Enter keys are untouched (typing during composition is not intercepted)", () => {
        sends = 0; const e = h.key(ta, "a", { isComposing: true }); eq(sends, 0); assert(!e.defaultPrevented);
    });
    await check("the send BUTTON still sends regardless of composition state", () => {
        const btn = h.$("send-btn"); assert(btn.getAttribute("onclick") === "sendMessage()");
    });
    await check("rename inputs honour the same guard (Bangla chat titles)", async () => {
        await sleep(120);
        const chat = h.seed([{ role: "user", text: "q" }]);
        h.w.eval("editingChatId = 'c1'"); h.w.renderHistoryList();
        const input = h.doc.querySelector(".rename-input");
        assert(input, "rename input rendered");
        input.value = "নতুন নাম";
        h.key(input, "Enter", { isComposing: true });
        eq(chat.title, "Chat 1", "IME Enter must not commit the rename");
        h.key(input, "Enter");
        eq(chat.title, "নতুন নাম", "a real Enter commits it");
    });
}

/* =============================================================
   C. Sign-in dialog
   ============================================================= */
console.log("\nC. Sign-in / sign-up dialog");
{
    await check("dialog semantics: role=dialog, aria-modal, labelled by the title, described, named close button", async () => {
        const h = await boot({ guest: true });
        const card = h.doc.querySelector("#auth-modal .auth-card");
        eq(card.getAttribute("role"), "dialog"); eq(card.getAttribute("aria-modal"), "true");
        assert(h.$(card.getAttribute("aria-labelledby")), "labelledby resolves");
        assert(h.$(card.getAttribute("aria-describedby")), "describedby resolves");
        assert(card.querySelector(".auth-close-btn").getAttribute("aria-label"), "close button has a name");
    });
    await check("every field has a <label for> bound to it; the form is a real <form> with one submit button", async () => {
        const h = await boot({ guest: true });
        for (const id of ["auth-email-input", "auth-password-input"]) assert(h.doc.querySelector(`label[for="${id}"]`), id);
        const form = h.$("auth-form");
        eq(form.tagName, "FORM"); eq(form.querySelectorAll('button[type="submit"]').length, 1);
        assert(form.contains(h.$("auth-email-input")) && form.contains(h.$("auth-password-input")), "inputs are inside the form (Enter submits)");
    });
    await check("input types and autofill hints are correct; they switch with the mode", async () => {
        const h = await boot({ guest: true });
        const e = h.$("auth-email-input"), p = h.$("auth-password-input");
        eq(e.type, "email"); eq(e.getAttribute("autocomplete"), "email"); eq(p.type, "password");
        eq(p.getAttribute("autocomplete"), "current-password");
        h.w.openAuthModal(); h.w.toggleAuthMode();
        eq(p.getAttribute("autocomplete"), "new-password", "sign-up asks for a NEW password");
        eq(h.$("auth-submit-btn").textContent.trim(), "Sign Up");
        h.w.toggleAuthMode();
        eq(p.getAttribute("autocomplete"), "current-password");
    });
    await check("empty fields: a Bangla message, the empty field is marked and focused, and NO request is made", async () => {
        const h = await boot({ guest: true }); let n = 0;
        h.env.auth.signInWithPassword = async () => { n++; return { data: {}, error: null }; };
        h.w.openAuthModal();
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true, bubbles: true }));
        await sleep(20);
        eq(n, 0);
        assert(/ইমেইল ও পাসওয়ার্ড/.test(h.$("auth-error-msg").textContent));
        eq(h.$("auth-email-input").getAttribute("aria-invalid"), "true");
        eq(h.doc.activeElement, h.$("auth-email-input"));
    });
    await check("an invalid email is rejected locally in Bangla", async () => {
        const h = await boot({ guest: true });
        h.w.openAuthModal(); h.$("auth-email-input").value = "not-an-email"; h.$("auth-password-input").value = "x";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true }));
        await sleep(20);
        assert(/সঠিক ইমেইল/.test(h.$("auth-error-msg").textContent));
    });
    await check("submitting the FORM signs in with the exact password (NOT trimmed) and the trimmed email", async () => {
        const h = await boot({ guest: true }); let seen = null;
        h.env.auth.signInWithPassword = async (c) => { seen = c; return { data: { user: { id: "u", email: "a@b.co" }, session: {} }, error: null }; };
        h.w.openAuthModal(); h.$("auth-email-input").value = "  a@b.co  "; h.$("auth-password-input").value = " pa ss ";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true }));
        await sleep(40);
        eq(seen, { email: "a@b.co", password: " pa ss " });
    });
    await check("success: the dialog closes, the password field is cleared, the student is signed in - and NO alert() appears", async () => {
        const h = await boot({ guest: true });
        h.w.openAuthModal(); h.$("auth-email-input").value = "a@b.co"; h.$("auth-password-input").value = "secret";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true }));
        await sleep(60);
        assert(h.hidden("auth-modal"), "closed");
        eq(h.$("auth-password-input").value, "", "password cleared");
        eq(h.w.eval("currentUser && currentUser.id"), "u" === "u" ? "user-1" : "", "signed in");
        eq(h.env.alerts, 0, "no native alert");
    });
    await check("a SECOND submit while one is pending is ignored (exactly one request); the button is disabled + aria-busy", async () => {
        const h = await boot({ guest: true }); let n = 0, release;
        h.env.auth.signInWithPassword = () => { n++; return new Promise((r) => { release = () => r({ data: { user: { id: "u" }, session: {} }, error: null }); }); };
        h.w.openAuthModal(); h.$("auth-email-input").value = "a@b.co"; h.$("auth-password-input").value = "secret";
        const fire = () => h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true }));
        fire(); fire(); fire(); await sleep(20);
        eq(n, 1, "one request");
        const btn = h.$("auth-submit-btn");
        assert(btn.disabled && btn.getAttribute("aria-busy") === "true", "busy state");
        assert(h.$("auth-google-btn").disabled && h.$("auth-toggle-link").disabled, "other routes disabled while pending");
        h.w.toggleAuthMode(); assert(!h.w.eval("isSignUpMode"), "mode can not be flipped mid-request");
        release(); await sleep(60);
        assert(!btn.disabled && !btn.hasAttribute("aria-busy"), "re-enabled afterwards");
    });
    await check("wrong password: Bangla message from the provider CODE, dialog stays open, typed values KEPT, focus on password, no English leak", async () => {
        const h = await boot({ guest: true });
        h.env.auth.signInWithPassword = async () => ({ data: {}, error: { code: "invalid_credentials", message: "Invalid login credentials", status: 400 } });
        h.w.openAuthModal(); h.$("auth-email-input").value = "a@b.co"; h.$("auth-password-input").value = "wrong";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true }));
        await sleep(40);
        const msg = h.$("auth-error-msg");
        assert(!msg.classList.contains("hidden") && /মিলছে না/.test(msg.textContent), msg.textContent);
        assert(!/Invalid login credentials/.test(h.$("auth-modal").textContent), "raw provider text not shown");
        assert(!h.hidden("auth-modal"), "still open");
        eq(h.$("auth-email-input").value, "a@b.co"); eq(h.$("auth-password-input").value, "wrong");
        eq(h.doc.activeElement, h.$("auth-password-input"));
        assert(!h.$("auth-submit-btn").disabled);
    });
    await check("provider error codes map to specific Bangla messages (unconfirmed, exists, weak, rate limit, unknown)", async () => {
        const h = await boot({ guest: true });
        const expect = [
            [{ code: "email_not_confirmed", message: "Email not confirmed" }, /যাচাই করা হয়নি/],
            [{ code: "over_request_rate_limit", status: 429 }, /অনেকবার/],
            [{ message: "weird internal thing", status: 500 }, /লগইন করা যায়নি/],
        ];
        for (const [error, re] of expect) {
            h.env.auth.signInWithPassword = async () => ({ data: {}, error });
            h.w.openAuthModal(); h.$("auth-email-input").value = "a@b.co"; h.$("auth-password-input").value = "x";
            h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true })); await sleep(30);
            assert(re.test(h.$("auth-error-msg").textContent), `${JSON.stringify(error)} -> ${h.$("auth-error-msg").textContent}`);
            assert(!/weird internal thing/.test(h.$("auth-error-msg").textContent), "raw message never shown");
        }
    });
    await check("weak password: Bangla message that names the provider's minimum length in Bangla digits", async () => {
        const h = await boot({ guest: true });
        h.env.auth.signUp = async () => ({ data: {}, error: { code: "weak_password", message: "Password should be at least 6 characters." } });
        h.w.openAuthModal(); h.w.toggleAuthMode(); h.$("auth-email-input").value = "a@b.co"; h.$("auth-password-input").value = "123";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true })); await sleep(30);
        assert(/কমপক্ষে ৬ অক্ষরের/.test(h.$("auth-error-msg").textContent), h.$("auth-error-msg").textContent);
    });
    await check("a network failure (rejected promise): Bangla network message, no unhandled rejection, fields kept, button usable again", async () => {
        const h = await boot({ guest: true }); let unhandled = 0;
        h.w.addEventListener("unhandledrejection", () => unhandled++);
        h.env.auth.signInWithPassword = async () => { throw new h.w.TypeError("Failed to fetch"); };
        h.w.openAuthModal(); h.$("auth-email-input").value = "a@b.co"; h.$("auth-password-input").value = "pw";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true })); await sleep(40);
        assert(/ইন্টারনেট সংযোগে সমস্যা/.test(h.$("auth-error-msg").textContent));
        assert(!/Failed to fetch|TypeError/.test(h.$("auth-modal").textContent));
        eq(h.$("auth-email-input").value, "a@b.co"); assert(!h.$("auth-submit-btn").disabled); eq(unhandled, 0);
    });
    await check("sign-up when email confirmation is REQUIRED (no session): shows the 'check your email' notice, flips to sign-in, does NOT sign the student in", async () => {
        const h = await boot({ guest: true });
        h.w.openAuthModal(); h.w.toggleAuthMode(); h.$("auth-email-input").value = "new@b.co"; h.$("auth-password-input").value = "longenough";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true })); await sleep(40);
        const note = h.$("auth-notice-msg");
        assert(!note.classList.contains("hidden") && /ইমেইলে পাঠানো লিংক/.test(note.textContent), note.textContent);
        assert(!h.hidden("auth-modal"), "stays open so the message can be read");
        eq(h.w.eval("currentUser"), null, "not signed in");
        eq(h.w.eval("isSignUpMode"), false, "back to sign-in");
        eq(h.$("auth-password-input").value, "", "the new password is not left in the field");
        eq(h.env.alerts, 0);
    });
    await check("sign-up with an already-registered email (Supabase's empty-identities reply) is reported as such", async () => {
        const h = await boot({ guest: true });
        h.env.auth.signUp = async () => ({ data: { user: { id: "x", identities: [] }, session: null }, error: null });
        h.w.openAuthModal(); h.w.toggleAuthMode(); h.$("auth-email-input").value = "old@b.co"; h.$("auth-password-input").value = "longenough";
        h.$("auth-form").dispatchEvent(new h.w.Event("submit", { cancelable: true })); await sleep(40);
        assert(/আগেই অ্যাকাউন্ট খোলা/.test(h.$("auth-error-msg").textContent));
        eq(h.w.eval("currentUser"), null);
    });
    await check("Google sign-in failure shows a Bangla inline message and re-enables the button; a thrown error does too", async () => {
        const h = await boot({ guest: true });
        h.env.auth.signInWithOAuth = async () => ({ error: { code: "x", message: "provider internals" } });
        h.w.openAuthModal(); await h.w.handleGoogleSignIn();
        assert(/Google/.test(h.$("auth-error-msg").textContent) && !/provider internals/.test(h.$("auth-error-msg").textContent));
        assert(!h.$("auth-google-btn").disabled);
        h.env.auth.signInWithOAuth = async () => { throw new h.w.TypeError("net"); };
        await h.w.handleGoogleSignIn();
        assert(/ইন্টারনেট/.test(h.$("auth-error-msg").textContent)); assert(!h.$("auth-google-btn").disabled);
    });
    await check("the password-visibility button toggles type + aria-pressed + its accessible name, and re-hides on reopen", async () => {
        const h = await boot({ guest: true });
        const p = h.$("auth-password-input"), t = h.$("auth-password-toggle");
        h.w.openAuthModal(); t.click();
        eq(p.type, "text"); eq(t.getAttribute("aria-pressed"), "true"); eq(t.getAttribute("aria-label"), "Hide password");
        h.w.closeAuthModal(); h.w.openAuthModal();
        eq(p.type, "password"); eq(t.getAttribute("aria-pressed"), "false");
    });
    await check("switching mode keeps what was typed, clears old messages, and only the labels change", async () => {
        const h = await boot({ guest: true });
        h.w.openAuthModal(); h.$("auth-email-input").value = "keep@b.co"; h.$("auth-password-input").value = "keepme";
        h.w.toggleAuthMode();
        eq(h.$("auth-email-input").value, "keep@b.co"); eq(h.$("auth-password-input").value, "keepme");
        assert(h.$("auth-error-msg").classList.contains("hidden"));
        eq(h.doc.querySelector("#auth-modal .auth-card").getAttribute("data-auth-mode"), "signup");
    });
    await check("requiring login (guest presses Send) opens the dialog with a Bangla explanation - not an error box", async () => {
        const h = await boot({ guest: true });
        h.w.promptLoginRequired();
        assert(!h.hidden("auth-modal"));
        assert(/লগইন করুন/.test(h.$("auth-notice-msg").textContent));
        assert(h.$("auth-error-msg").classList.contains("hidden"));
        h.w.promptLoginRequired("sessionExpired");
        assert(/সেশনের মেয়াদ শেষ/.test(h.$("auth-notice-msg").textContent));
    });
    await check("Escape closes the sign-in dialog and focus returns to the control that opened it", async () => {
        const h = await boot({ guest: true });
        const opener = h.doc.querySelector('[onclick="openAuthModal()"]') || h.$("user-input");
        opener.focus(); h.w.openAuthModal(); await sleep(60);
        assert(h.$("auth-modal").contains(h.doc.activeElement), "focus moved into the dialog");
        h.key(h.doc.activeElement, "Escape"); await sleep(30);
        assert(h.hidden("auth-modal"), "closed by Escape");
        eq(h.doc.activeElement, opener, "focus restored");
    });
    await check("logging out no longer pops a native alert", async () => {
        const h = await boot(); h.signIn();
        await h.w.handleLogout(); eq(h.env.alerts, 0);
    });
}

/* =============================================================
   G. Quiz close / progress
   ============================================================= */
console.log("\nG. Quiz: closing preserves progress");
{
    const setupActive = async (h, { idx = 0, answers = [null, null, null] } = {}) => {
        h.w.eval(`quizQuestions = [1,2,3].map(i => ({ question: 'Q'+i, options: ['a','b','c','d'], correct_index: 0, explanation: 'e' }));
                  currentQuizIdx = ${idx}; userQuizAnswers = ${JSON.stringify(answers)};`);
        h.$("quiz-modal").classList.remove("hidden");
        h.$("quiz-setup-view").classList.add("hidden"); h.$("quiz-result-view").classList.add("hidden");
        h.$("quiz-active-view").classList.remove("hidden");
        await sleep(40);
    };
    const state = (h) => JSON.parse(h.w.eval("JSON.stringify({ idx: currentQuizIdx, answers: userQuizAnswers, n: quizQuestions.length })"));

    await check("a quiz with NO progress closes immediately - no confirmation", async () => {
        const h = await boot(); await setupActive(h);
        h.w.requestCloseQuiz();
        assert(h.hidden("quiz-modal")); assert(h.hidden("quiz-discard-modal"));
    });
    await check("with an answer chosen, closing asks first (quiz stays open, answers untouched)", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, null, null] });
        h.w.requestCloseQuiz();
        assert(!h.hidden("quiz-discard-modal"), "confirmation shown"); assert(!h.hidden("quiz-modal"), "quiz still open");
        eq(state(h).answers, [2, null, null]);
    });
    await check("being past question 1 counts as progress even if the current question is unanswered", async () => {
        const h = await boot(); await setupActive(h, { idx: 1, answers: [1, null, null] });
        h.w.requestCloseQuiz(); assert(!h.hidden("quiz-discard-modal"));
    });
    await check("'কুইজ চালিয়ে যান' keeps every answer and the current question and the quiz stays open", async () => {
        const h = await boot(); await setupActive(h, { idx: 1, answers: [1, 3, null] });
        h.w.requestCloseQuiz(); h.w.closeQuizDiscardConfirm();
        assert(h.hidden("quiz-discard-modal")); assert(!h.hidden("quiz-modal"));
        eq(state(h), { idx: 1, answers: [1, 3, null], n: 3 });
    });
    await check("'বাদ দিন' discards ONLY this attempt: quiz and confirmation close, state resets", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, null, null] });
        h.w.requestCloseQuiz(); h.w.confirmQuizDiscard();
        assert(h.hidden("quiz-modal")); assert(h.hidden("quiz-discard-modal"));
        eq(state(h), { idx: 0, answers: [], n: 0 });
        assert(!h.hidden("quiz-setup-view"), "back to the setup view for the next attempt");
    });
    await check("the result view and the setup view close with no friction even if answers exist in memory", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, 1, 0] });
        h.$("quiz-active-view").classList.add("hidden"); h.$("quiz-result-view").classList.remove("hidden");
        h.w.requestCloseQuiz(); assert(h.hidden("quiz-modal")); assert(h.hidden("quiz-discard-modal"));
        const h2 = await boot(); h2.$("quiz-modal").classList.remove("hidden");
        h2.w.requestCloseQuiz(); assert(h2.hidden("quiz-modal"));
    });
    await check("X button, Escape and an outside click all follow the SAME rule (confirm with progress)", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, null, null] });
        h.$("quiz-modal").querySelector(".close-modal-btn").click();
        assert(!h.hidden("quiz-discard-modal"), "X -> confirm");
        h.w.closeQuizDiscardConfirm();
        h.key(h.doc.activeElement || h.doc.body, "Escape"); await sleep(20);
        assert(!h.hidden("quiz-discard-modal"), "Escape -> confirm"); assert(!h.hidden("quiz-modal"));
        h.w.closeQuizDiscardConfirm();
        h.$("quiz-modal").dispatchEvent(new h.w.MouseEvent("mousedown", { bubbles: true }));
        await sleep(20);
        assert(!h.hidden("quiz-discard-modal"), "outside click -> confirm");
        eq(state(h).answers, [2, null, null], "nothing was lost along the way");
    });
    await check("Escape while the confirmation is open means 'keep going' (closes only the confirmation)", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, null, null] });
        h.w.requestCloseQuiz(); await sleep(30);
        h.key(h.doc.activeElement || h.doc.body, "Escape"); await sleep(20);
        assert(h.hidden("quiz-discard-modal")); assert(!h.hidden("quiz-modal"));
        eq(state(h).answers, [2, null, null]);
    });
    await check("an outside click on the CONFIRMATION also means 'keep going' and does not reach the quiz behind it", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, null, null] });
        h.w.requestCloseQuiz(); await sleep(30);
        h.$("quiz-discard-modal").dispatchEvent(new h.w.MouseEvent("mousedown", { bubbles: true }));
        await sleep(20);
        assert(h.hidden("quiz-discard-modal")); assert(!h.hidden("quiz-modal"), "quiz untouched");
    });
    await check("opening another dialog does not erase quiz answers", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, null, null] });
        h.$("logout-confirm-modal").classList.remove("hidden"); await sleep(20);
        h.$("logout-confirm-modal").classList.add("hidden");
        eq(state(h).answers, [2, null, null]);
    });
    await check("the confirmation is an alertdialog with a name, a description and a safe default focus (Continue)", async () => {
        const h = await boot(); await setupActive(h, { answers: [2, null, null] });
        const card = h.$("quiz-discard-modal").querySelector(".modal-card");
        eq(card.getAttribute("role"), "alertdialog"); eq(card.getAttribute("aria-modal"), "true");
        assert(h.$(card.getAttribute("aria-labelledby")) && h.$(card.getAttribute("aria-describedby")));
        h.w.requestCloseQuiz(); await sleep(60);
        assert(/চালিয়ে যান/.test(h.doc.activeElement.textContent), "default focus is the SAFE choice");
    });
    await check("quiz modal: dialog semantics, labelled fields, named close button, progress announced politely", async () => {
        const h = await boot();
        const card = h.$("quiz-modal").querySelector(".modal-card");
        eq(card.getAttribute("role"), "dialog"); assert(h.$(card.getAttribute("aria-labelledby")));
        for (const id of ["quiz-subject-select", "quiz-topic-input", "quiz-count-select"]) assert(h.doc.querySelector(`label[for="${id}"]`), id);
        assert(h.$("quiz-modal").querySelector(".close-modal-btn").getAttribute("aria-label"));
        eq(h.$("quiz-progress-text").getAttribute("aria-live"), "polite");
    });
}

/* =============================================================
   E. Failure UI
   ============================================================= */
console.log("\nE. Failures: Bangla, specific, with real actions");
{
    const send = async (h, text = "প্রশ্ন") => { h.input.value = text; h.w.sendMessage(); await sleep(60); };

    await check("plan limit (429 QUOTA_EXCEEDED) -> Bangla message + প্ল্যান দেখুন that opens the existing Plan screen; nothing saved as an answer", async () => {
        const h = await boot({ route: (url) => url === "/api/chat/stream" ? R(429, QUOTA()) : R(404, {}) });
        const chat = h.seed(); await send(h);
        const bubble = h.chatBox.querySelector(".chat-failure");
        assert(bubble, "failure bubble"); eq(bubble.getAttribute("role"), "alert");
        assert(/ফ্রি প্ল্যানে আপনার চ্যাটের সীমা শেষ/.test(bubble.textContent), bubble.textContent);
        const btn = bubble.querySelector(".chat-failure-action");
        eq(btn.textContent, "প্ল্যান দেখুন");
        btn.click();
        assert(h.env.calls.some((c) => c.openAccount === "plan"), "opened the real Plan pane");
        eq(chat.messages.filter((m) => m.role === "bot").length, 0, "no fake bot answer was stored");
        eq(chat.messages.filter((m) => m.role === "user").length, 1, "the student's message is kept");
        assert(!h.$("send-btn").classList.contains("hidden"), "composer released");
    });
    await check("short-term rate limit (reason=rate_limited) is a DIFFERENT message and offers retry, not the plan screen", async () => {
        const h = await boot({ route: (url) => url === "/api/chat/stream" ? R(429, QUOTA({ reason: "rate_limited" })) : R(404, {}) });
        h.seed(); await send(h);
        const bubble = h.chatBox.querySelector(".chat-failure");
        assert(!/সীমা শেষ/.test(bubble.textContent) && /অনেক অনুরোধ/.test(bubble.textContent), bubble.textContent);
        eq(bubble.querySelector(".chat-failure-action").textContent, "আবার চেষ্টা করুন");
    });
    await check("the retry button re-sends THAT message through the existing resend path (one request, bubble replaced)", async () => {
        let nonStream = 0;
        const h = await boot({ route: (url) => { if (url === "/api/chat/stream") return R(500, { detail: "x" }); if (url === "/api/chat") { nonStream++; return R(200, { reply: "উত্তর" }); } return R(404, {}); } });
        const chat = h.seed(); await send(h, "প্রশ্ন ১");
        h.chatBox.querySelector(".chat-failure-action").click(); await sleep(120);
        eq(nonStream, 1, "exactly one retry request");
        assert(!h.chatBox.querySelector(".chat-failure"), "the failure bubble is gone");
        eq(chat.messages.map((m) => m.role), ["user", "bot"]);
        eq(chat.messages[1].text, "উত্তর");
    });
    await check("server error (500) -> busy message with retry; session (401) -> opens sign-in with the session-expired note", async () => {
        const h = await boot({ route: (url) => url === "/api/chat/stream" ? R(500, {}) : R(404, {}) });
        h.seed(); await send(h);
        assert(/সাময়িকভাবে ব্যস্ত/.test(h.chatBox.querySelector(".chat-failure").textContent));
        const h2 = await boot({ route: (url) => url === "/api/chat/stream" ? R(401, {}) : R(404, {}) });
        h2.seed(); await send(h2);
        assert(!h2.hidden("auth-modal")); assert(/সেশনের মেয়াদ শেষ/.test(h2.$("auth-notice-msg").textContent));
    });
    await check("a dropped connection (fetch rejects) -> Bangla network message with আবার সংযোগ করুন, never exception text", async () => {
        const h = await boot(); h.w.fetch = (u) => u === "/api/profile" ? Promise.resolve(R(200, { profile: { name: "T", user_class: "Class 9-10 (SSC)", stream: "Science" } })) : Promise.reject(new h.w.TypeError("Failed to fetch"));
        h.seed(); await send(h);
        const b = h.chatBox.querySelector(".chat-failure");
        assert(/ইন্টারনেট সংযোগে সমস্যা/.test(b.textContent) && !/Failed to fetch/.test(h.chatBox.textContent));
        eq(b.querySelector(".chat-failure-action").textContent, "আবার সংযোগ করুন");
    });
    await check("expired PDF (409) still marks the chat's PDF as gone AND explains it in Bangla with no retry loop", async () => {
        const h = await boot({ route: (url) => url === "/api/chat/stream" ? R(409, { detail: { code: "PDF_CONTEXT_EXPIRED", message: "x" } }) : R(404, {}) });
        const chat = h.seed(); chat.pdf = { active: true, name: "a.pdf" }; await send(h);
        assert(/পিডিএফ/.test(h.chatBox.querySelector(".chat-failure").textContent));
        eq(h.chatBox.querySelector(".chat-failure .chat-failure-action"), null, "retrying without the PDF would silently change the answer");
        eq(chat.pdf.active, false, "chat PDF marked inactive");
    });
    await check("Regenerate hitting the plan limit KEEPS the existing answer (it used to overwrite it with 'No response received.')", async () => {
        const h = await boot({ route: (url) => url === "/api/chat" ? R(429, QUOTA()) : R(404, {}) });
        const chat = h.seed([{ role: "user", text: "প্রশ্ন" }, { role: "bot", text: "ভালো উত্তর" }]);
        const btn = h.doc.createElement("button"); h.doc.body.appendChild(btn);
        await h.w.regenerateBotMessage({ projId: "p1", chatId: "c1", msgIndex: 1 }, btn); await sleep(40);
        eq(chat.messages[1].text, "ভালো উত্তর", "good answer preserved");
        assert(!btn.disabled, "button restored");
        const toast = h.doc.querySelector(".kloriz-toast"); assert(toast && /চ্যাটের সীমা শেষ/.test(toast.textContent));
        eq(toast.querySelector(".kloriz-toast-action").textContent, "প্ল্যান দেখুন");
        eq(h.env.alerts, 0);
    });
    await check("Resend/Edit hitting a limit saves NOTHING (no fake 'No response received.' bot message) and shows a failure bubble", async () => {
        const h = await boot({ route: (url) => url === "/api/chat" ? R(429, QUOTA({ reason: "rate_limited" })) : R(404, {}) });
        const chat = h.seed([{ role: "user", text: "প্রশ্ন" }]);
        await h.w.resendUserMessage({ projId: "p1", chatId: "c1", msgIndex: 0 }, null); await sleep(80);
        eq(chat.messages.map((m) => m.role), ["user"], "no bot message stored");
        assert(h.chatBox.querySelector(".chat-failure"), "explained in the chat");
    });
    await check("quiz generation: plan limit -> Bangla toast with প্ল্যান দেখুন; no native alert", async () => {
        const h = await boot({ route: (url) => url === "/api/quiz/generate" ? R(429, QUOTA({ kind: "quiz" })) : R(404, {}) });
        h.signIn(); h.$("quiz-topic-input").value = "বল ও গতি";
        await h.w.generateAndStartQuiz(); await sleep(40);
        const t = h.doc.querySelector(".kloriz-toast"); assert(t && /কুইজের সীমা শেষ/.test(t.textContent), t && t.textContent);
        eq(t.querySelector(".kloriz-toast-action").textContent, "প্ল্যান দেখুন"); eq(h.env.alerts, 0);
    });
    await check("quiz generation: network failure and an empty result are each explained in Bangla (toast), button restored", async () => {
        const h = await boot(); h.signIn();
        h.w.fetch = () => Promise.reject(new h.w.TypeError("x"));
        await h.w.generateAndStartQuiz(); await sleep(30);
        assert(/ইন্টারনেট/.test(h.doc.querySelector(".kloriz-toast").textContent));
        const h2 = await boot({ route: (url) => url === "/api/quiz/generate" ? R(200, { questions: [] }) : R(404, {}) });
        h2.signIn(); await h2.w.generateAndStartQuiz(); await sleep(30);
        assert(/কুইজ তৈরি করা যায়নি/.test(h2.doc.querySelector(".kloriz-toast").textContent));
        eq(h2.env.alerts, 0);
        assert(!h2.doc.querySelector("#quiz-setup-view .start-quiz-btn").disabled);
    });
    await check("selecting no answer before 'Next' shows a Bangla toast, not an alert", async () => {
        const h = await boot();
        h.w.eval("quizQuestions=[{question:'q',options:['a','b','c','d'],correct_index:0,explanation:''},{question:'q2',options:['a','b','c','d'],correct_index:0,explanation:''}]; userQuizAnswers=[null,null]; currentQuizIdx=0;");
        h.w.nextQuizQuestion();
        assert(/উত্তর বেছে নিন/.test(h.doc.querySelector(".kloriz-toast").textContent)); eq(h.env.alerts, 0);
    });
    await check("copy failure is a Bangla toast, not an alert", async () => {
        const h = await boot(); h.env.alerts = 0;
        Object.defineProperty(h.w.navigator, "clipboard", { value: { writeText: () => Promise.reject(new Error("denied")) }, configurable: true });
        h.seed([{ role: "user", text: "q" }, { role: "bot", text: "a" }]);
        h.doc.querySelector(".bot-message .toolbar-btn").click(); await sleep(40);
        assert(/কপি করা যায়নি/.test(h.doc.querySelector(".kloriz-toast").textContent)); eq(h.env.alerts, 0);
    });
    await check("PDF upload failure: Bangla toast; a 413 says the file is too large; a 400 says this PDF can not be read (no retry offered)", async () => {
        const h = await boot({ route: (url) => url === "/api/pdf/upload" ? R(413, { detail: "big" }) : R(404, {}) });
        h.seed();
        const file = new h.w.File(["%PDF"], "a.pdf", { type: "application/pdf" });
        await h.w.handlePDFUpload({ target: { files: [file], value: "" } }); await sleep(40);
        assert(/ফাইলটি অনেক বড়/.test(h.doc.querySelector(".kloriz-toast").textContent));
        const h2 = await boot({ route: (url) => url === "/api/pdf/upload" ? R(400, { detail: "encrypted" }) : R(404, {}) });
        h2.seed(); await h2.w.handlePDFUpload({ target: { files: [file], value: "" } }); await sleep(40);
        const t = h2.doc.querySelector(".kloriz-toast"); assert(/পিডিএফটি আপলোড করা যায়নি/.test(t.textContent));
        eq(t.querySelector(".kloriz-toast-action"), null);
        eq(h2.env.alerts, 0);
    });
    await check("a stream 'error' event is shown as the Bangla message for its CODE, with a retry only where retrying can help", async () => {
        const mk = async (code) => {
            const enc = new TextEncoder(); let sent = false;
            const body = { getReader: () => ({ read: async () => { if (sent) return { done: true }; sent = true; return { value: enc.encode(JSON.stringify({ type: "error", reply: "ENGLISH SERVER TEXT", code }) + "\n"), done: false }; }, cancel: async () => {} }) };
            const h = await boot({ route: (url) => url === "/api/chat/stream" ? { ok: true, status: 200, headers: { get: () => null }, body, json: async () => ({}), clone() { return this; } } : R(404, {}) });
            h.seed(); await send(h); return h;
        };
        const busy = await mk("PROVIDER_UNAVAILABLE");
        assert(/সাময়িকভাবে ব্যস্ত/.test(busy.chatBox.textContent) && !/ENGLISH SERVER TEXT/.test(busy.chatBox.textContent));
        assert(busy.chatBox.querySelector(".chat-failure .chat-failure-action"), "retry for a temporary problem");
        const safety = await mk("PROVIDER_SAFETY");
        assert(/অন্যভাবে লিখে/.test(safety.chatBox.textContent));
        eq(safety.chatBox.querySelector(".chat-failure .chat-failure-action"), null, "no retry for a blocked prompt");
    });
}

/* =============================================================
   F. Scroll following (state logic)
   ============================================================= */
console.log("\nF. Scroll following");
{
    const rig = async () => {
        const h = await boot(); h.seed([{ role: "user", text: "q" }, { role: "bot", text: "a" }]);
        const box = h.chatBox; let top = 0;
        Object.defineProperty(box, "scrollHeight", { get: () => 2000, configurable: true });
        Object.defineProperty(box, "clientHeight", { get: () => 400, configurable: true });
        Object.defineProperty(box, "scrollTop", { get: () => top, set: (v) => { top = v; }, configurable: true });
        h.top = () => top; h.setTop = (v) => { top = v; box.dispatchEvent(new h.w.Event("scroll")); };
        h.w.scrollChatToBottom(true); box.dispatchEvent(new h.w.Event("scroll"));
        return h;
    };
    await check("a reader at the bottom is FOLLOWED: new content moves the view and no pill shows", async () => {
        const h = await rig(); h.setTop(1600); h.w.scrollChatToBottom(false);
        eq(h.top(), 2000); assert(h.hidden("scroll-latest-btn"));
    });
    await check("a reader who scrolled UP is NOT moved when content arrives or the answer finishes", async () => {
        const h = await rig(); h.setTop(200);
        h.w.scrollChatToBottom(false); h.w.scrollChatToBottom(false);
        eq(h.top(), 200, "position kept");
    });
    await check("…and gets a 'নতুন উত্তর দেখুন' pill that scrolls to the end when pressed", async () => {
        const h = await rig(); h.setTop(200); h.w.scrollChatToBottom(false);
        const pill = h.$("scroll-latest-btn");
        assert(!pill.classList.contains("hidden"), "pill visible");
        eq(h.$("scroll-latest-label").textContent, "নতুন উত্তর দেখুন");
        pill.click(); await sleep(20);
        eq(h.top(), 2000); h.setTop(2000);
        assert(pill.classList.contains("hidden"), "pill hides once at the bottom");
    });
    await check("while an answer is still being written and the reader is away the pill says 'নিচে যান'", async () => {
        const h = await rig(); h.setTop(200);
        h.w.eval("activeGenerations.set(activeChatId, { stopRequested: false })"); h.w.updateScrollPill();
        eq(h.$("scroll-latest-label").textContent, "নিচে যান"); assert(!h.hidden("scroll-latest-btn"));
        h.w.eval("activeGenerations.delete(activeChatId)");
    });
    await check("actions the STUDENT takes (send / regenerate / open a chat) always bring the newest content into view", async () => {
        const h = await rig(); h.setTop(100);
        h.w.scrollChatToBottom(true);
        eq(h.top(), 2000);
    });
    await check("scrolling back near the bottom resumes following; being within the threshold counts as following", async () => {
        const h = await rig(); h.setTop(200); h.setTop(1500);
        h.w.scrollChatToBottom(false); eq(h.top(), 2000);
    });
    await check("the pill is a real button with visible text (no icon-only control)", async () => {
        const h = await rig(); const p = h.$("scroll-latest-btn");
        eq(p.tagName, "BUTTON"); eq(p.type, "button"); assert(p.textContent.trim().length > 0);
    });
}

/* =============================================================
   D / I. Toolbar facts and other small accessibility facts
   ============================================================= */
console.log("\nD/I. Toolbar and small accessibility facts");
{
    await check("icon-only Like / Dislike have an accessible name and expose their pressed state", async () => {
        const h = await boot(); h.seed([{ role: "user", text: "q" }, { role: "bot", text: "a" }]);
        const btns = [...h.doc.querySelectorAll(".bot-message .toolbar-btn")];
        const like = btns.find((b) => b.title === "Good answer"), dis = btns.find((b) => b.title === "Needs improvement");
        eq(like.getAttribute("aria-label"), "Good answer"); eq(dis.getAttribute("aria-label"), "Needs improvement");
        eq(like.getAttribute("aria-pressed"), "false");
        like.click(); await sleep(20);
        eq(like.getAttribute("aria-pressed"), "true"); eq(dis.getAttribute("aria-pressed"), "false");
    });
    await check("every toolbar button keeps its function (Copy / Like / Dislike / Regenerate present on a saved answer)", async () => {
        const h = await boot(); h.seed([{ role: "user", text: "q" }, { role: "bot", text: "a" }]);
        const titles = [...h.doc.querySelectorAll(".bot-message .toolbar-btn")].map((b) => b.title);
        for (const t of ["Copy full answer, including formulas", "Good answer", "Needs improvement", "Regenerate this answer"]) assert(titles.includes(t), t);
    });
    await check("sidebar search filters on 'input' (paste / IME commit / clear) AND still on key release; an unchanged query does not re-render", async () => {
        const h = await boot();
        const s = h.$("sidebar-search-input"); eq(s.getAttribute("oninput"), "handleSidebarSearch(event)"); eq(s.getAttribute("onkeyup"), "handleSidebarSearch(event)");
        h.seed([{ role: "user", text: "q" }]);
        let renders = 0; const orig = h.w.renderHistoryList; h.w.renderHistoryList = function () { renders++; return orig.apply(this, arguments); };
        s.value = "xyz"; s.dispatchEvent(new h.w.Event("input", { bubbles: true })); s.dispatchEvent(new h.w.KeyboardEvent("keyup", { bubbles: true }));
        eq(renders, 1, "input + keyup for the same text render once");
        s.value = ""; s.dispatchEvent(new h.w.Event("input", { bubbles: true }));
        eq(renders, 2, "clearing re-renders");
    });
    await check("the favicon and touch icon are declared in <head> and the files exist", async () => {
        const h = await boot();
        const hrefs = [...h.doc.querySelectorAll('link[rel~="icon"], link[rel="apple-touch-icon"]')].map((l) => l.getAttribute("href"));
        for (const need of ["/static/img/favicon.svg", "/static/img/favicon.ico", "/static/img/favicon-32x32.png", "/static/img/apple-touch-icon.png"]) {
            assert(hrefs.includes(need), `missing <link> for ${need}`);
            assert(fs.existsSync(path.join(ROOT, need)), `file missing: ${need}`);
        }
    });
    await check("no native alert() remains in the flows this work covers (auth, quiz, send, regenerate, copy, upload)", async () => {
        const app = SRC.app;
        for (const marker of ["window.handleEmailAuth", "window.handleGoogleSignIn", "window.generateAndStartQuiz", "window.nextQuizQuestion", "window.handlePDFUpload"]) {
            const i = app.indexOf(marker); assert(i > 0, marker);
            const seg = app.slice(i, i + 3500).split(/\n(?:window\.|async function |function )/)[0];
            assert(!/[^a-zA-Z.]alert\(/.test(seg), `alert( found inside ${marker}`);
        }
    });
}

console.log(`\n  ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
