"""The feed clients RomM exposes, as data.

RomM serves five homebrew installers from /api/feeds/. Three things vary
between them, and all three live here as data so embeds.py can render any
device without knowing which client it is looking at:

- where the client will accept credentials,
- whether it can authenticate its *downloads* or only its feed fetch,
- what hardware it runs on, which is not the same as what content it serves.

That last distinction earns its keep with pkgj: it runs only on a Vita, but
its feeds carry Vita, PSP and PSX content. A PSX game is something you pull
onto a Vita, not a console anyone points at this server - so PSX is a
ContentPlatform here and never a Device.

Devices are matched to RomM platforms by the content their feeds carry, and
by name rather than slug: the platforms payload the bot caches is sanitized
down to id/name/custom_name/display_name/rom_count, and slug is not among it.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Tuple


class AuthStyle(Enum):
    """Where a client will accept credentials."""

    # The client has username and password fields of its own, so the URL we
    # show stays bare and no secret appears in the Discord message.
    FIELDS = "fields"
    # The client takes URLs in a config file and nothing else, so the
    # credentials have to ride in the URL's userinfo component.
    URL_EMBEDDED = "url_embedded"


@dataclass(frozen=True)
class ContentPlatform:
    """A platform whose ROMs a feed can carry.

    Deliberately not a Device: this is what the *server* hosts, while a
    Device is what the *user* owns and runs a client on. They coincide for
    Switch and PS3; they do not for PSP and PSX, which a Vita fetches.

    `exact_names` is for names that would over-match as substrings -
    "PlayStation" is a prefix of "PlayStation 3" - while `name_fragments`
    are safe to look for anywhere in a platform's name.
    """

    key: str
    display_name: str
    exact_names: Tuple[str, ...] = ()
    name_fragments: Tuple[str, ...] = ()


@dataclass(frozen=True)
class FeedClient:
    """One homebrew installer."""

    key: str
    display_name: str
    auth_style: AuthStyle
    file_formats: str
    docs_url: str
    # Tinfoil signs in to the feed, then hands the console download URLs it
    # never attaches credentials to - so its downloads work only where the
    # server has DISABLE_DOWNLOAD_ENDPOINT_AUTH=true. Every other client
    # sends basic auth on both requests and needs no such thing. Warning
    # their users about it would be telling them to get a server weakened
    # for no reason, so this is a per-client fact, never a global one.
    needs_download_auth_disabled: bool = False
    setup_steps: Tuple[str, ...] = ()
    caveats: Tuple[str, ...] = ()
    # Tinfoil's "new file server" dialog has no URL field at all - it asks
    # for protocol, host, port and path separately.
    split_url_into_fields: bool = False


@dataclass(frozen=True)
class Feed:
    """One URL a client can be pointed at.

    `content` names the ContentPlatform whose ROMs this feed carries, which
    is what decides whether the feed is worth showing on a given server.

    `config_key` is the setting name the client's config file expects, where
    the client has one. pkgj will not read a bare URL: its config.txt is
    key-value lines, so a URL without `url_psp_games` in front of it is
    inert.

    `extra_content_types` exists so pkgi's long tail (Vita accepts eleven
    content types) can be summarized as a template line instead of being
    enumerated as eleven Feed entries that would swamp the embed.
    """

    client: FeedClient
    path: str
    label: str
    content: str
    config_key: Optional[str] = None
    extra_content_types: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Device:
    """Hardware a user owns and runs a feed client on."""

    key: str
    display_name: str
    feeds: Tuple[Feed, ...] = field(default_factory=tuple)

    @property
    def content_keys(self) -> Tuple[str, ...]:
        """Every ContentPlatform this device's feeds can carry, in order."""
        return tuple(dict.fromkeys(feed.content for feed in self.feeds))


# One documentation page with a per-client anchor. The older
# /Integrations/Tinfoil-integration/ path still resolves but 302s here.
DOCS = "https://docs.romm.app/latest/ecosystem/feed-clients/"

TINFOIL = FeedClient(
    key="tinfoil",
    display_name="Tinfoil",
    auth_style=AuthStyle.FIELDS,
    file_formats="`.nsp` and `.xci`",
    docs_url=f"{DOCS}#tinfoil",
    needs_download_auth_disabled=True,
    setup_steps=(
        "Open Tinfoil and go to **File Browser**.",
        "Scroll to the empty slot and press `-` to add a new file-server.",
        "Enter the settings above, then press `X` to save.",
        "Close and reopen Tinfoil so it rescans TitleIDs.",
    ),
    caveats=(
        "Filenames must carry the Switch title ID in brackets, e.g. "
        "`Game Title [0100000000010000].nsp`.",
    ),
    split_url_into_fields=True,
)

PKGJ = FeedClient(
    key="pkgj",
    display_name="pkgj",
    auth_style=AuthStyle.URL_EMBEDDED,
    file_formats="`.pkg`, plus compressed archives (`.zip`, `.7z`)",
    docs_url=f"{DOCS}#pkgj",
    setup_steps=(
        "Open `ux0:/pkgj/config.txt` in VitaShell.",
        "Add the lines above, one per feed you want.",
        "Save, then refresh inside pkgj.",
    ),
    caveats=(
        "pkgj reads URLs from a config file and has no password box, so "
        "your password goes in the URL. Replace `<your-password>` in each "
        "line.",
    ),
)

PKGI = FeedClient(
    key="pkgi",
    display_name="pkgi",
    auth_style=AuthStyle.URL_EMBEDDED,
    file_formats="`.pkg` only",
    docs_url=f"{DOCS}#pkgi",
    setup_steps=(
        "Add the URLs above to pkgi's `config.txt`.",
        "Refresh the list inside pkgi.",
    ),
    caveats=(
        "Every pkgi fork keeps its config.txt somewhere different and names "
        "its URL keys differently, so check your fork's README for the key "
        "that goes in front of each URL.",
        "PS3 and PSP only: those installs also want the matching `.rap` "
        "license file stored alongside the `.pkg`.",
    ),
)

FPKGI = FeedClient(
    key="fpkgi",
    display_name="fpkgi",
    auth_style=AuthStyle.FIELDS,
    file_formats="`.pkg` only",
    docs_url=f"{DOCS}#fpkgi",
    setup_steps=(
        "Add the URL above to fpkgi's config.",
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
    docs_url=f"{DOCS}#kekatsu",
    setup_steps=(
        "Add the URL above as a source in Kekatsu.",
        "Enter your username and password in Kekatsu's auth fields.",
    ),
    caveats=(
        "The feed lists every ROM on the platform, but only `.nds` files "
        "will install.",
        "The DS's wifi supports WEP and one older WPA variant only. A modern "
        "network needs a dedicated SSID or a travel router.",
    ),
)

SWITCH_CONTENT = ContentPlatform(
    "switch", "Nintendo Switch", name_fragments=("switch",)
)
PSVITA_CONTENT = ContentPlatform(
    "psvita", "PlayStation Vita", name_fragments=("vita",)
)
PSP_CONTENT = ContentPlatform(
    "psp", "PlayStation Portable",
    name_fragments=("psp", "playstation portable"),
)
PSX_CONTENT = ContentPlatform(
    "psx", "PlayStation",
    exact_names=("playstation", "sony playstation"),
    name_fragments=("psx", "ps1", "psone"),
)
PS3_CONTENT = ContentPlatform(
    "ps3", "PlayStation 3", name_fragments=("ps3", "playstation 3")
)
PS4_CONTENT = ContentPlatform(
    "ps4", "PlayStation 4", name_fragments=("ps4", "playstation 4")
)
PS5_CONTENT = ContentPlatform(
    "ps5", "PlayStation 5", name_fragments=("ps5", "playstation 5")
)
NDS_CONTENT = ContentPlatform(
    # "nds" and "nintendo ds" both miss "Nintendo 3DS", which is what we want.
    "nds", "Nintendo DS", name_fragments=("nintendo ds", "nds")
)

CONTENT_PLATFORMS: Tuple[ContentPlatform, ...] = (
    SWITCH_CONTENT, PSVITA_CONTENT, PSP_CONTENT, PSX_CONTENT,
    PS3_CONTENT, PS4_CONTENT, PS5_CONTENT, NDS_CONTENT,
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
    feeds=(Feed(TINFOIL, "/api/feeds/tinfoil", "Feed", "switch"),),
)

# pkgj runs here and nowhere else, which is why PSP and PSX content hangs
# off the Vita rather than off devices of their own.
PSVITA = Device(
    key="psvita",
    display_name="PlayStation Vita",
    feeds=(
        Feed(PKGJ, "/api/feeds/pkgj/psvita/games", "Vita games", "psvita", "url_games"),
        Feed(PKGJ, "/api/feeds/pkgj/psvita/dlc", "Vita DLC", "psvita", "url_dlcs"),
        Feed(PKGJ, "/api/feeds/pkgj/psp/games", "PSP games", "psp", "url_psp_games"),
        Feed(PKGJ, "/api/feeds/pkgj/psp/dlc", "PSP DLC", "psp", "url_psp_dlcs"),
        Feed(PKGJ, "/api/feeds/pkgj/psx/games", "PSX games", "psx", "url_psx_games"),
        Feed(PKGI, "/api/feeds/pkgi/psvita/game", "Games", "psvita",
             extra_content_types=_PKGI_EXTRA_VITA),
        Feed(PKGI, "/api/feeds/pkgi/psvita/dlc", "DLC", "psvita"),
    ),
)

PSP = Device(
    key="psp",
    display_name="PlayStation Portable",
    feeds=(
        Feed(PKGI, "/api/feeds/pkgi/psp/game", "Games", "psp",
             extra_content_types=_PKGI_EXTRA_BASE),
        Feed(PKGI, "/api/feeds/pkgi/psp/dlc", "DLC", "psp"),
    ),
)

PS3 = Device(
    key="ps3",
    display_name="PlayStation 3",
    feeds=(
        Feed(PKGI, "/api/feeds/pkgi/ps3/game", "Games", "ps3",
             extra_content_types=_PKGI_EXTRA_BASE),
        Feed(PKGI, "/api/feeds/pkgi/ps3/dlc", "DLC", "ps3"),
    ),
)

PS4 = Device(
    key="ps4",
    display_name="PlayStation 4",
    feeds=(Feed(FPKGI, "/api/feeds/fpkgi/ps4", "Feed", "ps4"),),
)

PS5 = Device(
    key="ps5",
    display_name="PlayStation 5",
    feeds=(Feed(FPKGI, "/api/feeds/fpkgi/ps5", "Feed", "ps5"),),
)

NDS = Device(
    key="nds",
    display_name="Nintendo DS",
    feeds=(Feed(KEKATSU, "/api/feeds/kekatsu/nds", "Feed", "nds"),),
)

DEVICES: Tuple[Device, ...] = (SWITCH, PSVITA, PSP, PS3, PS4, PS5, NDS)

DEVICES_BY_KEY: Dict[str, Device] = {device.key: device for device in DEVICES}
