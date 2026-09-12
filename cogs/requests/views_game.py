"""Views for choosing which game a request is for.

Shown before a request exists: pick an IGDB match, pick from what the
collection already has, or describe a specific version.
"""

import logging
import re
from datetime import datetime

import discord

from ..search import ROM_View
from .matching import filter_out_existing

logger = logging.getLogger(__name__)


class VariantRequestModal(discord.ui.Modal):
    def __init__(self, bot, platform_name, game_name, original_details, igdb_matches, ctx_or_interaction, author_id=None):
        super().__init__(title="Request Different Version")
        self.bot = bot
        self.platform_name = platform_name
        self.game_name = game_name
        self.original_details = original_details
        self.igdb_matches = igdb_matches
        
        # Handle both ctx and interaction objects
        if hasattr(ctx_or_interaction, 'author'):
            # It's a ctx object
            self.ctx = ctx_or_interaction
            self.author_id = ctx_or_interaction.author.id
        else:
            # It's an interaction object
            self.ctx = ctx_or_interaction
            self.author_id = author_id or ctx_or_interaction.user.id
        
        # Add text input for variant details
        self.variant_input = discord.ui.InputText(
            label="Specify Version",
            placeholder="e.g., 'English translation patch', 'Kaizo hack', 'PAL region', etc.",
            style=discord.InputTextStyle.long,
            required=True,
            max_length=500
        )
        self.add_item(self.variant_input)
        
        # Optional additional notes
        self.notes_input = discord.ui.InputText(
            label="Additional Notes (Optional)",
            placeholder="e.g., 'Current ROM broken', 'Update released', etc.",
            style=discord.InputTextStyle.long,
            required=False,
            max_length=500
        )
        self.add_item(self.notes_input)
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        # Combine the variant request with original details
        variant_details = f"Version Request: {self.variant_input.value}"
        if self.notes_input.value:
            variant_details += f"\nAdditional Notes: {self.notes_input.value}"
        
        if self.original_details:
            combined_details = f"{self.original_details}\n\n{variant_details}"
        else:
            combined_details = variant_details
        
        # Get the request cog and continue with the flow
        request_cog = self.bot.get_cog('Request')
        if request_cog:
            await request_cog.continue_request_flow(
                interaction,
                self.platform_name, 
                self.game_name, 
                combined_details, 
                self.igdb_matches
            )


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
    def __init__(self, bot, matches, platform_name=None):
        super().__init__(timeout=180)  # 3 minute timeout
        self.bot = bot
        self.matches = matches
        self.selected_game = None
        self.message = None
        self.platform_name = platform_name
        
        # Add select menu (row 1)
        self.select_menu = GameSelect(matches)
        self.add_item(self.select_menu)
        
        # Add "Submit Request" button first (row 2) - disabled initially
        self.submit_button = discord.ui.Button(
            label="Submit Request",
            style=discord.ButtonStyle.success,
            row=2,
            disabled=True  # Disabled until a game is selected
        )
        self.add_item(self.submit_button)
        
        # Add "Not Listed" button second (row 2)
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
        """Create an embed for the selected game"""
        embed = discord.Embed(
            title=f"{game['name']}",
            color=discord.Color.green()
        )
        
        # Set the romm logo as thumbnail
        embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        # Set the cover image as the main image
        if game.get('cover_url'):
            embed.set_image(url=game['cover_url'])
        
        # Always use the platform_name from the request
        search_cog = self.bot.get_cog('Search')
        platform_display = self.platform_name
        if search_cog:
            platform_display = search_cog.get_platform_with_emoji(self.platform_name)

        embed.add_field(
            name="Platform",
            value=platform_display,
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
            except ValueError:
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
        if igdb_name:
            igdb_link_name = igdb_name.lower().replace(' ', '-')
            igdb_link_name = re.sub(r'[^a-z0-9-]', '', igdb_link_name)
            igdb_url = f"https://www.igdb.com/games/{igdb_link_name}"
            
            # Get the formatted emoji using the helper method
            igdb_emoji = self.bot.get_formatted_emoji('igdb')
            
            embed.add_field(
                name="Links",
                value=f"[**{igdb_emoji} IGDB**]({igdb_url})",
                inline=True
            )
        
        return embed

    async def update_view_for_selection(self, game):
        self.selected_game = game  # Store the selected game
        embed = self.create_game_embed(game)
        
        # Enable the submit button and set its callback
        self.submit_button.disabled = False
        
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

        await self.message.edit(embed=embed, view=self)

    async def on_timeout(self):
        """Disable all components when the view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass  # Message was deleted or can't be edited


class ExistingGameWithIGDBView(discord.ui.View):
    """View that combines existing game selection with IGDB matching"""
    
    def __init__(self, bot, existing_matches, igdb_matches, platform_name, game_name, author_id):
        super().__init__(timeout=180)
        self.bot = bot
        self.existing_matches = existing_matches
        self.igdb_matches = igdb_matches
        self.platform_name = platform_name
        self.game_name = game_name
        self.author_id = author_id
        self.selected_rom = None
        self.selected_igdb = None
        self.message = None

        # Filter IGDB matches to remove games that already exist
        self.filtered_igdb_matches = filter_out_existing(existing_matches, igdb_matches)
        
        # Add select menu for existing games if multiple
        if len(existing_matches) > 1:
            self.existing_select = discord.ui.Select(
                placeholder="Download existing game from collection",
                custom_id="existing_game_select",
                row=0
            )
            
            for rom in existing_matches[:25]:
                display_name = rom['name'][:75] if len(rom['name']) > 75 else rom['name']
                file_name = rom.get('fs_name', 'Unknown filename')
                truncated_filename = (file_name[:47] + '...') if len(file_name) > 50 else file_name
                
                self.existing_select.add_option(
                    label=display_name,
                    value=str(rom['id']),
                    description=f"{truncated_filename}"
                )
            
            self.existing_select.callback = self.existing_select_callback
            self.add_item(self.existing_select)
        
        # Add IGDB select menu only if there are non-existing games
        if self.filtered_igdb_matches:
            self.igdb_select = discord.ui.Select(
                placeholder="Or request a different game from IGDB",
                custom_id="igdb_select",
                row=1
            )
            
            for i, match in enumerate(self.filtered_igdb_matches[:25]):
                description = f"{match['release_date']} | {', '.join(match['platforms'][:2])}"
                if len(description) > 100:
                    description = description[:97] + "..."
                
                self.igdb_select.add_option(
                    label=match["name"][:100],
                    description=description,
                    value=str(i)
                )
            
            self.igdb_select.callback = self.igdb_select_callback
            self.add_item(self.igdb_select)
        
        # Button row
        button_row = 2
        
        # If single existing match, add download button
        if len(existing_matches) == 1:
            download_btn = discord.ui.Button(
                label="Download Existing",
                style=discord.ButtonStyle.success,
                row=button_row
            )
            download_btn.callback = lambda i: self.handle_single_existing(i, existing_matches[0])
            self.add_item(download_btn)
        
        # Add "Request Different Version" button
        request_different = discord.ui.Button(
            label="Request Different Version",
            style=discord.ButtonStyle.primary,
            row=button_row
        )
        request_different.callback = self.request_different_callback
        self.add_item(request_different)
        
        # Add "Cancel" button
        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=button_row
        )
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)
    
    
    
    async def existing_select_callback(self, interaction: discord.Interaction):
        """Handle existing game selection for download"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        selected_rom_id = int(interaction.data['values'][0])
        self.selected_rom = next((rom for rom in self.existing_matches if rom['id'] == selected_rom_id), None)
        
        if self.selected_rom:
            # Show full ROM view for download
            await self.show_rom_for_download(interaction, self.selected_rom)
    
    async def igdb_select_callback(self, interaction: discord.Interaction):
        """Handle IGDB game selection for request"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        game_index = int(interaction.data['values'][0])
        self.selected_igdb = self.filtered_igdb_matches[game_index]  # Use filtered list
        
        # Clear all items completely - no buttons at all
        self.clear_items()
        
        # Update the embed to show selection
        embed = discord.Embed(
            title="Processing Request",
            description=f"Submitting request for **{self.selected_igdb['name']}**...",
            color=discord.Color.blue()
        )
        
        # Update the message with no view components
        await interaction.response.edit_message(embed=embed, view=self)
        
        # Process as a new request
        request_cog = self.bot.get_cog('Request')
        if request_cog:
            platform_name, mapping_id, platform_exists = await request_cog.get_platform_request_context(
                self.platform_name
            )
            await request_cog.process_request_with_platform(
                interaction,
                platform_name,
                self.selected_igdb['name'],  # Use IGDB name
                None,  # No additional details
                self.selected_igdb,
                self.message,
                mapping_id,
                platform_exists
            )
            self.stop()
        
    async def handle_single_existing(self, interaction: discord.Interaction, rom_data):
        """Handle download of single existing game"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        await self.show_rom_for_download(interaction, rom_data)
    
    async def show_rom_for_download(self, interaction, rom_data):
        """Show the full ROM view for downloading"""
        # Fetch full ROM details and show download interface
        try:
            detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom_data["id"]}')
            if detailed_rom:
                rom_data.update(detailed_rom)
        except Exception as e:
            logger.error(f"Error fetching ROM details: {e}")
        
        rom_view = ROM_View(self.bot, [rom_data], self.author_id, self.platform_name)
        rom_view.remove_item(rom_view.select)
        rom_view._selected_rom = rom_data
        
        rom_embed, cover_file = await rom_view.create_rom_embed(rom_data)
        await rom_view.update_file_select(rom_data)
        
        # Clear and rebuild view with download options
        self.clear_items()
        
        # Copy download components from ROM_View
        for item in rom_view.children:
            if isinstance(item, (discord.ui.Button, discord.ui.Select)):
                if isinstance(item, discord.ui.Select) and item.custom_id == "file_select":
                    async def file_select_callback(
                        interaction,
                        source_view=rom_view,
                        host_view=self
                    ):
                        await source_view.file_select_callback(
                            interaction,
                            target_view=host_view
                        )

                    item.callback = file_select_callback

                self.add_item(item)
        
        if cover_file:
            await interaction.response.edit_message(
                content="✅ **Download this game:**",
                embed=rom_embed,
                view=self,
                file=cover_file
            )
        else:
            await interaction.response.edit_message(
                content="✅ **Download this game:**",
                embed=rom_embed,
                view=self
            )
    
    async def request_different_callback(self, interaction: discord.Interaction):
        """User wants to request a different version"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        modal = VariantRequestModal(
            self.bot,
            self.platform_name,
            self.game_name,
            None,
            self.igdb_matches if self.igdb_matches else [],
            interaction
        )
        await interaction.response.send_modal(modal)
        self.stop()
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel the request"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
            
        self.clear_items()
        cancelled_button = discord.ui.Button(
            label="Cancelled",
            style=discord.ButtonStyle.secondary,
            disabled=True
        )
        self.add_item(cancelled_button)
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        """Disable all components when the view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass  # Message was deleted or can't be edited


class ExistingGameView(discord.ui.View):
    """View for when requested games already exist in the collection"""

    def __init__(self, bot, matches, platform_name, game_name, author_id):
        super().__init__(timeout=180)  # 3 minute timeout
        self.bot = bot
        self.matches = matches
        self.platform_name = platform_name
        self.game_name = game_name
        self.author_id = author_id
        self.selected_rom = None
        self.action = None  # 'download' or 'request_different'
        self.message = None  # Store the message reference
        
        # If we have multiple matches, add a select menu
        if len(matches) > 1:
            self.select = discord.ui.Select(
                placeholder="Select game to view/download",
                custom_id="existing_game_select"
            )
            
            for rom in matches[:25]:
                display_name = rom['name'][:75] if len(rom['name']) > 75 else rom['name']
                file_name = rom.get('fs_name', 'Unknown filename')
                truncated_filename = (file_name[:47] + '...') if len(file_name) > 50 else file_name
                
                self.select.add_option(
                    label=display_name,
                    value=str(rom['id']),
                    description=truncated_filename
                )
            
            self.select.callback = self.select_callback
            self.add_item(self.select)
        
        # Add "Request Different Version" button
        request_different = discord.ui.Button(
            label="Request Different Version",
            style=discord.ButtonStyle.primary,
            row=1
        )
        request_different.callback = self.request_different_callback
        self.add_item(request_different)
        
        # Add "Cancel" button
        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=1
        )
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)
        
        # If single match, show full ROM view immediately
        if len(matches) == 1:
            self.selected_rom = matches[0]
    
    async def create_full_rom_view(self, rom_data):
        """Create a full ROM view similar to search results"""
        
        # Fetch detailed ROM data if not already fetched
        try:
            if 'igdb' not in rom_data:  # Check if we have full details
                detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom_data["id"]}')
                if detailed_rom:
                    rom_data.update(detailed_rom)
        except Exception as e:
            logger.error(f"Error fetching detailed ROM data: {e}")
        
        # Platform emoji matching
        search_cog = self.bot.get_cog('Search')
        if search_cog and self.platform_name:
            platform_display = search_cog.get_platform_with_emoji(self.platform_name)
        else:
            platform_display = self.platform_name

        
        # Create ROM_View instance to use its embed creation
        rom_view = ROM_View(self.bot, [rom_data], self.author_id, self.platform_name)
        rom_embed, cover_file = await rom_view.create_rom_embed(rom_data)  # FIXED: Unpack tuple
        
        # Update the view to include file selection if available
        await rom_view.update_file_select(rom_data)
        
        # Copy over the file select dropdown if it exists
        file_select = None
        for item in rom_view.children:
            if isinstance(item, discord.ui.Select) and item.custom_id == "file_select":
                file_select = item
                break
        
        # Determine which row to use for buttons
        button_row = 2 if file_select else 1
        
        # Clear current items and rebuild view
        self.clear_items()
        
        # Re-add the game select if we have multiple matches
        if len(self.matches) > 1:
            self.add_item(self.select)
        
        # Add file select if available
        if file_select:
            async def file_select_callback(
                interaction,
                source_view=rom_view,
                host_view=self
            ):
                await source_view.file_select_callback(
                    interaction,
                    target_view=host_view
                )

            file_select.callback = file_select_callback
            self.add_item(file_select)
        
        # Add all three buttons on the same row
        # Add download button(s)
        for item in rom_view.children:
            if isinstance(item, discord.ui.Button) and "Download" in item.label:
                item.row = button_row
                self.add_item(item)
        
        # Re-add "Request Different Version" and "Cancel" buttons
        request_different = discord.ui.Button(
            label="Request Different Version",
            style=discord.ButtonStyle.primary,
            row=button_row  # Same row as download button
        )
        request_different.callback = self.request_different_callback
        self.add_item(request_different)
        
        # Add "Cancel" button
        cancel_button = discord.ui.Button(
            label="Cancel",
            style=discord.ButtonStyle.secondary,
            row=button_row  # Same row as other buttons
        )
        cancel_button.callback = self.cancel_callback
        self.add_item(cancel_button)
        
        # Return both embed and file (even if file is None)
        return rom_embed, cover_file
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle ROM selection"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        selected_rom_id = int(interaction.data['values'][0])
        self.selected_rom = next((rom for rom in self.matches if rom['id'] == selected_rom_id), None)
        
        if self.selected_rom:
            # Create full ROM embed - NOW UNPACKING TUPLE
            rom_embed, cover_file = await self.create_full_rom_view(self.selected_rom)
            
            # Edit the message with new embed and updated view, including file if available
            if cover_file:
                await interaction.response.edit_message(
                    content="✅ **This game is already available! You can download it now:**",
                    embed=rom_embed,
                    view=self,
                    file=cover_file
                )
            else:
                await interaction.response.edit_message(
                    content="✅ **This game is already available! You can download it now:**",
                    embed=rom_embed,
                    view=self
                )
    
    async def request_different_callback(self, interaction: discord.Interaction):
        """User wants to request a different version - show modal"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        # Show modal for variant input
        modal = VariantRequestModal(
            self.bot,
            self.platform_name,
            self.game_name,
            None,  # No original details from this flow
            [],    # No IGDB matches needed here
            interaction  # Pass the interaction as context
        )
        await interaction.response.send_modal(modal)
        self.stop()
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel the request"""
        self.action = "cancel"
        self.clear_items()
        cancelled_button = discord.ui.Button(
            label="Cancelled",
            style=discord.ButtonStyle.secondary,
            disabled=True
        )
        self.add_item(cancelled_button)
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        """Disable all components when the view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass  # Message was deleted or can't be edited
