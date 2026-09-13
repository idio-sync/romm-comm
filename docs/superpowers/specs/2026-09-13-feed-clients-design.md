# Feed Clients — Design Spec

**Date:** 2026-09-13
**Status:** Approved (brainstorming) — ready for implementation plan
**Feature area:** Generalize the hardcoded Switch/Tinfoil setup command into a `/feeds` cog covering all five RomM feed clients.

## Overview

RomM exposes URL feeds for five homebrew installers, all under `/api/feeds/` and all uniformly shaped. The bot currently surfaces exactly one of them, Tinfoil, as a hardcoded `/switch_shop_info` command ([cogs/info.py:298-378](../../../cogs/info.py#L298-L378)).

This feature replaces that command with a `cogs/feeds` package offering `/feeds device:<platform>`, covering five device ecosystems across eight devices, gated on what the server actually hosts, and personalized with the invoking user's RomM username.

The existing command is also **factually wrong today**: it advertises the path `/tinfoil/feed`, which RomM has since moved to `/api/feeds/tinfoil`, and it omits the `DISABLE_DOWNLOAD_ENDPOINT_AUTH=true` prerequisite without which Tinfoil downloads cannot work at all. Correcting those is part of the motivation, not a side effect.

## Decisions (locked during brainstorming)

| Decision | Choice |
|----------|--------|
| Command surface | `/feeds device:<autocomplete>` — argument is the **hardware the user owns**, not the homebrew client |
| Device gating | Autocomplete lists only devices whose platform exists on this RomM server |
| Feed auth | Per-client, decided by the client's own capability (see below) |
| Download auth | Treated as a server prerequisite, **detected by live probe**, not assumed |
| Response visibility | Ephemeral |
| Old command | `switch_shop_info` **deleted**, not aliased |
| Out of scope | QR codes, per-user tokens, writes to RomM, proxying/caching feed contents |

### Feed auth rule

The bot stores `romm_username` but deliberately **no password** ([database_manager.py:569](../../../database_manager.py#L569)), so it can never mint a fully-credentialed URL. The rule adopted is: *credentials go in the URL only when the client has nowhere else to put them.*

- **`AuthStyle.FIELDS`** — Tinfoil, pkgi, fpkgi, Kekatsu. These have discrete username/password fields in their own config UI. Emit a **bare URL** plus separate `Username:` (pre-filled from the user's link) and `Password: your RomM password` lines. No secret appears in the message.
- **`AuthStyle.URL_EMBEDDED`** — pkgj only. `/pkgj/config.txt` accepts a bare URL line and nothing else, so the URL must read `https://{username}:<your-password>@{domain}/api/feeds/pkgj/psvita/games`, with the password left as a literal placeholder the user substitutes.

This is encoded as a field on `FeedClient` so `embeds.py` branches in one place, rather than special-casing pkgj by name.

## RomM feed surface (from RomM's feed-clients documentation)

All feeds require basic auth. `DISABLE_DOWNLOAD_ENDPOINT_AUTH=true` opens **only** the per-file download endpoint (`GET /api/roms/{id}/content/…`) — it does **not** make the feed routes unauthenticated. Both facts matter and are frequently conflated.

| Client | Hardware | Path | Files |
|---|---|---|---|
| Tinfoil | Switch | `/api/feeds/tinfoil` | `.nsp` / `.xci` |
| pkgj | Vita / PSP / PSX | `/api/feeds/pkgj/{psvita,psp,psx}/{games,dlc}` | `.pkg` + archives |
| pkgi | PS3 / Vita / PSP | `/api/feeds/pkgi/{ps3,psvita,psp}/{content_type}` | `.pkg` only |
| fpkgi | PS4 / PS5 | `/api/feeds/fpkgi/{ps4,ps5}` | `.pkg` only |
| Kekatsu | NDS | `/api/feeds/kekatsu/nds` | `.nds` only |

pkgi content types: PS3 and PSP accept `game, dlc, demo, update, patch`; Vita additionally accepts `hack, manual, mod, translation, prototype, cheat`.

pkgj is not uniform across its three platforms: Vita and PSP expose both `games` and `dlc`, PSX exposes `games` only. The catalog encodes this per device rather than by templating a shared pair of paths.

**Device → client fan-out** (eight devices):

| Device | Clients |
|---|---|
| `switch` | Tinfoil |
| `psvita` | pkgj **and** pkgi |
| `psp` | pkgj **and** pkgi |
| `psx` | pkgj |
| `ps3` | pkgi |
| `ps4` | fpkgi |
| `ps5` | fpkgi |
| `nds` | Kekatsu |

**Client caveats that must appear in the embed** (each is a known support-thread cause):

- Tinfoil: requires title IDs in filenames — `Game Title [0100000000010000].nsp`.
- Tinfoil: authenticates the feed fetch but does not propagate credentials to the download URLs it receives, hence the download-auth prerequisite.
- fpkgi / Kekatsu: the feed lists **every** ROM on the platform, but only `.pkg` / `.nds` entries install. Users will see entries that fail.
- Kekatsu: the DS's wifi hardware supports WEP and one older WPA variant only; a modern network needs a dedicated SSID or travel router.
- pkgj: configured by appending the URL to `/pkgj/config.txt` via VitaShell.

## Architecture

A `cogs/feeds/` package, following the split style of `cogs/requests/`:

### `catalog.py` — pure data

No I/O, no `discord` imports. Frozen dataclasses:

- `FeedClient` — key, display name, `auth_style`, accepted file formats, caveat strings, docs URL.
- `Feed` — owning client, URL path, label (`"Games"`, `"DLC"`).
- `Device` — key, display name, the RomM slugs/names identifying it, and its tuple of feeds.

### `availability.py` — server facts

Two cached facts, both refreshed on the existing `SYNC_RATE` cadence rather than a new timer, both non-fatal on failure.

**Device availability.** Resolves `fetch_api_endpoint('platforms')` into a frozen set of device keys. Matches on `slug` first, falling back to the substring-on-`name`/`custom_name` check that `check_switch_platform` uses today, so custom-named platforms still resolve. On failure the set is treated as **permissive** (offer all eight) — a stale-but-permissive picker beats a command that silently vanishes.

**Download-auth probe.** Answers "will downloads 403 for this user?":

1. Fetch one ROM (`roms` endpoint, limit 1) for an `id` and `fs_name`.
2. Build its content URL via the existing shared [`build_rom_download_url`](../../../cogs/search.py#L39), then issue a **credential-free** `GET` with `Range: bytes=0-0` on a bare `aiohttp` session — deliberately **not** `bot.fetch_api_endpoint`, which attaches the bot's own token and would always return 200. The Range header keeps the probe to one byte regardless of ROM size.
3. Classify: `401`/`403` → `AUTH_ENABLED`; `200`/`206` → `AUTH_DISABLED`; anything else, a timeout, or an empty library → `UNKNOWN`.

The verdict drives one line per embed: a `⚠️` block naming `DISABLE_DOWNLOAD_ENDPOINT_AUTH` when auth is enabled, nothing when disabled, and a static advisory note when `UNKNOWN`. The degraded path is therefore never worse than the always-warn behavior.

### `embeds.py` — presentation

`device -> discord.Embed`, a pure function of `(device, romm_username | None, probe_verdict)` so it is testable without a bot.

- **Title** — platform emoji from `bot.emoji_dict` + display name, as `switch_shop_info` builds its title today.
- **One field per client.** Single-client devices get one field; Vita and PSP get two (pkgj and pkgi), each labeled with what it is for. This is where the device-first command surface absorbs the pkgj-vs-pkgi ambiguity rather than pushing it onto the user.
- **Field body** — URL(s), then auth per `auth_style`, then the file-format constraint, then client-specific setup steps.
- **Content types** — listing all eleven Vita pkgi content types would swamp the embed. Show `game` and `dlc` explicitly; note the remaining types with the path template.
- **Footer** — link to that client's RomM docs page.

### `__init__.py` — the cog

One `/feeds device:<autocomplete>` command plus `setup(bot)`. Autocomplete is backed by `availability`. An empty resolved set answers plainly that this server hosts no platforms with feed clients.

Username comes from `bot.db.get_user_link(ctx.author.id)`. Unlinked users get the literal `your RomM username` and a nudge toward linking, so the command still serves someone whose RomM account the bot does not know about.

### Knock-on cleanup

Deleting `switch_shop_info` from [cogs/info.py](../../../cogs/info.py) also removes `has_switch`, the `check_switch_platform` startup task, and the entire `cog_slash_command_check` override, which exists solely to gate that one command. Info sheds roughly 90 lines and a background task.

## Wiring

`cogs.feeds` joins the cog list at [bot.py:678](../../../bot.py#L678) with `['aiohttp']` in the parallel dependency map (the probe). `test_extension_loading.py` checks both.

**No new env vars.** The feature needs `API_BASE_URL` and `DOMAIN`, which `Config` already has. This is deliberate: the probe exists precisely so an admin need not declare their auth posture in a second place where it could drift from the truth.

## Testing

Following the existing per-concern test split:

- **Catalog integrity** — every device has at least one feed, every feed path starts `/api/feeds/`, every client key referenced by a feed exists. Catches a typo'd path that would otherwise surface as a user's 404.
- **Device resolution** against fabricated `platforms` payloads: slug match, `custom_name` match (the case `check_switch_platform` handles today, where a regression would be invisible), unknown platforms ignored, empty payload → permissive fallback.
- **Embed formatting** as a pure function, pinning the auth split explicitly: a pkgj embed contains `user:` credentials in its URL; a Tinfoil embed does not and carries separate username/password lines. This is the one rule a future edit is most likely to break silently.
- **Probe classification** as a pure function over status codes, transport mocked, plus one test asserting the probe's request carries **no `Authorization` header** — the bug that would make it always report "disabled".
- **Unlinked-user path** produces a usable embed with the literal placeholder.

## Risks and implementation-time checks

- **Slug values are unverified.** The device-to-slug matching table (`switch`, `psvita`, `psp`, `ps`/`psx`, `ps3`, `ps4`, `ps5`, `nds`) must be checked against a live `platforms` payload during implementation. The `custom_name` fallback limits the blast radius of a wrong guess.
- **Kekatsu platform breadth.** RomM's route is `/api/feeds/kekatsu/{platform_slug}` and some sources suggest slugs beyond `nds` work. Spec `nds` only; widen if verified against RomM's source.
- **Probe and rate-limiting.** The probe is an unauthenticated request the bot makes against its own server once per refresh. On an instance fronted by fail2ban, repeated 401s from the bot's IP are conceivable. One request per `SYNC_RATE` should sit under any sane threshold, but this is a behavior change and warrants a README line.
- **`UNKNOWN` on a fresh server.** An empty library yields `UNKNOWN`, which is the expected common case for a new instance and degrades to the static note.

## Documentation

The README command table gets `/feeds` in place of `/switch_shop_info`, plus a note on the startup probe.
