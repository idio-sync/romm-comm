"""Netplay session announcements.

RomM has run netplay since 5.x and tells nobody a session is happening: a
room exists, unlisted, until someone opens the same game's player and looks.
This package posts that fact to Discord and keeps the post current.

Split the way cogs/requests is - a pure state machine, pure renderers, a
view, and a cog that does the IO - because the interesting behaviour is the
state machine and it is worth testing without a bot.
"""

from .embeds import ENDED_HINT, build_netplay_embed, render_key
from .watcher import NetplayState, NetplayWatcher, advance

__all__ = [
    "ENDED_HINT",
    "NetplayState",
    "NetplayWatcher",
    "advance",
    "build_netplay_embed",
    "render_key",
]
