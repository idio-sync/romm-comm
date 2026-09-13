"""Rendering one announced session, per state.

render_key sits next to build_netplay_embed on purpose. The poll loop only
edits a message when the key changes, so a key that omits a field the embed
shows means the post silently stops updating. Adjacent, they are hard to
drift apart.

The host is printed as the raw string RomM reports. player_name is supplied
by the client and was trivially set to an arbitrary value during testing, so
it is a display hint and nothing else - never a mention, never an identity.
"""

from typing import Any, Dict, Optional

import discord

from .watcher import NetplayState, NetplayWatcher

ENDED_HINT = "Session over — run `/netplay` to announce a new one."
STALE_NOTE = "⚠️ Cannot reach RomM — this may be out of date."

STATE_COLORS = {
    NetplayState.PENDING: discord.Color.blurple,
    NetplayState.LIVE: discord.Color.green,
    NetplayState.ENDED: discord.Color.light_grey,
    NetplayState.EXPIRED: discord.Color.light_grey,
}

STATE_TITLES = {
    NetplayState.PENDING: "🕹️ Netplay starting",
    NetplayState.LIVE: "🟢 Netplay live",
    NetplayState.ENDED: "⚫ Netplay ended",
    NetplayState.EXPIRED: "⚫ Netplay",
}


def player_link(domain: str, rom_id: int) -> str:
    """The EmulatorJS player for one ROM.

    There is no room-id query parameter, so this lands the player in the right
    game and they pick the room from the netplay menu themselves.
    """
    return f"{domain}/rom/{rom_id}/ejs"


def sanitize_name(name: Any) -> str:
    """A client-supplied display name, defanged.

    Strips the characters that would let a host's chosen player_name turn into
    a mention, markdown, or a masked link in our embed. Brackets matter as
    much as the rest: embed field values render [text](url), so a name alone
    would otherwise be enough to publish a live hyperlink under the bot's
    name. This is presentation hygiene, not authentication: the name still
    proves nothing about who anyone is.
    """
    text = str(name or "Unknown")
    for char in ("<", ">", "@", "`", "*", "_", "~", "|", "[", "]", "\n", "\r"):
        text = text.replace(char, "")
    return text.strip()[:64] or "Unknown"


def format_room(room: Dict[str, Any]) -> str:
    """One room as a single line: name, seats, and whether it is locked."""
    name = sanitize_name(room.get("room_name")) or "Room"
    current = room.get("current", 0)
    maximum = room.get("max", 0)
    host = sanitize_name(room.get("player_name"))
    lock = " 🔒" if room.get("hasPassword") else ""
    return f"**{name}** — hosted by {host} · {current}/{maximum}{lock}"


def render_key(watcher: NetplayWatcher) -> str:
    """Everything the embed shows, flattened into a comparable string.

    The poll loop compares this against the last one to decide whether to
    spend a Discord edit. Anything the embed renders must appear here.
    """
    parts = [watcher.state.value, str(watcher.rom_id), str(watcher.stale)]
    for room_id in sorted(watcher.rooms):
        room = watcher.rooms[room_id]
        parts.append(
            "|".join(
                str(x)
                for x in (
                    room_id,
                    room.get("room_name"),
                    room.get("current"),
                    room.get("max"),
                    room.get("player_name"),
                    room.get("hasPassword"),
                )
            )
        )
    return "\n".join(parts)


def build_description(watcher: NetplayWatcher, domain: str) -> str:
    """The line under the title, which is what carries the call to action."""
    link = player_link(domain, watcher.rom_id)

    # A stale watcher is one we cannot currently see. "No room is open yet"
    # and "No session started" are both assertions, and neither is supportable
    # while RomM is unreachable, so the warning is not scoped to LIVE.
    # render_key carries `stale`, so a change here is actually delivered.
    warning = f"{STALE_NOTE}\n" if watcher.stale else ""

    if watcher.state is NetplayState.PENDING:
        return (
            f"{warning}"
            f"**{watcher.requester_name}** wants to play — no room is open yet.\n"
            f"[Open the player]({link}) and start one, or wait for theirs."
        )
    if watcher.state is NetplayState.LIVE:
        return f"{warning}[Join in your browser]({link})"
    if watcher.state is NetplayState.ENDED:
        return ENDED_HINT
    return f"{warning}No session started. {ENDED_HINT}"


def build_netplay_embed(
    watcher: NetplayWatcher,
    *,
    domain: str,
    core_name: Optional[str] = None,
) -> discord.Embed:
    """The announcement, in whatever state it is currently in.

    watcher.platform_display is whatever bot.platform_emoji.format() returned -
    a platform name with its emoji appended, not a bare emoji - so it goes in a
    field of its own rather than being spliced into the title.
    """
    embed = discord.Embed(
        title=f"{STATE_TITLES[watcher.state]} · {watcher.rom_name}"[:256],
        description=build_description(watcher, domain),
        color=STATE_COLORS[watcher.state](),
    )

    if watcher.cover_url:
        embed.set_thumbnail(url=watcher.cover_url)

    if watcher.platform_display:
        embed.add_field(name="Platform", value=watcher.platform_display, inline=True)

    if core_name:
        embed.add_field(name="Core", value=core_name, inline=True)

    if watcher.rooms and watcher.state is NetplayState.LIVE:
        rooms = "\n".join(
            format_room(watcher.rooms[room_id]) for room_id in sorted(watcher.rooms)
        )
        label = "Room" if len(watcher.rooms) == 1 else f"Rooms ({len(watcher.rooms)})"
        embed.add_field(name=label, value=rooms[:1024], inline=False)

    return embed
