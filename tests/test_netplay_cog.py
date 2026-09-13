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


if __name__ == "__main__":
    unittest.main()
