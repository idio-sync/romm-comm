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
from urllib.parse import quote

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

class ROM_View(discord.ui.View):
    def __init__(self, bot, search_results: List[Dict], author_id: int, platform_name: Optional[str] = None, initial_message: Optional[discord.Message] = None):
        super().__init__()
        self.bot = bot
        self.search_results = search_results
        self.author_id = author_id
        self.platform_name = platform_name
        self.message = initial_message
        self._selected_rom = None

        # Create ROM select menu only
        self.select = discord.ui.Select(
            placeholder="Select result to view details",
            custom_id="rom_select"
        )
        
        # Add options to select menu
        for rom in search_results[:25]:
            display_name = rom['name'][:75] if len(rom['name']) > 75 else rom['name']
            file_name = rom.get('fs_name', 'Unknown filename')
            
            # Get correct size for dropdown
            size_bytes = rom.get('fs_size_bytes', 0)  # Check ROM level first
            if not size_bytes and rom.get('files'):
                # For multi-file ROMs, sum the sizes
                size_bytes = sum(f.get('file_size_bytes', 0) for f in rom['files'])
            file_size = self.format_file_size(size_bytes)
            
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
        try:
            # When creating the download URL in the embed
            raw_file_name = rom_data.get('fs_name', 'unknown_file')
            # Use plus signs for spaces - these survive Discord->Browser->Nginx
            file_name = raw_file_name.replace(' ', '+')
            file_name = quote(file_name, safe='+')
            download_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_data['id']}/content/{file_name}"
            
            logger.debug(f"Embed download URL - raw: '{raw_file_name}'")
            logger.debug(f"Embed download URL - encoded: '{file_name}'")
            logger.debug(f"Embed download URL - final: {download_url}")
            igdb_name = rom_data['name'].lower().replace(' ', '-')
            igdb_name = re.sub(r'[^a-z0-9-]', '', igdb_name)
            igdb_url = f"https://www.igdb.com/games/{igdb_name}"
            romm_url = f"{self.bot.config.DOMAIN}/rom/{rom_data['id']}"
            logo_url = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png"
            
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
                search_cog = self.bot.get_cog('Search')
                if search_cog:
                    platform_display = search_cog.get_platform_with_emoji(platform_name)
                else:
                    platform_display = platform_name
                embed.add_field(name="Platform", value=platform_display, inline=True)
            
            # Add other metadata fields
            if metadatum := rom_data.get('metadatum'):
                if genres := metadatum.get('genres'):
                    if isinstance(genres, list):
                        genre_list = genres[:2]  # Take only first two genres
                        genre_display = ", ".join(genre_list)
                    else:
                        genre_display = str(genres)
                    embed.add_field(name="Genres", value=genre_display, inline=True)
            
            if metadatum := rom_data.get('metadatum'):
                if release_date := metadatum.get('first_release_date'):
                    try:
                        release_datetime = datetime.fromtimestamp(int(release_date))
                        formatted_date = release_datetime.strftime('%b %d, %Y')
                        embed.add_field(name="Release Date", value=formatted_date, inline=True)
                    except (ValueError, TypeError) as e:
                        logger.error(f"Error formatting date: {e}")
            
            if summary := rom_data.get('summary'):
                trimmed_summary = self.trim_summary_to_lines(summary, max_lines=3)
                if trimmed_summary:
                    embed.add_field(name="Summary", value=trimmed_summary, inline=False)
            
            if companies := metadatum.get('companies'):
                if isinstance(companies, list):
                    company_list = companies[:2]  # Take only first two companies
                    companies_str = ", ".join(company_list)
                else:
                    companies_str = str(companies)
                if companies_str:
                    embed.add_field(name="Companies", value=companies_str, inline=True)
            
            links = [
                f"[Romm]({romm_url})",
                f"[IGDB]({igdb_url})"
            ]
            embed.add_field(name="Links", value=" • ".join(links), inline=True)
            
            # File information
            if rom_data.get('multi') and rom_data.get('files'):
                files = rom_data.get('files', [])
                total_size = sum(f.get('file_size_bytes', 0) for f in files)
                files_info = []
                total_length = 0
                files_shown = 0
                total_files = len(files)
                max_length = 800  # Leave buffer for Discord's limit

                def would_exceed_limit(current_text: str, new_line: str) -> bool:
                    """Check if adding a new line would exceed Discord's limit"""
                    potential_total = len('\n'.join(current_text + [new_line]))
                    return potential_total > max_length
                
                # Sort files based on count
                if len(files) > 10:
                    sorted_files = sorted(
                        files, 
                        key=lambda x: x.get('file_size_bytes', 0), 
                        reverse=True
                    )[:10]
                else:
                    sorted_files = sorted(
                        files,
                        key=lambda x: x.get('file_name', '').lower()
                    )

                # Process each file
                for file_info in sorted_files:
                    # Get file size
                    size_bytes = file_info.get('file_size_bytes', 0)
                    size_str = self.format_file_size(size_bytes)

                    # Create file line
                    file_line = f"• {file_info['file_name']} ({size_str})"
                    line_length = len(file_line) + 1  # +1 for newline
                    
                    if total_length + line_length > max_length:
                        files_info.append("...")
                        break
                    
                    # Add file line
                    files_info.append(file_line)
                    total_length += line_length
                    files_shown += 1  # Increment counter when file is actually added
                 
                    # Get hash information
                    # hashes = []
                    # if crc := file_info.get('crc_hash'):
                    #    hashes.append(f"CRC: {crc}")
                    # if md5 := file_info.get('md5_hash'):
                    #    hashes.append(f"MD5: {md5}")
                    # if sha1 := file_info.get('sha1_hash'):
                    #    hashes.append(f"SHA1: {sha1}")

                    # Add hash line if we have hashes and room
                    # if hashes:
                    #    hash_line = "  " + " | ".join(hashes)
                    #    if not would_exceed_limit(files_info, hash_line):
                    #        files_info.append(hash_line)

                    
                # Create field name
                field_name = f"Files (Total: {self.format_file_size(total_size)}"
                if len(files) > files_shown:
                    field_name += f" - Showing {files_shown} of {(total_files)} files)"
                else:
                    field_name += ")"

                 # Verify final length
                final_text = "\n".join(files_info)
                if len(final_text) > 1024:
                    # If somehow still too long, truncate and show fewer files
                    files_info = files_info[:len(files_info)//2]
                    if files_info[-1] != "...":
                        files_info.append("...")
                    final_text = "\n".join(files_info)
                    files_shown = sum(1 for line in files_info if line.startswith("•"))
                    
                    # Update field name with new count
                    field_name = f"Files (Total: {self.format_file_size(total_size)}"
                    if len(files) > files_shown:
                        field_name += f" - Showing {files_shown} of {len(files)} files)"
                    else:
                        field_name += ")"
                
                # Add field to embed
                embed.add_field(
                    name=field_name,
                    value="\n".join(files_info),
                    inline=False
                )
            else:
                # Single file display
                file_size = self.format_file_size(rom_data.get('fs_size_bytes', 0))
                file_name = rom_data.get('fs_name' , 'unknown_file')
                
                # Add filename and hash values to embed
                file_info = [f"• {file_name}"]
                
                embed.add_field(
                    name=f"File ({file_size})",
                    value="\n".join(file_info),
                    inline=False
                )
                
                # Hash values for single file
                # hashes = []
                # if crc := rom_data.get('crc_hash'):
                #    hashes.append(f"**CRC:** {crc}")
                # if md5 := rom_data.get('md5_hash'):
                #    hashes.append(f"**MD5:** {md5}")
                # if sha1 := rom_data.get('sha1_hash'):
                #    hashes.append(f"**SHA1:** {sha1}")

                # if hashes:
                #    embed.add_field(name="Hash Values", value=" | ".join(hashes), inline=False)
                
            return embed
        except Exception as e:
            logger.error(f"Error creating ROM embed: {e}")
            raise

    def trim_summary_to_lines(self, summary: str, max_lines: int = 3, chars_per_line: int = 60) -> str:
        """Trim summary text to specified number of lines"""
        if not summary:
            return ""
            
        # Split existing newlines first
        lines = summary.split('\n')
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            summary = '\n'.join(lines)
            if len(lines) == max_lines:
                summary += "..."
            return summary
            
        # If we have fewer physical lines, check length-based wrapping
        current_line = 1
        current_length = 0
        result = []
        words = summary.replace('\n', ' ').split(' ')
        
        for word in words:
            # Check if adding this word would start a new line
            if current_length + len(word) + 1 > chars_per_line:
                current_line += 1
                current_length = len(word)
                # If this would exceed our max lines, stop here
                if current_line > max_lines:
                    result.append('...')
                    break
                result.append(word)
            else:
                current_length += len(word) + 1
                result.append(word)
                
            if current_line > max_lines:
                break
                
        return ' '.join(result)
    
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

    async def update_file_select(self, rom_data: Dict):
        """Update the file selection menu with available files"""
        try:
            # Remove existing file components if they exist
            components_to_remove = []
            for item in self.children:
                if isinstance(item, (discord.ui.Button, discord.ui.Select)) and item != self.select:
                    components_to_remove.append(item)
            
            for item in components_to_remove:
                self.remove_item(item)

            # Initialize filename and ID maps as instance variables
            self.filename_map = {}
            self.file_id_map = {}

            if rom_data.get('multi') and rom_data.get('files'):
                files = rom_data.get('files', [])
                if not files:
                    return
                
                # Sort files based on count
                if len(files) > 10:
                    sorted_files = sorted(
                        files, 
                        key=lambda x: x.get('file_size_bytes', 0),
                        reverse=True
                    )[:10]
                else:
                    sorted_files = sorted(
                        files,
                        key=lambda x: x.get('file_name', '').lower()
                    )
                
                # Create file select with appropriate max_values
                self.file_select = discord.ui.Select(
                    placeholder="Select files to download",
                    custom_id="file_select",
                    min_values=1,
                    max_values=min(len(sorted_files), 25)
                )
                
                # Add file options using shortened values
                for i, file_info in enumerate(sorted_files):
                    # Get size from file info
                    size_bytes = file_info.get('file_size_bytes', 0)
                    size = self.format_file_size(size_bytes)
                    
                    # Create shortened value and map it to full filename AND file ID
                    short_value = f"file_{i}"
                    self.filename_map[short_value] = file_info['file_name']
                    self.file_id_map[short_value] = str(file_info.get('id'))  # Store as string
                    
                    self.file_select.add_option(
                        label=file_info['file_name'][:75],
                        value=short_value,
                        description=f"Size: {size}"
                    )
                
                self.file_select.callback = self.file_select_callback
                self.add_item(self.file_select)

                # Create proper base URL for multi-file downloads
                file_name = quote(rom_data.get('fs_name', 'unknown_file'))
                base_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_data['id']}/content/{file_name}"

                # Download Selected button starts disabled
                self.download_selected = discord.ui.Button(
                    label="Download Selected",
                    style=discord.ButtonStyle.link,
                    url=base_url,  # Will be updated when files are selected
                    disabled=True
                )
                self.add_item(self.download_selected)

                # Download All button - no file_ids parameter means all files
                self.download_all = discord.ui.Button(
                    label="Download All",
                    style=discord.ButtonStyle.link,
                    url=base_url  # No file_ids parameter = download all
                )
                self.add_item(self.download_all)

            else:
                # Single file download button
                file_name = quote(rom_data.get('fs_name', 'unknown_file'))
                file_size = self.format_file_size(rom_data.get('fs_size_bytes', 0))
                download_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_data['id']}/content/{file_name}"
                
                self.download_all = discord.ui.Button(
                    label=f"Download ({file_size})",
                    style=discord.ButtonStyle.link,
                    url=download_url
                )
                self.add_item(self.download_all)

        except Exception as e:
            logger.error(f"Error updating file select: {e}")
            logger.error(f"ROM data: {rom_data}")
            raise

    async def file_select_callback(self, interaction: discord.Interaction):
        """Handle file selection"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This selection isn't for you!", ephemeral=True)
            return

        try:
            selected_short_values = interaction.data['values']
            logger.debug(f"Selected short values: {selected_short_values}")
            logger.debug(f"File ID map: {self.file_id_map}")
            
            if selected_short_values and hasattr(self, 'file_id_map'):
                # Get the file IDs for selected files
                selected_file_ids = [self.file_id_map[short_value] for short_value in selected_short_values]
                
                # Create the download URL with file_ids parameter
                file_name = quote(self._selected_rom.get('fs_name', 'unknown_file'))
                base_url = f"{self.bot.config.DOMAIN}/api/roms/{self._selected_rom['id']}/content/{file_name}"
                
                if selected_file_ids:
                    # Use file_ids parameter as expected by backend
                    file_ids_param = ','.join(selected_file_ids)
                    download_url = f"{base_url}?file_ids={file_ids_param}"
                else:
                    download_url = base_url
                
                logger.debug(f"Generated download URL: {download_url}")
                
                # Remove old download buttons
                for item in self.children[:]:
                    if isinstance(item, discord.ui.Button):
                        self.remove_item(item)
                
                # Add new download selected button with updated URL
                self.download_selected = discord.ui.Button(
                    label="Download Selected",
                    style=discord.ButtonStyle.link,
                    url=download_url,
                    disabled=False
                )
                self.add_item(self.download_selected)

                # Re-add download all button (no file_ids = all files)
                all_raw_filename = self._selected_rom.get('fs_name', 'unknown_file')
                all_file_name = quote(all_raw_filename, safe='')
                download_all_url = f"{self.bot.config.DOMAIN}/api/roms/{self._selected_rom['id']}/content/{all_file_name}"
                self.download_all = discord.ui.Button(
                    label="Download All",
                    style=discord.ButtonStyle.link,
                    url=download_all_url
                )
                self.add_item(self.download_all)
                
                await interaction.response.edit_message(view=self)
            else:
                # Disable download selected button if no files selected
                for item in self.children[:]:
                    if isinstance(item, discord.ui.Button) and item.label == "Download Selected":
                        item.disabled = True
                await interaction.response.edit_message(view=self)
                
        except Exception as e:
            logger.error(f"Error in file select callback: {e}")
            logger.error(f"Selected values: {selected_short_values if 'selected_short_values' in locals() else 'undefined'}")
            logger.error(f"ROM data: {self._selected_rom if hasattr(self, '_selected_rom') else 'No ROM selected'}")
            try:
                await interaction.response.defer(ephemeral=True)
                await interaction.followup.send("An error occurred while processing your selection", ephemeral=True)
            except discord.errors.InteractionResponded:
                await interaction.followup.send("An error occurred while processing your selection", ephemeral=True)

    async def download_selected_callback(self, interaction: discord.Interaction):
        """Handle downloading selected files"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This button isn't for you!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        
        if not self._selected_rom:
            await interaction.followup.send("Please select a ROM first!", ephemeral=True)
            return

        # Get selected values from the select menu
        selected_values = []
        for child in self.children:
            if isinstance(child, discord.ui.Select) and child.custom_id == "file_select":
                selected_values = child.values
                break

        if not selected_values:
            await interaction.followup.send("Please select files to download!", ephemeral=True)
            return
        
        file_name = self._selected_rom.get('fs_name', 'unknown_file').replace(' ', '%20')
        download_url = f"{self.bot.config.DOMAIN}/roms/{self._selected_rom['id']}/content/{file_name}?files={','.join(selected_values)}"
        
        await interaction.followup.send(
            f"Download link for selected files:\n{download_url}",
            ephemeral=True
        )

    def get_download_url(self, rom_id: int, file_name: str, selected_files: Optional[List[str]] = None) -> str:
        """Helper method to generate properly encoded download URLs"""
        try:
            base_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_id}/content/{quote(file_name)}"
            
            if selected_files:
                # Double encode: first each filename, then the entire parameter
                encoded_files = [quote(f) for f in selected_files]
                files_param = quote(','.join(encoded_files))
                return f"{base_url}?files={files_param}"
            
            return base_url
            
        except Exception as e:
            logger.error(f"Error generating download URL: {e}")
            return ""
    
    async def download_all_callback(self, interaction: discord.Interaction):
        """Handle downloading all files"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This button isn't for you!", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        
        if not self._selected_rom:
            await interaction.followup.send("Please select a ROM first!", ephemeral=True)
            return

        file_name = self._selected_rom.get('fs_name', 'unknown_file').replace(' ', '%20')
        download_url = f"{self.bot.config.DOMAIN}/api/roms/{self._selected_rom['id']}/content/{file_name}"
        
        await interaction.followup.send(
            f"Download link for all files:\n{download_url}",
            ephemeral=True
        )

    async def handle_qr_trigger(self, interaction: discord.Interaction, trigger_type: str):
        """Handle QR code generation and sending"""
        try:
            # For search results with multiple ROMs
            if len(self.search_results) > 1 and not self._selected_rom:
                await interaction.channel.send("Please select a ROM first!")
                return

            # Get the ROM data
            selected_rom = self._selected_rom if self._selected_rom else self.search_results[0]
            
            if not selected_rom:
                await interaction.channel.send("❌ Unable to find ROM data")
                return

            file_name = selected_rom.get('fs_name', 'unknown_file').replace(' ', '%20')
            download_url = f"{self.bot.config.DOMAIN}/api/roms/{selected_rom['id']}/content/{file_name}"
                
            qr_file = await self.generate_qr(download_url)
            if qr_file:
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
            await interaction.channel.send("❌ An error occurred while generating the QR code")

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

    def message_check(self, m):
        """Check if a message is a valid QR trigger"""
        if not m.reference or not hasattr(m.reference, 'cached_message'):
            return False
            
        referenced_message = m.reference.cached_message
        if not referenced_message:
            return False
            
        return (
            any(keyword in m.content.lower() for keyword in {'qr'}) and
            referenced_message.author.id == self.bot.user.id and
            referenced_message.embeds and
            self.message.embeds and
            referenced_message.embeds[0].title == self.message.embeds[0].title
        )
        
    def reaction_check(self, reaction, user):
        """Check if a reaction is a valid QR trigger"""
        valid_emojis = {'qr_code', '📱', 'qr'}
        return (
            user.id == self.author_id and
            reaction.message.embeds and
            self.message.embeds and
            reaction.message.embeds[0].title == self.message.embeds[0].title and
            (getattr(reaction.emoji, 'name', str(reaction.emoji)).lower() in valid_emojis)
        )    
    
    
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
                
                self._selected_rom = selected_rom
                embed = await self.create_rom_embed(selected_rom)
                
                # Remove all file-related components first
                components_to_remove = []
                for item in self.children[:]:
                    if isinstance(item, (discord.ui.Button, discord.ui.Select)) and item != self.select:
                        components_to_remove.append(item)
                
                for item in components_to_remove:
                    self.remove_item(item)
                
                # Update file components for both single and multi-file ROMs
                await self.update_file_select(selected_rom)
                
                edited_message = await interaction.message.edit(
                    content=interaction.message.content,
                    embed=embed,
                    view=self
                )
                
                self.message = edited_message
                await self.watch_for_qr_triggers(interaction)
            else:
                await interaction.followup.send("❌ Error retrieving ROM details", ephemeral=True)
        except Exception as e:
            logger.error(f"Error in select callback: {e}")
            await interaction.followup.send("❌ An error occurred while processing your selection", ephemeral=True)
                
class Search(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.platform_emoji_names = {}  # Will be populated from API data
        # Map of common platform name variations
        self.platform_variants = {
            '3DO Interactive Multiplayer': ['3do'],
            'Apple II': ['apple_ii'],
            'Amiga CD32': ['cd32'],
            'Amstrad CPC': ['amstrad'],
            'Apple Pippin':['pippin'],
            'Atari 2600': ['2600'],
            'Atari 5200': ['5200'],
            'Atari 7800': ['7800'],
            'Atari Jaguar': ['jaguar'],
            'Atari Lynx': ['lynx'],
            'Commodore C64/128/MAX': ['c64'],
            'Dreamcast': ['dreamcast'],
            'Family Computer Disk System': ['fds'],
            'FM Towns': ['fm_towns'],
            'Game & Watch':['game_and_watch'],
            'Game Boy': ['gameboy', 'gameboy_pocket'],
            'Game Boy Advance': ['gameboy_advance', 'gameboy_advance_sp', 'gameboy_micro'],
            'Game Boy Color': ['gameboy_color'],
            'J2ME': ['cell_java'],
            'Mac': ['mac', 'mac_imac'],
            'MSX': ['msx'],
            'N-Gage': ['n_gage'],
            'Neo Geo AES': ['neogeo_aes'],
            'Neo Geo CD': ['neogeo_cd'],
            'Neo Geo Pocket':['neogeo_pocket'],
            'Neo Geo Pocket Color': ['neogeo_pocket_color'],
            'Nintendo 3DS': ['3ds'],
            'Nintendo 64': ['n64'],
            'Nintendo 64Dd': ['n64_dd'],
            'Nintendo DS': ['ds', 'ds_lite'],
            'Nintendo DSi': ['dsi'],
            'Nintendo Entertainment System': ['nes'],
            'Nintendo GameCube': ['gamecube'],
            'Nintendo Switch': ['switch', 'switch_docked'],
            'PC-8800 Series': ['pc_88'],
            'PC-9800 Series': ['pc_98'],
            'PC (Microsoft Windows)': ['pc'],
            'Philips CD-i': ['cd_i'],
            'PlayStation': ['ps', 'ps_one'],
            'PlayStation 2': ['ps2', 'ps2_slim'],
            'PlayStation 3': ['ps3', 'ps3_slim'],
            'PlayStation 4': ['ps4'],
            'PlayStation Portable': ['psp', 'psp_go'],
            'PlayStation Vita': ['vita'],
            'Pokémon mini': ['pokemon_mini'],
            'Sega 32X': ['32x'],
            'Sega CD': ['sega_cd'],
            'Sega Game Gear': ['game_gear'],
            'Sega Master System/Mark III': ['master_system'],
            'Sega Mega Drive/Genesis': ['genesis', 'genesis_2', 'nomad'],
            'Sega Saturn': ['saturn_2'],
            'Sharp X68000': ['x68000'],
            'Sinclair Zxs': ['zx_spectrum'],
            'Super Nintendo Entertainment System': ['snes'],
            'Switch': ['switch', 'switch_docked'],
            'Turbografx-16/PC Engine CD': ['tg_16_cd'],
            'TurboGrafx-16/PC Engine': ['tg_16', 'turboduo', 'turboexpress'],
            'Vectrex': ['vectrex'],
            'Virtual Boy': ['virtual_boy'],
            'Visual Memory Unit / Visual Memory System': ['vmu'],
            'Wii': ['wii'],
            'Win3X': ['win_3x_gui'],
            'Windows': ['pc', 'win_9x'],
            'WonderSwan': ['wonderswan'],
            'Xbox': ['xbox_og'],
            'Xbox 360': ['xbox_360'],
        }
        bot.loop.create_task(self.initialize_platform_emoji_mappings())
    
    async def initialize_platform_emoji_mappings(self):
        """Initialize platform -> emoji mappings using API data"""
        await self.bot.wait_until_ready()
        
        try:
            # Get platforms from API
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                print("Warning: Could not fetch platforms for emoji mapping")
                return
                
            sanitized_platforms = self.bot.sanitize_data(raw_platforms, data_type='platforms')
            mapped_count = 0
            #print("\nPlatform Emoji Mappings:")
            
            for platform in sanitized_platforms:
                if 'name' in platform:
                    platform_name = platform['name']
                    variants = self.platform_variants.get(platform_name, [])
                    
                    # Try each variant
                    mapped = False
                    for variant in variants:
                        if variant in self.bot.emoji_dict:
                            self.platform_emoji_names[platform_name] = variant
                            #print(f"{platform_name} -> {variant}")
                            mapped = True
                            mapped_count += 1
                            break
                    
                    # If no variant worked, try simple name
                    if not mapped:
                        simple_name = platform_name.lower().replace(' ', '_').replace('-', '_')
                        if simple_name in self.bot.emoji_dict:
                            self.platform_emoji_names[platform_name] = simple_name
                            #print(f"{platform_name} -> {simple_name}")
                            mapped_count += 1
            
            print(f"Successfully mapped {mapped_count} platform(s) to custom emoji(s)")
            
            # Print unmapped platforms
            unmapped = [p['name'] for p in sanitized_platforms if p['name'] not in self.platform_emoji_names]
            if unmapped:
                print("\nUnmapped platforms:")
                for name in sorted(unmapped):
                    print(f"- {name}")
            
        except Exception as e:
            print(f"Error initializing platform emoji mappings: {e}")
    
    def get_platform_with_emoji(self, platform_name: str) -> str:
        """Returns platform name with its emoji if available."""
        if not platform_name or not hasattr(self.bot, 'emoji_dict'):
            return platform_name

        # Get the variant names - platform_variants returns a list
        variant_names = self.platform_variants.get(platform_name, [platform_name.lower().replace(' ', '_').replace('-', '_')])

        # If variant_names is a list (which it should be), use it directly
        # If somehow it's not a list, wrap it in a list
        variants_to_check = variant_names if isinstance(variant_names, list) else [variant_names]

        # Try to find a matching custom emoji
        for variant in variants_to_check:
            if variant in self.bot.emoji_dict:
                return f"{platform_name} {self.bot.emoji_dict[variant]}"

        # If no custom emoji found, use the fallback
        return f"{platform_name} 🎮"
        

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
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Re-initialize emoji mappings when bot reconnects"""
        await self.initialize_platform_emoji_mappings()
    
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
                platforms_list = "\n".join(
                    f"• {self.get_platform_with_emoji(p.get('name', 'Unknown'))}" 
                    for p in raw_platforms
                )
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
                title=f"Firmware Files for {self.get_platform_with_emoji(platform_data.get('name', platform))}",
                description=f"Found {len(firmware_data)} firmware file(s) {self.bot.emoji_dict['bios']}",
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
                    platforms_list = "\n".join(
                        f"• {self.get_platform_with_emoji(p['name'])}" 
                        for p in sorted(sanitized_platforms, key=lambda x: x['name'])
                    )
                    await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                    return

                if rom_count <= 0:
                    await ctx.respond(f"❌ No ROMs found for platform '{self.get_platform_with_emoji(platform_name)}'")
                    return

                # Try up to 5 times to find a valid ROM for the specific platform
                max_attempts = 5
                for attempt in range(max_attempts):
                    try:
                        # Get ROMs for platform
                        roms_response = await self.bot.fetch_api_endpoint(
                            f'roms?platform_id={platform_id}&limit={rom_count}'
                        )

                        # Handle paginated response
                        if roms_response and isinstance(roms_response, dict) and 'items' in roms_response:
                            all_roms = roms_response['items']
                        elif roms_response and isinstance(roms_response, list):
                            all_roms = roms_response
                        else:
                            all_roms = []
                        
                        if all_roms and isinstance(all_roms, list) and len(all_roms) > 0:
                            # Select a random ROM from the list
                            rom_data = random.choice(all_roms)
                            
                            # Get full ROM data
                            detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom_data["id"]}')
                            if detailed_rom:
                                rom_data = detailed_rom
                            
                            # Create view with explicit ROM data
                            view = ROM_View(self.bot, [rom_data], ctx.author.id, platform_name)
                            view.remove_item(view.select)
                            view._selected_rom = rom_data
                            embed = await view.create_rom_embed(rom_data)
                            await view.update_file_select(rom_data)

                            initial_message = await ctx.respond(
                                f"🎲 Found a random ROM from {self.get_platform_with_emoji(platform_name)}:",
                                embed=embed,
                                view=view
                            )

                            if isinstance(initial_message, discord.Interaction):
                                initial_message = await initial_message.original_response()
                            
                            view.message = initial_message
                            self.bot.loop.create_task(view.watch_for_qr_triggers(ctx.interaction))
                            return

                    except Exception as e:
                        logger.error(f"Error in attempt {attempt + 1}: {e}")
                    
                    logger.info(f"Random ROM attempt {attempt + 1} for platform {platform_name} failed")
                    await asyncio.sleep(1)

            else:
                # Random from full collection
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
                        # Get platform name if available
                        platform_name = None
                        if platform_id := rom_data.get('platform_id'):
                            platforms_data = self.bot.cache.get('platforms')
                            if platforms_data:
                                for p in platforms_data:
                                    if p.get('id') == platform_id:
                                        platform_name = p.get('name')
                                        break

                        # Create view with explicit ROM data
                        view = ROM_View(self.bot, [rom_data], ctx.author.id, platform_name)
                        view.remove_item(view.select)
                        view._selected_rom = rom_data
                        embed = await view.create_rom_embed(rom_data)
                        await view.update_file_select(rom_data)

                        initial_message = await ctx.respond(
                            f"🎲 Found a random ROM" + (f" from {self.get_platform_with_emoji(platform_name)}" if platform_name else "") + ":",
                            embed=embed,
                            view=view
                        )

                        if isinstance(initial_message, discord.Interaction):
                            initial_message = await initial_message.original_response()
                        
                        view.message = initial_message
                        self.bot.loop.create_task(view.watch_for_qr_triggers(ctx.interaction))
                        return

                    logger.info(f"Random ROM attempt {attempt + 1} with ID {random_rom_id} failed")
                    await asyncio.sleep(1)

            # If all attempts failed
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
                platforms_list = "\n".join(
                    f"• {self.get_platform_with_emoji(p['name'])}" 
                    for p in sorted(sanitized_platforms, key=lambda x: x['name'])
                )
                await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            # Search for ROMs
            search_term = game.strip()
            search_response = await self.bot.fetch_api_endpoint(
                f'roms?platform_id={platform_id}&search_term={search_term}&limit=25'
            )

            # Handle paginated response
            if search_response and isinstance(search_response, dict) and 'items' in search_response:
                search_results = search_response['items']
            elif search_response and isinstance(search_response, list):
                search_results = search_response
            else:
                search_results = []

            # If no results, try with modified search term
            if not search_results or len(search_results) == 0:
                search_words = search_term.split()  # Define search_words here
                if len(search_words) > 1:
                    search_term = ' '.join(search_words)
                    search_response = await self.bot.fetch_api_endpoint(
                        f'roms?platform_id={platform_id}&search_term={search_term}&limit=25'
                    )
                    
                    # Handle paginated response for retry
                    if search_response and isinstance(search_response, dict) and 'items' in search_response:
                        search_results = search_response['items']
                    elif search_response and isinstance(search_response, list):
                        search_results = search_response
                    else:
                        search_results = []

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
                    f"Found 25+ ROMs matching '{game}' for platform '{self.get_platform_with_emoji(platform_name)}'. "
                    f"Showing first 25 results.\nPlease refine your search terms for more specific results:"
                )
            else:
                initial_content = f"Found {len(search_results)} ROMs matching '{game}' for platform '{self.get_platform_with_emoji(platform_name)}':"

            # Create view first
            view = ROM_View(self.bot, search_results, ctx.author.id, platform_name)
            
            # Send message exactly like random command
            initial_message = await ctx.respond(
                initial_content,
                view=view
            )
            
            # Handle interaction response exactly like random command
            if isinstance(initial_message, discord.Interaction):
                initial_message = await initial_message.original_response()
            
            # Store message reference
            view.message = initial_message
            
        except Exception as e:
            logger.error(f"Error in search command: {e}", exc_info=True)
            await ctx.respond("❌ An error occurred while searching for ROMs")

def setup(bot):
    bot.add_cog(Search(bot))
