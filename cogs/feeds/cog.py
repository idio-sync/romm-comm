"""The /feeds command.

Thin by design: availability, probing and rendering all live in modules that
need no bot, so what is left here is argument handling and the two lookups
that need `self.bot`.
"""

import logging
import time
from typing import Optional

import discord
from discord.ext import commands

from .availability import available_devices, hosted_content_keys
from .catalog import DEVICES_BY_KEY
from .embeds import build_device_embed
from .probe import DownloadAuth, detect_download_auth

logger = logging.getLogger(__name__)


class Feeds(commands.Cog):
    """Setup instructions for RomM's homebrew feed clients."""

    def __init__(self, bot):
        # No I/O here: the cog has to be constructible from a bare fake bot.
        self.bot = bot
        self.download_auth = DownloadAuth.UNKNOWN
        self._probed_at = None

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready fires again after a reconnect; the staleness check below
        # is what keeps that from re-probing on every gateway hiccup.
        await self.ensure_probed()

    async def probe_when_ready(self):
        """Run the startup probe once the gateway connection is actually up.

        bot.py loads cogs from inside its own on_ready, and py-cord snapshots
        the on_ready listener list at dispatch time - a listener registered
        during that same dispatch (ours, above) misses the READY event that
        triggered it entirely. It would then first fire on a reconnect, or
        on the first /feeds invocation via ensure_probed(). setup() schedules
        this coroutine as a bare task instead, the same idiom the deleted
        Info.check_switch_platform used for the same reason.
        """
        await self.bot.wait_until_ready()
        try:
            await self.ensure_probed()
        except Exception as e:
            # A startup probe failure must never take the bot down with it.
            logger.error(f"Startup download-auth probe failed: {e}", exc_info=True)

    def _probe_is_stale(self) -> bool:
        if self._probed_at is None:
            return True
        age = time.monotonic() - self._probed_at
        return age > getattr(self.bot.config, "SYNC_RATE", 3600)

    async def ensure_probed(self):
        """Re-probe when the last verdict has aged past SYNC_RATE.

        A one-shot latch would freeze an UNKNOWN verdict for the life of the
        process if RomM happened to be unreachable or the library empty at
        startup, and would keep warning about download auth long after an
        admin turned it off.
        """
        if not self._probe_is_stale():
            return
        # Stamped before the await so concurrent invocations do not pile up
        # probes; a failure below leaves the stamp, and the next attempt
        # comes on the following staleness window.
        self._probed_at = time.monotonic()
        try:
            self.download_auth = await detect_download_auth(
                self.bot.config.DOMAIN, self.bot.fetch_api_endpoint
            )
        except Exception as e:
            logger.error(f"Download-auth probe failed: {e}", exc_info=True)
            self.download_auth = DownloadAuth.UNKNOWN

    def cached_platforms(self):
        """The platforms payload bot.py refreshes every SYNC_RATE."""
        return self.bot.cache.get("platforms")

    def offered_devices(self):
        """Hardware worth offering, given what this server holds ROMs for."""
        return available_devices(self.cached_platforms())

    async def romm_username(self, discord_id: int) -> Optional[str]:
        try:
            link = await self.bot.db.get_user_link(discord_id)
        except Exception as e:
            logger.error(f"Could not look up RomM link for {discord_id}: {e}")
            return None
        return (link or {}).get("romm_username")

    def device_title(self, device) -> str:
        """The display name, emoji-decorated when we can manage it.

        PlatformEmoji.format returns "Name <emoji>" and falls back to a plain
        🎮, so this cannot KeyError the way indexing emoji_dict would.
        """
        platform_emoji = getattr(self.bot, "platform_emoji", None)
        if not platform_emoji:
            return device.display_name
        try:
            return platform_emoji.format(device.display_name)
        except Exception:
            return device.display_name

    async def device_autocomplete(self, ctx: discord.AutocompleteContext):
        typed = (ctx.value or "").lower()
        return [
            device.display_name
            for device in self.offered_devices()
            if typed in device.display_name.lower()
        ][:25]

    @discord.slash_command(
        name="feeds",
        description="Setup instructions for connecting a console to this server",
    )
    async def feeds(
        self,
        ctx,
        device: discord.Option(
            str,
            "The console you want to set up",
            required=True,
            autocomplete=device_autocomplete,
        ),
    ):
        # Defer first. A cold first invocation runs ensure_probed, which can
        # spend three retries listing ROMs plus a 10s probe timeout - well
        # past Discord's 3-second interaction window, which would show the
        # user "The application did not respond".
        await ctx.defer(ephemeral=True)

        await self.ensure_probed()
        offered = self.offered_devices()

        if not offered:
            await ctx.respond(
                "This server doesn't host any platforms with feed clients.",
                ephemeral=True,
            )
            return

        wanted = device.strip().lower()
        match = next(
            (d for d in DEVICES_BY_KEY.values()
             if wanted in (d.key, d.display_name.lower())),
            None,
        )

        if not match:
            names = ", ".join(f"`{d.display_name}`" for d in offered)
            await ctx.respond(
                f"I don't have feeds for `{device}`. Available here: {names}",
                ephemeral=True,
            )
            return

        if match.key not in {d.key for d in offered}:
            names = ", ".join(f"`{d.display_name}`" for d in offered)
            await ctx.respond(
                f"This server doesn't host {match.display_name}. Available here: {names}",
                ephemeral=True,
            )
            return

        try:
            embed = build_device_embed(
                match,
                self.bot.config.DOMAIN,
                await self.romm_username(ctx.author.id),
                self.download_auth,
                hosted=hosted_content_keys(self.cached_platforms()),
                title=self.device_title(match),
            )
            await ctx.respond(embed=embed, ephemeral=True)
        except Exception as e:
            logger.error(f"Error building the feeds embed: {e}", exc_info=True)
            await ctx.respond(
                "❌ Something went wrong building those instructions.",
                ephemeral=True,
            )
