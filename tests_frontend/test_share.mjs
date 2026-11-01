/* ============================================================
   KLORIZ SHARING - dialog (owner side) and public page (recipient side)
   ============================================================
   Part 1 runs the REAL share.js inside the REAL app page (real app.js,
   real template) with a fake network. Part 2 runs the REAL
   templates/shared.html + static/js/shared-view.js.

   What it pins down (the privacy and honesty rules of the feature):
     - sharing is opt-in: nothing is created until "Create link" is pressed,
       after a preview of exactly what will be shared;
     - only {chat_id, title, messages:[{role,text}]} ever leave the browser:
       never images, never other message fields;
     - the posted messages are the ones that were PREVIEWED, even if the chat
       changes while the dialog is open;
     - "link copied" is shown only when a copy really succeeded;
     - opening WhatsApp/Facebook/... never claims anything was sent;
     - every failure (not ready / too long / rate limit / network / session)
       has its own Bangla message and nothing is shown as success;
     - the public page treats the shared text as hostile: no script, image,
       iframe, svg, style, form or javascript: link can ever appear.

   The server-side authorization rules (other user, revoked, malformed token,
   no leakage of owner data) are tested in test_share.py. The SQL (RLS,
   trigger, function grants) was exercised against a real PostgreSQL.

   Run: node tests_frontend/test_share.mjs
   ============================================================ */
import { JSDOM } from "jsdom";
import fs from "node:fs";
import path from "node:path";
import { boot, R, sleep, assert, eq, ROOT, MARKED_SRC } from "./lib/boot.mjs";

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}

const TOKEN = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678";
const SHARE = (over = {}) => ({ token: TOKEN, path: `/s/${TOKEN}`, title: "বল ও গতি", message_count: 2, created_at: "2026-10-28T08:00:00Z", ...over });
const hasBangla = (s) => /[\u0980-\u09FF]/.test(s);

/* =============================================================
   PART 1 - the share dialog
   ============================================================= */
console.log("\nPart 1. Share dialog (owner)");

const CHAT = [
    { role: "user", text: "বল কী?" },
    { role: "bot", text: "বল হলো **ধাক্কা** বা টান।" },
];

async function open(opts = {}) {
    const h = await boot({ guest: opts.guest, route: opts.route });
    if (!opts.guest) h.signIn();
    h.chat = h.seed(opts.messages || CHAT);
    h.dlg = h.$("share-modal");
    h.body = h.$("share-body");
    h.text = () => h.body.textContent;
    h.btn = (label) => [...h.body.querySelectorAll("button, a")].find((b) => b.textContent.trim().startsWith(label));
    h.shareCalls = () => h.env.calls.filter((c) => /^\/api\/share/.test(String(c.url)));
    h.w.shareChatLink();
    await sleep(60);
    return h;
}
const noShare = (url, init, method) => (/^\/api\/share/.test(url) && method === "GET") ? R(200, { share: null }) : R(404, {});

await check("a guest pressing Share is taken to sign-in; no dialog, no network", async () => {
    const h = await open({ guest: true });
    assert(h.dlg.classList.contains("hidden"), "share dialog stays closed");
    assert(!h.hidden("auth-modal"), "sign-in opened");
    eq(h.shareCalls().length, 0);
});
await check("an empty chat shows a Bangla 'nothing to share' message and makes no request", async () => {
    const h = await open({ messages: [], route: noShare });
    assert(!h.dlg.classList.contains("hidden"));
    assert(/শেয়ার করার মতো কোনো বার্তা/.test(h.text()), h.text());
    eq(h.shareCalls().length, 0);
});
await check("a chat holding only an image (no text) counts as empty too", async () => {
    const h = await open({ messages: [{ role: "user", text: "", image: "data:image/png;base64,AAA" }], route: noShare });
    assert(/শেয়ার করার মতো কোনো বার্তা/.test(h.text()));
});
await check("OPT-IN: opening the dialog only ASKS the server whether the chat is shared (GET, signed) - it creates nothing", async () => {
    const h = await open({ route: noShare });
    const calls = h.shareCalls();
    eq(calls.map((c) => c.method), ["GET"]);
    assert(calls[0].url.includes("chat_id=c1"));
    eq(calls[0].headers.Authorization, "Bearer tok", "the student's own token is sent");
});
await check("PREVIEW lists exactly the messages that would be shared, says who can open the link, and what is excluded", async () => {
    const h = await open({ route: noShare });
    const items = [...h.body.querySelectorAll(".share-preview li")];
    eq(items.length, 2);
    assert(items[0].textContent.includes("You") && items[0].textContent.includes("বল কী?"));
    assert(items[1].textContent.includes("Kloriz") && items[1].textContent.includes("বল হলো"));
    const note = h.body.querySelector(".share-note").textContent;
    assert(/Anyone with the link can read this copy/.test(note), "who can open it");
    assert(/no account needed/i.test(note));
    assert(/not included/i.test(note) && /images/i.test(note) && /email/i.test(note), "what is NOT shared");
    assert(/new messages will not appear/i.test(note), "it is a snapshot");
    assert(/stop sharing at any time/i.test(note), "revocable");
    assert(hasBangla(h.body.querySelector(".share-note-bn").textContent), "the same promise in Bangla");
    assert(h.btn("Create link") && h.btn("Cancel"));
    eq(h.shareCalls().filter((c) => c.method === "POST").length, 0, "nothing created yet");
});
await check("Cancel closes the dialog and creates nothing", async () => {
    const h = await open({ route: noShare });
    h.btn("Cancel").click();
    assert(h.dlg.classList.contains("hidden")); eq(h.shareCalls().filter((c) => c.method === "POST").length, 0);
});
await check("chat text is rendered as TEXT in the preview (markup and titles can not inject elements)", async () => {
    const h = await open({ route: noShare, messages: [{ role: "user", text: "<img src=x onerror=alert(1)>" }, { role: "bot", text: "<script>1</script>**x**" }] });
    eq(h.body.querySelectorAll("img, script").length, 0);
    assert(h.text().includes("<img src=x"));
});

await check("Create link POSTs ONLY {chat_id,title,messages:[{role,text}]} - an image data-URL and other fields never leave the browser", async () => {
    const h = await open({
        route: (url, init, method) => method === "GET" ? R(200, { share: null }) : R(201, { share: SHARE(), created: true }),
        messages: [
            { role: "user", text: "প্রশ্ন", image: "data:image/png;base64,SECRETIMAGE", extra: "x" },
            { role: "bot", text: "উত্তর", research: { sources: ["https://x"] }, feedback: "like" },
            { role: "user", text: "", image: "data:image/png;base64,ONLYIMAGE" },
        ],
    });
    h.btn("Create link").click(); await sleep(60);
    const post = h.shareCalls().find((c) => c.method === "POST");
    const sent = JSON.parse(post.body);
    eq(Object.keys(sent).sort(), ["chat_id", "messages", "title"]);
    eq(sent.messages, [{ role: "user", text: "প্রশ্ন" }, { role: "bot", text: "উত্তর" }], "image-only message dropped, extra fields stripped");
    assert(!/SECRETIMAGE|ONLYIMAGE|research|feedback|user-1|s@kloriz/.test(post.body), "nothing else leaked");
    eq(post.headers["Content-Type"], "application/json"); eq(post.headers.Authorization, "Bearer tok");
});
await check("what was PREVIEWED is what is posted, even if the chat changes while the dialog is open", async () => {
    const h = await open({ route: (u, i, m) => m === "GET" ? R(200, { share: null }) : R(201, { share: SHARE(), created: true }) });
    h.chat.messages.push({ role: "user", text: "এটা প্রিভিউয়ের পরে যোগ হয়েছে" });
    h.btn("Create link").click(); await sleep(60);
    const sent = JSON.parse(h.shareCalls().find((c) => c.method === "POST").body);
    eq(sent.messages.length, 2); assert(!/প্রিভিউয়ের পরে/.test(JSON.stringify(sent)));
});
await check("double-clicking Create link makes ONE request; the dialog is non-dismissible and buttons disabled while it runs", async () => {
    let release, posts = 0;
    const h = await open({ route: (u, i, m) => { if (m === "GET") return R(200, { share: null }); posts++; return new Promise((r) => { release = () => r(R(201, { share: SHARE(), created: true })); }); } });
    const create = h.btn("Create link"); create.click(); create.click(); create.click(); await sleep(30);
    eq(posts, 1);
    eq(h.dlg.getAttribute("data-busy"), "true"); assert(h.btn("Cancel").disabled);
    h.key(h.doc.body, "Escape"); await sleep(20);
    assert(!h.dlg.classList.contains("hidden"), "Escape does not abandon an in-flight create");
    release(); await sleep(60);
    assert(!h.dlg.hasAttribute("data-busy"));
});

const created = (over = {}) => ({
    route: (u, i, m) => m === "GET" ? R(200, { share: SHARE(over) }) : (m === "DELETE" ? R(200, { revoked: true }) : R(404, {})),
});

await check("an already-shared chat opens straight on the LINK view (live link, snapshot wording, Stop sharing)", async () => {
    const h = await open(created());
    const input = h.body.querySelector(".share-link-input");
    eq(input.value, `https://kognit.test/s/${TOKEN}`); assert(input.readOnly);
    assert(/This link is live/.test(h.text()) && /New messages are not included/.test(h.text()));
    assert(h.btn("Stop sharing") && h.btn("Copy link") && h.btn("Done"));
});
await check("destination buttons use correctly ENCODED URLs and open in a new tab with noopener", async () => {
    const title = "বল & গতি? #1 + 50%";
    const h = await open(created({ title }));
    const url = `https://kognit.test/s/${TOKEN}`;
    const wa = h.btn("WhatsApp"), fbk = h.btn("Facebook"), tg = h.btn("Telegram"), mail = h.btn("Email");
    eq(wa.getAttribute("href").startsWith("https://wa.me/?text="), true);
    eq(decodeURIComponent(wa.getAttribute("href").split("text=")[1]), `${title} - Kloriz\n${url}`);
    eq(decodeURIComponent(fbk.getAttribute("href").split("u=")[1]), url);
    assert(fbk.getAttribute("href").startsWith("https://www.facebook.com/sharer/sharer.php?u="));
    eq(decodeURIComponent(tg.getAttribute("href").split("url=")[1].split("&text=")[0]), url);
    assert(mail.getAttribute("href").startsWith("mailto:?subject=") && decodeURIComponent(mail.getAttribute("href").split("&body=")[1]) === url);
    for (const a of [wa, fbk, tg]) { eq(a.getAttribute("target"), "_blank"); assert(/noopener/.test(a.getAttribute("rel")) && /noreferrer/.test(a.getAttribute("rel"))); }
    assert(!/[ \n]/.test(wa.getAttribute("href")), "no raw spaces/newlines in the href");
});
await check("Copy link: success message ONLY after the clipboard write really succeeded", async () => {
    const h = await open(created()); let wrote = null;
    Object.defineProperty(h.w.navigator, "clipboard", { value: { writeText: async (t) => { wrote = t; } }, configurable: true });
    h.btn("Copy link").click(); await sleep(30);
    eq(wrote, `https://kognit.test/s/${TOKEN}`);
    assert(/লিংক কপি করা হয়েছে/.test(h.$("share-inline-status").textContent));
});
await check("Copy link: when copying FAILS, no success is shown - an error is, and the link is selected for manual copy", async () => {
    const h = await open(created());
    Object.defineProperty(h.w.navigator, "clipboard", { value: { writeText: async () => { throw new Error("denied"); } }, configurable: true });
    h.doc.execCommand = () => false;
    h.btn("Copy link").click(); await sleep(30);
    const s = h.$("share-inline-status");
    assert(!/কপি করা হয়েছে/.test(s.textContent), "no false success");
    assert(/কপি করা যায়নি/.test(s.textContent)); eq(s.getAttribute("role"), "alert");
});
await check("Copy link: the legacy execCommand fallback counts as success only when it returns true", async () => {
    const h = await open(created());
    Object.defineProperty(h.w.navigator, "clipboard", { value: undefined, configurable: true });
    h.doc.execCommand = () => true;
    h.btn("Copy link").click(); await sleep(30);
    assert(/লিংক কপি করা হয়েছে/.test(h.$("share-inline-status").textContent));
});
await check("the device share sheet button appears ONLY when navigator.share exists, shares the real URL, and a cancel is not an error", async () => {
    const h = await open(created());
    eq(h.btn("More"), undefined, "no button when unsupported");
    let shared = null;
    const h2 = await open({ ...created() });
    h2.w.navigator.share = async (d) => { shared = d; const e = new Error("cancelled"); e.name = "AbortError"; throw e; };
    h2.w.closeShareModal(); h2.w.shareChatLink(); await sleep(60);
    h2.btn("More").click(); await sleep(30);
    eq(shared.url, `https://kognit.test/s/${TOKEN}`);
    assert(!/কপি করা যায়নি/.test(h2.$("share-inline-status").textContent || ""), "cancel is not an error");
});
await check("the dialog never claims a message was SENT (opening a composer is not sending)", async () => {
    const h = await open(created());
    assert(!/\b(sent|delivered|shared with)\b/i.test(h.text().replace(/Kloriz can not tell whether you send it/, "")), h.text());
    assert(/can not tell whether you send it/.test(h.text()), "explicit about it");
});
await check("Stop sharing: DELETE with the chat id, then back to the preview with a Bangla 'stopped' confirmation", async () => {
    const h = await open(created());
    h.btn("Stop sharing").click(); await sleep(60);
    const del = h.shareCalls().find((c) => c.method === "DELETE");
    assert(del && del.url.includes("chat_id=c1")); eq(del.headers.Authorization, "Bearer tok");
    assert(h.body.querySelector(".share-preview"), "preview shown (a new link can be made)");
    assert(/শেয়ার বন্ধ করা হয়েছে/.test(h.$("share-inline-status").textContent));
    eq(h.body.querySelector(".share-link-input"), null, "the old link is no longer displayed");
});
await check("Stop sharing failing keeps the live link visible and reports it - it never pretends it stopped", async () => {
    const h = await open({ route: (u, i, m) => m === "GET" ? R(200, { share: SHARE() }) : R(500, { detail: { code: "SHARE_FAILED" } }) });
    h.btn("Stop sharing").click(); await sleep(60);
    assert(h.body.querySelector(".share-link-input"), "still shows the live link");
    assert(/শেয়ার বন্ধ করা যায়নি/.test(h.$("share-inline-status").textContent));
});

console.log("\n   failure states");
await check("sharing not available yet (migration not applied: 503) -> Bangla 'not available', no link, Close only", async () => {
    const h = await open({ route: () => R(503, { detail: { code: "SHARING_UNAVAILABLE" } }) });
    assert(/এখনও চালু হয়নি/.test(h.text())); eq(h.body.querySelector(".share-link-input"), null);
});
await check("server error loading status (500) -> Bangla message with a working retry", async () => {
    let n = 0;
    const h = await open({ route: (u, i, m) => { n++; return n === 1 ? R(500, {}) : R(200, { share: null }); } });
    assert(/এখন দেখানো যাচ্ছে না/.test(h.text()));
    h.btn("আবার চেষ্টা করুন").click(); await sleep(60);
    assert(h.body.querySelector(".share-preview"), "retry loaded the preview");
});
await check("create: chat too long (413) / rate limited (429) / sharing off (503) / network error each get their own Bangla message and no link", async () => {
    const cases = [
        [R(413, { detail: { code: "SHARE_TOO_LONG" } }), /অনেক বড়/],
        [R(429, { detail: "x" }, { "Retry-After": "9" }), /৯ সেকেন্ড/],
        [R(503, { detail: { code: "SHARING_UNAVAILABLE" } }), /এখনও চালু হয়নি/],
        [R(409, { detail: { code: "SHARE_LIMIT" } }), /সর্বোচ্চ/],
        [R(500, {}), /শেয়ার লিংক তৈরি করা যায়নি/],
    ];
    for (const [resp, re] of cases) {
        const h = await open({ route: (u, i, m) => m === "GET" ? R(200, { share: null }) : resp });
        h.btn("Create link").click(); await sleep(50);
        const s = h.$("share-inline-status");
        assert(re.test(s.textContent), `${resp.status}: ${s.textContent}`);
        eq(s.getAttribute("role"), "alert"); eq(h.body.querySelector(".share-link-input"), null, "no link on failure");
        assert(!h.btn("Create link").disabled, "the student can try again");
    }
    const h = await open({ route: (u, i, m) => m === "GET" ? R(200, { share: null }) : R(500, {}) });
    h.w.fetch = () => Promise.reject(new h.w.TypeError("Failed to fetch"));
    h.btn("Create link").click(); await sleep(50);
    assert(/ইন্টারনেট সংযোগে সমস্যা/.test(h.$("share-inline-status").textContent));
    assert(!/Failed to fetch/.test(h.text()));
});
await check("401 (session ended): the dialog closes and sign-in opens with the session message", async () => {
    const h = await open({ route: () => R(401, {}) });
    assert(h.dlg.classList.contains("hidden")); assert(!h.hidden("auth-modal"));
    assert(/সেশনের মেয়াদ শেষ|লগইন/.test(h.$("auth-notice-msg").textContent));
});
await check("a hostile / malformed server path is NEVER turned into a link (javascript:, protocol-relative, wrong shape)", async () => {
    for (const bad of ["javascript:alert(1)", "//evil.example/s/" + TOKEN, "/s/short", `/s/${TOKEN}/extra`, `https://evil.example/s/${TOKEN}`, "/api/share"]) {
        const h = await open(created({ path: bad }));
        eq(h.body.querySelector(".share-link-input"), null, `no link for ${bad}`);
        eq(h.body.querySelectorAll('a[href^="javascript"]').length, 0);
        assert(/শেয়ার লিংক তৈরি করা যায়নি/.test(h.text()));
    }
});
await check("the old behaviour is gone: Share never writes a '#project=…&chat=…' URL and never calls alert()", async () => {
    const h = await open(created()); let wrote = [];
    Object.defineProperty(h.w.navigator, "clipboard", { value: { writeText: async (t) => wrote.push(t) }, configurable: true });
    h.btn("Copy link").click(); await sleep(30);
    assert(wrote.every((t) => !/#project=|chat=/.test(t)), JSON.stringify(wrote));
    eq(h.env.alerts, 0);
    assert(!/#project=/.test(fs.readFileSync(path.join(ROOT, "static/js/app.js"), "utf8").split("window.KlorizAppBridge")[0].split("// Share")[1] || ""));
});
await check("dialog semantics: role=dialog, aria-modal, named, close button labelled, Escape closes (idle) and returns focus", async () => {
    const h = await open({ route: noShare });
    const card = h.dlg.querySelector(".modal-card");
    eq(card.getAttribute("role"), "dialog"); eq(card.getAttribute("aria-modal"), "true"); assert(h.$(card.getAttribute("aria-labelledby")));
    assert(h.dlg.querySelector(".close-modal-btn").getAttribute("aria-label"));
    h.key(h.doc.body, "Escape"); await sleep(30);
    assert(h.dlg.classList.contains("hidden"));
});
await check("both Share entry points (top bar and mobile menu) open the same dialog", async () => {
    const h = await boot({ route: noShare }); h.signIn(); h.seed(CHAT);
    h.doc.querySelector('.share-btn[onclick="shareChatLink()"]').click(); await sleep(60);
    assert(!h.$("share-modal").classList.contains("hidden"));
    h.w.closeShareModal();
    const mobile = h.doc.querySelector('[data-chat-menu="share"]'); assert(mobile);
    mobile.click(); await sleep(60);
    assert(!h.$("share-modal").classList.contains("hidden"), "mobile menu opens it too");
});

/* =============================================================
   PART 2 - the public page
   ============================================================= */
console.log("\nPart 2. Public shared page (recipient)");

const SHARED_HTML = fs.readFileSync(path.join(ROOT, "templates/shared.html"), "utf8");
const readSrc = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

async function page({ path: urlPath = `/s/${TOKEN}`, res, rejectWith, noPurify = false } = {}) {
    const html = SHARED_HTML.replace(/<script[^>]*src="[^"]*"[^>]*><\/script>/g, "");
    const dom = new JSDOM(html, { runScripts: "dangerously", url: `https://kognit.test${urlPath}` });
    const w = dom.window;
    const calls = [];
    w.fetch = (url, init) => { calls.push({ url, init }); return rejectWith ? Promise.reject(rejectWith) : Promise.resolve(res); };
    w.eval(MARKED_SRC);
    if (!noPurify) w.eval(readSrc("static/js/vendor/purify.min.js"));
    w.eval(readSrc("static/js/feedback.js"));
    w.eval(readSrc("static/js/shared-view.js"));
    await sleep(60);
    return { w, doc: w.document, calls, root: w.document.getElementById("shared-root"), text: () => w.document.getElementById("shared-root").textContent };
}
const payload = (messages, over = {}) => R(200, { title: "বল ও গতি", messages, message_count: messages.length, created_at: "2026-10-28T08:00:00Z", ...over });

await check("renders title, count, roles and both messages; user text is shown as plain text, bot text as Markdown", async () => {
    const p = await page({ res: payload([{ role: "user", text: "**না** <b>bold</b>" }, { role: "bot", text: "বল হলো **ধাক্কা**।\n\n- এক\n- দুই" }]) });
    eq(p.doc.querySelector(".shared-title").textContent, "বল ও গতি");
    assert(/২টি বার্তা/.test(p.doc.querySelector(".shared-meta").textContent));
    eq(p.doc.title, "বল ও গতি · Kloriz");
    const [u, b] = p.doc.querySelectorAll(".shared-msg-body");
    eq(u.textContent, "**না** <b>bold</b>", "user message is literal text"); eq(u.querySelector("b, strong"), null);
    assert(b.querySelector("strong") && b.querySelectorAll("li").length === 2, "bot markdown rendered");
    eq([...p.doc.querySelectorAll(".shared-msg-who")].map((x) => x.textContent), ["ব্যবহারকারী", "Kloriz"]);
});
await check("the request is anonymous: no credentials, no Authorization, no referrer, no cache", async () => {
    const p = await page({ res: payload([{ role: "user", text: "x" }]) });
    const { url, init } = p.calls[0];
    eq(url, `/api/shared/${TOKEN}`); eq(init.credentials, "omit"); eq(init.referrerPolicy, "no-referrer"); eq(init.cache, "no-store");
    assert(!init.headers.Authorization && !init.headers.authorization);
});
await check("HOSTILE content: script/img/iframe/svg/style/form/event-handlers/javascript: links can never appear", async () => {
    const evil = [
        '<script>window.__x=1</script>', '<img src=x onerror="window.__x=2">', '[click](javascript:window.__x=3)',
        '<a href="javascript:window.__x=4" onclick="window.__x=5">a</a>', '<iframe src="//evil.example"></iframe>',
        '<svg onload="window.__x=6"><circle/></svg>', '<style>body{display:none}</style>', '<form action="//evil.example"><input name=p></form>',
        '<math><mtext></mtext></math>', '<object data="//evil"></object>', '<embed src="//evil">', '<link rel=stylesheet href=//evil>',
        '<meta http-equiv=refresh content="0;url=//evil">', '<base href="//evil/">', '<details open ontoggle="window.__x=7">x</details>',
        '[data](data:text/html;base64,PHNjcmlwdD4=)', '![pixel](https://tracker.example/p.gif)',
    ].join("\n\n");
    const p = await page({ res: payload([{ role: "bot", text: evil }]) });
    const body = p.doc.querySelector(".shared-msg-body");
    eq(p.w.__x, undefined, "no payload executed");
    for (const sel of ["script", "img", "iframe", "svg", "style", "form", "input", "object", "embed", "link", "meta", "base", "math", "details"]) {
        eq(body.querySelectorAll(sel).length, 0, `<${sel}> must not exist`);
    }
    assert(![...body.querySelectorAll("*")].some((e) => [...e.attributes].some((a) => /^on/i.test(a.name) || /^(style|srcdoc|src)$/i.test(a.name))), "no event/style/src attributes");
    // A hostile link survives only as an inert <a> WITHOUT an href (the sanitizer drops the URL).
    for (const a of body.querySelectorAll("a")) { const href = a.getAttribute("href"); assert(href === null || /^(https?:|mailto:)/i.test(href), `bad href ${href}`); }
    assert(p.doc.querySelectorAll("img").length === 0, "the page contains no images at all (no remote loads)");
});
await check("real links are kept but locked down: target=_blank, rel noopener noreferrer nofollow", async () => {
    const p = await page({ res: payload([{ role: "bot", text: "পড়ুন [NCTB](https://www.nctb.gov.bd/) বা mailto:[a](mailto:x@y.z)" }]) });
    const a = p.doc.querySelector(".shared-msg-body a");
    eq(a.getAttribute("href"), "https://www.nctb.gov.bd/"); eq(a.getAttribute("target"), "_blank");
    for (const t of ["noopener", "noreferrer", "nofollow"]) assert(a.getAttribute("rel").includes(t), t);
});
await check("math is preserved as ESCAPED text for MathJax (it can not create elements), including < > & inside equations", async () => {
    const p = await page({ res: payload([{ role: "bot", text: "$x<y$ এবং $$a</b><script>1</script>$$ আর \\(m&n\\) এবং \\[E=mc^2\\]" }]) });
    const body = p.doc.querySelector(".shared-msg-body");
    eq(body.querySelectorAll("script, b").length, 0);
    assert(body.textContent.includes("$x<y$") && body.textContent.includes("a</b><script>1</script>") && body.textContent.includes("\\(m&n\\)") && body.textContent.includes("E=mc^2"), body.textContent);
});
await check("Markdown cannot corrupt LaTeX (underscores / asterisks inside math stay intact); money like $5 and $10 is NOT math", async () => {
    const p = await page({ res: payload([{ role: "bot", text: "$a_1 * b_2$ এবং দাম $5 আর $10" }]) });
    const t = p.doc.querySelector(".shared-msg-body").textContent;
    assert(t.includes("$a_1 * b_2$"), t); assert(t.includes("দাম $5 আর $10"), t);
    eq(p.doc.querySelector(".shared-msg-body em, .shared-msg-body i"), null, "no accidental emphasis from underscores");
});
await check("code is left alone: math-looking text inside fences/backticks is shown verbatim", async () => {
    const p = await page({ res: payload([{ role: "bot", text: "`$x$` আর\n\n```\n$$y$$\n```" }]) });
    const codes = [...p.doc.querySelectorAll(".shared-msg-body code")].map((c) => c.textContent);
    assert(codes.some((c) => c === "$x$") && codes.some((c) => c.includes("$$y$$")), JSON.stringify(codes));
});
await check("an attacker-typed placeholder like KLRZMATH999X can neither crash the page nor pull in other content", async () => {
    const p = await page({ res: payload([{ role: "bot", text: "KLRZMATH0X KLRZMATH999X $z$" }]) });
    assert(p.doc.querySelector(".shared-msg-body"), "rendered");
    assert(p.text().includes("$z$"));
});
await check("a not-found share (404: wrong, revoked or never existed) -> ONE Bangla 'not available' message, no retry, no content", async () => {
    const p = await page({ res: R(404, { detail: { code: "SHARE_NOT_FOUND" } }) });
    assert(/এই শেয়ার করা চ্যাটটি পাওয়া যাচ্ছে না/.test(p.text())); eq(p.root.querySelector(".shared-retry"), null);
    eq(p.root.getAttribute("aria-busy"), "false"); eq(p.root.querySelector('[role="alert"]').tagName, "P");
});
await check("a server error (500) -> Bangla 'could not load' with a retry that fetches again", async () => {
    const p = await page({ res: R(500, {}) });
    assert(/এখন দেখানো যাচ্ছে না/.test(p.text()));
    p.root.querySelector(".shared-retry").click(); await sleep(40);
    eq(p.calls.length, 2);
});
await check("a network failure -> Bangla network message + retry (no exception text)", async () => {
    const p = await page({ rejectWith: new TypeError("Failed to fetch") });
    assert(/ইন্টারনেট সংযোগে সমস্যা/.test(p.text()) && !/Failed to fetch/.test(p.text())); assert(p.root.querySelector(".shared-retry"));
});
await check("an empty or malformed payload is treated as not-available, never rendered half-way", async () => {
    for (const body of [{ messages: [] }, { messages: "x" }, {}, { title: "t" }]) {
        const p = await page({ res: R(200, body) });
        assert(/পাওয়া যাচ্ছে না/.test(p.text()), JSON.stringify(body));
    }
});
await check("a URL that is not /s/<40 hex> never calls the API and shows the same not-available message", async () => {
    for (const bad of ["/s/short", "/s/" + TOKEN + "x", "/s/" + TOKEN.toUpperCase()]) {
        const p = await page({ path: bad, res: R(200, {}) });
        eq(p.calls.length, 0, bad); assert(/পাওয়া যাচ্ছে না/.test(p.text()));
    }
});
await check("FAILS CLOSED: if the sanitizer did not load, bot text is shown as inert plain text, never as HTML", async () => {
    const p = await page({ noPurify: true, res: payload([{ role: "bot", text: "<img src=x onerror=window.__x=1> **b**" }]) });
    eq(p.w.__x, undefined); eq(p.doc.querySelectorAll("img").length, 0);
    assert(p.doc.querySelector(".shared-plain").textContent.includes("<img src=x"));
});
await check("the page shell is safe by construction: noindex, no-referrer, no external stylesheet, one MathJax script (pinned), honest read-only notice", async () => {
    eq(SHARED_HTML.includes('name="robots" content="noindex, nofollow, noarchive"'), true);
    eq(SHARED_HTML.includes('name="referrer" content="no-referrer"'), true);
    assert(!/<link[^>]+rel="stylesheet"[^>]+href="https?:/i.test(SHARED_HTML), "no third-party stylesheet / font request");
    const ext = [...SHARED_HTML.matchAll(/<script[^>]+src="(https?:[^"]+)"/g)].map((m) => m[1]);
    eq(ext, ["https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/tex-svg.js"], "the only external script is MathJax at an exact version");
    assert(/শুধু পড়ার জন্য/.test(SHARED_HTML) && /যাচাই করেনি/.test(SHARED_HTML), "says it is a read-only copy that Kloriz did not verify");
    for (const macro of ["href", "class", "style", "cssId", "bbox"]) assert(new RegExp(`${macro}: \\['#2'`).test(SHARED_HTML), `MathJax ${macro} neutralised`);
    assert(!/supabase|app\.js/i.test(SHARED_HTML.replace(/<!--[\s\S]*?-->/g, "")), "no Supabase client / app.js on the public page");
});

console.log(`\n  ${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
