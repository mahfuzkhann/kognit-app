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

let editingProjectId = null;
let editingChatId = null;
let searchQuery = "";

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
    supabaseClient.auth.onAuthStateChange(async (_event, session) => {
        // BUG-2 fix: supabase-js fires this callback for many event types,
        // not just an actual login/logout - including background token
        // refreshes and tab-visibility session rechecks. Reacting to every
        // one of those by reloading projects (and re-picking the active
        // chat via initializeActiveProject) was silently yanking the user
        // out of whatever chat they had open, with no click from them.
        // Only react to events that represent a real identity change.
        if (_event !== "SIGNED_IN" && _event !== "SIGNED_OUT") {
            return;
        }
        currentUser = session ? session.user : null;
        updateAuthUI(currentUser);
        if (currentUser) {
            await loadProjectsFromDatabase();
        } else {
            loadProjectsFromLocalStorage();
        }
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
            const localData = JSON.parse(localStorage.getItem("kognit_projects")) || [];
            if (localData.length > 0) {
                projects = normalizeProjectTitles(localData);
                for (const proj of projects) {
                    await syncProjectToDatabase(proj);
                }
            } else {
                projects = [];
                createNewProject();
                return;
            }
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
    if (projects.length === 0) {
        createNewProject();
        return;
    }

    activeProjectId = projects[0].id;
    const mostRecentChat = (projects[0].chats && projects[0].chats.length > 0) ? projects[0].chats[0] : null;
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

    chat.messages.forEach(msg => {
        const div = document.createElement("div");
        div.className = msg.role === "user" ? "user-message" : "bot-message";

        if (msg.role === "user") {
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
        } else {
            div.innerHTML = marked.parse(msg.text);
        }

        chatBox.appendChild(div);
    });

    if (window.MathJax) {
        MathJax.typesetPromise([chatBox]).catch((err) => console.error(err));
    }
    chatBox.scrollTop = chatBox.scrollHeight;
}

function renderHistoryList() {
    const historyList = document.getElementById("history-list");
    historyList.innerHTML = "";

    let hasMatch = false;

    projects.forEach(proj => {
        const projTitleMatches = proj.title.toLowerCase().includes(searchQuery);
        const matchingChats = (proj.chats || []).filter(chat => 
            chat.title.toLowerCase().includes(searchQuery)
        );

        if (projTitleMatches || matchingChats.length > 0) {
            hasMatch = true;

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
                const wrapper = document.createElement("div");
                wrapper.className = "project-title-wrapper";
                wrapper.innerHTML = `<span>📁</span><span class="project-title-text">${proj.title}</span>`;
                wrapper.onclick = () => {
                    activeProjectId = proj.id;
                    if (proj.chats.length > 0) loadChat(proj.id, proj.chats[0].id);
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
            chatsList.className = "chats-list";

            const chatsToDisplay = projTitleMatches && searchQuery !== "" ? proj.chats : (searchQuery !== "" ? matchingChats : proj.chats);

            (chatsToDisplay || []).forEach(chat => {
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
                    titleText.textContent = "💬 " + chat.title;
                    titleText.onclick = () => loadChat(proj.id, chat.id);

                    const chatActions = document.createElement("div");
                    chatActions.className = "item-actions";

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

                    chatActions.appendChild(editChatBtn);
                    chatActions.appendChild(delChatBtn);

                    chatItem.appendChild(titleText);
                    chatItem.appendChild(chatActions);
                }

                chatsList.appendChild(chatItem);
            });

            projCard.appendChild(chatsList);
            historyList.appendChild(projCard);
        }
    });

    if (!hasMatch && searchQuery !== "") {
        historyList.innerHTML = `<div class="no-results">No projects or chats found matching "${searchQuery}"</div>`;
    }
}

async function deleteProject(projId) {
    if (!confirm("Delete this project?")) return;
    projects = projects.filter(p => p.id !== projId);
    await deleteProjectFromDatabase(projId);
    saveProjectsToStorage();

    if (projects.length === 0) {
        createNewProject();
    } else {
        activeProjectId = projects[0].id;
        activeChatId = (projects[0].chats && projects[0].chats.length > 0) ? projects[0].chats[0].id : null;
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

window.handleKeyPress = function(event) {
    if (event.key === "Enter") sendMessage();
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

    try {
        const response = await fetch("/api/pdf/upload", { method: "POST", body: formData });
        await response.json();
        chatBox.removeChild(loadingMsg);

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

window.sendMessage = async function() {
    const inputField = document.getElementById("user-input");
    const chatBox = document.getElementById("chat-box");
    const promptText = inputField.value.trim();

    if (!promptText && !selectedImageFile) return;

    const proj = projects.find(p => p.id === activeProjectId);
    if (!proj) return;
    const currentChat = proj.chats.find(c => c.id === activeChatId);
    if (!currentChat) return;

    if (currentChat.messages.filter(m => m.role === "user").length === 0 && currentChat.title === "New Chat") {
        currentChat.title = generateChatTitle(promptText || "PDF/Image Query");
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
        chatBox.removeChild(loadingDiv);

        const replyText = data.reply || "No response received.";
        currentChat.messages.push({ role: "bot", text: replyText });
        saveProjectsToStorage();

        const botDiv = document.createElement("div");
        botDiv.className = "bot-message";
        botDiv.innerHTML = marked.parse(replyText);
        chatBox.appendChild(botDiv);

        if (window.MathJax) {
            MathJax.typesetPromise([botDiv]).catch((err) => console.error(err));
        }
    } catch (error) {
        if (chatBox.contains(loadingDiv)) chatBox.removeChild(loadingDiv);
        const errorDiv = document.createElement("div");
        errorDiv.className = "bot-message";
        errorDiv.textContent = "Error connecting to Kognit Engine.";
        chatBox.appendChild(errorDiv);
    }

    chatBox.scrollTop = chatBox.scrollHeight;
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

    try {
        const res = await fetch("/api/quiz/generate", { method: "POST", body: formData });
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