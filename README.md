# RomM-ComM (RomM Communicator Module)

A Discord bot that integrates with the [RomM](https://github.com/rommapp/romm) API to provide information about your ROM collection, handle user ROM requsts, and control RomM from Discord.

<p align="center">
  <img src=".github/Demo.gif" width="60%">
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

---

## Features

### Current

- **Recently Added**: Posts recently added ROM updates to a configured channel (batched when multiple ROMs are added).
- **Request system**: Submit and manage ROM requests entirely from Discord. Requests are enriched with IGDB metadata when available.
- **Search**: Platform-specific searches and a random ROM roll. Results include metadata and download links.
- **Stats**: Near real-time collection statistics shown in voice channel names, the bot "Now Playing" status, and via commands.
- **Multi-file support**: Searches support multi-file games; users can select one, several, or all files to download.
- **Firmware search**: Lists firmware files for a platform with names, sizes, hashes, and download links.
- **Scans**: Start/stop/status for different RomM scan types. The bot reports progress and a summary on completion.
- **Emojis**: Uploads custom console emojis on install; uses emojis in responses and stats. Nitro-aware to expand/revert the emoji set.
- **QR code generation**: Generate QR codes for 3DS/Vita installs by reacting to /search replies with QR emoji (requires download endpoint auth to be disabled on the RomM instance).
- **RomM user management**: Auto-create RomM accounts for Discord users via role assignment; manage Romm > Discord user linking via gui in Discord.
- **Switch Shop info**: Command to display instructions for connecting to a Tinfoil endpoint (download endpoint auth must be disabled).
- **Rate-limited Discord interactions**: Built-in rate limiting to avoid overloading the Discord API.

### Planned

- Alternative chat client integrations: Matrix, Telegram, Slack (no ETA).

---

## Requirements

- Python 3.8+
- Python dependancies (see non-Docker installation below)

---

## Installation

### Docker

1. `docker pull idiosync000/romm-comm:latest`
2. Pass the environment variables (see [Configuration](#configuration)).
3. Mount `/app/data` to a host directory — this stores the user/request DB and emoji sync status.

### Non-Docker (local)

1. Clone the repository or download the source code.
2. Install dependencies:

```bash
pip install py-cord aiohttp python-dotenv qrcode Pillow python-socketio requests aiosqlite
```

---

## Discord Bot Token Creation

- See the Pycord docs for bot creation and permission setup: https://docs.pycord.dev/en/stable/discord.html
- Enable **Privileged Gateway Intents** in the bot settings.

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
RECENT_ROMS_MAX_PER_POST=10
RECENT_ROMS_BULK_THRESHOLD=25
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
- `RECENT_ROMS_*` — Controls for recent-ROM posting (enabled, channel id, thresholds).

---

## Recently Added ROM Notifications
<img align="right" width="300" height="300" src=".github/screenshots/recent_single_rom.png">

- When enabled (`RECENT_ROMS_ENABLED=true`) the bot posts newly added ROMs to the configured channel.
- Multiple ROMs that occur within the configured batch window are grouped into a single batched response by platform.
- Large imports can trigger flood protection; thresholds for maximum listed ROMs and flood limits are adjustable via env vars.

**Note:** Avoid enabling recent-ROM notifications before initial scans. Long-running imports may overload the bot or cause noisy notifications.

---

## Visible Statistics

### Voice channel stats
<img align="right" width="300" height="300" src=".github/screenshots/VC%20Stats.png">

When enabled (`UPDATE_VOICE_NAMES=true`) the bot creates voice channels to display:
- Platform counts
- ROM count
- Save and save-state counts
- Screenshot count
- RomM user count
- Storage usage

Voice channel names are only updated when the underlying stat changes. The bot will create new channels and delete old ones to avoid duplicates.

### Bot status
<img align="right" width="300" height="300" src=".github/screenshots/Rich%20Presence.png">

The bot updates its "Now Playing" / status with the total ROM count whenever it refreshes API data.

---

## Emojis
<img align="right" width="300" height="300" src=".github/screenshots/Basic%20Emojis.png">

- On first boot or when joining a server, the bot uploads a standard set of custom console emojis (default ~50).
- If the server has boosted Nitro/extra emoji slots available, the bot can upload an extended emoji set; if Nitro is later removed the bot reverts to the standard list to preserve the most-used emojis.
- Emojis are used throughout bot responses to visually identify platforms when a matching emoji exists on the server. The extended emoji set covers less popular consoles as well as variants.

---

## Available Commands

- `/search [platform] [game]` — Search ROMs with interactive results and optional QR code for console installs.
- `/request`, `/my_requests`, `/request_admin` — Submit, view, and manage requests.
- `/random [platform]` — Fetch a random ROM (platform optional).
- `/firmware [platform]` — List firmware files with hash details and download links.
- `/scan [option]` — Run or check scans (admin only): `full`, `platform`, `stop`, `status`, `unidentified`, `hashes`, `new_platforms`, `partial`, `summary`.
- `/platforms` — Display all available platforms with their ROM counts.
- `/user_manager` — Manage Romm and Discord users (linking, new account prompting, etc.) (admin only).

---

## Requests

<img align="right" width="300" height="450" src=".github/screenshots/RequestManager.png">

**User features:**
- Submit requests with platform, game name, and optional details.
- Platforms match to IGDB, attempts IGDB matching for fetching metadata. 
- Non-matching requests will still go through for things like rom hacks or unrealeased games, just without metadata.
- Detect existing ROMs in Romm to avoid unnecissary requests and brings up /search results automatically if found.
- DMs requester notification when requests are avalable.
- Handles duplicate requests, additional requesters will also be notified if game is added. 
- Per user request cap (currently 25 pending each).
- Requests can be filled automatically during Romm filesystem scan or manually by admin.

**Admin features:**
- View, filter, and manage pending requests.
- Manually mark as fulfilled, reject or add notes.
- Requester's Discord avatar is present in the request embed as the thumbnail.

---

## User Manager

<img align="right" width="300" height="425" src=".github/screenshots/UserManager.png">

- `/manage_users` — Manage Romm and Discord users (admin only).
- Discord users can be linked to Romm users, entries in dropdown show if a user is linked or not (and to what user names).
- Linking is useful for keeping track of who on the server has a Romm account, also for enriching request information in the request manager.
- Unlinking accounts removes Romm user from server (unless user is an admin).
- Select Discord user and hit 'Create New Romm Account' button to manually onboard user to Romm via DM (see Onboarding via role).

**Onboarding via role:**
- Creates RomM accounts automatically when specified role is added to Discord user.
- Uses Discord display name + suffixes, generates a random password and DMs the user their login info.
- Deletes RomM accounts created by the bot when the role is removed, skips admin accounts.

---

## Error Handling

- Handles API connectivity issues, rate limits, data validation, and caching errors.
- Logs are descriptive but avoid exposing sensitive info.

---

## Security

- Uses OAuth2 bearer tokens for RomM API.
- Secrets configured in environment variables.
- No password logging.
- Strict permission checks for admin commands.

---

## Troubleshooting

- Verify the Discord bot token and permissions.
- Check API connectivity (`API_URL`).
- Review logs for issues.
- Confirm `.env` configuration.
- For flood noise during imports, temporarily disable recent-ROM posting.

---

## Contributing

Contributions are welcome. Open issues or PRs with clear descriptions, logs, and reproduction steps.

---






















