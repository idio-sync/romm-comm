"""Tests for the "new game available" embed formatters.

These came out of a 141-line create_single_rom_embed, whose pure middle could
not be reached without a RomM server and a Discord channel. The split itself
was checked by running the pre-split implementation against the new one over
61,152 input combinations with no divergence; these pin the pieces.
"""

import unittest

from cogs.recent_rom_embed import (
    LINK_SEPARATOR,
    SPACER_FIELD,
    build_access_links,
    build_footer_text,
    format_developer,
    format_release_date,
    rom_filename,
    truncate_filename,
    truncate_summary,
)

# 1995-03-10T00:00:00Z. IGDB stores a release date as UTC midnight, so the
# rendered date must be the UTC one on every host, not the local reading.
RELEASE_TS = 794793600


def _size(n):
    return f"{n} bytes"


class SummaryTests(unittest.TestCase):
    def test_a_short_summary_is_untouched(self):
        self.assertEqual(truncate_summary("Short."), "Short.")

    def test_a_long_summary_is_cut_at_the_last_sentence(self):
        summary = "a" * 105 + ". " + "b" * 100
        self.assertEqual(truncate_summary(summary), "a" * 105 + ".")

    def test_a_long_summary_with_no_late_sentence_break_gets_an_ellipsis(self):
        result = truncate_summary("z" * 200)
        self.assertEqual(len(result), 150)
        self.assertTrue(result.endswith("..."))

    def test_an_early_period_does_not_count_as_a_sentence_break(self):
        # A period before char 100 would leave too little text to be worth it,
        # so the ellipsis cut wins over the much shorter sentence cut.
        result = truncate_summary("a" * 50 + "." + "z" * 200)
        self.assertEqual(len(result), 150)
        self.assertTrue(result.endswith("..."))

    def test_a_period_just_past_the_floor_is_a_sentence_break(self):
        result = truncate_summary("a" * 101 + "." + "z" * 200)
        self.assertEqual(result, "a" * 101 + ".")


class ReleaseDateTests(unittest.TestCase):
    def test_a_second_timestamp_becomes_a_date(self):
        self.assertEqual(format_release_date({'first_release_date': RELEASE_TS}), "March 10, 1995")

    def test_a_millisecond_timestamp_is_scaled_down(self):
        # RomM passes IGDB's value through, which is sometimes in milliseconds.
        self.assertEqual(
            format_release_date({'first_release_date': RELEASE_TS * 1000}), "March 10, 1995"
        )

    def test_missing_metadata_is_unknown(self):
        self.assertEqual(format_release_date(None), "Unknown")
        self.assertEqual(format_release_date({}), "Unknown")
        self.assertEqual(format_release_date({'first_release_date': 0}), "Unknown")

    def test_an_unusable_timestamp_is_unknown_rather_than_an_error(self):
        self.assertEqual(format_release_date({'first_release_date': "nope"}), "Unknown")

    def test_a_timestamp_out_of_range_is_not_swallowed(self):
        # Only ValueError and TypeError are caught, which is what the pre-split
        # code did. A value this broken is a bad payload, not a missing date,
        # and it propagates rather than being reported as "Unknown".
        with self.assertRaises(OverflowError):
            format_release_date({'first_release_date': float("inf")})


class DeveloperTests(unittest.TestCase):
    def test_the_first_company_of_a_list_is_used(self):
        self.assertEqual(format_developer({'companies': ['Squaresoft', 'Nintendo']}), "Squaresoft")

    def test_a_bare_string_is_accepted(self):
        self.assertEqual(format_developer({'companies': 'Nintendo'}), "Nintendo")

    def test_a_long_name_is_truncated_to_thirty(self):
        result = format_developer({'companies': ['A very long company name indeed, truly']})
        self.assertEqual(len(result), 30)
        self.assertTrue(result.endswith("..."))

    def test_anything_else_is_unknown(self):
        self.assertEqual(format_developer(None), "Unknown")
        self.assertEqual(format_developer({'companies': []}), "Unknown")
        self.assertEqual(format_developer({'companies': {'x': 1}}), "Unknown")


class FilenameTests(unittest.TestCase):
    def test_either_romm_key_supplies_the_filename(self):
        self.assertEqual(rom_filename({'file_name': 'a.zip'}), 'a.zip')
        self.assertEqual(rom_filename({'fs_name': 'b.n64'}), 'b.n64')
        self.assertIsNone(rom_filename({}))

    def test_file_name_wins_over_fs_name(self):
        self.assertEqual(rom_filename({'file_name': 'a.zip', 'fs_name': 'b.n64'}), 'a.zip')

    def test_a_short_filename_is_untouched(self):
        self.assertEqual(truncate_filename("game.zip"), "game.zip")

    def test_a_long_filename_keeps_its_extension(self):
        result = truncate_filename("a" * 60 + ".zip")
        self.assertTrue(result.endswith("...zip"))
        # 49, not the 50 limit: the budget subtracts the extension length and
        # then adds the extension back, so the two cancel. Preserved as it was.
        self.assertEqual(len(result), 49)

    def test_the_kept_extension_length_does_not_change_the_result_length(self):
        # Consequence of that cancellation, pinned so it is not mistaken for
        # a bug and "fixed" without deciding to change the output.
        self.assertEqual(len(truncate_filename("a" * 60 + ".z")), 49)
        self.assertEqual(len(truncate_filename("a" * 60 + ".nes")), 49)

    def test_an_implausible_extension_is_not_preserved(self):
        result = truncate_filename("b" * 60 + ".verylongextension")
        self.assertEqual(result, "b" * 47 + "...")


class AccessLinkTests(unittest.TestCase):
    def test_romm_link_alone_without_a_filename(self):
        value = build_access_links({'id': 7}, domain="https://r", romm_emoji="<e>")
        self.assertEqual(value, "[**<e> RomM**](https://r/rom/7)")

    def test_a_download_link_is_added_and_the_filename_is_url_quoted(self):
        value = build_access_links(
            {'id': 7, 'file_name': 'spaces and #hash.zip'}, domain="https://r", romm_emoji="<e>"
        )
        self.assertIn("https://r/api/roms/7/content/spaces%20and%20%23hash.zip", value)

    def test_the_links_are_separated_by_an_en_space(self):
        # A plain space is narrower and reads differently in the embed.
        value = build_access_links(
            {'id': 7, 'file_name': 'a.zip'}, domain="https://r", romm_emoji="<e>"
        )
        self.assertIn(LINK_SEPARATOR, value)
        self.assertEqual(LINK_SEPARATOR, "\u2002")


class FooterTests(unittest.TestCase):
    def test_filename_size_and_the_standing_note(self):
        text = build_footer_text({'file_name': 'g.zip', 'fs_size_bytes': 10}, format_size=_size)
        self.assertEqual(text, "g.zip \u2022 10 bytes \u2022 Added to collection")

    def test_the_note_stands_alone_when_there_is_nothing_else(self):
        self.assertEqual(build_footer_text({}, format_size=_size), "Added to collection")

    def test_a_zero_size_is_omitted_rather_than_shown_as_zero(self):
        text = build_footer_text({'file_name': 'g.zip', 'fs_size_bytes': 0}, format_size=_size)
        self.assertEqual(text, "g.zip \u2022 Added to collection")


class SpacerTests(unittest.TestCase):
    def test_the_spacer_is_a_zero_width_space(self):
        self.assertEqual(SPACER_FIELD, "\u200b")
