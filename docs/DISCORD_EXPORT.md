# DiscordChatExporter: install and extract

Guild: Morpheus `1151741790408429580`. Format: JSON. No media. User token, not bot.

DiscordChatExporter warns that automating a **user** account violates Discord TOS and can get the account banned. Use a bot token if the server ever grants one with read access; until then a user token is the only way this account can see the guild.

Do **not** commit the token. Put it in the shell or in a gitignored file.

Exporter already on this Mac (if you installed it in August): `~/bin/discord-chat-exporter/DiscordChatExporter.Cli` v2.47.2 (self-contained `osx-arm64`). The PATH wrapper `discord-chat-exporter` has been a broken Linux ARM build — always call `DiscordChatExporter.Cli`.

Latest upstream as of this doc: **2.48**. Either version is fine for JSON export.

## 0. Install the CLI (macOS Apple Silicon)

Self-contained zip. No .NET SDK.

```bash
VER=2.48
DEST="$HOME/bin/discord-chat-exporter"
ZIP="DiscordChatExporter.Cli.osx-arm64.zip"

mkdir -p "$HOME/bin"
curl -fsSL -o "/tmp/$ZIP" \
  "https://github.com/Tyrrrz/DiscordChatExporter/releases/download/${VER}/${ZIP}"
rm -rf "$DEST"
mkdir -p "$DEST"
unzip -o "/tmp/$ZIP" -d "$DEST"
chmod +x "$DEST/DiscordChatExporter.Cli"
xattr -dr com.apple.quarantine "$DEST" 2>/dev/null || true

# optional: keep using the real binary, not a wrapper
# already in ~/.zshrc: export PATH="$HOME/bin/discord-chat-exporter:$PATH"
hash -r
"$DEST/DiscordChatExporter.Cli" --version
```

Other machines: same URL pattern, swap the zip.

| Host | Zip |
|---|---|
| macOS arm64 | `DiscordChatExporter.Cli.osx-arm64.zip` |
| macOS Intel | `DiscordChatExporter.Cli.osx-x64.zip` |
| Linux x64 | `DiscordChatExporter.Cli.linux-x64.zip` |
| Linux arm64 | `DiscordChatExporter.Cli.linux-arm64.zip` |

Docker alternative (does not need a local unzip):

```bash
docker pull tyrrrz/discordchatexporter:2.48
```

Official GUI/CLI downloads: https://github.com/Tyrrrz/DiscordChatExporter/releases

## 1. Token

```bash
"$HOME/bin/discord-chat-exporter/DiscordChatExporter.Cli" guide
```

User token (current method):

1. Open https://discord.com/app in a browser while logged in as the exporting account.
2. DevTools → **Application** (Chrome) / **Storage** (Firefox).
3. Local Storage → `https://discord.com` → key `token`. Copy the value **without** quotes.
4. Or DevTools → Network → any `api/v9` request → request header `Authorization`.

```bash
export DISCORD_TOKEN='…'    # never echo this into the repo
```

The August 2026 token is **dead** (`Authentication token is invalid`). Get a fresh one each session if Discord rotated it.

## 2. Output directory (outside git)

```bash
DCE="$HOME/bin/discord-chat-exporter/DiscordChatExporter.Cli"
OUT="$HOME/Documents/discord-export/morpheus"
mkdir -p "$OUT"
```

## 3. List channels — stop here and pick IDs

No voice, no threads. You choose what to export.

```bash
"$DCE" channels -g 1151741790408429580 --include-vc false --include-threads None
```

Paste the list. Reply with channel IDs and/or **category** IDs to keep. A category ID exports every channel in that category.

Always skip:

| Why | IDs / names |
|---|---|
| intro (ignore even if still on disk) | `1151741791226306593` intro-gmor |
| onboarding / admin / voice | WELCOME, VERIFICATION, VOICE CHANNELS, announcements, verified-links, get-roles, system-notifications |
| art / shipping | any channel or category named `ai-art` or `shipping` |
| forbidden last run | `1228051092323963020` — skip, do not retry in a loop |

## 4. Export only the IDs you chose

JSON, no `--media`, include archived threads, UTC timestamps. `-o` **must** end with `/`.

```bash
"$DCE" export \
  -c CHANNEL_OR_CATEGORY_ID [...] \
  -t "$DISCORD_TOKEN" \
  -f Json \
  --include-threads All \
  --utc \
  -o "$OUT/"
```

Do **not** run `exportguild` (that re-pulls skipped categories). Do **not** pass `--media`.

On `forbidden`, drop that ID and continue.

One file per channel or Discord thread (`GuildPublicThread` / `GuildPrivateThread` / forum post). That shape is enough for ingest.

## 5. Docker-flavoured export (optional)

```bash
docker run --rm \
  -e DISCORD_TOKEN \
  -v "$OUT:/out" \
  tyrrrz/discordchatexporter:2.48 \
  export -c CHANNEL_ID -f Json --include-threads All --utc -o /out/
```

Same skip list. Same token env var.
