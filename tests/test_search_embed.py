import unittest
from unittest.mock import patch

from cogs.search import NoResultsView, ROM_View


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


class FakeIGDB:
    def __init__(self, matches):
        self.matches = matches
        self.searches = []

    async def search_game(self, game_name, platform_name):
        self.searches.append((game_name, platform_name))
        return self.matches


class FakeRequestCog:
    requests_enabled = True

    def __init__(self, igdb_matches=None):
        self.legacy_calls = []
        self.platform_calls = []
        self.platform_context_calls = []
        self.igdb_enabled = igdb_matches is not None
        self.igdb = FakeIGDB(igdb_matches or [])

    async def check_if_game_exists(self, platform_name, game_name):
        return False, []

    async def get_platform_request_context(self, platform_name):
        self.platform_context_calls.append(platform_name)
        return "SNES", 7, True

    async def process_request(self, *args):
        self.legacy_calls.append(args)

    async def process_request_with_platform(self, *args):
        self.platform_calls.append(args)
        return 123


class FakeRequestBot(FakeBot):
    def __init__(self, request_cog):
        self.request_cog = request_cog

    def get_cog(self, name):
        if name == "Request":
            return self.request_cog
        return None


class FakeUser:
    def __init__(self, user_id=42):
        self.id = user_id


class FakeInteractionResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []

    async def defer(self):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        message = FakeMessage()
        self.messages.append((args, kwargs, message))
        return message


class FakeMessage:
    def __init__(self):
        self.edited_view = None

    async def edit(self, **kwargs):
        self.edited_view = kwargs.get("view")


class FakeInteraction:
    def __init__(self, user_id=42):
        self.user = FakeUser(user_id)
        self.response = FakeInteractionResponse()
        self.followup = FakeFollowup()
        self.message = FakeMessage()


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


class NoResultsRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_button_uses_platform_aware_request_flow(self):
        request_cog = FakeRequestCog()
        view = NoResultsView(
            FakeRequestBot(request_cog),
            "SNES",
            "Mega Man X",
            author_id=42,
        )

        interaction = FakeInteraction(user_id=42)
        request_button = next(item for item in view.children if item.label == "Request This Game")

        await request_button.callback(interaction)

        self.assertEqual(["SNES"], request_cog.platform_context_calls)
        self.assertEqual([], request_cog.legacy_calls)
        self.assertEqual(1, len(request_cog.platform_calls))

        call = request_cog.platform_calls[0]
        self.assertIs(interaction, call[0])
        self.assertEqual("SNES", call[1])
        self.assertEqual("Mega Man X", call[2])
        self.assertIsNone(call[3])
        self.assertIsNone(call[4])
        self.assertIsNone(call[5])
        self.assertEqual(7, call[6])
        self.assertTrue(call[7])

    async def test_request_button_with_igdb_selection_uses_platform_aware_request_flow(self):
        selected_game = {"id": 12345, "name": "Mega Man X"}
        request_cog = FakeRequestCog(igdb_matches=[selected_game])
        view = NoResultsView(
            FakeRequestBot(request_cog),
            "SNES",
            "Mega Man X",
            author_id=42,
        )

        interaction = FakeInteraction(user_id=42)
        request_button = next(item for item in view.children if item.label == "Request This Game")

        FakeGameSelectView.selected_game = selected_game
        with patch("cogs.requests.GameSelectView", FakeGameSelectView):
            await request_button.callback(interaction)

        self.assertEqual(["SNES"], request_cog.platform_context_calls)
        self.assertEqual([], request_cog.legacy_calls)
        self.assertEqual(1, len(request_cog.platform_calls))

        call = request_cog.platform_calls[0]
        self.assertIs(interaction, call[0])
        self.assertEqual("SNES", call[1])
        self.assertEqual("Mega Man X", call[2])
        self.assertIsNone(call[3])
        self.assertIs(selected_game, call[4])
        self.assertIs(interaction.followup.messages[0][2], call[5])
        self.assertEqual(7, call[6])
        self.assertTrue(call[7])
