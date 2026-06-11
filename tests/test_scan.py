import asyncio
import unittest

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


class FakeChannel:
    id = 1234


class FakeContext:
    def __init__(self):
        self.channel = FakeChannel()
        self.responses = []

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
