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
from database_manager import MasterDatabase

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
        self.ADMIN_ID = os.getenv('ADMIN_ID')
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
            # MODIFIED: Only convert CHANNEL_ID if it's not None or empty
            if self.CHANNEL_ID:
                self.CHANNEL_ID = int(self.CHANNEL_ID)
        except ValueError:
            # MODIFIED: Updated error message for clarity
            raise ValueError("GUILD_ID must be a numeric value. If provided, CHANNEL_ID must also be numeric.")

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
            command_prefix="!",
            intents=intents,
            application_id=os.getenv('RommBot/1.0')
        )

        # Initialize bot attributes    
        self.config = Config()
        self.cache = APICache(self.config.CACHE_TTL)
        self.rate_limiter = RateLimit()
        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        # Master database initialization - DON'T initialize here, wait for setup_hook
        self.db = None
        
        # OAuth token management attributes
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiry: float = 0
        self.token_lock = asyncio.Lock()
        
        # Initialize session with proper headers
        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()
        
        # CSRF token management
        self.csrf_token: Optional[str] = None
        self.csrf_cookie: Optional[str] = None
        self.csrf_expiry: float = 0
        
        # Add a commands sync flag
        self.synced = False
        
        # Global cooldown
        self._cd_bucket = commands.CooldownMapping.from_cooldown(1, 60, commands.BucketType.user)
        
        # Register error handler
        self.application_command_error = self.on_application_command_error

    async def get_oauth_token(self) -> bool:
        """Get initial OAuth token using username/password."""
        try:
            session = await self.ensure_session()
            
            # Token endpoint keeps the /api/ prefix
            token_url = f"{self.config.API_BASE_URL}/api/token"
            logger.debug(f"Requesting token from: {token_url}")
            
            # Prepare form data for OAuth2 password grant
            data = aiohttp.FormData()
            data.add_field('grant_type', 'password')
            data.add_field('username', self.config.USER)
            data.add_field('password', self.config.PASS)
            data.add_field('scope', 'roms.read platforms.read firmware.read users.read users.write me.write')
            
            # Simple headers for OAuth token request
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"
            }
            
            async with session.post(token_url, data=data, headers=headers) as response:
                response_text = await response.text()
                logger.debug(f"Token response status: {response.status}")
                logger.debug(f"Token response content-type: {response.headers.get('content-type')}")
                
                if response.status == 200:
                    try:
                        token_data = await response.json()
                        self.access_token = token_data.get('access_token')
                        self.refresh_token = token_data.get('refresh_token')
                        # Store expiry time (subtract 60 seconds for safety margin)
                        self.token_expiry = time.time() + token_data.get('expires', 900) - 60
                        logger.debug("Successfully obtained OAuth tokens")
                        return True
                    except Exception as e:
                        logger.error(f"Failed to parse token response: {e}")
                        logger.error(f"Response text: {response_text}")
                        return False
                else:
                    logger.error(f"Failed to get OAuth token. Status: {response.status}")
                    logger.error(f"Response: {response_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error getting OAuth token: {e}", exc_info=True)
            return False

    async def refresh_oauth_token(self) -> bool:
        """Refresh the OAuth token using the refresh token."""
        if not self.refresh_token:
            logger.debug("No refresh token available, getting new token")
            return await self.get_oauth_token()
        
        try:
            session = await self.ensure_session()
            token_url = f"{self.config.API_BASE_URL}/api/token"
            
            data = aiohttp.FormData()
            data.add_field('grant_type', 'refresh_token')
            data.add_field('refresh_token', self.refresh_token)
            
            async with session.post(token_url, data=data) as response:
                if response.status == 200:
                    token_data = await response.json()
                    self.access_token = token_data.get('access_token')
                    # Refresh token may or may not be returned
                    if 'refresh_token' in token_data:
                        self.refresh_token = token_data.get('refresh_token')
                    self.token_expiry = time.time() + token_data.get('expires', 900) - 60
                    logger.debug("Successfully refreshed OAuth token")
                    return True
                else:
                    logger.warning(f"Failed to refresh token, status: {response.status}")
                    # If refresh fails, try getting a new token
                    return await self.get_oauth_token()
                    
        except Exception as e:
            logger.error(f"Error refreshing OAuth token: {e}")
            return await self.get_oauth_token()

    async def ensure_valid_token(self) -> bool:
        """Ensure we have a valid OAuth token, refreshing if necessary."""
        async with self.token_lock:
            # Check if token is expired or missing
            if not self.access_token or time.time() >= self.token_expiry:
                logger.debug("Token expired or missing, refreshing...")
                if self.refresh_token and time.time() < self.token_expiry + 604800:  # 7 days
                    return await self.refresh_oauth_token()
                else:
                    return await self.get_oauth_token()
            return True
    
    async def get_csrf_token(self) -> Optional[str]:
        """Get CSRF token from the heartbeat endpoint."""
        try:
            session = await self.ensure_session()
            heartbeat_url = f"{self.config.API_BASE_URL}/heartbeat"
            
            async with session.get(heartbeat_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get heartbeat. Status: {response.status}")
                    return None
                
                # Extract CSRF token from Set-Cookie header
                set_cookie = response.headers.get('Set-Cookie')
                if not set_cookie:
                    logger.debug("No Set-Cookie header in heartbeat response")
                    return None
                
                # Parse the CSRF token from cookie
                if 'romm_csrftoken=' in set_cookie:
                    csrf_token = set_cookie.split('romm_csrftoken=')[1].split(';')[0]
                    self.csrf_token = csrf_token
                    self.csrf_cookie = f"romm_csrftoken={csrf_token}"
                    # CSRF tokens typically last for the session
                    self.csrf_expiry = time.time() + 3600  # 1 hour expiry
                    logger.debug(f"Extracted CSRF token: {csrf_token[:10]}...")
                    return csrf_token
                else:
                    logger.debug("CSRF token not found in Set-Cookie header")
                    return None
                    
        except Exception as e:
            logger.error(f"Error getting CSRF token: {e}")
            return None

    async def ensure_csrf_token(self) -> Optional[str]:
        """Ensure we have a valid CSRF token."""
        if not self.csrf_token or time.time() >= self.csrf_expiry:
            logger.debug("CSRF token expired or missing, fetching new one")
            return await self.get_csrf_token()
        return self.csrf_token
        
    async def make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        form_data: Optional[aiohttp.FormData] = None,
        require_csrf: bool = False
    ) -> Optional[Dict]:
        """Make an authenticated API request with proper headers."""
        try:
            if not await self.ensure_valid_token():
                logger.error("Failed to obtain valid OAuth token")
                return None
            
            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/{endpoint}"
            
            headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
            
            if require_csrf:
                csrf_token = await self.ensure_csrf_token()
                if csrf_token:
                    headers["X-CSRFToken"] = csrf_token
                    headers["Cookie"] = self.csrf_cookie
            
            request_kwargs = {"headers": headers}
            if data: request_kwargs["json"] = data
            if params: request_kwargs["params"] = params
            if form_data: request_kwargs["data"] = form_data
                
            async with session.request(method, url, **request_kwargs) as response:
                logger.debug(f"API Response: {method} {url} -> Status {response.status}")

                # This is the function that will handle the response
                async def handle_response(resp: aiohttp.ClientResponse) -> Optional[Dict]:
                    if resp.status in (200, 201, 204):
                        if resp.status == 204:
                            return {}  # Success with no content is a valid response

                        try:
                            json_response = await resp.json()
                            # A `null` response body becomes `None`. Treat this as a successful empty response.
                            return json_response if json_response is not None else {}
                        except Exception:
                            # An empty body or non-JSON response on a success status code is also a success.
                            logger.debug(f"Could not parse JSON from successful response (Status {resp.status}), but treating as success.")
                            return {}
                    else:
                        logger.error(f"Request failed with status {resp.status}")
                        try:
                            response_text = await resp.text()
                            logger.error(f"Response: {response_text}")
                        except Exception:
                            logger.error("Could not read response text.")
                        return None

                if response.status == 401:
                    logger.debug("Got 401, attempting to refresh token")
                    if await self.ensure_valid_token():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        async with session.request(method, url, **request_kwargs) as retry_response:
                            return await handle_response(retry_response)
                else:
                    return await handle_response(response)
                    
        except Exception as e:
            logger.error(f"Error making authenticated request: {e}", exc_info=True)
            return None
    
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

    def get_platform_display_name(self, platform_data: Dict) -> str:
        """Get the display name for a platform, preferring custom_name over name."""
        # Check if platform_data has custom_name and it's not empty/None
        custom_name = platform_data.get('custom_name')
        if custom_name and custom_name.strip():
            return custom_name.strip()
        
        # Fall back to regular name
        return platform_data.get('name', 'Unknown Platform')
    
    async def setup_hook(self):
        """Initialize database and other async resources before bot starts"""
        logger.info("Starting bot setup hook...")
        
        try:
            # Initialize database FIRST before anything else
            logger.info("Initializing database...")
            self.db = MasterDatabase()
            await self.db.initialize()
            
            # Verify tables were created
            table_status = await self.db.verify_tables_exist()
            if not all(table_status.values()):
                missing_tables = [t for t, exists in table_status.items() if not exists]
                logger.error(f"Missing tables after initialization: {missing_tables}")
                raise Exception(f"Database initialization incomplete: missing tables {missing_tables}")
            
            logger.info("✅ Database initialization verified successfully")
            
            # Initialize OAuth tokens AFTER database
            logger.info("Initializing OAuth tokens...")
            if not await self.get_oauth_token():
                logger.warning("Failed to obtain OAuth tokens, some features may not work")
            else:
                logger.info("✅ OAuth tokens initialized successfully")
                
        except Exception as e:
            logger.error(f"❌ Setup hook failed: {e}", exc_info=True)
            # Re-raise to prevent bot from starting with broken database
            raise
    
    def load_all_cogs(self):
        """Load all cogs."""
        cogs_to_load = [
            'cogs.emoji_manager', 
            'cogs.info', 
            'cogs.search', 
            'cogs.scan', 
            'cogs.requests',
            'cogs.user_manager',
            'cogs.recent_roms'
        ]
        
        # Dependencies for each cog
        cog_dependencies = {
            'cogs.emoji_manager': ['aiohttp'],
            'cogs.info': [],
            'cogs.search': ['aiohttp','qrcode'],
            'cogs.scan': ['socketio'],
            'cogs.requests': ['aiosqlite'],
            'cogs.user_manager': ['aiohttp','aiosqlite'],
            'cogs.recent_roms': ['aiosqlite']
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
        
        # Check that database is initialized
        if self.db is None or not self.db._initialized:
            # Try to initialize it now as a fallback
            if self.db is None:
                self.db = MasterDatabase()
            await self.db.initialize()
        
        # Load cogs only after database is confirmed ready
        self.load_all_cogs()
        loaded_cogs = list(self.cogs.keys())
        logger.debug(f"Currently loaded cogs: {loaded_cogs}")

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
                        "User-Agent": "RommBot/1.0",
                        "Accept": "application/json"
                    }
                )
            return self.session

    @tasks.loop(seconds=300)  # Default to 5 minutes, will be updated in before_loop
    async def update_loop(self):
        """Periodic API data update task."""
        await self.update_api_data()

    @tasks.loop(minutes=10)
    async def refresh_token_task(self):
        """Periodically refresh the OAuth token to keep it valid."""
        if self.access_token:
            await self.ensure_valid_token()

    @refresh_token_task.before_loop
    async def before_refresh_token(self):
        """Wait until the bot is ready before starting token refresh."""
        await self.wait_until_ready()
    
    @update_loop.before_loop
    async def before_update_loop(self):
        """Set up the update loop with config values."""
        await self.wait_until_ready()
        # Update the interval using the config value
        self.update_loop.change_interval(seconds=self.config.SYNC_RATE)
        logger.debug("Update loop initialized")

    async def fetch_api_endpoint(self, endpoint: str, bypass_cache: bool = False) -> Optional[Dict]:
        """Fetch data from API with caching and error handling."""
        # Bypass cache if specified
        if not bypass_cache:
            cached_data = self.cache.get(endpoint)
            if cached_data:
                logger.debug(f"Returning cached data for {endpoint}")
                return cached_data

        try:
            # Ensure we have a valid token
            if not await self.ensure_valid_token():
                logger.error("Failed to obtain valid OAuth token")
                return None
                
            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/{endpoint}"
            
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json"
            }
            
            # DEBUG: Log the request details
            logger.debug(f"Making request to: {url}")
            logger.debug(f"Using token: {self.access_token[:20] if self.access_token else 'None'}...")
            logger.debug(f"Authorization header: Bearer {self.access_token[:20] if self.access_token else 'None'}...")
        
            async with session.get(url, headers=headers) as response:
                content_type = response.headers.get('content-type', '')
                
                # DEBUG: Log response details
                logger.debug(f"Response status: {response.status}")
                logger.debug(f"Response content-type: {content_type}")
                
                if response.status == 401:
                    logger.debug("Got 401, attempting to refresh token")
                    if await self.ensure_valid_token():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        async with session.get(url, headers=headers) as retry_response:
                            if retry_response.status == 200 and 'application/json' in retry_response.headers.get('content-type', ''):
                                data = await retry_response.json()
                                logger.debug(f"Fetched fresh data for {endpoint} after token refresh")
                                if data:
                                    self.cache.set(endpoint, data)
                                return data
                            else:
                                logger.warning(f"API returned status {retry_response.status} for endpoint {endpoint}")
                                return None
                elif response.status == 200:
                    if 'application/json' in content_type:
                        try:
                            data = await response.json()
                            logger.debug(f"Fetched fresh data for {endpoint}")
                            if data:
                                self.cache.set(endpoint, data)
                            return data
                        except Exception as e:
                            logger.error(f"Error parsing JSON from {endpoint}: {e}")
                            return None
                    else:
                        logger.error(f"Expected JSON but got {content_type} for {endpoint}")
                        # DEBUG: Log the actual response content
                        response_text = await response.text()
                        logger.error(f"Response content (first 500 chars): {response_text[:500]}")
                        return None
                else:
                    logger.warning(f"API returned status {response.status} for endpoint {endpoint}")
                    response_text = await response.text()
                    logger.warning(f"Response content: {response_text[:200]}")
                    return None
                    
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
                    "Storage Size": self.bytes_to_tb(raw_data.get('TOTAL_FILESIZE_BYTES', 0))
                }
        
            elif data_type == 'platforms':
                return [
                    {
                        "id": platform.get("id", 0),
                        "name": platform.get("name", "Unknown Platform"),
                        "custom_name": platform.get("custom_name"),  # Include custom_name
                        "display_name": self.get_platform_display_name(platform),  # Add display_name
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
        # Add database cleanup
        if hasattr(self, 'db'):
            await self.db.close_all_connections()
        
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
        
        # Try to diagnose database issues
        if bot.db:
            logger.error("Attempting database diagnostic...")
            try:
                table_status = await bot.db.verify_tables_exist()
                logger.error(f"Table status: {table_status}")
            except Exception as diag_error:
                logger.error(f"Diagnostic failed: {diag_error}")
    finally:
        if bot.session and not bot.session.closed:
            await bot.session.close()
        if bot.db:
            await bot.db.close_all_connections()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error("Error running bot:", exc_info=True)
