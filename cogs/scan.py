import discord
from discord.ext import commands
import socketio
import asyncio
from datetime import datetime
import logging
import base64
import json
from typing import Optional, Dict, Any, List
from enum import Enum

logger = logging.getLogger(__name__)
logger.setLevel(logging.WARNING)

def is_admin():
    """Check if the user is the admin"""
    async def predicate(ctx: discord.ApplicationContext):
        return ctx.bot.is_admin(ctx.author)
    return commands.check(predicate)

class ScanType(str, Enum):
    """Enum for scan types to prevent typos and provide better code completion"""
    QUICK = "quick"
    COMPLETE = "complete"
    NEW_PLATFORMS = "new_platforms"
    PARTIAL = "partial"
    UNIDENTIFIED = "unidentified"
    HASHES = "hashes"

class ScanCommands(str, Enum):
    """Enum for scan command autocomplete"""
    PLATFORM = "platform"
    FULL = "full"
    STOP = "stop"
    STATUS = "status"
    UNIDENTIFIED = "unidentified"
    HASHES = "hashes"
    NEW = "new"
    PARTIAL = "partial"
    SUMMARY = "summary"

class Scan(commands.Cog):
    """A cog for handling ROM scanning operations"""

    def __init__(self, bot):
        self.bot = bot
        self.sio = socketio.AsyncClient(
            logger=False,
            engineio_logger=False,
            reconnection=True,
            reconnection_attempts=3
        )
        self.config = bot.config
        self.scan_start_time: Optional[datetime] = None
        self.last_channel: Optional[discord.TextChannel] = None
        self._connection_lock = asyncio.Lock()
        self.scan_progress: Dict[str, Any] = {}
        self.is_scanning: bool = False
        self.last_scan_stats: Dict[str, Any] = {}
        self.new_games: List[Dict[str, str]] = []
        self.external_scan: bool = False  # Track if scan was initiated externally
        self.setup_socket_handlers()

        logging.getLogger('socketio').setLevel(logging.WARNING)
        logging.getLogger('engineio').setLevel(logging.WARNING)

    async def cog_before_invoke(self, ctx: discord.ApplicationContext) -> bool:
        """Checks that should run before any command in this cog."""
        # Get the subcommand from the options
        current_command = ctx.interaction.data.get('options', [{}])[0].get('value', '').lower()
        
        # Allow status and stop commands even during scanning
        if self.is_scanning and current_command not in ['status', 'stop']:
            await ctx.respond("⌠A scan is already in progress. Use `/scan status` to check progress or `/scan stop` to stop it.")
            return False
        return True

    def setup_socket_handlers(self):
        """Set up Socket.IO event handlers"""
        @self.sio.event
        async def connect():
            logger.info("Connected to websocket server")

        @self.sio.event
        async def connect_error(error):
            logger.error(f"Failed to connect to websocket: {error}")
            await self._handle_connection_error(error)

        @self.sio.event
        async def disconnect():
            logger.warning("Disconnected from websocket server")
            self.is_scanning = False
            if self.last_channel and not self.external_scan:
                try:
                    await self.last_channel.send("📡 Disconnected from scan service")
                except Exception as e:
                    logger.error(f"Failed to send disconnect message: {e}")

        @self.sio.on('scan:scanning_platform')
        async def on_scanning_platform(data):
            try:
                # If this is the first scan event and we don't have a start time, it's an external scan
                if not self.scan_start_time and not self.is_scanning:
                    logger.info("Detected externally initiated scan")
                    self.scan_start_time = datetime.now()
                    self.is_scanning = True
                    self.external_scan = True
                    self.scan_progress = {
                        'current_platform': None,
                        'current_platform_slug': None,
                        'current_rom': None,
                        'platform_roms': 0,
                        'total_roms': 0,
                        'scanned_roms': 0,
                        'scanned_platforms': 0,
                        'added_platforms': 0,
                        'added_roms': 0,
                        'metadata_roms': 0
                    }
                
                if isinstance(data, dict):
                    platform_name = data.get('name', 'Unknown Platform')
                    platform_slug = data.get('slug', 'unknown')
                else:
                    platform_name = str(data)
                    platform_slug = 'unknown'
                
                self.scan_progress['current_platform'] = platform_name
                self.scan_progress['current_platform_slug'] = platform_slug
                self.scan_progress['platform_roms'] = 0  # Reset ROM count for new platform
                self.scan_progress['scanned_platforms'] = self.scan_progress.get('scanned_platforms', 0) + 1
                
                # Only send messages if we have a channel (Discord-initiated scan)
                if self.last_channel and not self.external_scan:
                    await self.last_channel.send(f"🔍 Scanning platform: {platform_name}")
                    
            except Exception as e:
                logger.error(f"Error handling platform scan update: {e}")

        @self.sio.on('scan:scanning_rom')
        async def on_scanning_rom(data):
            try:
                # If this is the first scan event and we don't have a start time, it's an external scan
                if not self.scan_start_time and not self.is_scanning:
                    logger.info("Detected externally initiated scan via ROM event")
                    self.scan_start_time = datetime.now()
                    self.is_scanning = True
                    self.external_scan = True
                    self.scan_progress = {
                        'current_platform': 'Unknown',
                        'current_platform_slug': None,
                        'current_rom': None,
                        'platform_roms': 0,
                        'total_roms': 0,
                        'scanned_roms': 0,
                        'scanned_platforms': 0,
                        'added_platforms': 0,
                        'added_roms': 0,
                        'metadata_roms': 0
                    }
                
                if isinstance(data, dict):
                    rom_name = data.get('name', 'Unknown ROM')
                    self.scan_progress['current_rom'] = rom_name
                    self.scan_progress['platform_roms'] = self.scan_progress.get('platform_roms', 0) + 1
                    self.scan_progress['total_roms'] = self.scan_progress.get('total_roms', 0) + 1
                    self.scan_progress['scanned_roms'] = self.scan_progress.get('scanned_roms', 0) + 1
                    
                    if data.get('is_new', False):
                        self.scan_progress['added_roms'] = self.scan_progress.get('added_roms', 0) + 1
                        # Use the display name stored in scan_progress for consistency
                        current_platform = self.scan_progress.get('current_platform', 'Unknown')
                        self.new_games.append({
                            'platform': current_platform,  # This will now be the display name
                            'name': rom_name,
                            'file_name': data.get('file_name', '')
                        })
                    if data.get('has_metadata', False):
                        self.scan_progress['metadata_roms'] = self.scan_progress.get('metadata_roms', 0) + 1
            except Exception as e:
                logger.error(f"Error handling ROM scan update: {e}")
                
        @self.sio.on('scan:done')
        async def on_scan_complete(stats):
            try:
                self.is_scanning = False
                
                # Handle externally initiated scans gracefully
                if self.scan_start_time is None:
                    logger.info("Scan completion received for externally initiated scan with no tracked start time")
                    # Provide minimal stats without duration
                    if self.external_scan:
                        # Just log it, don't send Discord messages for external scans
                        logger.info("External scan completed")
                    else:
                        logger.warning("Scan completion received but no scan was tracked")
                    
                    # Store basic stats if available
                    if stats:
                        self.last_scan_stats = {
                            'duration': 'Unknown (External Scan)',
                            'scanned_platforms': stats.get('scanned_platforms', 0),
                            'added_platforms': stats.get('added_platforms', 0),
                            'scanned_roms': stats.get('scanned_roms', 0),
                            'added_roms': stats.get('added_roms', 0),
                            'metadata_roms': stats.get('metadata_roms', 0),
                            'scanned_firmware': stats.get('scanned_firmware', 0),
                            'added_firmware': stats.get('added_firmware', 0)
                        }
                    
                    # Reset and return early
                    self._reset_scan_state()
                    return

                # Calculate duration for tracked scans
                duration = datetime.now() - self.scan_start_time
                duration_str = str(duration).split('.')[0]

                # Combine server stats with our tracked progress
                final_stats = {
                    'duration': duration_str,
                    'scanned_platforms': stats.get('scanned_platforms', 0) or self.scan_progress.get('scanned_platforms', 0),
                    'added_platforms': stats.get('added_platforms', 0) or self.scan_progress.get('added_platforms', 0),
                    'scanned_roms': stats.get('scanned_roms', 0) or self.scan_progress.get('scanned_roms', 0),
                    'added_roms': stats.get('added_roms', 0) or self.scan_progress.get('added_roms', 0),
                    'metadata_roms': stats.get('metadata_roms', 0) or self.scan_progress.get('metadata_roms', 0),
                    'scanned_firmware': stats.get('scanned_firmware', 0),
                    'added_firmware': stats.get('added_firmware', 0)
                }

                # Store stats for summary command
                self.last_scan_stats = {
                    **final_stats,
                    'total_roms_found': self.scan_progress.get('total_roms', 0)
                }

                # Only send Discord message if this was a Discord-initiated scan
                if self.last_channel and not self.external_scan:
                    message = [
                        f"✅ Scan completed in {duration_str}",
                        "",
                        "**Stats  📊:**",
                        f"- Duration  ⏱️: {duration_str}",
                        "",
                        "**Platforms  🎮:**",
                        f"- Platforms Scanned: {final_stats['scanned_platforms']}",
                        f"- New Platforms Added: {final_stats['added_platforms']}",
                        "",
                        "**ROMs  💾:**",
                        f"- Total ROMs Scanned: {final_stats['scanned_roms']}",
                        f"- New ROMs Added: {final_stats['added_roms']}",
                    ]
                    
                    if final_stats.get('metadata_roms'):
                        message.append(f"- ROMs with Metadata: {final_stats['metadata_roms']}")
                    
                    message.extend([
                        "",
                        f"**Firmware  {self.bot.emoji_dict.get('bios', '🔧')}:**",
                        f"- Firmware Scanned: {final_stats['scanned_firmware']}",
                        f"- New Firmware Added: {final_stats['added_firmware']}"
                    ])
                    
                    await self.last_channel.send('\n'.join(message))
                else:
                    # Log completion for external scans
                    logger.info(f"External scan completed - ROMs: {final_stats['scanned_roms']}, Added: {final_stats['added_roms']}")

                # If there are new games, dispatch the batch event
                if self.new_games:
                    logger.info(f"Dispatching batch_scan_complete with {len(self.new_games)} new games")
                    self.bot.dispatch('batch_scan_complete', self.new_games)
                
                # Reset scan state
                self._reset_scan_state()
                
            except Exception as e:
                logger.error(f"Error handling scan completion: {e}", exc_info=True)
                self._reset_scan_state()

    def _reset_scan_state(self):
        """Reset all scan-related state variables"""
        self.scan_start_time = None
        self.scan_progress = {}
        self.is_scanning = False
        self.new_games = []  # Reset new games list
        self.external_scan = False  # Reset external scan flag

    async def _handle_connection_error(self, error: str):
        """Handle connection errors and notify the user"""
        if self.last_channel and not self.external_scan:
            try:
                await self.last_channel.send(f"⌠Lost connection to scan service: {error}")
            except Exception as e:
                logger.error(f"Failed to send connection error message: {e}")
        self.is_scanning = False
        self._reset_scan_state()

    async def ensure_connected(self):
        """Ensure Socket.IO connection is established"""
        async with self._connection_lock:
            if not self.sio.connected:
                try:
                    base_url = self.config.API_BASE_URL.rstrip('/')
                    
                    auth_string = f"{self.config.USER}:{self.config.PASS}"
                    auth_bytes = auth_string.encode('ascii')
                    base64_auth = base64.b64encode(auth_bytes).decode('ascii')
                    
                    logger.debug(f"Connecting to: {base_url}")
                    headers = {
                        'Authorization': f'Basic {base64_auth}',
                        'User-Agent': 'RommBot/1.0'
                    }
                    
                    await self.sio.connect(
                        base_url,
                        headers=headers,
                        wait_timeout=30,
                        transports=['websocket'],
                        socketio_path='ws/socket.io'
                    )
                    logger.info("Connected successfully")
                    
                except Exception as e:
                    logger.error(f"Connection error: {str(e)}", exc_info=True)
                    raise

    async def scan_command_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete for scan subcommands"""
        commands = {
            "platform": "Scan a specific platform",
            "full": "Perform a full system scan",
            "stop": "Stop the current scan",
            "status": "Check current scan status",
            "unidentified": "Scan unidentified ROMs",
            "hashes": "Update ROM hashes",
            "new_platforms": "Scan new platforms only",
            "partial": "Scan ROMs with partial metadata",
            "summary": "View last scan summary"
        }
        
        user_input = ctx.value.lower() if ctx.value else ""
        return [
            cmd for cmd in commands.keys()
            if user_input in cmd.lower() or user_input in commands[cmd].lower()
        ]

    async def platform_name_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete for platform names, only used after 'platform' command"""
        # Only show platform options if the command is 'platform'
        if not ctx.options.get('command') or ctx.options['command'].lower() != 'platform':
            return []
            
        search_cog = self.bot.get_cog('Search')
        if not search_cog:
            return []
            
        return await search_cog.platform_autocomplete(ctx)

    @discord.slash_command(name="scan", description="ROM scanning commands (Admin only)")
    @is_admin()
    async def scan(
        self,
        ctx: discord.ApplicationContext,
        command: discord.Option(
            str,
            "Scan command to execute",
            required=True,
            autocomplete=scan_command_autocomplete
        ),
        platform: discord.Option(
            str,
            "Platform to scan (only for 'platform' command)",
            required=False,
            autocomplete=platform_name_autocomplete,
            default=None
        )
    ):
        await ctx.defer()
        
        try:
            command = command.lower()
            
            if command == "platform":
                if not platform:
                    await ctx.respond("⌠Platform name is required for the platform scan command")
                    return
                await self._scan_platform(ctx, platform)
                
            elif command == "full":
                await self._scan_full(ctx)
                
            elif command == "stop":
                await self._scan_stop(ctx)
                
            elif command == "status":
                await self._scan_status(ctx)
                
            elif command == "unidentified":
                await self._scan_unidentified(ctx)
                
            elif command == "hashes":
                await self._scan_hashes(ctx)
                
            elif command == "new_platforms":
                await self._scan_new_platforms(ctx)
                
            elif command == "partial":
                await self._scan_partial(ctx)
                
            elif command == "summary":
                await self._scan_summary(ctx)
                
            else:
                await ctx.respond(f"⌠Unknown scan command: {command}")
                
        except Exception as e:
            logger.error(f"Error in scan command: {e}", exc_info=True)
            await ctx.respond(f"⌠Error executing scan command: {str(e)}")

    # [Rest of the methods remain the same - _scan_platform, _scan_full, etc.]
    async def _scan_platform(self, ctx: discord.ApplicationContext, platform: str):
        """Handle platform-specific scan"""
        try:
            # Use raw platform data to access custom_name field
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            
            if not raw_platforms:
                await ctx.respond("⌠Error: Platform data not available")
                return
            
            # Find platform by name (including custom names)
            platform_id = None
            platform_display_name = None
            platform_lower = platform.lower()
            
            for p in raw_platforms:
                # Check custom name first
                custom_name = p.get('custom_name')
                if custom_name and custom_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    platform_display_name = self.bot.get_platform_display_name(p)
                    break
                
                # Check regular name
                regular_name = p.get('name', '')
                if regular_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    platform_display_name = self.bot.get_platform_display_name(p)
                    break
            
            if not platform_id:
                # Show available platforms with display names
                platforms_list = "\n".join(
                    f"• {self.bot.get_platform_display_name(p)}" 
                    for p in sorted(raw_platforms, key=lambda x: self.bot.get_platform_display_name(x))
                )
                await ctx.respond(f"⌠Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            await self.ensure_connected()
            
            self.last_channel = ctx.channel
            self.scan_start_time = datetime.now()
            self.is_scanning = True
            self.external_scan = False  # This is a Discord-initiated scan
            
            # Initialize scan progress for this platform using display name
            self.scan_progress = {
                'current_platform': platform_display_name,
                'current_platform_slug': None,
                'current_rom': None,
                'platform_roms': 0,
                'total_roms': 0,
                'scanned_roms': 0,
                'added_roms': 0,
                'metadata_roms': 0
            }
            
            options = {
                "platforms": [platform_id],
                "type": ScanType.QUICK.value,
                "roms_ids": [],
                "apis": ["igdb", "moby"]
            }
            
            self.new_games = []
            await self.sio.emit('scan', options)
            await ctx.respond(f"🔍 Started scanning platform: {platform_display_name}")
            
        except Exception as e:
            self.is_scanning = False
            self.new_games = []  
            raise

    async def _scan_full(self, ctx: discord.ApplicationContext):
        """Handle full system scan"""
        await self.ensure_connected()
        
        self.last_channel = ctx.channel
        self.scan_start_time = datetime.now()
        self.is_scanning = True
        self.external_scan = False  # Discord-initiated
        self.new_games = []

        # Initialize scan progress for full scan
        self.scan_progress = {
            'current_platform': None,
            'current_platform_slug': None,
            'current_rom': None,
            'platform_roms': 0,
            'total_roms': 0,
            'scanned_roms': 0,
            'scanned_platforms': 0,
            'added_platforms': 0,
            'added_roms': 0,
            'metadata_roms': 0
        }

        options = {
            "platforms": [],
            "type": ScanType.COMPLETE.value,
            "roms_ids": [],
            "apis": ["igdb", "moby"]
        }
        
        await self.sio.emit('scan', options)
        await ctx.respond("🔍 Started full system scan")

    async def _scan_stop(self, ctx: discord.ApplicationContext):
        """Handle scan stop command"""
        if not self.is_scanning:
            await ctx.respond("⌠No scan is currently running")
            return

        await self.ensure_connected()
        await self.sio.emit("scan:stop")
        
        # Note if this was an external scan
        if self.external_scan:
            await ctx.respond("🛑 Stop request sent for externally initiated scan")
        else:
            await ctx.respond("🛑 Scan stop request has been sent")
        
        self._reset_scan_state()

    async def _scan_status(self, ctx: discord.ApplicationContext):
        """Handle scan status check"""
        if not self.is_scanning:
            await ctx.respond("⌠No scan is currently running")
            return

        try:
            # Handle external scans that don't have a start time
            if self.scan_start_time:
                duration = datetime.now() - self.scan_start_time
                duration_str = str(duration).split('.')[0]
            else:
                duration_str = "Unknown (External Scan)"

            message = [
                f"📊  **Current Scan Status:**",
                f"- Scan Duration ⏱️: {duration_str}",
                f"- Initiated: {'Externally' if self.external_scan else 'Via Discord'}"
            ]

            # Add platform information
            current_platform = self.scan_progress.get('current_platform', 'Unknown')
            message.append(f"- Current Platform: {current_platform}")

            # Add ROM counts
            platform_roms = self.scan_progress.get('platform_roms', 0)
            total_roms = self.scan_progress.get('total_roms', 0)
            scanned_roms = self.scan_progress.get('scanned_roms', 0)
            added_roms = self.scan_progress.get('added_roms', 0)
            metadata_roms = self.scan_progress.get('metadata_roms', 0)

            message.extend([
                f"- ROMs scanned in Current Platform: {platform_roms}",
                f"- Total ROMs Scanned: {scanned_roms}",
                f"- New ROMs Added: {added_roms}",
            ])

            # Add current ROM being processed
            current_rom = self.scan_progress.get('current_rom', 'Unknown')
            message.append(f"- Currently Processing: {current_rom}")

            # Add platform counts for full scans
            if self.scan_progress.get('scanned_platforms'):
                scanned_platforms = self.scan_progress.get('scanned_platforms', 0)
                added_platforms = self.scan_progress.get('added_platforms', 0)
                message.extend([
                    f"- Platforms Scanned: {scanned_platforms}",
                    f"- New Platforms Added: {added_platforms}"
                ])

            # Send the formatted message once
            await ctx.respond('\n'.join(message))
            
        except Exception as e:
            logger.error(f"Error fetching scan status: {e}", exc_info=True)
            await ctx.respond("⌠Error: Failed to fetch scan status.")

    async def _scan_unidentified(self, ctx: discord.ApplicationContext):
        """Handle unidentified ROMs scan"""
        await self.ensure_connected()
        
        self.last_channel = ctx.channel
        self.scan_start_time = datetime.now()
        self.is_scanning = True
        self.external_scan = False

        # Initialize scan progress for unidentified scan
        self.scan_progress = {
            'current_platform': None,
            'current_platform_slug': None,
            'current_rom': None,
            'platform_roms': 0,
            'total_roms': 0,
            'scanned_roms': 0,
            'unidentified_roms': 0,
            'metadata_roms': 0
        }

        options = {
            "platforms": [],
            "type": ScanType.UNIDENTIFIED.value,
            "roms_ids": [],
            "apis": ["igdb", "moby"]
        }
        
        await self.sio.emit('scan', options)
        await ctx.respond("🔍 Started scanning unidentified ROMs")

    async def _scan_hashes(self, ctx: discord.ApplicationContext):
        """Handle ROM hash update scan"""
        await self.ensure_connected()
        
        self.last_channel = ctx.channel
        self.scan_start_time = datetime.now()
        self.is_scanning = True
        self.external_scan = False

        options = {
            "platforms": [],
            "type": ScanType.HASHES.value,
            "roms_ids": [],
            "apis": []
        }
        
        await self.sio.emit('scan', options)
        await ctx.respond("🔍 Started updating ROM hashes")

    async def _scan_new_platforms(self, ctx: discord.ApplicationContext):
        """Handle new platforms scan"""
        await self.ensure_connected()
        
        self.last_channel = ctx.channel
        self.scan_start_time = datetime.now()
        self.is_scanning = True
        self.external_scan = False

        options = {
            "platforms": [],
            "type": ScanType.NEW_PLATFORMS.value,
            "roms_ids": [],
            "apis": ["igdb", "moby"]
        }
        
        await self.sio.emit('scan', options)
        await ctx.respond("🔍 Started scanning for new platforms")

    async def _scan_partial(self, ctx: discord.ApplicationContext):
        """Handle partial metadata scan"""
        await self.ensure_connected()
        
        self.last_channel = ctx.channel
        self.scan_start_time = datetime.now()
        self.is_scanning = True
        self.external_scan = False

        options = {
            "platforms": [],
            "type": ScanType.PARTIAL.value,
            "roms_ids": [],
            "apis": ["igdb", "moby"]
        }
        
        await self.sio.emit('scan', options)
        await ctx.respond("🔍 Started scanning ROMs with partial metadata")

    async def _scan_summary(self, ctx: discord.ApplicationContext):
        """Handle scan summary request"""
        if not self.last_scan_stats:
            await ctx.respond("⌠No scan data available. Run a scan first!")
            return
            
        stats = self.last_scan_stats
        
        summary = [
            "📊 **Last Scan Summary**",
            f"Duration ⏱️: {stats.get('duration', 'Unknown')}",
            "",
            "**Platforms  🎮:**",
            f"- Platforms Scanned: {stats.get('scanned_platforms', 0)}",
            f"- New Platforms Added: {stats.get('added_platforms', 0)}",
            f"- Platforms with Metadata: {stats.get('metadata_platforms', 0)}",
            "",
            "**ROMs  💾:**",
            f"- Total ROMs Found: {stats.get('total_roms_found', 0)}",
            f"- ROMs Scanned: {stats.get('scanned_roms', 0)}",
            f"- New ROMs Added: {stats.get('added_roms', 0)}",
            f"- ROMs with Metadata: {stats.get('metadata_roms', 0)}",
            "",
            F"**Firmware  {self.bot.emoji_dict.get('bios', '🔧')}:**",
            f"- Firmware Scanned: {stats.get('scanned_firmware', 0)}",
            f"- New Firmware Added: {stats.get('added_firmware', 0)}"
        ]
        
        await ctx.respond('\n'.join(summary))

    async def cog_unload(self):
        """Cleanup when cog is unloaded."""
        if self.sio.connected:
            try:
                await self.sio.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting socket: {e}")
        self.new_games = []

def setup(bot):
    bot.add_cog(Scan(bot))
