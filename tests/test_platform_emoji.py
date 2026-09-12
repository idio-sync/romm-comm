"""Tests for naming a platform with its emoji.

This was a method on the Search cog that five other cogs reached for by
string. Its rules are a three-step fallback that nothing had ever exercised.
"""

import unittest
from types import SimpleNamespace

from cogs.platform_emoji import PLATFORM_VARIANTS, PlatformEmoji

FALLBACK = "🎮"


class FakeEmoji:
    """Stands in for a discord.Emoji, which renders as its markdown form."""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"<:{self.name}:1>"


def service(server_emojis=(), app_emojis=None):
    bot = SimpleNamespace(emojis=[FakeEmoji(n) for n in server_emojis])
    if app_emojis is not None:
        bot.emoji_dict = {n: f"<a:{n}:2>" for n in app_emojis}
    return PlatformEmoji(bot)


class FallbackOrderTests(unittest.TestCase):
    def test_a_server_emoji_wins(self):
        formatted = service(server_emojis=["n64"], app_emojis=["n64"]).format("Nintendo 64")

        self.assertEqual("Nintendo 64 <:n64:1>", formatted)

    def test_an_application_emoji_is_used_when_the_server_has_none(self):
        formatted = service(server_emojis=[], app_emojis=["n64"]).format("Nintendo 64")

        self.assertEqual("Nintendo 64 <a:n64:2>", formatted)

    def test_a_generic_emoji_when_neither_has_one(self):
        self.assertEqual(f"Nintendo 64 {FALLBACK}", service().format("Nintendo 64"))

    def test_a_bot_with_no_emoji_dict_at_all_still_formats(self):
        """emoji_dict is populated asynchronously, so it can be absent."""
        self.assertEqual(f"Nintendo 64 {FALLBACK}", service(app_emojis=None).format("Nintendo 64"))


class VariantTests(unittest.TestCase):
    def test_the_first_matching_variant_wins(self):
        """Game Boy lists gameboy before gameboy_pocket; both may exist."""
        both = service(server_emojis=["gameboy", "gameboy_pocket"])

        self.assertEqual("Game Boy <:gameboy:1>", both.format("Game Boy"))

    def test_a_later_variant_is_used_when_the_first_is_missing(self):
        self.assertEqual(
            "Game Boy <:gameboy_pocket:1>",
            service(server_emojis=["gameboy_pocket"]).format("Game Boy"),
        )

    def test_an_unlisted_platform_derives_its_own_name(self):
        """Spaces and hyphens become underscores, so a matching emoji is found
        without the platform needing a table entry."""
        derived = service(server_emojis=["some_new_console"])

        self.assertEqual("Some New Console <:some_new_console:1>", derived.format("Some New Console"))
        self.assertEqual("Some-New-Console <:some_new_console:1>", derived.format("Some-New-Console"))

    def test_two_platforms_may_share_one_emoji(self):
        """Famicom and Family Computer are the same machine."""
        shared = service(server_emojis=["famicom"])

        self.assertEqual(shared.format("Famicom"), "Famicom <:famicom:1>")
        self.assertEqual(shared.format("Family Computer"), "Family Computer <:famicom:1>")


class EdgeCaseTests(unittest.TestCase):
    def test_an_empty_name_is_returned_unchanged(self):
        """No name means no emoji and no stray fallback."""
        self.assertEqual("", service(server_emojis=["n64"]).format(""))

    def test_none_is_returned_unchanged(self):
        self.assertIsNone(service().format(None))

    def test_an_unrelated_emoji_is_not_used(self):
        self.assertEqual(f"Nintendo 64 {FALLBACK}", service(server_emojis=["snes"]).format("Nintendo 64"))


class TableTests(unittest.TestCase):
    def test_every_entry_maps_to_a_list_of_names(self):
        for platform, variants in PLATFORM_VARIANTS.items():
            self.assertIsInstance(variants, list, platform)
            self.assertTrue(variants, f"{platform} has no variants")
            for variant in variants:
                self.assertIsInstance(variant, str, platform)

    def test_variant_names_are_emoji_safe(self):
        """Discord emoji names are alphanumerics and underscores only, so a
        variant with a space or hyphen could never match anything."""
        for platform, variants in PLATFORM_VARIANTS.items():
            for variant in variants:
                self.assertTrue(
                    variant.replace("_", "").isalnum(),
                    f"{platform} -> {variant!r} cannot be a Discord emoji name",
                )


if __name__ == "__main__":
    unittest.main()
