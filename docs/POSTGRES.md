# Postgres (canonical)

The first schema sketch targeted [tinbase](https://www.tinbase.dev/) because you asked for it. **tinbase is not the production store.** It is alpha, one writer at a time, and **does not ship pgvector**. Keyword search would work; the original RAG design would not.

Canonical stack is the original OpenCode recommendation:

- **Postgres 16+**
- **`pg_trgm`** for fuzzy grep
- **`tsvector` / GIN** for keyword search
- **pgvector** (`vector(1024)` + HNSW cosine) on `thread_docs.embedding`

Local and Hetzner both run `pgvector/pgvector:pg16` via Compose so the migration is identical.

tinbase can still be used as a Studio against this database later **only if** it grows `--database-url` / pgvector. Do not run `npx tinbase start` against `supabase/migrations/` as they stand — `CREATE EXTENSION vector` and `vector(1024)` will fail or be skipped.

## Local

```bash
cp .env.example .env   # set POSTGRES_PASSWORD, MCP_AUTH_TOKEN
docker compose up -d postgres
docker compose logs -f postgres   # wait for "database system is ready"
```

Then:

```
host: 127.0.0.1
port: 5432
user: comms
password: from .env
database: morpheus_comms
```

Studio: any Postgres client, or `docker compose exec postgres psql -U comms -d morpheus_comms`.

Reset:

```bash
docker compose down -v
docker compose up -d postgres
```

Migrations in `supabase/migrations/` are applied on first boot (`/docker-entrypoint-initdb.d`). After that, apply new files by hand:

```bash
docker compose exec -T postgres \
  psql -U comms -d morpheus_comms \
  < supabase/migrations/YYYYMMDDHHMMSS_name.sql
```

## Why this matches the Slack design

Same tables (`workspaces`, `users`, `channels`, `messages`, `thread_docs`, `files`, `reactions`), plus `origin` (`slack` | `discord`). FTS on `text_plain`. RAG unit is a thread (or a standalone message). MCP talks to this database, not to tinbase.
