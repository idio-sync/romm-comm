"""Feed-client setup instructions for RomM's five homebrew installers."""

from .availability import available_device_keys, available_devices
from .catalog import DEVICES, DEVICES_BY_KEY, AuthStyle, Device, Feed, FeedClient
from .cog import Feeds
from .embeds import build_device_embed, feed_url
from .probe import detect_download_auth
from .verdict import DownloadAuth

__all__ = [
    "DEVICES",
    "DEVICES_BY_KEY",
    "AuthStyle",
    "Device",
    "DownloadAuth",
    "Feed",
    "FeedClient",
    "Feeds",
    "available_device_keys",
    "available_devices",
    "build_device_embed",
    "detect_download_auth",
    "feed_url",
]


def setup(bot):
    cog = Feeds(bot)
    bot.add_cog(cog)
    # Scheduled rather than awaited: cogs load from inside bot.py's own
    # on_ready, before the client is marked ready, so this has to run as
    # its own task that waits for readiness independently. See
    # Feeds.probe_when_ready for why the on_ready listener alone is not
    # enough.
    bot.loop.create_task(cog.probe_when_ready())
