from discord.ext import commands
import discord
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Union
import random
import qrcode
from PIL import Image
import io
import aiohttp
from io import BytesIO
import asyncio
import time

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

class ROM_View(discord.ui.View):
    """Shared view for both search and random commands"""
    def __init__(self, bot, search_results: List[Dict], author_id: int, platform_name: Optional[str] = None, initial_message: Optional[discord.Message] = None):
        super().__init__()
        self.bot = bot
        self.search_results = search_results
        self.author_id = author_id
        self.platform_name = platform_name
        self.message = initial_message
        
        # Create select menu
        self.select = discord.ui.Select(
            placeholder="Choose a ROM to view details",
            custom_id="rom_select"
        )
        
        # Add options to select menu
        for rom in search_results[:25]:
            display_name = rom['name'][:75] if len(rom['name']) > 75 else rom['name']
            file_name = rom.get('file_name', 'Unknown filename')
            file_size = self.format_file_size(rom.get('file_size_bytes'))
            
            truncated_filename = (file_name[:47] + '...') if len(file_name) > 50 else file_name
            
            self.select.add_option(
                label=display_name,
                value=str(rom['id']),
                description=f"{truncated_filename} ({file_size})"
            )
        
        self.select.callback = self.select_callback
        self.add_item(self.select)

    @staticmethod
    def format_file_size(size_bytes: Union[int, float]) -> str:
        """Format size in bytes to human readable format"""
        if not size_bytes or not isinstance(size_bytes, (int, float)):
            return "Unknown size"
        
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        size_value = float(size_bytes)
        unit_index = 0
        while size_value >= 1024 and unit_index < len(units) - 1:
            size_value /= 1024
            unit_index += 1
        return f"{size_value:.2f} {units[unit_index]}"

    async def create_rom_embed(self, rom_data: Dict) -> discord.Embed:
        """Create an embed for ROM details"""
        try:
            file_name = rom_data.get('file_name', 'unknown_file').replace(' ', '%20')
            download_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_data['id']}/content/{file_name}"
            igdb_name = rom_data['name'].lower().replace(' ', '-')
            igdb_name = re.sub(r'[^a-z0-9-]', '', igdb_name)
            igdb_url = f"https://www.igdb.com/games/{igdb_name}"
            romm_url = f"{self.bot.config.DOMAIN}/rom/{rom_data['id']}"
            logo_url = "https://raw.githubusercontent.com/rommapp/romm/release/.github/resources/romm_complete.png"
            
            embed = discord.Embed(
                title=f"{rom_data['name']}",
                color=discord.Color.green()
            )
            
            # Set romm logo as thumbnail
            embed.set_thumbnail(url=logo_url)
            
            # Add cover image
            if cover_url := rom_data.get('url_cover'):
                embed.set_image(url=cover_url)
            
            # Get platform name if not provided
            platform_name = self.platform_name
            if not platform_name and (platform_id := rom_data.get('platform_id')):
                platforms_data = self.bot.cache.get('platforms')
                if platforms_data:
                    for p in platforms_data:
                        if p.get('id') == platform_id:
                            platform_name = p.get('name', 'Unknown Platform')
                            break
            
            if platform_name:
                embed.add_field(name="Platform", value=platform_name, inline=True)
            
            # Add other fields
            if genres := rom_data.get('genres'):
                genre_list = ", ".join(genres) if isinstance(genres, list) else genres
                embed.add_field(name="Genres", value=genre_list, inline=True)
            
            if release_date := rom_data.get('first_release_date'):
                try:
                    release_datetime = datetime.fromtimestamp(int(release_date))
                    formatted_date = release_datetime.strftime('%b %d, %Y')
                    embed.add_field(name="Release Date", value=formatted_date, inline=True)
                except (ValueError, TypeError) as e:
                    logger.error(f"Error formatting date: {e}")
            
            if summary := rom_data.get('summary'):
                if len(summary) > 200:
                    summary = summary[:197] + "..."
                embed.add_field(name="Summary", value=summary, inline=False)
            
            if companies := rom_data.get('companies'):
                companies_str = ", ".join(companies) if isinstance(companies, list) else str(companies)
                if companies_str:
                    embed.add_field(name="Companies", value=companies_str, inline=False)
            
            # Download link with size
            file_size = self.format_file_size(rom_data.get('file_size_bytes'))
            embed.add_field(
                name=f"Download ({file_size})",
                value=f"[{rom_data.get('file_name', 'Download')}]({download_url})",
                inline=False
            )
            
            # Hash values
            hashes = []
            if crc := rom_data.get('crc_hash'):
                if md5 := rom_data.get('md5_hash'):
                    hashes.append(f"**CRC:** {crc} **MD5:** {md5}")
                else:
                    hashes.append(f"**CRC:** {crc}")
            elif md5 := rom_data.get('md5_hash'):
                hashes.append(f"**MD5:** {md5}")
            if sha1 := rom_data.get('sha1_hash'):
                hashes.append(f"**SHA1:** {sha1}")

            if hashes:
                embed.add_field(name="Hash Values", value="\n".join(hashes), inline=False)
                
            return embed
        except Exception as e:
            logger.error(f"Error creating ROM embed: {e}")
            raise

    async def generate_qr(self, url: str) -> discord.File:
        """Generate QR code for download URL"""
        try:
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=2,
            )
            qr.add_data(url)
            qr.make(fit=True)
            
            qr_img = qr.make_image(fill_color="black", back_color="white")
            
            byte_arr = BytesIO()
            qr_img.save(byte_arr, format='PNG')
            byte_arr.seek(0)
            
            return discord.File(byte_arr, filename="download_qr.png")
        except Exception as e:
            logger.error(f"Error generating QR code: {e}")
            return None

    async def handle_qr_trigger(self, interaction: discord.Interaction, trigger_type: str):
        """Handle QR code generation and sending"""
        try:
            # For search results with multiple ROMs
            if len(self.search_results) > 1 and not hasattr(interaction, 'values'):
                await interaction.channel.send("Please select a ROM first!")
                return

            # Get the ROM data
            selected_rom = None
            if len(self.search_results) == 1:
                selected_rom = self.search_results[0]
            elif hasattr(interaction, 'values'):
                selected_rom_id = int(interaction.values[0])
                selected_rom = next((rom for rom in self.search_results if rom['id'] == selected_rom_id), None)

            if not selected_rom:
                await interaction.channel.send("❌ Unable to find ROM data")
                return

            file_name = selected_rom.get('file_name', 'unknown_file').replace(' ', '%20')
            download_url = f"{self.bot.config.DOMAIN}/api/roms/{selected_rom['id']}/content/{file_name}"
                
            qr_file = await self.generate_qr(download_url)
            if qr_file:
                # Create an embed for the QR code
                embed = discord.Embed(
                    title=f"📱 QR Code for {selected_rom['name']}",
                    description=f"Triggered by {trigger_type}",
                    color=discord.Color.blue()
                )
                embed.set_image(url="attachment://download_qr.png")
                
                await interaction.channel.send(
                    embed=embed,
                    file=qr_file
                )
            else:
                await interaction.channel.send("❌ Failed to generate QR code")
        except Exception as e:
            logger.error(f"Error handling QR code request: {e}")
            await interaction.channel.send("❌ An error occurred while generating the QR code", ephemeral=True)

    async def start_watching_triggers(self, interaction: discord.Interaction):
        """Start watching for QR code triggers"""
        try:
            # Ensure we have a valid message reference
            if not self.message:
                logger.warning("No message reference for QR code triggers")
                return

            def message_check(m):
                # First verify the reference exists
                if not m.reference or not hasattr(m.reference, 'cached_message'):
                    return False
                    
                referenced_message = m.reference.cached_message
                # Make sure we can access the referenced message
                if not referenced_message:
                    return False
                    
                return (
                    any(keyword in m.content.lower() for keyword in {'qr'}) and
                    referenced_message.author.id == self.bot.user.id and  # Check if referencing our bot
                    referenced_message.embeds and  # Referenced message should have embed
                    self.message.embeds and  # Original message should have embed
                    referenced_message.embeds[0].title == self.message.embeds[0].title  # Compare embed titles
                )  
            def reaction_check(reaction, user):
                # List of accepted emoji names and Unicode emojis
                valid_emojis = {
                    'qr_code',  # Custom emoji names
                    '📱', 'qr'  # Unicode emojis and text alternatives
                }
                return (
                    user.id == self.author_id and
                    reaction.message.embeds and  # Ensure message has embeds
                    self.message.embeds and  # Ensure original message has embeds
                    reaction.message.embeds[0].title == self.message.embeds[0].title and  # Compare embeds
                    (getattr(reaction.emoji, 'name', str(reaction.emoji)).lower() in valid_emojis)  # Check emoji safely
                )
            # Create tasks for both events
            message_task = asyncio.create_task(
                self.bot.wait_for(
                    'message',
                    timeout=60.0,
                    check=message_check
                )
            )
            
            reaction_task = asyncio.create_task(
                self.bot.wait_for(
                    'reaction_add',
                    timeout=60.0,
                    check=reaction_check
                )
            )

            # Wait for either task to complete
            try:
                done, pending = await asyncio.wait(
                    [message_task, reaction_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Cancel pending tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

                # Get the result from the completed task
                result = done.pop().result()
                
                if isinstance(result, discord.Message):
                    trigger_type = "message reply"
                else:
                    reaction, user = result
                    emoji_identifier = str(reaction.emoji.name) if hasattr(reaction.emoji, 'name') else str(reaction.emoji)
                    trigger_type = f"reaction {reaction.emoji}"
                
                await self.handle_qr_trigger(interaction, trigger_type)

            except asyncio.TimeoutError:
                logger.info("QR code trigger watch timed out")
                return

        except Exception as e:
            logger.error(f"Error watching for triggers: {e}")

    async def watch_for_qr_triggers(self, interaction: discord.Interaction):
        """Start watching for QR code triggers after ROM selection"""
        if not self.message:
            logger.warning("No message reference for QR code triggers")
            return
            
        await self.start_watching_triggers(interaction)

    async def select_callback(self, interaction: discord.Interaction):
        """Handle ROM selection"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This selection menu isn't for you!", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            selected_rom_id = int(interaction.data['values'][0])
            selected_rom = next((rom for rom in self.search_results if rom['id'] == selected_rom_id), None)

            if selected_rom:
                try:
                    detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{selected_rom_id}')
                    if detailed_rom:
                        selected_rom.update(detailed_rom)
                except Exception as e:
                    logger.error(f"Error fetching detailed ROM data: {e}")

                embed = await self.create_rom_embed(selected_rom)
                await interaction.message.edit(
                    content=interaction.message.content,
                    embed=embed,
                    view=self
                )
                    
                # Store the message for QR code trigger reference
                self.message = interaction.message
                    
                # Start watching for QR code triggers
                await self.watch_for_qr_triggers(interaction)
            else:
                await interaction.followup.send("❌ Error retrieving ROM details", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in select callback: {e}")
            await interaction.followup.send("❌ An error occurred while processing your selection", ephemeral=True)

class Search(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def platform_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete function for platform names."""
        try:
            platforms_data = self.bot.cache.get('platforms')

            if not platforms_data:
                raw_platforms = await self.bot.fetch_api_endpoint('platforms')
                if raw_platforms:
                    platforms_data = self.bot.sanitize_data(raw_platforms, data_type='platforms')

            if platforms_data:
                platform_names = [p.get('name', '') for p in platforms_data if p.get('name')]
                user_input = ctx.value.lower()
                return [name for name in platform_names if user_input in name.lower()][:25]
        except Exception as e:
            logger.error(f"Error in platform autocomplete: {e}")
        return []

    @discord.slash_command(name="firmware", description="List firmware files available for a platform")
    async def firmware(self, ctx: discord.ApplicationContext, 
                      platform: discord.Option(str, "Platform to list firmware for", 
                                            required=True, autocomplete=platform_autocomplete)):
        """List firmware files for a specific platform."""
        await ctx.defer()

        try:
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                await ctx.respond("Failed to fetch platforms data")
                return

            platform_data = None
            for p in raw_platforms:
                if platform.lower() in p.get('name', '').lower():
                    platform_data = p
                    break

            if not platform_data:
                platforms_list = "\n".join(f"• {p.get('name', 'Unknown')}" for p in raw_platforms)
                await ctx.respond(f"Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            firmware_data = await self.bot.fetch_api_endpoint(f'firmware?platform_id={platform_data["id"]}')

            if not firmware_data:
                await ctx.respond(f"No firmware files found for platform '{platform_data.get('name', platform)}'")
                return

            def format_file_size(size_bytes):
                return ROM_View.format_file_size(size_bytes)

            embeds = []
            current_embed = discord.Embed(
                title=f"Firmware Files for {platform_data.get('name', platform)}",
                description=f"Found {len(firmware_data)} firmware file(s)",
                color=discord.Color.blue()
            )
            field_count = 0

            for firmware in firmware_data:
                file_name = firmware.get('file_name', 'unknown_file').replace(' ', '%20')
                download_url = f"{self.bot.config.DOMAIN}/api/firmware/{firmware.get('id')}/content/{file_name}"

                field_value = (
                    f"**Size:** {format_file_size(firmware.get('file_size_bytes'))}\n"
                    f"**CRC:** {firmware.get('crc_hash', 'N/A')}\n"
                    f"**MD5:** {firmware.get('md5_hash', 'N/A')}\n"
                    f"**SHA1:** {firmware.get('sha1_hash', 'N/A')}\n"
                    f"**Download:** [Link]({download_url})"
                )

                if field_count >= 25:
                    embeds.append(current_embed)
                    current_embed = discord.Embed(
                        title=f"Firmware Files for {platform_data.get('name', platform)} (Continued)",
                        color=discord.Color.blue()
                    )
                    field_count = 0

                current_embed.add_field(
                    name=firmware.get('file_name', 'Unknown Firmware'),
                    value=field_value,
                    inline=False
                )
                field_count += 1

            if field_count > 0:
                embeds.append(current_embed)

            if len(embeds) > 1:
                for i, embed in enumerate(embeds):
                    embed.set_footer(text=f"Page {i+1} of {len(embeds)}")

            for embed in embeds:
                await ctx.respond(embed=embed)

        except Exception as e:
            logger.error(f"Error in firmware command: {e}")
            await ctx.respond("❌ An error occurred while fetching firmware data")

    @discord.slash_command(name="random", description="Get a random ROM from the collection or a specific platform")
    async def random(
        self, 
        ctx: discord.ApplicationContext,
        platform: discord.Option(
            str, 
            "Platform to get random ROM from", 
            required=False,
            autocomplete=platform_autocomplete
        )
    ):
        """Get a random ROM from the collection or a specific platform."""
        await ctx.defer()
        try:
            if platform:
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
                        rom_count = p['rom_count']
                        break
                    
                if not platform_id:
                    platforms_list = "\n".join(f"• {name}" for name in sorted([p['name'] for p in sanitized_platforms]))
                    await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                    return

                if rom_count <= 0:
                    await ctx.respond(f"❌ No ROMs found for platform '{platform_name}'")
                    return

                # Try up to 5 times to find a valid ROM for the specific platform
                max_attempts = 5
                for attempt in range(max_attempts):
                    # Calculate random offset (subtract 1 from limit to avoid exceeding total)
                    random_offset = random.randint(0, max(0, rom_count - 1))
                
                    # First get the list of ROMs at this offset
                    rom_list = await self.bot.fetch_api_endpoint(
                        f'roms?platform_id={platform_id}&limit=1&order_by=random&order_dir=asc'
                    )
                
                    if rom_list and isinstance(rom_list, list) and len(rom_list) > 0:
                        rom_data = rom_list[0]  # Get the first ROM from the result
                    
                        # Fetch detailed ROM data if available
                        try:
                            detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom_data["id"]}')
                            if detailed_rom:
                                rom_data.update(detailed_rom)
                        except Exception as e:
                            logger.error(f"Error fetching detailed ROM data: {e}")
                    
                        # Create ROM view without select menu
                        view = ROM_View(self.bot, [rom_data], ctx.author.id, platform_name)
                        view.remove_item(view.select)
                        embed = await view.create_rom_embed(rom_data)

                        initial_message = await ctx.respond(
                            f"🎲 Found a random ROM from {platform_name}:",
                            embed=embed,
                            view=view
                        )
    
                        if isinstance(initial_message, discord.Interaction):
                            initial_message = await initial_message.original_response()
                    
                        view.message = initial_message
                        await view.watch_for_qr_triggers(ctx.interaction)
                        return

                    logger.info(f"Random ROM attempt {attempt + 1} for platform {platform_name} failed")
                    await asyncio.sleep(1)

            else:
                # Original random logic for any platform
                stats_data = self.bot.cache.get('stats')
                if not stats_data or 'Roms' not in stats_data:
                    await ctx.respond("❌ Unable to fetch collection data")
                    return
                
                total_roms = stats_data['Roms']
                if total_roms <= 0:
                    await ctx.respond("❌ No ROMs found in the collection")
                    return

                # Try up to 5 times to find a valid ROM
                max_attempts = 5
                for attempt in range(max_attempts):
                    random_rom_id = random.randint(1, total_roms)
                    rom_data = await self.bot.fetch_api_endpoint(f'roms/{random_rom_id}')
                
                    if rom_data and isinstance(rom_data, dict) and rom_data.get('id'):
                        # Get platform name
                        platform_name = None
                        if platform_id := rom_data.get('platform_id'):
                            platforms_data = self.bot.cache.get('platforms')
                            if platforms_data:
                                for p in platforms_data:
                                    if p.get('id') == platform_id:
                                        platform_name = p.get('name')
                                        break

                        # Create ROM view without select menu
                        view = ROM_View(self.bot, [rom_data], ctx.author.id, platform_name)
                        view.remove_item(view.select)
                        embed = await view.create_rom_embed(rom_data)

                        initial_message = await ctx.respond(
                            f"🎲 Found a random ROM" + (f" from {platform_name}" if platform_name else "") + ":",
                            embed=embed,
                            view=view
                        )
    
                        if isinstance(initial_message, discord.Interaction):
                            initial_message = await initial_message.original_response()
                    
                        view.message = initial_message
                        await view.watch_for_qr_triggers(ctx.interaction)
                        return

                    logger.info(f"Random ROM attempt {attempt + 1} with ID {random_rom_id} failed")
                    await asyncio.sleep(1)

            # If we tried max_attempts times and couldn't find a valid ROM
            await ctx.respond("❌ Failed to find a valid random ROM. Please try again.")

        except Exception as e:
            logger.error(f"Error in random command: {e}", exc_info=True)
            await ctx.respond("❌ An error occurred while fetching a random ROM")

    @discord.slash_command(name="search", description="Search for a ROM")
    async def search(self, ctx: discord.ApplicationContext,
                    platform: discord.Option(str, "Platform to search in", 
                                          required=True,
                                          autocomplete=platform_autocomplete),
                    game: discord.Option(str, "Game name to search for", required=True)):
        """Search for a ROM and provide download options."""
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
                platforms_list = "\n".join(f"• {name}" for name in sorted([p['name'] for p in sanitized_platforms]))
                await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            # Search for ROMs
            search_term = game.strip()
            search_results = await self.bot.fetch_api_endpoint(
                f'roms?platform_id={platform_id}&search_term={search_term}&limit=25'
            )
            
            if not search_results or len(search_results) == 0:
                search_words = search_term.split()
                if len(search_words) > 1:
                    search_term = ' '.join(search_words)
                    search_results = await self.bot.fetch_api_endpoint(
                        f'roms?platform_id={platform_id}&search_term={search_term}&limit=25'
                    )

            if not search_results or not isinstance(search_results, list) or len(search_results) == 0:
                await ctx.respond(f"No ROMs found matching '{game}' for platform '{platform_name}'")
                return

            # Sort results
            def sort_roms(rom):
                game_name = rom['name'].lower()
                filename = rom.get('file_name', '').upper()

                if game_name.startswith("the "):
                    game_name = game_name[4:]
        
                if "(USA)" in filename or "(USA, WORLD)" in filename:
                    if "BETA" in filename or "(PROTOTYPE)" in filename:
                        file_priority = 2
                    else:
                        file_priority = 0
                elif "(WORLD)" in filename:
                    if "BETA" in filename or "(PROTOTYPE)" in filename:
                        file_priority = 3
                    else:
                        file_priority = 1
                elif "BETA" in filename or "(PROTOTYPE)" in filename:
                    file_priority = 5
                elif "(DEMO)" in filename or "PROMOTIONAL" in filename or "SAMPLE" in filename or "SAMPLER" in filename:
                    if "BETA" in filename or "(PROTOTYPE)" in filename:
                        file_priority = 6
                    else:
                        file_priority = 4
                else:
                    file_priority = 3
        
                return (game_name, file_priority, filename.lower())

            search_results.sort(key=sort_roms)

            # Create initial message
            if len(search_results) >= 25:
                initial_content = (
                    f"Found 25+ ROMs matching '{game}' for platform '{platform_name}'. "
                    f"Showing first 25 results.\nPlease refine your search terms for more specific results:"
                )
            else:
                initial_content = f"Found {len(search_results)} ROMs matching '{game}' for platform '{platform_name}':"

            # Send message with view
            message = await ctx.respond(
                initial_content,
                view=ROM_View(self.bot, search_results, ctx.author.id, platform_name)
            )

        except Exception as e:
            logger.error(f"Error in search command: {e}", exc_info=True)
            await ctx.respond("❌ An error occurred while searching for ROMs")

def setup(bot):
    bot.add_cog(Search(bot))
