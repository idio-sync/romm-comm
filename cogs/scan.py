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
        self._connection_lock = asyncio.Lock()
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

        @self.sio.on('done')
        async def on_scan_complete(stats):
            try:
                if self.scan_start_time is None:
                    logger.error("Scan completion received but start time was not set")
                    return

                duration = datetime.now() - self.scan_start_time
                duration_str = str(duration).split('.')[0]
        
                # Get scan stats
                added_platforms = stats.get('added_platforms', 0)
                added_roms = stats.get('added_roms', 0)
                scanned_roms = stats.get('scanned_roms', 0)
                # total_data_added = stats.get('total_data_added', 0)  # Assuming data in bytes

                # Format total data added in a human-readable way
                #if total_data_added >= 1_000_000_000_000:
                    #data_size = f"{total_data_added / 1_000_000_000_000:.2f} TB"
                #elif total_data_added >= 1_000_000_000:
                    #data_size = f"{total_data_added / 1_000_000_000:.2f} GB"
                #elif total_data_added >= 1_000_000:
                    #data_size = f"{total_data_added / 1_000_000:.2f} MB"
                #elif total_data_added >= 1_000:
                    #data_size = f"{total_data_added / 1_000:.2f} KB"
                #else:
                    #data_size = f"{total_data_added} bytes"
        
                message = (
                    f"✅ Scan completed in {duration_str}\n"
                    f"📊 Stats:\n"
                    f"- Added Platforms: {added_platforms}\n"
                    f"- Added ROMs: {added_roms}\n"
                    f"- Total ROMs Scanned: {scanned_roms}\n"
                    #f"- Total Data Added: {data_size}"
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
        
        @self.sio.on('scan:scanning_platform')
        async def on_scanning_platform(platform_name):
            """Update the user about the progress of the platform scan."""
            try:
                if self.last_channel:
                await self.last_channel.send(f"🔍 Scanning platform: {platform_name}")
            except Exception as e:
                logger.error(f"Error sending scan update: {e}")

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
            
            await self.sio.emit('scan', json.dumps([platform_id]), {'complete_rescan': False})
            
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
        
            # Save the last used channel and scan start time for completion tracking
            self.last_channel = ctx.channel
            self.scan_start_time = datetime.now()

            # Emit the full scan event with complete_rescan set to True
            await self.sio.emit('scan', json.dumps([]), {'complete_rescan': True})

            # Notify user that full scan has started
            await ctx.respond("🔍 Started full system scan. Default maximum scan length is four hours.")
        
        except Exception as e:
            logger.error(f"Error in fullscan command: {e}")
            await ctx.respond("❌ Error: Failed to start full scan. Please try again later.")

    @discord.slash_command(name="stopscan", description="Stop the current scan process.")
    async def stopscan(self, ctx: discord.ApplicationContext):
        await ctx.defer()
    
        try:
            # Emit the `scan:stop` event to halt any ongoing scan
            await self.sio.emit("scan:stop")
        
            await ctx.respond("🛑 Scan stop request has been sent.")
        except Exception as e:
            logger.error(f"Error in stopscan command: {e}")
            await ctx.respond("❌ Error: Failed to send stop scan request.")
    
    @discord.slash_command(name="scanstatus", description="Get the current scan status.")
        async def scanstatus(self, ctx: discord.ApplicationContext):
            await ctx.defer()

            try:
                if self.scan_start_time is None:
                    await ctx.respond("❌ No scan is currently running.")
                    return

                # Calculate scan duration
                duration = datetime.now() - self.scan_start_time
                duration_str = str(duration).split('.')[0]
        
                # Get scan stats
                added_roms = self.scan_progress.get('added_roms', 0)
                # total_data_added = self.scan_progress.get('total_data_added', 0)  # Total data in bytes

                # Format total data added
                # if total_data_added >= 1_000_000_000_000:
                    # data_size = f"{total_data_added / 1_000_000_000_000:.2f} TB"
                # elif total_data_added >= 1_000_000_000:
                    # data_size = f"{total_data_added / 1_000_000_000:.2f} GB"
                # elif total_data_added >= 1_000_000:
                    # data_size = f"{total_data_added / 1_000_000:.2f} MB"
                # elif total_data_added >= 1_000:
                    # data_size = f"{total_data_added / 1_000:.2f} KB"
                # else:
                    # data_size = f"{total_data_added} bytes"
        
                message = (
                    f"⏱️ Scan Duration: {duration_str}\n"
                    f"📊 Current Scan Status:\n"
                    f"👾 ROMs Added So Far: {added_roms}\n"
                    # f"- Total Data Added: {data_size}"
                )

        await ctx.respond(message)
    except Exception as e:
        logger.error(f"Error fetching scan status: {e}")
        await ctx.respond("❌ Error: Failed to fetch scan status.")

    def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.sio.connected:
            asyncio.create_task(self.sio.disconnect())

def setup(bot):
    bot.add_cog(Scan(bot))
