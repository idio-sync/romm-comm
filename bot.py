import discord
from discord.ext import commands, tasks
import os
from dotenv import load_dotenv
import aiohttp
import asyncio
from datetime import datetime
import sys
from typing import Dict, Optional, Any, List
import logging
from collections import defaultdict
import time
import re

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('romm_bot')

# Filter out Discord's connection messages
logging.getLogger('discord').setLevel(logging.WARNING)

# Load environment variables from .env file
load_dotenv()

class APICache:
    """Cache manager for API data with TTL."""
    def __init__(self, ttl_seconds: int = 3600):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = ttl_seconds
        self.last_fetch: Dict[str, float] = defaultdict(float)

    def is_fresh(self, endpoint: str) -> bool:
        """Check if cached data is still fresh."""
        return time.time() - self.last_fetch.get(endpoint, 0) < self.ttl

    def get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """Get cached data if fresh."""
        return self.cache.get(endpoint) if self.is_fresh(endpoint) else None

    def set(self, endpoint: str, data: Dict[str, Any]):
        """Set cache data with current timestamp."""
        self.cache[endpoint] = data
        self.last_fetch[endpoint] = time.time()

class RateLimit:
    """Rate limit manager for Discord API calls."""
    def __init__(self, calls_per_minute: int = 30):
        self.calls_per_minute = calls_per_minute
        self.calls: list[float] = []

    async def acquire(self):
        """Wait if necessary to respect rate limits."""
        now = time.time()
        self.calls = [t for t in self.calls if now - t < 60]  # Clean old calls
        
        if len(self.calls) >= self.calls_per_minute:
            wait_time = 60 - (now - self.calls[0])
            if wait_time > 0:
                await asyncio.sleep(wait_time)
        
        self.calls.append(now)

class Config:
    """Configuration manager with validation."""
    def __init__(self):
        self.TOKEN = os.getenv('TOKEN')
        self.GUILD_ID = os.getenv('GUILD')
        self.CHANNEL_ID = os.getenv('CHANNEL_ID')
        self.API_BASE_URL = os.getenv('API_URL', '').rstrip('/')
        self.DOMAIN = os.getenv('DOMAIN', 'No website configured')
        self.SYNC_RATE = int(os.getenv('SYNC_RATE', 3600))
        self.UPDATE_VOICE_NAMES = os.getenv('UPDATE_VOICE_NAMES', 'true').lower() == 'true'
        self.SHOW_API_SUCCESS = os.getenv('SHOW_API_SUCCESS', 'false').lower() == 'true'
        self.CACHE_TTL = int(os.getenv('CACHE_TTL', 300))  # 5 minutes default
        self.API_TIMEOUT = int(os.getenv('API_TIMEOUT', 10))  # 10 seconds default
        self.USER = os.getenv('USER')
        self.PASS = os.getenv('PASS')
        
        self.validate()

    def validate(self):
        """Validate configuration values."""
        required = {'TOKEN', 'GUILD_ID', 'API_BASE_URL', 'USER', 'PASS'}
        missing = [k for k, v in vars(self).items() if k in required and not v]
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        
        try:
            self.GUILD_ID = int(self.GUILD_ID)
            self.CHANNEL_ID = int(self.CHANNEL_ID)
        except ValueError:
            raise ValueError("GUILD_ID and CHANNEL_ID must be numeric values")

def sanitize_platform_data(raw_data: List[Dict]) -> List[Dict]:
    """Sanitize and format the platforms endpoint data."""
    try:
        # Extract relevant platform information and sort alphabetically
        platforms = [
            {
                "id": platform.get("id", 0),  # Added ID field
                "name": platform.get("name", "Unknown Platform"),
                "rom_count": platform.get("rom_count", 0)
            }
            for platform in raw_data
            if isinstance(platform, dict) and platform.get("name") and platform.get("rom_count")
        ]
        
        # Sort alphabetically by platform name
        return sorted(platforms, key=lambda x: x["name"].lower())
    except Exception as e:
        logger.error(f"Error sanitizing platform data: {e}")
        return []

class RommBot(discord.Bot):
    """Extended Discord bot with additional functionality."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.config = Config()
        self.cache = APICache(self.config.CACHE_TTL)
        self.rate_limiter = RateLimit()
        self.stat_channels: Dict[str, discord.VoiceChannel] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

    async def ensure_session(self):
        """Ensure an active session exists."""
        async with self._session_lock:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.config.API_TIMEOUT)
                )
            return self.session

    async def fetch_api_endpoint(self, endpoint: str) -> Optional[Dict]:
        """Fetch data from API with caching and error handling."""
        # Check cache first
        cached_data = self.cache.get(endpoint)
        if cached_data:
            return cached_data

        try:
            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/{endpoint}"

            # Basic authentication
            username = self.config.USER
            password = self.config.PASS
            auth = aiohttp.BasicAuth(username, password)
            
            async with session.get(url, auth=auth) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        if data:  # Ensure we have valid data
                            self.cache.set(endpoint, data)
                            return data
                        logger.warning(f"Empty response from API for endpoint {endpoint}")
                    except Exception as e:
                        logger.error(f"Error parsing JSON from {endpoint}: {e}")
                else:
                    logger.warning(f"API returned status {response.status} for endpoint {endpoint}")
                return None
        except asyncio.TimeoutError:
            logger.error(f"Timeout while fetching {endpoint}")
        except Exception as e:
            logger.error(f"Error fetching {endpoint}: {e}")
        return None

    async def get_or_create_category(self, guild: discord.Guild, category_name: str) -> discord.CategoryChannel:
        """Get existing category or create new one if it doesn't exist."""
        # Look for existing category
        category = discord.utils.get(guild.categories, name=category_name)
        
        if not category:
            # Create new category if it doesn't exist
            await self.rate_limiter.acquire()
            try:
                category = await guild.create_category(name=category_name)
                logger.info(f"Created new category: {category_name}")
            except discord.Forbidden:
                logger.error("Bot lacks permissions to create category")
                raise
            except Exception as e:
                logger.error(f"Error creating category: {e}")
                raise
                
        return category

    async def update_voice_channel(self, channel: discord.VoiceChannel, new_name: str):
        """Update voice channel name with rate limiting."""
        if channel.name != new_name:
            await self.rate_limiter.acquire()
            try:
                await channel.edit(name=new_name)
                logger.info(f"Updated channel name to: {new_name}")
            except discord.Forbidden:
                logger.error("Bot lacks permissions to edit channel")
                raise
            except Exception as e:
                logger.error(f"Error updating channel: {e}")
                raise

    async def setup_hook(self):
        """Initialize bot resources."""
        await self.ensure_session()

    async def close(self):
        """Cleanup resources on shutdown."""
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

# Initialize bot with optimized configurations
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = RommBot(intents=intents)

# Stat type to emoji mapping
STAT_EMOJIS = {
    "Platforms": "🎮", "Roms": "👾", "Saves": "📁", 
    "States": "⏸", "Screenshots": "📸", "Storage Size": "💾"
}

def bytes_to_tb(bytes_value: int) -> float:
    """Convert bytes to terabytes with 2 decimal places."""
    return round(bytes_value / (1024 ** 4), 2)

def sanitize_stats_data(raw_data: Dict) -> Optional[Dict]:
    """Sanitize and format the stats endpoint data."""
    try:
        return {
            "Platforms": raw_data.get('PLATFORMS', 0),
            "Roms": raw_data.get('ROMS', 0),
            "Saves": raw_data.get('SAVES', 0),
            "States": raw_data.get('STATES', 0),
            "Screenshots": raw_data.get('SCREENSHOTS', 0),
            "Storage Size": bytes_to_tb(raw_data.get('FILESIZE', 0))
        }
    except Exception as e:
        logger.error(f"Error sanitizing stats data: {e}")
        return None

async def update_presence(bot: RommBot, status: bool):
    """Update bot's presence with rate limiting."""
    try:
        await bot.rate_limiter.acquire()
        if status and 'stats' in bot.cache.cache:
            stats_data = bot.cache.cache['stats']
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name=f"{stats_data['Roms']:,} games 🕹"
                ),
                status=discord.Status.online
            )
        else:
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name="0 games ⚠️Check Romm connection⚠️"
                ),
                status=discord.Status.do_not_disturb
            )
    except Exception as e:
        logger.error(f"Failed to update presence: {e}")

async def update_stat_channels(bot: RommBot, guild: discord.Guild):
    """Update stat channels with optimized rate limiting and caching."""
    if not bot.config.UPDATE_VOICE_NAMES:
        return

    stats_data = bot.cache.get('stats')
    if not stats_data:
        return

    try:
        category = await bot.get_or_create_category(guild, "Rom Server Stats")
        
        # Get existing channels efficiently
        existing_channels = {
            channel.name: channel 
            for channel in category.voice_channels
        }

        # Track channels to keep
        channels_to_keep = set()

        # Update or create channels
        for stat, value in stats_data.items():
            emoji = STAT_EMOJIS.get(stat, "📊")
            new_name = (f"{emoji} {stat}: {value:,} TB" if stat == "Storage Size" 
                       else f"{emoji} {stat}: {value:,}")
            
            # Find existing channel
            existing_channel = discord.utils.get(
                category.voice_channels,
                name__startswith=f"{emoji} {stat}:"
            )

            if existing_channel:
                if existing_channel.name != new_name:
                    await bot.update_voice_channel(existing_channel, new_name)
                bot.stat_channels[stat] = existing_channel
                channels_to_keep.add(existing_channel.id)
            else:
                await bot.rate_limiter.acquire()
                bot.stat_channels[stat] = await category.create_voice_channel(
                    name=new_name,
                    user_limit=0
                )
                channels_to_keep.add(bot.stat_channels[stat].id)

        # Clean up old channels
        for channel in category.voice_channels:
            if channel.id not in channels_to_keep:
                await bot.rate_limiter.acquire()
                await channel.delete()

    except Exception as e:
        logger.error(f"Error updating stat channels: {e}")

@tasks.loop(seconds=bot.config.SYNC_RATE)
async def update_api_data():
    """Periodic API data update task with error handling."""
    try:
        raw_stats = await bot.fetch_api_endpoint('stats')
        success = False
        
        if raw_stats is not None:  # Explicit check for None
            sanitized_stats = sanitize_stats_data(raw_stats)
            if sanitized_stats:
                bot.cache.set('stats', sanitized_stats)
                success = True
                logger.info("Successfully updated stats data")
            else:
                logger.warning("Failed to sanitize stats data")
        else:
            logger.warning("Failed to fetch stats data")

        raw_platforms = await bot.fetch_api_endpoint('platforms')
        success = False

        if raw_platforms is not None:  # Explicit check for None
            sanitized_platforms = sanitize_platform_data(raw_platforms)
            if sanitized_platforms:
                bot.cache.set('platforms', sanitized_platforms)
                success = True
                logger.info("Successfully updated platforms data")
            else:
                logger.warning("Failed to sanitize platforms data")
        else:
            logger.warning("Failed to fetch platforms data")

        await update_presence(bot, success)
        
        guild = bot.get_guild(bot.config.GUILD_ID)
        if guild and success:
            await update_stat_channels(bot, guild)
        
        if bot.config.SHOW_API_SUCCESS:
            channel = bot.get_channel(bot.config.CHANNEL_ID)
            if channel:
                status_message = (
                    f"✅ API data successfully updated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    if success else "❌ Failed to update API data"
                )
                await channel.send(status_message)
    except Exception as e:
        logger.error(f"Error in update task: {e}", exc_info=True)

@bot.event
async def on_ready():
    """Bot ready event handler."""
    logger.info(f'{bot.user} has connected to Discord!')
    update_api_data.start()

@bot.slash_command(name="refresh", description="Manually update API data")
async def refresh(ctx: discord.ApplicationContext):
    """Manual refresh command with proper error handling."""
    await ctx.defer()
    try:
        raw_stats = await bot.fetch_api_endpoint('stats')
        success = False
        
        if raw_stats is not None:  # Explicit check for None
            sanitized_stats = sanitize_stats_data(raw_stats)
            if sanitized_stats:
                bot.cache.set('stats', sanitized_stats)
                success = True
                logger.info("Successfully manually updated stats data")
            else:
                logger.warning("Failed to sanitize stats data")
        else:
            logger.warning("Failed to manually fetch stats data")

        raw_platforms = await bot.fetch_api_endpoint('platforms')
        success = False

        if raw_platforms is not None:  # Explicit check for None
            sanitized_platforms = sanitize_platform_data(raw_platforms)
            if sanitized_platforms:
                bot.cache.set('platforms', sanitized_platforms)
                success = True
                logger.info("Successfully manually updated platforms data")
            else:
                logger.warning("Failed to sanitize platforms data")
        else:
            logger.warning("Failed to manually fetch platforms data")

        await update_presence(bot, success)
        if success:
            await update_stat_channels(bot, ctx.guild)
        
        await ctx.respond(
            "✅ API data manually updated" if success else "❌ Failed to manually update API data"
        )
    except Exception as e:
        logger.error(f"Error in refresh command: {e}", exc_info=True)
        await ctx.respond("❌ An error occurred while manually updating data")

@bot.slash_command(name="website", description="Get the website URL")
async def website(ctx: discord.ApplicationContext):
    """Website information command."""
    await ctx.respond(
        embed=discord.Embed(
            title="Website Information",
            description=bot.config.DOMAIN,
            color=discord.Color.blue()
        )
    )

@bot.slash_command(name="stats", description="Display current API stats")
async def stats(ctx: discord.ApplicationContext):
    """Stats display command with cache usage."""
    try:
        stats_data = bot.cache.get('stats')
        if stats_data:
            last_fetch_time = bot.cache.last_fetch.get('stats')
            if last_fetch_time:
                time_str = datetime.fromtimestamp(last_fetch_time).strftime('%Y-%m-%d %H:%M:%S')
            else:
                time_str = "Unknown"
                
            embed = discord.Embed(
                title="Collection Stats",
                description=f"Last updated: {time_str}",
                color=discord.Color.blue()
            )
            
            for stat, value in stats_data.items():
                emoji = STAT_EMOJIS.get(stat, "📊")
                field_value = f"{value:,} TB" if stat == "Storage Size" else f"{value:,}"
                embed.add_field(name=f"{emoji} {stat}", value=field_value, inline=False)
            
            await ctx.respond(embed=embed)
        else:
            await ctx.respond("No API data available yet. Try using /refresh first!")
    except Exception as e:
        logger.error(f"Error in stats command: {e}", exc_info=True)
        await ctx.respond("❌ An error occurred while fetching stats")

@bot.slash_command(name="platforms", description="Display all platforms w/ROM counts")
async def platforms(ctx: discord.ApplicationContext):
    """Platforms display command with cache usage."""
    try:
        # Defer the response since it might take a moment to fetch
        await ctx.defer()
        
        # Fetch platforms data
        raw_platforms = await bot.fetch_api_endpoint('platforms')
        if raw_platforms:
            platforms_data = sanitize_platform_data(raw_platforms)
            
            if platforms_data:
                # Create embed with platform information
                embed = discord.Embed(
                    title="Available Platforms w/ROM counts",
                    description="",
                    color=discord.Color.blue()
                )
                
                # Split into multiple fields if needed (Discord has a 25 field limit)
                field_content = ""
                for platform in platforms_data:
                    platform_line = f"**{platform['name']}**: {platform['rom_count']:,} ROMs\n"
                    
                    # If adding this line would exceed Discord's limit, create a new field
                    if len(field_content) + len(platform_line) > 1024:
                        embed.add_field(
                            name="", 
                            value=field_content, 
                            inline=False
                        )
                        field_content = platform_line
                    else:
                        field_content += platform_line
                
                # Add any remaining content
                if field_content:
                    embed.add_field(
                        name="", 
                        value=field_content, 
                        inline=False
                    )
                
                # Add total at the bottom
                total_roms = sum(platform['rom_count'] for platform in platforms_data)
                embed.set_footer(text=f"Total ROMs across all platforms: {total_roms:,}")
                
                await ctx.respond(embed=embed)
            else:
                await ctx.respond("No platform data available!")
        else:
            await ctx.respond("Failed to fetch platform data. Please try again later.")
            
    except Exception as e:
        logger.error(f"Error in platforms command: {e}", exc_info=True)
        await ctx.respond("❌ An error occurred while fetching platform data")

async def get_platform_names(ctx: discord.AutocompleteContext):
    """Autocomplete function for platform names."""
    try:
        # Get platforms from cache first
        platforms_data = bot.cache.get('platforms')
        
        # If not in cache, fetch from API
        if not platforms_data:
            raw_platforms = await bot.fetch_api_endpoint('platforms')
            if raw_platforms:
                platforms_data = sanitize_platform_data(raw_platforms)
        
        if platforms_data:
            # Get all platform names
            platform_names = [p.get('name', '') for p in platforms_data if p.get('name')]
            
            # Filter based on user input
            user_input = ctx.value.lower()
            return [name for name in platform_names if user_input in name.lower()][:25]
    except Exception as e:
        logger.error(f"Error in platform autocomplete: {e}")
    
    return []

@bot.slash_command(name="search", description="Search for a ROM")
async def search(
    ctx: discord.ApplicationContext,
    platform: discord.Option(
        str, 
        "Platform to search in", 
        required=True,
        autocomplete=get_platform_names
    ),
    game: discord.Option(str, "Game name to search for", required=True)
):
    """Search for a ROM and provide download options."""
    await ctx.defer()  # Defer response since the search might take time
    
    try:
        # Fetch platform data
        raw_platforms = await bot.fetch_api_endpoint('platforms')
        if not raw_platforms:
            await ctx.respond("❌ Unable to fetch platforms data")
            return
            
        platform_id = None
        sanitized_platforms = sanitize_platform_data(raw_platforms)
        
        # Find matching platform (case-insensitive)
        for p in sanitized_platforms:
            if p['name'].lower() == platform.lower():
                platform_id = p['id']
                break
                
        if not platform_id:
            platforms_list = "\n".join(f"• {name}" for name in sorted([p['name'] for p in sanitized_platforms]))
            await ctx.respond(
                f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}"
            )
            return

        # Clean and prepare the search term to be more flexible
        search_term = game.strip()  # Remove leading/trailing spaces
        
        # Try exact search first
        search_results = await bot.fetch_api_endpoint(f'roms?platform_id={platform_id}&search_term={search_term}&limit=25')
        
        # If no results, try with each word separately
        if not search_results or len(search_results) == 0:
            # Split search term into words and filter out common words
            search_words = search_term.split()
            if len(search_words) > 1:
                # Try searching with just the significant words
                search_term = ' '.join(search_words)
                search_results = await bot.fetch_api_endpoint(f'roms?platform_id={platform_id}&search_term={search_term}&limit=25')

        if not search_results or not isinstance(search_results, list) or len(search_results) == 0:
            await ctx.respond(f"No ROMs found matching '{game}' for platform '{platform}'")
            return

        # Custom sorting function
        def sort_roms(rom):
            game_name = rom['name'].lower()
            filename = rom.get('file_name', '').upper()

            if game_name.startswith("the "):
                game_name = game_name[4:]
    
            if "(USA)" in filename or "(USA, WORLD)" in filename:
                if "BETA" in filename or "(PROTOTYPE)" in filename:
                    file_priority = 2
                else:
                    file_priority = 0
            elif "(WORLD)" in filename:
                if "BETA" in filename or "(PROTOTYPE)" in filename:
                    file_priority = 3
                else:
                    file_priority = 1
            elif "BETA" in filename or "(PROTOTYPE)" in filename:
                file_priority = 5
            elif "(DEMO)" in filename or "PROMOTIONAL" in filename or "SAMPLE" in filename or "SAMPLER" in filename:
                if "BETA" in filename or "(PROTOTYPE)" in filename:
                    file_priority = 6
                else:
                    file_priority = 4
            else:
                file_priority = 3
    
            return (game_name, file_priority, filename.lower())

        search_results.sort(key=sort_roms)

        def format_file_size(size_bytes):
            if not size_bytes or not isinstance(size_bytes, (int, float)):
                return "Unknown size"
            
            units = ['B', 'KB', 'MB', 'GB', 'TB']
            size_value = float(size_bytes)
            unit_index = 0
            while size_value >= 1024 and unit_index < len(units) - 1:
                size_value /= 1024
                unit_index += 1
            return f"{size_value:.2f} {units[unit_index]}"

        # Create selection menu
        options = []
        for rom in search_results[:25]:
            display_name = rom['name'][:75] if len(rom['name']) > 75 else rom['name']
            file_name = rom.get('file_name', 'Unknown filename')
            file_size = format_file_size(rom.get('file_size_bytes'))
            
            truncated_filename = (file_name[:47] + '...') if len(file_name) > 50 else file_name
            
            options.append(
                discord.SelectOption(
                    label=display_name,
                    value=str(rom['id']),
                    description=f"{truncated_filename} ({file_size})"
                )
            )

        class ROM_View(discord.ui.View):
            def __init__(self, search_results, author_id, platform_id, initial_message=None):
                super().__init__()
                self.search_results = search_results
                self.author_id = author_id
                self.platform_id = platform_id
                self.message = initial_message

            @discord.ui.select(
                placeholder="Choose a ROM to download",
                options=options,
                custom_id="rom_select"
            )
            async def select_callback(self, select: discord.ui.Select, interaction: discord.Interaction):
                if interaction.user.id != self.author_id:
                    await interaction.response.send_message("This selection menu isn't for you!", ephemeral=True)
                    return

                # Defer the update to prevent timeout
                await interaction.response.defer()

                selected_rom_id = int(select.values[0])
                selected_rom = next((rom for rom in self.search_results if rom['id'] == selected_rom_id), None)

                if selected_rom:
                    try:
                        detailed_rom = await bot.fetch_api_endpoint(f'roms/{selected_rom_id}')
                        if detailed_rom:
                            selected_rom.update(detailed_rom)
                    except Exception as e:
                        logger.error(f"Error fetching detailed ROM data: {e}")

                    # Generate links
                    file_name = selected_rom.get('file_name', 'unknown_file').replace(' ', '%20')
                    download_url = f"{bot.config.DOMAIN}/api/roms/{selected_rom['id']}/content/{file_name}"
                    igdb_name = selected_rom['name'].lower().replace(' ', '-')
                    igdb_name = re.sub(r'[^a-z0-9-]', '', igdb_name)
                    igdb_url = f"https://www.igdb.com/games/{igdb_name}"
                    romm_url = f"{bot.config.DOMAIN}/rom/{selected_rom['id']}"
                    logo_url = "https://raw.githubusercontent.com/rommapp/romm/release/.github/resources/romm_complete.png"
                    links_info = f"| [RomM]({romm_url})  |  [IGDB]({igdb_url})"
                                        
                    # Create embed
                    embed = discord.Embed(
                        title=f"{selected_rom['name']}",
                        color=discord.Color.green()
                    )
             
                    # Set romm logo as thumbnail
                    embed.set_thumbnail(url=logo_url)
                    
                    # Add cover image
                    if cover_url := selected_rom.get('url_cover'):
                        embed.set_image(url=cover_url)
                    
                    # Basic information
                    # Platform
                    embed.add_field(name="Platform", value=platform, inline=True)
                    
                    # Genres
                    if genres := selected_rom.get('genres'):
                        genre_list = ", ".join(genres) if isinstance(genres, list) else genres
                        embed.add_field(name="Genres", value=genre_list, inline=True)
                    
                    # Release Date (formatted as MMM DD, YYYY from Unix timestamp)
                    if release_date := selected_rom.get('first_release_date'):
                        try:
                            release_datetime = datetime.fromtimestamp(int(release_date))
                            formatted_date = release_datetime.strftime('%b %d, %Y')
                            embed.add_field(name="Release Date", value=formatted_date, inline=True)
                        except (ValueError, TypeError) as e:
                            logger.error(f"Error formatting date: {e}")                  

                    # Total Rating (Doesn't pull over romm api?)
                    # if total_rating := selected_rom.get('total_rating'):
                        # embed.add_field(name="Rating", value=f"{total_rating:.1f}/100", inline=True)
                                                      
                    # Summary (truncated at 200 characters)
                    if summary := selected_rom.get('summary'):
                        if len(summary) > 240:
                            summary = summary[:237] + "..."
                        embed.add_field(name="Summary", value=summary, inline=False)
                    
                    # Companies
                    if companies := selected_rom.get('companies'):
                        if isinstance(companies, list):
                            companies_str = ", ".join(companies)
                        else:
                            companies_str = str(companies)
                        if companies_str:
                            embed.add_field(name="Companies", value=companies_str, inline=False)
                    
                    # Download link with size
                    file_size = format_file_size(selected_rom.get('file_size_bytes'))
                    embed.add_field(
                        name=f"Download ({file_size})",
                        value=f"[{selected_rom.get('file_name', 'Download')}]({download_url})",
                        inline=False
                    )
                    
                    # Verification hashes
                    hashes_lines = []
                    # Format CRC and MD5 on first line
                    first_line_parts = []
                    if crc := selected_rom.get('crc_hash'):
                        first_line_parts.append(f"**CRC:** {crc}")
                    if md5 := selected_rom.get('md5_hash'):
                        first_line_parts.append(f"**MD5:** {md5}")
                    if first_line_parts:
                        hashes_lines.append(" • ".join(first_line_parts))

                    # Add SHA1 on second line
                    if sha1 := selected_rom.get('sha1_hash'):
                        hashes_lines.append(f"**SHA1:** {sha1}")

                    if hashes_lines:
                        embed.add_field(
                            name="Hash Values",
                            value="\n".join(hashes_lines),
                            inline=False
                        )

                    # Update the original message with new embed
                    await interaction.message.edit(
                        content=interaction.message.content,
                        embed=embed,
                        view=self
                    )
                else:
                    await interaction.followup.send("❌ Error retrieving ROM details", ephemeral=True)

        # Create initial message content
        if len(search_results) >= 25:
            initial_content = (
                f"Found 25+ ROMs matching '{game}' for platform '{platform}'. Showing first 25 results.\n"
                f"Please refine your search terms for more specific results:"
            )
        else:
            initial_content = f"Found {len(options)} ROMs matching '{game}' for platform '{platform}':"

        # Send initial message and store it
        initial_message = await ctx.respond(
            initial_content,
            view=ROM_View(search_results, ctx.author.id, platform_id, None)
        )

        # Update the view with the initial message
        view = ROM_View(search_results, ctx.author.id, platform_id, initial_message)
        await initial_message.edit(view=view)

    except Exception as e:
        logger.error(f"Error in search command: {e}", exc_info=True)
        await ctx.respond("❌ An error occurred while searching for ROMs")

@bot.slash_command(name="firmware", description="List firmware files for a platform")
async def firmware(
    ctx: discord.ApplicationContext,
            platform: discord.Option(
                str, 
                "Platform to list firmware for", 
                required=True,
        autocomplete=get_platform_names
    )
):
    """List firmware files for a specific platform."""
    await ctx.defer()
    
    try:
        # Fetch from API
        raw_platforms = await bot.fetch_api_endpoint('platforms')
        if not raw_platforms:
            await ctx.respond("Failed to fetch platforms data")
            return
            
        # Find matching platform (case-insensitive)
        platform_data = None
        for p in raw_platforms:
            if platform.lower() in p.get('name', '').lower():
                platform_data = p
                break

        if not platform_data:
            platforms_list = "\n".join(f"• {p.get('name', 'Unknown')}" 
                                     for p in raw_platforms)
            await ctx.respond(
                f"Platform '{platform}' not found. Available platforms:\n{platforms_list}"
            )
            return

        # Fetch firmware for the platform
        firmware_data = await bot.fetch_api_endpoint(f'firmware?platform_id={platform_data["id"]}')
        
        if not firmware_data:
            await ctx.respond(f"No firmware files found for platform '{platform_data.get('name', platform)}'")
            return

        def format_file_size(size_bytes):
            if not isinstance(size_bytes, (int, float)):
                return "Unknown size"
            units = ['B', 'KB', 'MB', 'GB', 'TB']
            size = float(size_bytes)
            unit_index = 0
            while size >= 1024 and unit_index < len(units) - 1:
                size /= 1024
                unit_index += 1
            return f"{size:.2f} {units[unit_index]}"

        # Create paginated embeds for firmware files
        embeds = []
        current_embed = discord.Embed(
            title=f"Firmware Files for {platform_data.get('name', platform)}",
            description=f"Found {len(firmware_data)} firmware file(s)",
            color=discord.Color.blue()
        )
        field_count = 0

        for firmware in firmware_data:
            # Generate download URL
            file_name = firmware.get('file_name', 'unknown_file').replace(' ', '%20')
            download_url = f"{bot.config.DOMAIN}/api/firmware/{firmware.get('id')}/content/{file_name}"
            
            field_value = (
                f"**Size:** {format_file_size(firmware.get('file_size_bytes'))}\n"
                f"**CRC:** {firmware.get('crc_hash', 'N/A')}\n"
                f"**MD5:** {firmware.get('md5_hash', 'N/A')}\n"
                f"**SHA1:** {firmware.get('sha1_hash', 'N/A')}\n"
                f"**Download:** [Link]({download_url})"
            )
            
            if field_count >= 25:
                embeds.append(current_embed)
                current_embed = discord.Embed(
                    title=f"Firmware Files for {platform_data.get('name', platform)} (Continued)",
                    color=discord.Color.blue()
                )
                field_count = 0
            
            current_embed.add_field(
                name=firmware.get('file_name', 'Unknown Firmware'),
                value=field_value,
                inline=False
            )
            field_count += 1

        if field_count > 0:
            embeds.append(current_embed)

        if len(embeds) > 1:
            for i, embed in enumerate(embeds):
                embed.set_footer(text=f"Page {i+1} of {len(embeds)}")

        for embed in embeds:
            await ctx.respond(embed=embed)
            
    except Exception as e:
        logger.error(f"Error in firmware command: {e}")
        await ctx.respond("❌ An error occurred while fetching firmware data")
        
if __name__ == "__main__":
    bot.run(bot.config.TOKEN)
