"""Tests for the cog's background reconciliation, against a real database.

Three methods that nothing drove: the ggrequestz status sync, the RomM
platform sync, and the pending-request lookup a scan uses. Two of them read
rows whose column order is decided in repo.py, so running them is the only way
to find out whether they still agree with it.
"""

import asyncio
import contextlib
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


class RecordingBot(FakeBot):
    """Records every DM, in order, and can make a recipient unreachable."""

    def __init__(self, db, platforms=None, unreachable=(), events=None):
        super().__init__(db, platforms)
        self.dms = []
        self.unreachable = set(unreachable)
        # Shared with a FakeCtx when the order of DMs against the reply matters.
        self.events = events

    def get_user(self, user_id):
        return None

    async def fetch_user(self, user_id):
        if user_id in self.unreachable:
            raise TimeoutError("connection dropped")

        bot = self

        class Recipient:
            avatar = None
            default_avatar = SimpleNamespace(url="https://example/default.png")

            async def send(self, message):
                bot.dms.append((user_id, message))
                if bot.events is not None:
                    bot.events.append(f"dm:{user_id}")

        return Recipient()


class FakeCtx:
    """Enough ApplicationContext for /my_requests."""

    def __init__(self, events=None):
        self.author = SimpleNamespace(id=42)
        self.events = events
        self.responses = []

    async def defer(self, **kwargs):
        return None

    async def respond(self, *args, **kwargs):
        self.responses.append((args, kwargs))
        if self.events is not None:
            self.events.append("respond")
        return SimpleNamespace(id=1)


@contextlib.contextmanager
def instant_dms():
    """Skip the one-second pacing between DMs."""
    import cogs.requests.notifications as module

    async def no_sleep(_seconds):
        return None

    original = module.asyncio.sleep
    module.asyncio.sleep = no_sleep
    try:
        yield
    finally:
        module.asyncio.sleep = original


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


class SyncedTransitionNotificationTests(unittest.TestCase):
    """A request ggrequestz closed is an ending like any other.

    The three paths that end a request inside Discord all DM the requester and
    the wait list. This one reached the same end and said nothing, so whether
    anybody heard depended on which side of the integration did the closing.
    """

    async def _announce(self, db, ggr_status, *, subscribers=(), notes=None,
                        igdb_game_name=None):
        repo = RequestsRepo(db)
        request_id = await make_request(repo, igdb_game_name=igdb_game_name)
        await repo.set_ggr_request_id(request_id, 500)
        for user_id in subscribers:
            await repo.add_subscriber(request_id, user_id, f"watcher{user_id}")

        payload = {"status": ggr_status}
        if notes is not None:
            payload["admin_notes"] = notes

        bot = RecordingBot(db)
        cog = make_cog(bot, db, FakeGGR({500: payload}))
        transitions = await cog._sync_statuses_from_ggrequestz()
        with instant_dms():
            await cog._announce_synced_transitions(transitions)
        return bot.dms

    def test_the_requester_is_told_their_request_landed(self):
        async def go(db):
            return await self._announce(db, "fulfilled")

        self.assertEqual(
            [(42, "✅ Your request for 'GoldenEye 007' has been fulfilled!")],
            run(go),
        )

    def test_the_wait_list_is_told_too_and_in_their_own_words(self):
        async def go(db):
            return await self._announce(db, "fulfilled", subscribers=[7, 8])

        dms = run(go)
        self.assertEqual([42, 7, 8], [user_id for user_id, _ in dms])
        self.assertIn("Your request for", dms[0][1])
        self.assertIn("The request you're following for", dms[1][1])
        self.assertIn("The request you're following for", dms[2][1])

    def test_a_rejection_carries_the_reason_ggrequestz_gave(self):
        async def go(db):
            return await self._announce(
                db, "rejected", subscribers=[7], notes="not obtainable"
            )

        dms = run(go)
        self.assertIn("has been rejected", dms[0][1])
        self.assertIn("not obtainable", dms[0][1])
        self.assertIn("was rejected", dms[1][1])
        self.assertIn("/request", dms[1][1], "a dead end should point somewhere")

    def test_a_cancellation_elsewhere_is_news_to_the_requester(self):
        """Unlike cancelling through /my_requests, where they already know."""
        async def go(db):
            return await self._announce(db, "cancelled")

        dms = run(go)
        self.assertIn("was cancelled", dms[0][1])
        self.assertIn("/request", dms[0][1])

    def test_the_igdb_name_is_preferred_when_there_is_one(self):
        async def go(db):
            return await self._announce(
                db, "fulfilled", igdb_game_name="GoldenEye 007 (1997)"
            )

        self.assertIn("GoldenEye 007 (1997)", run(go)[0][1])

    def test_a_status_that_is_not_an_ending_tells_nobody(self):
        async def go(db):
            return await self._announce(db, "approved", subscribers=[7])

        self.assertEqual([], run(go))

    def test_a_second_sync_says_nothing_more(self):
        """The status is written before anyone is told, so it only lands once."""
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_ggr_request_id(request_id, 500)
            await repo.add_subscriber(request_id, 7, "watcher")

            bot = RecordingBot(db)
            cog = make_cog(bot, db, FakeGGR({500: {"status": "fulfilled"}}))

            with instant_dms():
                await cog._announce_synced_transitions(
                    await cog._sync_statuses_from_ggrequestz()
                )
                first = list(bot.dms)
                await cog._announce_synced_transitions(
                    await cog._sync_statuses_from_ggrequestz()
                )
            return first, bot.dms

        first, total = run(go)
        self.assertEqual(2, len(first))
        self.assertEqual(first, total, "the second pass found nothing to announce")

    def test_an_unreachable_recipient_does_not_stop_the_rest(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_ggr_request_id(request_id, 500)
            await repo.add_subscriber(request_id, 7, "watcher")

            bot = RecordingBot(db, unreachable={42})
            cog = make_cog(bot, db, FakeGGR({500: {"status": "fulfilled"}}))
            with instant_dms():
                await cog._announce_synced_transitions(
                    await cog._sync_statuses_from_ggrequestz()
                )
            return bot.dms

        self.assertEqual([7], [user_id for user_id, _ in run(go)])


class MyRequestsWiringTests(unittest.TestCase):
    """The command has to actually announce what the sync found.

    Ordering is the point: the DMs are paced a second apart, so they belong
    after the reply rather than in front of it.
    """

    def test_my_requests_replies_first_then_announces(self):
        async def go(db):
            events = []
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_ggr_request_id(request_id, 500)
            await repo.add_subscriber(request_id, 7, "watcher")

            bot = RecordingBot(db, events=events)
            cog = make_cog(bot, db, FakeGGR({500: {"status": "fulfilled"}}))

            with instant_dms():
                await Request.my_requests.callback(
                    cog, FakeCtx(events), show_pending_only=False
                )
            return events

        events = run(go)
        self.assertEqual(["respond", "dm:42", "dm:7"], events)

    def test_a_sync_with_nothing_to_report_sends_no_dms(self):
        async def go(db):
            events = []
            repo = RequestsRepo(db)
            await make_request(repo)

            bot = RecordingBot(db, events=events)
            cog = make_cog(bot, db, FakeGGR({}))
            await Request.my_requests.callback(
                cog, FakeCtx(events), show_pending_only=False
            )
            return events

        self.assertEqual(["respond"], run(go))


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
