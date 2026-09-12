"""Tests for request title matching.

These functions decide whether a requested game already exists, and whether a
newly scanned ROM fulfils a pending request - so a wrong answer either hides a
game someone asked for or closes a request that was never filled. They were
previously methods on two view classes, reachable only by constructing a
discord.ui.View, and had no tests at all.
"""

import unittest

from cogs.requests.matching import (
    edit_distance_ratio,
    filter_out_existing,
    levenshtein_distance,
    normalize_title,
    significant_words,
    word_overlap_ratio,
)


class LevenshteinTests(unittest.TestCase):
    def test_known_distances(self):
        self.assertEqual(0, levenshtein_distance("mario", "mario"))
        self.assertEqual(1, levenshtein_distance("mario", "maria"))
        self.assertEqual(3, levenshtein_distance("kitten", "sitting"))

    def test_empty_strings(self):
        self.assertEqual(0, levenshtein_distance("", ""))
        self.assertEqual(5, levenshtein_distance("mario", ""))

    def test_argument_order_does_not_matter(self):
        self.assertEqual(
            levenshtein_distance("goldeneye", "golden eye"),
            levenshtein_distance("golden eye", "goldeneye"),
        )


class NormalizationTests(unittest.TestCase):
    def test_subtitle_separators_collapse(self):
        self.assertEqual(
            normalize_title("Zelda: Ocarina of Time"),
            normalize_title("Zelda - Ocarina of Time"),
        )

    def test_none_and_empty_are_safe(self):
        self.assertEqual("", normalize_title(None))
        self.assertEqual("", normalize_title(""))

    def test_common_words_are_dropped(self):
        # "of" is deliberately not asserted away: the stopword list does not
        # contain it, which is worth knowing given how many titles use it.
        # Adding it would change what matches, so it is a product call.
        self.assertEqual({"legend", "of", "zelda"}, significant_words("The Legend of Zelda"))
        self.assertNotIn("the", significant_words("The Legend of Zelda"))


class WordOverlapTests(unittest.TestCase):
    def test_identical_titles_score_one(self):
        self.assertEqual(1.0, word_overlap_ratio("Mario Kart", "Mario Kart"))

    def test_unrelated_titles_score_zero(self):
        self.assertEqual(0.0, word_overlap_ratio("Mario Kart", "Doom"))

    def test_empty_input_scores_zero(self):
        self.assertEqual(0.0, word_overlap_ratio("", "Mario"))
        self.assertEqual(0.0, word_overlap_ratio("Mario", ""))

    def test_title_made_only_of_common_words_scores_zero(self):
        self.assertEqual(0.0, word_overlap_ratio("the a an", "Mario"))

    def test_sequel_numbering_lowers_the_score(self):
        """The reason this metric is used for de-duplicating IGDB results.

        A sequel must not be mistaken for a game already in the collection.
        """
        self.assertLess(word_overlap_ratio("Final Fantasy VII", "Final Fantasy VIII"), 0.85)


class EditDistanceRatioTests(unittest.TestCase):
    def test_identical_titles_score_one(self):
        self.assertEqual(1.0, edit_distance_ratio("Mario Kart", "Mario Kart"))

    def test_punctuation_is_ignored(self):
        self.assertEqual(1.0, edit_distance_ratio("Mario Kart!", "Mario Kart"))

    def test_empty_input_scores_zero(self):
        self.assertEqual(0.0, edit_distance_ratio("", "Mario"))

    def test_title_made_only_of_common_words_scores_zero(self):
        self.assertEqual(0.0, edit_distance_ratio("the a an", "Mario"))

    def test_argument_order_does_not_matter(self):
        self.assertEqual(
            edit_distance_ratio("Golden Eye", "GoldenEye 007"),
            edit_distance_ratio("GoldenEye 007", "Golden Eye"),
        )

    def test_it_tolerates_sequel_numbering_where_word_overlap_does_not(self):
        """The two metrics genuinely disagree; this pins the difference down.

        Both were once named 'calculate_similarity', which invited treating
        them as interchangeable. They are not.
        """
        pair = ("Final Fantasy VII", "Final Fantasy VIII")
        self.assertGreater(edit_distance_ratio(*pair), 0.85)
        self.assertLess(word_overlap_ratio(*pair), 0.85)


class FilterOutExistingTests(unittest.TestCase):
    def test_no_igdb_matches_returns_empty(self):
        self.assertEqual([], filter_out_existing([{"name": "Mario Kart"}], []))

    def test_empty_collection_keeps_everything(self):
        matches = [{"name": "Mario Kart"}, {"name": "Doom"}]
        self.assertEqual(matches, filter_out_existing([], matches))

    def test_exact_match_is_dropped(self):
        result = filter_out_existing(
            [{"name": "Mario Kart 64"}],
            [{"name": "Mario Kart 64"}, {"name": "Doom"}],
        )
        self.assertEqual([{"name": "Doom"}], result)

    def test_match_survives_differing_subtitle_punctuation(self):
        """Normalization happens before comparison, not after."""
        result = filter_out_existing(
            [{"name": "Zelda: Ocarina of Time"}],
            [{"name": "Zelda - Ocarina of Time"}],
        )
        self.assertEqual([], result)

    def test_a_sequel_is_not_treated_as_already_owned(self):
        result = filter_out_existing(
            [{"name": "Final Fantasy VII"}],
            [{"name": "Final Fantasy VIII"}],
        )
        self.assertEqual([{"name": "Final Fantasy VIII"}], result)

    def test_entries_without_a_name_do_not_raise(self):
        result = filter_out_existing([{}], [{"name": "Doom"}, {}])
        self.assertEqual([{"name": "Doom"}], result)


if __name__ == "__main__":
    unittest.main()
