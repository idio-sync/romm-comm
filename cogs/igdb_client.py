from typing import List, Dict, Optional
import aiohttp
import logging
from datetime import datetime, timedelta
import os
import re
import discord
from discord.ext import commands

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


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

                # Process websites
                websites = {}
                if "websites" in game:
                    logger.debug(f"Processing websites for {game.get('name')}: {game['websites']}")
                    for website in game["websites"]:
                        website_type = website.get("type")  # Changed from 'category' to 'type'
                        url = website.get("url")
                        if website_type and url:
                            # Map type numbers to friendly names
                            type_map = {
                                1: "official",
                                13: "steam",
                                16: "epic",
                                17: "gog",
                                5: "twitter",
                                6: "twitch",
                                9: "youtube",
                                8: "instagram",
                                4: "facebook",
                                3: "wikipedia"
                            }
                            if website_type in type_map:
                                websites[type_map[website_type]] = url
                    logger.debug(f"Processed websites: {websites}")
                else:
                    logger.debug(f"No websites field for {game.get('name')}")

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
                    "alternative_names": [],
                    "websites": websites,
                    "rating": game.get("rating"),
                    "rating_count": game.get("rating_count"),
                    "hypes": game.get("hypes")
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


class IGDBGameView(discord.ui.View):
    """Interactive view for browsing IGDB games with detailed display"""
    
    def __init__(self, bot, games: List[Dict], title: str, platform_name: Optional[str] = None, show_full_date: bool = False, view_type: str = "upcoming"):
        super().__init__(timeout=300)
        self.bot = bot
        self.all_games = games  # Store all games for pagination
        self.title = title
        self.platform_name = platform_name
        self.show_full_date = show_full_date  # True for upcoming releases
        self.view_type = view_type  # "upcoming" or "popular"
        self.message = None
        self.viewing_detail = False  # Track if viewing detail or list
        
        # Pagination
        self.current_page = 0
        self.games_per_page = 25
        
        # Sorting
        if view_type == "upcoming":
            self.sort_method = "soonest"  # or "anticipated"
        elif view_type == "recent":
            self.sort_method = "newest"  # or "highest_rated"
        elif view_type == "exclusives":
            self.sort_method = "most_rated"  # or "highest_rated"
        else:  # popular
            self.sort_method = "most_rated"  # or "highest_rated"
        
        # Apply initial sort
        self._sort_games()
        
        # Get current page games
        self.games = self._get_current_page_games()
        
        # Create select menu with game options
        self.game_select = discord.ui.Select(
            placeholder="Select a game to view details...",
            min_values=1,
            max_values=1,
            row=0
        )
        
        self._populate_game_select()
        
        self.game_select.callback = self.select_callback
        self.add_item(self.game_select)
        
        # Navigation row (row 1)
        # Previous Page button
        self.prev_button = discord.ui.Button(
            label="◀ Previous",
            style=discord.ButtonStyle.primary,
            row=1,
            disabled=self.current_page == 0
        )
        self.prev_button.callback = self.prev_page_callback
        self.add_item(self.prev_button)
        
        # Next Page button
        total_pages = (len(self.all_games) + self.games_per_page - 1) // self.games_per_page
        self.next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.primary,
            row=1,
            disabled=self.current_page >= total_pages - 1
        )
        self.next_button.callback = self.next_page_callback
        self.add_item(self.next_button)
        
        # Sort toggle button
        if view_type == "upcoming":
            sort_label = "Sort: Most Anticipated" if self.sort_method == "soonest" else "Sort: Soonest"
        elif view_type == "recent":
            sort_label = "Sort: Highest Rated" if self.sort_method == "newest" else "Sort: Newest"
        else:
            sort_label = "Sort: Highest Rated" if self.sort_method == "most_rated" else "Sort: Most Rated"
            
        self.sort_button = discord.ui.Button(
            label=sort_label,
            style=discord.ButtonStyle.secondary,
            row=1
        )
        self.sort_button.callback = self.sort_toggle_callback
        self.add_item(self.sort_button)
        
        # Action row (row 2)
        # Add back button
        self.back_button = discord.ui.Button(
            label="← Back to List",
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=True  # Disabled when viewing list
        )
        self.back_button.callback = self.back_callback
        self.add_item(self.back_button)
        
        # Add "Request This Game" button
        self.request_button = discord.ui.Button(
            label="Request This Game",
            style=discord.ButtonStyle.success,
            row=2,
            disabled=True
        )
        self.request_button.callback = self.request_callback
        self.add_item(self.request_button)
    
    def _sort_games(self):
        """Sort games based on current sort method"""
        if self.view_type == "upcoming":
            if self.sort_method == "soonest":
                # Sort by release date (soonest first)
                self.all_games.sort(key=lambda g: g.get('release_date', 'ZZZ'))
            else:  # anticipated
                # Sort by hypes count (most anticipated)
                self.all_games.sort(
                    key=lambda g: (g.get('hypes') or 0), 
                    reverse=True
                )
        
        elif self.view_type == "recent":
            logger.debug(f"Sorting recent games by: {self.sort_method}") 
            if self.sort_method == "newest":
                # Sort by release date (newest first)
                self.all_games.sort(key=lambda g: g.get('release_date', ''), reverse=True)
                # Log first 3 games after sort
                logger.debug(f"After 'newest' sort, first 3 games:") 
                for i, game in enumerate(self.all_games[:3]): 
                    logger.debug(f"  {i+1}. {game.get('name')} - {game.get('release_date')}") 
            else:  # highest_rated
                # Sort by rating (highest first)
                self.all_games.sort(key=lambda g: (g.get('rating') or 0), reverse=True)
                # Log first 3 games after sort
                logger.debug(f"After 'highest_rated' sort, first 3 games:")
                for i, game in enumerate(self.all_games[:3]):  
                    logger.debug(f"  {i+1}. {game.get('name')} - Rating: {game.get('rating')}") 
        
        elif self.view_type == "exclusives":
                if self.sort_method == "most_rated":
                    self.all_games.sort(key=lambda g: (g.get('rating_count') or 0), reverse=True)
                else:  # highest_rated
                    self.all_games.sort(key=lambda g: (g.get('rating') or 0), reverse=True)       

        else:  # popular
            if self.sort_method == "most_rated":
                # Sort by rating_count (most rated first)
                self.all_games.sort(key=lambda g: (g.get('rating_count') or 0), reverse=True)
            else:  # highest_rated
                # Sort by rating (highest first)
                self.all_games.sort(key=lambda g: (g.get('rating') or 0), reverse=True)
    
    def _get_current_page_games(self) -> List[Dict]:
        """Get games for current page"""
        start_idx = self.current_page * self.games_per_page
        end_idx = start_idx + self.games_per_page
        return self.all_games[start_idx:end_idx]
    
    def _populate_game_select(self):
        """Populate the game select dropdown with current page games"""
        self.game_select.options.clear()
        
        # Add games to select menu
        for i, game in enumerate(self.games):
            game_name = game.get('name', 'Unknown')
            release_date = game.get('release_date', 'TBA')
            if release_date != 'Unknown' and release_date != 'TBA':
                try:
                    date_obj = datetime.strptime(release_date, "%Y-%m-%d")
                    year = date_obj.strftime("%Y")
                except:
                    year = "Unknown"
            else:
                year = "TBA"
            
            # Truncate name if too long
            label = game_name[:100] if len(game_name) <= 100 else game_name[:97] + "..."
            
            # Platform display - if filtered by platform, show that one (no emoji for dropdown)
            if self.platform_name:
                platform_display = self.platform_name
            else:
                # No filter - show first platform from game data
                platform_display = game.get('platforms', ['Unknown'])[0] if game.get('platforms') else 'Unknown'
            
            self.game_select.add_option(
                label=label,
                value=str(i),
                description=f"{year} • {platform_display}"[:100]
            )
    
    def _update_navigation_buttons(self):
        """Update the state of navigation buttons"""
        total_pages = (len(self.all_games) + self.games_per_page - 1) // self.games_per_page
        
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= total_pages - 1
        
        # Update sort button label
        if self.view_type == "upcoming":
            sort_label = "Sort: Most Anticipated" if self.sort_method == "soonest" else "Sort: Soonest"
        elif self.view_type == "recent":
            sort_label = "Sort: Highest Rated" if self.sort_method == "newest" else "Sort: Newest"
        elif self.view_type == "exclusives": 
            sort_label = "Sort: Highest Rated" if self.sort_method == "most_rated" else "Sort: Most Rated"
        else:
            sort_label = "Sort: Highest Rated" if self.sort_method == "most_rated" else "Sort: Most Rated"
        self.sort_button.label = sort_label
            
    def create_list_embed(self) -> discord.Embed:
        """Create the initial list embed"""
        # Adjust title based on platform
        if self.platform_name:
            search_cog = self.bot.get_cog('Search')
            platform_display = self.platform_name
            if search_cog:
                platform_display = search_cog.get_platform_with_emoji(self.platform_name)
            embed_title = f"{self.title} - {platform_display}"
        else:
            embed_title = self.title
        
        embed = discord.Embed(
            title=embed_title,
            description="Select a game from the dropdown below to view full details.",
            color=discord.Color.blue()
        )
        
        # Set RomM logo as thumbnail
        embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        # Create simple numbered list for current page
        game_list = []
        start_num = self.current_page * self.games_per_page + 1
        for i, game in enumerate(self.games, start_num):
            game_name = game.get('name', 'Unknown')
            release_date = game.get('release_date', 'TBA')
            
            # Format date based on whether it's upcoming releases or popular
            if release_date != 'Unknown' and release_date != 'TBA':
                try:
                    date_obj = datetime.strptime(release_date, "%Y-%m-%d")
                    if self.show_full_date:
                        # For upcoming: show full date (Dec 25, 2024)
                        formatted_date = date_obj.strftime("%b %d, %Y")
                    else:
                        # For popular: just year (2024)
                        formatted_date = date_obj.strftime("%Y")
                except:
                    formatted_date = "TBA"
            else:
                formatted_date = "TBA"
            
            game_list.append(f"**{i}.** {game_name} ({formatted_date})")
        
        # Split into multiple fields if needed (Discord field value limit is 1024 chars)
        current_field = []
        field_count = 1
        
        for game_entry in game_list:
            current_field.append(game_entry)
            # Check if adding next entry would exceed limit
            if len("\n".join(current_field)) > 900:  # Leave some buffer
                embed.add_field(
                    name=f"Games ({field_count})" if field_count > 1 else "Games",
                    value="\n".join(current_field),
                    inline=False
                )
                current_field = []
                field_count += 1
        
        # Add remaining games
        if current_field:
            embed.add_field(
                name=f"Games ({field_count})" if field_count > 1 else "Games",
                value="\n".join(current_field),
                inline=False
            )
        
        # Add footer with pagination info
        total_pages = (len(self.all_games) + self.games_per_page - 1) // self.games_per_page
        page_info = f"Page {self.current_page + 1} of {total_pages} • {len(self.all_games)} total games"
        
        # Add sort info
        if self.view_type == "upcoming":
            sort_info = "Sorted by: Soonest" if self.sort_method == "soonest" else "Sorted by: Most Anticipated"
        elif self.view_type == "recent":
            sort_info = "Sorted by: Newest" if self.sort_method == "newest" else "Sorted by: Highest Rated"
        elif self.view_type == "exclusives":
            sort_info = "Sorted by: Most Rated" if self.sort_method == "most_rated" else "Sorted by: Highest Rated"
        else:
            sort_info = "Sorted by: Most Rated" if self.sort_method == "most_rated" else "Sorted by: Highest Rated"
        
        embed.set_footer(
            text=f"{page_info} • {sort_info}"
        )
        
        return embed
    
    async def prev_page_callback(self, interaction: discord.Interaction):
        """Go to previous page"""
        if self.current_page > 0:
            self.current_page -= 1
            self.games = self._get_current_page_games()
            self._populate_game_select()
            self._update_navigation_buttons()
            
            # Reset to list view (dropdown indices changed)
            self.viewing_detail = False
            self.back_button.disabled = True
            self.request_button.disabled = True
            
            embed = self.create_list_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()
    
    async def next_page_callback(self, interaction: discord.Interaction):
        """Go to next page"""
        total_pages = (len(self.all_games) + self.games_per_page - 1) // self.games_per_page
        if self.current_page < total_pages - 1:
            self.current_page += 1
            self.games = self._get_current_page_games()
            self._populate_game_select()
            self._update_navigation_buttons()
            
            # Reset to list view (dropdown indices changed)
            self.viewing_detail = False
            self.back_button.disabled = True
            self.request_button.disabled = True
            
            embed = self.create_list_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()
    
    async def sort_toggle_callback(self, interaction: discord.Interaction):
        """Toggle between sort methods"""
        # Toggle sort method
        if self.view_type == "upcoming":
            self.sort_method = "anticipated" if self.sort_method == "soonest" else "soonest"
        elif self.view_type == "recent":
            self.sort_method = "highest_rated" if self.sort_method == "newest" else "newest"
        elif self.view_type == "exclusives":
            self.sort_method = "highest_rated" if self.sort_method == "most_rated" else "most_rated"
        else: # popular
            self.sort_method = "highest_rated" if self.sort_method == "most_rated" else "most_rated"
        
        # Reset to first page when changing sort
        self.current_page = 0
        
        # Re-sort games
        self._sort_games()
        
        # Update current page games
        self.games = self._get_current_page_games()
        self._populate_game_select()
        self._update_navigation_buttons()
        
        # Reset to list view (dropdown indices changed)
        self.viewing_detail = False
        self.back_button.disabled = True
        self.request_button.disabled = True
        
        embed = self.create_list_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    def create_game_detail_embed(self, game: Dict) -> discord.Embed:
        """Create detailed embed for a single game (like requests cog)"""
        game_name = game.get('name', 'Unknown')
        
        embed = discord.Embed(
            title=game_name,
            color=discord.Color.blue()
        )
        
        # Set cover image if available
        if game.get('cover_url'):
            embed.set_image(url=game['cover_url'])
        
        # Set RomM logo as thumbnail
        embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        # Platform field
        platforms = game.get('platforms', [])
        if platforms:
            # If viewing with platform filter, only show that platform
            if self.platform_name:
                search_cog = self.bot.get_cog('Search')
                if search_cog:
                    platform_str = search_cog.get_platform_with_emoji(self.platform_name)
                else:
                    platform_str = self.platform_name
            else:
                # No filter - show all platforms
                platform_str = ', '.join(platforms[:3])
                if len(platforms) > 3:
                    platform_str += f' (+{len(platforms) - 3} more)'
            
            embed.add_field(
                name="Platform" if self.platform_name else "Platforms",
                value=platform_str,
                inline=True
            )
        
        # Genre field
        genres = game.get('genres', [])
        if genres:
            genre_str = ', '.join(genres[:2])
            if len(genres) > 2:
                genre_str += f' (+{len(genres) - 2} more)'
            embed.add_field(
                name="Genre",
                value=genre_str,
                inline=True
            )
        
        # Release Date field
        release_date = game.get('release_date', 'Unknown')
        if release_date != 'Unknown' and release_date != 'TBA':
            try:
                date_obj = datetime.strptime(release_date, "%Y-%m-%d")
                formatted_date = date_obj.strftime("%B %d, %Y")
            except:
                formatted_date = release_date
        else:
            formatted_date = release_date
        
        embed.add_field(
            name="Release Date",
            value=formatted_date,
            inline=True
        )
        
        # IGDB Rating (if available)
        rating = game.get('rating')
        rating_count = game.get('rating_count')
        if rating:
            # Convert 0-100 to 0-5 stars
            stars = round(rating / 20)
            star_display = "⭐" * stars + "☆" * (5 - stars)
            rating_text = f"{star_display} {rating:.1f}/100"
            if rating_count:
                rating_text += f" ({rating_count:,} ratings)"
            
            embed.add_field(
                name="IGDB Rating",
                value=rating_text,
                inline=True
            )
        
        # Companies section
        companies = []
        if game.get('developers'):
            developers = game['developers'][:2]
            companies.extend(developers)
        if game.get('publishers') and game.get('publishers') != game.get('developers'):
            publishers = game['publishers']
            remaining_slots = 2 - len(companies)
            if remaining_slots > 0:
                companies.extend(publishers[:remaining_slots])
        
        if companies:
            embed.add_field(
                name="Companies",
                value=", ".join(companies),
                inline=True
            )
        
        # Game modes
        game_modes = game.get('game_modes', [])
        if game_modes:
            modes_str = ', '.join(game_modes[:3])
            if len(game_modes) > 3:
                modes_str += f' (+{len(game_modes) - 3} more)'
            embed.add_field(
                name="Game Modes",
                value=modes_str,
                inline=True
            )
        
        # Summary
        summary = game.get('summary', 'No summary available')
        if summary and summary != 'No summary available':
            if len(summary) > 500:
                summary = summary[:497] + "..."
            embed.add_field(
                name="Summary",
                value=summary,
                inline=False
            )
        
        # IGDB Link and other links
        igdb_link_name = game_name.lower().replace(' ', '-')
        igdb_link_name = re.sub(r'[^a-z0-9-]', '', igdb_link_name)
        igdb_url = f"https://www.igdb.com/games/{igdb_link_name}"

        igdb_emoji = self.bot.get_formatted_emoji('igdb')
        youtube_emoji = self.bot.get_formatted_emoji('youtube')
        steam_emoji = self.bot.get_formatted_emoji('steam')
        epic_emoji = self.bot.get_formatted_emoji('epic')
        gog_emoji = self.bot.get_formatted_emoji('gog')
        twitch_emoji = self.bot.get_formatted_emoji('twitch')

        # Build links list
        links = [f"[**{igdb_emoji} IGDB**]({igdb_url})"]

        # Add other website links if available
        websites = game.get('websites', {})

        # Priority order for links
        if 'official' in websites:
            links.append(f"[🌐 **Website**]({websites['official']})")
        if 'steam' in websites:
            links.append(f"[**{steam_emoji} Steam**]({websites['steam']})")
        # if 'epic' in websites:
        #     links.append(f"[**{epic_emoji} Epic**]({websites['epic']})")
        if 'gog' in websites:
            links.append(f"[**{gog_emoji} GOG**]({websites['gog']})")
        if 'youtube' in websites:
            links.append(f"[**{youtube_emoji} YouTube**]({websites['youtube']})")
        if 'twitch' in websites:
            links.append(f"[**{twitch_emoji} Twitch**]({websites['twitch']})")

        # Format links: 4 per row with better spacing
        links_formatted = []
        for i in range(0, len(links), 5):
            row = links[i:i+5]
            links_formatted.append("\u2003".join(row))

        embed.add_field(
            name="Links",
            value="\n\n".join(links_formatted),  # Each row on new line
            inline=False
        )
               
        return embed
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle game selection"""
        game_index = int(self.game_select.values[0])
        selected_game = self.games[game_index]
        
        # Update state - now viewing detail
        self.viewing_detail = True
        self.back_button.disabled = False
        self.request_button.disabled = False  # Enable request button
        
        # Disable navigation buttons when viewing detail
        self.prev_button.disabled = True
        self.next_button.disabled = True
        self.sort_button.disabled = True
        
        # Create detailed embed
        embed = self.create_game_detail_embed(selected_game)
        
        await interaction.response.edit_message(embed=embed, view=self)

    async def back_callback(self, interaction: discord.Interaction):
        """Return to game list"""
        # Update state - now viewing list
        self.viewing_detail = False
        self.back_button.disabled = True
        self.request_button.disabled = True
        
        # Re-enable navigation buttons based on current page
        total_pages = (len(self.all_games) + self.games_per_page - 1) // self.games_per_page
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= total_pages - 1
        self.sort_button.disabled = False
        
        embed = self.create_list_embed()
        await interaction.response.edit_message(embed=embed, view=self)
    
    async def request_callback(self, interaction: discord.Interaction):
        """Submit a request for the selected game"""
        # Get the currently selected game
        if not self.game_select.values:
            await interaction.response.send_message(
                "Please select a game from the dropdown first!",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        game_index = int(self.game_select.values[0])
        selected_game = self.games[game_index]
        game_name = selected_game.get('name', 'Unknown')
        
        # Build platform info
        if self.platform_name:
            platform_info = self.platform_name
        else:
            platforms = selected_game.get('platforms', [])
            platform_info = platforms[0] if platforms else "Unknown"
        
        # Get the request cog
        request_cog = self.bot.get_cog('Request')
        if not request_cog:
            await interaction.followup.send("❌ Request system is not available", ephemeral=True)
            return
        
        # Check if requests are enabled
        if not request_cog.requests_enabled:
            await interaction.followup.send("❌ The request system is currently disabled.", ephemeral=True)
            return
        
        try:
            # Get platform mapping from database
            async with self.bot.db.get_connection() as db:
                cursor = await db.execute('''
                    SELECT id, in_romm, romm_id
                    FROM platform_mappings
                    WHERE LOWER(display_name) = LOWER(?)
                    LIMIT 1
                ''', (platform_info,))
                
                platform_mapping = await cursor.fetchone()
                
                if not platform_mapping:
                    await interaction.followup.send(
                        f"⚠️ '{platform_info}' is not in the Romm platform database. "
                        "Please contact an admin to add this platform.",
                        ephemeral=True
                    )
                    return
                
                mapping_id, in_romm, romm_id = platform_mapping
                igdb_id = selected_game.get('id')
                
                # Check for existing requests (Integration with Existing Requests)
                cursor = await db.execute(
                    """
                    SELECT id, user_id, username, game_name, status
                    FROM requests 
                    WHERE platform = ? 
                    AND status = 'pending'
                    AND (igdb_id = ? OR LOWER(game_name) = LOWER(?))
                    """,
                    (platform_info, igdb_id, game_name)
                )
                existing_requests = await cursor.fetchall()
                
                # Check if user already requested or is subscribed
                user_already_requested = False
                existing_request_id = None
                original_requester_name = None
                
                for req_id, req_user_id, req_username, req_game, req_status in existing_requests:
                    existing_request_id = req_id
                    original_requester_name = req_username
                    
                    if req_user_id == interaction.user.id:
                        user_already_requested = True
                        break
                    
                    # Check if user is already a subscriber
                    cursor = await db.execute(
                        """
                        SELECT COUNT(*) FROM request_subscribers 
                        WHERE request_id = ? AND user_id = ?
                        """,
                        (req_id, interaction.user.id)
                    )
                    is_subscriber = (await cursor.fetchone())[0] > 0
                    
                    if is_subscriber:
                        user_already_requested = True
                    break
                
                # If user has already requested this game
                if user_already_requested:
                    embed = discord.Embed(
                        title="📋 Already Requested",
                        description="You have already requested this game or are subscribed to an existing request.",
                        color=discord.Color.orange()
                    )
                    
                    search_cog = self.bot.get_cog('Search')
                    platform_display = platform_info
                    if search_cog:
                        platform_display = search_cog.get_platform_with_emoji(platform_info)
                    
                    embed.add_field(name="Game", value=game_name, inline=True)
                    embed.add_field(name="Platform", value=platform_display, inline=True)
                    embed.add_field(name="Request ID", value=f"#{existing_request_id}", inline=True)
                    
                    if selected_game.get('cover_url'):
                        embed.set_thumbnail(url=selected_game['cover_url'])
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
                
                # If someone else has requested this game
                if existing_request_id and interaction.user.id != existing_requests[0][1]:
                    # Add user as a subscriber to existing request
                    await db.execute(
                        """
                        INSERT INTO request_subscribers (request_id, user_id, username)
                        VALUES (?, ?, ?)
                        """,
                        (existing_request_id, interaction.user.id, str(interaction.user))
                    )
                    await db.commit()
                    
                    # Count total subscribers
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM request_subscribers WHERE request_id = ?",
                        (existing_request_id,)
                    )
                    subscriber_count = (await cursor.fetchone())[0]
                    
                    embed = discord.Embed(
                        title="📋 Request Already Exists",
                        description=f"This game has already been requested by **{original_requester_name}**",
                        color=discord.Color.blue()
                    )
                    
                    search_cog = self.bot.get_cog('Search')
                    platform_display = platform_info
                    if search_cog:
                        platform_display = search_cog.get_platform_with_emoji(platform_info)
                    
                    embed.add_field(name="Game", value=game_name, inline=True)
                    embed.add_field(name="Platform", value=platform_display, inline=True)
                    embed.add_field(name="Request ID", value=f"#{existing_request_id}", inline=True)
                    
                    embed.add_field(
                        name="✅ You've been added to the notification list",
                        value=f"You and {subscriber_count} other user(s) will be notified when this request is fulfilled.",
                        inline=False
                    )
                    
                    if selected_game.get('cover_url'):
                        embed.set_thumbnail(url=selected_game['cover_url'])
                    
                    embed.set_footer(text="You'll receive a DM when this game is added to the collection")
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    return
                
                # If platform exists in Romm, check for existing games
                if in_romm and romm_id:
                    exists, matches = await request_cog.check_if_game_exists(platform_info, game_name)
                    
                    if exists:
                        # Game exists - show existing game view with IGDB options
                        from .requests import ExistingGameWithIGDBView
                        view = ExistingGameWithIGDBView(
                            self.bot,
                            matches,
                            [selected_game],  # Pass this IGDB game as option
                            platform_info,
                            game_name,
                            interaction.user.id
                        )
                        
                        search_cog = self.bot.get_cog('Search')
                        platform_with_emoji = search_cog.get_platform_with_emoji(platform_info) if search_cog else platform_info
                        
                        embed = discord.Embed(
                            title="Games Found in Collection",
                            description=f"Found {len(matches)} game(s) matching '{game_name}' that are already available:",
                            color=discord.Color.blue()
                        )
                        
                        for i, rom in enumerate(matches[:3]):
                            embed.add_field(
                                name=f"✅ {rom.get('name', 'Unknown')}",
                                value=f"Available now - {rom.get('fs_name', 'Unknown')}",
                                inline=False
                            )
                        
                        if len(matches) > 3:
                            embed.add_field(
                                name="...",
                                value=f"And {len(matches) - 3} more available",
                                inline=False
                            )
                        
                        has_other_games = bool(view.filtered_igdb_matches)
                        instructions = ["• **Select an existing game** from the dropdown to download it"]
                        
                        if has_other_games:
                            instructions.append(f"• **Request a different game** - Found {len(view.filtered_igdb_matches)} other game(s) on IGDB")
                        
                        instructions.append("• Click **Request Different Version** for ROM hacks, patches, or specific versions")
                        
                        embed.add_field(
                            name="What would you like to do?",
                            value="\n".join(instructions),
                            inline=False
                        )
                        
                        message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
                        view.message = message
                        return
                
                # Process the request directly with the IGDB data
                request_id = await request_cog.process_request_with_platform(
                    interaction,
                    platform_info,
                    game_name,
                    None,  # No additional details
                    selected_game,  # Pass the IGDB game data
                    None,  # No message to update
                    mapping_id,
                    in_romm,
                    send_response=False
                )
                
                # Check if request was created successfully
                if not request_id:
                    await interaction.followup.send("❌ An error occurred while processing your request", ephemeral=True)
                    return

                # Create a nice success embed with game metadata after request is processed  # <-- ALL NEW FROM HERE
                success_embed = discord.Embed(
                    title="✅ Request Submitted",
                    description=f"Your request for **{game_name}** has been submitted!",
                    color=discord.Color.green()
                )

                # Set cover image if available
                if selected_game.get('cover_url'):
                    success_embed.set_image(url=selected_game['cover_url'])
                
                # Always set RomM logo as thumbnail
                success_embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")

                # Platform field
                search_cog = self.bot.get_cog('Search')
                platform_display = platform_info
                if search_cog and in_romm:
                    platform_display = search_cog.get_platform_with_emoji(platform_info)

                platform_status = "✅ Available" if in_romm else "🆕 Not Yet Added"
                success_embed.add_field(
                    name="Platform", 
                    value=f"{platform_display}\n{platform_status}", 
                    inline=True
                )

                # Genre field
                genres = selected_game.get('genres', [])
                if genres:
                    genre_str = ', '.join(genres[:2])
                    if len(genres) > 2:
                        genre_str += f' (+{len(genres) - 2} more)'
                    success_embed.add_field(
                        name="Genre",
                        value=genre_str,
                        inline=True
                    )

                # Status
                success_embed.add_field(
                    name="Status",
                    value="⏳ Pending",
                    inline=True
                )

                # Release Date
                release_date = selected_game.get('release_date', 'Unknown')
                if release_date != 'Unknown' and release_date != 'TBA':
                    try:
                        date_obj = datetime.strptime(release_date, "%Y-%m-%d")
                        formatted_date = date_obj.strftime("%B %d, %Y")
                    except:
                        formatted_date = release_date
                    success_embed.add_field(
                        name="Release Date",
                        value=formatted_date,
                        inline=True
                    )

                # IGDB Rating
                rating = selected_game.get('rating')
                rating_count = selected_game.get('rating_count')
                if rating:
                    stars = round(rating / 20)
                    star_display = "⭐" * stars + "☆" * (5 - stars)
                    rating_text = f"{star_display} {rating:.1f}/100"
                    if rating_count:
                        rating_text += f" ({rating_count:,} ratings)"
                    success_embed.add_field(
                        name="IGDB Rating",
                        value=rating_text,
                        inline=True
                    )

                # Add Request ID field
                success_embed.add_field(
                    name="Request ID",
                    value=f"#{request_id}",
                    inline=True
                )
                
                # Companies
                companies = []
                if selected_game.get('developers'):
                    developers = selected_game['developers'][:2]
                    companies.extend(developers)
                if selected_game.get('publishers') and selected_game.get('publishers') != selected_game.get('developers'):
                    publishers = selected_game['publishers']
                    remaining_slots = 2 - len(companies)
                    if remaining_slots > 0:
                        companies.extend(publishers[:remaining_slots])

                if companies:
                    success_embed.add_field(
                        name="Companies",
                        value=", ".join(companies),
                        inline=True
                    )

                # Platform status warning
                if not in_romm:
                    success_embed.add_field(
                        name="📝 Note",
                        value="This platform needs to be added to the collection before this request can be fulfilled.",
                        inline=False
                    )

                # Summary
                summary = selected_game.get('summary', 'No summary available')
                if summary and summary != 'No summary available':
                    if len(summary) > 300:
                        summary = summary[:297] + "..."
                    success_embed.add_field(
                        name="Summary",
                        value=summary,
                        inline=False
                    )
                
                success_embed.set_footer(text=f"Request submitted by {interaction.user}")
                
                await interaction.followup.send(embed=success_embed, ephemeral=True)
                
        except Exception as e:
            logger.error(f"Error processing request from IGDB: {e}")
            await interaction.followup.send("❌ An error occurred while processing your request", ephemeral=True)

class IGDBHandler(commands.Cog):
    """IGDB integration commands for game discovery"""
    
    def __init__(self, bot):
        self.bot = bot
        self.igdb: Optional[IGDBClient] = None
        bot.loop.create_task(self.setup())
    
    async def setup(self):
        """Initialize IGDB client"""
        try:
            self.igdb = IGDBClient()
            logger.debug("✅ IGDB Handler initialized successfully")
        except ValueError as e:
            logger.warning(f"IGDB integration disabled: {e}")
            self.igdb = None
    
    async def platform_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete for platforms using the database"""
        try:
            user_input = ctx.value.lower()
            
            async with self.bot.db.get_connection() as db:
                cursor = await db.execute('''
                    SELECT display_name, in_romm, folder_name
                    FROM platform_mappings
                    WHERE LOWER(display_name) LIKE ?
                    OR LOWER(folder_name) LIKE ?
                    ORDER BY 
                        in_romm DESC,
                        display_name
                    LIMIT 25
                ''', (f'%{user_input}%', f'%{user_input}%'))
                
                results = await cursor.fetchall()
            
            choices = []
            for display_name, in_romm, folder_name in results:
                choices.append(discord.OptionChoice(
                    name=display_name[:100],
                    value=display_name
                ))
            
            return choices
            
        except Exception as e:
            logger.error(f"Error in platform autocomplete: {e}")
            return []
    
    async def get_platform_slug(self, platform_name: str) -> Optional[str]:
        """Get IGDB platform slug from database"""
        try:
            async with self.bot.db.get_connection() as db:
                cursor = await db.execute('''
                    SELECT igdb_slug
                    FROM platform_mappings
                    WHERE LOWER(display_name) = LOWER(?)
                    LIMIT 1
                ''', (platform_name,))
                
                result = await cursor.fetchone()
                return result[0] if result else None
                
        except Exception as e:
            logger.error(f"Error getting platform slug: {e}")
            return None
    
    async def fetch_upcoming_games(self, platform_slug: Optional[str] = None, limit: int = 25) -> List[Dict]:
        """Fetch upcoming games from IGDB"""
        try:
            if not await self.igdb.get_access_token():
                return []
            
            session = await self.igdb.ensure_session()
            headers = {
                "Client-ID": self.igdb.client_id,
                "Authorization": f"Bearer {self.igdb.access_token}"
            }
            
            # Get current timestamp and 1 year from now
            now = int(datetime.now().timestamp())
            one_year_later = int((datetime.now() + timedelta(days=365)).timestamp())
            
            url = "https://api.igdb.com/v4/games"
            
            # Build query for upcoming games - USE hypes field for anticipation
            query = (
                f'fields name,first_release_date,cover.url,platforms.name,genres.name,summary,websites.*,rating,rating_count,hypes; '  # <-- CHANGED to hypes
                f'where first_release_date > {now} & first_release_date < {one_year_later}'
            )
            
            # Add platform filter if specified
            if platform_slug:
                platform_id = await self.igdb.get_platform_id_from_slug(platform_slug)
                if platform_id:
                    query += f' & platforms = [{platform_id}]'
            
            query += f'; sort first_release_date asc; limit {limit};'
            
            logger.debug(f"IGDB upcoming query: {query}")
            
            async with session.post(url, headers=headers, data=query) as response:
                if response.status == 200:
                    games = await response.json()
                    logger.debug(f"Fetched {len(games)} upcoming games from IGDB")
                    if games:
                        logger.debug(f"Sample game data: {games[0].keys()}")
                        if 'hypes' in games[0]:
                            logger.debug(f"Hypes value: {games[0]['hypes']}")
                    return self.igdb._process_games_response(games)
                else:
                    error_text = await response.text()
                    logger.error(f"IGDB API error {response.status}: {error_text}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error fetching upcoming games: {e}")
            return []
    
    async def fetch_recent_games(self, platform_slug: Optional[str] = None, limit: int = 25) -> List[Dict]:
        """Fetch recently released games from IGDB"""
        try:
            if not await self.igdb.get_access_token():
                return []
            
            session = await self.igdb.ensure_session()
            headers = {
                "Client-ID": self.igdb.client_id,
                "Authorization": f"Bearer {self.igdb.access_token}"
            }
            
            # Get timestamp for 3 months ago and now
            three_months_ago = int((datetime.now() - timedelta(days=90)).timestamp())
            now = int(datetime.now().timestamp())
            
            url = "https://api.igdb.com/v4/games"
            
            # Build query for recent games - ADD RATING FILTER LIKE POPULAR
            query = (
                f'fields name,first_release_date,cover.url,platforms.name,genres.name,summary,websites.*,rating,rating_count; '
                f'where first_release_date > {three_months_ago} & first_release_date < {now}'
                f' & rating != null & rating_count > 2'
            )
            
            # Add platform filter if specified
            if platform_slug:
                platform_id = await self.igdb.get_platform_id_from_slug(platform_slug)
                if platform_id:
                    query += f' & platforms = [{platform_id}]'
            
            query += f'; limit {limit};'
            
            logger.debug(f"IGDB recent query: {query}")
            
            async with session.post(url, headers=headers, data=query) as response:
                if response.status == 200:
                    games = await response.json()
                    logger.debug(f"Fetched {len(games)} recent games from IGDB")
                    return self.igdb._process_games_response(games)
                else:
                    error_text = await response.text()
                    logger.error(f"IGDB API error {response.status}: {error_text}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error fetching recent games: {e}")
            return []
    
    async def fetch_popular_games(self, platform_slug: Optional[str] = None, limit: int = 25) -> List[Dict]:
        """Fetch popular games from IGDB"""
        try:
            if not await self.igdb.get_access_token():
                return []
            
            session = await self.igdb.ensure_session()
            headers = {
                "Client-ID": self.igdb.client_id,
                "Authorization": f"Bearer {self.igdb.access_token}"
            }
            
            url = "https://api.igdb.com/v4/games"
            
            # Build query for popular games (using rating and rating_count)
            # websites.* expands the websites relationship
            query = (
                'fields name,first_release_date,cover.url,platforms.name,rating,rating_count,genres.name,summary,websites.*; '
                'where rating != null & rating_count > 50'
            )
            
            # Add platform filter if specified
            if platform_slug:
                platform_id = await self.igdb.get_platform_id_from_slug(platform_slug)
                if platform_id:
                    query += f' & platforms = [{platform_id}]'
            
            query += f'; sort rating_count desc; limit {limit};'
            
            logger.debug(f"IGDB popular query: {query}")
            
            async with session.post(url, headers=headers, data=query) as response:
                if response.status == 200:
                    games = await response.json()
                    logger.debug(f"Fetched {len(games)} popular games from IGDB")
                    if games:
                        logger.debug(f"Sample game data: {games[0].keys()}")
                        if 'websites' in games[0]:
                            logger.debug(f"Websites found: {games[0]['websites']}")
                        else:
                            logger.debug("No websites in game data")
                    return self.igdb._process_games_response(games)
                else:
                    error_text = await response.text()
                    logger.error(f"IGDB API error {response.status}: {error_text}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error fetching popular games: {e}")
            return []
    
    async def fetch_exclusive_games(self, platform_slug: str, limit: int = 25) -> List[Dict]:
        """Fetch platform exclusive games from IGDB"""
        try:
            if not await self.igdb.get_access_token():
                return []
            
            session = await self.igdb.ensure_session()
            headers = {
                "Client-ID": self.igdb.client_id,
                "Authorization": f"Bearer {self.igdb.access_token}"
            }
            
            # Get platform ID
            platform_id = await self.igdb.get_platform_id_from_slug(platform_slug)
            if not platform_id:
                logger.error(f"Could not find platform ID for slug: {platform_slug}")
                return []
            
            url = "https://api.igdb.com/v4/games"
            
            # Query for games on this platform with ratings
            # We'll fetch more than needed and filter for exclusives in Python
            query = (
                f'fields name,first_release_date,cover.url,platforms.name,genres.name,summary,websites.*,rating,rating_count; '
                f'where platforms = [{platform_id}]'
              # f' & rating != null & rating_count > 1'
            )
            
            query += f'; sort rating_count desc; limit {limit * 3};'  # Fetch 3x since we'll filter
            
            logger.debug(f"IGDB exclusives query: {query}")
            
            async with session.post(url, headers=headers, data=query) as response:
                if response.status == 200:
                    games = await response.json()
                    processed_games = self.igdb._process_games_response(games)
                    
                    # Filter for exclusives - games with only 1 platform
                    exclusive_games = [
                        game for game in processed_games 
                        if len(game.get('platforms', [])) == 1
                    ]
                    
                    logger.debug(f"Fetched {len(games)} games, filtered to {len(exclusive_games)} exclusives")
                    
                    # Return only the requested limit
                    return exclusive_games[:limit]
                else:
                    error_text = await response.text()
                    logger.error(f"IGDB API error {response.status}: {error_text}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error fetching exclusive games: {e}")
            return []
    
    def create_game_list_embed(self, games: List[Dict], title: str, platform_name: Optional[str] = None) -> discord.Embed:
        """Create an embed listing games"""
        
        # Adjust title based on platform
        if platform_name:
            search_cog = self.bot.get_cog('Search')
            platform_display = platform_name
            if search_cog:
                platform_display = search_cog.get_platform_with_emoji(platform_name)
            embed_title = f"{title} - {platform_display}"
        else:
            embed_title = title
        
        embed = discord.Embed(
            title=embed_title,
            color=discord.Color.blue()
        )
        
        # Set RomM logo as thumbnail
        embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        if not games:
            embed.description = "No games found matching your criteria."
            return embed
        
        # Get IGDB emoji
        igdb_emoji = self.bot.get_formatted_emoji('igdb')
        
        # Add games as fields - cleaner presentation
        for i, game in enumerate(games[:10], 1):
            # Format release date
            release_date = game.get('release_date', 'TBA')
            if release_date != 'Unknown' and release_date != 'TBA':
                try:
                    date_obj = datetime.strptime(release_date, "%Y-%m-%d")
                    release_date = date_obj.strftime("%b %d, %Y")
                except:
                    pass
            
            # Create IGDB link
            game_name = game.get('name', 'Unknown')
            igdb_link_name = game_name.lower().replace(' ', '-')
            igdb_link_name = re.sub(r'[^a-z0-9-]', '', igdb_link_name)
            igdb_url = f"https://www.igdb.com/games/{igdb_link_name}"
            
            # Get first platform
            platforms = game.get('platforms', [])
            first_platform = platforms[0] if platforms else 'Unknown'
            platform_count = len(platforms)
            
            # Get first genre
            genres = game.get('genres', [])
            first_genre = genres[0] if genres else 'Unknown'
            
            # Build clean field value
            field_value = f"[{igdb_emoji} **View on IGDB**]({igdb_url})\n"
            field_value += f"📅 {release_date}\n"
            field_value += f"🎮 {first_platform}"
            if platform_count > 1:
                field_value += f" (+{platform_count - 1} more)"
            field_value += f"\n🎯 {first_genre}"
            if len(genres) > 1:
                field_value += f" (+{len(genres) - 1} more)"
            
            embed.add_field(
                name=f"{i}. {game_name}",
                value=field_value,
                inline=True  # Two columns
            )
        
        return embed
    
    @discord.slash_command(name="igdb", description="Browse games on IGDB")
    async def igdb(
        self,
        ctx: discord.ApplicationContext,
        view: discord.Option(
            str,
            "What to view",
            required=True,
            choices=[
                discord.OptionChoice(name="🗓️ Upcoming Releases", value="upcoming"),
                discord.OptionChoice(name="🔥 Popular Games", value="popular"),
                discord.OptionChoice(name="🆕 Recently Released", value="recent"),
                discord.OptionChoice(name="🎯 Platform Exclusives", value="exclusives")
            ]
        ),
        platform: discord.Option(
            str,
            "Filter by platform (optional, required for exclusives)",
            required=False,
            autocomplete=platform_autocomplete
        )
    ):
        """Browse games on IGDB - upcoming/recent releases, exclusives or popular games"""
        await ctx.defer()
        
        if not self.igdb:
            await ctx.respond("❌ IGDB integration is not configured.", ephemeral=True)
            return
        
        # Validate platform is provided for exclusives
        if view == "exclusives" and not platform:
            await ctx.respond("⚠️ Platform is required when viewing exclusives. Please select a platform.", ephemeral=True)
            return
        
        try:
            # Get platform slug if platform specified
            platform_slug = None
            if platform:
                platform_slug = await self.get_platform_slug(platform)
                if not platform_slug:
                    logger.warning(f"No IGDB slug found for platform: {platform}")
            
            # Fetch more games for pagination (100 instead of 25)
            if view == "upcoming":
                games = await self.fetch_upcoming_games(platform_slug, limit=100)
                title = "🗓️ Upcoming Releases"
                show_full_date = True  # Show full date for upcoming
                view_type = "upcoming"
            elif view == "recent": 
                games = await self.fetch_recent_games(platform_slug, limit=100)
                title = "🆕 Recently Released"
                show_full_date = True  # Show full date for recent
                view_type = "recent"
            elif view == "exclusives":
                games = await self.fetch_exclusive_games(platform_slug, limit=100)
                title = f"🎯 {platform} Exclusives"
                show_full_date = False
                view_type = "exclusives"
            else:  # popular
                games = await self.fetch_popular_games(platform_slug, limit=100)
                title = "🔥 Popular Games"
                show_full_date = False  # Just year for popular
                view_type = "popular"
            
            
            if not games:
                message = f"No {view} games found"
                if platform:
                    message += f" for {platform}"
                message += "."
                await ctx.respond(message, ephemeral=True)
                return
            
            # Create interactive view with game list and pagination
            game_view = IGDBGameView(self.bot, games, title, platform, show_full_date, view_type)
            initial_embed = game_view.create_list_embed()
            
            message = await ctx.respond(embed=initial_embed, view=game_view)
            
            # Store message reference
            if isinstance(message, discord.Interaction):
                game_view.message = await message.original_response()
            else:
                game_view.message = message
            
        except Exception as e:
            logger.error(f"Error in igdb command: {e}")
            await ctx.respond("❌ An error occurred while fetching games from IGDB.", ephemeral=True)
            
def setup(bot):
    bot.add_cog(IGDBHandler(bot))
