# integrations/ggrequestz.py

import discord
from discord.ext import commands
import aiohttp
import logging
import os
import asyncio
from typing import Optional, Dict, Any
from http.cookies import SimpleCookie

logger = logging.getLogger(__name__)

class GGRequestzIntegration(commands.Cog):
    """Minimal integration with GGRequestz"""
    
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.enabled = True
        
        # Configuration - normalize the base URL
        base_url = os.getenv('GGREQUESTZ_URL', '').rstrip('/')
        # Remove /api if present - we'll add it per endpoint
        if base_url.endswith('/api'):
            base_url = base_url[:-4]
        self.ggr_base_url = base_url
        
        self.ggr_username = os.getenv('GGREQUESTZ_USERNAME')
        self.ggr_password = os.getenv('GGREQUESTZ_PASSWORD')
        
        # Session and cookies
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookies: Dict[str, str] = {}  # Store cookies manually
        
        # Validate config
        if not all([self.ggr_base_url, self.ggr_username, self.ggr_password]):
            logger.error("GGRequestz integration missing required environment variables")
            self.enabled = False
        else:
            logger.info(f"GGRequestz integration enabled - URL: {self.ggr_base_url}")
            bot.loop.create_task(self.setup())
    
    async def setup(self):
        """Initialize connection"""
        if not self.enabled:
            return
        
        await self.bot.wait_until_ready()
        
        # Simple session without cookie jar - we'll manage cookies manually
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        )
        
        try:
            auth_success = await self.authenticate()
            if auth_success:
                logger.info("✅ GGRequestz authentication successful")
            else:
                logger.error("❌ GGRequestz authentication failed - check credentials")
                self.enabled = False
        except Exception as e:
            logger.error(f"❌ GGRequestz authentication exception: {e}")
            self.enabled = False
    
    async def authenticate(self) -> bool:
        """Authenticate with GGRequestz"""
        try:
            auth_url = f"{self.ggr_base_url}/api/auth/basic/login"
            
            form_data = aiohttp.FormData()
            form_data.add_field('username', self.ggr_username)
            form_data.add_field('password', self.ggr_password)
            
            logger.debug(f"Authenticating to: {auth_url}")
            
            async with self.session.post(
                auth_url,
                data=form_data,
                allow_redirects=False  # Don't follow redirects
            ) as response:
                logger.debug(f"Auth response status: {response.status}")
                
                if response.status in [200, 302]:
                    # Extract cookies from Set-Cookie header
                    set_cookie_header = response.headers.get('Set-Cookie', '')
                    logger.debug(f"Set-Cookie header: {set_cookie_header}")
                    
                    if set_cookie_header:
                        # Parse the cookie
                        cookie = SimpleCookie()
                        cookie.load(set_cookie_header)
                        
                        # Extract all cookies
                        for key, morsel in cookie.items():
                            self.cookies[key] = morsel.value
                            logger.debug(f"Stored cookie: {key}={morsel.value[:20]}...")
                        
                        if self.cookies:
                            logger.info("✅ Successfully authenticated with GGRequestz")
                            return True
                    
                    logger.error("Authentication returned success but no cookies in Set-Cookie header")
                    return False
                else:
                    response_text = await response.text()
                    logger.error(f"Authentication failed with status {response.status}: {response_text[:200]}")
                    return False
                
        except Exception as e:
            logger.error(f"Authentication error: {e}", exc_info=True)
            return False
    
    def get_auth_headers(self) -> Dict[str, str]:
        """Get authenticated headers with cookies"""
        headers = {'Content-Type': 'application/json'}
        
        if self.cookies:
            # Build cookie string from stored cookies
            cookie_str = '; '.join([f"{k}={v}" for k, v in self.cookies.items()])
            headers['Cookie'] = cookie_str
            logger.debug(f"Sending cookies: {cookie_str[:50]}...")
        
        return headers
    
    async def search_game(self, game_name: str, platform: Optional[str] = None) -> Optional[str]:
        """Search for a game and return its game_id"""
        if not self.enabled or not self.cookies:
            return None
        
        try:
            params = {'q': game_name, 'limit': 5}
            if platform:
                params['platforms'] = platform
            
            url = f"{self.ggr_base_url}/api/games/search"
            
            async with self.session.get(
                url,
                params=params,
                headers=self.get_auth_headers()
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('success') and data.get('data'):
                        games = data['data']
                        if games:
                            game = games[0]
                            return game.get('igdb_id') or game.get('id')
                
                logger.warning(f"Game search returned no results for: {game_name}")
                return None
                
        except Exception as e:
            logger.error(f"Error searching for game: {e}")
            return None
    
    async def create_request(self, game_name: str, platform: str, 
                           user_id: int, username: str,
                           igdb_id: Optional[int] = None,
                           details: Optional[str] = None,
                           discord_request_id: Optional[int] = None,
                           _retry: bool = True) -> Dict[str, Any]:
        """Create a request in GGRequestz"""
        if not self.enabled:
            return {"success": False, "error": "Integration not enabled"}
        
        if not self.cookies:
            logger.warning("No cookies, attempting re-authentication")
            if not await self.authenticate():
                return {"success": False, "error": "Authentication failed"}
        
        try:
            url = f"{self.ggr_base_url}/request"
            
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
            
            request_data = {
                "request_type": "game",
                "title": game_name,
                "igdb_id": igdb_id,
                "platforms": [platform] if platform else [],
                "priority": "medium",
                "description": description,
                "reason": f"Discord request from {username} (ID: {user_id})"
            }
            
            logger.debug(f"Creating request at: {url}")
            logger.debug(f"Request data: {request_data}")
            
            async with self.session.post(
                url,
                json=request_data,
                headers=self.get_auth_headers()
            ) as response:
                response_text = await response.text()
                logger.debug(f"Response status: {response.status}")
                logger.debug(f"Response text: {response_text}")
                
                if response.status in [200, 201]:
                    try:
                        response_data = await response.json()
                        logger.debug(f"Parsed response data: {response_data}")
                        
                        # Check for redirect response
                        if response_data.get('type') == 'redirect':
                            logger.warning(f"Received redirect to {response_data.get('location')}, session expired")
                            if _retry:
                                logger.info("Attempting re-authentication")
                                self.cookies = {}
                                if await self.authenticate():
                                    await asyncio.sleep(0.5)
                                    return await self.create_request(
                                        game_name, platform, user_id, username, 
                                        igdb_id, details, discord_request_id, _retry=False
                                    )
                            return {"success": False, "error": "Session expired"}
                        
                        if response_data.get('success'):
                            request_id = response_data.get('request', {}).get('id')
                            logger.info(f"Created GGRequestz request for {game_name} (ID: {request_id})")
                            return {
                                "success": True,
                                "request_id": request_id
                            }
                        else:
                            error_msg = response_data.get('error', 'Unknown error')
                            logger.error(f"Failed to create request: {error_msg}")
                            logger.error(f"Full response data: {response_data}")
                            return {"success": False, "error": error_msg}
                    except Exception as e:
                        logger.error(f"Failed to parse response: {e}")
                        logger.error(f"Response text was: {response_text}")
                        return {"success": False, "error": "Invalid response from server"}
                        
                elif response.status == 401 and _retry:
                    logger.info("Received 401, attempting re-authentication")
                    self.cookies = {}
                    if await self.authenticate():
                        await asyncio.sleep(0.5)
                        return await self.create_request(
                            game_name, platform, user_id, username, 
                            igdb_id, details, discord_request_id, _retry=False
                        )
                    return {"success": False, "error": "Re-authentication failed"}
                else:
                    logger.error(f"Request creation failed with status {response.status}: {response_text}")
                    return {"success": False, "error": f"HTTP {response.status}"}
                    
        except Exception as e:
            logger.error(f"Error creating request: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def update_request_status(self, ggr_request_id: int, status: str,
                                   admin_name: str,
                                   notes: Optional[str] = None,
                                   _retry: bool = True) -> Dict[str, Any]:
        """Update request status in GGRequestz"""
        if not self.enabled:
            return {"success": False, "error": "Integration not enabled"}
        
        if not self.cookies:
            if not await self.authenticate():
                return {"success": False, "error": "Authentication failed"}
        
        try:
            url = f"{self.ggr_base_url}/admin/api/requests/update"
            
            update_data = {
                "request_id": str(ggr_request_id),
                "status": status,
                "admin_notes": notes or f"Updated by {admin_name} via Discord"
            }
            
            logger.debug(f"Updating request {ggr_request_id} to status '{status}'")
            
            async with self.session.post(
                url,
                json=update_data,
                headers=self.get_auth_headers()
            ) as response:
                response_text = await response.text()
                logger.debug(f"Update response: {response.status} - {response_text[:200]}")
                
                if response.status == 200:
                    try:
                        data = await response.json()
                        
                        # Check for redirect response
                        if data.get('type') == 'redirect':
                            logger.warning(f"Received redirect, session expired")
                            if _retry:
                                self.cookies = {}
                                if await self.authenticate():
                                    await asyncio.sleep(0.5)
                                    return await self.update_request_status(
                                        ggr_request_id, status, admin_name, notes, _retry=False
                                    )
                            return {"success": False, "error": "Session expired"}
                        
                        if data.get('success'):
                            logger.info(f"✅ Successfully updated request {ggr_request_id} to {status}")
                            return {"success": True}
                        else:
                            error_msg = data.get('error', 'Unknown error')
                            return {"success": False, "error": error_msg}
                    except:
                        return {"success": False, "error": f"Invalid response: {response_text[:100]}"}
                        
                elif response.status == 401 and _retry:
                    logger.info("Received 401, attempting re-authentication")
                    self.cookies = {}
                    if await self.authenticate():
                        await asyncio.sleep(0.5)
                        return await self.update_request_status(
                            ggr_request_id, status, admin_name, notes, _retry=False
                        )
                    return {"success": False, "error": "Re-authentication failed"}
                else:
                    logger.error(f"Status update failed: {response.status} - {response_text[:200]}")
                    return {"success": False, "error": f"HTTP {response.status}: {response_text[:100]}"}
                    
        except Exception as e:
            logger.error(f"Error updating status: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
    
    async def get_request_details(self, ggr_request_id: int) -> Optional[Dict[str, Any]]:
        """Get request details from GGRequestz"""
        if not self.enabled or not self.cookies:
            return None
        
        try:
            url = f"{self.ggr_base_url}/request"
            
            async with self.session.get(
                url,
                headers=self.get_auth_headers()
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('success') and data.get('requests'):
                        for req in data['requests']:
                            if req.get('id') == str(ggr_request_id) or req.get('id') == ggr_request_id:
                                return req
                return None
                
        except Exception as e:
            logger.error(f"Error getting request details: {e}")
            return None
    
    async def get_request_by_id(self, ggr_request_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific request by ID from GGRequestz"""
        if not self.enabled or not self.cookies:
            return None
        
        try:
            # Use the API to get request details
            url = f"{self.ggr_base_url}/admin/api/requests/{ggr_request_id}"
            
            async with self.session.get(
                url,
                headers=self.get_auth_headers()
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('success'):
                        return data.get('request')
                return None
                
        except Exception as e:
            logger.error(f"Error getting request {ggr_request_id}: {e}")
            return None
    
    def cog_unload(self):
        """Cleanup"""
        if self.session:
            asyncio.create_task(self.session.close())


def setup(bot):
    bot.add_cog(GGRequestzIntegration(bot))
