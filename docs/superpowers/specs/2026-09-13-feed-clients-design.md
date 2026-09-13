# Feed Clients — Design Spec

**Date:** 2026-09-13
**Status:** Approved (brainstorming) — ready for implementation plan
**Feature area:** Generalize the hardcoded Switch/Tinfoil setup command into a `/feeds` cog covering all five RomM feed clients.

## Overview

RomM exposes URL feeds for five homebrew installers, all under `/api/feeds/` and all uniformly shaped. The bot currently surfaces exactly one of them, Tinfoil, as a hardcoded `/switch_shop_info` command ([cogs/info.py:298-378](../../../cogs/info.py#L298-L378)).

This feature replaces that command with a `cogs/feeds` package offering `/feeds device:<platform>`, covering five device ecosystems across eight devices, gated on what the server actually hosts, and personalized with the invoking user's RomM username.

The existing command is also **factually wrong today**: it advertises the path `/tinfoil/feed`, which RomM has since moved to `/api/feeds/tinfoil`. Its embed also omits the `DISABLE_DOWNLOAD_ENDPOINT_AUTH=true` prerequisite without which Tinfoil downloads cannot work at all — the README bullet ([README.md:51](../../../README.md#L51)) does mention it, so the gap is between the docs and the in-Discord guidance the user actually follows. Correcting both is part of the motivation, not a side effect.

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

**The username must be percent-encoded.** `romm_username` is a free-text `TEXT NOT NULL` column, and this is the one place in the design where user-controlled text lands in a security-relevant URL position: a username containing `@` silently repoints the URL at a different host, and `:`, `/`, `#`, `?` break it in other ways. Encode with `urllib.parse.quote(username, safe='')` — the repo has the idiom next door at [`encode_rom_download_filename`](../../../cogs/search.py#L33) — and test a username containing `@`.

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
- `Feed` — owning client, **concrete** URL path, label (`"Games"`, `"DLC"`), and `extra_content_types: tuple[str, ...]`.
- `Device` — key, display name, the RomM slugs/names identifying it, and its tuple of feeds.

**Content types resolve here, not in `embeds.py`.** pkgi accepts 5 content types on PS3/PSP and 11 on Vita; enumerating all of them as `Feed` entries would swamp the embed, and storing a template string would make the "every path starts `/api/feeds/`" integrity test vacuous. So: `Feed` stays concrete and is enumerated only for `game` and `dlc`, and the remaining types ride on `extra_content_types`, which `embeds.py` renders as a single template line. The catalog therefore owns the decision about what is shown in full versus summarized, and `embeds.py` owns no filtering rule.

### `availability.py` — server facts

Two server facts, both non-fatal on failure. They are refreshed by **different** mechanisms, because only one of them already has a cache.

**Device availability — read the existing platforms cache, add no timer.** `update_api_data` already refreshes `bot.cache.set('platforms', …)` every `SYNC_RATE` ([bot.py:957-967](../../../bot.py#L957-L967)), and `sanitize_data`'s platforms branch ([bot.py:863-873](../../../bot.py#L863-L873)) preserves both `name` and `custom_name` — exactly the two fields the matcher needs. So `availability.py` resolves device keys from `bot.cache.get('platforms')` **on demand**, per autocomplete invocation. This is free, always as fresh as `SYNC_RATE`, and requires no wiring.

This matters because there is **no cadence a cog can hook into**. `SYNC_RATE` drives exactly one thing, `bot.update_loop` ([bot.py:817-820](../../../bot.py#L817-L820)) calling `update_api_data`, which reaches cogs only through two explicit named calls to `get_cog('Info')` ([bot.py:992](../../../bot.py#L992)). There is no event, listener, or registry. (`Info.on_stats_update` exists but nothing calls it.) Any design that says "refreshed on the existing cadence" is really asking for either a `bot.py` edit or a new timer; reading the cache avoids needing either.

Matching: `slug` first, falling back to the substring-on-`name`/`custom_name` check that `check_switch_platform` uses today, so custom-named platforms still resolve. A missing or empty cache is treated as **permissive** (offer all eight) — a stale-but-permissive picker beats a command that silently vanishes.

**Accepted consequence of reusing that cache:** `sanitize_data` filters out platforms with a falsy `rom_count` ([bot.py:872](../../../bot.py#L872)). A platform that exists but holds no ROMs will therefore not be offered. For device gating this is the desired behavior — a feed with nothing in it helps nobody — but it is a deliberate decision, not an accident.

**Download-auth probe.** Answers "will downloads 403 for this user?" This one has no existing cache, so it owns its schedule: it runs from a `Cog.listener` on `on_ready` (not from `__init__`, which stays I/O-free), caches its verdict, and re-probes no more often than `SYNC_RATE` when a `/feeds` invocation finds the verdict stale. No `tasks.loop` — `bot.close()` ([bot.py:891-902](../../../bot.py#L891-L902)) cancels `update_loop` and `refresh_token_task` **by name**, so a cog-owned loop would not be cancelled on shutdown.

1. Fetch one ROM (`roms` endpoint, limit 1) for an `id` and `fs_name`. **The response has two shapes** — a dict with an `"items"` key, or a bare list. Handle both, using the idiom already at [cogs/achievements.py:201-207](../../../cogs/achievements.py#L201-L207); handling only one returns `UNKNOWN` on half of RomM's versions.
2. Build its content URL via the existing shared [`build_rom_download_url`](../../../cogs/search.py#L39), then issue a **credential-free** `GET` with `Range: bytes=0-0` on a bare `aiohttp` session. The Range header keeps the probe to one byte regardless of ROM size.
3. Classify: `401`/`403` → `AUTH_ENABLED`; `200`/`206` → `AUTH_DISABLED`; anything else, a timeout, or an empty library → `UNKNOWN`.

**Which base URL to probe.** `build_rom_download_url` takes a `domain`, and the two candidates are not interchangeable here. Probe **`config.DOMAIN`**, because that is the path the *user's console* will traverse — reverse proxy included — and the probe's whole purpose is to predict what happens to the user. `API_BASE_URL` is how the bot reaches the server, which in a containerized deploy is frequently a different address entirely, and testing it would answer a question nobody asked. `DOMAIN` defaults to the literal string `'No website configured'` ([bot.py:309](../../../bot.py#L309)), so an unset or placeholder `DOMAIN` **short-circuits to `UNKNOWN` without issuing a request**. Embed URLs use `DOMAIN` regardless.

**Why not `bot.fetch_api_endpoint`.** Two reasons, both worth stating so nobody "fixes" the probe by routing it back through the client: it attaches the bot's own credentials and would always return 200, *and* it cannot take a URL at all — it accepts an endpoint name relative to `API_BASE_URL` ([bot.py:497-502](../../../bot.py#L497-L502) → [romm_client.py:397](../../../romm_client.py#L397)).

The verdict drives one line per embed: a `⚠️` block naming `DISABLE_DOWNLOAD_ENDPOINT_AUTH` when auth is enabled, nothing when disabled, and a static advisory note when `UNKNOWN`. The degraded path is therefore never worse than the always-warn behavior.

### `embeds.py` — presentation

`device -> discord.Embed`, a pure function of `(device, romm_username | None, probe_verdict)` so it is testable without a bot.

- **Title** — platform emoji + display name. Resolve the emoji through the `bot.platform_emoji` service ([cogs/platform_emoji.py:110](../../../cogs/platform_emoji.py#L110)), **not** by indexing `bot.emoji_dict` the way `switch_shop_info` does. `PlatformEmoji` has a three-step fallback (server emoji → application emoji → 🎮) that five cogs already use; direct dictionary indexing is a `KeyError` for any device whose emoji was never uploaded, and unlike `switch_shop_info` — which swallows it in a broad `except` — a pure embed function has no such net. Since the emoji is a bot-dependent lookup, resolve it in `cog.py` and pass the resolved string into the pure `embeds.py` function.
- **One field per client.** Single-client devices get one field; Vita and PSP get two (pkgj and pkgi), each labeled with what it is for. This is where the device-first command surface absorbs the pkgj-vs-pkgi ambiguity rather than pushing it onto the user.
- **Field body** — URL(s), then auth per `auth_style`, then the file-format constraint, then client-specific setup steps.
- **Content types** — listing all eleven Vita pkgi content types would swamp the embed. Show `game` and `dlc` explicitly; note the remaining types with the path template.
- **Footer** — link to that client's RomM docs page.

### `cog.py` — the cog, and `__init__.py` — the entry point

Matching the `cogs/requests/` precedent exactly: the cog class lives in `cog.py`, and `__init__.py` holds re-exports plus `setup(bot)` (there, 39 lines against a 1217-line `cog.py`). `tests/test_extension_loading.py:81-84` pins that shape for requests, so following it keeps one convention rather than inventing a second.

`__init__` does **no I/O** — the probe is deferred to an `on_ready` listener. This keeps the cog constructible from a minimal fake bot, which matters if the feeds cog ever gets the real-load treatment `RequestsExtensionTests` gives requests.

One `/feeds device:<autocomplete>` command. Autocomplete is backed by `availability`, but **autocomplete is a convenience, not a gate** — py-cord does not constrain what a user can type and submit. All three paths need defined ephemeral responses:

- **Empty resolved set** — this server hosts no platforms with feed clients.
- **Unknown device key** (typed free-hand, matches no device) — say so, and list the devices that are available.
- **Known device, absent platform** (a real device key the server doesn't host) — say that this server doesn't host that platform, distinctly from the unknown-key case, since the two have different fixes.

**Responses are ephemeral** — restated here because the command it replaces responds publicly ([cogs/info.py:374](../../../cogs/info.py#L374)), and the Decisions table alone is easy to lose sight of at implementation time.

Username comes from `bot.db.get_user_link(ctx.author.id)`. Unlinked users get the literal `your RomM username` and a nudge toward linking, so the command still serves someone whose RomM account the bot does not know about.

### Knock-on cleanup

Deleting `switch_shop_info` ([cogs/info.py:297-378](../../../cogs/info.py#L297-L378), 82 lines) also removes `check_switch_platform` (142-166), the entire `cog_slash_command_check` override (168-174, which exists solely to gate that one command and returns `True` for every other Info command, so its removal is a behavioral no-op), and the two `has_switch`/task lines in `__init__` (21-22). About **116 lines**, taking `cogs/info.py` from 381 to roughly 265, and removing one startup task.

Verified: those names appear nowhere else in the repo and in no test.

## Wiring

`cogs.feeds` joins the `core_cogs` list ([bot.py:677](../../../bot.py#L677)) and gets an `['aiohttp']` entry in the separate `cog_dependencies` dict ([bot.py:690-700](../../../bot.py#L690-L700)) for the probe.

**Only the first of those is currently tested.** `declared_cogs()` ([tests/test_extension_loading.py:29-42](../../../tests/test_extension_loading.py#L29-L42)) AST-parses `core_cogs`; `cog_dependencies` is read by no test, so a missing or typo'd entry fails silently at startup with a `logger.error` and a skipped cog ([bot.py:710-713](../../../bot.py#L710-L713)). Close that gap as part of this work: a four-line test, built on the existing `declared_cogs()` helper, asserting every `core_cogs` entry has a `cog_dependencies` key.

**No new env vars.** The feature needs `API_BASE_URL` and `DOMAIN`, which `Config` already has. This is deliberate: the probe exists precisely so an admin need not declare their auth posture in a second place where it could drift from the truth.

## Testing

Following the existing per-concern test split:

- **Catalog integrity** — every device has at least one feed, every feed path starts `/api/feeds/`, every client key referenced by a feed exists. Catches a typo'd path that would otherwise surface as a user's 404.
- **Device resolution** against fabricated `platforms` payloads: slug match, `custom_name` match (the case `check_switch_platform` handles today, where a regression would be invisible), unknown platforms ignored, empty payload → permissive fallback.
- **Embed formatting** as a pure function, pinning the auth split explicitly: a pkgj embed contains `user:` credentials in its URL; a Tinfoil embed does not and carries separate username/password lines. This is the one rule a future edit is most likely to break silently.
- **Username encoding** — a username containing `@` produces a pkgj URL whose host is still the configured domain. This is a security assertion, not a formatting one.
- **Probe classification** as a pure function over status codes, transport mocked, plus one test asserting the probe's request carries **no `Authorization` header** — the bug that would make it always report "disabled" — and one asserting a placeholder `DOMAIN` yields `UNKNOWN` with no request issued.
- **Probe ROM-payload shapes** — both the `{"items": [...]}` dict and the bare list yield a usable ROM id.
- **Unknown and unavailable device keys** each produce their own response, since autocomplete does not constrain submitted values.
- **Unlinked-user path** produces a usable embed with the literal placeholder.
- **`cog_dependencies` coverage** — every `core_cogs` entry has a dependency-map key (closes the gap described in Wiring).

## Risks and implementation-time checks

- **Slug values are unverified.** The device-to-slug matching table (`switch`, `psvita`, `psp`, `ps`/`psx`, `ps3`, `ps4`, `ps5`, `nds`) must be checked against a live `platforms` payload during implementation. The `custom_name` fallback limits the blast radius of a wrong guess.
- **Kekatsu platform breadth.** RomM's route is `/api/feeds/kekatsu/{platform_slug}` and some sources suggest slugs beyond `nds` work. Spec `nds` only; widen if verified against RomM's source.
- **Probe and rate-limiting.** The probe is an unauthenticated request the bot makes against its own server once per refresh. On an instance fronted by fail2ban, repeated 401s from the bot's IP are conceivable. One request per `SYNC_RATE` should sit under any sane threshold, but this is a behavior change and warrants a README line.
- **`UNKNOWN` on a fresh server.** An empty library yields `UNKNOWN`, which is the expected common case for a new instance and degrades to the static note.

## Documentation

There is no `/switch_shop_info` row in the README command table today — the command appears only as a Features bullet at [README.md:51](../../../README.md#L51). So:

- Rewrite that bullet from "Switch Shop info" to the five-client feed support.
- **Add** a `/feeds [device]` row to the command table ([README.md:171-183](../../../README.md#L171-L183)), which the old command never had.
- Note the startup probe, including that it issues one unauthenticated request against the server per refresh.
- Name the replacement explicitly for anyone who had `/switch_shop_info` memorized — it vanishes from Discord's command list on the next sync, with no alias behind it.

## Future consumers of the probe

The download-auth prerequisite is not unique to feeds. `/search`'s QR code feature has the same requirement and no detection — [README.md:49](../../../README.md#L49) states it as an assumption, and `handle_qr_trigger` ([cogs/search.py:768](../../../cogs/search.py#L768)) simply assumes it holds. This spec deliberately keeps the probe private to `cogs/feeds/` to avoid speculative generality, but it is the obvious candidate for promotion to a bot-level cached fact if a second consumer appears. Noted here so it is findable rather than buried.
