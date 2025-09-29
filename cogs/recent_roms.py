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
import threading
from PIL import Image
from io import BytesIO
from typing import List, Optional

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
        
        # Thread-safe scan tracking with locks
        self.scan_lock = threading.Lock()
        self.current_scan_roms: List[Dict] = []
        self.current_scan_names: Set[str] = set()
        self.scan_completion_timer: Optional[asyncio.Task] = None
        
        # Track processed ROMs to prevent duplicates within a session
        self.processing_lock = threading.Lock()
        self.currently_processing: Set[int] = set()
        self.recently_processed: Set[int] = set()  # Track recently processed ROMs
        
        # IGDB client
        self.igdb = None
        
        # Socket.IO client
        self.sio = socketio.AsyncClient(
            logger=False, 
            engineio_logger=False, 
            reconnection=True, 
            reconnection_attempts=3,
            reconnection_delay=5,
            reconnection_delay_max=30
        )
        self._connection_lock = asyncio.Lock()
        self._handlers_registered = False
        
        # Create a dedicated event loop and thread for Socket.IO
        self.sio_loop = asyncio.new_event_loop()
        self.sio_thread = None
        
        if self.enabled:
            bot.loop.create_task(self.setup())
            logger.debug("Recent ROMs WebSocket monitor enabled")

    def setup_socket_handlers(self):
        """Set up Socket.IO event handlers that run in their own thread."""
        
        # Prevent multiple handler registration
        if self._handlers_registered:
            logger.debug("Socket handlers already registered, skipping")
            return
        
        self._handlers_registered = True
        
        async def _trigger_batch_processing_after_delay():
            """The timer's target. Waits for inactivity then hands off the batch."""
            try:
                await asyncio.sleep(300)  # Wait for 5 minutes of inactivity
                
                logger.info("Scan inactivity timer expired, handing off collected ROMs for processing.")
                with self.scan_lock:
                    if self.current_scan_roms:
                        roms_to_process = self.current_scan_roms.copy()
                        self.current_scan_roms.clear()
                        self.current_scan_names.clear()
                        
                        asyncio.run_coroutine_threadsafe(
                            self.handle_scan_complete(roms_to_process, {}), self.bot.loop
                        )
            except asyncio.CancelledError:
                logger.debug("Scan completion timer was reset by a new ROM event.")

        @self.sio.event
        async def connect():
            logger.info("RecentRomsMonitor connected to websocket server")
            with self.scan_lock:
                self.current_scan_roms.clear()
                self.current_scan_names.clear()
            # Clear recently processed on reconnect to allow fresh processing
            with self.processing_lock:
                self.recently_processed.clear()
        
        @self.sio.event
        async def disconnect():
            logger.info("RecentRomsMonitor disconnected from websocket server")
            # Cancel any pending timer
            if self.scan_completion_timer and not self.scan_completion_timer.done():
                self.scan_completion_timer.cancel()
        
        @self.sio.on('scan:scanning_rom')
        async def on_scanning_rom(data):
            """Collects ROMs and resets the inactivity timer."""
            rom_id = data.get('id')
            rom_name = data.get('name', 'Unknown')
            
            if not rom_id:
                logger.debug(f"Received ROM event without ID: {rom_name}")
                return
            
            # Early duplicate check within current session
            with self.processing_lock:
                if rom_id in self.currently_processing or rom_id in self.recently_processed:
                    logger.debug(f"ROM {rom_id} ({rom_name}) already processing or recently processed, skipping")
                    return
            
            logger.info(f"Received scan:scanning_rom event: {rom_name} (ID: {rom_id})")
            
            # Cancel existing timer
            if self.scan_completion_timer and not self.scan_completion_timer.done():
                self.scan_completion_timer.cancel()

            # Check if the ROM was posted before (database check)
            main_loop_future = asyncio.run_coroutine_threadsafe(
                self.has_been_posted(rom_id), self.bot.loop
            )
            try:
                already_posted = main_loop_future.result(timeout=5)
            except Exception as e:
                logger.error(f"Error checking if ROM was posted: {e}")
                already_posted = False

            if already_posted:
                logger.debug(f"ROM {rom_id} ({rom_name}) already posted to Discord, skipping")
                return
            
            # Thread-safe addition to batch
            with self.scan_lock:
                # Check duplicates within current batch
                if rom_name.lower() in self.current_scan_names:
                    logger.debug(f"ROM {rom_name} already in current batch, skipping")
                    return
                
                # Check if this exact ROM ID is already in the batch
                if any(r['id'] == rom_id for r in self.current_scan_roms):
                    logger.debug(f"ROM ID {rom_id} already in current batch, skipping")
                    return
                
                rom = {
                    'id': rom_id,
                    'name': rom_name,
                    'platform_name': data.get('platform_name', 'Unknown'),
                    'platform_id': data.get('platform_id'),
                    'file_name': data.get('file_name'),
                    'fs_name': data.get('fs_name'),
                    'fs_size_bytes': data.get('fs_size_bytes'),
                    'url_cover': data.get('url_cover'),
                    'created_at': data.get('created_at'),
                }
                
                self.current_scan_names.add(rom_name.lower())
                self.current_scan_roms.append(rom)
                
                # Mark as currently processing
                with self.processing_lock:
                    self.currently_processing.add(rom_id)
                
                logger.debug(f"SIO_THREAD: Queued ROM: {rom['name']} ({len(self.current_scan_roms)} in batch)")

            # Start new timer
            self.scan_completion_timer = self.sio_loop.create_task(_trigger_batch_processing_after_delay())

        @self.sio.on('scan:done')
        async def on_scan_done(stats):
            """Handles the official scan completion event."""
            logger.info("Received scan:done event")
            
            # Cancel timer if running
            if self.scan_completion_timer and not self.scan_completion_timer.done():
                self.scan_completion_timer.cancel()
            
            with self.scan_lock:
                if self.current_scan_roms:
                    roms_to_process = self.current_scan_roms.copy()
                    self.current_scan_roms.clear()
                    self.current_scan_names.clear()
                    
                    logger.info(f"Received scan:done, handing off {len(roms_to_process)} ROMs for processing.")
                    asyncio.run_coroutine_threadsafe(
                        self.handle_scan_complete(roms_to_process, stats), self.bot.loop
                    )
                else:
                    # This is normal when only unidentified ROMs were found
                    logger.info("Received scan:done with no identified ROMs to process (this is normal for scans with only unidentified ROMs)")

    def start_socketio_thread(self):
        """Run Socket.IO in a separate thread."""
        asyncio.set_event_loop(self.sio_loop)
        self.sio_loop.run_until_complete(self.ensure_connected())
        self.sio_loop.run_forever()

    async def setup(self):
        """Setup IGDB client and launch the WebSocket thread."""
        await self.initialize_igdb()
        await self.bot.wait_until_ready()
        
        # Setup handlers before starting thread
        self.setup_socket_handlers()
        
        if self.enabled:
            self.sio_thread = threading.Thread(target=self.start_socketio_thread, daemon=True)
            self.sio_thread.start()
            
            # Start a task to clean up recently processed periodically
            self.bot.loop.create_task(self.cleanup_recently_processed())

    async def cleanup_recently_processed(self):
        """Periodically clean up the recently processed set to prevent memory growth."""
        while self.enabled:
            await asyncio.sleep(3600)  # Clean up every hour
            with self.processing_lock:
                logger.debug(f"Clearing {len(self.recently_processed)} recently processed ROM IDs")
                self.recently_processed.clear()

    async def ensure_connected(self):
        """Ensure Socket.IO connection is established."""
        async with self._connection_lock:
            if not self.sio.connected:
                try:
                    base_url = self.config.API_BASE_URL.rstrip('/')
                    auth_string = f"{self.config.USER}:{self.config.PASS}"
                    auth_bytes = auth_string.encode('ascii')
                    base64_auth = base64.b64encode(auth_bytes).decode('ascii')
                    headers = {'Authorization': f'Basic {base64_auth}', 'User-Agent': 'RommBot/1.0'}
                    
                    logger.debug(f"RecentRomsMonitor connecting to: {base_url}")
                    await self.sio.connect(
                        base_url,
                        headers=headers,
                        wait_timeout=30,
                        transports=['websocket'],
                        socketio_path='ws/socket.io'
                    )
                except Exception as e:
                    logger.error(f"RecentRomsMonitor connection error: {str(e)}")
                    raise

    async def handle_scan_complete(self, roms: List[Dict], stats: Dict):
        """Processes the batch, posts to Discord, and dispatches events."""
        try:
            if not roms:
                return
            
            # Remove duplicates based on ROM ID
            unique_roms = {}
            for rom in roms:
                if rom['id'] not in unique_roms:
                    unique_roms[rom['id']] = rom
            
            roms = list(unique_roms.values())
            
            logger.info(f"MAIN_LOOP: Processing scan batch of {len(roms)} unique ROMs.")
            
            # Final database check before processing
            filtered_roms = []
            for rom in roms:
                if not await self.has_been_posted(rom['id']):
                    filtered_roms.append(rom)
                else:
                    logger.debug(f"ROM {rom['id']} ({rom['name']}) already in database, filtering out")
            
            if filtered_roms:
                await self.process_scan_batch(filtered_roms)
            
            # Move from currently_processing to recently_processed
            with self.processing_lock:
                for rom in roms:
                    rom_id = rom['id']
                    if rom_id in self.currently_processing:
                        self.currently_processing.remove(rom_id)
                    self.recently_processed.add(rom_id)
            
            logger.info("MAIN_LOOP: Triggering API data refresh after scan completion.")
            await self.bot.update_api_data()
            
        except Exception as e:
            logger.error(f"Error during main-thread batch processing: {e}", exc_info=True)
            # Clean up processing state on error
            with self.processing_lock:
                for rom in roms:
                    rom_id = rom['id']
                    if rom_id in self.currently_processing:
                        self.currently_processing.remove(rom_id)
    
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
            
            # Enrich ROM data with platform names if needed
            platforms_data = None
            for rom in roms:
                if platform_id := rom.get('platform_id'):
                    if not rom.get('platform_name'):
                        # Fetch platform data if not cached
                        if not platforms_data:
                            platforms_data = await self.bot.fetch_api_endpoint('platforms')
                        
                        if platforms_data:
                            for p in platforms_data:
                                if p.get('id') == platform_id:
                                    custom_name = p.get('custom_name')
                                    rom['platform_name'] = custom_name.strip() if custom_name and custom_name.strip() else p.get('name', 'Unknown')
                                    break
            
            # Mark as posted BEFORE sending to Discord to prevent race conditions
            await self.mark_as_posted(roms, batch_id)
            
            # Send to Discord based on batch size
            if len(roms) == 1:
                # Single ROM - detailed embed
                rom = roms[0]
                embed, cover_file = await self.create_single_rom_embed(rom)
                
                if cover_file:
                    await channel.send(embed=embed, file=cover_file)
                else:
                    await channel.send(embed=embed)
                    
            else:
                # Multiple ROMs - batch embed
                embed, composite_cover_file = await self.create_batch_embed(roms)
                
                if composite_cover_file:
                    await channel.send(embed=embed, file=composite_cover_file)
                else:
                    await channel.send(embed=embed)
            
            # ALWAYS dispatch to request system
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
            
            logger.info(f"Posted {len(roms)} new ROM(s) from scan")
        
        except Exception as e:
            logger.error(f"Error processing scan batch: {e}", exc_info=True)
    
    async def initialize_igdb(self):
        """Initialize IGDB client if available"""
        try:
            from .igdb_client import IGDBClient
            self.igdb = IGDBClient()
            logger.debug("IGDB client initialized for recent ROMs")
        except Exception as e:
            logger.warning(f"IGDB integration not available: {e}")
            self.igdb = None
    
    def create_composite_cover_image(self, image_bytes_list: List[bytes]) -> Optional[BytesIO]:
        """Creates a composite grid image from a list of cover image bytes."""
        if not image_bytes_list:
            return None

        try:
            # --- Configuration ---
            COVER_WIDTH, COVER_HEIGHT = 150, 200
            PADDING = 10
            
            num_images = len(image_bytes_list)

            # --- Determine Grid Layout based on your rules ---
            if num_images <= 3: # 2-3 covers: Single row (e.g., 3x1)
                cols, rows = num_images, 1
            elif num_images == 4: # 4 covers: 2x2 grid
                cols, rows = 2, 2
            elif num_images <= 6: # 5-6 covers: 2x3 grid
                cols, rows = 3, 2
            else: # 7-9 covers: 3x3 grid
                cols, rows = 3, 3

            # --- Calculate Canvas Size ---
            canvas_width = (cols * COVER_WIDTH) + ((cols + 1) * PADDING)
            canvas_height = (rows * COVER_HEIGHT) + ((rows + 1) * PADDING)
            
            # --- Create Canvas ---
            # Using RGBA for transparency support
            composite_image = Image.new('RGBA', (canvas_width, canvas_height), (0, 0, 0, 0))

            # --- Paste Covers onto Canvas ---
            for i, img_bytes in enumerate(image_bytes_list):
                if i >= (cols * rows): break # Stop if we have more images than grid spaces

                cover = Image.open(BytesIO(img_bytes)).convert("RGBA")
                cover.thumbnail((COVER_WIDTH, COVER_HEIGHT), Image.Resampling.LANCZOS)
                
                # Calculate position for this cover
                row, col = i // cols, i % cols
                x = PADDING + col * (COVER_WIDTH + PADDING)
                y = PADDING + row * (COVER_HEIGHT + PADDING)
                
                composite_image.paste(cover, (x, y), cover)

            # --- Save to a Bytes Buffer ---
            final_buffer = BytesIO()
            composite_image.save(final_buffer, format='PNG')
            final_buffer.seek(0) # IMPORTANT: Reset buffer position to the beginning
            
            return final_buffer
            
        except Exception as e:
            logger.error(f"Failed to create composite cover image: {e}")
            return None
    
    async def download_cover_image(self, rom_data: Dict) -> Optional[discord.File]:
        """Download cover image from Romm API and return as Discord File"""
        try:
            # Check if we have url_cover at all
            if not rom_data.get('url_cover'):
                return None
                
            # Build the direct cover URL from Romm API
            platform_id = rom_data.get('platform_id')
            rom_id = rom_data.get('id')
            
            if not platform_id or not rom_id:
                logger.warning("Missing platform_id or rom_id for cover download")
                return None
            
            # Construct the direct cover URL
            cover_url = f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png"
            
            logger.debug(f"Downloading cover from: {cover_url}")
            
            # Download the image
            import aiohttp
            from io import BytesIO
            async with aiohttp.ClientSession() as session:
                async with session.get(cover_url) as response:
                    if response.status == 200:
                        image_data = await response.read()
                        byte_arr = BytesIO(image_data)
                        byte_arr.seek(0)
                        return discord.File(byte_arr, filename="cover.png")
                    else:
                        logger.warning(f"Failed to download cover: HTTP {response.status}")
                        return None
                        
        except Exception as e:
            logger.error(f"Error downloading cover image: {e}")
            return None
    
    async def has_been_posted(self, rom_id: int) -> bool:
        """Check if a ROM has already been posted"""
        try:
            async with self.db.get_connection() as conn: 
                cursor = await conn.execute(
                    "SELECT 1 FROM posted_roms WHERE rom_id = ?",
                    (rom_id,)
                )
                result = await cursor.fetchone()
                return result is not None
        except Exception as e:
            logger.error(f"Error checking if ROM was posted: {e}")
            return False  # Assume not posted on error
    
    async def mark_as_posted(self, roms: List[Dict], batch_id: str = None):
        """Mark ROMs as posted to prevent duplicates"""
        try:
            async with self.db.get_connection() as conn:
                for rom in roms:
                    await conn.execute(
                        "INSERT OR IGNORE INTO posted_roms (rom_id, platform_name, rom_name, batch_id) VALUES (?, ?, ?, ?)",
                        (rom['id'], rom.get('platform_name', 'Unknown'), rom['name'], batch_id)
                    )
                await conn.commit()
                logger.debug(f"Marked {len(roms)} ROM(s) as posted in database")
        except Exception as e:
            logger.error(f"Error marking ROMs as posted: {e}")
    
    def format_file_size(self, size_bytes: Union[int, float]) -> str:
        """Format size in bytes to human readable format"""
        if not size_bytes or not isinstance(size_bytes, (int, float)):
            return "Unknown size"
        
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        size_value = float(size_bytes)
        unit_index = 0
        while size_value >= 1024 and unit_index < len(units) - 1:
            size_value /= 1024
            unit_index += 1
        return f"{size_value:.2f} {units[unit_index]}"
    
    async def get_igdb_metadata(self, rom_name: str, platform_name: str = None) -> Optional[Dict]:
        """Fetch IGDB metadata for a ROM"""
        if not self.igdb:
            return None
        
        try:
            igdb_slug = None
            if platform_name:
                # Updated to use shared database
                async with self.db.get_connection() as conn:
                    cursor = await conn.execute(
                        """SELECT igdb_slug FROM platform_mappings 
                           WHERE display_name = ? OR folder_name = ?""",
                        (platform_name, platform_name.lower().replace(' ', '-'))
                    )
                    result = await cursor.fetchone()
                    if result and result[0]:
                        igdb_slug = result[0]
                        logger.debug(f"Mapped platform '{platform_name}' to IGDB slug '{igdb_slug}'")
            
            # Search with slug if we found one, otherwise without platform filter
            matches = await self.igdb.search_game(rom_name, igdb_slug)
            if matches:
                # Prioritize an exact, case-insensitive name match
                for match in matches:
                    if match.get('name', '').lower() == rom_name.lower():
                        logger.debug(f"Found exact IGDB match for '{rom_name}': {match['name']}")
                        return match  # Return the exact match

                # If no exact match is found, fall back to the first result as a best guess
                logger.debug(f"No exact IGDB match for '{rom_name}', falling back to: {matches[0]['name']}")
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
    
    async def create_single_rom_embed(self, rom: Dict) -> Tuple[discord.Embed, Optional[discord.File]]:
        """Create a detailed embed for a single ROM with IGDB metadata"""
        platform_name = rom.get('platform_name', 'Unknown')
        
        # Fetch detailed ROM data to ensure we have file size info and cover URL
        if not rom.get('fs_size_bytes') and not rom.get('files'):
            try:
                detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom["id"]}', bypass_cache=True)
                if detailed_rom:
                    rom.update(detailed_rom)
            except Exception as e:
                logger.warning(f"Could not fetch detailed ROM data: {e}")
        
        # Try to get IGDB metadata
        igdb_data = await self.get_igdb_metadata(rom['name'], platform_name)
        
        # Download cover image if available from Romm
        cover_file = None
        if rom.get('url_cover'):
            cover_file = await self.download_cover_image(rom)
        
        # Create embed with a cleaner title
        embed = discord.Embed(
            title=f"{rom['name']}",
            color=discord.Color.green()
        )
        
        # Add description with summary if available from IGDB
        if igdb_data and igdb_data.get('summary'):
            summary = igdb_data['summary']
            # Truncate if too long, but try to end at a sentence
            if len(summary) > 150:
                summary = summary[:150]
                last_period = summary.rfind('.')
                if last_period > 100:  # If there's a period after character 150
                    summary = summary[:last_period + 1]
                else:
                    summary = summary[:147] + "..."
            embed.description = summary

        # Handle cover image - use Romm cover with attachment:// if available
        if cover_file:
            # Use the attachment:// method to embed the Romm cover
            embed.set_thumbnail(url="attachment://cover.png")
        elif igdb_data and igdb_data.get('cover_url'):
            # Fall back to IGDB cover if no Romm cover available
            embed.set_thumbnail(url=igdb_data['cover_url'])
        else:
            # Use RomM logo as final fallback
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        # Platform with emoji
        platform_text = self.get_platform_with_emoji(platform_name)
        
        # Row 1, Column 1: Platform
        embed.add_field(
            name="Platform",
            value=platform_text,
            inline=True
        )
        
        # Row 1, Column 2: Release Date
        release_text = "Unknown"
        if igdb_data and igdb_data.get('release_date') and igdb_data['release_date'] != "Unknown":
            try:
                date_obj = datetime.strptime(igdb_data['release_date'], "%Y-%m-%d")
                release_text = date_obj.strftime("%B %d, %Y")
            except:
                release_text = igdb_data.get('release_date', "Unknown")
        
        embed.add_field(
            name="Release Date",
            value=release_text,
            inline=True
        )
        
        # Add invisible field to force new row
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        
        # Row 2, Column 1: Developer
        developer_text = "Unknown"
        if igdb_data:
            companies = []
            if igdb_data.get('developers'):
                companies = igdb_data['developers'][:1]
            elif igdb_data.get('publishers'):
                companies = igdb_data['publishers'][:1]
            
            if companies:
                developer_text = companies[0]
                if len(developer_text) > 30:
                    developer_text = developer_text[:27] + "..."
        
        embed.add_field(
            name="Developer",
            value=developer_text,
            inline=True
        )
        
        # Row 2, Column 2: Access Links
        romm_url = f"{self.bot.config.DOMAIN}/rom/{rom['id']}"
        filename = rom.get('file_name') or rom.get('fs_name')
        
        access_links = [f"[:romm:]({romm_url}) "]
        
        if filename:
            safe_filename = quote(filename)
            rom_download_url = f"{self.bot.config.DOMAIN}/api/roms/{rom['id']}/content/{safe_filename}?"
            access_links.append(f" [**Download ⬇️**]({rom_download_url})")
            
        embed.add_field(
            name="Access",
            value=" • ".join(access_links),
            inline=True
        )
        
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        
        # Footer with file info
        footer_parts = []
        
        filename = rom.get('file_name') or rom.get('fs_name')
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

        # Return the embed and cover file (keep the file!)
        return embed, cover_file
    
    async def create_batch_embed(self, roms: List[Dict]) -> Tuple[discord.Embed, Optional[discord.File]]:
        """Create a summary embed for multiple ROMs with optional cover images"""
        
        # Check if this is a flood scenario or too many ROMs
        is_bulk = len(roms) >= self.bulk_display_threshold
        should_fetch_covers = 2 <= len(roms) <= 9 and not is_bulk
        
        # 1. Collect raw image bytes instead of discord.File objects
        cover_image_data = [] 
        if should_fetch_covers:
            download_tasks = []
            
            async def fetch_and_read_cover(rom_data):
                """Nested helper to download a cover and return its bytes."""
                cover_file = await self.download_cover_image(rom_data)
                if cover_file:
                    cover_file.fp.seek(0)
                    return cover_file.fp.read()
                return None

            for rom in roms:
                # Fetch detailed ROM data if needed for the cover URL
                if not rom.get('url_cover'):
                    try:
                        detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom["id"]}', bypass_cache=True)
                        if detailed_rom:
                            rom.update(detailed_rom)
                    except Exception as e:
                        logger.debug(f"Could not fetch detailed ROM data for covers: {e}")
                
                if rom.get('url_cover'):
                    download_tasks.append(fetch_and_read_cover(rom))

            # Download all covers concurrently
            if download_tasks:
                results = await asyncio.gather(*download_tasks, return_exceptions=True)
                cover_image_data = [data for data in results if data and not isinstance(data, Exception)]

        # 2. Create the composite image
        composite_file = None
        if cover_image_data:
            # Run the synchronous Pillow code in a thread to avoid blocking
            final_buffer = await self.bot.loop.run_in_executor(
                None, self.create_composite_cover_image, cover_image_data
            )
            if final_buffer:
                composite_file = discord.File(final_buffer, filename="composite_cover.png")
        
        if is_bulk:
            # Create a simplified bulk notification
            embed = discord.Embed(
                title=f"📦 Bulk Collection Update",
                description=f"{len(roms)} games have been added to the collection",
                color=discord.Color.orange()
            )
            
            # Group by platform for summary
            by_platform = defaultdict(int)
            for rom in roms:
                platform = rom.get('platform_name', 'Unknown')
                by_platform[platform] += 1
            
            # Show platform summary
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
                name="🔍 Note", 
                value="Showing summary view due to large number of additions.",
                inline=False
            )
        else:
            # Normal batch embed for reasonable number of ROMs
            embed = discord.Embed(
                title=f"🆕 {len(roms)} New Games Added",
                description="Multiple games have been added to the collection:",
                color=discord.Color.blue()
            )
            
            # Group by platform
            by_platform = defaultdict(list)
            for rom in roms:
                platform = rom.get('platform_name', 'Unknown')
                by_platform[platform].append(rom)
            
            # Add fields for each platform (Discord limit is 25 fields)
            field_count = 0
            roms_shown = 0
            
            for platform, platform_roms in sorted(by_platform.items()):
                if field_count >= 20:  # Leave room for other fields
                    remaining = len(roms) - roms_shown
                    embed.add_field(
                        name="And more...",
                        value=f"{remaining} additional ROM(s)",
                        inline=False
                    )
                    break
                
                # Create ROM list for this platform
                rom_list = []
                for i, rom in enumerate(platform_roms):
                    if i >= self.max_roms_per_post:
                        rom_list.append(f"• ...and {len(platform_roms) - i} more")
                        break
                    rom_list.append(f"• {rom['name']}")
                    roms_shown += 1
                
                embed.add_field(
                    name=self.get_platform_with_emoji(platform),
                    value="\n".join(rom_list),
                    inline=True
                )
                field_count += 1
        
        # Add link to RomM
        embed.add_field(
            name="View Collection",
            value=f"[Browse all games]({self.bot.config.DOMAIN})",
            inline=False
        )
        
        if composite_file:
            # Set the embed's image to the attached composite file
            embed.set_image(url="attachment://composite_cover.png")
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
            embed.set_footer(text=f"Batch update • {len(roms)} new games • Use /search to download")
        elif is_bulk:
            embed.set_footer(text=f"Bulk update • {len(roms)} games added")
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        else:
            # Fallback thumbnail if no covers were generated
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
            embed.set_footer(text=f"Batch update • {len(roms)} new games")
        
        # Return the single composite file
        return embed, composite_file 
              
    async def cog_unload(self):
        """Cleanup when cog is unloaded"""
        # Cancel any pending timer
        if self.scan_completion_timer and not self.scan_completion_timer.done():
            self.scan_completion_timer.cancel()
        
        # Disconnect socket
        if self.sio.connected:
            try:
                await self.sio.disconnect()
            except Exception as e:
                logger.error(f"Error disconnecting RecentRomsMonitor socket: {e}")
        
        # Shutdown the socket.IO event loop
        if self.sio_loop and self.sio_loop.is_running():
            self.sio_loop.call_soon_threadsafe(self.sio_loop.stop)
        
        # Clean up IGDB client
        if hasattr(self, 'igdb') and self.igdb:
            self.bot.loop.create_task(self.igdb.close())

def setup(bot):
    bot.add_cog(RecentRomsMonitor(bot))
