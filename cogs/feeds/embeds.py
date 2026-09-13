"""Rendering one device's feeds as a Discord embed.

Pure: everything bot-dependent (the emoji-decorated title, the user's RomM
username, the probe verdict, what the server hosts) arrives as an argument,
so the whole embed can be asserted against with no bot and no network.
"""

from typing import FrozenSet, List, Optional
from urllib.parse import quote, urlsplit, urlunsplit

import discord

from .catalog import AuthStyle, Device, Feed
from .urls import with_scheme
from .verdict import DownloadAuth

USERNAME_PLACEHOLDER = "<your-romm-username>"
PASSWORD_PLACEHOLDER = "<your-password>"

AUTH_WARNING = (
    "⚠️ {clients} cannot authenticate downloads - it signs in to the feed, "
    "then hands your console download links with no credentials on them. "
    "This server currently requires authentication to download, so games "
    "will list and then fail to install. An admin needs to set "
    "`DISABLE_DOWNLOAD_ENDPOINT_AUTH=true` on the RomM instance."
)

AUTH_ADVISORY = (
    "{clients} cannot authenticate downloads. If games list but fail to "
    "install, the RomM instance needs `DISABLE_DOWNLOAD_ENDPOINT_AUTH=true` "
    "set by an admin."
)

LINK_NUDGE = (
    "Ask an admin to link your Discord and RomM accounts, and this command "
    "will fill your username in for you."
)


def feed_url(domain: str, feed: Feed, username: Optional[str]) -> str:
    """The URL to show for one feed.

    Credentials go in the URL only for clients that have nowhere else to put
    them - pkgj and pkgi, which read URLs out of a config file and have no
    password box. A real username is percent-encoded because it is free text
    out of the database, and an unencoded "@" in it would silently repoint
    the URL at another host. The placeholder is left legible on purpose:
    encoding it would hand an unlinked user "%3Cyour-romm-username%3E".
    """
    base = with_scheme(domain)
    if feed.client.auth_style is not AuthStyle.URL_EMBEDDED:
        return f"{base}{feed.path}"

    name = quote(username, safe="") if username else USERNAME_PLACEHOLDER
    userinfo = f"{name}:{PASSWORD_PLACEHOLDER}"
    scheme, netloc, path, query, fragment = urlsplit(base)
    return urlunsplit((scheme, f"{userinfo}@{netloc}", path + feed.path, query, fragment))


def feed_config_line(domain: str, feed: Feed, username: Optional[str]) -> str:
    """One line of the client's config file, ready to paste.

    pkgj's config.txt is key-value lines, so a bare URL in it is inert - the
    key in front of it is what tells pkgj which list the URL feeds.
    """
    url = feed_url(domain, feed, username)
    return f"{feed.config_key} {url}" if feed.config_key else url


def download_auth_notice(verdict: DownloadAuth, device: Device) -> Optional[str]:
    """What to tell the reader about the download endpoint, if anything.

    Only for clients that genuinely need the flag. pkgj, pkgi, fpkgi and
    Kekatsu all send basic auth on the download request as well as the feed
    fetch, so telling their users to get the server's download auth turned
    off would be asking for a weakened server to fix a problem they do not
    have. Tinfoil is the one that cannot.
    """
    affected = [
        feed.client for feed in device.feeds
        if feed.client.needs_download_auth_disabled
    ]
    if not affected:
        return None

    names = " and ".join(dict.fromkeys(client.display_name for client in affected))
    if verdict is DownloadAuth.ENABLED:
        return AUTH_WARNING.format(clients=names)
    if verdict is DownloadAuth.UNKNOWN:
        return AUTH_ADVISORY.format(clients=names)
    return None


def _auth_lines(feed: Feed, username: Optional[str]) -> List[str]:
    if feed.client.auth_style is AuthStyle.URL_EMBEDDED:
        return [f"Replace `{PASSWORD_PLACEHOLDER}` with your RomM password."]
    shown = username or USERNAME_PLACEHOLDER
    return [
        f"**Username:** `{shown}`",
        "**Password:** your RomM password",
    ]


def _connection_fields(url: str) -> List[str]:
    """A URL broken into the fields a client like Tinfoil asks for.

    Tinfoil's "new file server" dialog has protocol, host, port and path
    boxes and no URL box, so handing it a URL alone would be a step back
    from the command this replaces.
    """
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return [
        f"**Protocol:** `{parts.scheme}`",
        f"**Host:** `{parts.hostname}`",
        f"**Port:** `{port}`",
        f"**Path:** `{parts.path}`",
    ]


def _client_field_value(feeds: List[Feed], domain: str, username: Optional[str]) -> str:
    """Everything the reader needs for one client, as one field body."""
    client = feeds[0].client
    base = with_scheme(domain)
    lines: List[str] = []

    for feed in feeds:
        url = feed_url(domain, feed, username)
        lines.append(f"**{feed.label}**")
        if client.split_url_into_fields:
            lines.extend(_connection_fields(url))
        else:
            lines.append(f"`{feed_config_line(domain, feed, username)}`")
        if feed.extra_content_types:
            types = ", ".join(f"`{t}`" for t in feed.extra_content_types)
            stem = f"{base}{feed.path.rsplit('/', 1)[0]}"
            lines.append(f"Also available at `{stem}/<type>` — {types}")

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
    hosted: Optional[FrozenSet[str]] = None,
    title: Optional[str] = None,
) -> discord.Embed:
    """One embed covering every feed client that serves `device`.

    `hosted` is the set of ContentPlatform keys this server holds. Feeds for
    content the server does not have are dropped: a Vita owner whose library
    is all PSP should not be handed three dead Vita URLs. Passing None keeps
    every feed, which is the right fallback when availability is unknown.
    """
    feeds = [
        feed for feed in device.feeds
        if hosted is None or feed.content in hosted
    ] or list(device.feeds)

    embed = discord.Embed(
        title=f"{title or device.display_name} — Download Feeds",
        description=(
            f"Point your {device.display_name} at this server. "
            "Everything below is specific to your account."
        ),
        color=discord.Color.blue(),
    )

    # dict.fromkeys keeps catalog order while collapsing duplicates, so a
    # device with seven feeds across two clients still gets two fields.
    for client_key in dict.fromkeys(feed.client.key for feed in feeds):
        for_client = [feed for feed in feeds if feed.client.key == client_key]
        embed.add_field(
            name=f"{for_client[0].client.display_name} — setup",
            value=_client_field_value(for_client, domain, username),
            inline=False,
        )

    notice = download_auth_notice(verdict, device)
    if notice:
        embed.add_field(name="Before you start", value=notice, inline=False)

    if not username:
        embed.add_field(name="Not linked yet", value=LINK_NUDGE, inline=False)

    docs = {feed.client.docs_url for feed in feeds}
    embed.set_footer(text=f"RomM feed documentation: {sorted(docs)[0]}")
    return embed
