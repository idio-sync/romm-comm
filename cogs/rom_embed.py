"""Formatting for the ROM search result embed.

These were the pure middle of a 266-line create_rom_embed - the part that
turns a ROM payload into text and never touches the network. They are here so
they can be tested without a Discord view or a RomM server.
"""

import logging
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

ROMM_LOGO = (
    "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main"
    "/.backend/isotipo-small.png"
)

# Discord caps a field value at 1024; the file listing stops well short so the
# "Showing N of M" line always has room.
MAX_FILE_LISTING_CHARS = 800

# Above this many files in one subfolder, the biggest are shown instead of all.
SUBFOLDER_FILE_LIMIT = 10

# Anything larger than this as a Unix timestamp would be past the year 2033,
# so IGDB gave it in milliseconds.
MAX_PLAUSIBLE_TIMESTAMP = 2_000_000_000

SUBFOLDER_ICONS = {
    'hack': '🔧',
    'dlc': '⬇️',
    'manual': '📖',
    'mod': '🎨',
    'patch': '📝',
    'update': '🔄',
    'demo': '🎮',
    'translation': '🌐',
    'prototype': '🔬',
    'cheat': '🃏',
    None: '📄'  # Default for main/root files
}

# Path segments recognised as a subfolder when the backend gives no category.
KNOWN_SUBFOLDERS = [
    'hack', 'dlc', 'manual', 'mod', 'patch',
    'update', 'demo', 'translation', 'prototype', 'cheat',
]

# Subfolder names that should not be title-cased.
SUBFOLDER_ACRONYMS = {'dlc': 'DLC'}


def format_file_size(size_bytes: Union[int, float]) -> str:
    """Format size in bytes to human readable format"""
    if not size_bytes or not isinstance(size_bytes, (int, float)):
        return "Unknown size"

    units = ['B', 'KB', 'MB', 'GB', 'TB']
    size_value = float(size_bytes)
    unit_index = 0
    while size_value >= 1024 and unit_index < len(units) - 1:
        size_value /= 1024
        unit_index += 1
    return f"{size_value:.2f} {units[unit_index]}"


def file_subfolder(file_info: Dict) -> Optional[str]:
    """The subfolder a file sits in, or None when it is at the root.

    The backend's category field wins where it has one; otherwise the path is
    searched for a segment naming a known subfolder.
    """
    if file_info.get('category'):
        return file_info['category'].lower()

    file_path = file_info.get('file_path', '')
    if not file_path:
        return None

    for part in file_path.split('/'):
        if part.lower() in KNOWN_SUBFOLDERS:
            return part.lower()
    return None


def subfolder_icon(subfolder: Optional[str]) -> str:
    """Get icon for subfolder type"""
    return SUBFOLDER_ICONS.get(subfolder, '📁')


def format_release_date(release_date) -> Optional[str]:
    """Render an IGDB release timestamp, in seconds or milliseconds.

    IGDB is inconsistent about the unit, so anything implausibly far in the
    future is treated as milliseconds. Returns None if it cannot be read.
    """
    if not release_date:
        return None
    try:
        if release_date > MAX_PLAUSIBLE_TIMESTAMP:
            release_date = release_date / 1000
        return datetime.fromtimestamp(int(release_date)).strftime('%b %d, %Y')
    except (ValueError, TypeError, OSError, OverflowError) as e:
        logger.error(f"Error formatting date: {e}")
        logger.error(f"Raw release_date value: {release_date}")
        return None


def truncate_field(value: str, limit: int = 1024) -> str:
    """Keep a field value inside Discord's limit."""
    return value if len(value) <= limit else value[:limit - 3] + "..."


def first_two(value, separator: str = ", ") -> str:
    """Render a list field as at most its first two entries."""
    if isinstance(value, list):
        return separator.join(value[:2])
    return str(value)


def build_links_value(
    rom_data: Dict,
    *,
    romm_url: str,
    igdb_url: str,
    pcgw_url: Optional[str],
    emoji: Callable[[str], str],
) -> str:
    """The Links field: RomM and IGDB always, the rest when the ROM has them.

    Two rows. RomM, IGDB and any trailer go on the first; achievements and
    PCGamingWiki on the second, which is omitted when neither applies.
    """
    top_row = [
        f"[**{emoji('romm')} RomM**]({romm_url})",
        f"[**{emoji('igdb')} IGDB**]({igdb_url})",
    ]

    if youtube_video_id := rom_data.get('youtube_video_id'):
        youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
        top_row.append(f"[**{emoji('youtube')} Trailer**]({youtube_url})")

    second_row = []
    if ra_id := rom_data.get('ra_id'):
        ra_url = f"https://retroachievements.org/game/{ra_id}"
        second_row.append(f"[**{emoji('retroachievements')} Achievements**]({ra_url})")

    if pcgw_url:
        second_row.append(f"[**{emoji('pcgw')} PCGWiki**]({pcgw_url})")

    links_value = " ".join(top_row)
    if second_row:
        links_value += "\n" + " ".join(second_row)
    return links_value


def _sorted_subfolder_files(subfolder_files: List[Dict]) -> List[Dict]:
    """Files of one subfolder in display order.

    A small subfolder is listed alphabetically. A large one is trimmed to its
    biggest files instead, on the grounds that the listing has a character
    budget and the big files are the ones worth naming.
    """
    if len(subfolder_files) > SUBFOLDER_FILE_LIMIT:
        return sorted(
            subfolder_files,
            key=lambda x: (x.get('file_size_bytes', 0), x.get('file_name', '').lower()),
            reverse=True,
        )[:SUBFOLDER_FILE_LIMIT]

    return sorted(subfolder_files, key=lambda x: x.get('file_name', '').lower())


def build_file_listing(files: List[Dict]) -> Tuple[str, str]:
    """Render a multi-file ROM's contents as (field name, field value).

    Files are grouped by subfolder, root files first, each group headed by its
    icon. The whole listing is held to a character budget; when it runs out an
    ellipsis is appended and the field name says how many of the total are
    shown.
    """
    total_size = sum(f.get('file_size_bytes', 0) for f in files)

    files_by_subfolder = defaultdict(list)
    for file_info in files:
        files_by_subfolder[file_subfolder(file_info)].append(file_info)

    # Root files (None) first, then subfolders alphabetically.
    sorted_subfolders = sorted(files_by_subfolder.keys(), key=lambda x: (x is not None, x))

    lines: List[str] = []
    total_length = 0
    files_shown = 0

    for subfolder in sorted_subfolders:
        # A blank line between groups, budget permitting.
        if lines and len(sorted_subfolders) > 1 and total_length + 1 < MAX_FILE_LISTING_CHARS:
            lines.append("")
            total_length += 1

        if subfolder:
            display_name = SUBFOLDER_ACRONYMS.get(subfolder, subfolder.capitalize())
            header_line = f"{subfolder_icon(subfolder)} **{display_name}**"
            if total_length + len(header_line) + 1 > MAX_FILE_LISTING_CHARS:
                lines.append("...")
                break
            lines.append(header_line)
            total_length += len(header_line) + 1

        for file_info in _sorted_subfolder_files(files_by_subfolder[subfolder]):
            size_str = format_file_size(file_info.get('file_size_bytes', 0))
            file_line = f"• {file_info['file_name']} ({size_str})"

            if total_length + len(file_line) + 1 > MAX_FILE_LISTING_CHARS:
                lines.append("...")
                break

            lines.append(file_line)
            total_length += len(file_line) + 1
            files_shown += 1

        if total_length >= MAX_FILE_LISTING_CHARS:
            break

    field_name = f"Files (Total: {format_file_size(total_size)}"
    if len(files) > files_shown:
        field_name += f" - Showing {files_shown} of {len(files)} files)"
    else:
        field_name += ")"

    return field_name, "\n".join(lines) if lines else "No files to display"


def build_single_file_field(rom_data: Dict) -> Tuple[str, str]:
    """Render a single-file ROM as (field name, field value)."""
    file_size = format_file_size(rom_data.get('fs_size_bytes', 0))
    file_name = rom_data.get('fs_name', 'unknown_file')

    subfolder = None
    files = rom_data.get('files') or []
    if len(files) == 1:
        subfolder = file_subfolder(files[0])

    value = f"• {file_name}"
    if subfolder:
        value = f"{subfolder_icon(subfolder)} [{subfolder.capitalize()}]\n• {file_name}"

    return f"File ({file_size})", value
