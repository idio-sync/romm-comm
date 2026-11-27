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


class FakeRomm:
    def __init__(self, version=None, error=None):
        self.version = version
        self.error = error
        self.scan_requests = []
        # What POST /api/tasks/scan answers: (status, body).
        self.scan_response = (202, {"task_id": "j1", "status": "queued"})

    async def get_server_version(self):
        if self.error:
            raise self.error
        return self.version

    async def start_scan(self, payload):
        self.scan_requests.append(payload)
        return self.scan_response


class FakeBot:
    def __init__(self, connect_result=True, romm_version=None):
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
        self.romm = FakeRomm(romm_version)

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


class ScanMetadataSourceTests(unittest.IsolatedAsyncioTestCase):
    """RomM 5.4 picks the enabled sources itself when "apis" is left out.

    Before 5.4 a missing "apis" meant no sources, so the bot must keep naming
    them for any server it cannot place at 5.4 or later.
    """

    async def emitted_options(self, version=None, error=None, run="_scan_full"):
        """The options RomM received, by whichever path the bot sent them."""
        bot = FakeBot(romm_version=version)
        bot.romm.error = error
        scan = Scan(bot)
        await getattr(scan, run)(FakeContext())
        sent = bot.romm.scan_requests + [
            options for event, options in bot.sio.emitted if event == "scan"
        ]
        self.assertEqual(1, len(sent))
        return sent[0]

    async def test_5_4_leaves_the_sources_to_romm(self):
        options = await self.emitted_options(version=(5, 4, 0))
        self.assertNotIn("apis", options)

    async def test_a_5_5_pre_release_leaves_the_sources_to_romm(self):
        options = await self.emitted_options(version=(5, 5, 0))
        self.assertNotIn("apis", options)

    async def test_5_3_still_names_igdb_and_moby(self):
        options = await self.emitted_options(version=(5, 3, 1))
        self.assertEqual(["igdb", "moby"], options["apis"])

    async def test_an_unknown_version_still_names_them(self):
        options = await self.emitted_options(version=None)
        self.assertEqual(["igdb", "moby"], options["apis"])

    async def test_a_failed_version_lookup_still_names_them(self):
        options = await self.emitted_options(error=RuntimeError("down"))
        self.assertEqual(["igdb", "moby"], options["apis"])

    async def test_a_hash_scan_keeps_its_empty_list_on_any_version(self):
        options = await self.emitted_options(version=(5, 4, 1), run="_scan_hashes")
        self.assertEqual([], options["apis"])

    async def test_only_keys_romm_5_4_accepts_are_sent(self):
        """5.4's ScanPayload forbids extra keys, failing the scan outright."""
        allowed = {"type", "platforms", "platform_fs_slugs", "roms_ids", "apis",
                   "launchbox_remote_enabled"}
        for run in ("_scan_full", "_scan_unidentified", "_scan_hashes",
                    "_scan_new_platforms", "_scan_partial"):
            for version in ((5, 4, 1), (5, 2, 0)):
                with self.subTest(run=run, version=version):
                    options = await self.emitted_options(version=version, run=run)
                    self.assertLessEqual(set(options), allowed)


class TasksApiScanTests(unittest.IsolatedAsyncioTestCase):
    """RomM 5.4+ starts scans over POST /api/tasks/scan.

    The gain is refusals arriving as an answer, before the bot says a scan
    is running. Anything that says nothing about the scan falls back to the
    socket, which every version takes.
    """

    async def start(self, version=(5, 4, 1), response=None):
        bot = FakeBot(romm_version=version)
        if response is not None:
            bot.romm.scan_response = response
        scan = Scan(bot)
        ctx = FakeContext()
        await scan._scan_full(ctx)
        return bot, scan, ctx

    async def test_5_4_uses_the_tasks_api_and_not_the_socket(self):
        bot, scan, ctx = await self.start()
        self.assertEqual(1, len(bot.romm.scan_requests))
        self.assertEqual([], bot.sio.emitted)
        self.assertTrue(scan.is_scanning)
        self.assertEqual("🔍 Started full system scan", ctx.responses[0][0][0])

    async def test_it_still_connects_the_socket_for_progress(self):
        bot, _, _ = await self.start()
        self.assertEqual(1, bot.socketio_manager.connect_calls)

    async def test_before_5_4_it_uses_the_socket(self):
        bot, scan, _ = await self.start(version=(5, 3, 1))
        self.assertEqual([], bot.romm.scan_requests)
        self.assertEqual("scan", bot.sio.emitted[0][0])
        self.assertTrue(scan.is_scanning)

    async def test_a_scan_already_running_is_refused_up_front(self):
        bot, scan, ctx = await self.start(
            response=(409, {"detail": "A library scan is already queued or running"})
        )
        self.assertFalse(scan.is_scanning)
        self.assertFalse(bot.scan_state["is_scanning"])
        self.assertIn("already running", ctx.responses[0][0][0])
        self.assertEqual([], bot.sio.emitted)

    async def test_no_scan_worker_is_refused_up_front(self):
        bot, scan, ctx = await self.start(response=(503, {"detail": "No worker"}))
        self.assertFalse(scan.is_scanning)
        self.assertIn("no scan worker", ctx.responses[0][0][0])

    async def test_invalid_options_show_romms_reason(self):
        bot, scan, ctx = await self.start(
            response=(422, {"detail": "platforms: Input should be a valid list"})
        )
        self.assertFalse(scan.is_scanning)
        self.assertIn("platforms: Input should be a valid list", ctx.responses[0][0][0])

    async def test_a_token_without_tasks_run_falls_back_to_the_socket(self):
        bot, scan, _ = await self.start(response=(403, {"detail": "Forbidden"}))
        self.assertEqual("scan", bot.sio.emitted[0][0])
        self.assertTrue(scan.is_scanning)

    async def test_an_unreachable_endpoint_falls_back_to_the_socket(self):
        bot, scan, _ = await self.start(response=(None, None))
        self.assertEqual("scan", bot.sio.emitted[0][0])
        self.assertTrue(scan.is_scanning)

    async def test_a_server_error_falls_back_to_the_socket(self):
        bot, scan, _ = await self.start(response=(500, None))
        self.assertEqual("scan", bot.sio.emitted[0][0])

    async def test_both_paths_get_the_same_options(self):
        rest_bot, _, _ = await self.start()
        socket_bot, _, _ = await self.start(response=(403, None))
        self.assertEqual(rest_bot.romm.scan_requests[0], socket_bot.sio.emitted[0][1])


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


class ScanDeferTests(unittest.IsolatedAsyncioTestCase):
    async def test_scan_command_defers_before_dispatching(self):
        bot = FakeBot(connect_result=True)
        scan = Scan(bot)
        ctx = FakeContext()

        # Call the raw callback (bypasses the @is_admin check) with a no-network subcommand
        await Scan.scan.callback(scan, ctx, command="summary", platform=None)

        self.assertTrue(ctx.deferred)
