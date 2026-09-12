"""Tests for the /request command itself.

The entry point to the whole feature, and until now the largest thing on this
branch that no test ran: the pieces it calls were covered one by one while the
command sequencing them was not.

It is also where the platform mapping row is read. That read used to be a
six-way tuple unpack of a row whose column order is decided in repo.py, one
file away; these run the command against a real database so the columns are
checked by use rather than by inspection.
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

import discord

import cogs.requests.cog as cog_module
from cogs.requests.cog import Request
from cogs.requests.repo import PlatformMappingsRepo, RequestsRepo
from cogs.requests.views_game import GameSelect, VariantRequestModal
from database_manager import MasterDatabase

N64 = "Nintendo 64"


def run(coro_fn):
    """Run a coroutine against a freshly initialised database."""

    async def main():
        directory = tempfile.mkdtemp()
        db = MasterDatabase(os.path.join(directory, "test.db"))
        await db.initialize()
        return await coro_fn(db)

    return asyncio.run(main())


async def seed_platform(db, *, in_romm=True, romm_id=5):
    """MasterDatabase.initialize seeds ~169 mappings; adjust one of them."""
    async with db.get_connection() as conn:
        await conn.execute(
            "UPDATE platform_mappings SET in_romm = ?, romm_id = ?, igdb_slug = ? "
            "WHERE display_name = ?",
            (1 if in_romm else 0, romm_id if in_romm else None, "n64", N64),
        )


class FakeAuthor:
    def __init__(self, ident=42):
        self.id = ident

    def __str__(self):
        return "requester#1"


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))
        return SimpleNamespace(id=1)


class FakeCtx:
    """An ApplicationContext as far as this command is concerned.

    Deliberately has no `user` attribute: responder_for keys off that to tell a
    context from an interaction, and picking the wrong branch is a real way to
    break this flow.
    """

    def __init__(self):
        self.author = FakeAuthor()
        self.deferred = False
        self.responses = []
        self.followup = FakeFollowup()

    async def defer(self, **kwargs):
        self.deferred = True

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))
        return SimpleNamespace(id=1)

    @property
    def replies(self):
        """Every embed or string the command sent, however it sent it."""
        out = []
        for args, kwargs in self.responses + self.followup.sent:
            out.extend(args)
            for key in ("content", "embed"):
                if kwargs.get(key) is not None:
                    out.append(kwargs[key])
        return out

    def text(self):
        """Everything the user would read, embed fields included."""
        parts = []
        for reply in self.replies:
            if isinstance(reply, str):
                parts.append(reply)
                continue
            footer = reply.footer.text if reply.footer else None
            parts += [reply.title or "", reply.description or "", footer or ""]
            for field in reply.fields:
                parts += [field.name or "", field.value or ""]
        return " ".join(parts)


class FakeBot:
    def __init__(self, db, collection=()):
        self.db = db
        self.config = SimpleNamespace(REQUESTS_ENABLED=True, DOMAIN="https://romm.test")
        self.collection = list(collection)
        self.endpoints = []

    def get_cog(self, name):
        return None

    def get_formatted_emoji(self, name):
        return f"<:{name}:1>"

    def get_platform_display_name(self, platform):
        return platform.get("custom_name") or platform.get("name")

    async def fetch_api_endpoint(self, endpoint, **kwargs):
        self.endpoints.append(endpoint)
        if endpoint == "platforms":
            return [{"id": 5, "name": N64, "custom_name": None}]
        if endpoint.startswith("roms?"):
            return {"items": self.collection}
        return None


class StubbedGameView(discord.ui.View):
    """Stands in for a view the command would otherwise wait 3 minutes on."""

    constructed = []

    def __init__(self, *args, **kwargs):
        super().__init__(timeout=1)
        StubbedGameView.constructed.append((args, kwargs))
        self.filtered_igdb_matches = []
        self.message = None

    async def wait(self):
        return False


def make_cog(bot, db, igdb=None):
    """The cog without its __init__, which spawns a setup task."""
    cog = object.__new__(Request)
    cog.bot = bot
    cog.db = db
    cog.repo = RequestsRepo(db)
    cog.platforms_repo = PlatformMappingsRepo(db)
    cog.igdb = igdb
    cog.ggr = None
    cog.requests_enabled = True
    cog.processing_lock = asyncio.Lock()
    return cog


async def invoke(cog, ctx, platform=N64, game="GoldenEye 007", details=None):
    """Call the command's own function, past the slash-command wrapper."""
    return await Request.request.callback(
        cog, ctx, platform=platform, game=game, details=details
    )


class UnknownPlatformTests(unittest.TestCase):
    def test_a_platform_not_in_the_mapping_table_is_refused(self):
        async def go(db):
            ctx = FakeCtx()
            cog = make_cog(FakeBot(db), db)
            await invoke(cog, ctx, platform="Atari Jaguar CD Plus")
            return ctx.text(), await cog.repo.list_all()

        text, rows = run(go)
        self.assertIn("not in our platform database", text)
        self.assertEqual([], list(rows), "nothing should be filed for a platform we do not know")

    def test_the_new_prefix_is_stripped_before_the_lookup(self):
        """The autocomplete offers "[+] Nintendo 64" for platforms RomM lacks."""
        async def go(db):
            await seed_platform(db, in_romm=False)
            ctx = FakeCtx()
            cog = make_cog(FakeBot(db), db)
            await invoke(cog, ctx, platform=f"[NEW] {N64}")
            return ctx.text(), await cog.repo.list_all()

        text, rows = run(go)
        self.assertNotIn("not in our platform database", text)
        self.assertEqual(1, len(rows))


class PlatformNotInRommTests(unittest.TestCase):
    """A platform RomM does not have yet cannot be searched for existing games."""

    def test_the_request_is_filed_without_a_collection_lookup(self):
        async def go(db):
            await seed_platform(db, in_romm=False)
            bot = FakeBot(db)
            ctx = FakeCtx()
            cog = make_cog(bot, db)
            await invoke(cog, ctx)
            return bot.endpoints, await cog.repo.list_all(), ctx.text()

        endpoints, rows, text = run(go)
        self.assertEqual([], endpoints, "no reason to ask RomM about a platform it lacks")
        self.assertEqual(1, len(rows))
        self.assertEqual("GoldenEye 007", rows[0]["game_name"])
        self.assertIn("Request Submitted", text)

    def test_the_confirmation_says_the_platform_is_not_yet_added(self):
        async def go(db):
            await seed_platform(db, in_romm=False)
            ctx = FakeCtx()
            await invoke(make_cog(FakeBot(db), db), ctx)
            return ctx.text()

        self.assertIn("Not Yet Added", run(go))


class PlatformInRommTests(unittest.TestCase):
    def test_a_game_already_in_the_collection_is_offered_instead(self):
        async def go(db):
            await seed_platform(db)
            bot = FakeBot(db, collection=[
                {"id": 1, "name": "GoldenEye 007", "fs_name": "goldeneye.z64"},
            ])
            ctx = FakeCtx()
            cog = make_cog(bot, db)
            original = cog_module.ExistingGameWithIGDBView
            cog_module.ExistingGameWithIGDBView = StubbedGameView
            StubbedGameView.constructed = []
            try:
                await invoke(cog, ctx)
            finally:
                cog_module.ExistingGameWithIGDBView = original
            return await cog.repo.list_all(), ctx.text(), StubbedGameView.constructed

        rows, text, constructed = run(go)
        self.assertEqual([], list(rows), "the game is already there; nothing to request")
        self.assertIn("Games Found in Collection", text)
        self.assertEqual(1, len(constructed))

    def test_a_game_the_collection_lacks_is_filed(self):
        async def go(db):
            await seed_platform(db)
            bot = FakeBot(db, collection=[])
            ctx = FakeCtx()
            cog = make_cog(bot, db)
            await invoke(cog, ctx)
            return bot.endpoints, await cog.repo.list_all()

        endpoints, rows = run(go)
        self.assertIn("platforms", endpoints, "the collection was checked")
        self.assertEqual(1, len(rows))
        self.assertEqual("GoldenEye 007", rows[0]["game_name"])

    def test_the_filed_request_carries_the_mapping_id_from_the_row(self):
        """Regression for the tuple unpack this read used to be.

        The mapping id, the in_romm flag and the romm id come out of one row
        whose column order lives in repo.py. Swap two of them and this is what
        goes wrong: the request is filed against the wrong mapping, or the
        collection is never searched.
        """
        async def go(db):
            await seed_platform(db)
            cog = make_cog(FakeBot(db, collection=[]), db)
            await invoke(cog, FakeCtx())
            rows = await cog.repo.list_all()
            mapping = await cog.platforms_repo.get_by_display_name(N64)
            return rows[0]["platform_mapping_id"], mapping["id"], rows[0]["platform"]

        stored, expected, platform = run(go)
        self.assertEqual(expected, stored)
        self.assertEqual(N64, platform)


class DuplicateTests(unittest.TestCase):
    def test_a_second_request_for_the_same_game_joins_the_first(self):
        async def go(db):
            await seed_platform(db)
            cog = make_cog(FakeBot(db, collection=[]), db)
            await invoke(cog, FakeCtx())

            other = FakeCtx()
            other.author = FakeAuthor(99)
            await invoke(cog, other)

            rows = await cog.repo.list_all()
            return len(rows), await cog.repo.subscriber_ids(rows[0]["id"]), other.text()

        count, subscribers, text = run(go)
        self.assertEqual(1, count, "the game is already requested; no second row")
        self.assertEqual([99], subscribers)
        self.assertIn("Request Already Exists", text)

    def test_the_same_person_asking_twice_is_told_they_already_did(self):
        async def go(db):
            await seed_platform(db)
            cog = make_cog(FakeBot(db, collection=[]), db)
            await invoke(cog, FakeCtx())

            again = FakeCtx()
            await invoke(cog, again)

            rows = await cog.repo.list_all()
            return len(rows), await cog.repo.subscriber_ids(rows[0]["id"]), again.text()

        count, subscribers, text = run(go)
        self.assertEqual(1, count)
        self.assertEqual([], subscribers, "you do not subscribe to your own request")
        self.assertIn("Already Requested", text)


class AutocompleteTests(unittest.TestCase):
    def test_platforms_romm_lacks_are_offered_with_a_marker(self):
        async def go(db):
            await seed_platform(db)
            cog = make_cog(FakeBot(db), db)
            available = await cog.platform_autocomplete_all(SimpleNamespace(value="nintendo 64"))
            missing = await cog.platform_autocomplete_all(SimpleNamespace(value="sega saturn"))
            return (
                [(c.name, c.value) for c in available],
                [(c.name, c.value) for c in missing],
            )

        available, missing = run(go)
        self.assertIn((N64, N64), available)
        self.assertTrue(
            any(name.startswith("[+]") and value == "Sega Saturn" for name, value in missing),
            missing,
        )

    def test_a_failure_returns_no_choices_rather_than_raising(self):
        """Discord shows an error in the picker if the callback raises."""
        async def go(db):
            cog = make_cog(FakeBot(db), db)
            cog.platforms_repo = SimpleNamespace(
                search_for_autocomplete=_raise(RuntimeError("database is locked"))
            )
            return await cog.platform_autocomplete_all(SimpleNamespace(value="x"))

        self.assertEqual([], run(go))


def _raise(error):
    async def boom(*args, **kwargs):
        raise error

    return boom


class GameSelectTests(unittest.TestCase):
    """The dropdown of IGDB matches, which no test constructed."""

    MATCHES = [
        {"name": "GoldenEye 007", "release_date": "1997-08-25",
         "platforms": [N64, "Wii"]},
        {"name": "A" * 150, "release_date": "Unknown", "platforms": ["X" * 120]},
    ]

    def test_options_are_built_within_discord_s_limits(self):
        select = GameSelect(self.MATCHES)

        self.assertEqual(["0", "1"], [option.value for option in select.options])
        for option in select.options:
            self.assertLessEqual(len(option.label), 100)
            self.assertLessEqual(len(option.description), 100)

    def test_the_label_is_the_game_name(self):
        select = GameSelect(self.MATCHES)

        self.assertEqual("GoldenEye 007", select.options[0].label)


class VariantRequestModalTests(unittest.IsolatedAsyncioTestCase):
    """Referenced by no test at all before this.

    Async because discord.ui.Modal reaches for the running loop when it is
    constructed, which is the sort of thing only building one finds out.
    """

    async def test_it_takes_its_author_from_a_context(self):
        ctx = FakeCtx()
        modal = VariantRequestModal(None, N64, "GoldenEye 007", None, [], ctx)

        self.assertEqual(42, modal.author_id)

    async def test_it_takes_its_author_from_an_interaction(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=7))
        modal = VariantRequestModal(None, N64, "GoldenEye 007", None, [], interaction)

        self.assertEqual(7, modal.author_id)

    async def test_an_explicit_author_id_wins_for_an_interaction(self):
        interaction = SimpleNamespace(user=SimpleNamespace(id=7))
        modal = VariantRequestModal(
            None, N64, "GoldenEye 007", None, [], interaction, author_id=99
        )

        self.assertEqual(99, modal.author_id)

    async def test_it_offers_a_required_version_field_and_an_optional_notes_one(self):
        modal = VariantRequestModal(None, N64, "GoldenEye 007", None, [], FakeCtx())

        self.assertTrue(modal.variant_input.required)
        self.assertFalse(modal.notes_input.required)
