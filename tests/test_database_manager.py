import os
import tempfile
import unittest

import aiosqlite

from database_manager import MasterDatabase


class RequestSchemaMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_adds_current_request_columns_to_existing_database(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    game_name TEXT NOT NULL
                )
                """
            )
            await db.commit()

        manager = MasterDatabase(db_path)
        migrated = await manager.migrate_for_ggrequestz()

        self.assertTrue(migrated)
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(requests)")
            columns = {row[1] for row in await cursor.fetchall()}

            self.assertIn("platform_mapping_id", columns)
            self.assertIn("igdb_game_name", columns)
            self.assertIn("ggr_request_id", columns)

    async def test_new_request_schema_has_ggr_request_index_after_migration(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        manager = MasterDatabase(db_path)
        async with aiosqlite.connect(db_path) as db:
            await manager._create_request_tables(db)
            await db.commit()

        migrated = await manager.migrate_for_ggrequestz()

        self.assertTrue(migrated)
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'index'
                  AND tbl_name = 'requests'
                  AND name = 'idx_ggr_request_id'
                """
            )
            self.assertIsNotNone(await cursor.fetchone())


class UserLinkSchemaMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_user_link_migration_adds_created_by_bot_column(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                CREATE TABLE user_links (
                    discord_id INTEGER PRIMARY KEY,
                    romm_username TEXT NOT NULL,
                    romm_id INTEGER NOT NULL,
                    discord_username TEXT,
                    discord_avatar TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            await db.commit()

        manager = MasterDatabase(db_path)
        migrated = await manager.migrate_user_link_schema()

        self.assertTrue(migrated)
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute("PRAGMA table_info(user_links)")
            columns = {row[1] for row in await cursor.fetchall()}

        self.assertIn("created_by_bot", columns)

    async def test_new_user_link_schema_has_created_by_bot_column(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        manager = MasterDatabase(db_path)
        async with aiosqlite.connect(db_path) as db:
            await manager._create_user_tables(db)
            await db.commit()

            cursor = await db.execute("PRAGMA table_info(user_links)")
            columns = {row[1] for row in await cursor.fetchall()}

        self.assertIn("created_by_bot", columns)


class PendingInviteTests(unittest.IsolatedAsyncioTestCase):
    async def _manager(self):
        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(db_path) and os.remove(db_path))

        manager = MasterDatabase(db_path)
        async with aiosqlite.connect(db_path) as db:
            await manager._create_user_tables(db)
            await db.commit()

        # get_connection() refuses to hand out a connection otherwise, and the
        # full initialize() path is more than these tests need.
        manager._initialized = True
        return manager

    async def test_create_user_tables_adds_pending_invites(self):
        manager = await self._manager()

        async with aiosqlite.connect(manager.db_path) as db:
            cursor = await db.execute("PRAGMA table_info(pending_invites)")
            columns = {row[1] for row in await cursor.fetchall()}

        self.assertEqual(
            {"discord_id", "jti", "role", "sent_at", "expires_at"}, columns
        )

    async def test_pending_invite_round_trips(self):
        manager = await self._manager()

        added = await manager.add_pending_invite(
            discord_id=1234,
            jti="jti-abc",
            role="user",
            sent_at="2026-09-11T12:00:00+00:00",
            expires_at="2026-09-25T12:00:00+00:00",
        )
        self.assertTrue(added)

        invite = await manager.get_pending_invite(1234)
        self.assertEqual("jti-abc", invite["jti"])
        self.assertEqual("user", invite["role"])
        self.assertEqual("2026-09-11T12:00:00+00:00", invite["sent_at"])
        self.assertEqual("2026-09-25T12:00:00+00:00", invite["expires_at"])

        self.assertEqual([invite], await manager.get_all_pending_invites())

        await manager.delete_pending_invite(1234)
        self.assertIsNone(await manager.get_pending_invite(1234))
        self.assertEqual([], await manager.get_all_pending_invites())

    async def test_resending_replaces_the_outstanding_invite(self):
        manager = await self._manager()

        await manager.add_pending_invite(1234, "first", "user", "2026-09-01T00:00:00+00:00", None)
        await manager.add_pending_invite(1234, "second", "user", "2026-09-02T00:00:00+00:00", None)

        invites = await manager.get_all_pending_invites()
        self.assertEqual(1, len(invites), "one outstanding invite per member")
        self.assertEqual("second", invites[0]["jti"])
