"""Data access for the requests tables.

Today this holds only the column list. The queries themselves still sit in the
views and the cog; moving them here is the next step.
"""

# Explicit column list for the requests table, in schema order. Naming the
# columns rather than selecting them all means a database that grew its newer
# columns through ALTER TABLE - which appends in whatever order the migration
# happened to run - still hands back the columns in the order we expect.
REQUEST_COLUMNS = (
    "id, user_id, username, platform, game_name, details, status, "
    "created_at, updated_at, fulfilled_by, fulfiller_name, notes, "
    "auto_fulfilled, igdb_id, platform_mapping_id, igdb_game_name, ggr_request_id"
)
