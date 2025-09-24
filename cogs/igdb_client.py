from typing import List, Dict, Optional, Tuple
import aiohttp
import logging
from datetime import datetime, timedelta
import os
from dotenv import load_dotenv
import re

logger = logging.getLogger(__name__)

class IGDBClient:
    """IGDB API client for game metadata"""
    def __init__(self):
        self.client_id = os.getenv('IGDB_CLIENT_ID')
        self.client_secret = os.getenv('IGDB_CLIENT_SECRET')
        self.access_token = None
        self.token_expires = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._platform_cache = {}  # Cache for platform slug to ID mapping
        
        if not all([self.client_id, self.client_secret]):
            logger.warning("IGDB credentials not found in environment variables")
            raise ValueError("IGDB credentials not found in environment variables")

    async def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure an active session exists and return it."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_access_token(self) -> bool:
        """Get or refresh Twitch OAuth token for IGDB access"""
        try:
            if self.access_token and self.token_expires and datetime.now() < self.token_expires:
                return True

            session = await self.ensure_session()
            url = f"https://id.twitch.tv/oauth2/token"
            params = {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials"
            }

            async with session.post(url, params=params) as response:
                if response.status == 200:
                    data = await response.json()
                    self.access_token = data["access_token"]
                    self.token_expires = datetime.now() + timedelta(seconds=data["expires_in"] - 100)
                    return True
                else:
                    logger.error(f"Failed to get IGDB token: {response.status}")
                    return False

        except Exception as e:
            logger.error(f"Error getting IGDB token: {e}")
            return False

    async def get_platform_id_from_slug(self, platform_slug: str) -> Optional[int]:
        """Get IGDB platform ID from slug"""
        if not platform_slug:
            return None
            
        # Check cache first
        if platform_slug in self._platform_cache:
            return self._platform_cache[platform_slug]
        
        try:
            if not await self.get_access_token():
                return None
            
            session = await self.ensure_session()
            headers = {
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.access_token}"
            }
            
            # Query IGDB platforms endpoint
            url = "https://api.igdb.com/v4/platforms"
            query = f'fields id,name,slug; where slug = "{platform_slug}"; limit 1;'
            
            async with session.post(url, headers=headers, data=query) as response:
                if response.status == 200:
                    platforms = await response.json()
                    if platforms and len(platforms) > 0:
                        platform_id = platforms[0].get("id")
                        # Cache the result
                        self._platform_cache[platform_slug] = platform_id
                        logger.debug(f"Mapped platform slug '{platform_slug}' to ID {platform_id}")
                        return platform_id
                    else:
                        logger.debug(f"No IGDB platform found for slug: {platform_slug}")
                        # Cache the negative result too
                        self._platform_cache[platform_slug] = None
                        return None
                else:
                    logger.error(f"Failed to query IGDB platforms: {response.status}")
                    return None
                    
        except Exception as e:
            logger.error(f"Error getting platform ID for slug '{platform_slug}': {e}")
            return None

    def prepare_search_term(self, game_name: str) -> str:
        """Prepare search term for IGDB - handle common issues"""
        # Remove special characters that might cause issues but keep apostrophes and hyphens
        cleaned = re.sub(r'[^\w\s\'-]', '', game_name)
        
        # Handle Roman numerals and common abbreviations
        replacements = {
            ' ii ': ' 2 ',
            ' iii ': ' 3 ',
            ' iv ': ' 4 ',
            ' v ': ' 5 ',
            ' vi ': ' 6 ',
            ' vii ': ' 7 ',
            ' viii ': ' 8 ',
            ' ix ': ' 9 ',
            ' x ': ' 10 ',
        }
        
        cleaned_lower = ' ' + cleaned.lower() + ' '
        for roman, arabic in replacements.items():
            cleaned_lower = cleaned_lower.replace(roman, arabic)
        
        return cleaned_lower.strip()

    async def search_game(self, game_name: str, platform_slug: str = None) -> List[Dict]:
        """Search for a game on IGDB with platform filtering"""
        try:
            if not await self.get_access_token():
                return []

            # Get platform ID from slug if provided
            platform_id = None
            if platform_slug:
                platform_id = await self.get_platform_id_from_slug(platform_slug)
                # If we can't map the platform, log but continue search without platform filter
                if platform_id is None:
                    logger.info(f"Platform slug '{platform_slug}' not found in IGDB, searching all platforms")

            all_results = []
            seen_ids = set()
            
            # Prepare multiple search attempts
            search_attempts = []
            
            # 1. Original search term
            search_attempts.append(game_name.strip())
            
            # 2. Cleaned/prepared search term
            prepared_term = self.prepare_search_term(game_name)
            if prepared_term != game_name.strip():
                search_attempts.append(prepared_term)
            
            # 3. If multiple words, try key parts
            words = game_name.strip().split()
            if len(words) > 1:
                # Try first word only (often the main game name)
                search_attempts.append(words[0])
                
                # If last word is a number, try first word + number (for sequels)
                if words[-1].isdigit():
                    search_attempts.append(f"{words[0]} {words[-1]}")
                
                # Try without common words
                important_words = [w for w in words if w.lower() not in ['the', 'of', 'and', 'a', 'an']]
                if len(important_words) > 0 and len(important_words) < len(words):
                    search_attempts.append(' '.join(important_words))
            
            # Remove duplicates while preserving order
            seen = set()
            unique_attempts = []
            for attempt in search_attempts:
                if attempt.lower() not in seen:
                    seen.add(attempt.lower())
                    unique_attempts.append(attempt)
            search_attempts = unique_attempts[:3]
            
            session = await self.ensure_session()
            headers = {
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.access_token}"
            }
            
            # Try each search strategy
            for attempt in search_attempts:
                results = await self._perform_igdb_search(session, headers, attempt, platform_id)
                
                # Add unique results
                for game in results:
                    if game["id"] not in seen_ids:
                        seen_ids.add(game["id"])
                        all_results.append(game)
                
                # Stop if we have enough good results
                if len(all_results) >= 5:
                    break
            
            # Sort results by relevance
            all_results = self._sort_by_relevance(all_results, game_name, platform_id)
            
            return all_results[:10]
            
        except Exception as e:
            logger.error(f"Error searching IGDB: {e}")
            return []

    async def _perform_igdb_search(self, session: aiohttp.ClientSession, headers: dict, 
                                  search_term: str, platform_id: Optional[int] = None) -> List[Dict]:
        """Perform a single IGDB search"""
        try:
            url = "https://api.igdb.com/v4/games"
            
            # Build the IGDB query
            query = (
                f'search "{search_term}"; '
                'fields name,alternative_names.name,platforms.name,first_release_date,'
                'summary,cover.url,game_modes.name,genres.name,involved_companies.company.name,'
                'involved_companies.developer,involved_companies.publisher;'
            )
            
            # Add platform filter if we have a valid platform ID
            if platform_id:
                query += f' where platforms = [{platform_id}];'
            
            query += " limit 20;"
            
            logger.debug(f"IGDB query: {query}")
            
            async with session.post(url, headers=headers, data=query) as response:
                if response.status == 200:
                    games = await response.json()
                    processed_games = self._process_games_response(games)
                    
                    # Get alternative names for better matching
                    if processed_games:
                        await self._fetch_alternative_names(session, headers, processed_games)
                    
                    return processed_games
                else:
                    logger.debug(f"IGDB search returned status {response.status} for term: {search_term}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error in IGDB search for '{search_term}': {e}")
            return []

    async def _fetch_alternative_names(self, session: aiohttp.ClientSession, headers: dict, 
                                      processed_games: List[Dict]):
        """Fetch and add alternative names to games"""
        try:
            game_ids = [g["id"] for g in processed_games if "id" in g]
            if not game_ids:
                return
                
            alt_names_url = "https://api.igdb.com/v4/alternative_names"
            alt_names_query = f"fields name,comment,game; where game = ({','.join(map(str, game_ids))});"
            
            async with session.post(alt_names_url, headers=headers, data=alt_names_query) as response:
                if response.status == 200:
                    alt_names_data = await response.json()
                    self._add_alternative_names(processed_games, alt_names_data)
                    
        except Exception as e:
            logger.debug(f"Error fetching alternative names: {e}")

    def _sort_by_relevance(self, games: List[Dict], original_query: str, platform_id: Optional[int] = None) -> List[Dict]:
        """Sort games by relevance to the original query"""
        def calculate_relevance(game: Dict) -> float:
            score = 0.0
            query_lower = original_query.lower()
            game_name_lower = game["name"].lower()
            
            # Exact match
            if game_name_lower == query_lower:
                score += 100
            # Starts with query
            elif game_name_lower.startswith(query_lower):
                score += 80
            # Contains exact query
            elif query_lower in game_name_lower:
                score += 60
            
            # Check alternative names
            for alt_name in game.get("alternative_names", []):
                alt_lower = alt_name["name"].lower()
                if alt_lower == query_lower:
                    score += 90
                elif alt_lower.startswith(query_lower):
                    score += 70
                elif query_lower in alt_lower:
                    score += 50
            
            # Bonus for matching platform (if platform filter was requested)
            if platform_id and game.get("platforms"):
                # Check if game is on the requested platform
                platform_names = [p.lower() for p in game.get("platforms", [])]
                if platform_names:
                    score += 10  # Bonus for having the platform we searched for
            
            # Bonus for having cover art
            if game.get("cover_url"):
                score += 5
            
            # Bonus for having release date
            if game.get("release_date") != "Unknown":
                score += 3
            
            # Bonus for having summary
            if game.get("summary") and game["summary"] != "No summary available":
                score += 2
            
            return score
        
        return sorted(games, key=calculate_relevance, reverse=True)

    def _process_games_response(self, games: List[Dict]) -> List[Dict]:
        """Process and format IGDB game data"""
        processed_games = []
        for game in games:
            try:
                # Format the release date
                release_date = "Unknown"
                if "first_release_date" in game:
                    release_date = datetime.fromtimestamp(
                        game["first_release_date"]
                    ).strftime("%Y-%m-%d")

                # Process cover URL if present
                cover_url = None
                if "cover" in game and "url" in game["cover"]:
                    cover_url = game["cover"]["url"].replace("t_thumb", "t_cover_big")
                    if not cover_url.startswith("https:"):
                        cover_url = "https:" + cover_url

                # Get platform names if available
                platforms = []
                if "platforms" in game:
                    platforms = [p.get("name", "") for p in game["platforms"]]

                # Get developers and publishers
                developers = []
                publishers = []
                if "involved_companies" in game:
                    for company in game["involved_companies"]:
                        company_name = company.get("company", {}).get("name", "")
                        if company.get("developer"):
                            developers.append(company_name)
                        if company.get("publisher"):
                            publishers.append(company_name)

                # Get genres and game modes
                genres = [g.get("name", "") for g in game.get("genres", [])]
                game_modes = [m.get("name", "") for m in game.get("game_modes", [])]

                processed_game = {
                    "id": game.get("id"),
                    "name": game.get("name", "Unknown"),
                    "platforms": platforms,
                    "release_date": release_date,
                    "summary": game.get("summary", "No summary available"),
                    "cover_url": cover_url,
                    "developers": developers,
                    "publishers": publishers,
                    "genres": genres,
                    "game_modes": game_modes,
                    "alternative_names": []
                }
                processed_games.append(processed_game)

            except Exception as e:
                logger.error(f"Error processing game data: {e}")
                continue

        return processed_games

    def _add_alternative_names(self, processed_games: List[Dict], alt_names_data: List[Dict]):
        """Add alternative names to processed games"""
        # Create a mapping of game IDs to their alternative names
        alt_names_map = {}
        for alt_name in alt_names_data:
            game_id = alt_name.get("game")
            if game_id:
                if game_id not in alt_names_map:
                    alt_names_map[game_id] = []
                name_info = {
                    "name": alt_name.get("name", ""),
                    "comment": alt_name.get("comment", "")
                }
                alt_names_map[game_id].append(name_info)

        # Add alternative names to each processed game
        for game in processed_games:
            if game["id"] in alt_names_map:
                game["alternative_names"] = alt_names_map[game["id"]]

    async def close(self):
        """Clean up resources"""
        if self._session and not self._session.closed:
            await self._session.close()
