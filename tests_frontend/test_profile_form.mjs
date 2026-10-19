/* ============================================================
   KLORIZ - STUDENT PROFILE FORM (jsdom)
   ============================================================
   Runs the REAL templates/index.html and REAL static/js/app.js (plus the
   same companion scripts the other jsdom suites load) in jsdom. Stubbed:
   Supabase and fetch. The fake /api/profile records every request, so the
   assertions are about what the form actually SENDS and SHOWS.

   Covers: seven individual classes (single choice), stream visible only
   for Classes 9-12 and cleared when leaving them, the exact class/stream
   in the PUT body, validation, failed/duplicate saves, success only after
   the server confirms, legacy grouped profiles (never mapped), first-run
   onboarding, accessibility semantics, scope guards (no extra fields) and
   the CSS contract (local teal tokens, no blue/indigo, responsive grid).

   WHAT THIS FILE CANNOT PROVE: real layout, wrapping, colours or tap
   targets in a browser - jsdom has no layout engine. Those need a visual
   check in a real browser (see the delivery notes).

   Run: node tests_frontend/test_profile_form.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

let passed = 0, failed = 0;
const failures = [];
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; failures.push({ name, e }); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const MARKED_SRC = fs.readFileSync(path.join(HERE, "node_modules", "marked", "lib", "marked.umd.js"), "utf8");
const SRC = {
    stream: read("static/js/stream-render.js"),
    purify: read("static/js/vendor/purify.min.js"),
    app: read("static/js/app.js"),
    store: read("static/js/notes-store.js"),
    notes: read("static/js/notes.js"),
    nav: read("static/js/nav-views.js"),
};
const TEMPLATE = read("templates/index.html");
const CSS = read("static/css/style.css");

const CLASSES = ["Class 6", "Class 7", "Class 8", "Class 9", "Class 10", "Class 11", "Class 12"];
const NO_STREAM = ["Class 6", "Class 7", "Class 8"];
const WITH_STREAM = ["Class 9", "Class 10", "Class 11", "Class 12"];
const STREAMS = ["Science (বিজ্ঞান)", "Commerce (ব্যবসায় শিক্ষা)", "Arts (মানবিক)"];
const SAVED_CONFIRM_WAIT_MS = 850; // > PROFILE_SAVED_CONFIRM_MS (700) in app.js

/* ------------------------------------------------------------
   Boot the real app with a controllable fake /api/profile
   ------------------------------------------------------------ */
async function boot({ profile = { name: "Rahman Khan", user_class: "Class 10", stream: "Science (বিজ্ঞান)" }, putHandler } = {}) {
    const html = TEMPLATE.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kloriz.test/" });
    const w = dom.window;
    w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    w.TextEncoder = TextEncoder; w.TextDecoder = TextDecoder; w.MessageChannel = MessageChannel;
    w.alert = () => {}; w.confirm = () => true;
    Object.defineProperty(w.navigator, "onLine", { get: () => true, configurable: true });
    w.indexedDB = undefined;

    const backend = { profile, puts: [], gets: 0, putHandler: null, hold: null };
    const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
    backend.putHandler = putHandler || ((entries) => json(200, { profile: {
        user_id: "user-1", name: entries.name, user_class: entries.user_class, stream: entries.stream || null,
    } }));

    w.fetch = (url, opts = {}) => {
        const method = (opts.method || "GET").toUpperCase();
        if (url === "/api/profile" && method === "GET") {
            backend.gets++;
            return Promise.resolve(backend.profile ? json(200, { profile: backend.profile }) : json(404, { detail: "none" }));
        }
        if (url === "/api/profile" && method === "PUT") {
            const entries = Object.fromEntries(opts.body.entries());
            backend.puts.push({ entries, headers: opts.headers || {} });
            if (backend.hold) return backend.hold.then(() => backend.putHandler(entries));
            return Promise.resolve().then(() => backend.putHandler(entries));
        }
        return Promise.resolve(json(404, { detail: "nope" }));
    };

    const env = { session: { access_token: "tok", user: { id: "user-1", email: "r@example.com" } } };
    const query = { select: () => query, order: async () => ({ data: [], error: null }), upsert: async () => ({ error: null }), delete: () => query, eq: async () => ({ error: null }) };
    w.supabase = { createClient: () => ({
        auth: {
            getSession: async () => ({ data: { session: env.session } }),
            onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
            signOut: async () => ({}),
        },
        from: () => query,
    }) };
    w.eval(MARKED_SRC);
    w.Element.prototype.getClientRects = function () { return this.isConnected ? [{}] : []; };
    w.MathJax = Object.assign(w.MathJax || {}, { typesetPromise: async () => {}, typesetClear: () => {}, startup: { promise: Promise.resolve() } });

    const add = (src) => { const s = w.document.createElement("script"); s.textContent = src; w.document.body.appendChild(s); };
    add(SRC.stream); add(SRC.purify); add(SRC.app);
    w.openAuthModal = () => {};
    add(SRC.store); add(SRC.notes); add(SRC.nav);
    w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
    await sleep(60);

    const doc = w.document;
    const $ = (id) => doc.getElementById(id);
    const api = {
        w, doc, env, backend, $,
        modal: $("profile-modal"),
        name: $("profile-name-input"),
        saveBtn: $("profile-save-btn"),
        error: $("profile-error-msg"),
        status: $("profile-status-msg"),
        streamGroup: $("profile-stream-group"),
        isHidden: (el) => el.classList.contains("hidden"),
        radios: (group) => [...doc.querySelectorAll(`#${group} input[type="radio"]`)],
        checked: (group) => { const c = doc.querySelector(`#${group} input[type="radio"]:checked`); return c ? c.value : null; },
        pick: (group, value) => { const r = api.radios(group).find((x) => x.value === value); if (!r) throw new Error(`no ${value} in ${group}`); r.click(); },
        pickClass: (v) => api.pick("profile-class-group", v),
        pickStream: (v) => api.pick("profile-stream-pills", v),
        open: async (opts) => { w.openProfileModal(opts); await sleep(70); },
        save: async () => { const p = w.saveProfile(); await sleep(20); return p; },
        settle: () => sleep(40),
    };
    return api;
}

/* =============================================================
   A. Template contract (static, no scripts)
   ============================================================= */
console.log("\nA. Template contract");
{
    const dom = new JSDOM(TEMPLATE.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, ""));
    const doc = dom.window.document;
    const modal = doc.getElementById("profile-modal");
    const radioValues = (g) => [...doc.querySelectorAll(`#${g} input[type="radio"]`)].map((r) => r.value);

    await check("header shows the profile icon, 'Your Profile' and the required subtitle", () => {
        assert(modal.querySelector(".profile-avatar svg"), "avatar icon");
        eq(doc.getElementById("profile-modal-title-text").textContent.trim(), "Your Profile");
        eq(doc.getElementById("profile-modal-subtitle").textContent.trim(), "Manage your academic information and preferences");
    });
    await check("Full Name is a real <label for> bound to the text input (maxlength 100 preserved)", () => {
        const label = modal.querySelector('label[for="profile-name-input"]');
        assert(label, "label present");
        eq(label.textContent.trim(), "Full Name");
        const input = doc.getElementById("profile-name-input");
        eq(input.type, "text"); eq(input.getAttribute("maxlength"), "100");
    });
    await check("exactly seven individual class options, Class 6 ... Class 12, in order", () => {
        eq(radioValues("profile-class-group"), CLASSES);
    });
    await check("no grouped class option exists anywhere in the form", () => {
        const text = modal.textContent;
        for (const g of ["Class 6-8", "Class 9-10", "Class 11-12", "(SSC)", "(HSC)"]) assert(!text.includes(g), `grouped label found: ${g}`);
        assert(!/Class \d+\s*[-\u2013]\s*\d+/.test(doc.getElementById("profile-class-group").outerHTML), "range-style class in markup");
    });
    await check("stream offers Science, Commerce, Arts with the existing bilingual values", () => {
        eq(radioValues("profile-stream-pills"), STREAMS);
        const labels = [...doc.querySelectorAll("#profile-stream-pills .profile-choice-text")].map((e) => e.textContent);
        eq(labels, STREAMS);
    });
    await check("class and stream are single-choice radiogroups with accessible names", () => {
        for (const [id, label] of [["profile-class-group", "profile-class-label"], ["profile-stream-pills", "profile-stream-label"]]) {
            const g = doc.getElementById(id);
            eq(g.getAttribute("role"), "radiogroup");
            eq(g.getAttribute("aria-labelledby"), label);
            assert(doc.getElementById(label).textContent.trim().length > 0, "label text");
        }
        const classNames = new Set([...doc.querySelectorAll("#profile-class-group input")].map((r) => r.name));
        const streamNames = new Set([...doc.querySelectorAll("#profile-stream-pills input")].map((r) => r.name));
        eq(classNames.size, 1, "one radio name for classes"); eq(streamNames.size, 1, "one radio name for streams");
        assert([...classNames][0] !== [...streamNames][0], "class and stream must be separate groups");
    });
    await check("every option is a <label> wrapping a native radio, with a checkmark icon", () => {
        const choices = [...modal.querySelectorAll(".profile-choice")];
        eq(choices.length, 10);
        for (const c of choices) {
            eq(c.tagName, "LABEL");
            assert(c.querySelector('input[type="radio"]'), "native radio");
            assert(c.querySelector(".profile-choice-check"), "checkmark svg");
            assert(c.querySelector(".profile-choice-check").getAttribute("aria-hidden") === "true", "decorative check hidden from AT");
        }
    });
    await check("error is role=alert, status is a polite live region that exists up-front", () => {
        eq(doc.getElementById("profile-error-msg").getAttribute("role"), "alert");
        const st = doc.getElementById("profile-status-msg");
        eq(st.getAttribute("role"), "status"); eq(st.getAttribute("aria-live"), "polite");
        eq(st.textContent, "");
    });
    await check("the error message sits above the fields (always in view), the status message by the Save button", () => {
        const pos = (id) => { const el = doc.getElementById(id); return [...modal.querySelectorAll("*")].indexOf(el); };
        assert(pos("profile-error-msg") < pos("profile-name-input"), "error must precede the first field");
        assert(pos("profile-status-msg") > pos("profile-stream-pills") && pos("profile-status-msg") < pos("profile-save-btn"), "status sits just above Save");
    });
    await check("Save Changes is a type=button, full-width primary button (not a form submit)", () => {
        const b = doc.getElementById("profile-save-btn");
        eq(b.textContent.trim(), "Save Changes"); eq(b.getAttribute("type"), "button");
        assert(b.classList.contains("btn-primary"), "reuses the existing white primary button");
        assert(!modal.querySelector("form"), "no <form>: selecting options can never submit");
    });
    await check("SCOPE GUARD: no Email, Preferred Language or Learning Goals field was added", () => {
        assert(!modal.querySelector('input[type="email"]'), "email input");
        assert(!modal.querySelector("select"), "language select");
        assert(!/Preferred Language|Learning Goals|Email/i.test(modal.textContent), "extra field labels");
        eq([...modal.querySelectorAll("input")].filter((i) => i.type !== "radio").length, 1, "only the name text input besides radios");
    });
    await check("existing IDs other code depends on are preserved", () => {
        for (const id of ["profile-modal", "profile-modal-title-text", "profile-error-msg", "profile-name-input",
            "profile-class-group", "profile-stream-group", "profile-stream-pills", "profile-save-btn", "profile-class-label", "profile-stream-label"]) {
            assert(doc.getElementById(id), `missing #${id}`);
        }
        assert(doc.querySelector("#profile-modal .close-modal-btn"), "close button");
    });
}

/* =============================================================
   B. Class selection
   ============================================================= */
console.log("\nB. Class selection");
await check("an existing profile loads with its exact class and stream selected", async () => {
    const t = await boot();
    await t.open();
    eq(t.checked("profile-class-group"), "Class 10");
    eq(t.checked("profile-stream-pills"), "Science (বিজ্ঞান)");
    eq(t.name.value, "Rahman Khan");
    assert(!t.isHidden(t.streamGroup), "stream visible for Class 10");
});
await check("only one class can be selected at a time, and the selection moves", async () => {
    const t = await boot();
    await t.open();
    for (const c of CLASSES) {
        t.pickClass(c);
        eq(t.radios("profile-class-group").filter((r) => r.checked).map((r) => r.value), [c], `after picking ${c}`);
    }
});
await check("exactly one stream can be selected at a time", async () => {
    const t = await boot();
    await t.open();
    for (const s of STREAMS) {
        t.pickStream(s);
        eq(t.radios("profile-stream-pills").filter((r) => r.checked).map((r) => r.value), [s]);
    }
});

/* =============================================================
   C. Stream visibility rules
   ============================================================= */
console.log("\nC. Stream visibility");
for (const c of CLASSES) {
    await check(`${c}: stream ${WITH_STREAM.includes(c) ? "is shown" : "is hidden"}`, async () => {
        const t = await boot();
        await t.open();
        t.pickClass(c);
        eq(t.isHidden(t.streamGroup), NO_STREAM.includes(c), c);
    });
}
await check("switching from Class 9-12 to Class 6-8 clears the stream", async () => {
    const t = await boot();
    await t.open();
    t.pickClass("Class 9"); t.pickStream("Arts (মানবিক)");
    eq(t.checked("profile-stream-pills"), "Arts (মানবিক)");
    t.pickClass("Class 6");
    assert(t.isHidden(t.streamGroup), "hidden");
    eq(t.checked("profile-stream-pills"), null, "stream cleared, not just hidden");
    t.pickClass("Class 9");
    eq(t.checked("profile-stream-pills"), null, "the cleared stream does not silently come back");
});
await check("moving between Classes 9-12 keeps the chosen stream", async () => {
    const t = await boot();
    await t.open();
    t.pickClass("Class 9"); t.pickStream("Commerce (ব্যবসায় শিক্ষা)");
    t.pickClass("Class 12");
    eq(t.checked("profile-stream-pills"), "Commerce (ব্যবসায় শিক্ষা)");
});
await check("a Class 6-8 profile opens with the stream hidden and nothing selected", async () => {
    const t = await boot({ profile: { name: "Nila", user_class: "Class 7", stream: null } });
    await t.open();
    eq(t.checked("profile-class-group"), "Class 7");
    assert(t.isHidden(t.streamGroup), "hidden");
    eq(t.checked("profile-stream-pills"), null);
});

/* =============================================================
   D. What is actually saved
   ============================================================= */
console.log("\nD. Saved payload");
for (const c of NO_STREAM) {
    await check(`${c} saves the exact class with an empty stream`, async () => {
        const t = await boot();
        await t.open();
        t.pickStream("Arts (মানবিক)");        // a stale stream from before
        t.pickClass(c);
        await t.save(); await t.settle();
        eq(t.backend.puts.length, 1);
        const sent = t.backend.puts[0].entries;
        eq(sent.user_class, c); eq(sent.stream, "", "no stream for classes 6-8");
        eq(sent.name, "Rahman Khan");
        eq(t.backend.puts[0].headers.Authorization, "Bearer tok");
    });
}
for (const c of WITH_STREAM) {
    for (const s of STREAMS) {
        await check(`${c} + ${s} saves exactly that pair`, async () => {
            const t = await boot();
            await t.open();
            t.pickClass(c); t.pickStream(s);
            await t.save(); await t.settle();
            eq(t.backend.puts.length, 1);
            eq([t.backend.puts[0].entries.user_class, t.backend.puts[0].entries.stream], [c, s]);
        });
    }
}
await check("save-time guard: a stale checked stream is never sent for Class 6-8", async () => {
    // updateProfileStreamVisibility() already clears the stream when a Class 6-8 is picked, so
    // force the inconsistent DOM state directly to prove saveProfile() does not rely on that.
    const t = await boot();
    await t.open();
    t.pickClass("Class 7");
    t.radios("profile-stream-pills").find((r) => r.value === "Arts (মানবিক)").checked = true;
    await t.save(); await t.settle();
    eq(t.backend.puts.length, 1);
    eq([t.backend.puts[0].entries.user_class, t.backend.puts[0].entries.stream], ["Class 7", ""]);
});
await check("the saved class is never a grouped band", async () => {
    for (const c of CLASSES) {
        const t = await boot();
        await t.open();
        t.pickClass(c);
        if (WITH_STREAM.includes(c)) t.pickStream(STREAMS[0]);
        await t.save(); await t.settle();
        assert(!/\d-\d|SSC|HSC/.test(t.backend.puts[0].entries.user_class), `grouped value sent for ${c}`);
    }
});
await check("the name is trimmed and an edited name is what gets saved", async () => {
    const t = await boot();
    await t.open();
    t.name.value = "   Fatema Begum  ";
    await t.save(); await t.settle();
    eq(t.backend.puts[0].entries.name, "Fatema Begum");
});
await check("selecting classes and streams never sends a request by itself", async () => {
    const t = await boot();
    await t.open();
    for (const c of CLASSES) t.pickClass(c);
    for (const s of STREAMS) t.pickStream(s);
    await sleep(60);
    eq(t.backend.puts.length, 0, "no PUT without pressing Save");
});
await check("Enter in the name field saves", async () => {
    const t = await boot();
    await t.open();
    t.name.dispatchEvent(new t.w.KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
    await sleep(60);
    eq(t.backend.puts.length, 1);
});

/* =============================================================
   E. Validation (client-side feedback; backend stays authoritative)
   ============================================================= */
console.log("\nE. Validation feedback");
await check("empty name: clear error, aria-invalid, focus on the field, nothing sent", async () => {
    const t = await boot();
    await t.open();
    t.name.value = "   ";
    await t.save();
    assert(!t.isHidden(t.error), "error visible");
    eq(t.error.textContent, "Please enter your name.");
    eq(t.name.getAttribute("aria-invalid"), "true");
    eq(t.w.document.activeElement, t.name);
    eq(t.backend.puts.length, 0);
});
await check("no class chosen (new student): asks for a class and sends nothing", async () => {
    const t = await boot({ profile: null });
    await t.settle();
    t.name.value = "New Student";
    await t.save();
    eq(t.error.textContent, "Please select your class.");
    eq(t.backend.puts.length, 0);
});
await check("Class 9-12 without a stream: asks for a stream and sends nothing", async () => {
    const t = await boot();
    await t.open();
    t.pickClass("Class 11");
    t.pickStream("Science (বিজ্ঞান)");
    t.radios("profile-stream-pills").forEach((r) => { r.checked = false; });
    await t.save();
    eq(t.error.textContent, "Please select your stream.");
    eq(t.backend.puts.length, 0);
});
await check("changing a selection hides a stale validation error", async () => {
    const t = await boot();
    await t.open();
    t.name.value = "";
    await t.save();
    assert(!t.isHidden(t.error));
    t.pickClass("Class 8");
    assert(t.isHidden(t.error), "error cleared by selecting");
    eq(t.name.hasAttribute("aria-invalid"), false);
});

/* =============================================================
   F. Save outcome: failure, duplicate, success
   ============================================================= */
console.log("\nF. Save outcome");
await check("server error: message shown, NO success text, button usable again, profile unchanged", async () => {
    const t = await boot({ putHandler: () => ({ ok: false, status: 422, json: async () => ({ detail: "This class does not have a stream." }) }) });
    await t.open();
    t.pickClass("Class 6");
    const before = JSON.stringify(t.w.eval("currentProfile"));
    await t.save(); await t.settle();
    eq(t.error.textContent, "This class does not have a stream.");
    assert(!t.isHidden(t.error));
    eq(t.status.textContent, "", "no success announcement");
    eq(t.saveBtn.disabled, false); eq(t.saveBtn.textContent, "Save Changes");
    eq(JSON.stringify(t.w.eval("currentProfile")), before, "currentProfile untouched on failure");
    assert(!t.isHidden(t.modal), "modal stays open so the student can retry");
});
await check("network failure: generic error, no success, button re-enabled", async () => {
    const t = await boot({ putHandler: () => { throw new Error("offline"); } });
    await t.open();
    await t.save(); await t.settle();
    eq(t.error.textContent, "Could not save your profile. Please try again.");
    eq(t.status.textContent, ""); eq(t.saveBtn.disabled, false);
});
await check("malformed 200 response without a profile is treated as a failure", async () => {
    const t = await boot({ putHandler: () => ({ ok: true, status: 200, json: async () => ({}) }) });
    await t.open();
    await t.save(); await t.settle();
    eq(t.status.textContent, ""); assert(!t.isHidden(t.error));
});
await check("duplicate submissions are blocked while a save is in flight", async () => {
    const t = await boot();
    let release; t.backend.hold = new Promise((r) => { release = r; });
    await t.open();
    t.pickClass("Class 8");
    const first = t.w.saveProfile();
    await sleep(20);
    eq(t.saveBtn.disabled, true); eq(t.saveBtn.textContent, "Saving...");
    // Double click / Enter while pending. Deliberately NOT awaited: if the guard were
    // missing these would queue behind the held request and hang instead of failing cleanly.
    t.w.saveProfile(); t.w.saveProfile();
    t.saveBtn.click();
    await sleep(40);
    eq(t.backend.puts.length, 1, "exactly one PUT while the first is pending");
    eq(t.status.textContent, "", "no success before the server answers");
    release(); await first; await t.settle();
    eq(t.backend.puts.length, 1);
});
await check("success is announced only AFTER the server confirms, then the form closes", async () => {
    const t = await boot();
    let release; t.backend.hold = new Promise((r) => { release = r; });
    await t.open();
    t.pickClass("Class 8");
    const p = t.w.saveProfile();
    await sleep(30);
    eq(t.status.textContent, "", "nothing announced while pending");
    release(); await p; await t.settle();
    eq(t.status.textContent, "Profile saved.");
    assert(!t.isHidden(t.modal), "confirmation visible before the modal closes");
    await sleep(SAVED_CONFIRM_WAIT_MS);
    assert(t.isHidden(t.modal), "modal closes after the confirmation");
});
await check("no duplicate save while the success confirmation is showing", async () => {
    const t = await boot();
    await t.open();
    await t.save(); await t.settle();
    eq(t.status.textContent, "Profile saved.");
    eq(t.saveBtn.disabled, true);
    await t.w.saveProfile(); t.saveBtn.click();
    await sleep(30);
    eq(t.backend.puts.length, 1);
});
await check("after success the sidebar profile row reflects the saved class/stream (no page reload)", async () => {
    const t = await boot();
    await t.open();
    t.pickClass("Class 12"); t.pickStream("Arts (মানবিক)");
    await t.save(); await t.settle();
    eq(t.$("user-display-meta").textContent, "Class 12 · Arts (মানবিক)");
    eq(t.w.eval("currentProfile.user_class"), "Class 12");
});
await check("the form shows what the SERVER stored, not what was typed", async () => {
    const t = await boot({ putHandler: (e) => ({ ok: true, status: 200, json: async () => ({ profile: {
        user_id: "user-1", name: "Server Cleaned Name", user_class: "Class 9", stream: "Commerce (ব্যবসায় শিক্ষা)" } }) }) });
    await t.open();
    t.pickClass("Class 7");
    t.name.value = "typed name";
    await t.save(); await t.settle();
    eq(t.checked("profile-class-group"), "Class 9"); eq(t.checked("profile-stream-pills"), "Commerce (ব্যবসায় শিক্ষা)");
    eq(t.name.value, "Server Cleaned Name");
});
await check("a saved profile reopens correctly (persistence round trip through the fake server)", async () => {
    const t = await boot();
    await t.open();
    t.pickClass("Class 9"); t.pickStream("Commerce (ব্যবসায় শিক্ষা)");
    await t.save(); await sleep(SAVED_CONFIRM_WAIT_MS);
    await t.open();
    eq(t.checked("profile-class-group"), "Class 9"); eq(t.checked("profile-stream-pills"), "Commerce (ব্যবসায় শিক্ষা)");
    eq(t.saveBtn.disabled, false, "button re-enabled on reopen");
    eq(t.status.textContent, "", "old confirmation cleared on reopen");
});
await check("closing the modal during the confirmation cancels the pending auto-close safely", async () => {
    const t = await boot();
    await t.open();
    await t.save(); await t.settle();
    t.w.closeProfileModal();
    await t.open();                                  // reopened quickly
    await sleep(SAVED_CONFIRM_WAIT_MS);
    assert(!t.isHidden(t.modal), "the stale timer must not close a re-opened modal");
});

/* =============================================================
   G. Legacy grouped profiles (never mapped)
   ============================================================= */
console.log("\nG. Legacy grouped profiles");
await check("legacy 'Class 9-10 (SSC)': no class pre-selected, hint asks to choose, stream hidden", async () => {
    const t = await boot({ profile: { name: "Old Student", user_class: "Class 9-10 (SSC)", stream: "Science (বিজ্ঞান)" } });
    await t.open();
    eq(t.checked("profile-class-group"), null, "never guessed as Class 9 or Class 10");
    eq(t.$("profile-class-hint").textContent, "Please choose your exact class");
    assert(t.isHidden(t.streamGroup), "stream hidden until a class 9-12 is chosen");
});
await check("legacy student picks Class 10: saved stream is restored, then saved as exact Class 10", async () => {
    const t = await boot({ profile: { name: "Old Student", user_class: "Class 9-10 (SSC)", stream: "Science (বিজ্ঞান)" } });
    await t.open();
    t.pickClass("Class 10");
    assert(!t.isHidden(t.streamGroup));
    eq(t.checked("profile-stream-pills"), "Science (বিজ্ঞান)");
    await t.save(); await t.settle();
    eq([t.backend.puts[0].entries.user_class, t.backend.puts[0].entries.stream], ["Class 10", "Science (বিজ্ঞান)"]);
});
await check("legacy student cannot save without choosing a class (no silent mapping)", async () => {
    const t = await boot({ profile: { name: "Old Student", user_class: "Class 11-12 (HSC)", stream: "Arts (মানবিক)" } });
    await t.open();
    await t.save();
    eq(t.error.textContent, "Please select your class.");
    eq(t.backend.puts.length, 0);
});
await check("legacy 'Class 6-8': nothing pre-selected; choosing Class 7 saves with no stream", async () => {
    const t = await boot({ profile: { name: "Old Student", user_class: "Class 6-8", stream: null } });
    await t.open();
    eq(t.checked("profile-class-group"), null);
    t.pickClass("Class 7");
    await t.save(); await t.settle();
    eq([t.backend.puts[0].entries.user_class, t.backend.puts[0].entries.stream], ["Class 7", ""]);
});
await check("legacy label still shows in the sidebar until the student re-saves", async () => {
    const t = await boot({ profile: { name: "Old Student", user_class: "Class 9-10 (SSC)", stream: "Science (বিজ্ঞান)" } });
    eq(t.$("user-display-meta").textContent, "Class 9-10 (SSC) · Science (বিজ্ঞান)");
});

/* =============================================================
   H. First-run onboarding keeps its behaviour
   ============================================================= */
console.log("\nH. Onboarding");
await check("a student with no profile gets the required onboarding form", async () => {
    const t = await boot({ profile: null });
    await t.settle();
    assert(!t.isHidden(t.modal), "auto-opened");
    eq(t.modal.getAttribute("data-required"), "true", "still non-dismissible");
    eq(t.$("profile-modal-title-text").textContent, "Set up your learning space");
    eq(t.saveBtn.textContent, "Get Started");
    eq(t.$("profile-modal-subtitle").textContent, "Choose your class so Kloriz matches your syllabus");
});
await check("onboarding starts with nothing pre-selected and the stream hidden", async () => {
    const t = await boot({ profile: null });
    await t.settle();
    eq(t.checked("profile-class-group"), null); eq(t.checked("profile-stream-pills"), null);
    assert(t.isHidden(t.streamGroup));
});
await check("onboarding save creates the profile with the exact class and stream", async () => {
    const t = await boot({ profile: null });
    await t.settle();
    t.name.value = "Karim Uddin";
    t.pickClass("Class 11"); t.pickStream("Commerce (ব্যবসায় শিক্ষা)");
    await t.save(); await t.settle();
    eq(t.backend.puts[0].entries, { name: "Karim Uddin", user_class: "Class 11", stream: "Commerce (ব্যবসায় শিক্ষা)" });
});
await check("editing mode: title/subtitle/button revert to the profile wording", async () => {
    const t = await boot();
    await t.open();
    eq(t.modal.getAttribute("data-required"), "false");
    eq(t.$("profile-modal-title-text").textContent, "Your Profile");
    eq(t.$("profile-modal-subtitle").textContent, "Manage your academic information and preferences");
    eq(t.saveBtn.textContent, "Save Changes");
});

/* =============================================================
   I. CSS contract (static)
   ============================================================= */
console.log("\nI. CSS contract");
const cssStart = CSS.indexOf("/* ---- PROFILE MODAL (redesigned)");
const cssEnd = CSS.indexOf("/* PHASE 6A CORRECTION: pill-button choice groups");
const PROFILE_CSS = CSS.slice(cssStart, cssEnd);
await check("the profile CSS block was found", () => { assert(cssStart > 0 && cssEnd > cssStart, "markers"); });
await check("teal selection accent is defined LOCALLY on .profile-card for dark, light and OS-light", () => {
    assert(/\.profile-card\s*\{[^}]*--profile-accent:\s*#2dd4bf/s.test(PROFILE_CSS), "dark teal");
    assert(/:root\[data-theme="light"\]\s+\.profile-card\s*\{[^}]*--profile-accent:\s*#0f766e/s.test(PROFILE_CSS), "light teal");
    assert(/prefers-color-scheme:\s*light\)\s*\{\s*:root:not\(\[data-theme\]\)\s+\.profile-card/s.test(PROFILE_CSS), "OS-light");
});
await check("the global theme tokens were NOT changed to add teal", () => {
    const tokens = read("static/css/tokens.css");
    assert(!/teal|--profile-accent|#2dd4bf|#0f766e/i.test(tokens), "tokens.css must stay untouched");
});
await check("selected card has a teal border, a tint and a visible checkmark (not colour alone)", () => {
    assert(/:checked \+ \.profile-choice-box\s*\{[^}]*border-color:\s*var\(--profile-accent\)/s.test(PROFILE_CSS));
    assert(/:checked \+ \.profile-choice-box \.profile-choice-check\s*\{[^}]*opacity:\s*1/s.test(PROFILE_CSS));
});
await check("no blue/indigo/purple is used in the profile styles", () => {
    assert(!/var\(--(blue|indigo)-|var\(--color-accent(-quiz)?(-hover)?\)|#6366f1|#4f46e5|#3730a3|#7c3aed|purple|gradient\(\s*(135|to)/i.test(PROFILE_CSS), "blue/indigo/purple reference");
});
await check("surfaces, borders, text and focus all come from the existing semantic tokens", () => {
    for (const t of ["--color-surface-input", "--color-border-input", "--color-text-primary", "--color-text-muted", "--color-focus"]) {
        assert(PROFILE_CSS.includes(`var(${t})`), `expected ${t}`);
    }
});
await check("keyboard focus is visible on the card and the radio is not display:none", () => {
    assert(/\.profile-choice-input:focus-visible \+ \.profile-choice-box\s*\{[^}]*outline:\s*2px solid/s.test(PROFILE_CSS));
    assert(!/\.profile-choice-input\s*\{[^}]*display:\s*none/s.test(PROFILE_CSS), "must stay focusable");
});
await check("class grid is 4 columns on wide, 3 on narrow, 2 on very narrow; stream collapses to 1", () => {
    assert(/\.profile-class-grid\s*\{\s*grid-template-columns:\s*repeat\(4,/s.test(PROFILE_CSS));
    assert(/max-width:\s*559px\)\s*\{[^@]*\.profile-class-grid\s*\{\s*grid-template-columns:\s*repeat\(3,/s.test(PROFILE_CSS));
    assert(/max-width:\s*399px\)\s*\{\s*\.profile-class-grid\s*\{\s*grid-template-columns:\s*repeat\(2,/s.test(PROFILE_CSS));
    assert(/\.profile-stream-grid\s*\{\s*grid-template-columns:\s*repeat\(3,/s.test(PROFILE_CSS));
    assert(/\.profile-stream-grid\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/s.test(PROFILE_CSS));
});
await check("cards cannot overflow the modal: minmax(0,1fr) columns, modal scrolls internally", () => {
    assert(!/repeat\(\d, 1fr\)/.test(PROFILE_CSS), "plain 1fr lets long labels overflow");
    assert(/\.profile-card\s*\{[^}]*max-height:\s*92dvh;[^}]*overflow-y:\s*auto/s.test(PROFILE_CSS));
});
await check("selection causes no layout shift: check is absolutely positioned, side padding reserved", () => {
    assert(/\.profile-choice-check\s*\{[^}]*position:\s*absolute/s.test(PROFILE_CSS));
    assert(/\.profile-choice-box\s*\{[^}]*padding:\s*8px 26px/s.test(PROFILE_CSS));
});
await check("touch targets are at least 44px tall", () => {
    assert(/\.profile-choice-box\s*\{[^}]*min-height:\s*44px/s.test(PROFILE_CSS));
});
await check("the Save button is full width with disabled styling (btn-primary base)", () => {
    assert(/\.profile-save-btn:disabled\s*\{[^}]*cursor:\s*not-allowed/s.test(PROFILE_CSS));
    assert(/\.btn-primary\s*\{[^}]*width:\s*100%/s.test(CSS), "base .btn-primary is already full width");
});

/* ============================================================ */
console.log(`\n${passed} passed, ${failed} failed`);
if (failed) {
    console.log("\nFailures:");
    for (const f of failures) console.log(` - ${f.name}\n   ${f.e.stack || f.e.message}`);
    process.exit(1);
}
