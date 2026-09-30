# Schema

One Postgres, two origins. Same tables for Slack dump and DiscordChatExporter JSON. Search is cross-source unless `origin` is passed.

Backend: **Postgres 16+ with pg_trgm + pgvector**, same image locally and on Hetzner (`pgvector/pgvector:pg16`). See `docs/POSTGRES.md`.

tinbase was a local-only idea; it does not ship pgvector, so it is not part of this stack.

Keyword / `from:` / date / channel search uses `tsvector`. Semantic / RAG uses `thread_docs.embedding vector(1024)` with HNSW cosine.

## Tables

Discriminator is `origin` (`slack` | `discord`). Native IDs stay strings (Slack `C`/`U`/`ts`, Discord snowflakes).

No identity-linking table. A handful of display names will collide; searches stay per-origin user unless someone later adds a mapping.

- `workspaces` — Slack team `T08PC074FJQ` / Discord guild `1151741790408429580`
- `users` — Slack `users.json`; Discord upserted from message authors, mentions, reaction users
- `channels` — Slack channels/DMs/MPIMs; Discord guild channels + thread channels (`parent_id` = category or thread parent)
- `messages` — one row per message. PK is a prefixed id: `slack:{channel}:{ts}` or `discord:{snowflake}`
- `thread_docs` — RAG unit: Slack thread (`thread_ts`) or Discord thread-channel; standalones get their own row
- `files` — Slack `__uploads/` + Discord attachment URLs (CDN URLs expire)
- `reactions` — emoji + user id list

`messages.subtype` flags join/system noise (`channel_join`, `GuildMemberJoin`, `bot_message`, …). Keep the rows; exclude them from `thread_docs` and FTS queries by default with `public.is_content(subtype)` (true for null, `thread_broadcast`, `file_share`, `me_message`, Discord `Default` / `Reply`).

Slack `messages.native_id` is `{channel}:{ts}` (ts is only unique per channel). `thread_docs.id` is the parent message id (`slack:{channel}:{thread_ts}`, `discord:{thread id}` = starter message) or the standalone message id; `text_plain` is `@handle: text` lines plus `[file: name]`.

Generated `tsv` columns + GIN indexes cover keyword search. `pg_trgm` GIN on `text_plain` covers fuzzy `grep`.

## Query shapes

```sql
-- keywords + person + date, either origin or both
select m.id, m.origin, m.sent_at, m.text_plain
from messages m
join users u on u.origin = m.origin and u.native_id = m.user_id
where m.tsv @@ websearch_to_tsquery('english', 'compute router')
  and public.is_content(m.subtype)
  and ($origin::origin is null or m.origin = $origin)
  and ($from::text is null or u.handle ilike $from or u.display_name ilike $from)
  and m.sent_at >= $after and m.sent_at < $before
order by ts_rank_cd(m.tsv, websearch_to_tsquery('english', 'compute router')) desc;
```

MCP (`mcp/server.py`, `docs/MCP.md`): `hybrid_search`, `search_threads`, `semantic_search`, `search_messages`, `grep`, `get_thread`, `get_message`, `list_channels`, `list_users`, `channel_activity`, `archive_stats` — all take optional `origin`; DMs excluded unless `include_dms`. Semantic/hybrid fall back to FTS until `ingest.embed` fills embeddings.

## Why this is compatible with the Slack design

The OpenCode Slack schema was `users` / `channels` / `messages` / `files` / `reactions`, FTS on plain text, thread-as-RAG-document. This is that schema plus `origin` and string native IDs. Discord `Reply` is `reply_to_id`, not a Slack thread. Discord threads are child channels, not `thread_ts`.
