"""Rendering one announced session, per state.

render_key sits next to build_netplay_embed on purpose. The poll loop only
edits a message when the key changes, so a key that omits a field the embed
shows means the post silently stops updating. Adjacent, they are hard to
drift apart. The one deliberate exception is the relative timestamp, which
Discord re-renders client-side from a fixed value - putting it in the key
would churn an edit every tick forever.

The host is printed as the raw string RomM reports. player_name is supplied
by the client and was trivially set to an arbitrary value during testing, so
it is a display hint and nothing else - never a mention, never an identity.
The roster beside it is the opposite: Discord user ids that Discord itself
authenticated. The two are never combined into one number, because they
measure different things - intent to play, and RomM's count of who is
actually in the room.
"""

from typing import Any, Dict, Optional

import discord

from .watcher import NetplayState, NetplayWatcher

ENDED_HINT = "Run `/netplay` to announce a new one."
STALE_NOTE = "⚠️ Cannot reach RomM — this may be out of date."

STATE_COLORS = {
    NetplayState.PENDING: discord.Color.blurple,
    NetplayState.LIVE: discord.Color.green,
    NetplayState.ENDED: discord.Color.light_grey,
    NetplayState.EXPIRED: discord.Color.light_grey,
}

# The state moved out of the title so the title can be the game. Discord puts
# the author line above it in small type, which is the right weight for a
# status the colour bar is already signalling.
STATE_AUTHORS = {
    NetplayState.PENDING: "🕹️ Netplay starting",
    NetplayState.LIVE: "🟢 Netplay live",
    NetplayState.ENDED: "⚫ Netplay ended",
    NetplayState.EXPIRED: "⚫ Netplay — no session started",
}

# One field, three tenses, tracking the state machine.
ROSTER_LABELS = {
    NetplayState.PENDING: "Waiting",
    NetplayState.LIVE: "Playing",
    NetplayState.ENDED: "Played",
    NetplayState.EXPIRED: "Waited",
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


def relative(moment: Optional[float]) -> str:
    """A Discord relative timestamp, or empty when we have no moment.

    Discord renders <t:unix:R> as "4 minutes ago" in the reader's own locale
    and keeps it current on its own. That is why it must stay out of
    render_key: the value never changes, only the display, so re-rendering it
    server-side would spend an edit to change nothing.
    """
    return f"<t:{int(moment)}:R>" if moment else ""


def format_room(room: Dict[str, Any]) -> str:
    """One room as a single line: name, seats, and whether it is locked."""
    name = sanitize_name(room.get("room_name")) or "Room"
    current = room.get("current", 0)
    maximum = room.get("max", 0)
    host = sanitize_name(room.get("player_name"))
    lock = " 🔒" if room.get("hasPassword") else ""
    return f"**{name}** — hosted by {host} · {current}/{maximum}{lock}"


def seat_summary(rooms: Dict[str, Any]) -> str:
    """The one fact a reader is actually looking for, in words.

    "1/2" buried at the end of a sentence is the least prominent thing in the
    post and the most decision-relevant, so it leads instead - and says what
    it means rather than making the reader do the subtraction.
    """
    taken = sum(r.get("current", 0) for r in rooms.values())
    total = sum(r.get("max", 0) for r in rooms.values())
    free = max(total - taken, 0)

    if not total:
        return "**A room is open**"
    if not free:
        return f"**Full** — {taken} of {total}"
    seats = "seat" if free == 1 else "seats"
    return f"**{free} {seats} open** — {taken} of {total} players"


def format_duration(seconds: float) -> str:
    """How long the session ran, rounded to something a human would say."""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, rest = divmod(minutes, 60)
    if not rest:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    return f"{hours}h {rest}m"


def render_key(watcher: NetplayWatcher) -> str:
    """Everything the embed shows, flattened into a comparable string.

    The poll loop compares this against the last one to decide whether to
    spend a Discord edit. Anything the embed renders must appear here - except
    the relative timestamps, which Discord re-renders on its own (see
    `relative`).
    """
    parts = [
        watcher.state.value,
        str(watcher.rom_id),
        str(watcher.stale),
        ",".join(str(uid) for uid in watcher.roster),
    ]
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
    """What sits under the title: the status, then the detail beneath it."""
    link = player_link(domain, watcher.rom_id)

    # A stale watcher is one we cannot currently see. "No room is open yet"
    # and "No session started" are both assertions, and neither is supportable
    # while RomM is unreachable, so the warning is not scoped to LIVE.
    # render_key carries `stale`, so a change here is actually delivered.
    lines = [STALE_NOTE] if watcher.stale else []

    if watcher.state is NetplayState.PENDING:
        lines.append(f"**{watcher.requester_name}** wants to play — no room is open yet.")
        lines.append(
            f"[Open the player]({link}) and start one · announced "
            f"{relative(watcher.created_at)}"
        )
        return "\n".join(lines)

    if watcher.state is NetplayState.LIVE:
        lines.append(seat_summary(watcher.rooms))
        # One room is the common case, and a labelled field for a single line
        # is a heading over nothing. Several rooms keep the field, where the
        # label starts doing real work.
        if len(watcher.rooms) == 1:
            only = next(iter(watcher.rooms.values()))
            room = sanitize_name(only.get("room_name"))
            host = sanitize_name(only.get("player_name"))
            lock = " 🔒" if only.get("hasPassword") else ""
            opened = relative(watcher.live_since)
            since = f" · opened {opened}" if opened else ""
            lines.append(f"Room “{room}”, hosted by {host}{lock}{since}")
        return "\n".join(lines)

    if watcher.state is NetplayState.ENDED:
        if watcher.live_since and watcher.ended_at:
            ran = format_duration(watcher.ended_at - watcher.live_since)
            lines.append(f"Ran for {ran} · ended {relative(watcher.ended_at)}")
        else:
            lines.append("Session over.")
        return "\n".join(lines)

    lines.append(
        f"**{watcher.requester_name}** announced this "
        f"{relative(watcher.created_at)}, but no room was ever opened."
    )
    return "\n".join(lines)


def build_netplay_embed(
    watcher: NetplayWatcher,
    *,
    domain: str,
    core_name: Optional[str] = None,
) -> discord.Embed:
    """The announcement, in whatever state it is currently in.

    Platform and the roster are both inline, which is the whole reason
    Platform is affordable: Discord only puts fields side by side once there
    are two of them, so a lone inline field costs a full row for half a row of
    information.
    """
    embed = discord.Embed(
        title=watcher.rom_name[:256],
        description=build_description(watcher, domain),
        color=STATE_COLORS[watcher.state](),
    )
    embed.set_author(name=STATE_AUTHORS[watcher.state])

    if watcher.cover_url:
        embed.set_thumbnail(url=watcher.cover_url)

    if watcher.platform_display:
        embed.add_field(name="Platform", value=watcher.platform_display, inline=True)

    if core_name:
        embed.add_field(name="Core", value=core_name, inline=True)

    if watcher.roster:
        # Mentions in an embed render as the member's name and notify nobody,
        # so this reads as a roster rather than a pile of pings.
        names = " · ".join(f"<@{uid}>" for uid in watcher.roster)
        embed.add_field(
            name=ROSTER_LABELS[watcher.state], value=names[:1024], inline=True
        )

    if len(watcher.rooms) > 1 and watcher.state is NetplayState.LIVE:
        rooms = "\n".join(
            format_room(watcher.rooms[room_id]) for room_id in sorted(watcher.rooms)
        )
        embed.add_field(name=f"Rooms ({len(watcher.rooms)})", value=rooms[:1024],
                        inline=False)

    if watcher.state in (NetplayState.ENDED, NetplayState.EXPIRED):
        embed.set_footer(text=ENDED_HINT)
    elif watcher.stale:
        embed.set_footer(text="retrying")
    else:
        embed.set_footer(text="updates automatically")

    return embed
