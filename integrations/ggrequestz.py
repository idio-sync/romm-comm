# integrations/ggrequestz.py

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import aiohttp
from discord.ext import commands

logger = logging.getLogger(__name__)

# ggrequestz has no "one request by id" endpoint, so get_request_by_id pages
# the caller's request list. The page size is the list endpoint's own
# parameter; the page cap stops a bad id walking an unbounded history.
GGR_REQUEST_PAGE_SIZE = 100
GGR_REQUEST_MAX_PAGES = 20

# ggrequestz enforces API key scopes per route (src/lib/apiScopes.js), and
# resolution is default-deny: a key without the scope gets a 403 naming what it
# lacks, not a 401. These are the scopes this integration's calls need.
GGR_REQUIRED_SCOPES = (
    'requests:read',   # GET /api/request -- status sync
    'requests:write',  # POST /api/request -- mirroring a Discord request
    'games:read',      # GET /api/games/{id}, GET /api/search -- game_data cache
)

def _failure_from_response(status: int, body_text: str) -> Dict[str, Any]:
    """A failed call's result, keeping whatever ggrequestz said about it.

    ggrequestz explains its own refusals: a 409 from POST /api/request carries
    the reason and the `existing_request_id` of the request already open for
    that game, and a 403 names the scope the API key lacks. Reporting only
    "HTTP 409" threw all of that away and left the log saying no more than that
    something went wrong.
    """
    failure: Dict[str, Any] = {
        "success": False,
        "error": f"HTTP {status}",
        "status": status,
    }
    try:
        body = json.loads(body_text)
    except (json.JSONDecodeError, ValueError):
        return failure

    if not isinstance(body, dict):
        return failure

    if body.get('error'):
        failure["error"] = body['error']
    if body.get('existing_request_id') is not None:
        failure["existing_request_id"] = body['existing_request_id']
    if body.get('required_scope'):
        failure["required_scope"] = body['required_scope']
    return failure


class GGRequestzIntegration(commands.Cog):
    """Integration with GGRequestz API using API key authentication"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.enabled = True
        
        # Configuration comes from Config, which owns all environment reading.
        self.ggr_base_url = bot.config.GGREQUESTZ_URL
        self.ggr_api_key = bot.config.GGREQUESTZ_API_KEY
        
        # Session
        self.session: Optional[aiohttp.ClientSession] = None
        self._setup_complete = asyncio.Event()  # Track when setup is done

        # Map endpoints to their paths and auth methods
        self.endpoints = {
            'version': {'path': '/api/version', 'auth': 'bearer'},
            'search': {'path': '/api/search', 'auth': 'bearer'},
            'games': {'path': '/api/games', 'auth': 'bearer'},
            'request': {'path': '/api/request', 'auth': 'bearer'},
            'request_list': {'path': '/api/request', 'auth': 'bearer'},
        }
        
        # Validate config
        if not self.ggr_base_url:
            logger.error("GGREQUESTZ_URL is required")
            self.enabled = False
        elif not self.ggr_api_key:
            logger.error("GGREQUESTZ_API_KEY is required")
            self.enabled = False
        else:
            logger.debug(f"GGRequestz integration enabled - Base URL: {self.ggr_base_url}")
            bot.loop.create_task(self.setup())
    
    def get_endpoint_url(self, endpoint_name: str, path_params: Optional[str] = None) -> str:
        """Get the full URL for a named endpoint"""
        endpoint_info = self.endpoints.get(endpoint_name)
        if not endpoint_info:
            # If not in mapping, assume it uses /api prefix
            endpoint_path = f"/api/{endpoint_name}"
        else:
            endpoint_path = endpoint_info['path']
        
        # Add path parameters if provided (e.g., for /games/{id})
        if path_params:
            endpoint_path = f"{endpoint_path}/{path_params}"
        
        return f"{self.ggr_base_url}{endpoint_path}"
    
    async def ensure_session(self) -> bool:
        """Ensure session is initialized before making API calls"""
        if not self.enabled:
            return False
        # Wait for setup to complete (with timeout)
        try:
            await asyncio.wait_for(self._setup_complete.wait(), timeout=30.0)
        except TimeoutError:
            logger.error("Timeout waiting for GGRequestz session initialization")
            return False
        return self.session is not None and not self.session.closed

    async def setup(self):
        """Initialize the session and check the server is reachable.

        This used to be described as validating the API key. It cannot: on
        current ggrequestz /api/version is in publicApiRoutes, so it answers
        200 before authentication is even attempted, and a revoked, expired or
        mis-scoped key passes here just as well as a good one. What it does
        establish is that the URL points at a ggrequestz that answers, which is
        the other half of a misconfiguration and worth keeping.

        A bad key surfaces at first use instead, as a 401, or as a 403 naming
        the missing scope -- see GGR_REQUIRED_SCOPES.
        """
        if not self.enabled:
            self._setup_complete.set()  # Signal completion even if disabled
            return

        await self.bot.wait_until_ready()
        
        # Create session with SSL handling for self-signed certs
        import ssl
        ssl_context = ssl.create_default_context()
        
        # For local development with self-signed certs, disable verification
        # WARNING: Only use this for local/development environments
        if '192.168.' in self.ggr_base_url or 'localhost' in self.ggr_base_url:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            logger.warning("SSL verification disabled for local development server")
        
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30)
        )
        
        # Reachability check; see the docstring for what it does not prove.
        try:
            url = self.get_endpoint_url('version')
            async with self.session.get(url, headers=self.get_auth_headers('version')) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                        logger.error(f"❌ Invalid JSON response from version endpoint: {e}")
                        self.enabled = False
                        return
                    version = data.get('version', 'unknown')
                    logger.info(f"✅ GGRequestz reachable (v{version})")
                else:
                    logger.error(f"❌ GGRequestz version check failed: {response.status}")
                    self.enabled = False
        except Exception as e:
            logger.error(f"❌ GGRequestz version check error: {e}")
            self.enabled = False
        finally:
            # Signal that setup is complete (whether successful or not)
            self._setup_complete.set()
    
    def get_auth_headers(self, endpoint_name: Optional[str] = None) -> Dict[str, str]:
        """Get authenticated headers - Bearer auth for all endpoints"""
        return {
            'Authorization': f'Bearer {self.ggr_api_key}',
            'Content-Type': 'application/json'
        }
    
    async def get_game_details(self, igdb_id: str) -> Optional[Dict[str, Any]]:
        """Get detailed game information by IGDB ID"""
        if not await self.ensure_session():
            return None

        try:
            url = self.get_endpoint_url('games', igdb_id)

            async with self.session.get(
                url,
                headers=self.get_auth_headers('games')
            ) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                        logger.error(f"Invalid JSON response for game details {igdb_id}: {e}")
                        return None
                    # /api/games/{id} returns the game object itself -- `return
                    # json(game)`, with no envelope. src/lib/openapi.json still
                    # documents {"success": true, "game": {...}}, which is what
                    # this used to read, so every lookup came back None and the
                    # game_data cache below was silently never sent. Both
                    # shapes are accepted because the spec and the route
                    # disagree and only one of them is the server.
                    if not isinstance(data, dict) or not data:
                        return None
                    if data.get('success') and 'game' in data:
                        return data.get('game')
                    return data
                return None

        except Exception as e:
            logger.error(f"Error getting game details for IGDB ID {igdb_id}: {e}")
            return None
    
    async def search_game(self, game_name: str, platform: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Search for a game and return game info including IGDB ID"""
        if not await self.ensure_session():
            return None
        
        try:
            params = {
                'q': game_name,
                'per_page': 5
            }
            
            url = self.get_endpoint_url('search')
            
            async with self.session.get(
                url,
                params=params,
                headers=self.get_auth_headers('search')
            ) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                        logger.error(f"Invalid JSON response for game search '{game_name}': {e}")
                        return None
                    if data.get('success') and data.get('hits'):
                        hits = data['hits']
                        if hits:
                            # Filter by platform if specified
                            for hit in hits:
                                game = hit.get('document', {})
                                if platform:
                                    platforms = game.get('platforms', [])
                                    if platform.lower() in [p.lower() for p in platforms]:
                                        return game
                                else:
                                    return game

                            # Return first result if no platform match
                            return hits[0].get('document', {})

                logger.warning(f"Game search returned no results for: {game_name}")
                return None
                
        except Exception as e:
            logger.error(f"Error searching for game: {e}")
            return None
    
    def _build_game_data_cache(self, game_info: Dict[str, Any]) -> Dict[str, Any]:
        """Build game_data object for caching from game info"""
        game_data = {}
        
        # Map fields from game info to game_data structure
        field_mapping = {
            'title': 'title',
            'summary': 'summary',
            'cover_url': 'cover_url',
            'rating': 'rating',
            'release_date': 'release_date',
            'platforms': 'platforms',
            'genres': 'genres',
            'screenshots': 'screenshots',
            'videos': 'videos',
            'companies': 'companies',
            'game_modes': 'game_modes'
        }
        
        for source_field, target_field in field_mapping.items():
            if source_field in game_info and game_info[source_field] is not None:
                game_data[target_field] = game_info[source_field]
        
        return game_data if game_data else None
    
    async def create_request(self, game_name: str, platform: str, 
                           user_id: int, username: str,
                           igdb_id: Optional[str] = None,
                           details: Optional[str] = None,
                           discord_request_id: Optional[int] = None,
                           request_type: str = "game",
                           priority: str = "medium") -> Dict[str, Any]:
        """
        Create a request in GGRequestz with automatic game data caching
        
        Args:
            game_name: Title of the game
            platform: Platform name
            user_id: Discord user ID
            username: Discord username
            igdb_id: Optional IGDB ID (will search if not provided)
            details: Additional request details
            discord_request_id: Discord request ID for tracking
            request_type: Type of request ("game", "update", or "fix")
            priority: Priority level ("low", "medium", "high", "urgent")
        """
        if not await self.ensure_session():
            return {"success": False, "error": "Integration not enabled or session not ready"}

        try:
            url = self.get_endpoint_url('request')

            logger.debug(f"Attempting to create request at: {url}")
            
            # Build description
            description_parts = [
                "**Requested via Discord Bot**",
                f"• Discord User: {username} (ID: {user_id})",
                f"• Game: {game_name}",
                f"• Platform: {platform}",
            ]
            
            if discord_request_id:
                description_parts.append(f"• Discord Request ID: #{discord_request_id}")
            
            if details and details.strip():
                description_parts.append(f"\n**Additional Notes:**\n{details}")
            
            description = "\n".join(description_parts)
            
            # Build request data
            request_data = {
                "request_type": request_type,
                "title": game_name,
                "platforms": [platform] if platform else [],
                "priority": priority,
                "description": description
            }
            
            # Fetch game data for caching
            game_info = None
            if igdb_id:
                # If IGDB ID provided, get full game details
                request_data["igdb_id"] = str(igdb_id)
                game_info = await self.get_game_details(igdb_id)
            else:
                # Search for the game to get IGDB ID and details
                game_info = await self.search_game(game_name, platform)
                if game_info and game_info.get('igdb_id'):
                    request_data["igdb_id"] = str(game_info['igdb_id'])
            
            # Add game_data for caching if we found game info
            if game_info:
                game_data = self._build_game_data_cache(game_info)
                if game_data:
                    request_data["game_data"] = game_data
                    logger.debug(f"Including game_data cache for {game_name}")
            
            logger.debug(f"Creating request at: {url}")
            logger.debug(f"Request type: {request_type}, Priority: {priority}")
            
            # Use X-API-Key authentication for /request endpoint
            headers = self.get_auth_headers('request')
            
            async with self.session.post(
                url,
                json=request_data,
                headers=headers
            ) as response:
                response_text = await response.text()
                logger.debug(f"Response status: {response.status}")
                logger.debug(f"Response text: {response_text[:500]}")  # Log first 500 chars
                
                if response.status in [200, 201]:
                    try:
                        # Try to parse as JSON
                        response_data = json.loads(response_text)
                        
                        # Handle different response formats
                        # Some APIs return the created object directly without a success field
                        if response_data.get('success'):
                            request_obj = response_data.get('request', {})
                            request_id = request_obj.get('id')
                        elif response_data.get('id'):
                            # Response might be the request object directly
                            request_obj = response_data
                            request_id = response_data.get('id')
                        elif response_data.get('request_id'):
                            # Alternative format
                            request_id = response_data.get('request_id')
                            request_obj = response_data
                        else:
                            # Log the actual structure to understand it
                            logger.warning(f"Unexpected response structure: {response_data}")
                            # Still consider it successful if we got 200/201
                            request_id = response_data.get('id') or response_data.get('request_id') or 'unknown'
                            request_obj = response_data
                        
                        logger.debug(f"✅ Created GGRequestz {request_type} request for {game_name} (ID: {request_id})")
                        return {
                            "success": True,
                            "request_id": request_id,
                            "data": request_obj
                        }
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse JSON response: {e}")
                        logger.error(f"Response text was: {response_text[:500]}")
                        # If we got 200/201 but can't parse, still consider it success
                        return {
                            "success": True,
                            "request_id": "unknown",
                            "data": {"raw_response": response_text[:500]}
                        }
                else:
                    logger.error(f"Request creation failed with status {response.status}")
                    logger.error(f"URL attempted: {url}")
                    logger.error(f"Response: {response_text[:500]}")
                    return _failure_from_response(response.status, response_text)
                    
        except Exception as e:
            logger.error(f"Error creating request: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def get_user_requests(self, limit: int = 20, offset: int = 0,
                               status: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Get current user's requests

        Args:
            limit: Number of requests to return
            offset: Offset for pagination
            status: Filter by status (pending, approved, fulfilled, rejected, cancelled)
        """
        if not await self.ensure_session():
            return None

        try:
            url = self.get_endpoint_url('request_list')
            params = {
                'limit': limit,
                'offset': offset
            }
            if status:
                params['status'] = status
            
            async with self.session.get(
                url,
                params=params,
                headers=self.get_auth_headers('request_list')
            ) as response:
                if response.status == 200:
                    try:
                        data = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                        logger.error(f"Invalid JSON response for user requests: {e}")
                        return None
                    if data.get('success'):
                        return data
                return None
                
        except Exception as e:
            logger.error(f"Error getting user requests: {e}")
            return None
    
    async def get_request_by_id(self, request_id) -> Optional[Dict[str, Any]]:
        """One request as ggrequestz currently sees it, or None.

        ggrequestz has no endpoint for a single request: /api/request offers
        POST (create) and GET (list the caller's own requests) and nothing
        else. So this pages the list and matches on id.

        Two details that make a naive version silently never match:

        - the id's type is not guaranteed on either side: ggr_request_id is
          whatever was stored when the request was created, and the list
          serialises the column as ggrequestz's driver hands it over. A direct
          == would silently never match on a mismatch, so both are compared as
          str();
        - the list response carries no `admin_notes`. That field exists only
          on the admin update response, so a status synced back from
          ggrequestz arrives without the reason attached, and the caller's
          note falls back to a bare "Synced from ggrequestz".

        Verified against XTREEMMAK/ggrequestz src/lib/openapi.json.
        """
        if not await self.ensure_session():
            return None

        wanted = str(request_id)
        offset = 0
        for _ in range(GGR_REQUEST_MAX_PAGES):
            page = await self.get_user_requests(
                limit=GGR_REQUEST_PAGE_SIZE, offset=offset
            )
            if not page:
                return None

            entries = page.get('requests') or []
            for entry in entries:
                if str(entry.get('id')) == wanted:
                    return entry

            # A short page is the last page.
            if len(entries) < GGR_REQUEST_PAGE_SIZE:
                return None
            offset += GGR_REQUEST_PAGE_SIZE

        logger.warning(
            f"Gave up looking for ggrequestz request {request_id} after "
            f"{GGR_REQUEST_MAX_PAGES} pages"
        )
        return None

    async def cog_unload(self):
        """Cleanup session on cog unload"""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.debug("GGRequestz session closed")


def setup(bot):
    bot.add_cog(GGRequestzIntegration(bot))
