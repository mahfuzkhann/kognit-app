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
    // SIDEBAR IA FIX: only consider REAL projects here, never the reserved
    // Recent Chats bucket (DEFAULT_PROJECT_ID). Before the Projects/Recent
    // Chats split, `projects` only ever contained real projects, so
    // `projects[0]` was always a safe pick for "land the user somewhere on
    // load." Now that the default bucket can also live in `projects` - and
    // can even sort to the front after Supabase's updated_at ordering, if a
    // standalone chat was the most recently touched thing - landing there
    // by default would be a surprising place to open the app. On-load
    // behavior stays exactly as before: always a real project, defaulting
    // to a fresh chat inside it (BUG-1). Recent Chats remains something the
    // user navigates to deliberately via the sidebar.
    const realProjects = projects.filter(p => p.id !== DEFAULT_PROJECT_ID);

    if (realProjects.length === 0) {
        createNewProject();
        return;
    }

    activeProjectId = realProjects[0].id;
    const mostRecentChat = (realProjects[0].chats && realProjects[0].chats.length > 0) ? realProjects[0].chats[0] : null;
    const mostRecentChatIsEmpty = mostRecentChat &&
        mostRecentChat.messages.filter(m => m.role === "user").length === 0;

    if (mostRecentChatIsEmpty) {
        // The most recent chat has no user messages yet (e.g. it was just
        // created and never used) - reuse it rather than piling up empty
        // "New Chat" entries every time the app is opened.
        activeChatId = mostRecentChat.id;
        renderHistoryList();
        loadChat(activeProjectId, activeChatId);
    } else {
        // BUG-1 fix: start on a genuinely fresh chat instead of silently
        // resuming whatever conversation was last active. No existing
        // chat is deleted - all of them remain listed in the sidebar and
        // can still be reopened manually at any time.
        createNewChat(activeProjectId);
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
    const title = document.getElementById("auth-modal-title");
    const submitBtn = document.getElementById("auth-submit-btn");
    const toggleDesc = document.getElementById("auth-toggle-desc");
    const toggleLink = document.getElementById("auth-toggle-link");

    if (isSignUpMode) {
        title.textContent = "🚀 Create a Kognit Account";
        submitBtn.textContent = "Sign Up";
        toggleDesc.textContent = "Already have an account?";
        toggleLink.textContent = "Sign In";
        submitBtn.onclick = () => handleEmailAuth('signup');
    } else {
        title.textContent = "🔐 Sign In to Kognit";
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

// ==================== WORKSPACE & CHAT FUNCTIONS ====================

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

// Scans rawText for $...$/$$...$$ segments and replaces ONLY the ones
// containing Bengali Unicode with safe HTML (see renderBengaliMathSegment
// above). Segments with no Bengali are returned byte-for-byte unchanged,
// so MathJax.typesetPromise() (called after marked.parse() further down
// the pipeline) still typesets every non-Bengali formula exactly as
// before - zero behavior change for pure math.
function preprocessBengaliMath(rawText) {
    if (!rawText) return rawText;

    // Skip fenced code blocks entirely so a literal "$" inside a student's
    // code sample is never mistaken for a math delimiter.
    const parts = rawText.split(/(```[\s\S]*?```)/g);

    return parts.map((chunk, idx) => {
        const isCodeFence = idx % 2 === 1;
        if (isCodeFence) return chunk;

        return chunk.replace(MATH_SEGMENT_REGEX, (match, displayContent, inlineContent) => {
            const isDisplay = displayContent !== undefined;
            const content = isDisplay ? displayContent : inlineContent;

            if (!BENGALI_UNICODE_RANGE.test(content)) {
                return match; // No Bangla - leave untouched for MathJax.
            }
            return renderBengaliMathSegment(content, isDisplay);
        });
    }).join("");
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
    speaker: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><path d="M15.5 8.5a5 5 0 0 1 0 7"></path></svg>`,
    speakerOff: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"></polygon><line x1="23" y1="9" x2="17" y2="15"></line><line x1="17" y1="9" x2="23" y2="15"></line></svg>`,
    thumbsUp: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"></path><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"></path></svg>`,
    thumbsDown: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 15v4a3 3 0 0 0 3 3l4-9V2H5.72a2 2 0 0 0-2 1.7l-1.38 9a2 2 0 0 0 2 2.3H10z"></path><path d="M17 2h3a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-3"></path></svg>`,
    regenerate: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>`
};

function createBotMessageElement(rawText, meta = {}) {
    const wrapper = document.createElement("div");
    wrapper.className = "bot-message";

    const contentDiv = document.createElement("div");
    contentDiv.className = "bot-message-content";
    // Bengali-containing math segments are swapped for safe HTML BEFORE
    // Markdown parsing (see preprocessBengaliMath above); everything else
    // - including every non-Bengali formula - reaches marked.parse() and
    // MathJax exactly as it always has.
    contentDiv.innerHTML = marked.parse(preprocessBengaliMath(rawText));
    wrapper.appendChild(contentDiv);

    wrapper.appendChild(buildBotToolbar(rawText, meta));

    return wrapper;
}

// Builds the subtle icon-only action row shown under every AI answer: Copy,
// Read Aloud, Like, Dislike, Regenerate. `meta` identifies where this exact
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

    // ---- Read Aloud ----
    // Uses the browser's native SpeechSynthesis API (no backend dependency).
    // Bangla support depends entirely on the browser/OS's installed voices -
    // if none is available, speechSynthesis silently falls back to a
    // default voice rather than failing, which is acceptable MVP behavior.
    const speakBtn = document.createElement("button");
    speakBtn.type = "button";
    speakBtn.className = "toolbar-btn";
    speakBtn.title = "Read answer aloud";
    speakBtn.innerHTML = `${TOOLBAR_ICONS.speaker}<span>Listen</span>`;
    if (!("speechSynthesis" in window)) {
        speakBtn.disabled = true;
        speakBtn.title = "Read Aloud isn't supported in this browser";
    } else {
        speakBtn.onclick = () => toggleReadAloud(rawText, speakBtn);
    }
    toolbar.appendChild(speakBtn);

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

// Strips Markdown/LaTeX punctuation that would otherwise be read aloud
// literally ("hash", "dollar dollar", "star star", ...) by the speech
// engine. This is a plain-text approximation, not a full LaTeX-to-speech
// converter - acceptable for MVP read-aloud of prose answers.
function stripMarkupForSpeech(rawText) {
    return rawText
        .replace(/\$\$?/g, " ")
        .replace(/\\\[|\\\]|\\\(|\\\)/g, " ")
        .replace(/[#*_`>~]+/g, " ")
        .replace(/\[(.*?)\]\(.*?\)/g, "$1")
        .replace(/\s+/g, " ")
        .trim();
}

function toggleReadAloud(rawText, btnEl) {
    if (!("speechSynthesis" in window)) return;

    // Acts as a play/stop toggle: clicking the active speaker again cancels
    // playback instead of restarting it.
    if (window.speechSynthesis.speaking) {
        window.speechSynthesis.cancel();
        return; // onend below resets the icon/label for whichever button was speaking
    }

    const utterance = new SpeechSynthesisUtterance(stripMarkupForSpeech(rawText));
    btnEl.innerHTML = `${TOOLBAR_ICONS.speakerOff}<span>Stop</span>`;

    const reset = () => { btnEl.innerHTML = `${TOOLBAR_ICONS.speaker}<span>Listen</span>`; };
    utterance.onend = reset;
    utterance.onerror = reset;

    window.speechSynthesis.speak(utterance);
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

// FEATURE 4: Regenerate. Re-asks the ORIGINAL preceding user turn (with the
// same conversation history that turn originally had) and replaces this bot
// message's text in place, preserving its position in the chat so the
// surrounding conversation order is untouched.
//
// Known MVP limitation: if the original user turn included an uploaded
// image, we do not retain the raw image file (only its lightweight base64
// preview for on-screen display) - regenerating a text-only re-ask would
// silently drop the visual context the original answer was based on, which
// could produce a misleading result. Regenerate is therefore disabled for
// image-based turns rather than approximating an answer without the image.
async function regenerateBotMessage(meta, btnEl) {
    const proj = projects.find(p => p.id === meta.projId);
    const chat = proj ? proj.chats.find(c => c.id === meta.chatId) : null;
    if (!chat) return;

    const userMsg = chat.messages[meta.msgIndex - 1];
    if (!userMsg || userMsg.role !== "user") {
        alert("Can't find the original question for this answer.");
        return;
    }
    if (userMsg.image) {
        alert("Regenerate isn't available for image-based questions yet.");
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

    try {
        const headers = {};
        const { data: { session } } = await supabaseClient.auth.getSession();
        if (session) headers["Authorization"] = `Bearer ${session.access_token}`;

        const response = await fetch("/api/chat", { method: "POST", headers, body: formData });
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

window.setMode = function(mode) {
    currentMode = mode;
    document.getElementById("btn-direct").classList.toggle("active", mode === "direct");
    document.getElementById("btn-socratic").classList.toggle("active", mode === "socratic");
};

window.handleSidebarSearch = function(event) {
    searchQuery = event.target.value.trim().toLowerCase();
    renderHistoryList();
};

window.createNewProject = function() {
    const newProj = {
        id: "proj_" + Date.now(),
        title: "New Project",
        chats: [
            {
                id: "chat_" + Date.now(),
                title: "New Chat",
                messages: [
                    {
                        role: "bot",
                        text: "👋 <b>Welcome to Kognit!</b> Your board and syllabus context are active. Ask any question or upload a chapter PDF!"
                    }
                ]
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
        messages: [
            {
                role: "bot",
                text: "👋 <b>Welcome to Kognit!</b> Start a fresh topic or practice problem in this chat."
            }
        ]
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
        messages: [
            {
                role: "bot",
                text: "👋 <b>Welcome to Kognit!</b> Start a fresh topic or practice problem in this chat."
            }
        ]
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

    chat.messages.forEach((msg, index) => {
        if (msg.role === "user") {
            const div = document.createElement("div");
            div.className = "user-message";
            if (msg.image) {
                const img = document.createElement("img");
                img.src = msg.image;
                img.className = "user-msg-image";
                div.appendChild(img);
            }
            if (msg.text) {
                const span = document.createElement("span");
                span.textContent = msg.text;
                div.appendChild(span);
            }
            chatBox.appendChild(div);
        } else {
            chatBox.appendChild(createBotMessageElement(msg.text, {
                projId: projId,
                chatId: chatId,
                msgIndex: index,
                feedback: msg.feedback || null
            }));
        }
    });

    if (window.MathJax) {
        MathJax.typesetPromise([chatBox]).catch((err) => console.error(err));
    }
    chatBox.scrollTop = chatBox.scrollHeight;
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
        titleText.textContent = (chat.pinned ? "📌 " : "💬 ") + chat.title;
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
        pinBtn.innerHTML = `📌`;
        pinBtn.onclick = (e) => { e.stopPropagation(); togglePinChat(chat); };

        const editChatBtn = document.createElement("button");
        editChatBtn.className = "action-btn";
        editChatBtn.title = "Rename Chat";
        editChatBtn.innerHTML = `✏️`;
        editChatBtn.onclick = (e) => { e.stopPropagation(); editingChatId = chat.id; renderHistoryList(); };

        const delChatBtn = document.createElement("button");
        delChatBtn.className = "action-btn delete-btn";
        delChatBtn.title = "Delete Chat";
        delChatBtn.innerHTML = `🗑️`;
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
            `<span class="project-chevron ${isCollapsed ? "collapsed" : ""}">▾</span>` +
            `<span>📁</span><span class="project-title-text">${proj.title}</span>`;
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
        addChatBtn.innerHTML = `➕`;
        addChatBtn.onclick = (e) => { e.stopPropagation(); createNewChat(proj.id); };

        const editBtn = document.createElement("button");
        editBtn.className = "action-btn";
        editBtn.title = "Rename Project";
        editBtn.innerHTML = `✏️`;
        editBtn.onclick = (e) => { e.stopPropagation(); editingProjectId = proj.id; renderHistoryList(); };

        const delBtn = document.createElement("button");
        delBtn.className = "action-btn delete-btn";
        delBtn.title = "Delete Project";
        delBtn.innerHTML = `🗑️`;
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
    // after the user's last real project is deleted - checking
    // `projects.length` there would silently skip creating a fresh project
    // and could land the user on an empty/no-chat screen instead.
    const realProjects = projects.filter(p => p.id !== DEFAULT_PROJECT_ID);
    if (realProjects.length === 0) {
        createNewProject();
    } else {
        activeProjectId = realProjects[0].id;
        activeChatId = (realProjects[0].chats && realProjects[0].chats.length > 0) ? realProjects[0].chats[0].id : null;
        renderHistoryList();
        if (activeChatId) loadChat(activeProjectId, activeChatId);
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

    const formData = new FormData();
    formData.append("file", file);

    const chatBox = document.getElementById("chat-box");
    const loadingMsg = document.createElement("div");
    loadingMsg.className = "bot-message";
    loadingMsg.textContent = "📖 Reading and indexing PDF: " + file.name + "...";
    chatBox.appendChild(loadingMsg);
    chatBox.scrollTop = chatBox.scrollHeight;

    const headers = {};
    const { data: { session } } = await supabaseClient.auth.getSession();
    if (session) {
        headers["Authorization"] = `Bearer ${session.access_token}`;
    }

    try {
        const response = await fetch("/api/pdf/upload", { method: "POST", headers: headers, body: formData });
        await response.json();
        chatBox.removeChild(loadingMsg);

        if (!response.ok) {
            // Covers 401 (not logged in / expired session) as well as any
            // other upload failure - never show the "loaded successfully"
            // message for a non-2xx response.
            alert(response.status === 401
                ? "Please log in to upload a PDF."
                : "Failed to upload and parse PDF.");
            chatBox.scrollTop = chatBox.scrollHeight;
            return;
        }

        isPDFLoaded = true;
        document.getElementById("pdf-status-text").textContent = `📄 PDF Active: ${file.name}`;
        document.getElementById("pdf-status-bar").classList.remove("hidden");

        const successDiv = document.createElement("div");
        successDiv.className = "bot-message";
        successDiv.innerHTML = `✅ <b>PDF Loaded Successfully!</b> You can now ask questions directly from <i>${file.name}</i>.`;
        chatBox.appendChild(successDiv);
    } catch (e) {
        if (chatBox.contains(loadingMsg)) chatBox.removeChild(loadingMsg);
        alert("Failed to upload and parse PDF.");
    }
    chatBox.scrollTop = chatBox.scrollHeight;
};

window.clearPDFContext = function() {
    isPDFLoaded = false;
    document.getElementById("pdf-input").value = "";
    document.getElementById("pdf-status-bar").classList.add("hidden");
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

    const userMsgObj = { role: "user", text: promptText, image: selectedImageBase64 };
    currentChat.messages.push(userMsgObj);
    saveProjectsToStorage();

    const userDiv = document.createElement("div");
    userDiv.className = "user-message";
    if (selectedImageBase64) {
        const img = document.createElement("img");
        img.src = selectedImageBase64;
        img.className = "user-msg-image";
        userDiv.appendChild(img);
    }
    if (promptText) {
        const span = document.createElement("span");
        span.textContent = promptText;
        userDiv.appendChild(span);
    }
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
        const data = await response.json();

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

            if (window.MathJax) {
                MathJax.typesetPromise([botDiv]).catch((err) => console.error(err));
            }
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
        alert("🔗 Shareable link copied to clipboard!");
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
}

window.generateAndStartQuiz = async function() {
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
        const data = await res.json();

        if (data.questions && data.questions.length > 0) {
            quizQuestions = data.questions;
            currentQuizIdx = 0;
            userQuizAnswers = new Array(quizQuestions.length).fill(null);

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
}