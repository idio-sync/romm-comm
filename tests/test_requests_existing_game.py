import unittest
import asyncio
from unittest.mock import patch

import discord

from cogs.requests import ExistingGameView, Request


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


class FakeRequestBot(FakeBot):
    def __init__(self, user):
        self.db = FakeDatabase()
        self.user = user

    async def fetch_user(self, user_id):
        return self.user


async def instant_sleep(delay):
    return None


class ExistingGameViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_rom_view_keeps_multi_file_selector(self):
        rom = {
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
        view = ExistingGameView(FakeBot(), [rom], "SNES", "Mega Man X", author_id=42)

        await view.create_full_rom_view(rom)

        file_selects = [
            item
            for item in view.children
            if isinstance(item, discord.ui.Select) and item.custom_id == "file_select"
        ]

        self.assertEqual(1, len(file_selects))


class RequestAutoFulfillmentTests(unittest.IsolatedAsyncioTestCase):
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
