"""Slack export / slackdump zip -> Postgres (origin=slack). Mapping: docs/INGEST.md.

    python -m ingest.slack --source ~/temp/slackdump-extract
    python -m ingest.slack --source ~/Downloads/slackdump_20260826_012610.zip

Pure mapping functions (map_*, slack_to_plain) are DB-free; main() writes.
Re-runs are idempotent (upserts; reactions and thread_docs are rebuilt).
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import zipfile
from pathlib import Path

from ingest.common import (
    database_url, rebuild_thread_docs, replace_reactions, ts_to_dt, upsert_channels,
    upsert_files, upsert_messages, upsert_users, upsert_workspaces,
)

ORIGIN = "slack"
DEFAULT_TEAM = "T08PC074FJQ"
DEFAULT_DOMAIN = "morpheus-qyv9301"


# ---------------------------------------------------------------- source

class Source:
    """Extracted directory or the zip itself (read in place, no temp unzip)."""

    def __init__(self, path: str):
        self.path = Path(path).expanduser()
        self.zip = None
        if self.path.is_file() and zipfile.is_zipfile(self.path):
            self.zip = zipfile.ZipFile(self.path)
            self._names = set(self.zip.namelist())
        elif not self.path.is_dir():
            raise SystemExit(f"--source not found: {self.path}")

    def read_json(self, rel: str, default=None):
        if self.zip:
            if rel not in self._names:
                return default
            return json.loads(self.zip.read(rel))
        p = self.path / rel
        return json.loads(p.read_text()) if p.exists() else default

    def exists(self, rel: str) -> bool:
        if self.zip:
            return rel in self._names
        return (self.path / rel).exists()

    def day_files(self) -> dict[str, list[str]]:
        """dir name -> sorted day-file relpaths (`<dir>/YYYY-MM-DD.json`)."""
        out: dict[str, list[str]] = {}
        if self.zip:
            names = [n for n in self._names if n.count("/") == 1 and n.endswith(".json")]
        else:
            names = [f"{p.parent.name}/{p.name}" for p in self.path.glob("*/*.json")]
        for n in names:
            d, f = n.split("/")
            if d.startswith("__") or not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.json", f):
                continue
            out.setdefault(d, []).append(n)
        for v in out.values():
            v.sort()
        return out


# ---------------------------------------------------------------- text

_LINK = re.compile(r"<([^<>]+)>")


def slack_to_plain(text: str | None, handles: dict[str, str], channels: dict[str, str]) -> str:
    """<@U…> -> @handle, <#C…|name> -> #name, <url|label> -> label (url), entities."""
    if not text:
        return ""

    def sub(m: re.Match) -> str:
        body = m.group(1)
        target, _, label = body.partition("|")
        if target.startswith("@"):
            uid = target[1:]
            return "@" + (handles.get(uid) or label or uid)
        if target.startswith("#"):
            cid = target[1:]
            return "#" + (label or channels.get(cid) or cid)
        if target.startswith("!"):
            cmd = target[1:]
            if cmd in ("here", "channel", "everyone"):
                return "@" + cmd
            if cmd.startswith("subteam^"):
                return label or "@group"
            if cmd.startswith("date^"):
                return label or cmd
            return label or cmd
        if target.startswith("mailto:"):
            return label or target[7:]
        bare = target.split("://", 1)[-1]
        if label and label not in (target, bare, bare.rstrip("/")):
            return f"{label} ({target})"
        return target

    return html.unescape(_LINK.sub(sub, text))


# ---------------------------------------------------------------- mapping

def map_user(u: dict, team: str) -> dict:
    prof = u.get("profile") or {}
    return {
        "origin": ORIGIN,
        "native_id": u["id"],
        "workspace_id": u.get("team_id") or team,
        "handle": u.get("name"),
        "display_name": prof.get("display_name") or prof.get("real_name") or u.get("real_name"),
        "is_bot": bool(u.get("is_bot")),
        "is_deleted": bool(u.get("deleted")),
        "extra": u,
    }


def map_channel(c: dict, kind: str, team: str, name: str | None = None) -> dict:
    extra = {k: v for k, v in c.items() if k != "members"}
    extra["member_ids"] = c.get("members") or []
    return {
        "origin": ORIGIN,
        "native_id": c["id"],
        "workspace_id": c.get("context_team_id") or team,
        "parent_id": None,
        "name": name or c.get("name"),
        "topic": ((c.get("topic") or {}).get("value") or (c.get("purpose") or {}).get("value")
                  or None),
        "kind": kind,
        "is_archived": bool(c.get("is_archived")),
        "extra": extra,
    }


def channel_rows(channels: list, dms: list, mpims: list, handles: dict, team: str) -> list[dict]:
    rows = [map_channel(c, "private" if c.get("is_private") else "public", team) for c in channels]
    for d in dms:
        members = [handles.get(u, u) for u in d.get("members") or []]
        rows.append(map_channel(d, "im", team, name="dm:" + ",".join(sorted(members))))
    rows += [map_channel(c, "mpim", team) for c in mpims]
    return rows


def dir_to_channel(channels: list, dms: list, mpims: list) -> dict[str, str]:
    """Export dir name -> channel id. Channels/MPIMs by name, DMs by D… id."""
    out = {c["name"]: c["id"] for c in channels + mpims if c.get("name")}
    out.update({d["id"]: d["id"] for d in dms})
    return out


def map_message(m: dict, channel_id: str, handles: dict, channels: dict) -> dict:
    ts = m["ts"]
    edited = (m.get("edited") or {}).get("ts")
    return {
        "id": f"slack:{channel_id}:{ts}",
        "origin": ORIGIN,
        "native_id": f"{channel_id}:{ts}",
        "channel_id": channel_id,
        "user_id": m.get("user") or m.get("bot_id"),
        "thread_id": m.get("thread_ts"),
        "reply_to_id": None,
        "sent_at": ts_to_dt(ts),
        "edited_at": ts_to_dt(edited),
        "subtype": m.get("subtype"),
        "is_pinned": bool(m.get("pinned_to")),
        "text_raw": m.get("text"),
        "text_plain": slack_to_plain(m.get("text"), handles, channels),
        "raw": m,
    }


def map_files(m: dict, message_id: str, src: Source | None = None) -> list[dict]:
    rows = []
    for f in m.get("files") or []:
        if not f.get("id"):
            continue
        rel = f"__uploads/{f['id']}/{f['name']}" if f.get("name") else None
        rows.append({
            "origin": ORIGIN,
            "native_id": f["id"],
            "message_id": message_id,
            "filename": f.get("name"),
            "mimetype": f.get("mimetype") or None,
            "size_bytes": f.get("size"),
            "url": f.get("url_private") or f.get("permalink") or None,
            "local_path": rel if (rel and src is not None and src.exists(rel)) else None,
            "extra": {k: v for k, v in f.items() if not k.startswith("thumb_")},
        })
    return rows


def map_reactions(m: dict, message_id: str) -> list[dict]:
    return [
        {"message_id": message_id, "emoji": r["name"], "user_ids": r.get("users") or [],
         "count": r.get("count") or len(r.get("users") or [])}
        for r in m.get("reactions") or [] if r.get("name")
    ]


def extra_users(raw_messages: list[dict], known: set[str], team: str) -> list[dict]:
    """Authors missing from users.json: bots (bot_id) and stray ids (user_profile)."""
    out: dict[str, dict] = {}
    for m in raw_messages:
        uid = m.get("user") or m.get("bot_id")
        if not uid or uid in known or uid in out:
            continue
        prof = m.get("user_profile") or {}
        bot = m.get("bot_profile") or {}
        is_bot = not m.get("user") or bool(bot)
        handle = prof.get("name") or m.get("username") or bot.get("name") or uid
        out[uid] = {
            "origin": ORIGIN, "native_id": uid, "workspace_id": m.get("team") or team,
            "handle": handle,
            "display_name": prof.get("display_name") or prof.get("real_name")
                            or m.get("username") or bot.get("name"),
            "is_bot": is_bot, "is_deleted": False,
            "extra": {"source": "message", "user_profile": prof, "bot_profile": bot},
        }
    return list(out.values())


# ---------------------------------------------------------------- run

def load(src: Source, team_override: str | None = None) -> dict:
    users = src.read_json("users.json", [])
    channels = src.read_json("channels.json", [])
    dms = src.read_json("dms.json", [])
    mpims = src.read_json("mpims.json", [])
    groups = src.read_json("groups.json", [])  # private channels in some exports
    known_ids = {c["id"] for c in channels}
    channels += [g for g in groups if g["id"] not in known_ids]

    team = team_override or next((u["team_id"] for u in users if u.get("team_id")), DEFAULT_TEAM)
    handles = {u["id"]: u.get("name") or u["id"] for u in users}
    chan_names = {c["id"]: c.get("name") for c in channels + mpims}
    dmap = dir_to_channel(channels, dms, mpims)

    raw_by_channel: dict[str, list[dict]] = {}
    unknown_dirs = []
    for d, files in src.day_files().items():
        cid = dmap.get(d)
        if not cid:
            unknown_dirs.append(d)
            continue
        for rel in files:
            for m in src.read_json(rel, []):
                if m.get("type") == "message" and m.get("ts"):
                    raw_by_channel.setdefault(cid, []).append(m)

    all_raw = [m for ms in raw_by_channel.values() for m in ms]
    user_rows = [map_user(u, team) for u in users]
    user_rows += extra_users(all_raw, set(handles), team)
    for u in user_rows:
        handles.setdefault(u["native_id"], u["handle"] or u["native_id"])

    msgs, files, reacts = [], [], []
    seen = set()
    for cid, raws in raw_by_channel.items():
        for m in raws:
            row = map_message(m, cid, handles, chan_names)
            if row["id"] in seen:  # same ts in two day files: keep the first
                continue
            seen.add(row["id"])
            msgs.append(row)
            files += map_files(m, row["id"], src)
            reacts += map_reactions(m, row["id"])

    return {
        "team": team,
        "users": user_rows,
        "channels": channel_rows(channels, dms, mpims, handles, team),
        "messages": msgs,
        "files": files,
        "reactions": reacts,
        "unknown_dirs": unknown_dirs,
    }


def write(conn, data: dict, domain: str) -> None:
    team = data["team"]
    upsert_workspaces(conn, [{"origin": ORIGIN, "native_id": team, "name": domain,
                              "extra": {"domain": domain}}])
    upsert_users(conn, data["users"])
    upsert_channels(conn, data["channels"])
    B = 1000
    for i in range(0, len(data["messages"]), B):
        upsert_messages(conn, data["messages"][i:i + B])
    upsert_files(conn, data["files"])
    ids = [m["id"] for m in data["messages"]]
    replace_reactions(conn, ids, data["reactions"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="extracted dir or slackdump zip")
    ap.add_argument("--database-url")
    ap.add_argument("--team", help=f"override team id (default from users.json, {DEFAULT_TEAM})")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN, help="workspace subdomain for permalinks")
    ap.add_argument("--dry-run", action="store_true", help="map only, print counts")
    a = ap.parse_args(argv)

    t0 = time.time()
    data = load(Source(a.source), a.team)
    kinds: dict[str, int] = {}
    for c in data["channels"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    print(f"mapped: users={len(data['users'])} channels={kinds} messages={len(data['messages'])} "
          f"files={len(data['files'])} "
          f"local_files={sum(1 for f in data['files'] if f['local_path'])} "
          f"reactions={len(data['reactions'])}")
    if data["unknown_dirs"]:
        print(f"skipped dirs with no channel: {sorted(data['unknown_dirs'])}", file=sys.stderr)
    if a.dry_run:
        return 0

    import psycopg
    with psycopg.connect(database_url(a.database_url)) as conn:
        write(conn, data, a.domain)
        cids = sorted({m["channel_id"] for m in data["messages"]})
        n = rebuild_thread_docs(conn, ORIGIN, cids)
        conn.commit()
    print(f"written; thread_docs={n} in {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
