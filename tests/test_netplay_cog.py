"""Registering a watcher, and the guards that run before one is created.

The command itself needs a live interaction, so these test the pieces it
delegates to: the watcher cap, and that a registered watcher starts PENDING
pointed at the right ROM.
"""

import unittest
from types import SimpleNamespace

import discord

from cogs.netplay.cog import Netplay
from cogs.netplay.embeds import render_key
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


ROM = {"id": 50265, "name": "Super Bomberman", "url_cover": "https://x/c.png"}


def register(cog, rom=ROM, requester_id=7):
    return cog.register_watcher(
        rom,
        requester_id=requester_id,
        requester_name="alice",
        channel_id=9,
        platform_display="SNES",
    )


class RegisterWatcherTests(unittest.TestCase):
    def test_new_watcher_starts_pending(self):
        watcher = register(make_cog())
        self.assertIs(watcher.state, NetplayState.PENDING)
        self.assertEqual(watcher.rom_id, 50265)
        self.assertEqual(watcher.rom_name, "Super Bomberman")

    def test_watcher_carries_what_the_embed_needs(self):
        """The poll loop re-renders without a ctx, so this is captured now."""
        watcher = register(make_cog())
        self.assertEqual(watcher.requester_name, "alice")
        self.assertEqual(watcher.platform_display, "SNES")
        self.assertEqual(watcher.cover_url, "https://x/c.png")

    def test_watcher_is_stored_by_rom_id(self):
        cog = make_cog()
        register(cog)
        self.assertIn(50265, cog.watchers)

    def test_at_capacity_is_reported(self):
        cog = make_cog(max_watchers=1)
        register(cog)
        self.assertTrue(cog.at_capacity())

    def test_below_capacity_is_not(self):
        self.assertFalse(make_cog(max_watchers=1).at_capacity())

    def test_a_live_watcher_for_the_rom_is_found(self):
        """Re-announcing must not orphan the first post."""
        cog = make_cog()
        register(cog)
        self.assertIsNotNone(cog.live_watcher_for(50265))

    def test_no_live_watcher_for_an_unannounced_rom(self):
        self.assertIsNone(make_cog().live_watcher_for(50265))

    def test_a_terminal_watcher_does_not_block_re_announcing(self):
        cog = make_cog()
        watcher = register(cog)
        watcher.state = NetplayState.ENDED
        self.assertIsNone(cog.live_watcher_for(50265))


class FakeMessage:
    def __init__(self):
        self.edits = 0

    async def edit(self, **kwargs):
        self.edits += 1


class FlakyMessage(FakeMessage):
    """Rejects the first `fail_times` edits the way Discord would."""

    def __init__(self, fail_times):
        super().__init__()
        self.remaining_failures = fail_times

    async def edit(self, **kwargs):
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise discord.HTTPException(
                SimpleNamespace(status=500, reason="Internal Server Error"),
                "rate limited",
            )
        await super().edit(**kwargs)


class FakeChannel:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, message_id):
        return self._message


def make_polling_cog(poll_results, message=None):
    """A cog whose room polls return canned results, one per tick."""
    cog = make_cog()
    cog.bot.config.DOMAIN = "https://roms.example.com"
    cog._results = list(poll_results)
    message = message or FakeMessage()
    cog._message = message

    async def list_rooms(rom_id):
        return cog._results.pop(0) if cog._results else {}

    cog.bot.romm = SimpleNamespace(list_netplay_rooms=list_rooms)
    cog.bot.get_channel = lambda cid: FakeChannel(message)
    return cog


ROOM = {"r1": {"room_name": "Bomberman", "current": 2, "max": 4,
               "player_name": "idiosync", "hasPassword": False}}


class TickTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_room_appearing_edits_the_message(self):
        cog = make_polling_cog([ROOM])
        watcher = register(cog)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        self.assertEqual(cog._message.edits, 1)
        self.assertIs(watcher.state, NetplayState.LIVE)

    async def test_an_unchanged_poll_does_not_edit(self):
        """Edit suppression: 25 watchers editing every 20s hits Discord limits."""
        cog = make_polling_cog([ROOM, ROOM])
        watcher = register(cog)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        await cog.tick(now=1020.0)
        self.assertEqual(cog._message.edits, 1)

    async def test_terminal_watchers_are_dropped(self):
        cog = make_polling_cog([ROOM, {}])
        watcher = register(cog)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        await cog.tick(now=1020.0)
        self.assertEqual(cog.watchers, {})

    async def test_registering_during_a_tick_does_not_raise(self):
        """The loop awaits per watcher; a command can mutate the registry."""
        cog = make_polling_cog([ROOM])
        watcher = register(cog)
        watcher.message_id = 1

        original = cog.bot.romm.list_netplay_rooms

        async def mutate_then_poll(rom_id):
            register(cog, rom={"id": 999, "name": "Other"})
            return await original(rom_id)

        cog.bot.romm.list_netplay_rooms = mutate_then_poll
        await cog.tick(now=1000.0)  # must not raise RuntimeError
        self.assertIn(999, cog.watchers)

    async def test_a_failed_poll_leaves_the_watcher_alone(self):
        cog = make_polling_cog([None])
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = ROOM
        watcher.message_id = 1
        # Already up to date, as it would be straight out of announce().
        watcher.last_render_key = render_key(watcher)
        await cog.tick(now=1000.0)
        self.assertIs(watcher.state, NetplayState.LIVE)
        self.assertEqual(cog._message.edits, 0)

    async def test_a_failed_edit_is_retried_on_the_next_tick(self):
        """The poll may report nothing new; the post is still wrong."""
        message = FlakyMessage(fail_times=1)
        cog = make_polling_cog([ROOM, ROOM], message=message)
        watcher = register(cog)
        watcher.message_id = 1

        await cog.tick(now=1000.0)
        self.assertEqual(message.edits, 0)
        self.assertIsNone(watcher.last_render_key)

        await cog.tick(now=1020.0)
        self.assertEqual(message.edits, 1)

    async def test_a_terminal_watcher_survives_a_failed_final_edit(self):
        """Dropping it would leave the post reading live for a dead session."""
        message = FlakyMessage(fail_times=99)
        cog = make_polling_cog([{}], message=message)
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = ROOM
        watcher.message_id = 1

        await cog.tick(now=1000.0)
        self.assertIs(watcher.state, NetplayState.ENDED)
        self.assertIn(50265, cog.watchers)

    async def test_cleanup_does_not_delete_a_replacement_watcher(self):
        """A new /netplay for the same ROM can land during the awaits."""
        cog = make_polling_cog([{}])
        old = register(cog)
        old.state = NetplayState.LIVE
        old.rooms = ROOM
        old.message_id = 1

        original = cog.bot.romm.list_netplay_rooms

        async def replace_then_poll(rom_id):
            result = await original(rom_id)
            cog.watchers[50265] = register(cog)
            return result

        cog.bot.romm.list_netplay_rooms = replace_then_poll
        await cog.tick(now=1000.0)

        self.assertIn(50265, cog.watchers)
        self.assertIsNot(cog.watchers[50265], old)

    async def test_a_watcher_still_publishing_is_skipped(self):
        """message_id is None until announce() finishes sending."""
        cog = make_polling_cog([ROOM])
        watcher = register(cog)
        self.assertIsNone(watcher.message_id)

        await cog.tick(now=1000.0)

        self.assertEqual(cog._message.edits, 0)
        self.assertIsNone(watcher.last_render_key)

    async def test_a_terminal_watcher_with_no_channel_is_dropped(self):
        """A deleted channel is permanent: keep the watcher and it polls forever."""
        cog = make_polling_cog([{}])
        cog.bot.get_channel = lambda cid: None
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = ROOM
        watcher.message_id = 1

        await cog.tick(now=1000.0)

        self.assertEqual(cog.watchers, {})

    async def test_a_watcher_whose_poll_raises_does_not_starve_the_rest(self):
        """One deterministic failure must not skip the rest of the snapshot."""
        cog = make_polling_cog([])
        first = register(cog)
        second = register(cog, rom={"id": 999, "name": "Other"})
        first.message_id = 1
        second.message_id = 2
        polled = []

        async def explode_on_the_first(rom_id):
            polled.append(rom_id)
            if rom_id == first.rom_id:
                raise RuntimeError("boom")
            return ROOM

        cog.bot.romm.list_netplay_rooms = explode_on_the_first

        await cog.tick(now=1000.0)

        self.assertEqual(polled, [first.rom_id, second.rom_id])
        self.assertIs(second.state, NetplayState.LIVE)


if __name__ == "__main__":
    unittest.main()
