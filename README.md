# RomM-ComM (RomM Communicator Module)

A Discord bot that integrates with the [RomM](https://github.com/rommapp/romm) API to provide information about your ROM collection and control RomM from Discord.

<p align="center">
  <img src=".github/Demo.gif" width="90%">
</p>

---

## Table of Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Discord Bot Token Creation](#discord-bot-token-creation)
- [RomM Settings](#romm-settings)
- [Configuration](#configuration)
- [Recently Added ROM Notifications](#recently-added-rom-notifications)
- [Visible Statistics](#visible-statistics)
- [Emojis](#emojis)
- [Available Commands](#available-commands)
- [Requests](#requests)
- [User Manager](#user-manager)
- [Error Handling](#error-handling)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)
- [Gallery](#gallery)

---

## Features

### Current

- **Recently Added**: Posts recently added ROM updates to a configured channel (batched when multiple ROMs are added).
- **Stats**: Near real-time collection statistics shown in voice channel names, the bot "Now Playing" status, and via commands.
- **Search**: Platform-specific searches and a random ROM roll. Results include download links, file information, and cover images when available.
- **Multi-file support**: Searches support multi-file games; users can select one, several, or all files to download.
- **Firmware search**: Lists firmware files for a platform with names, sizes, hashes, and download links.
- **Scans**: Start/stop/status for different RomM scan types. The bot reports progress and a summary on completion.
- **Request system**: Submit and manage ROM requests entirely from Discord. Requests are enriched with IGDB metadata when available.
- **Request dashboard**: Optional web dashboard for admins to manage requests.
- **Emojis**: Uploads custom console emojis on install; uses emojis in responses and stats. Nitro-aware to expand/revert the emoji set.
- **QR code generation**: Generate QR codes for 3DS/Vita installs (requires download endpoint auth to be disabled on the RomM instance).
- **RomM user management**: Auto-create RomM accounts for Discord users with a configured role and optionally delete accounts when the role is removed.
- **Switch Shop info**: Command to display instructions for connecting to a Tinfoil endpoint (download endpoint auth must be disabled).
- **Rate-limited Discord interactions**: Built-in rate limiting to avoid overloading the Discord API.
- **Caching**: Only fetches stats that have changed since the last fetch to reduce load.

### Planned

- Alternative chat client integrations: Matrix, Telegram, Slack (no ETA).

---

## Requirements

- Python 3.8+
- py-cord
- aiohttp
- python-dotenv
- qrcode
- Pillow
- python-socketio
- requests
- aiosqlite

---

## Installation

### Docker

1. `docker pull idiosync000/romm-comm:latest`
2. Pass the environment variables (see [Configuration](#configuration)).
3. Mount `/app/data` to a host directory — this stores the request DB and emoji sync status.

### Non-Docker (local)

1. Clone the repository or download the source code.
2. Install dependencies:

```bash
pip install py-cord aiohttp python-dotenv qrcode Pillow python-socketio requests aiosqlite
```

---

## Discord Bot Token Creation

- See the Pycord docs for bot creation and permission setup: https://docs.pycord.dev/en/stable/discord.html
- Enable **Privileged Gateway Intents** in the bot settings as required by your bot features.

---

## RomM Settings

If you want downloads to work without requiring users to log in (including Switch shop / QR code console installs), set the following on your RomM server environment:

```env
DISABLE_DOWNLOAD_ENDPOINT_AUTH=true
```

Setting this disables authentication for the download endpoint. If not set (or set to `false`), users must be logged in to RomM to download files.

---

## Configuration

Create a `.env` file in the project root with the following variables.

```env
# Required
TOKEN=your_discord_bot_token
GUILD=your_guild_id
API_URL=http://your_romm_host:port
USER=api_username
PASS=api_password

# Optional
ADMIN_ID=admin_user_id
DOMAIN=your_website_domain
REQUESTS_ENABLED=true
IGDB_CLIENT_ID=your_client_id
IGDB_CLIENT_SECRET=your_client_secret
AUTO_REGISTER_ROLE_ID=romm_users_role_id
UPDATE_VOICE_NAMES=true
CHANNEL_ID=your_channel_id
RECENT_ROMS_ENABLED=true
RECENT_ROMS_CHANNEL_ID=your_channel_id
WEB_DASHBOARD_ENABLED=true
WEB_DASHBOARD_PORT=8080
DASHBOARD_PASSWORD=yourpassword
```

### Configuration details

**Required:**
- `TOKEN` — Discord bot token.
- `GUILD` — Discord server (guild) ID.
- `API_URL` — Base URL for your RomM instance (use `http://ip:port` or a domain).
- `USER` / `PASS` — API credentials for RomM.

**Common optional settings (defaults shown where applicable):**
- `ADMIN_ID` — User ID allowed to run admin commands (scan, sync users, etc.).
- `DOMAIN` — Public domain for download links (default: `No website configured`).
- `SYNC_RATE` — How often to sync with the API in seconds (default: `3600`).
- `UPDATE_VOICE_NAMES` — Enable voice channel stats (default: `true`).
- `REQUESTS_ENABLED` — Enable request commands (default: `true`).
- `IGDB_CLIENT_ID`, `IGDB_CLIENT_SECRET` — For request metadata (can be shared with RomM).
- `AUTO_REGISTER_ROLE_ID` — Role that triggers automatic RomM account creation.
- `SHOW_API_SUCCESS` — Show API sync results and errors in Discord (default: `false`).
- `CHANNEL_ID` — Channel for sync results and user manager logs.
- `CACHE_TTL` — Cache TTL in seconds (default: `3900`).
- `API_TIMEOUT` — API request timeout in seconds (default: `10`).
- `RECENT_ROMS_*` — Controls for recent-ROM posting (enabled, channel id, intervals, batch size, thresholds).
- `WEB_DASHBOARD_*` — Dashboard enable/host/port/password settings.

---

## Recently Added ROM Notifications

- When enabled (`RECENT_ROMS_ENABLED=true`) the bot posts newly added ROMs to the configured channel.
- Multiple ROMs that occur within the configured batch window are grouped into a single batched response by platform.
- Large imports can trigger flood protection; thresholds for maximum listed ROMs and flood limits are adjustable via env vars.

**Note:** Only enable recent-ROM posting on installations where most ROMs are already scanned. Large, long-running imports may overload the bot or cause noisy notifications.

---

## Visible Statistics

### Voice channel stats

When enabled (`UPDATE_VOICE_NAMES=true`) the bot creates voice channels to display:
- Platform counts
- ROM count
- Save and save-state counts
- Screenshot count
- RomM user count
- Storage usage

Voice channel names are only updated when the underlying stat changes. The bot will create new channels and delete old ones to avoid duplicates.

### Bot status

The bot updates its "Now Playing" / status with the total ROM count whenever it refreshes API data.

---

## Emojis

- On first boot or when joining a server, the bot uploads a standard set of custom console emojis (default ~50).
- If the server has boosted Nitro/extra emoji slots available, the bot can upload an extended emoji set; if Nitro is later removed the bot reverts to the standard list to preserve the most-used emojis.
- Emojis are used throughout bot responses to visually identify platforms when a matching emoji exists on the server.

---

## Available Commands

- `/platforms` — Display all available platforms with their ROM counts.
- `/search [platform] [game]` — Search ROMs with interactive results and optional QR code for console installs.
- `/random [platform]` — Fetch a random ROM (platform optional).
- `/firmware [platform]` — List firmware files with hash details and download links.
- `/scan [option]` — Run or check scans: `full`, `platform`, `stop`, `status`, `unidentified`, `hashes`, `new_platforms`, `partial`, `summary`.
- `/request`, `/my_requests`, `/request_admin` — Submit, view, and manage requests.
- `/sync_users` — Admin-only user sync.

---

## Requests System

**User features:**
- Submit requests with platform, game name, and optional details.
- Detect existing ROMs to avoid duplicates.
- Attempts IGDB matching for metadata.
- Per-user request cap (default: 25).
- DM notifications when requests are fulfilled or rejected.

**Admin features:**
- View, filter, and manage pending requests.
- Fulfill/reject/add notes directly or via the web dashboard.

**Dashboard:**
- Filter requests by status, platform, user, or fulfillment method.
- Admins can manually manage requests from a browser.

---

## User Manager

- `/sync_users` — Sync Discord users who have the auto-register role (admin only).

**Account creation:**
- Creates RomM accounts automatically when role is added.
- Uses Discord display name + suffixes.
- Generates a random password and DMs the user.
- Preserves and warns on existing admin accounts.

**Role removal:**
- Deletes RomM accounts created by the bot when the role is removed.
- Skips admin accounts and logs protected deletions.

---

## Error Handling

- Handles API connectivity issues, rate limits, data validation, and caching errors.
- Logs are descriptive but avoid exposing sensitive info.

---

## Security

- Uses OAuth2 bearer tokens for RomM API.
- Secrets configured in environment variables.
- No password logging.
- Strict permission checks.

---

## Troubleshooting

- Verify the Discord bot token and permissions.
- Check API connectivity (`API_URL`).
- Review logs for issues.
- Confirm `.env` configuration.
- For flood noise during imports, temporarily disable recent-ROM posting.

---

## Gallery

### Platform & Search
<p align="center">
  <img src=".github/screenshots/SlashPlatforms.png" width="45%">
  <img src=".github/screenshots/SingleFile.png" width="45%">
</p>

### Random & Firmware
<p align="center">
  <img src=".github/screenshots/BotStatus.png" width="45%">
  <img src=".github/screenshots/SlashFirmware.png" width="45%">
</p>

### Scans
<p align="center">
  <img src=".github/screenshots/SlashScanStatus.png" width="60%">
</p>

### Requests
<p align="center">
  <img src=".github/screenshots/RequestSubmitted.png" width="45%">
  <img src=".github/screenshots/RequestFulfilledDM.png" width="45%">
</p>

### User Manager
<p align="center">
  <img src=".github/screenshots/UserManagerMessages.png" width="45%">
  <img src=".github/screenshots/UserNotification.png" width="45%">
</p>

### Emojis & Nitro Support
<p align="center">
  <img src=".github/screenshots/Nitro.png" width="60%">
</p>

---

## Contributing

Contributions are welcome. Open issues or PRs with clear descriptions, logs, and reproduction steps.

---




