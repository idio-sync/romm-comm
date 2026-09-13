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

PLATFORM = "Super Nintendo Entertainment System"


def make_cog(rom_payload=None, domain="https://roms.example.com", platform_id=1):
    cog = object.__new__(Netplay)

    async def find_platform_by_name(name, platforms_data=None):
        """Mirrors bot.find_platform_by_name's (id, display_name) tuple."""
        return (platform_id, PLATFORM) if platform_id else (None, None)

    cog.bot = SimpleNamespace(
        config=SimpleNamespace(DOMAIN=domain),
        fetch_api_endpoint=_fetcher(rom_payload),
        find_platform_by_name=find_platform_by_name,
        platform_emoji=SimpleNamespace(format=lambda name: name + " :snes:"),
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
        found, truncated, _ = await make_cog(roms(0)).resolve_roms("snes", "nothing")
        self.assertEqual(found, [])
        self.assertFalse(truncated)

    async def test_single_match_is_returned_alone(self):
        found, truncated, _ = await make_cog(roms(1)).resolve_roms("snes", "bomberman")
        self.assertEqual(len(found), 1)
        self.assertFalse(truncated)

    async def test_several_matches_all_returned(self):
        found, truncated, _ = await make_cog(roms(5)).resolve_roms("snes", "bomberman")
        self.assertEqual(len(found), 5)
        self.assertFalse(truncated)

    async def test_exactly_the_cap_is_not_truncated(self):
        found, truncated, _ = await make_cog(roms(MAX_SELECT_OPTIONS)).resolve_roms("snes", "b")
        self.assertEqual(len(found), MAX_SELECT_OPTIONS)
        self.assertFalse(truncated)

    async def test_over_the_cap_truncates_and_says_so(self):
        found, truncated, _ = await make_cog(roms(40)).resolve_roms("snes", "b")
        self.assertEqual(len(found), MAX_SELECT_OPTIONS)
        self.assertTrue(truncated)

    async def test_a_failed_search_is_not_an_empty_one(self):
        found, _, _ = await make_cog(None).resolve_roms("snes", "b")
        self.assertIsNone(found)

    async def test_unknown_platform_returns_no_roms(self):
        cog = make_cog(roms(5), platform_id=None)
        found, truncated, _ = await cog.resolve_roms("nope", "b")
        self.assertEqual(found, [])
        self.assertFalse(truncated)

    async def test_platform_display_comes_from_the_platform_lookup(self):
        """Not from the ROM payload, which has no reliable display-name key."""
        _, _, display = await make_cog(roms(1)).resolve_roms("snes", "b")
        self.assertIn(PLATFORM, display)

    async def test_search_term_is_encoded_and_both_platform_params_sent(self):
        """An & in a title would otherwise truncate the query string."""
        cog = make_cog(roms(1))
        seen = []

        async def capture(endpoint, bypass_cache=False):
            seen.append(endpoint)
            return roms(1)

        cog.bot.fetch_api_endpoint = capture
        await cog.resolve_roms("snes", "Tetris & Dr. Mario")
        query = [e for e in seen if e.startswith("roms?")][0]
        self.assertNotIn("& Dr", query)
        self.assertIn("platform_ids=", query)


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
