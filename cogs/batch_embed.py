"""Embeds announcing a batch of newly added ROMs.

A scan that adds two games and one that adds two hundred want different
messages: the first names them, the second summarises by platform. Both are
here, apart from the cover downloading they used to be interleaved with, so
that what the announcement says can be tested without a RomM server.
"""

import logging
from collections import defaultdict
from typing import Callable, Dict, List

import discord

from .rom_embed import ROMM_LOGO

logger = logging.getLogger(__name__)

# Most platforms named in a bulk summary before it says "and N more".
MAX_PLATFORMS_LISTED = 10

# Most games named per platform in a regular batch.
MAX_GAMES_PER_PLATFORM = 10


def group_names_by_platform(roms: List[Dict]) -> Dict[str, List[str]]:
    """Game names grouped under the platform they were added to."""
    grouped = defaultdict(list)
    for rom in roms:
        grouped[rom.get('platform_name', 'Unknown')].append(rom['name'])
    return grouped


def count_by_platform(roms: List[Dict]) -> Dict[str, int]:
    """How many games each platform gained."""
    counts = defaultdict(int)
    for rom in roms:
        counts[rom.get('platform_name', 'Unknown')] += 1
    return counts


def build_bulk_embed(roms: List[Dict], *, platform_display: Callable[[str], str]) -> discord.Embed:
    """Summary for a batch too large to name every game in.

    Platforms are listed busiest first, because with a hundred additions the
    useful question is which parts of the collection grew.
    """
    embed = discord.Embed(
        title="📦 Bulk Collection Update",
        description=f"{len(roms)} games have been added to the collection",
        color=discord.Color.orange()
    )
    embed.set_thumbnail(url=ROMM_LOGO)

    by_platform = count_by_platform(roms)
    busiest = sorted(by_platform.items(), key=lambda x: x[1], reverse=True)

    platform_summary = [
        f"• {platform_display(platform)}: {count} ROMs"
        for platform, count in busiest[:MAX_PLATFORMS_LISTED]
    ]
    if len(by_platform) > MAX_PLATFORMS_LISTED:
        platform_summary.append(
            f"• ...and {len(by_platform) - MAX_PLATFORMS_LISTED} more platforms"
        )

    embed.add_field(name="Platforms Updated", value="\n".join(platform_summary), inline=False)
    embed.add_field(
        name="📋 Note",
        value=(
            "Showing summary view due to large number of additions. "
            "Use `/search` to find specific games."
        ),
        inline=False
    )
    return embed


def build_batch_embed(
    roms: List[Dict],
    *,
    platform_display: Callable[[str], str],
    romm_url: str,
    romm_emoji: str,
    has_composite: bool,
) -> discord.Embed:
    """Announcement naming each new game, grouped by platform."""
    embed = discord.Embed(
        title=f"🆕 {len(roms)} New Games Added",
        description="Multiple games have been added to the collection:",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=ROMM_LOGO)

    if has_composite:
        embed.set_image(url="attachment://composite_cover.png")

    by_platform = group_names_by_platform(roms)
    for platform in sorted(by_platform.keys()):
        games = by_platform[platform]
        games_text = "\n".join(f"• {game}" for game in games[:MAX_GAMES_PER_PLATFORM])
        if len(games) > MAX_GAMES_PER_PLATFORM:
            games_text += f"\n• ...and {len(games) - MAX_GAMES_PER_PLATFORM} more"

        embed.add_field(name=platform_display(platform), value=games_text, inline=False)

    embed.add_field(
        name="View Collection",
        value=f"[{romm_emoji} Browse all games]({romm_url})",
        inline=False
    )
    return embed


def add_batch_footer(embed: discord.Embed, rom_count: int) -> discord.Embed:
    """The footer both batch embeds share."""
    embed.set_footer(text=f"Batch update • {rom_count} new games • Use /search to download")
    return embed
