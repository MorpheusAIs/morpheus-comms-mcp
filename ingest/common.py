"""Shared ingest pieces: DB url, upsert writers, thread_docs builder.

Mappers (slack.py / discord.py) produce plain dicts shaped like the tables.
Everything here that touches Postgres takes an open psycopg connection.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from psycopg.types.json import Jsonb

REPO = Path(__file__).resolve().parent.parent

# Mirrors public.is_content() (migration 20260929140000).
CONTENT_SUBTYPES = {None, "thread_broadcast", "file_share", "me_message", "Default", "Reply"}


def is_content(subtype: str | None) -> bool:
    return subtype in CONTENT_SUBTYPES


def _dotenv() -> dict:
    path = REPO / ".env"
    out: dict = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def database_url(cli: str | None = None) -> str:
    """--database-url > INGEST_DATABASE_URL > POSTGRES_* (env or repo .env) on 127.0.0.1."""
    if cli:
        return cli
    if os.environ.get("INGEST_DATABASE_URL"):
        return os.environ["INGEST_DATABASE_URL"]
    env = {**_dotenv(), **os.environ}
    pw = env.get("POSTGRES_PASSWORD")
    if not pw:
        raise SystemExit("no database url: pass --database-url or set POSTGRES_PASSWORD / .env")
    user = env.get("POSTGRES_USER", "comms")
    name = env.get("POSTGRES_DB", "morpheus_comms")
    host = env.get("POSTGRES_HOST", "127.0.0.1")
    port = env.get("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


def ts_to_dt(ts: str | float | None) -> datetime | None:
    if ts in (None, "", "0", 0):
        return None
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


# ---------------------------------------------------------------- writers

def _j(v):
    return Jsonb(v if v is not None else {})


def upsert_workspaces(conn, rows: list[dict]) -> None:
    conn.cursor().executemany(
        """
        insert into workspaces (origin, native_id, name, extra)
        values (%(origin)s, %(native_id)s, %(name)s, %(extra)s)
        on conflict (origin, native_id) do update
          set name = coalesce(excluded.name, workspaces.name),
              extra = workspaces.extra || excluded.extra
        """,
        [{**r, "extra": _j(r.get("extra"))} for r in rows],
    )


def upsert_users(conn, rows: list[dict], overwrite: bool = True) -> None:
    """overwrite=False only fills gaps (Discord mentions / reaction users are partial)."""
    if overwrite:
        conflict = """
          set workspace_id = excluded.workspace_id,
              handle = excluded.handle,
              display_name = excluded.display_name,
              is_bot = excluded.is_bot,
              is_deleted = excluded.is_deleted,
              extra = excluded.extra
        """
    else:
        conflict = """
          set handle = coalesce(users.handle, excluded.handle),
              display_name = coalesce(users.display_name, excluded.display_name)
        """
    conn.cursor().executemany(
        f"""
        insert into users (origin, native_id, workspace_id, handle, display_name,
                           is_bot, is_deleted, extra)
        values (%(origin)s, %(native_id)s, %(workspace_id)s, %(handle)s,
                %(display_name)s, %(is_bot)s, %(is_deleted)s, %(extra)s)
        on conflict (origin, native_id) do update {conflict}
        """,
        [
            {"is_bot": False, "is_deleted": False, **r, "extra": _j(r.get("extra"))}
            for r in rows
        ],
    )


def upsert_channels(conn, rows: list[dict]) -> None:
    conn.cursor().executemany(
        """
        insert into channels (origin, native_id, workspace_id, parent_id, name, topic,
                              kind, is_archived, extra)
        values (%(origin)s, %(native_id)s, %(workspace_id)s, %(parent_id)s, %(name)s,
                %(topic)s, %(kind)s, %(is_archived)s, %(extra)s)
        on conflict (origin, native_id) do update
          set workspace_id = excluded.workspace_id,
              parent_id = excluded.parent_id,
              name = excluded.name,
              topic = excluded.topic,
              kind = excluded.kind,
              is_archived = excluded.is_archived,
              extra = excluded.extra
        """,
        [
            {"parent_id": None, "topic": None, "is_archived": False, **r,
             "extra": _j(r.get("extra"))}
            for r in rows
        ],
    )


def upsert_messages(conn, rows: list[dict]) -> None:
    conn.cursor().executemany(
        """
        insert into messages (id, origin, native_id, channel_id, user_id, thread_id,
                              reply_to_id, sent_at, edited_at, subtype, is_pinned,
                              text_raw, text_plain, raw)
        values (%(id)s, %(origin)s, %(native_id)s, %(channel_id)s, %(user_id)s,
                %(thread_id)s, %(reply_to_id)s, %(sent_at)s, %(edited_at)s,
                %(subtype)s, %(is_pinned)s, %(text_raw)s, %(text_plain)s, %(raw)s)
        on conflict (id) do update
          set channel_id = excluded.channel_id,
              user_id = excluded.user_id,
              thread_id = excluded.thread_id,
              reply_to_id = excluded.reply_to_id,
              sent_at = excluded.sent_at,
              edited_at = excluded.edited_at,
              subtype = excluded.subtype,
              is_pinned = excluded.is_pinned,
              text_raw = excluded.text_raw,
              text_plain = excluded.text_plain,
              raw = excluded.raw
        """,
        [{**r, "raw": Jsonb(r["raw"])} for r in rows],
    )


def upsert_files(conn, rows: list[dict]) -> None:
    conn.cursor().executemany(
        """
        insert into files (origin, native_id, message_id, filename, mimetype,
                           size_bytes, url, local_path, extra)
        values (%(origin)s, %(native_id)s, %(message_id)s, %(filename)s, %(mimetype)s,
                %(size_bytes)s, %(url)s, %(local_path)s, %(extra)s)
        on conflict (origin, native_id) do update
          set message_id = excluded.message_id,
              filename = excluded.filename,
              mimetype = excluded.mimetype,
              size_bytes = excluded.size_bytes,
              url = excluded.url,
              local_path = coalesce(excluded.local_path, files.local_path),
              extra = excluded.extra
        """,
        [{**r, "extra": _j(r.get("extra"))} for r in rows],
    )


def replace_reactions(conn, message_ids: list[str], rows: list[dict]) -> None:
    """Reactions are a snapshot per message: delete then insert for these messages."""
    cur = conn.cursor()
    cur.execute("delete from reactions where message_id = any(%s)", (message_ids,))
    cur.executemany(
        """
        insert into reactions (message_id, emoji, user_ids, count)
        values (%(message_id)s, %(emoji)s, %(user_ids)s, %(count)s)
        on conflict (message_id, emoji) do update
          set user_ids = excluded.user_ids, count = excluded.count
        """,
        rows,
    )


# ---------------------------------------------------------------- thread_docs

def doc_key(msg: dict) -> str:
    """thread_docs.id for a message: parent message id for threads, own id otherwise.

    Slack thread parent id is slack:{channel}:{thread_ts} (even if the parent row is
    missing). Discord thread channels: discord:{thread channel id} — equal to the
    starter message id in the parent channel.
    """
    tid = msg.get("thread_id")
    if not tid:
        return msg["id"]
    if msg["origin"] == "slack":
        return f"slack:{msg['channel_id']}:{tid}"
    return f"discord:{tid}"


def build_thread_docs(messages: list[dict], handles: dict[str, str]) -> list[dict]:
    """Pure: message rows (id, origin, channel_id, user_id, thread_id, sent_at, subtype,
    text_plain, file_names?) -> thread_docs rows. Noise subtypes are dropped; docs with
    no text are skipped."""
    groups: dict[str, list[dict]] = {}
    for m in messages:
        if not is_content(m.get("subtype")):
            continue
        groups.setdefault(doc_key(m), []).append(m)

    docs = []
    for key, msgs in groups.items():
        msgs.sort(key=lambda m: (m["sent_at"], m["id"]))
        lines, participants = [], []
        for m in msgs:
            text = (m.get("text_plain") or "").strip()
            names = m.get("file_names") or []
            if names:
                text = (text + " " if text else "") + " ".join(f"[file: {n}]" for n in names)
            if not text:
                continue
            uid = m.get("user_id")
            who = handles.get(uid) or uid or "unknown"
            lines.append(f"@{who}: {text}")
            if uid and uid not in participants:
                participants.append(uid)
        if not lines:
            continue
        first = msgs[0]
        # Discord: the starter lives in the parent channel with id == thread id, so
        # it lands in the same group as the thread; the doc sits on the parent channel.
        parent = next((m for m in msgs if m["id"] == key), first)
        docs.append({
            "id": key,
            "origin": first["origin"],
            "channel_id": parent["channel_id"],
            "thread_id": next((m["thread_id"] for m in msgs if m.get("thread_id")), None),
            "sent_at": parent["sent_at"],
            "participant_ids": participants,
            "text_plain": "\n".join(lines),
        })
    return docs


def rebuild_thread_docs(conn, origin: str, channel_ids: list[str]) -> int:
    """Rebuild thread_docs for these channels from what is in `messages` now.

    Embeddings survive when a doc's text is unchanged; stale docs are deleted.
    """
    rows = conn.execute(
        """
        select m.id, m.origin::text as origin, m.channel_id, m.user_id, m.thread_id,
               m.sent_at, m.subtype, m.text_plain,
               (select array_agg(f.filename order by f.native_id)
                  from files f where f.message_id = m.id and f.filename is not null)
                 as file_names
        from messages m
        where m.origin = %s and m.channel_id = any(%s)
        """,
        (origin, channel_ids),
    ).fetchall()
    handles = dict(
        conn.execute(
            "select native_id, coalesce(handle, display_name) from users where origin = %s",
            (origin,),
        ).fetchall()
    )
    cols = ("id", "origin", "channel_id", "user_id", "thread_id", "sent_at", "subtype",
            "text_plain", "file_names")
    docs = build_thread_docs([dict(zip(cols, r)) for r in rows], handles)
    cur = conn.cursor()
    cur.executemany(
        """
        insert into thread_docs (id, origin, channel_id, thread_id, sent_at,
                                 participant_ids, text_plain)
        values (%(id)s, %(origin)s, %(channel_id)s, %(thread_id)s, %(sent_at)s,
                %(participant_ids)s, %(text_plain)s)
        on conflict (id) do update
          set origin = excluded.origin,
              channel_id = excluded.channel_id,
              thread_id = excluded.thread_id,
              sent_at = excluded.sent_at,
              participant_ids = excluded.participant_ids,
              embedding = case
                when thread_docs.text_plain is distinct from excluded.text_plain then null
                else thread_docs.embedding end,
              text_plain = excluded.text_plain
        """,
        docs,
    )
    cur.execute(
        "delete from thread_docs where origin = %s and channel_id = any(%s) "
        "and not (id = any(%s))",
        (origin, channel_ids, [d["id"] for d in docs]),
    )
    return len(docs)
