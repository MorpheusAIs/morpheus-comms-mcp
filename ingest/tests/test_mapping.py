"""DB-free tests for the Slack / Discord mappers and the thread_docs builder.

    .venv/bin/python -m pytest ingest/tests -q
"""

from __future__ import annotations

import json

from ingest import discord, slack
from ingest.common import build_thread_docs

TEAM = "T1"
USERS = [
    {"id": "U1", "team_id": TEAM, "name": "alice", "deleted": False, "is_bot": False,
     "profile": {"display_name": "", "real_name": "Alice A"}},
    {"id": "U2", "team_id": TEAM, "name": "bob", "deleted": True, "is_bot": False,
     "profile": {"display_name": "Bobby", "real_name": "Bob B"}},
]
CHANNELS = [
    {"id": "C1", "name": "general", "is_private": False, "topic": {"value": "hi"}},
    {"id": "C2", "name": "secret", "is_private": True, "is_archived": True},
]
DMS = [{"id": "D1", "members": ["U1", "U2"]}]
MPIMS = [{"id": "G1", "name": "mpdm-alice--bob-1", "is_mpim": True}]

DAY1 = [
    {"type": "message", "user": "U1", "ts": "1700000000.000100", "thread_ts": "1700000000.000100",
     "text": "hey <@U2> see <#C2|secret> and <https://x.com/a|the doc> &amp; <https://y.com>",
     "reactions": [{"name": "heart", "count": 1, "users": ["U2"]}],
     "files": [{"id": "F1", "name": "a.png", "mimetype": "image/png", "size": 3,
                "url_private": "https://files.slack.com/F1"}],
     "pinned_to": ["C1"]},
    {"type": "message", "subtype": "channel_join", "user": "U2", "ts": "1700000001.000000",
     "text": "<@U2> has joined the channel"},
    {"type": "message", "user": "U2", "ts": "1700000002.000000", "text": "standalone",
     "edited": {"user": "U2", "ts": "1700000003.000000"}},
]
DAY2 = [  # reply lands in a later day file
    {"type": "message", "user": "U2", "ts": "1700090000.000000",
     "thread_ts": "1700000000.000100", "text": "reply <!here>"},
    {"type": "message", "subtype": "bot_message", "bot_id": "B1", "username": "alerts",
     "ts": "1700090001.000000", "text": "deploy ok"},
]


class FakeSource:
    def __init__(self, files: dict):
        self.files = files

    def read_json(self, rel, default=None):
        return self.files.get(rel, default)

    def exists(self, rel):
        return rel in self.files or rel == "__uploads/F1/a.png"

    def day_files(self):
        out: dict = {}
        for k in self.files:
            if k.count("/") == 1:
                out.setdefault(k.split("/")[0], []).append(k)
        return out


def slack_fixture():
    return FakeSource({
        "users.json": USERS, "channels.json": CHANNELS, "dms.json": DMS, "mpims.json": MPIMS,
        "general/2023-11-14.json": DAY1, "general/2023-11-15.json": DAY2,
        "D1/2023-11-14.json": [{"type": "message", "user": "U1", "ts": "1700000005.0",
                                "text": "psst"}],
        "mystery/2023-11-14.json": [],
    })


def test_slack_text():
    out = slack.slack_to_plain(DAY1[0]["text"], {"U2": "bob"}, {})
    assert out == "hey @bob see #secret and the doc (https://x.com/a) & https://y.com"
    assert slack.slack_to_plain("<https://x.com/a|x.com/a>", {}, {}) == "https://x.com/a"
    assert slack.slack_to_plain("<mailto:a@b.c|a@b.c>", {}, {}) == "a@b.c"


def test_slack_load():
    d = slack.load(slack_fixture())
    assert d["team"] == TEAM
    users = {u["native_id"]: u for u in d["users"]}
    assert users["U1"]["display_name"] == "Alice A"
    assert users["U2"]["display_name"] == "Bobby" and users["U2"]["is_deleted"]
    assert users["B1"]["is_bot"] and users["B1"]["handle"] == "alerts"

    kinds = {c["native_id"]: c["kind"] for c in d["channels"]}
    assert kinds == {"C1": "public", "C2": "private", "D1": "im", "G1": "mpim"}
    assert d["unknown_dirs"] == ["mystery"]

    msgs = {m["id"]: m for m in d["messages"]}
    parent = msgs["slack:C1:1700000000.000100"]
    assert parent["thread_id"] == "1700000000.000100" and parent["is_pinned"]
    assert parent["native_id"] == "C1:1700000000.000100"
    assert parent["sent_at"].isoformat().startswith("2023-11-14T22:13:20")
    assert msgs["slack:C1:1700000002.000000"]["edited_at"] is not None
    assert msgs["slack:C1:1700090000.000000"]["text_plain"] == "reply @here"
    assert "slack:D1:1700000005.0" in msgs
    json.dumps(parent["raw"])

    assert d["files"][0]["local_path"] == "__uploads/F1/a.png"
    assert d["reactions"] == [{"message_id": parent["id"], "emoji": "heart",
                               "user_ids": ["U2"], "count": 1}]


def test_slack_thread_docs():
    d = slack.load(slack_fixture())
    rows = [{**m, "file_names": ["a.png"] if m["id"].endswith("0100") else None}
            for m in d["messages"] if m["channel_id"] == "C1"]
    docs = {x["id"]: x for x in build_thread_docs(rows, {"U1": "alice", "U2": "bob"})}
    assert set(docs) == {"slack:C1:1700000000.000100", "slack:C1:1700000002.000000"}
    t = docs["slack:C1:1700000000.000100"]
    assert t["participant_ids"] == ["U1", "U2"]
    assert t["text_plain"].splitlines() == [
        "@alice: hey @bob see #secret and the doc (https://x.com/a) & https://y.com "
        "[file: a.png]",
        "@bob: reply @here",
    ]
    assert t["sent_at"] == d["messages"][0]["sent_at"]


GUILD = {"id": "G9", "name": "Morpheus", "iconUrl": None}
AUTHOR = {"id": "100", "name": "carol", "nickname": "Carol C", "isBot": False}
DC_CHANNEL = {
    "guild": GUILD,
    "channel": {"id": "500", "type": "GuildTextChat", "categoryId": "400",
                "category": "DEV", "name": "dev", "topic": "code"},
    "messages": [
        {"id": "600", "type": "Default", "timestamp": "2024-01-01T00:00:00+00:00",
         "timestampEdited": "2024-01-01T00:05:00+00:00", "isPinned": True,
         "content": "thread starter <@200> <:mor:123>", "author": AUTHOR,
         "attachments": [{"id": "700", "url": "https://cdn.discordapp.com/x.png",
                          "fileName": "x.png", "fileSizeBytes": 10}],
         "reactions": [{"emoji": {"id": "", "name": "👍", "code": "thumbsup"}, "count": 1,
                        "users": [{"id": "200", "name": "dave"}]}],
         "mentions": [{"id": "200", "name": "dave", "nickname": None}]},
        {"id": "601", "type": "Reply", "timestamp": "2024-01-01T00:01:00Z",
         "content": "a reply", "author": {"id": "200", "name": "dave"},
         "reference": {"messageId": "600", "channelId": "500"}},
        {"id": "602", "type": "GuildMemberJoin", "timestamp": "2024-01-01T00:02:00Z",
         "content": "", "author": {"id": "300", "name": "eve"}},
    ],
}
DC_THREAD = {
    "guild": GUILD,
    "channel": {"id": "600", "type": "GuildPublicThread", "categoryId": "500",
                "category": "dev", "name": "thread starter"},
    "messages": [
        {"id": "610", "type": "ThreadStarterMessage", "timestamp": "2024-01-01T00:00:01Z",
         "content": "", "author": AUTHOR},
        {"id": "611", "type": "Default", "timestamp": "2024-01-01T00:03:00Z",
         "content": "in thread", "author": {"id": "200", "name": "dave"}},
    ],
}


def test_discord_map():
    c = discord.map_export(DC_CHANNEL)
    assert c["workspace"]["native_id"] == "G9"
    assert c["channel"]["kind"] == "public" and c["channel"]["parent_id"] == "400"
    m = {x["id"]: x for x in c["messages"]}
    assert m["discord:600"]["text_plain"] == "thread starter @dave :mor:"
    assert m["discord:600"]["is_pinned"] and m["discord:600"]["edited_at"]
    assert m["discord:600"]["thread_id"] is None
    assert m["discord:601"]["reply_to_id"] == "discord:600"
    assert m["discord:601"]["thread_id"] is None  # Reply is not a thread
    assert m["discord:602"]["subtype"] == "GuildMemberJoin"
    assert c["files"][0]["mimetype"] == "image/png" and c["files"][0]["local_path"] is None
    assert c["reactions"][0]["emoji"] == "thumbsup"
    assert {u["native_id"] for u in c["users"]} == {"100", "200", "300"}
    assert next(u for u in c["users"] if u["native_id"] == "100")["display_name"] == "Carol C"

    t = discord.map_export(DC_THREAD)
    assert t["channel"]["kind"] == "thread" and t["channel"]["parent_id"] == "500"
    assert [x["id"] for x in t["messages"]] == ["discord:611"]
    assert t["messages"][0]["thread_id"] == "600"


def test_discord_thread_docs():
    rows = discord.map_export(DC_CHANNEL)["messages"] + discord.map_export(DC_THREAD)["messages"]
    docs = {x["id"]: x for x in build_thread_docs(rows, {"100": "carol", "200": "dave"})}
    assert set(docs) == {"discord:600", "discord:601"}
    t = docs["discord:600"]
    assert t["channel_id"] == "500" and t["thread_id"] == "600"
    assert t["text_plain"].splitlines() == ["@carol: thread starter @dave :mor:",
                                            "@dave: in thread"]
    assert t["participant_ids"] == ["100", "200"]
