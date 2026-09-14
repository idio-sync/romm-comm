"""Tests for the ROM embed's formatting.

These were the pure middle of a 266-line create_rom_embed and could only be
reached by constructing a Discord view and answering its API calls. The file
listing in particular is intricate - subfolder grouping, a character budget,
two different sort orders - and had no coverage at all.
"""

import unittest

from cogs.rom_embed import (
    MAX_FILE_LISTING_CHARS,
    build_file_listing,
    build_links_value,
    build_single_file_field,
    file_subfolder,
    first_two,
    format_file_size,
    format_release_date,
    subfolder_icon,
    truncate_field,
)

# GoldenEye 007's release, 1997-08-25 UTC. Rendered in local time, so the
# tests derive the expected string rather than asserting that date.
GOLDENEYE_RELEASE = 872467200


def emoji(name):
    return f":{name}:"


def rom_file(name, size=1024, **extra):
    return {"file_name": name, "file_size_bytes": size, **extra}


class FormatFileSizeTests(unittest.TestCase):
    def test_each_unit_boundary(self):
        self.assertEqual("512.00 B", format_file_size(512))
        self.assertEqual("1.00 KB", format_file_size(1024))
        self.assertEqual("1.00 MB", format_file_size(1024 ** 2))
        self.assertEqual("1.00 GB", format_file_size(1024 ** 3))
        self.assertEqual("1.00 TB", format_file_size(1024 ** 4))

    def test_it_stops_at_terabytes(self):
        self.assertEqual("1024.00 TB", format_file_size(1024 ** 5))

    def test_unusable_values_say_so(self):
        for value in (0, None, "", "big", [], False):
            self.assertEqual("Unknown size", format_file_size(value))


class SubfolderTests(unittest.TestCase):
    def test_the_backend_category_wins(self):
        self.assertEqual("dlc", file_subfolder({"category": "DLC", "file_path": "x/patch/y"}))

    def test_the_path_is_searched_when_there_is_no_category(self):
        self.assertEqual("patch", file_subfolder({"file_path": "roms/Patch/thing.bin"}))

    def test_a_path_naming_nothing_known_is_root(self):
        self.assertIsNone(file_subfolder({"file_path": "roms/whatever/thing.bin"}))

    def test_no_path_and_no_category_is_root(self):
        self.assertIsNone(file_subfolder({}))

    def test_every_known_subfolder_has_its_own_icon(self):
        icons = {subfolder_icon(name) for name in
                 ("hack", "dlc", "manual", "mod", "patch", "update", "demo",
                  "translation", "prototype", "cheat")}
        self.assertEqual(10, len(icons), "two subfolders share an icon")

    def test_root_and_unknown_get_different_defaults(self):
        self.assertNotEqual(subfolder_icon(None), subfolder_icon("something-else"))


class ReleaseDateTests(unittest.TestCase):
    def test_a_timestamp_in_seconds(self):
        # IGDB stores a release date as UTC midnight, so the same date has to
        # come out on every host. Deriving the expectation with the same call
        # the code under test uses would assert nothing.
        self.assertEqual("Aug 25, 1997", format_release_date(GOLDENEYE_RELEASE))

    def test_a_timestamp_in_milliseconds_means_the_same_day(self):
        """IGDB is inconsistent about the unit; both must land on one date."""
        self.assertEqual(
            format_release_date(GOLDENEYE_RELEASE),
            format_release_date(GOLDENEYE_RELEASE * 1000),
        )

    def test_missing_and_unreadable_values_render_as_nothing(self):
        for value in (None, 0, "", "not a date", [1]):
            self.assertIsNone(format_release_date(value))


class FieldHelperTests(unittest.TestCase):
    def test_first_two_of_a_longer_list(self):
        self.assertEqual("A, B", first_two(["A", "B", "C"]))

    def test_a_plain_value_passes_through(self):
        self.assertEqual("Shooter", first_two("Shooter"))

    def test_truncation_respects_discords_limit(self):
        value = truncate_field("x" * 2000)
        self.assertEqual(1024, len(value))
        self.assertTrue(value.endswith("..."))

    def test_a_short_value_is_untouched(self):
        self.assertEqual("short", truncate_field("short"))


class LinksTests(unittest.TestCase):
    def build(self, rom_data, pcgw_url=None):
        return build_links_value(
            rom_data,
            romm_url="https://romm.example/rom/7",
            igdb_url="https://www.igdb.com/games/goldeneye-007",
            pcgw_url=pcgw_url,
            emoji=emoji,
        )

    def test_romm_and_igdb_are_always_present(self):
        value = self.build({})

        self.assertIn("RomM", value)
        self.assertIn("IGDB", value)
        self.assertNotIn("\n", value, "no second row when there is nothing for it")

    def test_a_trailer_joins_the_first_row(self):
        value = self.build({"youtube_video_id": "abc123"})

        self.assertIn("https://www.youtube.com/watch?v=abc123", value)
        self.assertNotIn("\n", value)

    def test_achievements_and_pcgamingwiki_share_the_second_row(self):
        value = self.build({"ra_id": 555}, pcgw_url="https://pcgamingwiki.com/x")

        first_row, second_row = value.split("\n")
        self.assertIn("RomM", first_row)
        self.assertIn("https://retroachievements.org/game/555", second_row)
        self.assertIn("PCGWiki", second_row)

    def test_either_one_alone_still_makes_a_second_row(self):
        self.assertIn("\n", self.build({"ra_id": 555}))
        self.assertIn("\n", self.build({}, pcgw_url="https://pcgamingwiki.com/x"))


class FileListingTests(unittest.TestCase):
    def test_a_short_listing_shows_everything(self):
        name, value = build_file_listing([rom_file("a.bin"), rom_file("b.bin")])

        self.assertIn("Total:", name)
        self.assertNotIn("Showing", name)
        self.assertEqual(["• a.bin (1.00 KB)", "• b.bin (1.00 KB)"], value.splitlines())

    def test_root_files_come_before_subfolders(self):
        name, value = build_file_listing([
            rom_file("zz.bin", category="patch"),
            rom_file("aa.bin"),
        ])

        lines = [line for line in value.splitlines() if line]
        self.assertTrue(lines[0].startswith("• aa.bin"))
        self.assertIn("**Patch**", lines[1])

    def test_dlc_keeps_its_capitals(self):
        """It is an acronym; "Dlc" would be wrong, and is special-cased."""
        _, value = build_file_listing([rom_file("x.bin", category="dlc")])

        self.assertIn("**DLC**", value)

    def test_other_subfolders_are_capitalised(self):
        _, value = build_file_listing([rom_file("x.bin", category="patch")])

        self.assertIn("**Patch**", value)

    def test_a_small_subfolder_is_listed_alphabetically(self):
        _, value = build_file_listing([
            rom_file("c.bin", size=9000),
            rom_file("a.bin", size=1),
            rom_file("b.bin", size=5000),
        ])

        self.assertEqual(
            ["a.bin", "b.bin", "c.bin"],
            [line.split(" ")[1] for line in value.splitlines() if line],
        )

    def test_a_large_subfolder_shows_its_biggest_files_instead(self):
        """Past ten files the budget matters more than completeness, so the
        listing switches to naming the largest rather than the first."""
        files = [rom_file(f"f{i:02d}.bin", size=100 * i) for i in range(15)]

        _, value = build_file_listing(files)

        listed = [line.split(" ")[1] for line in value.splitlines() if line.startswith("•")]
        self.assertEqual(10, len(listed))
        self.assertIn("f14.bin", listed)
        self.assertNotIn("f00.bin", listed)

    def test_the_character_budget_cuts_the_listing_short(self):
        """Two separate limits trim this listing, and only one is the budget.

        A flat list of more than ten files is capped at ten before the budget
        is ever reached, so exercising the budget needs few files with long
        names rather than many with short ones.
        """
        files = [rom_file(f"{'n' * 200}_{i}.bin") for i in range(8)]

        _, value = build_file_listing(files)

        self.assertLessEqual(len(value), MAX_FILE_LISTING_CHARS + len("..."))
        self.assertTrue(value.endswith("..."))

    def test_the_per_subfolder_cap_trims_a_long_flat_list(self):
        files = [rom_file(f"{'n' * 60}_{i}.bin") for i in range(50)]

        name, value = build_file_listing(files)

        self.assertEqual(10, len([line for line in value.splitlines() if line]))
        self.assertLess(len(value), MAX_FILE_LISTING_CHARS)

    def test_a_truncated_listing_says_how_many_it_showed(self):
        files = [rom_file(f"{'n' * 60}_{i}.bin") for i in range(50)]

        name, _ = build_file_listing(files)

        self.assertIn("Showing", name)
        self.assertIn("of 50 files", name)

    def test_the_total_covers_every_file_not_just_the_listed_ones(self):
        files = [rom_file(f"{'n' * 60}_{i}.bin", size=1024 ** 2) for i in range(50)]

        name, _ = build_file_listing(files)

        self.assertIn("50.00 MB", name)

    def test_no_files_still_produces_a_usable_field(self):
        name, value = build_file_listing([])

        self.assertEqual("No files to display", value)
        self.assertIn("Unknown size", name)


class SingleFileTests(unittest.TestCase):
    def test_a_plain_single_file(self):
        name, value = build_single_file_field(
            {"fs_name": "goldeneye.z64", "fs_size_bytes": 8 * 1024 * 1024}
        )

        self.assertEqual("File (8.00 MB)", name)
        self.assertEqual("• goldeneye.z64", value)

    def test_a_single_file_in_a_subfolder_is_labelled(self):
        name, value = build_single_file_field({
            "fs_name": "patch.ips",
            "fs_size_bytes": 1024,
            "files": [{"file_name": "patch.ips", "category": "patch"}],
        })

        self.assertIn("[Patch]", value)
        self.assertIn("• patch.ips", value)

    def test_more_than_one_file_is_not_treated_as_a_subfolder(self):
        _, value = build_single_file_field({
            "fs_name": "game.bin",
            "files": [
                {"file_name": "a", "category": "patch"},
                {"file_name": "b", "category": "patch"},
            ],
        })

        self.assertEqual("• game.bin", value)

    def test_a_missing_name_falls_back(self):
        name, value = build_single_file_field({})

        self.assertEqual("• unknown_file", value)
        self.assertEqual("File (Unknown size)", name)


if __name__ == "__main__":
    unittest.main()
