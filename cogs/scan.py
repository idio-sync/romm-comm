import discord
from discord.ext import commands
import socketio
import asyncio
import os
from datetime import datetime
import logging
import base64
from typing import Optional

logger = logging.getLogger(__name__)

class Scan(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.sio = socketio.AsyncClient()
        self.config = bot.config
        self.scan_start_time: Optional[datetime] = None
        self.last_channel: Optional[discord.TextChannel] = None
        self._connection_lock = asyncio.Lock()  # Add lock for connection management
        self.setup_socket_handlers()

    def setup_socket_handlers(self):
        @self.sio.on('connect')
        async def on_connect():
            logger.info("Successfully connected to websocket")

        @self.sio.on('connect_error')
        async def on_connect_error(error):
            logger.error(f"Failed to connect to websocket: {error}")
            if self.last_channel:
                try:
                    await self.last_channel.send("❌ Lost connection to scan service. Please try again.")
                except Exception as e:
                    logger.error(f"Failed to send connection error message: {e}")

        @self.sio.on('disconnect')
        async def on_disconnect():
            logger.warning("Disconnected from websocket")

        @self.sio.on('scan:done')
        async def on_scan_complete(stats):
            try:
                if self.scan_start_time is None:
                    logger.error("Scan completion received but start time was not set")
                    return

                duration = datetime.now() - self.scan_start_time
                duration_str = str(duration).split('.')[0]
                
                message = (
                    f"✅ Scan completed in {duration_str}\n"
                    f"📊 Stats:\n"
                    f"- Added Platforms: {stats.get('added_platforms', 0)}\n"
                    f"- Added ROMs: {stats.get('added_roms', 0)}\n"
                    f"- Total ROMs Scanned: {stats.get('scanned_roms', 0)}"
                )
                
                if self.last_channel:
                    await self.last_channel.send(message)
            except Exception as e:
                logger.error(f"Error handling scan completion: {e}")

        @self.sio.on('scan:done_ko')
        async def on_scan_error(error_message):
            try:
                if self.last_channel:
                    await self.last_channel.send(f"❌ Scan failed: {error_message}")
            except Exception as e:
                logger.error(f"Error handling scan error: {e}")

    async def ensure_connected(self):
        async with self._connection_lock:  # Use lock to prevent multiple simultaneous connection attempts
            if not self.sio.connected:
                try:
                    # Create basic auth header
                    auth_string = f"{self.config.USER}:{self.config.PASS}"
                    auth_bytes = auth_string.encode('ascii')
                    base64_auth = base64.b64encode(auth_bytes).decode('ascii')
                    
                    # Use the API base URL from config
                    websocket_url = self.config.API_BASE_URL.replace('http://', 'ws://')
                    if websocket_url.startswith('https://'):
                        websocket_url = websocket_url.replace('https://', 'wss://')
                    
                    await self.sio.connect(
                        websocket_url,
                        headers={
                            'Authorization': f'Basic {base64_auth}',
                            'User-Agent': 'RommBot/1.0'
                        },
                        wait_timeout=10,
                        transports=['websocket']  # Force websocket transport
                    )
                    logger.info(f"Connected to websocket at {websocket_url}")
                except Exception as e:
                    logger.error(f"Failed to connect to backend: {e}")
                    raise Exception(f"Failed to connect to scan service. Please try again later.")

    async def platform_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete function for platform names."""
        try:
            platforms_data = self.bot.cache.get('platforms')
            
            if platforms_data:
                platform_names = [p.get('name', '') for p in platforms_data if p.get('name')]
                user_input = ctx.value.lower()
                return [name for name in platform_names if user_input in name.lower()][:25]
            else:
                logger.warning("No platforms data found in cache")
                return []
        except Exception as e:
            logger.error(f"Error in platform autocomplete: {e}")
            return []

    @commands.cooldown(1, 300, commands.BucketType.guild)
    @discord.slash_command(name="scan", description="Scan a specific platform")
    async def scan(
        self, 
        ctx: discord.ApplicationContext,
        platform: discord.Option(
            str, 
            "Platform to scan", 
            required=True,
            autocomplete=platform_autocomplete
        )
    ):
        await ctx.defer()
        
        try:
            platforms_data = self.bot.cache.get('platforms')
            
            if not platforms_data:
                await ctx.respond("❌ Error: Platform data not available. Please try again later.")
                return
            
            # Find platform ID (case-insensitive)
            platform_id = None
            platform_name = None
            for p in platforms_data:
                if p.get('name', '').lower() == platform.lower():
                    platform_id = p.get('id')
                    platform_name = p.get('name')
                    break
            
            if not platform_id:
                await ctx.respond(f"❌ Error: Platform '{platform}' not found")
                return

            # Connect to websocket before updating channel and time
            await self.ensure_connected()
            
            self.last_channel = ctx.channel
            self.scan_start_time = datetime.now()
            
            await self.sio.emit('scan', {
                'platforms': [platform_id],
                'type': 'quick',
                'roms_ids': [],
                'apis': ['igdb', 'moby']
            })
            
            await ctx.respond(f"🔍 Started scanning platform: {platform_name}")
            
        except Exception as e:
            logger.error(f"Error in scan command: {e}")
            await ctx.respond("❌ Error: Failed to start scan. Please try again later.")

    @commands.cooldown(1, 1800, commands.BucketType.guild)
    @discord.slash_command(name="fullscan", description="Perform a full system scan")
    async def fullscan(self, ctx: discord.ApplicationContext):
        await ctx.defer()
        
        try:
            # Connect to websocket before updating channel and time
            await self.ensure_connected()
            
            self.last_channel = ctx.channel
            self.scan_start_time = datetime.now()
            
            await self.sio.emit('scan', {
                'platforms': [],
                'type': 'complete',
                'roms_ids': [],
                'apis': ['igdb', 'moby']
            })
            
            await ctx.respond("🔍 Started full system scan. Default maximum scan length is four hours.")
            
        except Exception as e:
            logger.error(f"Error in fullscan command: {e}")
            await ctx.respond("❌ Error: Failed to start full scan. Please try again later.")

    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.sio.connected:
            asyncio.create_task(self.sio.disconnect())

def setup(bot):
    bot.add_cog(Scan(bot))
