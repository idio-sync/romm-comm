"""Formatting for the "new game available" embed.

The pure middle of RecentRoms.create_single_rom_embed, which is otherwise an
async method that fetches ROM detail from RomM and downloads cover art. These
turn a ROM payload into text and never touch the network, so they can be
tested without a RomM server.

Deliberately not merged with cogs/rom_embed.py: that renders a search result
and formats sizes to two decimal places with an "Unknown size" fallback, where
this one uses one decimal place and a different unit ladder. The two look
similar and are not interchangeable.
"""

import logging
import os
from datetime import UTC, datetime
from typing import Callable, Dict, Optional
from urllib.parse import quote

from .rom_embed import MAX_PLAUSIBLE_TIMESTAMP

logger = logging.getLogger(__name__)

UNKNOWN = "Unknown"

# A zero-width name/value pair. Discord lays inline fields out three to a row,
# so an empty one pads the row and forces the next field onto a new line.
SPACER_FIELD = "\u200b"

# An EN SPACE, not a plain one: it sets the two access links slightly further
# apart than a space would. Indistinguishable when read, so it is named.
LINK_SEPARATOR = "\u2002"

# The embed description, not a field, so this is a blurb rather than the full
# summary. Cut at the last sentence that leaves a readable amount of text.
SUMMARY_LIMIT = 150
SUMMARY_SENTENCE_FLOOR = 100

DEVELOPER_LIMIT = 30
FOOTER_FILENAME_LIMIT = 50
# Beyond this an "extension" is not an extension, so the name is cut blindly.
MAX_MEANINGFUL_EXTENSION = 10


def truncate_summary(summary: str) -> str:
    """Cut a summary to the blurb length, preferring a sentence boundary."""
    if len(summary) <= SUMMARY_LIMIT:
        return summary
    summary = summary[:SUMMARY_LIMIT]
    last_period = summary.rfind('.')
    if last_period > SUMMARY_SENTENCE_FLOOR:
        return summary[:last_period + 1]
    return summary[:SUMMARY_LIMIT - 3] + "..."


def format_release_date(metadatum: Optional[Dict]) -> str:
    """RomM's IGDB release timestamp as a date, or "Unknown".

    Read as UTC: IGDB stores a release date as midnight UTC, so a naive
    fromtimestamp renders the previous day for any host west of UTC.

    Only ValueError and TypeError are caught, which is what the original did:
    a timestamp far enough out of range to raise OverflowError or OSError is
    a broken payload and should not be swallowed.
    """
    if not metadatum:
        return UNKNOWN
    release_date = metadatum.get('first_release_date')
    if not release_date:
        return UNKNOWN
    try:
        if release_date > MAX_PLAUSIBLE_TIMESTAMP:
            release_date = release_date / 1000
        return datetime.fromtimestamp(int(release_date), tz=UTC).strftime("%B %d, %Y")
    except (ValueError, TypeError) as e:
        logger.debug(f"Error formatting release date: {e}")
        return UNKNOWN


def format_developer(metadatum: Optional[Dict]) -> str:
    """The first company RomM lists, truncated, or "Unknown".

    RomM has been seen to send `companies` as either a list or a bare string,
    so both are handled.
    """
    if not metadatum:
        return UNKNOWN
    companies = metadatum.get('companies')
    if not companies:
        return UNKNOWN
    if isinstance(companies, list):
        developer = companies[0]
    elif isinstance(companies, str):
        developer = companies
    else:
        return UNKNOWN
    if len(developer) > DEVELOPER_LIMIT:
        return developer[:DEVELOPER_LIMIT - 3] + "..."
    return developer


def rom_filename(rom: Dict) -> Optional[str]:
    """The ROM's filename under either of the two keys RomM uses."""
    return rom.get('file_name') or rom.get('fs_name')


def build_access_links(
    rom: Dict,
    *,
    domain: str,
    romm_emoji: str,
) -> str:
    """The Access field: the RomM page, plus a direct download when we have a
    filename to build the content URL from."""
    links = [f"[**{romm_emoji} RomM**]({domain}/rom/{rom['id']})"]
    filename = rom_filename(rom)
    if filename:
        download_url = f"{domain}/api/roms/{rom['id']}/content/{quote(filename)}"
        links.append(f"[**\u2b07\ufe0f Download**]({download_url})")
    return LINK_SEPARATOR.join(links)


def truncate_filename(filename: str) -> str:
    """Shorten a filename for the footer, keeping a real extension visible."""
    if len(filename) <= FOOTER_FILENAME_LIMIT:
        return filename
    name, ext = os.path.splitext(filename)
    if len(ext) <= MAX_MEANINGFUL_EXTENSION:
        # The extension length is subtracted from the budget and then added
        # back, so this is always 49 characters whatever the extension is.
        # Kept as it was; changing it changes what the footer looks like.
        return name[:FOOTER_FILENAME_LIMIT - 4 - len(ext)] + "..." + ext
    return filename[:FOOTER_FILENAME_LIMIT - 3] + "..."


def build_footer_text(rom: Dict, *, format_size: Callable[[int], str]) -> str:
    """Filename, size and the standing "Added to collection" note."""
    parts = []
    filename = rom_filename(rom)
    if filename:
        parts.append(truncate_filename(filename))
    if rom.get("fs_size_bytes"):
        parts.append(format_size(rom["fs_size_bytes"]))
    parts.append("Added to collection")
    return " \u2022 ".join(parts)
