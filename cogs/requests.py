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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class Request(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # Create data directory if it doesn't exist
        self.data_dir = Path('data')
        self.data_dir.mkdir(exist_ok=True)
        
        # Set database path in data directory
        self.db_path = self.data_dir / 'requests.db'
        
        self.requests_enabled = bot.config.REQUESTS_ENABLED
        bot.loop.create_task(self.setup_database())
        self.bot.add_listener(self.on_scan_complete, 'on_scan_complete')
    
    async def cog_check(self, ctx: discord.ApplicationContext) -> bool:
        """Check if requests are enabled before any command in this cog"""
        if not self.requests_enabled and not ctx.author.guild_permissions.administrator:
            await ctx.respond("❌ The request system is currently disabled.")
            return False
        return True    
    
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
                f'roms?platform_id={platform_id}&search_term={game_name}&limit=5'
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

    async def check_pending_requests(self, platform: str, game_name: str) -> List[int]:
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

    @commands.has_permissions(administrator=True)
    @discord.slash_command(name="toggle_requests", description="Enable or disable the request system")
    async def toggle_requests(
        self,
        ctx: discord.ApplicationContext,
        enabled: discord.Option(bool, "Enable or disable requests", required=True)
    ):
        """Toggle the request system on/off"""
        self.requests_enabled = enabled
        status = "enabled" if enabled else "disabled"
        await ctx.respond(f"✅ Request system has been {status}.")

    @discord.slash_command(name="request_status", description="Check if the request system is enabled")
    async def requeststatus(self, ctx: discord.ApplicationContext):
        """Check the current status of the request system"""
        status = "enabled" if self.requests_enabled else "disabled"
        await ctx.respond(f"Request system is currently {status}.")   
    
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
        """Submit a request for a ROM"""
        await ctx.defer()

        try:
            # Get platform data
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                await ctx.respond("❌ Unable to fetch platforms data")
                return
            
            # Find matching platform
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

            # First check if the game already exists
            exists, matches = await self.check_if_game_exists(platform_name, game)
            
            if exists:
                embed = discord.Embed(
                    title="Game Already Available",
                    description="This game appears to already be in our collection:",
                    color=discord.Color.blue()
                )
                
                for rom in matches:
                    embed.add_field(
                        name=rom.get('name', 'Unknown'),
                        value=f"File: {rom.get('file_name', 'Unknown')}\n",
                        inline=False
                    )
                    
                embed.set_footer(text="If these aren't what you're looking for, you can still submit your request.")
                
                # Create confirmation buttons
                class ConfirmView(discord.ui.View):
                    def __init__(self):
                        super().__init__()
                        self.value = None

                    @discord.ui.button(label="Submit Request Anyway", style=discord.ButtonStyle.primary)
                    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
                        self.value = True
                        self.stop()

                    @discord.ui.button(label="Cancel Request", style=discord.ButtonStyle.secondary)
                    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
                        self.value = False
                        self.stop()

                view = ConfirmView()
                msg = await ctx.respond(embed=embed, view=view)
                
                # Wait for the user's response
                await view.wait()
                
                if not view.value:
                    await ctx.respond("Request cancelled.")
                    return

            # Continue with request submission
            async with aiosqlite.connect(str(self.db_path)) as db:
                # Check pending requests limit
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM requests WHERE user_id = ? AND status = 'pending'",
                    (ctx.author.id,)
                )
                pending_count = (await cursor.fetchone())[0]

                if pending_count >= 3:
                    await ctx.respond("❌ You already have 3 pending requests. Please wait for them to be fulfilled or cancel some.")
                    return

                # Insert the new request
                await db.execute(
                    """
                    INSERT INTO requests (user_id, username, platform, game_name, details)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ctx.author.id, str(ctx.author), platform_name, game, details)
                )
                await db.commit()

                embed = discord.Embed(
                    title="ROM Request Submitted",
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )
                embed.add_field(name="Platform", value=platform_name, inline=True)
                embed.add_field(name="Game", value=game, inline=True)
                if details:
                    embed.add_field(name="Details", value=details, inline=False)
                embed.set_footer(text=f"Requested by {ctx.author}")

                await ctx.respond(embed=embed)

        except Exception as e:
            logger.error(f"Error submitting request: {e}")
            await ctx.respond("❌ An error occurred while submitting your request.")

    async def on_scan_complete(self, stats: Dict):
        """Handle scan completion event"""
        try:
            # Get the platform and game name from the scan stats
            platform = stats.get('current_platform')
            game_name = stats.get('current_rom')
            
            if not platform or not game_name:
                return

            # Check for pending requests that match this game
            fulfilled_requests = await self.check_pending_requests(platform, game_name)
            
            if not fulfilled_requests:
                return

            async with aiosqlite.connect(self.db_path) as db:
                for req_id, user_id, req_game in fulfilled_requests:
                    # Update request status
                    await db.execute(
                        """
                        UPDATE requests 
                        SET status = 'fulfilled', 
                            updated_at = CURRENT_TIMESTAMP, 
                            notes = ?, 
                            auto_fulfilled = 1
                        WHERE id = ?
                        """,
                        (f"Automatically fulfilled by system scan - Found: {game_name}", req_id)
                    )
                    
                    # Notify user
                    try:
                        user = await self.bot.fetch_user(user_id)
                        await user.send(
                            f"✅ Good news! Your request for '{req_game}' has been automatically fulfilled! "
                            f"The game was found during a system scan.\n"
                            f"You can use the search command to find and download it."
                        )
                    except:
                        logger.warning(f"Could not DM user {user_id}")
                
                await db.commit()

        except Exception as e:
            logger.error(f"Error in scan completion handler: {e}")

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
                        embed.add_field(name="Details", value=req[5], inline=False)
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
                        embed = discord.Embed(
                            title=f"Request #{req[0]}",
                            color=discord.Color.blue(),
                            timestamp=datetime.fromisoformat(req[7].replace('Z', '+00:00'))
                        )
                        embed.add_field(name="Requester", value=req[2], inline=True)
                        embed.add_field(name="Platform", value=req[3], inline=True)
                        embed.add_field(name="Game", value=req[4], inline=True)
                        if req[5]:  # details
                            embed.add_field(name="Details", value=req[5], inline=False)
                        embeds.append(embed)

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