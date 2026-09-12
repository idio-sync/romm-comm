"""Tests for the IGDB browse-flow embeds.

These came out of a 368-line request_callback, where the ~120 lines that built
the confirmation embed sat inside a Discord interaction handler and could not
be reached any other way.
"""

import unittest

from cogs.igdb_embeds import (
    ROMM_LOGO,
    build_existing_games_embed,
    build_igdb_request_submitted_embed,
    build_igdb_subscribed_embed,
    format_genres,
    format_rating,
    format_release_date,
    top_companies,
)

FULL_GAME = {
    "id": 555,
    "name": "GoldenEye 007",
    "release_date": "1997-08-25",
    "genres": ["Shooter", "Adventure"],
    "developers": ["Rare"],
    "publishers": ["Nintendo"],
    "rating": 88.4,
    "rating_count": 1234,
    "summary": "A spy shooter.",
    "cover_url": "https://images.igdb.com/cover.jpg",
}


class FormatHelperTests(unittest.TestCase):
    def test_a_real_date_is_spelled_out(self):
        self.assertEqual("August 25, 1997", format_release_date("1997-08-25"))

    def test_placeholder_dates_render_as_nothing(self):
        for value in ("Unknown", "TBA", "", None):
            self.assertIsNone(format_release_date(value))

    def test_an_unparseable_date_is_passed_through(self):
        """IGDB sometimes gives a year or a quarter rather than a date."""
        self.assertEqual("1997", format_release_date("1997"))

    def test_a_rating_becomes_stars_plus_the_score(self):
        self.assertEqual("⭐⭐⭐⭐☆ 88.4/100 (1,234 ratings)", format_rating(88.4, 1234))

    def test_a_rating_without_a_count_omits_the_count(self):
        self.assertEqual("⭐⭐⭐⭐☆ 88.4/100", format_rating(88.4, None))

    def test_no_rating_renders_as_nothing(self):
        self.assertIsNone(format_rating(None, 10))

    def test_five_stars_at_the_top_of_the_scale(self):
        self.assertTrue(format_rating(100.0, None).startswith("⭐⭐⭐⭐⭐"))

    def test_genres_are_capped_at_two_with_a_remainder(self):
        self.assertEqual("A, B (+2 more)", format_genres(["A", "B", "C", "D"]))

    def test_two_or_fewer_genres_have_no_remainder(self):
        self.assertEqual("A, B", format_genres(["A", "B"]))

    def test_no_genres_render_as_nothing(self):
        self.assertIsNone(format_genres([]))


class TopCompaniesTests(unittest.TestCase):
    def test_developers_come_first(self):
        self.assertEqual(
            ["Rare", "Nintendo"],
            top_companies({"developers": ["Rare"], "publishers": ["Nintendo"]}),
        )

    def test_at_most_two_are_returned(self):
        self.assertEqual(
            ["A", "B"],
            top_companies({"developers": ["A", "B", "C"], "publishers": ["D"]}),
        )

    def test_publishers_identical_to_developers_are_not_repeated(self):
        """IGDB reports self-published games this way often enough to matter."""
        self.assertEqual(["Rare"], top_companies({"developers": ["Rare"], "publishers": ["Rare"]}))

    def test_publishers_alone_are_used_when_there_is_no_developer(self):
        self.assertEqual(["Nintendo"], top_companies({"publishers": ["Nintendo"]}))

    def test_a_game_with_neither_yields_nothing(self):
        self.assertEqual([], top_companies({}))


class SubmittedEmbedTests(unittest.TestCase):
    def field(self, embed, name):
        for candidate in embed.fields:
            if candidate.name == name:
                return candidate.value
        return None

    def build(self, game=None, platform_in_romm=True):
        return build_igdb_request_submitted_embed(
            selected_game=game if game is not None else FULL_GAME,
            game_name="GoldenEye 007",
            platform_display="Nintendo 64",
            platform_in_romm=platform_in_romm,
            request_id=7,
            author_name="me",
        )

    def test_a_complete_game_fills_every_field(self):
        embed = self.build()

        self.assertIn("GoldenEye 007", embed.description)
        self.assertIn("Available", self.field(embed, "Platform"))
        self.assertEqual("Shooter, Adventure", self.field(embed, "Genre"))
        self.assertEqual("August 25, 1997", self.field(embed, "Release Date"))
        self.assertIn("88.4/100", self.field(embed, "IGDB Rating"))
        self.assertEqual("#7", self.field(embed, "Request ID"))
        self.assertEqual("Rare, Nintendo", self.field(embed, "Companies"))
        self.assertEqual("A spy shooter.", self.field(embed, "Summary"))

    def test_the_cover_is_the_image_and_the_logo_is_the_thumbnail(self):
        embed = self.build()

        self.assertEqual("https://images.igdb.com/cover.jpg", embed.image.url)
        self.assertEqual(ROMM_LOGO, embed.thumbnail.url)

    def test_a_sparse_game_omits_the_optional_fields(self):
        embed = self.build(game={"name": "Mystery Game"})

        for optional in ("Genre", "Release Date", "IGDB Rating", "Companies", "Summary"):
            self.assertIsNone(self.field(embed, optional), f"{optional} should be absent")
        self.assertEqual("#7", self.field(embed, "Request ID"))

    def test_a_missing_platform_gets_a_note(self):
        embed = self.build(platform_in_romm=False)

        self.assertIn("Not Yet Added", self.field(embed, "Platform"))
        self.assertIsNotNone(self.field(embed, "📝 Note"))

    def test_a_long_summary_is_truncated(self):
        embed = self.build(game={**FULL_GAME, "summary": "x" * 500})

        summary = self.field(embed, "Summary")
        self.assertEqual(300, len(summary))
        self.assertTrue(summary.endswith("..."))

    def test_the_placeholder_summary_is_not_shown(self):
        embed = self.build(game={**FULL_GAME, "summary": "No summary available"})

        self.assertIsNone(self.field(embed, "Summary"))


class ExistingGamesEmbedTests(unittest.TestCase):
    def field(self, embed, name):
        for candidate in embed.fields:
            if candidate.name == name:
                return candidate.value
        return None

    def rom(self, name):
        return {"name": name, "fs_name": f"{name}.z64"}

    def test_matches_are_listed(self):
        embed = build_existing_games_embed(
            [self.rom("GoldenEye 007")], "GoldenEye", other_igdb_count=0
        )

        self.assertIn("Found 1 game(s)", embed.description)
        self.assertIn("GoldenEye 007.z64", self.field(embed, "✅ GoldenEye 007"))

    def test_only_three_are_shown_and_the_rest_are_counted(self):
        embed = build_existing_games_embed(
            [self.rom(f"Game {i}") for i in range(5)], "Game", other_igdb_count=0
        )

        self.assertEqual("And 2 more available", self.field(embed, "..."))

    def test_the_request_a_different_game_option_appears_only_when_there_is_one(self):
        with_others = build_existing_games_embed([self.rom("A")], "A", other_igdb_count=4)
        without = build_existing_games_embed([self.rom("A")], "A", other_igdb_count=0)

        self.assertIn(
            "Found 4 other game(s)", self.field(with_others, "What would you like to do?")
        )
        self.assertNotIn(
            "other game(s)", self.field(without, "What would you like to do?")
        )


class SubscribedEmbedTests(unittest.TestCase):
    def test_it_credits_the_original_requester_and_counts_the_others(self):
        embed = build_igdb_subscribed_embed(
            game_name="Doom",
            platform_display="PC",
            request_id=7,
            requester_name="someone",
            subscriber_count=3,
            selected_game={},
        )

        self.assertIn("someone", embed.description)
        values = [field.value for field in embed.fields]
        self.assertTrue(any("3 other user(s)" in value for value in values))


if __name__ == "__main__":
    unittest.main()
