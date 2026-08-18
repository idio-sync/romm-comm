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
