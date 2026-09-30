# Deploy MCP on an existing Hetzner box

The search API is a **Docker image** (`mcp`) in front of **Postgres 16 + pgvector** (`postgres`). Both are defined in `docker-compose.yml`. Do not run tinbase on the server.

Known SSH alias on this laptop (connectivity from the workstation was not verified when this doc was written):

```
Host hetzner-ai-gateway
    HostName 46.224.231.149
    User root
    IdentityFile ~/.ssh/hetzner-ai-gateway
```

If this is the wrong box, use whatever host already runs Docker for Morpheus. Do not install a second Postgres on a public interface.

## Shape

```
[agents / Cursor] --HTTPS--> [existing reverse proxy] --loopback--> mcp:8080
                                                      --loopback--> postgres:5432
```

- Postgres binds **127.0.0.1:5432** (or Docker-internal only). Never 0.0.0.0.
- MCP binds **127.0.0.1:8080**. Put the existing Caddy/nginx/Traefik in front with TLS and a bearer token.
- MCP transport: Streamable HTTP at `/mcp`. Stdio is for laptops, not this host.

Empty tables are fine for a smoke deploy. Ingest is a separate step.

## 1. One-time on the box

```bash
ssh hetzner-ai-gateway

# Docker Engine + Compose plugin if missing
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
docker compose version

mkdir -p /opt/morpheus-comms
```

Clone the **private** repo with a deploy key or `gh` as a machine user. Do not copy a personal PAT into world-readable files.

```bash
cd /opt/morpheus-comms
git clone git@github.com:MorpheusAIs/morpheus-comms.git .
```

## 2. Secrets

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Set at least:

| Var | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | strong random; Compose interpolates it into `DATABASE_URL` |
| `MCP_AUTH_TOKEN` | bearer token agents send as `Authorization: Bearer …` |

Do not put Discord/Slack tokens on this host unless you also ingest here.

## 3. Build and start

```bash
cd /opt/morpheus-comms
docker compose pull
docker compose build mcp
docker compose up -d
docker compose ps
docker compose logs -f postgres mcp
```

Health:

```bash
curl -sS http://127.0.0.1:8080/health
# {"ok": true}

curl -sS -D- -o /dev/null \
  -H "Authorization: Bearer $MCP_AUTH_TOKEN" \
  http://127.0.0.1:8080/mcp
```

## 4. Reverse proxy (existing instance)

Do not open 8080 or 5432 on the public interface. Add a location/host to whatever already terminates TLS.

Caddy example (hostname is yours):

```
comms.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

nginx (repeat for `/api/tools` if non-MCP callers use the REST shim):

```
location /mcp {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

Firewall: only 22/80/443 from the world.

```bash
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

## 5. Point an MCP client at it

```json
{
  "mcpServers": {
    "morpheus-comms": {
      "url": "https://comms.example.com/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_AUTH_TOKEN>"
      }
    }
  }
}
```

Tools: `docs/MCP.md` (`hybrid_search` first). All take optional `origin` = `slack` | `discord`; DMs are excluded unless `include_dms=true`. Non-MCP callers: `POST /api/tools/{name}` with the same bearer token.

Behind the proxy, optionally set `MCP_ALLOWED_HOSTS=comms.example.com` (Host allowlist) and `MCP_CORS_ORIGINS=https://frontend.example.com` (CORS is off by default). Set `EMBED_API_KEY` for semantic/hybrid search.

Data: run ingest on the Mac against a tunnel (`ssh -L 5433:127.0.0.1:5432 hetzner-ai-gateway`, then `--database-url postgresql://comms:…@127.0.0.1:5433/morpheus_comms`), or `pg_dump` local → `pg_restore` there.

## 6. Updates

```bash
cd /opt/morpheus-comms
git pull
docker compose build mcp
docker compose up -d
```

New SQL files are **not** auto-applied on an existing volume. Apply them:

```bash
docker compose exec -T postgres \
  psql -U comms -d morpheus_comms \
  < supabase/migrations/YYYYMMDDHHMMSS_name.sql
```

## 7. Backups

```bash
docker compose exec -T postgres \
  pg_dump -U comms -d morpheus_comms -Fc \
  > /var/backups/morpheus-comms-$(date -u +%Y%m%d).dump
```

Restore is `pg_restore` into a fresh `pgvector/pgvector:pg16` container.

## 8. What this box is not for

- Discord export (run that on the Mac; JSON is local).
- tinbase.
- Binding Postgres to `0.0.0.0`.
- Putting `DISCORD_TOKEN` in Compose.
