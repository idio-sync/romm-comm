import discord
from discord.ext import commands
import logging
from datetime import datetime
from typing import Optional, Dict, List, Tuple
import json
import os
import aiosqlite
import asyncio
import re
from pathlib import Path
from .search import Search
from .igdb_client import IGDBClient
from collections import defaultdict

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class GameSelect(discord.ui.Select):
    def __init__(self, matches):
        options = []
        for i, match in enumerate(matches):
            description = f"{match['release_date']} | {', '.join(match['platforms'][:2])}"
            if len(description) > 100:
                description = description[:97] + "..."
                
            options.append(
                discord.SelectOption(
                    label=match["name"][:100],
                    description=description,
                    value=str(i)
                )
            )
        
        super().__init__(
            placeholder="Select the correct game...",
            options=options,
            min_values=1,
            max_values=1,
            row=1
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        game_index = int(self.values[0])
        selected_game = self.view.matches[game_index]
        await self.view.update_view_for_selection(selected_game)

class GameSelectView(discord.ui.View):
    def __init__(self, matches, platform_name=None):
        super().__init__()
        self.matches = matches
        self.selected_game = None
        self.message = None
        self.submit_button = None
        self.platform_name = platform_name
        
        # Add select menu
        self.select_menu = GameSelect(matches)
        self.add_item(self.select_menu)
        
        # Add "Not Listed" button
        not_listed_button = discord.ui.Button(
            label="Not Listed",
            style=discord.ButtonStyle.secondary,
            row=2
        )
        
        async def not_listed_callback(interaction: discord.Interaction):
            self.selected_game = "manual"
            await interaction.response.defer()
            self.stop()
        
        not_listed_button.callback = not_listed_callback
        self.add_item(not_listed_button)

    def create_game_embed(self, game):
        embed = discord.Embed(
            title=f"{game['name']}",
            color=discord.Color.green()
        )
        
        # Set the romm logo as thumbnail
        embed.set_thumbnail(url="https://raw.githubusercontent.com/rommapp/romm/release/.github/resources/romm_complete.png")
        
        # Set the cover image as the main image
        if game.get('cover_url'):
            embed.set_image(url=game['cover_url'])
        
        # Always use the platform_name from the request
        embed.add_field(
            name="Platform",
            value=self.platform_name,
            inline=True
        )
        
        if game.get('genres'):
            embed.add_field(
                name="Genre",
                value=", ".join(game['genres'][:2]),
                inline=True
            )
            
        if game['release_date'] != "Unknown":
            try:
                date_obj = datetime.strptime(game['release_date'], "%Y-%m-%d")
                formatted_date = date_obj.strftime("%B %d, %Y")
            except:
                formatted_date = game['release_date']
        else:
            formatted_date = "Unknown"
            
        embed.add_field(
            name="Release Date",
            value=formatted_date,
            inline=True
        )
        
        # Summary section
        if game["summary"]:
            summary = game["summary"]
            if len(summary) > 300:
                summary = summary[:297] + "..."
            embed.add_field(
                name="Summary",
                value=summary,
                inline=False
            )
            
        # Companies section
        companies = []
        if game['developers']:
            companies.extend(game['developers'][:2])
        if game['publishers'] and game['publishers'] != game['developers']:
            remaining_slots = 2 - len(companies)
            if remaining_slots > 0:
                companies.extend(game['publishers'][:remaining_slots])
        
        if companies:
            embed.add_field(
                name="Companies",
                value=", ".join(companies),
                inline=True
            )
                
        # Create IGDB link
        igdb_name = game['name'].lower().replace(' ', '-')
        igdb_name = re.sub(r'[^a-z0-9-]', '', igdb_name)
        igdb_url = f"https://www.igdb.com/games/{igdb_name}"
        
        # Links section
        embed.add_field(
            name="Links",
            value=f"[IGDB]({igdb_url})",
            inline=True
        )
        
        return embed

    async def update_view_for_selection(self, game):
        self.selected_game = game  # Store the selected game
        embed = self.create_game_embed(game)
        
        # Add submit button if not already present
        if not self.submit_button:
            self.submit_button = discord.ui.Button(
                label="Submit Request",
                style=discord.ButtonStyle.success,
                row=3
            )
            
            async def submit_callback(interaction: discord.Interaction):
                await interaction.response.defer()
                
                # Update button appearance
                self.submit_button.label = "Request Submitted"
                self.submit_button.disabled = True
                self.submit_button.style = discord.ButtonStyle.secondary
                
                # Remove select menu and Not Listed button
                for item in self.children[:]:
                    if isinstance(item, (discord.ui.Select, discord.ui.Button)) and item != self.submit_button:
                        self.remove_item(item)
                
                # Update the message with the modified view
                await self.message.edit(view=self)
                
                # Stop the view
                self.stop()
            
            self.submit_button.callback = submit_callback
            self.add_item(self.submit_button)
        
        await self.message.edit(embed=embed, view=self)

class Request(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.igdb: Optional[IGDBClient] = None
        
        # Create data directory if it doesn't exist
        self.data_dir = Path('data')
        self.data_dir.mkdir(exist_ok=True)
        
        # Set database path in data directory
        self.db_path = self.data_dir / 'requests.db'
        
        self.requests_enabled = bot.config.REQUESTS_ENABLED
        bot.loop.create_task(self.setup())
        self.processing_lock = asyncio.Lock()

    async def cog_check(self, ctx: discord.ApplicationContext) -> bool:
        """Check if requests are enabled before any command in this cog"""
        if not self.requests_enabled and not ctx.author.guild_permissions.administrator:
            await ctx.respond("❌ The request system is currently disabled.")
            return False
        return True    
    
    async def setup(self):
        """Set up database and initialize IGDB client"""
        await self.setup_database()
        try:
            self.igdb = IGDBClient()
        except ValueError as e:
            logger.warning(f"IGDB integration disabled: {e}")
            self.igdb = None
            
    @property
    def igdb_enabled(self) -> bool:
        """Check if IGDB integration is enabled"""
        return self.igdb is not None
    
    async def setup_database(self):
        """Create the requests database and tables if they don't exist"""
        async with aiosqlite.connect(str(self.db_path)) as db:  # Convert Path to string
            await db.execute('''
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    details TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    fulfilled_by INTEGER,
                    fulfiller_name TEXT,
                    notes TEXT,
                    auto_fulfilled BOOLEAN DEFAULT 0
                )
            ''')
            await db.commit()

    async def check_if_game_exists(self, platform: str, game_name: str) -> Tuple[bool, List[Dict]]:
        """Check if a game already exists in the database"""
        try:
            # First get platform ID
            platforms_data = self.bot.cache.get('platforms')
            if not platforms_data:
                platforms_data = await self.bot.fetch_api_endpoint('platforms')

            platform_id = None
            for p in platforms_data:
                if p.get('name', '').lower() == platform.lower():
                    platform_id = p.get('id')
                    break

            if not platform_id:
                return False, []

            # Search for the game
            search_results = await self.bot.fetch_api_endpoint(
                f'roms?platform_id={platform_id}&search_term={game_name}&limit=25'
            )

            if not search_results or not isinstance(search_results, list):
                return False, []

            # Check for close matches
            matches = []
            game_name_lower = game_name.lower()
            for rom in search_results:
                rom_name = rom.get('name', '').lower()
                # Use more sophisticated matching
                if (game_name_lower in rom_name or 
                    rom_name in game_name_lower or
                    self.calculate_similarity(game_name_lower, rom_name) > 0.8):
                    matches.append(rom)

            return bool(matches), matches

        except Exception as e:
            logger.error(f"Error checking game existence: {e}")
            return False, []
    
    def calculate_similarity(self, str1: str, str2: str) -> float:
        """Calculate similarity between two strings"""
        # Remove common words and characters that might differ
        common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to'}
        special_chars = r'[^\w\s]'
        
        str1_clean = re.sub(special_chars, '', ' '.join(word for word in str1.lower().split() if word not in common_words))
        str2_clean = re.sub(special_chars, '', ' '.join(word for word in str2.lower().split() if word not in common_words))
        
        # Simple Levenshtein distance calculation
        if not str1_clean or not str2_clean:
            return 0.0
            
        longer = str1_clean if len(str1_clean) > len(str2_clean) else str2_clean
        shorter = str2_clean if len(str1_clean) > len(str2_clean) else str1_clean
        
        distance = self._levenshtein_distance(longer, shorter)
        return 1 - (distance / len(longer))

    def _levenshtein_distance(self, s1: str, s2: str) -> int:
        """Calculate the Levenshtein distance between two strings"""
        if len(s1) < len(s2):
            return self._levenshtein_distance(s2, s1)

        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]
        
    @commands.Cog.listener()
    async def on_batch_scan_complete(self, new_games: List[Dict[str, str]]):
        """Handle batch scan completion event"""
        async with self.processing_lock:  # Prevent concurrent processing
            try:
                if not new_games:
                    return

                logger.info(f"Processing batch of {len(new_games)} new games")

                # Fetch all pending requests once
                async with aiosqlite.connect(str(self.db_path)) as db:
                    cursor = await db.execute(
                        "SELECT id, user_id, platform, game_name FROM requests WHERE status = 'pending'"
                    )
                    pending_requests = await cursor.fetchall()

                if not pending_requests:
                    return

                # Match games to requests
                fulfillments = []
                notifications = defaultdict(list)  # user_id -> list of fulfilled games

                for req_id, user_id, req_platform, req_game in pending_requests:
                    for new_game in new_games:
                        if (req_platform.lower() == new_game['platform'].lower() and 
                            self.calculate_similarity(req_game.lower(), new_game['name'].lower()) > 0.8):
                            fulfillments.append({
                                'req_id': req_id,
                                'user_id': user_id,
                                'game_name': new_game['name']
                            })
                            notifications[user_id].append(req_game)
                            break  # Stop checking other games once a match is found

                if fulfillments:
                    logger.info(f"Found {len(fulfillments)} matches for auto-fulfillment")
                    
                    # Bulk update requests
                    async with aiosqlite.connect(str(self.db_path)) as db:
                        await db.executemany(
                            """
                            UPDATE requests 
                            SET status = 'fulfilled',
                                updated_at = CURRENT_TIMESTAMP,
                                notes = ?,
                                auto_fulfilled = 1
                            WHERE id = ?
                            """,
                            [(f"Automatically fulfilled by system scan - Found: {f['game_name']}", 
                              f['req_id']) for f in fulfillments]
                        )
                        await db.commit()

                    # Send notifications with rate limiting
                    for user_id, fulfilled_games in notifications.items():
                        try:
                            user = await self.bot.fetch_user(user_id)
                            if user:
                                if len(fulfilled_games) == 1:
                                    message = (
                                        f"✅ Good news! Your request for '{fulfilled_games[0]}' "
                                        f"has been automatically fulfilled!"
                                    )
                                else:
                                    game_list = "\n• ".join(fulfilled_games)
                                    message = (
                                        f"✅ Good news! Multiple requests have been fulfilled:\n• {game_list}"
                                    )
                                
                                await user.send(
                                    message + "\nYou can use the search command to find and download these games."
                                )
                                await asyncio.sleep(1)  # Rate limit between notifications
                        except Exception as e:
                            logger.warning(f"Could not notify user {user_id}: {e}")

            except Exception as e:
                logger.error(f"Error in batch scan completion handler: {e}", exc_info=True)

    async def check_pending_requests(self, platform: str, game_name: str) -> List[Tuple[int, int, str]]:
        """Check if there are any pending requests for this game"""
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute(
                    """
                    SELECT id, user_id, game_name 
                    FROM requests 
                    WHERE platform = ? AND status = 'pending'
                    """,
                    (platform,)
                )
                pending_requests = await cursor.fetchall()

                fulfilled_requests = []
                for req_id, user_id, req_game in pending_requests:
                    if self.calculate_similarity(game_name.lower(), req_game.lower()) > 0.8:
                        fulfilled_requests.append((req_id, user_id, req_game))

                return fulfilled_requests

        except Exception as e:
            logger.error(f"Error checking pending requests: {e}")
            return []

    async def process_request(self, ctx, platform_name, game, details, selected_game, message):
        """Process and save the request"""
        try:
            async with aiosqlite.connect(str(self.db_path)) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM requests WHERE user_id = ? AND status = 'pending'",
                    (ctx.author.id,)
                )
                pending_count = (await cursor.fetchone())[0]

                if pending_count >= 5:
                    await ctx.respond("❌ You already have 5 pending requests. Please wait for them to be fulfilled or cancel some.")
                    return

                # Add IGDB metadata to details if available
                if selected_game:
                    alt_names_str = ""
                    if selected_game.get('alternative_names'):
                        alt_names = [f"{alt['name']} ({alt['comment']}" if alt.get('comment') else alt['name'] 
                                   for alt in selected_game['alternative_names']]
                        alt_names_str = f"\nAlternative Names: {', '.join(alt_names)}"

                    igdb_details = (
                        f"IGDB Metadata:\n"
                        f"Game: {selected_game['name']}{alt_names_str}\n"
                        f"Release Date: {selected_game['release_date']}\n"
                        f"Platforms: {', '.join(selected_game['platforms'])}\n"
                        f"Developers: {', '.join(selected_game['developers']) if selected_game['developers'] else 'Unknown'}\n"
                        f"Publishers: {', '.join(selected_game['publishers']) if selected_game['publishers'] else 'Unknown'}\n"
                        f"Genres: {', '.join(selected_game['genres']) if selected_game['genres'] else 'Unknown'}\n"
                        f"Game Modes: {', '.join(selected_game['game_modes']) if selected_game['game_modes'] else 'Unknown'}\n"
                        f"Summary: {selected_game['summary']}\n"
                    )
                    if details:
                        details = f"{details}\n\n{igdb_details}"
                    else:
                        details = igdb_details

                # Insert the request into the database
                await db.execute(
                    """
                    INSERT INTO requests (user_id, username, platform, game_name, details)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ctx.author.id, str(ctx.author), platform_name, game, details)
                )
                await db.commit()

                if message and selected_game:
                    view = GameSelectView(matches=[selected_game], platform_name=platform_name)  # Use keyword arguments
                    embed = view.create_game_embed(selected_game)
                    embed.set_footer(text=f"Request submitted by {ctx.author}")
                    await message.edit(embed=embed)
                else:
                    # Create basic embed for manual submissions
                    embed = discord.Embed(
                        title=f"{game}",
                        color=discord.Color.green()
                    )
                    embed.add_field(name="Platform", value=platform_name, inline=True)
                    if details:
                        embed.add_field(name="Details", value=details[:1024], inline=False)
                    embed.set_footer(text=f"Request submitted by {ctx.author}")
                    await ctx.respond(embed=embed)

        except Exception as e:
            logger.error(f"Error processing request: {e}")
            await ctx.respond("❌ An error occurred while processing the request.")
             
    @discord.slash_command(name="request", description="Submit a ROM request")
    async def request(
        self,
        ctx: discord.ApplicationContext,
        platform: discord.Option(
            str,
            "Platform for the requested game",
            required=True,
            autocomplete=Search.platform_autocomplete
        ),
        game: discord.Option(str, "Name of the game", required=True),
        details: discord.Option(str, "Additional details (version, region, etc.)", required=False)
    ):
        """Submit a request for a ROM with IGDB verification"""
        await ctx.defer()

        try:
            # Validate platform
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                await ctx.respond("❌ Unable to fetch platforms data")
                return
            
            platform_id = None
            platform_name = None
            sanitized_platforms = self.bot.sanitize_data(raw_platforms, data_type='platforms')
            
            for p in sanitized_platforms:
                if p['name'].lower() == platform.lower():
                    platform_id = p['id']
                    platform_name = p['name']
                    break
                    
            if not platform_id:
                platforms_list = "\n".join(
                    f"• {self.bot.get_cog('Search').get_platform_with_emoji(p['name'])}" 
                    for p in sorted(sanitized_platforms, key=lambda x: x['name'])
                )
                await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            # Check if game exists in current collection
            exists, matches = await self.check_if_game_exists(platform_name, game)
            
            # Search IGDB for game metadata if available
            igdb_matches = []
            if self.igdb_enabled:
                try:
                    igdb_matches = await self.igdb.search_game(game, platform_name)
                except Exception as e:
                    logger.error(f"Error fetching IGDB data: {e}")
                    # Continue without IGDB data if there's an error
            
            if exists:
                embed = discord.Embed(
                    title="Similar Games Found",
                    description="These games appear to be already in our collection:",
                    color=discord.Color.blue()
                )
                
                for rom in matches:
                    embed.add_field(
                        name=rom.get('name', 'Unknown'),
                        value=f"File: {rom.get('file_name', 'Unknown')}\n",
                        inline=False
                    )

                # If we have IGDB matches, check if they confirm it's the same game
                if igdb_matches:
                    # Find the best matching IGDB game
                    best_match = None
                    best_match_score = 0
                    requested_game_lower = game.lower()
                    
                    for igdb_game in igdb_matches:
                        # Check main name
                        if self.calculate_similarity(requested_game_lower, igdb_game['name'].lower()) > best_match_score:
                            best_match = igdb_game
                            best_match_score = self.calculate_similarity(requested_game_lower, igdb_game['name'].lower())
                        
                        # Check alternative names
                        for alt_name in igdb_game.get('alternative_names', []):
                            alt_name_text = alt_name['name'].lower()
                            score = self.calculate_similarity(requested_game_lower, alt_name_text)
                            if score > best_match_score:
                                best_match = igdb_game
                                best_match_score = score

                    if best_match:
                        embed.add_field(
                            name="IGDB Match Found",
                            value=(
                                f"This appears to be: {best_match['name']}\n"
                                f"Released: {best_match['release_date']}\n"
                                f"Developer: {', '.join(best_match['developers']) if best_match['developers'] else 'Unknown'}\n"
                            ),
                            inline=False
                        )
                        if best_match['cover_url']:
                            embed.set_thumbnail(url=best_match['cover_url'])

                # Create confirmation buttons
                class ConfirmView(discord.ui.View):
                    def __init__(self):
                        super().__init__()
                        self.value = None
                        self.message = None

                    async def on_timeout(self) -> None:
                        if self.message:
                            for item in self.children:
                                item.disabled = True
                            await self.message.edit(view=self)

                    @discord.ui.button(label="Different Game/Variant", style=discord.ButtonStyle.primary)
                    async def different_game(self, button: discord.ui.Button, interaction: discord.Interaction):
                        self.value = "different"
                        await interaction.response.defer()
                        self.stop()

                    @discord.ui.button(label="Cancel Request", style=discord.ButtonStyle.secondary)
                    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
                        self.value = "cancel"
                        self.clear_items()
                        cancelled_button = discord.ui.Button(
                            label="Request Cancelled",
                            style=discord.ButtonStyle.secondary,
                            disabled=True
                        )
                        self.add_item(cancelled_button)
                        await interaction.response.edit_message(view=self)
                        self.stop()

                view = ConfirmView()
                view.message = await ctx.respond(embed=embed, view=view)
                await view.wait()
                
                if view.value == "cancel":
                    return
                elif view.value != "different":
                    timeout_view = discord.ui.View()
                    timeout_button = discord.ui.Button(
                        label="Selection Timed Out",
                        style=discord.ButtonStyle.secondary,
                        disabled=True
                    )
                    timeout_view.add_item(timeout_button)
                    await view.message.edit(view=timeout_view)
                    return

            # If we get here, either the game doesn't exist or user confirmed different version needed
            if igdb_matches:
                select_view = GameSelectView(igdb_matches, platform_name)  # Pass platform_name here
                select_embed = discord.Embed(
                    title="Game Selection",
                    description="Please select the correct game from the list below:",
                    color=discord.Color.blue()
                )
                # Use the view's create_game_embed method with the first match
                initial_embed = select_view.create_game_embed(igdb_matches[0])
                select_view.message = await ctx.respond(embed=initial_embed, view=select_view)
                
                # Wait for selection
                await select_view.wait()
                
                if not select_view.selected_game:
                    timeout_view = discord.ui.View()
                    timeout_button = discord.ui.Button(
                        label="Selection Timed Out",
                        style=discord.ButtonStyle.secondary,
                        disabled=True
                    )
                    timeout_view.add_item(timeout_button)
                    await select_view.message.edit(view=timeout_view)
                    return
                elif select_view.selected_game == "manual":
                    selected_game = None
                else:
                    selected_game = select_view.selected_game
                    await self.process_request(ctx, platform_name, game, details, selected_game, select_view.message)
                    return

            # If we get here, either no IGDB matches or manual entry selected
            await self.process_request(ctx, platform_name, game, details, selected_game, None)

        except Exception as e:
            logger.error(f"Error submitting request: {e}")
            await ctx.respond("❌ An error occurred while submitting your request.")
    
    @discord.slash_command(name="my_requests", description="View your ROM requests")
    async def my_requests(self, ctx: discord.ApplicationContext):
        """View your submitted requests"""
        await ctx.defer()

        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT * FROM requests WHERE user_id = ? ORDER BY created_at DESC",
                    (ctx.author.id,)
                )
                requests = await cursor.fetchall()

                if not requests:
                    await ctx.respond("You haven't made any requests yet.")
                    return

                embeds = []
                for req in requests:
                    embed = discord.Embed(
                        title=f"Request #{req[0]}",
                        color=discord.Color.blue() if req[6] == 'pending' else discord.Color.green(),
                        timestamp=datetime.fromisoformat(req[7].replace('Z', '+00:00'))
                    )
                    embed.add_field(name="Platform", value=req[3], inline=True)
                    embed.add_field(name="Game", value=req[4], inline=True)
                    embed.add_field(name="Status", value=req[6].title(), inline=True)
                    if req[5]:  # details
                        embed.add_field(name="Details", value=req[5][:1024], inline=False)
                    if req[9]:  # fulfilled_by
                        embed.add_field(name="Fulfilled By", value=req[10], inline=True)
                    if req[11]:  # notes
                        embed.add_field(name="Notes", value=req[11], inline=False)
                    embeds.append(embed)

                # Send embeds (Discord has a limit of 10 embeds per message)
                for i in range(0, len(embeds), 10):
                    if i == 0:
                        await ctx.respond(embeds=embeds[i:i+10])
                    else:
                        await ctx.channel.send(embeds=embeds[i:i+10])

        except Exception as e:
            logger.error(f"Error fetching requests: {e}")
            await ctx.respond("❌ An error occurred while fetching your requests.")

    @discord.slash_command(name="cancel_request", description="Cancel one of your pending requests")
    async def cancel_request(
        self,
        ctx: discord.ApplicationContext,
        request_id: discord.Option(int, "ID of the request to cancel", required=True)
    ):
        """Cancel a pending request"""
        await ctx.defer()

        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Verify request exists and belongs to user
                cursor = await db.execute(
                    "SELECT status FROM requests WHERE id = ? AND user_id = ?",
                    (request_id, ctx.author.id)
                )
                result = await cursor.fetchone()

                if not result:
                    await ctx.respond("❌ Request not found or you don't have permission to cancel it.")
                    return

                if result[0] != 'pending':
                    await ctx.respond("❌ Only pending requests can be cancelled.")
                    return

                # Cancel the request
                await db.execute(
                    "UPDATE requests SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (request_id,)
                )
                await db.commit()

                await ctx.respond(f"✅ Request #{request_id} has been cancelled.")

        except Exception as e:
            logger.error(f"Error cancelling request: {e}")
            await ctx.respond("❌ An error occurred while cancelling the request.")

    @commands.has_permissions(administrator=True)
    @discord.slash_command(name="request_admin", description="Admin commands for managing requests")
    async def request_admin(
        self,
        ctx: discord.ApplicationContext,
        action: discord.Option(
            str,
            "Action to perform",
            required=True,
            choices=["list", "fulfill", "reject", "addnote"]
        ),
        request_id: discord.Option(int, "ID of the request", required=False),
        note: discord.Option(str, "Note or comment to add", required=False)
    ):
        """Admin commands for managing requests"""
        await ctx.defer()

        try:
            async with aiosqlite.connect(self.db_path) as db:
                if action == "list":
                    cursor = await db.execute(
                        "SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at ASC"
                    )
                    requests = await cursor.fetchall()

                    if not requests:
                        await ctx.respond("No pending requests.")
                        return

                    embeds = []
                    for req in requests:
                        # Parse the details field to extract IGDB metadata if it exists
                        details = req[5] if req[5] else ""
                        game_data = {}
                        cover_url = None
                        igdb_name = req[4]  # Default to requested game name

                        if "IGDB Metadata:" in details:
                            # Extract IGDB metadata from details
                            try:
                                metadata_lines = details.split("IGDB Metadata:\n")[1].split("\n")
                                for line in metadata_lines:
                                    if ": " in line:
                                        key, value = line.split(": ", 1)
                                        game_data[key] = value
                                        if key == "Game":
                                            igdb_name = value.split(" (", 1)[0]  # Remove any parentheses part

                                # Look for cover URL in the whole details text
                                cover_matches = re.findall(r'cover_url:\s*(https://[^\s]+)', details)
                                if cover_matches:
                                    cover_url = cover_matches[0]
                            except Exception as e:
                                logger.error(f"Error parsing metadata: {e}")

                        embed = discord.Embed(
                            title=igdb_name,
                            color=discord.Color.blue(),
                            timestamp=datetime.fromisoformat(req[7].replace('Z', '+00:00'))
                        )

                        # Set IGDB logo as thumbnail and cover as main image if available
                        if cover_url:
                            embed.set_image(url=cover_url)
                        else:
                            embed.set_thumbnail(url="https://www.igdb.com/packs/static/igdbLogo-bcd49db90003ee7cd4f4.svg")

                        # Platform field
                        embed.add_field(
                            name="Platform",
                            value=req[3],  # platform
                            inline=True
                        )

                        # Genre field if available
                        if "Genres" in game_data:
                            genres = game_data["Genres"].split(", ")[:2]
                            embed.add_field(
                                name="Genre",
                                value=", ".join(genres),
                                inline=True
                            )

                        # Release Date if available
                        if "Release Date" in game_data:
                            try:
                                date_obj = datetime.strptime(game_data["Release Date"], "%Y-%m-%d")
                                formatted_date = date_obj.strftime("%B %d, %Y")
                            except:
                                formatted_date = game_data["Release Date"]
                            embed.add_field(
                                name="Release Date",
                                value=formatted_date,
                                inline=True
                            )

                        # Summary if available
                        if "Summary" in game_data:
                            summary = game_data["Summary"]
                            if len(summary) > 300:
                                summary = summary[:297] + "..."
                            embed.add_field(
                                name="Summary",
                                value=summary,
                                inline=False
                            )

                        # Companies if available
                        companies = []
                        if "Developers" in game_data:
                            developers = game_data["Developers"].split(", ")[:2]
                            companies.extend(developers)
                        if "Publishers" in game_data:
                            publishers = game_data["Publishers"].split(", ")
                            remaining_slots = 2 - len(companies)
                            if remaining_slots > 0:
                                companies.extend(publishers[:remaining_slots])

                        if companies:
                            embed.add_field(
                                name="Companies",
                                value=", ".join(companies),
                                inline=True
                            )

                        # Links section (IGDB)
                        igdb_link_name = igdb_name.lower().replace(' ', '-')
                        igdb_link_name = re.sub(r'[^a-z0-9-]', '', igdb_link_name)
                        igdb_url = f"https://www.igdb.com/games/{igdb_link_name}"
                        embed.add_field(
                            name="Links",
                            value=f"[IGDB]({igdb_url})",
                            inline=True
                        )

                        # Request information at the bottom
                        embed.set_footer(text=f"Request #{req[0]} • Requested by {req[2]}")

                        embeds.append(embed)

                    # Send embeds (Discord has a limit of 10 embeds per message)
                    for i in range(0, len(embeds), 10):
                        if i == 0:
                            await ctx.respond(embeds=embeds[i:i+10])
                        else:
                            await ctx.channel.send(embeds=embeds[i:i+10])

                elif action in ["fulfill", "reject"]:
                    if not request_id:
                        await ctx.respond("❌ Request ID is required for this action.")
                        return

                    # Update request status
                    await db.execute(
                        """
                        UPDATE requests 
                        SET status = ?, fulfilled_by = ?, fulfiller_name = ?, updated_at = CURRENT_TIMESTAMP, notes = ?
                        WHERE id = ?
                        """,
                        (action, ctx.author.id, str(ctx.author), note, request_id)
                    )
                    await db.commit()

                    # Fetch request details to notify user
                    cursor = await db.execute("SELECT user_id, game_name FROM requests WHERE id = ?", (request_id,))
                    req = await cursor.fetchone()
                    
                    if req:
                        try:
                            user = await self.bot.fetch_user(req[0])
                            if action == "fulfill":
                                await user.send(f"✅ Your request for '{req[1]}' has been fulfilled!")
                            else:
                                await user.send(f"❌ Your request for '{req[1]}' has been rejected." + 
                                              (f"\nReason: {note}" if note else ""))
                        except:
                            logger.warning(f"Could not DM user {req[0]}")

                    await ctx.respond(f"✅ Request #{request_id} has been marked as {action}ed.")

                elif action == "addnote":
                    if not request_id or not note:
                        await ctx.respond("❌ Both request ID and note are required for this action.")
                        return

                    await db.execute(
                        "UPDATE requests SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (note, request_id)
                    )
                    await db.commit()

                    await ctx.respond(f"✅ Note added to request #{request_id}.")

        except Exception as e:
            logger.error(f"Error in request admin command: {e}")
            await ctx.respond("❌ An error occurred while processing the command.")

def setup(bot):
    bot.add_cog(Request(bot))
