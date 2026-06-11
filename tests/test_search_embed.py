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

    async def test_rom_download_url_uses_api_path_and_stable_filename_encoding(self):
        view = ROM_View(FakeBot(), [], author_id=42, platform_name="NES")

        url = view.build_rom_download_url(321, "Mega Man X (USA).zip")

        self.assertEqual(
            "https://romm.example/api/roms/321/content/Mega+Man+X+%28USA%29.zip",
            url,
        )

    async def test_rom_download_url_can_filter_by_file_ids(self):
        view = ROM_View(FakeBot(), [], author_id=42, platform_name="NES")

        url = view.build_rom_download_url(321, "Mega Man X.zip", file_ids=["11", "22"])

        self.assertEqual(
            "https://romm.example/api/roms/321/content/Mega+Man+X.zip?file_ids=11,22",
            url,
        )


class SearchDownloadUrlBoundaryTests(unittest.TestCase):
    def test_search_does_not_build_non_api_rom_content_urls(self):
        source = __import__("pathlib").Path("cogs/search.py").read_text(encoding="utf-8")

        self.assertNotIn("}/roms/{", source)
