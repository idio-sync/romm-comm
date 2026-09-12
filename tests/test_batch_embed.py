"""Tests for the new-ROMs batch announcement.

Two shapes of message: one names every game, the other summarises by platform
once a scan adds more than the bulk threshold. Both were inside a 187-line
create_batch_embed, downstream of ~95 lines of parallel cover downloading, so
neither could be checked without a RomM server.
"""

import unittest

from cogs.batch_embed import (
    MAX_GAMES_PER_PLATFORM,
    MAX_PLATFORMS_LISTED,
    add_batch_footer,
    build_batch_embed,
    build_bulk_embed,
    count_by_platform,
    group_names_by_platform,
)
from cogs.rom_embed import ROMM_LOGO


def platform_display(name):
    return f":{name}: {name}"


def roms(count, platform="Nintendo 64", start=0):
    return [
        {"id": start + i, "name": f"Game {start + i}", "platform_name": platform}
        for i in range(count)
    ]


def field(embed, name):
    for candidate in embed.fields:
        if candidate.name == name:
            return candidate.value
    return None


class GroupingTests(unittest.TestCase):
    def test_names_are_grouped_under_their_platform(self):
        grouped = group_names_by_platform(roms(2) + roms(1, "SNES", 100))

        self.assertEqual(["Game 0", "Game 1"], grouped["Nintendo 64"])
        self.assertEqual(["Game 100"], grouped["SNES"])

    def test_a_rom_without_a_platform_is_grouped_as_unknown(self):
        grouped = group_names_by_platform([{"id": 1, "name": "Mystery"}])

        self.assertEqual(["Mystery"], grouped["Unknown"])

    def test_counting_agrees_with_grouping(self):
        payload = roms(3) + roms(2, "SNES", 100)

        counts = count_by_platform(payload)
        grouped = group_names_by_platform(payload)

        self.assertEqual({k: len(v) for k, v in grouped.items()}, dict(counts))


class BulkEmbedTests(unittest.TestCase):
    def build(self, payload):
        return build_bulk_embed(payload, platform_display=platform_display)

    def test_it_counts_rather_than_names(self):
        embed = self.build(roms(30))

        self.assertIn("30 games", embed.description)
        self.assertIn("30 ROMs", field(embed, "Platforms Updated"))
        self.assertNotIn("Game 0", field(embed, "Platforms Updated"))

    def test_the_busiest_platform_comes_first(self):
        """With a hundred additions, which parts of the collection grew is the
        useful question - so this list is by count, not alphabetical."""
        embed = self.build(roms(2, "Aaa") + roms(9, "Zzz", 100))

        lines = field(embed, "Platforms Updated").splitlines()
        self.assertIn("Zzz", lines[0])
        self.assertIn("Aaa", lines[1])

    def test_platforms_beyond_the_limit_are_counted_not_listed(self):
        payload = sum((roms(1, f"Platform {i}", i * 10) for i in range(14)), [])

        embed = self.build(payload)

        lines = field(embed, "Platforms Updated").splitlines()
        self.assertEqual(MAX_PLATFORMS_LISTED + 1, len(lines))
        self.assertIn("and 4 more platforms", lines[-1])

    def test_exactly_the_limit_adds_no_overflow_line(self):
        payload = sum((roms(1, f"Platform {i}", i * 10) for i in range(MAX_PLATFORMS_LISTED)), [])

        lines = field(self.build(payload), "Platforms Updated").splitlines()

        self.assertEqual(MAX_PLATFORMS_LISTED, len(lines))
        self.assertNotIn("more platforms", lines[-1])

    def test_it_points_at_search_since_it_names_nothing(self):
        self.assertIn("/search", field(self.build(roms(30)), "📋 Note"))

    def test_it_carries_the_project_thumbnail_and_no_cover(self):
        embed = self.build(roms(30))

        self.assertEqual(ROMM_LOGO, embed.thumbnail.url)
        self.assertIsNone(embed.image)


class BatchEmbedTests(unittest.TestCase):
    def build(self, payload, has_composite=False):
        return build_batch_embed(
            payload,
            platform_display=platform_display,
            romm_url="https://romm.example",
            romm_emoji=":romm:",
            has_composite=has_composite,
        )

    def test_every_game_is_named(self):
        embed = self.build(roms(3))

        self.assertIn("3 New Games", embed.title)
        self.assertEqual(
            ["• Game 0", "• Game 1", "• Game 2"],
            field(embed, ":Nintendo 64: Nintendo 64").splitlines(),
        )

    def test_platforms_are_listed_alphabetically(self):
        embed = self.build(roms(1, "Zzz") + roms(1, "Aaa", 100))

        names = [f.name for f in embed.fields]
        self.assertLess(names.index(":Aaa: Aaa"), names.index(":Zzz: Zzz"))

    def test_games_beyond_the_limit_are_counted_not_named(self):
        embed = self.build(roms(MAX_GAMES_PER_PLATFORM + 3))

        lines = field(embed, ":Nintendo 64: Nintendo 64").splitlines()
        self.assertEqual(MAX_GAMES_PER_PLATFORM + 1, len(lines))
        self.assertIn("and 3 more", lines[-1])

    def test_exactly_the_limit_adds_no_overflow_line(self):
        embed = self.build(roms(MAX_GAMES_PER_PLATFORM))

        lines = field(embed, ":Nintendo 64: Nintendo 64").splitlines()
        self.assertEqual(MAX_GAMES_PER_PLATFORM, len(lines))
        self.assertNotIn("more", lines[-1])

    def test_the_collection_link_is_always_last(self):
        embed = self.build(roms(2))

        self.assertEqual("View Collection", embed.fields[-1].name)
        self.assertIn("https://romm.example", embed.fields[-1].value)

    def test_a_composite_becomes_the_embed_image(self):
        with_cover = self.build(roms(2), has_composite=True)
        without = self.build(roms(2), has_composite=False)

        self.assertEqual("attachment://composite_cover.png", with_cover.image.url)
        self.assertIsNone(without.image)


class FooterTests(unittest.TestCase):
    def test_both_shapes_get_the_same_footer(self):
        bulk = add_batch_footer(build_bulk_embed(roms(30), platform_display=platform_display), 30)
        batch = add_batch_footer(
            build_batch_embed(
                roms(3),
                platform_display=platform_display,
                romm_url="https://romm.example",
                romm_emoji=":romm:",
                has_composite=False,
            ),
            3,
        )

        self.assertIn("30 new games", bulk.footer.text)
        self.assertIn("3 new games", batch.footer.text)
        self.assertIn("/search", bulk.footer.text)


if __name__ == "__main__":
    unittest.main()
