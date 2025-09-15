import discord
from discord.ext import commands, tasks
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Union
import json
import os
from pathlib import Path
import asyncio
from collections import defaultdict
import aiosqlite

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class RecentRomsMonitor(commands.Cog):
    """Automatically monitor and post recently added ROMs to Discord"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
        
        # Configuration (convert minutes to seconds for internal use)
        self.recent_roms_channel_id = int(os.getenv('RECENT_ROMS_CHANNEL_ID', str(bot.config.CHANNEL_ID)))
        self.check_interval_minutes = int(os.getenv('RECENT_ROMS_CHECK_MINUTES', '5'))
        self.check_interval = self.check_interval_minutes * 60
        self.batch_window_minutes = float(os.getenv('RECENT_ROMS_BATCH_MINUTES', '1'))
        self.batch_window = self.batch_window_minutes * 60
        self.max_roms_per_post = int(os.getenv('RECENT_ROMS_MAX_PER_POST', '10'))
        self.flood_threshold = int(os.getenv('RECENT_ROMS_FLOOD_THRESHOLD', '25'))
        self.enabled = os.getenv('RECENT_ROMS_ENABLED', 'TRUE').upper() == 'TRUE'
        
        # State management
        self.data_dir = Path('data')
        self.data_dir.mkdir(exist_ok=True)
        self.db_path = self.data_dir / 'recent_roms.db'
        self.pending_batch = []
        self.batch_timer = None
        
        # IGDB client
        self.igdb = None
        
        # Start monitoring
        if self.enabled:
            bot.loop.create_task(self.setup())
            logger.info(f"Recent ROMs monitor enabled - checking every {self.check_interval_minutes} minutes")
            logger.debug(f"Flood protection: Will summarize if {self.flood_threshold}+ ROMs detected")
        else:
            logger.info("Recent ROMs monitor disabled via configuration")
    
    async def setup(self):
        """Setup database and IGDB client"""
        await self.setup_database()
        await self.initialize_igdb()
        
        # Wait for bot to be ready before starting the loop
        await self.bot.wait_until_ready()
        
        # Start the monitoring loop
        if self.enabled:
            self.check_recent_roms.start()
    
    async def initialize_igdb(self):
        """Initialize IGDB client if available"""
        try:
            from .igdb_client import IGDBClient
            self.igdb = IGDBClient()
            logger.debug("IGDB client initialized for recent ROMs")
        except Exception as e:
            logger.warning(f"IGDB integration not available: {e}")
            self.igdb = None
    
    async def setup_database(self):
        """Create database for tracking posted ROMs"""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS posted_roms (
                    rom_id INTEGER PRIMARY KEY,
                    platform_name TEXT,
                    rom_name TEXT,
                    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    batch_id TEXT
                )
            ''')
            await db.commit()
    
    async def has_been_posted(self, rom_id: int) -> bool:
        """Check if a ROM has already been posted"""
        async with aiosqlite.connect(str(self.db_path)) as db:
            cursor = await db.execute(
                "SELECT 1 FROM posted_roms WHERE rom_id = ?",
                (rom_id,)
            )
            result = await cursor.fetchone()
            return result is not None
    
    async def mark_as_posted(self, roms: List[Dict], batch_id: str = None):
        """Mark ROMs as posted to prevent duplicates"""
        async with aiosqlite.connect(str(self.db_path)) as db:
            for rom in roms:
                await db.execute(
                    "INSERT OR IGNORE INTO posted_roms (rom_id, platform_name, rom_name, batch_id) VALUES (?, ?, ?, ?)",
                    (rom['id'], rom.get('platform_name', 'Unknown'), rom['name'], batch_id)
                )
            await db.commit()
    
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
    
    async def get_recent_roms(self) -> List[Dict]:
        """Fetch recently added ROMs from the API"""
        try:
            # Fetch recent ROMs
            limit = min(50, self.flood_threshold * 2)  # Cap at reasonable limit
            logger.debug(f"Fetching recent ROMs with limit={limit}, bypass_cache=True")
            recent_roms = await self.bot.fetch_api_endpoint(
                f'roms?order_by=created_at&order_dir=desc&limit={limit}',
                bypass_cache=True  # Always get fresh data
            )
            
            if not recent_roms:
                return []
            
            # Handle paginated response
            if isinstance(recent_roms, dict) and 'items' in recent_roms:
                recent_roms = recent_roms['items']
            
            # Filter out already posted ROMs
            new_roms = []
            platforms_data = None
            
            for rom in recent_roms:
                if not await self.has_been_posted(rom['id']):
                    # Add platform name
                    if platform_id := rom.get('platform_id'):
                        # Try cached platforms first, fetch if not available
                        if not platforms_data:
                            platforms_data = self.bot.cache.get('platforms')
                            if not platforms_data:
                                # Cache miss - fetch fresh data
                                platforms_data = await self.bot.fetch_api_endpoint('platforms')
                        
                        if platforms_data:
                            for p in platforms_data:
                                if p.get('id') == platform_id:
                                    rom['platform_name'] = p.get('name', 'Unknown')
                                    break
                    new_roms.append(rom)
                    
                    # Stop if we hit flood threshold
                    if len(new_roms) >= self.flood_threshold:
                        logger.warning(f"Flood protection: Found {self.flood_threshold}+ new ROMs, limiting to threshold")
                        break
            
            return new_roms
            
        except Exception as e:
            logger.error(f"Error fetching recent ROMs: {e}")
            return []
    
    async def get_igdb_metadata(self, rom_name: str, platform_name: str = None) -> Optional[Dict]:
        """Fetch IGDB metadata for a ROM"""
        if not self.igdb:
            return None
        
        try:
            matches = await self.igdb.search_game(rom_name, platform_name)
            if matches:
                # Return the best match
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
    
    async def create_single_rom_embed(self, rom: Dict) -> discord.Embed:
        """Create a detailed embed for a single ROM with IGDB metadata"""
        platform_name = rom.get('platform_name', 'Unknown')
        
        # Fetch detailed ROM data to ensure we have file size info
        if not rom.get('fs_size_bytes') and not rom.get('files'):
            try:
                detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom["id"]}', bypass_cache=True)
                if detailed_rom:
                    rom.update(detailed_rom)
            except Exception as e:
                logger.warning(f"Could not fetch detailed ROM data: {e}")
        
        # Try to get IGDB metadata
        igdb_data = await self.get_igdb_metadata(rom['name'], platform_name)
        
        # Create embed
        embed = discord.Embed(
            title=f"🆕 {rom['name']}",
            color=discord.Color.green()
        )

        platform_text = self.get_platform_with_emoji(platform_name)

        size_text = "Unknown"
        if rom.get("fs_size_bytes"):
            size_text = self.format_file_size(rom["fs_size_bytes"])

        # First Row: Add as two inline fields with spacer between
        embed.add_field(
            name="Platform",
            value=platform_text,
            inline=True
        )
        
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        
        embed.add_field(
            name="Size",
            value=size_text,
            inline=True
        )
        
        # Second row: Release Date | Developer (only if IGDB data exists)
        if igdb_data:
            release_text = igdb_data.get('release_date', "Unknown")
            companies = []
            if igdb_data.get('developers'):
                companies.extend(igdb_data['developers'][:2])
            elif igdb_data.get('publishers'):
                companies.extend(igdb_data['publishers'][:2])
            developer_text = ", ".join(companies) if companies else "Unknown"

            # Only add fields if at least one has meaningful data
            if release_text != "Unknown" or developer_text != "Unknown":
                # Use two separate inline fields for side-by-side display
                if release_text != "Unknown":
                    embed.add_field(
                        name="Release Date",
                        value=release_text,
                        inline=True
                    )
                
                embed.add_field(name="\u200b", value="\u200b", inline=True)
                
                if developer_text != "Unknown":
                    embed.add_field(
                        name="Developer",
                        value=developer_text,
                        inline=True
                    )

        if igdb_data and igdb_data.get('cover_url'):
            embed.set_thumbnail(url=igdb_data['cover_url'])

        romm_url = f"{self.bot.config.DOMAIN}/rom/{rom['id']}"
        embed.add_field(
            name="View in RomM",
            value=f"[Click here]({romm_url})",
            inline=False
        )

        embed.set_footer(text="New ROM added to the collection")

        return embed
    
    async def create_batch_embed(self, roms: List[Dict]) -> discord.Embed:
        """Create a summary embed for multiple ROMs"""
        # Check if this is a flood scenario
        is_flood = len(roms) >= self.flood_threshold
        
        if is_flood:
            # Create a simplified flood notification
            embed = discord.Embed(
                title=f"🌊 Large Collection Update",
                description=f"A large number of ROMs ({len(roms)}+) have been added to the collection!",
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
                name="📝 Note",
                value="This appears to be an initial scan or large import. Individual ROM notifications have been suppressed to prevent spam.",
                inline=False
            )
        else:
            # Normal batch embed for reasonable number of ROMs
            embed = discord.Embed(
                title=f"🆕 {len(roms)} New ROMs Added",
                description="Multiple ROMs have been added to the collection:",
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
            value=f"[Browse all ROMs]({self.bot.config.DOMAIN})",
            inline=False
        )
        
        if is_flood:
            embed.set_footer(text=f"Large import detected • {len(roms)} ROMs")
        else:
            embed.set_footer(text=f"Batch update • {len(roms)} new ROMs")
            
        embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        return embed
    
    async def process_batch(self):
        """Process and post the batched ROMs"""
        if not self.pending_batch:
            return
        
        channel = self.bot.get_channel(self.recent_roms_channel_id)
        if not channel:
            logger.error(f"Recent ROMs channel {self.recent_roms_channel_id} not found")
            return
        
        try:
            batch_id = datetime.utcnow().isoformat()
            is_flood = len(self.pending_batch) >= self.flood_threshold
            
            if len(self.pending_batch) == 1:
                # Single ROM - create detailed embed
                rom = self.pending_batch[0]
                embed = await self.create_single_rom_embed(rom)
                await channel.send(embed=embed)
            else:
                # Multiple ROMs - create summary embed
                embed = await self.create_batch_embed(self.pending_batch)
                await channel.send(embed=embed)
            
            # Mark all as posted
            await self.mark_as_posted(self.pending_batch, batch_id)
            
            # Only dispatch to request system if not a flood scenario
            # (to avoid overwhelming the request fulfillment system)
            if not is_flood:
                requests_cog = self.bot.get_cog('Request')
                if requests_cog:
                    logger.debug(f"Dispatching batch_scan_complete event with {len(self.pending_batch)} ROMs")
                    await self.bot.dispatch('batch_scan_complete', [
                        {
                            'platform': rom.get('platform_name', 'Unknown'),
                            'name': rom['name'],
                            'file_name': rom.get('file_name', '')
                        }
                        for rom in self.pending_batch
                    ])
            else:
                logger.info("Skipping request fulfillment dispatch due to flood protection")
            
            logger.info(f"Posted {len(self.pending_batch)} new ROM(s) to Discord" + 
                       (" (flood mode)" if is_flood else ""))
            
        except Exception as e:
            logger.error(f"Error processing batch: {e}")
        finally:
            # Clear the batch
            self.pending_batch = []
            self.batch_timer = None
    
    @tasks.loop(seconds=300)  # Default 5 minutes, will be updated from config
    async def check_recent_roms(self):
        """Periodically check for new ROMs"""
        try:
            new_roms = await self.get_recent_roms()
            
            if new_roms:
                if len(new_roms) >= self.flood_threshold:
                    logger.info(f"Flood protection activated: Found {len(new_roms)} new ROM(s)")
                else:
                    logger.info(f"Found {len(new_roms)} new ROM(s)")
                
                # Add to pending batch
                self.pending_batch.extend(new_roms)
                
                # Cancel existing timer if any
                if self.batch_timer:
                    self.batch_timer.cancel()
                
                # Start new batch timer
                self.batch_timer = self.bot.loop.create_task(self.batch_timer_callback())
                
        except Exception as e:
            logger.error(f"Error in check_recent_roms: {e}")
    
    async def batch_timer_callback(self):
        """Wait for batch window then process"""
        await asyncio.sleep(self.batch_window)
        await self.process_batch()
    
    @check_recent_roms.before_loop
    async def before_check_recent_roms(self):
        """Set up the loop with config values"""
        await self.bot.wait_until_ready()
        self.check_recent_roms.change_interval(seconds=self.check_interval)
            
    def cog_unload(self):
        """Clean up when cog is unloaded"""
        if hasattr(self, 'check_recent_roms') and self.check_recent_roms.is_running():
            self.check_recent_roms.cancel()
        
        if hasattr(self, 'batch_timer') and self.batch_timer:
            self.batch_timer.cancel()
        
        if hasattr(self, 'igdb') and self.igdb:
            self.bot.loop.create_task(self.igdb.close())

def setup(bot):
    bot.add_cog(RecentRomsMonitor(bot))
