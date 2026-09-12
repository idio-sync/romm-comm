import logging
from urllib.parse import quote_plus

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

RA_LOGO_URL = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png"
TOP_N = 10

# RA award tiers that count as "mastered" for the leaderboard (case-insensitive).
# RA's real tiers include beaten-softcore / beaten-hardcore / completed / mastered;
# "beaten-*" intentionally does NOT count as mastery.
MASTERY_KINDS = {"mastered", "completed"}


def _is_mastery(kind) -> bool:
    return bool(kind) and str(kind).strip().lower() in MASTERY_KINDS


def _results_of(user) -> list:
    """Defensively read a user's RA progression results (flattened dict, keys may be absent)."""
    prog = user.get("ra_progression") or {}
    return prog.get("results") or []


def _short_name(name, limit=24):
    name = name or ""
    return name if len(name) <= limit else name[:limit - 1] + "…"


def compute_global_leaderboard(users, links, bot_username=None):
    """Rank users by total RA achievements earned across all games.

    users: list of RomM UserSchema dicts (id, username, ra_progression).
    links: dict mapping romm user id -> resolved Discord display name (linked users only).
    bot_username: RomM username of the bot's own account to exclude (or None/empty to skip).
    Returns: list of row dicts sorted best-first. Each row:
        {romm_id, name, is_linked, earned, hardcore, mastered}
    """
    bot_name = (bot_username or "").strip().lower()
    rows = []
    for user in users:
        username = user.get("username") or ""
        if bot_name and username.strip().lower() == bot_name:
            continue
        results = _results_of(user)
        if not results:
            continue
        earned = sum((r.get("num_awarded") or 0) for r in results)
        if earned < 1:
            continue
        hardcore = sum((r.get("num_awarded_hardcore") or 0) for r in results)
        mastered = sum(1 for r in results if _is_mastery(r.get("highest_award_kind")))
        romm_id = user.get("id")
        linked_name = links.get(romm_id)
        rows.append({
            "romm_id": romm_id,
            "name": linked_name or username,
            "is_linked": linked_name is not None,
            "earned": earned,
            "hardcore": hardcore,
            "mastered": mastered,
        })
    rows.sort(key=lambda r: (-r["earned"], -r["hardcore"], -r["mastered"], r["name"].lower()))
    return rows


def compute_game_leaderboard(users, links, rom_ra_id, bot_username=None):
    """Rank users by achievements earned for one specific game (matched on rom_ra_id).

    Returns rows sorted best-first; each row:
        {romm_id, name, is_linked, earned, max_possible, hardcore, award_kind}
    Per-game tiebreak is hardcore -> name (mastery is implied by earned == max_possible).
    """
    bot_name = (bot_username or "").strip().lower()
    rows = []
    for user in users:
        username = user.get("username") or ""
        if bot_name and username.strip().lower() == bot_name:
            continue
        entry = next((r for r in _results_of(user) if r.get("rom_ra_id") == rom_ra_id), None)
        if entry is None:
            continue
        earned = entry.get("num_awarded") or 0
        if earned < 1:
            continue
        romm_id = user.get("id")
        linked_name = links.get(romm_id)
        rows.append({
            "romm_id": romm_id,
            "name": linked_name or username,
            "is_linked": linked_name is not None,
            "earned": earned,
            "max_possible": entry.get("max_possible"),
            "hardcore": entry.get("num_awarded_hardcore") or 0,
            "award_kind": entry.get("highest_award_kind"),
        })
    rows.sort(key=lambda r: (-r["earned"], -r["hardcore"], r["name"].lower()))
    return rows


class RetroAchievements(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db_manager = bot.db

    async def _build_links_map(self, ctx):
        """Return {romm_id: discord_display_name} for all linked users.

        Resolves the live guild member display name when possible, falling back to
        the stored Discord username, then the RomM username.
        """
        links = {}
        try:
            for link in await self.db_manager.get_all_user_links():
                romm_id = link.get("romm_id")
                if romm_id is None:
                    continue
                name = None
                member = ctx.guild.get_member(link["discord_id"]) if ctx.guild else None
                if member:
                    name = member.display_name
                links[romm_id] = name or link.get("discord_username") or link.get("romm_username")
        except Exception as e:
            logger.error(f"Failed to build user-links map: {e}")
        return links

    def _format_global_line(self, rank, row):
        marker = "🔗 " if row["is_linked"] else ""
        return (f"**#{rank}** {marker}**{_short_name(row['name'])}** — "
                f"{row['earned']:,} 🏆 · {row['hardcore']:,} hardcore · {row['mastered']} mastered")

    def _build_global_embed(self, rows):
        embed = discord.Embed(
            title="🏆 RetroAchievements Leaderboard",
            description="Ranked by total achievements earned",
            color=discord.Color.gold(),
        )
        lines = [self._format_global_line(i + 1, r) for i, r in enumerate(rows[:TOP_N])]
        embed.add_field(name="​", value="\n".join(lines), inline=False)
        embed.set_thumbnail(url=RA_LOGO_URL)
        embed.set_footer(text=f"Top {min(TOP_N, len(rows))} of {len(rows)} players with RA progress · 🔗 = linked Discord member")
        return embed

    @discord.slash_command(
        name="ra-leaderboard",
        description="RetroAchievements leaderboard (global, or per-game if you name a game)",
    )
    async def ra_leaderboard(self, ctx: discord.ApplicationContext,
                             game: discord.Option(str, "Game name for a per-game board", required=False, default=None) = None):
        await ctx.defer()
        users = await self.bot.fetch_api_endpoint('users')
        if users is None:
            await ctx.respond("❌ Can't read RomM users right now — check RomM connectivity and make sure the bot's token includes the `users.read` scope.")
            return

        bot_username = getattr(self.bot.config, "USER", None)
        links = await self._build_links_map(ctx)

        if game:
            await self._respond_game_leaderboard(ctx, users, links, bot_username, game)
            return

        rows = compute_global_leaderboard(users, links, bot_username=bot_username)
        if not rows:
            await ctx.respond("No RetroAchievements progress is tracked yet.")
            return
        await ctx.respond(embed=self._build_global_embed(rows))

    def _format_game_line(self, rank, row):
        marker = "🔗 " if row["is_linked"] else ""
        mp = row.get("max_possible")
        count = f"{row['earned']}/{mp}" if mp else f"{row['earned']}"
        award = f" ✦ {row['award_kind'].replace('-', ' ').title()}" if row.get("award_kind") else ""
        return (f"**#{rank}** {marker}**{_short_name(row['name'])}** — "
                f"{count} 🏆{award} · {row['hardcore']:,} hardcore")

    def _build_game_embed(self, rom, rows):
        title = rom.get("name") or "Unknown game"
        platform = rom.get("platform_name")
        header_title = f"🏆 RetroAchievements — {title}" + (f" ({platform})" if platform else "")
        set_size = next((r["max_possible"] for r in rows if r.get("max_possible")), None)
        desc = "Ranked by achievements earned"
        if set_size:
            desc += f" · {set_size} achievements in this set"
        embed = discord.Embed(title=header_title, description=desc, color=discord.Color.gold())
        lines = [self._format_game_line(i + 1, r) for i, r in enumerate(rows[:TOP_N])]
        embed.add_field(name="​", value="\n".join(lines), inline=False)
        # Cover served unauthenticated at /assets/...; set directly as thumbnail when present.
        platform_id, rom_id = rom.get("platform_id"), rom.get("id")
        if rom.get("url_cover") and platform_id and rom_id:
            embed.set_thumbnail(url=f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png")
        else:
            embed.set_thumbnail(url=RA_LOGO_URL)
        embed.set_footer(text=f"Top {min(TOP_N, len(rows))} of {len(rows)} players who have played this game · 🔗 = linked Discord member")
        return embed

    async def _respond_game_leaderboard(self, ctx, users, links, bot_username, game):
        search = await self.bot.fetch_api_endpoint(f"roms?search_term={quote_plus(game)}&limit=25")
        if isinstance(search, dict):
            items = search.get("items", [])
        elif isinstance(search, list):
            items = search
        else:
            items = []
        if not items:
            await ctx.respond(f"Couldn't find a game matching “{game}”.")
            return
        rom = items[0]
        ra_id = rom.get("ra_id")
        if not ra_id:
            await ctx.respond(f"**{rom.get('name', game)}** isn't tracked on RetroAchievements.")
            return

        rows = compute_game_leaderboard(users, links, ra_id, bot_username=bot_username)
        if not rows:
            # Diagnostic: distinguish a systematic ra_id/rom_ra_id mismatch from a genuinely unplayed game.
            any_progression = any(_results_of(u) for u in users)
            if any_progression:
                logger.warning(
                    f"/ra-leaderboard '{game}' resolved ra_id={ra_id} but matched 0 progression rows "
                    f"while users have RA progress — possible ra_id/rom_ra_id id-space mismatch."
                )
            await ctx.respond(f"No one has RetroAchievements progress on **{rom.get('name', game)}** yet.")
            return
        await ctx.respond(embed=self._build_game_embed(rom, rows))


def setup(bot):
    bot.add_cog(RetroAchievements(bot))
