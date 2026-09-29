# morpheus-comms

Unified Slack + Discord archive for Morpheus: one Postgres store, one search surface, `origin` tagged as `slack` or `discord`.

Local backend is [tinbase](https://www.tinbase.dev/) (embedded Postgres 17, Supabase-compatible REST / Studio). Agents will talk to it through an MCP server; humans can use Studio or SQL.

This repo starts as schema + ingest design. Dumps stay on disk outside git (`~/Documents/Morpheus/slackdump_*.zip`, `~/Documents/discord-export/`).

## Status

- Schema designed for Slack dump + DiscordChatExporter JSON
- No identity linking (same display name on both sides is coincidence)
- Discord channel list is gated on a fresh user token (the August token is dead)
- Ingest, embeddings, and MCP are not built yet

## Layout

```
supabase/migrations/   tinbase / Supabase CLI migrations
docs/SCHEMA.md         tables, FTS, RAG unit, tinbase limits
docs/DISCORD_EXPORT.md how to list then export selected channels
docs/INGEST.md         how Slack + Discord map onto the tables
```

## Local Postgres (tinbase)

```bash
npx tinbase start
# API: http://127.0.0.1:54321
# Studio: http://127.0.0.1:54321/_/
```

Migrations in `supabase/migrations/` apply on boot. Default engine on macOS is native Postgres 17. Keyword search uses `tsvector` + `pg_trgm`. Semantic search waits on pgvector (tinbase does not ship the extension yet) or a later `--database-url` pointing at a Postgres that does.

## Next

1. Fresh Discord user token → list channels → you pick IDs
2. Export JSON, no media, skip `intro-gmor` and onboarding/voice
3. Slack ingest from `slackdump_20260826_012610.zip`
4. Discord ingest from the new JSON
5. MCP tools over the same tables
