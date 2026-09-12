import asyncio
import unittest
from datetime import datetime

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


class _RefreshFakeMessage:
    def __init__(self, mid, events):
        self.id = mid
        self._events = events

    async def edit(self, **kwargs):
        self._events.append("edit")

    async def delete(self):
        self._events.append("delete")


class _RefreshFakeChannel:
    def __init__(self, events):
        self._events = events

    async def fetch_message(self, mid):
        self._events.append(f"fetch:{mid}")
        return _RefreshFakeMessage(mid, self._events)

    async def send(self, *args, **kwargs):
        self._events.append("send")
        return _RefreshFakeMessage(999, self._events)


class _RefreshFakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append(content)


class _RefreshFakeCtx:
    def __init__(self):
        self.followup = _RefreshFakeFollowup()

    async def defer(self, **kwargs):
        pass


class _RefreshFakeBot:
    def __init__(self, channel, available_ids):
        self._channel = channel
        self._available_ids = available_ids

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_api_endpoint(self, endpoint, bypass_cache=False):
        rom_id = int(endpoint.split('/')[-1])
        if rom_id in self._available_ids:
            return {'id': rom_id, 'name': f'Game {rom_id}',
                    'fs_name': f'g{rom_id}', 'platform_name': 'NES'}
        return None


def _build_refresh_monitor(events, available_ids, notification):
    monitor = object.__new__(RecentRomsMonitor)
    monitor.enabled = True
    monitor.recent_roms_channel_id = 1234
    monitor.bot = _RefreshFakeBot(_RefreshFakeChannel(events), available_ids)

    async def get_recent_notifications(limit, days):
        return [notification]

    update_calls = []

    async def update_message_ids(roms, message_id, batch_id):
        update_calls.append([r['id'] for r in roms])

    async def enrich(roms):
        pass

    async def create_single(rom):
        events.append("single_embed")
        return object(), None

    async def create_batch(roms):
        events.append("batch_embed")
        return object(), object()  # truthy composite -> delete+repost path

    monitor.get_recent_notifications = get_recent_notifications
    monitor.update_message_ids = update_message_ids
    monitor.enrich_roms_with_platform_names = enrich
    monitor.create_single_rom_embed = create_single
    monitor.create_batch_embed = create_batch
    return monitor, update_calls


class RecentRomsRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_refetch_skips_rebuild_instead_of_dropping_games(self):
        # A 2-ROM batch where only ROM 1 refetches must NOT be rebuilt as a
        # single-ROM embed (which would drop ROM 2) or partially re-keyed.
        events = []
        notification = {
            'message_id': 100, 'batch_id': 'b1', 'posted_at': 'x',
            'roms': [{'id': 1, 'name': 'Game 1', 'platform': 'NES'},
                     {'id': 2, 'name': 'Game 2', 'platform': 'NES'}],
        }
        monitor, update_calls = _build_refresh_monitor(events, {1}, notification)

        ctx = _RefreshFakeCtx()
        await RecentRomsMonitor.refresh_recent.callback(monitor, ctx, count=1)

        # Notification left intact: no rebuild, no edit/delete/send, no id re-keying
        self.assertNotIn("single_embed", events)
        self.assertNotIn("batch_embed", events)
        self.assertNotIn("edit", events)
        self.assertNotIn("delete", events)
        self.assertNotIn("send", events)
        self.assertEqual([], update_calls)
        self.assertTrue(any("Skipped" in m for m in ctx.followup.messages))

    async def test_full_refetch_updates_message_id_for_entire_batch(self):
        # When all ROMs refetch and a new cover forces delete+repost, the
        # message_id is updated for EVERY rom in the batch (not a subset).
        events = []
        notification = {
            'message_id': 100, 'batch_id': 'b1', 'posted_at': 'x',
            'roms': [{'id': 1, 'name': 'Game 1', 'platform': 'NES'},
                     {'id': 2, 'name': 'Game 2', 'platform': 'NES'}],
        }
        monitor, update_calls = _build_refresh_monitor(events, {1, 2}, notification)

        ctx = _RefreshFakeCtx()
        await RecentRomsMonitor.refresh_recent.callback(monitor, ctx, count=1)

        self.assertIn("batch_embed", events)
        self.assertIn("send", events)
        self.assertIn("delete", events)
        self.assertEqual([[1, 2]], update_calls)
