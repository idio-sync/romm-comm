"""The /netplay command and the loop that keeps its posts current.

RomM's netplay list endpoint takes one game_id and has no enumerate-all form
(omitting it is a 422), so this cog polls exactly the ROMs that have a live
Discord post. That bounds the poll set to things someone is watching, and
every watcher reaches a terminal state and drops itself.

NETPLAY_ENABLED is enforced here rather than at load time because core_cogs
is an unconditional list - the {STEM}_ENABLED convention belongs to
load_integration_cogs, which this cog is deliberately not part of.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import discord
from discord.ext import commands

from .embeds import build_netplay_embed, render_key
from .views import MAX_SELECT_OPTIONS, RomSelectView
from .watcher import NetplayWatcher

logger = logging.getLogger(__name__)

# bot.py's default when DOMAIN is unset. It is a sentence, not a URL, and it
# must never end up inside a join link.
UNSET_DOMAIN = "No website configured"

ROM_SEARCH_LIMIT = 100


class Netplay(commands.Cog):
    """Announce netplay sessions and keep the announcements current."""

    def __init__(self, bot):
        self.bot = bot
        self.enabled = bot.config.NETPLAY_ENABLED
        # Both start True so a probe that cannot reach RomM fails open. Each
        # is flipped to False only on a positive answer: RomM reporting
        # netplay off, or a 401/403 on the room endpoint.
        self.server_enabled = True
        self.scope_ok = True
        self.watchers: Dict[int, NetplayWatcher] = {}

        if not self.enabled:
            logger.info("Netplay disabled (NETPLAY_ENABLED=false)")
            return

        # Not an on_ready listener: bot.py:779 calls load_all_cogs() from
        # inside on_ready, and Client.dispatch snapshots its listener list
        # before running them, so a cog added during that dispatch never sees
        # that event. Not cog_load either - py-cord has no such hook.
        bot.loop.create_task(self.check_server())

    # ------------------------------------------------------------- readiness

    def domain_configured(self) -> bool:
        """Whether DOMAIN is something we can build a join link out of."""
        domain = (self.bot.config.DOMAIN or "").strip()
        return bool(domain) and domain != UNSET_DOMAIN

    async def check_server(self) -> None:
        """Ask RomM once whether netplay is usable, and say so in the log.

        Fails open throughout: a probe that cannot reach RomM (it is still
        starting, say) leaves the feature enabled rather than silently
        disabling it until the next bot restart.
        """
        await self.bot.wait_until_ready()

        config = await self.bot.romm.get_server_config()

        if config is None:
            logger.warning(
                "Could not read RomM config to check netplay support; leaving "
                "netplay enabled. Commands will fail if the server has it off."
            )
            return

        self.server_enabled = bool(config.get("EJS_NETPLAY_ENABLED"))

        if not self.server_enabled:
            logger.warning(
                "RomM reports netplay disabled (emulatorjs.netplay.enabled in "
                "config.yml). /netplay will load but refuse to run."
            )
            return

        if not config.get("EJS_NETPLAY_ICE_SERVERS"):
            logger.warning(
                "RomM netplay has no ICE servers configured. WebRTC will use "
                "host candidates only: LAN play works, remote players will "
                "generally fail to connect."
            )

        await self.check_scope()

        logger.info("✅ RomM netplay available")

    async def check_scope(self) -> None:
        """Refuse the feature outright when the token definitely cannot poll.

        /api/config needs no special scope, so it cannot detect this: a token
        without assets.read passes that check and then returns None from every
        room poll. Announcements would post and then silently never update -
        worse than not offering the command, because the post looks live.

        Only a confirmed 401/403 disables anything. An undetermined probe
        (server down, network flaky) leaves the feature on, consistent with
        the fail-open rule above.
        """
        authorized = await self.bot.romm.netplay_scope_ok()

        if authorized is False:
            self.scope_ok = False
            logger.error(
                "The RomM token is missing the 'assets.read' scope, which "
                "/api/netplay/list requires. /netplay is disabled until the "
                "token is reissued with that scope."
            )
        elif authorized is None:
            logger.warning(
                "Could not confirm the RomM token has 'assets.read'; leaving "
                "netplay enabled. If announcements never find a room, that "
                "scope is the first thing to check."
            )

    @commands.Cog.listener()
    async def on_ready(self):
        if self.enabled and not self.server_enabled:
            await self.check_server()

    # ------------------------------------------------------------ resolution

    async def resolve_roms(
        self, platform: str, game: str
    ) -> Tuple[Optional[List[Dict[str, Any]]], bool, str]:
        """Search RomM for ROMs matching `game` on `platform`.

        Returns (roms, truncated, platform_display). `roms` is None when the
        search itself failed, which is not the same as finding nothing.
        Truncation is reported rather than hidden: Discord caps a select at 25
        options and the caller has to tell the user when matches were dropped.

        The display name comes from the platform lookup we already did, not
        from the ROM payload - no RomM rom row in this codebase is read for a
        display-name key, and inventing one risks it silently being absent.
        """
        platform_id, platform_name = await self.bot.find_platform_by_name(
            platform, await self.bot.fetch_api_endpoint('platforms')
        )
        if not platform_id:
            return [], False, ""

        display = self.bot.platform_emoji.format(platform_name) if platform_name else ""

        # Both platform_id and platform_ids, matching every other call site
        # (cogs/search.py:1495, :1708). The term is quoted because an & or #
        # in a game title would otherwise truncate or corrupt the query.
        payload = await self.bot.fetch_api_endpoint(
            f'roms?platform_id={platform_id}&platform_ids={platform_id}'
            f'&search_term={quote(game)}&limit={ROM_SEARCH_LIMIT}',
            bypass_cache=True,
        )
        if payload is None:
            return None, False, display

        if isinstance(payload, dict):
            items = payload.get('items', [])
        elif isinstance(payload, list):
            items = payload
        else:
            items = []

        truncated = len(items) > MAX_SELECT_OPTIONS
        return list(items[:MAX_SELECT_OPTIONS]), truncated, display

    # -------------------------------------------------------------- registry

    def at_capacity(self) -> bool:
        """Whether the poll set is full.

        The cap exists because every watcher costs a request per tick against
        RomM, and RommClient has no working client-side throttle - RateLimit
        is instantiated but acquire() is never called.
        """
        return len(self.watchers) >= self.bot.config.NETPLAY_MAX_WATCHERS

    def live_watcher_for(self, rom_id: int) -> Optional[NetplayWatcher]:
        """A non-terminal watcher already tracking this ROM, if there is one.

        One watcher per ROM: two posts for the same game would poll the same
        endpoint twice a tick for identical answers, and whichever post lost
        the registry slot would never be edited again - stranded forever on
        "waiting for a room".
        """
        watcher = self.watchers.get(rom_id)
        return watcher if watcher and not watcher.is_terminal else None

    def register_watcher(
        self,
        rom: Dict[str, Any],
        *,
        requester_id: int,
        requester_name: str,
        channel_id: int,
        platform_display: str,
    ) -> NetplayWatcher:
        """Start tracking one ROM.

        Everything the embed will need is captured here rather than looked up
        per render: the poll loop has no ctx, and bot.get_user() may no longer
        have the requester cached by the time a room opens.
        """
        watcher = NetplayWatcher(
            rom_id=int(rom["id"]),
            rom_name=str(rom.get("name") or rom.get("fs_name") or "Unknown"),
            requester_id=requester_id,
            requester_name=requester_name,
            channel_id=channel_id,
            created_at=time.time(),
            platform_display=platform_display,
            cover_url=rom.get("url_cover") or None,
        )
        self.watchers[watcher.rom_id] = watcher
        return watcher

    # --------------------------------------------------------------- command

    async def platform_autocomplete(self, ctx: discord.AutocompleteContext):
        """Platforms RomM actually has, the way cogs/search.py:1341 does it.

        Not the requests cog's platforms_repo: that returns three-column
        aiosqlite Rows (display_name, in_romm, folder_name), it offers
        platforms RomM does *not* have - which cannot be played - and it would
        couple this cog to cogs.requests and aiosqlite.
        """
        try:
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                return []

            names = [
                self.bot.get_platform_display_name(p)
                for p in raw_platforms
                if self.bot.get_platform_display_name(p)
            ]
            user_input = ctx.value.lower()
            return [name for name in names if user_input in name.lower()][:25]
        except Exception as e:
            logger.error(f"Error in netplay platform autocomplete: {e}")
            return []

    @discord.slash_command(name="netplay", description="Announce a netplay session for a game")
    async def netplay(
        self,
        ctx: discord.ApplicationContext,
        platform: discord.Option(str, "Platform the game is on", required=True,
                                 autocomplete=platform_autocomplete),
        game: discord.Option(str, "Game to play", required=True),
    ):
        """Post an announcement and start tracking the session."""
        await ctx.defer()

        if not self.enabled:
            await ctx.respond("Netplay is disabled on this bot.")
            return

        if not self.server_enabled:
            await ctx.respond(
                "This RomM server has netplay turned off. An admin needs to set "
                "`emulatorjs.netplay.enabled: true` in RomM's config.yml."
            )
            return

        if not self.scope_ok:
            # Refusing beats posting an announcement that can never update.
            await ctx.respond(
                "My RomM token is missing the `assets.read` scope, so I cannot "
                "see netplay rooms. An admin needs to reissue it with that "
                "scope added."
            )
            return

        if not self.domain_configured():
            await ctx.respond(
                "No public RomM URL is configured, so I cannot build a join "
                "link. An admin needs to set the `DOMAIN` environment variable."
            )
            return

        # Checked here to fail fast, and again in announce() - everything
        # between the two is awaited (a ROM search, possibly a human picking
        # from a select), and other invocations register watchers during it.
        if self.at_capacity():
            await ctx.respond(
                "I am already tracking as many netplay sessions as I can. "
                "Wait for one to finish and try again."
            )
            return

        roms, truncated, platform_display = await self.resolve_roms(platform, game)

        if roms is None:
            await ctx.respond("Could not reach RomM to search for that game.")
            return

        if not roms:
            await ctx.respond(f"No ROMs on **{platform}** matching **{game}**.")
            return

        if len(roms) == 1:
            await self.announce(ctx, roms[0], platform_display)
            return

        note = ""
        if truncated:
            note = (
                f"\nShowing the first {MAX_SELECT_OPTIONS} matches — "
                "narrow your search if the one you want is missing."
            )

        view = RomSelectView(roms, requester_id=ctx.author.id)
        view.message = await ctx.respond(f"Which one?{note}", view=view)
        await view.wait()

        if view.selected_rom is None:
            return

        await self.announce(ctx, view.selected_rom, platform_display)

    async def announce(
        self,
        ctx: discord.ApplicationContext,
        rom: Dict[str, Any],
        platform_display: str,
    ) -> None:
        """Post the PENDING embed and register the watcher behind it."""
        existing = self.live_watcher_for(int(rom["id"]))
        if existing is not None:
            await ctx.respond(
                f"**{existing.rom_name}** already has a live announcement in "
                "this server. Use that post rather than starting a second one."
            )
            return

        # Re-checked here rather than trusting the check in netplay(): a ROM
        # search and possibly a human picking from a select happened in
        # between, and other invocations were free to take the last slot.
        if self.at_capacity():
            await ctx.respond(
                "I am already tracking as many netplay sessions as I can. "
                "Wait for one to finish and try again."
            )
            return

        watcher = self.register_watcher(
            rom,
            requester_id=ctx.author.id,
            requester_name=ctx.author.display_name,
            channel_id=ctx.channel_id,
            platform_display=platform_display,
        )

        # Captured before the await, not after. The watcher is already in the
        # registry, so a poll can advance it to LIVE while this message is in
        # flight; recording render_key(watcher) afterwards would claim we had
        # delivered a LIVE embed when what actually went out said PENDING, and
        # the post would never be corrected. refresh_message skips a watcher
        # whose message_id is still None, so the tick in between is a no-op.
        embed = self.render(watcher)
        sent_key = render_key(watcher)

        # ctx.respond, not ctx.send: after a defer only a response or followup
        # clears Discord's "thinking..." placeholder, and ApplicationContext
        # .send is the plain Messageable send. It returns a WebhookMessage
        # here, so .id is available.
        try:
            message = await ctx.respond(embed=embed)
        except discord.HTTPException as e:
            # Release the slot. Leaving a watcher with no message behind would
            # hold a capacity slot and block this ROM forever, polling to
            # update a post that does not exist.
            logger.warning(f"Could not post netplay announcement: {e}")
            if self.watchers.get(watcher.rom_id) is watcher:
                del self.watchers[watcher.rom_id]
            return

        watcher.message_id = message.id
        watcher.last_render_key = sent_key

    def render(self, watcher: NetplayWatcher) -> discord.Embed:
        """Build the embed for a watcher's current state.

        Everything it needs was captured on the watcher at announce time, so
        this works identically from a command and from the poll loop.
        """
        return build_netplay_embed(watcher, domain=self.bot.config.DOMAIN)
