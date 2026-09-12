"""Embeds for the IGDB browse flow.

Kept apart from cogs/requests/embeds.py on purpose. These render an IGDB
search result, which carries ratings, summaries and cover art the requests
tables never store, and their wording differs from the request-flow embeds in
ways users would notice. The two look similar; they are not interchangeable.
"""

import logging
import re
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

import discord

logger = logging.getLogger(__name__)

ROMM_LOGO = (
    "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main"
    "/.backend/isotipo-small.png"
)

PLATFORM_MISSING_NOTE = (
    "This platform needs to be added to the collection before this request can be fulfilled."
)


def format_release_date(release_date: Optional[str]) -> Optional[str]:
    """Render an IGDB release date, leaving anything unparseable as it came."""
    if not release_date or release_date in ('Unknown', 'TBA'):
        return None
    try:
        return datetime.strptime(release_date, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return release_date


def format_rating(rating: Optional[float], rating_count: Optional[int]) -> Optional[str]:
    """IGDB's 0-100 rating as five stars plus the raw score."""
    if not rating:
        return None
    stars = round(rating / 20)
    text = "⭐" * stars + "☆" * (5 - stars) + f" {rating:.1f}/100"
    if rating_count:
        text += f" ({rating_count:,} ratings)"
    return text


def format_capped_list(values, cap: int) -> Optional[str]:
    """First `cap` values, comma-joined, with a count of whatever is left over."""
    if not values:
        return None
    text = ', '.join(values[:cap])
    if len(values) > cap:
        text += f' (+{len(values) - cap} more)'
    return text


def format_genres(genres) -> Optional[str]:
    """At most two genres, with a count of whatever is left over."""
    return format_capped_list(genres, 2)


def top_companies(selected_game: Dict) -> list:
    """Up to two companies, developers first.

    Publishers are skipped when they are the same list as the developers,
    which IGDB reports often enough to be worth special-casing.
    """
    companies = []
    developers = selected_game.get('developers')
    if developers:
        companies.extend(developers[:2])

    publishers = selected_game.get('publishers')
    if publishers and publishers != developers:
        remaining_slots = 2 - len(companies)
        if remaining_slots > 0:
            companies.extend(publishers[:remaining_slots])

    return companies


def build_igdb_request_submitted_embed(
    *,
    selected_game: Dict,
    game_name: str,
    platform_display: str,
    platform_in_romm: bool,
    request_id: int,
    author_name: str,
) -> discord.Embed:
    """Confirmation for a request made from an IGDB search result."""
    embed = discord.Embed(
        title="✅ Request Submitted",
        description=f"Your request for **{game_name}** has been submitted!",
        color=discord.Color.green()
    )

    if selected_game.get('cover_url'):
        embed.set_image(url=selected_game['cover_url'])
    embed.set_thumbnail(url=ROMM_LOGO)

    platform_status = "✅ Available" if platform_in_romm else "🆕 Not Yet Added"
    embed.add_field(
        name="Platform",
        value=f"{platform_display}\n{platform_status}",
        inline=True
    )

    genre_str = format_genres(selected_game.get('genres', []))
    if genre_str:
        embed.add_field(name="Genre", value=genre_str, inline=True)

    embed.add_field(name="Status", value="⏳ Pending", inline=True)

    formatted_date = format_release_date(selected_game.get('release_date', 'Unknown'))
    if formatted_date:
        embed.add_field(name="Release Date", value=formatted_date, inline=True)

    rating_text = format_rating(selected_game.get('rating'), selected_game.get('rating_count'))
    if rating_text:
        embed.add_field(name="IGDB Rating", value=rating_text, inline=True)

    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)

    companies = top_companies(selected_game)
    if companies:
        embed.add_field(name="Companies", value=", ".join(companies), inline=True)

    if not platform_in_romm:
        embed.add_field(name="📝 Note", value=PLATFORM_MISSING_NOTE, inline=False)

    summary = selected_game.get('summary', 'No summary available')
    if summary and summary != 'No summary available':
        if len(summary) > 300:
            summary = summary[:297] + "..."
        embed.add_field(name="Summary", value=summary, inline=False)

    embed.set_footer(text=f"Request submitted by {author_name}")
    return embed


def build_igdb_already_requested_embed(
    *, game_name: str, platform_display: str, request_id: int, selected_game: Dict
) -> discord.Embed:
    """The user already has this request open, or is already subscribed to one."""
    embed = discord.Embed(
        title="📋 Already Requested",
        description="You have already requested this game or are subscribed to an existing request.",
        color=discord.Color.orange()
    )
    embed.add_field(name="Game", value=game_name, inline=True)
    embed.add_field(name="Platform", value=platform_display, inline=True)
    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)

    if selected_game.get('cover_url'):
        embed.set_thumbnail(url=selected_game['cover_url'])
    return embed


def build_igdb_subscribed_embed(
    *,
    game_name: str,
    platform_display: str,
    request_id: int,
    requester_name: str,
    subscriber_count: int,
    selected_game: Dict,
) -> discord.Embed:
    """Someone else asked first; this user joins the notification list."""
    embed = discord.Embed(
        title="📋 Request Already Exists",
        description=f"This game has already been requested by **{requester_name}**",
        color=discord.Color.blue()
    )
    embed.add_field(name="Game", value=game_name, inline=True)
    embed.add_field(name="Platform", value=platform_display, inline=True)
    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)
    embed.add_field(
        name="✅ You've been added to the notification list",
        value=(
            f"You and {subscriber_count} other user(s) will be notified "
            "when this request is fulfilled."
        ),
        inline=False
    )

    if selected_game.get('cover_url'):
        embed.set_thumbnail(url=selected_game['cover_url'])
    embed.set_footer(text="You'll receive a DM when this game is added to the collection")
    return embed


def build_existing_games_embed(matches: list, game_name: str, other_igdb_count: int) -> discord.Embed:
    """The collection already has something matching; offer it before requesting."""
    embed = discord.Embed(
        title="Games Found in Collection",
        description=(
            f"Found {len(matches)} game(s) matching '{game_name}' that are already available:"
        ),
        color=discord.Color.blue()
    )

    for rom in matches[:3]:
        embed.add_field(
            name=f"✅ {rom.get('name', 'Unknown')}",
            value=f"Available now - {rom.get('fs_name', 'Unknown')}",
            inline=False
        )

    if len(matches) > 3:
        embed.add_field(name="...", value=f"And {len(matches) - 3} more available", inline=False)

    instructions = ["• **Select an existing game** from the dropdown to download it"]
    if other_igdb_count:
        instructions.append(
            f"• **Request a different game** - Found {other_igdb_count} other game(s) on IGDB"
        )
    instructions.append(
        "• Click **Request Different Version** for ROM hacks, patches, or specific versions"
    )

    embed.add_field(name="What would you like to do?", value="\n".join(instructions), inline=False)
    return embed


# --- the detail embed -------------------------------------------------------
#
# IGDBGameView.create_game_detail_embed renders one game as a full page. It is
# one optional field per IGDB attribute, which is why it was long rather than
# complicated; the per-attribute formatters live here so the builder reads as
# the list of fields it is.

SUMMARY_LIMIT = 500

# Links shown under the detail embed, in render order, after the IGDB link
# itself. `None` for the emoji name means the literal globe, which sits
# *outside* the bold span where the custom emoji sit inside it -- a wording
# difference users would notice, so it is preserved rather than tidied.
#
# IGDB also reports an 'epic' website and we fetch it, but the Epic link has
# been commented out of this list since it was written. Left out here too,
# deliberately, rather than quietly turned on by the move.
DETAIL_WEBSITE_LINKS = (
    ('official', None, 'Website'),
    ('steam', 'steam', 'Steam'),
    ('gog', 'gog', 'GOG'),
    ('youtube', 'youtube', 'YouTube'),
    ('twitch', 'twitch', 'Twitch'),
)

LINKS_PER_ROW = 5


def igdb_slug(game_name: str) -> str:
    """IGDB's own URL slug: lowercase, spaces to hyphens, everything else gone."""
    return re.sub(r'[^a-z0-9-]', '', game_name.lower().replace(' ', '-'))


def build_detail_links_value(
    *,
    game_name: str,
    websites: Optional[Dict],
    emoji: Callable[[str], str],
) -> str:
    """The Links field: IGDB first, then whichever storefronts IGDB knows about.

    Membership, not truthiness, decides whether a link is rendered -- IGDB has
    been seen to report a key with an empty URL, and the original rendered a
    dead link in that case. Preserved; changing it is a fix, not a move.
    """
    websites = websites or {}
    links = [f"[**{emoji('igdb')} IGDB**](https://www.igdb.com/games/{igdb_slug(game_name)})"]
    for key, emoji_name, label in DETAIL_WEBSITE_LINKS:
        if key not in websites:
            continue
        if emoji_name is None:
            links.append(f"[\U0001F310 **{label}**]({websites[key]})")
        else:
            links.append(f"[**{emoji(emoji_name)} {label}**]({websites[key]})")

    rows = [
        "\u2003".join(links[i:i + LINKS_PER_ROW])
        for i in range(0, len(links), LINKS_PER_ROW)
    ]
    return "\n\n".join(rows)


def build_detail_platform_field(
    platforms,
    platform_name: Optional[str],
    platform_display: Callable[[str], str],
) -> Optional[Tuple[str, str]]:
    """The Platform(s) field, as (name, value), or None when there are none.

    A filtered view shows only the platform that was filtered on, singular;
    an unfiltered one shows up to three of the game's own, plural.
    """
    if not platforms:
        return None
    if platform_name:
        return "Platform", platform_display(platform_name)
    return "Platforms", format_capped_list(platforms, 3)


def format_detail_release_date(release_date: Optional[str]) -> str:
    """Release date for the detail embed, which always shows the field.

    Where `format_release_date` returns None for a date it cannot use, this
    falls back to showing it as IGDB sent it, and to 'Unknown' when there is
    nothing to show.
    """
    return format_release_date(release_date) or release_date or 'Unknown'


def format_detail_summary(summary: Optional[str]) -> Optional[str]:
    """The summary, truncated, or None when IGDB supplied no real one."""
    if not summary or summary == 'No summary available':
        return None
    if len(summary) > SUMMARY_LIMIT:
        return summary[:SUMMARY_LIMIT - 3] + "..."
    return summary
