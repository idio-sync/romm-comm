import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, timedelta, timezone
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
from dateutil.parser import parse as parse_datetime

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def is_admin():
    """Check if the user is the admin"""
    async def predicate(ctx: discord.ApplicationContext):
        # This check relies on the is_admin method in your main bot class
        return ctx.bot.is_admin(ctx.author)
    return commands.check(predicate)

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
        self._session_lock = asyncio.Lock()  # Lock for session creation
        
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
                # Capture scan start time on first ROM
                async with self.bot.scan_state_lock:
                    if self.bot.scan_state['is_scanning'] and not self.bot.scan_state.get('notification_cutoff_time'):
                        # Store cutoff with 10-second buffer to handle clock skew
                        self.bot.scan_state['notification_cutoff_time'] = datetime.utcnow() - timedelta(seconds=10)
                        logger.info(f"Set notification cutoff time: {self.bot.scan_state['notification_cutoff_time']}")
                
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
                        # DON'T await here - let it run independently
                        asyncio.create_task(self.handle_scan_complete(roms_to_process, stats))
                    else:
                        logger.info("Scan complete with no identified ROMs to process")
                        # Clear cutoff time only if no ROMs to process
                        async with self.bot.scan_state_lock:
                            if 'notification_cutoff_time' in self.bot.scan_state:
                                del self.bot.scan_state['notification_cutoff_time']
                                logger.debug("Cleared notification cutoff time (no ROMs)")
                        
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
            # Run database migration
            await self.migrate_add_message_id()
            
            # Initialize IGDB
            await self.initialize_igdb()
            
            # Initialize HTTP session for downloads
            await self._ensure_http_session()
            
            # Setup socket handlers
            self.setup_socket_handlers()
            
            # Start cleanup task
            self.cleanup_task.start()
            
            logger.debug("Recent ROMs monitor setup complete")
            
        except Exception as e:
            logger.error(f"Error setting up Recent ROMs monitor: {e}", exc_info=True)

    async def _ensure_http_session(self) -> aiohttp.ClientSession:
        """Ensure HTTP session exists and is open, with proper locking to prevent race conditions"""
        async with self._session_lock:
            if not self.http_session or self.http_session.closed:
                self.http_session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=30)
                )
        return self.http_session

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
            
            # Clear cutoff time AFTER all processing is complete
            async with self.bot.scan_state_lock:
                if 'notification_cutoff_time' in self.bot.scan_state:
                    del self.bot.scan_state['notification_cutoff_time']
                    logger.debug("Cleared notification cutoff time after processing")
            
        except Exception as e:
            logger.error(f"Error during batch processing: {e}", exc_info=True)
            
            # Clean up processing state on error
            async with self.processing_lock:
                for rom in roms:
                    rom_id = rom['id']
                    if rom_id in self.currently_processing:
                        self.currently_processing.remove(rom_id)
            
            # Clear cutoff time even on error
            async with self.bot.scan_state_lock:
                if 'notification_cutoff_time' in self.bot.scan_state:
                    del self.bot.scan_state['notification_cutoff_time']
                    logger.debug("Cleared notification cutoff time after error")
    
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
    
    async def fetch_rom_details_batch(self, rom_ids: List[int], batch_size: int = 30) -> List[Dict]:
        """Fetch detailed ROM data in parallel batches"""
        all_details = []
        
        for i in range(0, len(rom_ids), batch_size):
            batch = rom_ids[i:i + batch_size]
            
            # Fetch this batch in parallel
            tasks = [
                self.bot.fetch_api_endpoint(f'roms/{rom_id}', bypass_cache=True)
                for rom_id in batch
            ]
            
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Filter out exceptions and None results
                for rom_id, result in zip(batch, results):
                    if isinstance(result, Exception):
                        logger.warning(f"Error fetching ROM {rom_id}: {result}")
                    elif result:
                        all_details.append(result)
                    else:
                        logger.warning(f"No data returned for ROM {rom_id}")
                        
            except Exception as e:
                logger.error(f"Error in batch fetch: {e}")
        
        return all_details
    
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
            
            # Get cutoff time from scan state
            cutoff_time = None
            async with self.bot.scan_state_lock:
                cutoff_time = self.bot.scan_state.get('notification_cutoff_time')
            
            if not cutoff_time:
                logger.warning("No cutoff time found, will notify about all ROMs")
            
            # Fetch detailed ROM data in parallel batches
            rom_ids = [rom['id'] for rom in roms]
            fetch_start = time.time()
            detailed_roms = await self.fetch_rom_details_batch(rom_ids, batch_size=30)
            fetch_duration = time.time() - fetch_start
            
            logger.info(f"Fetched details for {len(detailed_roms)}/{len(rom_ids)} ROMs in {fetch_duration:.2f}s")
            
            if not detailed_roms:
                logger.warning("Could not fetch details for any ROMs")
                return
            
            # Filter ROMs by created_at timestamp
            new_roms = []
            filtered_count = 0
            parse_errors = 0
            
            for rom in detailed_roms:
                created_at_str = rom.get('created_at')
                
                if not created_at_str:
                    logger.warning(f"ROM {rom.get('id')} has no created_at timestamp, including it")
                    new_roms.append(rom)
                    continue
                
                try:
                    # Parse ISO format datetime
                    created_at = parse_datetime(created_at_str)
                    
                    # Ensure timezone-aware (convert to UTC if needed)
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=timezone.utc)
                    else:
                        created_at = created_at.astimezone(timezone.utc)
                    
                    # Remove timezone info for comparison (both should be UTC now)
                    created_at = created_at.replace(tzinfo=None)
                    
                    # Filter: only include ROMs created during or after the scan
                    if cutoff_time and created_at < cutoff_time:
                        logger.debug(f"Filtering out ROM {rom.get('name')} (created {created_at} < cutoff {cutoff_time})")
                        filtered_count += 1
                    else:
                        new_roms.append(rom)
                        
                except Exception as e:
                    logger.warning(f"Error parsing created_at for ROM {rom.get('id')}: {e}, including it")
                    new_roms.append(rom)
                    parse_errors += 1
            
            if filtered_count > 0:
                logger.info(f"Filtered out {filtered_count} pre-existing ROMs, {len(new_roms)} are genuinely new")
            if parse_errors > 0:
                logger.warning(f"Had {parse_errors} timestamp parsing errors")
            
            if not new_roms:
                logger.info("No new ROMs to post after filtering")
                return
            
            # Enrich ROM data with platform names
            await self.enrich_roms_with_platform_names(new_roms)
            
            # Mark as posted BEFORE sending
            await self.mark_as_posted(new_roms, batch_id)
            
            # Rest of existing posting logic...
            is_flood = len(new_roms) >= self.bulk_display_threshold
            discord_success = False
            message_id = None
            
            if len(new_roms) == 1:
                rom = new_roms[0]
                embed, cover_file = await self.create_single_rom_embed(rom)
                
                try:
                    if cover_file:
                        message = await channel.send(embed=embed, file=cover_file)
                    else:
                        message = await channel.send(embed=embed)
                    message_id = message.id
                    discord_success = True
                except Exception as e:
                    logger.error(f"Failed to send single ROM to Discord: {e}")
                    await self.unmark_as_posted([rom['id']])
                finally:
                    if cover_file and hasattr(cover_file, 'fp'):
                        cover_file.fp.close()
            else:
                embed, composite_cover_file = await self.create_batch_embed(new_roms)
                
                try:
                    if composite_cover_file:
                        message = await channel.send(embed=embed, file=composite_cover_file)
                    else:
                        message = await channel.send(embed=embed)
                    message_id = message.id
                    discord_success = True
                except Exception as e:
                    logger.error(f"Failed to send batch to Discord: {e}")
                    await self.unmark_as_posted([rom['id'] for rom in new_roms])
                finally:
                    if composite_cover_file and hasattr(composite_cover_file, 'fp'):
                        composite_cover_file.fp.close()
            
            if discord_success and message_id:
                await self.update_message_ids(new_roms, message_id, batch_id)
                logger.info(f"Posted {len(new_roms)} new ROM(s) from scan (message_id: {message_id})")
                
                # Dispatch to request system
                requests_cog = self.bot.get_cog('Request')
                if requests_cog:
                    self.bot.dispatch('batch_scan_complete', [
                        {
                            'id': rom['id'],
                            'platform': rom.get('platform_name', 'Unknown'),
                            'name': rom['name'],
                            'fs_name': rom.get('fs_name', ''),
                            'file_name': rom.get('file_name', '')
                        }
                        for rom in new_roms
                    ])
            
        except Exception as e:
            logger.error(f"Error processing scan batch: {e}", exc_info=True)
    
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
        """Download cover image with retry logic and validation"""
        # Maximum image constraints to prevent DoS via oversized images
        MAX_IMAGE_DIMENSION = 4096  # Max width or height
        MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB max file size

        platform_id = rom.get('platform_id')
        rom_id = rom.get('id')

        if not platform_id or not rom_id:
            return None

        cover_url = f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png"

        for attempt in range(max_retries):
            try:
                session = await self._ensure_http_session()

                async with session.get(cover_url) as response:
                    if response.status == 200:
                        # Check content-length header first if available
                        content_length = response.headers.get('content-length')
                        if content_length and int(content_length) > MAX_IMAGE_BYTES:
                            logger.warning(f"Cover image for ROM {rom_id} too large ({content_length} bytes), skipping")
                            return None

                        image_data = await response.read()

                        # Validate downloaded size
                        if len(image_data) > MAX_IMAGE_BYTES:
                            logger.warning(f"Cover image for ROM {rom_id} too large ({len(image_data)} bytes), skipping")
                            return None

                        # Validate image dimensions before returning
                        try:
                            img = Image.open(BytesIO(image_data))
                            if img.width > MAX_IMAGE_DIMENSION or img.height > MAX_IMAGE_DIMENSION:
                                logger.warning(f"Cover image for ROM {rom_id} dimensions too large ({img.width}x{img.height}), skipping")
                                img.close()
                                return None
                            img.close()
                        except Exception as e:
                            logger.warning(f"Could not validate image dimensions for ROM {rom_id}: {e}")
                            # Continue anyway - PIL will fail later if image is truly invalid

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
    
    async def update_message_ids(self, roms: List[Dict], message_id: int, batch_id: str):
        """Update posted ROMs with their Discord message ID"""
        try:
            async with self.db.get_connection() as conn:
                await conn.executemany(
                    "UPDATE posted_roms SET message_id = ? WHERE rom_id = ? AND batch_id = ?",
                    [(message_id, rom['id'], batch_id) for rom in roms]
                )
                await conn.commit()
                logger.debug(f"Updated {len(roms)} ROM(s) with message_id {message_id}")
        except Exception as e:
            logger.error(f"Error updating message IDs: {e}")

    async def get_recent_notifications(self, limit: int = 10, days: int = 7) -> List[Dict]:
        """Get recent ROM notifications with message IDs"""
        try:
            async with self.db.get_connection() as conn:
                cursor = await conn.execute(
                    """
                    SELECT DISTINCT message_id, batch_id, posted_at
                    FROM posted_roms 
                    WHERE message_id IS NOT NULL 
                    AND posted_at > datetime('now', '-' || ? || ' days')
                    ORDER BY posted_at DESC 
                    LIMIT ?
                    """,
                    (days, limit)
                )
                results = await cursor.fetchall()
                
                notifications = []
                for row in results:
                    message_id, batch_id, posted_at = row
                    
                    # Get all ROMs in this batch
                    rom_cursor = await conn.execute(
                        "SELECT rom_id, rom_name, platform_name FROM posted_roms WHERE batch_id = ?",
                        (batch_id,)
                    )
                    roms = await rom_cursor.fetchall()
                    
                    notifications.append({
                        'message_id': message_id,
                        'batch_id': batch_id,
                        'posted_at': posted_at,
                        'roms': [{'id': r[0], 'name': r[1], 'platform': r[2]} for r in roms]
                    })
                
                return notifications
                
        except Exception as e:
            logger.error(f"Error fetching recent notifications: {e}")
            return []
    
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
            # Maximum image dimensions to prevent DoS via oversized images
            MAX_IMAGE_DIMENSION = 4096  # Max width or height
            MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB max file size

            async def download_and_load_cover(rom: Dict) -> Optional[Image.Image]:
                """Download cover and return loaded PIL Image"""
                platform_id = rom.get('platform_id')
                rom_id = rom.get('id')

                if not platform_id or not rom_id:
                    return None

                cover_url = f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png"

                try:
                    session = await self._ensure_http_session()

                    async with session.get(cover_url) as response:
                        if response.status == 200:
                            # Check content-length header first if available
                            content_length = response.headers.get('content-length')
                            if content_length and int(content_length) > MAX_IMAGE_BYTES:
                                logger.warning(f"Cover image for ROM {rom_id} too large ({content_length} bytes), skipping")
                                return None

                            # Read all data first
                            image_bytes = await response.read()

                            # Validate downloaded size
                            if len(image_bytes) > MAX_IMAGE_BYTES:
                                logger.warning(f"Cover image for ROM {rom_id} too large ({len(image_bytes)} bytes), skipping")
                                return None

                            # Immediately load and decode the complete image
                            # This forces PIL to read ALL image data
                            img = Image.open(BytesIO(image_bytes))

                            # Validate dimensions before loading full image data
                            if img.width > MAX_IMAGE_DIMENSION or img.height > MAX_IMAGE_DIMENSION:
                                logger.warning(f"Cover image for ROM {rom_id} dimensions too large ({img.width}x{img.height}), skipping")
                                img.close()
                                return None

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
    
    @discord.slash_command(
        name="refresh_recent_metadata",
        description="Refresh posted recent ROM notifications with latest metadata (admin only)."
    )
    @discord.option(
        "count",
        description="Number of recent notifications to refresh (default: 1)",
        required=False,
        min_value=1,
        max_value=20,
        default=1
    )
    @is_admin()
    async def refresh_recent(self, ctx: discord.ApplicationContext, count: int = 5):
        """Refresh recent ROM notifications with updated metadata"""
        if not self.enabled:
            await ctx.respond("Recent ROMs monitoring is disabled.", ephemeral=True)
            return
        
        await ctx.defer(ephemeral=True)
        
        try:
            # Get recent notifications
            notifications = await self.get_recent_notifications(limit=count, days=7)
            
            if not notifications:
                await ctx.followup.send("No recent notifications found to refresh.", ephemeral=True)
                return
            
            channel = self.bot.get_channel(self.recent_roms_channel_id)
            if not channel:
                await ctx.followup.send("Recent ROMs channel not found.", ephemeral=True)
                return
            
            refreshed = 0
            failed = 0
            skipped = 0
            
            for notification in notifications:
                try:
                    message_id = notification['message_id']
                    rom_data = notification['roms']
                    
                    # Try to fetch the message
                    try:
                        message = await channel.fetch_message(message_id)
                    except discord.NotFound:
                        logger.warning(f"Message {message_id} not found (deleted?)")
                        skipped += 1
                        continue
                    except discord.Forbidden:
                        logger.error(f"No permission to access message {message_id}")
                        skipped += 1
                        continue
                    
                    # Re-fetch ROM data from API
                    rom_ids = [rom['id'] for rom in rom_data]
                    updated_roms = []
                    
                    for rom_id in rom_ids:
                        try:
                            rom_details = await self.bot.fetch_api_endpoint(f'roms/{rom_id}', bypass_cache=True)
                            if rom_details:
                                updated_roms.append(rom_details)
                        except Exception as e:
                            logger.warning(f"Could not fetch ROM {rom_id}: {e}")
                    
                    if not updated_roms:
                        skipped += 1
                        continue
                    
                    # Enrich with platform names
                    await self.enrich_roms_with_platform_names(updated_roms)
                    
                    # Recreate embed(s)
                    if len(updated_roms) == 1:
                        # Single ROM
                        embed, cover_file = await self.create_single_rom_embed(updated_roms[0])
                        
                        try:
                            if cover_file:
                                # Can't replace attachments, so send new message and delete old
                                new_message = await channel.send(embed=embed, file=cover_file)
                                await message.delete()
                                # Update message ID in database
                                await self.update_message_ids(updated_roms, new_message.id, notification['batch_id'])
                            else:
                                # Just update embed
                                await message.edit(embed=embed)
                            refreshed += 1
                        finally:
                            if cover_file and hasattr(cover_file, 'fp'):
                                cover_file.fp.close()
                    else:
                        # Batch
                        embed, composite_file = await self.create_batch_embed(updated_roms)
                        
                        try:
                            if composite_file:
                                # Can't replace attachments, so send new message and delete old
                                new_message = await channel.send(embed=embed, file=composite_file)
                                await message.delete()
                                # Update message ID in database
                                await self.update_message_ids(updated_roms, new_message.id, notification['batch_id'])
                            else:
                                # Just update embed
                                await message.edit(embed=embed)
                            refreshed += 1
                        finally:
                            if composite_file and hasattr(composite_file, 'fp'):
                                composite_file.fp.close()
                    
                except Exception as e:
                    logger.error(f"Error refreshing notification: {e}")
                    failed += 1
            
            # Summary
            summary_parts = []
            if refreshed > 0:
                summary_parts.append(f"Refreshed {refreshed} notification(s)")
            if skipped > 0:
                summary_parts.append(f"Skipped {skipped} (deleted/inaccessible)")
            if failed > 0:
                summary_parts.append(f"Failed {failed}")
            
            summary = " | ".join(summary_parts) if summary_parts else "No notifications refreshed"
            await ctx.followup.send(summary, ephemeral=True)
            
        except Exception as e:
            logger.error(f"Error in refresh_recent command: {e}", exc_info=True)
            await ctx.followup.send(f"Error refreshing notifications: {str(e)}", ephemeral=True)
    
    async def migrate_add_message_id(self):
        """Add message_id column to posted_roms table if it doesn't exist"""
        try:
            async with self.db.get_connection() as conn:
                # Check if column exists
                cursor = await conn.execute("PRAGMA table_info(posted_roms)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]
                
                if 'message_id' not in column_names:
                    await conn.execute("ALTER TABLE posted_roms ADD COLUMN message_id INTEGER")
                    await conn.commit()
                    logger.info("Added message_id column to posted_roms table")
        except Exception as e:
            logger.error(f"Error migrating database: {e}")
    
    
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
