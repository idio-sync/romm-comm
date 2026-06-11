import unittest

import discord

from cogs.requests import ExistingGameView


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
