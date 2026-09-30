# Ingest mapping

Slack zip: `~/Downloads/slackdump_20260826_012610.zip` on this Mac (JSON-only extract at `~/temp/slackdump-extract`, no `__uploads/`). How to dump or unzip: `docs/SLACK_DUMP.md`. Discord JSON lands in `~/Documents/discord-export/morpheus/`. `ingest.discord` skips `intro-gmor` (`1151741791226306593`) even if it is still on disk.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r ingest/requirements.txt

.venv/bin/python -m ingest.slack   --source ~/temp/slackdump-extract   # or the .zip, read in place
.venv/bin/python -m ingest.discord --source ~/Documents/discord-export/morpheus
.venv/bin/python -m ingest.embed                                        # thread_docs.embedding where null

.venv/bin/python -m pytest ingest/tests -q                              # mapping tests, no DB
```

DB: `--database-url`, else `INGEST_DATABASE_URL`, else `POSTGRES_*` from env / repo `.env` on `127.0.0.1:5432` (the compose port). `--dry-run` maps and prints counts only.

Every run is idempotent: upserts on primary keys; reactions are replaced per message; `thread_docs` are rebuilt for the channels touched (embedding kept when the doc text is unchanged, stale docs deleted). Slack full run ≈ 1 min (mostly reading 1725 day files).

`ingest.embed`: `EMBED_BASE_URL` (default `https://api.mordiem.com/api/v1`), `EMBED_API_KEY`, `EMBED_MODEL` (default `text-embedding-bge-m3`, 1024-d), from env or `.env`. Batches of 32 (`--batch-size`), each doc cut to 6000 chars (`--max-chars`), newest first, commit per batch, retry with backoff on 5xx/429/timeouts. Exit 2 on no credit (402, or 429 mentioning credit/quota), 1 on other errors. Re-run to resume. `--limit N`, `--origin slack`.

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

As built (`ingest/slack.py`):

- Dir → channel: channel/MPIM dirs by name (`mpdm-…`), DM dirs are the `D…` id. Dirs with no match are reported and skipped.
- `users`: handle = `name`, display_name = `profile.display_name` or `real_name`, `is_bot`, `is_deleted` = `deleted`, `extra` = raw user. Authors not in `users.json` (bot ids, `USLACK`) get a row from `user_profile` / `bot_profile` / `username`.
- `channels`: `is_private` → private/public; DMs named `dm:{handle},{handle}`; MPIM name kept. `members` → `extra.member_ids`.
- `messages`: id `slack:{channel}:{ts}`, native_id `{channel}:{ts}`, user_id = `user` or `bot_id`, `thread_id` = `thread_ts`, `edited_at` = `edited.ts`, `is_pinned` = `pinned_to`. `text_plain`: `<@U>`→`@handle`, `<#C|name>`→`#name`, `<url|label>`→`label (url)`, `<!here>`→`@here`, entities unescaped.
- `files`: `url_private`; `local_path` = `__uploads/<id>/<name>` only if present in the source.
- `thread_docs`: one per thread (parent + replies, across day files) or standalone message; noise subtypes and `bot_message` excluded (`public.is_content`); `[file: name]` appended.

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

`ThreadStarterMessage` stubs in thread files are skipped; the real starter (parent channel, id = thread id) and the thread's messages form one `thread_docs` row `discord:{thread id}` on the parent channel. `reply_to_id` = `discord:{reference.messageId}`. Mentions / reaction users only fill gaps in `users`; authors overwrite.

Embeds stay in `raw` jsonb (link unfurls). Stickers too.

## Shared rules

- `messages.id` = `slack:{channel_id}:{ts}` or `discord:{message_id}`
- Upsert on `(origin, native_id)`
- Keep `raw jsonb`
- Do not embed join/system subtypes
- Slack files are durable; Discord CDN URLs will rot
