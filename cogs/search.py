from discord.ext import commands
import discord
import logging
import re
from datetime import datetime
from typing import Dict, List, Optional, Union, Tuple
import random
import qrcode
from PIL import Image
import io
import aiohttp
from io import BytesIO
import asyncio
import time
from urllib.parse import quote
from collections import defaultdict

# Set up logging
import logging
logger = logging.getLogger(__name__)

class ROM_View(discord.ui.View):
    def __init__(self, bot, search_results: List[Dict], author_id: int, platform_name: Optional[str] = None, initial_message: Optional[discord.Message] = None):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
        self.search_results = search_results
        self.author_id = author_id
        self.platform_name = platform_name
        self.message = initial_message
        self._selected_rom = None
        self.emoji_dict = {}

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
            
            # Check if files are in subfolders for the description
            subfolder_types = set()
            if rom.get('files'):
                for f in rom['files']:
                    subfolder = self.get_file_subfolder(f)
                    if subfolder:
                        subfolder_types.add(subfolder)
            
            # Build description
            if subfolder_types:
                subfolder_text = f"[{', '.join(sorted(subfolder_types))}] "
                truncated_filename = (file_name[:40] + '...') if len(file_name) > 43 else file_name
                description = f"{truncated_filename} + {subfolder_text} ({file_size})"
            else:
                truncated_filename = (file_name[:47] + '...') if len(file_name) > 50 else file_name
                description = f"{truncated_filename} ({file_size})"
            
            # Ensure description doesn't exceed Discord's limit (100 chars)
            if len(description) > 100:
                description = description[:97] + "..."
            
            self.select.add_option(
                label=display_name,
                value=str(rom['id']),
                description=description
            )

        self.select.callback = self.select_callback
        self.add_item(self.select)

    async def on_timeout(self):
        """Disable all components when the view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass  # Message was deleted or can't be edited

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
        
    @staticmethod
    def get_file_subfolder(file_info: Dict) -> Optional[str]:
        # First prefer backend category field
        if file_info.get('category'):
            return file_info['category'].lower()
        
        # Fallback to path parsing if no category
        file_path = file_info.get('file_path', '')
        if not file_path:
            return None

        known_subfolders = ['hack', 'dlc', 'manual', 'mod', 'patch', 'update', 'demo', 'translation', 'prototype']
        path_parts = file_path.split('/')
        for part in path_parts:  # include all parts
            if part.lower() in known_subfolders:
                return part.lower()
        return None
    
    @staticmethod
    def get_subfolder_icon(subfolder: Optional[str]) -> str:
        """Get icon for subfolder type"""
        icons = {
            'hack': '🔧',
            'dlc':'⬇️',
            'manual': '📖',
            'mod': '🎨',
            'patch': '📝',
            'update': '🔄',
            'demo': '🎮',
            'translation': '🌐',
            'prototype': '🔬',
            None: '📄'  # Default for main/root files
        }
        return icons.get(subfolder, '📁')

    async def download_cover_image(self, rom_data: Dict) -> Optional[discord.File]:
        """Download cover image from Romm API and return as Discord File"""
        # Maximum image constraints to prevent DoS via oversized images
        MAX_IMAGE_DIMENSION = 4096  # Max width or height
        MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB max file size

        try:
            # Check if we have url_cover at all
            if not rom_data.get('url_cover'):
                return None

            # Build the direct cover URL from Romm API
            platform_id = rom_data.get('platform_id')
            rom_id = rom_data.get('id')

            if not platform_id or not rom_id:
                logger.warning("Missing platform_id or rom_id for cover download")
                return None

            # Construct the direct cover URL
            cover_url = f"{self.bot.config.API_BASE_URL}/assets/romm/resources/roms/{platform_id}/{rom_id}/cover/big.png"

            logger.debug(f"Downloading cover from: {cover_url}")

            # Use bot's shared session if available, otherwise create one
            session = getattr(self.bot, 'session', None)
            close_session = False
            if not session or session.closed:
                session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                close_session = True

            try:
                async with session.get(cover_url) as response:
                    if response.status == 200:
                        # Check content-length header first if available
                        content_length = response.headers.get('content-length')
                        if content_length and int(content_length) > MAX_IMAGE_BYTES:
                            logger.warning(f"Cover image for ROM {rom_id} too large ({content_length} bytes), skipping")
                            return None

                        image_data = await response.read()

                        # Validate downloaded size
                        if len(image_data) > MAX_IMAGE_BYTES:
                            logger.warning(f"Cover image for ROM {rom_id} too large ({len(image_data)} bytes), skipping")
                            return None

                        # Validate image dimensions before returning
                        try:
                            img = Image.open(BytesIO(image_data))
                            if img.width > MAX_IMAGE_DIMENSION or img.height > MAX_IMAGE_DIMENSION:
                                logger.warning(f"Cover image for ROM {rom_id} dimensions too large ({img.width}x{img.height}), skipping")
                                img.close()
                                return None
                            img.close()
                        except Exception as e:
                            logger.warning(f"Could not validate image dimensions for ROM {rom_id}: {e}")
                            # Continue anyway - PIL will fail later if image is truly invalid

                        byte_arr = BytesIO(image_data)
                        byte_arr.seek(0)
                        return discord.File(byte_arr, filename="cover.png")
                    else:
                        logger.warning(f"Failed to download cover: HTTP {response.status}")
                        return None
            finally:
                if close_session:
                    await session.close()

        except Exception as e:
            logger.error(f"Error downloading cover image: {e}")
            return None
    
    async def create_rom_embed(self, rom_data: Dict) -> Tuple[discord.Embed, Optional[discord.File]]:
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
            
            # Download cover image if available
            cover_file = None
            if rom_data.get('url_cover'):
                cover_file = await self.download_cover_image(rom_data)
                if cover_file:
                    # Set the image to use the attachment
                    embed.set_image(url="attachment://cover.png")
            
            # Get platform name if not provided
            platform_name = self.platform_name
            if not platform_name and (platform_id := rom_data.get('platform_id')):
                # Get raw platforms data to access custom_name
                raw_platforms_data = await self.bot.fetch_api_endpoint('platforms')
                if raw_platforms_data:
                    for p in raw_platforms_data:
                        if p.get('id') == platform_id:
                            platform_name = self.bot.get_platform_display_name(p)
                            break
                else:
                    # Fallback to cached platforms data
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
            
            # Rest of the embed creation remains the same...
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
                        # Check if timestamp is in milliseconds (if it's too large)
                        # A reasonable date should be less than 2,000,000,000 (year 2033)
                        if release_date > 2_000_000_000:
                            # Convert milliseconds to seconds
                            release_date = release_date / 1000
                        
                        release_datetime = datetime.fromtimestamp(int(release_date))
                        formatted_date = release_datetime.strftime('%b %d, %Y')
                        embed.add_field(name="Release Date", value=formatted_date, inline=True)
                    except (ValueError, TypeError) as e:
                        logger.error(f"Error formatting date: {e}")
                        logger.error(f"Raw release_date value: {release_date}")
            
            if summary := rom_data.get('summary'):
                trimmed_summary = self.trim_summary_to_lines(summary, max_lines=3)
                if trimmed_summary:
                    # Enforce Discord's 1024 character limit
                    if len(trimmed_summary) > 1024:
                        trimmed_summary = trimmed_summary[:1021] + "..."
                    embed.add_field(name="Summary", value=trimmed_summary, inline=False)
            
            if companies := metadatum.get('companies'):
                if isinstance(companies, list):
                    company_list = companies[:2]  # Take only first two companies
                    companies_str = ", ".join(company_list)
                else:
                    companies_str = str(companies)

                # Truncate to Discord's 1024 character limit
                if len(companies_str) > 1024:
                    companies_str = companies_str[:1021] + "..."
                
                embed.add_field(name="Companies", value=companies_str, inline=True)
            
            # Check if this is a PC platform and get PCGamingWiki link
            pcgw_url = None
            if platform_name and self.is_pc_platform(platform_name):
                # Get IGDB ID directly from ROM data
                igdb_id = rom_data.get('igdb_id')
                
                if igdb_id:
                    game_name = rom_data.get('name', '')
                    pcgw_url = await self.get_pcgamingwiki_url(igdb_id, game_name)
            
            # Build the links section with two rows
            romm_emoji = self.bot.get_formatted_emoji('romm')
            igdb_emoji = self.bot.get_formatted_emoji('igdb')
            launchbox_emoji = self.bot.get_formatted_emoji('launchbox')
            hash_emoji = self.bot.get_formatted_emoji('hash')

            # Top row links
            top_row_links = [
                f"[**{romm_emoji} RomM**]({romm_url})",
                f"[**{igdb_emoji} IGDB**]({igdb_url})"
            ]

            # Add YouTube to top row if available
            if youtube_video_id := rom_data.get('youtube_video_id'):
                youtube_emoji = self.bot.get_formatted_emoji('youtube')
                youtube_url = f"https://www.youtube.com/watch?v={youtube_video_id}"
                top_row_links.append(f"[**{youtube_emoji} Trailer**]({youtube_url})")

            # Build the final links value
            links_value = " ".join(top_row_links)

            # Second row - add achievements and/or PCGamingWiki if available
            second_row_links = []

            if ra_id := rom_data.get('ra_id'):
                ra_emoji = self.bot.get_formatted_emoji('retroachievements')
                ra_url = f"https://retroachievements.org/game/{ra_id}"
                second_row_links.append(f"[**{ra_emoji} Achievements**]({ra_url})")

            if pcgw_url:
                pcgw_emoji = self.bot.get_formatted_emoji('pcgw')
                second_row_links.append(f"[**{pcgw_emoji} PCGWiki**]({pcgw_url})")

            # Add second row if there's anything to show
            if second_row_links:
                links_value += "\n" + " ".join(second_row_links)

            # Add the field to embed
            embed.add_field(name="Links", value=links_value, inline=True)
            
            # File information (rest remains the same as original)
            if rom_data.get('multi') and rom_data.get('files'):
                files = rom_data.get('files', [])
                total_size = sum(f.get('file_size_bytes', 0) for f in files)
                
                # Group files by subfolder
                files_by_subfolder = defaultdict(list)
                for file_info in files:
                    subfolder = self.get_file_subfolder(file_info)
                    files_by_subfolder[subfolder].append(file_info)
                
                # Sort subfolders with None (main) first
                sorted_subfolders = sorted(files_by_subfolder.keys(), key=lambda x: (x is not None, x))
                
                files_info = []
                total_length = 0
                files_shown = 0
                total_files = len(files)
                max_length = 800
                
                for subfolder in sorted_subfolders:
                    subfolder_files = files_by_subfolder[subfolder]
                    
                    # Add subfolder header if not main files
                    if files_info and len(sorted_subfolders) > 1:  # Add spacing between groups
                        if total_length + 1 < max_length:
                            files_info.append("")
                            total_length += 1
                    
                    if subfolder:
                        icon = self.get_subfolder_icon(subfolder)
                        # Special handling for acronyms that should be all caps
                        acronyms = {'dlc': 'DLC'}
                        display_name = acronyms.get(subfolder, subfolder.capitalize())
                        header_line = f"{icon} **{display_name}**"
                        if total_length + len(header_line) + 1 > max_length:
                            files_info.append("...")
                            break
                        files_info.append(header_line)
                        total_length += len(header_line) + 1
                    
                    # Sort files in this subfolder
                    sorted_files = sorted(
                        subfolder_files,
                        key=lambda x: (x.get('file_size_bytes', 0), x.get('file_name', '').lower()),
                        reverse=(len(subfolder_files) > 10)
                    )[:10] if len(subfolder_files) > 10 else sorted(
                        subfolder_files,
                        key=lambda x: x.get('file_name', '').lower()
                    )
                    
                    # Add files from this subfolder
                    for file_info in sorted_files:
                        size_bytes = file_info.get('file_size_bytes', 0)
                        size_str = self.format_file_size(size_bytes)
                        file_line = f"• {file_info['file_name']} ({size_str})"
                        line_length = len(file_line) + 1
                        
                        if total_length + line_length > max_length:
                            files_info.append("...")
                            break
                        
                        files_info.append(file_line)
                        total_length += line_length
                        files_shown += 1
                    
                    if total_length >= max_length:
                        break
                
                # Create field name
                field_name = f"Files (Total: {self.format_file_size(total_size)}"
                if len(files) > files_shown:
                    field_name += f" - Showing {files_shown} of {total_files} files)"
                else:
                    field_name += ")"
                
                # Add field to embed
                embed.add_field(
                    name=field_name,
                    value="\n".join(files_info) if files_info else "No files to display",
                    inline=False
                )
            else:
                # Single file display
                file_size = self.format_file_size(rom_data.get('fs_size_bytes', 0))
                file_name = rom_data.get('fs_name', 'unknown_file')
                
                # Check if single file is in a subfolder
                subfolder = None
                if rom_data.get('files') and len(rom_data.get('files', [])) == 1:
                    subfolder = self.get_file_subfolder(rom_data['files'][0])
                
                file_info_text = f"• {file_name}"
                if subfolder:
                    icon = self.get_subfolder_icon(subfolder)
                    file_info_text = f"{icon} [{subfolder.capitalize()}]\n• {file_name}"
                
                embed.add_field(
                    name=f"File ({file_size})",
                    value=file_info_text,
                    inline=False
                )
                
            return embed, cover_file
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
        """Update file selection dropdown for multi-file ROMs"""
        try:
            # Clear existing file-related components
            components_to_remove = []
            for item in self.children:
                if isinstance(item, (discord.ui.Button, discord.ui.Select)) and item != self.select:
                    components_to_remove.append(item)
            
            for item in components_to_remove:
                self.remove_item(item)

            # Initialize tracking variables
            self.filename_map = {}
            self.file_id_map = {}
            self.selected_files = set()  # Track selected files
            self.file_info_map = {}  # Store file info for quick access

            if rom_data.get('multi') and rom_data.get('files'):
                files = rom_data.get('files', [])
                if not files:
                    return
                
                # Sort and prepare files
                files_with_subfolder = [(f, self.get_file_subfolder(f)) for f in files]
                files_with_subfolder.sort(key=lambda x: (x[1] is not None, x[1], x[0].get('file_name', '').lower()))
                
                # Limit to 25 files (Discord limit)
                display_files = files_with_subfolder[:25]
                
                # Create file select with dynamic placeholder
                self.file_select = discord.ui.Select(
                    placeholder=self._get_file_select_placeholder(),
                    custom_id="file_select",
                    min_values=0,  # Allow deselection
                    max_values=min(len(display_files), 25)
                )
                
                # Add file options with better formatting
                for i, (file_info, subfolder) in enumerate(display_files):
                    short_value = f"file_{i}"
                    self.filename_map[short_value] = file_info['file_name']
                    self.file_id_map[short_value] = str(file_info.get('id'))
                    self.file_info_map[short_value] = (file_info, subfolder)
                    
                    # Format option label
                    label = self._format_file_option_label(file_info, subfolder, short_value)
                    
                    self.file_select.add_option(
                        label=label,
                        value=short_value,
                        emoji=self._get_file_emoji(short_value, subfolder)
                    )
                
                self.file_select.callback = self.file_select_callback
                self.add_item(self.file_select)

                # Create download buttons
                file_name = quote(rom_data.get('fs_name', 'unknown_file'))
                base_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_data['id']}/content/{file_name}"

                # Download Selected button (initially disabled)
                self.download_selected = discord.ui.Button(
                    label="Download Selected (0 files)",
                    style=discord.ButtonStyle.link,
                    url=base_url,
                    disabled=True
                )
                self.add_item(self.download_selected)

                # Download All button
                total_size = sum(f.get('file_size_bytes', 0) for f in rom_data.get('files', []))
                all_files_label = f"Download All ({len(files)} files, {self.format_file_size(total_size)})"
                
                self.download_all = discord.ui.Button(
                    label=all_files_label,
                    style=discord.ButtonStyle.link,
                    url=base_url
                )
                self.add_item(self.download_all)

            else:
                # Single file download
                self._add_single_file_download(rom_data)

        except Exception as e:
            logger.error(f"Error updating file select: {e}")
            raise

    def _get_file_select_placeholder(self) -> str:
        """Generate dynamic placeholder based on selected files"""
        if not self.selected_files:
            return "Select files to download"
        
        count = len(self.selected_files)
        if count == 1:
            # Show the name of the single selected file
            file_id = next(iter(self.selected_files))
            if file_id in self.filename_map:
                filename = self.filename_map[file_id]
                if len(filename) > 30:
                    filename = filename[:27] + "..."
                return f"Selected: {filename}"
            return "1 file selected"
        else:
            # Calculate total size of selected files
            total_size = 0
            for file_id in self.selected_files:
                if file_id in self.file_info_map:
                    file_info, _ = self.file_info_map[file_id]
                    total_size += file_info.get('file_size_bytes', 0)
            
            size_str = self.format_file_size(total_size)
            return f"{count} files selected ({size_str})"

    def _format_file_option_label(self, file_info: Dict, subfolder: Optional[str], short_value: str) -> str:
        """Format file option label with selection indicator"""
        size = self.format_file_size(file_info.get('file_size_bytes', 0))
        file_name = file_info['file_name'][:75]
        
        # Add checkmark if selected
        selected_prefix = "✓ " if short_value in self.selected_files else ""
        
        # Build label
        if subfolder:
            label = f"{selected_prefix}[{subfolder}] {file_name} ({size})"
        else:
            label = f"{selected_prefix}{file_name} ({size})"
        
        # Truncate if too long
        if len(label) > 100:
            max_name_len = 100 - len(f"{selected_prefix}[{subfolder}]  ({size})") if subfolder else 100 - len(f"{selected_prefix} ({size})")
            truncated_name = file_name[:max_name_len-3] + "..."
            if subfolder:
                label = f"{selected_prefix}[{subfolder}] {truncated_name} ({size})"
            else:
                label = f"{selected_prefix}{truncated_name} ({size})"
        
        return label

    def _get_file_emoji(self, short_value: str, subfolder: Optional[str]) -> Optional[str]:
        """Get emoji for file option based on state and type"""
        if short_value in self.selected_files:
            return "✅"  # Selected indicator
        return self.get_subfolder_icon(subfolder)

    def _add_single_file_download(self, rom_data: Dict):
        """Add download button for single file ROM"""
        file_name = quote(rom_data.get('fs_name', 'unknown_file'))
        file_size = self.format_file_size(rom_data.get('fs_size_bytes', 0))
        download_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_data['id']}/content/{file_name}"
        
        self.download_all = discord.ui.Button(
            label=f"Download ({file_size})",
            style=discord.ButtonStyle.link,
            url=download_url
        )
        self.add_item(self.download_all)

    async def file_select_callback(self, interaction: discord.Interaction):
        """Handle file selection with improved feedback"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This selection isn't for you!", ephemeral=True)
            return

        try:
            selected_values = interaction.data.get('values', [])
            
            # Update selected files set
            self.selected_files = set(selected_values)
            
            logger.debug(f"Selected files: {selected_values}")
            
            # Update the dropdown to show new selection state
            self.file_select.placeholder = self._get_file_select_placeholder()
            
            # Rebuild options with selection indicators
            self.file_select.options.clear()
            for short_value, (file_info, subfolder) in self.file_info_map.items():
                label = self._format_file_option_label(file_info, subfolder, short_value)
                self.file_select.add_option(
                    label=label,
                    value=short_value,
                    emoji=self._get_file_emoji(short_value, subfolder)
                )
            
            # Update download buttons
            await self._update_download_buttons(interaction)
            
        except Exception as e:
            logger.error(f"Error in file selection: {e}", exc_info=True)
            await interaction.response.send_message(
                "An error occurred while updating selection. Please try again.",
                ephemeral=True
            )

    async def _update_download_buttons(self, interaction: discord.Interaction):
        """Update download buttons based on selection"""
        # Remove old buttons
        buttons_to_remove = [item for item in self.children if isinstance(item, discord.ui.Button)]
        for button in buttons_to_remove:
            self.remove_item(button)
        
        if self.selected_files and hasattr(self, 'file_id_map'):
            # Get selected file IDs
            selected_file_ids = [self.file_id_map[sv] for sv in self.selected_files if sv in self.file_id_map]
            
            # Calculate total size of selected files
            total_size = 0
            for file_id in self.selected_files:
                if file_id in self.file_info_map:
                    file_info, _ = self.file_info_map[file_id]
                    total_size += file_info.get('file_size_bytes', 0)
            
            # Create download URL with file_ids
            file_name = quote(self._selected_rom.get('fs_name', 'unknown_file'))
            base_url = f"{self.bot.config.DOMAIN}/api/roms/{self._selected_rom['id']}/content/{file_name}"
            
            if selected_file_ids:
                file_ids_param = ','.join(selected_file_ids)
                download_url = f"{base_url}?file_ids={file_ids_param}"
            else:
                download_url = base_url
            
            # Add updated download selected button
            count = len(self.selected_files)
            size_str = self.format_file_size(total_size)
            self.download_selected = discord.ui.Button(
                label=f"Download Selected ({count} {'file' if count == 1 else 'files'}, {size_str})",
                style=discord.ButtonStyle.link,
                url=download_url,
                disabled=False
            )
            self.add_item(self.download_selected)
        else:
            # No files selected - disable download selected
            file_name = quote(self._selected_rom.get('fs_name', 'unknown_file'))
            base_url = f"{self.bot.config.DOMAIN}/api/roms/{self._selected_rom['id']}/content/{file_name}"
            
            self.download_selected = discord.ui.Button(
                label="Download Selected (0 files)",
                style=discord.ButtonStyle.link,
                url=base_url,
                disabled=True
            )
            self.add_item(self.download_selected)
        
        # Re-add download all button
        if hasattr(self, '_selected_rom') and self._selected_rom:
            files = self._selected_rom.get('files', [])
            total_size = sum(f.get('file_size_bytes', 0) for f in files)
            all_files_label = f"Download All ({len(files)} files, {self.format_file_size(total_size)})"
        else:
            all_files_label = "Download All"
        
        file_name = quote(self._selected_rom.get('fs_name', 'unknown_file'))
        download_all_url = f"{self.bot.config.DOMAIN}/api/roms/{self._selected_rom['id']}/content/{file_name}"
        
        self.download_all = discord.ui.Button(
            label=all_files_label,
            style=discord.ButtonStyle.link,
            url=download_all_url
        )
        self.add_item(self.download_all)
        
        await interaction.response.edit_message(view=self)

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
            done, pending = await asyncio.wait(
                [message_task, reaction_task],
                return_when=asyncio.FIRST_COMPLETED,
                timeout=60.0  # Add overall timeout to wait()
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    # Both exceptions are expected when cancelling tasks
                    pass

            # Process completed task if any
            if done:
                completed_task = done.pop()
                try:
                    result = await completed_task
                    
                    if isinstance(result, discord.Message):
                        trigger_type = "message reply"
                    else:
                        reaction, user = result
                        trigger_type = f"reaction {reaction.emoji}"
                    
                    await self.handle_qr_trigger(interaction, trigger_type)
                    
                except asyncio.TimeoutError:
                    # Individual task timed out
                    logger.debug("QR code trigger watch timed out")
                except Exception as e:
                    logger.error(f"Error processing QR trigger result: {e}")
            else:
                # Both tasks timed out (no task completed)
                logger.debug("QR code trigger watch timed out")

        except asyncio.TimeoutError:
            # Overall timeout from wait()
            logger.debug("QR code trigger watch timed out")
        except Exception as e:
            logger.error(f"Error watching for triggers: {e}")

    async def watch_for_qr_triggers(self, interaction: discord.Interaction):
        """Start watching for QR code triggers after ROM selection"""
        if not self.message:
            logger.warning("No message reference for QR code triggers")
            return
        
        # Create task but don't await it - let it run in background
        task = asyncio.create_task(self.start_watching_triggers(interaction))
        
        # Add error handler to prevent unhandled exceptions
        def handle_task_exception(task):
            try:
                task.result()
            except asyncio.CancelledError:
                pass  # Task was cancelled, this is fine
            except asyncio.TimeoutError:
                pass  # Task timed out normally, this is fine
            except Exception as e:
                logger.error(f"Unexpected error in QR trigger task: {e}")
        
        task.add_done_callback(handle_task_exception)

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
    
    
    def is_pc_platform(self, platform_name: str) -> bool:
        """Check if the platform is a PC platform"""
        pc_platforms = [
            "PC (Microsoft Windows)",
            "PC - Windows", 
            "PC - Win3X",
            "PC - DOS",
            "Windows",
            "DOS",
            "Win"
        ]
        return any(pc_plat.lower() in platform_name.lower() for pc_plat in pc_platforms)

    async def get_pcgamingwiki_url(self, igdb_id: int, game_name: str) -> str | None:
        """Get PCGamingWiki URL using Steam/GOG ID or fallback to name"""
        # Try to get IGDB data with external_games
        igdb_handler = self.bot.get_cog('IGDBHandler')
        if not igdb_handler or not igdb_handler.igdb:
            return self._build_pcgw_fallback_url(game_name)
        
        try:
            # Fetch game data with external_games
            game_data = await igdb_handler.igdb.get_game_by_id(igdb_id)
            
            if game_data:
                steam_id = game_data.get('steam_id')
                gog_id = game_data.get('gog_id')
                
                # Primary: Use Steam ID redirect
                if steam_id:
                    return f"https://www.pcgamingwiki.com/api/appid.php?appid={steam_id}"
                
                # Secondary: Use GOG ID redirect
                if gog_id:
                    return f"https://www.pcgamingwiki.com/api/gog.php?id={gog_id}"
            
            # Fallback to name-based URL
            return self._build_pcgw_fallback_url(game_name)
            
        except Exception as e:
            logger.error(f"Error getting PCGamingWiki URL: {e}")
            return self._build_pcgw_fallback_url(game_name)

    def _build_pcgw_fallback_url(self, game_name: str) -> str:
        """Build PCGamingWiki URL from game name"""
        import re
        # Sanitize game name for URL
        sanitized_name = game_name.strip()
        # Remove special characters, replace spaces with underscores
        sanitized_name = re.sub(r'[^\w\s-]', '', sanitized_name)
        sanitized_name = re.sub(r'\s+', '_', sanitized_name)
        return f"https://www.pcgamingwiki.com/wiki/{sanitized_name}"
    
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
                platform_name = self.platform_name or selected_rom.get('platform_name', 'Unknown')
                logger.info(f"ROM selected - User: {interaction.user} (ID: {interaction.user.id}) | ROM: '{selected_rom['name']}' | ROM ID: #{selected_rom_id} | Platform: {platform_name}")
                try:
                    detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{selected_rom_id}')
                    if detailed_rom:
                        selected_rom.update(detailed_rom)
                except Exception as e:
                    logger.error(f"Error fetching detailed ROM data: {e}")
                
                self._selected_rom = selected_rom
                embed, cover_file = await self.create_rom_embed(selected_rom)
                
                # Remove all file-related components first
                components_to_remove = []
                for item in self.children[:]:
                    if isinstance(item, (discord.ui.Button, discord.ui.Select)) and item != self.select:
                        components_to_remove.append(item)
                
                for item in components_to_remove:
                    self.remove_item(item)
                
                # Update file components for both single and multi-file ROMs
                await self.update_file_select(selected_rom)
                
                # Edit message with file parameter (not attachments)
                if cover_file:
                    edited_message = await interaction.message.edit(
                        content=interaction.message.content,
                        embed=embed,
                        view=self,
                        file=cover_file  # Use file parameter, not attachments
                    )
                else:
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
                
class NoResultsView(discord.ui.View):
    """View shown when search returns no results, offering to request the game"""
    
    def __init__(self, bot, platform_name: str, game_name: str, author_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.platform_name = platform_name
        self.game_name = game_name
        self.author_id = author_id
        
        # Add "Request This Game" button
        request_button = discord.ui.Button(
            label="Request This Game",
            style=discord.ButtonStyle.primary,
            emoji="📝"
        )
        request_button.callback = self.request_game_callback
        self.add_item(request_button)
        
        # Add "Search Tips" button for help
        tips_button = discord.ui.Button(
            label="💡 Search Tips",
            style=discord.ButtonStyle.secondary
        )
        tips_button.callback = self.search_tips_callback
        self.add_item(tips_button)
    
    async def request_game_callback(self, interaction: discord.Interaction):
        """Handle the request button click"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This button isn't for you!", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        # Get the Request cog
        request_cog = self.bot.get_cog('Request')
        if not request_cog:
            await interaction.followup.send("❌ Request system is not available", ephemeral=True)
            return
        
        # Check if requests are enabled
        if not request_cog.requests_enabled:
            await interaction.followup.send("❌ The request system is currently disabled.", ephemeral=True)
            return
        
        try:
            # Check if game already exists (double-check with broader search)
            exists, matches = await request_cog.check_if_game_exists(self.platform_name, self.game_name)
            
            # Search IGDB for metadata if available
            igdb_matches = []
            if request_cog.igdb_enabled:
                try:
                    igdb_matches = await request_cog.igdb.search_game(self.game_name, self.platform_name)
                except Exception as e:
                    logger.error(f"Error fetching IGDB data: {e}")
            
            if exists and matches:
                # Game was found with broader search - show it
                from .requests import ExistingGameView
                view = ExistingGameView(self.bot, matches, self.platform_name, self.game_name, self.author_id)
                
                embed = discord.Embed(
                    title="Game Found!",
                    description=f"A broader search found {len(matches)} matching game(s) for '{self.game_name}':",
                    color=discord.Color.blue()
                )
                
                for i, rom in enumerate(matches[:3]):
                    embed.add_field(
                        name=rom.get('name', 'Unknown'),
                        value=f"File: {rom.get('fs_name', 'Unknown')}",
                        inline=False
                    )
                
                if len(matches) > 3:
                    embed.set_footer(text=f"...and {len(matches) - 3} more")
                
                message = await interaction.followup.send(embed=embed, view=view)
                view.message = message
                
            elif igdb_matches:
                # Show IGDB selection
                from .requests import GameSelectView
                select_view = GameSelectView(self.bot, igdb_matches, self.platform_name)
                initial_embed = select_view.create_game_embed(igdb_matches[0])
                select_view.message = await interaction.followup.send(
                    "Select the correct game from IGDB:",
                    embed=initial_embed,
                    view=select_view
                )
                
                await select_view.wait()
                
                if select_view.selected_game and select_view.selected_game != "manual":
                    await request_cog.process_request(
                        interaction, 
                        self.platform_name, 
                        self.game_name, 
                        None,  # No additional details
                        select_view.selected_game, 
                        select_view.message
                    )
            else:
                # Direct request without IGDB
                await request_cog.process_request(
                    interaction,
                    self.platform_name,
                    self.game_name,
                    None,  # No additional details
                    None,  # No IGDB data
                    None   # No message to update
                )
                
            # Disable buttons after use
            for item in self.children:
                item.disabled = True
            
            try:
                await interaction.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass  # Message might have been deleted or not accessible
                
        except Exception as e:
            logger.error(f"Error processing request from search: {e}")
            await interaction.followup.send("❌ An error occurred while processing your request", ephemeral=True)
    
    async def search_tips_callback(self, interaction: discord.Interaction):
        """Show search tips"""
        embed = discord.Embed(
            title="💡 Search Tips",
            description="Here are some tips for better search results:",
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name="1. Try shorter search terms",
            value="Instead of 'The Legend of Zelda: Ocarina of Time', try 'Zelda Ocarina' or just 'Ocarina'",
            inline=False
        )
        
        embed.add_field(
            name="2. Check the platform name",
            value="Make sure you're using the exact platform name from the autocomplete",
            inline=False
        )
        
        embed.add_field(
            name="3. Try alternate names",
            value="Some games have different regional names (e.g., 'Mega Man' vs 'Rockman')",
            inline=False
        )
        
        embed.add_field(
            name="4. Remove special characters",
            value="Try searching without colons, apostrophes, or other punctuation",
            inline=False
        )
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def on_timeout(self):
        """Disable all components when the view times out"""
        for item in self.children:
            item.disabled = True
        # NoResultsView doesn't store message reference, so we can't update it
        # The view will simply stop accepting interactions


class Search(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.platform_emoji_names = {}  # Will be populated from API data
        self._emojis_initialized = False
        
        # Map of common platform name variations
        self.platform_variants = {
            '3DO Interactive Multiplayer': ['3do'],
            'Apple II': ['apple_ii'],
            'Amiga': ['amiga'],
            'Amiga CD32': ['cd32'],
            'Amstrad CPC': ['amstrad'],
            'Apple Pippin': ['pippin'],
            'Arcade - MAME': ['arcade'],
            'Arcade - PC Based': ['arcade'],
            'Arcade - FinalBurn Neo': ['arcade'],
            'Atari 2600': ['2600'],
            'Atari 5200': ['5200'],
            'Atari 7800': ['7800'],
            'Atari Jaguar': ['jaguar'],
            'Atari Jaguar CD': ['jaguar_cd'],
            'Atari Lynx': ['lynx'],
            'Casio Loopy': ['loopy'],
            'Commodore C64/128/MAX': ['c64'],
            'Dreamcast': ['dreamcast'],
            'Family Computer': ['famicom'],
            'Famicom': ['famicom'],
            'Family Computer Disk System': ['fds'],
            'Famicom Disk System': ['fds'],
            'FM Towns': ['fm_towns'],
            'Game & Watch': ['game_and_watch'],
            'Game Boy': ['gameboy', 'gameboy_pocket'],
            'Game Boy Advance': ['gameboy_advance', 'gameboy_advance_sp', 'gameboy_micro'],
            'Game Boy Color': ['gameboy_color'],
            'J2ME': ['cell_java'],
            'Mac': ['mac', 'mac_imac'],
            'Mega Duck/Cougar Boy': ['mega_duck'],
            'MSX': ['msx'],
            'MSX2': ['msx'],
            'N-Gage': ['n_gage'],
            'Neo Geo AES': ['neogeo_aes'],
            'Neo Geo CD': ['neogeo_cd'],
            'Neo Geo Pocket': ['neogeo_pocket'],
            'Neo Geo Pocket Color': ['neogeo_pocket_color'],
            'Nintendo 3DS': ['3ds'],
            'Nintendo 64': ['n64'],
            'Nintendo 64Dd': ['n64_dd'],
            'Nintendo 64DD': ['n64_dd'],
            'Nintendo DS': ['ds', 'ds_lite'],
            'Nintendo DSi': ['dsi'],
            'Nintendo Entertainment System': ['nes'],
            'Nintendo GameCube': ['gamecube'],
            'Nintendo Switch': ['switch', 'switch_docked'],
            'PC-8800 Series': ['pc_88'],
            'PC-9800 Series': ['pc_98'],
            'PC-FX': ['pc_fx'],
            'PC (Microsoft Windows)': ['pc'],
            'PC - DOS': ['dos'],
            'PC - Win3X': ['win_3x_gui', 'pc'],
            'PC - Windows': ['pc', 'win_9x'],
            'Philips CD-i': ['cd_i'],
            'PlayStation': ['ps', 'ps_one'],
            'PlayStation 2': ['ps2', 'ps2_slim'],
            'PlayStation 3': ['ps3', 'ps3_slim'],
            'PlayStation 4': ['ps4'],
            'PlayStation 5': ['ps5'],
            'PlayStation Portable': ['psp', 'psp_go'],
            'PlayStation Vita': ['vita'],
            'Pokémon mini': ['pokemon_mini'],
            'Sega 32X': ['32x'],
            'Sega CD': ['sega_cd'],
            'Segacd': ['sega_cd'],
            'Sega Game Gear': ['game_gear'],
            'Sega Master System/Mark III': ['master_system'],
            'Sega Mega Drive/Genesis': ['genesis', 'genesis_2', 'nomad'],
            'Sega Pico': ['pico'],
            'Sega Saturn': ['saturn_2'],
            'Sharp X68000': ['x68000'],
            'Sinclair Zxs': ['zx_spectrum'],
            'Super Famicom': ['sfam'],
            'Super Nintendo Entertainment System': ['snes'],
            'Switch': ['switch', 'switch_docked'],
            'Teknoparrot': ['teknoparrot'],
            'Turbografx-16/PC Engine CD': ['tg_16_cd'],
            'TurboGrafx-16/PC Engine': ['tg_16', 'turboduo', 'turboexpress'],
            'Vectrex': ['vectrex'],
            'Virtual Boy': ['virtual_boy'],
            'Visual Memory Unit / Visual Memory System': ['vmu'],
            'Wii': ['wii'],
            'Windows': ['pc'],
            'WonderSwan': ['wonderswan'],
            'WonderSwan Color': ['wonderswan'],
            'Xbox': ['xbox_og'],
            'Xbox 360': ['xbox_360'],
            'Xbox One': ['xbone'],
        }
        bot.loop.create_task(self.initialize_platform_emoji_mappings())
    
    async def initialize_platform_emoji_mappings(self):
        """Initialize platform -> emoji mappings using API data"""
        if self._emojis_initialized:
            return
        
        await self.bot.wait_until_ready()
        
        # Wait for emojis to be loaded
        max_wait = 30  # seconds
        for _ in range(max_wait * 2):  # Check every 0.5 seconds
            if hasattr(self.bot, 'emoji_dict') and len(self.bot.emoji_dict) > 0:
                logger.info(f"Emoji dict ready with {len(self.bot.emoji_dict)} emojis")
                break
            await asyncio.sleep(0.5)
        else:
            logger.warning("Emoji dict not populated after waiting, continuing anyway")
        
        try:
            # Get platforms from API
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                logger.warning("Could not fetch platforms for emoji mapping")
                return
                
            # Don't sanitize here, we need the full platform data including custom_name
            mapped_count = 0
            
            for platform in raw_platforms:
                if 'name' in platform:
                    platform_name = platform['name']
                    # Use custom name for mapping if available
                    display_name = self.bot.get_platform_display_name(platform)
                    
                    variants = self.platform_variants.get(platform_name, [])
                    
                    # Try each variant
                    mapped = False
                    for variant in variants:
                        if variant in self.bot.emoji_dict:
                            # Store mapping for both regular and custom names
                            self.platform_emoji_names[platform_name] = variant
                            if display_name != platform_name:
                                self.platform_emoji_names[display_name] = variant
                            mapped = True
                            mapped_count += 1
                            break
                    
                    # If no variant worked, try simple name
                    if not mapped:
                        simple_name = platform_name.lower().replace(' ', '_').replace('-', '_')
                        if simple_name in self.bot.emoji_dict:
                            self.platform_emoji_names[platform_name] = simple_name
                            if display_name != platform_name:
                                self.platform_emoji_names[display_name] = simple_name
                            mapped_count += 1
            
            logger.info(f"Successfully mapped {mapped_count} platform(s) to custom server emoji(s)")
            
            # Log unmapped platforms for debugging
            unmapped = [p['name'] for p in raw_platforms if p['name'] not in self.platform_emoji_names]
            if unmapped:
                logger.debug(f"Unmapped platforms: {', '.join(sorted(unmapped))}")

            self._emojis_initialized = True

        except Exception as e:
            logger.error(f"Error initializing platform emoji mappings: {e}")
    
    def get_platform_with_emoji(self, platform_name: str) -> str:
        """Returns platform name with its emoji if available."""
        if not platform_name:
            return platform_name

        # Get the potential emoji names (e.g., ['n64']) for the platform
        variant_names = self.platform_variants.get(
            platform_name, [platform_name.lower().replace(' ', '_').replace('-', '_')]
        )
        variants_to_check = variant_names if isinstance(variant_names, list) else [variant_names]

        # Build a quick lookup for all visible server emojis
        # self.bot.emojis contains all server-specific emojis the bot can see
        server_emojis_by_name = {e.name: e for e in self.bot.emojis}

        for variant in variants_to_check:
            # Priority 1: Check for a server-specific emoji.
            if variant in server_emojis_by_name:
                return f"{platform_name} {server_emojis_by_name[variant]}"
            
            # Priority 2: Check for a global application emoji.
            # Add safe checking here
            if hasattr(self.bot, 'emoji_dict') and variant in self.bot.emoji_dict:
                return f"{platform_name} {self.bot.emoji_dict[variant]}"

        # If no custom emoji found, use a fallback.
        return f"{platform_name} 🎮"
        

    async def platform_autocomplete(self, ctx: discord.AutocompleteContext):
        """Autocomplete function for platform names."""
        try:
            # Get raw platforms to access custom_name field
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            
            if raw_platforms:
                platform_names = []
                for p in raw_platforms:
                    # Prefer custom name over regular name
                    display_name = self.bot.get_platform_display_name(p)
                    if display_name:
                        platform_names.append(display_name)
                
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

            platform_id, platform_display_name = await self.bot.find_platform_by_name(platform, raw_platforms)

            if not platform_id:
                # Show available platforms with display names
                platforms_list = "\n".join(
                    f"• {self.get_platform_with_emoji(self.bot.get_platform_display_name(p))}"
                    for p in raw_platforms
                )
                await ctx.respond(f"Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            firmware_data = await self.bot.fetch_api_endpoint(f'firmware?platform_id={platform_id}')

            if not firmware_data:
                await ctx.respond(f"No firmware files found for platform '{platform_display_name}'")
                return

            def format_file_size(size_bytes):
                return ROM_View.format_file_size(size_bytes)

            embeds = []
            current_embed = discord.Embed(
                title=f"Firmware Files for {self.get_platform_with_emoji(platform_display_name)}",
                description=f"Found {len(firmware_data)} firmware file(s) {self.bot.emoji_dict.get('bios', '🔧')}",
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
                        title=f"Firmware Files for {platform_display_name} (Continued)",
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
                platform_id, platform_display_name = await self.bot.find_platform_by_name(platform, raw_platforms)

                if not platform_id:
                    # Show available platforms with display names
                    platforms_list = "\n".join(
                        f"• {self.get_platform_with_emoji(self.bot.get_platform_display_name(p))}"
                        for p in sorted(raw_platforms, key=lambda x: self.bot.get_platform_display_name(x))
                    )
                    await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                    return

                # Find platform_data from already-fetched platforms
                platform_data = None
                for p in raw_platforms:
                    if p['id'] == platform_id:
                        platform_data = p
                        break
                
                rom_count = platform_data.get('rom_count', 0) if platform_data else 0
                
                if rom_count <= 0:
                    await ctx.respond(f"❌ No ROMs found for platform '{self.get_platform_with_emoji(platform_display_name)}'")
                    return

                # Try up to 10 times to find a valid ROM for the specific platform
                max_attempts = 10
                for attempt in range(max_attempts):
                    try:
                        # Get ROMs for platform
                        roms_response = await self.bot.fetch_api_endpoint(
                            f'roms?platform_id={platform_id}&platform_ids={platform_id}&limit={rom_count}'
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
                            
                            logger.info(f"Random ROM found - User: {ctx.author} (ID: {ctx.author.id}) | ROM: '{rom_data['name']}' | ROM ID: #{rom_data['id']} | Platform: {platform_display_name}")
                            
                            # Create view with explicit ROM data
                            view = ROM_View(self.bot, [rom_data], ctx.author.id, platform_display_name)
                            view.remove_item(view.select)
                            view._selected_rom = rom_data
                            embed, cover_file = await view.create_rom_embed(rom_data)  # Changed to unpack tuple
                            await view.update_file_select(rom_data)

                            # Send with file if available
                            if cover_file:
                                initial_message = await ctx.respond(
                                    f"🎲 Found a random ROM from {self.get_platform_with_emoji(platform_display_name)}:",
                                    embed=embed,
                                    view=view,
                                    file=cover_file  # Add the file
                                )
                            else:
                                initial_message = await ctx.respond(
                                    f"🎲 Found a random ROM from {self.get_platform_with_emoji(platform_display_name)}:",
                                    embed=embed,
                                    view=view
                                )

                            if isinstance(initial_message, discord.Interaction):
                                initial_message = await initial_message.original_response()
                            
                            view.message = initial_message
                            task = self.bot.loop.create_task(view.watch_for_qr_triggers(ctx.interaction))
                            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
                            return

                    except Exception as e:
                        logger.error(f"Error in attempt {attempt + 1}: {e}")
                    
                    logger.info(f"Random ROM attempt {attempt + 1} for platform {platform_display_name} failed")
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

                # Try up to 10 times to find a valid ROM
                max_attempts = 10
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
                        
                        logger.info(f"Random ROM found - User: {ctx.author} (ID: {ctx.author.id}) | ROM: '{rom_data['name']}' | ROM ID: #{rom_data['id']} | Platform: {platform_name or 'Unknown'}")

                        # Create view with explicit ROM data
                        view = ROM_View(self.bot, [rom_data], ctx.author.id, platform_name)
                        view.remove_item(view.select)
                        view._selected_rom = rom_data
                        embed, cover_file = await view.create_rom_embed(rom_data)  # Changed to unpack tuple
                        await view.update_file_select(rom_data)

                        # Send with file if available
                        message_content = f"🎲 Found a random ROM" + (f" from {self.get_platform_with_emoji(platform_name)}" if platform_name else "") + ":"
                        if cover_file:
                            initial_message = await ctx.respond(
                                message_content,
                                embed=embed,
                                view=view,
                                file=cover_file  # Add the file
                            )
                        else:
                            initial_message = await ctx.respond(
                                message_content,
                                embed=embed,
                                view=view
                            )

                        if isinstance(initial_message, discord.Interaction):
                            initial_message = await initial_message.original_response()
                        
                        view.message = initial_message
                        task = self.bot.loop.create_task(view.watch_for_qr_triggers(ctx.interaction))
                        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
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
            logger.info(f"Search command - User: {ctx.author} (ID: {ctx.author.id}) | Query: '{game}' | Platform: {platform}")
            # Get platform data
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                await ctx.respond("❌ Unable to fetch platforms data")
                return

            # Find matching platform
            platform_id, platform_display_name = await self.bot.find_platform_by_name(platform, raw_platforms)

            if not platform_id:
                # Show available platforms with display names
                platforms_list = "\n".join(
                    f"• {self.get_platform_with_emoji(self.bot.get_platform_display_name(p))}"
                    for p in sorted(raw_platforms, key=lambda x: self.bot.get_platform_display_name(x))
                )
                await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            # Search for ROMs
            search_term = game.strip()
            search_results = []

            # Try multiple search strategies
            search_attempts = [
                search_term,  # Original search
            ]

            # If the search term has multiple words, try searching for key parts
            words = search_term.split()
            if len(words) > 1:
                # Add first word only (often the main game name)
                search_attempts.append(words[0])
                
                # If last word is a number, try first word + number (for sequels)
                if words[-1].isdigit():
                    search_attempts.append(f"{words[0]} {words[-1]}")

            # Try each search strategy
            for attempt in search_attempts:
                search_response = await self.bot.fetch_api_endpoint(
                    f'roms?platform_id={platform_id}&platform_ids={platform_id}&search_term={attempt}&limit=100'
                )
                
                # Handle paginated response
                if search_response and isinstance(search_response, dict) and 'items' in search_response:
                    all_results = search_response['items']
                elif search_response and isinstance(search_response, list):
                    all_results = search_response
                else:
                    all_results = []
                
                # If we're on a broader search, filter results to match original intent
                if attempt != search_term and all_results:
                    # Filter to games that contain all important words from original search
                    important_words = [w.lower() for w in words if not w.lower() in ['the', 'of', 'and']]
                    search_results = [
                        rom for rom in all_results
                        if all(word in rom['name'].lower() for word in important_words)
                    ][:25]  # Limit to 25 results
                else:
                    search_results = all_results[:25]
                
                if search_results:
                    break  # Found results, stop trying

            if not search_results or not isinstance(search_results, list) or len(search_results) == 0:
                logger.info(f"Search no results - User: {ctx.author} (ID: {ctx.author.id}) | Query: '{game}' | Platform: {platform_display_name}")
                # Create an embed for no results
                embed = discord.Embed(
                    title="No Results Found",
                    description=f"No ROMs found matching '**{game}**' for platform '**{self.get_platform_with_emoji(platform_display_name)}**'",
                    color=discord.Color.orange()
                )
                
                embed.add_field(
                    name="What would you like to do?",
                    value="• Click **Request This Game** to submit a request for this ROM\n"
                          "• Click **Search Tips** for help improving your search\n"
                          "• Try searching again with different terms or check the platform name",
                    inline=False
                )
                
                # Add the no results view with request button
                view = NoResultsView(self.bot, platform_display_name, game, ctx.author.id)
                await ctx.respond(embed=embed, view=view)
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
                    f"Found 25+ ROMs matching '{game}' for platform '{self.get_platform_with_emoji(platform_display_name)}'. "
                    f"Showing first 25 results.\nPlease refine your search terms for more specific results:"
                )
            else:
                initial_content = f"Found {len(search_results)} ROMs matching '{game}' for platform '{self.get_platform_with_emoji(platform_display_name)}':"

            # Create view first
            view = ROM_View(self.bot, search_results, ctx.author.id, platform_display_name)
            
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
