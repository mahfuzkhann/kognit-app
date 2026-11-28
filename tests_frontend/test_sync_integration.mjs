/* ============================================================
   KOGNIT - NOTEBOOK SYNC INTEGRATION (real app.js + real project-sync.js in jsdom)
   Only the network and Supabase are faked. Proves app.js really uses the
   version-checked save path, UUID ids and explicit createdAt; it does NOT
   prove the SQL (supabase/tests/project_cas_0019_test.sql does).
   Run: node tests_frontend/test_sync_integration.mjs
   ============================================================ */
import { boot, assert, eq, sleep } from "./lib/boot.mjs";

let passed = 0, failed = 0;
async function check(name, fn) {
    try { await fn(); passed++; console.log(`  PASS  ${name}`); }
    catch (e) { failed++; console.log(`  FAIL  ${name}\n        ${e.message}`); }
}
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const okRpc = (calls, version = 1) => async (name, args) => {
    calls.push({ name, args: JSON.parse(JSON.stringify(args)) });
    return { data: { status: "ok", version: (args.p_expected_version || 0) + 1, updated_at: "t" }, error: null };
};

console.log("\n== signed-in student ==");
await check("new notebook: UUID ids, explicit createdAt, saved with expected=null through save_user_project", async () => {
    const h = await boot(); const calls = []; h.env.rpc = okRpc(calls);
    await h.w.createNewProject("Chemistry");
    await sleep(40);
    const p = h.w.getProjectsSnapshot().find((x) => x.title === "Chemistry");
    assert(p, "project missing");
    assert(UUID.test(p.id), "project id not a UUID: " + p.id);
    assert(UUID.test(p.chats[0].id), "chat id not a UUID");
    assert(typeof p.createdAt === "number" && typeof p.chats[0].createdAt === "number", "createdAt missing");
    const call = calls.find((c) => c.args.p_id === p.id);
    assert(call && call.name === "save_user_project", "RPC not used");
    eq(call.args.p_expected_version, null);
    eq(p.version, 1);
});
await check("the next save of the same notebook sends the stored version", async () => {
    const h = await boot(); const calls = []; h.env.rpc = okRpc(calls);
    await h.w.createNewProject("Physics"); await sleep(40);
    const p = h.w.getProjectsSnapshot().find((x) => x.title === "Physics");
    h.w.createNewChat(p.id); await sleep(40);
    const mine = calls.filter((c) => c.args.p_id === p.id);
    assert(mine.length >= 2, "expected 2 saves, got " + mine.length);
    eq(mine[mine.length - 1].args.p_expected_version, 1);
    eq(p.version, 2);
});
await check("a new chat shows its time in the sidebar from createdAt (a UUID id carries no time)", async () => {
    const h = await boot(); h.env.rpc = okRpc([]);
    h.w.createStandaloneChat(); await sleep(40);
    assert(h.doc.querySelector(".chat-item-time"), "no time shown for a createdAt-only chat");
});
await check("conflict from another device: merged in place, nothing lost, the merge is saved with the server version", async () => {
    const h = await boot(); const calls = [];
    h.env.rpc = async (name, args) => {
        calls.push(JSON.parse(JSON.stringify(args)));
        if (calls.length === 1) {
            return { data: { status: "conflict", server: { id: args.p_id, title: args.p_title, version: 7, updated_at: "t",
                chats: [{ id: "from-phone", title: "Phone chat", messages: [{ role: "user", text: "hello from phone" }] }] } }, error: null };
        }
        return { data: { status: "ok", version: args.p_expected_version + 1, updated_at: "t" }, error: null };
    };
    await h.w.createNewProject("Biology"); await sleep(60);
    const p = h.w.getProjectsSnapshot().find((x) => x.title === "Biology");
    const ids = p.chats.map((c) => c.id);
    assert(ids.includes("from-phone"), "other device's chat lost");
    assert(p.chats.length === 2, "local chat lost");
    eq(calls[1].p_expected_version, 7);
    eq(calls[1].p_chats.length, 2);
    eq(p.version, 8);
});
await check("network failure keeps the notebook locally, keeps the same version, and the next save succeeds", async () => {
    const h = await boot(); const calls = []; let fail = false;
    h.env.rpc = async (name, args) => { if (fail) throw new Error("offline"); return okRpc(calls)(name, args); };
    await h.w.createNewProject("Maths"); await sleep(40);
    const p = h.w.getProjectsSnapshot().find((x) => x.title === "Maths");
    fail = true; h.w.createNewChat(p.id); await sleep(40);
    eq(p.version, 1, "version must not change on failure");
    assert(p.chats.length === 2, "chat lost");
    fail = false; h.w.createNewChat(p.id); await sleep(40);
    eq(p.version, 2);
});
await check("a deleted chat is not brought back by a later merge", async () => {
    const h = await boot(); let n = 0; let conflictServed = false; const calls = [];
    h.env.rpc = async (name, args) => {
        calls.push(JSON.parse(JSON.stringify(args)));
        n++;
        if (n === 3) { conflictServed = true; return { data: { status: "conflict", server: { id: args.p_id, title: args.p_title, version: 9, updated_at: "t", chats: args.p_chats.concat([{ id: "ghost", title: "Ghost", messages: [] }]) } }, error: null }; }
        return { data: { status: "ok", version: (args.p_expected_version || 0) + 1, updated_at: "t" }, error: null };
    };
    await h.w.createNewProject("History"); await sleep(40);
    const p = h.w.getProjectsSnapshot().find((x) => x.title === "History");
    h.w.createNewChat(p.id); await sleep(40);
    // simulate the server already holding a chat we delete here
    p.chats.push({ id: "ghost", title: "Ghost", messages: [] });
    h.w.eval("deleteChat")(p.id, "ghost"); await sleep(60);
    assert(conflictServed, "test did not reach the conflict");
    assert(!p.chats.some((c) => c.id === "ghost"), "deleted chat came back");
});

console.log("\n== guest ==");
await check("guests never call the sync RPC, but still get UUID ids and createdAt", async () => {
    const h = await boot({ guest: true }); let called = 0; h.env.rpc = async () => { called++; return { data: null, error: null }; };
    await h.w.createNewProject("Local"); await sleep(40);
    eq(called, 0);
    const p = h.w.getProjectsSnapshot().find((x) => x.title === "Local");
    assert(UUID.test(p.id) && typeof p.createdAt === "number");
});

console.log("\n== page wiring ==");
await check("index.html loads project-sync.js before app.js; sw.js precaches it", async () => {
    const fs = await import("node:fs"); const { ROOT } = await import("./lib/boot.mjs");
    const html = fs.readFileSync(ROOT + "/templates/index.html", "utf8");
    assert(html.indexOf("/static/js/project-sync.js") > 0 && html.indexOf("/static/js/project-sync.js") < html.indexOf("/static/js/app.js"));
    assert(fs.readFileSync(ROOT + "/static/sw.js", "utf8").includes("/static/js/project-sync.js"));
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);