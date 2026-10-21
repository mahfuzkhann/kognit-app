/* ============================================================
   KOGNIT PHASE 9B — NAVIGATION TESTS
   ============================================================
   Loads the REAL template and REAL static/js/nav-views.js in
   jsdom. app.js itself is stubbed (it needs a live Supabase
   session to boot) with the exact accessor-function contract
   nav-views.js actually depends on - see the PHASE 9B comment
   block in app.js for that contract.

   Run: node tests_frontend/test_nav_views.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

let passed = 0, failed = 0;
const failures = [];

async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; failures.push({ name, e }); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (a !== b) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }

function makeFakeProjects() {
    return [
        { id: "p1", title: "Physics Revision", chats: [{ id: "c1", title: "Newton's Laws" }, { id: "c2", title: "Momentum" }] },
        { id: "p2", title: "Bangla Essays", chats: [] }
    ];
}

async function buildDom({ projects = makeFakeProjects(), activeProjectId = "p1", session = { access_token: "tok" }, snapshot = { strengths: [], needs_practice: [] }, loadState = "ready", deleteResult = true, renameResult = { ok: true } } = {}) {
    let html = fs.readFileSync(path.join(ROOT, "templates", "index.html"), "utf8");
    html = html.replace(/<script src="https:\/\/[^"]*"[^>]*><\/script>/g, "");
    for (const f of ["app.js", "ui-core.js", "stream-render.js", "theme.js", "nav-views.js"]) {
        html = html.replace(new RegExp(`<script src="/static/js/${f}"></script>`), "");
    }

    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kognit.test/" });
    const { window } = dom;

    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

    // ---- Stub of app.js's PHASE 9B contract, matching its real signatures ----
    const calls = { createNewProject: [], createNewChat: [], deleteProject: [], renameProject: [], startRenamingProject: [], openProjectAndChat: [], openQuizModal: 0 };
    window.getProjectsSnapshot = () => projects;
    window.getActiveIds = () => ({ activeProjectId, activeChatId: null });
    // NOTEBOOKS contract (see the NOTEBOOKS comment block in app.js)
    window.getDefaultProjectId = () => "proj_recent_default";
    window.getProjectsLoadState = () => loadState;
    window.reloadProjectsFromDatabase = async () => {};
    window.createNewProject = (title) => {
        calls.createNewProject.push(title);
        projects.unshift({ id: "pNew", title, chats: [{ id: "cNew", title: "New Chat", messages: [] }] });
        activeProjectId = "pNew";
        return Promise.resolve({ cachedLocally: true, savedRemotely: true, signedIn: true });
    };
    window.renameProject = (projId, title) => { calls.renameProject.push([projId, title]); return Promise.resolve(renameResult); };
    window.createNewChat = (projId) => { calls.createNewChat.push(projId); };
    window.deleteProject = (projId, options) => { calls.deleteProject.push([projId, options]); return Promise.resolve(deleteResult); };
    window.startRenamingProject = (projId) => { calls.startRenamingProject.push(projId); };
    window.openProjectAndChat = (projId, chatId) => { calls.openProjectAndChat.push([projId, chatId]); };
    window.openQuizModal = () => { calls.openQuizModal++; };
    window.renderHistoryList = () => {};
    window._spOpenQuizForTopic = (subject, topic) => { calls.openQuizForTopic = [subject, topic]; };
    window._spGroupBySubject = (entries) => {
        const groups = new Map();
        entries.forEach((e) => {
            if (!groups.has(e.subject)) groups.set(e.subject, new Set());
            groups.get(e.subject).add(e.topic);
        });
        return groups;
    };
    window.supabaseClient = { auth: { getSession: async () => ({ data: { session } }) } };
    window._spAuthedGet = async (url) => {
        if (url === "/api/profile/snapshot") return snapshot;
        throw new Error("unexpected URL in test: " + url);
    };

    const navViewsJs = fs.readFileSync(path.join(ROOT, "static", "js", "nav-views.js"), "utf8");
    const script = window.document.createElement("script");
    script.textContent = navViewsJs;
    window.document.body.appendChild(script);

    if (window.document.readyState === "loading") {
        await new Promise((resolve) => window.document.addEventListener("DOMContentLoaded", resolve, { once: true }));
    }
    // Let any in-flight async rendering (Quizzes view's fetch) settle.
    await new Promise((r) => setTimeout(r, 10));

    return { dom, window, doc: window.document, calls };
}

console.log("\nTemplate contract");

const rawHtml = fs.readFileSync(path.join(ROOT, "templates", "index.html"), "utf8");

await check("no Home/Scheduled/Plugins nav items exist (Decision 3)", () => {
    assert(!rawHtml.includes('id="nav-tab-home"'));
    assert(!/Scheduled<\/span>/.test(rawHtml));
    assert(!/Plugins<\/span>/.test(rawHtml));
});

await check("nav rail uses proper ARIA tablist semantics", () => {
    assert(rawHtml.includes('role="tablist"'));
    // Pocket Notes added a fourth tab (below Projects), by approved design.
    assert((rawHtml.match(/role="tab"/g) || []).length === 4, "exactly 4 tabs");
});

console.log("\nSwitching");

await check("Chat is the default active tab on first load", async () => {
    const { doc } = await buildDom();
    assert(doc.getElementById("nav-tab-chat").classList.contains("active"));
    assert(!doc.getElementById("chat-view").classList.contains("hidden"));
    assert(doc.getElementById("quizzes-view").classList.contains("hidden"));
    assert(doc.getElementById("projects-view").classList.contains("hidden"));
});

await check("clicking Quizzes shows the quizzes view and hides chat", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-quizzes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
    assert(!doc.getElementById("quizzes-view").classList.contains("hidden"));
    assert(doc.getElementById("chat-view").classList.contains("hidden"));
    eq(doc.getElementById("nav-tab-quizzes").getAttribute("aria-selected"), "true");
    eq(doc.getElementById("nav-tab-chat").getAttribute("aria-selected"), "false");
});

await check("switching to Quizzes/Projects also hides the chat sidebar history section", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(doc.getElementById("chat-sidebar-section").classList.contains("hidden"),
        "Decision 9B.1: chat history belongs to the Chat destination, must not linger on other tabs");
});

await check("switching back to Chat restores the sidebar history section", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    doc.getElementById("nav-tab-chat").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(!doc.getElementById("chat-sidebar-section").classList.contains("hidden"));
});

await check("active tab persists across a reload (localStorage)", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-quizzes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(window.localStorage.getItem("kognit-active-nav-tab"), "quizzes");
});

console.log("\nKeyboard (ARIA tablist pattern)");

await check("ArrowRight moves from Chat to Quizzes and activates it", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-chat").focus();
    doc.querySelector(".nav-rail").dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    eq(doc.activeElement.id, "nav-tab-quizzes");
    assert(doc.getElementById("nav-tab-quizzes").classList.contains("active"));
});

await check("ArrowLeft wraps from Chat to the LAST tab, Pocket Notes (roving tabindex wraps)", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-chat").focus();
    doc.querySelector(".nav-rail").dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    eq(doc.activeElement.id, "nav-tab-notes");
});

await check("Pocket Notes tab sits below Projects and controls #notes-view", () => {
    const order = [...rawHtml.matchAll(/id="nav-tab-([a-z]+)"/g)].map((m) => m[1]);
    eq(order.join(","), "chat,quizzes,projects,notes");
    assert(rawHtml.includes('aria-controls="notes-view"'), "tab must control #notes-view");
    assert(/id="notes-view"[^>]*role="tabpanel"[^>]*aria-labelledby="nav-tab-notes"/.test(rawHtml), "panel semantics");
});

await check("ArrowRight from Projects moves to Pocket Notes, and Notes wraps back to Chat", async () => {
    const { doc, window } = await buildDom();
    const rail = doc.querySelector(".nav-rail");
    doc.getElementById("nav-tab-projects").focus();
    rail.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    eq(doc.activeElement.id, "nav-tab-notes");
    rail.dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
    eq(doc.activeElement.id, "nav-tab-chat");
});

await check("switching to Pocket Notes shows #notes-view, hides the others and the chat sidebar, and notifies KognitNotes", async () => {
    const { doc, window } = await buildDom();
    let shown = 0;
    window.KognitNotes = { onShow() { shown++; } };
    doc.getElementById("nav-tab-notes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(!doc.getElementById("notes-view").classList.contains("hidden"), "notes view visible");
    assert(doc.getElementById("chat-view").classList.contains("hidden"), "chat view hidden");
    assert(doc.getElementById("chat-sidebar-section").classList.contains("hidden"), "chat history hidden");
    eq(doc.getElementById("nav-tab-notes").getAttribute("aria-selected"), "true");
    eq(shown, 1);
});

await check("Pocket Notes tab works even if notes.js failed to load (no KognitNotes)", async () => {
    const { doc, window } = await buildDom();
    delete window.KognitNotes;
    doc.getElementById("nav-tab-notes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(!doc.getElementById("notes-view").classList.contains("hidden"));
});

await check("only the active tab is in the Tab order (roving tabindex)", async () => {
    const { doc } = await buildDom();
    eq(doc.getElementById("nav-tab-chat").tabIndex, 0);
    eq(doc.getElementById("nav-tab-quizzes").tabIndex, -1);
    eq(doc.getElementById("nav-tab-projects").tabIndex, -1);
});

console.log("\nQuizzes view - reuses existing quiz modal, no duplicate logic");

await check("Start a New Quiz calls the EXISTING openQuizModal(), nothing new", async () => {
    const { doc, window, calls } = await buildDom();
    doc.getElementById("nav-tab-quizzes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
    doc.getElementById("quizzes-view-start-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(calls.openQuizModal, 1);
});

await check("with no learning evidence, shows an honest empty state (no invented history)", async () => {
    const { doc, window } = await buildDom({ snapshot: { strengths: [], needs_practice: [] } });
    doc.getElementById("nav-tab-quizzes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
    const topics = doc.getElementById("quizzes-view-topics");
    assert(topics.textContent.toLowerCase().includes("quiz"), "should explain there's nothing yet, not fabricate topics");
    assert(!topics.querySelector(".quizzes-topic-chip"), "no chips should render when there is no evidence");
});

await check("needs_practice and strengths render as real evidence, from the SAME snapshot endpoint", async () => {
    const snapshot = {
        strengths: [{ subject: "Physics", topic: "Newton's Laws", topic_key: "physics:newton" }],
        needs_practice: [{ subject: "Chemistry", topic: "Stoichiometry", topic_key: "chem:stoich" }]
    };
    const { doc, window } = await buildDom({ snapshot });
    doc.getElementById("nav-tab-quizzes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
    const topics = doc.getElementById("quizzes-view-topics");
    assert(topics.textContent.includes("Physics"));
    assert(topics.textContent.includes("Chemistry"));
});

await check("clicking a topic chip reuses the EXISTING _spOpenQuizForTopic, not a new quiz-start path", async () => {
    const snapshot = { strengths: [], needs_practice: [{ subject: "Chemistry", topic: "Stoichiometry", topic_key: "chem:stoich" }] };
    const { doc, window, calls } = await buildDom({ snapshot });
    doc.getElementById("nav-tab-quizzes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
    doc.querySelector(".quizzes-topic-chip").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(calls.openQuizForTopic[0], "Chemistry");
    eq(calls.openQuizForTopic[1], "Stoichiometry");
});

await check("logged-out student sees an honest login prompt, not a silent failure", async () => {
    const { doc, window } = await buildDom({ session: null });
    doc.getElementById("nav-tab-quizzes").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
    assert(doc.getElementById("quizzes-view-topics").textContent.toLowerCase().includes("log in"));
});

console.log("\nNotebooks view - dedicated view, built from the SAME data (Decision 4)");

const click = (window, el) => el.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
const tick = (ms = 10) => new Promise((r) => setTimeout(r, ms));
const goNotebooks = (doc, window) => click(window, doc.getElementById("nav-tab-projects"));
const cardFor = (doc, text) => Array.from(doc.querySelectorAll(".nb-card")).find((c) => c.textContent.includes(text));
const submitForm = async (window, doc) => {
    doc.getElementById("notebook-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await tick();
};

await check("the nav tab and sidebar are labelled Notebooks; internal ids are unchanged", async () => {
    const { doc } = await buildDom();
    eq(doc.getElementById("nav-tab-projects").textContent.trim(), "Notebooks");
    assert(!rawHtml.includes("<span>Projects</span>"));
});

await check("renders one card per notebook from getProjectsSnapshot(), no second data source", async () => {
    const { doc, window } = await buildDom();
    goNotebooks(doc, window);
    eq(doc.querySelectorAll(".nb-card").length, 2);
    assert(doc.getElementById("projects-view").textContent.includes("Physics Revision"));
    assert(doc.getElementById("projects-view").textContent.includes("Bangla Essays"));
    eq(doc.querySelector(".nb-title").textContent, "Notebooks");
});

await check("the reserved Recent Chats bucket is never listed as a notebook", async () => {
    const projects = [...makeFakeProjects(), { id: "proj_recent_default", title: "Recent Chats", chats: [{ id: "x", title: "t" }] }];
    const { doc, window } = await buildDom({ projects });
    goNotebooks(doc, window);
    eq(doc.querySelectorAll(".nb-card").length, 2);
    assert(!doc.getElementById("projects-view").textContent.includes("Recent Chats"));
});

await check("chat count is DERIVED from real chats, never invented", async () => {
    const { doc, window } = await buildDom();
    goNotebooks(doc, window);
    assert(cardFor(doc, "Physics Revision").querySelector(".nb-card-meta").textContent.includes("2 chats"));
    assert(cardFor(doc, "Bangla Essays").querySelector(".nb-card-meta").textContent.includes("0 chats"));
});

await check("dates come only from real timestamps; nothing is invented when none exist", async () => {
    const projects = [
        { id: "a", title: "No timestamps", chats: [] },
        { id: "b", title: "Server stamp", chats: [], updated_at: new Date(Date.now() - 2 * 86400000).toISOString() },
        { id: "proj_" + (Date.now() - 3 * 86400000), title: "From id", chats: [] }
    ];
    const { doc, window } = await buildDom({ projects });
    goNotebooks(doc, window);
    const none = cardFor(doc, "No timestamps").querySelector(".nb-card-meta").textContent;
    assert(!/updated|created|ago/i.test(none), "no date text without a real timestamp: " + none);
    assert(cardFor(doc, "Server stamp").textContent.includes("Updated 2 days ago"));
    assert(cardFor(doc, "From id").textContent.includes("Created 3 days ago"));
    const all = doc.getElementById("projects-view").textContent.toLowerCase();
    assert(!all.includes("progress") && !all.includes("streak"), "no invented metrics");
});

await check("the current notebook is marked with a text label, not colour alone", async () => {
    const { doc, window } = await buildDom({ activeProjectId: "p1" });
    goNotebooks(doc, window);
    assert(cardFor(doc, "Physics Revision").classList.contains("nb-card--current"));
    assert(cardFor(doc, "Physics Revision").textContent.includes("Current"));
    assert(!cardFor(doc, "Bangla Essays").classList.contains("nb-card--current"));
});

await check("Open shows the notebook's chats (detail view) with Back to Notebooks", async () => {
    const { doc, window } = await buildDom();
    goNotebooks(doc, window);
    click(window, cardFor(doc, "Physics Revision").querySelector(".nb-open-btn"));
    eq(doc.querySelector(".nb-title").textContent, "Physics Revision");
    eq(doc.querySelectorAll(".nb-chat-row").length, 2);
    assert(doc.querySelector(".nb-back-btn").textContent.includes("Back to Notebooks"));
    click(window, doc.querySelector(".nb-back-btn"));
    eq(doc.querySelectorAll(".nb-card").length, 2);
});

await check("clicking a chat calls the EXISTING openProjectAndChat with that chat, then switches to Chat", async () => {
    const { doc, window, calls } = await buildDom({ activeProjectId: "p2" });
    goNotebooks(doc, window);
    click(window, cardFor(doc, "Physics Revision").querySelector(".nb-open-btn"));
    click(window, Array.from(doc.querySelectorAll(".nb-chat-row")).find((r) => r.textContent.includes("Momentum")));
    eq(calls.openProjectAndChat[0][0], "p1");
    eq(calls.openProjectAndChat[0][1], "c2");
    assert(!doc.getElementById("chat-view").classList.contains("hidden"));
});

await check("New chat inside a notebook (even an empty one) uses the EXISTING createNewChat, no error", async () => {
    const { doc, window, calls } = await buildDom();
    goNotebooks(doc, window);
    click(window, cardFor(doc, "Bangla Essays").querySelector(".nb-open-btn"));
    assert(doc.querySelector(".nb-empty").textContent.includes("No chats in this notebook yet"));
    click(window, doc.getElementById("notebook-detail-new-chat-btn"));
    eq(calls.createNewChat[0], "p2");
    assert(!doc.getElementById("chat-view").classList.contains("hidden"));
});

await check("create: empty and whitespace names are rejected without calling createNewProject", async () => {
    const { doc, window, calls } = await buildDom();
    goNotebooks(doc, window);
    click(window, doc.getElementById("projects-view-new-btn"));
    assert(!doc.getElementById("notebook-modal").classList.contains("hidden"));
    eq(doc.getElementById("notebook-modal-title").textContent, "Create notebook");
    doc.getElementById("notebook-name-input").value = "   ";
    await submitForm(window, doc);
    eq(calls.createNewProject.length, 0);
    assert(!doc.getElementById("notebook-name-error").classList.contains("hidden"));
});

await check("create: a valid name calls the EXISTING createNewProject once and opens the new notebook", async () => {
    const { doc, window, calls } = await buildDom();
    goNotebooks(doc, window);
    click(window, doc.getElementById("projects-view-new-btn"));
    doc.getElementById("notebook-name-input").value = "  HSC   Physics — Vector ";
    await submitForm(window, doc);
    eq(calls.createNewProject.length, 1);
    eq(calls.createNewProject[0], "HSC Physics — Vector");
    assert(doc.getElementById("notebook-modal").classList.contains("hidden"));
    eq(doc.querySelector(".nb-title").textContent, "HSC Physics — Vector");
});

await check("create: duplicate submissions while a request is in flight are ignored", async () => {
    const { doc, window, calls } = await buildDom();
    window.createNewProject = (title) => { calls.createNewProject.push(title); return new Promise((r) => setTimeout(() => r({ signedIn: false }), 40)); };
    goNotebooks(doc, window);
    click(window, doc.getElementById("projects-view-new-btn"));
    doc.getElementById("notebook-name-input").value = "Chemistry";
    doc.getElementById("notebook-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    doc.getElementById("notebook-form").dispatchEvent(new window.Event("submit", { bubbles: true, cancelable: true }));
    await tick(5);
    eq(calls.createNewProject.length, 1);
    eq(doc.getElementById("notebook-submit").textContent, "Creating\u2026");
    eq(doc.getElementById("notebook-modal").getAttribute("data-busy"), "true");
    window.closeNotebookModal();
    assert(!doc.getElementById("notebook-modal").classList.contains("hidden"), "must not close while saving");
    await tick(80);
    assert(doc.getElementById("notebook-modal").classList.contains("hidden"));
});

await check("create is disabled while the account's notebooks are still loading (no misleading empty state)", async () => {
    const { doc, window } = await buildDom({ projects: [], loadState: "loading" });
    goNotebooks(doc, window);
    assert(doc.querySelector(".nb-loading"), "skeleton shown");
    eq(doc.querySelector(".nb-empty"), null);
    assert(doc.getElementById("projects-view-new-btn").disabled);
});

await check("rename: prefilled, validated, and delegated to the EXISTING-data renameProject; failure keeps the dialog open", async () => {
    const { doc, window, calls } = await buildDom({ renameResult: { ok: false, reason: "sync" } });
    goNotebooks(doc, window);
    click(window, cardFor(doc, "Physics Revision").querySelector(".nb-menu-btn"));
    click(window, Array.from(cardFor(doc, "Physics Revision").querySelectorAll(".nb-menu-item")).find((i) => i.textContent.includes("Rename")));
    eq(doc.getElementById("notebook-name-input").value, "Physics Revision");
    eq(doc.getElementById("notebook-modal-title").textContent, "Rename notebook");
    doc.getElementById("notebook-name-input").value = "Physics Revision 2";
    await submitForm(window, doc);
    eq(calls.renameProject[0][0], "p1");
    eq(calls.renameProject[0][1], "Physics Revision 2");
    assert(!doc.getElementById("notebook-modal").classList.contains("hidden"), "dialog stays open on failure");
    assert(!doc.getElementById("notebook-name-error").classList.contains("hidden"));
});

await check("delete: requires confirmation, names the notebook and states the real consequence", async () => {
    const { doc, window, calls } = await buildDom();
    goNotebooks(doc, window);
    click(window, cardFor(doc, "Physics Revision").querySelector(".nb-menu-btn"));
    click(window, Array.from(cardFor(doc, "Physics Revision").querySelectorAll(".nb-menu-item")).find((i) => i.textContent.includes("Delete")));
    eq(calls.deleteProject.length, 0, "nothing is deleted before confirming");
    const desc = doc.getElementById("notebook-delete-desc").textContent;
    assert(desc.includes("Physics Revision") && desc.includes("2 chats") && desc.includes("permanently"), desc);
    window.closeNotebookDeleteModal();
    eq(calls.deleteProject.length, 0, "cancel deletes nothing");
});

await check("delete: confirm calls the EXISTING deleteProject({confirmed:true}); a failure is shown and the notebook stays", async () => {
    const { doc, window, calls } = await buildDom({ deleteResult: false });
    goNotebooks(doc, window);
    click(window, cardFor(doc, "Physics Revision").querySelector(".nb-menu-btn"));
    click(window, Array.from(cardFor(doc, "Physics Revision").querySelectorAll(".nb-menu-item")).find((i) => i.textContent.includes("Delete")));
    click(window, doc.getElementById("notebook-delete-confirm"));
    await tick();
    eq(calls.deleteProject[0][0], "p1");
    eq(calls.deleteProject[0][1].confirmed, true);
    assert(!doc.getElementById("notebook-delete-error").classList.contains("hidden"));
    assert(!doc.getElementById("notebook-delete-modal").classList.contains("hidden"));
    assert(cardFor(doc, "Physics Revision"), "still listed after a failed delete");
});

await check("search filters by name, shows a useful no-results state, and Clear restores the list", async () => {
    const { doc, window } = await buildDom();
    goNotebooks(doc, window);
    const input = doc.getElementById("notebook-search-input");
    input.value = "bangla";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    eq(doc.querySelectorAll(".nb-card").length, 1);
    input.value = "zzz";
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
    assert(doc.querySelector(".nb-no-results").textContent.includes("No notebooks match"));
    click(window, doc.querySelector(".nb-no-results .nb-ghost-btn"));
    eq(doc.querySelectorAll(".nb-card").length, 2);
});

await check("zero notebooks shows the designed empty state, not an error", async () => {
    const { doc, window } = await buildDom({ projects: [] });
    goNotebooks(doc, window);
    const text = doc.querySelector(".nb-empty").textContent;
    assert(text.includes("Create your first notebook"));
    assert(text.includes("Organize your study chats by subject, chapter or learning goal."));
    eq(doc.querySelector(".nb-toolbar"), null);
});

await check("a failed account load shows a banner with Retry and still lists the saved copy", async () => {
    const { doc, window } = await buildDom({ loadState: "degraded" });
    goNotebooks(doc, window);
    assert(doc.querySelector(".nb-banner").textContent.includes("Retry"));
    eq(doc.querySelectorAll(".nb-card").length, 2);
});

await check("SECURITY: a notebook title containing HTML/script is escaped, never executed", async () => {
    const malicious = [
        { id: "pX", title: '<img src=x onerror="window.__pwned=true">', chats: [{ id: "cX", title: "<b>hi</b>" }] }
    ];
    const { doc, window } = await buildDom({ projects: malicious });
    window.__pwned = undefined;
    goNotebooks(doc, window);
    assert(window.__pwned === undefined, "injected markup must never execute");
    const titleEl = doc.querySelector(".nb-card-title");
    eq(titleEl.querySelector("img"), null, "the <img> must not become a real element");
    assert(titleEl.textContent.includes("<img"), "the raw text should be visible as text, not parsed as HTML");
    click(window, doc.querySelector(".nb-open-btn"));
    eq(doc.querySelector(".nb-chat-title").querySelector("b"), null);
    eq(doc.querySelector(".nb-title").querySelector("img"), null);
});

await check("the Notebooks dialogs are registered with the shared modal layer (Escape closers + busy guards)", async () => {
    const uiCore = fs.readFileSync(path.join(ROOT, "static", "js", "ui-core.js"), "utf8");
    assert(uiCore.includes('"notebook-modal": "closeNotebookModal"'));
    assert(uiCore.includes('"notebook-delete-modal": "closeNotebookDeleteModal"'));
    assert(/"notebook-modal": function \(modal\) \{\s*return modal\.getAttribute\("data-busy"\) === "true";/.test(uiCore));
    assert(rawHtml.includes('id="notebook-modal" class="modal-overlay hidden"'));
    assert(rawHtml.includes('id="notebook-delete-modal" class="modal-overlay hidden"'));
});

console.log("\n9D/9E integration - learning profile & theme stay reachable from every tab");

await check("the profile trigger and theme toggle are OUTSIDE the Chat-scoped section, so they never hide on Quizzes/Projects", () => {
    // This is a real improvement over the pre-Phase-9 state (Phase 8A
    // audit flagged the Student Profile panel as reachable from only one
    // sidebar button): the footer containing profile-trigger-btn and
    // theme-toggle-btn sits structurally OUTSIDE #chat-sidebar-section,
    // so switching to Quizzes or Projects can never hide it.
    const chatSectionMatch = rawHtml.match(/<div id="chat-sidebar-section"[\s\S]*?<!-- \/#chat-sidebar-section -->/);
    assert(chatSectionMatch, "chat-sidebar-section wrapper not found");
    assert(!chatSectionMatch[0].includes('id="profile-trigger-btn"'),
        "profile trigger must NOT be inside the Chat-scoped section");
    assert(!chatSectionMatch[0].includes('id="theme-toggle-btn"'),
        "theme toggle must NOT be inside the Chat-scoped section");
});

await check("the Student Profile panel stays reachable from every tab: trigger opens the account menu, its header opens the panel", async () => {
    // ACCOUNT MENU change: the sidebar profile row used to open the Student
    // Profile panel directly. It now opens the account menu, whose name/email
    // header opens the SAME panel (window.openStudentProfilePanel), so the
    // learning profile is still reachable from every tab.
    const { doc, window } = await buildDom();
    let opened = 0;
    window.openStudentProfilePanel = () => { opened++; };
    window.getCurrentProfile = () => ({ name: "Test Student" });
    for (const f of ["i18n.js", "profile-menu.js"]) {
        const script = doc.createElement("script");
        script.textContent = fs.readFileSync(path.join(ROOT, "static", "js", f), "utf8");
        doc.body.appendChild(script);
    }
    doc.getElementById("auth-logged-view").classList.remove("hidden");

    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const trigger = doc.getElementById("profile-trigger-btn");
    trigger.dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(!doc.getElementById("profile-menu").classList.contains("hidden"),
        "the profile row must open the account menu from the Projects tab");
    eq(opened, 0, "opening the menu must not itself open the panel");

    doc.querySelector('[data-pm-action="learning-profile"]')
        .dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(opened, 1, "the menu header must open the Student Profile panel");
    assert(doc.getElementById("profile-menu").classList.contains("hidden"), "menu closes after the action");
});

console.log("\nMobile drawer integration (Phase 8C)");

await check("nav-rail-btn is included in the drawer's close-on-navigate selector list", () => {
    const uiCoreJs = fs.readFileSync(path.join(ROOT, "static", "js", "ui-core.js"), "utf8");
    assert(uiCoreJs.includes(".nav-rail-btn"), "tapping a nav tab on mobile must close the drawer like other navigation");
});

console.log(`\n${"=".repeat(52)}`);
console.log(`  ${passed} passed, ${failed} failed`);
console.log("=".repeat(52));
if (failed) {
    for (const f of failures) console.log(`\n${f.name}\n  ${f.e.stack}`);
    process.exit(1);
}
