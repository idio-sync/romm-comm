import asyncio
from datetime import datetime
import unittest

from cogs.recent_roms import RecentRomsMonitor


class FakeMessage:
    id = 9876


class FakeChannel:
    def __init__(self, events):
        self.events = events

    async def send(self, *args, **kwargs):
        self.events.append("send")
        return FakeMessage()


class FakeBot:
    def __init__(self, channel):
        self.scan_state = {"notification_cutoff_time": datetime(2025, 1, 1)}
        self.scan_state_lock = asyncio.Lock()
        self.channel = channel
        self.dispatched = []

    def get_channel(self, channel_id):
        return self.channel

    def get_cog(self, name):
        return None

    def dispatch(self, name, *args):
        self.dispatched.append((name, args))


def build_monitor(events, detailed_roms):
    monitor = object.__new__(RecentRomsMonitor)
    monitor.recent_roms_channel_id = 1234
    monitor.bulk_display_threshold = 25
    monitor.bot = FakeBot(FakeChannel(events))

    async def fetch_rom_details_batch(rom_ids, batch_size=30):
        events.append("fetch")
        return [rom.copy() for rom in detailed_roms]

    async def enrich_roms_with_platform_names(roms):
        events.append("enrich")
        for rom in roms:
            rom.setdefault("platform_name", "NES")

    async def mark_as_posted(roms, batch_id=None):
        events.append("mark")

    async def update_message_ids(roms, message_id, batch_id):
        events.append("update")

    monitor.fetch_rom_details_batch = fetch_rom_details_batch
    monitor.enrich_roms_with_platform_names = enrich_roms_with_platform_names
    monitor.mark_as_posted = mark_as_posted
    monitor.update_message_ids = update_message_ids
    return monitor


class RecentRomsPostingTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_rom_embed_failure_does_not_mark_rom_posted(self):
        events = []
        roms = [{"id": 1, "name": "Broken Cover", "created_at": "2026-01-01T00:00:00Z"}]
        monitor = build_monitor(events, roms)

        async def create_single_rom_embed(rom):
            events.append("embed")
            raise RuntimeError("embed failed")

        monitor.create_single_rom_embed = create_single_rom_embed

        with self.assertLogs("cogs.recent_roms", level="ERROR") as logs:
            await monitor.process_scan_batch([{"id": 1}])

        self.assertEqual(["fetch", "enrich", "embed"], events)
        self.assertIn("Error processing scan batch", "\n".join(logs.output))

    async def test_single_rom_is_marked_posted_only_after_successful_send(self):
        events = []
        roms = [{"id": 2, "name": "Good Game", "created_at": "2026-01-01T00:00:00Z"}]
        monitor = build_monitor(events, roms)

        async def create_single_rom_embed(rom):
            events.append("embed")
            return object(), None

        monitor.create_single_rom_embed = create_single_rom_embed

        with self.assertLogs("cogs.recent_roms", level="INFO") as logs:
            await monitor.process_scan_batch([{"id": 2}])

        self.assertEqual(["fetch", "enrich", "embed", "send", "mark", "update"], events)
        self.assertIn("Posted 1 new ROM(s)", "\n".join(logs.output))


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
