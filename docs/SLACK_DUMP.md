# Slackdump: install, dump, and import an existing zip

Workspace: **morpheus-qyv9301** (team `T08PC074FJQ`). Tool: [rusq/slackdump](https://github.com/rusq/slackdump) v4.4.4 (Homebrew).

The zip is Slack-export-format JSON plus slackdump extras (`__uploads/` for attachment bytes). That is the source for Postgres ingest (`origin=slack`). Do **not** commit dumps. Keep them next to the existing archive:

```
~/Documents/Morpheus/slackdump_20260826_012610.zip   # ~3 GB, 26 Aug 2026
~/temp/slackdump-extract/                            # unzip of that dump
```

slackdump's own `mcp` command is a **browse-only** server over a zip/dir. The unified Slack+Discord search MCP in this repo is separate (`mcp/` + Postgres). Use slackdump MCP only to inspect a dump before ingest.

User-token dumps can violate Slack TOS. This is the same method used for the August archive.

## 0. Install

macOS (this machine already has it):

```bash
brew install slackdump
# or: brew upgrade slackdump   # need >= 4.4.4
slackdump version
# Slackdump 4.4.4 (commit: Homebrew) ...
```

Credentials live in `~/Library/Caches/slackdump/` (`*.bin`). Do not copy those files into git.

## 1. Authenticate a workspace (macOS QR flow)

Browser automation (`workspace new` + EZ-Login) routinely dies on this Mac (Brave/CDP: existing browser session, about:blank, net::ERR_ABORTED). Use **QR login** and a **bare name**, never a URL.

```bash
# 1. Quit Chromium-family browsers completely (Cmd+Q). Confirm:
pgrep -fl "Brave Browser|Google Chrome|Chromium" || true

# 2. Name only — not https://morpheus-qyv9301.slack.com
slackdump workspace new morpheus-qyv9301
```

In the login-type menu: arrow down to **QR Code**, Enter.

Then in your normal Slack web session:

1. Workspace name (top-left) -> **Sign in on mobile**.
2. Right-click the QR image -> **Copy Image URL** (`data:image/png;base64,...`). Codes expire in minutes.
3. Paste into the slackdump prompt, Enter.

Expect authenticated then Success: added workspace "morpheus-qyv9301".

```bash
slackdump workspace list
# => morpheus-qyv9301 (file: morpheus-qyv9301.bin, ...)
```

If you passed a URL as the name, list is empty and nothing was saved. Rerun with the bare name.

This workspace is already on disk from 26 Aug 2026. Re-auth only if export starts failing with login errors.

Other login methods (`slackdump help login`): `xoxc-` + cookie, or a bot `xoxb-` token. Bot tokens only see what the bot is invited to.

## 2. List what you can dump

```bash
slackdump workspace select morpheus-qyv9301
slackdump list channels -no-save
slackdump list users -no-save
```

Channel syntax for later flags: Slack ID (`C08PXU4GTQU`), `#name`, or a Slack URL. See `slackdump help syntax` for include/exclude.

## 3. Fresh dump (export zip)

Full workspace, files included (Mattermost layout: `__uploads/<FILE_ID>/filename`). This is what the August dump used.

```bash
OUT="$HOME/Documents/Morpheus"
STAMP=$(date -u +%Y%m%d_%H%M%S)

cd "$OUT"
slackdump export \
  -workspace morpheus-qyv9301 \
  -files=true \
  -type mattermost \
  -o "$OUT/slackdump_${STAMP}.zip"
```

That will take a while and can be several GB. DMs and MPIMs are included by default (`-chan-types mpim,im,public_channel,private_channel`).

Useful variants:

```bash
# no attachment bytes (small, URLs only — worse for ingest)
slackdump export -workspace morpheus-qyv9301 -files=false -o "$OUT/slackdump_${STAMP}_nofiles.zip"

# one channel
slackdump export -workspace morpheus-qyv9301 -o "$OUT/api-gateway.zip" C08...   # or #api-gateway

# date window (UTC)
slackdump export -workspace morpheus-qyv9301 \
  -time-from 2026-01-01T00:00:00 \
  -time-to   2026-09-01T00:00:00 \
  -o "$OUT/slackdump_2026h1.zip"
```

Do **not** put `-o` inside this git repo. `*.zip` is gitignored, but keep dumps in `~/Documents/Morpheus/` or `~/temp/`.

### Resume / incremental

`export` is a full run. For crash-resume use **archive** (SQLite), then convert:

```bash
DIR="$HOME/temp/slackdump-archive-$STAMP"
mkdir -p "$DIR"
slackdump archive -workspace morpheus-qyv9301 -o "$DIR"
# if it dies:
slackdump resume "$DIR"

slackdump convert -f export -o "$OUT/slackdump_${STAMP}.zip" "$DIR"
```

## 4. Import an existing zip

The August dump is already the right format. You do **not** need to dump again to ingest it.

Path: `~/Documents/Morpheus/slackdump_20260826_012610.zip`
~3 GB, 5188 files, Mattermost layout, messages Apr 2025-Aug 2026.

### 4a. Extract for Postgres ingest (this repo)

Ingest reads a directory of `channels.json`, `users.json`, `dms.json`, `mpims.json`, `{channel}/*.json`, and `__uploads/`. Unzip once; do not unzip into git.

```bash
ZIP="$HOME/Documents/Morpheus/slackdump_20260826_012610.zip"
DEST="$HOME/temp/slackdump-extract"

mkdir -p "$DEST"
# already extracted on this Mac; re-run only if DEST is missing or stale
if [ ! -f "$DEST/users.json" ]; then
  unzip -q "$ZIP" -d "$DEST"
fi

ls "$DEST/users.json" "$DEST/channels.json" "$DEST/dms.json" "$DEST/mpims.json"
# day files: $DEST/<channel-name-or-id>/YYYY-MM-DD.json
# files:     $DEST/__uploads/<FILE_ID>/...
```

Field mapping: `docs/INGEST.md` (Slack section). The Python ingest job is **not built yet**; when it exists it should take `--source "$DEST"` (or the zip, unzipping to a temp dir). Until then, `$DEST` is the working copy.

Re-extract over a dirty tree:

```bash
rm -rf "$DEST"
mkdir -p "$DEST"
unzip -q "$ZIP" -d "$DEST"
```

### 4b. Browse the zip without extracting

```bash
slackdump view "$HOME/Documents/Morpheus/slackdump_20260826_012610.zip"
```

Opens a local viewer. Same command works on an extracted directory.

Browse-only MCP (not the Hetzner search MCP):

```bash
slackdump mcp "$HOME/Documents/Morpheus/slackdump_20260826_012610.zip"
# tools: load_source, list_channels, get_messages, get_thread
```

### 4c. Convert zip to SQLite (optional)

Useful if you want SQL over the dump without waiting for our ingest:

```bash
slackdump convert -f database \
  -o "$HOME/temp/slackdump-sqlite" \
  "$HOME/Documents/Morpheus/slackdump_20260826_012610.zip"
# then: sqlite3 "$HOME/temp/slackdump-sqlite/slackdump.sqlite"
```

The other way (database -> export zip) is how you turn an `archive` run into the format this repo expects.

### 4d. Official Slack export zip

If you ever get a zip from Slack admin **Export** instead of slackdump: same JSON layout, usually **no** `__uploads/` bytes. Hydrate files (needs a token) with `slackdump tools hydrate <export_file>`, or ingest metadata-only and leave `files.local_path` null.

## 5. Sanity check after unzip

```bash
python3 - <<'PY'
import json
from pathlib import Path
root = Path.home() / "temp" / "slackdump-extract"
for name in ("users.json", "channels.json", "dms.json", "mpims.json"):
    data = json.loads((root / name).read_text())
    print(f"{name:16} {len(data):5d}")
days = list(root.glob("*/*.json"))
print("day files", len(days))
uploads = list((root / "__uploads").glob("*/*")) if (root / "__uploads").exists() else []
print("uploaded files", len(uploads))
PY
```

Expected on the August dump (approximate): 63 users, 58 channels, 53 DMs, 5 MPIMs, ~1725 day files.

## 6. What not to do

- Do not commit zips, `__uploads/`, or `~/Library/Caches/slackdump/*.bin`.
- Do not use slackdump `mcp` as the production search API; that is `docs/HETZNER.md`.
- Do not dump into `Morpheus/morpheus-comms/`.
- Do not pass the Slack URL as the workspace **name**.
