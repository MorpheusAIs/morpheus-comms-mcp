# Schema

One Postgres, two origins. Same tables for Slack dump and DiscordChatExporter JSON. Search is cross-source unless `origin` is passed.

Backend: [tinbase](https://www.tinbase.dev/) native engine (Postgres 17). Migrations follow Supabase CLI layout so they stay portable.

## Tinbase constraints that shape this

| Feature | Status in tinbase 0.18 |
|---|---|
| `tsvector` / GIN / `websearch_to_tsquery` | yes (real Postgres 17) |
| `pg_trgm`, `pgcrypto`, `citext` | yes (extensions schema) |
| PostgREST FTS (`fts`, `plfts`) | yes — MCP can use supabase-js |
| `pgvector` | **no** — needs an extension binary |
| ParadeDB `pg_search` | **no** |
| One writer at a time | yes — fine for ingest-then-query |

Keyword / `from:` / date / channel search goes live on tinbase. Semantic / RAG embeddings are stored as `float4[]` until we point tinbase at a Postgres with pgvector (`tinbase start --database-url postgres://…`) or tinbase ships the extension. Cosine can be computed in SQL or in the MCP process.

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

`messages.subtype` flags join/system noise (`channel_join`, `GuildMemberJoin`, `bot_message`, …). Keep the rows; exclude them from `thread_docs` and FTS queries by default.

Generated `tsv` columns + GIN indexes cover keyword search. `pg_trgm` GIN on `text_plain` covers fuzzy `grep`.

## Query shapes

```sql
-- keywords + person + date, either origin or both
select m.id, m.origin, m.sent_at, m.text_plain
from messages m
join users u on u.origin = m.origin and u.native_id = m.user_id
where m.tsv @@ websearch_to_tsquery('english', 'compute router')
  and m.subtype is null
  and ($origin::origin is null or m.origin = $origin)
  and ($from::text is null or u.handle ilike $from or u.display_name ilike $from)
  and m.sent_at >= $after and m.sent_at < $before
order by ts_rank_cd(m.tsv, websearch_to_tsquery('english', 'compute router')) desc;
```

MCP (later): `search_messages`, `get_thread`, `get_message`, `list_channels`, `grep` — all take optional `origin`. Direct Postgres / PostgREST is the same tables.

## Why this is compatible with the Slack design

The OpenCode Slack schema was `users` / `channels` / `messages` / `files` / `reactions`, FTS on plain text, thread-as-RAG-document. This is that schema plus `origin` and string native IDs. Discord `Reply` is `reply_to_id`, not a Slack thread. Discord threads are child channels, not `thread_ts`.
