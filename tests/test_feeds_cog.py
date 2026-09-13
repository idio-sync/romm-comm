"""The /feeds command's own branches.

Autocomplete narrows what a user is shown, but py-cord does not stop them
typing something else and submitting it, so "unknown console" and "console
this server does not host" are both reachable and both need their own
answer. Every response is ephemeral - this embed carries the user's RomM
username, and the command it replaces answered in public.
"""

import unittest
from types import SimpleNamespace

from cogs.feeds.cog import Feeds
from cogs.feeds.verdict import DownloadAuth


class FakeCtx:
    """Records what the command answered with."""

    def __init__(self, author_id=1):
        self.author = SimpleNamespace(id=author_id)
        self.responses = []
        self.deferred = None

    async def defer(self, ephemeral=False):
        self.deferred = ephemeral

    async def respond(self, content=None, embed=None, ephemeral=False):
        self.responses.append(
            SimpleNamespace(content=content, embed=embed, ephemeral=ephemeral)
        )


class FakeCache:
    def __init__(self, platforms):
        self._platforms = platforms

    def get(self, key):
        return self._platforms if key == "platforms" else None


class FakeDb:
    def __init__(self, link=None):
        self._link = link

    async def get_user_link(self, discord_id):
        return self._link


def platform(name):
    return {"id": 1, "name": name, "custom_name": None,
            "display_name": name, "rom_count": 5}


def make_cog(platforms, link=None):
    bot = SimpleNamespace(
        config=SimpleNamespace(DOMAIN="https://romm.example", SYNC_RATE=3600),
        cache=FakeCache(platforms),
        db=FakeDb(link),
        platform_emoji=None,
    )
    cog = Feeds(bot)
    # Skip the network probe; its own tests cover it.
    cog._probed_at = 1.0
    cog.download_auth = DownloadAuth.DISABLED
    cog._probe_is_stale = lambda: False
    return cog


class ResponseBranchTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, cog, ctx, device):
        # Reach past the py-cord decorator to the function it wrapped.
        await Feeds.feeds.callback(cog, ctx, device)

    async def test_a_hosted_device_gets_an_embed(self):
        cog = make_cog([platform("Nintendo Switch")], {"romm_username": "ana"})
        ctx = FakeCtx()
        await self.invoke(cog, ctx, "Nintendo Switch")
        self.assertIsNotNone(ctx.responses[0].embed)

    async def test_an_unknown_console_is_told_what_is_available(self):
        cog = make_cog([platform("Nintendo Switch")])
        ctx = FakeCtx()
        await self.invoke(cog, ctx, "Dreamcast")
        content = ctx.responses[0].content
        self.assertIn("Dreamcast", content)
        self.assertIn("Nintendo Switch", content)

    async def test_a_real_console_this_server_lacks_gets_its_own_answer(self):
        # Distinct from the unknown case: the fix is different, so the
        # message has to be too.
        cog = make_cog([platform("Nintendo Switch")])
        ctx = FakeCtx()
        await self.invoke(cog, ctx, "PlayStation Vita")
        content = ctx.responses[0].content
        self.assertIn("doesn't host", content)
        self.assertIn("PlayStation Vita", content)

    async def test_a_server_with_no_feed_platforms_says_so(self):
        cog = make_cog([platform("Nintendo 64")])
        ctx = FakeCtx()
        await self.invoke(cog, ctx, "Nintendo Switch")
        self.assertIn("doesn't host any platforms", ctx.responses[0].content)

    async def test_a_device_key_works_as_well_as_a_display_name(self):
        cog = make_cog([platform("Nintendo Switch")])
        ctx = FakeCtx()
        await self.invoke(cog, ctx, "switch")
        self.assertIsNotNone(ctx.responses[0].embed)

    async def test_matching_is_case_and_whitespace_insensitive(self):
        cog = make_cog([platform("Nintendo Switch")])
        ctx = FakeCtx()
        await self.invoke(cog, ctx, "  NINTENDO switch ")
        self.assertIsNotNone(ctx.responses[0].embed)


class EphemeralityTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_branch_answers_privately(self):
        """The embed carries the user's RomM username.

        The command this replaces answered in public, so this is a change
        that could be undone by accident.
        """
        cases = [
            ([platform("Nintendo Switch")], "Nintendo Switch"),
            ([platform("Nintendo Switch")], "Dreamcast"),
            ([platform("Nintendo Switch")], "PlayStation Vita"),
            ([platform("Nintendo 64")], "Nintendo Switch"),
        ]
        for platforms, asked in cases:
            with self.subTest(asked=asked):
                cog = make_cog(platforms)
                ctx = FakeCtx()
                await Feeds.feeds.callback(cog, ctx, asked)
                self.assertTrue(ctx.responses[0].ephemeral)
                self.assertTrue(ctx.deferred)


class AutocompleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_hosted_consoles_are_offered(self):
        cog = make_cog([platform("Nintendo Switch"), platform("Nintendo 64")])
        options = await cog.device_autocomplete(SimpleNamespace(value=""))
        self.assertEqual(options, ["Nintendo Switch"])

    async def test_typing_narrows_the_list(self):
        cog = make_cog([platform("PlayStation 4"), platform("PlayStation 5")])
        options = await cog.device_autocomplete(SimpleNamespace(value="5"))
        self.assertEqual(options, ["PlayStation 5"])

    async def test_a_psp_only_library_still_offers_the_vita(self):
        # The hardware-versus-content split, reaching the user-visible end.
        cog = make_cog([platform("PlayStation Portable")])
        options = await cog.device_autocomplete(SimpleNamespace(value=""))
        self.assertIn("PlayStation Vita", options)

    async def test_discord_s_twenty_five_option_cap_is_respected(self):
        cog = make_cog(None)  # None means "could not tell" - offers everything
        options = await cog.device_autocomplete(SimpleNamespace(value=""))
        self.assertLessEqual(len(options), 25)


class UsernameTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_linked_user_gets_their_username(self):
        cog = make_cog([platform("Nintendo Switch")], {"romm_username": "ana"})
        self.assertEqual(await cog.romm_username(1), "ana")

    async def test_an_unlinked_user_yields_none_rather_than_raising(self):
        cog = make_cog([platform("Nintendo Switch")], None)
        self.assertIsNone(await cog.romm_username(1))

    async def test_a_database_failure_degrades_to_none(self):
        cog = make_cog([platform("Nintendo Switch")])

        async def explode(discord_id):
            raise RuntimeError("database is gone")

        cog.bot.db.get_user_link = explode
        self.assertIsNone(await cog.romm_username(1))
