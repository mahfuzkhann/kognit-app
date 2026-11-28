/* ============================================================
   KOGNIT - NOTEBOOK SYNC CORE TESTS (project-sync.js)
   Loads the REAL static/js/project-sync.js. The fake server below
   re-implements the documented behaviour of save_user_project
   (supabase/migrations/0019_project_versioning_cas.sql) including its
   replay rule; the SQL itself is proven on real PostgreSQL by
   supabase/tests/project_cas_0019_test.sql, NOT by this file.

   Run: node tests_frontend/test_project_sync.mjs
   ============================================================ */
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const Sync = require(path.resolve(HERE, "..", "static/js/project-sync.js"));

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
function assert(c, m) { if (!c) throw new Error(m || "assertion failed"); }
function eq(a, b, m) { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(`${m || "mismatch"}: expected ${JSON.stringify(b)}, got ${JSON.stringify(a)}`); }
const J = (x) => JSON.parse(JSON.stringify(x));
const msg = (role, text) => ({ role, text });
const chat = (id, messages = [], extra = {}) => ({ id, title: id, messages, ...extra });

/* ---------- fake server (mirrors 0019 semantics) ---------- */
function makeServer(owner = "u1") {
    const rows = new Map();       // id -> {owner,title,chats,version}
    const log = [];
    let failNext = 0;             // number of upcoming calls to fail with a network error
    let errorCode = null;
    let loseReplyOnce = false;
    const srv = {
        rows, log,
        failNetwork(n = 1) { failNext = n; },
        failWithCode(c) { errorCode = c; },
        loseReply() { loseReplyOnce = true; },
        foreignRow(id) { rows.set(id, { owner: "someone-else", title: "x", chats: [], version: 1 }); },
        async rpc(a) {
            log.push(J(a));
            if (failNext > 0) { failNext--; throw new Error("network down"); }
            if (errorCode) { const c = errorCode; errorCode = null; return { data: null, error: { code: c, message: "e" } }; }
            const out = apply(a);
            if (loseReplyOnce && out.status === "ok") { loseReplyOnce = false; throw new Error("reply lost"); }
            return { data: out, error: null };
        },
    };
    function apply(a) {
        const r = rows.get(a.p_id);
        if (!r || r.owner !== owner) {
            if (r && r.owner !== owner) return { status: "id_unavailable" };
            if (a.p_expected_version !== null) return { status: "not_found" };
            rows.set(a.p_id, { owner, title: a.p_title, chats: J(a.p_chats), version: 1 });
            return { status: "ok", created: true, version: 1, updated_at: "t1" };
        }
        if (a.p_expected_version !== null && r.version === a.p_expected_version) {
            r.title = a.p_title; r.chats = J(a.p_chats); r.version += 1;
            return { status: "ok", version: r.version, updated_at: "t" + r.version };
        }
        if (r.title === a.p_title && JSON.stringify(r.chats) === JSON.stringify(a.p_chats) &&
            (a.p_expected_version === null || r.version === a.p_expected_version + 1)) {
            return { status: "ok", replayed: true, version: r.version, updated_at: "t" + r.version };
        }
        return { status: "conflict", server: { id: a.p_id, title: r.title, chats: J(r.chats), version: r.version, updated_at: "t" + r.version } };
    }
    return srv;
}
const newProject = (id = "p1", chats = []) => ({ id, title: "Biology", chats, version: null });

console.log("\n== ids and creation time ==");
await check("newId returns RFC-4122 v4 UUIDs and never repeats", () => {
    const seen = new Set();
    for (let i = 0; i < 2000; i++) {
        const id = Sync.newId();
        assert(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(id), id);
        assert(!seen.has(id), "duplicate id");
        seen.add(id);
    }
});
await check("new ids satisfy the database id pattern", () => {
    assert(/^[A-Za-z0-9_-]{1,100}$/.test(Sync.newId()));
});
await check("createdAtOf: explicit createdAt wins, legacy id still works, UUID without createdAt has none", () => {
    eq(Sync.createdAtOf({ id: "chat_1700000000000" }, "chat"), 1700000000000);
    eq(Sync.createdAtOf({ id: "x", createdAt: 1800000000000 }, "chat"), 1800000000000);
    eq(Sync.createdAtOf({ id: Sync.newId() }, "chat"), null);
    eq(Sync.createdAtOf({ id: "chat_1700000000000", createdAt: 5 }, "chat"), 5);
    eq(Sync.createdAtOf({ id: "chat_abc" }, "chat"), null);
    eq(Sync.createdAtOf(null, "chat"), null);
});

console.log("\n== merge rule ==");
await check("chat only on server is kept", () => {
    const m = Sync.mergeChats([], [chat("a", [msg("user", "hi")])]);
    eq(m.map((c) => c.id), ["a"]);
});
await check("chat only local (not deleted) is kept and goes first", () => {
    const m = Sync.mergeChats([chat("l")], [chat("s")]);
    eq(m.map((c) => c.id), ["l", "s"]);
});
await check("a chat deleted locally is NOT resurrected from the server", () => {
    const m = Sync.mergeChats([], [chat("gone"), chat("s")], { deletedChatIds: new Set(["gone"]) });
    eq(m.map((c) => c.id), ["s"]);
});
await check("identical chats stay single", () => {
    const c = chat("a", [msg("user", "q")]);
    eq(Sync.mergeChats([J(c)], [J(c)]).length, 1);
});
await check("server is a prefix of local -> local wins", () => {
    const m = Sync.mergeChats([chat("a", [msg("user", "q"), msg("bot", "a")])], [chat("a", [msg("user", "q")])]);
    eq(m[0].messages.length, 2);
});
await check("local is a prefix of server -> server wins", () => {
    const m = Sync.mergeChats([chat("a", [msg("user", "q")])], [chat("a", [msg("user", "q"), msg("bot", "a")])]);
    eq(m[0].messages.length, 2);
});
await check("...except while that chat is generating a response here (busy): local is kept", () => {
    const local = [chat("a", [msg("user", "q")])];
    const m = Sync.mergeChats(local, [chat("a", [msg("user", "q"), msg("bot", "other device")])], { busyChatIds: new Set(["a"]) });
    eq(m[0].messages.length, 1);
});
await check("diverged chats: server keeps the id, local survives as a NEW copy with a UUID", () => {
    const local = [chat("a", [msg("user", "q"), msg("bot", "LOCAL")], { title: "Cells" })];
    const server = [chat("a", [msg("user", "q"), msg("bot", "SERVER")])];
    const m = Sync.mergeChats(local, server, { makeId: () => "new-id" });
    eq(m.length, 2);
    const orig = m.find((c) => c.id === "a"), copy = m.find((c) => c.id === "new-id");
    eq(orig.messages[1].text, "SERVER");
    eq(copy.messages[1].text, "LOCAL");
    assert(copy.title === "Cells" + Sync.COPY_SUFFIX, copy.title);
    assert(typeof copy.createdAt === "number");
});
await check("no message is ever lost: every message of both sides is present after a diverged merge", () => {
    const local = [chat("a", [msg("user", "1"), msg("bot", "L")])];
    const server = [chat("a", [msg("user", "1"), msg("bot", "S")])];
    const texts = Sync.mergeChats(local, server).flatMap((c) => c.messages.map((m) => m.text));
    assert(texts.includes("L") && texts.includes("S"));
});
await check("title: server's, unless renamed locally", () => {
    const l = { id: "p", title: "Mine", chats: [] }, s = { id: "p", title: "Theirs", chats: [], version: 4 };
    eq(Sync.mergeProject(l, s, {}).title, "Theirs");
    eq(Sync.mergeProject(l, s, { titleRenamedLocally: true }).title, "Mine");
    eq(Sync.mergeProject(l, s, {}).version, 4);
});
await check("merge never mutates its inputs", () => {
    const l = [chat("a", [msg("user", "1"), msg("bot", "L")])], s = [chat("a", [msg("user", "1"), msg("bot", "S")])];
    const l0 = J(l), s0 = J(s);
    Sync.mergeChats(l, s);
    eq(l, l0); eq(s, s0);
});
await check("applyMerged keeps the identity of live chat objects and their messages array (streaming safety)", () => {
    const live = chat("a", [msg("user", "q")]);
    const messagesRef = live.messages;
    const project = { id: "p", title: "t", chats: [live], version: 1 };
    const merged = Sync.mergeProject(project, { id: "p", title: "t", chats: [chat("a", [msg("user", "q"), msg("bot", "x")]), chat("b")], version: 2 }, {});
    Sync.applyMerged(project, merged);
    assert(project.chats.find((c) => c.id === "a") === live, "chat object replaced");
    assert(live.messages === messagesRef, "messages array replaced");
    eq(live.messages.length, 2);
    eq(project.chats.length, 2);
    eq(project.version, 2);
});

console.log("\n== saver: happy path ==");
await check("a new project is created with expected=null and gets version 1", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject();
    const r = await saver.saveProject(p);
    eq(r.status, "saved"); eq(p.version, 1); eq(s.log[0].p_expected_version, null);
});
await check("a following save sends the stored version and bumps it", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject(); await saver.saveProject(p);
    p.chats.push(chat("c1")); const r = await saver.saveProject(p);
    eq(r.status, "saved"); eq(p.version, 2); eq(s.log[1].p_expected_version, 1);
});

console.log("\n== saver: two devices ==");
await check("device B saves after device A: conflict is merged, nothing lost, merge is saved", async () => {
    const s = makeServer();
    const A = Sync.createSaver({ rpc: (a) => s.rpc(a) }), B = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const pa = newProject("p1", [chat("c0", [msg("user", "shared")])]);
    await A.saveProject(pa);
    const pb = J(pa);                                   // B loaded version 1
    pa.chats.unshift(chat("fromA", [msg("user", "A")]));
    await A.saveProject(pa);                            // server v2
    pb.chats.unshift(chat("fromB", [msg("user", "B")]));
    const r = await B.saveProject(pb);                  // stale -> conflict -> merge
    eq(r.status, "merged");
    const ids = s.rows.get("p1").chats.map((c) => c.id).sort();
    eq(ids, ["c0", "fromA", "fromB"]);
    eq(pb.version, s.rows.get("p1").version);
});
await check("two devices edit the SAME chat: both versions survive", async () => {
    const s = makeServer();
    const A = Sync.createSaver({ rpc: (a) => s.rpc(a) }), B = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const pa = newProject("p1", [chat("c", [msg("user", "q")])]);
    await A.saveProject(pa);
    const pb = J(pa);
    pa.chats[0].messages.push(msg("bot", "answer-A")); await A.saveProject(pa);
    pb.chats[0].messages.push(msg("bot", "answer-B"));
    const r = await B.saveProject(pb);
    eq(r.status, "merged");
    const texts = s.rows.get("p1").chats.flatMap((c) => c.messages.map((m) => m.text));
    assert(texts.includes("answer-A") && texts.includes("answer-B"), JSON.stringify(texts));
});
await check("deleted-here chat is not resurrected by a conflict merge", async () => {
    const s = makeServer();
    const A = Sync.createSaver({ rpc: (a) => s.rpc(a) }), B = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const pa = newProject("p1", [chat("keep"), chat("drop")]);
    await A.saveProject(pa);
    const pb = J(pa);
    pa.title = "A renamed"; await A.saveProject(pa);
    pb.chats = pb.chats.filter((c) => c.id !== "drop"); B.noteChatDeleted("p1", "drop");
    await B.saveProject(pb);
    eq(s.rows.get("p1").chats.map((c) => c.id), ["keep"]);
});
await check("renamed-here title survives a conflict merge", async () => {
    const s = makeServer();
    const A = Sync.createSaver({ rpc: (a) => s.rpc(a) }), B = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const pa = newProject(); await A.saveProject(pa);
    const pb = J(pa);
    pa.chats.push(chat("x")); await A.saveProject(pa);
    pb.title = "My new name"; B.noteRenamed("p1");
    await B.saveProject(pb);
    eq(s.rows.get("p1").title, "My new name");
});
await check("an endless conflict stops after MAX_ATTEMPTS, keeps the local data, and reports conflict_unresolved", async () => {
    const s = makeServer();
    let n = 0;
    const rpc = async (a) => {                           // a rival writes between every attempt
        const res = await s.rpc(a);
        if (res.data.status === "conflict") { const r = s.rows.get(a.p_id); r.version += 1; r.chats.push(chat("r" + (++n))); }
        return res;
    };
    const saver = Sync.createSaver({ rpc });
    const p = newProject("p1", [chat("mine")]);
    s.rows.set("p1", { owner: "u1", title: "t", chats: [], version: 2 }); p.version = 1;
    const r = await saver.saveProject(p);
    eq(r.status, "conflict_unresolved");
    assert(p.chats.some((c) => c.id === "mine"), "local chat lost");
    assert(s.log.length <= Sync.MAX_ATTEMPTS + 1, "too many calls: " + s.log.length);
});

console.log("\n== saver: failures ==");
await check("network failure -> offline; the SAME expected version is reused on retry; no new id", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject(); await saver.saveProject(p);
    p.chats.push(chat("c")); s.failNetwork(1);
    const r1 = await saver.saveProject(p);
    eq(r1.status, "offline"); eq(p.version, 1); eq(p.id, "p1");
    const r2 = await saver.saveProject(p);
    eq(r2.status, "saved"); eq(s.log[s.log.length - 1].p_expected_version, 1);
});
await check("committed save whose reply was lost: the retry is accepted as ok (replay), version +1 only", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject(); await saver.saveProject(p);
    p.chats.push(chat("c")); s.loseReply();
    const r1 = await saver.saveProject(p); eq(r1.status, "offline");
    const r2 = await saver.saveProject(p);
    eq(r2.status, "saved"); eq(s.rows.get("p1").version, 2); eq(p.version, 2);
});
await check("a creation whose reply was lost is replay-safe too (no duplicate row, no conflict)", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject("p9", [chat("c")]); s.loseReply();
    eq((await saver.saveProject(p)).status, "offline");
    const r = await saver.saveProject(p);
    eq(r.status, "saved"); eq(s.rows.size, 1);
});
await check("permanent database errors are 'rejected' with a reason and are not retried", async () => {
    for (const [code, reason] of [["28000", "not_signed_in"], ["22023", "invalid"], ["54000", "too_large"]]) {
        const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
        s.failWithCode(code);
        const r = await saver.saveProject(newProject());
        eq(r.status, "rejected"); eq(r.reason, reason); eq(s.log.length, 1, "retried a permanent error");
    }
});
await check("other database errors are treated as temporary (offline)", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    s.failWithCode("57014");
    eq((await saver.saveProject(newProject())).status, "offline");
});
await check("a malformed server answer never counts as saved", async () => {
    for (const data of [null, {}, { status: 5 }, { status: "weird" }, { status: "conflict" }]) {
        const saver = Sync.createSaver({ rpc: async () => ({ data, error: null }) });
        const p = newProject();
        const r = await saver.saveProject(p);
        assert(r.status === "offline", JSON.stringify(data) + " -> " + r.status);
        eq(p.version, null);
    }
});
await check("not_found (deleted on another device): the local copy is re-created, not dropped", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject("p1", [chat("c", [msg("user", "mine")])]); await saver.saveProject(p);
    s.rows.delete("p1");
    p.chats.push(chat("d"));
    const r = await saver.saveProject(p);
    eq(r.status, "recreated");
    eq(s.rows.get("p1").chats.length, 2); eq(p.version, 1);
});
await check("id_unavailable: a NEW uuid is minted once, reported, and the data is saved under it", async () => {
    const s = makeServer(); s.foreignRow("taken");
    let reported = null;
    const saver = Sync.createSaver({ rpc: (a) => s.rpc(a), onIdChanged: (f, t) => { reported = { f, t }; } });
    const p = newProject("taken", [chat("c")]);
    const r = await saver.saveProject(p);
    eq(r.status, "saved"); assert(r.idChanged && r.idChanged.from === "taken" && r.idChanged.to === p.id);
    assert(p.id !== "taken"); eq(reported.f, "taken"); assert(s.rows.get(p.id), "not saved under new id");
    eq(s.rows.get("taken").owner, "someone-else");
});

console.log("\n== saver: duplicate / rapid saves ==");
await check("20 rapid saves of one notebook collapse into at most 2 server calls and the LAST state wins", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject(); await saver.saveProject(p);
    const before = s.log.length;
    const ps = [];
    for (let i = 0; i < 20; i++) { p.title = "T" + i; ps.push(saver.saveProject(p)); }
    await Promise.all(ps);
    assert(s.log.length - before <= 2, "calls: " + (s.log.length - before));
    eq(s.rows.get("p1").title, "T19");
});
await check("saves of DIFFERENT notebooks run independently", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const a = newProject("a"), b = newProject("b");
    await Promise.all([saver.saveProject(a), saver.saveProject(b)]);
    eq([...s.rows.keys()].sort(), ["a", "b"]);
});
await check("a failed save does not wedge later saves of the same notebook", async () => {
    const s = makeServer(), saver = Sync.createSaver({ rpc: (a) => s.rpc(a) });
    const p = newProject(); s.failNetwork(1);
    eq((await saver.saveProject(p)).status, "offline");
    eq((await saver.saveProject(p)).status, "saved");
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);