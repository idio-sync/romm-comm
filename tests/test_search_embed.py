import unittest

from cogs.search import ROM_View


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


class SearchEmbedTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_rom_embed_handles_missing_metadatum(self):
        rom = {
            "id": 123,
            "name": "Metadata Free Game",
            "fs_name": "Metadata Free Game.zip",
            "platform_id": 7,
            "summary": "A game without metadata should still render.",
        }
        view = ROM_View(FakeBot(), [rom], author_id=42, platform_name="NES")

        embed, cover_file = await view.create_rom_embed(rom)

        self.assertEqual("Metadata Free Game", embed.title)
        self.assertIsNone(cover_file)
