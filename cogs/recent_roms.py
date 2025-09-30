import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, timedelta
import socketio
from typing import Dict, List, Optional, Set, Union, Tuple
import json
import os
from pathlib import Path
import asyncio
from collections import defaultdict
import aiosqlite
import re
from urllib.parse import quote
import base64
from PIL import Image
from io import BytesIO
import aiohttp
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class RecentRomsMonitor(commands.Cog):
    """Monitor ROMs via WebSocket connection to RomM scan events"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        
        # Configuration
        self.recent_roms_channel_id = int(os.getenv('RECENT_ROMS_CHANNEL_ID', str(bot.config.CHANNEL_ID)))
        self.max_roms_per_post = int(os.getenv('RECENT_ROMS_MAX_PER_POST', '10'))
        self.bulk_display_threshold = int(os.getenv('RECENT_ROMS_BULK_THRESHOLD', '25'))
        self.enabled = os.getenv('RECENT_ROMS_ENABLED', 'TRUE').upper() == 'TRUE'

        # Use shared master database
        self.db = bot.db
        
        # Async locks instead of threading locks
        self.scan_lock = asyncio.Lock()
        self.processing_lock = asyncio.Lock()
        
        # Scan tracking
        self.current_scan_roms: List[Dict] = []
        self.current_scan_names: Set[str] = set()
        self.scan_completion_timer: Optional[asyncio.Task] = None
        
        # Track processed ROMs to prevent duplicates
        self.currently_processing: Set[int] = set()
        self.recently_processed: Set[int] = set()
        self.last_cleanup: datetime = datetime.utcnow()
        
        # IGDB client
        self.igdb = None
        
        # Reusable HTTP session for downloads
        self.http_session: Optional[aiohttp.ClientSession] = None
        
        # Platform data cache
        self.platform_cache: Optional[Dict] = None
        self.platform_cache_time: Optional[datetime] = None
        self.platform_cache_ttl = timedelta(minutes=30)
        
        # Use shared SocketIO manager
        self.sio = bot.socketio_manager.sio
  
        self._handlers_registered = False
        
        if self.enabled:
            bot.loop.create_task(self.setup())
            logger.debug("Recent ROMs WebSocket monitor enabled")

    def setup_socket_handlers(self):
        """Set up Socket.IO event handlers"""
        
        if self._handlers_registered:
            logger.debug("Socket handlers already registered, skipping")
            return
        
        self._handlers_registered = True
        
        @self.sio.event
        async def connect():
            """Handle connection event"""
            logger.info("✅ RecentRomsMonitor connected to websocket server")
            async with self.scan_lock:
                self.current_scan_roms.clear()
                self.current_scan_names.clear()
            async with self.processing_lock:
                self.recently_processed.clear()

        @self.sio.event
        async def disconnect():
            """Handle disconnection event"""
            logger.warning("RecentRomsMonitor disconnected from websocket server")
            
            # Cancel any pending timer
            if self.scan_completion_timer and not self.scan_completion_timer.done():
                self.scan_completion_timer.cancel()
                try:
                    await self.scan_completion_timer
                except asyncio.CancelledError:
                    pass

        @self.sio.event
        async def connect_error(data):
            """Handle connection errors"""
            logger.error(f"Socket.IO connection error: {data}")
        
        @self.sio.on('scan:scanning_rom')
        async def on_scanning_rom(data):
            """Handle new ROM being scanned"""
            try:
                # API sends 'id' and 'name', not 'rom_id' and 'rom_name'
                rom_id = data.get('id') or data.get('rom_id')
                rom_name = data.get('name') or data.get('rom_name', 'Unknown')
                
                if not rom_id:
                    logger.debug(f"Received scan:scanning_rom event without rom_id: {data.keys()}")
                    return
                
                # Skip ROMs that are still being identified (first emission)
                # Only process the second emission with complete metadata
                if data.get('is_identifying') is True:
                    logger.debug(f"ROM {rom_id} still being identified, waiting for complete data")
                    return
                
                # Check if already processed (prevents duplicates from multiple socket events)
                async with self.processing_lock:
                    if rom_id in self.currently_processing or rom_id in self.recently_processed:
                        logger.debug(f"ROM {rom_id} already in processing queue, skipping")
                        return
                
                # Early database check to prevent duplicate notifications
                # This is intentional: duplicate socket events or multiple ROM versions
                # should only generate ONE notification per game
                if await self.has_been_posted(rom_id):
                    logger.debug(f"ROM {rom_id} already posted to database, skipping")
                    return
                
                async with self.scan_lock:
                    # Check for duplicates in current batch
                    if rom_name.lower() in self.current_scan_names:
                        logger.debug(f"Duplicate ROM name in batch: {rom_name}")
                        return
                    
                    # Add to batch - handle both field name formats
                    rom = {
                        'id': rom_id,
                        'name': rom_name,
                        'platform_name': data.get('platform_name', 'Unknown'),
                        'platform_id': data.get('platform_id'),
                        'file_name': data.get('file_name'),
                        'fs_name': data.get('fs_name') or rom_name,
                        'fs_size_bytes': data.get('fs_size_bytes'),
                        'url_cover': data.get('url_cover'),
                        'created_at': data.get('created_at'),
                    }
                    
                    self.current_scan_names.add(rom_name.lower())
                    self.current_scan_roms.append(rom)
                    
                    # Mark as currently processing
                    async with self.processing_lock:
                        self.currently_processing.add(rom_id)
                    
                    logger.debug(f"Queued ROM: {rom['name']} ({len(self.current_scan_roms)} in batch)")
                    
                    # Reset or start the inactivity timer
                    if self.scan_completion_timer and not self.scan_completion_timer.done():
                        self.scan_completion_timer.cancel()
                        try:
                            await self.scan_completion_timer
                        except asyncio.CancelledError:
                            pass
                    
                    self.scan_completion_timer = asyncio.create_task(
                        self._trigger_batch_processing_after_delay()
                    )
                    
            except Exception as e:
                logger.error(f"Error handling scan:scanning_rom event: {e}", exc_info=True)
        
        @self.sio.on('scan:done')
        async def on_scan_done(stats):
            """Handle scan completion"""
            try:
                logger.info("Received scan:done event")
                
                # Cancel timer if running
                if self.scan_completion_timer and not self.scan_completion_timer.done():
                    self.scan_completion_timer.cancel()
                    try:
                        await self.scan_completion_timer
                    except asyncio.CancelledError:
                        pass
                
                async with self.scan_lock:
                    if self.current_scan_roms:
                        roms_to_process = self.current_scan_roms.copy()
                        self.current_scan_roms.clear()
                        self.current_scan_names.clear()
                        
                        logger.info(f"Scan complete, processing {len(roms_to_process)} ROMs")
                        asyncio.create_task(self.handle_scan_complete(roms_to_process, stats))
                    else:
                        logger.info("Scan complete with no identified ROMs to process")
                        
            except Exception as e:
                logger.error(f"Error handling scan:done event: {e}", exc_info=True)
    
    async def _trigger_batch_processing_after_delay(self):
        """Wait for inactivity then process the batch"""
        try:
            await asyncio.sleep(300)  # 5 minutes of inactivity
            
            logger.info("Scan inactivity timer expired, processing collected ROMs")
            
            async with self.scan_lock:
                if self.current_scan_roms:
                    roms_to_process = self.current_scan_roms.copy()
                    self.current_scan_roms.clear()
                    self.current_scan_names.clear()
                    
                    asyncio.create_task(self.handle_scan_complete(roms_to_process, {}))
                    
        except asyncio.CancelledError:
            logger.debug("Scan completion timer cancelled")
        except Exception as e:
            logger.error(f"Error in batch processing timer: {e}", exc_info=True)

    async def setup(self):
        """Initialize IGDB client and connect to websocket"""
        try:
            # Initialize IGDB
            await self.initialize_igdb()
            
            # Initialize HTTP session for downloads
            self.http_session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30)
            )
            
            # Setup socket handlers
            self.setup_socket_handlers()
            
            # Start cleanup task
            self.cleanup_task.start()
            
            logger.info("Recent ROMs monitor setup complete")
            
        except Exception as e:
            logger.error(f"Error setting up Recent ROMs monitor: {e}", exc_info=True)
    
    @tasks.loop(hours=1)
    async def cleanup_task(self):
        """Periodic cleanup of memory"""
        try:
            async with self.processing_lock:
                # Keep only last 1000 recently processed ROMs
                if len(self.recently_processed) > 1000:
                    # Convert to list, keep last 1000, convert back to set
                    recent_list = list(self.recently_processed)
                    self.recently_processed = set(recent_list[-1000:])
                    logger.debug(f"Cleaned up recently_processed set to {len(self.recently_processed)} items")
            
            # Clear platform cache if old
            if self.platform_cache_time:
                age = datetime.utcnow() - self.platform_cache_time
                if age > self.platform_cache_ttl:
                    self.platform_cache = None
                    self.platform_cache_time = None
                    logger.debug("Cleared platform cache")
                    
        except Exception as e:
            logger.error(f"Error in cleanup task: {e}", exc_info=True)
    
    async def handle_scan_complete(self, roms: List[Dict], stats: Dict):
        """Handle completed scan batch in main event loop"""
        try:
            if not roms:
                return
            
            # Remove duplicates based on ROM ID
            unique_roms = {rom['id']: rom for rom in roms}
            roms = list(unique_roms.values())
            
            logger.info(f"Processing scan batch of {len(roms)} unique ROMs")
            
            # Batch database check
            rom_ids = [rom['id'] for rom in roms]
            posted_ids = await self.get_posted_rom_ids(rom_ids)
            
            filtered_roms = [rom for rom in roms if rom['id'] not in posted_ids]
            
            if filtered_roms:
                logger.info(f"{len(filtered_roms)} ROMs to post (filtered {len(posted_ids)} already posted)")
                await self.process_scan_batch(filtered_roms)
            else:
                logger.info("All ROMs already posted, skipping")
            
            # Move from currently_processing to recently_processed
            async with self.processing_lock:
                for rom in roms:
                    rom_id = rom['id']
                    if rom_id in self.currently_processing:
                        self.currently_processing.remove(rom_id)
                    self.recently_processed.add(rom_id)
            
            logger.info("Triggering API data refresh after scan completion")
            await self.bot.update_api_data()
            
        except Exception as e:
            logger.error(f"Error during batch processing: {e}", exc_info=True)
            
            # Clean up processing state on error
            async with self.processing_lock:
                for rom in roms:
                    rom_id = rom['id']
                    if rom_id in self.currently_processing:
                        self.currently_processing.remove(rom_id)
    
    async def get_posted_rom_ids(self, rom_ids: List[int]) -> Set[int]:
        """Batch check if ROMs have been posted"""
        if not rom_ids:
            return set()
        
        try:
            async with self.db.get_connection() as conn:
                placeholders = ','.join('?' * len(rom_ids))
                cursor = await conn.execute(
                    f"SELECT rom_id FROM posted_roms WHERE rom_id IN ({placeholders})",
                    rom_ids
                )
                results = await cursor.fetchall()
                return {row[0] for row in results}
                
        except Exception as e:
            logger.error(f"Error checking posted ROMs: {e}")
            return set()
    
    async def has_been_posted(self, rom_id: int) -> bool:
        """Check if a single ROM has been posted"""
        result = await self.get_posted_rom_ids([rom_id])
        return rom_id in result
    
    async def get_platform_data(self) -> Optional[Dict]:
        """Get cached platform data or fetch if needed"""
        if self.platform_cache and self.platform_cache_time:
            age = datetime.utcnow() - self.platform_cache_time
            if age < self.platform_cache_ttl:
                return self.platform_cache
        
        # Fetch fresh platform data
        platforms = await self.bot.fetch_api_endpoint('platforms')
        if platforms:
            self.platform_cache = {p['id']: p for p in platforms}
            self.platform_cache_time = datetime.utcnow()
            return self.platform_cache
        
        return None
    
    async def enrich_roms_with_platform_names(self, roms: List[Dict]):
        """Enrich ROMs with platform names in batch"""
        try:
            # Find which ROMs need platform enrichment
            roms_needing_enrichment = [
                rom for rom in roms 
                if rom.get('platform_id') and not rom.get('platform_name')
            ]
            
            if not roms_needing_enrichment:
                return
            
            # Get platform data
            platform_data = await self.get_platform_data()
            if not platform_data:
                logger.warning("Could not fetch platform data for enrichment")
                return
            
            # Enrich ROMs
            for rom in roms_needing_enrichment:
                platform_id = rom.get('platform_id')
                if platform_id in platform_data:
                    platform = platform_data[platform_id]
                    custom_name = platform.get('custom_name')
                    rom['platform_name'] = (
                        custom_name.strip() if custom_name and custom_name.strip() 
                        else platform.get('name', 'Unknown')
                    )
                    
        except Exception as e:
            logger.error(f"Error enriching ROMs with platform names: {e}")
    
    async def process_scan_batch(self, roms: List[Dict]):
        """Process and post the ROMs from a completed scan"""
        if not roms:
            return
        
        channel = self.bot.get_channel(self.recent_roms_channel_id)
        if not channel:
            logger.error(f"Recent ROMs channel {self.recent_roms_channel_id} not found")
            return
        
        try:
            batch_id = datetime.utcnow().isoformat()
            is_flood = len(roms) >= self.bulk_display_threshold
            
            # Enrich ROM data with platform names
            await self.enrich_roms_with_platform_names(roms)
            
            # Mark as posted BEFORE sending to prevent duplicate notifications
            # This prevents race conditions from duplicate socket events or multiple ROM versions
            await self.mark_as_posted(roms, batch_id)
            
            # Send to Discord based on batch size
            discord_success = False
            
            if len(roms) == 1:
                # Single ROM - detailed embed
                rom = roms[0]
                embed, cover_file = await self.create_single_rom_embed(rom)
                
                try:
                    if cover_file:
                        await channel.send(embed=embed, file=cover_file)
                    else:
                        await channel.send(embed=embed)
                    discord_success = True
                except Exception as e:
                    logger.error(f"Failed to send single ROM to Discord: {e}")
                    # Rollback: remove from posted_roms since notification failed
                    await self.unmark_as_posted([rom['id']])
                finally:
                    # Clean up file object
                    if cover_file and hasattr(cover_file, 'fp'):
                        cover_file.fp.close()
                    
            else:
                # Multiple ROMs - batch embed
                embed, composite_cover_file = await self.create_batch_embed(roms)
                
                try:
                    if composite_cover_file:
                        await channel.send(embed=embed, file=composite_cover_file)
                    else:
                        await channel.send(embed=embed)
                    discord_success = True
                except Exception as e:
                    logger.error(f"Failed to send batch to Discord: {e}")
                    # Rollback: remove from posted_roms since notification failed
                    await self.unmark_as_posted([rom['id'] for rom in roms])
                finally:
                    # Clean up file object
                    if composite_cover_file and hasattr(composite_cover_file, 'fp'):
                        composite_cover_file.fp.close()
            
            if discord_success:
                logger.info(f"Posted {len(roms)} new ROM(s) from scan")
                
                # Dispatch to request system
                requests_cog = self.bot.get_cog('Request')
                if requests_cog:
                    logger.debug(f"Dispatching batch_scan_complete event with {len(roms)} ROMs")
                    self.bot.dispatch('batch_scan_complete', [
                        {
                            'id': rom['id'],
                            'platform': rom.get('platform_name', 'Unknown'),
                            'name': rom['name'],
                            'fs_name': rom.get('fs_name', ''),
                            'file_name': rom.get('file_name', '')
                        }
                        for rom in roms
                    ])
            else:
                logger.error("Failed to post to Discord, ROMs unmarked for retry")
        
        except Exception as e:
            logger.error(f"Error processing scan batch: {e}", exc_info=True)
            # On unexpected error, try to rollback
            try:
                await self.unmark_as_posted([rom['id'] for rom in roms])
            except Exception as rollback_error:
                logger.error(f"Failed to rollback posted status: {rollback_error}")
    
    async def mark_as_posted(self, roms: List[Dict], batch_id: str = None):
        """Mark ROMs as posted to prevent duplicates"""
        try:
            async with self.db.get_connection() as conn:
                await conn.executemany(
                    "INSERT OR IGNORE INTO posted_roms (rom_id, platform_name, rom_name, batch_id) VALUES (?, ?, ?, ?)",
                    [(rom['id'], rom.get('platform_name', 'Unknown'), rom['name'], batch_id) for rom in roms]
                )
                await conn.commit()
                
        except Exception as e:
            logger.error(f"Error marking ROMs as posted: {e}")
    
    async def unmark_as_posted(self, rom_ids: List[int]):
        """Remove ROMs from posted table (for rollback on Discord failure)"""
        try:
            async with self.db.get_connection() as conn:
                placeholders = ','.join('?' * len(rom_ids))
                await conn.execute(
                    f"DELETE FROM posted_roms WHERE rom_id IN ({placeholders})",
                    rom_ids
                )
                await conn.commit()
                logger.debug(f"Unmarked {len(rom_ids)} ROM(s) for retry after Discord failure")
                
        except Exception as e:
            logger.error(f"Error unmarking ROMs: {e}")
    
    async def download_cover_image_with_retry(self, rom: Dict, max_retries: int = 3) -> Optional[discord.File]:
        """Download cover image with retry logic"""
        platform_id = rom.get('platform_id')
        rom_id = rom.get('id')
        
        if not platform_id or not rom_id:
            return None
        
        cover_url = f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png"
        
        for attempt in range(max_retries):
            try:
                if not self.http_session or self.http_session.closed:
                    self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                
                async with self.http_session.get(cover_url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        byte_arr = BytesIO(image_data)
                        byte_arr.seek(0)
                        return discord.File(byte_arr, filename="cover.png")
                    elif response.status == 404:
                        # Don't retry on 404
                        logger.debug(f"Cover not found (404): {cover_url}")
                        return None
                    else:
                        logger.warning(f"Failed to download cover: HTTP {response.status} (attempt {attempt + 1}/{max_retries})")
                        
            except asyncio.TimeoutError:
                logger.warning(f"Cover download timeout (attempt {attempt + 1}/{max_retries})")
            except Exception as e:
                logger.error(f"Error downloading cover (attempt {attempt + 1}/{max_retries}): {e}")
            
            # Wait before retry (except on last attempt)
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))  # Exponential backoff
        
        return None
    
    async def download_cover_image(self, rom: Dict) -> Optional[discord.File]:
        """Legacy method for compatibility"""
        return await self.download_cover_image_with_retry(rom)
    
    async def initialize_igdb(self):
        """Initialize IGDB client if available"""
        try:
            from .igdb_client import IGDBClient
            self.igdb = IGDBClient()
            logger.debug("IGDB client initialized for recent ROMs")
        except Exception as e:
            logger.warning(f"IGDB integration not available: {e}")
            self.igdb = None
    
    async def get_igdb_metadata(self, rom_name: str, platform_name: str) -> Optional[Dict]:
        """Fetch IGDB metadata for a ROM"""
        if not self.igdb:
            return None
        
        try:
            # No fuzzy parameter - just pass the required arguments
            matches = await self.igdb.search_game(rom_name, platform_name)
            
            if matches:
                if len(matches) > 1:
                    logger.debug(f"Multiple IGDB matches for '{rom_name}', using first")
                return matches[0]
            return None
            
        except Exception as e:
            logger.error(f"Error fetching IGDB metadata: {e}")
            return None
    
    def get_platform_with_emoji(self, platform_name: str) -> str:
        """Get platform name with emoji"""
        search_cog = self.bot.get_cog('Search')
        if search_cog:
            return search_cog.get_platform_with_emoji(platform_name)
        return platform_name
    
    def format_file_size(self, size_bytes: int) -> str:
        """Format file size in human-readable format"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size_bytes < 1024.0:
                return f"{size_bytes:.1f} {unit}"
            size_bytes /= 1024.0
        return f"{size_bytes:.1f} PB"
    
    async def create_single_rom_embed(self, rom: Dict) -> Tuple[discord.Embed, Optional[discord.File]]:
        """Create a detailed embed for a single ROM with RomM metadata"""
        platform_name = rom.get('platform_name', 'Unknown')
        
        # Fetch detailed ROM data from RomM to get complete metadata
        detailed_rom = None
        try:
            detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom["id"]}', bypass_cache=True)
            if detailed_rom:
                rom.update(detailed_rom)
        except Exception as e:
            logger.warning(f"Could not fetch detailed ROM data: {e}")
        
        # Download cover image if available from RomM
        cover_file = None
        if rom.get('url_cover'):
            cover_file = await self.download_cover_image_with_retry(rom)
        
        # Create embed
        embed = discord.Embed(
            title=f"{rom['name']}",
            color=discord.Color.green()
        )
        
        # Add summary from RomM if available
        if rom.get('summary'):
            summary = rom['summary']
            # Truncate if too long
            if len(summary) > 150:
                summary = summary[:150]
                last_period = summary.rfind('.')
                if last_period > 100:
                    summary = summary[:last_period + 1]
                else:
                    summary = summary[:147] + "..."
            embed.description = summary
        
        # Handle cover image
        if cover_file:
            embed.set_thumbnail(url="attachment://cover.png")
        else:
            # Use RomM logo as fallback
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        # Platform with emoji
        platform_text = self.get_platform_with_emoji(platform_name)
        
        embed.add_field(
            name="Platform",
            value=platform_text,
            inline=True
        )
        
        # Release Date from RomM metadata
        release_text = "Unknown"
        if metadatum := rom.get('metadatum'):
            if release_date := metadatum.get('first_release_date'):
                try:
                    # Check if timestamp is in milliseconds
                    if release_date > 2_000_000_000:
                        release_date = release_date / 1000
                    
                    release_datetime = datetime.fromtimestamp(int(release_date))
                    release_text = release_datetime.strftime("%B %d, %Y")
                except (ValueError, TypeError) as e:
                    logger.debug(f"Error formatting release date: {e}")
        
        embed.add_field(
            name="Release Date",
            value=release_text,
            inline=True
        )
        
        # Add invisible field to force new row
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        
        # Developer/Companies from RomM metadata
        developer_text = "Unknown"
        if metadatum := rom.get('metadatum'):
            if companies := metadatum.get('companies'):
                if isinstance(companies, list):
                    company_list = companies[:1]  # Take first company
                    if company_list:
                        developer_text = company_list[0]
                        if len(developer_text) > 30:
                            developer_text = developer_text[:27] + "..."
                elif isinstance(companies, str):
                    developer_text = companies
                    if len(developer_text) > 30:
                        developer_text = developer_text[:27] + "..."
        
        embed.add_field(
            name="Developer",
            value=developer_text,
            inline=True
        )
        
        # Access Links with properly formatted emoji
        romm_url = f"{self.bot.config.DOMAIN}/rom/{rom['id']}"
        filename = rom.get('file_name') or rom.get('fs_name')
        
        # Get formatted RomM emoji
        romm_emoji = self.bot.get_formatted_emoji('romm')
        access_links = [f"[**{romm_emoji} RomM**]({romm_url})"]
        
        if filename:
            safe_filename = quote(filename)
            rom_download_url = f"{self.bot.config.DOMAIN}/api/roms/{rom['id']}/content/{safe_filename}"
            access_links.append(f"[**⬇️ Download**]({rom_download_url})")
        
        embed.add_field(
            name="Access",
            value=" ".join(access_links),
            inline=True
        )
        
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        
        # Footer with file info
        footer_parts = []
        
        if filename:
            if len(filename) > 50:
                name, ext = os.path.splitext(filename)
                if len(ext) <= 10:
                    truncated = name[:46 - len(ext)] + "..." + ext
                else:
                    truncated = filename[:47] + "..."
                footer_parts.append(truncated)
            else:
                footer_parts.append(filename)
        
        if rom.get("fs_size_bytes"):
            size_text = self.format_file_size(rom["fs_size_bytes"])
            footer_parts.append(size_text)
        
        footer_parts.append("Added to collection")
        
        embed.set_footer(text=" • ".join(footer_parts))
        embed.set_author(name="🆕 New Game Available")
        
        return embed, cover_file
    
    def calculate_grid_dimensions(self, num_images: int) -> Tuple[int, int, int, int]:
        """
        Calculate optimal grid dimensions and thumbnail size based on number of images.
        Returns: (cols, rows, thumb_width, thumb_height)
        
        Design principles:
        - Wider grids (up to 6 columns) to show more covers at larger sizes
        - Fewer images = larger, more detailed covers
        - More images = more columns, but maintain readable cover size
        - Maintain cover aspect ratio (approximately 2:3)
        - Keep total width under 1400px for optimal Discord viewing
        - Aesthetic limit: 24 covers (beyond this, too many covers to appreciate)
        """
        
        if num_images <= 0:
            return (0, 0, 0, 0)
        elif num_images == 2:
            # 2x1 grid - very large covers
            return (2, 1, 320, 448)
        elif num_images == 3:
            # 3x1 grid - large covers
            return (3, 1, 280, 392)
        elif num_images == 4:
            # 4x1 or 2x2 grid - large covers
            return (4, 1, 250, 350)
        elif num_images <= 6:
            # 3x2 grid - large-medium covers
            return (3, 2, 240, 336)
        elif num_images <= 8:
            # 4x2 grid - medium covers
            return (4, 2, 210, 294)
        elif num_images <= 10:
            # 5x2 grid - medium covers
            return (5, 2, 190, 266)
        elif num_images <= 12:
            # 6x2 or 4x3 grid - medium covers
            return (6, 2, 180, 252)
        elif num_images <= 15:
            # 5x3 grid - medium-small covers
            return (5, 3, 180, 252)
        elif num_images <= 18:
            # 6x3 grid - medium-small covers
            return (6, 3, 170, 238)
        elif num_images <= 24:
            # 6x4 grid - small covers (aesthetic limit)
            return (6, 4, 160, 224)
        else:
            # Beyond 24, covers become too overwhelming
            # This shouldn't happen due to should_fetch_covers check
            return (6, 4, 160, 224)
    
    def create_composite_from_images(self, images: List[Image.Image]) -> Optional[BytesIO]:
        """Creates a composite grid image from pre-loaded PIL Images with intelligent sizing"""
        try:
            if not images:
                return None
            
            num_images = len(images)
            
            # Calculate optimal grid dimensions and sizes
            cols, rows, thumb_width, thumb_height = self.calculate_grid_dimensions(num_images)
            
            if cols == 0 or rows == 0:
                return None
            
            spacing = 12  # Slightly increased spacing for larger images
            
            # Create composite with spacing
            composite_width = (cols * thumb_width) + ((cols - 1) * spacing)
            composite_height = (rows * thumb_height) + ((rows - 1) * spacing)
            
            # Transparent background (RGBA mode)
            composite = Image.new('RGBA', (composite_width, composite_height), (0, 0, 0, 0))
            
            # Paste thumbnails with spacing
            for idx, img in enumerate(images):
                # Images are already loaded and in RGBA mode
                # Create a copy to avoid modifying the original
                img_copy = img.copy()
                
                # Create thumbnail (maintains aspect ratio, may be smaller than target)
                img_copy.thumbnail((thumb_width, thumb_height), Image.Resampling.LANCZOS)
                
                # Create a fixed-size canvas with transparent background
                canvas = Image.new('RGBA', (thumb_width, thumb_height), (0, 0, 0, 0))
                
                # Center the thumbnail on the canvas
                paste_x = (thumb_width - img_copy.width) // 2
                paste_y = (thumb_height - img_copy.height) // 2
                canvas.paste(img_copy, (paste_x, paste_y))
                
                # Calculate position in composite
                row = idx // cols
                col = idx % cols
                x = col * (thumb_width + spacing)
                y = row * (thumb_height + spacing)
                
                # Paste the fixed-size canvas onto composite
                composite.paste(canvas, (x, y))
            
            # Save to buffer with high quality
            buffer = BytesIO()
            composite.save(buffer, format='PNG', optimize=False)  # Don't optimize for better quality
            buffer.seek(0)
            
            logger.debug(f"Created composite: {num_images} images, {cols}x{rows} grid, {thumb_width}x{thumb_height}px covers, total: {composite_width}x{composite_height}px")
            
            return buffer
            
        except Exception as e:
            logger.error(f"Error creating composite image: {e}", exc_info=True)
            return None
    
    def create_composite_cover_image(self, image_bytes_list: List[bytes]) -> Optional[BytesIO]:
        """Legacy method - converts bytes to images then creates composite"""
        try:
            images = []
            for img_bytes in image_bytes_list:
                try:
                    img = Image.open(BytesIO(img_bytes))
                    img.load()  # Force load all pixel data
                    if img.mode != 'RGBA':
                        img = img.convert('RGBA')
                    images.append(img)
                except Exception as e:
                    logger.warning(f"Failed to load cover image: {e}")
            
            if not images:
                return None
                
            return self.create_composite_from_images(images)
            
        except Exception as e:
            logger.error(f"Error in legacy composite method: {e}", exc_info=True)
            return None
    
    async def create_batch_embed(self, roms: List[Dict]) -> Tuple[discord.Embed, Optional[discord.File]]:
        """Create a summary embed for multiple ROMs"""
        is_bulk = len(roms) >= self.bulk_display_threshold
        should_fetch_covers = 2 <= len(roms) <= 24 and not is_bulk  # Increased from 16 to 24
        
        # Download covers in parallel if needed
        cover_images = []  # Store PIL Image objects directly
        if should_fetch_covers:
            # Fetch detailed ROM data for covers in parallel
            detail_tasks = []
            for rom in roms:
                if not rom.get('url_cover'):
                    detail_tasks.append(
                        self.bot.fetch_api_endpoint(f'roms/{rom["id"]}', bypass_cache=True)
                    )
                else:
                    detail_tasks.append(asyncio.sleep(0))
            
            if detail_tasks:
                detail_results = await asyncio.gather(*detail_tasks, return_exceptions=True)
                for i, result in enumerate(detail_results):
                    if isinstance(result, dict) and result:
                        roms[i].update(result)
            
            # Download all covers in parallel - load as PIL Images immediately
            async def download_and_load_cover(rom: Dict) -> Optional[Image.Image]:
                """Download cover and return loaded PIL Image"""
                platform_id = rom.get('platform_id')
                rom_id = rom.get('id')
                
                if not platform_id or not rom_id:
                    return None
                
                cover_url = f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png"
                
                try:
                    if not self.http_session or self.http_session.closed:
                        self.http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                    
                    async with self.http_session.get(cover_url) as response:
                        if response.status == 200:
                            # Read all data first
                            image_bytes = await response.read()
                            
                            # Immediately load and decode the complete image
                            # This forces PIL to read ALL image data
                            img = Image.open(BytesIO(image_bytes))
                            img.load()  # Force loading all pixel data into memory
                            
                            # Convert to RGBA for consistency
                            if img.mode != 'RGBA':
                                img = img.convert('RGBA')
                            
                            return img
                        else:
                            return None
                            
                except Exception as e:
                    logger.error(f"Error downloading/loading cover for ROM {rom_id}: {e}")
                    return None
            
            cover_tasks = [
                download_and_load_cover(rom) 
                for rom in roms if rom.get('url_cover')
            ]
            
            if cover_tasks:
                cover_results = await asyncio.gather(*cover_tasks, return_exceptions=True)
                cover_images = [
                    img for img in cover_results 
                    if img and isinstance(img, Image.Image) and not isinstance(img, Exception)
                ]
        
        # Create composite image in executor
        composite_file = None
        if cover_images:
            final_buffer = await self.bot.loop.run_in_executor(
                None, self.create_composite_from_images, cover_images
            )
            if final_buffer:
                composite_file = discord.File(final_buffer, filename="composite_cover.png")
        
        # Create embed
        if is_bulk:
            embed = discord.Embed(
                title=f"📦 Bulk Collection Update",
                description=f"{len(roms)} games have been added to the collection",
                color=discord.Color.orange()
            )
            
            # Set RomM logo as thumbnail
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
            
            # Group by platform
            by_platform = defaultdict(int)
            for rom in roms:
                platform = rom.get('platform_name', 'Unknown')
                by_platform[platform] += 1
            
            # Platform summary
            platform_summary = []
            for platform, count in sorted(by_platform.items(), key=lambda x: x[1], reverse=True)[:10]:
                platform_summary.append(f"• {self.get_platform_with_emoji(platform)}: {count} ROMs")
            
            if len(by_platform) > 10:
                platform_summary.append(f"• ...and {len(by_platform) - 10} more platforms")
            
            embed.add_field(
                name="Platforms Updated",
                value="\n".join(platform_summary),
                inline=False
            )
            
            embed.add_field(
                name="📋 Note",
                value="Showing summary view due to large number of additions. Use `/search` to find specific games.",
                inline=False
            )
        else:
            # Regular batch embed - match original formatting
            embed = discord.Embed(
                title=f"🆕 {len(roms)} New Games Added",
                description="Multiple games have been added to the collection:",
                color=discord.Color.blue()
            )
            
            # Set RomM logo as thumbnail
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
            
            # Set composite cover as main image if available
            if composite_file:
                embed.set_image(url="attachment://composite_cover.png")
            
            # Group by platform
            by_platform = defaultdict(list)
            for rom in roms:
                platform = rom.get('platform_name', 'Unknown')
                by_platform[platform].append(rom['name'])
            
            # Show games by platform - each game on a separate line
            for platform in sorted(by_platform.keys()):
                games = by_platform[platform]
                platform_display = self.get_platform_with_emoji(platform)
                
                # Format games with each on a new line
                games_text = "\n".join([f"• {game}" for game in games[:10]])
                if len(games) > 10:
                    games_text += f"\n• ...and {len(games) - 10} more"
                
                embed.add_field(
                    name=platform_display,
                    value=games_text,
                    inline=False
                )
            
            # View collection field with link
            romm_url = self.bot.config.DOMAIN
            romm_emoji = self.bot.get_formatted_emoji('romm')
            embed.add_field(
                name="View Collection",
                value=f"[{romm_emoji} Browse all games]({romm_url})",
                inline=False
            )
        
        embed.set_footer(text=f"Batch update • {len(roms)} new games • Use /search to download")
        
        return embed, composite_file
    
    def cog_unload(self):
        """Cleanup when cog is unloaded"""
        # Stop tasks
        if hasattr(self, 'cleanup_task'):
            self.cleanup_task.cancel()
        
        # Disconnect socket
        if self.sio.connected:
            asyncio.create_task(self.sio.disconnect())
        
        # Close HTTP session
        if self.http_session and not self.http_session.closed:
            asyncio.create_task(self.http_session.close())

def setup(bot):
    """Setup function for the cog"""
    bot.add_cog(RecentRomsMonitor(bot))
