# integrations/ggrequestz.py

import discord
from discord.ext import commands
import aiohttp
import logging
import os
import asyncio
import json
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

class GGRequestzIntegration(commands.Cog):
    """Integration with GGRequestz API using API key authentication"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.enabled = True
        
        # Configuration - store base URL without /api
        base_url = os.getenv('GGREQUESTZ_URL', '').rstrip('/')
        # Remove /api if it was included in the env var
        if base_url.endswith('/api'):
            base_url = base_url[:-4]
        self.ggr_base_url = base_url
        
        # API Key authentication (required)
        self.ggr_api_key = os.getenv('GGREQUESTZ_API_KEY')
        
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
            'rescind': {'path': '/api/request/rescind', 'auth': 'bearer'},
            'watchlist_add': {'path': '/api/watchlist/add', 'auth': 'bearer'},
            'watchlist_remove': {'path': '/api/watchlist/remove', 'auth': 'bearer'}, 
            'watchlist_status': {'path': '/api/watchlist/status', 'auth': 'bearer'},
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
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for GGRequestz session initialization")
            return False
        return self.session is not None and not self.session.closed

    async def setup(self):
        """Initialize connection and validate API key"""
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
        
        # Test the API key with a simple request
        try:
            url = self.get_endpoint_url('version')
            async with self.session.get(url, headers=self.get_auth_headers('version')) as response:
                if response.status == 200:
                    data = await response.json()
                    version = data.get('version', 'unknown')
                    logger.info(f"✅ GGRequestz API key validated successfully (v{version})")
                else:
                    logger.error(f"❌ API key validation failed: {response.status}")
                    self.enabled = False
        except Exception as e:
            logger.error(f"❌ API key validation error: {e}")
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
                    data = await response.json()
                    if data.get('success'):
                        return data.get('game')
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
                    data = await response.json()
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
                f"**Requested via Discord Bot**",
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
                    return {"success": False, "error": f"HTTP {response.status}"}
                    
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
                    data = await response.json()
                    if data.get('success'):
                        return data
                return None
                
        except Exception as e:
            logger.error(f"Error getting user requests: {e}")
            return None
    
    async def rescind_request(self, request_id: str) -> Dict[str, Any]:
        """Rescind/cancel a request"""
        if not await self.ensure_session():
            return {"success": False, "error": "Integration not enabled or session not ready"}

        try:
            url = self.get_endpoint_url('rescind')
            
            request_data = {
                "request_id": request_id  # Changed from requestId
            }
            
            async with self.session.post(
                url,
                json=request_data,
                headers=self.get_auth_headers('rescind')
            ) as response:
                response_text = await response.text()
                
                if response.status == 200:
                    try:
                        data = json.loads(response_text)
                        if data.get('success'):
                            logger.info(f"✅ Successfully rescinded request {request_id}")
                            return {"success": True}
                        else:
                            return {"success": False, "error": data.get('error', 'Unknown error')}
                    except:
                        return {"success": False, "error": f"Invalid response: {response_text[:100]}"}
                else:
                    logger.error(f"Rescind failed: {response.status} - {response_text[:200]}")
                    return {"success": False, "error": f"HTTP {response.status}"}
                    
        except Exception as e:
            logger.error(f"Error rescinding request: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def add_to_watchlist(self, igdb_id: str) -> Dict[str, Any]:
        """Add a game to the user's watchlist"""
        if not await self.ensure_session():
            return {"success": False, "error": "Integration not enabled or session not ready"}

        try:
            url = self.get_endpoint_url('watchlist_add')
            
            async with self.session.post(
                url,
                json={"igdb_id": igdb_id},  # Correct field name
                headers=self.get_auth_headers('watchlist_add')
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    return {"success": False, "error": f"HTTP {response.status}"}
                    
        except Exception as e:
            logger.error(f"Error adding to watchlist: {e}")
            return {"success": False, "error": str(e)}
    
    async def remove_from_watchlist(self, igdb_id: str) -> Dict[str, Any]:
        """Remove a game from the user's watchlist"""
        if not await self.ensure_session():
            return {"success": False, "error": "Integration not enabled or session not ready"}

        try:
            url = self.get_endpoint_url('watchlist_remove')
            
            async with self.session.post(
                url,
                json={"igdb_id": igdb_id},
                headers=self.get_auth_headers('watchlist_remove')
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data
                else:
                    return {"success": False, "error": f"HTTP {response.status}"}
                    
        except Exception as e:
            logger.error(f"Error removing from watchlist: {e}")
            return {"success": False, "error": str(e)}
    
    async def check_watchlist_status(self, igdb_id: str) -> bool:
        """Check if a game is in the user's watchlist"""
        if not await self.ensure_session():
            return False

        try:
            # Include the ID in the path
            url = f"{self.ggr_base_url}/api/watchlist/status/{igdb_id}"
            
            async with self.session.get(
                url,
                headers=self.get_auth_headers('watchlist_status')
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    return data.get('isInWatchlist', False)  # Changed from inWatchlist
                return False
                
        except Exception as e:
            logger.error(f"Error checking watchlist: {e}")
            return False
    
    def cog_unload(self):
        """Cleanup"""
        if self.session:
            asyncio.create_task(self.session.close())


def setup(bot):
    bot.add_cog(GGRequestzIntegration(bot))
