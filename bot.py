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
from pathlib import Path
import socketio
import base64 

# Load environment variables from .env file
load_dotenv()

# Configure logging
log_level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)

logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('romm_bot')

# Filter out Discord's connection messages
logging.getLogger('discord').setLevel(logging.WARNING)

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

class SocketIOManager:
    """Shared Socket.IO connection manager for all cogs"""
    
    def __init__(self, config):
        self.config = config
        self.sio = socketio.AsyncClient(
            logger=False,
            engineio_logger=False,
            reconnection=True,
            reconnection_attempts=0,
            reconnection_delay=2,
            reconnection_delay_max=60,
            randomization_factor=0.5
        )
        self._connection_lock = asyncio.Lock()
        self._connection_errors = 0
        self._last_successful_connect = time.time()
        self._health_monitor_task = None
        
    async def connect(self):
        """Connect to RomM Socket.IO server"""
        async with self._connection_lock:
            if self.sio.connected:
                return True
            
            max_retries = 5
            retry_delay = 2
            
            for attempt in range(max_retries):
                try:
                    logger.debug(f"SocketIO Manager connecting (attempt {attempt + 1}/{max_retries})...")
                    
                    base_url = self.config.API_BASE_URL.rstrip('/')
                    auth_string = f"{self.config.USER}:{self.config.PASS}"
                    auth_bytes = auth_string.encode('ascii')
                    base64_auth = base64.b64encode(auth_bytes).decode('ascii')
                    
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
                    
                    self._last_successful_connect = time.time()
                    self._connection_errors = 0
                    logger.info("✅ SocketIO Manager connected successfully")
                    return True
                    
                except Exception as e:
                    logger.error(f"Connection attempt {attempt + 1} failed: {e}")
                    self._connection_errors += 1
                    
                    if attempt < max_retries - 1:
                        wait_time = retry_delay * (2 ** attempt)
                        await asyncio.sleep(wait_time)
            
            return False
    
    async def disconnect(self):
        """Disconnect from server"""
        try:
            if self.sio.connected:
                await self.sio.disconnect()
        except Exception as e:
            logger.error(f"Error disconnecting SocketIO: {e}")
    
    async def start_health_monitor(self, bot):
        """Start connection health monitoring"""
        if self._health_monitor_task and not self._health_monitor_task.done():
            return
        
        self._health_monitor_task = asyncio.create_task(self._monitor_health(bot))
    
    async def _monitor_health(self, bot):
        """Monitor connection health with HTTP checks"""
        await asyncio.sleep(30)
        
        consecutive_failures = 0
        max_failures = 3
        
        health_check_timeout = bot.config.API_TIMEOUT + 10  # Add 10s buffer
        
        while True:
            try:
                if not self.sio.connected:
                    logger.warning("SocketIO disconnected, reconnecting...")
                    await self.connect()
                    consecutive_failures = 0
                    await asyncio.sleep(30)
                    continue
                
                # Verify API is reachable
                try:
                    platforms = await asyncio.wait_for(
                        bot.fetch_api_endpoint('platforms', bypass_cache=True),
                        timeout=health_check_timeout
                    )
                    
                    if platforms is not None:
                        consecutive_failures = 0
                        logger.debug("SocketIO health check passed")
                    else:
                        consecutive_failures += 1
                        logger.warning(f"API check failed ({consecutive_failures}/{max_failures})")
                        
                except asyncio.TimeoutError:
                    consecutive_failures += 1
                    logger.warning(f"API timeout after {health_check_timeout}s ({consecutive_failures}/{max_failures})")
                except Exception as e:
                    consecutive_failures += 1
                    logger.warning(f"API error ({consecutive_failures}/{max_failures}): {e}")
                
                if consecutive_failures >= max_failures:
                    logger.error("Forcing reconnection due to failed health checks...")
                    try:
                        await self.sio.disconnect()
                    except Exception:
                        pass
                    await asyncio.sleep(2)
                    await self.connect()
                    consecutive_failures = 0
                
                await asyncio.sleep(60)
            
            except Exception as e:
                logger.error(f"Error in health monitor: {e}", exc_info=True)
                await asyncio.sleep(60)  # Wait before retrying

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
        self.API_TIMEOUT = int(os.getenv('API_TIMEOUT', 30))  # 30 seconds default
        self.USER = os.getenv('USER')
        self.PASS = os.getenv('PASS')
        requests_env = os.getenv('REQUESTS_ENABLED', 'TRUE').upper()
        self.REQUESTS_ENABLED = requests_env == 'TRUE'
        
        self.validate()
        
        logger.debug("RommBot.__init__() completed")

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
            # application_id=os.getenv('RommBot/1.0')
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
        
        # Shared scan state across cogs
        self.scan_state = {
            'is_scanning': False,
            'scan_start_time': None,
            'initiated_by': None,  # 'discord', 'romm', or None
            'scan_type': None,
            'channel_id': None
        }
        self.scan_state_lock = asyncio.Lock()
        
        # Shared SocketIO manager
        self.socketio_manager = None 

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
                        return None
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

    def is_admin(self, user):
        """Check if user is admin"""
        logger.debug(f"Checking admin for user: {user} (ID: {user.id})")
        logger.debug(f"Config ADMIN_ID: '{self.config.ADMIN_ID}' (type: {type(self.config.ADMIN_ID)})")
        
        if not self.config.ADMIN_ID:
            logger.debug("ADMIN_ID is not set or empty")
            return False
        
        # Check user ID
        user_id_str = str(user.id)
        logger.debug(f"User ID as string: '{user_id_str}'")
        logger.debug(f"Comparing: '{user_id_str}' == '{self.config.ADMIN_ID}'")
        
        if user_id_str == self.config.ADMIN_ID:
            logger.debug(f"✓ User ID match! User {user} is admin")
            return True
        else:
            logger.debug(f"✗ User ID does not match")
        
        # Check roles if user has them
        if hasattr(user, 'roles'):
            logger.debug(f"User has {len(user.roles)} roles")
            for role in user.roles:
                role_id_str = str(role.id)
                logger.debug(f"Checking role: {role.name} (ID: {role_id_str})")
                if role_id_str == self.config.ADMIN_ID:
                    logger.debug(f"✓ Role match! User {user} has admin role {role.name}")
                    return True
            logger.debug("✗ No matching admin role found")
        else:
            logger.debug("User has no roles attribute (might be in DMs)")
        
        logger.debug(f"✗ User {user} is NOT admin")
        return False
    
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
        try:    
            # Initialize database FIRST before anything else
            logger.debug("Initializing database...")
            self.db = MasterDatabase()
            await self.db.initialize()
            logger.debug("Database initialize() completed")
            
            # Verify tables were created
            table_status = await self.db.verify_tables_exist()
            logger.debug(f"Table verification result: {table_status}")
            
            if not all(table_status.values()):
                missing_tables = [t for t, exists in table_status.items() if not exists]
                logger.error(f"Missing tables after initialization: {missing_tables}")
                raise Exception(f"Database initialization incomplete: missing tables {missing_tables}")
            
            logger.debug("✅ Database initialization verified successfully")
            
            # Initialize shared SocketIO manager
            logger.debug("About to initialize SocketIO manager...")
            
            try:
                self.socketio_manager = SocketIOManager(self.config)
                logger.debug(f"SocketIOManager created successfully")
                
                logger.debug("Attempting to connect to SocketIO...")
                connect_result = await self.socketio_manager.connect()
                logger.debug(f"SocketIO connect result: {connect_result}")
                
                logger.debug("Starting health monitor...")
                await self.socketio_manager.start_health_monitor(self)
                logger.debug("✅ SocketIO manager initialized")
                
            except Exception as e:
                logger.error(f"FAILED to initialize SocketIOManager: {e}", exc_info=True)
                raise
            
            # Initialize OAuth tokens AFTER database
            logger.debug("Initializing OAuth tokens...")
            if not await self.get_oauth_token():
                logger.warning("Failed to obtain OAuth tokens, some features may not work")
            else:
                logger.info("✅ OAuth tokens initialized successfully")
                                       
        except Exception as e:
            logger.error("=" * 50)
            logger.error(f"SETUP HOOK FAILED: {e}")
            logger.error("=" * 50)
            logger.error("Full traceback:", exc_info=True)
            raise
    
    def load_all_cogs(self):
        """Load all cogs."""
        core_cogs = [
            'cogs.emoji_manager', 
            'cogs.igdb_client',
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
            'cogs.igdb_client': [],
            'cogs.info': [],
            'cogs.search': ['aiohttp','qrcode'],
            'cogs.scan': ['socketio'],
            'cogs.requests': ['aiosqlite'],
            'cogs.user_manager': ['aiohttp','aiosqlite'],
            'cogs.recent_roms': ['aiosqlite']
        }

        for cog in core_cogs:
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
        
        # Load integration cogs from separate folder
        self.load_integration_cogs()
        
    def load_integration_cogs(self):
        """Load optional integration cogs from integrations folder."""
        integrations_dir = Path('integrations')
        
        if not integrations_dir.exists():
            logger.debug("No integrations directory found")
            return
        
        # Find all Python files in integrations folder
        for file in integrations_dir.glob('*.py'):
            if file.stem.startswith('_'):
                continue
                
            cog_name = f"integrations.{file.stem}"
            
            # Check if enabled in .env
            env_key = f"{file.stem.upper()}_ENABLED"
            if os.getenv(env_key, 'false').lower() != 'true':
                logger.debug(f"Integration {file.stem} is disabled (set {env_key}=true to enable)")
                continue
            
            try:
                self.load_extension(cog_name)
                logger.info(f"Loaded integration: {file.stem}")
            except Exception as e:
                logger.error(f"Failed to load integration {file.stem}: {e}")
  
    async def on_ready(self):
        """When bot is ready, start tasks."""
        logger.info(f'{self.user} has connected to Discord!')
        
        # Check that database is initialized
        #if self.db is None or not self.db._initialized:
        #    # Try to initialize it now as a fallback
        #    if self.db is None:
        #        self.db = MasterDatabase()
        #    await self.db.initialize()
        
        # Initialize SocketIO manager if not already done
        #if self.socketio_manager is None:
        #    logger.warning("SocketIO manager was not initialized in setup_hook!")
        #    logger.info("Initializing SocketIO manager...")
        #    try:
        #        self.socketio_manager = SocketIOManager(self.config)
        #        await self.socketio_manager.connect()
        #        await self.socketio_manager.start_health_monitor(self)
        #        logger.info("✅ SocketIO manager initialized")
        #    except Exception as e:
        #        logger.error(f"Failed to initialize SocketIO manager: {e}", exc_info=True)
        
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
        
        # Start token refresh loop if not running
        if not self.refresh_token_task.is_running():
            self.refresh_token_task.start()
                    
    async def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure an active session exists with optimized settings."""
        async with self._session_lock:
            if self.session is None or self.session.closed:
                # Configure connector with keepalive
                connector = aiohttp.TCPConnector(
                    limit=10,                      # Max connections
                    limit_per_host=5,              # Max per host
                    ttl_dns_cache=300,             # DNS cache 5 min
                    force_close=False,             # Enable keepalive
                    enable_cleanup_closed=True,
                    keepalive_timeout=75           # Keep connections alive
                )
                
                self.session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(
                        total=self.config.API_TIMEOUT,      # Overall timeout
                        connect=5,                           # Connection timeout
                        sock_read=self.config.API_TIMEOUT   # Read timeout
                    ),
                    headers={
                        "User-Agent": "RommBot/1.0",
                        "Accept": "application/json",
                        "Connection": "keep-alive"          # Explicit keepalive
                    }
                )
            return self.session
    
    def get_formatted_emoji(self, name: str) -> str:
        """Get formatted emoji string for use in embeds"""
        emoji_manager = self.get_cog('EmojiManager')
        if emoji_manager:
            return emoji_manager.get_emoji(name)
        return f":{name}:"
    
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

    async def fetch_api_endpoint(self, endpoint: str, bypass_cache: bool = False, max_retries: int = 2) -> Optional[Dict]:
        """Fetch data from API with caching, error handling, and retries."""
        # Bypass cache if specified
        if not bypass_cache:
            cached_data = self.cache.get(endpoint)
            if cached_data:
                logger.debug(f"Returning cached data for {endpoint}")
                return cached_data

        for attempt in range(max_retries + 1):
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
                
                logger.debug(f"Making request to: {url} (attempt {attempt + 1}/{max_retries + 1})")
            
                async with session.get(url, headers=headers) as response:
                    logger.debug(f"Response status: {response.status}")
                    logger.debug(f"Response content-type: {response.headers.get('content-type', 'unknown')}")
                    
                    if response.status == 200:
                        data = await response.json()
                        logger.debug(f"Fetched fresh data for {endpoint}")
                        self.cache.set(endpoint, data)
                        return data
                    else:
                        error_text = await response.text()
                        logger.error(f"API returned status {response.status}: {error_text}")
                        
                        # Don't retry on auth failures
                        if response.status in [401, 403]:
                            return None
                            
            except asyncio.TimeoutError:
                if attempt < max_retries:
                    wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                    logger.warning(f"Request timeout, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries + 1})")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Request failed after {max_retries + 1} attempts (timeout)")
                    return None
                    
            except Exception as e:
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    logger.warning(f"Request error: {e}, retrying in {wait_time}s... (attempt {attempt + 1}/{max_retries + 1})")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Request failed after {max_retries + 1} attempts: {e}")
                    return None
        
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
    
    # Manually initialize - py-cord's auto setup_hook isn't working
    logger.debug("Running manual initialization...")
    await bot.setup_hook()
    logger.info("Initialization complete, starting bot...")
    
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


