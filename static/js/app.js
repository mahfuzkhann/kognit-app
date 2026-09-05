// ==================== SUPABASE CONFIGURATION ====================
const SUPABASE_URL = "https://ywxsxhzlienovbbakvcg.supabase.co";
const SUPABASE_ANON_KEY = "sb_publishable_sN0pdD6FX5IfOrJFpfqovQ_bH1NanXm";

// Client Initialization
const supabaseClient = supabase.createClient(SUPABASE_URL, SUPABASE_ANON_KEY);

let currentUser = null;
let isSignUpMode = false;

// ==================== APP STATE ====================
let currentMode = "direct";
let projects = [];
let activeProjectId = null;
let activeChatId = null;

// SIDEBAR IA FIX (Projects vs Recent Chats):
//
// Previously the global "+ New Chat" button called createNewChat() with no
// project id, which defaulted to whatever project happened to be active -
// so a "standalone" chat silently landed inside whatever project the user
// was last looking at. There was also no concept of a chat existing outside
// a project at all.
//
// Minimal, beta-safe fix (no database schema change, no migration): one
// reserved project id acts as the "Recent Chats" bucket. It is a project
// exactly like any other in the data model (same user_projects row shape:
// id/title/chats) - the ONLY thing that makes it special is this constant
// id, checked by every function below that needs to tell it apart from a
// real, user-created project. This keeps it fully backward compatible with
// existing accounts (nothing is migrated; the bucket is created lazily,
// the first time a user actually creates a standalone chat) and with the
// existing Supabase RLS/upsert path (it's just another row the owning user
// already has full access to).
//
// This project is NEVER shown as a folder, is NEVER user-renamable, and can
// NEVER be deleted via deleteProject() - see the guards below.
const DEFAULT_PROJECT_ID = "proj_recent_default";

let editingProjectId = null;
let editingChatId = null;
let searchQuery = "";

// ==================== SIDEBAR IA: COLLAPSIBLE PROJECT FOLDERS ====================
// FEATURE 1: which real projects are currently collapsed (chats hidden).
// This is a pure UI/display preference, not student data, so it is kept in
// localStorage only (not synced to Supabase) - same tier of persistence as
// "which project was last active" already was before this feature existed.
// A project not present in this set is EXPANDED by default (matches the
// pre-existing behavior where a project's chats were always visible).
const COLLAPSED_PROJECTS_STORAGE_KEY = "kognit_collapsed_projects";

function loadCollapsedProjectIds() {
    try {
        const raw = localStorage.getItem(COLLAPSED_PROJECTS_STORAGE_KEY);
        return new Set(raw ? JSON.parse(raw) : []);
    } catch (e) {
        return new Set();
    }
}

let collapsedProjectIds = loadCollapsedProjectIds();

function saveCollapsedProjectIds() {
    localStorage.setItem(COLLAPSED_PROJECTS_STORAGE_KEY, JSON.stringify([...collapsedProjectIds]));
}

function toggleProjectCollapsed(projId) {
    if (collapsedProjectIds.has(projId)) {
        collapsedProjectIds.delete(projId);
    } else {
        collapsedProjectIds.add(projId);
    }
    saveCollapsedProjectIds();
    renderHistoryList();
}

// A newly created project/chat should always be visibly expanded, even if
// the user had previously collapsed that project id in an earlier session.
function ensureProjectExpanded(projId) {
    if (collapsedProjectIds.has(projId)) {
        collapsedProjectIds.delete(projId);
        saveCollapsedProjectIds();
    }
}

// ==================== FEATURE 3: PIN CHAT (sort helper) ====================
// Pinned chats float to the top of whichever list they belong to (a
// project's nested chats, or the flat Recent Chats list), while preserving
// the existing relative order of same-pinned-state chats. This is
// DISPLAY-ONLY sorting - it never mutates the underlying chats array, so
// the array's own order (newest-first via unshift on creation) stays intact
// as the actual persisted storage order. Array.prototype.sort is stable per
// spec (ES2019+), so ties keep their original relative order.
function sortChatsForDisplay(chatsList) {
    return [...(chatsList || [])].sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
}

function togglePinChat(chat) {
    chat.pinned = !chat.pinned;
    saveProjectsToStorage();
    renderHistoryList();
}

let selectedImageBase64 = null;
let selectedImageFile = null;
let isPDFLoaded = false;

// Quiz State
let quizQuestions = [];
let currentQuizIdx = 0;
let userQuizAnswers = [];
// DATABASE FOUNDATION (Phase 1): the quiz_id returned by /api/quiz/generate,
// needed by submitQuizAttempt() to tell the backend which server-held quiz
// definition to grade against. null whenever there is no active,
// submittable quiz (before generation, or after a successful/attempted
// submission clears it in resetQuizModal()).
let currentQuizId = null;

document.addEventListener("DOMContentLoaded", async () => {
    // Active Session Check
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (session) {
        currentUser = session.user;
        updateAuthUI(currentUser);
        await loadProjectsFromDatabase();
    } else {
        loadProjectsFromLocalStorage();
    }

    // Auth Change Listener
    supabaseClient.auth.onAuthStateChange((_event, session) => {
        // CHAT-05 fix: this listener used to reload the entire project list
        // and re-pick the active chat (via loadProjectsFromDatabase ->
        // initializeActiveProject) every time it fired - but Supabase's
        // client fires this for far more than just an actual login/logout,
        // including session-recovery checks tied to the browser tab
        // regaining focus/visibility. That was silently replacing whatever
        // chat was open - even mid-AI-response - with a freshly (re)picked
        // one, with no click from the user. The true initial page load
        // above already loads projects once; explicit login
        // (handleEmailAuth, below) and explicit logout (handleLogout,
        // below) each already trigger their own project load/reset
        // directly, on the actual user action rather than on this passive
        // listener. Google OAuth login is a full-page redirect back to
        // Kognit, so it's already covered by the initial load above too.
        // This listener now ONLY keeps currentUser/the header UI in sync -
        // it must never touch `projects`, `activeProjectId`, or
        // `activeChatId` again after the initial load, no matter how many
        // times it fires or why.
        currentUser = session ? session.user : null;
        updateAuthUI(currentUser);
    });
});

// ==================== DATABASE & STORAGE SYNC ====================

// BUG-3 fallback: defends against any legacy/corrupted record missing a
// title (renderHistoryList() calls .toLowerCase() directly on these -
// without this, a single untitled record would throw and break the whole
// sidebar). New projects/chats always get "New Project"/"New Chat" at
// creation already, so this only matters for pre-existing/imported data.
function normalizeProjectTitles(projList) {
    (projList || []).forEach(p => {
        if (!p.title) p.title = "Untitled Project";
        (p.chats || []).forEach(c => {
            if (!c.title) c.title = "Untitled Chat";
        });
    });
    return projList;
}

async function loadProjectsFromDatabase() {
    if (!currentUser) return;

    try {
        const { data, error } = await supabaseClient
            .from("user_projects")
            .select("*")
            .order("updated_at", { ascending: false });

        if (error) {
            console.error("Error fetching projects:", error.message);
            loadProjectsFromLocalStorage();
            return;
        }

        if (data && data.length > 0) {
            projects = normalizeProjectTitles(data.map(p => ({
                id: p.id,
                title: p.title,
                chats: p.chats || []
            })));
        } else {
            // Security fix: this used to fall back to whatever was cached
            // in localStorage["kognit_projects"] and silently sync it into
            // this (possibly brand-new) account. That leftover data could
            // belong to a different person entirely (e.g. a previous user
            // on a shared browser who logged out without clearing local
            // data). A newly logged-in account with an empty database must
            // always start fresh - it must never inherit anyone else's
            // local cache, guest or otherwise.
            projects = [];
            createNewProject();
            return;
        }

        initializeActiveProject();
    } catch (err) {
        console.error("Database error:", err);
        loadProjectsFromLocalStorage();
    }
}

function loadProjectsFromLocalStorage() {
    projects = normalizeProjectTitles(JSON.parse(localStorage.getItem("kognit_projects")) || []);
    initializeActiveProject();
}

function initializeActiveProject() {
    // BUG 4 FIX (startup/re-login regression): this used to short-circuit
    // here with `if (realProjects.length === 0) { createNewProject(); return; }`,
    // which unconditionally created a new empty real Project on every app
    // load/reload/re-login whenever the user had zero real Projects - even
    // when they still had usable Recent Chats (e.g. immediately after
    // deleting their last real Project via deleteProject(), which already
    // correctly leaves the user in Recent Chats without creating a new
    // Project). Reloading the page or logging back in then silently
    // recreated the exact unwanted empty Project deleteProject() had just
    // avoided, since this function never checked whether Recent Chats
    // already had somewhere usable to land.
    //
    // The truly-brand-new-account case (empty database, no rows at all) is
    // handled separately and earlier, in loadProjectsFromDatabase()
    // (`projects = []; createNewProject(); return;` before this function is
    // even called) - that path is untouched by this fix.
    //
    // STARTUP CHAT LOCATION FIX: the chat that opens automatically when
    // Kognit launches must always be a standalone Recent Chats chat - the
    // exact same bucket/type window.createStandaloneChat() (the global
    // "+ New Chat" button) creates - never a chat inside a real project.
    //
    // Previously this set activeProjectId = realProjects[0].id and either
    // reused or created the startup chat there (see the former BUG-1 fix
    // comment this replaces), so every fresh app open silently landed - or
    // added a new chat - inside whichever real project happened to be
    // first. Real projects and every chat already inside them are
    // completely untouched by this change: they are simply no longer where
    // the startup chat lives. The user still opens any real project and
    // its chats normally via the sidebar.
    const defaultProject = getOrCreateDefaultProject();
    const mostRecentChat = defaultProject.chats.length > 0 ? defaultProject.chats[0] : null;
    const mostRecentChatIsEmpty = mostRecentChat &&
        mostRecentChat.messages.filter(m => m.role === "user").length === 0;

    if (mostRecentChatIsEmpty) {
        // The most recent Recent Chats entry has no user messages yet (e.g.
        // it was just created and never used) - reuse it rather than piling
        // up empty "New Chat" entries in Recent Chats every time the app is
        // opened. Same rule that previously applied to the first real
        // project, now applied to Recent Chats instead.
        activeProjectId = defaultProject.id;
        activeChatId = mostRecentChat.id;
        renderHistoryList();
        loadChat(activeProjectId, activeChatId);
    } else {
        // BUG-1 fix (preserved): start on a genuinely fresh chat instead of
        // silently resuming whatever conversation was last active. No
        // existing chat is deleted - every chat in every real project, and
        // every existing Recent Chats entry, remains listed in the sidebar
        // and can still be reopened manually. Goes through the exact same
        // createStandaloneChat() the global "+ New Chat" button uses, so
        // the startup chat is identical in type/location to one created by
        // that button.
        createStandaloneChat();
    }
}

async function saveProjectsToStorage() {
    localStorage.setItem("kognit_projects", JSON.stringify(projects));

    if (currentUser) {
        const activeProj = projects.find(p => p.id === activeProjectId);
        if (activeProj) {
            await syncProjectToDatabase(activeProj);
        }
    }
}

async function syncProjectToDatabase(project) {
    if (!currentUser) return;

    const payload = {
        id: project.id,
        user_id: currentUser.id,
        title: project.title,
        chats: project.chats,
        updated_at: new Date().toISOString()
    };

    const { error } = await supabaseClient
        .from("user_projects")
        .upsert(payload, { onConflict: "id" });

    if (error) {
        console.error("Supabase Sync Error:", error.message);
    }
}

async function deleteProjectFromDatabase(projId) {
    if (!currentUser) return;

    const { error } = await supabaseClient
        .from("user_projects")
        .delete()
        .eq("id", projId);

    if (error) {
        console.error("Delete from Supabase Error:", error.message);
    }
}

// ==================== AUTH FUNCTIONS ====================

window.openAuthModal = function() {
    document.getElementById("auth-modal").classList.remove("hidden");
    document.getElementById("auth-error-msg").classList.add("hidden");
};

window.closeAuthModal = function() {
    document.getElementById("auth-modal").classList.add("hidden");
};

window.toggleAuthMode = function() {
    isSignUpMode = !isSignUpMode;
    // FEATURE 8: the modal header icon (lock, in the HTML markup) is fixed
    // and no longer overwritten here - only the text span next to it
    // changes between the two modes.
    const titleText = document.getElementById("auth-modal-title-text");
    const submitBtn = document.getElementById("auth-submit-btn");
    const toggleDesc = document.getElementById("auth-toggle-desc");
    const toggleLink = document.getElementById("auth-toggle-link");

    if (isSignUpMode) {
        titleText.textContent = "Create a Kognit Account";
        submitBtn.textContent = "Sign Up";
        toggleDesc.textContent = "Already have an account?";
        toggleLink.textContent = "Sign In";
        submitBtn.onclick = () => handleEmailAuth('signup');
    } else {
        titleText.textContent = "Sign In to Kognit";
        submitBtn.textContent = "Sign In";
        toggleDesc.textContent = "Don't have an account?";
        toggleLink.textContent = "Sign Up";
        submitBtn.onclick = () => handleEmailAuth('login');
    }
};

window.handleEmailAuth = async function(type) {
    const email = document.getElementById("auth-email-input").value.trim();
    const password = document.getElementById("auth-password-input").value.trim();
    const errorMsg = document.getElementById("auth-error-msg");

    errorMsg.classList.add("hidden");

    if (!email || !password) {
        errorMsg.textContent = "Please enter both email and password.";
        errorMsg.classList.remove("hidden");
        return;
    }

    let result;
    if (type === 'signup') {
        result = await supabaseClient.auth.signUp({ email, password });
    } else {
        result = await supabaseClient.auth.signInWithPassword({ email, password });
    }

    if (result.error) {
        errorMsg.textContent = result.error.message;
        errorMsg.classList.remove("hidden");
    } else {
        // CHAT-05 fix: onAuthStateChange no longer triggers project loads
        // (see its comment above) - this explicit user action needs to
        // drive the load itself now.
        currentUser = result.data.user;
        updateAuthUI(currentUser);
        await loadProjectsFromDatabase();
        closeAuthModal();
        alert(type === 'signup' ? "Registration successful! Please check your email to verify." : "Logged in successfully!");
    }
};

window.handleGoogleSignIn = async function() {
    const { error } = await supabaseClient.auth.signInWithOAuth({
        provider: 'google',
        options: {
            redirectTo: window.location.origin
        }
    });

    if (error) {
        const errorMsg = document.getElementById("auth-error-msg");
        errorMsg.textContent = error.message;
        errorMsg.classList.remove("hidden");
    }
};

window.handleLogout = async function() {
    await supabaseClient.auth.signOut();
    currentUser = null;
    updateAuthUI(null);

    // Security fix: the logged-in user's projects were mirrored into
    // localStorage while they were signed in (see saveProjectsToStorage).
    // Clearing it here ensures the next person to use this browser -
    // whether a guest or a different account - cannot see or inherit this
    // user's chats/projects.
    localStorage.removeItem("kognit_projects");
    projects = [];
    activeProjectId = null;
    activeChatId = null;

    loadProjectsFromLocalStorage();
    alert("Logged out successfully!");
};

function updateAuthUI(user) {
    const guestView = document.getElementById("auth-guest-view");
    const loggedView = document.getElementById("auth-logged-view");
    const emailText = document.getElementById("user-display-email");

    if (user) {
        guestView.classList.add("hidden");
        loggedView.classList.remove("hidden");
        emailText.textContent = user.email;
    } else {
        guestView.classList.remove("hidden");
        loggedView.classList.add("hidden");
    }
}

// ==================== BUG 1 FIX: LOGIN-REQUIRED UX ====================
// Login is mandatory for every protected action (chat, quiz, PDF upload) -
// the backend already enforces this (see get_current_user_id in
// backend/main.py). Previously, a logged-out user's protected request
// still got sent, the backend correctly rejected it with 401, but the
// caller (sendMessage/generateAndStartQuiz) never checked response status
// before reading the JSON body, so the student saw a misleading generic
// message ("No response received." / "Failed to generate quiz.") instead
// of being told to log in.
//
// This reuses the EXISTING login/signup modal as-is (openAuthModal,
// toggleAuthMode, handleEmailAuth, handleGoogleSignIn are all completely
// unchanged) - it only opens that modal and reuses its existing
// #auth-error-msg slot to show a short explanatory message. No new UI,
// no new authentication system.
function promptLoginRequired() {
    openAuthModal();
    const errorMsg = document.getElementById("auth-error-msg");
    if (errorMsg) {
        errorMsg.textContent = "Please log in or create an account to continue using Kognit.";
        errorMsg.classList.remove("hidden");
    }
}

// ==================== WORKSPACE & CHAT FUNCTIONS ====================

// ==================== SHARED MATHJAX RENDERING HELPER ====================
// BUG FIX (raw "$$"/"\text{...}"/"_" visible to students instead of rendered
// math): every call site used to do `if (window.MathJax) MathJax.typesetPromise(...)`.
// That guard checks the wrong thing. `window.MathJax` is the plain
// configuration OBJECT set in the inline <script> in templates/index.html -
// it exists and is truthy from the very first moment the page starts
// parsing, long before the async-loaded MathJax library
// (<script id="MathJax-script" async src=".../tex-svg.js">, also in
// templates/index.html) has actually finished downloading and initializing.
// `typesetPromise` is a method the real library adds to that object later -
// until then it simply does not exist. So the old guard passes even when
// MathJax isn't ready, and `MathJax.typesetPromise([...])` throws
// "TypeError: MathJax.typesetPromise is not a function" SYNCHRONOUSLY, before
// the trailing `.catch()` can even attach - an uncaught exception that
// silently leaves the raw LaTeX source on screen, permanently (nothing else
// ever re-triggers typesetting for that message). This is most likely to be
// hit on page load/reload (loadChat() runs inside the DOMContentLoaded
// handler, often before the MathJax CDN bundle has finished fetching+
// parsing), which is exactly the symptom reported: existing chat history
// showing raw "$$6\text{CO}_2 ..." on open.
//
// This one helper replaces every direct MathJax.typesetPromise(...) call
// site (loadChat() - covers initial page load, switching chats, and
// regenerate, since regenerateBotMessage() re-renders via loadChat();
// and the reply-append path inside submitUserMessageAndAppendReplyInner() -
// covers new messages, resend, and edit/resubmit, since all of those funnel
// through either loadChat() or that same reply-append code). There is
// exactly one place that knows how to safely wait for MathJax:
//   1. Already ready (`typesetPromise` exists) -> call it immediately.
//   2. Script has started loading and exposed `MathJax.startup.promise`
//      (the documented MathJax v3 mechanism for "resolve once it's safe to
//      call typesetPromise") -> chain on that.
//   3. Script hasn't even started executing yet (window.MathJax is still
//      just the bare config object, no `startup` property at all) -> a
//      short, BOUNDED poll. This state should be extremely brief in
//      practice (the <script async> tag starts fetching immediately on
//      page parse) but is handled explicitly rather than assumed away.
// In every case, failure (MathJax never becomes ready within the bound, or
// typesetPromise itself rejects) is caught and logged - it never throws an
// uncaught exception and never blocks the rest of the chat UI. If MathJax
// truly never loads (e.g. the CDN is blocked), the affected message's math
// stays as raw text and a console warning is logged - the same degraded
// outcome as today's bug, but explicit and non-crashing instead of an
// uncaught exception, and every OTHER part of the UI keeps working.
const MATHJAX_READY_POLL_INTERVAL_MS = 100;
const MATHJAX_READY_MAX_WAIT_MS = 8000; // generous for a slow CDN fetch; bounded so a permanently-broken load doesn't poll forever.

function typesetMathJax(elements) {
    if (window.MathJax && typeof window.MathJax.typesetPromise === "function") {
        return window.MathJax.typesetPromise(elements).catch((err) => {
            console.error("MathJax typeset error:", err);
        });
    }

    if (window.MathJax && window.MathJax.startup && window.MathJax.startup.promise) {
        return window.MathJax.startup.promise
            .then(() => {
                if (typeof window.MathJax.typesetPromise === "function") {
                    return window.MathJax.typesetPromise(elements);
                }
                console.warn("MathJax startup resolved but typesetPromise is still unavailable.");
            })
            .catch((err) => {
                console.error("MathJax typeset error:", err);
            });
    }

    // MathJax hasn't started executing at all yet. Poll briefly rather than
    // giving up immediately (which would show raw math unnecessarily on a
    // slightly slow connection) or polling forever (which would leak timers
    // if MathJax genuinely never loads, e.g. the CDN is blocked/offline).
    return new Promise((resolve) => {
        const start = Date.now();
        const poll = () => {
            const ready = window.MathJax && (typeof window.MathJax.typesetPromise === "function" || (window.MathJax.startup && window.MathJax.startup.promise));
            if (ready) {
                resolve(typesetMathJax(elements));
                return;
            }
            if (Date.now() - start >= MATHJAX_READY_MAX_WAIT_MS) {
                console.warn("MathJax did not become available within the wait period; formulas on this message may show as raw text until the page is reloaded.");
                resolve();
                return;
            }
            setTimeout(poll, MATHJAX_READY_POLL_INTERVAL_MS);
        };
        poll();
    });
}

// BUG FIX (Bengali text broken inside math formulas): MathJax's SVG/CHTML
// renderers do not run full complex-script text shaping on the content
// they typeset - Bengali Unicode inside math mode (even inside \text{...})
// ends up laid out as isolated per-character glyphs instead of one shaped
// text run, which breaks conjunct formation and detaches vowel signs
// (matras) from their base consonant. `mtextInheritFont: true` in
// templates/index.html only fixes the font-family used; it cannot fix
// this shaping problem, since MathJax never hands the string to the
// browser's native text shaper in the first place.
//
// Fix: intercept math segments BEFORE MathJax ever sees them. Any
// $...$ / $$...$$ segment that contains Bengali Unicode (U+0980-U+09FF)
// is converted here into plain, real HTML text (with hand-built
// fraction/superscript/subscript markup for the handful of LaTeX
// constructs NCTB content actually uses), so the browser's native text
// shaping engine renders the Bengali correctly - exactly like normal
// prose. Segments with NO Bengali are left completely untouched and
// still go through MathJax exactly as before (zero change to existing
// math rendering).
const BENGALI_UNICODE_RANGE = /[\u0980-\u09FF]/;

// $$...$$ (display) is matched before $...$ (inline) in the same
// alternation so a display block's own $ characters are never mistaken
// for a pair of inline segments.
const MATH_SEGMENT_REGEX = /\$\$([\s\S]+?)\$\$|\$([^\$\n]+?)\$/g;

function escapeHtmlForMath(str) {
    return str
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
}

// Ordered longest-command-first so e.g. "\leq" is matched before the
// shorter "\le" would otherwise match a prefix of it and leave a
// stray "q" behind.
const BENGALI_MATH_SYMBOL_REPLACEMENTS = [
    ["\\\\leq", "≤"], ["\\\\le", "≤"],
    ["\\\\geq", "≥"], ["\\\\ge", "≥"],
    ["\\\\neq", "≠"], ["\\\\ne", "≠"],
    ["\\\\approx", "≈"],
    ["\\\\times", "×"], ["\\\\div", "÷"], ["\\\\cdot", "·"], ["\\\\pm", "±"],
    ["\\\\infty", "∞"], ["\\\\pi", "π"], ["\\\\Delta", "Δ"],
    ["\\\\%", "%"]
];

// Renders one Bengali-containing math segment's raw LaTeX (delimiters
// already stripped) as safe HTML. Scope is intentionally limited to the
// LaTeX constructs Kognit's NCTB content actually produces (frac, sqrt,
// sup/sub, common symbols) - this is NOT a general LaTeX parser.
// BUG FIX (raw "text"/"frac"/"sqrt" tokens leaking into answers, e.g.
// "textমূল্য অনুপাত"): renderBengaliMathSegment only had explicit
// handling for \frac and \sqrt. Any other LaTeX wrapper command -
// most commonly \text{...} around a Bengali label - fell through to the
// generic fallback further down, which drops the backslash but keeps
// the command NAME as literal text before stripping braces:
// "\text{মূল্য অনুপাত}" -> "text{মূল্য অনুপাত}" -> "textমূল্য অনুপাত".
// Fix: unwrap these commands to their bare content BEFORE \frac/\sqrt
// run, so (a) the command name never leaks and (b) a wrapper nested
// inside a \frac argument (e.g. \frac{\text{কিছু}}{2}) no longer blocks
// the frac regex below, which only matches brace-free arguments.
const BENGALI_MATH_TEXT_WRAPPER_COMMANDS = [
    "text", "mathrm", "textbf", "textit", "mathbf", "mathit", "emph", "bf", "rm"
];

function renderBengaliMathSegment(rawLatex, isDisplay) {
    // Escape FIRST, before any tag-building regex runs below, so nothing
    // from the AI's output (even if it contained literal HTML) can ever
    // become a real tag - every "<" / ">" a student's content might
    // contain is inert text by the time we start inserting our own markup.
    let text = escapeHtmlForMath(rawLatex.trim());

    // Unwrap \text{...}/\mathrm{...}/etc to bare content. Looped (like the
    // \frac loop below) so a wrapper nested inside another wrapper - e.g.
    // \textbf{\text{কিছু}} - still fully resolves instead of stopping
    // after one pass.
    let previousWrapper;
    do {
        previousWrapper = text;
        for (const cmd of BENGALI_MATH_TEXT_WRAPPER_COMMANDS) {
            text = text.replace(new RegExp("\\\\" + cmd + "\\{([^{}]*)\\}", "g"), "$1");
        }
    } while (text !== previousWrapper);

    // \left / \right are sizing directives with no standalone meaning
    // once rendered as plain text - drop the command, keep whatever
    // delimiter character follows it untouched (e.g. "\left(" -> "(").
    text = text.replace(/\\left|\\right/g, "");

    // \frac{A}{B} -> a real HTML fraction. Non-nested only (MVP scope):
    // a \frac whose numerator/denominator itself contains another
    // \frac/\sqrt is left as literal text rather than mis-rendered, which
    // is a safer fallback than broken nested markup. The loop lets
    // multiple, non-nested fractions in the same segment all convert.
    let previous;
    do {
        previous = text;
        text = text.replace(
            /\\frac\{([^{}]*)\}\{([^{}]*)\}/g,
            '<span class="kg-frac"><span class="kg-frac-num">$1</span><span class="kg-frac-den">$2</span></span>'
        );
    } while (text !== previous);

    // \sqrt{A}
    text = text.replace(
        /\\sqrt\{([^{}]*)\}/g,
        '<span class="kg-sqrt"><span class="kg-sqrt-radical">√</span><span class="kg-sqrt-content">$1</span></span>'
    );

    // Superscript / subscript: braced form first, then single-char form.
    text = text.replace(/\^\{([^{}]*)\}/g, "<sup>$1</sup>");
    text = text.replace(/\^([A-Za-z0-9])/g, "<sup>$1</sup>");
    text = text.replace(/_\{([^{}]*)\}/g, "<sub>$1</sub>");
    text = text.replace(/_([A-Za-z0-9])/g, "<sub>$1</sub>");

    // Common LaTeX symbols used in NCTB math/science/business content.
    for (const [pattern, replacement] of BENGALI_MATH_SYMBOL_REPLACEMENTS) {
        text = text.replace(new RegExp(pattern, "g"), replacement);
    }

    // Graceful fallback: any LaTeX command we don't explicitly handle
    // above just has its backslash dropped and the command name kept as
    // plain text, rather than showing a raw "\command" to the student.
    // Any stray braces left over from an unhandled construct are removed
    // too, since they carry no visual meaning once the command is gone.
    text = text.replace(/\\([a-zA-Z]+)/g, "$1");
    text = text.replace(/[{}]/g, "");

    const displayClass = isDisplay ? " kognit-bengali-formula--block" : "";
    return `<span class="kognit-bengali-formula${displayClass}">${text}</span>`;
}

// BUG FIX (marked.js corrupting valid LaTeX before MathJax ever sees it):
// the function that used to live here (preprocessBengaliMath) only protected
// $...$/$$...$$ segments that contained Bengali Unicode - every OTHER math
// segment (i.e. every pure chemistry/math formula) was returned completely
// untouched, on the assumption that MathJax would see it exactly as written.
// That assumption is false: the untouched segment still gets run through
// marked.parse() (see createBotMessageElement below) BEFORE MathJax ever
// runs, and marked applies ordinary Markdown emphasis rules to it. Verified
// directly: marked.parse("\\text{C}_6\\text{H}_{12}") produces
// "\\text{C}<em>6\\text{H}</em>{12}" - the underscores are silently consumed
// as italic-emphasis delimiters and the LaTeX is corrupted before MathJax
// ever gets a chance to typeset it. (This specifically happens when a
// command like \text{...} sits immediately before the underscore, since
// that makes the underscore NOT "intraword" by Markdown's rules - a bare
// "CO_2" with no \text{} wrapper is NOT affected, which is why this bug is
// intermittent-looking rather than affecting every formula.)
//
// Fix: extend the same "intercept before marked.parse(), restore after"
// strategy already used for Bengali segments to EVERY math segment, Bengali
// or not:
//   - Bengali segments: unchanged behavior - rendered immediately to safe,
//     already-escaped HTML via renderBengaliMathSegment (marked.js passes
//     raw inline HTML through untouched by default, which is what already
//     made this work).
//   - Every other (non-Bengali) segment: swapped for an inert placeholder
//     token that contains no Markdown-significant characters, so marked.js
//     cannot possibly reinterpret it. After marked.parse() runs, the
//     placeholder is swapped back for the ORIGINAL raw segment text -
//     delimiters, backslashes, underscores, braces, all byte-for-byte
//     unchanged - via restoreProtectedMathSegments(), so MathJax then
//     typesets the pristine original source exactly as if marked had never
//     touched it. The restored text is passed through the same
//     escapeHtmlForMath() used for Bengali segments (escapes only
//     & < > " ' - never touches $, \, _, {, }) as defensive-in-depth
//     hardening against the pathological case of literal "<"/">" characters
//     inside a math segment ending up interpreted as real HTML tags; for
//     every realistic LaTeX/chemistry formula this is a no-op since none of
//     those characters normally appear there.
//
// Each placeholder replaces the segment's FULL match (delimiters included)
// as a single token with no embedded newlines, which also means a
// multi-line display-math block can no longer be split across separate
// Markdown paragraphs by marked - a secondary robustness improvement, not
// just a workaround for the emphasis bug.
const MATH_PLACEHOLDER_PREFIX = "\uE000KGMATH";
const MATH_PLACEHOLDER_SUFFIX = "\uE001";

// Scans rawText for $...$/$$...$$ segments. Bengali-containing segments are
// rendered to safe HTML immediately (unchanged existing behavior); every
// other segment is replaced with a placeholder token and its original raw
// text is collected in `segments` for restoreProtectedMathSegments() to
// swap back in after marked.parse() runs. Returns both the placeholder-
// bearing text and the segments array (restoration needs both).
function protectMathSegments(rawText) {
    if (!rawText) return { text: rawText, segments: [] };

    const segments = [];

    // Skip fenced code blocks entirely so a literal "$" inside a student's
    // code sample is never mistaken for a math delimiter.
    const parts = rawText.split(/(```[\s\S]*?```)/g);

    const text = parts.map((chunk, idx) => {
        const isCodeFence = idx % 2 === 1;
        if (isCodeFence) return chunk;

        return chunk.replace(MATH_SEGMENT_REGEX, (match, displayContent, inlineContent) => {
            const isDisplay = displayContent !== undefined;
            const content = isDisplay ? displayContent : inlineContent;

            if (BENGALI_UNICODE_RANGE.test(content)) {
                return renderBengaliMathSegment(content, isDisplay);
            }

            const index = segments.length;
            segments.push(match); // full match, delimiters included, untouched
            return MATH_PLACEHOLDER_PREFIX + index + MATH_PLACEHOLDER_SUFFIX;
        });
    }).join("");

    return { text, segments };
}

// Swaps every placeholder token in marked.parse()'s HTML output back for its
// original raw math text (HTML-escaped defensively - see comment above).
// Must be called on the HTML string AFTER marked.parse(), before it is
// assigned to any element's innerHTML.
function restoreProtectedMathSegments(html, segments) {
    if (!segments.length) return html;
    const placeholderRegex = new RegExp(MATH_PLACEHOLDER_PREFIX + "(\\d+)" + MATH_PLACEHOLDER_SUFFIX, "g");
    return html.replace(placeholderRegex, (fullMatch, indexStr) => {
        const segment = segments[Number(indexStr)];
        // Defensive fallback: should never happen (every placeholder this
        // module creates has a corresponding entry), but never let a lookup
        // miss leave a raw sentinel character visible to the student.
        if (segment === undefined) return "";
        return escapeHtmlForMath(segment);
    });
}

// BUG FIX (can't copy formulas): MathJax renders every equation as an SVG
// made of vector <path> shapes, not real text - the browser's native
// select/copy has nothing to grab there (this is a side effect of MathJax
// using SVG output instead of CHTML, see templates/index.html for why).
// Rather than fighting MathJax's rendering, every bot message gets its own
// "Copy" button that copies the ORIGINAL raw Markdown+LaTeX source (already
// held in memory as msg.text/replyText) straight to the clipboard - formulas
// included, exactly as Gemini wrote them ($...$, $$...$$, etc.) - while the
// on-screen MathJax rendering itself is completely untouched.
// ==================== FEATURE 4: AI ANSWER ACTION TOOLBAR ====================
// Minimal inline SVG icons (currentColor-based, no external icon library in
// this vanilla-JS project) - explicitly no emoji, per product spec.
const TOOLBAR_ICONS = {
    copy: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>`,
    check: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`,
    thumbsUp: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"></path><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"></path></svg>`,
    thumbsDown: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"></path><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"></path></svg>`,
    regenerate: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>`
};

// ==================== FEATURE 8: SHARED PRODUCT UI ICON SET ====================
// One consistent, minimal inline-SVG icon system for every Kognit product
// UI control (buttons, inputs, status labels). Same visual language as
// TOOLBAR_ICONS above (currentColor stroke, no fill) so both sets look like
// one system. Scope is strictly PRODUCT UI controls - emoji that are part
// of AI-generated or hardcoded chat-bubble message TEXT (e.g. the welcome
// message, PDF-loaded confirmation) are intentionally left untouched here;
// they are message content, not interface controls.
const UI_ICONS = {
    search: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>`,
    lock: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="11" width="14" height="10" rx="2"></rect><path d="M8 11V7a4 4 0 0 1 8 0v4"></path></svg>`,
    logout: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"></path><polyline points="16 17 21 12 16 7"></polyline><line x1="21" y1="12" x2="9" y2="12"></line></svg>`,
    share: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"></circle><circle cx="6" cy="12" r="3"></circle><circle cx="18" cy="19" r="3"></circle><line x1="8.6" y1="10.5" x2="15.4" y2="6.5"></line><line x1="8.6" y1="13.5" x2="15.4" y2="17.5"></line></svg>`,
    file: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline></svg>`,
    image: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2"></rect><circle cx="8.5" cy="8.5" r="1.5"></circle><polyline points="21 15 16 10 5 21"></polyline></svg>`,
    close: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>`,
    pin: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="17" x2="12" y2="22"></line><path d="M5 17h14l-1.5-1.5a2 2 0 0 1-.6-1.4V8a5 5 0 0 0-10 0v6.1a2 2 0 0 1-.6 1.4L5 17z"></path></svg>`,
    chat: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"></path></svg>`,
    edit: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.12 2.12 0 0 1 3 3L12 15l-4 1 1-4z"></path></svg>`,
    trash: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"></path><path d="M10 11v6"></path><path d="M14 11v6"></path><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"></path></svg>`,
    folder: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"></path></svg>`,
    plus: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>`,
    chevron: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>`,
    quiz: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"></circle><circle cx="12" cy="12" r="5"></circle><circle cx="12" cy="12" r="1"></circle></svg>`,
    trophy: `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 21h8"></path><path d="M12 17v4"></path><path d="M7 4h10v5a5 5 0 0 1-10 0V4z"></path><path d="M7 5H4a3 3 0 0 0 3 5"></path><path d="M17 5h3a3 3 0 0 1-3 5"></path></svg>`,
    // PHASE 5: used only by the empty-chat greeting (see
    // renderEmptyChatGreeting below). Same stroke-based visual language as
    // the rest of this icon set - a plain four-point spark, not an emoji.
    spark: `<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l1.6 5.4L19 10l-5.4 1.6L12 17l-1.6-5.4L5 10l5.4-1.6L12 3z"></path></svg>`
};

function createBotMessageElement(rawText, meta = {}) {
    const wrapper = document.createElement("div");
    wrapper.className = "bot-message";

    const contentDiv = document.createElement("div");
    contentDiv.className = "bot-message-content";
    // ALL math segments (Bengali or not) are protected before marked.parse()
    // runs, and non-Bengali ones are restored to their original, untouched
    // raw text afterwards - see protectMathSegments/restoreProtectedMathSegments
    // above for why this is necessary (marked.js can otherwise corrupt valid
    // LaTeX underscores as Markdown emphasis).
    const { text: protectedText, segments } = protectMathSegments(rawText);
    contentDiv.innerHTML = restoreProtectedMathSegments(marked.parse(protectedText), segments);
    wrapper.appendChild(contentDiv);

    wrapper.appendChild(buildBotToolbar(rawText, meta));

    return wrapper;
}

// Builds the subtle icon-only action row shown under every AI answer: Copy,
// Like, Dislike, Regenerate. `meta` identifies where this exact
// message lives ({projId, chatId, msgIndex}) so Like/Dislike/Regenerate can
// find and persist against the right message. meta is optional/best-effort
// - if any id is missing (should not normally happen), the feedback and
// regenerate buttons simply no-op rather than throwing.
function buildBotToolbar(rawText, meta) {
    const toolbar = document.createElement("div");
    toolbar.className = "bot-toolbar";

    // ---- Copy (pre-existing feature, restyled into the shared toolbar) ----
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "toolbar-btn";
    copyBtn.title = "Copy full answer, including formulas";
    copyBtn.innerHTML = `${TOOLBAR_ICONS.copy}<span>Copy</span>`;
    copyBtn.onclick = () => copyBotMessageText(rawText, copyBtn);
    toolbar.appendChild(copyBtn);

    // ---- Like / Dislike ----
    const likeBtn = document.createElement("button");
    likeBtn.type = "button";
    likeBtn.className = "toolbar-btn";
    likeBtn.title = "Good answer";
    likeBtn.innerHTML = TOOLBAR_ICONS.thumbsUp;

    const dislikeBtn = document.createElement("button");
    dislikeBtn.type = "button";
    dislikeBtn.className = "toolbar-btn";
    dislikeBtn.title = "Needs improvement";
    dislikeBtn.innerHTML = TOOLBAR_ICONS.thumbsDown;

    const canGiveFeedback = meta && meta.projId && meta.chatId && Number.isInteger(meta.msgIndex);
    applyFeedbackVisualState(likeBtn, dislikeBtn, meta ? meta.feedback : null);
    if (canGiveFeedback) {
        likeBtn.onclick = () => setMessageFeedback(meta, "like", likeBtn, dislikeBtn);
        dislikeBtn.onclick = () => setMessageFeedback(meta, "dislike", likeBtn, dislikeBtn);
    } else {
        likeBtn.disabled = true;
        dislikeBtn.disabled = true;
    }
    toolbar.appendChild(likeBtn);
    toolbar.appendChild(dislikeBtn);

    // ---- Regenerate ----
    const regenBtn = document.createElement("button");
    regenBtn.type = "button";
    regenBtn.className = "toolbar-btn";
    regenBtn.title = "Regenerate this answer";
    regenBtn.innerHTML = `${TOOLBAR_ICONS.regenerate}<span>Regenerate</span>`;
    if (canGiveFeedback) {
        regenBtn.onclick = () => regenerateBotMessage(meta, regenBtn);
    } else {
        regenBtn.disabled = true;
    }
    toolbar.appendChild(regenBtn);

    return toolbar;
}

function applyFeedbackVisualState(likeBtn, dislikeBtn, feedback) {
    likeBtn.classList.toggle("active-like", feedback === "like");
    dislikeBtn.classList.toggle("active-dislike", feedback === "dislike");
}

function copyBotMessageText(rawText, btnEl) {
    if (!navigator.clipboard) {
        alert("Clipboard access isn't available in this browser.");
        return;
    }
    const originalHtml = btnEl.innerHTML;
    navigator.clipboard.writeText(rawText).then(() => {
        btnEl.innerHTML = `${TOOLBAR_ICONS.check}<span>Copied</span>`;
        btnEl.disabled = true;
        setTimeout(() => {
            btnEl.innerHTML = originalHtml;
            btnEl.disabled = false;
        }, 1500);
    }).catch(() => {
        alert("Couldn't copy to clipboard. Please try selecting the text manually.");
    });
}

// FEATURE 4: Like/Dislike. Persists straight onto the message object inside
// `projects` (proj.chats[].messages[].feedback), which already flows
// through the exact same saveProjectsToStorage()/syncProjectToDatabase()
// path as every other piece of chat data - no new schema, table, or
// endpoint required. For a logged-in user this is genuinely durable
// (Supabase, user-isolated via the existing RLS on user_projects); for a
// guest it lives in localStorage only, same as the rest of their chat
// history today.
function setMessageFeedback(meta, feedback, likeBtn, dislikeBtn) {
    const proj = projects.find(p => p.id === meta.projId);
    const chat = proj ? proj.chats.find(c => c.id === meta.chatId) : null;
    const msg = chat ? chat.messages[meta.msgIndex] : null;
    if (!msg) return;

    // Clicking the already-active choice clears it (toggle off).
    msg.feedback = (msg.feedback === feedback) ? null : feedback;
    meta.feedback = msg.feedback;
    applyFeedbackVisualState(likeBtn, dislikeBtn, msg.feedback);
    saveProjectsToStorage();
}

// Reattaches a previously-attached image to a NEW /api/chat request. Kognit
// only ever keeps the raw File object for the original upload (see
// selectedImageFile in handleImageSelect/sendMessage) - once a message is
// saved, only its lightweight base64 preview (msg.image, a data: URL) is
// kept for on-screen display. Regenerate/Edit/Resend need to send that same
// image data again, so this rebuilds a real, uploadable File from the
// stored data URL. Lossless (exact original bytes), used by
// regenerateBotMessage() and submitUserMessageAndAppendReply() below.
function dataURLToFile(dataUrl, filename) {
    const [header, base64Data] = dataUrl.split(",");
    const mimeMatch = header.match(/data:(.*?);base64/);
    const mime = mimeMatch ? mimeMatch[1] : "image/png";
    const binary = atob(base64Data);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) {
        bytes[i] = binary.charCodeAt(i);
    }
    return new File([bytes], filename, { type: mime });
}

// FEATURE 4: Regenerate. Re-asks the ORIGINAL preceding user turn (with the
// same conversation history that turn originally had) and replaces this bot
// message's text in place, preserving its position in the chat so the
// surrounding conversation order is untouched.
//
// PHASE 2 UPDATE: this previously refused to run for image-based turns,
// because only the lightweight base64 preview (not the original File) was
// available to resend. dataURLToFile() (above) now rebuilds a real,
// uploadable File from that same preview, so image-based turns can be
// regenerated too - this also backs the new per-user-message "Resend"
// action added in Phase 2 (see resendUserMessage below), which delegates
// here whenever the user message it's acting on already has a bot reply
// immediately after it.
async function regenerateBotMessage(meta, btnEl) {
    const proj = projects.find(p => p.id === meta.projId);
    const chat = proj ? proj.chats.find(c => c.id === meta.chatId) : null;
    if (!chat) return;

    const userMsg = chat.messages[meta.msgIndex - 1];
    if (!userMsg || userMsg.role !== "user") {
        alert("Can't find the original question for this answer.");
        return;
    }

    const originalHtml = btnEl.innerHTML;
    btnEl.disabled = true;
    btnEl.innerHTML = `${TOOLBAR_ICONS.regenerate}<span>Working...</span>`;

    // Same bounded-history construction as sendMessage(), but only up to
    // (not including) the user turn being re-asked.
    const priorMessages = chat.messages.slice(0, meta.msgIndex - 1);
    const boundedHistory = priorMessages
        .slice(-20)
        .map(m => {
            let text = (m.text || "").trim();
            if (!text && m.image) text = "[Student uploaded an image]";
            return { role: m.role, text: text };
        });

    const formData = new FormData();
    formData.append("prompt", userMsg.text || "Analyze this document/image.");
    formData.append("mode", currentMode);
    formData.append("board", document.getElementById("board-select").value);
    formData.append("user_class", document.getElementById("class-select").value);
    formData.append("stream", document.getElementById("stream-select").value);
    formData.append("history", JSON.stringify(boundedHistory));
    // BUG 2 FIX: scopes PDF context lookup to the chat that owns this
    // regenerate request - meta.chatId is already the correct, existing
    // race-safe identifier used elsewhere in this function.
    formData.append("chat_id", meta.chatId);

    if (userMsg.image) {
        try {
            formData.append("image", dataURLToFile(userMsg.image, "attachment.png"));
        } catch (e) {
            console.error("Couldn't reattach image for regenerate:", e);
            alert("Couldn't reattach the original image for regenerate. Please try again.");
            btnEl.disabled = false;
            btnEl.innerHTML = originalHtml;
            return;
        }
    }

    try {
        const headers = {};
        const { data: { session } } = await supabaseClient.auth.getSession();
        if (session) headers["Authorization"] = `Bearer ${session.access_token}`;

        const response = await fetch("/api/chat", { method: "POST", headers, body: formData });

        // BUG 1 FIX: a 401 here means the session was lost/expired between
        // page load and this request reaching the backend - handle it as
        // an authentication failure (shared login prompt), not as a
        // generic regenerate failure. Restore the button first so it isn't
        // left stuck disabled in its "Working..." state. Every other
        // response (200 with any reply text, including Gemini timeout/
        // quota/safety-block strings) is unaffected by this check.
        if (response.status === 401) {
            btnEl.disabled = false;
            btnEl.innerHTML = originalHtml;
            promptLoginRequired();
            return;
        }

        const data = await response.json();
        const replyText = data.reply || "No response received.";

        // Replace in place (same array index) - never insert a duplicate
        // user or bot message, and never shift later messages' indices.
        chat.messages[meta.msgIndex] = { role: "bot", text: replyText };
        saveProjectsToStorage();

        if (activeProjectId === meta.projId && activeChatId === meta.chatId) {
            loadChat(meta.projId, meta.chatId);
        }
    } catch (e) {
        alert("Couldn't regenerate this answer. Please try again.");
        btnEl.disabled = false;
        btnEl.innerHTML = originalHtml;
    }
}

// ==================== PHASE 2: USER MESSAGE ACTION TOOLBAR ====================
// Mirrors buildBotToolbar/createBotMessageElement above: one shared element
// builder (createUserMessageElement) used everywhere a user message is
// rendered (loadChat's history replay AND sendMessage's optimistic
// append), so the toolbar and its behavior are identical and defined once.
// `meta` identifies where this exact message lives ({projId, chatId,
// msgIndex}) so Edit/Resend can find and persist against the right message.
function createUserMessageElement(msg, meta) {
    const wrapper = document.createElement("div");
    wrapper.className = "user-message";

    const canAct = meta && meta.projId && meta.chatId && Number.isInteger(meta.msgIndex);
    if (canAct) {
        // Used by enterUserMessageEditMode() to find this exact DOM node
        // again later (a plain index into chat.messages doesn't map 1:1 to
        // a position in chatBox, which also contains bot messages).
        wrapper.dataset.msgIndex = String(meta.msgIndex);
    }

    if (msg.image) {
        const img = document.createElement("img");
        img.src = msg.image;
        img.className = "user-msg-image";
        wrapper.appendChild(img);
    }
    if (msg.text) {
        const span = document.createElement("span");
        span.className = "user-msg-text";
        span.textContent = msg.text;
        wrapper.appendChild(span);
    }

    if (canAct) {
        wrapper.appendChild(buildUserToolbar(msg, meta));
    }

    return wrapper;
}

function buildUserToolbar(msg, meta) {
    const toolbar = document.createElement("div");
    toolbar.className = "user-toolbar";

    // ---- Copy ---- (copyBotMessageText is generic despite its name - it
    // just copies whatever raw text string it's given - so it's reused
    // as-is here rather than duplicated.)
    const copyBtn = document.createElement("button");
    copyBtn.type = "button";
    copyBtn.className = "toolbar-btn";
    copyBtn.title = "Copy message";
    copyBtn.innerHTML = `${TOOLBAR_ICONS.copy}<span>Copy</span>`;
    if (msg.text) {
        copyBtn.onclick = () => copyBotMessageText(msg.text, copyBtn);
    } else {
        copyBtn.disabled = true;
    }
    toolbar.appendChild(copyBtn);

    // ---- Edit ----
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "toolbar-btn";
    editBtn.title = "Edit and resend this message";
    editBtn.innerHTML = `${UI_ICONS.edit}<span>Edit</span>`;
    editBtn.onclick = () => enterUserMessageEditMode(meta);
    toolbar.appendChild(editBtn);

    // ---- Regenerate / Resend ----
    const resendBtn = document.createElement("button");
    resendBtn.type = "button";
    resendBtn.className = "toolbar-btn";
    resendBtn.title = "Resend this message";
    resendBtn.innerHTML = `${TOOLBAR_ICONS.regenerate}<span>Resend</span>`;
    resendBtn.onclick = () => resendUserMessage(meta, resendBtn);
    toolbar.appendChild(resendBtn);

    return toolbar;
}

// Swaps one user message bubble into an inline edit form (textarea + Save/
// Cancel). Only acts on the chat currently on screen - the toolbar button
// that calls this only exists on messages already rendered there.
function enterUserMessageEditMode(meta) {
    if (activeProjectId !== meta.projId || activeChatId !== meta.chatId) return;

    const proj = projects.find(p => p.id === meta.projId);
    const chat = proj ? proj.chats.find(c => c.id === meta.chatId) : null;
    const msg = chat ? chat.messages[meta.msgIndex] : null;
    if (!msg || msg.role !== "user") return;

    const chatBox = document.getElementById("chat-box");
    const wrapper = chatBox.querySelector(`.user-message[data-msg-index="${meta.msgIndex}"]`);
    if (!wrapper) return;

    wrapper.innerHTML = "";

    // The attachment itself is shown but not editable in this MVP edit UI -
    // only the text can change. The image is still resent as-is on Save
    // (see submitUserMessageAndAppendReply -> dataURLToFile).
    if (msg.image) {
        const img = document.createElement("img");
        img.src = msg.image;
        img.className = "user-msg-image";
        wrapper.appendChild(img);
    }

    const textarea = document.createElement("textarea");
    textarea.className = "user-edit-textarea";
    textarea.value = msg.text || "";
    wrapper.appendChild(textarea);

    const actions = document.createElement("div");
    actions.className = "user-edit-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "user-edit-btn cancel-btn";
    cancelBtn.textContent = "Cancel";
    // Cancel restores the ORIGINAL message unchanged: nothing was mutated
    // in `projects` yet, so a plain re-render is enough to revert the view.
    cancelBtn.onclick = () => loadChat(meta.projId, meta.chatId);

    const saveBtn = document.createElement("button");
    saveBtn.type = "button";
    saveBtn.className = "user-edit-btn save-btn";
    saveBtn.textContent = "Save";
    saveBtn.onclick = () => {
        const newText = textarea.value.trim();
        if (!newText && !msg.image) {
            alert("Message can't be empty.");
            return;
        }
        submitUserMessageAndAppendReply(meta, newText);
    };

    actions.appendChild(cancelBtn);
    actions.appendChild(saveBtn);
    wrapper.appendChild(actions);

    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

// Shared submit path for BOTH:
//  - Edit -> Save (textOverride is the new, edited text)
//  - Resend, but only for the edge case where this user message has no bot
//    reply immediately after it yet (see resendUserMessage below - the
//    normal case, an existing reply, delegates to regenerateBotMessage
//    instead and never reaches this function)
//
// Per the Phase 2 spec, Edit always removes every conversation turn after
// the edited message (the old downstream answers were reasoning about
// content that no longer exists) and re-submits to get a fresh reply. That
// same truncate-then-append behavior is also exactly correct for the
// resend-with-no-existing-reply case, since there is nothing meaningful
// after this message to preserve there either.
// BUG FIX (found via Phase 2 automated testing, deterministic repro): unlike
// regenerateBotMessage - which disables its OWN button synchronously before
// awaiting, so a rapid double-click on that exact button is a native no-op -
// this function has no such protection, AND (unlike regenerateBotMessage) it
// calls loadChat() synchronously up front, before the network request
// resolves. That re-render creates a brand-new, fully enabled Resend/Save
// button for the same still-reply-less message. A fast second click on that
// freshly-rendered button (Resend's no-existing-reply fallback path, or a
// second Edit->Save on the same message before the first finishes) used to
// re-enter this function concurrently: both calls independently push() a bot
// reply once their own request resolved, producing two duplicate bot
// messages for one user question instead of one.
//
// Fix: a synchronous, key-based in-flight guard. The check-and-set below has
// no `await` between them, so it is atomic on JS's single-threaded event
// loop - no other call can interleave between the `has()` check and the
// `add()` that immediately follows it, regardless of how many buttons the
// user manages to click.
const inFlightUserMessageKeys = new Set();

async function submitUserMessageAndAppendReply(meta, textOverride) {
    const requestKey = `${meta.projId}:${meta.chatId}:${meta.msgIndex}`;
    if (inFlightUserMessageKeys.has(requestKey)) {
        // A request for this exact message is already in flight (e.g. a
        // fast double-click) - silently ignore the duplicate trigger rather
        // than firing a second overlapping request.
        return;
    }

    const proj = projects.find(p => p.id === meta.projId);
    const chat = proj ? proj.chats.find(c => c.id === meta.chatId) : null;
    if (!chat) return;

    const msg = chat.messages[meta.msgIndex];
    if (!msg || msg.role !== "user") return;

    const finalText = (textOverride !== undefined ? textOverride : (msg.text || "")).trim();
    if (!finalText && !msg.image) {
        alert("Message can't be empty.");
        return;
    }

    inFlightUserMessageKeys.add(requestKey);
    try {
        await submitUserMessageAndAppendReplyInner(meta, msg, finalText, chat, proj);
    } finally {
        inFlightUserMessageKeys.delete(requestKey);
    }
}

async function submitUserMessageAndAppendReplyInner(meta, msg, finalText, chat, proj) {
    msg.text = finalText;
    chat.messages = chat.messages.slice(0, meta.msgIndex + 1);
    saveProjectsToStorage();

    // CHAT-05-style race protection: remember which chat this belongs to.
    // If the user navigates away before the reply comes back, it is still
    // saved into the correct chat's data - just not painted on screen.
    const requestProjectId = meta.projId;
    const requestChatId = meta.chatId;
    const isStillViewingThisChat = () => activeProjectId === requestProjectId && activeChatId === requestChatId;

    let chatBox = null;
    if (isStillViewingThisChat()) {
        loadChat(requestProjectId, requestChatId);
        chatBox = document.getElementById("chat-box");
    }

    const priorMessages = chat.messages.slice(0, -1);
    const boundedHistory = priorMessages
        .slice(-20)
        .map(m => {
            let text = (m.text || "").trim();
            if (!text && m.image) text = "[Student uploaded an image]";
            return { role: m.role, text: text };
        });

    const formData = new FormData();
    formData.append("prompt", finalText || "Analyze this document/image.");
    formData.append("mode", currentMode);
    formData.append("board", document.getElementById("board-select").value);
    formData.append("user_class", document.getElementById("class-select").value);
    formData.append("stream", document.getElementById("stream-select").value);
    formData.append("history", JSON.stringify(boundedHistory));
    // BUG 2 FIX: requestChatId is the same CHAT-05-style race-safe id
    // already captured above for this request - reused here rather than
    // re-reading activeChatId, which could have changed by now.
    formData.append("chat_id", requestChatId);

    if (msg.image) {
        try {
            formData.append("image", dataURLToFile(msg.image, "attachment.png"));
        } catch (e) {
            console.error("Couldn't reattach image for edit/resend:", e);
        }
    }

    let loadingDiv = null;
    if (chatBox) {
        loadingDiv = document.createElement("div");
        loadingDiv.className = "bot-message";
        loadingDiv.textContent = "Kognit is searching through your book & notes...";
        chatBox.appendChild(loadingDiv);
        chatBox.scrollTop = chatBox.scrollHeight;
    }

    try {
        const headers = {};
        const { data: { session } } = await supabaseClient.auth.getSession();
        if (session) headers["Authorization"] = `Bearer ${session.access_token}`;

        const response = await fetch("/api/chat", { method: "POST", headers, body: formData });

        // BUG 1 FIX: a 401 here means the session was lost/expired between
        // page load and this request reaching the backend - handle it as
        // an authentication failure (shared login prompt), not as the
        // generic "Error connecting to Kognit Engine." text. Every other
        // response (200 with any reply text, including Gemini timeout/
        // quota/safety-block strings) is unaffected by this check.
        if (response.status === 401) {
            if (isStillViewingThisChat() && loadingDiv && document.getElementById("chat-box").contains(loadingDiv)) {
                loadingDiv.remove();
            }
            promptLoginRequired();
            return;
        }

        const data = await response.json();
        const replyText = data.reply || "No response received.";

        chat.messages.push({ role: "bot", text: replyText });
        saveProjectsToStorage();
        maybeGenerateAiTitle(proj, chat);

        if (isStillViewingThisChat()) {
            loadChat(requestProjectId, requestChatId);
        }
    } catch (e) {
        if (isStillViewingThisChat() && loadingDiv && document.getElementById("chat-box").contains(loadingDiv)) {
            loadingDiv.textContent = "Error connecting to Kognit Engine.";
        }
    }
}

// Resend: re-sends a user message's existing prompt (unchanged text).
//  - If this message already has a bot reply immediately after it (the
//    normal case), reuse the exact same in-place-replace path as the bot
//    toolbar's own Regenerate button, so there is exactly one Regenerate
//    implementation, not two - and everything after that reply stays
//    untouched.
//  - Otherwise (e.g. a prior request errored out without saving a reply -
//    see sendMessage's catch block), fall back to submitting fresh and
//    appending a new reply.
function resendUserMessage(meta, btnEl) {
    const proj = projects.find(p => p.id === meta.projId);
    const chat = proj ? proj.chats.find(c => c.id === meta.chatId) : null;
    if (!chat) return;

    const nextMsg = chat.messages[meta.msgIndex + 1];
    if (nextMsg && nextMsg.role === "bot") {
        return regenerateBotMessage({ projId: meta.projId, chatId: meta.chatId, msgIndex: meta.msgIndex + 1 }, btnEl);
    }
    return submitUserMessageAndAppendReply(meta, undefined);
}

// ==================== BUG 3 FIX: PER-CHAT BOARD/CLASS/STREAM/MODE ====================
// Board, Class, Stream, and Direct/Socratic mode used to live only in the
// #board-select/#class-select/#stream-select DOM elements and the global
// `currentMode` variable - genuinely global state shared across every chat.
// Switching chats never touched them, so whatever was last selected in
// Chat A silently carried over into Chat B, even though the student might
// reasonably believe Chat B has its own curriculum/mode context.
//
// Fix mirrors the existing per-chat PDF isolation pattern (chat.pdf +
// applyPDFStatusUI, see BUG 2 FIX above): each chat object now carries its
// own optional `settings` field ({ board, user_class, stream, mode }).
// DOM/`currentMode` remain the single live "what's currently on screen"
// source of truth (every existing formData.append(...) read site for these
// values is intentionally left untouched) - they are just now kept in sync
// with the active chat on every switch (applyChatSettingsUI, called from
// loadChat) and written back into the active chat the moment the student
// changes one (saveActiveChatSetting, called from setMode/handleContextSettingChange).

// Matches the default selected <option> in each dropdown and the default
// active mode button in templates/index.html, so a chat that has never had
// its own settings explicitly set behaves exactly as it did before this fix.
const DEFAULT_CHAT_SETTINGS = {
    board: "BD NCTB (Bangla)",
    user_class: "Class 9-10 (SSC)",
    stream: "Science (বিজ্ঞান)",
    mode: "direct"
};

// Always returns a COMPLETE settings object for a chat, filling in any
// missing key from DEFAULT_CHAT_SETTINGS - covers brand-new chats
// (chat.settings is undefined) and any pre-existing chat created before
// this fix shipped (same situation, same fallback).
function getEffectiveChatSettings(chat) {
    return Object.assign({}, DEFAULT_CHAT_SETTINGS, (chat && chat.settings) || {});
}

// Syncs the on-screen Board/Class/Stream dropdowns and Direct/Socratic mode
// buttons + `currentMode` to the chat being switched INTO. Called by
// loadChat() so every chat switch restores that chat's own settings instead
// of leaving whatever the previously open chat had selected on screen.
function applyChatSettingsUI(chat) {
    const settings = getEffectiveChatSettings(chat);

    document.getElementById("board-select").value = settings.board;
    document.getElementById("class-select").value = settings.user_class;
    document.getElementById("stream-select").value = settings.stream;

    currentMode = settings.mode;
    document.getElementById("btn-direct").classList.toggle("active", currentMode === "direct");
    document.getElementById("btn-socratic").classList.toggle("active", currentMode === "socratic");
}

// Persists one Board/Class/Stream/Mode change into the CURRENTLY ACTIVE
// chat's own settings only - never any other chat. If there's somehow no
// active chat, this is a no-op (no global fallback that could bleed into
// whichever chat is opened next).
function saveActiveChatSetting(key, value) {
    const proj = projects.find(p => p.id === activeProjectId);
    const chat = proj ? proj.chats.find(c => c.id === activeChatId) : null;
    if (!chat) return;

    chat.settings = getEffectiveChatSettings(chat);
    chat.settings[key] = value;
    saveProjectsToStorage();
}

// Called via onchange="handleContextSettingChange(...)" on the Board/Class/
// Stream <select> elements in templates/index.html.
window.handleContextSettingChange = function(settingsKey, elementId) {
    const value = document.getElementById(elementId).value;
    saveActiveChatSetting(settingsKey, value);
};

window.setMode = function(mode) {
    currentMode = mode;
    document.getElementById("btn-direct").classList.toggle("active", mode === "direct");
    document.getElementById("btn-socratic").classList.toggle("active", mode === "socratic");
    saveActiveChatSetting("mode", mode);
};

window.handleSidebarSearch = function(event) {
    searchQuery = event.target.value.trim().toLowerCase();
    renderHistoryList();
};

// ==================== PHASE 5: DYNAMIC EMPTY-CHAT GREETING ====================
// Replaces the old hardcoded "👋 Welcome to Kognit!" bot message that used
// to be pushed into chat.messages[0] at creation time (see the removed
// `messages: [{ role: "bot", text: "👋 ..." }]` blocks previously in
// createNewProject/createNewChat/createStandaloneChat below).
//
// This is intentionally a pure frontend EMPTY-STATE UI element, not a chat
// message:
//   - It is never written into chat.messages, so it is never part of the
//     "history" sent to /api/chat (see boundedHistory in sendMessage),
//     never synced to Supabase (syncProjectToDatabase serializes
//     chat.messages as-is), and never cached into
//     localStorage["kognit_projects"].
//   - Because it doesn't exist in chat.messages, there is nothing to strip
//     out on send - loadChat() simply never puts it there in the first
//     place for a chat that already has messages, and sendMessage() below
//     removes the on-screen greeting element (not a message) the moment
//     the first real message is sent.
//   - New chats now start with messages: [] (empty). A brand-new chat and
//     a previously-used-then-emptied chat are visually identical - both
//     just have zero messages - so one code path (loadChat) covers both.
const EMPTY_CHAT_GREETINGS = [
    "What would you like to learn today?",
    "What can I help you understand?",
    "Ready to explore something new?",
    "What are we studying today?",
    "Ask me anything about your coursework.",
    "Let's work through a problem together.",
    "Where should we start today?"
];

function getRandomGreeting() {
    return EMPTY_CHAT_GREETINGS[Math.floor(Math.random() * EMPTY_CHAT_GREETINGS.length)];
}

// Renders the empty-state greeting into an already-emptied #chat-box.
// Deliberately NOT styled like `.bot-message` (no bubble, no border, no
// toolbar) so it reads as an empty-conversation placeholder rather than a
// stored AI reply - see `.empty-chat-greeting` in style.css.
function renderEmptyChatGreeting(chatBox) {
    const greetingDiv = document.createElement("div");
    greetingDiv.className = "empty-chat-greeting";
    greetingDiv.innerHTML = `
        <span class="empty-chat-greeting-icon">${UI_ICONS.spark}</span>
        <p class="empty-chat-greeting-text">${getRandomGreeting()}</p>
    `;
    chatBox.appendChild(greetingDiv);
}

window.createNewProject = function() {
    const newProj = {
        id: "proj_" + Date.now(),
        title: "New Project",
        chats: [
            {
                id: "chat_" + Date.now(),
                title: "New Chat",
                // PHASE 5: no more hardcoded welcome message here - an
                // empty chat now gets its greeting from
                // renderEmptyChatGreeting() at render time (loadChat),
                // purely as UI, not as stored chat data.
                messages: []
            }
        ]
    };

    projects.unshift(newProj);
    activeProjectId = newProj.id;
    activeChatId = newProj.chats[0].id;
    ensureProjectExpanded(newProj.id);

    saveProjectsToStorage();
    renderHistoryList();
    loadChat(activeProjectId, activeChatId);
};

window.createNewChat = function(projectId = null) {
    const targetProjId = projectId || activeProjectId;
    const project = projects.find(p => p.id === targetProjId);

    if (!project) return;

    const newChat = {
        id: "chat_" + Date.now(),
        title: "New Chat",
        // PHASE 5: see comment above createNewProject.
        messages: []
    };

    project.chats.unshift(newChat);
    activeProjectId = project.id;
    activeChatId = newChat.id;
    ensureProjectExpanded(project.id);

    saveProjectsToStorage();
    renderHistoryList();
    loadChat(activeProjectId, activeChatId);
};

// Returns the reserved "Recent Chats" project, creating it (in-memory,
// then persisted via the normal saveProjectsToStorage path) the first time
// it's actually needed. Never returns a second copy - callers can rely on
// there being at most one project with id === DEFAULT_PROJECT_ID.
function getOrCreateDefaultProject() {
    let defaultProject = projects.find(p => p.id === DEFAULT_PROJECT_ID);
    if (!defaultProject) {
        defaultProject = {
            id: DEFAULT_PROJECT_ID,
            title: "Recent Chats",
            chats: []
        };
        // Pushed, not unshifted: this bucket is filtered out of the normal
        // project list everywhere below and rendered in its own section,
        // so its position in the underlying array doesn't affect display
        // order - unshift vs push makes no visible difference, push just
        // avoids implying it's a "newest project."
        projects.push(defaultProject);
    }
    return defaultProject;
}

// Global "+ New Chat" button handler - creates a standalone chat that
// belongs to no project, so it appears only under "Recent Chats" and never
// inside whatever project the user happened to have open. This is the fix
// for chats incorrectly appearing inside projects: previously this button
// called createNewChat() with no id, which silently used activeProjectId.
window.createStandaloneChat = function() {
    const defaultProject = getOrCreateDefaultProject();

    const newChat = {
        id: "chat_" + Date.now(),
        title: "New Chat",
        // PHASE 5: see comment above createNewProject.
        messages: []
    };

    defaultProject.chats.unshift(newChat);
    activeProjectId = defaultProject.id;
    activeChatId = newChat.id;

    saveProjectsToStorage();
    renderHistoryList();
    loadChat(activeProjectId, activeChatId);
};

function loadChat(projId, chatId) {
    activeProjectId = projId;
    activeChatId = chatId;

    renderHistoryList();
    const chatBox = document.getElementById("chat-box");
    chatBox.innerHTML = "";

    const proj = projects.find(p => p.id === projId);
    if (!proj) return;

    const chat = proj.chats.find(c => c.id === chatId);
    if (!chat) return;

    // PHASE 5: an empty chat (brand-new, or an existing chat that simply
    // has no messages yet) shows the dynamic greeting instead of iterating
    // zero messages. Chats with at least one message render exactly as
    // before - the greeting never appears once real conversation exists.
    if (!chat.messages || chat.messages.length === 0) {
        renderEmptyChatGreeting(chatBox);
    } else {
        chat.messages.forEach((msg, index) => {
            if (msg.role === "user") {
                chatBox.appendChild(createUserMessageElement(msg, {
                    projId: projId,
                    chatId: chatId,
                    msgIndex: index
                }));
            } else {
                chatBox.appendChild(createBotMessageElement(msg.text, {
                    projId: projId,
                    chatId: chatId,
                    msgIndex: index,
                    feedback: msg.feedback || null
                }));
            }
        });
    }

    // BUG 2 FIX: the PDF status bar must always reflect the chat being
    // switched INTO, never whatever the previously open chat left behind.
    // chat.pdf is set by handlePDFUpload()/clearPDFContext() below; a chat
    // that never had a PDF (or an older chat created before this fix)
    // simply has no `pdf` field, which correctly falls through to "hide".
    applyPDFStatusUI(chat);

    // BUG 3 FIX: same reasoning as applyPDFStatusUI above, but for
    // Board/Class/Stream/Mode - see the BUG 3 FIX block above setMode().
    applyChatSettingsUI(chat);

    typesetMathJax([chatBox]);
    chatBox.scrollTop = chatBox.scrollHeight;
}

// BUG 2 FIX: single place that syncs the #pdf-status-bar UI + isPDFLoaded
// to a given chat's stored `pdf` metadata. Used by loadChat() (switching
// chats) and by handlePDFUpload()/clearPDFContext() (after those actions
// change that metadata for the currently open chat).
function applyPDFStatusUI(chat) {
    const statusBar = document.getElementById("pdf-status-bar");
    const statusText = document.getElementById("pdf-status-text");

    if (chat && chat.pdf && chat.pdf.active) {
        isPDFLoaded = true;
        statusText.textContent = `PDF Active: ${chat.pdf.filename || "document.pdf"}`;
        statusBar.classList.remove("hidden");
    } else {
        isPDFLoaded = false;
        statusBar.classList.add("hidden");
    }
}

// Builds one chat row (.chat-item) - used both for chats nested inside a
// project folder and for standalone chats in the flat "Recent Chats" list.
// Identical behavior in both places (open/rename/delete), only the parent
// project id passed in differs, so this is pulled out once rather than
// duplicated for the two rendering contexts below.
function renderChatItem(proj, chat) {
    const chatItem = document.createElement("div");
    chatItem.className = `chat-item ${chat.id === activeChatId ? "active" : ""}`;

    if (editingChatId === chat.id) {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "rename-input";
        input.value = chat.title;
        input.onclick = (e) => e.stopPropagation();

        const saveChatTitle = () => {
            if (input.value.trim()) {
                chat.title = input.value.trim();
                // FEATURE 2: a manual rename always wins from now on - the
                // background AI title generator (maybeGenerateAiTitle) must
                // never overwrite a title the user deliberately chose.
                chat.titleSource = "manual";
                saveProjectsToStorage();
            }
            editingChatId = null;
            renderHistoryList();
        };

        input.onkeydown = (e) => {
            if (e.key === "Enter") saveChatTitle();
            if (e.key === "Escape") { editingChatId = null; renderHistoryList(); }
        };
        input.onblur = saveChatTitle;
        chatItem.appendChild(input);
        setTimeout(() => input.focus(), 50);
    } else {
        const titleText = document.createElement("span");
        titleText.className = "chat-title-text";
        // FEATURE 8: built as separate DOM nodes (icon span + text node)
        // rather than one interpolated string, so chat.title always goes
        // through a text node exactly as it did before - no change to how
        // titles are escaped/rendered, only the emoji prefix becomes an SVG.
        const titleIcon = document.createElement("span");
        titleIcon.className = "chat-item-icon";
        titleIcon.innerHTML = chat.pinned ? UI_ICONS.pin : UI_ICONS.chat;
        titleText.appendChild(titleIcon);
        titleText.appendChild(document.createTextNode(chat.title));
        titleText.onclick = () => loadChat(proj.id, chat.id);

        const chatActions = document.createElement("div");
        chatActions.className = "item-actions";

        // FEATURE 3: pin/unpin toggle. Kept as a plain action-btn (same
        // visual language as edit/delete) so it doesn't visually compete
        // with the AI answer toolbar icons below, which are a separate
        // feature (Feature 4) in a different part of the UI.
        const pinBtn = document.createElement("button");
        pinBtn.className = `action-btn pin-btn ${chat.pinned ? "pinned" : ""}`;
        pinBtn.title = chat.pinned ? "Unpin Chat" : "Pin Chat";
        pinBtn.innerHTML = UI_ICONS.pin;
        pinBtn.onclick = (e) => { e.stopPropagation(); togglePinChat(chat); };

        const editChatBtn = document.createElement("button");
        editChatBtn.className = "action-btn";
        editChatBtn.title = "Rename Chat";
        editChatBtn.innerHTML = UI_ICONS.edit;
        editChatBtn.onclick = (e) => { e.stopPropagation(); editingChatId = chat.id; renderHistoryList(); };

        const delChatBtn = document.createElement("button");
        delChatBtn.className = "action-btn delete-btn";
        delChatBtn.title = "Delete Chat";
        delChatBtn.innerHTML = UI_ICONS.trash;
        delChatBtn.onclick = (e) => { e.stopPropagation(); deleteChat(proj.id, chat.id); };

        chatActions.appendChild(pinBtn);
        chatActions.appendChild(editChatBtn);
        chatActions.appendChild(delChatBtn);

        chatItem.appendChild(titleText);
        chatItem.appendChild(chatActions);
    }

    return chatItem;
}

// Renders one real (non-default) project as a folder card with its nested
// chats - unchanged in behavior from before the Projects/Recent Chats
// split, just extracted into its own function.
function renderProjectCard(proj) {
    const projTitleMatches = proj.title.toLowerCase().includes(searchQuery);
    const matchingChats = (proj.chats || []).filter(chat =>
        chat.title.toLowerCase().includes(searchQuery)
    );

    if (!(projTitleMatches || matchingChats.length > 0)) return null;

    const projCard = document.createElement("div");
    projCard.className = `project-card ${proj.id === activeProjectId ? "active-project" : ""}`;

    const header = document.createElement("div");
    header.className = "project-header";

    if (editingProjectId === proj.id) {
        const input = document.createElement("input");
        input.type = "text";
        input.className = "rename-input";
        input.value = proj.title;
        input.onclick = (e) => e.stopPropagation();

        const saveProjectTitle = () => {
            if (input.value.trim()) {
                proj.title = input.value.trim();
                saveProjectsToStorage();
            }
            editingProjectId = null;
            renderHistoryList();
        };

        input.onkeydown = (e) => {
            if (e.key === "Enter") saveProjectTitle();
            if (e.key === "Escape") { editingProjectId = null; renderHistoryList(); }
        };
        input.onblur = saveProjectTitle;
        header.appendChild(input);
        setTimeout(() => input.focus(), 50);
    } else {
        // FEATURE 1: collapsible folder. A search in progress always forces
        // the project open (so search results are visible) regardless of
        // the user's saved collapsed/expanded preference - collapsing a
        // project that has a matching chat inside it would silently hide
        // the very result the user just searched for.
        const isSearching = searchQuery !== "";
        const isCollapsed = !isSearching && collapsedProjectIds.has(proj.id);

        const wrapper = document.createElement("div");
        wrapper.className = "project-title-wrapper";
        wrapper.innerHTML =
            `<span class="project-chevron ${isCollapsed ? "collapsed" : ""}">${UI_ICONS.chevron}</span>` +
            `<span class="project-folder-icon">${UI_ICONS.folder}</span><span class="project-title-text">${proj.title}</span>`;
        // Clicking the project row/title ONLY expands or collapses its
        // chat list (per product spec) - it no longer also navigates into
        // the first chat. Opening a specific chat is done by clicking that
        // chat row directly, same as always.
        wrapper.onclick = () => {
            if (isSearching) return; // no-op while a search is forcing it open
            toggleProjectCollapsed(proj.id);
        };

        const actions = document.createElement("div");
        actions.className = "item-actions";

        const addChatBtn = document.createElement("button");
        addChatBtn.className = "action-btn";
        addChatBtn.title = "Add Chat";
        addChatBtn.innerHTML = UI_ICONS.plus;
        addChatBtn.onclick = (e) => { e.stopPropagation(); createNewChat(proj.id); };

        const editBtn = document.createElement("button");
        editBtn.className = "action-btn";
        editBtn.title = "Rename Project";
        editBtn.innerHTML = UI_ICONS.edit;
        editBtn.onclick = (e) => { e.stopPropagation(); editingProjectId = proj.id; renderHistoryList(); };

        const delBtn = document.createElement("button");
        delBtn.className = "action-btn delete-btn";
        delBtn.title = "Delete Project";
        delBtn.innerHTML = UI_ICONS.trash;
        delBtn.onclick = (e) => { e.stopPropagation(); deleteProject(proj.id); };

        actions.appendChild(addChatBtn);
        actions.appendChild(editBtn);
        actions.appendChild(delBtn);

        header.appendChild(wrapper);
        header.appendChild(actions);
    }

    projCard.appendChild(header);

    const chatsList = document.createElement("div");
    const isSearchingForList = searchQuery !== "";
    const isCollapsedForList = !isSearchingForList && collapsedProjectIds.has(proj.id) && editingProjectId !== proj.id;
    chatsList.className = `chats-list ${isCollapsedForList ? "collapsed" : ""}`;

    const chatsToDisplay = projTitleMatches && searchQuery !== "" ? proj.chats : (searchQuery !== "" ? matchingChats : proj.chats);
    // FEATURE 3: pinned chats render first within this project's folder.
    sortChatsForDisplay(chatsToDisplay).forEach(chat => {
        chatsList.appendChild(renderChatItem(proj, chat));
    });

    projCard.appendChild(chatsList);
    return projCard;
}

function renderHistoryList() {
    const historyList = document.getElementById("history-list");
    historyList.innerHTML = "";

    let hasMatch = false;

    // SIDEBAR IA FIX: real (user-created) projects are rendered as folder
    // cards, exactly as before. The reserved DEFAULT_PROJECT_ID bucket is
    // excluded here and rendered separately below as a flat "Recent Chats"
    // list instead - this is what stops a standalone chat from ever
    // appearing nested inside a project, and stops a project's chats from
    // ever leaking into Recent Chats.
    const realProjects = projects.filter(p => p.id !== DEFAULT_PROJECT_ID);
    const defaultProject = projects.find(p => p.id === DEFAULT_PROJECT_ID);

    realProjects.forEach(proj => {
        const card = renderProjectCard(proj);
        if (card) {
            hasMatch = true;
            historyList.appendChild(card);
        }
    });

    if (defaultProject) {
        const matchingRecentChats = (defaultProject.chats || []).filter(chat =>
            chat.title.toLowerCase().includes(searchQuery)
        );

        // Only render the "Recent Chats" heading/section at all if there is
        // at least one standalone chat (or a search match among them) -
        // an empty section for a bucket most users may never touch would
        // just be sidebar clutter.
        if (matchingRecentChats.length > 0) {
            hasMatch = true;

            const recentHeading = document.createElement("div");
            recentHeading.className = "history-title recent-chats-heading";
            recentHeading.textContent = "Recent Chats";
            historyList.appendChild(recentHeading);

            const recentList = document.createElement("div");
            recentList.className = "recent-chats-list";
            // FEATURE 3: pinned standalone chats float to the top of
            // Recent Chats, same rule as pinned chats inside a project.
            sortChatsForDisplay(matchingRecentChats).forEach(chat => {
                recentList.appendChild(renderChatItem(defaultProject, chat));
            });
            historyList.appendChild(recentList);
        }
    }

    if (!hasMatch && searchQuery !== "") {
        historyList.innerHTML = `<div class="no-results">No projects or chats found matching "${searchQuery}"</div>`;
    }
}

async function deleteProject(projId) {
    // Defensive guard: the Recent Chats bucket has no delete button in the
    // UI (renderProjectCard is never called for it - see renderHistoryList),
    // so this should be unreachable via normal use. Kept here anyway since
    // deleteProject is attached to `window`-adjacent click handlers and
    // this function must never be able to wipe out every standalone chat
    // even if called some other way.
    if (projId === DEFAULT_PROJECT_ID) return;

    if (!confirm("Delete this project?")) return;
    projects = projects.filter(p => p.id !== projId);
    await deleteProjectFromDatabase(projId);
    saveProjectsToStorage();

    // Clean up the now-meaningless collapsed/expanded preference for this
    // (deleted) project id, so localStorage doesn't accumulate stale ids
    // forever across a user's account lifetime.
    if (collapsedProjectIds.has(projId)) {
        collapsedProjectIds.delete(projId);
        saveCollapsedProjectIds();
    }

    // SIDEBAR IA FIX: must check REAL projects here, not `projects` as a
    // whole. Before the Recent Chats bucket existed, `projects.length === 0`
    // correctly meant "the user has nothing left" and guaranteed a fresh
    // project. Now `projects` can still contain the default bucket even
    // after the user's last real project is deleted.
    const realProjects = projects.filter(p => p.id !== DEFAULT_PROJECT_ID);

    if (realProjects.length > 0) {
        activeProjectId = realProjects[0].id;
        activeChatId = (realProjects[0].chats && realProjects[0].chats.length > 0) ? realProjects[0].chats[0].id : null;
        renderHistoryList();
        if (activeChatId) loadChat(activeProjectId, activeChatId);
        return;
    }

    // BUG 4 FIX: deleting the last real Project used to unconditionally
    // call createNewProject() here - even when the student still had
    // Recent Chats (the project-less standalone bucket, see
    // DEFAULT_PROJECT_ID above). That silently manufactured an empty,
    // unwanted "New Project" folder every time, even though existing
    // Recent Chats were already a perfectly valid place to land. Zero
    // *Projects* remaining does not mean the student has nothing left -
    // whether Recent Chats exist is what actually determines that.
    const defaultProject = projects.find(p => p.id === DEFAULT_PROJECT_ID);
    const hasRecentChats = defaultProject && defaultProject.chats && defaultProject.chats.length > 0;

    if (hasRecentChats) {
        // Land on the existing Recent Chats instead of manufacturing a new
        // empty Project - the chats stay exactly where they already were,
        // never moved into a freshly-created Project.
        activeProjectId = defaultProject.id;
        activeChatId = defaultProject.chats[0].id;
        renderHistoryList();
        loadChat(activeProjectId, activeChatId);
    } else {
        // Truly nothing left anywhere (no real Projects, no Recent Chats) -
        // the UI still needs some active chat to render. Reuse the exact
        // same fallback the startup flow (initializeActiveProject) already
        // uses in this situation: create a standalone chat in Recent Chats,
        // NOT a new Project - so no automatic default Project is introduced
        // merely because the Project list is empty.
        createStandaloneChat();
    }
}

function deleteChat(projId, chatId) {
    if (!confirm("Delete this chat?")) return;
    const proj = projects.find(p => p.id === projId);
    if (!proj) return;
    proj.chats = proj.chats.filter(c => c.id !== chatId);
    saveProjectsToStorage();

    if (proj.chats.length === 0) {
        createNewChat(projId);
    } else {
        if (activeChatId === chatId) activeChatId = proj.chats[0].id;
        renderHistoryList();
        loadChat(projId, activeChatId);
    }
}

// NEW BUG 2 FIX: Replace onkeypress with onkeydown to handle Shift+Enter.
// Textarea now supports multi-line input. Enter sends message, Shift+Enter
// inserts newline. Auto-grows up to max-height 200px, then becomes scrollable.
// ==================== PHASE 4: COMPOSER "+" ACTION MENU ====================
// Replaces the previous two separate PDF/Image icon buttons with a single
// unified menu. This section ONLY controls menu open/close state and
// forwards clicks to the existing, unchanged upload inputs/handlers
// (#pdf-input -> handlePDFUpload, #image-input -> handleImageSelect) - no
// new upload pipeline is introduced here.
function isComposerMenuOpen() {
    const menu = document.getElementById("composer-menu");
    return !!menu && !menu.classList.contains("hidden");
}

function closeComposerMenu() {
    const menu = document.getElementById("composer-menu");
    const btn = document.getElementById("composer-plus-btn");
    if (menu) menu.classList.add("hidden");
    if (btn) btn.setAttribute("aria-expanded", "false");
}

function openComposerMenu() {
    const menu = document.getElementById("composer-menu");
    const btn = document.getElementById("composer-plus-btn");
    if (menu) menu.classList.remove("hidden");
    if (btn) btn.setAttribute("aria-expanded", "true");
}

window.toggleComposerMenu = function(event) {
    // Stop propagation so the document-level "click outside closes the
    // menu" listener below doesn't immediately re-close the menu this
    // same click just opened.
    if (event) event.stopPropagation();
    if (isComposerMenuOpen()) {
        closeComposerMenu();
    } else {
        openComposerMenu();
    }
};

// Menu item handlers - close the menu, then delegate to the SAME hidden
// <input type="file"> elements and onchange handlers that existed before
// this phase (handlePDFUpload / handleImageSelect in this file are
// completely unchanged).
window.triggerComposerPDFUpload = function() {
    closeComposerMenu();
    const input = document.getElementById("pdf-input");
    if (input) input.click();
};

window.triggerComposerImageUpload = function() {
    closeComposerMenu();
    const input = document.getElementById("image-input");
    if (input) input.click();
};

// Outside click closes the menu. Attached once at script load (this
// script tag runs after the composer markup, at the end of <body>, so
// these elements already exist). Guards on isComposerMenuOpen() first so
// this is a no-op on every ordinary click elsewhere in the app.
document.addEventListener("click", (event) => {
    if (!isComposerMenuOpen()) return;
    const wrapper = document.querySelector(".composer-attach-wrapper");
    if (wrapper && !wrapper.contains(event.target)) {
        closeComposerMenu();
    }
});

// Escape closes the menu, matching the existing Escape-to-cancel pattern
// already used for project/chat rename inputs elsewhere in this file.
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && isComposerMenuOpen()) {
        closeComposerMenu();
    }
});

window.handleKeyDown = function(event) {
    const textarea = document.getElementById("user-input");
    
    if (event.key === "Enter") {
        // Ctrl+Enter or plain Enter sends the message
        // Shift+Enter inserts a newline
        if (!event.shiftKey) {
            event.preventDefault();
            sendMessage();
        }
        // If Shift is held, the default textarea newline behavior proceeds
    }
};

window.handlePDFUpload = async function(event) {
    const file = event.target.files[0];
    if (!file) return;

    // BUG 1 FIX: /api/pdf/upload is a protected endpoint (login is
    // mandatory) - see sendMessage() for the matching guard/401-handling
    // pattern. Checked before any upload UI/state is touched.
    if (!currentUser) {
        promptLoginRequired();
        return;
    }

    // BUG 2 FIX: resolve and lock in which chat this upload belongs to
    // BEFORE the await below, same CHAT-05-style race protection already
    // used by sendMessage()/submitUserMessageAndAppendReplyInner(). If the
    // student switches chats while the upload is still in flight, the PDF
    // must still attach to the chat that was active when they picked the
    // file - not whatever chat happens to be open when the response comes
    // back.
    const requestProjectId = activeProjectId;
    const requestChatId = activeChatId;
    const proj = projects.find(p => p.id === requestProjectId);
    const chat = proj ? proj.chats.find(c => c.id === requestChatId) : null;
    if (!chat) return;

    const formData = new FormData();
    formData.append("file", file);
    formData.append("chat_id", requestChatId);

    const chatBox = document.getElementById("chat-box");
    const isStillViewingThisChat = () => activeProjectId === requestProjectId && activeChatId === requestChatId;

    // PHASE 5: same reasoning as in sendMessage() - a PDF can be uploaded
    // before the student types anything, while the empty-chat greeting is
    // still showing. Clear it so the upload status text doesn't render
    // alongside it. Only touch the DOM if this chat is still on screen.
    let loadingMsg = null;
    if (isStillViewingThisChat()) {
        const existingGreeting = chatBox.querySelector(".empty-chat-greeting");
        if (existingGreeting) existingGreeting.remove();

        loadingMsg = document.createElement("div");
        loadingMsg.className = "bot-message";
        loadingMsg.textContent = "📖 Reading and indexing PDF: " + file.name + "...";
        chatBox.appendChild(loadingMsg);
        chatBox.scrollTop = chatBox.scrollHeight;
    }

    const headers = {};
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (session) {
        headers["Authorization"] = `Bearer ${session.access_token}`;
    }

    try {
        const response = await fetch("/api/pdf/upload", { method: "POST", headers: headers, body: formData });
        await response.json();
        if (loadingMsg && chatBox.contains(loadingMsg)) chatBox.removeChild(loadingMsg);

        if (!response.ok) {
            // Covers 401 (not logged in / expired session) as well as any
            // other upload failure - never show the "loaded successfully"
            // message for a non-2xx response.
            // BUG 1 FIX: a 401 here means the session was lost/expired
            // between the currentUser check above and this request
            // reaching the backend - use the same login prompt as every
            // other protected action instead of a plain alert.
            if (response.status === 401) {
                promptLoginRequired();
            } else {
                alert("Failed to upload and parse PDF.");
            }
            if (isStillViewingThisChat()) chatBox.scrollTop = chatBox.scrollHeight;
            return;
        }

        // BUG 2 FIX: PDF metadata is stored on the chat it belongs to, not
        // in a global variable - this is what makes loadChat() able to
        // correctly restore/hide the status bar per chat later. Only a
        // boolean + filename + timestamp are kept; the extracted PDF text
        // itself never leaves the backend's in-memory store.
        chat.pdf = {
            active: true,
            filename: file.name,
            uploadedAt: new Date().toISOString()
        };
        saveProjectsToStorage();

        if (isStillViewingThisChat()) {
            applyPDFStatusUI(chat);

            const successDiv = document.createElement("div");
            successDiv.className = "bot-message";
            successDiv.innerHTML = `✅ <b>PDF Loaded Successfully!</b> You can now ask questions directly from <i>${file.name}</i>.`;
            chatBox.appendChild(successDiv);
            chatBox.scrollTop = chatBox.scrollHeight;
        }
    } catch (e) {
        if (loadingMsg && chatBox.contains(loadingMsg)) chatBox.removeChild(loadingMsg);
        alert("Failed to upload and parse PDF.");
    }
};

window.clearPDFContext = async function() {
    // BUG 2 FIX: previously this only hid the UI and reset a global flag -
    // the server-side PDF context for the chat kept answering questions
    // from the "removed" PDF. Now it (1) tells the backend to drop this
    // specific chat's context, then (2) updates only this chat's own
    // metadata, so Chat B's PDF (if any) is never touched.
    const proj = projects.find(p => p.id === activeProjectId);
    const chat = proj ? proj.chats.find(c => c.id === activeChatId) : null;

    document.getElementById("pdf-input").value = "";

    if (chat && currentUser) {
        const headers = {};
        const { data: { session } } = await supabaseClient.auth.getSession();
        if (session) {
            headers["Authorization"] = `Bearer ${session.access_token}`;
        }

        try {
            const formData = new FormData();
            formData.append("chat_id", chat.id);
            await fetch("/api/pdf/clear", { method: "POST", headers: headers, body: formData });
        } catch (e) {
            console.error("Failed to clear server-side PDF context:", e);
            // Fall through and clear the local/UI state anyway - the
            // in-memory backend context will still be replaced the next
            // time this chat_id is used for an upload, and worst case
            // here is a transient network hiccup, not silent data leakage
            // to another chat.
        }
    }

    if (chat) {
        chat.pdf = { active: false };
        saveProjectsToStorage();
    }

    if (chat && activeChatId === chat.id) {
        applyPDFStatusUI(chat);
    } else {
        isPDFLoaded = false;
        document.getElementById("pdf-status-bar").classList.add("hidden");
    }
};

window.handleImageSelect = function(event) {
    const file = event.target.files[0];
    if (!file) return;

    selectedImageFile = file;
    const reader = new FileReader();

    reader.onload = function (e) {
        selectedImageBase64 = e.target.result;
        document.getElementById("image-preview-img").src = selectedImageBase64;
        document.getElementById("image-preview-bar").classList.remove("hidden");
    };
    reader.readAsDataURL(file);
};

window.clearSelectedImage = function() {
    selectedImageBase64 = null;
    selectedImageFile = null;
    document.getElementById("image-input").value = "";
    document.getElementById("image-preview-bar").classList.add("hidden");
};

// BUG-3 fix: deterministic, client-side chat title generation - no AI API
// call. Strips basic Markdown syntax and a few common filler openers
// ("I am building", "How do I", etc.) so the title leads with the actual
// subject, then truncates at a word boundary rather than mid-word. Called
// exactly once per chat (see the guard at its call site in sendMessage) -
// never re-runs on later messages, so an existing title is always stable.
function generateChatTitle(rawText) {
    const MAX_TITLE_LENGTH = 40;
    if (!rawText) return "New Chat";

    let cleaned = rawText
        .replace(/[#*_`>~]+/g, "")
        .replace(/\[(.*?)\]\(.*?\)/g, "$1") // markdown links -> just the link text
        .replace(/\s+/g, " ")
        .trim();

    const fillerPrefixes = [
        /^i am building /i,
        /^i'm building /i,
        /^i want to /i,
        /^please help me (with |understand )?/i,
        /^please explain /i,
        /^explain /i,
        /^how do i /i,
        /^how can i /i,
        /^what is /i,
        /^what are /i,
        /^can you /i,
        /^tell me about /i,
    ];
    for (const pattern of fillerPrefixes) {
        if (pattern.test(cleaned)) {
            cleaned = cleaned.replace(pattern, "");
            break;
        }
    }
    cleaned = cleaned.trim();
    if (!cleaned) cleaned = rawText.trim(); // stripping left nothing usable - fall back to the original

    cleaned = cleaned.charAt(0).toUpperCase() + cleaned.slice(1);

    if (cleaned.length <= MAX_TITLE_LENGTH) return cleaned;

    const truncated = cleaned.slice(0, MAX_TITLE_LENGTH);
    const lastSpace = truncated.lastIndexOf(" ");
    const base = lastSpace > 15 ? truncated.slice(0, lastSpace) : truncated;
    return base.replace(/[.,;:!?]+$/, "") + "...";
}

// FEATURE 2, part 3: context-aware AI title generation.
//
// Runs AFTER the student already has their answer on screen - never on the
// critical answer path, and never more than ONCE per chat (aiTitleGenerated
// guards this), so it never spams the AI backend on every message. It only
// fires once the chat has real context to summarize (>= 2 user turns
// answered) - a single "Hey" has nothing meaningful to title yet, which is
// exactly the case called out in the product spec.
//
// Silently does nothing (keeps the existing heuristic title) if:
// - the chat was manually renamed (titleSource === "manual"),
// - a title was already AI-generated for this chat,
// - there isn't enough context yet,
// - the backend call fails for any reason (network error, AI error, etc).
async function maybeGenerateAiTitle(proj, chat) {
    if (!chat || chat.titleSource === "manual" || chat.aiTitleGenerated) return;

    const userTurns = chat.messages.filter(m => m.role === "user").length;
    if (userTurns < 2) return;

    // Guard against duplicate concurrent calls (e.g. two quick messages
    // both crossing the >=2 threshold before the first request returns).
    chat.aiTitleGenerated = true;

    try {
        const boundedHistory = chat.messages
            .slice(-20)
            .map(m => ({ role: m.role, text: (m.text || "").trim() || (m.image ? "[Student uploaded an image]" : "") }))
            .filter(m => m.text);

        const formData = new FormData();
        formData.append("history", JSON.stringify(boundedHistory));
        formData.append("board", document.getElementById("board-select").value);

        const headers = {};
        const { data: { session } } = await supabaseClient.auth.getSession();
        if (session) headers["Authorization"] = `Bearer ${session.access_token}`;
        if (!session) { chat.aiTitleGenerated = false; return; } // endpoint requires auth; quietly skip for guests

        const res = await fetch("/api/chat/title", { method: "POST", headers, body: formData });
        const data = await res.json();

        // Re-check titleSource: the user may have manually renamed the
        // chat in the time this request was in flight.
        if (data.title && chat.titleSource !== "manual") {
            chat.title = data.title;
            chat.titleSource = "ai";
            if (proj && proj.title === "New Project") proj.title = data.title;
            saveProjectsToStorage();
            renderHistoryList();
        }
    } catch (e) {
        console.error("AI title generation failed (non-critical):", e);
        // Leave aiTitleGenerated=true anyway - do not retry automatically;
        // the heuristic title already in place remains a perfectly usable
        // fallback, and retry-storms on a flaky connection are worse than
        // one imperfect title.
    }
}

window.sendMessage = async function() {
    const inputField = document.getElementById("user-input");
    const chatBox = document.getElementById("chat-box");
    const promptText = inputField.value.trim();

    if (!promptText && !selectedImageFile) return;

    // BUG 1 FIX: /api/chat is a protected endpoint (login is mandatory) -
    // if the frontend already knows there's no authenticated user, don't
    // silently send the request just to have it rejected with a 401 that
    // then shows up as a misleading "No response received." Prompt login
    // instead, before any chat/message state is touched.
    if (!currentUser) {
        promptLoginRequired();
        return;
    }

    const proj = projects.find(p => p.id === activeProjectId);
    if (!proj) return;
    const currentChat = proj.chats.find(c => c.id === activeChatId);
    if (!currentChat) return;

    // CHAT-05 race-condition protection: remember exactly which chat this
    // request belongs to by ID. If the user manually switches to a
    // different chat while this request is still in flight (a legitimate
    // action, not a bug), the response must still be saved into THIS
    // chat's data - not lost, and not visually injected into whatever
    // chat happens to be on screen when it completes. See the re-resolve
    // by ID below, after the await.
    const requestProjectId = activeProjectId;
    const requestChatId = activeChatId;

    if (currentChat.messages.filter(m => m.role === "user").length === 0 && currentChat.title === "New Chat") {
        // Instant placeholder title (FEATURE 2, part 1): shown immediately
        // so the user never waits on the AI for a title. titleSource
        // starts as "heuristic" - maybeGenerateAiTitle() below is free to
        // replace it later with a context-aware AI title once there is
        // enough conversation to work with, UNLESS the user manually
        // renames the chat first (which sets titleSource to "manual" and
        // permanently opts this chat out of auto-retitling).
        currentChat.title = generateChatTitle(promptText || "PDF/Image Query");
        currentChat.titleSource = "heuristic";
        currentChat.aiTitleGenerated = false;
        if (proj.title === "New Project") proj.title = currentChat.title;
        renderHistoryList();
    }

    // PHASE 5: the greeting (if shown) is a DOM-only empty-state element,
    // never part of currentChat.messages - remove it here, before the
    // first real message renders, so it never lingers alongside actual
    // conversation content. No-op (querySelector returns null) for a chat
    // that already had messages, since loadChat() never rendered a
    // greeting into it in the first place.
    const existingGreeting = chatBox.querySelector(".empty-chat-greeting");
    if (existingGreeting) existingGreeting.remove();

    const userMsgObj = { role: "user", text: promptText, image: selectedImageBase64 };
    currentChat.messages.push(userMsgObj);
    const newUserMsgIndex = currentChat.messages.length - 1;
    saveProjectsToStorage();

    const userDiv = createUserMessageElement(userMsgObj, {
        projId: requestProjectId,
        chatId: requestChatId,
        msgIndex: newUserMsgIndex
    });
    chatBox.appendChild(userDiv);

    // Bounded conversation history for same-chat context (CHAT-04 fix).
    // currentChat.messages currently ends with the user message we just
    // pushed above (the CURRENT prompt) - exclude it here since it's sent
    // separately via the "prompt" field. Only prior turns go in "history".
    // Cap: 10 user+assistant exchanges = 20 messages max (MVP window).
    // Images are intentionally dropped from history entries (never resend
    // old base64 image data) - only the role+text of each past turn.
    // A prior user turn that was image-only (no typed text) still gets a
    // short text placeholder here rather than being sent empty - the
    // backend drops empty-text entries, and silently dropping a user turn
    // while keeping its bot reply would break Gemini's expected user/model
    // alternation in the history it receives.
    const MAX_HISTORY_MESSAGES = 20;
    const priorMessages = currentChat.messages.slice(0, -1);
    const boundedHistory = priorMessages
        .slice(-MAX_HISTORY_MESSAGES)
        .map(m => {
            let text = (m.text || "").trim();
            if (!text && m.image) text = "[Student uploaded an image]";
            return { role: m.role, text: text };
        });

    const formData = new FormData();
    formData.append("prompt", promptText || "Analyze this document/image.");
    formData.append("mode", currentMode);
    formData.append("board", document.getElementById("board-select").value);
    formData.append("user_class", document.getElementById("class-select").value);
    formData.append("stream", document.getElementById("stream-select").value);
    formData.append("history", JSON.stringify(boundedHistory));
    // BUG 2 FIX: requestChatId (captured above, before this async flow
    // continues) is the chat that actually owns this message - reused
    // here for the same CHAT-05 race-safety reason it exists at all.
    formData.append("chat_id", requestChatId);

    if (selectedImageFile) formData.append("image", selectedImageFile);

    inputField.value = "";
    // Reset textarea height to minimum after sending
    inputField.style.height = "auto";
    clearSelectedImage();
    chatBox.scrollTop = chatBox.scrollHeight;

    const loadingDiv = document.createElement("div");
    loadingDiv.className = "bot-message";
    loadingDiv.textContent = "Kognit is searching through your book & notes...";
    chatBox.appendChild(loadingDiv);

    const headers = {};
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (session) {
        headers["Authorization"] = `Bearer ${session.access_token}`;
    }

    try {
        const response = await fetch("/api/chat", { method: "POST", headers: headers, body: formData });

        // Re-resolve the target project/chat by the ID captured before the
        // request started, rather than trusting the `proj`/`currentChat`
        // references captured before this await - see the comment above
        // where requestProjectId/requestChatId were captured.
        const targetProj = projects.find(p => p.id === requestProjectId);
        const targetChat = targetProj ? targetProj.chats.find(c => c.id === requestChatId) : null;
        const isStillViewingThisChat = activeProjectId === requestProjectId && activeChatId === requestChatId;

        // The loading indicator is only meaningful if we're still looking
        // at the chat that produced it - if the user switched chats since,
        // loadChat() already cleared/replaced chatBox's content and
        // loadingDiv isn't part of the currently-rendered chat anyway.
        if (isStillViewingThisChat && chatBox.contains(loadingDiv)) {
            chatBox.removeChild(loadingDiv);
        }

        // BUG 1 FIX: a 401 here means the session was lost/expired between
        // the frontend's own currentUser check above and this request
        // actually reaching the backend - handle it explicitly as an
        // authentication failure (login prompt), never as a generic
        // AI/provider error. This is distinct from every other failure
        // mode (Gemini timeout/quota/safety-block), which all still come
        // back as a normal 200 response with an explanatory "reply" string
        // from backend/ai_engine.py and are completely unaffected by this
        // check. The already-sent user message is left in place (it did
        // send - only the reply failed), matching how the existing catch
        // block below already leaves the sent user message untouched on
        // a network failure.
        if (response.status === 401) {
            promptLoginRequired();
            return;
        }

        // PHASE 4: a 429 here means the backend rate limiter rejected this
        // request before it ever reached Gemini (see
        // backend/main.py:_check_rate_limit). Without this check, the code
        // below would fall through to `data.reply || "No response
        // received."` - a 429 JSON body is `{"detail": ...}` with no
        // `reply` field, so the student would see the misleading "No
        // response received." as if the backend were broken, instead of
        // "you're sending messages too fast." Handled the same way as the
        // 401 case above: return early, before touching chat state.
        if (response.status === 429) {
            // loadingDiv was already removed above (if still viewing this
            // chat) - just show the rate-limit message in its place.
            if (isStillViewingThisChat) {
                const rateLimitDiv = document.createElement("div");
                rateLimitDiv.className = "bot-message";
                rateLimitDiv.textContent = "You're sending messages too quickly. Please wait a moment and try again.";
                chatBox.appendChild(rateLimitDiv);
            }
            return;
        }

        const data = await response.json();
        const replyText = data.reply || "No response received.";

        let newBotMsgIndex = -1;
        if (targetChat) {
            targetChat.messages.push({ role: "bot", text: replyText });
            newBotMsgIndex = targetChat.messages.length - 1;
            saveProjectsToStorage();

            // FEATURE 2, part 2: fire-and-forget, does not block the reply
            // the student is already looking at (see comment on the
            // function below for exactly when/how often this runs).
            maybeGenerateAiTitle(targetProj, targetChat);
        }

        if (isStillViewingThisChat) {
            const botDiv = createBotMessageElement(replyText, {
                projId: requestProjectId,
                chatId: requestChatId,
                msgIndex: newBotMsgIndex
            });
            chatBox.appendChild(botDiv);

            typesetMathJax([botDiv]);
        }
        // If the user switched chats before the response arrived, it's
        // still correctly saved above - it will simply be there the next
        // time that chat is reopened, instead of appearing in whichever
        // chat happens to be on screen right now.
    } catch (error) {
        const isStillViewingThisChat = activeProjectId === requestProjectId && activeChatId === requestChatId;
        if (isStillViewingThisChat) {
            if (chatBox.contains(loadingDiv)) chatBox.removeChild(loadingDiv);
            const errorDiv = document.createElement("div");
            errorDiv.className = "bot-message";
            errorDiv.textContent = "Error connecting to Kognit Engine.";
            chatBox.appendChild(errorDiv);
        }
    }

    if (activeProjectId === requestProjectId && activeChatId === requestChatId) {
        chatBox.scrollTop = chatBox.scrollHeight;
    }
};

// Share
window.shareChatLink = function() {
    const shareableUrl = `${window.location.origin}/#project=${activeProjectId}&chat=${activeChatId}`;
    navigator.clipboard.writeText(shareableUrl).then(() => {
        alert("Shareable link copied to clipboard!");
    }).catch(err => {
        alert("Failed to copy link.");
    });
};

// Quiz System
window.openQuizModal = function() {
    document.getElementById("quiz-modal").classList.remove("hidden");
    resetQuizModal();
};

window.closeQuizModal = function() {
    document.getElementById("quiz-modal").classList.add("hidden");
};

function resetQuizModal() {
    document.getElementById("quiz-setup-view").classList.remove("hidden");
    document.getElementById("quiz-active-view").classList.add("hidden");
    document.getElementById("quiz-result-view").classList.add("hidden");
    quizQuestions = [];
    currentQuizIdx = 0;
    userQuizAnswers = [];
    currentQuizId = null;
    const saveStatus = document.getElementById("quiz-save-status");
    if (saveStatus) {
        saveStatus.textContent = "";
        saveStatus.className = "quiz-save-status hidden";
    }
}

window.generateAndStartQuiz = async function() {
    // BUG 1 FIX: /api/quiz/generate is a protected endpoint (login is
    // mandatory) - see sendMessage() for the matching guard/401-handling
    // pattern. Checked before touching the quiz UI at all.
    if (!currentUser) {
        promptLoginRequired();
        return;
    }

    const topic = document.getElementById("quiz-topic-input").value.trim() || "General Practice";
    const count = document.getElementById("quiz-count-select").value;

    const formData = new FormData();
    formData.append("board", document.getElementById("board-select").value);
    formData.append("user_class", document.getElementById("class-select").value);
    formData.append("subject", document.getElementById("stream-select").value);
    formData.append("topic", topic);
    formData.append("count", count);

    const setupBtn = document.querySelector("#quiz-setup-view .start-quiz-btn");
    setupBtn.textContent = "AI is generating questions...";
    setupBtn.disabled = true;

    const headers = {};
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (session) {
        headers["Authorization"] = `Bearer ${session.access_token}`;
    }

    try {
        const res = await fetch("/api/quiz/generate", { method: "POST", headers: headers, body: formData });

        // BUG 1 FIX: a 401 here means the session was lost/expired between
        // the currentUser check above and this request reaching the
        // backend - show the login prompt instead of the generic
        // "Failed to generate quiz." message.
        if (res.status === 401) {
            promptLoginRequired();
            return;
        }

        // PHASE 4: a 429 here means the backend rate limiter rejected this
        // request before it ever reached Gemini (see
        // backend/main.py:_check_rate_limit) - quiz generation is
        // Kognit's heaviest single Gemini call, so it has the tightest
        // limit. Without this check it would fall through to `data.
        // questions` being undefined and show the generic "Failed to
        // generate quiz." alert, which doesn't tell the student anything
        // useful about what happened or what to do next.
        if (res.status === 429) {
            alert("You're generating quizzes too quickly. Please wait a moment and try again.");
            return;
        }

        const data = await res.json();

        if (data.questions && data.questions.length > 0) {
            quizQuestions = data.questions;
            currentQuizIdx = 0;
            userQuizAnswers = new Array(quizQuestions.length).fill(null);
            // DATABASE FOUNDATION (Phase 1): quiz_id identifies the
            // server-held definition submitQuizAttempt() will grade
            // against. May be null in a legacy/degraded-server response
            // shape - submitQuizAttempt() below handles that gracefully.
            currentQuizId = data.quiz_id || null;

            document.getElementById("quiz-setup-view").classList.add("hidden");
            document.getElementById("quiz-active-view").classList.remove("hidden");
            renderQuizQuestion();
        } else {
            alert("Failed to generate quiz.");
        }
    } catch (e) {
        alert("Error connecting to Quiz Engine.");
    } finally {
        setupBtn.textContent = "Generate Quiz with AI";
        setupBtn.disabled = false;
    }
};

function renderQuizQuestion() {
    const q = quizQuestions[currentQuizIdx];
    document.getElementById("quiz-progress-text").textContent = `Question ${currentQuizIdx + 1} of ${quizQuestions.length}`;

    const container = document.getElementById("quiz-question-container");
    container.innerHTML = `
        <div class="quiz-q-title"><b>Q${currentQuizIdx + 1}:</b> ${q.question}</div>
        <div class="quiz-options-list">
            ${q.options.map((opt, i) => `
                <button class="quiz-opt-btn ${userQuizAnswers[currentQuizIdx] === i ? 'selected' : ''}" onclick="selectQuizOption(${i})">
                    ${String.fromCharCode(65 + i)}. ${opt}
                </button>
            `).join('')}
        </div>
    `;

    const nextBtn = document.getElementById("quiz-next-btn");
    nextBtn.textContent = currentQuizIdx === quizQuestions.length - 1 ? "Submit Test" : "Next Question";
}

window.selectQuizOption = function(optIdx) {
    userQuizAnswers[currentQuizIdx] = optIdx;
    renderQuizQuestion();
};

window.nextQuizQuestion = function() {
    if (userQuizAnswers[currentQuizIdx] === null) {
        alert("Please select an answer!");
        return;
    }

    if (currentQuizIdx < quizQuestions.length - 1) {
        currentQuizIdx++;
        renderQuizQuestion();
    } else {
        showQuizResults();
    }
};

function showQuizResults() {
    document.getElementById("quiz-active-view").classList.add("hidden");
    document.getElementById("quiz-result-view").classList.remove("hidden");

    // UNCHANGED: this client-side score/rendering is kept exactly as it
    // was - it's what the student sees instantly, with no round trip.
    // Persistence (submitQuizAttempt below) is purely additive: it
    // separately asks the backend to grade+save the same answers against
    // its own server-held quiz definition and does not affect what's
    // rendered here either way.
    let score = 0;
    quizQuestions.forEach((q, i) => {
        if (userQuizAnswers[i] === q.correct_index) score++;
    });

    document.getElementById("result-score-text").textContent = `Your Score: ${score} / ${quizQuestions.length}`;

    const list = document.getElementById("result-details-list");
    list.innerHTML = quizQuestions.map((q, i) => {
        const isCorrect = userQuizAnswers[i] === q.correct_index;
        return `
            <div class="result-item" style="border-left: 3px solid ${isCorrect ? '#4ade80' : '#f87171'};">
                <div><b>Q${i + 1}:</b> ${q.question}</div>
                <div>Your Answer: <span style="color:${isCorrect ? '#4ade80' : '#f87171'};">${q.options[userQuizAnswers[i]]}</span></div>
                ${!isCorrect ? `<div>Correct Answer: <span style="color:#4ade80;">${q.options[q.correct_index]}</span></div>` : ''}
                <div class="result-explanation">💡 <b>Explanation:</b> ${q.explanation}</div>
            </div>
        `;
    }).join('');

    submitQuizAttempt();
}

// DATABASE FOUNDATION (Phase 1). Fires once, right after the (already
// working, unchanged) client-side result is shown. Never blocks or alters
// that display - this only updates the small #quiz-save-status line under
// the score with a save/failure indicator. The score shown to the student
// above is NOT replaced by whatever the backend returns; both are computed
// independently (client-side here for instant display, server-side in
// backend/database.py:save_quiz_attempt for the persisted, authoritative
// record) and should normally agree.
async function submitQuizAttempt() {
    const statusEl = document.getElementById("quiz-save-status");
    if (!statusEl) return;

    if (!currentQuizId) {
        // Nothing to submit against (e.g. an older/degraded generate
        // response without a quiz_id) - be honest about it rather than
        // silently doing nothing.
        statusEl.textContent = "⚠️ This result was not saved to your history.";
        statusEl.className = "quiz-save-status warning";
        return;
    }

    statusEl.textContent = "Saving your result...";
    statusEl.className = "quiz-save-status";

    const { data: { session } } = await supabaseClient.auth.getSession();
    if (!session) {
        // Session expired sometime during the quiz - the result the
        // student just saw is still theirs, it just won't be saved.
        statusEl.textContent = "⚠️ Please log in again to save this result.";
        statusEl.className = "quiz-save-status warning";
        return;
    }

    const formData = new FormData();
    formData.append("quiz_id", currentQuizId);
    formData.append("answers", JSON.stringify(userQuizAnswers));

    try {
        const res = await fetch("/api/quiz/submit", {
            method: "POST",
            headers: { "Authorization": `Bearer ${session.access_token}` },
            body: formData,
        });

        if (res.ok) {
            statusEl.textContent = "✅ Result saved.";
            statusEl.className = "quiz-save-status success";
            // Consumed server-side (backend/main.py pops it on success) -
            // clear locally too so a stray re-render can't try to resubmit it.
            currentQuizId = null;
        } else if (res.status === 409) {
            // Already submitted (e.g. a duplicate call) - not an error the
            // student needs to see as a failure.
            statusEl.textContent = "✅ Result saved.";
            statusEl.className = "quiz-save-status success";
            currentQuizId = null;
        } else if (res.status === 401) {
            statusEl.textContent = "⚠️ Please log in again to save this result.";
            statusEl.className = "quiz-save-status warning";
        } else {
            statusEl.textContent = "⚠️ Could not save this result. You can retake the quiz to try again.";
            statusEl.className = "quiz-save-status warning";
        }
    } catch (e) {
        statusEl.textContent = "⚠️ Could not save this result. You can retake the quiz to try again.";
        statusEl.className = "quiz-save-status warning";
    }
}