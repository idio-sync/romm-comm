"""Rendering for a single request row.

RequestAdminView and UserRequestsView both show the same request; they differ
only in which buttons sit underneath. They used to carry a copy of this builder
each - 233 lines apiece, identical but for the docstring - so every change to
how a request looks had to be made twice.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional

import discord

from ..igdb_embeds import igdb_slug

logger = logging.getLogger(__name__)

DEFAULT_THUMBNAIL = (
    "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main"
    "/.backend/isotipo-small.png"
)

STATUS_COLORS = {
    'pending': discord.Color.yellow,
    'fulfilled': discord.Color.green,
    'cancelled': discord.Color.light_grey,
    'reject': discord.Color.red,
}

STATUS_EMOJI = {
    'pending': '⏳',
    'fulfilled': '✅',
    'cancelled': '🚫',
    'reject': '❌',
}


@dataclass
class RequestDetails:
    """What the free-text `details` column encodes.

    Requests store their IGDB match and any version notes as formatted text
    rather than columns, so this is the one place that format is parsed.
    """

    game_data: Dict[str, str] = field(default_factory=dict)
    cover_url: Optional[str] = None
    igdb_name: Optional[str] = None
    version_request: Optional[str] = None
    additional_notes: Optional[str] = None


def parse_request_details(details: str, fallback_name: str) -> RequestDetails:
    """Pull the IGDB metadata and version notes out of a request's details."""
    parsed = RequestDetails(igdb_name=fallback_name)
    details = details or ""

    if "Version Request:" in details:
        try:
            version_parts = details.split("Version Request: ", 1)[1].split("\n", 1)
            parsed.version_request = version_parts[0]
            if len(version_parts) > 1 and "Additional Notes:" in version_parts[1]:
                parsed.additional_notes = (
                    version_parts[1].replace("Additional Notes: ", "").split("\n")[0]
                )
        except (IndexError, ValueError):
            pass  # Parsing failed, use defaults

    if "IGDB Metadata:" in details:
        try:
            metadata_lines = details.split("IGDB Metadata:\n")[1].split("\n")
            for line in metadata_lines:
                if ": " in line:
                    key, value = line.split(": ", 1)
                    parsed.game_data[key] = value
                    if key == "Game":
                        parsed.igdb_name = value.split(" (", 1)[0]

            cover_matches = re.findall(r'Cover URL:\s*(https://[^\s]+)', details)
            if cover_matches:
                parsed.cover_url = cover_matches[0]
        except Exception as e:
            logger.error(f"Error parsing metadata: {e}")

    return parsed


def platform_is_in_romm(req, platform_status: Dict[Any, bool]) -> bool:
    """Look up a request's platform in the view's pre-fetched status cache.

    The cache is keyed by mapping id where the request has one, and by
    "name:<platform>" otherwise. An unknown platform counts as absent.
    """
    mapping_id = req['platform_mapping_id']
    if mapping_id and mapping_id in platform_status:
        logger.debug(f"Platform check (cached by ID): {platform_status[mapping_id]}")
        return platform_status[mapping_id]

    by_name = f"name:{req['platform']}"
    if by_name in platform_status:
        logger.debug(f"Platform check (cached by name): {platform_status[by_name]}")
        return platform_status[by_name]

    logger.debug(f"No cached platform status for {req['platform']}")
    return False


GAME_DATA_UNKNOWN = "Unknown"
SUMMARY_LIMIT = 500
FIELD_LIMIT = 1024


def game_data_list(game_data: Dict, key: str, cap: int) -> list:
    """A comma-joined game_data entry as a capped list.

    Empty when the key is absent or IGDB stored its "Unknown" placeholder --
    the two ways this table records "we do not know", which are not the same
    as the field being empty.
    """
    if key not in game_data or game_data[key] == GAME_DATA_UNKNOWN:
        return []
    return game_data[key].split(", ")[:cap]


def format_stored_release_date(game_data: Dict) -> Optional[str]:
    """The stored release date, left as it came if it will not parse."""
    stored = game_data.get("Release Date")
    if "Release Date" not in game_data or stored == GAME_DATA_UNKNOWN:
        return None
    try:
        return datetime.strptime(stored, "%Y-%m-%d").strftime("%B %d, %Y")
    except ValueError:
        return stored


def stored_companies(game_data: Dict) -> list:
    """Up to two companies, developers first, publishers filling what is left."""
    companies = game_data_list(game_data, "Developers", 2)
    remaining_slots = 2 - len(companies)
    if remaining_slots > 0:
        companies.extend(game_data_list(game_data, "Publishers", remaining_slots))
    return companies


def format_stored_summary(game_data: Dict) -> Optional[str]:
    """The stored summary, truncated. Presence decides, not content.

    Unlike the IGDB browse flow there is no "No summary available" placeholder
    here: a request row either carries a summary or does not.
    """
    if "Summary" not in game_data:
        return None
    summary = game_data["Summary"]
    if len(summary) > SUMMARY_LIMIT:
        return summary[:SUMMARY_LIMIT - 3] + "..."
    return summary


def build_platform_fields(req, platform_status: Dict[Any, bool], bot) -> list:
    """The Platform field, plus the warning that follows it when RomM lacks it.

    A platform RomM does not have yet is shown plainly: the emoji reads as a
    claim that it is available.

    Takes `bot` rather than the emoji service so that `bot.platform_emoji` is
    reached only on the branch that needs it. Callers that never hit that
    branch must keep working without the attribute, which is how the
    notification tests drive this.
    """
    exists = platform_is_in_romm(req, platform_status)
    display = bot.platform_emoji.format(req['platform']) if exists else req['platform']
    fields = [("Platform", f"{display} {' ✅' if exists else '🆕'}", True)]
    if not exists:
        fields.append((
            "⚠️ Platform Status",
            "This platform needs to be added to Romm before fulfillment",
            False,
        ))
    return fields


def build_request_links_value(igdb_name: Optional[str], emoji) -> Optional[str]:
    """The IGDB link, or None when the row never recorded a name to link to."""
    if not igdb_name:
        return None
    return f"[**{emoji('igdb')} IGDB**](https://www.igdb.com/games/{igdb_slug(igdb_name)})"


def build_fulfiller_fields(req) -> list:
    """Who closed the request, and whether a human did."""
    fields = []
    if req['fulfilled_by']:
        action = "Fulfilled" if req['status'] == 'fulfilled' else "Rejected"
        fields.append((f"✍️ {action} By", req['fulfiller_name'], True))
    if req['auto_fulfilled']:
        fields.append(("🤖 Auto-Fulfilled", "Yes", True))
    return fields


def build_request_embed(
    req,
    *,
    bot,
    platform_status: Dict[Any, bool],
    position: int,
    total: int,
    user_avatar_url: Optional[str] = None,
) -> discord.Embed:
    """Render one request row.

    Args:
        req: A row from the requests table, addressed by column name.
        bot: Used for the platform emoji service and the IGDB emoji.
        platform_status: Pre-fetched in_romm flags, see platform_is_in_romm.
        position: 1-based index of this request within the view.
        total: How many requests the view is paging through.
        user_avatar_url: Requester's avatar, used as the thumbnail.
    """
    parsed = parse_request_details(req['details'], fallback_name=req['game_name'])
    game_data = parsed.game_data

    colour = STATUS_COLORS.get(req['status'], discord.Color.blue)()
    embed = discord.Embed(title=f"{parsed.igdb_name}", color=colour)

    # Platform renders before the Request ID; the warning it may bring with it
    # renders after, on its own row.
    platform_field, *platform_warning = build_platform_fields(req, platform_status, bot)

    # Order matters: this is the order the fields appear in the embed. A value
    # of None drops the field.
    fields = [
        ("Status", f"{STATUS_EMOJI.get(req['status'], '❓')} **{req['status'].title()}**", True),
        platform_field,
        ("Request ID", f"#{req['id']}", True),
        *platform_warning,
        ("Version Requested", parsed.version_request and parsed.version_request[:FIELD_LIMIT], False),
        (
            "Additional Notes from User",
            parsed.additional_notes and parsed.additional_notes[:FIELD_LIMIT],
            False,
        ),
        ("Genre", ", ".join(game_data_list(game_data, "Genres", 2)) or None, True),
        ("Release Date", format_stored_release_date(game_data), True),
        ("Companies", ", ".join(stored_companies(game_data)) or None, True),
        ("Summary", format_stored_summary(game_data), False),
        ("Admin Notes", req['notes'] and req['notes'][:FIELD_LIMIT], False),
        *build_fulfiller_fields(req),
        ("Links", build_request_links_value(parsed.igdb_name, bot.get_formatted_emoji), True),
    ]

    if parsed.cover_url and parsed.cover_url != 'None':
        embed.set_image(url=parsed.cover_url)
    embed.set_thumbnail(url=user_avatar_url or DEFAULT_THUMBNAIL)

    for name, value, inline in fields:
        if value:
            embed.add_field(name=name, value=value, inline=inline)

    embed.set_footer(
        text=(
            f"Request {position}/{total} • Requested by {req['username']} "
            "• Use buttons to navigate"
        )
    )

    return embed


def format_igdb_details(selected_game: Dict) -> str:
    """Serialize an IGDB match into the text the details column stores.

    The counterpart to parse_request_details: this writes the format, that
    reads it. They are kept together so the two halves cannot drift.
    """
    alt_names_str = ""
    if selected_game.get('alternative_names'):
        alt_names = [
            f"{alt['name']} ({alt['comment']})" if alt.get('comment') else alt['name']
            for alt in selected_game['alternative_names']
        ]
        alt_names_str = f"\nAlternative Names: {', '.join(alt_names)}"

    def joined(key: str) -> str:
        values = selected_game.get(key)
        return ', '.join(values) if values else 'Unknown'

    return (
        f"IGDB Metadata:\n"
        f"Game: {selected_game['name']}{alt_names_str}\n"
        f"Release Date: {selected_game.get('release_date', 'Unknown')}\n"
        f"Platforms: {', '.join(selected_game.get('platforms', []))}\n"
        f"Developers: {joined('developers')}\n"
        f"Publishers: {joined('publishers')}\n"
        f"Genres: {joined('genres')}\n"
        f"Game Modes: {joined('game_modes')}\n"
        f"Summary: {selected_game.get('summary', 'No summary available')}\n"
        f"Cover URL: {selected_game.get('cover_url', 'None')}\n"
    )


def _cover_or_default(embed: discord.Embed, selected_game: Optional[Dict]) -> None:
    if selected_game and selected_game.get('cover_url'):
        embed.set_thumbnail(url=selected_game['cover_url'])
    else:
        embed.set_thumbnail(url=DEFAULT_THUMBNAIL)


def build_already_requested_embed(
    *, game: str, platform_display: str, request_id: int, selected_game: Optional[Dict]
) -> discord.Embed:
    """Shown when the requester already has this exact request open."""
    embed = discord.Embed(
        title="📋 Already Requested",
        description="You have already requested this game.",
        color=discord.Color.orange()
    )
    embed.add_field(name="Game", value=game, inline=True)
    embed.add_field(name="Platform", value=platform_display, inline=True)
    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)
    embed.add_field(name="Status", value="⏳ Still Pending", inline=True)
    embed.set_footer(text="You'll receive a DM when this game is added to the collection")
    _cover_or_default(embed, selected_game)
    return embed


def build_subscribed_embed(
    *,
    game: str,
    platform_display: str,
    request_id: int,
    requester_name: str,
    subscriber_count: int,
    selected_game: Optional[Dict],
) -> discord.Embed:
    """Shown when someone else asked first and this user joins the wait list."""
    embed = discord.Embed(
        title="📋 Request Already Exists",
        description=f"This game has already been requested by **{requester_name}**",
        color=discord.Color.blue()
    )
    embed.add_field(name="Game", value=game, inline=True)
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
    embed.set_footer(text="You'll receive a DM when this game is added to the collection")
    _cover_or_default(embed, selected_game)
    return embed


PLATFORM_MISSING_NOTE = (
    "This platform needs to be added to the collection before this request can be fulfilled."
)


def build_request_submitted_embed(
    *,
    game: str,
    platform_display: str,
    platform_exists: bool,
    request_id: int,
    author_name: str,
    user_details: Optional[str],
) -> discord.Embed:
    """Confirmation for a request submitted without an IGDB selection."""
    embed = discord.Embed(
        title="✅ Request Submitted",
        description=f"Your request for **{game}** has been submitted!",
        color=discord.Color.green()
    )

    platform_status = "✅ Available" if platform_exists else "🆕 Not Yet Added"
    embed.add_field(name="Game", value=game, inline=True)
    embed.add_field(name="Platform", value=f"{platform_display}\n{platform_status}", inline=True)
    embed.add_field(name="Status", value="⏳ Pending", inline=True)
    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)

    if not platform_exists:
        embed.add_field(name="📝 Note", value=PLATFORM_MISSING_NOTE, inline=False)

    if user_details and "IGDB Metadata:" not in user_details:
        embed.add_field(name="Details", value=user_details[:1024], inline=False)

    embed.set_footer(text=f"Request submitted by {author_name}")
    embed.set_thumbnail(url=DEFAULT_THUMBNAIL)
    return embed
