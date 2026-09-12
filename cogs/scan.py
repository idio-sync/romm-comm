import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from admin_checks import is_admin

logger = logging.getLogger(__name__)

class ScanType(str, Enum):
    """Enum for scan types to prevent typos and provide better code completion

    Values must match RomM's ScanType enum, which the scan socket resolves with
    ScanType[options["type"].upper()] - an unknown value fails the scan.
    """
    QUICK = "quick"
    COMPLETE = "complete"
    NEW_PLATFORMS = "new_platforms"
    PARTIAL = "update"
    UNIDENTIFIED = "unmatched"
    HASHES = "hashes"

class ScanCommands(str, Enum):
    """Enum for scan command autocomplete"""
    PLATFORM = "platform"
    FULL = "full"
    STOP = "stop"
    STATUS = "status"
    DETECT = "detect"
    UNIDENTIFIED = "unidentified"
    HASHES = "hashes"
    NEW = "new"
    PARTIAL = "partial"
    SUMMARY = "summary"

class Scan(commands.Cog):
    """A cog for handling ROM scanning operations"""

    def __init__(self, bot):
        self.bot = bot
        self.sio = bot.socketio_manager.sio
        self.config = bot.config
        self.scan_start_time: Optional[datetime] = None
        self.last_channel: Optional[discord.TextChannel] = None
        self.scan_progress: Dict[str, Any] = {}
        self.is_scanning: bool = False
        self.last_scan_stats: Dict[str, Any] = {}
        self.new_games: List[Dict[str, str]] = []
        self.external_scan: bool = False  # Track if scan was initiated externally
        
        # First event detection flags
        self._first_event_received: bool = False
        self._scan_initiated_externally: bool = False
        
        logging.getLogger('socketio').setLevel(logging.WARNING)
        logging.getLogger('engineio').setLevel(logging.WARNING)

    @commands.Cog.listener('on_romm_connect')
    async def on_romm_connect(self):
        logger.info("Connected to websocket server")
        # Reset detection flags on connect
        self._first_event_received = False
        self._scan_initiated_externally = False

    @commands.Cog.listener('on_romm_connect_error')
    async def on_romm_connect_error(self, error):
        logger.error(f"Failed to connect to websocket: {error}")
        await self._handle_connection_error(error)

    @commands.Cog.listener('on_romm_disconnect')
    async def on_romm_disconnect(self):
        logger.warning("Disconnected from websocket server")

        # Mark scan as interrupted if we were tracking one
        if self.is_scanning:
            logger.warning("Disconnected during active scan")
            await self._clear_shared_scan_state()

        self.is_scanning = False
        if self.last_channel and not self.external_scan:
            try:
                await self.last_channel.send("📡 Disconnected from scan service")
            except Exception as e:
                logger.error(f"Failed to send disconnect message: {e}")

    @commands.Cog.listener('on_romm_scan_platform')
    async def on_romm_scan_platform(self, data):
        try:
            # First event detection - if this is the first event and we didn't initiate scan
            # Use lock to prevent race condition when checking and setting is_scanning
            async with self.bot.scan_state_lock:
                if not self._first_event_received and not self.is_scanning:
                    logger.info("🔍 External scan detected via platform event")
                    self._scan_initiated_externally = True
                    self.scan_start_time = datetime.now()
                    self.is_scanning = True
                    self.external_scan = True

                    # Update bot-level shared state (already holding lock)
                    self.bot.scan_state.update({
                        'is_scanning': True,
                        'scan_start_time': self.scan_start_time,
                        'initiated_by': 'romm',
                        'scan_type': 'platform',
                        'channel_id': None
                    })

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

                self._first_event_received = True

            # Handle platform data
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

    @commands.Cog.listener('on_romm_scan_rom')
    async def on_romm_scan_rom(self, data):
        try:
            # First event detection - same logic as platform
            # Use lock to prevent race condition when checking and setting is_scanning
            async with self.bot.scan_state_lock:
                if not self._first_event_received and not self.is_scanning:
                    logger.info("🔍 External scan detected via ROM event")
                    self._scan_initiated_externally = True
                    self.scan_start_time = datetime.now()
                    self.is_scanning = True
                    self.external_scan = True

                    # Update bot-level shared state (already holding lock)
                    self.bot.scan_state.update({
                        'is_scanning': True,
                        'scan_start_time': self.scan_start_time,
                        'initiated_by': 'romm',
                        'scan_type': 'rom',
                        'channel_id': None
                    })

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

                self._first_event_received = True

            # Handle ROM data
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
                        'platform': current_platform,
                        'name': rom_name,
                        'file_name': data.get('file_name', '')
                    })
                if data.get('has_metadata', False):
                    self.scan_progress['metadata_roms'] = self.scan_progress.get('metadata_roms', 0) + 1
        except Exception as e:
            logger.error(f"Error handling ROM scan update: {e}")

    @commands.Cog.listener('on_romm_scan_done')
    async def on_romm_scan_done(self, stats):
        try:
            logger.info(f"📊 Scan complete event received with stats: {stats}")

            # Reset detection flags
            self._first_event_received = False
            self._scan_initiated_externally = False

            # Update bot-level shared state
            async with self.bot.scan_state_lock:
                self.bot.scan_state.update({
                    'is_scanning': False,
                    'scan_start_time': None,
                    'initiated_by': None,
                    'scan_type': None,
                    'channel_id': None
                })

            self.is_scanning = False

            # Handle externally initiated scans gracefully
            if self.scan_start_time is None:
                logger.info("Scan completion received for externally initiated scan with no tracked start time")

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

            # Reset scan state
            self._reset_scan_state()

        except Exception as e:
            logger.error(f"Error handling scan completion: {e}", exc_info=True)
            self._reset_scan_state()

    @commands.Cog.listener('on_romm_scan_error')
    async def on_romm_scan_error(self, error_message):
        """Handle scan errors"""
        logger.error(f"❌ Scan failed: {error_message}")

        # Reset detection flags
        self._first_event_received = False
        self._scan_initiated_externally = False

        # Update bot-level state
        async with self.bot.scan_state_lock:
            self.bot.scan_state.update({
                'is_scanning': False,
                'scan_start_time': None,
                'initiated_by': None,
                'scan_type': None,
                'channel_id': None
            })

        # Notify channel if Discord-initiated
        if self.last_channel and not self.external_scan:
            try:
                await self.last_channel.send(f"❌ Scan failed: {error_message}")
            except Exception as e:
                logger.error(f"Failed to send error message: {e}")

        self._reset_scan_state()

    def _reset_scan_state(self):
        """Reset all scan-related state variables"""
        self.scan_start_time = None
        self.scan_progress = {}
        self.is_scanning = False
        self.new_games = []  # Reset new games list
        self.external_scan = False  # Reset external scan flag
        self._first_event_received = False
        self._scan_initiated_externally = False

    async def _clear_shared_scan_state(self):
        """Reset shared bot-level scan state."""
        async with self.bot.scan_state_lock:
            self.bot.scan_state.update({
                'is_scanning': False,
                'scan_start_time': None,
                'initiated_by': None,
                'scan_type': None,
                'channel_id': None
            })

    async def _start_discord_scan(
        self,
        ctx: discord.ApplicationContext,
        *,
        scan_type: str,
        options: Dict[str, Any],
        progress: Dict[str, Any],
        response_message: str
    ) -> bool:
        """Connect, emit a scan command, then mark scan state after the emit succeeds."""
        if self.is_scanning:
            await ctx.respond(
                "❌ A scan is already in progress. Use `/scan status` to check progress "
                "or `/scan stop` to stop it."
            )
            return False
        connected = await self.bot.socketio_manager.connect()
        if not connected:
            await self._clear_shared_scan_state()
            self._reset_scan_state()
            await ctx.respond("❌ Unable to connect to scan service. Please try again later.")
            return False

        try:
            await self.sio.emit('scan', options)
        except Exception as e:
            logger.error(f"Failed to start {scan_type} scan: {e}", exc_info=True)
            await self._clear_shared_scan_state()
            self._reset_scan_state()
            await ctx.respond("❌ Failed to start scan: could not send request to RomM.")
            return False

        started_at = datetime.now()
        async with self.bot.scan_state_lock:
            self.bot.scan_state.update({
                'is_scanning': True,
                'scan_start_time': started_at,
                'initiated_by': 'discord',
                'scan_type': scan_type,
                'channel_id': ctx.channel.id
            })

        self.last_channel = ctx.channel
        self.scan_start_time = started_at
        self.is_scanning = True
        self.external_scan = False
        self.scan_progress = progress
        self.new_games = []

        await ctx.respond(response_message)
        return True

    async def _handle_connection_error(self, error: str):
        """Handle connection errors and notify the user"""
        if self.last_channel and not self.external_scan:
            try:
                await self.last_channel.send(f"❌ Lost connection to scan service: {error}")
            except Exception as e:
                logger.error(f"Failed to send connection error message: {e}")
        await self._clear_shared_scan_state()
        self._reset_scan_state()

    async def scan_command_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete for scan subcommands"""
        commands = {
            "platform": "Scan a specific platform",
            "full": "Perform a full system scan",
            "stop": "Stop the current scan",
            "status": "Check current scan status",
            "unidentified": "Scan unidentified ROMs",
            "hashes": "Update ROM hashes",
            "new": "Scan new platforms only",
            "partial": "Scan ROMs with partial metadata",
            "summary": "View last scan summary"
        }
        
        user_input = ctx.value.lower() if ctx.value else ""
        return [
            cmd for cmd in commands
            if user_input in cmd.lower() or user_input in commands[cmd].lower()
        ]

    async def platform_name_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete for platform names"""
        search_cog = self.bot.get_cog('Search')
        if not search_cog:
            return []
        
        return await search_cog.platform_autocomplete(ctx)

    @commands.slash_command(
        name="scan",
        description="Scan ROMs and fetch metadata (admin only)"
    )
    @is_admin()
    async def scan(
        self,
        ctx: discord.ApplicationContext,
        command: discord.Option(
            str,
            description="Scan command to execute",
            autocomplete=scan_command_autocomplete,
            required=True
        ),
        platform: discord.Option(
            str,
            description="Platform name (only for platform scan)",
            autocomplete=platform_name_autocomplete,
            required=False,
            default=None
        )
    ):
        """Scan ROMs and fetch metadata from various sources"""
        await ctx.defer()
        command = command.lower()
        
        if command == "platform":
            if not platform:
                await ctx.respond("❌ Platform name is required for platform scan")
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
        elif command == "new":
            await self._scan_new_platforms(ctx)
        elif command == "partial":
            await self._scan_partial(ctx)
        elif command == "summary":
            await self._scan_summary(ctx)
        else:
            await ctx.respond(f"❌ Unknown scan command: {command}")

    async def _scan_platform(self, ctx: discord.ApplicationContext, platform: str):
        """Handle platform-specific scan"""
        try:
            # Use raw platform data to access custom_name field
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')

            if not raw_platforms:
                await ctx.respond("❌ Error: Platform data not available")
                return

            # Find platform by name (including custom names)
            platform_id, platform_display_name = await self.bot.find_platform_by_name(platform, raw_platforms)

            if not platform_id:
                # Show available platforms with display names
                platforms_list = "\n".join(
                    f"• {self.bot.get_platform_display_name(p)}"
                    for p in sorted(raw_platforms, key=lambda x: self.bot.get_platform_display_name(x))
                )
                await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            progress = {
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
            
            await self._start_discord_scan(
                ctx,
                scan_type='platform',
                options=options,
                progress=progress,
                response_message=f"🔍 Started scanning platform: {platform_display_name}"
            )
            
        except Exception as e:
            await self._clear_shared_scan_state()
            self._reset_scan_state()
            raise

    async def _scan_full(self, ctx: discord.ApplicationContext):
        """Handle full system scan"""
        progress = {
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
        
        await self._start_discord_scan(
            ctx,
            scan_type='complete',
            options=options,
            progress=progress,
            response_message="🔍 Started full system scan"
        )

    async def _scan_stop(self, ctx: discord.ApplicationContext):
        """Handle scan stop command"""
        if not self.is_scanning:
            await ctx.respond("❌ No scan is currently running")
            return

        connected = await self.bot.socketio_manager.connect()
        if not connected:
            await ctx.respond("❌ Unable to connect to scan service. Stop request was not sent.")
            return

        try:
            await self.sio.emit("scan:stop")
        except Exception as e:
            logger.error(f"Failed to send scan stop request: {e}", exc_info=True)
            await ctx.respond("❌ Failed to send stop request to RomM.")
            return
        
        # Note if this was an external scan
        if self.external_scan:
            await ctx.respond("🛑 Stop request sent for externally initiated scan")
        else:
            await ctx.respond("🛑 Scan stop request has been sent")

        self._reset_scan_state()
        # Also clear the shared bot.scan_state so /scan status doesn't report a
        # phantom "in progress" scan after a stop (local reset alone left it set).
        await self._clear_shared_scan_state()

    async def _scan_status(self, ctx: discord.ApplicationContext):
        """Detect if a scan is running and show its status"""
        
        # Check shared bot state
        async with self.bot.scan_state_lock:
            state = self.bot.scan_state.copy()
        
        embed = discord.Embed(
            title="🔍 Scan Detection",
            color=discord.Color.blue() if state['is_scanning'] else discord.Color.green()
        )
        
        if not state['is_scanning'] and not self.is_scanning:
            embed.add_field(
                name="Status",
                value="✅ No scan currently running",
                inline=False
            )
        else:
            initiated_by = state.get('initiated_by', 'unknown')
            scan_type = state.get('scan_type', 'unknown')
            start_time = state.get('scan_start_time')
            
            if start_time:
                duration = datetime.now() - start_time
                duration_str = str(duration).split('.')[0]
            else:
                duration_str = "Unknown"
            
            embed.add_field(
                name="Status",
                value="🟢 Scan in Progress",
                inline=False
            )
            
            embed.add_field(
                name="Initiated By",
                value=f"{'🤖 Discord Bot' if initiated_by == 'discord' else '🌐 RomM WebUI'}",
                inline=True
            )
            
            embed.add_field(
                name="Scan Type",
                value=scan_type.title() if scan_type else "Unknown",
                inline=True
            )
            
            embed.add_field(
                name="Duration",
                value=duration_str,
                inline=True
            )
            
            if self.scan_progress:
                current_platform = self.scan_progress.get('current_platform')
                scanned_roms = self.scan_progress.get('scanned_roms', 0)
                added_roms = self.scan_progress.get('added_roms', 0)
                scanned_platforms = self.scan_progress.get('scanned_platforms', 0)
                
                if current_platform:
                    embed.add_field(
                        name="Current Platform",
                        value=current_platform,
                        inline=False
                    )
                
                progress_text = f"**Platforms:** {scanned_platforms} scanned\n"
                progress_text += f"**ROMs:** {scanned_roms} scanned, {added_roms} new"
                
                embed.add_field(
                    name="Progress",
                    value=progress_text,
                    inline=False
                )
        
        await ctx.respond(embed=embed)

    async def _scan_unidentified(self, ctx: discord.ApplicationContext):
        """Handle unidentified ROMs scan"""
        progress = {
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
        
        await self._start_discord_scan(
            ctx,
            scan_type='unidentified',
            options=options,
            progress=progress,
            response_message="🔍 Started scanning unidentified ROMs"
        )

    async def _scan_hashes(self, ctx: discord.ApplicationContext):
        """Handle ROM hash update scan"""
        options = {
            "platforms": [],
            "type": ScanType.HASHES.value,
            "roms_ids": [],
            "apis": []
        }
        
        await self._start_discord_scan(
            ctx,
            scan_type='hashes',
            options=options,
            progress={},
            response_message="🔍 Started updating ROM hashes"
        )

    async def _scan_new_platforms(self, ctx: discord.ApplicationContext):
        """Handle new platforms scan"""
        options = {
            "platforms": [],
            "type": ScanType.NEW_PLATFORMS.value,
            "roms_ids": [],
            "apis": ["igdb", "moby"]
        }
        
        await self._start_discord_scan(
            ctx,
            scan_type='new_platforms',
            options=options,
            progress={},
            response_message="🔍 Started scanning for new platforms"
        )

    async def _scan_partial(self, ctx: discord.ApplicationContext):
        """Handle partial metadata scan"""
        options = {
            "platforms": [],
            "type": ScanType.PARTIAL.value,
            "roms_ids": [],
            "apis": ["igdb", "moby"]
        }
        
        await self._start_discord_scan(
            ctx,
            scan_type='partial',
            options=options,
            progress={},
            response_message="🔍 Started scanning ROMs with partial metadata"
        )

    async def _scan_summary(self, ctx: discord.ApplicationContext):
        """Handle scan summary request"""
        if not self.last_scan_stats:
            await ctx.respond("❌ No scan data available. Run a scan first!")
            return
            
        stats = self.last_scan_stats
        
        summary = [
            "📊 **Last Scan Summary**",
            f"Duration ⏱️: {stats.get('duration', 'Unknown')}",
            "",
            "**Platforms  🎮:**",
            f"- Platforms Scanned: {stats.get('scanned_platforms', 0)}",
            f"- New Platforms Added: {stats.get('added_platforms', 0)}",
            "",
            "**ROMs  💾:**",
            f"- Total ROMs Found: {stats.get('total_roms_found', 0)}",
            f"- ROMs Scanned: {stats.get('scanned_roms', 0)}",
            f"- New ROMs Added: {stats.get('added_roms', 0)}",
            f"- ROMs with Metadata: {stats.get('metadata_roms', 0)}",
            "",
            f"**Firmware  {self.bot.emoji_dict.get('bios', '🔧')}:**",
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
