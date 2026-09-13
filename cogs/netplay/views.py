"""Picking one ROM out of a search result.

Deliberately not cogs/requests/views_game.py's GameSelect: that one is built
from IGDB match dicts, hard-wires request-submission buttons, and truncates
at 25 with no page state. Netplay needs a RomM rom id and nothing else, so
this is a smaller component rather than a generalisation of that one -
generalising it would mean changing the request flow.

Discord caps a select at 25 options. This truncates too, but the caller says
so out loud rather than silently dropping matches.
"""

import logging
from typing import Any, Dict, List, Optional

import discord

logger = logging.getLogger(__name__)

MAX_SELECT_OPTIONS = 25


class RomSelect(discord.ui.Select):
    """One option per ROM, valued by rom id."""

    def __init__(self, roms: List[Dict[str, Any]]):
        self.roms = {str(rom["id"]): rom for rom in roms}

        options = [
            discord.SelectOption(
                label=str(rom.get("name") or rom.get("fs_name") or "Unknown")[:100],
                description=(str(rom.get("fs_name") or "")[:100] or None),
                value=str(rom["id"]),
            )
            for rom in roms
        ]

        super().__init__(
            placeholder="Pick the ROM to play...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.view.selected_rom = self.roms[self.values[0]]
        self.view.stop()


class RomSelectView(discord.ui.View):
    """Holds the select and the answer. Nothing else.

    Scoped to the person who ran the command: the announcement it produces is
    attributed to them, so letting a passer-by choose the ROM would put their
    name on a session they did not pick.
    """

    def __init__(self, roms: List[Dict[str, Any]], requester_id: int):
        super().__init__(timeout=120)
        self.requester_id = requester_id
        self.selected_rom: Optional[Dict[str, Any]] = None
        self.message: Optional[discord.Message] = None
        self.add_item(RomSelect(roms))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "This picker belongs to whoever ran the command. "
            "Run `/netplay` yourself to announce a session.",
            ephemeral=True,
        )
        return False

    async def on_timeout(self):
        """Leave nothing clickable behind."""
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable a timed-out netplay ROM select")
