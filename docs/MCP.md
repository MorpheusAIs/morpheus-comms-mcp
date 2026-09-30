# MCP server

`mcp/server.py`: Python FastMCP (mcp 1.x) over the Postgres archive. One Docker image, `docker compose up -d mcp`, listens on `127.0.0.1:8080`.

| Path | What |
|---|---|
| `POST /mcp` | Streamable HTTP MCP. Stateless, JSON responses (no session id, no SSE needed). Works with `@ai-sdk/mcp` HTTP transport, Cursor, Claude. |
| `GET /api/tools` | REST shim: tool names, docstrings, params |
| `POST /api/tools/{name}` | REST shim: JSON body = tool args, response = tool result as JSON |
| `GET /health` | `{"ok": true}` when Postgres answers. No auth. |

Everything except `/health` needs `Authorization: Bearer $MCP_AUTH_TOKEN`.

## Env

| Var | Default | |
|---|---|---|
| `DATABASE_URL` | composed in `docker-compose.yml` | |
| `MCP_AUTH_TOKEN` | required | bearer token |
| `MCP_CORS_ORIGINS` | empty = CORS off | comma list of browser origins |
| `MCP_ALLOWED_HOSTS` | empty = no Host check | e.g. `comms.example.com`; turns on DNS-rebinding protection |
| `SLACK_DOMAIN` | `morpheus-qyv9301` | Slack permalinks |
| `EMBED_BASE_URL` | `https://api.mordiem.com/api/v1` | OpenAI-compatible `/embeddings` |
| `EMBED_API_KEY` | empty | without it semantic/hybrid fall back to FTS |
| `EMBED_MODEL` | `text-embedding-bge-m3` | must be 1024-d (same as `ingest.embed`) |

## Common rules

- `origin`: `slack` | `discord` | omitted (both).
- DM / MPIM channels (`kind` `im` / `mpim`) are excluded unless `include_dms=true`. `get_message` / `get_thread` / `channel_activity` return an error for a DM without it.
- `channel`: channel id or name (`#` optional). Exact id/name wins, else substring. Discord thread children of a matched channel are included.
- `after` / `before`: ISO date or timestamp (`2026-01-01`). `after` inclusive, `before` exclusive.
- System rows (joins, bots, huddles, …) are excluded from search: `public.is_content(subtype)`.
- `permalink`: Slack `https://morpheus-qyv9301.slack.com/archives/{channel}/p{ts}` (+ `?thread_ts=…&cid=…` for replies); Discord `https://discord.com/channels/{guild}/{channel}/{message}`.

Message row (`search_messages`, `grep`, `get_message`, `get_thread`):
`id, origin, channel_id, channel_name, channel_kind, user_id, user_handle, user_display_name, thread_id, reply_to_id, sent_at, edited_at, subtype, is_pinned, text_plain, permalink` (+ `files[]`, `reactions[]` on `get_message` / `get_thread`).

Thread row (`search_threads`, `semantic_search`, `hybrid_search`):
`id, origin, channel_id, channel_name, channel_kind, thread_id, sent_at, participants (handles), reply_count, snippet, permalink` + a score field. `id` is the parent message id; pass it to `get_thread`. `snippet` marks matches as `**word**`.

## Tools

### Thread search (use these first)

`hybrid_search(query, origin?, channel?, after?, before?, limit=10, include_dms=false)` → `{mode, results, note?}`
Reciprocal-rank fusion (k=60) of FTS and pgvector over `thread_docs`. `mode` = `hybrid`, or `fts` when there are no embeddings or the embed call failed (`note` says why). Each result: `score`, `fts_rank`, `vec_rank` (1-based or null). This is what the frontend calls.

`search_threads(query, origin?, channel?, after?, before?, limit=10, include_dms=false)` → `[thread row + rank]`
Postgres FTS (`websearch_to_tsquery`: `"exact phrase"`, `-exclude`, `or`) over whole threads / standalone messages.

`semantic_search(query, origin?, limit=8, channel?, after?, before?, include_dms=false)` → `{mode, results, note?}`
Embeds `query` with `EMBED_*`, cosine over `thread_docs.embedding` (HNSW). `mode` = `semantic`, or `fts_fallback` (results from `search_threads`). Each result: `score` = cosine similarity.

### Messages

`search_messages(query, origin?, from_user?, channel?, after?, before?, limit=20, include_dms=false)` → `[message row]`
FTS over single messages. `from_user` matches handle or display name (`ILIKE`, `%` allowed).

`grep(pattern, origin?, limit=50, include_dms=false)` → `[message row]`
Case-insensitive substring, newest first.

`get_message(id, include_dms=false)` → message row + `files`, `reactions`
`id` = `slack:{channel}:{ts}` or `discord:{snowflake}`.

`get_thread(id, origin?, include_dms=false)` → `[message row + files, reactions]`, oldest first
`id` = any message id in the thread, a thread doc id, a Slack `thread_ts`, or a Discord thread channel id. Standalone message → just that message.

### Channels, people, stats

`list_channels(origin?, include_dms=false)` → `[{origin, native_id, name, kind, parent_id, topic, is_archived, messages, last_at}]`

`list_users(origin?, query?, limit=100, include_dms=false)` → `[{origin, native_id, handle, display_name, is_bot, is_deleted, messages, first_at, last_at}]`, most active first. `query` = substring of handle / display name.

`channel_activity(channel, origin?, include_dms=false)` → `{channel, messages, posters, threads, first_message, last_message, by_month: [{month: "YYYY-MM", messages}], top_posters: [{user_id, handle, display_name, messages}], other_matches?}`

`archive_stats(include_dms=false)` → `{origins: [{origin, messages_all, messages, first_at, last_at}], by_kind: [{origin, kind, channels, messages}], users: [{origin, users, bots}], thread_docs: [{origin, thread_docs, threads, embedded}], top_channels: [...10]}`

## Calling it

MCP (Streamable HTTP, plain JSON):

```bash
T=…  # MCP_AUTH_TOKEN
curl -s http://127.0.0.1:8080/mcp \
  -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"hybrid_search","arguments":{"query":"compute router","limit":5}}}'
```

`initialize` and `tools/list` work the same way; stateless mode needs no `mcp-session-id`.

AI SDK:

```ts
import { createMCPClient } from '@ai-sdk/mcp';
const mcp = await createMCPClient({
  transport: { type: 'http', url: 'https://comms.example.com/mcp',
               headers: { Authorization: `Bearer ${process.env.COMMS_MCP_TOKEN}` } },
});
const tools = await mcp.tools();
```

REST shim (same tools, same auth, no JSON-RPC):

```bash
curl -s http://127.0.0.1:8080/api/tools/hybrid_search \
  -H "Authorization: Bearer $T" -H 'content-type: application/json' \
  -d '{"query":"compute router","limit":5}'
```

Errors: `401` bad token, `404` unknown tool, `400` bad JSON / unknown or missing arg / bad date, `500` anything else. Body is `{"error": "…"}`.
