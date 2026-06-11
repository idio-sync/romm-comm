import unittest
import asyncio
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
                rows=[(77, 42, "SNES", "Mega Man X", 12345, "Mega Man X")]
            )
        if "FROM request_subscribers" in query:
            return FakeCursor(rows=[])
        if "FROM platform_mappings" in query:
            return FakeCursor(row=("SNES",))
        if "SELECT ggr_request_id FROM requests" in query:
            return FakeCursor(row=("ggr-77",))
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
