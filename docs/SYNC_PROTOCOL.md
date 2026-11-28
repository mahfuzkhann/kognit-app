# Notebook / chat synchronization protocol (version-checked, compare-and-set)

Status: **database side implemented and tested** (migration `0019_project_versioning_cas.sql`).
**Browser side not yet implemented** - it arrives with the consolidated `app.js`. Until then the app still
saves with the old plain upsert; the new trigger already makes those old writes visible to version checks.

## The problem (verified in `static/js/app.js`, `syncProjectToDatabase`)

A notebook (`user_projects` row) holds EVERY chat as one JSON value. The app saves the whole row with
`upsert(..., {onConflict: "id"})` and a client-chosen `updated_at`. A phone and a laptop (or two tabs) that
both have the notebook open overwrite each other: the last save silently erases the other's chats.
Nothing detects it and nothing can recover it. Ids are `proj_<Date.now()>` / `chat_<Date.now()>`, which can
collide when two things are created in the same millisecond or on two devices.

## Data model

| Column | Meaning |
|---|---|
| `id` text | Notebook id. Legacy `proj_<ms>`, `proj_recent_default_<userId>`; **new ones are UUIDs**. Pattern `[A-Za-z0-9_-]{1,100}` |
| `user_id` uuid | Owner. Always `auth.uid()` - the client never sends it |
| `title` text | 1-200 chars (blank -> "Untitled notebook") |
| `chats` jsonb | JSON **array** of chats, <= 10 MB |
| `version` bigint | Starts at 1. +1 on every accepted change |
| `updated_at` | Set by the server. A client clock is never trusted |

Chats keep their own ids inside `chats`. New chats get UUIDs and an explicit `createdAt` (ms). Legacy
`chat_<ms>` ids keep working and their creation time is still derived from the id; **UUID ids carry no time, so
the app must store `createdAt` on every new chat/notebook** (the sidebar's "2h ago" depends on it).

## The save call

`supabase.rpc("save_user_project", {p_id, p_title, p_chats, p_expected_version})` - `p_expected_version` is the
version the client last saw, or `null` for a notebook it believes is new. The function is `SECURITY INVOKER`, so
Row Level Security applies: only the caller's own rows are visible or writable.

| `status` | Meaning | Client action |
|---|---|---|
| `ok` | Saved. `{version, updated_at}`, `created:true` for a new notebook, `replayed:true` if the identical content was already stored | Remember `version` |
| `conflict` | Someone saved in between. `{server:{id,title,chats,version,updated_at}}`. **Nothing was written** | Merge (below), retry with `server.version` |
| `not_found` | Expected an existing notebook; it is gone (deleted elsewhere). Nothing written | Keep the local copy and re-create it (`expected = null`); tell the student |
| `id_unavailable` | A NEW id is used by a notebook the caller does not own. Reveals nothing about it | Generate a new UUID and retry |
| (error 28000 / 22023 / 54000) | not signed in / invalid id or chats / over 10 MB | Not retried automatically |

Guarantees (proved on real PostgreSQL, `supabase/tests/project_cas_0019_test.sql` and
`concurrency_project_cas_test.sh`):
- 24 simultaneous saves holding the same version: **exactly 1 wins**, 23 get `conflict`; the stored title and
  chats are from the same writer; version is exactly +1.
- 24 simultaneous creates of one new id: exactly 1 row.
- A retry of a save that committed but lost its reply returns `ok` (no false conflict, no double bump).
- A stale writer never changes anything; a future version is never accepted.
- Another student can neither read, change, delete nor take over a notebook; id collisions leak nothing.
- Old cached app versions that still `upsert`/`update` directly cannot slip past: a trigger bumps `version`
  on every update that did not bump it itself.

## Client algorithm (to be implemented in `app.js`)

State per notebook: `version` (last confirmed) and the working copy.
1. **Save**: call the RPC with `expected = version` (`null` if never saved). On `ok` store the returned version.
2. **Conflict**: merge the working copy with `server` as below, save the merge with `expected = server.version`.
   At most 3 attempts; if still conflicting, keep everything locally, show "Couldn't sync - your chats are safe on
   this device" and retry on the next change or when the tab regains focus.
3. **Network failure / timeout**: keep the local cache, keep the SAME `expected` version, retry later (safe: the
   RPC is replay-safe). Never mint a new id for a retry.
4. **Load**: take `version` from the server row. Never invent one.
5. **Multi-tab**: tabs share `localStorage`; a `storage`/`BroadcastChannel` event triggers a reload of the
   notebook, and any save that raced is settled by the version check.

### Merge rule (server-authoritative, never drops a message)

Compare chats by `id`:
- only on the server -> keep; only local and not deleted locally -> keep (union);
- on both: if the server's messages are a prefix of the local ones, take local; if the local ones are a prefix of
  the server's, take the server's; if they **diverged**, keep the server's chat under its id and keep the local one
  as a NEW chat (new UUID, title + " (copy from this device)"). Nothing is discarded silently.
- title: the server's, unless the student renamed it locally in this session.

## Deleting

`delete` stays a direct, row-level delete (an explicit student action, protected by RLS). It is not version-checked:
deleting a notebook that has newer edits elsewhere deletes it. The app confirms before deleting.

## Not covered

- Real-time push between devices (changes appear on the next load / focus / conflict, not instantly).
- Message-level merging inside one diverged chat (the copy rule keeps both versions instead).
- Offline editing beyond what `localStorage` already caches.