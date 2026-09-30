"""Morpheus comms MCP: search over Slack+Discord rows in Postgres.

Keyword (FTS) + trigram grep over messages; thread-level FTS, pgvector semantic and
hybrid (RRF) search over thread_docs; archive stats. Tools: docs/MCP.md.

Transport: Streamable HTTP at /mcp (stateless, JSON responses), GET /health,
REST shim POST /api/tools/{name}. Bearer auth on everything but /health.
DM / MPIM channels are excluded unless include_dms=True.
"""

from __future__ import annotations

import functools
import hmac
import inspect
import json
import os
from contextlib import contextmanager
from typing import Any

import anyio
import httpx
import psycopg
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from psycopg.rows import dict_row
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

DATABASE_URL = os.environ.get("DATABASE_URL", "")
AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
HOST = os.environ.get("MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("MCP_PORT", "8080"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("MCP_CORS_ORIGINS", "").split(",") if o.strip()]
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
SLACK_DOMAIN = os.environ.get("SLACK_DOMAIN", "morpheus-qyv9301")
EMBED_BASE_URL = os.environ.get("EMBED_BASE_URL", "https://api.mordiem.com/api/v1").rstrip("/")
EMBED_API_KEY = os.environ.get("EMBED_API_KEY", "")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "text-embedding-bge-m3")
EMBED_TIMEOUT = float(os.environ.get("EMBED_TIMEOUT", "15"))

DM_KINDS = ("im", "mpim")
RRF_K = 60
HEADLINE_OPTS = (
    "MaxFragments=2, MinWords=8, MaxWords=35, FragmentDelimiter=' … ', "
    "StartSel=**, StopSel=**"
)

mcp = FastMCP(
    "morpheus-comms",
    stateless_http=True,
    json_response=True,
    transport_security=(
        TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                  allowed_hosts=ALLOWED_HOSTS)
        if ALLOWED_HOSTS
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    ),
)
TOOLS: dict = {}


def tool(fn):
    """Register a sync tool: MCP gets an async wrapper (runs in a worker thread),
    the REST shim calls the plain function."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    mcp.tool()(wrapper)
    TOOLS[fn.__name__] = fn
    return fn


@contextmanager
def db():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- helpers

def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        out[k] = v.isoformat() if hasattr(v, "isoformat") else v
    return out


def _limit(n: int, hi: int = 100) -> int:
    return max(1, min(int(n), hi))


def _origin_clause(origin: str | None, args: list, col: str = "origin") -> str:
    if origin in ("slack", "discord"):
        args.append(origin)
        return f" AND {col} = %s"
    return ""


def _dm_clause(include_dms: bool, alias: str = "c") -> str:
    if include_dms:
        return ""
    return f" AND coalesce({alias}.kind, '') NOT IN ('im', 'mpim')"


def _time_clause(after: str | None, before: str | None, args: list, col: str) -> str:
    sql = ""
    if after:
        args.append(after)
        sql += f" AND {col} >= %s::timestamptz"
    if before:
        args.append(before)
        sql += f" AND {col} < %s::timestamptz"
    return sql


def _resolve_channels(conn, channel: str, origin: str | None) -> list[dict]:
    """Channel id or name (leading # ok). Exact id/name wins over substring.
    Discord thread children of a matched channel are included."""
    name = channel.lstrip("#")
    args: list = [channel, name]
    oc = _origin_clause(origin, args)
    rows = conn.execute(
        "SELECT origin::text AS origin, native_id, name, kind, topic, parent_id, is_archived "
        f"FROM channels WHERE (native_id = %s OR lower(name) = lower(%s)){oc} "
        "ORDER BY origin, name",
        args,
    ).fetchall()
    if not rows:
        args = [name]
        oc = _origin_clause(origin, args)
        rows = conn.execute(
            "SELECT origin::text AS origin, native_id, name, kind, topic, parent_id, is_archived "
            f"FROM channels WHERE name ILIKE '%%' || %s || '%%'{oc} ORDER BY origin, name",
            args,
        ).fetchall()
    if rows:
        kids = conn.execute(
            "SELECT origin::text AS origin, native_id, name, kind, topic, parent_id, is_archived "
            "FROM channels WHERE kind = 'thread' AND parent_id = ANY(%s)",
            ([r["native_id"] for r in rows],),
        ).fetchall()
        rows += kids
    return rows


def _channel_clause(conn, channel: str | None, origin: str | None, args: list,
                    col: str) -> str | None:
    """SQL filter for channel; None means the channel matched nothing."""
    if not channel:
        return ""
    ids = [r["native_id"] for r in _resolve_channels(conn, channel, origin)]
    if not ids:
        return None
    args.append(ids)
    return f" AND {col} = ANY(%s)"


def _slack_link(channel_id: str, ts: str, thread_ts: str | None = None) -> str:
    url = f"https://{SLACK_DOMAIN}.slack.com/archives/{channel_id}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        url += f"?thread_ts={thread_ts}&cid={channel_id}"
    return url


def _msg_permalink(r: dict) -> str | None:
    if r.get("origin") == "slack":
        ts = r["id"].rsplit(":", 1)[-1]
        return _slack_link(r["channel_id"], ts, r.get("thread_id"))
    if r.get("origin") == "discord" and r.get("workspace_id"):
        return f"https://discord.com/channels/{r['workspace_id']}/{r['channel_id']}/{r['id'].split(':', 1)[1]}"
    return None


def _doc_permalink(r: dict) -> str | None:
    if r.get("origin") == "slack":
        return _slack_link(r["channel_id"], r["id"].rsplit(":", 1)[-1])
    if r.get("origin") == "discord" and r.get("workspace_id"):
        if r.get("thread_id"):
            return f"https://discord.com/channels/{r['workspace_id']}/{r['thread_id']}"
        return f"https://discord.com/channels/{r['workspace_id']}/{r['channel_id']}/{r['id'].split(':', 1)[1]}"
    return None


MSG_SELECT = """
  SELECT m.id, m.origin::text AS origin, m.channel_id, c.name AS channel_name,
         c.kind AS channel_kind, c.workspace_id, m.user_id, u.handle AS user_handle,
         u.display_name AS user_display_name, m.thread_id, m.reply_to_id, m.sent_at,
         m.edited_at, m.subtype, m.is_pinned, m.text_plain
  FROM messages m
  LEFT JOIN channels c ON c.origin = m.origin AND c.native_id = m.channel_id
  LEFT JOIN users u ON u.origin = m.origin AND u.native_id = m.user_id
"""


def _msg_out(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        r = dict(r)
        r["permalink"] = _msg_permalink(r)
        r.pop("workspace_id", None)
        out.append(_jsonable(r))
    return out


def _attach_files_reactions(conn, msgs: list[dict]) -> None:
    ids = [m["id"] for m in msgs]
    if not ids:
        return
    files: dict = {}
    for f in conn.execute(
        "SELECT message_id, native_id AS id, filename, mimetype, size_bytes, url, local_path "
        "FROM files WHERE message_id = ANY(%s) ORDER BY native_id",
        (ids,),
    ).fetchall():
        files.setdefault(f.pop("message_id"), []).append(f)
    reacts: dict = {}
    for r in conn.execute(
        "SELECT message_id, emoji, count FROM reactions WHERE message_id = ANY(%s) "
        "ORDER BY count DESC, emoji",
        (ids,),
    ).fetchall():
        reacts.setdefault(r.pop("message_id"), []).append(r)
    for m in msgs:
        m["files"] = files.get(m["id"], [])
        m["reactions"] = reacts.get(m["id"], [])


# ---------------------------------------------------------------- message tools

@tool
def search_messages(
    query: str,
    origin: str | None = None,
    from_user: str | None = None,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 20,
    include_dms: bool = False,
) -> list[dict]:
    """Keyword search over single messages (Postgres FTS, websearch syntax).
    origin=slack|discord or omit for both. from_user matches handle or display name.
    channel is an id or name. after/before are ISO dates. System rows are excluded."""
    args: list = [query]
    sql = MSG_SELECT + """
      WHERE public.is_content(m.subtype)
        AND m.tsv @@ websearch_to_tsquery('english', %s)
    """
    sql += _origin_clause(origin, args, "m.origin")
    sql += _dm_clause(include_dms)
    if from_user:
        args += [from_user, from_user]
        sql += " AND (u.handle ILIKE %s OR u.display_name ILIKE %s)"
    with db() as conn:
        cc = _channel_clause(conn, channel, origin, args, "m.channel_id")
        if cc is None:
            return []
        sql += cc
        sql += _time_clause(after, before, args, "m.sent_at")
        args += [query, _limit(limit)]
        sql += (
            " ORDER BY ts_rank_cd(m.tsv, websearch_to_tsquery('english', %s)) DESC, "
            "m.sent_at DESC LIMIT %s"
        )
        rows = conn.execute(sql, args).fetchall()
    return _msg_out(rows)


@tool
def grep(
    pattern: str,
    origin: str | None = None,
    limit: int = 50,
    include_dms: bool = False,
) -> list[dict]:
    """Case-insensitive substring grep on message plain text (trigram index)."""
    args: list = [pattern]
    sql = MSG_SELECT + """
      WHERE public.is_content(m.subtype)
        AND m.text_plain ILIKE '%%' || %s || '%%'
    """
    sql += _origin_clause(origin, args, "m.origin")
    sql += _dm_clause(include_dms)
    args.append(_limit(limit))
    sql += " ORDER BY m.sent_at DESC LIMIT %s"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return _msg_out(rows)


@tool
def list_channels(origin: str | None = None, include_dms: bool = False) -> list[dict]:
    """Channels with message counts (content rows only)."""
    args: list = []
    sql = """
      SELECT c.origin::text AS origin, c.native_id, c.name, c.kind, c.parent_id,
             c.topic, c.is_archived, coalesce(s.messages, 0) AS messages, s.last_at
      FROM channels c
      LEFT JOIN (
        SELECT origin, channel_id, count(*) AS messages, max(sent_at) AS last_at
        FROM messages WHERE public.is_content(subtype)
        GROUP BY origin, channel_id
      ) s ON s.origin = c.origin AND s.channel_id = c.native_id
      WHERE true
    """
    sql += _origin_clause(origin, args, "c.origin")
    sql += _dm_clause(include_dms)
    sql += " ORDER BY c.origin, c.name"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_jsonable(r) for r in rows]


@tool
def get_message(id: str, include_dms: bool = False) -> dict[str, Any]:
    """id is slack:{channel}:{ts} or discord:{snowflake}. Includes files and reactions."""
    with db() as conn:
        row = conn.execute(MSG_SELECT + " WHERE m.id = %s", (id,)).fetchone()
        if not row:
            return {"error": "not found", "id": id}
        if not include_dms and row["channel_kind"] in DM_KINDS:
            return {"error": "message is in a DM; pass include_dms=true", "id": id}
        out = _msg_out([row])
        _attach_files_reactions(conn, out)
    return out[0]


@tool
def get_thread(id: str, origin: str | None = None, include_dms: bool = False) -> list[dict]:
    """Whole thread, oldest first. id: any message id in the thread, a thread doc id,
    a Slack thread_ts, or a Discord thread channel id. Includes files and reactions."""
    with db() as conn:
        where, args = None, []
        m = conn.execute(
            "SELECT origin::text AS origin, channel_id, thread_id FROM messages WHERE id = %s",
            (id,),
        ).fetchone()
        if m is None and id.startswith("slack:") and id.count(":") == 2:
            _, cid, ts = id.split(":")
            m = {"origin": "slack", "channel_id": cid, "thread_id": ts}
        if m is not None:
            if not m["thread_id"]:
                where, args = "m.id = %s", [id]
            elif m["origin"] == "slack":
                where = "m.origin = 'slack' AND m.channel_id = %s AND m.thread_id = %s"
                args = [m["channel_id"], m["thread_id"]]
            else:  # discord: starter (id == thread id) + thread channel
                where = "m.origin = 'discord' AND (m.thread_id = %s OR m.id = %s)"
                args = [m["thread_id"], f"discord:{m['thread_id']}"]
        else:
            where = "(m.thread_id = %s OR m.native_id = %s OR m.id = %s)"
            args = [id, id, f"discord:{id}"]
            where += _origin_clause(origin, args, "m.origin")
        rows = conn.execute(
            MSG_SELECT + f" WHERE {where}{_dm_clause(include_dms)} ORDER BY m.sent_at, m.id",
            args,
        ).fetchall()
        out = _msg_out(rows)
        _attach_files_reactions(conn, out)
    return out


# ---------------------------------------------------------------- thread-doc search

DOC_SELECT = """
  SELECT d.id, d.origin::text AS origin, d.channel_id, c.name AS channel_name,
         c.kind AS channel_kind, c.workspace_id, d.thread_id, d.sent_at,
         (SELECT coalesce(array_agg(coalesce(u.handle, t.p) ORDER BY t.o), '{}')
            FROM unnest(d.participant_ids) WITH ORDINALITY AS t(p, o)
            LEFT JOIN users u ON u.origin = d.origin AND u.native_id = t.p) AS participants,
         CASE WHEN d.thread_id IS NULL THEN 0 ELSE (
           SELECT count(*) FROM messages m
           WHERE m.origin = d.origin AND m.thread_id = d.thread_id
             AND (d.origin = 'discord' OR m.channel_id = d.channel_id)
             AND m.id <> d.id AND public.is_content(m.subtype)) END AS reply_count,
         ts_headline('english', d.text_plain, websearch_to_tsquery('english', %s), %s)
           AS snippet
  FROM thread_docs d
  LEFT JOIN channels c ON c.origin = d.origin AND c.native_id = d.channel_id
  WHERE d.id = ANY(%s)
"""


def _doc_filters(conn, origin, channel, after, before, include_dms, args) -> str | None:
    sql = _origin_clause(origin, args, "d.origin")
    sql += _dm_clause(include_dms)
    cc = _channel_clause(conn, channel, origin, args, "d.channel_id")
    if cc is None:
        return None
    sql += cc
    sql += _time_clause(after, before, args, "d.sent_at")
    return sql


def _fts_hits(conn, query, filters, fargs, n) -> list[tuple[str, float]]:
    rows = conn.execute(
        f"""
        SELECT d.id, ts_rank_cd(d.tsv, q) AS rank
        FROM thread_docs d
        CROSS JOIN websearch_to_tsquery('english', %s) AS q
        LEFT JOIN channels c ON c.origin = d.origin AND c.native_id = d.channel_id
        WHERE d.tsv @@ q {filters}
        ORDER BY rank DESC, d.sent_at DESC
        LIMIT %s
        """,
        [query, *fargs, n],
    ).fetchall()
    return [(r["id"], float(r["rank"])) for r in rows]


def _vec_hits(conn, vec: str, filters, fargs, n) -> list[tuple[str, float]]:
    with conn.transaction():
        conn.execute("SET LOCAL hnsw.ef_search = 200")
        conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        rows = conn.execute(
            f"""
            SELECT d.id, d.embedding <=> %s::vector AS dist
            FROM thread_docs d
            LEFT JOIN channels c ON c.origin = d.origin AND c.native_id = d.channel_id
            WHERE d.embedding IS NOT NULL {filters}
            ORDER BY d.embedding <=> %s::vector
            LIMIT %s
            """,
            [vec, *fargs, vec, n],
        ).fetchall()
    return [(r["id"], float(r["dist"])) for r in rows]


def _docs(conn, ids: list[str], query: str, scores: dict[str, dict]) -> list[dict]:
    if not ids:
        return []
    rows = conn.execute(DOC_SELECT, [query, HEADLINE_OPTS, ids]).fetchall()
    by_id = {r["id"]: r for r in rows}
    out = []
    for i in ids:
        r = by_id.get(i)
        if not r:
            continue
        r = dict(r)
        r["permalink"] = _doc_permalink(r)
        r.pop("workspace_id", None)
        r.update(scores.get(i, {}))
        out.append(_jsonable(r))
    return out


def _embed_query(text: str) -> str:
    if not EMBED_API_KEY:
        raise RuntimeError("EMBED_API_KEY is not set")
    r = httpx.post(
        f"{EMBED_BASE_URL}/embeddings",
        headers={"Authorization": f"Bearer {EMBED_API_KEY}"},
        json={"model": EMBED_MODEL, "input": [text[:6000]]},
        timeout=EMBED_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"embeddings HTTP {r.status_code}: {r.text[:200]}")
    v = r.json()["data"][0]["embedding"]
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def _has_embeddings(conn) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM thread_docs WHERE embedding IS NOT NULL) AS e"
    ).fetchone()["e"]


@tool
def search_threads(
    query: str,
    origin: str | None = None,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 10,
    include_dms: bool = False,
) -> list[dict]:
    """Keyword (FTS) search over whole threads / standalone messages (thread_docs).
    Returns id, origin, channel_id, channel_name, thread_id, sent_at, participants,
    snippet (**match** highlighted), reply_count, permalink, rank."""
    with db() as conn:
        fargs: list = []
        filters = _doc_filters(conn, origin, channel, after, before, include_dms, fargs)
        if filters is None:
            return []
        hits = _fts_hits(conn, query, filters, fargs, _limit(limit, 50))
        return _docs(conn, [i for i, _ in hits], query, {i: {"rank": s} for i, s in hits})


@tool
def semantic_search(
    query: str,
    origin: str | None = None,
    limit: int = 8,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    include_dms: bool = False,
) -> dict[str, Any]:
    """Vector (pgvector cosine) search over thread_docs using the EMBED_* model.
    Returns {mode, results[, note]}. mode=semantic, or fts_fallback when embeddings
    are missing or the embed call fails (results then come from search_threads)."""
    n = _limit(limit, 50)
    note = None
    with db() as conn:
        fargs: list = []
        filters = _doc_filters(conn, origin, channel, after, before, include_dms, fargs)
        if filters is None:
            return {"mode": "semantic", "results": [], "note": "channel not found"}
        if not _has_embeddings(conn):
            note = "no embeddings in thread_docs yet (run ingest.embed)"
        else:
            try:
                vec = _embed_query(query)
            except Exception as e:  # noqa: BLE001 - any embed failure falls back
                note = f"embedding query failed: {e}"
            else:
                hits = _vec_hits(conn, vec, filters, fargs, n)
                results = _docs(conn, [i for i, _ in hits], query,
                                {i: {"score": round(1 - d, 4)} for i, d in hits})
                return {"mode": "semantic", "results": results}
    return {"mode": "fts_fallback", "note": note,
            "results": search_threads(query, origin, channel, after, before, n, include_dms)}


@tool
def hybrid_search(
    query: str,
    origin: str | None = None,
    channel: str | None = None,
    after: str | None = None,
    before: str | None = None,
    limit: int = 10,
    include_dms: bool = False,
) -> dict[str, Any]:
    """Primary search: reciprocal-rank fusion of FTS + semantic over thread_docs.
    Falls back to FTS only when embeddings are missing or the embed call fails.
    Returns {mode: hybrid|fts, results[, note]}; each result has score, fts_rank,
    vec_rank (1-based, null when absent from that list)."""
    n = _limit(limit, 50)
    pool = max(50, n * 3)
    note = None
    with db() as conn:
        fargs: list = []
        filters = _doc_filters(conn, origin, channel, after, before, include_dms, fargs)
        if filters is None:
            return {"mode": "fts", "results": [], "note": "channel not found"}
        fts = _fts_hits(conn, query, filters, fargs, pool)
        vec: list = []
        if not _has_embeddings(conn):
            note = "no embeddings in thread_docs yet (run ingest.embed)"
        else:
            try:
                vec = _vec_hits(conn, _embed_query(query), filters, fargs, pool)
            except Exception as e:  # noqa: BLE001
                note = f"embedding query failed: {e}"
        scores: dict[str, dict] = {}
        for rank, (i, _) in enumerate(fts, 1):
            s = scores.setdefault(i, {"score": 0.0, "fts_rank": None, "vec_rank": None})
            s["score"] += 1 / (RRF_K + rank)
            s["fts_rank"] = rank
        for rank, (i, _) in enumerate(vec, 1):
            s = scores.setdefault(i, {"score": 0.0, "fts_rank": None, "vec_rank": None})
            s["score"] += 1 / (RRF_K + rank)
            s["vec_rank"] = rank
        for s in scores.values():
            s["score"] = round(s["score"], 6)
        ids = sorted(scores, key=lambda i: -scores[i]["score"])[:n]
        results = _docs(conn, ids, query, scores)
    out = {"mode": "hybrid" if vec else "fts", "results": results}
    if note:
        out["note"] = note
    return out


# ---------------------------------------------------------------- people / stats

@tool
def list_users(
    origin: str | None = None,
    query: str | None = None,
    limit: int = 100,
    include_dms: bool = False,
) -> list[dict]:
    """Users with message counts and first/last message time, most active first.
    query matches handle or display name (substring)."""
    args: list = []
    dm = _dm_clause(include_dms)
    sql = f"""
      SELECT u.origin::text AS origin, u.native_id, u.handle, u.display_name, u.is_bot,
             u.is_deleted, coalesce(s.messages, 0) AS messages, s.first_at, s.last_at
      FROM users u
      LEFT JOIN LATERAL (
        SELECT count(*) AS messages, min(m.sent_at) AS first_at, max(m.sent_at) AS last_at
        FROM messages m
        LEFT JOIN channels c ON c.origin = m.origin AND c.native_id = m.channel_id
        WHERE m.origin = u.origin AND m.user_id = u.native_id
          AND public.is_content(m.subtype) {dm}
      ) s ON true
      WHERE true
    """
    sql += _origin_clause(origin, args, "u.origin")
    if query:
        args += [query, query]
        sql += (" AND (u.handle ILIKE '%%' || %s || '%%' "
                "OR u.display_name ILIKE '%%' || %s || '%%')")
    args.append(_limit(limit, 500))
    sql += " ORDER BY messages DESC, u.handle LIMIT %s"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [_jsonable(r) for r in rows]


def _edge_message(conn, ids: list[str], order: str) -> dict | None:
    row = conn.execute(
        MSG_SELECT + " WHERE m.origin = %s AND m.channel_id = ANY(%s) "
        f"AND public.is_content(m.subtype) ORDER BY m.sent_at {order} LIMIT 1",
        ids,
    ).fetchone()
    if not row:
        return None
    r = _msg_out([row])[0]
    r["text_plain"] = (r["text_plain"] or "")[:280]
    return r


@tool
def channel_activity(
    channel: str,
    origin: str | None = None,
    include_dms: bool = False,
) -> dict[str, Any]:
    """Activity for one channel (id or name): message counts by month, top posters,
    first/last message. Discord thread children are counted with their parent."""
    with db() as conn:
        matches = _resolve_channels(conn, channel, origin)
        top = [r for r in matches if r["kind"] != "thread"] or matches
        if not top:
            return {"error": "channel not found", "channel": channel}
        ch = top[0]
        if not include_dms and ch["kind"] in DM_KINDS:
            return {"error": "channel is a DM; pass include_dms=true", "channel": channel}
        ids = [ch["native_id"]] + [
            r["native_id"] for r in matches
            if r["kind"] == "thread" and r["parent_id"] == ch["native_id"]
        ]
        base = ("FROM messages m WHERE m.origin = %s AND m.channel_id = ANY(%s) "
                "AND public.is_content(m.subtype)")
        a = [ch["origin"], ids]
        totals = conn.execute(
            f"SELECT count(*) AS messages, count(DISTINCT m.user_id) AS posters, "
            f"count(DISTINCT m.thread_id) AS threads {base}", a,
        ).fetchone()
        by_month = conn.execute(
            f"SELECT to_char(date_trunc('month', m.sent_at), 'YYYY-MM') AS month, "
            f"count(*) AS messages {base} GROUP BY 1 ORDER BY 1", a,
        ).fetchall()
        posters = conn.execute(
            f"""
            SELECT m.user_id, u.handle, u.display_name, count(*) AS messages
            FROM messages m
            LEFT JOIN users u ON u.origin = m.origin AND u.native_id = m.user_id
            WHERE m.origin = %s AND m.channel_id = ANY(%s) AND public.is_content(m.subtype)
            GROUP BY 1, 2, 3 ORDER BY messages DESC LIMIT 10
            """, a,
        ).fetchall()
        out = {
            "channel": _jsonable(ch),
            **totals,
            "first_message": _edge_message(conn, a, "ASC"),
            "last_message": _edge_message(conn, a, "DESC"),
            "by_month": [dict(r) for r in by_month],
            "top_posters": [dict(r) for r in posters],
        }
        others = [f"{r['origin']}:{r['name']}" for r in top[1:]]
        if others:
            out["other_matches"] = others
    return out


@tool
def archive_stats(include_dms: bool = False) -> dict[str, Any]:
    """Totals per origin/channel kind, users, thread_docs + embedding coverage,
    date range per origin, and top 10 channels by message count."""
    dm = _dm_clause(include_dms)
    with db() as conn:
        by_kind = conn.execute(
            f"""
            SELECT c.origin::text AS origin, c.kind, count(DISTINCT c.native_id) AS channels,
                   coalesce(sum(s.messages), 0)::bigint AS messages
            FROM channels c
            LEFT JOIN (SELECT origin, channel_id, count(*) AS messages FROM messages
                       WHERE public.is_content(subtype) GROUP BY 1, 2) s
              ON s.origin = c.origin AND s.channel_id = c.native_id
            WHERE true {dm}
            GROUP BY 1, 2 ORDER BY 1, 2
            """
        ).fetchall()
        origins = conn.execute(
            f"""
            SELECT m.origin::text AS origin, count(*) AS messages_all,
                   count(*) FILTER (WHERE public.is_content(m.subtype)) AS messages,
                   min(m.sent_at) AS first_at, max(m.sent_at) AS last_at
            FROM messages m
            LEFT JOIN channels c ON c.origin = m.origin AND c.native_id = m.channel_id
            WHERE true {dm}
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        users = conn.execute(
            "SELECT origin::text AS origin, count(*) AS users, "
            "count(*) FILTER (WHERE is_bot) AS bots FROM users GROUP BY 1 ORDER BY 1"
        ).fetchall()
        docs = conn.execute(
            f"""
            SELECT d.origin::text AS origin, count(*) AS thread_docs,
                   count(*) FILTER (WHERE d.thread_id IS NOT NULL) AS threads,
                   count(d.embedding) AS embedded
            FROM thread_docs d
            LEFT JOIN channels c ON c.origin = d.origin AND c.native_id = d.channel_id
            WHERE true {dm}
            GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        top = conn.execute(
            f"""
            SELECT m.origin::text AS origin, m.channel_id, c.name, c.kind,
                   count(*) AS messages, max(m.sent_at) AS last_at
            FROM messages m
            LEFT JOIN channels c ON c.origin = m.origin AND c.native_id = m.channel_id
            WHERE public.is_content(m.subtype) {dm}
            GROUP BY 1, 2, 3, 4 ORDER BY messages DESC LIMIT 10
            """
        ).fetchall()
    return {
        "origins": [_jsonable(r) for r in origins],
        "by_kind": [dict(r) for r in by_kind],
        "users": [dict(r) for r in users],
        "thread_docs": [dict(r) for r in docs],
        "top_channels": [_jsonable(r) for r in top],
        "include_dms": include_dms,
    }


# ---------------------------------------------------------------- HTTP

class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/"):
            return await call_next(request)
        if AUTH_TOKEN:
            got = request.headers.get("authorization", "")
            if not hmac.compare_digest(got.encode(), f"Bearer {AUTH_TOKEN}".encode()):
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


async def rest_list(_request: Request) -> Response:
    out = []
    for name, fn in TOOLS.items():
        params = {
            p.name: (None if p.default is inspect.Parameter.empty else p.default)
            for p in inspect.signature(fn).parameters.values()
        }
        required = [p.name for p in inspect.signature(fn).parameters.values()
                    if p.default is inspect.Parameter.empty]
        out.append({"name": name, "description": inspect.getdoc(fn),
                    "params": params, "required": required})
    return JSONResponse(out)


async def rest_call(request: Request) -> Response:
    """POST /api/tools/{name}, JSON body = tool args. Same bearer auth as /mcp."""
    name = request.path_params["name"]
    fn = TOOLS.get(name)
    if fn is None:
        return JSONResponse({"error": f"unknown tool {name}", "tools": sorted(TOOLS)},
                            status_code=404)
    raw = await request.body()
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        return JSONResponse({"error": f"bad json: {e}"}, status_code=400)
    if not isinstance(args, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    try:
        inspect.signature(fn).bind(**args)
    except TypeError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        result = await anyio.to_thread.run_sync(functools.partial(fn, **args))
    except psycopg.errors.DataError as e:
        return JSONResponse({"error": str(e).strip()}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse(result)


def main() -> None:
    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuth)
    if CORS_ORIGINS:  # off unless MCP_CORS_ORIGINS is set; outermost so preflight skips auth
        app.add_middleware(
            CORSMiddleware,
            allow_origins=CORS_ORIGINS,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["authorization", "content-type", "accept", "mcp-session-id",
                           "mcp-protocol-version", "last-event-id"],
            expose_headers=["mcp-session-id"],
        )
    app.router.routes.insert(0, Route("/health", health, methods=["GET"]))
    app.router.routes.insert(1, Route("/api/tools", rest_list, methods=["GET"]))
    app.router.routes.insert(2, Route("/api/tools/{name}", rest_call, methods=["POST"]))
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()
