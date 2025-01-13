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
        self.SYNC_RATE = int(os.getenv('SYNC_RATE', 3600)) # 1 hour default
        self.UPDATE_VOICE_NAMES = os.getenv('UPDATE_VOICE_NAMES', 'true').lower() == 'true'
        self.SHOW_API_SUCCESS = os.getenv('SHOW_API_SUCCESS', 'false').lower() == 'true'
        self.CACHE_TTL = int(os.getenv('CACHE_TTL', 3900))  # 65 minutes default
        self.API_TIMEOUT = int(os.getenv('API_TIMEOUT', 10))  # 10 seconds default
        self.USER = os.getenv('USER')
        self.PASS = os.getenv('PASS')
        requests_env = os.getenv('REQUESTS_ENABLED', 'TRUE').upper()
        self.REQUESTS_ENABLED = requests_env == 'TRUE'
        
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

class RommBot(discord.Bot):
    """Extended Discord bot with additional functionality."""
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True
        intents.reactions = True
        intents.dm_messages = True
        intents.dm_reactions = True
        super().__init__(
            command_prefix="!",  # Add a prefix even if you don't use it
            intents=intents,
            application_id=os.getenv('RommBot/1.0')
        )

        # Initialize bot attributes    
        self.config = Config()
        self.cache = APICache(self.config.CACHE_TTL)
        self.rate_limiter = RateLimit()
        # self.stat_channels: Dict[str, discord.VoiceChannel] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        # Add a commands sync flag
        self.synced = False

        # Global cooldown
        self._cd_bucket = commands.CooldownMapping.from_cooldown(1, 60, commands.BucketType.user)
        
        # Register error handler
        self.application_command_error = self.on_application_command_error

    async def on_application_command_error(self, ctx: discord.ApplicationContext, error: discord.DiscordException):
        """Global error handler for all slash commands."""
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.respond(
                f"⌛ This command is on cooldown. Try again in {error.retry_after:.0f} seconds.", 
                ephemeral=True
            )
        elif isinstance(error, commands.MissingPermissions):
            await ctx.respond(
                "❌ You don't have the required permissions to use this command.", 
                ephemeral=True
            )
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.respond(
                "❌ I don't have the required permissions to execute this command.", 
                ephemeral=True
            )
        else:
            logger.error(f"Command error in {ctx.command}: {error}", exc_info=True)
            await ctx.respond(
                "❌ An error occurred while processing this command.", 
                ephemeral=True
            )

    async def before_slash_command_invoke(self, ctx: discord.ApplicationContext):
        """Add cooldown check before any slash command is invoked."""
        if not await self.is_owner(ctx.author):  # Skip cooldown for bot owner
            bucket = self._cd_bucket.get_bucket(ctx.message)
            retry_after = bucket.update_rate_limit()
            if retry_after:
                raise commands.CommandOnCooldown(bucket, retry_after, self._cd_bucket.type)

    def load_all_cogs(self):
        """Load all cogs."""
        cogs_to_load = [
            'cogs.emoji_manager', 
            'cogs.info', 
            'cogs.search', 
            'cogs.scan', 
            'cogs.requests',
            'cogs.user_manager',
            'cogs.download_monitor'
        ]
        
        # Dependencies for each cog
        cog_dependencies = {
            'cogs.emoji_manager': ['aiohttp'],
            'cogs.info': [],
            'cogs.search': ['aiohttp','qrcode'],
            'cogs.scan': ['socketio'],
            'cogs.requests': ['aiosqlite'],
            'cogs.user_manager': ['aiohttp','aiosqlite'],
            'cogs.download_monitor': ['aiosqlite','docker']
        }

        for cog in cogs_to_load:
            try:
                # Check dependencies before loading
                if cog in cog_dependencies:
                    missing_deps = []
                    for dep in cog_dependencies[cog]:
                        try:
                            __import__(dep)
                        except ImportError:
                            missing_deps.append(dep)
                
                    if missing_deps:
                        logger.error(f"Missing dependencies for {cog}: {', '.join(missing_deps)}")
                        logger.error(f"Please install using: pip install {' '.join(missing_deps)}")
                        continue
            
                # Load the cog synchronously
                self.load_extension(cog)
              # logger.info(f"Successfully loaded {cog}")
            except Exception as e:
                logger.error(f"Failed to load extension {cog}", exc_info=True)
                logger.error(f"Error details: {str(e)}")
  
    async def on_ready(self):
        """When bot is ready, start tasks."""
        logger.info(f'{self.user} has connected to Discord!')

        # Load cogs
        self.load_all_cogs()
        loaded_cogs = list(self.cogs.keys())
        logger.info(f"Currently loaded cogs: {loaded_cogs}")

        # Only sync commands once
        if not self.synced:
            try:
                guild = self.get_guild(self.config.GUILD_ID)
                if guild:
                    # Sync to specific guild
                    synced = await self.sync_commands(guild_ids=[guild.id])
                    logger.info(f"Synced commands to guild {guild.name}")
                    self.synced = True
                
                    # Debug info about available commands
                    all_commands = [
                        cmd.name for cmd in self.application_commands
                    ]
                    logger.info(f"Available commands: {all_commands}")
            except Exception as e:
                logger.error(f"Failed to sync commands: {e}", exc_info=True)
        
        # Start update loop if not running
        if not self.update_loop.is_running():
            self.update_loop.start()
                    
    async def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure an active session exists and return it."""
        async with self._session_lock:
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.config.API_TIMEOUT),
                    headers={
                        "User-Agent": f"RommBot/1.0",  # Identify as bot in RomM logs
                        "Accept": "application/json"
                    }
                )
            return self.session

    @tasks.loop(seconds=300)  # Default to 5 minutes, will be updated in before_loop
    async def update_loop(self):
        """Periodic API data update task."""
        await self.update_api_data()

    @update_loop.before_loop
    async def before_update_loop(self):
        """Set up the update loop with config values."""
        await self.wait_until_ready()
        # Update the interval using the config value
        self.update_loop.change_interval(seconds=self.config.SYNC_RATE)
        logger.info("Update loop initialized")

    async def fetch_api_endpoint(self, endpoint: str, bypass_cache: bool = False) -> Optional[Dict]:
        """Fetch data from API with caching and error handling."""
        # Bypass cache if specified
        if not bypass_cache:
            cached_data = self.cache.get(endpoint)
            if cached_data:
                logger.info(f"Returning cached data for {endpoint}")
                return cached_data

        try:
            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/{endpoint}"

            # Basic authentication
            auth = aiohttp.BasicAuth(self.config.USER, self.config.PASS)
        
            async with session.get(url, auth=auth) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                        logger.info(f"Fetched fresh data for {endpoint}")
                        # Store data in cache after fetching fresh data
                        if data:
                            self.cache.set(endpoint, data)
                        return data
                    except Exception as e:
                        logger.error(f"Error parsing JSON from {endpoint}: {e}")
                else:
                    logger.warning(f"API returned status {response.status} for endpoint {endpoint}")
                return None
        except asyncio.TimeoutError:
            logger.error(f"Timeout while fetching {endpoint}")
        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching {endpoint}: {e}")
        except Exception as e:
            logger.error(f"Error fetching {endpoint}: {e}")
        return None

    @staticmethod
    def bytes_to_tb(bytes_value: int) -> float:
        """Convert bytes to terabytes with 2 decimal places."""
        return round(bytes_value / (1024 ** 4), 2)

    def sanitize_data(self, raw_data: Dict, data_type: str) -> Optional[Dict]:
        """Generalized function to sanitize various types of data."""
        try:
            if data_type == 'stats':
                return {
                    "Platforms": raw_data.get('PLATFORMS', 0),
                    "Roms": raw_data.get('ROMS', 0),
                    "Saves": raw_data.get('SAVES', 0),
                    "States": raw_data.get('STATES', 0),
                    "Screenshots": raw_data.get('SCREENSHOTS', 0),
                    "Storage Size": self.bytes_to_tb(raw_data.get('FILESIZE', 0))
                }
        
            elif data_type == 'platforms':
                return [
                    {
                        "id": platform.get("id", 0),
                        "name": platform.get("name", "Unknown Platform"),
                        "rom_count": platform.get("rom_count", 0)
                    }
                    for platform in raw_data if isinstance(platform, dict) and platform.get("name") and platform.get("rom_count")
                ]
        
            elif data_type == 'user_count':
                user_count = raw_data.get('user_count', 0)
                # Validate the user count as a non-negative integer
                if isinstance(user_count, int) and user_count >= 0:
                    return {"user_count": user_count}
                else:
                    logger.warning(f"Invalid user count data: {user_count}")
                    return None
            else:
                logger.warning(f"Unsupported data type for sanitization: {data_type}")
                return None

        except Exception as e:
            logger.error(f"Error sanitizing {data_type} data: {e}")
            return None

    async def close(self):
        """Cleanup resources on shutdown."""
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    async def update_api_data(self):
        """Periodic API data update task with error handling."""
        try:
            # Stats Update
            raw_stats = await self.fetch_api_endpoint('stats', bypass_cache=True)
            stats_success = False
        
            if raw_stats is not None:
                sanitized_stats = self.sanitize_data(raw_stats, 'stats')
                if sanitized_stats:
                    self.cache.set('stats', sanitized_stats)
                    stats_success = True
                    logger.info("Successfully updated stats data")
                else:
                    logger.warning("Failed to sanitize stats data")
            else:
                logger.warning("Failed to fetch stats data")

            # Platforms Update
            raw_platforms = await self.fetch_api_endpoint('platforms', bypass_cache=True)
            platforms_success = False
        
            if raw_platforms is not None:
                sanitized_platforms = self.sanitize_data(raw_platforms, 'platforms')
                if sanitized_platforms:
                    self.cache.set('platforms', sanitized_platforms)
                    platforms_success = True
                    logger.info("Successfully updated platforms data")
                else:
                    logger.warning("Failed to sanitize platforms data")
            else:
                logger.warning("Failed to fetch platforms data")

            # User Count Update
            user_count_success = False
            try:
                users_data = await self.fetch_api_endpoint('users', bypass_cache=True)
                if users_data is not None:
                    user_count_data = {"user_count": len(users_data)}
                    sanitized_user_count = self.sanitize_data(user_count_data, 'user_count')
                    if sanitized_user_count is not None:
                        self.cache.set('user_count', sanitized_user_count)
                        user_count_success = True
                        logger.info(f"Successfully updated user count data: {sanitized_user_count}")
                    else:
                        logger.warning("Failed to sanitize user count data")
                else:
                    logger.warning("Failed to fetch users data")
            except Exception as e:
                logger.error(f"Error fetching user count data: {e}")

            # Update presence based on overall success
            success = stats_success and platforms_success and user_count_success
            info_cog = self.get_cog('Info')
            if info_cog:
                await info_cog.update_presence(success)
            else:
                logger.error("Info cog not found when trying to update presence")
        
            # Update stat channels if stats were updated
            guild = self.get_guild(self.config.GUILD_ID)
            if guild and success:
                if info_cog:
                    await info_cog.update_stat_channels(guild)
                else:
                    logger.error("Info cog not found when trying to update stat channels")
        
            if self.config.SHOW_API_SUCCESS:
                channel = self.get_channel(self.config.CHANNEL_ID)
                if channel:
                    status_message = (
                        f"✅ API data successfully updated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        if success else "❌ Failed to update API data"
                    )
                    await channel.send(status_message)

        except Exception as e:
            logger.error(f"Error in update task: {e}", exc_info=True)

async def main():
    bot = RommBot()
    try:
        await bot.start(bot.config.TOKEN)
    except Exception as e:
        logger.error("Error starting bot:", exc_info=True)
    finally:
        if bot.session and not bot.session.closed:
            await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error("Error running bot:", exc_info=True)
