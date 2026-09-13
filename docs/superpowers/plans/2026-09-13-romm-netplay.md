# RomM Netplay Coordination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a `/netplay` slash command that announces a RomM netplay session in Discord and keeps the announcement embed current — waiting, room open with N/M players, ended — by polling RomM.

**Architecture:** Two new methods on the existing `RommClient` read `/api/config` and `/api/netplay/list`. A new `cogs/netplay/` package holds a pure state machine (`watcher.py`), pure renderers (`embeds.py`), a ROM select (`views.py`), and the cog that owns the command, ROM resolution and one `discord.ext.tasks` poll loop (`cog.py`). Nothing goes in `integrations/` — that folder is for third-party services, and netplay is RomM's own API.

**Tech Stack:** Python 3.12+, py-cord (`discord.slash_command`, `discord.ext.tasks`), aiohttp (via `RommClient`), `unittest` run under pytest.

**Spec:** [docs/superpowers/specs/2026-09-13-romm-netplay-design.md](../specs/2026-09-13-romm-netplay-design.md)

## Global Constraints

- **Nothing in `integrations/`.** That folder is only for external services with their own credentials (ggrequestz). Netplay is RomM's own API — client methods go on `romm_client.py`.
- **`Config` in `bot.py` owns all environment reading.** No scattered `os.getenv` in cogs.
- **The host is rendered as the raw `player_name` string.** No Discord identity enrichment in v1 — `player_name` is client-supplied and spoofable. Never render a `<@id>` mention from it.
- **`None` means the call failed; `{}` means no rooms.** A failed poll must never flip an embed to `ENDED`.
- **ruff:** line-length target 110, hard max 180, `max-complexity = 15`. First-party imports are `cogs`, `integrations`, `admin_checks`, `bot`, `database_manager`.
- **Tests:** `unittest` (`TestCase` for pure logic, `IsolatedAsyncioTestCase` for async), subjects built with `object.__new__` where construction needs a live bot. Run with `python -m pytest`.
- **Poll defaults:** interval `20`s, pending timeout `900`s, max watchers `25`, failure threshold `3`.

---

## File Structure

| File | Responsibility |
|---|---|
| `romm_client.py` (modify) | `get_server_config()`, `list_netplay_rooms()`; add `assets.read` to the OAuth grant. |
| `README.md` (modify) | Document `assets.read` and the `/netplay` command. |
| `bot.py` (modify) | `NETPLAY_*` config; register `cogs.netplay` in `core_cogs` + `cog_dependencies`. |
| `cogs/netplay/__init__.py` (create) | Re-exports and `setup()`. |
| `cogs/netplay/watcher.py` (create) | `NetplayState`, `NetplayWatcher`, `advance()`. Pure — no Discord, no HTTP. |
| `cogs/netplay/embeds.py` (create) | `build_netplay_embed()`, `render_key()`. Pure. |
| `cogs/netplay/views.py` (create) | `RomSelect`, `RomSelectView`. New — the request flow's picker is not reusable. |
| `cogs/netplay/cog.py` (create) | The command, ROM resolution, watcher registry, poll loop. |
| `tests/test_netplay_client.py` (create) | Task 1 |
| `tests/test_netplay_watcher.py` (create) | Task 2 |
| `tests/test_netplay_embeds.py` (create) | Task 3 |
| `tests/test_netplay_resolution.py` (create) | Task 5 |
| `tests/test_netplay_cog.py` (create) | Tasks 6-7 |

---

## Task 1: RomM client netplay methods and the `assets.read` scope

**Files:**
- Modify: `romm_client.py:158` (OAuth scope string), and append two methods to `RommClient`
- Modify: `README.md:152` (documented token scopes)
- Test: `tests/test_netplay_client.py` (create)

**Interfaces:**
- Consumes: `RommClient.make_authenticated_request(method, endpoint, params=...)`
- Produces:
  - `async RommClient.get_server_config() -> Optional[Dict[str, Any]]`
  - `async RommClient.list_netplay_rooms(rom_id: int) -> Optional[Dict[str, Any]]`

**Context:** `/api/netplay/list` requires scope `assets.read`, which is currently in neither the OAuth grant nor the README. Without it every call 403s, and `_read_response` returns `None` for all non-2xx alike, so the failure is indistinguishable from a network error.

Do **not** use `fetch_api_endpoint`: `_get_json` calls `self.cache.set(...)` at `romm_client.py:468` even when `bypass_cache=True`, so polling through it poisons the shared cache with values that are stale within seconds.

- [ ] **Step 1: Write the failing test**

Create `tests/test_netplay_client.py`:

```python
"""The two RomM netplay reads, and the None-vs-{} contract they promise.

These go through make_authenticated_request rather than fetch_api_endpoint
because the latter writes to the shared APICache even on its bypass path
(romm_client.py:468), which would fill the cache with room lists that are
stale seconds later. The tests pin the endpoint and params so a later
refactor cannot quietly switch helpers.
"""

import unittest
from typing import Any, Dict, Optional

from romm_client import RommClient


class RecordingClient(RommClient):
    """A RommClient that records calls instead of making them."""

    def __init__(self, result: Optional[Dict[str, Any]]):
        self.calls: list = []
        self._result = result

    async def make_authenticated_request(self, method, endpoint, data=None,
                                         params=None, form_data=None,
                                         require_csrf=False):
        self.calls.append((method, endpoint, params))
        return self._result


class ListNetplayRoomsTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_the_netplay_list_endpoint_with_game_id(self):
        client = RecordingClient({})
        await client.list_netplay_rooms(50265)
        self.assertEqual(client.calls, [("GET", "netplay/list", {"game_id": 50265})])

    async def test_empty_dict_means_no_rooms(self):
        client = RecordingClient({})
        self.assertEqual(await client.list_netplay_rooms(50265), {})

    async def test_none_means_the_call_failed(self):
        """Distinct from {}. A failed poll must not read as an ended session."""
        client = RecordingClient(None)
        self.assertIsNone(await client.list_netplay_rooms(50265))

    async def test_returns_the_room_payload_unchanged(self):
        rooms = {
            "84c4be1e": {
                "room_name": "Bomberman",
                "current": 2,
                "max": 4,
                "player_name": "idiosync",
                "hasPassword": False,
            }
        }
        client = RecordingClient(rooms)
        self.assertEqual(await client.list_netplay_rooms(50265), rooms)


class GetServerConfigTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_the_config_endpoint(self):
        client = RecordingClient({"EJS_NETPLAY_ENABLED": True})
        await client.get_server_config()
        self.assertEqual(client.calls, [("GET", "config", None)])

    async def test_returns_none_on_failure(self):
        client = RecordingClient(None)
        self.assertIsNone(await client.get_server_config())


class ScopeTests(unittest.TestCase):
    def test_oauth_grant_requests_assets_read(self):
        """netplay/list is scope assets.read; without it every call 403s."""
        source = open("romm_client.py", encoding="utf-8").read()
        scope_line = next(ln for ln in source.splitlines() if "add_field('scope'" in ln)
        self.assertIn("assets.read", scope_line)

    def test_readme_documents_assets_read(self):
        readme = open("README.md", encoding="utf-8").read()
        self.assertIn("assets.read", readme)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_netplay_client.py -v`
Expected: FAIL — `AttributeError: 'RecordingClient' object has no attribute 'list_netplay_rooms'`, plus both `ScopeTests` failing on the missing `assets.read`.

- [ ] **Step 3: Add the two methods**

Append to the `RommClient` class in `romm_client.py`, after `_get_json`:

```python
    # --------------------------------------------------------------- netplay

    async def get_server_config(self) -> Optional[Dict[str, Any]]:
        """GET /api/config - what this RomM server supports.

        Carries EJS_NETPLAY_ENABLED and EJS_NETPLAY_ICE_SERVERS. Must be
        authenticated: RomM redacts the ICE server list to [] and the config
        parse error to None for anonymous callers, because ICE entries can
        hold TURN credentials. An unauthenticated check reports a correctly
        configured server as unconfigured.
        """
        return await self.make_authenticated_request('GET', 'config')

    async def list_netplay_rooms(self, rom_id: int) -> Optional[Dict[str, Any]]:
        """GET /api/netplay/list?game_id=<rom_id> - open rooms for one ROM.

        Returns a dict keyed by room session id, or {} when nothing is open.
        game_id is mandatory - omitting it is a 422 - so there is no way to
        list every room on the server, which is why callers poll a bounded
        set of ROMs rather than enumerating.

        Deliberately not routed through fetch_api_endpoint: that helper writes
        to the shared APICache even when bypass_cache is set, and a room list
        is stale within seconds. The cost is that there is no retry here;
        callers must tolerate a None and try again on the next tick.
        """
        return await self.make_authenticated_request(
            'GET', 'netplay/list', params={'game_id': rom_id}
        )
```

- [ ] **Step 4: Add `assets.read` to the OAuth grant**

In `romm_client.py:158`, change the scope string:

```python
            data.add_field('scope', 'roms.read platforms.read firmware.read users.read users.write me.write assets.read')
```

> **Coordination:** the per-user auth spec adds `roms.user.write` to this same line. If that change has already landed, append `assets.read` to whatever is there rather than replacing the string. The intended union is:
> `roms.read platforms.read firmware.read users.read users.write me.write assets.read roms.user.write`

- [ ] **Step 5: Document the scope in the README**

In `README.md:152`, update the documented scope list in the `ROMM_CLIENT_TOKEN` bullet to match, and add this sentence to that bullet:

```markdown
`assets.read` is required by the netplay commands; existing tokens created before it was added must be reissued.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_netplay_client.py -v`
Expected: PASS (8 tests)

- [ ] **Step 7: Lint**

Run: `python -m ruff check romm_client.py tests/test_netplay_client.py`
Expected: no new violations

- [ ] **Step 8: Commit**

```bash
git add romm_client.py README.md tests/test_netplay_client.py
git commit -m "feat(netplay): read RomM netplay rooms and server config

/api/netplay/list needs scope assets.read, which was in neither the OAuth
grant nor the README, so every call would have 403'd - and _read_response
returns None for all non-2xx alike, making that indistinguishable from a
network error.

Neither method uses fetch_api_endpoint: _get_json writes to the shared
APICache even on the bypass path, and a room list is stale in seconds. The
cost is no retry, which callers absorb by tolerating a None until the next
poll."
```

---

## Task 2: The watcher state machine

**Files:**
- Create: `cogs/netplay/__init__.py`
- Create: `cogs/netplay/watcher.py`
- Test: `tests/test_netplay_watcher.py`

**Interfaces:**
- Consumes: nothing (pure module — no Discord, no HTTP)
- Produces:
  - `NetplayState` enum: `PENDING`, `LIVE`, `ENDED`, `EXPIRED`
  - `NetplayWatcher` dataclass with fields `rom_id: int`, `rom_name: str`, `platform_name: str`, `requester_id: int`, `channel_id: int`, `created_at: float`, `message_id: Optional[int] = None`, `state: NetplayState = PENDING`, `rooms: Dict[str, Any] = {}`, `consecutive_failures: int = 0`, `last_render_key: Optional[str] = None`
  - `NetplayWatcher.is_terminal -> bool`

> `platform_name` holds a **display name** ("Super Nintendo Entertainment
> System"), sourced from RomM's `rom['platform_display_name']`. That is what
> `PlatformEmoji.format()` keys on — its `PLATFORM_VARIANTS` table is keyed by
> display name, not slug. Storing a slug here would silently lose the emoji.
  - `advance(watcher, rooms, now, pending_timeout=900.0, failure_threshold=3) -> bool` — mutates the watcher, returns whether anything a reader would see changed

**Context:** This is the whole behavioural core, and it is pure so it can be tested exhaustively without a bot. Two rules matter most: a `None` poll is a failure and must not end a session, and terminal states never change again.

- [ ] **Step 1: Write the failing test**

Create `tests/test_netplay_watcher.py`:

```python
"""The netplay session state machine.

Pure by design: every transition is exercised here with fabricated poll
results, so the cog's loop only has to do IO. The rule this file exists to
protect is that a failed poll (None) is not an empty poll ({}) - conflating
them ends a live session every time RomM hiccups.
"""

import unittest

from cogs.netplay.watcher import NetplayState, NetplayWatcher, advance

ROOM = {"r1": {"room_name": "Bomberman", "current": 2, "max": 4,
               "player_name": "idiosync", "hasPassword": False}}


def make_watcher(**overrides):
    values = {
        "rom_id": 50265,
        "rom_name": "Super Bomberman",
        "platform_name": "Super Nintendo Entertainment System",
        "requester_id": 1,
        "channel_id": 2,
        "created_at": 1000.0,
    }
    values.update(overrides)
    return NetplayWatcher(**values)


class PendingTests(unittest.TestCase):
    def test_starts_pending(self):
        self.assertIs(make_watcher().state, NetplayState.PENDING)

    def test_empty_poll_leaves_it_pending(self):
        w = make_watcher()
        changed = advance(w, {}, now=1010.0)
        self.assertIs(w.state, NetplayState.PENDING)
        self.assertFalse(changed)

    def test_a_room_makes_it_live(self):
        w = make_watcher()
        changed = advance(w, ROOM, now=1010.0)
        self.assertIs(w.state, NetplayState.LIVE)
        self.assertEqual(w.rooms, ROOM)
        self.assertTrue(changed)

    def test_expires_after_the_timeout(self):
        w = make_watcher()
        changed = advance(w, {}, now=1000.0 + 900.0, pending_timeout=900.0)
        self.assertIs(w.state, NetplayState.EXPIRED)
        self.assertTrue(changed)

    def test_does_not_expire_one_second_early(self):
        w = make_watcher()
        advance(w, {}, now=1000.0 + 899.0, pending_timeout=900.0)
        self.assertIs(w.state, NetplayState.PENDING)


class LiveTests(unittest.TestCase):
    def test_empty_poll_ends_the_session(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        changed = advance(w, {}, now=1010.0)
        self.assertIs(w.state, NetplayState.ENDED)
        self.assertTrue(changed)

    def test_player_count_change_is_reported_as_changed(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        grown = {"r1": dict(ROOM["r1"], current=3)}
        self.assertTrue(advance(w, grown, now=1010.0))
        self.assertEqual(w.rooms["r1"]["current"], 3)

    def test_identical_poll_is_not_a_change(self):
        """Drives edit suppression: an unchanged poll must not trigger an edit."""
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        self.assertFalse(advance(w, dict(ROOM), now=1010.0))

    def test_multiple_concurrent_rooms_are_all_kept(self):
        two = {"r1": ROOM["r1"], "r2": dict(ROOM["r1"], room_name="Second")}
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        advance(w, two, now=1010.0)
        self.assertEqual(len(w.rooms), 2)

    def test_one_room_closing_while_another_opens_stays_live(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        replacement = {"r2": dict(ROOM["r1"], room_name="Second")}
        advance(w, replacement, now=1010.0)
        self.assertIs(w.state, NetplayState.LIVE)


class FailureTests(unittest.TestCase):
    def test_a_failed_poll_does_not_end_a_live_session(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        changed = advance(w, None, now=1010.0)
        self.assertIs(w.state, NetplayState.LIVE)
        self.assertFalse(changed)

    def test_repeated_failures_eventually_end_it(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        advance(w, None, now=1010.0, failure_threshold=3)
        advance(w, None, now=1020.0, failure_threshold=3)
        self.assertIs(w.state, NetplayState.LIVE)
        changed = advance(w, None, now=1030.0, failure_threshold=3)
        self.assertIs(w.state, NetplayState.ENDED)
        self.assertTrue(changed)

    def test_a_success_resets_the_failure_count(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        advance(w, None, now=1010.0, failure_threshold=3)
        advance(w, None, now=1020.0, failure_threshold=3)
        advance(w, ROOM, now=1030.0, failure_threshold=3)
        self.assertEqual(w.consecutive_failures, 0)
        advance(w, None, now=1040.0, failure_threshold=3)
        self.assertIs(w.state, NetplayState.LIVE)

    def test_failures_while_pending_do_not_expire_early(self):
        w = make_watcher()
        for t in (1010.0, 1020.0, 1030.0, 1040.0):
            advance(w, None, now=t, failure_threshold=3)
        self.assertIs(w.state, NetplayState.PENDING)


class TerminalTests(unittest.TestCase):
    def test_ended_is_terminal(self):
        w = make_watcher(state=NetplayState.ENDED)
        self.assertFalse(advance(w, ROOM, now=1010.0))
        self.assertIs(w.state, NetplayState.ENDED)

    def test_expired_is_terminal(self):
        w = make_watcher(state=NetplayState.EXPIRED)
        self.assertFalse(advance(w, ROOM, now=1010.0))
        self.assertIs(w.state, NetplayState.EXPIRED)

    def test_is_terminal_flag(self):
        self.assertFalse(make_watcher().is_terminal)
        self.assertFalse(make_watcher(state=NetplayState.LIVE).is_terminal)
        self.assertTrue(make_watcher(state=NetplayState.ENDED).is_terminal)
        self.assertTrue(make_watcher(state=NetplayState.EXPIRED).is_terminal)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_netplay_watcher.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.netplay'`

- [ ] **Step 3: Create the package init**

Create `cogs/netplay/__init__.py`:

```python
"""Netplay session announcements.

RomM has run netplay since 5.x and tells nobody a session is happening: a
room exists, unlisted, until someone opens the same game's player and looks.
This package posts that fact to Discord and keeps the post current.

Split the way cogs/requests is - a pure state machine, pure renderers, a
view, and a cog that does the IO - because the interesting behaviour is the
state machine and it is worth testing without a bot.
"""

from .watcher import NetplayState, NetplayWatcher, advance

__all__ = [
    "NetplayState",
    "NetplayWatcher",
    "advance",
]
```

> `setup()` is added in Task 5, once the cog exists. Leaving it out until then keeps this task's deliverable importable and testable on its own.

- [ ] **Step 4: Write the state machine**

Create `cogs/netplay/watcher.py`:

```python
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
DEFAULT_FAILURE_THRESHOLD = 3


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
    platform_name: str
    requester_id: int
    channel_id: int
    created_at: float
    message_id: Optional[int] = None
    state: NetplayState = NetplayState.PENDING
    rooms: Dict[str, Any] = field(default_factory=dict)
    consecutive_failures: int = 0
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
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
) -> bool:
    """Apply one poll result. Returns whether a reader would see a difference.

    `rooms` is None for a failed request, {} for a successful empty one. The
    return value drives edit suppression: the caller only re-renders when this
    says something changed.
    """
    if watcher.is_terminal:
        return False

    if rooms is None:
        return _handle_failure(watcher, failure_threshold)

    watcher.consecutive_failures = 0

    if rooms:
        changed = watcher.state is not NetplayState.LIVE or watcher.rooms != rooms
        watcher.state = NetplayState.LIVE
        watcher.rooms = rooms
        return changed

    return _handle_empty(watcher, now, pending_timeout)


def _handle_failure(watcher: NetplayWatcher, failure_threshold: int) -> bool:
    """A failed poll is not an ended session - until it keeps failing.

    There is no retry underneath this: list_netplay_rooms goes through
    make_authenticated_request, which has none. This count is the only
    resilience the design has, so it is deliberately forgiving.
    """
    watcher.consecutive_failures += 1
    if (
        watcher.state is NetplayState.LIVE
        and watcher.consecutive_failures >= failure_threshold
    ):
        watcher.state = NetplayState.ENDED
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_netplay_watcher.py -v`
Expected: PASS (18 tests)

- [ ] **Step 6: Lint**

Run: `python -m ruff check cogs/netplay tests/test_netplay_watcher.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add cogs/netplay/__init__.py cogs/netplay/watcher.py tests/test_netplay_watcher.py
git commit -m "feat(netplay): the session state machine

PENDING -> LIVE -> ENDED/EXPIRED, kept pure so every transition is testable
without a bot. PENDING is the state that carries the value: the announcement
is useful before the room exists, which is the coordination gap this feature
exists to close.

None and {} are different things and the tests say so. list_netplay_rooms has
no retry beneath it, so a single failed poll must not end a live session; the
consecutive-failure count is the only resilience in the design."
```

---

## Task 3: Embed renderers

**Files:**
- Create: `cogs/netplay/embeds.py`
- Modify: `cogs/netplay/__init__.py` (re-export)
- Test: `tests/test_netplay_embeds.py`

**Interfaces:**
- Consumes: `NetplayState`, `NetplayWatcher` from Task 2
- Produces:
  - `render_key(watcher: NetplayWatcher) -> str` — everything the embed shows, flattened; used for edit suppression
  - `build_netplay_embed(watcher, *, domain, requester_name, core_name=None, cover_url=None, platform_display='') -> discord.Embed`

> `platform_display` is the already-formatted string from
> `bot.platform_emoji.format(name)`, which returns `"Nintendo 64 <emoji>"` —
> a name *and* an emoji, not a bare emoji. It is rendered as its own field
> rather than spliced into the title.
  - `STATE_COLORS`, `STATE_TITLES` dicts
  - `ENDED_HINT` constant

**Context:** `render_key` lives beside the renderer deliberately: if the key misses a field the embed shows, the embed silently stops updating. Keeping them adjacent makes that drift visible.

The host is the raw `player_name` string. It is client-supplied and was trivially spoofed during testing, so it must never become a mention or a Discord identity.

- [ ] **Step 1: Write the failing test**

Create `tests/test_netplay_embeds.py`:

```python
"""What each netplay state renders, and what must never appear in it.

render_key is tested alongside the embed because they have to agree: if the
key omits something the embed shows, the post silently stops updating when
only that field changes.
"""

import unittest

from cogs.netplay.embeds import (
    ENDED_HINT,
    build_netplay_embed,
    render_key,
)
from cogs.netplay.watcher import NetplayState, NetplayWatcher

ROOM = {"r1": {"room_name": "Bomberman", "current": 2, "max": 4,
               "player_name": "idiosync", "hasPassword": False}}


def make_watcher(**overrides):
    values = {
        "rom_id": 50265,
        "rom_name": "Super Bomberman",
        "platform_name": "Super Nintendo Entertainment System",
        "requester_id": 1,
        "channel_id": 2,
        "created_at": 1000.0,
    }
    values.update(overrides)
    return NetplayWatcher(**values)


def build(watcher, **kwargs):
    kwargs.setdefault("domain", "https://roms.example.com")
    kwargs.setdefault("requester_name", "alice")
    return build_netplay_embed(watcher, **kwargs)


class RenderKeyTests(unittest.TestCase):
    def test_state_change_changes_the_key(self):
        pending = render_key(make_watcher())
        live = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        self.assertNotEqual(pending, live)

    def test_player_count_change_changes_the_key(self):
        before = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        grown = {"r1": dict(ROOM["r1"], current=3)}
        after = render_key(make_watcher(state=NetplayState.LIVE, rooms=grown))
        self.assertNotEqual(before, after)

    def test_identical_watchers_share_a_key(self):
        a = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        b = render_key(make_watcher(state=NetplayState.LIVE, rooms=dict(ROOM)))
        self.assertEqual(a, b)

    def test_room_name_change_changes_the_key(self):
        before = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        renamed = {"r1": dict(ROOM["r1"], room_name="Different")}
        after = render_key(make_watcher(state=NetplayState.LIVE, rooms=renamed))
        self.assertNotEqual(before, after)


class EmbedTests(unittest.TestCase):
    def test_pending_names_the_game_and_links_the_player(self):
        embed = build(make_watcher())
        self.assertIn("Super Bomberman", embed.title)
        self.assertIn("https://roms.example.com/rom/50265/ejs", embed.description)

    def test_live_shows_host_and_seats(self):
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        body = embed.description + "".join(f.value for f in embed.fields)
        self.assertIn("idiosync", body)
        self.assertIn("2/4", body)

    def test_password_protected_room_is_marked(self):
        locked = {"r1": dict(ROOM["r1"], hasPassword=True)}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=locked))
        body = "".join(f.value for f in embed.fields)
        self.assertIn("🔒", body)

    def test_host_is_never_rendered_as_a_mention(self):
        """player_name is client-supplied and was spoofed in testing."""
        spoofed = {"r1": dict(ROOM["r1"], player_name="<@1234567890>")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=spoofed))
        body = embed.description + "".join(f.value for f in embed.fields)
        self.assertNotIn("<@1234567890>", body)

    def test_multiple_rooms_all_render(self):
        two = {"r1": ROOM["r1"], "r2": dict(ROOM["r1"], room_name="Second")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=two))
        body = "".join(f.value for f in embed.fields)
        self.assertIn("Bomberman", body)
        self.assertIn("Second", body)

    def test_ended_tells_the_reader_how_to_start_another(self):
        embed = build(make_watcher(state=NetplayState.ENDED))
        self.assertIn(ENDED_HINT, embed.description)

    def test_expired_says_no_session_started(self):
        embed = build(make_watcher(state=NetplayState.EXPIRED))
        self.assertIn("No session started", embed.description)

    def test_core_is_shown_when_known(self):
        embed = build(make_watcher(), core_name="snes9x")
        body = "".join(f.value for f in embed.fields)
        self.assertIn("snes9x", body)

    def test_missing_cover_art_is_not_fatal(self):
        embed = build(make_watcher(), cover_url=None)
        self.assertIsNotNone(embed)

    def test_cover_art_is_used_when_present(self):
        embed = build(make_watcher(), cover_url="https://example.com/c.png")
        self.assertEqual(embed.thumbnail.url, "https://example.com/c.png")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_netplay_embeds.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.netplay.embeds'`

- [ ] **Step 3: Write the renderers**

Create `cogs/netplay/embeds.py`:

```python
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
    a mention or markdown in our embed. This is presentation hygiene, not
    authentication: the name still proves nothing about who anyone is.
    """
    text = str(name or "Unknown")
    for char in ("<", ">", "@", "`", "*", "_", "~", "|"):
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
    parts = [watcher.state.value, str(watcher.rom_id)]
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


def build_description(watcher: NetplayWatcher, domain: str, requester_name: str) -> str:
    """The line under the title, which is what carries the call to action."""
    link = player_link(domain, watcher.rom_id)

    if watcher.state is NetplayState.PENDING:
        return (
            f"**{requester_name}** wants to play — no room is open yet.\n"
            f"[Open the player]({link}) and start one, or wait for theirs."
        )
    if watcher.state is NetplayState.LIVE:
        return f"[Join in your browser]({link})"
    if watcher.state is NetplayState.ENDED:
        return ENDED_HINT
    return f"No session started. {ENDED_HINT}"


def build_netplay_embed(
    watcher: NetplayWatcher,
    *,
    domain: str,
    requester_name: str,
    core_name: Optional[str] = None,
    cover_url: Optional[str] = None,
    platform_display: str = "",
) -> discord.Embed:
    """The announcement, in whatever state it is currently in.

    `platform_display` is whatever bot.platform_emoji.format() returned - a
    platform name with its emoji appended, not a bare emoji - so it goes in a
    field of its own rather than being spliced into the title.
    """
    embed = discord.Embed(
        title=f"{STATE_TITLES[watcher.state]} · {watcher.rom_name}"[:256],
        description=build_description(watcher, domain, requester_name),
        color=STATE_COLORS[watcher.state](),
    )

    if cover_url:
        embed.set_thumbnail(url=cover_url)

    if platform_display:
        embed.add_field(name="Platform", value=platform_display, inline=True)

    if core_name:
        embed.add_field(name="Core", value=core_name, inline=True)

    if watcher.rooms and watcher.state is NetplayState.LIVE:
        rooms = "\n".join(
            format_room(watcher.rooms[room_id]) for room_id in sorted(watcher.rooms)
        )
        label = "Room" if len(watcher.rooms) == 1 else f"Rooms ({len(watcher.rooms)})"
        embed.add_field(name=label, value=rooms[:1024], inline=False)

    return embed
```

- [ ] **Step 4: Re-export from the package**

Replace the import and `__all__` in `cogs/netplay/__init__.py`:

```python
from .embeds import ENDED_HINT, build_netplay_embed, render_key
from .watcher import NetplayState, NetplayWatcher, advance

__all__ = [
    "ENDED_HINT",
    "NetplayState",
    "NetplayWatcher",
    "advance",
    "build_netplay_embed",
    "render_key",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_netplay_embeds.py -v`
Expected: PASS (14 tests)

- [ ] **Step 6: Lint**

Run: `python -m ruff check cogs/netplay tests/test_netplay_embeds.py`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add cogs/netplay/embeds.py cogs/netplay/__init__.py tests/test_netplay_embeds.py
git commit -m "feat(netplay): render an announcement per state

render_key lives beside the embed builder because they have to agree - a key
missing a field the embed shows means the post silently stops updating when
only that field changes.

The host is printed as a sanitized raw string. player_name comes from the
client and was set to an arbitrary value in testing, so it never becomes a
mention: a host could otherwise pick another member's name and have the bot
vouch for them."
```

---

## Task 4: The ROM select view

**Files:**
- Create: `cogs/netplay/views.py`
- Test: covered in Task 5 (`tests/test_netplay_resolution.py`) — the view is constructor-only logic and is exercised there

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `MAX_SELECT_OPTIONS = 25`
  - `RomSelect(discord.ui.Select)` — options built from RomM ROM rows, `value` is `str(rom['id'])`
  - `RomSelectView(discord.ui.View)` with `selected_rom: Optional[Dict]`, `timeout=120`

**Context:** The request flow's `GameSelect` is **not** reusable here. It truncates with `matches[:25]` and has no page state; `GameSelectView` hard-wires "Submit Request" and "Not Listed" buttons; and its options come from IGDB fields (`match['release_date']`, `match['platforms']`) while netplay needs a RomM rom id. There is also no ROM-name autocomplete anywhere in the repo. This is a new, deliberately small component — do not try to generalise `GameSelect`, which would be a change to the request flow and is out of scope.

- [ ] **Step 1: Write the view**

Create `cogs/netplay/views.py`:

```python
"""Picking one ROM out of a search result.

Deliberately not cogs/requests/views_game.py's GameSelect: that one is built
from IGDB match dicts, hard-wires request-submission buttons, and truncates
at 25 with no page state. Netplay needs a RomM rom id and nothing else, so
this is a smaller component rather than a generalisation of that one -
generalising it would mean changing the request flow.

Discord caps a select at 25 options. This truncates too, but the caller says
so out loud rather than silently dropping matches.
"""

import logging
from typing import Any, Dict, List, Optional

import discord

logger = logging.getLogger(__name__)

MAX_SELECT_OPTIONS = 25


class RomSelect(discord.ui.Select):
    """One option per ROM, valued by rom id."""

    def __init__(self, roms: List[Dict[str, Any]]):
        self.roms = {str(rom["id"]): rom for rom in roms}

        options = [
            discord.SelectOption(
                label=str(rom.get("name") or rom.get("fs_name") or "Unknown")[:100],
                description=(str(rom.get("fs_name") or "")[:100] or None),
                value=str(rom["id"]),
            )
            for rom in roms
        ]

        super().__init__(
            placeholder="Pick the ROM to play...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.selected_rom = self.roms[self.values[0]]
        self.view.stop()


class RomSelectView(discord.ui.View):
    """Holds the select and the answer. Nothing else."""

    def __init__(self, roms: List[Dict[str, Any]]):
        super().__init__(timeout=120)
        self.selected_rom: Optional[Dict[str, Any]] = None
        self.message: Optional[discord.Message] = None
        self.add_item(RomSelect(roms))

    async def on_timeout(self):
        """Leave nothing clickable behind."""
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable a timed-out netplay ROM select")
```

- [ ] **Step 2: Verify it imports**

Run: `python -c "from cogs.netplay.views import RomSelectView, MAX_SELECT_OPTIONS; print(MAX_SELECT_OPTIONS)"`
Expected: `25`

- [ ] **Step 3: Lint**

Run: `python -m ruff check cogs/netplay/views.py`
Expected: clean

- [ ] **Step 4: Commit**

```bash
git add cogs/netplay/views.py
git commit -m "feat(netplay): a ROM select valued by rom id

Not a reuse of the request flow's GameSelect, which is built from IGDB match
dicts, hard-wires Submit Request and Not Listed, and truncates at 25 with no
page state. Netplay needs a rom id, so this is a smaller separate component;
generalising the other one would mean changing the request flow."
```

---

## Task 5: Config, ROM resolution, and the cog skeleton

**Files:**
- Modify: `bot.py` (Config additions; `core_cogs` and `cog_dependencies` entries)
- Create: `cogs/netplay/cog.py`
- Modify: `cogs/netplay/__init__.py` (add `setup`)
- Test: `tests/test_netplay_resolution.py`

**Interfaces:**
- Consumes: `RommClient.get_server_config` (Task 1), `RomSelectView` (Task 4)
- Produces:
  - `Netplay(commands.Cog)` with `bot`, `enabled: bool`, `server_enabled: bool`, `watchers: Dict[int, NetplayWatcher]`
  - `async Netplay.check_server() -> None` — sets `server_enabled`
  - `async Netplay.resolve_roms(platform: str, game: str) -> Tuple[List[Dict], bool]` — `(roms, truncated)`
  - `Netplay.domain_configured() -> bool`
  - `setup(bot)` in `cogs/netplay/__init__.py`

**Context:** `core_cogs` (bot.py:677-687) is an unconditional list — no core cog is env-gated, and the `{STEM}_ENABLED` convention belongs to `load_integration_cogs`, which this cog deliberately does not use. So `NETPLAY_ENABLED` is enforced inside the cog, not at load time. Every core cog also has an entry in the parallel `cog_dependencies` dict; omitting it is easy to miss.

`DOMAIN` defaults to the literal string `No website configured` (bot.py:309). Unset, the join link would render as `No website configured/rom/50265/ejs`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_netplay_resolution.py`:

```python
"""Turning a platform and a search term into one RomM rom id.

There is no ROM-name autocomplete in this codebase, so resolution is a search
against RomM plus a select. The cases that matter are the boundaries: nothing
found, exactly one (no select at all), and more than a select can hold - which
truncates, and has to say so rather than dropping matches silently.
"""

import unittest
from types import SimpleNamespace

from cogs.netplay.cog import Netplay
from cogs.netplay.views import MAX_SELECT_OPTIONS


def make_cog(rom_payload=None, domain="https://roms.example.com"):
    cog = object.__new__(Netplay)
    cog.bot = SimpleNamespace(
        config=SimpleNamespace(DOMAIN=domain),
        fetch_api_endpoint=_fetcher(rom_payload),
    )
    cog.enabled = True
    cog.server_enabled = True
    cog.watchers = {}
    return cog


def _fetcher(payload):
    async def fetch(endpoint, bypass_cache=False):
        return payload
    return fetch


def roms(count):
    return {"items": [{"id": 1000 + i, "name": f"Game {i}"} for i in range(count)]}


class ResolveRomsTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_matches_returns_empty(self):
        found, truncated = await make_cog(roms(0)).resolve_roms("snes", "nothing")
        self.assertEqual(found, [])
        self.assertFalse(truncated)

    async def test_single_match_is_returned_alone(self):
        found, truncated = await make_cog(roms(1)).resolve_roms("snes", "bomberman")
        self.assertEqual(len(found), 1)
        self.assertFalse(truncated)

    async def test_several_matches_all_returned(self):
        found, truncated = await make_cog(roms(5)).resolve_roms("snes", "bomberman")
        self.assertEqual(len(found), 5)
        self.assertFalse(truncated)

    async def test_exactly_the_cap_is_not_truncated(self):
        found, truncated = await make_cog(roms(MAX_SELECT_OPTIONS)).resolve_roms("snes", "b")
        self.assertEqual(len(found), MAX_SELECT_OPTIONS)
        self.assertFalse(truncated)

    async def test_over_the_cap_truncates_and_says_so(self):
        found, truncated = await make_cog(roms(40)).resolve_roms("snes", "b")
        self.assertEqual(len(found), MAX_SELECT_OPTIONS)
        self.assertTrue(truncated)

    async def test_a_failed_search_is_not_an_empty_one(self):
        found, truncated = await make_cog(None).resolve_roms("snes", "b")
        self.assertIsNone(found)


class DomainGuardTests(unittest.TestCase):
    def test_default_domain_is_rejected(self):
        """bot.py defaults DOMAIN to this sentence; it must not reach a URL."""
        cog = make_cog(roms(1), domain="No website configured")
        self.assertFalse(cog.domain_configured())

    def test_real_domain_is_accepted(self):
        self.assertTrue(make_cog(roms(1)).domain_configured())

    def test_empty_domain_is_rejected(self):
        self.assertFalse(make_cog(roms(1), domain="").domain_configured())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_netplay_resolution.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cogs.netplay.cog'`

- [ ] **Step 3: Add config to `bot.py`**

In `Config.__init__`, after the `AUTO_REGISTER_ROLE_ID` line, add:

```python
        # Netplay announcements. The cog is always loaded (core_cogs is an
        # unconditional list); NETPLAY_ENABLED is enforced inside it, which is
        # also where the server's own EJS_NETPLAY_ENABLED is checked.
        self.NETPLAY_ENABLED = self.parse_bool(os.getenv('NETPLAY_ENABLED', 'true'), True)
        self.NETPLAY_POLL_INTERVAL = int(os.getenv('NETPLAY_POLL_INTERVAL', '20'))
        self.NETPLAY_PENDING_TIMEOUT = int(os.getenv('NETPLAY_PENDING_TIMEOUT', '900'))
        self.NETPLAY_MAX_WATCHERS = int(os.getenv('NETPLAY_MAX_WATCHERS', '25'))
```

- [ ] **Step 4: Register the cog in `bot.py`**

Add `'cogs.netplay',` to the `core_cogs` list (after `'cogs.achievements'`), and add the matching dependency entry to `cog_dependencies`:

```python
            'cogs.netplay': ['aiohttp']
```

- [ ] **Step 5: Write the cog skeleton**

Create `cogs/netplay/cog.py`:

```python
"""The /netplay command and the loop that keeps its posts current.

RomM's netplay list endpoint takes one game_id and has no enumerate-all form
(omitting it is a 422), so this cog polls exactly the ROMs that have a live
Discord post. That bounds the poll set to things someone is watching, and
every watcher reaches a terminal state and drops itself.

NETPLAY_ENABLED is enforced here rather than at load time because core_cogs
is an unconditional list - the {STEM}_ENABLED convention belongs to
load_integration_cogs, which this cog is deliberately not part of.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from .views import MAX_SELECT_OPTIONS
from .watcher import NetplayWatcher

logger = logging.getLogger(__name__)

# bot.py's default when DOMAIN is unset. It is a sentence, not a URL, and it
# must never end up inside a join link.
UNSET_DOMAIN = "No website configured"

ROM_SEARCH_LIMIT = 100


class Netplay(commands.Cog):
    """Announce netplay sessions and keep the announcements current."""

    def __init__(self, bot):
        self.bot = bot
        self.enabled = bot.config.NETPLAY_ENABLED
        self.server_enabled = False
        self.watchers: Dict[int, NetplayWatcher] = {}

        if not self.enabled:
            logger.info("Netplay disabled (NETPLAY_ENABLED=false)")

    # ------------------------------------------------------------- readiness

    def domain_configured(self) -> bool:
        """Whether DOMAIN is something we can build a join link out of."""
        domain = (self.bot.config.DOMAIN or "").strip()
        return bool(domain) and domain != UNSET_DOMAIN

    async def check_server(self) -> None:
        """Ask RomM once whether netplay is on, and warn about ICE servers.

        Fails open: a probe that cannot reach RomM (it is still starting, say)
        leaves the feature enabled rather than silently disabling it until the
        next bot restart.
        """
        config = await self.bot.romm.get_server_config()

        if config is None:
            logger.warning(
                "Could not read RomM config to check netplay support; leaving "
                "netplay enabled. Commands will fail if the server has it off."
            )
            self.server_enabled = True
            return

        self.server_enabled = bool(config.get("EJS_NETPLAY_ENABLED"))

        if not self.server_enabled:
            logger.warning(
                "RomM reports netplay disabled (emulatorjs.netplay.enabled in "
                "config.yml). /netplay will load but refuse to run."
            )
            return

        if not config.get("EJS_NETPLAY_ICE_SERVERS"):
            logger.warning(
                "RomM netplay has no ICE servers configured. WebRTC will use "
                "host candidates only: LAN play works, remote players will "
                "generally fail to connect."
            )

        logger.info("✅ RomM netplay available")

    @commands.Cog.listener()
    async def on_ready(self):
        if self.enabled and not self.server_enabled:
            await self.check_server()

    # ------------------------------------------------------------ resolution

    async def resolve_roms(
        self, platform: str, game: str
    ) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
        """Search RomM for ROMs matching `game` on `platform`.

        Returns (roms, truncated). `roms` is None when the search itself
        failed, which is not the same as finding nothing. Truncation is
        reported rather than hidden: Discord caps a select at 25 options and
        the caller has to tell the user when matches were dropped.
        """
        platform_id, _ = await self.bot.find_platform_by_name(
            platform, await self.bot.fetch_api_endpoint('platforms')
        )
        if not platform_id:
            return [], False

        payload = await self.bot.fetch_api_endpoint(
            f'roms?platform_id={platform_id}&search_term={game}&limit={ROM_SEARCH_LIMIT}',
            bypass_cache=True,
        )
        if payload is None:
            return None, False

        items = payload.get('items', payload if isinstance(payload, list) else [])
        truncated = len(items) > MAX_SELECT_OPTIONS
        return list(items[:MAX_SELECT_OPTIONS]), truncated
```

> `find_platform_by_name` already exists on the bot and is what `cogs/search.py` uses; `resolve_roms` returning `([], False)` for an unknown platform lets the command give a platform-specific error.

- [ ] **Step 6: Add `setup()` to the package**

Append to `cogs/netplay/__init__.py`:

```python
from .cog import Netplay  # noqa: E402  (after the pure modules, to avoid a cycle)

__all__.append("Netplay")


def setup(bot):
    bot.add_cog(Netplay(bot))
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python -m pytest tests/test_netplay_resolution.py -v`
Expected: PASS (9 tests)

- [ ] **Step 8: Run the whole suite — the extension test now covers this cog**

Run: `python -m pytest -q`
Expected: PASS. `tests/test_extension_loading.py` reads `core_cogs` out of `bot.py` by AST, so `cogs.netplay` is now asserted to import and expose a callable `setup()`.

- [ ] **Step 9: Lint**

Run: `python -m ruff check bot.py cogs/netplay tests/test_netplay_resolution.py`
Expected: clean

- [ ] **Step 10: Commit**

```bash
git add bot.py cogs/netplay/cog.py cogs/netplay/__init__.py tests/test_netplay_resolution.py
git commit -m "feat(netplay): config, server probe and ROM resolution

NETPLAY_ENABLED is enforced inside the cog, not at load: core_cogs is an
unconditional list and the {STEM}_ENABLED convention belongs to
load_integration_cogs, which this is deliberately not part of. The parallel
cog_dependencies entry is easy to miss, so it goes in with the registration.

The server probe fails open. A bot that boots while RomM is still starting
would otherwise disable netplay until the next restart, which is a worse
outcome than a command that errors once.

DOMAIN defaults to the literal string 'No website configured', so
domain_configured() guards it before it can be pasted into a join URL."
```

---

## Task 6: The `/netplay` command

**Files:**
- Modify: `cogs/netplay/cog.py` (add the command)
- Test: `tests/test_netplay_cog.py` (create)

**Interfaces:**
- Consumes: `resolve_roms`, `domain_configured` (Task 5); `RomSelectView` (Task 4); `NetplayWatcher` (Task 2); `build_netplay_embed` (Task 3)
- Produces:
  - `Netplay.netplay` slash command
  - `Netplay.register_watcher(rom, ctx) -> NetplayWatcher`
  - `Netplay.platform_autocomplete` (reuses `platforms_repo.search_for_autocomplete`)

**Context:** The command mirrors `/search`'s shape — a `platform` autocomplete plus a free-text `game` — because that is the proven pattern in this codebase and there is no ROM-name autocomplete to do better with.

- [ ] **Step 1: Write the failing test**

Create `tests/test_netplay_cog.py`:

```python
"""Registering a watcher, and the guards that run before one is created.

The command itself needs a live interaction, so these test the pieces it
delegates to: the watcher cap, and that a registered watcher starts PENDING
pointed at the right ROM.
"""

import unittest
from types import SimpleNamespace

from cogs.netplay.cog import Netplay
from cogs.netplay.watcher import NetplayState


def make_cog(max_watchers=25):
    cog = object.__new__(Netplay)
    cog.bot = SimpleNamespace(
        config=SimpleNamespace(
            DOMAIN="https://roms.example.com",
            NETPLAY_MAX_WATCHERS=max_watchers,
            NETPLAY_PENDING_TIMEOUT=900,
        )
    )
    cog.enabled = True
    cog.server_enabled = True
    cog.watchers = {}
    return cog


ROM = {"id": 50265, "name": "Super Bomberman",
       "platform_display_name": "Super Nintendo Entertainment System"}


class RegisterWatcherTests(unittest.TestCase):
    def test_new_watcher_starts_pending(self):
        cog = make_cog()
        watcher = cog.register_watcher(ROM, requester_id=7, channel_id=9)
        self.assertIs(watcher.state, NetplayState.PENDING)
        self.assertEqual(watcher.rom_id, 50265)
        self.assertEqual(watcher.rom_name, "Super Bomberman")

    def test_watcher_is_stored_by_rom_id(self):
        cog = make_cog()
        cog.register_watcher(ROM, requester_id=7, channel_id=9)
        self.assertIn(50265, cog.watchers)

    def test_at_capacity_is_reported(self):
        cog = make_cog(max_watchers=1)
        cog.register_watcher(ROM, requester_id=7, channel_id=9)
        self.assertTrue(cog.at_capacity())

    def test_below_capacity_is_not(self):
        self.assertFalse(make_cog(max_watchers=1).at_capacity())

    def test_re_announcing_the_same_rom_reuses_the_slot(self):
        """Two posts polling one rom id would double the request rate."""
        cog = make_cog()
        first = cog.register_watcher(ROM, requester_id=7, channel_id=9)
        second = cog.register_watcher(ROM, requester_id=8, channel_id=9)
        self.assertEqual(len(cog.watchers), 1)
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_netplay_cog.py -v`
Expected: FAIL — `AttributeError: 'Netplay' object has no attribute 'register_watcher'`

- [ ] **Step 3: Add the registry helpers and the command**

Append to the `Netplay` class in `cogs/netplay/cog.py`:

```python
    # -------------------------------------------------------------- registry

    def at_capacity(self) -> bool:
        """Whether the poll set is full.

        The cap exists because every watcher costs a request per tick against
        RomM, and RommClient has no working client-side throttle - RateLimit
        is instantiated but acquire() is never called.
        """
        return len(self.watchers) >= self.bot.config.NETPLAY_MAX_WATCHERS

    def register_watcher(
        self, rom: Dict[str, Any], requester_id: int, channel_id: int
    ) -> NetplayWatcher:
        """Start tracking one ROM. Keyed by rom id, so re-announcing replaces.

        One watcher per ROM rather than per post: two posts for the same game
        would poll the same endpoint twice a tick for identical answers.
        """
        watcher = NetplayWatcher(
            rom_id=int(rom["id"]),
            rom_name=str(rom.get("name") or rom.get("fs_name") or "Unknown"),
            platform_name=str(rom.get("platform_display_name") or ""),
            requester_id=requester_id,
            channel_id=channel_id,
            created_at=time.time(),
        )
        self.watchers[watcher.rom_id] = watcher
        return watcher

    # --------------------------------------------------------------- command

    async def platform_autocomplete(self, ctx: discord.AutocompleteContext):
        """Same platform list the request flow offers."""
        try:
            requests_cog = self.bot.get_cog('Request')
            if requests_cog is None:
                return []
            results = await requests_cog.platforms_repo.search_for_autocomplete(ctx.value)
            return [name for name, _ in results] if results else []
        except Exception as e:
            logger.error(f"Error in netplay platform autocomplete: {e}")
            return []

    @discord.slash_command(name="netplay", description="Announce a netplay session for a game")
    async def netplay(
        self,
        ctx: discord.ApplicationContext,
        platform: discord.Option(str, "Platform the game is on", required=True,
                                 autocomplete=platform_autocomplete),
        game: discord.Option(str, "Game to play", required=True),
    ):
        """Post an announcement and start tracking the session."""
        await ctx.defer()

        if not self.enabled:
            await ctx.respond("Netplay is disabled on this bot.")
            return

        if not self.server_enabled:
            await ctx.respond(
                "This RomM server has netplay turned off. An admin needs to set "
                "`emulatorjs.netplay.enabled: true` in RomM's config.yml."
            )
            return

        if not self.domain_configured():
            await ctx.respond(
                "No public RomM URL is configured, so I cannot build a join "
                "link. An admin needs to set the `DOMAIN` environment variable."
            )
            return

        if self.at_capacity():
            await ctx.respond(
                "I am already tracking as many netplay sessions as I can. "
                "Wait for one to finish and try again."
            )
            return

        roms, truncated = await self.resolve_roms(platform, game)

        if roms is None:
            await ctx.respond("Could not reach RomM to search for that game.")
            return

        if not roms:
            await ctx.respond(f"No ROMs on **{platform}** matching **{game}**.")
            return

        if len(roms) == 1:
            await self.announce(ctx, roms[0])
            return

        note = ""
        if truncated:
            note = (
                f"\nShowing the first {MAX_SELECT_OPTIONS} matches — "
                "narrow your search if the one you want is missing."
            )

        view = RomSelectView(roms)
        view.message = await ctx.respond(f"Which one?{note}", view=view)
        await view.wait()

        if view.selected_rom is None:
            return

        await self.announce(ctx, view.selected_rom)

    async def announce(self, ctx: discord.ApplicationContext, rom: Dict[str, Any]) -> None:
        """Post the PENDING embed and register the watcher behind it."""
        watcher = self.register_watcher(
            rom, requester_id=ctx.author.id, channel_id=ctx.channel_id
        )

        embed = self.render(watcher, ctx.author.display_name)
        message = await ctx.send(embed=embed)

        watcher.message_id = message.id
        watcher.last_render_key = render_key(watcher)

    def render(self, watcher: NetplayWatcher, requester_name: str) -> discord.Embed:
        """Build the embed for a watcher's current state."""
        # PlatformEmoji.format() takes a display name and returns that name
        # with its emoji appended ("Nintendo 64 <emoji>"), so this is a whole
        # display string, not a bare emoji.
        platform_display = ""
        if getattr(self.bot, 'platform_emoji', None) and watcher.platform_name:
            platform_display = self.bot.platform_emoji.format(watcher.platform_name)

        return build_netplay_embed(
            watcher,
            domain=self.bot.config.DOMAIN,
            requester_name=requester_name,
            platform_display=platform_display,
        )
```

- [ ] **Step 4: Add the new imports**

At the top of `cogs/netplay/cog.py`, extend the local imports:

```python
from .embeds import build_netplay_embed, render_key
from .views import MAX_SELECT_OPTIONS, RomSelectView
from .watcher import NetplayWatcher
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/test_netplay_cog.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Confirm the command registers**

Run: `python -m pytest tests/test_extension_loading.py -v`
Expected: PASS — py-cord reads the `Option` annotations at decoration time, so a malformed signature fails here rather than at runtime.

- [ ] **Step 7: Lint**

Run: `python -m ruff check cogs/netplay tests/test_netplay_cog.py`
Expected: clean

- [ ] **Step 8: Commit**

```bash
git add cogs/netplay/cog.py tests/test_netplay_cog.py
git commit -m "feat(netplay): the /netplay command

Mirrors /search's shape - a platform autocomplete plus free-text game -
because there is no ROM-name autocomplete in this codebase to do better
with. Over 25 matches the select truncates and the reply says so, rather
than dropping matches silently.

Watchers are keyed by rom id, so announcing the same game twice replaces
rather than doubling the poll rate on one endpoint. Every guard that can
refuse - disabled, server-off, no DOMAIN, at capacity - runs before any
work."
```

---

## Task 7: The poll loop

**Files:**
- Modify: `cogs/netplay/cog.py` (add the loop and lifecycle)
- Test: `tests/test_netplay_cog.py` (extend)

**Interfaces:**
- Consumes: everything above
- Produces:
  - `Netplay.poll_sessions` — a `tasks.loop`
  - `async Netplay.tick(now: float) -> None` — one pass, testable without the loop
  - `Netplay.cog_unload()`

**Context:** Three mechanics are mandatory, not optional:

1. **Iterate a snapshot.** The loop awaits an HTTP call per watcher; a `/netplay` invocation during any of those awaits mutates the registry. Iterating the live dict raises `RuntimeError: dictionary changed size during iteration`, intermittently and under load.
2. **Edit only when `render_key` changed.** At 20s and 25 watchers, editing every tick is 25 edits per 20s into one or two channels — straight into Discord's per-channel edit throttling. This is the most likely production failure of the design.
3. **Set the interval at runtime.** `tasks.loop` fixes its interval at decoration time, so `NETPLAY_POLL_INTERVAL` must be applied via `change_interval` in `before_loop`. Precedent: `bot.py:817`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_netplay_cog.py`, before the `if __name__` block:

```python
class FakeMessage:
    def __init__(self):
        self.edits = 0

    async def edit(self, **kwargs):
        self.edits += 1


class FakeChannel:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, message_id):
        return self._message


def make_polling_cog(poll_results, message=None):
    """A cog whose room polls return canned results, one per tick."""
    cog = make_cog()
    cog._results = list(poll_results)
    message = message or FakeMessage()
    cog._message = message

    async def list_rooms(rom_id):
        return cog._results.pop(0) if cog._results else {}

    cog.bot.romm = SimpleNamespace(list_netplay_rooms=list_rooms)
    cog.bot.get_channel = lambda cid: FakeChannel(message)
    cog.bot.get_user = lambda uid: SimpleNamespace(display_name="alice")
    cog.bot.platform_emoji = None
    return cog


ROOM = {"r1": {"room_name": "Bomberman", "current": 2, "max": 4,
               "player_name": "idiosync", "hasPassword": False}}


class TickTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_room_appearing_edits_the_message(self):
        cog = make_polling_cog([ROOM])
        watcher = cog.register_watcher(ROM, requester_id=7, channel_id=9)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        self.assertEqual(cog._message.edits, 1)
        self.assertIs(watcher.state, NetplayState.LIVE)

    async def test_an_unchanged_poll_does_not_edit(self):
        """Edit suppression: 25 watchers editing every 20s hits Discord limits."""
        cog = make_polling_cog([ROOM, ROOM])
        watcher = cog.register_watcher(ROM, requester_id=7, channel_id=9)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        await cog.tick(now=1020.0)
        self.assertEqual(cog._message.edits, 1)

    async def test_terminal_watchers_are_dropped(self):
        cog = make_polling_cog([ROOM, {}])
        watcher = cog.register_watcher(ROM, requester_id=7, channel_id=9)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        await cog.tick(now=1020.0)
        self.assertEqual(cog.watchers, {})

    async def test_registering_during_a_tick_does_not_raise(self):
        """The loop awaits per watcher; a command can mutate the registry."""
        cog = make_polling_cog([ROOM])
        watcher = cog.register_watcher(ROM, requester_id=7, channel_id=9)
        watcher.message_id = 1

        original = cog.bot.romm.list_netplay_rooms

        async def mutate_then_poll(rom_id):
            cog.register_watcher(
                {"id": 999, "name": "Other"}, requester_id=1, channel_id=9
            )
            return await original(rom_id)

        cog.bot.romm.list_netplay_rooms = mutate_then_poll
        await cog.tick(now=1000.0)  # must not raise RuntimeError
        self.assertIn(999, cog.watchers)

    async def test_a_failed_poll_leaves_the_watcher_alone(self):
        cog = make_polling_cog([None])
        watcher = cog.register_watcher(ROM, requester_id=7, channel_id=9)
        watcher.state = NetplayState.LIVE
        watcher.rooms = ROOM
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        self.assertIs(watcher.state, NetplayState.LIVE)
        self.assertEqual(cog._message.edits, 0)
```

Add `NetplayState` to the existing import at the top of the file if it is not already there.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_netplay_cog.py -v`
Expected: FAIL — `AttributeError: 'Netplay' object has no attribute 'tick'`

- [ ] **Step 3: Write the loop**

Append to the `Netplay` class in `cogs/netplay/cog.py`:

```python
    # ------------------------------------------------------------- poll loop

    async def tick(self, now: float) -> None:
        """One pass over every watcher.

        Iterates a snapshot, not the live dict: each watcher costs an awaited
        HTTP call, and a /netplay invocation during any of those awaits
        mutates the registry. Terminal watchers are collected and removed
        after the pass rather than during it.
        """
        finished = []

        for rom_id, watcher in list(self.watchers.items()):
            rooms = await self.bot.romm.list_netplay_rooms(rom_id)

            if not advance(
                watcher,
                rooms,
                now=now,
                pending_timeout=self.bot.config.NETPLAY_PENDING_TIMEOUT,
            ):
                if watcher.is_terminal:
                    finished.append(rom_id)
                continue

            await self.refresh_message(watcher)

            if watcher.is_terminal:
                finished.append(rom_id)

        for rom_id in finished:
            self.watchers.pop(rom_id, None)

    async def refresh_message(self, watcher: NetplayWatcher) -> None:
        """Re-render one announcement, but only if it would look different.

        render_key is the guard. Without it, 25 watchers on a 20s interval
        issue 25 edits every 20s into a handful of channels, which is the
        fastest way to hit Discord's per-channel edit throttle.
        """
        key = render_key(watcher)
        if key == watcher.last_render_key:
            return

        channel = self.bot.get_channel(watcher.channel_id)
        if channel is None or watcher.message_id is None:
            return

        user = self.bot.get_user(watcher.requester_id)
        requester_name = user.display_name if user else "Someone"

        try:
            message = await channel.fetch_message(watcher.message_id)
            await message.edit(embed=self.render(watcher, requester_name))
            watcher.last_render_key = key
        except discord.NotFound:
            # The post is gone; stop tracking it rather than retrying forever.
            logger.debug(f"Netplay message for rom {watcher.rom_id} was deleted")
            self.watchers.pop(watcher.rom_id, None)
        except discord.HTTPException as e:
            logger.warning(f"Could not update netplay embed for {watcher.rom_id}: {e}")

    @tasks.loop(seconds=20)
    async def poll_sessions(self):
        """Advance every tracked session. Interval set in before_loop."""
        try:
            await self.tick(time.time())
        except Exception as e:
            # A raise here stops the loop permanently, taking every live
            # announcement with it.
            logger.error(f"Netplay poll failed: {e}", exc_info=True)

    @poll_sessions.before_loop
    async def before_poll(self):
        """tasks.loop fixes its interval at decoration time; override it here."""
        await self.bot.wait_until_ready()
        self.poll_sessions.change_interval(seconds=self.bot.config.NETPLAY_POLL_INTERVAL)

    def cog_unload(self):
        self.poll_sessions.cancel()
```

- [ ] **Step 4: Start the loop**

In `Netplay.__init__`, after the `enabled` check, add:

```python
        if self.enabled:
            self.poll_sessions.start()
```

- [ ] **Step 5: Add the remaining imports**

At the top of `cogs/netplay/cog.py`, add `tasks` to the discord.ext import and `advance` to the watcher import:

```python
from discord.ext import commands, tasks

from .watcher import NetplayWatcher, advance
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_netplay_cog.py -v`
Expected: PASS (10 tests)

- [ ] **Step 7: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS, no regressions

- [ ] **Step 8: Lint**

Run: `python -m ruff check .`
Expected: clean

- [ ] **Step 9: Commit**

```bash
git add cogs/netplay/cog.py tests/test_netplay_cog.py
git commit -m "feat(netplay): poll tracked sessions and update their posts

Three things here are load-bearing rather than stylistic. The tick iterates
a snapshot, because it awaits an HTTP call per watcher and a /netplay run
during any of those awaits mutates the registry. It edits only when
render_key changes, because 25 watchers on a 20s interval otherwise issue 25
edits per 20s into a couple of channels. And the interval is applied in
before_loop, because tasks.loop fixes it at decoration time and would
ignore NETPLAY_POLL_INTERVAL.

The loop swallows exceptions: an escape would stop it permanently and take
every live announcement with it."
```

---

## Task 8: Documentation

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: everything
- Produces: user-facing docs

- [ ] **Step 1: Add the command to the command list**

In the `## Available Commands` section, after the `/platforms` line:

```markdown
- `/netplay [platform] [game]` — Announce a netplay session for a game in your library. The bot posts an embed with a join link and keeps it updated as players join and leave, then marks it ended when the session finishes.
```

- [ ] **Step 2: Add a feature bullet**

In `### Current`, after the **Search** bullet:

```markdown
- **Netplay announcements**: Announce a RomM netplay session from Discord. The embed tracks the session live — waiting for a room, who is hosting, how many seats are filled — and links players straight into the browser player. Requires RomM netplay to be enabled server-side.
```

- [ ] **Step 3: Add the configuration section**

After the `## Requests` section, add:

```markdown
## Netplay

<img align="right" width="300" src=".github/screenshots/Netplay.png">

`/netplay [platform] [game]` posts an announcement for a game in your library
and keeps it current: it starts as "waiting for a room", flips to the host and
seat count once someone opens one, and marks itself ended when the session
finishes.

**RomM server prerequisites.** Netplay is a RomM feature, and the bot only
reports it — it cannot turn it on. In RomM's `config.yml`:

```yaml
emulatorjs:
  netplay:
    enabled: true
    ice_servers:
      - urls: "stun:stun.l.google.com:19302"
```

Restart RomM, then confirm with an **authenticated** request — the ICE server
list is redacted to `[]` for anonymous callers, so an unauthenticated check
reports a working server as unconfigured:

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://your-romm/api/config \
  | jq '{EJS_NETPLAY_ENABLED, EJS_NETPLAY_ICE_SERVERS}'
```

Two things worth knowing before enabling it:

- Turning netplay on switches in-browser play to the EmulatorJS `nightly`
  build for **all** users, not just netplay sessions.
- RomM netplay is host-streams-video, not lockstep. The host renders the game
  and uploads a video stream to each guest, so a four-player room means three
  outbound streams. The best host is whoever has the best upload, and STUN
  alone will not connect two players who are both behind symmetric NAT — that
  needs a TURN server.

**Bot configuration** (all optional):

```env
NETPLAY_ENABLED=true
NETPLAY_POLL_INTERVAL=20
NETPLAY_PENDING_TIMEOUT=900
NETPLAY_MAX_WATCHERS=25
```

- `NETPLAY_ENABLED` — enable the `/netplay` command (default: `true`).
- `NETPLAY_POLL_INTERVAL` — seconds between room checks (default: `20`).
- `NETPLAY_PENDING_TIMEOUT` — seconds before an announcement with no room gives up (default: `900`).
- `NETPLAY_MAX_WATCHERS` — how many sessions to track at once (default: `25`). Each one costs one RomM request per interval.

`DOMAIN` must be set to your public RomM URL, or the bot cannot build a join
link and `/netplay` will refuse to run.
```

- [ ] **Step 4: Add the env vars to the sample block**

In the `.env` example under `## Configuration`, after the `GGREQUESTZ_*` lines:

```env
NETPLAY_ENABLED=true
```

- [ ] **Step 5: Verify the docs match the code**

Run: `python -m pytest tests/test_netplay_client.py::ScopeTests -v`
Expected: PASS — confirms `assets.read` is documented and requested.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs(netplay): document the command and its prerequisites

The server-side prerequisites are the part an operator cannot guess: netplay
lives only in RomM's config.yml, enabling it moves everyone to EmulatorJS
nightly, and the verification curl has to be authenticated because RomM
redacts the ICE server list for anonymous callers - which makes a correctly
configured server look unconfigured."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §2.1 endpoint, `None` vs `{}` | 1 |
| §2.4 config + auth redaction | 1, 5 |
| §3 v1 scope | 6, 7 |
| §3 v2 deferrals | not built (correct) |
| §4 file structure | 2-7 |
| §4.1 `views_game.py` not reusable | 4 |
| §5.1 state machine | 2 |
| §5.2 accepted gaps + `ENDED` hint | 2, 3 |
| §5.3 snapshot / edit-on-change / `change_interval` | 7 |
| §5.4 multiple rooms | 2, 3 |
| §5.5 restart | 2 (in-memory registry) |
| §6.1 ROM resolution + truncation | 5, 6 |
| §6.2 embed contents + join link | 3, 6 |
| §6.3 raw `player_name` | 3 |
| §7 client methods | 1 |
| §7.1 v2 db helper | not built (correct) |
| §8 config + scope | 1, 5 |
| §9 failure modes | 5, 6, 7 |
| §10 testing | every task |
| §13 scope-string coordination | 1 (Step 4 note) |

**Gap found and closed:** §6.2 says the embed shows the core from
`EJS_DEFAULT_CORES`. `build_netplay_embed` accepts `core_name` and Task 3
tests it, but Task 6's `render()` never passes it. This is deliberate and
recorded here rather than left silent: `EJS_DEFAULT_CORES` was **empty** on
the reference server, so there is nothing to show and no way to test the
wiring against reality. The parameter exists and is covered; populating it is
a one-line change in `render()` once a server has cores configured.

**Placeholder scan:** none — every step has real code or a real command.

**Type error found and fixed:** an earlier draft had `render()` calling
`bot.platform_emoji.get_emoji(slug)`. No such method exists — the real API is
`PlatformEmoji.format(display_name)` (`cogs/platform_emoji.py:117`), it is keyed
by **display name** rather than slug, and it returns `"Name <emoji>"` rather
than a bare emoji. The watcher field is now `platform_name`, sourced from
RomM's `platform_display_name`, and the embed renders it as its own field.
Exactly the unchecked-string class of bug `tests/test_cog_lookups.py` exists to
catch.

**Type consistency:** `advance()` signature matches between Task 2 and Task 7.
`render_key`/`build_netplay_embed` names match between Tasks 3, 6 and 7.
`resolve_roms` returns `(roms, truncated)` in Tasks 5 and 6.
`MAX_SELECT_OPTIONS` is defined in Task 4 and used in Tasks 5 and 6.
`NetplayWatcher` field names match across Tasks 2, 3, 6 and 7.
