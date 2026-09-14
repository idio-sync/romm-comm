"""One announced netplay session, and the rules that move it along.

Pure: no Discord, no HTTP, no clock of its own. `advance` takes the poll
result and the current time and mutates the watcher, which makes every
transition testable directly and leaves the cog's loop doing nothing but IO.

The distinction that matters is None vs {}. RomM returning {} means nobody is
in a room; the client returning None means the request failed. Treating a
failed request as an empty room list would end a live session every time the
server hiccups, so failures are counted and only end a session once they
persist.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

DEFAULT_PENDING_TIMEOUT = 900.0
DEFAULT_STALE_AFTER = 3


class NetplayState(str, Enum):
    """Where an announcement is in its life.

    PENDING is the state that carries the actual value: the announcement is
    useful before a room exists, because that is when someone is deciding
    whether to join.
    """

    PENDING = "pending"
    LIVE = "live"
    ENDED = "ended"
    EXPIRED = "expired"


TERMINAL_STATES = frozenset({NetplayState.ENDED, NetplayState.EXPIRED})


@dataclass
class NetplayWatcher:
    """An announcement being tracked, and the last thing we knew about it."""

    rom_id: int
    rom_name: str
    requester_id: int
    # Captured at announce time: the poll loop cannot assume bot.get_user()
    # still has this user cached, and a guild nickname is not what
    # get_user().display_name returns anyway.
    requester_name: str
    channel_id: int
    created_at: float
    platform_display: str = ""
    cover_url: Optional[str] = None
    message_id: Optional[int] = None
    state: NetplayState = NetplayState.PENDING
    rooms: Dict[str, Any] = field(default_factory=dict)
    consecutive_failures: int = 0
    # Discord user ids of everyone who pressed Join, in press order. RomM
    # cannot tell us who is actually in the room - /netplay/list returns the
    # owner and a count, and the socket event carrying the roster is scoped to
    # the room, so subscribing would mean calling join-room and occupying one
    # of max_players. This is the Discord-side answer, and Discord
    # authenticates it, which player_name is not.
    roster: List[int] = field(default_factory=list)
    # When the first room appeared, and when the last one closed. Kept apart
    # from created_at so "ran for" measures the session rather than the wait.
    live_since: Optional[float] = None
    ended_at: Optional[float] = None
    # Set once polls have failed enough times that what we are showing can no
    # longer be trusted. Not a state: the session is probably still running,
    # we just cannot see it, and saying "ended" would be a claim we cannot
    # support. Cleared by the first successful poll.
    stale: bool = False
    last_render_key: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        """Terminal watchers are dropped from the registry after a tick."""
        return self.state in TERMINAL_STATES


def advance(
    watcher: NetplayWatcher,
    rooms: Optional[Dict[str, Any]],
    now: float,
    pending_timeout: float = DEFAULT_PENDING_TIMEOUT,
    stale_after: int = DEFAULT_STALE_AFTER,
) -> bool:
    """Apply one poll result. Returns whether a reader would see a difference.

    `rooms` is None for a failed request, {} for a successful empty one.

    The return value is informational - it reports whether this poll changed
    anything a reader would notice. It is deliberately not what gates the
    re-render: the cog calls refresh_message every tick and suppresses the
    edit by comparing against the last render Discord actually accepted, so
    an edit that failed is retried on a later tick that reports no change.
    Gating on this instead would strand that post on its old contents.
    """
    if watcher.is_terminal:
        return False

    if rooms is None:
        return _handle_failure(watcher, now, pending_timeout, stale_after)

    # A successful poll always clears staleness, and un-staling is itself a
    # visible change even when the rooms are identical.
    recovered = watcher.stale
    watcher.stale = False
    watcher.consecutive_failures = 0

    if rooms:
        changed = watcher.state is not NetplayState.LIVE or watcher.rooms != rooms
        if watcher.live_since is None:
            watcher.live_since = now
        watcher.state = NetplayState.LIVE
        watcher.rooms = rooms
        return changed or recovered

    return _handle_empty(watcher, now, pending_timeout) or recovered


def _handle_failure(
    watcher: NetplayWatcher,
    now: float,
    pending_timeout: float,
    stale_after: int,
) -> bool:
    """A failed poll is never an ended session, however long it goes on.

    There is no retry underneath this: list_netplay_rooms goes through
    make_authenticated_request, which has none. So failures are expected to
    arrive in clusters, and the only honest response is to say the display
    cannot be trusted right now - not to invent an ending. A stale watcher
    keeps being polled and recovers on its own when RomM answers again.
    """
    watcher.consecutive_failures += 1

    # Staleness is not scoped to LIVE. A PENDING post that keeps saying "no
    # room is open yet" through an outage is asserting something we cannot
    # see either, and it carries that flag into EXPIRED.
    became_stale = not watcher.stale and watcher.consecutive_failures >= stale_after
    watcher.stale = watcher.stale or became_stale

    if watcher.state is NetplayState.LIVE:
        return became_stale

    # PENDING. The timeout has to be checked here too, not only on the empty
    # -success path: with RomM unreachable, every announcement would otherwise
    # sit in PENDING forever, fill the watcher cap, and never drain - which is
    # exactly the self-limiting property the poll set depends on.
    if now - watcher.created_at >= pending_timeout:
        watcher.state = NetplayState.EXPIRED
        return True
    return became_stale


def _handle_empty(watcher: NetplayWatcher, now: float, pending_timeout: float) -> bool:
    """No rooms open: either the session finished, or it never started."""
    if watcher.state is NetplayState.LIVE:
        watcher.state = NetplayState.ENDED
        watcher.ended_at = now
        return True

    if now - watcher.created_at >= pending_timeout:
        watcher.state = NetplayState.EXPIRED
        return True

    return False
