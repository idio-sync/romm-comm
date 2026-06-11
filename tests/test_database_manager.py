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
