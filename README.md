# morpheus-comms

Unified Slack + Discord archive for Morpheus: one Postgres store, one search surface, `origin` tagged as `slack` or `discord`.

**Store:** Postgres 16 + `pg_trgm` + pgvector (the original design). Local and Hetzner run the same `docker-compose.yml`.

tinbase was considered for local Studio. It cannot install pgvector and is not used.

Dumps stay outside git (`~/Documents/Morpheus/slackdump_*.zip`, `~/Documents/discord-export/`). No identity linking.

## Status

- Schema + MCP skeleton exist; ingest is not built
- Discord channel list needs a **fresh** user token (August token is dead)
- Remote GitHub repo is private (`MorpheusAIs/morpheus-comms`); push when you say so

## Layout

```
docs/DISCORD_EXPORT.md     install DiscordChatExporter CLI + extract steps
docs/SLACK_DUMP.md         install slackdump, fresh export, import existing zip
docs/POSTGRES.md           local Postgres 16 + pgvector
docs/HETZNER.md            deploy MCP docker image on an existing Hetzner box
docs/SCHEMA.md             tables, FTS, RAG unit
docs/INGEST.md             Slack + Discord field mapping
supabase/migrations/       applied on first Postgres boot
mcp/                       Streamable HTTP MCP server (Docker image)
docker-compose.yml         postgres + mcp, loopback ports only
```

## Quick start (local)

```bash
cp .env.example .env          # set POSTGRES_PASSWORD and MCP_AUTH_TOKEN
docker compose up -d postgres # schema applies on empty volume
# optional: docker compose up -d mcp
```

## Next

1. Fresh Discord token → list channels (`docs/DISCORD_EXPORT.md`) → you pick IDs
2. Export JSON, no media, skip intro-gmor / onboarding / voice
3. Slack: use existing zip or dump again (`docs/SLACK_DUMP.md`), then ingest
4. Discord ingest from the new JSON
5. Point agents at the MCP (`docs/HETZNER.md` on the server)
