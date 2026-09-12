"""Title matching for requests.

Two different similarity metrics live here, and the distinction matters. They
were previously two methods on two different classes, both called some variant
of "calculate_similarity", one of them carrying the comment "you can reuse the
one from Request cog" - which was never true, because they do not agree:

    word_overlap_ratio("Final Fantasy VII", "Final Fantasy VIII")  -> 0.50
    edit_distance_ratio("Final Fantasy VII", "Final Fantasy VIII") -> 0.94

Word overlap ignores spelling and asks how many significant words two titles
share, so it is strict about sequels and roman numerals. Edit distance asks how
many characters differ, so it is tolerant of them. Nothing here picks one over
the other; the call sites keep the metric they have always used.
"""

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

# Dropped before comparison: they say nothing about which game a title names.
COMMON_WORDS = frozenset({'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to'})

_SPECIAL_CHARS = r'[^\w\s]'
_TITLE_SEPARATORS = r'[:\-\s]+'


def significant_words(text: str) -> set:
    """Lowercase words of `text` with the common ones removed."""
    return {word for word in text.lower().split() if word not in COMMON_WORDS}


def normalize_title(name: str) -> str:
    """Flatten the punctuation that separates a title from its subtitle.

    "Zelda: Ocarina of Time" and "Zelda - Ocarina of Time" normalize alike.
    """
    return re.sub(_TITLE_SEPARATORS, ' ', (name or '').lower()).strip()


def word_overlap_ratio(str1: str, str2: str) -> float:
    """Jaccard index over significant words: shared / total, 0.0 to 1.0.

    Strict about titles that differ by a word, including sequel numbering.
    """
    if not str1 or not str2:
        return 0.0

    words1 = significant_words(str1)
    words2 = significant_words(str2)

    if not words1 or not words2:
        return 0.0

    union = words1 | words2
    return len(words1 & words2) / len(union) if union else 0.0


def edit_distance_ratio(str1: str, str2: str) -> float:
    """How alike two titles are by character edits, 0.0 to 1.0.

    Common words and punctuation are stripped first, then the Levenshtein
    distance is scaled by the length of the longer string.
    """
    str1_clean = _clean_for_edit_distance(str1)
    str2_clean = _clean_for_edit_distance(str2)

    if not str1_clean or not str2_clean:
        return 0.0

    longer, shorter = (str1_clean, str2_clean)
    if len(str2_clean) > len(str1_clean):
        longer, shorter = str2_clean, str1_clean

    return 1 - (levenshtein_distance(longer, shorter) / len(longer))


def _clean_for_edit_distance(text: str) -> str:
    kept = ' '.join(word for word in text.lower().split() if word not in COMMON_WORDS)
    return re.sub(_SPECIAL_CHARS, '', kept)


def levenshtein_distance(s1: str, s2: str) -> int:
    """Number of single-character edits between two strings."""
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)

    if len(s2) == 0:
        return len(s1)

    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row

    return previous_row[-1]


def filter_out_existing(
    existing_roms: Iterable[Dict],
    igdb_matches: Iterable[Dict],
    threshold: float = 0.85,
) -> List[Dict]:
    """Drop IGDB results the collection already has.

    Both sides are matched on their normalized names, by exact equality or by
    word overlap above `threshold`.
    """
    if not igdb_matches:
        return []

    normalized_existing = [normalize_title(rom.get('name', '')) for rom in existing_roms]

    filtered = []
    for igdb_game in igdb_matches:
        igdb_normalized = normalize_title(igdb_game.get('name', ''))
        already_have = any(
            igdb_normalized == rom_normalized
            or word_overlap_ratio(igdb_normalized, rom_normalized) > threshold
            for rom_normalized in normalized_existing
        )
        if not already_have:
            filtered.append(igdb_game)

    return filtered


@dataclass
class DuplicateMatch:
    """An existing pending request that covers the same game."""

    request_id: int
    requester_id: int
    requester_name: str
    is_own_request: bool


def find_duplicate_request(
    candidates: Iterable[Dict],
    *,
    game_name: str,
    igdb_id: Optional[int],
    user_id: int,
    threshold: float = 0.8,
) -> Optional[DuplicateMatch]:
    """Find the first pending request that already covers this game.

    A matching IGDB id is decisive. Failing that, titles are compared by edit
    distance, which tolerates the punctuation and spelling drift between what
    someone types and what is already on file.

    Only the first match is returned, which is what the original inline loop
    did: it stopped at the first row that matched rather than looking for a
    better one further down.
    """
    for row in candidates:
        by_igdb = bool(igdb_id and row['igdb_id'] and igdb_id == row['igdb_id'])
        if not by_igdb:
            if edit_distance_ratio(game_name.lower(), row['game_name'].lower()) <= threshold:
                continue

        return DuplicateMatch(
            request_id=row['id'],
            requester_id=row['user_id'],
            requester_name=row['username'],
            is_own_request=row['user_id'] == user_id,
        )

    return None
