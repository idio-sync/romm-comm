"""Tests for the cog's background reconciliation, against a real database.

Three methods that nothing drove: the ggrequestz status sync, the RomM
platform sync, and the pending-request lookup a scan uses. Two of them read
rows whose column order is decided in repo.py, so running them is the only way
to find out whether they still agree with it.
"""

import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from cogs.requests.cog import Request
from cogs.requests.repo import PlatformMappingsRepo, RequestsRepo
from database_manager import MasterDatabase

N64 = "Nintendo 64"


def run(coro_fn):
    async def main():
        directory = tempfile.mkdtemp()
        db = MasterDatabase(os.path.join(directory, "test.db"))
        await db.initialize()
        return await coro_fn(db)

    return asyncio.run(main())


class FakeBot:
    def __init__(self, db, platforms=None):
        self.db = db
        self.config = SimpleNamespace(REQUESTS_ENABLED=True, DOMAIN="https://romm.test")
        self.platforms = platforms

    def get_cog(self, name):
        return None

    def get_formatted_emoji(self, name):
        return f"<:{name}:1>"

    async def fetch_api_endpoint(self, endpoint, **kwargs):
        return self.platforms if endpoint == "platforms" else None


class FakeGGR:
    def __init__(self, statuses=None, enabled=True):
        self.enabled = enabled
        self.statuses = statuses or {}
        self.asked = []

    async def get_request_by_id(self, ggr_id):
        self.asked.append(ggr_id)
        return self.statuses.get(ggr_id)


def make_cog(bot, db, ggr=None):
    cog = object.__new__(Request)
    cog.bot = bot
    cog.db = db
    cog.repo = RequestsRepo(db)
    cog.platforms_repo = PlatformMappingsRepo(db)
    cog.igdb = None
    cog.ggr = ggr
    cog.requests_enabled = True
    cog.processing_lock = asyncio.Lock()
    return cog


async def make_request(repo, **overrides):
    values = {
        "user_id": 42,
        "username": "requester",
        "platform": N64,
        "game_name": "GoldenEye 007",
        "details": None,
        "igdb_id": None,
        "platform_mapping_id": None,
        "igdb_game_name": None,
    }
    values.update(overrides)
    return await repo.create(**values)


class GGRequestzSyncTests(unittest.TestCase):
    """Statuses set on the ggrequestz side are adopted locally."""

    async def _synced(self, db, ggr_status, local_status="pending"):
        repo = RequestsRepo(db)
        request_id = await make_request(repo)
        await repo.set_ggr_request_id(request_id, 500)
        if local_status != "pending":
            await repo.apply_synced_status(request_id, local_status, "earlier")

        ggr = FakeGGR({500: {"status": ggr_status, "admin_notes": "on its way"}})
        cog = make_cog(FakeBot(db), db, ggr)
        await cog._sync_statuses_from_ggrequestz()

        rows = await repo.list_all()
        return rows[0], ggr

    def test_a_rejection_maps_onto_the_local_name_for_it(self):
        """ggrequestz says 'rejected'; locally the status is 'reject'."""
        async def go(db):
            row, _ = await self._synced(db, "rejected")
            return row["status"], row["notes"]

        status, notes = run(go)
        self.assertEqual("reject", status)
        self.assertIn("Synced from ggrequestz", notes)
        self.assertIn("on its way", notes)

    def test_a_fulfilment_is_adopted(self):
        async def go(db):
            row, _ = await self._synced(db, "fulfilled")
            return row["status"]

        self.assertEqual("fulfilled", run(go))

    def test_approved_has_no_local_equivalent_and_changes_nothing(self):
        """Deliberate: the local statuses have no 'approved'."""
        async def go(db):
            row, _ = await self._synced(db, "approved")
            return row["status"], row["notes"]

        status, notes = run(go)
        self.assertEqual("pending", status)
        self.assertIsNone(notes)

    def test_an_unknown_status_changes_nothing(self):
        async def go(db):
            row, _ = await self._synced(db, "something-new")
            return row["status"]

        self.assertEqual("pending", run(go))

    def test_a_request_already_in_that_state_is_left_alone(self):
        async def go(db):
            row, _ = await self._synced(db, "fulfilled", local_status="fulfilled")
            return row["notes"]

        self.assertEqual("earlier", run(go), "no rewrite when nothing changed")

    def test_closed_requests_are_not_reconciled(self):
        """Nothing left to sync once a request is fulfilled or rejected."""
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_ggr_request_id(request_id, 500)
            await repo.apply_synced_status(request_id, "reject", "no")

            ggr = FakeGGR({500: {"status": "fulfilled"}})
            await make_cog(FakeBot(db), db, ggr)._sync_statuses_from_ggrequestz()
            return ggr.asked

        self.assertEqual([], run(go))

    def test_the_sync_can_be_narrowed_to_one_user(self):
        async def go(db):
            repo = RequestsRepo(db)
            mine = await make_request(repo, user_id=1)
            theirs = await make_request(repo, user_id=2, game_name="Perfect Dark")
            await repo.set_ggr_request_id(mine, 501)
            await repo.set_ggr_request_id(theirs, 502)

            ggr = FakeGGR({
                501: {"status": "fulfilled"},
                502: {"status": "fulfilled"},
            })
            await make_cog(FakeBot(db), db, ggr)._sync_statuses_from_ggrequestz(user_id=1)
            return ggr.asked

        self.assertEqual([501], run(go))

    def test_a_request_ggrequestz_has_never_heard_of_is_skipped(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_ggr_request_id(request_id, 500)

            await make_cog(FakeBot(db), db, FakeGGR({}))._sync_statuses_from_ggrequestz()
            return (await repo.list_all())[0]["status"]

        self.assertEqual("pending", run(go))

    def test_nothing_happens_when_the_integration_is_off(self):
        async def go(db):
            ggr = FakeGGR(enabled=False)
            await make_cog(FakeBot(db), db, ggr)._sync_statuses_from_ggrequestz()
            return ggr.asked

        self.assertEqual([], run(go))


class PlatformSyncTests(unittest.TestCase):
    """Which platforms RomM has, matched against the seeded mapping list."""

    async def _flags(self, db, display_name=N64):
        mapping = await PlatformMappingsRepo(db).get_by_display_name(display_name)
        return bool(mapping["in_romm"]), mapping["romm_id"]

    def test_a_platform_matched_by_name_is_marked_present(self):
        async def go(db):
            bot = FakeBot(db, platforms=[{"id": 7, "name": N64, "custom_name": None}])
            await make_cog(bot, db).sync_romm_platforms()
            return await self._flags(db)

        self.assertEqual((True, 7), run(go))

    def test_a_platform_matched_by_its_custom_name(self):
        async def go(db):
            bot = FakeBot(db, platforms=[
                {"id": 8, "name": "something-else", "custom_name": N64},
            ])
            await make_cog(bot, db).sync_romm_platforms()
            return await self._flags(db)

        self.assertEqual((True, 8), run(go))

    def test_a_folder_name_with_hyphens_still_matches(self):
        """RomM reports a folder where the mapping holds a spaced name."""
        async def go(db):
            async with db.get_connection() as conn:
                await conn.execute(
                    "UPDATE platform_mappings SET folder_name = ? WHERE display_name = ?",
                    ("Nintendo-64", N64),
                )
            bot = FakeBot(db, platforms=[
                {"id": 9, "name": "Nintendo 64", "custom_name": None},
            ])
            await make_cog(bot, db).sync_romm_platforms()
            return await self._flags(db)

        self.assertEqual((True, 9), run(go))

    def test_an_unknown_platform_marks_nothing(self):
        async def go(db):
            bot = FakeBot(db, platforms=[
                {"id": 10, "name": "Atari Jaguar CD Plus", "custom_name": None},
            ])
            await make_cog(bot, db).sync_romm_platforms()
            return await self._flags(db)

        self.assertEqual((False, None), run(go))

    def test_a_platform_with_no_name_at_all_is_skipped(self):
        async def go(db):
            bot = FakeBot(db, platforms=[
                {"id": 11, "name": "", "custom_name": None},
                {"id": 12, "name": N64, "custom_name": None},
            ])
            await make_cog(bot, db).sync_romm_platforms()
            return await self._flags(db)

        self.assertEqual((True, 12), run(go), "the nameless one must not stop the rest")

    def test_an_unreachable_api_leaves_the_table_alone(self):
        async def go(db):
            await make_cog(FakeBot(db, platforms=None), db).sync_romm_platforms()
            return await self._flags(db)

        self.assertEqual((False, None), run(go))


class PendingRequestLookupTests(unittest.TestCase):
    """What a scan asks before deciding a new file fulfils something."""

    def test_a_near_enough_title_on_the_same_platform_matches(self):
        async def go(db):
            repo = RequestsRepo(db)
            await make_request(repo, game_name="GoldenEye 007")
            cog = make_cog(FakeBot(db), db)
            return await cog.check_pending_requests(N64, "Goldeneye 007!")

        matches = run(go)
        self.assertEqual(1, len(matches))
        self.assertEqual((42, "GoldenEye 007"), matches[0][1:])

    def test_a_different_game_does_not_match(self):
        async def go(db):
            repo = RequestsRepo(db)
            await make_request(repo, game_name="Perfect Dark")
            cog = make_cog(FakeBot(db), db)
            return await cog.check_pending_requests(N64, "GoldenEye 007")

        self.assertEqual([], run(go))

    def test_another_platform_does_not_match(self):
        async def go(db):
            repo = RequestsRepo(db)
            await make_request(repo, platform="Sega Saturn")
            cog = make_cog(FakeBot(db), db)
            return await cog.check_pending_requests(N64, "GoldenEye 007")

        self.assertEqual([], run(go))

    def test_a_closed_request_is_not_waiting_on_anything(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.mark_fulfilled(request_id, by_id=1, by_name="admin")
            cog = make_cog(FakeBot(db), db)
            return await cog.check_pending_requests(N64, "GoldenEye 007")

        self.assertEqual([], run(go))
