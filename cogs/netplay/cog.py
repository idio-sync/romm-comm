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
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from discord.ext import commands

from .views import MAX_SELECT_OPTIONS
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
