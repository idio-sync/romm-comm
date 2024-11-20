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

    async def search_game(self, game_name: str, platform_name: str = None) -> List[Dict]:
        """Search for a game on IGDB including alternative names"""
        try:
            if not await self.get_access_token():
                return []

            session = await self.ensure_session()
            url = "https://api.igdb.com/v4/games"
            
            # Build the IGDB query including alternative names
            query = (
                f'search "{game_name}"; '
                'fields name,alternative_names.name,platforms.name,first_release_date,'
                'summary,cover.url,game_modes.name,genres.name,involved_companies.company.name,'
                'involved_companies.developer,involved_companies.publisher;'
            )
            
            if platform_name:
                # Add platform filter if specified
                query += f' where platforms.name ~ *"{platform_name}"*;'
            query += " limit 10;"

            headers = {
                "Client-ID": self.client_id,
                "Authorization": f"Bearer {self.access_token}"
            }

            async with session.post(url, headers=headers, data=query) as response:
                if response.status == 200:
                    games = await response.json()
                    processed_games = self._process_games_response(games)
                    
                    # Get alternative names for each game
                    alt_names_url = "https://api.igdb.com/v4/alternative_names"
                    game_ids = [g["id"] for g in games if "id" in g]
                    if game_ids:
                        alt_names_query = f"fields name,comment,game; where game = ({','.join(map(str, game_ids))});"
                        async with session.post(alt_names_url, headers=headers, data=alt_names_query) as alt_response:
                            if alt_response.status == 200:
                                alt_names_data = await alt_response.json()
                                # Add alternative names to processed games
                                self._add_alternative_names(processed_games, alt_names_data)
                    
                    return processed_games
                else:
                    logger.error(f"IGDB API error: {response.status}")
                    return []

        except Exception as e:
            logger.error(f"Error searching IGDB: {e}")
            return []

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
                    "alternative_names": []  # Will be populated later
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