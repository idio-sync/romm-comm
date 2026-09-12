"""Tests for the pieces split out of process_request_with_platform.

That method was 336 lines: duplicate detection, subscriber handling, IGDB
metadata formatting, the insert, the ggrequestz mirror and three different
embeds, all inline. None of it could be reached without a Discord interaction.
These cover the parts that are now plain functions.
"""

import sqlite3
import unittest

from cogs.requests.embeds import (
    build_already_requested_embed,
    build_request_submitted_embed,
    build_subscribed_embed,
    format_igdb_details,
    parse_request_details,
)
from cogs.requests.matching import find_duplicate_request

_ROW_CONN = sqlite3.connect(":memory:")
_ROW_CONN.row_factory = sqlite3.Row


def candidate(id, user_id, username, game_name, igdb_id=None):
    """A row shaped like list_duplicate_candidates returns."""
    return _ROW_CONN.execute(
        "SELECT ? AS id, ? AS user_id, ? AS username, ? AS game_name, ? AS igdb_id",
        (id, user_id, username, game_name, igdb_id),
    ).fetchone()


class FindDuplicateRequestTests(unittest.TestCase):
    def test_no_candidates_means_no_duplicate(self):
        self.assertIsNone(
            find_duplicate_request([], game_name="Doom", igdb_id=None, user_id=1)
        )

    def test_a_matching_igdb_id_is_decisive(self):
        """Even when the titles were typed differently."""
        rows = [candidate(7, 2, "someone", "DOOM (1993)", igdb_id=555)]

        match = find_duplicate_request(rows, game_name="Doom", igdb_id=555, user_id=1)

        self.assertIsNotNone(match)
        self.assertEqual(7, match.request_id)
        self.assertEqual("someone", match.requester_name)
        self.assertFalse(match.is_own_request)

    def test_a_near_identical_title_matches_without_an_igdb_id(self):
        rows = [candidate(7, 2, "someone", "Mario Kart 64")]

        match = find_duplicate_request(rows, game_name="Mario Kart 64!", igdb_id=None, user_id=1)

        self.assertIsNotNone(match)
        self.assertEqual(7, match.request_id)

    def test_a_different_game_is_not_a_duplicate(self):
        rows = [candidate(7, 2, "someone", "Sonic the Hedgehog")]

        self.assertIsNone(
            find_duplicate_request(rows, game_name="Doom", igdb_id=None, user_id=1)
        )

    def test_the_requester_recognises_their_own_request(self):
        rows = [candidate(7, 1, "me", "Doom")]

        match = find_duplicate_request(rows, game_name="Doom", igdb_id=None, user_id=1)

        self.assertTrue(match.is_own_request)

    def test_a_different_igdb_id_falls_through_to_title_comparison(self):
        """Two IGDB ids that disagree do not rule a title match out.

        The original loop did the same: the id check is an `if`, and a failed
        one falls through to the `elif` on similarity.
        """
        rows = [candidate(7, 2, "someone", "Doom", igdb_id=999)]

        match = find_duplicate_request(rows, game_name="Doom", igdb_id=555, user_id=1)

        self.assertIsNotNone(match)

    def test_the_first_match_wins(self):
        """Matching the original behaviour: it stopped at the first hit."""
        rows = [
            candidate(7, 2, "first", "Doom"),
            candidate(8, 3, "second", "Doom"),
        ]

        match = find_duplicate_request(rows, game_name="Doom", igdb_id=None, user_id=1)

        self.assertEqual(7, match.request_id)

    def test_non_matching_rows_are_skipped_to_reach_a_later_match(self):
        rows = [
            candidate(7, 2, "first", "Sonic the Hedgehog"),
            candidate(8, 3, "second", "Doom"),
        ]

        match = find_duplicate_request(rows, game_name="Doom", igdb_id=None, user_id=1)

        self.assertEqual(8, match.request_id)


class FormatIgdbDetailsTests(unittest.TestCase):
    GAME = {
        "id": 555,
        "name": "GoldenEye 007",
        "release_date": "1997-08-25",
        "platforms": ["Nintendo 64"],
        "developers": ["Rare"],
        "publishers": ["Nintendo"],
        "genres": ["Shooter", "Adventure"],
        "game_modes": ["Single player"],
        "summary": "A spy shooter.",
        "cover_url": "https://images.igdb.com/cover.jpg",
    }

    def test_what_it_writes_is_what_the_parser_reads(self):
        """The two halves of the details format must agree.

        format_igdb_details writes the block, parse_request_details reads it.
        A change to either that is not mirrored in the other breaks every
        request embed, silently.
        """
        details = format_igdb_details(self.GAME)

        parsed = parse_request_details(details, fallback_name="ignored")

        self.assertEqual("GoldenEye 007", parsed.igdb_name)
        self.assertEqual("Shooter, Adventure", parsed.game_data["Genres"])
        self.assertEqual("Rare", parsed.game_data["Developers"])
        self.assertEqual("https://images.igdb.com/cover.jpg", parsed.cover_url)

    def test_missing_fields_render_as_unknown(self):
        details = format_igdb_details({"name": "Mystery Game"})

        self.assertIn("Developers: Unknown", details)
        self.assertIn("Publishers: Unknown", details)
        self.assertIn("Genres: Unknown", details)
        self.assertIn("Release Date: Unknown", details)
        self.assertIn("Cover URL: None", details)

    def test_empty_lists_also_render_as_unknown(self):
        """A present-but-empty list is no more informative than a missing one."""
        details = format_igdb_details({"name": "Mystery Game", "developers": []})

        self.assertIn("Developers: Unknown", details)

    def test_alternative_names_are_appended_to_the_game_line(self):
        details = format_igdb_details({
            "name": "Contra",
            "alternative_names": [
                {"name": "Probotector", "comment": "European title"},
                {"name": "Gryzor"},
            ],
        })

        self.assertIn("Alternative Names: Probotector (European title), Gryzor", details)


class RequestEmbedTests(unittest.TestCase):
    def field(self, embed, name):
        for candidate_field in embed.fields:
            if candidate_field.name == name:
                return candidate_field.value
        return None

    def test_already_requested_embed_names_the_open_request(self):
        embed = build_already_requested_embed(
            game="Doom", platform_display="PC", request_id=7, selected_game=None
        )

        self.assertEqual("#7", self.field(embed, "Request ID"))
        self.assertIn("Pending", self.field(embed, "Status"))

    def test_subscribed_embed_credits_the_original_requester(self):
        embed = build_subscribed_embed(
            game="Doom",
            platform_display="PC",
            request_id=7,
            requester_name="someone",
            subscriber_count=3,
            selected_game=None,
        )

        self.assertIn("someone", embed.description)
        self.assertIn("3 other user(s)", self.field(
            embed, "✅ You've been added to the notification list"
        ))

    def test_a_cover_url_becomes_the_thumbnail(self):
        embed = build_already_requested_embed(
            game="Doom",
            platform_display="PC",
            request_id=7,
            selected_game={"cover_url": "https://images.igdb.com/doom.jpg"},
        )

        self.assertEqual("https://images.igdb.com/doom.jpg", embed.thumbnail.url)

    def test_a_missing_platform_is_called_out_on_the_confirmation(self):
        embed = build_request_submitted_embed(
            game="Doom",
            platform_display="3DO",
            platform_exists=False,
            request_id=7,
            author_name="me",
            user_details=None,
        )

        self.assertIn("Not Yet Added", self.field(embed, "Platform"))
        self.assertIsNotNone(self.field(embed, "📝 Note"))

    def test_an_available_platform_gets_no_note(self):
        embed = build_request_submitted_embed(
            game="Doom",
            platform_display="PC",
            platform_exists=True,
            request_id=7,
            author_name="me",
            user_details=None,
        )

        self.assertIn("Available", self.field(embed, "Platform"))
        self.assertIsNone(self.field(embed, "📝 Note"))

    def test_the_igdb_block_is_not_echoed_back_as_user_details(self):
        """Only what the user actually typed belongs in the Details field."""
        embed = build_request_submitted_embed(
            game="Doom",
            platform_display="PC",
            platform_exists=True,
            request_id=7,
            author_name="me",
            user_details="IGDB Metadata:\nGame: Doom",
        )

        self.assertIsNone(self.field(embed, "Details"))

    def test_what_the_user_typed_is_shown(self):
        embed = build_request_submitted_embed(
            game="Doom",
            platform_display="PC",
            platform_exists=True,
            request_id=7,
            author_name="me",
            user_details="PAL copy please",
        )

        self.assertEqual("PAL copy please", self.field(embed, "Details"))


if __name__ == "__main__":
    unittest.main()
