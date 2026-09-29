# Discord dump (fresh)

Exporter: `~/bin/discord-chat-exporter/DiscordChatExporter.Cli` (v2.47.2). Wrapper `discord-chat-exporter` may be the broken Linux ARM build — call the binary.

Guild: Morpheus `1151741790408429580`. User token, not bot. DiscordChatExporter warns this violates Discord TOS.

Do **not** put the token in this repo. Export it in the shell:

```bash
export DISCORD_TOKEN='…'   # Application tab in Discord web DevTools → localStorage token
```

Output dir (outside git):

```bash
DCE="$HOME/bin/discord-chat-exporter/DiscordChatExporter.Cli"
OUT="$HOME/Documents/discord-export/morpheus"
mkdir -p "$OUT"
```

## 1. List channels (do this first)

No voice, no threads — you pick text channels/categories, then we export with threads.

```bash
"$DCE" channels -g 1151741790408429580 --include-vc false --include-threads None
```

Paste the list back. Reply with the channel or category IDs to keep.

Always skip:

| Why | IDs / names |
|---|---|
| intro (already have; ignore) | `1151741791226306593` intro-gmor |
| onboarding / admin / voice (deleted last dump) | WELCOME, VERIFICATION, VOICE CHANNELS, announcements, verified-links, get-roles, system-notifications |
| art / shipping | any channel or category named `ai-art` or `shipping` |
| forbidden last run | `1228051092323963020` — skip, do not retry in a loop |

## 2. Export only the IDs you chose

JSON, no media, include archived threads, UTC timestamps. Category IDs export every channel in that category.

```bash
"$DCE" export \
  -c CHANNEL_OR_CATEGORY_ID [...] \
  -t "$DISCORD_TOKEN" \
  -f Json \
  --include-threads All \
  --utc \
  -o "$OUT/"
```

`-o` must end with `/`. Do **not** pass `--media`. Do **not** run `exportguild` (that re-pulls skipped categories).

On `forbidden`, skip that ID and continue.

## 3. What a file looks like (ingest already designed)

Each file is one channel or one Discord thread:

```json
{
  "guild": { "id": "1151741790408429580", "name": "Morpheus" },
  "channel": {
    "id": "…",
    "type": "GuildTextChat",
    "categoryId": "…",
    "category": "COMMUNITY LOUNGE",
    "name": "…",
    "topic": null
  },
  "messages": [ { "id": "…", "type": "Default", "timestamp": "…", "content": "…", "author": {}, "reference": {} } ]
}
```

Thread channels (`GuildPublicThread` / `GuildPrivateThread` / forum posts) are separate files. That is enough to ingest; remaining guild volume does not change the schema.
