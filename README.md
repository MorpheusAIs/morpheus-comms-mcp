# morpheus-comms

Unified Slack + Discord archive for Morpheus: one Postgres store, one search surface, `origin` tagged as `slack` or `discord`.

**Store:** Postgres 16 + `pg_trgm` + pgvector (the original design). Local and Hetzner run the same `docker-compose.yml`.

tinbase was considered for local Studio. It cannot install pgvector and is not used.

Dumps stay outside git (`~/Downloads/slackdump_*.zip` on this Mac, extracted at `~/temp/slackdump-extract/`; `~/Documents/discord-export/`). No identity linking.

## Status

- Slack ingested locally (Aug 2026 dump: 63 users + 5 bot authors, 58 channels, 53 DMs, 5 MPIMs, 11,606 messages, 4,741 thread docs)
- MCP: FTS / grep / thread search / semantic + hybrid search / stats, plus a REST shim (`docs/MCP.md`)
- Embeddings not run yet (`python -m ingest.embed`; needs `EMBED_API_KEY` with credit). Until then semantic/hybrid fall back to FTS
- Discord ingest written + fixture-tested; no export on disk yet. Channel list needs a **fresh** user token (August token is dead)
- Remote GitHub repo is private (`MorpheusAIs/morpheus-comms`); push when you say so

## Layout

```
docs/DISCORD_EXPORT.md     install DiscordChatExporter CLI + extract steps
docs/SLACK_DUMP.md         install slackdump, fresh export, import existing zip
docs/POSTGRES.md           local Postgres 16 + pgvector
docs/HETZNER.md            deploy MCP docker image on an existing Hetzner box
docs/SCHEMA.md             tables, FTS, RAG unit
docs/INGEST.md             Slack + Discord field mapping, how to run ingest + embeddings
docs/MCP.md                MCP tools, REST shim, env
supabase/migrations/       applied on first Postgres boot (later files: apply by hand)
ingest/                    slack.py, discord.py, embed.py (+ tests/, requirements.txt)
mcp/                       Streamable HTTP MCP server (Docker image)
docker-compose.yml         postgres + mcp, loopback ports only
```

## Quick start (local)

```bash
cp .env.example .env && chmod 600 .env   # set POSTGRES_PASSWORD, MCP_AUTH_TOKEN (EMBED_API_KEY later)
docker compose up -d --build             # postgres (schema on empty volume) + mcp on 127.0.0.1:8080

python3 -m venv .venv && .venv/bin/pip install -r ingest/requirements.txt
.venv/bin/python -m ingest.slack --source ~/temp/slackdump-extract
.venv/bin/python -m ingest.embed          # when the embed key has credit
curl -s 127.0.0.1:8080/health
```

## Next

1. Run `ingest.embed` once the key has credit
2. Fresh Discord token → list channels (`docs/DISCORD_EXPORT.md`) → you pick IDs
3. Export JSON, no media, skip intro-gmor / onboarding / voice; `python -m ingest.discord --source ~/Documents/discord-export/morpheus`
4. Point agents / the frontend at the MCP (`docs/HETZNER.md` on the server, `docs/MCP.md` for tools)
