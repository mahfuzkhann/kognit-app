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

async function buildDom({ projects = makeFakeProjects(), activeProjectId = "p1", session = { access_token: "tok" }, snapshot = { strengths: [], needs_practice: [] } } = {}) {
    let html = fs.readFileSync(path.join(ROOT, "templates", "index.html"), "utf8");
    html = html.replace(/<script src="https:\/\/[^"]*"[^>]*><\/script>/g, "");
    for (const f of ["app.js", "ui-core.js", "stream-render.js", "theme.js", "nav-views.js"]) {
        html = html.replace(new RegExp(`<script src="/static/js/${f}"></script>`), "");
    }

    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kognit.test/" });
    const { window } = dom;

    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

    // ---- Stub of app.js's PHASE 9B contract, matching its real signatures ----
    const calls = { createNewProject: 0, createNewChat: [], deleteProject: [], startRenamingProject: [], openProjectAndChat: [], openQuizModal: 0 };
    window.getProjectsSnapshot = () => projects;
    window.getActiveIds = () => ({ activeProjectId, activeChatId: null });
    window.createNewProject = () => { calls.createNewProject++; };
    window.createNewChat = (projId) => { calls.createNewChat.push(projId); };
    window.deleteProject = (projId) => { calls.deleteProject.push(projId); return Promise.resolve(); };
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
    assert((rawHtml.match(/role="tab"/g) || []).length === 3, "exactly 3 tabs");
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

await check("ArrowLeft wraps from Chat to Projects (roving tabindex wraps)", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-chat").focus();
    doc.querySelector(".nav-rail").dispatchEvent(new window.KeyboardEvent("keydown", { key: "ArrowLeft", bubbles: true }));
    eq(doc.activeElement.id, "nav-tab-projects");
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

console.log("\nProjects view - dedicated view, built from the SAME data (Decision 4)");

await check("renders one card per project from getProjectsSnapshot(), no second data source", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const cards = doc.querySelectorAll(".project-card-tile");
    eq(cards.length, 2);
    assert(doc.getElementById("projects-view-grid").textContent.includes("Physics Revision"));
    assert(doc.getElementById("projects-view-grid").textContent.includes("Bangla Essays"));
});

await check("chat count is DERIVED from real chats, never invented", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const cards = doc.querySelectorAll(".project-card-tile");
    const physicsCard = Array.from(cards).find((c) => c.textContent.includes("Physics Revision"));
    const emptyCard = Array.from(cards).find((c) => c.textContent.includes("Bangla Essays"));
    assert(physicsCard.querySelector(".project-card-tile-meta").textContent.includes("2"));
    assert(emptyCard.querySelector(".project-card-tile-meta").textContent.includes("0"));
});

await check("no invented metadata: no dates, no 'last edited', no activity stats appear", async () => {
    const { doc, window } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const text = doc.getElementById("projects-view-grid").textContent.toLowerCase();
    assert(!text.includes("ago"), "no fabricated relative timestamps");
    assert(!text.includes("last edited"));
});

await check("the active project's card is visually marked", async () => {
    const { doc, window } = await buildDom({ activeProjectId: "p1" });
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const cards = doc.querySelectorAll(".project-card-tile");
    const physicsCard = Array.from(cards).find((c) => c.textContent.includes("Physics Revision"));
    assert(physicsCard.classList.contains("project-card-tile--active"));
});

await check("Open on a project with existing chats calls openProjectAndChat with its FIRST chat, switches to Chat", async () => {
    const { doc, window, calls } = await buildDom({ activeProjectId: "p2" });
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const physicsCard = Array.from(doc.querySelectorAll(".project-card-tile")).find((c) => c.textContent.includes("Physics Revision"));
    physicsCard.querySelector(".project-card-tile-open").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(calls.openProjectAndChat[0][0], "p1");
    eq(calls.openProjectAndChat[0][1], "c1");
    assert(!doc.getElementById("chat-view").classList.contains("hidden"), "should land back on Chat");
});

await check("Open on a project with ZERO chats creates one, does not error", async () => {
    const { doc, window, calls } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const emptyCard = Array.from(doc.querySelectorAll(".project-card-tile")).find((c) => c.textContent.includes("Bangla Essays"));
    emptyCard.querySelector(".project-card-tile-open").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(calls.createNewChat[0], "p2");
});

await check("Rename delegates to the EXISTING startRenamingProject + sidebar UI, no duplicate rename form", async () => {
    const { doc, window, calls } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const physicsCard = Array.from(doc.querySelectorAll(".project-card-tile")).find((c) => c.textContent.includes("Physics Revision"));
    physicsCard.querySelector(".project-card-tile-rename").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(calls.startRenamingProject[0], "p1");
    assert(!doc.getElementById("chat-view").classList.contains("hidden"),
        "rename UI lives in the sidebar, so this must switch back to Chat to show it");
});

await check("Delete calls the EXISTING deleteProject (which owns its own confirm())", async () => {
    const { doc, window, calls } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    const physicsCard = Array.from(doc.querySelectorAll(".project-card-tile")).find((c) => c.textContent.includes("Physics Revision"));
    physicsCard.querySelector(".project-card-tile-delete").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 5));
    eq(calls.deleteProject[0], "p1");
});

await check("+ New Project calls the EXISTING createNewProject, then switches to Chat", async () => {
    const { doc, window, calls } = await buildDom();
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    doc.getElementById("projects-view-new-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    eq(calls.createNewProject, 1);
    assert(!doc.getElementById("chat-view").classList.contains("hidden"));
});

await check("zero projects shows an honest empty state, not an error", async () => {
    const { doc, window } = await buildDom({ projects: [] });
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(doc.getElementById("projects-view-grid").textContent.toLowerCase().includes("no projects"));
});

await check("SECURITY: a project title containing HTML/script is escaped, never executed", async () => {
    const malicious = [
        { id: "pX", title: '<img src=x onerror="window.__pwned=true">', chats: [] }
    ];
    const { doc, window } = await buildDom({ projects: malicious });
    window.__pwned = undefined;
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(window.__pwned === undefined, "injected markup must never execute");
    const titleEl = doc.querySelector(".project-card-tile-title");
    eq(titleEl.querySelector("img"), null, "the <img> must not become a real element");
    assert(titleEl.textContent.includes("<img"), "the raw text should be visible as text, not parsed as HTML");
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

await check("opening the Student Profile panel works identically regardless of active nav tab", async () => {
    const { doc, window } = await buildDom();
    window.openStudentProfilePanel = () => {
        doc.getElementById("student-profile-modal").classList.remove("hidden");
    };
    doc.getElementById("nav-tab-projects").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    doc.getElementById("profile-trigger-btn").dispatchEvent(new window.MouseEvent("click", { bubbles: true }));
    assert(!doc.getElementById("student-profile-modal").classList.contains("hidden"),
        "profile panel must open the same way from any tab, not just Chat");
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
