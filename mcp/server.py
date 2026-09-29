"""Morpheus comms MCP: keyword/grep over Slack+Discord rows in Postgres.

Semantic search lands after ingest fills thread_docs.embedding.
Transport: Streamable HTTP at /mcp, plus GET /health.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
import uvicorn

DATABASE_URL = os.environ.get("DATABASE_URL", "")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8080"))

mcp = FastMCP("morpheus-comms")


@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def _origin_clause(origin: str | None, args: list) -> str:
    if origin in ("slack", "discord"):
        args.append(origin)
        return " AND origin = %s"
    return ""


@mcp.tool()
def search_messages(
    query: str,
    origin: str | None = None,
    from_user: str | None = None,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Keyword search (Postgres FTS). origin=slack|discord or omit for both."""
    args: list = [query]
    sql = """
      SELECT m.id, m.origin, m.channel_id, m.user_id, m.sent_at, m.thread_id,
             m.text_plain
      FROM messages m
      LEFT JOIN users u
        ON u.origin = m.origin AND u.native_id = m.user_id
      WHERE m.subtype IS NULL
        AND m.tsv @@ websearch_to_tsquery('english', %s)
    """
    sql += _origin_clause(origin, args)
    if from_user:
        args.append(from_user)
        args.append(from_user)
        sql += " AND (u.handle ILIKE %s OR u.display_name ILIKE %s)"
    if channel:
        args.append(channel)
        args.append(channel)
        sql += (
            " AND (m.channel_id = %s OR m.channel_id IN "
            "(SELECT native_id FROM channels c WHERE c.origin = m.origin "
            "AND c.name ILIKE '%%' || %s || '%%'))"
        )
    if after:
        args.append(after)
        sql += " AND m.sent_at >= %s::timestamptz"
    if before:
        args.append(before)
        sql += " AND m.sent_at < %s::timestamptz"
    args.append(query)
    args.append(max(1, min(limit, 100)))
    sql += (
        " ORDER BY ts_rank_cd(m.tsv, websearch_to_tsquery('english', %s)) DESC, "
        "m.sent_at DESC LIMIT %s"
    )
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_jsonable(r) for r in rows]


@mcp.tool()
def grep(pattern: str, origin: str | None = None, limit: int = 50) -> list[dict]:
    """Substring grep on message plain text."""
    args: list = [pattern]
    sql = """
      SELECT id, origin, channel_id, user_id, sent_at, text_plain
      FROM messages
      WHERE subtype IS NULL AND text_plain ILIKE '%%' || %s || '%%'
    """
    sql += _origin_clause(origin, args)
    args.append(max(1, min(limit, 100)))
    sql += " ORDER BY sent_at DESC LIMIT %s"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_jsonable(r) for r in rows]


@mcp.tool()
def list_channels(origin: str | None = None) -> list[dict]:
    args: list = []
    sql = (
        "SELECT origin, native_id, name, kind, parent_id, is_archived "
        "FROM channels WHERE true"
    )
    sql += _origin_clause(origin, args)
    sql += " ORDER BY origin, name"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_jsonable(r) for r in rows]


@mcp.tool()
def get_message(id: str) -> dict:
    """id is slack:{channel}:{ts} or discord:{snowflake}."""
    with db() as conn:
        row = conn.execute(
            "SELECT id, origin, native_id, channel_id, user_id, thread_id, "
            "reply_to_id, sent_at, subtype, text_plain FROM messages WHERE id = %s",
            (id,),
        ).fetchone()
    if not row:
        return {"error": "not found", "id": id}
    return _jsonable(row)


@mcp.tool()
def get_thread(id: str) -> list[dict]:
    """Slack thread_id (ts) or Discord thread channel id; also accepts messages.id."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, origin, channel_id, user_id, thread_id, sent_at, text_plain
            FROM messages
            WHERE thread_id = %s OR id = %s OR native_id = %s
            ORDER BY sent_at
            """,
            (id, id, id),
        ).fetchall()
    return [_jsonable(r) for r in rows]


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        if AUTH_TOKEN:
            got = request.headers.get("authorization", "")
            if got != f"Bearer {AUTH_TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_request: Request) -> Response:
    ok = True
    try:
        with db() as conn:
            conn.execute("SELECT 1")
    except Exception:
        ok = False
    return JSONResponse({"ok": ok}, status_code=200 if ok else 503)


def main() -> None:
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth)
    app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
