import asyncio
import base64
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import aiohttp
import discord
import socketio
from discord.ext import commands, tasks
from dotenv import load_dotenv

from cogs.platform_emoji import PlatformEmoji
from database_manager import MasterDatabase
from romm_client import RommClient

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

logging.getLogger('discord').setLevel(logging.WARNING)



class SocketIOManager:
    """Shared Socket.IO connection manager for all cogs"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config = bot.config
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
        self._session_cookie = None
        self._register_event_handlers()

    async def _fetch_session_cookie(self) -> Optional[str]:
        """Log in via /api/login to obtain a RomM session cookie.

        RomM resolves the identity of a Socket.IO client from the server-side
        session attached to the handshake cookie, not from the Authorization
        header. Privileged socket events (scan, scan:stop) are rejected without
        it, so the handshake has to carry a real session cookie. Client API
        tokens cannot open a session, so this needs ROMM_USER / ROMM_PASS.
        """
        if not (self.config.USER and self.config.PASS):
            logger.warning(
                "No ROMM_USER / ROMM_PASS configured, so no RomM session can be "
                "opened; RomM will reject scan commands from this bot"
            )
            return None

        base_url = self.config.API_BASE_URL.rstrip('/')
        auth = aiohttp.BasicAuth(self.config.USER, self.config.PASS)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base_url}/api/login", auth=auth) as response:
                    if response.status != 200:
                        logger.warning(
                            f"Session login failed (status {response.status}); "
                            "scan commands will be rejected by RomM"
                        )
                        return None

                    for cookie_header in response.headers.getall('Set-Cookie', []):
                        match = re.search(r'romm_session=([^;]+)', cookie_header)
                        if match:
                            logger.debug("Obtained RomM session cookie for Socket.IO")
                            return f"romm_session={match.group(1)}"

                    logger.warning("Login succeeded but no romm_session cookie was returned")
                    return None
        except Exception as e:
            logger.warning(f"Could not obtain RomM session cookie: {e}")
            return None

    def _register_event_handlers(self):
        """Register the one-and-only set of Socket.IO handlers and re-broadcast
        each as a bot event. Cogs subscribe via @commands.Cog.listener instead of
        competing for the single-handler-per-event socket client."""
        self.sio.on('connect', self._on_sio_connect)
        self.sio.on('disconnect', self._on_sio_disconnect)
        self.sio.on('connect_error', self._on_sio_connect_error)
        self.sio.on('scan:scanning_platform', self._on_sio_scan_platform)
        self.sio.on('scan:scanning_rom', self._on_sio_scan_rom)
        self.sio.on('scan:done', self._on_sio_scan_done)
        self.sio.on('scan:done_ko', self._on_sio_scan_error)

    async def _on_sio_connect(self):
        logger.debug("Socket.IO connect event; broadcasting romm_connect")
        self.bot.dispatch('romm_connect')

    async def _on_sio_disconnect(self, *args):
        logger.debug("Socket.IO disconnect event; broadcasting romm_disconnect")
        self.bot.dispatch('romm_disconnect')

    async def _on_sio_connect_error(self, *args):
        data = args[0] if args else None
        logger.error(f"Socket.IO connect error: {data}")
        self.bot.dispatch('romm_connect_error', data)

    async def _on_sio_scan_platform(self, data=None):
        self.bot.dispatch('romm_scan_platform', data)

    async def _on_sio_scan_rom(self, data=None):
        self.bot.dispatch('romm_scan_rom', data)

    async def _on_sio_scan_done(self, stats=None):
        self.bot.dispatch('romm_scan_done', stats)

    async def _on_sio_scan_error(self, error_message=None):
        self.bot.dispatch('romm_scan_error', error_message)

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
                    if self.config.ROMM_CLIENT_TOKEN:
                        # Client API tokens authenticate via Bearer, same as the HTTP API.
                        headers = {
                            'Authorization': f'Bearer {self.config.ROMM_CLIENT_TOKEN}',
                            'User-Agent': 'RommBot/1.0'
                        }
                    else:
                        auth_string = f"{self.config.USER}:{self.config.PASS}"
                        auth_bytes = auth_string.encode('ascii')
                        base64_auth = base64.b64encode(auth_bytes).decode('ascii')
                        headers = {
                            'Authorization': f'Basic {base64_auth}',
                            'User-Agent': 'RommBot/1.0'
                        }

                    # RomM authorizes privileged socket events (scan, scan:stop)
                    # from the session cookie only, never from the Authorization
                    # header, so refresh it on every connection attempt.
                    self._session_cookie = await self._fetch_session_cookie()
                    if self._session_cookie:
                        headers['Cookie'] = self._session_cookie

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
        """Disconnect from server and stop the health monitor."""
        # Cancel the monitor first: it reconnects any socket it finds closed,
        # so disconnecting while it runs just triggers a reconnect 30s later.
        if self._health_monitor_task and not self._health_monitor_task.done():
            self._health_monitor_task.cancel()
            try:
                await self._health_monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error stopping SocketIO health monitor: {e}")

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
                        
                except TimeoutError:
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

    @staticmethod
    def parse_bool(value: str, default: bool = False) -> bool:
        """Parse a string value as boolean consistently.

        Args:
            value: The string to parse (case-insensitive)
            default: Default value if parsing fails

        Returns:
            True if value is 'true', '1', 'yes', 'on' (case-insensitive)
            False if value is 'false', '0', 'no', 'off' (case-insensitive)
            default otherwise
        """
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in ('true', '1', 'yes', 'on'):
            return True
        if normalized in ('false', '0', 'no', 'off'):
            return False
        return default

    def __init__(self):
        self.TOKEN = os.getenv('TOKEN')
        self.GUILD_ID = os.getenv('GUILD')
        self.CHANNEL_ID = os.getenv('CHANNEL_ID')
        self.ADMIN_ID = os.getenv('ADMIN_ID')
        self.API_BASE_URL = os.getenv('API_URL', '').rstrip('/')
        self.DOMAIN = os.getenv('DOMAIN', 'No website configured').rstrip('/')
        self.SYNC_RATE = int(os.getenv('SYNC_RATE', '3600'))  # 1 hour default
        self.UPDATE_VOICE_NAMES = self.parse_bool(os.getenv('UPDATE_VOICE_NAMES', 'true'), True)
        self.SHOW_API_SUCCESS = self.parse_bool(os.getenv('SHOW_API_SUCCESS', 'false'), False)
        self.CACHE_TTL = int(os.getenv('CACHE_TTL', '3900'))  # 65 minutes default
        self.API_TIMEOUT = int(os.getenv('API_TIMEOUT', '30'))  # 30 seconds default
        explicit_user = os.getenv('ROMM_USER') or os.getenv('ROMM_USERNAME')
        explicit_pass = os.getenv('ROMM_PASS') or os.getenv('ROMM_PASSWORD')
        legacy_user = os.getenv('USER')
        legacy_pass = os.getenv('PASS')

        self.USER = explicit_user
        self.PASS = explicit_pass

        if not explicit_user and not explicit_pass:
            self.USER = legacy_user
            self.PASS = legacy_pass
            if legacy_user or legacy_pass:
                logger.warning("USER/PASS are deprecated for RomM credentials; use ROMM_USER/ROMM_PASS instead")
        elif explicit_user and not explicit_pass and legacy_pass:
            self.PASS = legacy_pass
            logger.warning("PASS is deprecated for RomM credentials; use ROMM_PASS instead")

        # RomM client API token (preferred over USER/PASS when set).
        # Create one in the RomM web UI (user profile -> API tokens) or via
        # POST /api/client-tokens. Sent as `Authorization: Bearer <token>`.
        self.ROMM_CLIENT_TOKEN = os.getenv('ROMM_CLIENT_TOKEN') or None

        self.REQUESTS_ENABLED = self.parse_bool(os.getenv('REQUESTS_ENABLED', 'true'), True)

        # Cog-specific config (centralized here to avoid scattered os.getenv calls)
        self.RECENT_ROMS_CHANNEL_ID = os.getenv('RECENT_ROMS_CHANNEL_ID')
        self.RECENT_ROMS_MAX_PER_POST = int(os.getenv('RECENT_ROMS_MAX_PER_POST', '10'))
        self.RECENT_ROMS_BULK_THRESHOLD = int(os.getenv('RECENT_ROMS_BULK_THRESHOLD', '25'))
        self.RECENT_ROMS_ENABLED = self.parse_bool(os.getenv('RECENT_ROMS_ENABLED', 'true'), True)
        self.IGDB_CLIENT_ID = os.getenv('IGDB_CLIENT_ID')
        self.IGDB_CLIENT_SECRET = os.getenv('IGDB_CLIENT_SECRET')
        self.AUTO_REGISTER_ROLE_ID = os.getenv('AUTO_REGISTER_ROLE_ID')

        # Netplay announcements. The cog is always loaded (core_cogs is an
        # unconditional list); NETPLAY_ENABLED is enforced inside it, which is
        # also where the server's own EJS_NETPLAY_ENABLED is checked.
        self.NETPLAY_ENABLED = self.parse_bool(os.getenv('NETPLAY_ENABLED', 'true'), True)
        self.NETPLAY_POLL_INTERVAL = int(os.getenv('NETPLAY_POLL_INTERVAL', '20'))
        self.NETPLAY_PENDING_TIMEOUT = int(os.getenv('NETPLAY_PENDING_TIMEOUT', '900'))
        self.NETPLAY_MAX_WATCHERS = int(os.getenv('NETPLAY_MAX_WATCHERS', '25'))

        # GGRequestz integration. The URL is stored without a trailing /api;
        # the integration appends the path itself.
        ggr_url = os.getenv('GGREQUESTZ_URL', '').rstrip('/')
        if ggr_url.endswith('/api'):
            ggr_url = ggr_url[:-4]
        self.GGREQUESTZ_URL = ggr_url
        self.GGREQUESTZ_API_KEY = os.getenv('GGREQUESTZ_API_KEY')
        self.ENABLE_USER_MANAGER = self.parse_bool(os.getenv('ENABLE_USER_MANAGER', 'true'), True)

        self.validate()

        logger.debug("Config initialized")

    def validate(self):
        """Validate configuration values."""
        required = {
            'TOKEN': self.TOKEN,
            'GUILD': self.GUILD_ID,
            'API_URL': self.API_BASE_URL,
        }
        # RomM API auth: a client token replaces username/password.
        # When no client token is set, fall back to requiring ROMM_USER/ROMM_PASS.
        if not self.ROMM_CLIENT_TOKEN:
            required['ROMM_USER'] = self.USER
            required['ROMM_PASS'] = self.PASS
        missing = [k for k, v in required.items() if not v]
        
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")
        
        try:
            self.GUILD_ID = int(self.GUILD_ID)
            # CHANNEL_ID is optional; only coerce it when one was supplied.
            if self.CHANNEL_ID:
                self.CHANNEL_ID = int(self.CHANNEL_ID)
        except ValueError:
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

        # Everything to do with talking to RomM - the HTTP session, OAuth and
        # CSRF tokens, the response cache - belongs to the client.
        self.romm = RommClient(self.config)

        # Formatting a platform name with its emoji is wanted in five cogs and
        # belongs to none of them, so it is a service rather than a lookup of
        # whichever cog happened to own the table.
        self.platform_emoji = PlatformEmoji(self)

        # Master database initialization - DON'T initialize here, wait for setup_hook
        self.db = None

        # Add a commands sync flag
        self.synced = False
        
        # Global cooldown for slash commands
        self._cd_bucket = commands.CooldownMapping.from_cooldown(1, 60, commands.BucketType.user)
        
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



    

        
    
    # The RomM API client owns the session, the tokens and the cache. These
    # delegates and aliases keep the call sites in the cogs working unchanged.

    @property
    def cache(self):
        return self.romm.cache

    @property
    def rate_limiter(self):
        return self.romm.rate_limiter

    @property
    def session(self) -> Optional[aiohttp.ClientSession]:
        return self.romm.session

    @property
    def access_token(self) -> Optional[str]:
        return self.romm.access_token

    @property
    def csrf_token(self) -> Optional[str]:
        return self.romm.csrf_token

    async def ensure_session(self) -> aiohttp.ClientSession:
        return await self.romm.ensure_session()

    async def get_oauth_token(self) -> bool:
        return await self.romm.get_oauth_token()

    async def refresh_oauth_token(self) -> bool:
        return await self.romm.refresh_oauth_token()

    async def ensure_valid_token(self) -> bool:
        return await self.romm.ensure_valid_token()

    async def get_csrf_token(self) -> Optional[str]:
        return await self.romm.get_csrf_token()

    async def ensure_csrf_token(self) -> Optional[str]:
        return await self.romm.ensure_csrf_token()

    async def make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        form_data: Optional[aiohttp.FormData] = None,
        require_csrf: bool = False
    ) -> Optional[Dict]:
        return await self.romm.make_authenticated_request(
            method, endpoint, data=data, params=params,
            form_data=form_data, require_csrf=require_csrf
        )

    async def fetch_api_endpoint(
        self, endpoint: str, bypass_cache: bool = False, max_retries: int = 2
    ) -> Optional[Dict]:
        return await self.romm.fetch_api_endpoint(
            endpoint, bypass_cache=bypass_cache, max_retries=max_retries
        )

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
            logger.debug("✗ User ID does not match")
        
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
    
    async def slash_cooldown_check(self, ctx: discord.ApplicationContext) -> bool:
        """Global cooldown check for all slash commands."""
        if await self.is_owner(ctx.author):  # Skip cooldown for bot owner
            return True
        # Use ctx directly for ApplicationContext (slash commands don't have .message)
        bucket = self._cd_bucket.get_bucket(ctx)
        retry_after = bucket.update_rate_limit()
        if retry_after:
            raise commands.CommandOnCooldown(bucket, retry_after, self._cd_bucket.type)
        return True

    def get_platform_display_name(self, platform_data: Dict) -> str:
        """Get the display name for a platform, preferring custom_name over name."""
        # Check if platform_data has custom_name and it's not empty/None
        custom_name = platform_data.get('custom_name')
        if custom_name and custom_name.strip():
            return custom_name.strip()

        # Fall back to regular name
        return platform_data.get('name', 'Unknown Platform')

    async def find_platform_by_name(self, platform_name: str, platforms_data: list = None) -> tuple:
        """
        Find platform data by name, checking both regular and custom names.

        Args:
            platform_name: The platform name to search for
            platforms_data: Optional pre-fetched platforms list to avoid redundant API calls

        Returns:
            Tuple of (platform_id, platform_display_name) or (None, None) if not found
        """
        if platforms_data is None:
            platforms_data = await self.fetch_api_endpoint('platforms')

        if not platforms_data:
            return None, None

        platform_lower = platform_name.lower()

        for p in platforms_data:
            # Check custom name first
            custom_name = p.get('custom_name')
            if custom_name and custom_name.lower() == platform_lower:
                return p['id'], self.get_platform_display_name(p)

            # Check regular name
            regular_name = p.get('name', '')
            if regular_name.lower() == platform_lower:
                return p['id'], self.get_platform_display_name(p)

        return None, None

    async def setup_hook(self):
        """Initialize database and other async resources before bot starts"""
        try:
            # Register global error handler for slash commands
            self.add_listener(self.on_application_command_error)
            # Register cooldown check as before_invoke hook for application commands
            self.before_invoke(self.slash_cooldown_check)

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
                self.socketio_manager = SocketIOManager(self)
                logger.debug("SocketIOManager created successfully")
                
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
            if not await self.ensure_valid_token():
                logger.warning("Failed to obtain RomM API token, some features may not work")
            else:
                logger.info("✅ RomM API token initialized successfully")
                                       
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
            'cogs.recent_roms',
            'cogs.achievements',
            'cogs.netplay'
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
            'cogs.recent_roms': ['aiosqlite'],
            'cogs.achievements': [],
            'cogs.netplay': ['aiohttp']
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
        # Client API tokens are static and never need refreshing.
        if self.config.ROMM_CLIENT_TOKEN:
            return
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
        """Graceful shutdown with proper cleanup of all resources and tasks."""
        logger.info("Initiating graceful shutdown...")

        # Cancel background task loops
        if self.update_loop.is_running():
            self.update_loop.cancel()
            logger.debug("Cancelled update_loop")

        if self.refresh_token_task.is_running():
            self.refresh_token_task.cancel()
            logger.debug("Cancelled refresh_token_task")

        # Close the shared SocketIO connection. It is owned here rather than
        # by the cogs that listen on it, so this is the only place it is torn
        # down.
        if self.socketio_manager is not None:
            await self.socketio_manager.disconnect()
            logger.debug("Disconnected SocketIO")

        # Cancel any pending asyncio tasks created by this bot
        pending_tasks = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and 'close' not in task.get_name()
        ]

        if pending_tasks:
            logger.debug(f"Cancelling {len(pending_tasks)} pending tasks...")
            for task in pending_tasks:
                task.cancel()

            # Wait for tasks to complete cancellation with timeout
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        # Close database connections
        if self.db is not None:
            await self.db.close_all_connections()
            logger.debug("Closed database connections")

        # Close HTTP session
        await self.romm.close()
        logger.debug("Closed HTTP session")

        logger.info("Graceful shutdown complete")
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
        # Use getattr for safer attribute access in case bot wasn't fully initialized
        session = getattr(bot, 'session', None)
        if session and not session.closed:
            await session.close()
        db = getattr(bot, 'db', None)
        if db:
            await db.close_all_connections()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error("Error running bot:", exc_info=True)


