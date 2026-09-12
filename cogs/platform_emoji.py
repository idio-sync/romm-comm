"""Naming a platform with the emoji that belongs to it.

This lived on the Search cog, so eleven call sites across five other cogs
reached for `bot.get_cog('Search')` to format a platform name - a runtime
lookup by string, for a table and two dictionaries that have nothing to do
with searching.

Each of those call sites had to cope with Search not being loaded, and quietly
showed a bare platform name when it was not. The service is always there, so
that fallback is gone.
"""

import logging

logger = logging.getLogger(__name__)

# Platform display name -> the emoji names that may stand for it, best first.
PLATFORM_VARIANTS = {
    '3DO Interactive Multiplayer': ['3do'],
    'Apple II': ['apple_ii'],
    'Amiga': ['amiga'],
    'Amiga CD32': ['cd32'],
    'Amstrad CPC': ['amstrad'],
    'Apple Pippin': ['pippin'],
    'Arcade - MAME': ['arcade'],
    'Arcade - PC Based': ['arcade'],
    'Arcade - FinalBurn Neo': ['arcade'],
    'Atari 2600': ['2600'],
    'Atari 5200': ['5200'],
    'Atari 7800': ['7800'],
    'Atari Jaguar': ['jaguar'],
    'Atari Jaguar CD': ['jaguar_cd'],
    'Atari Lynx': ['lynx'],
    'Casio Loopy': ['loopy'],
    'Commodore C64/128/MAX': ['c64'],
    'Dreamcast': ['dreamcast'],
    'Family Computer': ['famicom'],
    'Famicom': ['famicom'],
    'Family Computer Disk System': ['fds'],
    'Famicom Disk System': ['fds'],
    'FM Towns': ['fm_towns'],
    'Game & Watch': ['game_and_watch'],
    'Game Boy': ['gameboy', 'gameboy_pocket'],
    'Game Boy Advance': ['gameboy_advance', 'gameboy_advance_sp', 'gameboy_micro'],
    'Game Boy Color': ['gameboy_color'],
    'J2ME': ['cell_java'],
    'Mac': ['mac', 'mac_imac'],
    'Mega Duck/Cougar Boy': ['mega_duck'],
    'MSX': ['msx'],
    'MSX2': ['msx'],
    'N-Gage': ['n_gage'],
    'Neo Geo AES': ['neogeo_aes'],
    'Neo Geo CD': ['neogeo_cd'],
    'Neo Geo Pocket': ['neogeo_pocket'],
    'Neo Geo Pocket Color': ['neogeo_pocket_color'],
    'Nintendo 3DS': ['3ds'],
    'Nintendo 64': ['n64'],
    'Nintendo 64Dd': ['n64_dd'],
    'Nintendo 64DD': ['n64_dd'],
    'Nintendo DS': ['ds', 'ds_lite'],
    'Nintendo DSi': ['dsi'],
    'Nintendo Entertainment System': ['nes'],
    'Nintendo GameCube': ['gamecube'],
    'Nintendo Switch': ['switch', 'switch_docked'],
    'PC-8800 Series': ['pc_88'],
    'PC-9800 Series': ['pc_98'],
    'PC-FX': ['pc_fx'],
    'PC (Microsoft Windows)': ['pc'],
    'PC - DOS': ['dos'],
    'PC - Win3X': ['win_3x_gui', 'pc'],
    'PC - Windows': ['pc', 'win_9x'],
    'Philips CD-i': ['cd_i'],
    'PlayStation': ['ps', 'ps_one'],
    'PlayStation 2': ['ps2', 'ps2_slim'],
    'PlayStation 3': ['ps3', 'ps3_slim'],
    'PlayStation 4': ['ps4'],
    'PlayStation 5': ['ps5'],
    'PlayStation Portable': ['psp', 'psp_go'],
    'PlayStation Vita': ['vita'],
    'Pokémon mini': ['pokemon_mini'],
    'Sega 32X': ['32x'],
    'Sega CD': ['sega_cd'],
    'Segacd': ['sega_cd'],
    'Sega Game Gear': ['game_gear'],
    'Sega Master System/Mark III': ['master_system'],
    'Sega Mega Drive/Genesis': ['genesis', 'genesis_2', 'nomad'],
    'Sega Pico': ['pico'],
    'Sega Saturn': ['saturn_2'],
    'Sharp X68000': ['x68000'],
    'Sinclair Zxs': ['zx_spectrum'],
    'Super Famicom': ['sfam'],
    'Super Nintendo Entertainment System': ['snes'],
    'Switch': ['switch', 'switch_docked'],
    'Teknoparrot': ['teknoparrot'],
    'Turbografx-16/PC Engine CD': ['tg_16_cd'],
    'TurboGrafx-16/PC Engine': ['tg_16', 'turboduo', 'turboexpress'],
    'Vectrex': ['vectrex'],
    'Virtual Boy': ['virtual_boy'],
    'Visual Memory Unit / Visual Memory System': ['vmu'],
    'Wii': ['wii'],
    'Windows': ['pc'],
    'WonderSwan': ['wonderswan'],
    'WonderSwan Color': ['wonderswan'],
    'Xbox': ['xbox_og'],
    'Xbox 360': ['xbox_360'],
    'Xbox One': ['xbone'],
}


class PlatformEmoji:
    """Formats a platform name with a server, application, or fallback emoji."""

    def __init__(self, bot):
        self.bot = bot
        self.variants = PLATFORM_VARIANTS

    def format(self, platform_name: str) -> str:
        """Returns platform name with its emoji if available."""
        if not platform_name:
            return platform_name

        # Get the potential emoji names (e.g., ['n64']) for the platform
        variant_names = self.variants.get(
            platform_name, [platform_name.lower().replace(' ', '_').replace('-', '_')]
        )
        variants_to_check = variant_names if isinstance(variant_names, list) else [variant_names]

        # Build a quick lookup for all visible server emojis
        # self.bot.emojis contains all server-specific emojis the bot can see
        server_emojis_by_name = {e.name: e for e in self.bot.emojis}

        for variant in variants_to_check:
            # Priority 1: Check for a server-specific emoji.
            if variant in server_emojis_by_name:
                return f"{platform_name} {server_emojis_by_name[variant]}"

            # Priority 2: Check for a global application emoji.
            # Add safe checking here
            if hasattr(self.bot, 'emoji_dict') and variant in self.bot.emoji_dict:
                return f"{platform_name} {self.bot.emoji_dict[variant]}"

        # If no custom emoji found, use a fallback.
        return f"{platform_name} 🎮"
