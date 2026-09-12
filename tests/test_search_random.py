import unittest

from cogs.search import Search


class FakeBot:
    """Records endpoint calls and answers them from a canned mapping."""

    def __init__(self, responses=None, cache=None):
        self.responses = responses or {}
        self.cache = cache if cache is not None else {}
        self.calls = []

    async def fetch_api_endpoint(self, endpoint, bypass_cache=False):
        self.calls.append(endpoint)
        value = self.responses.get(endpoint)
        if isinstance(value, Exception):
            raise value
        return value


def build_search(responses=None, cache=None):
    cog = object.__new__(Search)
    cog.bot = FakeBot(responses, cache)
    return cog


SIMPLE_ROM = {"id": 7, "name": "Chrono Trigger", "platform_id": 3}
DETAILED_ROM = {"id": 7, "name": "Chrono Trigger", "platform_id": 3, "files": ["a"]}


class FetchRandomRomTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_scope_is_sent_as_platform_ids(self):
        cog = build_search({
            "roms/random?platform_ids=3": SIMPLE_ROM,
            "roms/7": DETAILED_ROM,
        })

        rom = await cog.fetch_random_rom(platform_id=3)

        self.assertEqual(DETAILED_ROM, rom)
        self.assertEqual("roms/random?platform_ids=3", cog.bot.calls[0])

    async def test_whole_library_sends_no_filter(self):
        cog = build_search({"roms/random": SIMPLE_ROM, "roms/7": DETAILED_ROM})

        rom = await cog.fetch_random_rom()

        self.assertEqual(DETAILED_ROM, rom)
        self.assertEqual("roms/random", cog.bot.calls[0])

    async def test_missing_endpoint_reads_as_none(self):
        # A RomM older than 5.2.0 has no such route; fetch_api_endpoint gives
        # back None, and the caller falls back from there.
        cog = build_search({"roms/random": None})

        self.assertIsNone(await cog.fetch_random_rom())

    async def test_raising_endpoint_reads_as_none(self):
        cog = build_search({"roms/random": RuntimeError("404")})

        self.assertIsNone(await cog.fetch_random_rom())

    async def test_null_answer_for_an_empty_scope_reads_as_none(self):
        cog = build_search({"roms/random?platform_ids=99": None})

        self.assertIsNone(await cog.fetch_random_rom(platform_id=99))

    async def test_simple_rom_is_upgraded_to_the_full_record(self):
        cog = build_search({"roms/random": SIMPLE_ROM, "roms/7": DETAILED_ROM})

        rom = await cog.fetch_random_rom()

        self.assertEqual(["roms/random", "roms/7"], cog.bot.calls)
        self.assertIn("files", rom, "the embed needs the detailed record")

    async def test_simple_rom_is_kept_when_the_detail_fetch_fails(self):
        cog = build_search({"roms/random": SIMPLE_ROM, "roms/7": None})

        self.assertEqual(SIMPLE_ROM, await cog.fetch_random_rom())


class PlatformNameForRomTests(unittest.TestCase):
    def test_name_is_resolved_from_the_platform_cache(self):
        cog = build_search(cache={"platforms": [{"id": 3, "name": "SNES"}]})

        self.assertEqual("SNES", cog.platform_name_for_rom(SIMPLE_ROM))

    def test_unknown_platform_gives_none(self):
        cog = build_search(cache={"platforms": [{"id": 99, "name": "Genesis"}]})

        self.assertIsNone(cog.platform_name_for_rom(SIMPLE_ROM))

    def test_empty_cache_gives_none(self):
        cog = build_search(cache={})

        self.assertIsNone(cog.platform_name_for_rom(SIMPLE_ROM))

    def test_rom_without_a_platform_gives_none(self):
        cog = build_search(cache={"platforms": [{"id": 3, "name": "SNES"}]})

        self.assertIsNone(cog.platform_name_for_rom({"id": 7}))


class PickRandomRomLegacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_platform_path_chooses_from_the_listing(self):
        cog = build_search({
            "roms?platform_id=3&platform_ids=3&limit=2": {"items": [SIMPLE_ROM]},
            "roms/7": DETAILED_ROM,
        })

        rom = await cog.pick_random_rom_legacy(platform_id=3, rom_count=2)

        self.assertEqual(DETAILED_ROM, rom)

    async def test_bare_list_response_is_accepted(self):
        cog = build_search({
            "roms?platform_id=3&platform_ids=3&limit=2": [SIMPLE_ROM],
            "roms/7": DETAILED_ROM,
        })

        self.assertEqual(DETAILED_ROM, await cog.pick_random_rom_legacy(platform_id=3, rom_count=2))

    async def test_collection_path_guesses_at_rom_ids(self):
        cog = build_search({"roms/1": DETAILED_ROM})

        rom = await cog.pick_random_rom_legacy(total_roms=1)

        self.assertEqual(DETAILED_ROM, rom)
