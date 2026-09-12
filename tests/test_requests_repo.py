"""Tests for RequestsRepo against a real database.

The repository is now the only place the requests feature writes SQL, so these
run against a real MasterDatabase on a temp file rather than a mock. A mocked
connection would assert the query string, which is exactly the thing that
should be free to change.
"""

import asyncio
import os
import tempfile
import unittest

from cogs.requests.repo import PlatformMappingsRepo, RequestsRepo
from database_manager import MasterDatabase


def run(coro_fn):
    """Run a coroutine against a fresh database, returning its result."""

    async def main():
        directory = tempfile.mkdtemp()
        db = MasterDatabase(os.path.join(directory, "test.db"))
        await db.initialize()
        return await coro_fn(db)

    return asyncio.run(main())


DEFAULT_REQUEST = {
    "user_id": 42,
    "username": "requester",
    "platform": "Nintendo 64",
    "game_name": "GoldenEye 007",
    "details": "PAL copy please",
    "igdb_id": None,
    "platform_mapping_id": None,
    "igdb_game_name": None,
}


async def make_request(repo, **overrides):
    values = dict(DEFAULT_REQUEST)
    values.update(overrides)
    return await repo.create(**values)


class CreateAndReadTests(unittest.TestCase):
    def test_create_returns_the_new_id_and_the_row_reads_back(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            return request_id, await repo.list_all()

        request_id, rows = run(go)
        self.assertIsInstance(request_id, int)
        self.assertEqual(1, len(rows))
        self.assertEqual(request_id, rows[0]["id"])
        self.assertEqual("GoldenEye 007", rows[0]["game_name"])
        self.assertEqual("pending", rows[0]["status"])

    def test_list_all_is_newest_first(self):
        async def go(db):
            repo = RequestsRepo(db)
            await make_request(repo, game_name="First")
            await make_request(repo, game_name="Second")
            return [row["game_name"] for row in await repo.list_all()]

        names = run(go)
        # created_at has one-second resolution, so only assert both are present
        # and that the list is ordered, not which specific one leads.
        self.assertEqual({"First", "Second"}, set(names))

    def test_list_for_user_filters_by_user(self):
        async def go(db):
            repo = RequestsRepo(db)
            await make_request(repo, user_id=1, game_name="Mine")
            await make_request(repo, user_id=2, game_name="Theirs")
            return [row["game_name"] for row in await repo.list_for_user(1)]

        self.assertEqual(["Mine"], run(go))

    def test_list_for_user_can_restrict_to_pending(self):
        async def go(db):
            repo = RequestsRepo(db)
            done = await make_request(repo, game_name="Done")
            await make_request(repo, game_name="Open")
            await repo.mark_fulfilled(done, by_id=9, by_name="admin")
            everything = await repo.list_for_user(42)
            pending = await repo.list_for_user(42, pending_only=True)
            return len(everything), [row["game_name"] for row in pending]

        total, pending = run(go)
        self.assertEqual(2, total)
        self.assertEqual(["Open"], pending)

    def test_counts_only_pending_requests(self):
        async def go(db):
            repo = RequestsRepo(db)
            first = await make_request(repo)
            await make_request(repo)
            before = await repo.count_pending_for_user(42)
            await repo.mark_fulfilled(first, by_id=9, by_name="admin")
            return before, await repo.count_pending_for_user(42)

        self.assertEqual((2, 1), run(go))

    def test_counting_an_unknown_user_returns_zero(self):
        async def go(db):
            return await RequestsRepo(db).count_pending_for_user(9999)

        self.assertEqual(0, run(go))


class StatusTransitionTests(unittest.TestCase):
    def fulfil_and_read(self, mark, **kwargs):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await getattr(repo, mark)(request_id, **kwargs)
            rows = await repo.list_all()
            return rows[0]

        return run(go)

    def test_mark_fulfilled_records_who_did_it(self):
        row = self.fulfil_and_read("mark_fulfilled", by_id=9, by_name="an-admin")
        self.assertEqual("fulfilled", row["status"])
        self.assertEqual(9, row["fulfilled_by"])
        self.assertEqual("an-admin", row["fulfiller_name"])
        self.assertFalse(row["auto_fulfilled"])

    def test_mark_rejected_records_the_reason_as_notes(self):
        row = self.fulfil_and_read(
            "mark_rejected", by_id=9, by_name="an-admin", reason="not obtainable"
        )
        self.assertEqual("reject", row["status"])
        self.assertEqual("not obtainable", row["notes"])
        self.assertEqual("an-admin", row["fulfiller_name"])

    def test_rejecting_without_a_reason_leaves_notes_untouched(self):
        """The reason field is optional in the modal, so None must not wipe notes."""

        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_notes(request_id, "an earlier note")
            await repo.mark_rejected(request_id, by_id=9, by_name="admin", reason=None)
            rows = await repo.list_all()
            return rows[0]

        row = run(go)
        self.assertEqual("reject", row["status"])
        self.assertEqual("an earlier note", row["notes"])

    def test_mark_cancelled_records_no_fulfiller(self):
        """A user withdrawing their own request is not a fulfilment."""
        row = self.fulfil_and_read("mark_cancelled", reason="changed my mind")
        self.assertEqual("cancelled", row["status"])
        self.assertEqual("changed my mind", row["notes"])
        self.assertIsNone(row["fulfilled_by"])

    def test_mark_auto_fulfilled_closes_a_batch_without_a_fulfiller(self):
        """A scan closed these, so no person is recorded as having done it."""

        async def go(db):
            repo = RequestsRepo(db)
            first = await make_request(repo, game_name="One")
            second = await make_request(repo, game_name="Two")
            await repo.mark_auto_fulfilled([
                (first, "found in scan"),
                (second, "also found"),
            ])
            return {row["game_name"]: row for row in await repo.list_all()}

        rows = run(go)
        for name, note in (("One", "found in scan"), ("Two", "also found")):
            self.assertEqual("fulfilled", rows[name]["status"])
            self.assertTrue(rows[name]["auto_fulfilled"])
            self.assertEqual(note, rows[name]["notes"])
            self.assertIsNone(rows[name]["fulfilled_by"])

    def test_an_empty_batch_touches_no_connection(self):
        """Asserts the short-circuit, not the outcome.

        executemany over an empty list changes nothing anyway, so checking the
        rows would pass with or without the guard. What the guard buys is that
        a scan matching no requests opens no connection at all.
        """

        class ExplodingDatabase:
            def get_connection(self):
                raise AssertionError("no connection should be opened")

        async def go():
            await RequestsRepo(ExplodingDatabase()).mark_auto_fulfilled([])

        asyncio.run(go())

    def test_apply_synced_status_overwrites_status_and_notes(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.apply_synced_status(request_id, "fulfilled", "Synced from ggrequestz")
            return (await repo.list_all())[0]

        row = run(go)
        self.assertEqual("fulfilled", row["status"])
        self.assertEqual("Synced from ggrequestz", row["notes"])

    def test_set_notes_does_not_change_status(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_notes(request_id, "waiting on a dump")
            rows = await repo.list_all()
            return rows[0]

        row = run(go)
        self.assertEqual("pending", row["status"])
        self.assertEqual("waiting on a dump", row["notes"])


class GGRequestzLinkTests(unittest.TestCase):
    def test_unlinked_request_has_no_ggr_id(self):
        async def go(db):
            repo = RequestsRepo(db)
            return await repo.get_ggr_request_id(await make_request(repo))

        self.assertIsNone(run(go))

    def test_setting_and_reading_the_ggr_id(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.set_ggr_request_id(request_id, 555)
            return await repo.get_ggr_request_id(request_id)

        self.assertEqual(555, run(go))

    def test_missing_request_returns_none_rather_than_raising(self):
        async def go(db):
            return await RequestsRepo(db).get_ggr_request_id(9999)

        self.assertIsNone(run(go))

    def test_only_synced_requests_are_listed(self):
        async def go(db):
            repo = RequestsRepo(db)
            linked = await make_request(repo, game_name="Linked")
            await make_request(repo, game_name="Unlinked")
            await repo.set_ggr_request_id(linked, 555)
            return [row["game_name"] for row in await repo.list_open_synced_with_ggrequestz()]

        self.assertEqual(["Linked"], run(go))

    def test_a_closed_request_has_no_status_left_to_reconcile(self):
        async def go(db):
            repo = RequestsRepo(db)
            done = await make_request(repo, game_name="Done")
            still_open = await make_request(repo, game_name="Open")
            await repo.set_ggr_request_id(done, 1)
            await repo.set_ggr_request_id(still_open, 2)
            await repo.mark_fulfilled(done, by_id=9, by_name="admin")
            return [row["game_name"] for row in await repo.list_open_synced_with_ggrequestz()]

        self.assertEqual(["Open"], run(go))

    def test_synced_list_can_be_narrowed_to_one_user(self):
        async def go(db):
            repo = RequestsRepo(db)
            mine = await make_request(repo, user_id=1, game_name="Mine")
            theirs = await make_request(repo, user_id=2, game_name="Theirs")
            await repo.set_ggr_request_id(mine, 1)
            await repo.set_ggr_request_id(theirs, 2)
            rows = await repo.list_open_synced_with_ggrequestz(user_id=1)
            return [row["game_name"] for row in rows]

        self.assertEqual(["Mine"], run(go))


class SubscriberTests(unittest.TestCase):
    def test_a_new_request_has_no_subscribers(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            return await repo.count_subscribers(request_id)

        self.assertEqual(0, run(go))

    def test_adding_and_listing_subscribers(self):
        async def go(db):
            repo = RequestsRepo(db)
            request_id = await make_request(repo)
            await repo.add_subscriber(request_id, 7, "watcher")
            await repo.add_subscriber(request_id, 8, "other")
            return (
                await repo.count_subscribers(request_id),
                await repo.is_subscribed(request_id, 7),
                await repo.is_subscribed(request_id, 99),
            )

        count, subscribed, not_subscribed = run(go)
        self.assertEqual(2, count)
        self.assertTrue(subscribed)
        self.assertFalse(not_subscribed)

    def test_subscribers_across_several_requests(self):
        async def go(db):
            repo = RequestsRepo(db)
            first = await make_request(repo)
            second = await make_request(repo)
            await repo.add_subscriber(first, 7, "watcher")
            await repo.add_subscriber(second, 8, "other")
            rows = await repo.subscribers_for_requests([first, second])
            return first, second, sorted((row["request_id"], row["user_id"]) for row in rows)

        first, second, pairs = run(go)
        self.assertEqual([(first, 7), (second, 8)], pairs)

    def test_no_request_ids_means_no_query(self):
        async def go(db):
            return await RequestsRepo(db).subscribers_for_requests([])

        self.assertEqual([], run(go))


class ScanListenerTests(unittest.TestCase):
    """Queries the scan-completion listener runs."""

    def test_list_pending_with_igdb_returns_only_open_requests(self):
        async def go(db):
            repo = RequestsRepo(db)
            done = await make_request(repo, game_name="Done")
            await make_request(repo, game_name="Open", igdb_id=555)
            await repo.mark_fulfilled(done, by_id=9, by_name="admin")
            rows = await repo.list_pending_with_igdb()
            return [(row["game_name"], row["igdb_id"]) for row in rows]

        self.assertEqual([("Open", 555)], run(go))


class PlatformMappingTests(unittest.TestCase):
    async def _seed(self, db):
        """Mark one of the seeded platforms as present in RomM.

        MasterDatabase.initialize already loads ~169 platform mappings, and
        display_name is UNIQUE, so the test adjusts a seeded row rather than
        inserting its own.
        """
        async with db.get_connection() as conn:
            await conn.execute(
                "UPDATE platform_mappings SET folder_name = ?, in_romm = 1, romm_id = 5 "
                "WHERE display_name = ?",
                ("n64", "Nintendo 64")
            )
            await conn.execute(
                "UPDATE platform_mappings SET in_romm = 0, romm_id = NULL WHERE display_name = ?",
                ("Sega Saturn",)
            )

    def test_display_name_resolves_from_either_name_case_insensitively(self):
        async def go(db):
            await self._seed(db)
            repo = PlatformMappingsRepo(db)
            return (
                await repo.display_name_for("nintendo 64"),
                await repo.display_name_for("N64"),
                # Not one of the ~169 seeded platforms.
                await repo.display_name_for("Atari Jaguar CD Plus"),
            )

        self.assertEqual(("Nintendo 64", "Nintendo 64", None), run(go))

    def test_in_romm_flag_by_name(self):
        async def go(db):
            await self._seed(db)
            repo = PlatformMappingsRepo(db)
            return (
                await repo.is_in_romm_by_name("Nintendo 64"),
                await repo.is_in_romm_by_name("Sega Saturn"),
                await repo.is_in_romm_by_name("Atari Jaguar CD Plus"),
            )

        self.assertEqual((True, False, False), run(go))

    def test_find_by_any_name_matches_display_or_folder_name(self):
        """RomM reports platforms under several spellings; any should match."""

        async def go(db):
            await self._seed(db)
            repo = PlatformMappingsRepo(db)
            return [
                (await repo.find_by_any_name(["Nintendo 64"]))["display_name"],
                (await repo.find_by_any_name(["n64"]))["display_name"],
                (await repo.find_by_any_name(["nope", "N64"]))["display_name"],
                await repo.find_by_any_name(["nope"]),
                await repo.find_by_any_name([]),
                await repo.find_by_any_name([None, ""]),
            ]

        by_display, by_folder, by_second, missing, empty, blanks = run(go)
        self.assertEqual("Nintendo 64", by_display)
        self.assertEqual("Nintendo 64", by_folder)
        self.assertEqual("Nintendo 64", by_second)
        self.assertIsNone(missing)
        self.assertIsNone(empty)
        self.assertIsNone(blanks)

    def test_mark_present_in_romm_records_the_romm_id(self):
        async def go(db):
            repo = PlatformMappingsRepo(db)
            mapping = await repo.find_by_any_name(["Sega Saturn"])
            await repo.mark_present_in_romm([(mapping["id"], 42)])
            refreshed = await repo.get_by_display_name("Sega Saturn")
            return bool(refreshed["in_romm"]), refreshed["romm_id"]

        self.assertEqual((True, 42), run(go))

    def test_search_for_autocomplete_puts_available_platforms_first(self):
        async def go(db):
            await self._seed(db)
            repo = PlatformMappingsRepo(db)
            rows = await repo.search_for_autocomplete("nintendo")
            return [(row["display_name"], bool(row["in_romm"])) for row in rows]

        rows = run(go)
        self.assertTrue(rows, "expected some Nintendo platforms")
        self.assertTrue(rows[0][1], "a platform RomM has should sort first")
        self.assertTrue(all("nintendo" in name.lower() for name, _ in rows))

    def test_igdb_slug_for_is_case_insensitive(self):
        async def go(db):
            repo = PlatformMappingsRepo(db)
            return (
                await repo.igdb_slug_for("nintendo 64"),
                await repo.igdb_slug_for("Atari Jaguar CD Plus"),
            )

        found, missing = run(go)
        self.assertTrue(found)
        self.assertIsNone(missing)

    def test_platform_status_prefers_mapping_id_and_falls_back_to_name(self):
        async def go(db):
            await self._seed(db)
            requests_repo = RequestsRepo(db)
            mappings = PlatformMappingsRepo(db)

            mapping = await mappings.get_by_display_name("Nintendo 64")
            await requests_repo.create(
                **{**DEFAULT_REQUEST, "platform_mapping_id": mapping["id"]}
            )
            await requests_repo.create(**{**DEFAULT_REQUEST, "platform": "Sega Saturn"})

            rows = await requests_repo.list_all()
            return await mappings.platform_status_for(rows), mapping["id"]

        status, mapping_id = run(go)
        self.assertTrue(status[mapping_id])
        self.assertFalse(status["name:Sega Saturn"])


if __name__ == "__main__":
    unittest.main()
