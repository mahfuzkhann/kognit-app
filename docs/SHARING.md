# Chat sharing (opt-in snapshot links)

**Status:** implemented, **not live until migration `0013` is applied** (see "Turning it on").
Until then every share endpoint answers `503 SHARING_UNAVAILABLE` and the dialog says sharing is
not available yet; nothing pretends to work.

## What a share is

A **frozen copy of one chat**, created only when the student presses **Create link** after seeing
a preview of exactly what will be shared. The link is `/s/<token>`. It never points at the
student's real chat or project: there is no code path from a token to `user_projects` or to any
other student data.

| Property | How it is enforced |
|---|---|
| Opt-in | Opening the dialog only *asks* whether the chat is already shared (`GET /api/share`). A row is created only by `POST /api/share`, sent from the "Create link" button. |
| Preview == what is stored | The messages previewed are captured in the browser and are the ones posted, even if the chat changes while the dialog is open. |
| Minimal content | Only each message's `role` and `text`. Images, research/source data, feedback state, ids and every other field are dropped by an allow-list (`backend/share_engine.py`). No name, email, user id, chat id or timestamps of messages. |
| Unguessable | 40 hex characters (160 bits) generated **by the database** in a `BEFORE INSERT` trigger. A client-supplied token is overwritten. |
| Owner-only management | Row-level security: a student can select / insert / delete only their own rows. There is **no UPDATE** privilege: an approved snapshot can not be edited. |
| Anonymous read | Only through `get_shared_chat(token)` (`SECURITY DEFINER`, pinned `search_path`). It returns `{title, snapshot, message_count, created_at}` or `null`. `anon` has no table privilege. |
| Revocation | **Stop sharing** *deletes* the row. The content is gone at once. Old links stay dead; creating a new link makes a new token. |
| No oracle | Malformed, unknown and revoked tokens all return the same `404 SHARE_NOT_FOUND` body. |
| Limits | 1 share per chat, 50 per student, 200 messages, 20,000 characters per message, ~300,000 in total. Enforced in `share_engine.py` **and** by SQL constraints/trigger. |
| Account deletion | `on delete cascade` removes every share. |

## Routes

| Route | Who | Purpose |
|---|---|---|
| `POST /api/share` | signed in | Create (idempotent per chat; never overwrites an existing snapshot) |
| `GET /api/share?chat_id=` | signed in | Is this chat shared? returns the owner's own link |
| `DELETE /api/share?chat_id=` | signed in | Stop sharing |
| `GET /api/shared/{token}` | anyone | Public JSON of one snapshot |
| `GET /s/{token}` | anyone | Public read-only page (`templates/shared.html`, `static/js/shared-view.js`) |

Owner routes use the student's **own** Supabase token (never a service-role key). The public
route uses only the public key and only calls `get_shared_chat`.

## The public page

* `noindex, nofollow, noarchive`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`,
  `frame-ancestors 'none'`.
* No Supabase client, no sign-in, no `app.js`. The only third-party request is MathJax at an
  exact pinned version; `marked` and DOMPurify are vendored.
* The text is treated as hostile: math is lifted out, Markdown is parsed, the HTML is sanitised
  with a strict allow-list (no `<img>`, forms, scripts, styles, `javascript:` links), links get
  `rel="noopener noreferrer nofollow" target="_blank"`, and only then is the math put back,
  HTML-escaped. If the sanitiser fails to load the page shows plain text.
* The page says it is a read-only copy that Kloriz has not verified.

## Turning it on

1. Review `supabase/migrations/0013_shared_chats.sql`.
2. Run it once in the Supabase SQL editor (it is safe to re-run).
3. Run `supabase/verify_schema.sql` and `supabase/verify_policies.sql` (both now include
   `shared_chats`) and confirm they report no problems.
4. Optional but recommended on a **throwaway local Postgres, never on the project**:
   `psql -v ON_ERROR_STOP=1 -d scratch -f supabase/tests/0013_shared_chats_check.sql`
   exercises RLS, the trigger, the constraints, the public function, revocation, the cap and the
   cascade as different users.
5. Rollback (deletes every share): the commented block at the end of the migration.

## Things to know

* **The token is a bearer credential** - anyone who has the link can read the copy (the dialog
  says so). The server's own logs never contain it, and the uvicorn access log has the token
  replaced by `<redacted>`. A reverse proxy or CDN in front of the app has its **own** access
  log: configure it to omit or mask `/s/` and `/api/shared/` paths.
* A snapshot is the student's own text plus the AI's reply **as the browser held it**. The
  server cannot prove the AI wrote what the copy attributes to it, which is why the page carries
  the "not verified by Kloriz" notice.
* There is no expiry yet (links live until revoked). If you want one, add an `expires_at` column
  and a check in `get_shared_chat`; the UI already has a single "not available" state for it.
* The public endpoints are not rate limited (a lookup is one indexed read, tokens are
  unguessable). If abuse appears, limit them at the proxy.
* Facebook / WhatsApp / Telegram / email buttons only **open** the destination's composer. The
  dialog never claims anything was sent.