"""DiscordChatExporter JSON -> Postgres (origin=discord). Mapping: docs/INGEST.md.

    python -m ingest.discord --source ~/Documents/discord-export/morpheus

Reads every *.json under --source (recursive) that has guild/channel/messages.
One file per channel or thread. Pure mapping (map_export) is DB-free.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ingest.common import (
    database_url, rebuild_thread_docs, replace_reactions, upsert_channels, upsert_files,
    upsert_messages, upsert_users, upsert_workspaces,
)

ORIGIN = "discord"
SKIP_CHANNELS = {"1151741791226306593"}  # intro-gmor

KINDS = {
    "GuildTextChat": "public",
    "GuildNews": "public",
    "GuildAnnouncement": "public",
    "GuildPublicThread": "thread",
    "GuildPrivateThread": "thread",
    "GuildNewsThread": "thread",
    "GuildForum": "forum",
    "GuildMedia": "forum",
    "GuildVoiceChat": "voice",
    "GuildStageVoice": "voice",
    "GuildCategory": "category",
    "DirectTextChat": "im",
    "DirectGroupTextChat": "mpim",
}
THREAD_KINDS = {"thread"}


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def channel_kind(t: str | None) -> str:
    return KINDS.get(t or "", (t or "unknown").lower())


def map_user(u: dict, guild_id: str) -> dict:
    return {
        "origin": ORIGIN,
        "native_id": str(u["id"]),
        "workspace_id": guild_id,
        "handle": u.get("name"),
        "display_name": u.get("nickname") or u.get("globalName") or u.get("name"),
        "is_bot": bool(u.get("isBot")),
        "is_deleted": (u.get("name") or "").lower() == "deleted user",
        "extra": {k: u[k] for k in ("discriminator", "color", "avatarUrl", "roles") if k in u},
    }


_MENTION = re.compile(r"<@!?(\d+)>")
_CHAN = re.compile(r"<#(\d+)>")
_ROLE = re.compile(r"<@&(\d+)>")
_EMOJI = re.compile(r"<a?(:\w+:)\d+>")
_TS = re.compile(r"<t:(\d+)(?::\w)?>")


def discord_to_plain(text: str | None, handles: dict[str, str], channels: dict[str, str]) -> str:
    """DCE JSON content is mostly plain already; resolve any leftover raw markup."""
    if not text:
        return ""
    text = _MENTION.sub(lambda m: "@" + handles.get(m.group(1), m.group(1)), text)
    text = _CHAN.sub(lambda m: "#" + channels.get(m.group(1), m.group(1)), text)
    text = _ROLE.sub("@role", text)
    text = _EMOJI.sub(r"\1", text)
    text = _TS.sub(
        lambda m: datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc).isoformat(), text
    )
    return text


def map_export(doc: dict, channel_names: dict[str, str] | None = None) -> dict:
    """One DCE export file -> table rows. channel_names: id -> name for <#id> resolution."""
    guild = doc["guild"]
    ch = doc["channel"]
    gid = str(guild["id"])
    cid = str(ch["id"])
    kind = channel_kind(ch.get("type"))
    is_thread = kind in THREAD_KINDS

    users: dict[str, dict] = {}
    partial: dict[str, dict] = {}
    handles: dict[str, str] = {}

    def see(u: dict, full: bool) -> None:
        if not u or not u.get("id"):
            return
        row = map_user(u, gid)
        handles.setdefault(row["native_id"], row["handle"] or row["native_id"])
        if full:
            users[row["native_id"]] = row
        else:
            partial.setdefault(row["native_id"], row)

    for m in doc.get("messages") or []:
        see(m.get("author"), True)
        for u in m.get("mentions") or []:
            see(u, False)
        for r in m.get("reactions") or []:
            for u in r.get("users") or []:
                see(u, False)

    names = {cid: ch.get("name"), **(channel_names or {})}
    channel = {
        "origin": ORIGIN,
        "native_id": cid,
        "workspace_id": gid,
        "parent_id": str(ch["categoryId"]) if ch.get("categoryId") else None,
        "name": ch.get("name"),
        "topic": ch.get("topic") or None,
        "kind": kind,
        "is_archived": False,
        "extra": {k: ch.get(k) for k in ("type", "category", "categoryId", "iconUrl")
                  if ch.get(k) is not None},
    }

    msgs, files, reacts = [], [], []
    for m in doc.get("messages") or []:
        if m.get("type") == "ThreadStarterMessage":
            continue  # stub pointing at the real starter in the parent channel
        mid = str(m["id"])
        ref = (m.get("reference") or {}).get("messageId")
        row = {
            "id": f"discord:{mid}",
            "origin": ORIGIN,
            "native_id": mid,
            "channel_id": cid,
            "user_id": str(m["author"]["id"]) if m.get("author") else None,
            "thread_id": cid if is_thread else None,
            "reply_to_id": f"discord:{ref}" if ref and m.get("type") == "Reply" else None,
            "sent_at": parse_dt(m.get("timestamp")),
            "edited_at": parse_dt(m.get("timestampEdited")),
            "subtype": m.get("type") or "Default",
            "is_pinned": bool(m.get("isPinned")),
            "text_raw": m.get("content"),
            "text_plain": discord_to_plain(m.get("content"), handles, names),
            "raw": m,
        }
        msgs.append(row)
        for a in m.get("attachments") or []:
            fn = a.get("fileName")
            files.append({
                "origin": ORIGIN,
                "native_id": str(a["id"]),
                "message_id": row["id"],
                "filename": fn,
                "mimetype": mimetypes.guess_type(fn or "")[0],
                "size_bytes": a.get("fileSizeBytes"),
                "url": a.get("url"),
                "local_path": None,
                "extra": a,
            })
        for r in m.get("reactions") or []:
            e = r.get("emoji") or {}
            emoji = e.get("code") or e.get("name")
            if not emoji:
                continue
            reacts.append({
                "message_id": row["id"],
                "emoji": emoji,
                "user_ids": [str(u["id"]) for u in r.get("users") or [] if u.get("id")],
                "count": r.get("count") or len(r.get("users") or []),
            })

    return {
        "workspace": {"origin": ORIGIN, "native_id": gid, "name": guild.get("name"),
                      "extra": {"iconUrl": guild.get("iconUrl")}},
        "channel": channel,
        "users": list(users.values()),
        "partial_users": [u for k, u in partial.items() if k not in users],
        "messages": msgs,
        "files": files,
        "reactions": reacts,
    }


def load_exports(root: Path) -> list[dict]:
    out = []
    for p in sorted(root.rglob("*.json")):
        try:
            doc = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"skip {p}: {e}", file=sys.stderr)
            continue
        if not isinstance(doc, dict) or not {"guild", "channel", "messages"} <= doc.keys():
            continue
        if str(doc["channel"].get("id")) in SKIP_CHANNELS:
            print(f"skip {p.name}: excluded channel", file=sys.stderr)
            continue
        out.append(doc)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="DiscordChatExporter JSON dir")
    ap.add_argument("--database-url")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    t0 = time.time()
    docs = load_exports(Path(a.source).expanduser())
    names = {str(d["channel"]["id"]): d["channel"].get("name") for d in docs}
    mapped = [map_export(d, names) for d in docs]
    print(f"mapped: files={len(mapped)} messages={sum(len(m['messages']) for m in mapped)}")
    if a.dry_run or not mapped:
        return 0

    import psycopg
    with psycopg.connect(database_url(a.database_url)) as conn:
        upsert_workspaces(conn, list({m["workspace"]["native_id"]: m["workspace"]
                                      for m in mapped}.values()))
        users = {u["native_id"]: u for m in mapped for u in m["users"]}
        partial = {u["native_id"]: u for m in mapped for u in m["partial_users"]
                   if u["native_id"] not in users}
        upsert_users(conn, list(partial.values()), overwrite=False)
        upsert_users(conn, list(users.values()))
        # parents (categories / parent channels) we never exported still need no row;
        # parent_id is just a string.
        upsert_channels(conn, [m["channel"] for m in mapped])
        for m in mapped:
            upsert_messages(conn, m["messages"])
            upsert_files(conn, m["files"])
            replace_reactions(conn, [x["id"] for x in m["messages"]], m["reactions"])
        # thread docs merge the starter (parent channel) with the thread channel, so
        # rebuild parents and sibling threads of anything touched too
        touched = sorted({m["channel"]["native_id"] for m in mapped})
        related = conn.execute(
            """
            select native_id from channels where origin = 'discord' and (
              native_id = any(%(ids)s)
              or parent_id = any(%(ids)s)
              or native_id in (select parent_id from channels
                               where origin = 'discord' and kind = 'thread'
                                 and native_id = any(%(ids)s)))
            """,
            {"ids": touched},
        ).fetchall()
        n = rebuild_thread_docs(conn, ORIGIN, sorted({r[0] for r in related} | set(touched)))
        conn.commit()
    print(f"written; thread_docs={n} in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
