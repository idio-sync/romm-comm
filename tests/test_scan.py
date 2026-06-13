import asyncio
import unittest
from datetime import datetime

from cogs.scan import Scan


class FakeSIO:
    def __init__(self, bot=None):
        self.bot = bot
        self.emitted = []
        self.emit_error = None
        self.state_seen_during_emit = None

    def event(self, handler):
        return handler

    def on(self, event_name):
        def decorator(handler):
            return handler

        return decorator

    async def emit(self, event_name, data=None):
        if self.bot:
            self.state_seen_during_emit = self.bot.scan_state.copy()
        if self.emit_error:
            raise self.emit_error
        self.emitted.append((event_name, data))


class FakeSocketIOManager:
    def __init__(self, sio, connect_result=True):
        self.sio = sio
        self.connect_result = connect_result
        self.connect_calls = 0

    async def connect(self):
        self.connect_calls += 1
        return self.connect_result


class FakeConfig:
    pass


class FakeBot:
    def __init__(self, connect_result=True):
        self.scan_state = {
            "is_scanning": False,
            "scan_start_time": None,
            "initiated_by": None,
            "scan_type": None,
            "channel_id": None,
        }
        self.scan_state_lock = asyncio.Lock()
        self.config = FakeConfig()
        self.sio = FakeSIO(self)
        self.socketio_manager = FakeSocketIOManager(self.sio, connect_result)
        self.emoji_dict = {}
        self.dispatched = []

    def dispatch(self, event, *args):
        self.dispatched.append((event, args))


class FakeChannel:
    id = 1234

    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeContext:
    def __init__(self):
        self.channel = FakeChannel()
        self.responses = []
        self.deferred = False

    async def defer(self, *args, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))


class ScanStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_scan_does_not_mark_scanning_when_socket_connection_fails(self):
        bot = FakeBot(connect_result=False)
        scan = Scan(bot)
        ctx = FakeContext()

        await scan._scan_full(ctx)

        self.assertFalse(scan.is_scanning)
        self.assertFalse(bot.scan_state["is_scanning"])
        self.assertEqual([], bot.sio.emitted)
        self.assertIn("Unable to connect", ctx.responses[0][0][0])

    async def test_full_scan_resets_state_when_emit_fails(self):
        bot = FakeBot(connect_result=True)
        bot.sio.emit_error = RuntimeError("socket write failed")
        scan = Scan(bot)
        ctx = FakeContext()

        with self.assertLogs("cogs.scan", level="ERROR") as logs:
            await scan._scan_full(ctx)

        self.assertIn("Failed to start complete scan", "\n".join(logs.output))
        self.assertFalse(scan.is_scanning)
        self.assertFalse(bot.scan_state["is_scanning"])
        self.assertIsNone(bot.scan_state["scan_start_time"])
        self.assertIn("Failed to start", ctx.responses[0][0][0])

    async def test_full_scan_marks_scanning_only_after_emit_succeeds(self):
        bot = FakeBot(connect_result=True)
        scan = Scan(bot)
        ctx = FakeContext()

        await scan._scan_full(ctx)

        self.assertFalse(bot.sio.state_seen_during_emit["is_scanning"])
        self.assertTrue(scan.is_scanning)
        self.assertTrue(bot.scan_state["is_scanning"])
        self.assertEqual("complete", bot.scan_state["scan_type"])
        self.assertEqual("🔍 Started full system scan", ctx.responses[0][0][0])

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
