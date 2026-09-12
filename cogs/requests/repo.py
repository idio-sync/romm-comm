"""Data access for the requests tables.

Every query against `requests`, `request_subscribers` and `platform_mappings`
that the requests feature makes lives here. Views and the cog call methods;
they do not write SQL, and they do not know the schema.

Rows come back as sqlite3.Row, so callers read them by column name.
"""

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Explicit column list for the requests table, in schema order. Naming the
# columns rather than selecting them all means a database that grew its newer
# columns through ALTER TABLE - which appends in whatever order the migration
# happened to run - still hands back the columns in the order we expect.
REQUEST_COLUMNS = (
    "id, user_id, username, platform, game_name, details, status, "
    "created_at, updated_at, fulfilled_by, fulfiller_name, notes, "
    "auto_fulfilled, igdb_id, platform_mapping_id, igdb_game_name, ggr_request_id"
)


class RequestsRepo:
    """Queries for the requests feature, over a MasterDatabase."""

    def __init__(self, db):
        self.db = db

    # ---------------------------------------------------------------- reading

    async def list_all(self) -> List[Any]:
        """Every request, newest first."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT {REQUEST_COLUMNS} FROM requests ORDER BY created_at DESC"
            )
            return await cursor.fetchall()

    async def list_pending(self) -> List[Any]:
        """Pending requests, oldest first - the order an admin works through."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT {REQUEST_COLUMNS} FROM requests "
                "WHERE status = 'pending' ORDER BY created_at ASC"
            )
            return await cursor.fetchall()

    async def list_for_user(self, user_id: int, pending_only: bool = False) -> List[Any]:
        """One user's requests, newest first."""
        clause = "WHERE user_id = ?"
        if pending_only:
            clause += " AND status = 'pending'"
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT {REQUEST_COLUMNS} FROM requests {clause} ORDER BY created_at DESC",
                (user_id,)
            )
            return await cursor.fetchall()

    async def count_pending_for_user(self, user_id: int) -> int:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS count FROM requests WHERE user_id = ? AND status = 'pending'",
                (user_id,)
            )
            row = await cursor.fetchone()
            return row['count'] if row else 0

    async def get_ggr_request_id(self, request_id: int) -> Optional[int]:
        """The paired ggrequestz request, if this request was synced there."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT ggr_request_id FROM requests WHERE id = ?",
                (request_id,)
            )
            row = await cursor.fetchone()
            return row['ggr_request_id'] if row else None

    async def get_igdb_info(self, request_id: int) -> Optional[Any]:
        """The IGDB id, game name and platform for one request."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT igdb_id, game_name, platform FROM requests WHERE id = ?",
                (request_id,)
            )
            return await cursor.fetchone()

    async def list_pending_for_platform(self, platform: str) -> List[Any]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, user_id, game_name
                FROM requests
                WHERE platform = ? AND status = 'pending'
                """,
                (platform,)
            )
            return await cursor.fetchall()

    async def list_duplicate_candidates(self, platform: str, igdb_id: Optional[int]) -> List[Any]:
        """Pending requests on this platform that might be the same game.

        Rows with a matching IGDB id, plus rows with none at all - those still
        have to be compared by title.
        """
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, user_id, username, game_name, igdb_id
                FROM requests
                WHERE platform = ?
                AND status = 'pending'
                AND (igdb_id = ? OR igdb_id IS NULL)
                """,
                (platform, igdb_id)
            )
            return await cursor.fetchall()

    async def list_synced_with_ggrequestz(self, user_id: Optional[int] = None) -> List[Any]:
        """Requests that have a ggrequestz counterpart, for status reconciliation."""
        clause = "WHERE ggr_request_id IS NOT NULL"
        params: Sequence = ()
        if user_id is not None:
            clause += " AND user_id = ?"
            params = (user_id,)
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT id, ggr_request_id, status, user_id, game_name FROM requests {clause}",
                params
            )
            return await cursor.fetchall()

    # ---------------------------------------------------------------- writing

    async def create(
        self,
        *,
        user_id: int,
        username: str,
        platform: str,
        game_name: str,
        details: Optional[str],
        igdb_id: Optional[int],
        platform_mapping_id: Optional[int],
        igdb_game_name: Optional[str],
    ) -> int:
        """Insert a request and return its id."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO requests
                    (user_id, username, platform, game_name, details,
                     igdb_id, platform_mapping_id, igdb_game_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, username, platform, game_name, details,
                 igdb_id, platform_mapping_id, igdb_game_name)
            )
            return cursor.lastrowid

    async def mark_fulfilled(self, request_id: int, *, by_id: int, by_name: str) -> None:
        await self._set_resolution(request_id, 'fulfilled', by_id=by_id, by_name=by_name)

    async def mark_rejected(
        self, request_id: int, *, by_id: int, by_name: str, reason: Optional[str]
    ) -> None:
        await self._set_resolution(
            request_id, 'reject', by_id=by_id, by_name=by_name, notes=reason
        )

    async def mark_cancelled(self, request_id: int, *, reason: Optional[str]) -> None:
        """A user withdrawing their own request - no fulfiller is recorded."""
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE requests
                SET status = 'cancelled',
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (reason, request_id)
            )

    async def mark_auto_fulfilled(
        self, request_id: int, *, by_id: int, by_name: str, notes: str
    ) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                """
                UPDATE requests
                SET status = 'fulfilled',
                    fulfilled_by = ?,
                    fulfiller_name = ?,
                    notes = ?,
                    auto_fulfilled = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (by_id, by_name, notes, request_id)
            )

    async def _set_resolution(
        self,
        request_id: int,
        status: str,
        *,
        by_id: int,
        by_name: str,
        notes: Optional[str] = None,
    ) -> None:
        assignments = "status = ?, fulfilled_by = ?, fulfiller_name = ?"
        params: List[Any] = [status, by_id, by_name]
        if notes is not None:
            assignments += ", notes = ?"
            params.append(notes)
        params.append(request_id)

        async with self.db.get_connection() as conn:
            await conn.execute(
                f"UPDATE requests SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                params
            )

    async def set_notes(self, request_id: int, notes: str) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE requests SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (notes, request_id)
            )

    async def set_ggr_request_id(self, request_id: int, ggr_request_id: int) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                "UPDATE requests SET ggr_request_id = ? WHERE id = ?",
                (ggr_request_id, request_id)
            )

    # ------------------------------------------------------------ subscribers

    async def subscriber_ids(self, request_id: int) -> List[int]:
        """Discord ids watching a request, besides the original requester."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT user_id FROM request_subscribers WHERE request_id = ?",
                (request_id,)
            )
            return [row['user_id'] for row in await cursor.fetchall()]

    async def subscribers_for_requests(self, request_ids: Sequence[int]) -> List[Any]:
        """Subscribers across several requests, for batch notification."""
        if not request_ids:
            return []
        placeholders = ','.join('?' * len(request_ids))
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT request_id, user_id FROM request_subscribers "
                f"WHERE request_id IN ({placeholders})",
                tuple(request_ids)
            )
            return await cursor.fetchall()

    async def count_subscribers(self, request_id: int) -> int:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS count FROM request_subscribers WHERE request_id = ?",
                (request_id,)
            )
            row = await cursor.fetchone()
            return row['count'] if row else 0

    async def is_subscribed(self, request_id: int, user_id: int) -> bool:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT COUNT(*) AS count FROM request_subscribers "
                "WHERE request_id = ? AND user_id = ?",
                (request_id, user_id)
            )
            row = await cursor.fetchone()
            return bool(row['count']) if row else False

    async def add_subscriber(self, request_id: int, user_id: int, username: str) -> None:
        async with self.db.get_connection() as conn:
            await conn.execute(
                "INSERT INTO request_subscribers (request_id, user_id, username) VALUES (?, ?, ?)",
                (request_id, user_id, username)
            )


class PlatformMappingsRepo:
    """Queries for the platform_mappings table.

    A separate table with its own lifecycle - it is synced from RomM rather
    than written by users - so it gets its own repository.
    """

    def __init__(self, db):
        self.db = db

    async def display_name_for(self, platform_name: str) -> Optional[str]:
        """The canonical display name for a platform, matched case-insensitively."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT display_name FROM platform_mappings
                WHERE LOWER(display_name) = LOWER(?) OR LOWER(folder_name) = LOWER(?)
                LIMIT 1
                """,
                (platform_name, platform_name)
            )
            row = await cursor.fetchone()
            return row['display_name'] if row else None

    async def get_by_display_name(self, display_name: str) -> Optional[Any]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, in_romm, romm_id, folder_name, igdb_slug, moby_slug
                FROM platform_mappings
                WHERE display_name = ?
                """,
                (display_name,)
            )
            return await cursor.fetchone()

    async def lookup_context(self, platform_name: str) -> Optional[Any]:
        """Id, display name and in_romm flag, matched on either name."""
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, display_name, in_romm
                FROM platform_mappings
                WHERE LOWER(display_name) = LOWER(?) OR LOWER(folder_name) = LOWER(?)
                LIMIT 1
                """,
                (platform_name, platform_name)
            )
            return await cursor.fetchone()

    async def is_in_romm(self, mapping_id: int) -> bool:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT in_romm FROM platform_mappings WHERE id = ?",
                (mapping_id,)
            )
            row = await cursor.fetchone()
            return bool(row['in_romm']) if row else False

    async def is_in_romm_by_name(self, display_name: str) -> bool:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT in_romm FROM platform_mappings WHERE LOWER(display_name) = LOWER(?)",
                (display_name,)
            )
            row = await cursor.fetchone()
            return bool(row['in_romm']) if row else False

    async def list_for_autocomplete(self) -> List[Any]:
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT display_name, in_romm, folder_name FROM platform_mappings "
                "ORDER BY in_romm DESC, display_name"
            )
            return await cursor.fetchall()

    async def platform_status_for(self, requests: Iterable[Any]) -> Dict[Any, bool]:
        """Pre-fetch in_romm for a page of requests.

        Keyed by mapping id where a request has one, and by "name:<platform>"
        otherwise, which is the shape the embed builder expects.
        """
        status: Dict[Any, bool] = {}
        for req in requests:
            mapping_id = req['platform_mapping_id']
            if mapping_id and mapping_id not in status:
                status[mapping_id] = await self.is_in_romm(mapping_id)

            if not mapping_id or mapping_id not in status:
                key = f"name:{req['platform']}"
                if key not in status:
                    status[key] = await self.is_in_romm_by_name(req['platform'])
        return status
