"""Coverage for the two request embed builders.

These render straight from a `requests` table row. They used to read it by
position (``req[3]``, ``req[14]``), which meant a column appended by an
ALTER TABLE migration silently shifted what every field showed. They now read
by name, and these tests pin that down by feeding them a real sqlite3.Row
built from the live schema rather than a hand-written tuple.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from cogs.platform_emoji import PlatformEmoji
from cogs.requests import REQUEST_COLUMNS, RequestAdminView, UserRequestsView
from cogs.requests.embeds import DEFAULT_THUMBNAIL, parse_request_details
from database_manager import MasterDatabase


class FakeBot:
    """Only what the embed builders reach for."""

    # The real bot builds this in __init__; with no emojis loaded it falls
    # back to a generic one, which is what these tests see.
    platform_emoji = PlatformEmoji(SimpleNamespace(emojis=[]))

    def get_cog(self, name):
        return None

    def get_formatted_emoji(self, name):
        return f":{name}:"


def insert_and_fetch(**overrides):
    """Round-trip a request through the real schema and hand back the row.

    Going through MasterDatabase rather than a literal tuple is the point:
    the row's column order and types come from the same CREATE TABLE and the
    same row_factory the cog sees in production.
    """

    values = {
        "user_id": 42,
        "username": "requester",
        "platform": "Nintendo 64",
        "game_name": "GoldenEye 007",
        "details": "PAL copy please",
        "status": "pending",
        "notes": None,
        "fulfilled_by": None,
        "fulfiller_name": None,
        "auto_fulfilled": 0,
        "igdb_id": None,
        "platform_mapping_id": None,
        "igdb_game_name": None,
    }
    values.update(overrides)

    async def run():
        directory = tempfile.mkdtemp()
        db = MasterDatabase(os.path.join(directory, "test.db"))
        await db.initialize()

        columns = ", ".join(values)
        placeholders = ", ".join("?" * len(values))
        async with db.get_connection() as conn:
            await conn.execute(
                f"INSERT INTO requests ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        async with db.get_connection() as conn:
            cursor = await conn.execute(f"SELECT {REQUEST_COLUMNS} FROM requests")
            return await cursor.fetchone()

    return asyncio.run(run())


class RequestColumnsTests(unittest.TestCase):
    def test_column_list_matches_the_live_schema(self):
        """REQUEST_COLUMNS must name every column the table actually has.

        If a migration adds one and this list is not updated, the new column
        is invisible to every view. Catch that here rather than in a callback.
        """

        async def run():
            directory = tempfile.mkdtemp()
            db = MasterDatabase(os.path.join(directory, "test.db"))
            await db.initialize()
            async with db.get_connection() as conn:
                cursor = await conn.execute("PRAGMA table_info(requests)")
                return [row["name"] for row in await cursor.fetchall()]

        schema_columns = asyncio.run(run())
        listed = [name.strip() for name in REQUEST_COLUMNS.split(",")]
        self.assertEqual(sorted(schema_columns), sorted(listed))

    def test_rows_are_addressable_by_name(self):
        row = insert_and_fetch()
        self.assertIsInstance(row, sqlite3.Row)
        self.assertEqual("Nintendo 64", row["platform"])
        self.assertEqual("GoldenEye 007", row["game_name"])
        self.assertIsNone(row["platform_mapping_id"])


class ParseRequestDetailsTests(unittest.TestCase):
    """The details column carries structured data as free text.

    Now that parsing is a pure function rather than the opening 30 lines of a
    233-line method, it can be tested directly instead of through an embed.
    """

    def test_plain_details_fall_back_to_the_requested_name(self):
        parsed = parse_request_details("PAL copy please", fallback_name="GoldenEye 007")
        self.assertEqual("GoldenEye 007", parsed.igdb_name)
        self.assertEqual({}, parsed.game_data)
        self.assertIsNone(parsed.cover_url)

    def test_igdb_metadata_block_is_extracted(self):
        details = "\n".join([
            "IGDB Metadata:",
            "Game: GoldenEye 007 (1997)",
            "Genres: Shooter, Adventure",
            "Release Date: 1997-08-25",
            "Cover URL: https://images.igdb.com/cover.jpg",
        ])
        parsed = parse_request_details(details, fallback_name="ignored")
        self.assertEqual("GoldenEye 007", parsed.igdb_name)
        self.assertEqual("Shooter, Adventure", parsed.game_data["Genres"])
        self.assertEqual("https://images.igdb.com/cover.jpg", parsed.cover_url)

    def test_version_request_and_notes_are_split(self):
        details = "\n".join(["Version Request: PAL", "Additional Notes: boxed if possible"])
        parsed = parse_request_details(details, fallback_name="x")
        self.assertEqual("PAL", parsed.version_request)
        self.assertEqual("boxed if possible", parsed.additional_notes)

    def test_empty_details_do_not_raise(self):
        for value in ("", None):
            parsed = parse_request_details(value, fallback_name="x")
            self.assertEqual("x", parsed.igdb_name)
            self.assertIsNone(parsed.version_request)


class EmbedBuilderTests(unittest.TestCase):
    """Both views build the same embed; assert against both so they cannot drift."""

    def build_both(self, row, platform_status=None):
        """discord.ui.View needs a running loop, so build inside one."""

        async def run():
            admin = RequestAdminView(FakeBot(), [row], admin_id=1, db=None)
            user = UserRequestsView(FakeBot(), [row], user_id=42, db=None)
            if platform_status is not None:
                admin.platform_status = dict(platform_status)
                user.platform_status = dict(platform_status)
            return admin.create_request_embed(row), user.create_request_embed(row)

        return asyncio.run(run())

    def field(self, embed, name):
        for candidate in embed.fields:
            if candidate.name == name:
                return candidate.value
        return None

    def test_pending_request_renders_core_fields(self):
        row = insert_and_fetch()
        for embed in self.build_both(row):
            self.assertIn("GoldenEye 007", embed.title)
            self.assertIn("Pending", self.field(embed, "Status"))
            self.assertIn("Nintendo 64", self.field(embed, "Platform"))
            self.assertIn("requester", embed.footer.text)

    def test_platform_is_not_confused_with_another_column(self):
        """The regression that motivated this: platform came from req[3].

        A game name that would sort into a neighbouring column position makes
        an off-by-one read obvious instead of plausible.
        """
        row = insert_and_fetch(platform="Sega Saturn", game_name="Nintendo 64")
        for embed in self.build_both(row):
            self.assertIn("Sega Saturn", self.field(embed, "Platform"))
            self.assertIn("Nintendo 64", embed.title)

    def test_fulfilled_request_shows_fulfiller_and_notes(self):
        row = insert_and_fetch(
            status="fulfilled",
            fulfilled_by=99,
            fulfiller_name="an-admin",
            notes="Added from the weekly drop",
            auto_fulfilled=1,
        )
        for embed in self.build_both(row):
            self.assertIn("Fulfilled", self.field(embed, "Status"))
            self.assertEqual("an-admin", self.field(embed, "✍️ Fulfilled By"))
            self.assertEqual("Added from the weekly drop", self.field(embed, "Admin Notes"))
            self.assertEqual("Yes", self.field(embed, "🤖 Auto-Fulfilled"))

    def test_falls_back_to_the_project_thumbnail(self):
        row = insert_and_fetch()
        for embed in self.build_both(row):
            self.assertEqual(DEFAULT_THUMBNAIL, embed.thumbnail.url)

    def test_rejected_request_labels_the_actor_as_rejecter(self):
        row = insert_and_fetch(status="reject", fulfilled_by=99, fulfiller_name="an-admin")
        for embed in self.build_both(row):
            self.assertEqual("an-admin", self.field(embed, "✍️ Rejected By"))

    def test_platform_marked_present_when_cached_status_says_so(self):
        row = insert_and_fetch(platform_mapping_id=7)
        for embed in self.build_both(row, platform_status={7: True}):
            self.assertIn("✅", self.field(embed, "Platform"))
        for embed in self.build_both(row, platform_status={7: False}):
            self.assertIn("🆕", self.field(embed, "Platform"))


if __name__ == "__main__":
    unittest.main()
