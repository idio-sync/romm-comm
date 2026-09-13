"""Architecture test: the cogs still load, and still register their commands.

Every other test in this suite builds its subject with object.__new__ and sets
the attributes it needs, which is what makes the views and the cog testable at
all - but it means no test had ever run a cog's real __init__, called setup(),
or handed the result to py-cord.

That gap matters most for cogs/requests, which went from one module to ten. A
package that fails to import, a setup() that no longer calls add_cog, or a cog
whose __init__ raises would pass all of the other tests and fail at startup.

It also pins the slash command signatures. py-cord reads the Option
annotations when the decorator runs, so a change there is a registration-time
failure rather than a call-time one - which is the reason UP006 and UP007 are
deferred in pyproject.toml rather than autofixed.
"""

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord


def declared_cogs():
    """The extension list out of bot.py, read rather than restated.

    Keeps this honest if a cog is added, renamed or dropped.
    """
    tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "core_cogs" in targets and isinstance(node.value, ast.List):
                return [
                    element.value for element in node.value.elts
                    if isinstance(element, ast.Constant)
                ]
    raise AssertionError("core_cogs is no longer a plain list in bot.py")


def declared_dependencies():
    """The cog_dependencies map out of bot.py, read rather than restated.

    Nothing else checks this map. A cog missing from it loads without its
    import guard; a cog misspelled in it is simply never matched. Both fail
    silently at startup with a logged error and a skipped cog.
    """
    tree = ast.parse(Path("bot.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "cog_dependencies" in targets and isinstance(node.value, ast.Dict):
                return {
                    key.value: [
                        element.value for element in value.elts
                        if isinstance(element, ast.Constant)
                    ]
                    for key, value in zip(node.value.keys, node.value.values)
                    if isinstance(key, ast.Constant) and isinstance(value, ast.List)
                }
    raise AssertionError("cog_dependencies is no longer a plain dict in bot.py")


def fake_bot(loop):
    """A bot with what the requests cog touches, and nothing else.

    A real discord.Bot, so add_cog and the command registration are py-cord's
    own rather than a stand-in for them.
    """
    bot = discord.Bot(intents=discord.Intents.default())
    bot.loop = loop
    bot.db = object()
    bot.config = SimpleNamespace(
        REQUESTS_ENABLED=True,
        # Absent credentials are a supported configuration: the cog is
        # expected to come up with IGDB disabled rather than fail to load.
        IGDB_CLIENT_ID=None,
        IGDB_CLIENT_SECRET=None,
    )

    async def fetch_api_endpoint(*args, **kwargs):
        return None

    bot.fetch_api_endpoint = fetch_api_endpoint
    return bot


class ExtensionListTests(unittest.TestCase):
    def test_every_declared_cog_exists_and_can_be_loaded(self):
        """Each name in core_cogs resolves to a module exposing setup()."""
        import importlib

        for dotted in declared_cogs():
            with self.subTest(cog=dotted):
                module = importlib.import_module(dotted)
                self.assertTrue(
                    callable(getattr(module, "setup", None)),
                    f"{dotted} has no setup() for load_extension to call",
                )

    def test_the_requests_package_is_what_bot_py_loads(self):
        """It is a package now; the entry point moved to its __init__."""
        self.assertIn("cogs.requests", declared_cogs())
        self.assertTrue(Path("cogs/requests/__init__.py").exists())


class DependencyMapTests(unittest.TestCase):
    def test_every_declared_cog_has_a_dependency_entry(self):
        missing = set(declared_cogs()) - set(declared_dependencies())
        self.assertEqual(missing, set(), f"no cog_dependencies entry for {missing}")

    def test_the_dependency_map_names_no_cog_that_is_not_loaded(self):
        extra = set(declared_dependencies()) - set(declared_cogs())
        self.assertEqual(extra, set(), f"cog_dependencies names unloaded {extra}")

    def test_feeds_declares_aiohttp_for_its_probe(self):
        self.assertIn("aiohttp", declared_dependencies().get("cogs.feeds", []))


class RequestsExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def load(self):
        bot = fake_bot(asyncio.get_running_loop())
        bot.load_extension("cogs.requests")
        # __init__ spawns setup() as a task; let it finish so a failure in it
        # surfaces here rather than as an unretrieved exception at teardown.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return bot

    async def test_the_extension_loads_and_registers_the_cog(self):
        bot = await self.load()

        self.assertIsNotNone(bot.get_cog("Request"))

    async def test_it_registers_all_three_commands(self):
        bot = await self.load()

        self.assertEqual(
            ["my_requests", "request", "request_admin"],
            sorted(command.name for command in bot.get_cog("Request").get_commands()),
        )

    async def test_the_request_command_keeps_its_option_signature(self):
        """Read at decoration time, so a broken annotation never reaches a call."""
        bot = await self.load()
        command = next(
            c for c in bot.get_cog("Request").get_commands() if c.name == "request"
        )

        self.assertEqual(
            [("platform", True, True), ("game", True, False), ("details", False, False)],
            [(o.name, o.required, bool(o.autocomplete)) for o in command.options],
        )

    async def test_missing_igdb_credentials_disable_it_rather_than_fail_the_load(self):
        bot = await self.load()

        self.assertFalse(bot.get_cog("Request").igdb_enabled)
