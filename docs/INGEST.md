# Ingest mapping

Do not ingest until dumps are on disk. Slack zip is already at `~/Documents/Morpheus/slackdump_20260826_012610.zip` (extract `~/temp/slackdump-extract`). Discord JSON lands in `~/Documents/discord-export/morpheus/`. Skip `intro-gmor` (`1151741791226306593`) even if it is still on disk.

## Slack

| Dump | Table |
|---|---|
| team id in messages (`T08PC074FJQ`) | `workspaces` origin=slack |
| `users.json` | `users` |
| `channels.json` / `dms.json` / `mpims.json` | `channels` kind = public / private / im / mpim |
| `{channel}/*.json` day files | `messages` — reassemble threads by `thread_ts` (41% of replies are in another day file) |
| `files[]` + `__uploads/<id>/` | `files.local_path` |
| `reactions[]` | `reactions` |

Resolve `<@U…>` and `<url|label>` into `text_plain`. Parent when `ts == thread_ts`. `thread_docs` text is lines `@handle: …`.

## Discord

| Export | Table |
|---|---|
| `guild` | `workspaces` origin=discord |
| `channel` | `channels` (type → kind: GuildTextChat=public, GuildPublicThread=thread, …) |
| `channel.categoryId` | `channels.parent_id` for text; for thread files parent is the parent channel id |
| `messages[]` | `messages` native_id = snowflake |
| `author` / `mentions` / reaction `users` | upsert `users` |
| `type` | `subtype` (`GuildMemberJoin`, `Reply`, `Default`, …) |
| `reference.messageId` | `reply_to_id` |
| `attachments[]` | `files` (url + filename; bytes not saved) |
| `reactions[]` | `reactions` (use `emoji.code` or `emoji.name`) |
| `timestampEdited` | `edited_at` |

A Discord thread file: every message in that file gets `thread_id = channel.id`. Do not treat `Reply` as a thread.

Embeds stay in `raw` jsonb (link unfurls). Stickers too.

## Shared rules

- `messages.id` = `slack:{channel_id}:{ts}` or `discord:{message_id}`
- Upsert on `(origin, native_id)`
- Keep `raw jsonb`
- Do not embed join/system subtypes
- Slack files are durable; Discord CDN URLs will rot
