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
            projects = data.map(p => ({
                id: p.id,
                title: p.title,
                chats: p.chats || []
            }));
        } else {
            const localData = JSON.parse(localStorage.getItem("kognit_projects")) || [];
            if (localData.length > 0) {
                projects = localData;
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
    projects = JSON.parse(localStorage.getItem("kognit_projects")) || [];
    initializeActiveProject();
}

function initializeActiveProject() {
    if (projects.length === 0) {
        createNewProject();
    } else {
        activeProjectId = projects[0].id;
        if (projects[0].chats && projects[0].chats.length > 0) {
            activeChatId = projects[0].chats[0].id;
        } else {
            createNewChat();
        }
        renderHistoryList();
        loadChat(activeProjectId, activeChatId);
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
        const titleText = promptText || "PDF/Image Query";
        currentChat.title = titleText.length > 18 ? titleText.substring(0, 18) + "..." : titleText;
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

    const formData = new FormData();
    formData.append("prompt", promptText || "Analyze this document/image.");
    formData.append("mode", currentMode);
    formData.append("board", document.getElementById("board-select").value);
    formData.append("user_class", document.getElementById("class-select").value);
    formData.append("stream", document.getElementById("stream-select").value);

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

// Export & Share
window.exportChatToMarkdown = async function() {
    const proj = projects.find(p => p.id === activeProjectId);
    if (!proj) return;
    const currentChat = proj.chats.find(c => c.id === activeChatId);
    if (!currentChat) return;

    const formData = new FormData();
    formData.append("title", currentChat.title || "Kognit Study Note");
    formData.append("chat_data", JSON.stringify(currentChat.messages));

    try {
        const res = await fetch("/api/export/markdown", { method: "POST", body: formData });
        const blob = await res.blob();
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${currentChat.title.replace(/[^a-zA-Z0-9]/g, "_")}.md`;
        document.body.appendChild(a);
        a.click();
        a.remove();
    } catch (e) {
        alert("Failed to export Markdown.");
    }
};

// ---- PDF export math-rendering helper ----------------------------------
// html2canvas (used internally by html2pdf.js) does not correctly compose
// the nested <g transform="matrix(...)"> groups that MathJax's SVG output
// uses for display-mode equations, fractions, and radicals (multi-part
// constructs at different baselines). This produced doubled/overlapping
// strokes in exported PDFs (e.g. quadratic formula) even though simple
// inline equations (E=mc^2, H2O) rendered fine, since those use a flatter
// SVG structure.
//
// Fix: rasterize each MathJax <svg> to a flat PNG on an off-screen canvas
// before capture. A flat PNG has no transform stack for html2canvas to
// misinterpret. This is export-only — it never touches the live, on-screen
// SVG that the user sees while chatting.
async function svgToPngDataUrl(svgElement, scale = 3) {
    const rect = svgElement.getBoundingClientRect();
    const width = Math.max(1, Math.ceil(rect.width));
    const height = Math.max(1, Math.ceil(rect.height));

    const clone = svgElement.cloneNode(true);
    clone.setAttribute("width", width * scale);
    clone.setAttribute("height", height * scale);
    if (!clone.getAttribute("xmlns")) {
        clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    }

    const svgString = new XMLSerializer().serializeToString(clone);
    const svgBlob = new Blob([svgString], { type: "image/svg+xml;charset=utf-8" });
    const blobUrl = URL.createObjectURL(svgBlob);

    try {
        const loadedImage = await new Promise((resolve, reject) => {
            const image = new Image();
            image.onload = () => resolve(image);
            image.onerror = () => reject(new Error("Failed to load SVG as image for PDF export"));
            image.src = blobUrl;
        });

        const canvas = document.createElement("canvas");
        canvas.width = width * scale;
        canvas.height = height * scale;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(loadedImage, 0, 0, canvas.width, canvas.height);

        return { dataUrl: canvas.toDataURL("image/png"), width, height };
    } finally {
        URL.revokeObjectURL(blobUrl);
    }
}

window.exportChatToPDF = async function() {
    const chatElement = document.getElementById("chat-box");
    const proj = projects.find(p => p.id === activeProjectId);
    const currentChat = proj ? proj.chats.find(c => c.id === activeChatId) : null;
    const title = currentChat ? currentChat.title : "Kognit_Note";

    // Ensure all math in the chat has fully finished typesetting before
    // any capture step touches the DOM.
    if (window.MathJax && window.MathJax.typesetPromise) {
        try {
            await MathJax.typesetPromise([chatElement]);
        } catch (err) {
            console.error("MathJax typeset error before PDF export:", err);
        }
    }

    // Temporarily swap each MathJax SVG for a rasterized PNG (export-only).
    // restoreList tracks what needs to be undone in the finally block below.
    const restoreList = [];
    const mathSvgs = chatElement.querySelectorAll("mjx-container svg");

    for (const svg of mathSvgs) {
        try {
            const { dataUrl, width, height } = await svgToPngDataUrl(svg);
            const img = document.createElement("img");
            img.src = dataUrl;
            img.style.width = width + "px";
            img.style.height = height + "px";
            img.style.display = "inline-block";
            img.className = "kognit-pdf-export-math-img";

            const originalDisplay = svg.style.display;
            svg.style.display = "none";
            svg.insertAdjacentElement("afterend", img);

            restoreList.push({ svg, img, originalDisplay });
        } catch (err) {
            // If rasterization fails for a single equation, leave its SVG
            // as-is (previous behavior) rather than failing the whole export.
            console.error("Failed to rasterize an equation for PDF export:", err);
        }
    }

    const opt = {
        margin:       10,
        filename:     `${title.replace(/[^a-zA-Z0-9]/g, "_")}.pdf`,
        image:        { type: 'jpeg', quality: 0.98 },
        html2canvas:  { scale: 2 },
        jsPDF:        { unit: 'mm', format: 'a4', orientation: 'portrait' }
    };

    try {
        await html2pdf().set(opt).from(chatElement).save();
    } finally {
        // Always restore the live chat to its original state, whether
        // export succeeded or failed.
        restoreList.forEach(({ svg, img, originalDisplay }) => {
            img.remove();
            svg.style.display = originalDisplay;
        });
    }
};

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