import discord
from discord.ext import commands
import aiohttp
import asyncio
from typing import Dict, List, Tuple
import logging
import base64

logger = logging.getLogger('romm_bot')

class EmojiManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # Different URLs for different emoji sets
        self.emoji_url_list = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/emoji/emoji_urls.txt"
        self.emoji_url_list_extended = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/emoji/emoji_urls_extended.txt"
        self.emoji_url_list_application = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/emoji/emoji_urls_application.txt"
        
        # Store different emoji types
        self.bot.emoji_dict = {}  # Application emojis (work everywhere)
        self.server_emojis = {}   # Server-specific emojis (per guild)
        
        # Track sync status
        self.app_emoji_synced = False
        self.sync_in_progress = False
        self.db = bot.db  # Database for tracking server sync state
        
        # Platform mappingsfor server emojis
        self.platform_variants = {
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
            'Casio Loopy': ['loopy],
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
        
        # Start initialization on boot
        bot.loop.create_task(self.initialize_all_emojis())

    async def get_application_emojis(self):
        """Get application emojis using direct HTTP request"""
        try:
            if not self.bot.application_id:
                app_info = await self.bot.application_info()
                self.bot.application_id = app_info.id
            
            route = discord.http.Route('GET', '/applications/{application_id}/emojis',
                                     application_id=self.bot.application_id)
            data = await self.bot.http.request(route)
            
            if isinstance(data, dict) and 'items' in data:
                return data['items']
            elif isinstance(data, list):
                return data
            else:
                return []
                
        except Exception as e:
            logger.error(f"Error fetching application emojis: {e}")
            return []

    async def create_application_emoji(self, name: str, image_data: bytes):
        """Create an application emoji"""
        try:
            if not self.bot.application_id:
                app_info = await self.bot.application_info()
                self.bot.application_id = app_info.id
            
            image_b64 = base64.b64encode(image_data).decode('utf-8')
            image_format = 'data:image/png;base64,'
            
            payload = {
                'name': name,
                'image': f"{image_format}{image_b64}"
            }
            
            route = discord.http.Route('POST', '/applications/{application_id}/emojis',
                                     application_id=self.bot.application_id)
            return await self.bot.http.request(route, json=payload)
        except Exception as e:
            logger.error(f"Error creating application emoji {name}: {e}")
            raise

    async def initialize_all_emojis(self):
        """Initialize and sync all emojis on startup"""
        await self.bot.wait_until_ready()
        
        while not self.bot.db._initialized:
            await asyncio.sleep(0.1)
        
        logger.debug("Starting emoji initialization...")
        
        try:
            # Load existing application emojis
            app_emojis = await self.get_application_emojis()
            
            for emoji_data in app_emojis:
                emoji = discord.PartialEmoji(
                    name=emoji_data['name'],
                    id=int(emoji_data['id']),
                    animated=emoji_data.get('animated', False)
                )
                self.bot.emoji_dict[emoji_data['name']] = emoji
            
            logger.info(f"Loaded {len(app_emojis)} existing application emojis")
            
            # Auto-sync missing application emojis
            await self.sync_application_emojis(silent=False)
            
            # Check and sync server emojis
            await self.check_server_emojis_on_boot()
            
        except Exception as e:
            logger.error(f"Error initializing emojis: {e}")

    async def check_server_emojis_on_boot(self):
        """Check and sync server emojis on boot"""
        for guild in self.bot.guilds:
            try:
                await self.sync_server_emojis(guild)
            except Exception as e:
                logger.error(f"Error syncing server emojis for {guild.name}: {e}")

    def is_nitro_server(self, guild: discord.Guild) -> bool:
        """Check if server has Nitro boost"""
        return guild and guild.emoji_limit > 50

    async def get_guild_sync_state(self, guild_id: int) -> dict:
        """Get sync state for a guild from database"""
        # Use 'async with' to correctly get a database connection
        async with self.db.get_connection() as conn:
            cursor = await conn.execute(
                "SELECT nitro_status, emoji_limit FROM emoji_sync_state WHERE guild_id = ?",
                (guild_id,)
            )
            row = await cursor.fetchone()
            if row:
                return {
                    'nitro_status': bool(row[0]),
                    'emoji_limit': row[1]
                }
            return None

    async def save_guild_sync_state(self, guild_id: int, nitro_status: bool, emoji_limit: int):
        """Save guild sync state to database"""
        async with self.db.get_connection() as conn:
            await conn.execute('''
                INSERT OR REPLACE INTO emoji_sync_state 
                (guild_id, nitro_status, emoji_limit, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ''', (guild_id, int(nitro_status), emoji_limit))

    async def sync_server_emojis(self, guild: discord.Guild):
        """Sync server-specific emojis"""
        if not guild.me.guild_permissions.manage_emojis:
            logger.warning(f"No emoji permissions in {guild.name}")
            return
        
        try:
            # Check current Nitro status
            current_nitro = self.is_nitro_server(guild)
            current_limit = guild.emoji_limit
            
            # Get previous state
            prev_state = await self.get_guild_sync_state(guild.id)
            
            # Handle Nitro status changes
            if prev_state and prev_state['nitro_status'] != current_nitro:
                await self.handle_nitro_change(guild, prev_state['nitro_status'], current_nitro)
            
            # Load appropriate emoji list
            emoji_type = 'extended' if current_nitro else 'standard'
            emoji_list = await self.load_emoji_list(emoji_type)
            
            if not emoji_list:
                return
            
            # Limit to what the server can hold
            max_emojis = min(len(emoji_list), current_limit)
            emoji_list = emoji_list[:max_emojis]
            
            # Find missing emojis
            existing_names = {e.name for e in guild.emojis}
            missing_emojis = [(name, url) for name, url in emoji_list if name not in existing_names]
            
            if missing_emojis:
                slots_available = current_limit - len(guild.emojis)
                
                logger.info(f"Server {guild.name}: {len(missing_emojis)} missing emojis, {slots_available} slots available")
                
                # Upload missing emojis
                uploaded = 0
                for name, url in missing_emojis[:slots_available]:
                    try:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as response:
                                if response.status == 200:
                                    image_data = await response.read()
                                    await guild.create_custom_emoji(
                                        name=name,
                                        image=image_data,
                                        reason="Auto-sync server emojis on boot"
                                    )
                                    uploaded += 1
                                    
                                    # Update server emoji cache
                                    if guild.id not in self.server_emojis:
                                        self.server_emojis[guild.id] = {}
                                    
                                    # Rate limiting
                                    await asyncio.sleep(1.5)
                    except Exception as e:
                        logger.error(f"Error uploading {name} to {guild.name}: {e}")
                
                if uploaded > 0:
                    logger.info(f"Uploaded {uploaded} emojis to {guild.name}")
            
            # Save current state
            await self.save_guild_sync_state(guild.id, current_nitro, current_limit)
            
            # Update server emoji cache
            self.server_emojis[guild.id] = {e.name: e for e in guild.emojis}
            
        except Exception as e:
            logger.error(f"Error syncing server emojis for {guild.name}: {e}")

    async def handle_nitro_change(self, guild: discord.Guild, had_nitro: bool, has_nitro: bool):
        """Handle emoji adjustments when Nitro status changes"""
        logger.info(f"Nitro status changed for {guild.name}: {had_nitro} -> {has_nitro}")
        
        if had_nitro and not has_nitro:
            # Lost Nitro - remove excess emojis
            emoji_list = await self.load_emoji_list('standard')
            keep_names = {name for name, _ in emoji_list[:50]}
            
            for emoji in guild.emojis:
                if emoji.name not in keep_names:
                    try:
                        await emoji.delete(reason="Server lost Nitro status")
                        await asyncio.sleep(1.2)
                    except Exception as e:
                        logger.error(f"Error removing emoji {emoji.name}: {e}")

    async def load_emoji_list(self, emoji_type: str = 'standard') -> List[Tuple[str, str]]:
        """Load emoji list from URL based on type"""
        try:
            if emoji_type == 'application':
                url = self.emoji_url_list_application
            elif emoji_type == 'extended':
                url = self.emoji_url_list_extended
            else:  # standard
                url = self.emoji_url_list
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return []
                    content = await response.text()
            
            emoji_list = []
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):
                    try:
                        name, url = line.split('|')
                        clean_name = name.strip().replace('-', '_').lower()
                        emoji_list.append((clean_name, url.strip()))
                    except ValueError:
                        continue
            
            return emoji_list
            
        except Exception as e:
            logger.error(f"Failed to load {emoji_type} emoji list: {e}")
            return []

    async def sync_application_emojis(self, silent: bool = False):
        """Sync application emojis with the application emoji list"""
        if self.sync_in_progress:
            return 0
            
        self.sync_in_progress = True
        
        try:
            app_emojis = await self.get_application_emojis()
            existing_names = {emoji['name'] for emoji in app_emojis}
            
            emoji_list = await self.load_emoji_list(emoji_type='application')
            if not emoji_list:
                return 0
            
            missing_emojis = [(name, url) for name, url in emoji_list if name not in existing_names]
            
            if not missing_emojis:
                logger.info("All application emojis already synced!")
                return 0
            
            slots_available = 2000 - len(existing_names)
            if slots_available < len(missing_emojis):
                missing_emojis = missing_emojis[:slots_available]
            
            logger.info(f"Uploading {len(missing_emojis)} missing application emojis...")
            
            uploaded = 0
            for name, url in missing_emojis:
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url) as response:
                            if response.status != 200:
                                continue
                            image_data = await response.read()
                    
                    emoji_data = await self.create_application_emoji(name, image_data)
                    
                    emoji = discord.PartialEmoji(
                        name=emoji_data['name'],
                        id=int(emoji_data['id']),
                        animated=emoji_data.get('animated', False)
                    )
                    self.bot.emoji_dict[name] = emoji
                    uploaded += 1
                    
                    if uploaded % 10 == 0:
                        logger.info(f"Progress: {uploaded}/{len(missing_emojis)} emojis uploaded")
                    
                    if uploaded % 25 == 0:
                        await asyncio.sleep(60)
                    else:
                        await asyncio.sleep(2.5)
                        
                except Exception as e:
                    logger.error(f"Error uploading {name}: {e}")
            
            logger.info(f"Application emoji sync complete: {uploaded} uploaded")
            return uploaded
            
        finally:
            self.sync_in_progress = False

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """When bot joins a new server, sync emojis"""
        logger.info(f"Joined {guild.name}")
        await self.sync_server_emojis(guild)

def setup(bot):
    bot.add_cog(EmojiManager(bot))

