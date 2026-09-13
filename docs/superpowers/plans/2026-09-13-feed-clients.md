# Feed Clients Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded `/switch_shop_info` command with a `/feeds device:<hardware>` command covering all five RomM feed clients (Tinfoil, pkgj, pkgi, fpkgi, Kekatsu) across eight devices.

**Architecture:** A `cogs/feeds/` package split by responsibility: a pure-data catalog of clients and devices, a pure resolver turning RomM's platform list into available devices, an injectable probe that detects whether the server's download endpoint requires auth, pure embed builders, and a thin cog that wires them to py-cord. Everything except `cog.py` is testable without a bot or a network.

**Tech Stack:** Python 3.12, py-cord, aiohttp, pytest running `unittest.TestCase` classes, ruff (line wall 180, `max-complexity` 15, isort with `known-first-party = ["cogs", ...]`).

**Spec:** [docs/superpowers/specs/2026-09-13-feed-clients-design.md](../specs/2026-09-13-feed-clients-design.md)

## Global Constraints

- **Python 3.12** (`target-version = "py312"`), but **do not** modernize typing: `UP006`/`UP007`/`UP035`/`UP045` are deliberately ignored repo-wide because py-cord introspects slash-command annotations at registration. Use `Dict`, `Tuple`, `Optional` from `typing`, matching the surrounding cogs.
- **Line wall 180 characters**, target 110. **`max-complexity` 15 per function** — no new `# noqa: C901`.
- **isort is enforced** (`I` rules). First-party imports are `cogs`, `integrations`, `admin_checks`, `bot`, `database_manager`.
- **Tests are `unittest.TestCase` classes run by pytest** (`testpaths = ["tests"]`, `addopts = "-q"`). Test method names are full sentences describing the behavior, as in `tests/test_recent_rom_embed.py`. Module docstrings say *why* the test file exists.
- **No new environment variables.** The feature uses `config.DOMAIN` and `config.API_BASE_URL`, which already exist.
- **All `/feeds` responses are ephemeral.**
- **Credentials rule:** a feed URL carries `username:<password>@` **only** for `AuthStyle.URL_EMBEDDED` (pkgj alone). Every other client gets a bare URL plus separate username/password lines. The bot never has, stores, or asks for a password.

## Two spec corrections discovered while planning

Both were verified against the code; the plan below implements the corrected behavior.

1. **Platform `slug` is not available, so matching is by name only.** `bot.cache` is a property returning `self.romm.cache` ([bot.py:446](../../../bot.py#L446)) — the same cache `fetch_api_endpoint` reads. `update_api_data` overwrites the `'platforms'` key with its **sanitized** payload ([bot.py:863-873](../../../bot.py#L863-L873)), which keeps `id`, `name`, `custom_name`, `display_name`, `rom_count` and drops everything else, `slug` included. Getting slugs would need `bypass_cache=True` — a network round-trip per autocomplete keystroke, which is untenable. So `Device` carries name fragments and exact names, not slugs. This is what `check_switch_platform` already does, so it is a known-working approach rather than a new risk.
2. **`availability.py` and `probe.py` are separate files.** The spec put both server facts in `availability.py`. They share no code and refresh by different mechanisms (one reads an existing cache, the other owns an `on_ready` listener), so keeping them together would be a file with two responsibilities. Split, per the plan's file-structure guidance.

## File Structure

| File | Responsibility |
|---|---|
| `cogs/feeds/catalog.py` | **Create.** Pure data: `AuthStyle`, `FeedClient`, `Feed`, `Device`, the five clients, the eight devices. No I/O, no `discord` import. |
| `cogs/feeds/availability.py` | **Create.** Pure: sanitized platforms payload → set of available device keys. |
| `cogs/feeds/probe.py` | **Create.** Download-auth detection. Pure classifiers plus one injectable async entry point. |
| `cogs/feeds/embeds.py` | **Create.** Pure: `(device, title, username, verdict, domain)` → `discord.Embed`. |
| `cogs/feeds/cog.py` | **Create.** The `Feeds` cog: one slash command, its autocomplete, the `on_ready` probe listener. |
| `cogs/feeds/__init__.py` | **Create.** Re-exports plus `setup(bot)`, mirroring `cogs/requests/__init__.py`. |
| `bot.py:676`, `bot.py:688-697` | **Modify.** Add `cogs.feeds` to `core_cogs` and `['aiohttp']` to `cog_dependencies`. |
| `cogs/info.py:21-22, 142-174, 297-378` | **Modify.** Delete `switch_shop_info`, `check_switch_platform`, `cog_slash_command_check`, and the two `__init__` lines. |
| `README.md:51`, `README.md:171-183` | **Modify.** Rewrite the Features bullet; add a `/feeds` command-table row. |
| `tests/test_feed_catalog.py` | **Create.** Catalog integrity. |
| `tests/test_feed_availability.py` | **Create.** Device resolution. |
| `tests/test_feed_probe.py` | **Create.** Probe classification and request shape. |
| `tests/test_feed_embeds.py` | **Create.** Embed content, including the credentials rule. |
| `tests/test_extension_loading.py` | **Modify.** Add `cog_dependencies` coverage test. |

---

### Task 1: The catalog

**Files:**
- Create: `cogs/feeds/__init__.py` (empty placeholder for now; Task 5 fills it)
- Create: `cogs/feeds/catalog.py`
- Test: `tests/test_feed_catalog.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AuthStyle` (enum, members `FIELDS` / `URL_EMBEDDED`); `FeedClient(key, display_name, auth_style, file_formats, docs_url, setup_steps, caveats)`; `Feed(client, path, label, extra_content_types)`; `Device(key, display_name, exact_names, name_fragments, feeds)`; module constants `DEVICES: Tuple[Device, ...]` and `DEVICES_BY_KEY: Dict[str, Device]`.

- [ ] **Step 1: Create the package directory with an empty `__init__.py`**

```bash
mkdir -p cogs/feeds
printf '' > cogs/feeds/__init__.py
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_feed_catalog.py`:

```python
"""Integrity checks for the feed catalog.

The catalog is a hand-maintained table of URLs. A typo in a path does not
fail anywhere in the bot - it reaches the user as a 404 from their console,
which is the worst possible place to discover it. These tests are the only
thing standing between an edit and that outcome, so they check shape rather
than behavior: every path well-formed, every device reachable, every client
referenced by a feed actually declared.
"""

import unittest

from cogs.feeds.catalog import DEVICES, DEVICES_BY_KEY, AuthStyle, Device


class CatalogShapeTests(unittest.TestCase):
    def test_every_device_offers_at_least_one_feed(self):
        for device in DEVICES:
            with self.subTest(device=device.key):
                self.assertTrue(device.feeds, f"{device.key} has no feeds")

    def test_every_feed_path_is_a_romm_feed_route(self):
        for device in DEVICES:
            for feed in device.feeds:
                with self.subTest(device=device.key, path=feed.path):
                    self.assertTrue(feed.path.startswith("/api/feeds/"))
                    self.assertFalse(feed.path.endswith("/"))

    def test_device_keys_are_unique(self):
        keys = [device.key for device in DEVICES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_lookup_table_matches_the_device_tuple(self):
        self.assertEqual(set(DEVICES_BY_KEY), {device.key for device in DEVICES})
        for key, device in DEVICES_BY_KEY.items():
            self.assertEqual(device.key, key)

    def test_every_device_can_be_matched_by_something(self):
        # A device with neither exact names nor fragments could never be
        # resolved from a platform list, so it would be invisible forever.
        for device in DEVICES:
            with self.subTest(device=device.key):
                self.assertTrue(device.exact_names or device.name_fragments)

    def test_all_five_clients_are_represented(self):
        clients = {feed.client.key for device in DEVICES for feed in device.feeds}
        self.assertEqual(clients, {"tinfoil", "pkgj", "pkgi", "fpkgi", "kekatsu"})

    def test_only_pkgj_embeds_credentials_in_its_url(self):
        # The rule the whole credential design rests on. If a second client
        # ever acquires URL_EMBEDDED, that is a decision someone must make
        # deliberately, not a default that drifted.
        embedding = {
            feed.client.key
            for device in DEVICES
            for feed in device.feeds
            if feed.client.auth_style is AuthStyle.URL_EMBEDDED
        }
        self.assertEqual(embedding, {"pkgj"})

    def test_devices_are_frozen(self):
        with self.assertRaises(Exception):
            DEVICES[0].key = "mutated"


class ContentTypeTests(unittest.TestCase):
    def test_extra_content_types_only_hang_off_pkgi_feeds(self):
        for device in DEVICES:
            for feed in device.feeds:
                if feed.extra_content_types:
                    with self.subTest(device=device.key):
                        self.assertEqual(feed.client.key, "pkgi")

    def test_the_vita_pkgi_game_feed_lists_its_extra_types(self):
        vita = DEVICES_BY_KEY["psvita"]
        game_feed = next(
            feed for feed in vita.feeds
            if feed.client.key == "pkgi" and feed.path.endswith("/game")
        )
        self.assertIn("translation", game_feed.extra_content_types)
        self.assertIn("prototype", game_feed.extra_content_types)

    def test_ps3_pkgi_offers_fewer_extra_types_than_vita(self):
        # PS3 and PSP accept five content types; Vita accepts eleven.
        ps3 = DEVICES_BY_KEY["ps3"]
        vita = DEVICES_BY_KEY["psvita"]
        ps3_extra = {t for feed in ps3.feeds for t in feed.extra_content_types}
        vita_extra = {t for feed in vita.feeds for t in feed.extra_content_types}
        self.assertTrue(ps3_extra < vita_extra)


class DeviceCoverageTests(unittest.TestCase):
    def test_the_eight_expected_devices_are_present(self):
        self.assertEqual(
            set(DEVICES_BY_KEY),
            {"switch", "psvita", "psp", "psx", "ps3", "ps4", "ps5", "nds"},
        )

    def test_vita_and_psp_each_offer_both_pkgj_and_pkgi(self):
        for key in ("psvita", "psp"):
            with self.subTest(device=key):
                clients = {feed.client.key for feed in DEVICES_BY_KEY[key].feeds}
                self.assertEqual(clients, {"pkgj", "pkgi"})

    def test_psx_offers_pkgj_games_only(self):
        # RomM exposes no DLC feed for PSX.
        psx = DEVICES_BY_KEY["psx"]
        self.assertEqual(len(psx.feeds), 1)
        self.assertEqual(psx.feeds[0].path, "/api/feeds/pkgj/psx/games")

    def test_a_device_is_a_dataclass_instance(self):
        self.assertIsInstance(DEVICES_BY_KEY["switch"], Device)
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `python -m pytest tests/test_feed_catalog.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.feeds.catalog'`

- [ ] **Step 4: Write `cogs/feeds/catalog.py`**

```python
"""The feed clients RomM exposes, as data.

RomM serves five homebrew installers from /api/feeds/. The URL shape is
uniform across them; what differs is where each client will accept
credentials and what it does with a file it cannot install. Both facts live
here as data so embeds.py can render any device without knowing which client
it is looking at.

Devices are matched to RomM platforms by name, not slug: the platforms
payload the bot caches is sanitized down to id/name/custom_name/display_name/
rom_count, and slug is not among them.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Tuple


class AuthStyle(Enum):
    """Where a client will accept credentials."""

    # The client has username and password fields of its own, so the URL we
    # show stays bare and no secret appears in the Discord message.
    FIELDS = "fields"
    # The client takes a URL and nothing else (pkgj's config.txt), so the
    # credentials have to ride in the URL's userinfo component.
    URL_EMBEDDED = "url_embedded"


@dataclass(frozen=True)
class FeedClient:
    """One homebrew installer."""

    key: str
    display_name: str
    auth_style: AuthStyle
    file_formats: str
    docs_url: str
    setup_steps: Tuple[str, ...] = ()
    caveats: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Feed:
    """One URL a client can be pointed at.

    `extra_content_types` exists so pkgi's long tail (Vita accepts eleven
    content types) can be summarized as a template line instead of being
    enumerated as eleven Feed entries that would swamp the embed.
    """

    client: FeedClient
    path: str
    label: str
    extra_content_types: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Device:
    """A piece of hardware a user owns, and the feeds that serve it.

    `exact_names` is for names that would over-match as substrings -
    "PlayStation" is a prefix of "PlayStation 3" - while `name_fragments`
    are safe to look for anywhere in a platform's name.
    """

    key: str
    display_name: str
    exact_names: Tuple[str, ...]
    name_fragments: Tuple[str, ...]
    feeds: Tuple[Feed, ...] = field(default_factory=tuple)


DOCS = "https://docs.romm.app/latest"

TINFOIL = FeedClient(
    key="tinfoil",
    display_name="Tinfoil",
    auth_style=AuthStyle.FIELDS,
    file_formats="`.nsp` and `.xci`",
    docs_url=f"{DOCS}/Integrations/Tinfoil-integration/",
    setup_steps=(
        "Open Tinfoil and go to **File Browser**.",
        "Scroll to the empty slot and press `-` to add a new file-server.",
        "Enter the settings below, then press `X` to save.",
        "Close and reopen Tinfoil so it rescans TitleIDs.",
    ),
    caveats=(
        "Filenames must carry the Switch title ID in brackets, e.g. "
        "`Game Title [0100000000010000].nsp`.",
    ),
)

PKGJ = FeedClient(
    key="pkgj",
    display_name="pkgj",
    auth_style=AuthStyle.URL_EMBEDDED,
    file_formats="`.pkg`, plus compressed archives (`.zip`, `.7z`)",
    docs_url=f"{DOCS}/ecosystem/feed-clients/",
    setup_steps=(
        "Open `ux0:/pkgj/config.txt` in VitaShell.",
        "Append the URL below as a new line.",
        "Save, then refresh inside pkgj.",
    ),
    caveats=(
        "pkgj takes a URL and nothing else, so your password has to go in "
        "the URL itself. Replace `<your-password>` below.",
    ),
)

PKGI = FeedClient(
    key="pkgi",
    display_name="pkgi",
    auth_style=AuthStyle.FIELDS,
    file_formats="`.pkg` only",
    docs_url=f"{DOCS}/ecosystem/feed-clients/",
    setup_steps=(
        "Add the URL below as a feed in pkgi's configuration.",
        "Enter your username and password in pkgi's own auth fields.",
    ),
    caveats=(
        "PS3 and PSP installs also want the matching `.rap` license file "
        "stored alongside the `.pkg`.",
    ),
)

FPKGI = FeedClient(
    key="fpkgi",
    display_name="fpkgi",
    auth_style=AuthStyle.FIELDS,
    file_formats="`.pkg` only",
    docs_url=f"{DOCS}/ecosystem/feed-clients/",
    setup_steps=(
        "Add the URL below to fpkgi's config.",
        "Enter your username and password in fpkgi's auth fields.",
    ),
    caveats=(
        "The feed lists every ROM on the platform, but only `.pkg` files "
        "will install - other entries will appear and then fail.",
    ),
)

KEKATSU = FeedClient(
    key="kekatsu",
    display_name="Kekatsu DS",
    auth_style=AuthStyle.FIELDS,
    file_formats="`.nds` only",
    docs_url=f"{DOCS}/ecosystem/feed-clients/",
    setup_steps=(
        "Add the URL below as a source in Kekatsu.",
        "Enter your username and password in Kekatsu's auth fields.",
    ),
    caveats=(
        "The feed lists every ROM on the platform, but only `.nds` files "
        "will install.",
        "The DS's wifi supports WEP and one older WPA variant only. A modern "
        "network needs a dedicated SSID or a travel router.",
    ),
)

# PS3 and PSP accept five pkgi content types; Vita accepts eleven. `game` and
# `dlc` get their own Feed; the rest ride along as a summarized template line.
_PKGI_EXTRA_BASE = ("demo", "update", "patch")
_PKGI_EXTRA_VITA = _PKGI_EXTRA_BASE + (
    "hack", "manual", "mod", "translation", "prototype", "cheat",
)

SWITCH = Device(
    key="switch",
    display_name="Nintendo Switch",
    exact_names=(),
    name_fragments=("switch",),
    feeds=(Feed(TINFOIL, "/api/feeds/tinfoil", "Feed"),),
)

PSVITA = Device(
    key="psvita",
    display_name="PlayStation Vita",
    exact_names=(),
    name_fragments=("vita",),
    feeds=(
        Feed(PKGJ, "/api/feeds/pkgj/psvita/games", "Games"),
        Feed(PKGJ, "/api/feeds/pkgj/psvita/dlc", "DLC"),
        Feed(PKGI, "/api/feeds/pkgi/psvita/game", "Games", _PKGI_EXTRA_VITA),
        Feed(PKGI, "/api/feeds/pkgi/psvita/dlc", "DLC"),
    ),
)

PSP = Device(
    key="psp",
    display_name="PlayStation Portable",
    exact_names=(),
    name_fragments=("psp", "playstation portable"),
    feeds=(
        Feed(PKGJ, "/api/feeds/pkgj/psp/games", "Games"),
        Feed(PKGJ, "/api/feeds/pkgj/psp/dlc", "DLC"),
        Feed(PKGI, "/api/feeds/pkgi/psp/game", "Games", _PKGI_EXTRA_BASE),
        Feed(PKGI, "/api/feeds/pkgi/psp/dlc", "DLC"),
    ),
)

PSX = Device(
    key="psx",
    display_name="PlayStation",
    # "playstation" as a fragment would match every other Sony platform, so
    # the bare name is matched exactly and only the unambiguous short forms
    # are treated as fragments.
    exact_names=("playstation", "sony playstation"),
    name_fragments=("psx", "ps1", "psone"),
    feeds=(Feed(PKGJ, "/api/feeds/pkgj/psx/games", "Games"),),
)

PS3 = Device(
    key="ps3",
    display_name="PlayStation 3",
    exact_names=(),
    name_fragments=("ps3", "playstation 3"),
    feeds=(
        Feed(PKGI, "/api/feeds/pkgi/ps3/game", "Games", _PKGI_EXTRA_BASE),
        Feed(PKGI, "/api/feeds/pkgi/ps3/dlc", "DLC"),
    ),
)

PS4 = Device(
    key="ps4",
    display_name="PlayStation 4",
    exact_names=(),
    name_fragments=("ps4", "playstation 4"),
    feeds=(Feed(FPKGI, "/api/feeds/fpkgi/ps4", "Feed"),),
)

PS5 = Device(
    key="ps5",
    display_name="PlayStation 5",
    exact_names=(),
    name_fragments=("ps5", "playstation 5"),
    feeds=(Feed(FPKGI, "/api/feeds/fpkgi/ps5", "Feed"),),
)

NDS = Device(
    key="nds",
    display_name="Nintendo DS",
    exact_names=(),
    # "nds" and "nintendo ds" both miss "Nintendo 3DS", which is what we want.
    name_fragments=("nintendo ds", "nds"),
    feeds=(Feed(KEKATSU, "/api/feeds/kekatsu/nds", "Feed"),),
)

DEVICES: Tuple[Device, ...] = (SWITCH, PSVITA, PSP, PSX, PS3, PS4, PS5, NDS)

DEVICES_BY_KEY: Dict[str, Device] = {device.key: device for device in DEVICES}
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_feed_catalog.py -v`
Expected: PASS (15 tests across three classes).

- [ ] **Step 6: Lint**

Run: `python -m ruff check cogs/feeds/ tests/test_feed_catalog.py`
Expected: `All checks passed!`

- [ ] **Step 7: Commit**

```bash
git add cogs/feeds/__init__.py cogs/feeds/catalog.py tests/test_feed_catalog.py
git commit -m "feat(feeds): the five RomM feed clients and eight devices, as data"
```

---

### Task 2: Device availability

**Files:**
- Create: `cogs/feeds/availability.py`
- Test: `tests/test_feed_availability.py`

**Interfaces:**
- Consumes: `DEVICES`, `Device` from `cogs.feeds.catalog`.
- Produces: `available_device_keys(platforms) -> FrozenSet[str]` and `available_devices(platforms) -> Tuple[Device, ...]`. `platforms` is the sanitized list from `bot.cache.get('platforms')`, or `None`. A `None`/empty payload returns **every** device key (permissive fallback).

- [ ] **Step 1: Write the failing test**

Create `tests/test_feed_availability.py`:

```python
"""Resolving RomM's platform list into devices the /feeds command can offer.

The payload these read is the sanitized one bot.py caches, which keeps
name and custom_name and drops slug - so matching is by name, exactly as
the Switch check this replaces did. The custom_name case is the one most
likely to regress invisibly: a server whose admin renamed "Nintendo Switch"
to "Switch (Modded)" must still be offered Tinfoil.
"""

import unittest

from cogs.feeds.availability import available_device_keys, available_devices


def platform(name, custom_name=None):
    """One entry shaped like bot.sanitize_data's platforms branch."""
    return {
        "id": 1,
        "name": name,
        "custom_name": custom_name,
        "display_name": custom_name or name,
        "rom_count": 10,
    }


class MatchingTests(unittest.TestCase):
    def test_a_platform_named_for_the_device_matches_it(self):
        self.assertEqual(
            available_device_keys([platform("Nintendo Switch")]),
            frozenset({"switch"}),
        )

    def test_a_custom_name_matches_when_the_real_name_does_not(self):
        keys = available_device_keys([platform("Some Internal Slug", "Switch (Modded)")])
        self.assertEqual(keys, frozenset({"switch"}))

    def test_matching_ignores_case_and_surrounding_whitespace(self):
        self.assertEqual(
            available_device_keys([platform("  nintendo SWITCH  ")]),
            frozenset({"switch"}),
        )

    def test_unrelated_platforms_match_nothing(self):
        keys = available_device_keys([platform("Nintendo 64"), platform("Sega Genesis")])
        self.assertEqual(keys, frozenset())

    def test_several_platforms_resolve_to_several_devices(self):
        keys = available_device_keys([
            platform("Nintendo Switch"),
            platform("PlayStation Vita"),
            platform("Nintendo DS"),
        ])
        self.assertEqual(keys, frozenset({"switch", "psvita", "nds"}))


class OverMatchingTests(unittest.TestCase):
    """The Sony family and the DS family are where substring matching bites."""

    def test_playstation_3_is_not_also_the_original_playstation(self):
        self.assertEqual(available_device_keys([platform("PlayStation 3")]), frozenset({"ps3"}))

    def test_the_original_playstation_matches_only_psx(self):
        self.assertEqual(available_device_keys([platform("PlayStation")]), frozenset({"psx"}))

    def test_playstation_portable_is_psp_and_not_psx(self):
        self.assertEqual(
            available_device_keys([platform("PlayStation Portable")]),
            frozenset({"psp"}),
        )

    def test_nintendo_3ds_is_not_the_ds(self):
        self.assertEqual(available_device_keys([platform("Nintendo 3DS")]), frozenset())

    def test_ps5_and_ps4_stay_distinct(self):
        keys = available_device_keys([platform("PlayStation 4"), platform("PlayStation 5")])
        self.assertEqual(keys, frozenset({"ps4", "ps5"}))


class FallbackTests(unittest.TestCase):
    def test_a_missing_cache_offers_everything(self):
        # A stale-but-permissive picker beats a command that silently vanishes.
        self.assertEqual(len(available_device_keys(None)), 8)

    def test_an_empty_list_offers_everything(self):
        self.assertEqual(len(available_device_keys([])), 8)

    def test_a_malformed_entry_is_skipped_rather_than_raising(self):
        keys = available_device_keys([{"nonsense": True}, platform("Nintendo Switch")])
        self.assertEqual(keys, frozenset({"switch"}))

    def test_a_null_name_is_survivable(self):
        # sanitize_data defaults name, but custom_name is passed through as
        # None, and the Switch check this replaces guarded for exactly this.
        keys = available_device_keys([{"name": None, "custom_name": None}])
        self.assertEqual(keys, frozenset())


class OrderingTests(unittest.TestCase):
    def test_available_devices_returns_catalog_order_not_payload_order(self):
        devices = available_devices([platform("Nintendo DS"), platform("Nintendo Switch")])
        self.assertEqual([d.key for d in devices], ["switch", "nds"])

    def test_available_devices_returns_device_objects(self):
        devices = available_devices([platform("Nintendo Switch")])
        self.assertEqual(devices[0].display_name, "Nintendo Switch")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_feed_availability.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.feeds.availability'`

- [ ] **Step 3: Write `cogs/feeds/availability.py`**

```python
"""Which devices this RomM server actually hosts.

Reads the platforms payload bot.py already caches every SYNC_RATE rather
than fetching or scheduling anything of its own: bot.cache is the API
client's cache, and update_api_data refreshes the 'platforms' key on that
same loop. That payload is sanitized down to id/name/custom_name/
display_name/rom_count, so matching is by name - there is no slug to match
on - and platforms holding no ROMs have already been filtered out upstream,
which is the behavior we want for gating anyway.
"""

import logging
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .catalog import DEVICES, Device

logger = logging.getLogger(__name__)


def _labels(entry: Dict[str, Any]) -> List[str]:
    """The names one platform entry can be recognized by, normalized."""
    raw = (entry.get("name"), entry.get("custom_name"))
    return [str(value).strip().lower() for value in raw if value]


def _matches(device: Device, label: str) -> bool:
    if label in device.exact_names:
        return True
    return any(fragment in label for fragment in device.name_fragments)


def available_device_keys(platforms: Optional[List[Dict[str, Any]]]) -> FrozenSet[str]:
    """Device keys this server can serve feeds for.

    An absent or empty payload means we could not find out, which resolves
    to every device: a picker that offers too much is recoverable, one that
    offers nothing looks like the command is broken.
    """
    if not platforms:
        logger.debug("No cached platforms; offering every feed device")
        return frozenset(device.key for device in DEVICES)

    found = set()
    for entry in platforms:
        if not isinstance(entry, dict):
            continue
        for label in _labels(entry):
            for device in DEVICES:
                if _matches(device, label):
                    found.add(device.key)
    return frozenset(found)


def available_devices(platforms: Optional[List[Dict[str, Any]]]) -> Tuple[Device, ...]:
    """As above, but the Device objects, in catalog order."""
    keys = available_device_keys(platforms)
    return tuple(device for device in DEVICES if device.key in keys)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_feed_availability.py -v`
Expected: PASS, 16 tests.

If `test_the_original_playstation_matches_only_psx` fails, the cause is a fragment on another Sony device matching the bare name — check that `PS3`/`PS4`/`PS5` fragments all include the digit.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check cogs/feeds/ tests/test_feed_availability.py
git add cogs/feeds/availability.py tests/test_feed_availability.py
git commit -m "feat(feeds): resolve hosted platforms into offerable devices"
```

---

### Task 3: The download-auth probe

**Files:**
- Create: `cogs/feeds/probe.py`
- Test: `tests/test_feed_probe.py`

**Interfaces:**
- Consumes: `build_rom_download_url` from `cogs.search`.
- Produces: `DownloadAuth` (enum: `ENABLED`, `DISABLED`, `UNKNOWN`); `first_rom(payload) -> Optional[Dict]`; `classify(status) -> DownloadAuth`; `is_probeable(domain) -> bool`; `async detect_download_auth(domain, fetch_api_endpoint, http_get=None) -> DownloadAuth`.
- `http_get` is an async callable `(url: str) -> int` returning an HTTP status. It is injected so tests exercise the real control flow with no mocking library; production passes `None` and gets `_aiohttp_get`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_feed_probe.py`:

```python
"""Detecting whether this RomM server's download endpoint requires auth.

The single most common way a correct feed setup still fails: the feed itself
authenticates fine, then every download 403s because
DISABLE_DOWNLOAD_ENDPOINT_AUTH is not set. The probe answers that before the
user wires anything up.

The probe must be credential-free - a probe that carries the bot's own token
always sees 200 and would cheerfully report "no auth required" on a locked
server, which is the exact wrong answer. That property has its own test.
"""

import unittest

from cogs.feeds.probe import (
    DownloadAuth,
    classify,
    detect_download_auth,
    first_rom,
    is_probeable,
)


class ClassifyTests(unittest.TestCase):
    def test_unauthorized_means_download_auth_is_on(self):
        self.assertIs(classify(401), DownloadAuth.ENABLED)

    def test_forbidden_means_download_auth_is_on(self):
        self.assertIs(classify(403), DownloadAuth.ENABLED)

    def test_ok_means_download_auth_is_off(self):
        self.assertIs(classify(200), DownloadAuth.DISABLED)

    def test_partial_content_means_download_auth_is_off(self):
        # The probe sends Range: bytes=0-0, so 206 is the expected success.
        self.assertIs(classify(206), DownloadAuth.DISABLED)

    def test_a_server_error_tells_us_nothing(self):
        self.assertIs(classify(500), DownloadAuth.UNKNOWN)

    def test_a_not_found_tells_us_nothing(self):
        # The ROM we picked may have been deleted between listing and probing.
        self.assertIs(classify(404), DownloadAuth.UNKNOWN)

    def test_no_status_at_all_tells_us_nothing(self):
        self.assertIs(classify(None), DownloadAuth.UNKNOWN)


class RomPayloadTests(unittest.TestCase):
    """RomM returns two shapes for this endpoint depending on version."""

    def test_a_paged_payload_yields_its_first_item(self):
        payload = {"items": [{"id": 7, "fs_name": "a.nsp"}], "total": 1}
        self.assertEqual(first_rom(payload)["id"], 7)

    def test_a_bare_list_yields_its_first_entry(self):
        self.assertEqual(first_rom([{"id": 9, "fs_name": "b.nsp"}])["id"], 9)

    def test_an_empty_library_yields_nothing(self):
        self.assertIsNone(first_rom([]))
        self.assertIsNone(first_rom({"items": []}))
        self.assertIsNone(first_rom(None))

    def test_a_shape_we_do_not_recognize_yields_nothing(self):
        self.assertIsNone(first_rom({"unexpected": "shape"}))


class ProbeableDomainTests(unittest.TestCase):
    def test_a_real_domain_is_probeable(self):
        self.assertTrue(is_probeable("https://romm.example"))

    def test_the_unset_domain_placeholder_is_not(self):
        # bot.py defaults DOMAIN to this literal string.
        self.assertFalse(is_probeable("No website configured"))

    def test_an_empty_domain_is_not(self):
        self.assertFalse(is_probeable(""))
        self.assertFalse(is_probeable(None))


class DetectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requested = []

    def recorder(self, status):
        async def http_get(url):
            self.requested.append(url)
            return status
        return http_get

    async def fetch_one_rom(self, endpoint, **kwargs):
        return {"items": [{"id": 42, "fs_name": "Some Game.nsp"}]}

    async def fetch_nothing(self, endpoint, **kwargs):
        return []

    async def test_a_401_from_the_content_url_reports_auth_enabled(self):
        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, self.recorder(401)
        )
        self.assertIs(verdict, DownloadAuth.ENABLED)

    async def test_a_206_reports_auth_disabled(self):
        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, self.recorder(206)
        )
        self.assertIs(verdict, DownloadAuth.DISABLED)

    async def test_the_probed_url_is_the_rom_content_endpoint(self):
        await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, self.recorder(200)
        )
        self.assertEqual(
            self.requested,
            ["https://romm.example/api/roms/42/content/Some+Game.nsp"],
        )

    async def test_an_empty_library_is_unknown_and_issues_no_request(self):
        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_nothing, self.recorder(200)
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)
        self.assertEqual(self.requested, [])

    async def test_a_placeholder_domain_is_unknown_and_issues_no_request(self):
        verdict = await detect_download_auth(
            "No website configured", self.fetch_one_rom, self.recorder(200)
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)
        self.assertEqual(self.requested, [])

    async def test_a_transport_failure_is_unknown_rather_than_a_crash(self):
        async def explode(url):
            raise OSError("connection refused")

        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, explode
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)

    async def test_a_failure_listing_roms_is_unknown_rather_than_a_crash(self):
        async def explode(endpoint, **kwargs):
            raise OSError("connection refused")

        verdict = await detect_download_auth(
            "https://romm.example", explode, self.recorder(200)
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)


class CredentialFreedomTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_probe_sends_no_authorization_header(self):
        """The property the whole probe depends on.

        A probe carrying the bot's credentials sees 200 on a locked server
        and reports "no auth needed" - telling every user their downloads
        will work, right before none of them do.
        """
        seen = {}

        async def http_get(url, headers=None):
            seen["headers"] = headers or {}
            return 206

        async def fetch(endpoint, **kwargs):
            return [{"id": 1, "fs_name": "x.nsp"}]

        await detect_download_auth("https://romm.example", fetch, http_get)
        sent = {key.lower() for key in seen["headers"]}
        self.assertNotIn("authorization", sent)
        self.assertNotIn("cookie", sent)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_feed_probe.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.feeds.probe'`

Note `CredentialFreedomTests` passes an `http_get` taking `(url, headers=None)` while `DetectTests` passes one taking `(url)`. The implementation must call `http_get(url)` positionally with no second argument, so both signatures work — that is deliberate, and it is what proves the default header set is empty rather than merely unasserted.

- [ ] **Step 3: Write `cogs/feeds/probe.py`**

```python
"""Does this server's download endpoint require authentication?

DISABLE_DOWNLOAD_ENDPOINT_AUTH=true opens GET /api/roms/{id}/content/... and
nothing else - the /api/feeds/ routes still want basic auth either way. The
two are constantly conflated, and the consequence of getting it wrong is a
user who sets everything up correctly and then watches every download fail.

The probe is deliberately not routed through bot.fetch_api_endpoint. That
method attaches the bot's own credentials, which would return 200 on a
locked server and produce exactly the wrong answer - and it cannot take a
URL anyway, only an endpoint name relative to API_BASE_URL.
"""

import logging
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import aiohttp

from cogs.search import build_rom_download_url

logger = logging.getLogger(__name__)

# bot.py's default when DOMAIN is unset. Probing it would be meaningless.
UNSET_DOMAIN = "No website configured"

PROBE_TIMEOUT_SECONDS = 10


class DownloadAuth(Enum):
    """What we found out about the download endpoint."""

    ENABLED = "enabled"    # Downloads will 403 for feed clients.
    DISABLED = "disabled"  # Downloads are open; feed clients will work.
    UNKNOWN = "unknown"    # We could not tell; fall back to a generic note.


def is_probeable(domain: Optional[str]) -> bool:
    """Whether `domain` is a real address worth sending a request to."""
    return bool(domain) and domain.strip() != UNSET_DOMAIN


def first_rom(payload: Union[Dict[str, Any], List[Any], None]) -> Optional[Dict[str, Any]]:
    """One ROM out of either shape RomM returns for the roms endpoint.

    Older versions answer with a bare list, newer ones with a paged dict.
    Handling only one of them would report UNKNOWN on half the versions in
    the wild.
    """
    if isinstance(payload, dict):
        payload = payload.get("items")
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    return first if isinstance(first, dict) else None


def classify(status: Optional[int]) -> DownloadAuth:
    """Turn the probe's HTTP status into a verdict."""
    if status in (401, 403):
        return DownloadAuth.ENABLED
    if status in (200, 206):
        return DownloadAuth.DISABLED
    return DownloadAuth.UNKNOWN


async def _aiohttp_get(url: str) -> Optional[int]:
    """One byte, no credentials, no shared session.

    A fresh session is the point: the bot's own client carries tokens and
    cookies, and reusing it would defeat the probe.
    """
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"Range": "bytes=0-0"}) as response:
            return response.status


async def detect_download_auth(
    domain: str,
    fetch_api_endpoint: Callable[..., Awaitable[Any]],
    http_get: Optional[Callable[[str], Awaitable[Optional[int]]]] = None,
) -> DownloadAuth:
    """Probe `domain` for whether ROM downloads need credentials.

    `domain` is config.DOMAIN rather than API_BASE_URL on purpose: DOMAIN is
    the address the user's console will reach, reverse proxy included, and
    predicting what happens to the user is the entire point. API_BASE_URL is
    how the bot reaches the server, which is frequently a different address
    and would answer a question nobody asked.
    """
    if not is_probeable(domain):
        logger.debug("DOMAIN is unset; skipping the download-auth probe")
        return DownloadAuth.UNKNOWN

    get = http_get or _aiohttp_get

    try:
        payload = await fetch_api_endpoint("roms?limit=1")
    except Exception as e:
        logger.debug(f"Could not list ROMs for the download-auth probe: {e}")
        return DownloadAuth.UNKNOWN

    rom = first_rom(payload)
    if not rom or rom.get("id") is None:
        logger.debug("No ROMs available to probe with")
        return DownloadAuth.UNKNOWN

    url = build_rom_download_url(domain, rom["id"], rom.get("fs_name", "unknown_file"))

    try:
        status = await get(url)
    except Exception as e:
        logger.debug(f"Download-auth probe request failed: {e}")
        return DownloadAuth.UNKNOWN

    verdict = classify(status)
    logger.info(f"Download-auth probe: HTTP {status} -> {verdict.value}")
    return verdict
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_feed_probe.py -v`
Expected: PASS.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check cogs/feeds/ tests/test_feed_probe.py
git add cogs/feeds/probe.py tests/test_feed_probe.py
git commit -m "feat(feeds): probe whether the download endpoint requires auth"
```

---

### Task 4: The embeds

**Files:**
- Create: `cogs/feeds/embeds.py`
- Test: `tests/test_feed_embeds.py`

**Interfaces:**
- Consumes: `AuthStyle`, `Device`, `Feed` from `cogs.feeds.catalog`; `DownloadAuth` from `cogs.feeds.probe`.
- Produces: `USERNAME_PLACEHOLDER`, `PASSWORD_PLACEHOLDER`; `feed_url(domain, feed, username) -> str`; `download_auth_notice(verdict) -> Optional[str]`; `build_device_embed(device, domain, username, verdict, title=None) -> discord.Embed`.
- `username` is `None` for a user with no RomM link; `title` overrides the embed title so `cog.py` can pass an emoji-decorated name.

- [ ] **Step 1: Write the failing test**

Create `tests/test_feed_embeds.py`:

```python
"""The /feeds embed, as a pure function.

The rule worth protecting here is the credential split: pkgj's config.txt
takes a URL and nothing else, so its URL carries user:<password>@, while
every other client has auth fields of its own and gets a bare URL. Nothing
about that is enforced by the type system, and getting it backwards would
either break pkgj or print credentials where they did not need to be.
"""

import unittest

from cogs.feeds.catalog import DEVICES_BY_KEY
from cogs.feeds.embeds import (
    PASSWORD_PLACEHOLDER,
    USERNAME_PLACEHOLDER,
    build_device_embed,
    download_auth_notice,
    feed_url,
)
from cogs.feeds.probe import DownloadAuth

DOMAIN = "https://romm.example"


def embed_text(embed):
    """Everything a reader would see, flattened."""
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(field.value or "")
    if embed.footer:
        parts.append(embed.footer.text or "")
    return "\n".join(parts)


class UrlTests(unittest.TestCase):
    def test_a_fields_client_gets_a_bare_url(self):
        tinfoil = DEVICES_BY_KEY["switch"].feeds[0]
        self.assertEqual(feed_url(DOMAIN, tinfoil, "ana"), f"{DOMAIN}/api/feeds/tinfoil")

    def test_pkgj_carries_the_username_and_a_password_placeholder(self):
        pkgj = DEVICES_BY_KEY["psvita"].feeds[0]
        url = feed_url(DOMAIN, pkgj, "ana")
        self.assertEqual(
            url,
            f"https://ana:{PASSWORD_PLACEHOLDER}@romm.example/api/feeds/pkgj/psvita/games",
        )

    def test_pkgj_without_a_link_still_produces_a_usable_template(self):
        pkgj = DEVICES_BY_KEY["psvita"].feeds[0]
        url = feed_url(DOMAIN, pkgj, None)
        self.assertIn(USERNAME_PLACEHOLDER, url)
        self.assertIn(PASSWORD_PLACEHOLDER, url)

    def test_a_trailing_slash_on_the_domain_does_not_double_up(self):
        tinfoil = DEVICES_BY_KEY["switch"].feeds[0]
        self.assertEqual(
            feed_url("https://romm.example/", tinfoil, "ana"),
            f"{DOMAIN}/api/feeds/tinfoil",
        )

    def test_a_username_with_an_at_sign_cannot_repoint_the_host(self):
        """The one place user-controlled text lands in a URL's userinfo.

        Unencoded, "ana@evil.test" would make the host evil.test and send
        the user's password somewhere else entirely.
        """
        pkgj = DEVICES_BY_KEY["psvita"].feeds[0]
        url = feed_url(DOMAIN, pkgj, "ana@evil.test")
        self.assertNotIn("@evil.test/", url)
        self.assertIn("ana%40evil.test", url)
        self.assertIn("@romm.example/", url)

    def test_other_reserved_characters_in_a_username_are_encoded(self):
        pkgj = DEVICES_BY_KEY["psvita"].feeds[0]
        url = feed_url(DOMAIN, pkgj, "a/b:c?d#e")
        self.assertIn("a%2Fb%3Ac%3Fd%23e", url)

    def test_an_http_domain_is_left_as_http(self):
        # Some self-hosters run plain HTTP on a LAN; rewriting it would
        # silently produce a URL that does not resolve.
        pkgj = DEVICES_BY_KEY["psvita"].feeds[0]
        url = feed_url("http://192.168.1.5:8080", pkgj, "ana")
        self.assertTrue(url.startswith("http://ana:"))


class NoticeTests(unittest.TestCase):
    def test_auth_enabled_produces_a_warning(self):
        notice = download_auth_notice(DownloadAuth.ENABLED)
        self.assertIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", notice)
        self.assertIn("⚠️", notice)

    def test_auth_disabled_produces_no_notice_at_all(self):
        self.assertIsNone(download_auth_notice(DownloadAuth.DISABLED))

    def test_unknown_falls_back_to_a_generic_advisory(self):
        notice = download_auth_notice(DownloadAuth.UNKNOWN)
        self.assertIsNotNone(notice)
        self.assertIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", notice)
        self.assertNotIn("⚠️", notice)


class EmbedTests(unittest.TestCase):
    def test_a_single_client_device_gets_one_client_field(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        names = [f.name for f in embed.fields]
        self.assertEqual(sum("Tinfoil" in n for n in names), 1)

    def test_vita_shows_both_pkgj_and_pkgi(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        text = embed_text(embed)
        self.assertIn("pkgj", text)
        self.assertIn("pkgi", text)

    def test_a_fields_client_shows_the_username_as_a_separate_line(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        text = embed_text(embed)
        self.assertIn("ana", text)
        self.assertIn("your RomM password", text)

    def test_a_fields_client_never_prints_a_credentialed_url(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        self.assertNotIn("ana:", embed_text(embed))

    def test_an_unlinked_user_gets_a_placeholder_and_a_nudge(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, None, DownloadAuth.DISABLED
        )
        text = embed_text(embed)
        self.assertIn(USERNAME_PLACEHOLDER, text)

    def test_the_warning_appears_when_download_auth_is_on(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.ENABLED
        )
        self.assertIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", embed_text(embed))

    def test_no_warning_clutter_when_download_auth_is_off(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        self.assertNotIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", embed_text(embed))

    def test_file_format_limits_are_stated(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["nds"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        self.assertIn(".nds", embed_text(embed))

    def test_client_caveats_reach_the_reader(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["nds"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        self.assertIn("WEP", embed_text(embed))

    def test_pkgi_extra_content_types_are_summarized_not_enumerated(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED
        )
        text = embed_text(embed)
        self.assertIn("translation", text)
        # Summarized as a template line, not eleven separate URLs.
        self.assertLessEqual(text.count("/api/feeds/pkgi/psvita/"), 3)

    def test_a_supplied_title_overrides_the_device_name(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED,
            title="Nintendo Switch <:switch:1>",
        )
        self.assertIn("<:switch:1>", embed.title)

    def test_every_device_builds_without_raising(self):
        for key, device in DEVICES_BY_KEY.items():
            for verdict in DownloadAuth:
                for username in ("ana", None):
                    with self.subTest(device=key, verdict=verdict, user=username):
                        embed = build_device_embed(device, DOMAIN, username, verdict)
                        self.assertTrue(embed.fields)

    def test_no_field_exceeds_discord_s_value_limit(self):
        for key, device in DEVICES_BY_KEY.items():
            embed = build_device_embed(device, DOMAIN, "ana", DownloadAuth.ENABLED)
            for field in embed.fields:
                with self.subTest(device=key, field=field.name):
                    self.assertLessEqual(len(field.value), 1024)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_feed_embeds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.feeds.embeds'`

- [ ] **Step 3: Write `cogs/feeds/embeds.py`**

```python
"""Rendering one device's feeds as a Discord embed.

Pure: everything bot-dependent (the emoji-decorated title, the user's RomM
username, the probe verdict) arrives as an argument, so the whole embed can
be asserted against in a test with no bot and no network.
"""

from typing import List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import discord

from .catalog import AuthStyle, Device, Feed
from .probe import DownloadAuth

USERNAME_PLACEHOLDER = "<your-romm-username>"
PASSWORD_PLACEHOLDER = "<your-password>"

AUTH_WARNING = (
    "⚠️ This server currently requires authentication to download files, so "
    "feed clients will list games and then fail to install them. An admin "
    "needs to set `DISABLE_DOWNLOAD_ENDPOINT_AUTH=true` on the RomM instance."
)

AUTH_ADVISORY = (
    "If downloads fail after the feed itself loads, the RomM instance needs "
    "`DISABLE_DOWNLOAD_ENDPOINT_AUTH=true` set by an admin."
)

LINK_NUDGE = (
    "Run `/user_manager` or ask an admin to link your Discord and RomM "
    "accounts, and this command will fill your username in for you."
)


def feed_url(domain: str, feed: Feed, username: Optional[str]) -> str:
    """The URL to show for one feed.

    Credentials go in the URL only for clients that have nowhere else to put
    them - pkgj, whose config.txt is a list of bare URLs. The username is
    percent-encoded because it is free text out of the database, and an
    unencoded "@" in it would silently repoint the URL at another host.
    """
    base = domain.rstrip("/")
    if feed.client.auth_style is not AuthStyle.URL_EMBEDDED:
        return f"{base}{feed.path}"

    name = username or USERNAME_PLACEHOLDER
    userinfo = f"{quote(name, safe='')}:{PASSWORD_PLACEHOLDER}"
    scheme, netloc, path, query, fragment = urlsplit(base)
    return urlunsplit((scheme, f"{userinfo}@{netloc}", path + feed.path, query, fragment))


def download_auth_notice(verdict: DownloadAuth) -> Optional[str]:
    """What to tell the reader about the download endpoint, if anything."""
    if verdict is DownloadAuth.ENABLED:
        return AUTH_WARNING
    if verdict is DownloadAuth.UNKNOWN:
        return AUTH_ADVISORY
    return None


def _auth_lines(feed: Feed, username: Optional[str]) -> List[str]:
    if feed.client.auth_style is AuthStyle.URL_EMBEDDED:
        return [f"Replace `{PASSWORD_PLACEHOLDER}` with your RomM password."]
    shown = username or USERNAME_PLACEHOLDER
    return [
        f"**Username:** `{shown}`",
        "**Password:** your RomM password",
    ]


def _client_field_value(device: Device, client_key: str, domain: str, username: Optional[str]) -> str:
    """Everything the reader needs for one client, as one field body."""
    feeds = [feed for feed in device.feeds if feed.client.key == client_key]
    client = feeds[0].client
    lines: List[str] = []

    for feed in feeds:
        lines.append(f"**{feed.label}**")
        lines.append(f"`{feed_url(domain, feed, username)}`")
        if feed.extra_content_types:
            types = ", ".join(f"`{t}`" for t in feed.extra_content_types)
            stem = feed.path.rsplit("/", 1)[0]
            lines.append(f"Also available at `{stem}/<type>`: {types}")

    lines.append("")
    lines.extend(_auth_lines(feeds[0], username))
    lines.append(f"**Accepts:** {client.file_formats}")

    if client.setup_steps:
        lines.append("")
        lines.extend(f"{n}. {step}" for n, step in enumerate(client.setup_steps, 1))

    for caveat in client.caveats:
        lines.append(f"• {caveat}")

    return "\n".join(lines)[:1024]


def build_device_embed(
    device: Device,
    domain: str,
    username: Optional[str],
    verdict: DownloadAuth,
    title: Optional[str] = None,
) -> discord.Embed:
    """One embed covering every feed client that serves `device`."""
    embed = discord.Embed(
        title=f"{title or device.display_name} — Download Feeds",
        description=(
            f"Point your {device.display_name} at this server. "
            "Everything below is specific to your account."
        ),
        color=discord.Color.blue(),
    )

    # dict.fromkeys keeps catalog order while collapsing duplicates, so a
    # device with four feeds across two clients still gets two fields.
    for client_key in dict.fromkeys(feed.client.key for feed in device.feeds):
        client = next(f.client for f in device.feeds if f.client.key == client_key)
        embed.add_field(
            name=f"{client.display_name} — setup",
            value=_client_field_value(device, client_key, domain, username),
            inline=False,
        )

    notice = download_auth_notice(verdict)
    if notice:
        embed.add_field(name="Before you start", value=notice, inline=False)

    if not username:
        embed.add_field(name="Not linked yet", value=LINK_NUDGE, inline=False)

    docs = {feed.client.docs_url for feed in device.feeds}
    embed.set_footer(text=f"RomM feed documentation: {sorted(docs)[0]}")
    return embed
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_feed_embeds.py -v`
Expected: PASS.

If `test_no_field_exceeds_discord_s_value_limit` fails, the `[:1024]` truncation in `_client_field_value` is being defeated by a later append — check that the slice is the last operation.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check cogs/feeds/ tests/test_feed_embeds.py
git add cogs/feeds/embeds.py tests/test_feed_embeds.py
git commit -m "feat(feeds): render a device's feeds, credentials only where needed"
```

---

### Task 5: The cog

**Files:**
- Create: `cogs/feeds/cog.py`
- Modify: `cogs/feeds/__init__.py` (replace the empty placeholder from Task 1)

**Interfaces:**
- Consumes: `available_devices`, `DEVICES_BY_KEY`, `build_device_embed`, `detect_download_auth`, `DownloadAuth`.
- Produces: `Feeds` (the cog class) and `setup(bot)`.

- [ ] **Step 1: Write `cogs/feeds/cog.py`**

There is no test step for this file: it is the bot-dependent seam, and every decision worth asserting was already pushed into Tasks 1-4. Keep it thin enough that this stays true.

```python
"""The /feeds command.

Thin by design: availability, probing and rendering all live in modules that
need no bot, so what is left here is argument handling and the two lookups
that need `self.bot`.
"""

import logging
from typing import Optional

import discord
from discord.ext import commands

from .availability import available_devices
from .catalog import DEVICES_BY_KEY
from .embeds import build_device_embed
from .probe import DownloadAuth, detect_download_auth

logger = logging.getLogger(__name__)


class Feeds(commands.Cog):
    """Setup instructions for RomM's homebrew feed clients."""

    def __init__(self, bot):
        # No I/O here: the cog has to be constructible from a bare fake bot.
        self.bot = bot
        self.download_auth = DownloadAuth.UNKNOWN
        self._probed = False

    @commands.Cog.listener()
    async def on_ready(self):
        await self.ensure_probed()

    async def ensure_probed(self):
        """Probe once per process. Cheap to call from anywhere."""
        if self._probed:
            return
        self._probed = True
        try:
            self.download_auth = await detect_download_auth(
                self.bot.config.DOMAIN, self.bot.fetch_api_endpoint
            )
        except Exception as e:
            logger.error(f"Download-auth probe failed: {e}", exc_info=True)
            self.download_auth = DownloadAuth.UNKNOWN

    def offered_devices(self):
        """Devices this server hosts, from the platforms cache bot.py fills."""
        return available_devices(self.bot.cache.get("platforms"))

    async def romm_username(self, discord_id: int) -> Optional[str]:
        try:
            link = await self.bot.db.get_user_link(discord_id)
        except Exception as e:
            logger.error(f"Could not look up RomM link for {discord_id}: {e}")
            return None
        return (link or {}).get("romm_username")

    def device_title(self, device) -> str:
        """The display name, emoji-decorated when we can manage it.

        PlatformEmoji.format returns "Name <emoji>" and falls back to a plain
        🎮, so this cannot KeyError the way indexing emoji_dict would.
        """
        platform_emoji = getattr(self.bot, "platform_emoji", None)
        if not platform_emoji:
            return device.display_name
        try:
            return platform_emoji.format(device.display_name)
        except Exception:
            return device.display_name

    async def device_autocomplete(self, ctx: discord.AutocompleteContext):
        typed = (ctx.value or "").lower()
        return [
            device.display_name
            for device in self.offered_devices()
            if typed in device.display_name.lower()
        ][:25]

    @discord.slash_command(
        name="feeds",
        description="Setup instructions for connecting a console to this server",
    )
    async def feeds(
        self,
        ctx,
        device: discord.Option(
            str,
            "The console you want to set up",
            required=True,
            autocomplete=device_autocomplete,
        ),
    ):
        await self.ensure_probed()
        offered = self.offered_devices()

        if not offered:
            await ctx.respond(
                "This server doesn't host any platforms with feed clients.",
                ephemeral=True,
            )
            return

        wanted = device.strip().lower()
        match = next(
            (d for d in DEVICES_BY_KEY.values()
             if wanted in (d.key, d.display_name.lower())),
            None,
        )

        if not match:
            names = ", ".join(f"`{d.display_name}`" for d in offered)
            await ctx.respond(
                f"I don't have feeds for `{device}`. Available here: {names}",
                ephemeral=True,
            )
            return

        if match.key not in {d.key for d in offered}:
            names = ", ".join(f"`{d.display_name}`" for d in offered)
            await ctx.respond(
                f"This server doesn't host {match.display_name}. Available here: {names}",
                ephemeral=True,
            )
            return

        try:
            embed = build_device_embed(
                match,
                self.bot.config.DOMAIN,
                await self.romm_username(ctx.author.id),
                self.download_auth,
                title=self.device_title(match),
            )
            await ctx.respond(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error building the feeds embed: {e}", exc_info=True)
            await ctx.respond(
                "❌ Something went wrong building those instructions.",
                ephemeral=True,
            )
```

- [ ] **Step 2: Write `cogs/feeds/__init__.py`**

Mirrors `cogs/requests/__init__.py`: re-exports plus `setup`.

```python
"""Feed-client setup instructions for RomM's five homebrew installers."""

from .availability import available_device_keys, available_devices
from .catalog import DEVICES, DEVICES_BY_KEY, AuthStyle, Device, Feed, FeedClient
from .cog import Feeds
from .embeds import build_device_embed, feed_url
from .probe import DownloadAuth, detect_download_auth

__all__ = [
    "DEVICES",
    "DEVICES_BY_KEY",
    "AuthStyle",
    "Device",
    "DownloadAuth",
    "Feed",
    "FeedClient",
    "Feeds",
    "available_device_keys",
    "available_devices",
    "build_device_embed",
    "detect_download_auth",
    "feed_url",
]


def setup(bot):
    bot.add_cog(Feeds(bot))
```

- [ ] **Step 3: Verify the extension imports and exposes `setup`**

Run: `python -c "import cogs.feeds; print(callable(cogs.feeds.setup))"`
Expected: `True`

- [ ] **Step 4: Run the whole suite to confirm nothing regressed**

Run: `python -m pytest -q`
Expected: all tests pass.

- [ ] **Step 5: Lint and commit**

```bash
python -m ruff check cogs/feeds/
git add cogs/feeds/cog.py cogs/feeds/__init__.py
git commit -m "feat(feeds): the /feeds command over the catalog, probe and embeds"
```

---

### Task 6: Wiring, removal, and docs

**Files:**
- Modify: `bot.py:676` (the `core_cogs` list), `bot.py:688-697` (`cog_dependencies`)
- Modify: `cogs/info.py` — delete lines 21-22, 142-174, 297-378
- Modify: `tests/test_extension_loading.py` — add dependency-map coverage
- Modify: `README.md:51` and `README.md:171-183`

**Interfaces:**
- Consumes: `cogs.feeds.setup` from Task 5.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the failing test for dependency-map coverage**

Add to `tests/test_extension_loading.py`. First add a reader alongside the existing `declared_cogs()` helper:

```python
def declared_dependencies():
    """The cog_dependencies map out of bot.py, read rather than restated.

    Nothing else checks this map. A cog missing from it loads without its
    import guard; a cog misspelled in it is simply never matched. Both fail
    silently at startup with a logged error and a skipped cog.
    """
    tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "cog_dependencies" in targets and isinstance(node.value, ast.Dict):
                return {
                    key.value: [
                        element.value for element in value.elts
                        if isinstance(element, ast.Constant)
                    ]
                    for key, value in zip(node.value.keys, node.value.values)
                    if isinstance(key, ast.Constant) and isinstance(value, ast.List)
                }
    raise AssertionError("cog_dependencies is no longer a plain dict in bot.py")
```

Then add the test class:

```python
class DependencyMapTests(unittest.TestCase):
    def test_every_declared_cog_has_a_dependency_entry(self):
        missing = set(declared_cogs()) - set(declared_dependencies())
        self.assertEqual(missing, set(), f"no cog_dependencies entry for {missing}")

    def test_the_dependency_map_names_no_cog_that_is_not_loaded(self):
        extra = set(declared_dependencies()) - set(declared_cogs())
        self.assertEqual(extra, set(), f"cog_dependencies names unloaded {extra}")

    def test_feeds_declares_aiohttp_for_its_probe(self):
        self.assertIn("aiohttp", declared_dependencies().get("cogs.feeds", []))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_extension_loading.py -v -k Dependency`
Expected: FAIL — `cogs.feeds` is in neither list yet, so `test_feeds_declares_aiohttp_for_its_probe` fails on the missing key.

- [ ] **Step 3: Register the cog in `bot.py`**

In the `core_cogs` list (opens at `bot.py:676`), add `'cogs.feeds',` after `'cogs.info',`. In the `cog_dependencies` dict (`bot.py:688-697`), add `'cogs.feeds': ['aiohttp'],` in the matching position.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_extension_loading.py -v`
Expected: PASS, including the pre-existing tests.

- [ ] **Step 5: Delete the superseded Switch command from `cogs/info.py`**

Three deletions, bottom-up so the line numbers hold:

1. Lines 297-378 — the whole `@discord.slash_command(name="switch_shop_info", ...)` decorator and `switch_shop_info` method.
2. Lines 168-174 — the `cog_slash_command_check` method. It gates only `switch_shop_info` and returns `True` for every other Info command, so removing it changes nothing else.
3. Lines 142-166 — the `check_switch_platform` method.
4. Lines 21-22 — `self.has_switch = False` and `bot.loop.create_task(self.check_switch_platform())` in `__init__`.

- [ ] **Step 6: Verify nothing else referenced them**

Run: `grep -rn "has_switch\|check_switch_platform\|switch_shop_info\|cog_slash_command_check" --include="*.py" .`
Expected: no output.

- [ ] **Step 7: Confirm Info still imports and the suite still passes**

Run: `python -c "import cogs.info" && python -m pytest -q`
Expected: import succeeds; all tests pass.

- [ ] **Step 8: Update `README.md`**

Replace the Features bullet at line 51:

```markdown
- **Feed clients**: `/feeds` gives per-console setup instructions for RomM's five URL feeds — [Tinfoil](https://tinfoil.io/Download) (Switch), pkgj and pkgi (Vita/PSP/PSX/PS3), fpkgi (PS4/PS5), and Kekatsu (DS). Only consoles this server actually hosts are offered, and the bot checks on startup whether the RomM instance has download-endpoint auth disabled, warning you if a setup is going to fail.
```

Add to the command table (after the `/platforms` row, around line 174):

```markdown
- `/feeds [device]` — Setup instructions for connecting a console to this server: feed URL, your RomM username, client-specific steps, and the file formats that console can actually install. Replaces the old `/switch_shop_info`, which covered Switch only. Responses are private to you.
```

- [ ] **Step 9: Full verification**

```bash
python -m pytest -q
python -m ruff check .
```

Expected: all tests pass; `All checks passed!`.

- [ ] **Step 10: Commit**

```bash
git add bot.py cogs/info.py tests/test_extension_loading.py README.md
git commit -m "feat(feeds): load the feeds cog and retire switch_shop_info

The Switch command it replaces advertised /tinfoil/feed, a path RomM moved
to /api/feeds/tinfoil, and its embed never mentioned the download-auth
prerequisite without which Tinfoil cannot download anything.

Deleting it also retires check_switch_platform, the has_switch flag and the
cog_slash_command_check override that existed only to gate that one command.

Also closes a gap in test_extension_loading: cog_dependencies was read by no
test, so a missing entry failed silently at startup."
```

---

## Manual verification

The automated tests cover everything except py-cord registration and the live probe, neither of which can run without a guild and a RomM server. After Task 6, on a real instance:

1. Start the bot; confirm the log shows a `Download-auth probe: HTTP <status> -> <verdict>` line, and that the verdict matches the instance's actual `DISABLE_DOWNLOAD_ENDPOINT_AUTH` setting.
2. Run `/feeds` and confirm the autocomplete lists only consoles the server hosts.
3. Pick Switch; confirm the response is ephemeral, the username is yours, and the URL reads `/api/feeds/tinfoil`.
4. Pick PlayStation Vita; confirm both pkgj and pkgi appear, and that **only** the pkgj URL contains `your-username:<your-password>@`.
5. Confirm `/switch_shop_info` no longer appears in Discord's command picker.
6. If a test account exists with no RomM link, run `/feeds` as it and confirm the placeholder plus the linking nudge.
