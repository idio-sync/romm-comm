# Scan Socket.IO Dispatcher Implementation Plan (Wave 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the Socket.IO event-handler collision between `cogs/scan.py` and `cogs/recent_roms.py` so the scan-completion summary and `/scan summary` work again, by making the shared `SocketIOManager` the single owner of all socket events and re-broadcasting them as bot events that any cog can subscribe to.

**Architecture:** Today both cogs call `@self.sio.on('scan:done')` / `@self.sio.on('scan:scanning_rom')` (plus `connect`/`disconnect`/`connect_error`) on the *same* shared `python-socketio` client. python-socketio keeps exactly one handler per event name, so whichever cog registers last (recent_roms) silently overrides the other (scan). We move all socket-event handling into `SocketIOManager` (in `bot.py`); each manager handler calls `self.bot.dispatch('romm_<event>', ...)`. Both cogs then subscribe with `@commands.Cog.listener('on_romm_<event>')` — discord/pycord's dispatch supports unlimited listeners per event, so both cogs receive every event. recent_roms remains the sole dispatcher of `batch_scan_complete` (decided with the maintainer); scan.py's redundant dispatch is removed.

**Tech Stack:** Python 3.12, py-cord (Discord), python-socketio (AsyncClient), unittest (`IsolatedAsyncioTestCase`). Tests run with `.venv/Scripts/python.exe -m unittest`.

---

## Design decisions (locked with maintainer)

1. **Central dispatcher** in `SocketIOManager` re-broadcasting bot events (not a cog-to-cog forwarder).
2. **recent_roms keeps `batch_scan_complete`.** scan.py stops dispatching it. Consequence (accepted): auto-fulfill-during-scan runs only when Recent ROMs monitoring is enabled — which is already effectively true today. This also removes the latent `KeyError: 'id'` that scan.py's thin payload would cause when Recent ROMs is disabled, because that dispatch path is deleted rather than fixed.

## Event mapping (single source of truth for all tasks)

| Socket.IO event | `bot.dispatch(...)` | Cog listener method |
| --- | --- | --- |
| `connect` | `romm_connect` | `on_romm_connect(self)` |
| `disconnect` | `romm_disconnect` | `on_romm_disconnect(self)` |
| `connect_error` | `romm_connect_error` | `on_romm_connect_error(self, data)` |
| `scan:scanning_platform` | `romm_scan_platform` | `on_romm_scan_platform(self, data)` |
| `scan:scanning_rom` | `romm_scan_rom` | `on_romm_scan_rom(self, data)` |
| `scan:done` | `romm_scan_done` | `on_romm_scan_done(self, stats)` |
| `scan:done_ko` | `romm_scan_error` | `on_romm_scan_error(self, error_message)` |

## File structure

- **Modify `bot.py`** — `SocketIOManager` gains a bot reference, registers the single set of socket handlers, and re-broadcasts each as a bot event. Constructor call at line 719 updated.
- **Modify `cogs/scan.py`** — delete `setup_socket_handlers()` and its `@self.sio.on`/`@self.sio.event` closures; re-add the same bodies as `@commands.Cog.listener('on_romm_*')` methods; delete the `batch_scan_complete` dispatch; move the re-entry guard into `_start_discord_scan`; remove the now-broken `cog_before_invoke`; `defer` the interaction.
- **Modify `cogs/recent_roms.py`** — delete `setup_socket_handlers()` + `_handlers_registered` machinery and its call in `setup()`; re-add the two handler bodies as `@commands.Cog.listener('on_romm_*')` methods. Keep the `batch_scan_complete` dispatch.
- **Create `tests/test_socketio_dispatch.py`** — unit-tests that `SocketIOManager` re-broadcasts each socket event as the correct bot event.
- **Modify `tests/test_scan.py`** — add `dispatch`/`defer` to the fakes; add listener-behavior tests and a regression test that the cog no longer owns socket handlers.
- **Modify `tests/test_recent_roms.py`** — add a regression test that the monitor no longer owns socket handlers and that its rom-listener queues a ROM.

> **Mechanical-move note:** Tasks 2 and 3 mostly *move existing handler bodies* into listener methods. Where a step says "move the body verbatim from lines X–Y," copy that exact code unchanged (re-indenting as a method) except for the explicit deletions called out. Do not rewrite the logic.

---

### Task 1: Add a bot-event dispatcher to `SocketIOManager`

This task is additive and changes no behavior on its own: the manager's handlers are registered in `setup_hook`, but the cogs still register their own `@self.sio.on` handlers in `on_ready` (later), so the cogs still "win" until Tasks 2–3. Existing behavior is preserved; the new code is exercised by unit tests.

**Files:**
- Modify: `bot.py` (class `SocketIOManager`, ~lines 73–136, and the constructor call at line 719)
- Test: `tests/test_socketio_dispatch.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_socketio_dispatch.py`:

```python
import unittest

from bot import SocketIOManager


class FakeConfig:
    API_BASE_URL = "http://localhost"
    USER = "user"
    PASS = "pass"
    API_TIMEOUT = 30


class FakeBot:
    def __init__(self):
        self.config = FakeConfig()
        self.dispatched = []

    def dispatch(self, event, *args):
        self.dispatched.append((event, args))


class SocketIODispatchTests(unittest.IsolatedAsyncioTestCase):
    def _manager(self):
        bot = FakeBot()
        return bot, SocketIOManager(bot)

    async def test_scan_done_rebroadcasts_as_bot_event(self):
        bot, mgr = self._manager()
        await mgr._on_sio_scan_done({"scanned_roms": 5})
        self.assertIn(("romm_scan_done", ({"scanned_roms": 5},)), bot.dispatched)

    async def test_scan_rom_rebroadcasts_as_bot_event(self):
        bot, mgr = self._manager()
        await mgr._on_sio_scan_rom({"id": 1, "name": "Mega Man"})
        self.assertIn(("romm_scan_rom", ({"id": 1, "name": "Mega Man"},)), bot.dispatched)

    async def test_scan_platform_and_error_rebroadcast(self):
        bot, mgr = self._manager()
        await mgr._on_sio_scan_platform({"name": "SNES"})
        await mgr._on_sio_scan_error("boom")
        events = {e for e, _ in bot.dispatched}
        self.assertIn("romm_scan_platform", events)
        self.assertIn("romm_scan_error", events)

    async def test_connect_disconnect_and_connect_error_rebroadcast(self):
        bot, mgr = self._manager()
        await mgr._on_sio_connect()
        await mgr._on_sio_disconnect()
        await mgr._on_sio_connect_error("nope")
        events = {e for e, _ in bot.dispatched}
        self.assertEqual(
            {"romm_connect", "romm_disconnect", "romm_connect_error"} & events,
            {"romm_connect", "romm_disconnect", "romm_connect_error"},
        )

    async def test_manager_owns_the_socket_handlers(self):
        _, mgr = self._manager()
        registered = mgr.sio.handlers.get("/", {})
        for event in ("connect", "disconnect", "scan:scanning_rom", "scan:done", "scan:done_ko"):
            self.assertIn(event, registered)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m unittest tests.test_socketio_dispatch -v`
Expected: FAIL — `AttributeError: 'SocketIOManager' object has no attribute '_on_sio_scan_done'` (and the constructor still takes `config`, so `SocketIOManager(bot)` stores the bot as `self.config`).

- [ ] **Step 3: Change the `SocketIOManager` constructor to take the bot**

In `bot.py`, change the start of `SocketIOManager.__init__` from:

```python
    def __init__(self, config):
        self.config = config
        self.sio = socketio.AsyncClient(
```

to:

```python
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        self.sio = socketio.AsyncClient(
```

- [ ] **Step 4: Register the single handler set + add the re-broadcast methods**

In `bot.py`, at the END of `SocketIOManager.__init__` (after `self._health_monitor_task = None`), add:

```python
        self._register_event_handlers()

    def _register_event_handlers(self):
        """Register the one-and-only set of Socket.IO handlers and re-broadcast
        each as a bot event. Cogs subscribe via @commands.Cog.listener instead of
        competing for the single-handler-per-event socket client."""
        self.sio.on('connect', self._on_sio_connect)
        self.sio.on('disconnect', self._on_sio_disconnect)
        self.sio.on('connect_error', self._on_sio_connect_error)
        self.sio.on('scan:scanning_platform', self._on_sio_scan_platform)
        self.sio.on('scan:scanning_rom', self._on_sio_scan_rom)
        self.sio.on('scan:done', self._on_sio_scan_done)
        self.sio.on('scan:done_ko', self._on_sio_scan_error)

    async def _on_sio_connect(self):
        logger.debug("Socket.IO connect event; broadcasting romm_connect")
        self.bot.dispatch('romm_connect')

    async def _on_sio_disconnect(self, *args):
        logger.debug("Socket.IO disconnect event; broadcasting romm_disconnect")
        self.bot.dispatch('romm_disconnect')

    async def _on_sio_connect_error(self, *args):
        data = args[0] if args else None
        logger.error(f"Socket.IO connect error: {data}")
        self.bot.dispatch('romm_connect_error', data)

    async def _on_sio_scan_platform(self, data=None):
        self.bot.dispatch('romm_scan_platform', data)

    async def _on_sio_scan_rom(self, data=None):
        self.bot.dispatch('romm_scan_rom', data)

    async def _on_sio_scan_done(self, stats=None):
        self.bot.dispatch('romm_scan_done', stats)

    async def _on_sio_scan_error(self, error_message=None):
        self.bot.dispatch('romm_scan_error', error_message)
```

(The `def _register_event_handlers` line replaces nothing — it begins immediately after the new `self._register_event_handlers()` call that you appended to `__init__`.)

- [ ] **Step 5: Update the constructor call**

In `bot.py` line 719, change:

```python
                self.socketio_manager = SocketIOManager(self.config)
```

to:

```python
                self.socketio_manager = SocketIOManager(self)
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m unittest tests.test_socketio_dispatch -v`
Expected: PASS (all 5 tests).

- [ ] **Step 7: Run the full suite to confirm no regression**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"`
Expected: OK — all prior tests pass plus the 5 new dispatch tests. (Treat absolute counts as informational; the pass/fail signal is "no failures, no decrease.")

- [ ] **Step 8: Commit**

```bash
git add bot.py tests/test_socketio_dispatch.py
git commit -m "feat(scan): add SocketIOManager bot-event dispatcher"
```

---

### Task 2: Migrate the Scan cog onto bot-event listeners

Convert every `@self.sio.on(...)` / `@self.sio.event` closure inside `setup_socket_handlers` (scan.py:73–353) into a `@commands.Cog.listener('on_romm_*')` method with the **same body**, delete the wrapper and the `setup_socket_handlers()` call, and delete scan.py's `batch_scan_complete` dispatch. After this task, scan's listeners are defined but still dormant for the events recent_roms overrides (fixed in Task 3) — no regression vs today.

**Files:**
- Modify: `cogs/scan.py` (delete lines 57 call + 73–353 wrapper; remove dispatch at 316–318; add listener methods)
- Test: `tests/test_scan.py` (add fakes + behavior/regression tests)

- [ ] **Step 1: Write the failing tests**

In `tests/test_scan.py`, add `dispatch` + `defer` support to the fakes and new tests. Add these fields to `FakeBot.__init__` (after `self.emoji_dict = {}`):

```python
        self.dispatched = []

    def dispatch(self, event, *args):
        self.dispatched.append((event, args))
```

Replace `FakeChannel` with a send-recording version:

```python
class FakeChannel:
    id = 1234

    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
```

Update `FakeContext.__init__` to build the channel and support defer:

```python
class FakeContext:
    def __init__(self):
        self.channel = FakeChannel()
        self.responses = []
        self.deferred = False

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))
```

Then add a new test class at the end of the file:

```python
class ScanListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_done_listener_posts_summary_and_stores_stats(self):
        bot = FakeBot()
        scan = Scan(bot)
        channel = FakeChannel()
        scan.last_channel = channel
        scan.external_scan = False
        scan.is_scanning = True
        scan.scan_start_time = datetime.now()
        scan.scan_progress = {"total_roms": 3}

        await scan.on_romm_scan_done({"scanned_roms": 3, "added_roms": 1})

        self.assertEqual(scan.last_scan_stats["scanned_roms"], 3)
        self.assertTrue(channel.sent)
        self.assertFalse(scan.is_scanning)

    async def test_scan_done_listener_does_not_dispatch_batch_complete(self):
        bot = FakeBot()
        scan = Scan(bot)
        scan.last_channel = None
        scan.is_scanning = True
        scan.scan_start_time = datetime.now()
        scan.scan_progress = {"total_roms": 1}
        scan.new_games = [{"id": 7, "platform": "SNES", "name": "X"}]

        await scan.on_romm_scan_done({"scanned_roms": 1})

        self.assertEqual([], [e for e, _ in bot.dispatched if e == "batch_scan_complete"])

    async def test_scan_rom_listener_tracks_new_games(self):
        bot = FakeBot()
        scan = Scan(bot)
        scan.is_scanning = True
        scan.external_scan = False
        scan._first_event_received = True
        scan.scan_progress = {"current_platform": "SNES", "total_roms": 0,
                              "scanned_roms": 0, "platform_roms": 0}
        scan.new_games = []

        await scan.on_romm_scan_rom({"name": "Chrono Trigger", "is_new": True,
                                     "file_name": "ct.sfc"})

        self.assertEqual(1, len(scan.new_games))
        self.assertEqual("Chrono Trigger", scan.new_games[0]["name"])

    def test_scan_cog_no_longer_owns_socket_handlers(self):
        self.assertFalse(hasattr(Scan, "setup_socket_handlers"))
```

Add `from datetime import datetime` to the imports at the top of `tests/test_scan.py`.

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scan.ScanListenerTests -v`
Expected: FAIL — `AttributeError: 'Scan' object has no attribute 'on_romm_scan_done'`, and `test_scan_cog_no_longer_owns_socket_handlers` fails because `setup_socket_handlers` still exists.

- [ ] **Step 3: Remove the socket-handler registration from `__init__`**

In `cogs/scan.py`, delete line 57 (`        self.setup_socket_handlers()`). Leave `self.sio = bot.socketio_manager.sio` at line 43 (still used by `_scan_stop`/`_start_discord_scan` for `self.sio.emit(...)`).

- [ ] **Step 4: Convert the closures to listener methods**

Replace the entire `def setup_socket_handlers(self):` method (scan.py:73–353) with the following listener methods. **Each body is the current closure body, moved verbatim**, except `on_romm_scan_done` which drops the `batch_scan_complete` dispatch.

```python
    @commands.Cog.listener('on_romm_connect')
    async def on_romm_connect(self):
        # MOVE VERBATIM: body of the old `connect()` closure (scan.py ~76–80):
        #   logger.info("Connected to websocket server")
        #   self._first_event_received = False
        #   self._scan_initiated_externally = False
        ...

    @commands.Cog.listener('on_romm_connect_error')
    async def on_romm_connect_error(self, error):
        # MOVE VERBATIM: body of old `connect_error(error)` closure (scan.py ~83–85):
        #   logger.error(f"Failed to connect to websocket: {error}")
        #   await self._handle_connection_error(error)
        ...

    @commands.Cog.listener('on_romm_disconnect')
    async def on_romm_disconnect(self):
        # MOVE VERBATIM: body of old `disconnect()` closure (scan.py ~89–101)
        ...

    @commands.Cog.listener('on_romm_scan_platform')
    async def on_romm_scan_platform(self, data):
        # MOVE VERBATIM: body of old `on_scanning_platform(data)` closure (scan.py ~105–158)
        ...

    @commands.Cog.listener('on_romm_scan_rom')
    async def on_romm_scan_rom(self, data):
        # MOVE VERBATIM: body of old `on_scanning_rom(data)` closure (scan.py ~162–217)
        ...

    @commands.Cog.listener('on_romm_scan_done')
    async def on_romm_scan_done(self, stats):
        # MOVE VERBATIM: body of old `on_scan_complete(stats)` closure (scan.py ~221–325)
        # EXCEPT delete these three lines (the dispatch — recent_roms now owns it):
        #     if self.new_games:
        #         logger.info(f"Dispatching batch_scan_complete with {len(self.new_games)} new games")
        #         self.bot.dispatch('batch_scan_complete', self.new_games)
        ...

    @commands.Cog.listener('on_romm_scan_error')
    async def on_romm_scan_error(self, error_message):
        # MOVE VERBATIM: body of old `on_scan_error(error_message)` closure (scan.py ~328–353)
        ...
```

The helper methods `_reset_scan_state`, `_clear_shared_scan_state`, `_start_discord_scan`, `_handle_connection_error` (scan.py:355–430) are unchanged and remain in the class.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scan -v`
Expected: PASS (the 3 original `ScanStartupTests` + the 4 new `ScanListenerTests`).

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"`
Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add cogs/scan.py tests/test_scan.py
git commit -m "refactor(scan): consume romm_* bot events; drop duplicate batch dispatch"
```

---

### Task 3: Migrate RecentRomsMonitor onto bot-event listeners (completes the fix)

After this task no cog registers socket handlers; the manager is the sole owner, so both cogs receive every event and the collision is gone.

**Files:**
- Modify: `cogs/recent_roms.py` (delete `_handlers_registered` flag init at line 72; delete `setup_socket_handlers()` at 78–228 and its call at 263; add two listener methods; keep `batch_scan_complete` dispatch)
- Test: `tests/test_recent_roms.py` (regression + rom-listener test)

- [ ] **Step 1: Write the failing tests**

While here, harmonize the existing `FakeBot.dispatch` in `tests/test_recent_roms.py` from `def dispatch(self, name, payload)` to `def dispatch(self, name, *args)` (and store `(name, args)`) so it can't `TypeError` if a future test makes the recent_roms listener dispatch with multiple args. No current test reads `.dispatched`, so this is safe.

Then add a new test class:

```python
class RecentRomsListenerTests(unittest.IsolatedAsyncioTestCase):
    def test_monitor_no_longer_owns_socket_handlers(self):
        self.assertFalse(hasattr(RecentRomsMonitor, "setup_socket_handlers"))

    async def test_scan_rom_listener_queues_rom(self):
        monitor = object.__new__(RecentRomsMonitor)
        monitor.bot = FakeBot(FakeChannel([]))
        # FakeBot.__init__ provides scan_state_lock (required by the handler body).
        monitor.bot.scan_state = {"is_scanning": True}
        monitor.scan_lock = asyncio.Lock()
        monitor.processing_lock = asyncio.Lock()
        monitor.current_scan_roms = []
        monitor.current_scan_names = set()
        monitor.currently_processing = set()
        monitor.recently_processed = set()
        monitor.scan_completion_timer = None

        async def has_been_posted(rom_id):
            return False

        monitor.has_been_posted = has_been_posted

        await monitor.on_romm_scan_rom({"id": 42, "name": "Earthbound",
                                        "platform_name": "SNES"})

        self.assertEqual(1, len(monitor.current_scan_roms))
        self.assertEqual(42, monitor.current_scan_roms[0]["id"])
        # Cancel the inactivity timer the handler started so the test loop is clean
        if monitor.scan_completion_timer:
            monitor.scan_completion_timer.cancel()
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m unittest tests.test_recent_roms.RecentRomsListenerTests -v`
Expected: FAIL — `setup_socket_handlers` still exists; `on_romm_scan_rom` not defined.

- [ ] **Step 3: Remove the registration machinery**

In `cogs/recent_roms.py`:
- Delete line 72: `        self._handlers_registered = False`
- Delete line 263 (the `            self.setup_socket_handlers()` call inside `setup()`), and its preceding comment line 262 (`            # Setup socket handlers`).

- [ ] **Step 4: Convert the two closures to listener methods**

Replace the entire `def setup_socket_handlers(self):` method (recent_roms.py:78–228) — including the `_handlers_registered` guard at 81–85 and the `connect`/`disconnect`/`connect_error` closures — with these listener methods. **Move bodies verbatim**; the `_handlers_registered` early-return guard (lines 81–85) is **deleted** (no longer needed — listeners register once when the cog loads).

```python
    @commands.Cog.listener('on_romm_connect')
    async def on_romm_connect(self):
        # MOVE VERBATIM: body of old `connect()` closure (recent_roms.py ~90–95)
        ...

    @commands.Cog.listener('on_romm_disconnect')
    async def on_romm_disconnect(self):
        # MOVE VERBATIM: body of old `disconnect()` closure (recent_roms.py ~100–108)
        ...

    @commands.Cog.listener('on_romm_connect_error')
    async def on_romm_connect_error(self, data):
        # MOVE VERBATIM: body of old `connect_error(data)` closure (recent_roms.py ~112–113):
        #   logger.error(f"Socket.IO connection error: {data}")
        ...

    @commands.Cog.listener('on_romm_scan_rom')
    async def on_romm_scan_rom(self, data):
        # MOVE VERBATIM: body of old `on_scanning_rom(data)` closure (recent_roms.py ~118–194)
        ...

    @commands.Cog.listener('on_romm_scan_done')
    async def on_romm_scan_done(self, stats):
        # MOVE VERBATIM: body of old `on_scan_done(stats)` closure (recent_roms.py ~199–228).
        # NOTE: this body has NO batch_scan_complete dispatch of its own — the dispatch lives
        # in process_scan_batch (line 596) and is left untouched. Just move the batching logic
        # (it spawns handle_scan_complete, which eventually reaches process_scan_batch).
        ...
```

Everything else in recent_roms.py (the `_trigger_batch_processing_after_delay`, `setup`, `process_scan_batch`, etc.) is unchanged. Note `process_scan_batch` still dispatches `batch_scan_complete` (recent_roms.py:596) — leave it.

- [ ] **Step 5: Run the tests**

Run: `.venv/Scripts/python.exe -m unittest tests.test_recent_roms -v`
Expected: PASS (original 2 `RecentRomsPostingTests` + 2 new `RecentRomsListenerTests`).

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"`
Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add cogs/recent_roms.py tests/test_recent_roms.py
git commit -m "refactor(recent_roms): consume romm_* bot events; remove socket collision"
```

---

### Task 4: Make the scan re-entry guard actually block (replace broken `cog_before_invoke`)

`cog_before_invoke` returns `False` to block a second scan, but py-cord ignores the return value, so the command body still runs. All six scan-START subcommands funnel through `_start_discord_scan` (verified: `_scan_platform`, `_scan_full`, `_scan_unidentified`, `_scan_hashes`, `_scan_new_platforms`, `_scan_partial`), while `status`/`stop`/`summary` do not — so the guard belongs at the top of `_start_discord_scan`.

**Files:**
- Modify: `cogs/scan.py` (delete `cog_before_invoke` at 62–71; add guard at top of `_start_discord_scan`)
- Test: `tests/test_scan.py`

- [ ] **Step 1: Write the failing test**

Add to `ScanStartupTests` in `tests/test_scan.py`:

```python
    async def test_start_scan_is_blocked_when_already_scanning(self):
        bot = FakeBot(connect_result=True)
        scan = Scan(bot)
        scan.is_scanning = True
        ctx = FakeContext()

        result = await scan._start_discord_scan(
            ctx, scan_type="complete", options={}, progress={},
            response_message="should not send",
        )

        self.assertFalse(result)
        self.assertEqual(0, bot.socketio_manager.connect_calls)
        self.assertEqual([], bot.sio.emitted)
        self.assertIn("already in progress", ctx.responses[0][0][0])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scan.ScanStartupTests.test_start_scan_is_blocked_when_already_scanning -v`
Expected: FAIL — the guard doesn't exist, so it tries to connect (`connect_calls == 1`).

- [ ] **Step 3: Delete the broken hook**

In `cogs/scan.py`, delete the whole `cog_before_invoke` method (lines 62–71).

- [ ] **Step 4: Add the guard at the choke point**

In `cogs/scan.py`, at the very top of `_start_discord_scan` (immediately after the docstring, before `connected = await self.bot.socketio_manager.connect()`), insert:

```python
        if self.is_scanning:
            await ctx.respond(
                "❌ A scan is already in progress. Use `/scan status` to check progress "
                "or `/scan stop` to stop it."
            )
            return False
```

- [ ] **Step 5: Run the test**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scan.ScanStartupTests -v`
Expected: PASS (all four).

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"`
Expected: OK.

- [ ] **Step 7: Commit**

```bash
git add cogs/scan.py tests/test_scan.py
git commit -m "fix(scan): block re-entrant scans at the start choke point"
```

---

### Task 5: Defer the scan interaction to survive the 3-second ack deadline

`_scan_platform` and `_start_discord_scan` do network I/O (platform fetch + socket connect) before the first `ctx.respond`. If that exceeds ~3s the interaction token expires and the response 404s. Deferring up front acks immediately; subsequent `ctx.respond` calls become followups in py-cord.

**Files:**
- Modify: `cogs/scan.py` (`scan` command body, ~line 482)
- Test: `tests/test_scan.py`

- [ ] **Step 1: Write the failing test**

Add a new test class to `tests/test_scan.py`:

```python
class ScanDeferTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_command_defers_before_dispatching(self):
        bot = FakeBot(connect_result=True)
        scan = Scan(bot)
        ctx = FakeContext()

        # Call the raw callback (bypasses the @is_admin check) with a no-network subcommand
        await Scan.scan.callback(scan, ctx, command="summary", platform=None)

        self.assertTrue(ctx.deferred)
```

(`ctx.defer`/`ctx.deferred` were added to `FakeContext` in Task 2.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scan.ScanDeferTests -v`
Expected: FAIL — `ctx.deferred` is `False` (no defer yet).

- [ ] **Step 3: Add the defer**

In `cogs/scan.py`, in the `scan` command, insert `await ctx.defer()` as the first statement of the body, immediately after the docstring `"""Scan ROMs and fetch metadata from various sources"""` and before `command = command.lower()`:

```python
        await ctx.defer()
        command = command.lower()
```

> Note: `ctx.defer()` defaults to `invisible=True` (an ephemeral-style "thinking" state); the subsequent `ctx.respond(...)` followups still post the visible "🔍 Started…" message. This is acceptable. If the maintainer wants a visible "Bot is thinking…" indicator, use `await ctx.defer(ephemeral=False)`. Confirm the indicator behaves acceptably during live verification.

- [ ] **Step 4: Run the test**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scan.ScanDeferTests -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"`
Expected: OK.

- [ ] **Step 6: Commit**

```bash
git add cogs/scan.py tests/test_scan.py
git commit -m "fix(scan): defer the interaction before slow scan setup"
```

---

## Post-implementation manual verification (requires a live RomM instance)

Automated tests cover the dispatch wiring and listener behavior, but the end-to-end socket path can only be confirmed against a running RomM server. After Tasks 1–5:

1. Start the bot against a real RomM instance with `RECENT_ROMS_ENABLED=true`.
2. Run `/scan full` (or `/scan platform <name>`). Confirm:
   - The "🔍 Started…" ack appears immediately (defer working).
   - Recently-added notifications still post (recent_roms listener alive).
   - **The scan-completion summary now posts** (scan listener alive — the bug this fixes).
   - `/scan summary` shows the just-completed scan's stats (no longer empty).
3. While a scan runs, run `/scan full` again → "already in progress" and no second scan starts. Run `/scan status` → shows progress. Run `/scan stop` → stops and `/scan status` then reports no scan running.
4. If any pending requests match scanned games, confirm exactly **one** auto-fulfill DM per user (no duplicates from a revived second dispatcher).

## Out of scope (track as Wave 3)

- Scan-state TOCTOU: two near-simultaneous `/scan` starts can both pass the unlocked `is_scanning` guard (finding #5). The guard added in Task 4 matches today's behavior; making it atomic needs a lock+reservation.
- Disconnect/transient-reconnect mis-detecting a user scan as external (finding #8).
- Auto-fulfill fuzzy-match false positives at the 0.8 threshold (finding #10).

---

## Self-review

- **Spec coverage:** Collision fix (Tasks 1–3) ✓; `cog_before_invoke` → real abort (Task 4) ✓; `ctx.defer()` (Task 5) ✓; payload-schema issue resolved by deleting scan.py's thin dispatch per the locked decision (Task 2) ✓; recent_roms keeps `batch_scan_complete` ✓.
- **Type/name consistency:** Bot event names (`romm_connect`, `romm_disconnect`, `romm_connect_error`, `romm_scan_platform`, `romm_scan_rom`, `romm_scan_done`, `romm_scan_error`) match between the manager's `dispatch` calls (Task 1) and the `@commands.Cog.listener('on_romm_*')` names (Tasks 2–3) per the mapping table. Manager handler names (`_on_sio_*`) are internal and only referenced within `bot.py`.
- **Ordering:** Tasks 1→2→3 are sequential (the cutover); the system never regresses below today's behavior in the intermediate states. Tasks 4 and 5 are independent and may run in either order after Task 1.
- **Placeholder scan:** The only `...` markers are explicit "move this existing block verbatim" instructions with exact line references for a mechanical refactor; all new code (manager, tests, guard, defer) is shown in full.
