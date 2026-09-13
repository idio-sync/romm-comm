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
from typing import Any, Dict, Optional

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

    `rooms` is None for a failed request, {} for a successful empty one. The
    return value drives edit suppression: the caller only re-renders when this
    says something changed.
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

    if watcher.state is NetplayState.LIVE:
        became_stale = (
            not watcher.stale and watcher.consecutive_failures >= stale_after
        )
        watcher.stale = watcher.stale or watcher.consecutive_failures >= stale_after
        return became_stale

    # PENDING. The timeout has to be checked here too, not only on the empty
    # -success path: with RomM unreachable, every announcement would otherwise
    # sit in PENDING forever, fill the watcher cap, and never drain - which is
    # exactly the self-limiting property the poll set depends on.
    if now - watcher.created_at >= pending_timeout:
        watcher.state = NetplayState.EXPIRED
        return True
    return False


def _handle_empty(watcher: NetplayWatcher, now: float, pending_timeout: float) -> bool:
    """No rooms open: either the session finished, or it never started."""
    if watcher.state is NetplayState.LIVE:
        watcher.state = NetplayState.ENDED
        return True

    if now - watcher.created_at >= pending_timeout:
        watcher.state = NetplayState.EXPIRED
        return True

    return False
