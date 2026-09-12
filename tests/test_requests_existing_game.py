import asyncio
import sqlite3
import unittest
from unittest.mock import patch

import discord

from cogs.requests import ExistingGameView, ExistingGameWithIGDBView, Request


class FakeConfig:
    API_BASE_URL = "https://romm.example"
    DOMAIN = "https://romm.example"


class FakeBot:
    config = FakeConfig()
    cache = {}

    async def fetch_api_endpoint(self, endpoint):
        return None

    def get_cog(self, name):
        return None

    def get_formatted_emoji(self, name):
        return f":{name}:"


# MasterDatabase.get_connection sets row_factory, so every row the cog sees
# is an sqlite3.Row addressed by column name. Build real ones here rather than
# tuples, so these doubles cannot drift from what production hands back.
_ROW_FACTORY_CONN = sqlite3.connect(":memory:")
_ROW_FACTORY_CONN.row_factory = sqlite3.Row


def row(**columns):
    """Build a real sqlite3.Row with the given column names and values."""
    selected = ", ".join(f"? AS {name}" for name in columns)
    return _ROW_FACTORY_CONN.execute(f"SELECT {selected}", tuple(columns.values())).fetchone()


class FakeCursor:
    def __init__(self, rows=None, row=None):
        self.rows = rows or []
        self.row = row

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.row


class FakeConnection:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, params=None):
        if "FROM requests WHERE status = 'pending'" in query:
            return FakeCursor(
                rows=[
                    row(
                        id=77,
                        user_id=42,
                        platform="SNES",
                        game_name="Mega Man X",
                        igdb_id=12345,
                        igdb_game_name="Mega Man X",
                    )
                ]
            )
        if "FROM request_subscribers" in query:
            return FakeCursor(rows=[])
        if "FROM platform_mappings" in query:
            return FakeCursor(row=row(display_name="SNES"))
        if "SELECT ggr_request_id FROM requests" in query:
            return FakeCursor(row=row(ggr_request_id="ggr-77"))
        return FakeCursor()

    async def executemany(self, query, params):
        return None

    async def commit(self):
        return None


class FakeDatabase:
    def get_connection(self):
        return FakeConnection()


class FakeGGR:
    enabled = True

    async def update_request_status(self, **kwargs):
        return {"success": True}


class FakeUser:
    def __init__(self):
        self.messages = []

    async def send(self, message):
        self.messages.append(message)


class FakeInteractionUser:
    def __init__(self, user_id):
        self.id = user_id


class FakeInteractionResponse:
    def __init__(self):
        self.edited_view = None
        self.sent_messages = []

    async def edit_message(self, **kwargs):
        self.edited_view = kwargs.get("view")

    async def send_message(self, *args, **kwargs):
        self.sent_messages.append((args, kwargs))


class FakeInteraction:
    def __init__(self, user_id=42, values=None):
        self.user = FakeInteractionUser(user_id)
        self.data = {"values": values or []}
        self.response = FakeInteractionResponse()


class FakeRequestBot(FakeBot):
    def __init__(self, user):
        self.db = FakeDatabase()
        self.user = user

    async def fetch_user(self, user_id):
        return self.user


class FakePlatformAwareRequestCog:
    def __init__(self):
        self.legacy_calls = []
        self.platform_calls = []
        self.platform_context_calls = []

    async def get_platform_request_context(self, platform_name):
        self.platform_context_calls.append(platform_name)
        return "SNES", 7, True

    async def process_request(self, *args):
        self.legacy_calls.append(args)

    async def process_request_with_platform(self, *args):
        self.platform_calls.append(args)
        return 123


class FakeBotWithRequestCog(FakeBot):
    def __init__(self, request_cog):
        self.request_cog = request_cog

    def get_cog(self, name):
        if name == "Request":
            return self.request_cog
        return None


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        message = FakeMessage()
        self.messages.append((args, kwargs, message))
        return message


class FakeMessage:
    pass


class FakeContext:
    def __init__(self):
        self.followup = FakeFollowup()


class FakeGameSelectView:
    selected_game = None

    def __init__(self, bot, matches, platform_name):
        self.bot = bot
        self.matches = matches
        self.platform_name = platform_name
        self.message = None

    def create_game_embed(self, selected_game):
        return object()

    async def wait(self):
        self.selected_game = self.__class__.selected_game


async def instant_sleep(delay):
    return None


def make_multi_file_rom():
    return {
        "id": 321,
        "name": "Mega Man X",
        "fs_name": "Mega Man X.zip",
        "platform_id": 7,
        "igdb": {},
        "multi": True,
        "files": [
            {
                "id": 11,
                "file_name": "Mega Man X.sfc",
                "file_size_bytes": 1024,
            },
            {
                "id": 22,
                "file_name": "Mega Man X Manual.pdf",
                "file_size_bytes": 2048,
                "category": "manual",
            },
        ],
    }


class ExistingGameViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_rom_view_keeps_multi_file_selector(self):
        rom = make_multi_file_rom()
        view = ExistingGameView(FakeBot(), [rom], "SNES", "Mega Man X", author_id=42)

        await view.create_full_rom_view(rom)

        file_selects = [
            item
            for item in view.children
            if isinstance(item, discord.ui.Select) and item.custom_id == "file_select"
        ]

        self.assertEqual(1, len(file_selects))

    async def test_full_rom_view_keeps_multi_file_download_all_button(self):
        rom = make_multi_file_rom()
        view = ExistingGameView(FakeBot(), [rom], "SNES", "Mega Man X", author_id=42)

        await view.create_full_rom_view(rom)

        download_labels = [
            item.label
            for item in view.children
            if isinstance(item, discord.ui.Button) and "Download" in item.label
        ]

        self.assertIn("Download All (2 files, 3.00 KB)", download_labels)

    async def test_file_selection_keeps_existing_game_view_controls(self):
        rom = make_multi_file_rom()
        view = ExistingGameView(FakeBot(), [rom], "SNES", "Mega Man X", author_id=42)

        await view.create_full_rom_view(rom)

        file_select = next(
            item
            for item in view.children
            if isinstance(item, discord.ui.Select) and item.custom_id == "file_select"
        )

        interaction = FakeInteraction(values=["file_0"])
        await file_select.callback(interaction)

        self.assertIs(view, interaction.response.edited_view)

        button_labels = [
            item.label for item in view.children if isinstance(item, discord.ui.Button)
        ]
        self.assertIn("Request Different Version", button_labels)
        self.assertIn("Download Selected (1 file, 1.00 KB)", button_labels)

    async def test_igdb_existing_file_selection_keeps_existing_game_view(self):
        rom = make_multi_file_rom()
        view = ExistingGameWithIGDBView(
            FakeBot(), [rom], [], "SNES", "Mega Man X", author_id=42
        )

        await view.show_rom_for_download(FakeInteraction(), rom)

        file_select = next(
            item
            for item in view.children
            if isinstance(item, discord.ui.Select) and item.custom_id == "file_select"
        )

        interaction = FakeInteraction(values=["file_0"])
        await file_select.callback(interaction)

        self.assertIs(view, interaction.response.edited_view)

    async def test_igdb_existing_request_uses_platform_aware_request_flow(self):
        rom = make_multi_file_rom()
        selected_game = {
            "id": 12345,
            "name": "Mega Man Xtreme",
            "release_date": "2000",
            "platforms": ["Game Boy Color"],
        }
        request_cog = FakePlatformAwareRequestCog()
        view = ExistingGameWithIGDBView(
            FakeBotWithRequestCog(request_cog),
            [rom],
            [selected_game],
            "SNES",
            "Mega Man X",
            author_id=42,
        )
        view.message = object()

        interaction = FakeInteraction(values=["0"])
        await view.igdb_select_callback(interaction)

        self.assertEqual(["SNES"], request_cog.platform_context_calls)
        self.assertEqual([], request_cog.legacy_calls)
        self.assertEqual(1, len(request_cog.platform_calls))

        call = request_cog.platform_calls[0]
        self.assertIs(interaction, call[0])
        self.assertEqual("SNES", call[1])
        self.assertEqual("Mega Man Xtreme", call[2])
        self.assertIsNone(call[3])
        self.assertIs(selected_game, call[4])
        self.assertIs(view.message, call[5])
        self.assertEqual(7, call[6])
        self.assertTrue(call[7])


class RequestAutoFulfillmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_fulfillment_dm_is_sent_without_ggrequestz(self):
        user = FakeUser()
        bot = FakeRequestBot(user)
        request = Request.__new__(Request)
        request.bot = bot
        request.db = bot.db
        request.ggr = None
        request.processing_lock = asyncio.Lock()

        new_games = [
            {
                "id": 321,
                "name": "Mega Man X",
                "platform": "SNES",
                "igdb_id": 12345,
                "fs_name": "Mega Man X (USA).zip",
            }
        ]

        with patch("cogs.requests.asyncio.sleep", instant_sleep):
            await request.on_batch_scan_complete(new_games)

        self.assertEqual(1, len(user.messages))

    async def test_auto_fulfillment_dm_uses_stable_download_url(self):
        user = FakeUser()
        bot = FakeRequestBot(user)
        request = Request.__new__(Request)
        request.bot = bot
        request.db = bot.db
        request.ggr = FakeGGR()
        request.processing_lock = asyncio.Lock()

        new_games = [
            {
                "id": 321,
                "name": "Mega Man X",
                "platform": "SNES",
                "igdb_id": 12345,
                "fs_name": "Mega Man X (USA).zip",
            }
        ]

        with patch("cogs.requests.asyncio.sleep", instant_sleep):
            await request.on_batch_scan_complete(new_games)

        self.assertEqual(1, len(user.messages))
        self.assertIn(
            "[**Download**](https://romm.example/api/roms/321/content/Mega+Man+X+%28USA%29.zip)",
            user.messages[0],
        )


class RequestContinueFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_process_request_delegates_to_platform_aware_request_flow(self):
        request = Request.__new__(Request)
        platform_context_calls = []
        platform_calls = []
        selected_game = {"id": 12345, "name": "Mega Man X"}
        message = object()

        async def get_platform_request_context(platform_name):
            platform_context_calls.append(platform_name)
            return "SNES", 7, True

        async def process_request_with_platform(*args):
            platform_calls.append(args)
            return 123

        request.get_platform_request_context = get_platform_request_context
        request.process_request_with_platform = process_request_with_platform
        ctx = object()

        result = await request.process_request(
            ctx,
            "SNES",
            "Mega Man X",
            "Version Request: USA",
            selected_game,
            message,
        )

        self.assertEqual(123, result)
        self.assertEqual(["SNES"], platform_context_calls)
        self.assertEqual(1, len(platform_calls))

        call = platform_calls[0]
        self.assertIs(ctx, call[0])
        self.assertEqual("SNES", call[1])
        self.assertEqual("Mega Man X", call[2])
        self.assertEqual("Version Request: USA", call[3])
        self.assertIs(selected_game, call[4])
        self.assertIs(message, call[5])
        self.assertEqual(7, call[6])
        self.assertTrue(call[7])

    async def test_continue_request_flow_without_igdb_uses_platform_aware_request_flow(self):
        request = Request.__new__(Request)
        legacy_calls = []
        platform_calls = []
        platform_context_calls = []

        async def get_platform_request_context(platform_name):
            platform_context_calls.append(platform_name)
            return "SNES", 7, True

        async def process_request(*args):
            legacy_calls.append(args)

        async def process_request_with_platform(*args):
            platform_calls.append(args)
            return 123

        request.get_platform_request_context = get_platform_request_context
        request.process_request = process_request
        request.process_request_with_platform = process_request_with_platform
        ctx = object()

        await request.continue_request_flow(
            ctx,
            "SNES",
            "Mega Man X",
            "Version Request: USA",
            [],
        )

        self.assertEqual(["SNES"], platform_context_calls)
        self.assertEqual([], legacy_calls)
        self.assertEqual(1, len(platform_calls))

        call = platform_calls[0]
        self.assertIs(ctx, call[0])
        self.assertEqual("SNES", call[1])
        self.assertEqual("Mega Man X", call[2])
        self.assertEqual("Version Request: USA", call[3])
        self.assertIsNone(call[4])
        self.assertIsNone(call[5])
        self.assertEqual(7, call[6])
        self.assertTrue(call[7])

    async def test_continue_request_flow_with_igdb_uses_platform_aware_request_flow(self):
        request = Request.__new__(Request)
        legacy_calls = []
        platform_calls = []
        platform_context_calls = []
        selected_game = {"id": 12345, "name": "Mega Man X"}

        async def get_platform_request_context(platform_name):
            platform_context_calls.append(platform_name)
            return "SNES", 7, True

        async def process_request(*args):
            legacy_calls.append(args)

        async def process_request_with_platform(*args):
            platform_calls.append(args)
            return 123

        request.bot = FakeBot()
        request.get_platform_request_context = get_platform_request_context
        request.process_request = process_request
        request.process_request_with_platform = process_request_with_platform
        ctx = FakeContext()

        FakeGameSelectView.selected_game = selected_game
        with patch("cogs.requests.GameSelectView", FakeGameSelectView):
            await request.continue_request_flow(
                ctx,
                "SNES",
                "Mega Man X",
                "Version Request: USA",
                [selected_game],
            )

        self.assertEqual(["SNES"], platform_context_calls)
        self.assertEqual([], legacy_calls)
        self.assertEqual(1, len(platform_calls))

        call = platform_calls[0]
        self.assertIs(ctx, call[0])
        self.assertEqual("SNES", call[1])
        self.assertEqual("Mega Man X", call[2])
        self.assertEqual("Version Request: USA", call[3])
        self.assertIs(selected_game, call[4])
        self.assertIs(ctx.followup.messages[0][2], call[5])
        self.assertEqual(7, call[6])
        self.assertTrue(call[7])
