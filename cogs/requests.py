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
from .search import ROM_View
from urllib.parse import quote
import aiohttp

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def is_admin():
    """Check if the user is the admin"""
    async def predicate(ctx: discord.ApplicationContext):
        return ctx.bot.is_admin(ctx.author)
    return commands.check(predicate)

class RequestAdminView(discord.ui.View):
    """Paginated view for managing requests"""
    
    def __init__(self, bot, requests_data, admin_id, db):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
        self.db = db
        self.requests = requests_data
        self.admin_id = admin_id
        self.current_index = 0
        self.message = None
        
        # Create buttons
        self.back_button = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.primary,
            disabled=True  # Start with back disabled on first page
        )
        self.back_button.callback = self.back_callback
        
        self.fulfill_button = discord.ui.Button(
            label="Fulfill",
            style=discord.ButtonStyle.success,
        )
        self.fulfill_button.callback = self.fulfill_callback
        
        self.reject_button = discord.ui.Button(
            label="Reject",
            style=discord.ButtonStyle.danger,
        )
        self.reject_button.callback = self.reject_callback
        
        self.forward_button = discord.ui.Button(
            label="Next →",
            style=discord.ButtonStyle.primary,
            disabled=len(requests_data) <= 1  # Disable if only one request
        )
        self.forward_button.callback = self.forward_callback
        
        # Add note button 
        self.note_button = discord.ui.Button(
            label="Add Note",
            style=discord.ButtonStyle.secondary,
        )
        self.note_button.callback = self.note_callback
                
        # Add all buttons
        self.add_item(self.back_button)
        self.add_item(self.fulfill_button)
        self.add_item(self.reject_button)
        self.add_item(self.forward_button)
        self.add_item(self.note_button)
        
        self.update_button_states()
    
    def update_button_states(self):
        """Update button states based on current index and request status"""
        if not self.requests:
            for item in self.children:
                item.disabled = True
            return
            
        # Navigation buttons
        self.back_button.disabled = self.current_index == 0
        self.forward_button.disabled = self.current_index >= len(self.requests) - 1
        
        # Action buttons - disable for non-pending requests
        current_request = self.requests[self.current_index]
        is_pending = current_request[6] == 'pending'
        self.fulfill_button.disabled = not is_pending
        self.reject_button.disabled = not is_pending
    
    def create_request_embed(self, req, user_avatar_url=None):
        """Create an embed for a request with status indication"""
        # Parse details for IGDB metadata
        details = req[5] if req[5] else ""
        game_data = {}
        cover_url = None
        igdb_name = req[4]  # Default to requested game name
        
        # Extract version request info if present
        version_request = None
        additional_notes = None
        if "Version Request:" in details:
            try:
                version_parts = details.split("Version Request: ", 1)[1].split("\n", 1)
                version_request = version_parts[0]
                if len(version_parts) > 1 and "Additional Notes:" in version_parts[1]:
                    additional_notes = version_parts[1].replace("Additional Notes: ", "").split("\n")[0]
            except:
                pass

        if "IGDB Metadata:" in details:
            try:
                metadata_lines = details.split("IGDB Metadata:\n")[1].split("\n")
                for line in metadata_lines:
                    if ": " in line:
                        key, value = line.split(": ", 1)
                        game_data[key] = value
                        if key == "Game":
                            igdb_name = value.split(" (", 1)[0]
                
                cover_matches = re.findall(r'Cover URL:\s*(https://[^\s]+)', details)
                if cover_matches:
                    cover_url = cover_matches[0]
            except Exception as e:
                logger.error(f"Error parsing metadata: {e}")
        
        # Determine status color
        status_colors = {
            'pending': discord.Color.yellow(),
            'fulfilled': discord.Color.green(),
            'cancelled': discord.Color.light_grey(),
            'reject': discord.Color.red()
        }
        
        # Create embed
        status = req[6].upper()
        embed = discord.Embed(
            title=f"{igdb_name}",
            color=status_colors.get(req[6], discord.Color.blue()),
        )
        
        # Add status indicator field at the top
        status_emoji = {
            'pending': '⏳',
            'fulfilled': '✅',
            'cancelled': '🚫',
            'reject': '❌'
        }.get(req[6], '❓')
        
        embed.add_field(
            name="Status",
            value=f"{status_emoji} **{req[6].title()}**",
            inline=True
        )
        
        # Platform field
        search_cog = self.bot.get_cog('Search')
        platform_display = req[3]
        if search_cog:
            platform_display = search_cog.get_platform_with_emoji(req[3])

        embed.add_field(
            name="Platform",
            value=platform_display,
            inline=True
        )
        
        # Request ID field
        embed.add_field(
            name="Request ID",
            value=f"#{req[0]}",
            inline=True
        )
        
        # If there's a version request, add it prominently
        if version_request:
            embed.add_field(
                name="Version Requested",
                value=version_request[:1024],
                inline=False
            )
        
        if additional_notes:
            embed.add_field(
                name="Additional Notes from User",
                value=additional_notes[:1024],
                inline=False
            )
        
        # Set images
        if cover_url and cover_url != 'None':
            embed.set_image(url=cover_url)
        
        # Set user avatar as thumbnail
        if user_avatar_url:
            embed.set_thumbnail(url=user_avatar_url)
        else:
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        # Genre field if available
        if "Genres" in game_data and game_data["Genres"] != "Unknown":
            genres = game_data["Genres"].split(", ")[:2]
            embed.add_field(
                name="Genre",
                value=", ".join(genres),
                inline=True
            )
        
        # Release Date if available
        if "Release Date" in game_data and game_data["Release Date"] != "Unknown":
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
        
        # Companies if available
        companies = []
        if "Developers" in game_data and game_data["Developers"] != "Unknown":
            developers = game_data["Developers"].split(", ")[:2]
            companies.extend(developers)
        if "Publishers" in game_data and game_data["Publishers"] != "Unknown":
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
        
        # Summary if available
        if "Summary" in game_data:
            summary = game_data["Summary"]
            if len(summary) > 500:
                summary = summary[:497] + "..."
            embed.add_field(
                name="Summary",
                value=summary,
                inline=False
            )
        
        # Admin notes if present
        if req[11]:  # notes field
            embed.add_field(
                name="Admin Notes",
                value=req[11][:1024],
                inline=False
            )
        
        # Fulfillment info if fulfilled/rejected
        if req[9]:  # fulfilled_by
            action = "Fulfilled" if req[6] == 'fulfilled' else "Rejected"
            embed.add_field(
                name=f"✍️ {action} By",
                value=req[10],  # fulfiller_name
                inline=True
            )
            
        # Auto-fulfilled indicator
        if req[12]:  # auto_fulfilled
            embed.add_field(
                name="🤖 Auto-Fulfilled",
                value="Yes",
                inline=True
            )
        
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
        
        # Footer with requester info and pagination
        total = len(self.requests)
        embed.set_footer(
            text=f"Request {self.current_index + 1}/{total} • Requested by {req[2]} • Use buttons to navigate"
        )
        
        return embed
    
    async def back_callback(self, interaction: discord.Interaction):
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        if self.current_index > 0:
            self.current_index -= 1
            self.update_button_states()
            
            # Fetch user avatar
            user_avatar_url = None
            try:
                user = self.bot.get_user(self.requests[self.current_index][1])
                if not user:
                    user = await self.bot.fetch_user(self.requests[self.current_index][1])
                if user and user.avatar:
                    user_avatar_url = user.avatar.url
                elif user:
                    user_avatar_url = user.default_avatar.url
            except:
                pass
            
            embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def forward_callback(self, interaction: discord.Interaction):
        """Navigate to next request"""
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        if self.current_index < len(self.requests) - 1:
            self.current_index += 1
            self.update_button_states()
            
            # Fetch user avatar
            user_avatar_url = None
            try:
                user = self.bot.get_user(self.requests[self.current_index][1])
                if not user:
                    user = await self.bot.fetch_user(self.requests[self.current_index][1])
                if user and user.avatar:
                    user_avatar_url = user.avatar.url
                elif user:
                    user_avatar_url = user.default_avatar.url
            except:
                pass
            
            embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def fulfill_callback(self, interaction: discord.Interaction):
        """Mark current request as fulfilled"""
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        current_request = self.requests[self.current_index]
        request_id = current_request[0]
        
        try:
            async with self.db.get_connection() as db:
                await db.execute(
                    """
                    UPDATE requests 
                    SET status = 'fulfilled', 
                        fulfilled_by = ?, 
                        fulfiller_name = ?, 
                        updated_at = CURRENT_TIMESTAMP 
                    WHERE id = ?
                    """,
                    (interaction.user.id, str(interaction.user), request_id)
                )
                await db.commit()
                
                logger.info(f"Request fulfilled manually - Admin: {interaction.user} | Request ID: #{request_id} | Discord: {current_request[2]} (ID: {current_request[1]}) | Game: '{current_request[4]}' | Platform: {current_request[3]}")
                
                # Get all subscribers for this request
                cursor = await db.execute(
                    "SELECT user_id FROM request_subscribers WHERE request_id = ?",
                    (request_id,)
                )
                subscribers = await cursor.fetchall()
                
                # Notify original requester
                try:
                    # Prioritize the stored IGDB name, fall back to the user's requested name
                    igdb_game_name = current_request[15] if len(current_request) > 15 else None
                    display_game_name = igdb_game_name if igdb_game_name else current_request[4]

                    user = await self.bot.fetch_user(current_request[1])
                    await user.send(f"✅ Your request for '{display_game_name}' has been fulfilled!")
                except:
                    logger.warning(f"Could not DM user {current_request[1]}")
            
            # Update the request in our list
            updated_request = list(current_request)
            updated_request[6] = 'fulfilled'
            updated_request[9] = interaction.user.id
            updated_request[10] = str(interaction.user)
            self.requests[self.current_index] = tuple(updated_request)
            
            # Update view
            self.update_button_states()
            
            # Fetch user avatar
            user_avatar_url = None
            try:
                user = self.bot.get_user(self.requests[self.current_index][1])
                if not user:
                    user = await self.bot.fetch_user(self.requests[self.current_index][1])
                if user and user.avatar:
                    user_avatar_url = user.avatar.url
                elif user:
                    user_avatar_url = user.default_avatar.url
            except:
                pass
            
            embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
            await interaction.followup.edit_message(message_id=self.message.id, embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Error fulfilling request: {e}")
            await interaction.followup.send("❌ An error occurred while fulfilling the request.", ephemeral=True)
    
    async def reject_callback(self, interaction: discord.Interaction):
        """Show modal for rejection reason then reject"""
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        current_request = self.requests[self.current_index]
        
        class RejectModal(discord.ui.Modal):
            def __init__(self, view, request_data, db):
                super().__init__(title="Reject Request")
                self.view = view
                self.request_data = request_data
                self.db = db
                
                self.reason = discord.ui.InputText(
                    label="Rejection Reason",
                    placeholder="Enter reason for rejection (optional)",
                    style=discord.InputTextStyle.long,
                    required=False,
                    max_length=500
                )
                self.add_item(self.reason)
            
            async def callback(self, modal_interaction: discord.Interaction):
                await modal_interaction.response.defer()
                
                request_id = self.request_data[0]
                reason = self.reason.value or None
                
                try:
                    async with self.db.get_connection() as db:
                        await db.execute(
                            """
                            UPDATE requests 
                            SET status = 'reject', 
                                fulfilled_by = ?, 
                                fulfiller_name = ?, 
                                notes = ?,
                                updated_at = CURRENT_TIMESTAMP 
                            WHERE id = ?
                            """,
                            (modal_interaction.user.id, str(modal_interaction.user), reason, request_id)
                        )
                        await db.commit()
                        
                        logger.info(f"Request rejected - Admin: {modal_interaction.user} | Request ID: #{request_id} | Discord: {self.request_data[2]} (ID: {self.request_data[1]}) | Game: '{self.request_data[4]}' | Platform: {self.request_data[3]} | Reason: {reason or 'No reason provided'}")
                        
                        # Notify user
                        try:
                            # Prioritize the stored IGDB name, fall back to the user's requested name
                            igdb_game_name = self.request_data[15] if len(self.request_data) > 15 else None
                            display_game_name = igdb_game_name if igdb_game_name else self.request_data[4]

                            user = await self.view.bot.fetch_user(self.request_data[1])
                            message = f"❌ Your request for '{display_game_name}' has been rejected."
                            if reason:
                                message += f"\nReason: {reason}"
                            await user.send(message)
                        except:
                            logger.warning(f"Could not DM user {self.request_data[1]}")
                    
                    # Update the request in our list
                    updated_request = list(self.request_data)
                    updated_request[6] = 'reject'
                    updated_request[9] = modal_interaction.user.id
                    updated_request[10] = str(modal_interaction.user)
                    updated_request[11] = reason
                    self.view.requests[self.view.current_index] = tuple(updated_request)
                    
                    # Update view
                    self.view.update_button_states()
                    embed = self.view.create_request_embed(self.view.requests[self.view.current_index])
                    await modal_interaction.followup.edit_message(
                        message_id=self.view.message.id, 
                        embed=embed, 
                        view=self.view
                    )
                    
                except Exception as e:
                    logger.error(f"Error rejecting request: {e}")
                    await modal_interaction.followup.send(
                        "❌ An error occurred while rejecting the request.", 
                        ephemeral=True
                    )
        
        modal = RejectModal(self, current_request, self.db)
        await interaction.response.send_modal(modal)
    
    async def note_callback(self, interaction: discord.Interaction):
        """Add a note to the current request"""
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        current_request = self.requests[self.current_index]
        
        class NoteModal(discord.ui.Modal):
            def __init__(self, view, request_data, db):
                super().__init__(title="Add Note to Request")
                self.view = view
                self.request_data = request_data
                self.db = db
                
                # Show current note if exists
                current_note = request_data[11] or ""
                self.note = discord.ui.InputText(
                    label="Note",
                    placeholder="Enter note for this request",
                    style=discord.InputTextStyle.long,
                    required=True,
                    max_length=500,
                    value=current_note
                )
                self.add_item(self.note)
            
            async def callback(self, modal_interaction: discord.Interaction):
                await modal_interaction.response.defer()
                
                request_id = self.request_data[0]
                note = self.note.value
                
                try:
                    async with self.view.db.get_connection() as db:
                        await db.execute(
                            "UPDATE requests SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (note, request_id)
                        )
                        await db.commit()
                    
                    # Update the request in our list
                    updated_request = list(self.request_data)
                    updated_request[11] = note
                    self.view.requests[self.view.current_index] = tuple(updated_request)
                    
                    # Update view
                    embed = self.view.create_request_embed(self.view.requests[self.view.current_index])
                    await modal_interaction.followup.edit_message(
                        message_id=self.view.message.id,
                        embed=embed,
                        view=self.view
                    )
                    
                except Exception as e:
                    logger.error(f"Error adding note: {e}")
                    await modal_interaction.followup.send(
                        "❌ An error occurred while adding the note.",
                        ephemeral=True
                    )
        
        modal = NoteModal(self, current_request, self.db)
        await interaction.response.send_modal(modal)
    
    async def refresh_callback(self, interaction: discord.Interaction):
        """Refresh the requests list from database"""
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    "SELECT * FROM requests ORDER BY created_at DESC"
                )
                self.requests = await cursor.fetchall()
            
            # Reset to first page if current index is out of bounds
            if self.current_index >= len(self.requests):
                self.current_index = 0
            
            self.update_button_states()
            
            if self.requests:
                # Fetch user avatar for current request
                user_avatar_url = None
                try:
                    user = self.bot.get_user(self.requests[self.current_index][1])
                    if not user:
                        user = await self.bot.fetch_user(self.requests[self.current_index][1])
                    if user and user.avatar:
                        user_avatar_url = user.avatar.url
                    elif user:
                        user_avatar_url = user.default_avatar.url
                except:
                    pass
                
                embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
                await interaction.followup.edit_message(
                    message_id=self.message.id,
                    content=None,
                    embed=embed,
                    view=self
                )
            else:
                embed = discord.Embed(
                    title="No Requests",
                    description="There are currently no requests in the system.",
                    color=discord.Color.light_grey()
                )
                await interaction.followup.edit_message(
                    message_id=self.message.id,
                    content=None,
                    embed=embed,
                    view=self
                )
                
        except Exception as e:
            logger.error(f"Error refreshing requests: {e}")
            await interaction.followup.send(
                "❌ An error occurred while refreshing the requests.",
                ephemeral=True
            )

class UserRequestsView(discord.ui.View):
    """Paginated view for users to manage their own requests"""
    
    def __init__(self, bot, requests_data, user_id, db):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
        self.requests = requests_data
        self.user_id = user_id
        self.current_index = 0
        self.message = None
        self.db = db
        
        # Create buttons
        self.back_button = discord.ui.Button(
            label="← Back",
            style=discord.ButtonStyle.primary,
            disabled=True  # Start with back disabled on first page
        )
        self.back_button.callback = self.back_callback
        
        self.cancel_button = discord.ui.Button(
            label="Cancel Request",
            style=discord.ButtonStyle.danger,
        )
        self.cancel_button.callback = self.cancel_callback
        
        self.note_button = discord.ui.Button(
            label="Add Note",
            style=discord.ButtonStyle.secondary,
        )
        self.note_button.callback = self.note_callback
        
        self.forward_button = discord.ui.Button(
            label="Next →",
            style=discord.ButtonStyle.primary,
            disabled=len(requests_data) <= 1  # Disable if only one request
        )
        self.forward_button.callback = self.forward_callback
        
                
        # Add all buttons
        self.add_item(self.back_button)
        self.add_item(self.cancel_button)
        self.add_item(self.note_button)
        self.add_item(self.forward_button)
        
        self.update_button_states()
    
    def update_button_states(self):
        """Update button states based on current index and request status"""
        if not self.requests:
            for item in self.children:
                item.disabled = True
            return
            
        # Navigation buttons
        self.back_button.disabled = self.current_index == 0
        self.forward_button.disabled = self.current_index >= len(self.requests) - 1
        
        # Action buttons - disable cancel for non-pending requests
        current_request = self.requests[self.current_index]
        is_pending = current_request[6] == 'pending'
        self.cancel_button.disabled = not is_pending
        
        # Update cancel button label and style based on status
        if not is_pending:
            if current_request[6] == 'fulfilled':
                self.cancel_button.label = "Fulfilled"
                self.cancel_button.style = discord.ButtonStyle.success  # Green
            elif current_request[6] == 'reject':
                self.cancel_button.label = "Rejected"
                self.cancel_button.style = discord.ButtonStyle.danger  # Red
            elif current_request[6] == 'cancelled':
                self.cancel_button.label = "Cancelled"
                self.cancel_button.style = discord.ButtonStyle.danger  # Red
            else:
                self.cancel_button.label = "Not Available"
                self.cancel_button.style = discord.ButtonStyle.secondary  # Gray
        else:
            self.cancel_button.label = "Cancel Request"
            self.cancel_button.style = discord.ButtonStyle.danger  # Red for cancel action
    
    def create_request_embed(self, req, user_avatar_url=None):
        """Create an embed for a request with status indication and platform status"""
        # Parse details for IGDB metadata
        details = req[5] if req[5] else ""
        game_data = {}
        cover_url = None
        igdb_name = req[4]  # Default to requested game name
        
        # Extract version request info if present
        version_request = None
        additional_notes = None
        if "Version Request:" in details:
            try:
                version_parts = details.split("Version Request: ", 1)[1].split("\n", 1)
                version_request = version_parts[0]
                if len(version_parts) > 1 and "Additional Notes:" in version_parts[1]:
                    additional_notes = version_parts[1].replace("Additional Notes: ", "").split("\n")[0]
            except:
                pass

        if "IGDB Metadata:" in details:
            try:
                metadata_lines = details.split("IGDB Metadata:\n")[1].split("\n")
                for line in metadata_lines:
                    if ": " in line:
                        key, value = line.split(": ", 1)
                        game_data[key] = value
                        if key == "Game":
                            igdb_name = value.split(" (", 1)[0]
                
                cover_matches = re.findall(r'Cover URL:\s*(https://[^\s]+)', details)
                if cover_matches:
                    cover_url = cover_matches[0]
            except Exception as e:
                logger.error(f"Error parsing metadata: {e}")
        
        # Determine status color
        status_colors = {
            'pending': discord.Color.yellow(),
            'fulfilled': discord.Color.green(),
            'cancelled': discord.Color.light_grey(),
            'reject': discord.Color.red()
        }
        
        # Create embed
        status = req[6].upper()
        embed = discord.Embed(
            title=f"{igdb_name}",
            color=status_colors.get(req[6], discord.Color.blue()),
        )
        
        # Add status indicator field at the top
        status_emoji = {
            'pending': '⏳',
            'fulfilled': '✅',
            'cancelled': '🚫',
            'reject': '❌'
        }.get(req[6], '❓')
        
        embed.add_field(
            name="Status",
            value=f"{status_emoji} **{req[6].title()}**",
            inline=True
        )
        
        # Platform field with existence check
        search_cog = self.bot.get_cog('Search')
        platform_display = req[3]
        platform_exists_in_romm = False
        
        # Check if platform exists in Romm
        try:
            # First check the platform_mapping_id if it exists (assuming column 13)
            # Adjust index based on your actual database schema
            platform_mapping_id = req[13] if len(req) > 13 else None
            
            if platform_mapping_id:
                # Check platform status from mapping
                import asyncio
                loop = asyncio.get_event_loop()
                
                async def check_platform():
                    from pathlib import Path
                    import aiosqlite
                    async with self.db.get_connection() as db:
                        cursor = await db.execute(
                            "SELECT in_romm FROM platform_mappings WHERE id = ?",
                            (platform_mapping_id,)
                        )
                        result = await cursor.fetchone()
                        return result[0] if result else False
                
                # Run async check in sync context
                future = asyncio.run_coroutine_threadsafe(check_platform(), loop)
                try:
                    platform_exists_in_romm = future.result(timeout=1)
                except:
                    # Fallback to checking via API
                    pass
            
            # Fallback: check via API if no mapping info
            if not platform_mapping_id:
                raw_platforms = asyncio.run_coroutine_threadsafe(
                    self.bot.fetch_api_endpoint('platforms'), 
                    loop
                ).result(timeout=2)
                
                if raw_platforms:
                    platform_lower = req[3].lower()
                    for p in raw_platforms:
                        custom_name = p.get('custom_name')
                        regular_name = p.get('name', '')
                        
                        if (custom_name and custom_name.lower() == platform_lower) or \
                           (regular_name.lower() == platform_lower):
                            platform_exists_in_romm = True
                            platform_display = self.bot.get_platform_display_name(p)
                            break
        except Exception as e:
            logger.debug(f"Could not check platform existence: {e}")
        
        # Format platform display with emoji and status
        if search_cog and platform_exists_in_romm:
            platform_display = search_cog.get_platform_with_emoji(platform_display)
        
        platform_status_icon = "✅" if platform_exists_in_romm else "🆕"
        
        embed.add_field(
            name="Platform",
            value=f"{platform_display} {platform_status_icon}",
            inline=True
        )
        
        # Request ID field
        embed.add_field(
            name="Request ID",
            value=f"#{req[0]}",
            inline=True
        )
        
        # If platform doesn't exist, add warning
        if not platform_exists_in_romm:
            embed.add_field(
                name="⚠️ Platform Status",
                value="This platform needs to be added to Romm before fulfillment",
                inline=False
            )
        
        # If there's a version request, add it prominently
        if version_request:
            embed.add_field(
                name="Version Requested",
                value=version_request[:1024],
                inline=False
            )
        
        if additional_notes:
            embed.add_field(
                name="Additional Notes from User",
                value=additional_notes[:1024],
                inline=False
            )
        
        # Set images
        if cover_url and cover_url != 'None':
            embed.set_image(url=cover_url)
        
        # Set user avatar as thumbnail
        if user_avatar_url:
            embed.set_thumbnail(url=user_avatar_url)
        else:
            embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
        
        # Genre field if available
        if "Genres" in game_data and game_data["Genres"] != "Unknown":
            genres = game_data["Genres"].split(", ")[:2]
            embed.add_field(
                name="Genre",
                value=", ".join(genres),
                inline=True
            )
        
        # Release Date if available
        if "Release Date" in game_data and game_data["Release Date"] != "Unknown":
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
        
        # Companies if available
        companies = []
        if "Developers" in game_data and game_data["Developers"] != "Unknown":
            developers = game_data["Developers"].split(", ")[:2]
            companies.extend(developers)
        if "Publishers" in game_data and game_data["Publishers"] != "Unknown":
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
        
        # Summary if available
        if "Summary" in game_data:
            summary = game_data["Summary"]
            if len(summary) > 500:
                summary = summary[:497] + "..."
            embed.add_field(
                name="Summary",
                value=summary,
                inline=False
            )
        
        # Admin notes if present
        if req[11]:  # notes field
            embed.add_field(
                name="Admin Notes",
                value=req[11][:1024],
                inline=False
            )
        
        # Fulfillment info if fulfilled/rejected
        if req[9]:  # fulfilled_by
            action = "Fulfilled" if req[6] == 'fulfilled' else "Rejected"
            embed.add_field(
                name=f"✍️ {action} By",
                value=req[10],  # fulfiller_name
                inline=True
            )
            
        # Auto-fulfilled indicator
        if req[12]:  # auto_fulfilled
            embed.add_field(
                name="🤖 Auto-Fulfilled",
                value="Yes",
                inline=True
            )
        
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
        
        # Footer with requester info and pagination
        total = len(self.requests)
        embed.set_footer(
            text=f"Request {self.current_index + 1}/{total} • Requested by {req[2]} • Use buttons to navigate"
        )
        
        return embed
    
    async def back_callback(self, interaction: discord.Interaction):
        """Navigate to previous request"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        if self.current_index > 0:
            self.current_index -= 1
            self.update_button_states()
            
            # Fetch user avatar
            user_avatar_url = None
            try:
                user = self.bot.get_user(self.requests[self.current_index][1])
                if not user:
                    user = await self.bot.fetch_user(self.requests[self.current_index][1])
                if user and user.avatar:
                    user_avatar_url = user.avatar.url
                elif user:
                    user_avatar_url = user.default_avatar.url
            except:
                pass
            
            embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def forward_callback(self, interaction: discord.Interaction):
        """Navigate to next request"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        if self.current_index < len(self.requests) - 1:
            self.current_index += 1
            self.update_button_states()
            
            # Fetch user avatar
            user_avatar_url = None
            try:
                user = self.bot.get_user(self.requests[self.current_index][1])
                if not user:
                    user = await self.bot.fetch_user(self.requests[self.current_index][1])
                if user and user.avatar:
                    user_avatar_url = user.avatar.url
                elif user:
                    user_avatar_url = user.default_avatar.url
            except:
                pass
            
            embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel the current pending request"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        current_request = self.requests[self.current_index]
        
        if current_request[6] != 'pending':
            await interaction.response.send_message("Only pending requests can be cancelled.", ephemeral=True)
            return
        
        # Show confirmation modal
        class CancelConfirmModal(discord.ui.Modal):
            def __init__(self, view, request_data):
                super().__init__(title="Cancel Request")
                self.view = view
                self.request_data = request_data
                
                self.reason = discord.ui.InputText(
                    label="Cancellation Reason (Optional)",
                    placeholder="Why are you cancelling this request?",
                    style=discord.InputTextStyle.long,
                    required=False,
                    max_length=500
                )
                self.add_item(self.reason)
            
            async def callback(self, modal_interaction: discord.Interaction):
                await modal_interaction.response.defer()
                
                request_id = self.request_data[0]
                reason = self.reason.value or "User cancelled"
                
                try:
                    async with self.view.db.get_connection() as db:
                        await db.execute(
                            """
                            UPDATE requests 
                            SET status = 'cancelled', 
                                notes = ?,
                                updated_at = CURRENT_TIMESTAMP 
                            WHERE id = ?
                            """,
                            (reason, request_id)
                        )
                        await db.commit()
                    
                    # Update the request in our list
                    updated_request = list(self.request_data)
                    updated_request[6] = 'cancelled'
                    updated_request[11] = reason
                    self.view.requests[self.view.current_index] = tuple(updated_request)
                    
                    # Update button states
                    self.view.update_button_states()
                    
                    # Fetch user avatar
                    user_avatar_url = None
                    try:
                        user = self.view.bot.get_user(self.view.requests[self.view.current_index][1])
                        if not user:
                            user = await self.view.bot.fetch_user(self.view.requests[self.view.current_index][1])
                        if user and user.avatar:
                            user_avatar_url = user.avatar.url
                        elif user:
                            user_avatar_url = user.default_avatar.url
                    except:
                        pass
                    
                    # Update view with cancelled status
                    embed = self.view.create_request_embed(self.view.requests[self.view.current_index], user_avatar_url)
                    await modal_interaction.followup.edit_message(
                        message_id=self.view.message.id,
                        embed=embed,
                        view=self.view
                    )
                    
                    # Send confirmation message
                    await modal_interaction.followup.send(
                        f"✅ Request #{request_id} has been cancelled.",
                        ephemeral=True
                    )
                    
                except Exception as e:
                    logger.error(f"Error cancelling request: {e}")
                    await modal_interaction.followup.send(
                        "❌ An error occurred while cancelling the request.",
                        ephemeral=True
                    )
        
        modal = CancelConfirmModal(self, current_request)
        await interaction.response.send_modal(modal)
    
    async def note_callback(self, interaction: discord.Interaction):
        """Add or edit a note on the current request"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        current_request = self.requests[self.current_index]
        
        class NoteModal(discord.ui.Modal):
            def __init__(self, view, request_data):
                super().__init__(title="Add/Edit Note")
                self.view = view
                self.request_data = request_data
                
                # Show current note if exists
                current_note = request_data[11] or ""
                self.note = discord.ui.InputText(
                    label="Your Note",
                    placeholder="Add any additional information about this request",
                    style=discord.InputTextStyle.long,
                    required=False,
                    max_length=500,
                    value=current_note
                )
                self.add_item(self.note)
            
            async def callback(self, modal_interaction: discord.Interaction):
                await modal_interaction.response.defer()
                
                request_id = self.request_data[0]
                note = self.note.value
                
                try:
                    async with self.view.db.get_connection() as db:
                        await db.execute(
                            "UPDATE requests SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (note, request_id)
                        )
                        await db.commit()
                    
                    # Update the request in our list
                    updated_request = list(self.request_data)
                    updated_request[11] = note
                    self.view.requests[self.view.current_index] = tuple(updated_request)
                    
                    # Fetch user avatar
                    user_avatar_url = None
                    try:
                        user = self.view.bot.get_user(self.view.requests[self.view.current_index][1])
                        if not user:
                            user = await self.view.bot.fetch_user(self.view.requests[self.view.current_index][1])
                        if user and user.avatar:
                            user_avatar_url = user.avatar.url
                        elif user:
                            user_avatar_url = user.default_avatar.url
                    except:
                        pass
                    
                    # Update view
                    embed = self.view.create_request_embed(self.view.requests[self.view.current_index], user_avatar_url)
                    await modal_interaction.followup.edit_message(
                        message_id=self.view.message.id,
                        embed=embed,
                        view=self.view
                    )
                    
                    # Send confirmation
                    await modal_interaction.followup.send(
                        "✅ Note updated successfully.",
                        ephemeral=True
                    )
                    
                except Exception as e:
                    logger.error(f"Error adding note: {e}")
                    await modal_interaction.followup.send(
                        "❌ An error occurred while adding the note.",
                        ephemeral=True
                    )
        
        modal = NoteModal(self, current_request)
        await interaction.response.send_modal(modal)
    
    async def refresh_callback(self, interaction: discord.Interaction):
        """Refresh the requests list from database"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    "SELECT * FROM requests WHERE user_id = ? ORDER BY created_at DESC",
                    (self.user_id,)
                )
                self.requests = await cursor.fetchall()
            
            # Reset to first page if current index is out of bounds
            if self.current_index >= len(self.requests):
                self.current_index = 0
            
            self.update_button_states()
            
            if self.requests:
                embed = self.create_request_embed(self.requests[self.current_index])
                await interaction.followup.edit_message(
                    message_id=self.message.id,
                    embed=embed,
                    view=self
                )
            else:
                embed = discord.Embed(
                    title="No Requests",
                    description="You haven't made any requests yet.",
                    color=discord.Color.light_grey()
                )
                await interaction.followup.edit_message(
                    message_id=self.message.id,
                    embed=embed,
                    view=self
                )
                
        except Exception as e:
            logger.error(f"Error refreshing requests: {e}")
            await interaction.followup.send(
                "❌ An error occurred while refreshing your requests.",
                ephemeral=True
            )

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
        super().__init__()
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

class ExistingGameWithIGDBView(discord.ui.View):
    """View that combines existing game selection with IGDB matching"""
    
    def __init__(self, bot, existing_matches, igdb_matches, platform_name, game_name, author_id):
        super().__init__(timeout=180)
        self.bot = bot
        self.existing_matches = existing_matches
        self.platform_name = platform_name
        self.game_name = game_name
        self.author_id = author_id
        self.selected_rom = None
        self.selected_igdb = None
        self.message = None
        
        # Filter IGDB matches to remove games that already exist
        self.filtered_igdb_matches = self._filter_igdb_matches(existing_matches, igdb_matches)
        
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
    
    def _filter_igdb_matches(self, existing_roms, igdb_matches):
        """Filter out IGDB games that already exist in the collection"""
        if not igdb_matches:
            return []
        
        filtered = []
        
        for igdb_game in igdb_matches:
            igdb_name = igdb_game.get('name', '').lower()
            
            # Normalize IGDB name for comparison
            import re
            igdb_normalized = re.sub(r'[:\-\s]+', ' ', igdb_name).strip()
            
            # Check if this IGDB game matches any existing ROM
            is_existing = False
            for rom in existing_roms:
                rom_name = rom.get('name', '').lower()
                rom_normalized = re.sub(r'[:\-\s]+', ' ', rom_name).strip()
                
                # Check for high similarity or exact match
                if (igdb_normalized == rom_normalized or 
                    self._calculate_similarity(igdb_normalized, rom_normalized) > 0.85):
                    is_existing = True
                    break
            
            # Only include if it doesn't match any existing game
            if not is_existing:
                filtered.append(igdb_game)
        
        return filtered
    
    def _calculate_similarity(self, str1: str, str2: str) -> float:
        """Simple similarity calculation (you can reuse the one from Request cog)"""
        if not str1 or not str2:
            return 0.0
        
        # Remove common words
        common_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to'}
        words1 = set(word for word in str1.lower().split() if word not in common_words)
        words2 = set(word for word in str2.lower().split() if word not in common_words)
        
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        return len(intersection) / len(union) if union else 0.0
    
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
            await request_cog.process_request(
                interaction,
                self.platform_name,
                self.selected_igdb['name'],  # Use IGDB name
                None,  # No additional details
                self.selected_igdb,
                self.message
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
        
        from .search import ROM_View
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
        
        from .requests import VariantRequestModal
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

class ExistingGameView(discord.ui.View):
    """View for when requested games already exist in the collection"""
    
    def __init__(self, bot, matches, platform_name, game_name, author_id):
        super().__init__()
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
        from .search import ROM_View
        
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
            if isinstance(item, discord.ui.Select) and item.custom_id == "rom_file_select":
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
            self.add_item(file_select)
        
        # Add all three buttons on the same row
        # Add download button(s)
        for item in rom_view.children:
            if isinstance(item, discord.ui.Button) and "Download" in item.label:
                item.row = button_row
                self.add_item(item)
                break
        
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


class Request(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.igdb: Optional[IGDBClient] = None
        
        # Use master db
        self.db = bot.db
        
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
        try:
            # Initialize IGDB client
            self.igdb = IGDBClient()
            
            # Sync Romm platforms (mappings already initialized by database)
            await self.sync_romm_platforms()
            
            logger.debug("Request cog setup completed successfully")
        except ValueError as e:
            logger.warning(f"IGDB integration disabled: {e}")
            self.igdb = None
            # Still try to sync platforms even without IGDB
            try:
                await self.sync_romm_platforms()
            except Exception as sync_error:
                logger.error(f"Failed to sync platforms: {sync_error}")
            
    async def _get_canonical_platform_name(self, platform_name: str) -> Optional[str]:
        """Looks up the canonical display_name for a given platform alias."""
        if not platform_name:
            return None
        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    """SELECT display_name FROM platform_mappings 
                       WHERE LOWER(display_name) = LOWER(?) OR LOWER(folder_name) = LOWER(?)
                       LIMIT 1""",
                    (platform_name, platform_name)
                )
                result = await cursor.fetchone()
                return result[0] if result else platform_name
        except Exception:
            # If DB fails, fallback to using the original name
            return platform_name
    
    @property
    def igdb_enabled(self) -> bool:
        """Check if IGDB integration is enabled"""
        return self.igdb is not None

    async def sync_romm_platforms(self):
        """Sync current Romm platforms with master list"""
        try:
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                return
            
            async with self.db.get_connection() as db:
                for platform in raw_platforms:
                    display_name = self.bot.get_platform_display_name(platform)
                    platform_name = platform.get('name', '').lower()
                    
                    # Try to match by name or folder patterns
                    cursor = await db.execute('''
                        SELECT id FROM platform_mappings 
                        WHERE LOWER(display_name) = LOWER(?)
                        OR LOWER(folder_name) = LOWER(?)
                        OR LOWER(folder_name) = LOWER(?)
                        LIMIT 1
                    ''', (display_name, platform_name, platform_name.replace(' ', '-')))
                    
                    result = await cursor.fetchone()
                    
                    if result:
                        # Update the mapping to show it exists in Romm
                        await db.execute('''
                            UPDATE platform_mappings 
                            SET in_romm = 1, romm_id = ?
                            WHERE id = ?
                        ''', (platform['id'], result[0]))
                
                await db.commit()
                
        except Exception as e:
            logger.error(f"Error syncing Romm platforms: {e}")
    
    async def platform_autocomplete_all(self, ctx: discord.AutocompleteContext):
        """Autocomplete for all platforms, not just those in Romm"""
        try:
            user_input = ctx.value.lower()
            
            # Use the master database's connection method
            async with self.db.get_connection() as db:
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
            
            # Process results
            choices = []
            for display_name, in_romm, folder_name in results:
                if in_romm:
                    label = display_name
                else:
                    label = f"[+] {display_name}"
                
                choices.append(discord.OptionChoice(
                    name=label[:100],
                    value=display_name
                ))
            
            return choices
            
        except Exception as e:
            logger.error(f"Error in platform autocomplete: {e}")
            return []
    
    async def check_if_game_exists(self, platform: str, game_name: str) -> Tuple[bool, List[Dict]]:
        """Check if a game already exists in the database"""
        try:
            # Get raw platforms data to access custom_name field
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                return False, []

            # Find platform by name (including custom names)
            platform_id = None
            platform_lower = platform.lower()
            
            for p in raw_platforms:
                # Check custom name first
                custom_name = p.get('custom_name')
                if custom_name and custom_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    break
                
                # Check regular name
                regular_name = p.get('name', '')
                if regular_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    break

            if not platform_id:
                return False, []

            # Search for the game
            search_response = await self.bot.fetch_api_endpoint(
                f'roms?platform_id={platform_id}&search_term={game_name}&limit=25'
            )

            # Handle paginated response
            if search_response and isinstance(search_response, dict) and 'items' in search_response:
                search_results = search_response['items']
            elif search_response and isinstance(search_response, list):
                search_results = search_response
            else:
                search_results = []

            if not search_results:
                return False, []

            # Return all reasonably matching games
            matches = []
            game_name_lower = game_name.lower()
            for rom in search_results:
                rom_name = rom.get('name', '').lower()
                # Simple matching - let the user decide what they want
                if (game_name_lower in rom_name or 
                    rom_name in game_name_lower or
                    self.calculate_similarity(game_name_lower, rom_name) > 0.7):
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
        """Handle batch scan completion event with improved matching logic."""
        async with self.processing_lock:
            try:
                if not new_games:
                    return

                logger.info(f"Processing batch of {len(new_games)} new games")

                pending_requests = []
                all_subscribers = defaultdict(list)
                
                async with self.db.get_connection() as db:
                    cursor = await db.execute("SELECT * FROM requests WHERE status = 'pending'")
                    pending_requests = await cursor.fetchall()
                    
                    if not pending_requests:
                        return
                    
                    request_ids = [req[0] for req in pending_requests]
                    placeholders = ','.join('?' * len(request_ids))
                    cursor = await db.execute(
                        f"SELECT request_id, user_id FROM request_subscribers WHERE request_id IN ({placeholders})",
                        request_ids
                    )
                    for req_id, user_id in await cursor.fetchall():
                        all_subscribers[req_id].append(user_id)
                
                fulfillments = []
                notifications = defaultdict(list)
                
                for req in pending_requests:
                    # Unpack the full request tuple
                    req_id, user_id, _, req_platform, req_game, _, _, _, _, _, _, _, _, req_igdb_id, _, req_igdb_game_name = req

                    for new_game in new_games:
                        new_game_igdb_id = new_game.get('igdb_id')

                        # NORMALIZE PLATFORM NAMES ---
                        canonical_req_platform = await self._get_canonical_platform_name(req_platform)
                        canonical_new_game_platform = await self._get_canonical_platform_name(new_game['platform'])
                        platform_match = canonical_req_platform and (canonical_req_platform == canonical_new_game_platform)
                        
                        # PRIORITIZE IGDB NAME FOR SIMILARITY CHECK ---
                        name_to_compare = req_igdb_game_name if req_igdb_game_name else req_game
                        name_match = self.calculate_similarity(name_to_compare, new_game['name']) > 0.8
                        
                        igdb_match = req_igdb_id is not None and new_game_igdb_id is not None and req_igdb_id == new_game_igdb_id

                        if platform_match and (igdb_match or name_match):
                            fulfillments.append({'req_id': req_id, 'game_name': new_game['name']})
                            notifications[user_id].append(new_game)
                            for subscriber_id in all_subscribers.get(req_id, []):
                                notifications[subscriber_id].append(new_game)
                            logger.info(f"Request #{req_id} ('{req_game}') matched to new game '{new_game['name']}'.")
                            break 
                
                if fulfillments:
                    async with self.db.get_connection() as db:
                        await db.executemany(
                            """UPDATE requests 
                               SET status = 'fulfilled', updated_at = CURRENT_TIMESTAMP, 
                                   notes = ?, auto_fulfilled = 1
                               WHERE id = ?""",
                            [(f"Automatically fulfilled - Found: {f['game_name']}", f['req_id']) for f in fulfillments]
                        )
                        await db.commit()
                    
                    logger.info(f"Sending DMs with links for {len(notifications)} user(s).")
                    for user_id, fulfilled_games in notifications.items():
                        try:
                            user = await self.bot.fetch_user(user_id)
                            if not user: continue

                            # De-duplicate games in case user subscribed to multiple similar requests
                            unique_games = {g['id']: g for g in fulfilled_games}.values()

                            for game in unique_games:
                                game_name = game['name']
                                rom_id = game['id']
                                filename = game.get('fs_name') or game.get('file_name')
                                
                                romm_url = f"{self.bot.config.DOMAIN}/rom/{rom_id}"
                                
                                message_parts = [f"✅ Your request for **{game_name}** is now available!"]
                                link_parts = [f"[View on RomM]({romm_url})"]

                                if filename:
                                    safe_filename = quote(filename)
                                    download_url = f"{self.bot.config.DOMAIN}/api/roms/{rom_id}/content/{safe_filename}?"
                                    link_parts.append(f"[Direct Download]({download_url})")
                                
                                # Join the links with a separator and add them as a single line
                                message_parts.append(" • ".join(link_parts))

                                message = "\n".join(message_parts)
                                await user.send(message)
                                await asyncio.sleep(1)

                        except discord.Forbidden:
                            logger.warning(f"Could not notify user {user_id}: They have DMs disabled.")
                        except Exception as e:
                            logger.warning(f"Could not notify user {user_id}: {e}")
                    
            except Exception as e:
                logger.error(f"Error in batch scan completion handler: {e}", exc_info=True)

    async def check_pending_requests(self, platform: str, game_name: str) -> List[Tuple[int, int, str]]:
        """Check if there are any pending requests for this game"""
        try:
            async with self.db.get_connection() as db:
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

    async def process_request(self, ctx_or_interaction, platform_name, game, details, selected_game, message):
        """Process and save the request"""
        try:
            # Handle both ctx and interaction objects
            if hasattr(ctx_or_interaction, 'user'):
                author = ctx_or_interaction.user
                author_name = str(ctx_or_interaction.user)
                
                async def respond(content=None, embed=None, embeds=None):
                    kwargs = {}
                    if content is not None:
                        kwargs['content'] = content
                    if embed is not None:
                        kwargs['embed'] = embed
                    elif embeds is not None:
                        kwargs['embeds'] = embeds
                    
                    try:
                        if ctx_or_interaction.response.is_done():
                            return await ctx_or_interaction.followup.send(**kwargs)
                        else:
                            return await ctx_or_interaction.response.send_message(**kwargs)
                    except discord.errors.InteractionResponded:
                        return await ctx_or_interaction.followup.send(**kwargs)
            else:
                author = ctx_or_interaction.author
                author_name = str(ctx_or_interaction.author)
                
                async def respond(content=None, embed=None, embeds=None):
                    kwargs = {}
                    if content is not None:
                        kwargs['content'] = content
                    if embed is not None:
                        kwargs['embed'] = embed
                    elif embeds is not None:
                        kwargs['embeds'] = embeds
                        
                    return await ctx_or_interaction.respond(**kwargs)
            
            # FIRST: Gather all data from database
            existing_request_id = None
            user_already_requested = False
            pending_count = 0
            
            async with self.db.get_connection() as db:
                igdb_id = None
                if selected_game and selected_game.get('id'):
                    igdb_id = selected_game['id']
                # Get existing requests
                cursor = await db.execute(
                    """
                    SELECT id, user_id, username, game_name, igdb_id 
                    FROM requests 
                    WHERE platform = ? 
                    AND status = 'pending'
                    AND (igdb_id = ? OR igdb_id IS NULL)
                    """,
                    (platform_name, igdb_id)
                )
                existing_requests = await cursor.fetchall()
                
                # Check pending count
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM requests WHERE user_id = ? AND status = 'pending'",
                    (author.id,)
                )
                pending_count = (await cursor.fetchone())[0]
                
                # Check if there's a similar pending request
                existing_request_id = None
                user_already_requested = False
                
                for req_id, req_user_id, req_username, req_game, req_igdb_id in existing_requests:
                    # If IGDB IDs match, it's definitely the same game
                    if igdb_id and req_igdb_id and igdb_id == req_igdb_id:
                        existing_request_id = req_id
                        original_requester_id = req_user_id
                        original_requester_name = req_username
                        
                        if req_user_id == author.id:
                            user_already_requested = True
                            break
                    # Otherwise use name similarity
                    elif self.calculate_similarity(game.lower(), req_game.lower()) > 0.8:
                        existing_request_id = req_id
                        original_requester_id = req_user_id
                        original_requester_name = req_username
                        
                        if req_user_id == author.id:
                            user_already_requested = True
                            break
                    
                    # Check if user is already a subscriber
                    if existing_request_id:
                        cursor = await db.execute(
                            """
                            SELECT COUNT(*) FROM request_subscribers 
                            WHERE request_id = ? AND user_id = ?
                            """,
                            (req_id, author.id)
                        )
                        is_subscriber = (await cursor.fetchone())[0] > 0
                        
                        if is_subscriber:
                            user_already_requested = True
                        break
                    
                # If user has already requested this game
                if user_already_requested:
                    embed = discord.Embed(
                        title="📋 Already Requested",
                        description="You have already requested this game.",
                        color=discord.Color.orange()
                    )
                    
                    await respond(embed=embed)
                    return
                    
                if pending_count >= 25:
                    await respond(content="❌ You already have 25 pending requests...")
                    return
                    
                    search_cog = self.bot.get_cog('Search')
                    platform_display = platform_name
                    if search_cog:
                        platform_display = search_cog.get_platform_with_emoji(platform_name)
                    
                    embed.add_field(name="Game", value=game, inline=True)
                    embed.add_field(name="Platform", value=platform_display, inline=True)
                    embed.add_field(name="Request ID", value=f"#{existing_request_id}", inline=True)
                    embed.add_field(name="Status", value="⏳ Still Pending", inline=True)
                    
                    embed.set_footer(text="You'll receive a DM when this game is added to the collection")
                    
                    if selected_game and selected_game.get('cover_url'):
                        embed.set_thumbnail(url=selected_game['cover_url'])
                    else:
                        embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
                    
                    await respond(embed=embed)
                    return
                
                # If someone else has requested this game
                if existing_request_id and author.id != original_requester_id:
                    # Add user as a subscriber to existing request
                    await db.execute(
                        """
                        INSERT INTO request_subscribers (request_id, user_id, username)
                        VALUES (?, ?, ?)
                        """,
                        (existing_request_id, author.id, author_name)
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
                    platform_display = platform_name
                    if search_cog:
                        platform_display = search_cog.get_platform_with_emoji(platform_name)
                    
                    embed.add_field(name="Game", value=game, inline=True)
                    embed.add_field(name="Platform", value=platform_display, inline=True)
                    embed.add_field(name="Request ID", value=f"#{existing_request_id}", inline=True)
                    
                    embed.add_field(
                        name="✅ You've been added to the notification list",
                        value=f"You and {subscriber_count} other user(s) will be notified when this request is fulfilled.",
                        inline=False
                    )
                    
                    if selected_game and selected_game.get('cover_url'):
                        embed.set_thumbnail(url=selected_game['cover_url'])
                    else:
                        embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
                    
                    embed.set_footer(text="You'll receive a DM when this game is added to the collection")
                    
                    await respond(embed=embed)
                    return
                
                # Check user's pending request limit
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM requests WHERE user_id = ? AND status = 'pending'",
                    (author.id,)
                )
                pending_count = (await cursor.fetchone())[0]

                if pending_count >= 25:
                    await respond(content="❌ You already have 25 pending requests. Please wait for them to be fulfilled or cancel some.")
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
                        f"Cover URL: {selected_game.get('cover_url', 'None')}\n"
                    )
                    if details:
                        details = f"{details}\n\n{igdb_details}"
                    else:
                        details = igdb_details

                # Insert the new request
                async with self.db.get_connection() as db:
                    
                    cursor = await db.execute(
                        """
                        INSERT INTO requests (user_id, username, platform, game_name, details, igdb_id)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (author.id, author_name, platform_name, game, details, igdb_id)
                    )
                    await db.commit()
                    request_id = cursor.lastrowid
                    
                    logger.info(f"Request created - Discord: {author_name} (ID: {author.id}) | Game: '{game}' | Platform: {platform_name} | Request ID: #{request_id}")

                if message and selected_game:
                    view = GameSelectView(self.bot, matches=[selected_game], platform_name=platform_name)
                    embed = view.create_game_embed(selected_game)
                    embed.set_footer(text=f"Request #{request_id} submitted by {author_name}")
                    await message.edit(embed=embed)
                else:
                    # Create basic embed for manual submissions
                    embed = discord.Embed(
                        title=f"✅ Request Submitted",
                        description=f"Your request for **{game}** has been submitted!",
                        color=discord.Color.green()
                    )
                    
                    search_cog = self.bot.get_cog('Search')
                    platform_display = platform_name
                    if search_cog:
                        platform_display = search_cog.get_platform_with_emoji(platform_name)
                    
                    embed.add_field(name="Game", value=game, inline=True)
                    embed.add_field(name="Platform", value=platform_display, inline=True)
                    embed.add_field(name="Status", value="⏳ Pending", inline=True)
                    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)
                    
                    if details and "IGDB Metadata:" not in details:
                        embed.add_field(name="Details", value=details[:1024], inline=False)
                    
                    embed.set_footer(text=f"Request submitted by {author_name}")
                    embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
                    
                    await respond(embed=embed)

        except Exception as e:
            logger.error(f"Error processing request: {e}")
            try:
                if 'respond' in locals():
                    await respond(content="❌ An error occurred while processing the request.")
                else:
                    if hasattr(ctx_or_interaction, 'user'):
                        await ctx_or_interaction.followup.send("❌ An error occurred while processing the request.")
                    else:
                        await ctx_or_interaction.respond("❌ An error occurred while processing the request.")
            except Exception as error_e:
                logger.error(f"Could not send error message to user: {error_e}")
    
    async def process_request_with_platform(self, ctx_or_interaction, platform_display_name, 
                                       game, details, selected_game, message, 
                                       mapping_id, platform_exists):
        """Process and save the request with platform mapping"""
        try:
            # Handle both ctx and interaction objects
            if hasattr(ctx_or_interaction, 'user'):
                author = ctx_or_interaction.user
                author_name = str(ctx_or_interaction.user)
                
                async def respond(content=None, embed=None, embeds=None):
                    kwargs = {}
                    if content is not None:
                        kwargs['content'] = content
                    if embed is not None:
                        kwargs['embed'] = embed
                    elif embeds is not None:
                        kwargs['embeds'] = embeds
                    
                    try:
                        if ctx_or_interaction.response.is_done():
                            return await ctx_or_interaction.followup.send(**kwargs)
                        else:
                            return await ctx_or_interaction.response.send_message(**kwargs)
                    except discord.errors.InteractionResponded:
                        return await ctx_or_interaction.followup.send(**kwargs)
            else:
                author = ctx_or_interaction.author
                author_name = str(ctx_or_interaction.author)
                
                async def respond(content=None, embed=None, embeds=None):
                    kwargs = {}
                    if content is not None:
                        kwargs['content'] = content
                    if embed is not None:
                        kwargs['embed'] = embed
                    elif embeds is not None:
                        kwargs['embeds'] = embeds
                        
                    return await ctx_or_interaction.respond(**kwargs)
            
            async with self.db.get_connection() as db:
                # Extract IGDB ID if available
                igdb_id = None
                igdb_name = None
                if selected_game and selected_game.get('id'):
                    igdb_id = selected_game['id']
                    igdb_name = selected_game.get('name')
                
                # Check for existing pending request (only if platform exists in Romm)
                if platform_exists:
                    cursor = await db.execute(
                        """
                        SELECT id, user_id, username, game_name, igdb_id 
                        FROM requests 
                        WHERE platform = ? 
                        AND status = 'pending'
                        AND (igdb_id = ? OR igdb_id IS NULL)
                        """,
                        (platform_display_name, igdb_id)
                    )
                    existing_requests = await cursor.fetchall()
                    
                    # Check for existing/duplicate requests...
                    # (existing duplicate checking code)
                
                # Check user's pending request limit
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM requests WHERE user_id = ? AND status = 'pending'",
                    (author.id,)
                )
                pending_count = (await cursor.fetchone())[0]

                if pending_count >= 25:
                    await respond(content="❌ You already have 25 pending requests. Please wait for them to be fulfilled or cancel some.")
                    return

                # Add IGDB metadata to details if available
                if selected_game:
                    alt_names_str = ""
                    if selected_game.get('alternative_names'):
                        alt_names = [f"{alt['name']} ({alt['comment']})" if alt.get('comment') else alt['name'] 
                                   for alt in selected_game['alternative_names']]
                        alt_names_str = f"\nAlternative Names: {', '.join(alt_names)}"

                    igdb_details = (
                        f"IGDB Metadata:\n"
                        f"Game: {selected_game['name']}{alt_names_str}\n"
                        f"Release Date: {selected_game.get('release_date', 'Unknown')}\n"
                        f"Platforms: {', '.join(selected_game.get('platforms', []))}\n"
                        f"Developers: {', '.join(selected_game.get('developers', [])) if selected_game.get('developers') else 'Unknown'}\n"
                        f"Publishers: {', '.join(selected_game.get('publishers', [])) if selected_game.get('publishers') else 'Unknown'}\n"
                        f"Genres: {', '.join(selected_game.get('genres', [])) if selected_game.get('genres') else 'Unknown'}\n"
                        f"Game Modes: {', '.join(selected_game.get('game_modes', [])) if selected_game.get('game_modes') else 'Unknown'}\n"
                        f"Summary: {selected_game.get('summary', 'No summary available')}\n"
                        f"Cover URL: {selected_game.get('cover_url', 'None')}\n"
                    )
                    if details:
                        details = f"{details}\n\n{igdb_details}"
                    else:
                        details = igdb_details

                # Insert the new request with platform mapping
                cursor = await db.execute(
                    """
                    INSERT INTO requests (user_id, username, platform, game_name, details, igdb_id, platform_mapping_id, igdb_game_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (author.id, author_name, platform_display_name, game, details, igdb_id, mapping_id, igdb_name) # <-- ADD igdb_name HERE
                )
                await db.commit()
                
                request_id = cursor.lastrowid
                
                logger.info(f"Request created - Discord: {author_name} (ID: {author.id}) | Game: '{game}' | Platform: {platform_display_name} | Request ID: #{request_id}")

                if message and selected_game:
                    view = GameSelectView(self.bot, matches=[selected_game], platform_name=platform_display_name)
                    embed = view.create_game_embed(selected_game)
                    embed.set_footer(text=f"Request #{request_id} submitted by {author_name}")
                    
                    # Add platform status indicator if platform doesn't exist
                    if not platform_exists:
                        embed.add_field(
                            name="⚠️ Platform Status",
                            value="This platform needs to be added to the collection before this request can be fulfilled.",
                            inline=False
                        )
                    
                    await message.edit(embed=embed)
                else:
                    # Create basic embed for manual submissions
                    embed = discord.Embed(
                        title=f"✅ Request Submitted",
                        description=f"Your request for **{game}** has been submitted!",
                        color=discord.Color.green()
                    )
                    
                    search_cog = self.bot.get_cog('Search')
                    platform_display = platform_display_name
                    if search_cog and platform_exists:
                        platform_display = search_cog.get_platform_with_emoji(platform_display_name)
                    
                    platform_status = "✅ Available" if platform_exists else "🆕 Not Yet Added"
                    
                    embed.add_field(name="Game", value=game, inline=True)
                    embed.add_field(name="Platform", value=f"{platform_display}\n{platform_status}", inline=True)
                    embed.add_field(name="Status", value="⏳ Pending", inline=True)
                    embed.add_field(name="Request ID", value=f"#{request_id}", inline=True)
                    
                    if not platform_exists:
                        embed.add_field(
                            name="📝 Note",
                            value="This platform needs to be added to the collection before this request can be fulfilled.",
                            inline=False
                        )
                    
                    if details and "IGDB Metadata:" not in details:
                        embed.add_field(name="Details", value=details[:1024], inline=False)
                    
                    embed.set_footer(text=f"Request submitted by {author_name}")
                    embed.set_thumbnail(url="https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/isotipo-small.png")
                    
                    await respond(embed=embed)

        except Exception as e:
            logger.error(f"Error processing request: {e}")
            try:
                if 'respond' in locals():
                    await respond(content="❌ An error occurred while processing the request.")
                else:
                    if hasattr(ctx_or_interaction, 'user'):
                        await ctx_or_interaction.followup.send("❌ An error occurred while processing the request.")
                    else:
                        await ctx_or_interaction.respond("❌ An error occurred while processing the request.")
            except Exception as error_e:
                logger.error(f"Could not send error message to user: {error_e}")
        
    async def get_request_igdb_data(self, request_id: int) -> Optional[Dict]:
        """Retrieve IGDB data for a request if IGDB ID was stored"""
        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    "SELECT igdb_id, game_name, platform FROM requests WHERE id = ?",
                    (request_id,)
                )
                result = await cursor.fetchone()
                
                if result and result[0]:  # If IGDB ID exists
                    igdb_id = result[0]
                    game_name = result[1]
                    platform_name = result[2]
                    
                    # You can now use this IGDB ID for direct API calls
                    # For example, fetch fresh data from IGDB by ID
                    if self.igdb_enabled:
                        # This would require adding a method to IGDBClient to fetch by ID
                        # return await self.igdb.get_game_by_id(igdb_id)
                        return {"igdb_id": igdb_id, "game_name": game_name, "platform": platform_name}
                
                return None
                
        except Exception as e:
            logger.error(f"Error retrieving IGDB data for request: {e}")
            return None
    
    async def continue_request_flow(self, ctx, platform_display_name, game, details, igdb_matches):
        """Continue with the request flow when user wants different version"""
        
        if igdb_matches:
            # Show IGDB selection
            select_view = GameSelectView(self.bot, igdb_matches, platform_display_name)
            initial_embed = select_view.create_game_embed(igdb_matches[0])
            select_view.message = await ctx.followup.send(
                "Please select the correct game from the list below:",
                embed=initial_embed,
                view=select_view
            )
            
            await select_view.wait()
            
            if not select_view.selected_game:
                # Timeout
                return
            elif select_view.selected_game == "manual":
                selected_game = None
            else:
                selected_game = select_view.selected_game
                # Process request with IGDB data including ID
                await self.process_request(ctx, platform_display_name, game, details, selected_game, select_view.message)
                return
        
        # Process manual request (no IGDB ID)
        await self.process_request(ctx, platform_display_name, game, details, None, None)
    
    @discord.slash_command(name="request", description="Submit a ROM request")
    async def request(
        self,
        ctx: discord.ApplicationContext,
        platform: discord.Option(
            str,
            "Platform for the requested game (existing or new)",
            required=True,
            autocomplete=platform_autocomplete_all  # Use the new autocomplete
        ),
        game: discord.Option(str, "Name of the game", required=True),
        details: discord.Option(str, "Additional details (version, region, etc.)", required=False)
    ):
        """Submit a request for a ROM with platform validation"""
        await ctx.defer()

        try:
            # Clean platform name (remove [NEW] prefix if present)
            platform_clean = platform.replace("[NEW] ", "")
            # Check if platform exists in our mappings
            async with self.db.get_connection() as db:
                cursor = await db.execute('''
                    SELECT id, in_romm, romm_id, folder_name, igdb_slug, moby_slug
                    FROM platform_mappings
                    WHERE display_name = ?
                ''', (platform_clean,))
                
                platform_mapping = await cursor.fetchone()
                
                if not platform_mapping:
                    # Platform not in our master list - ask for confirmation
                    await ctx.respond(
                        f"⚠️ '{platform}' is not in our platform database. "
                        "Please use a platform from the autocomplete list or contact an admin to add a new platform.",
                        ephemeral=True
                    )
                    return
                
                mapping_id, in_romm, romm_id, folder_name, igdb_slug, moby_slug = platform_mapping
                platform_display_name = platform_clean
                
                # If platform exists in Romm, check for existing games
                if in_romm and romm_id:
                    # Get the actual Romm platform data
                    raw_platforms = await self.bot.fetch_api_endpoint('platforms')
                    if raw_platforms:
                        for p in raw_platforms:
                            if p.get('id') == romm_id:
                                platform_display_name = self.bot.get_platform_display_name(p)
                                break
                    
                    # Check if game exists in current collection
                    exists, matches = await self.check_if_game_exists(platform_display_name, game)
                    
                    if exists:
                        # Game exists in collection - but also fetch IGDB matches
                        search_cog = self.bot.get_cog('Search')
                        platform_with_emoji = search_cog.get_platform_with_emoji(platform_display_name) if search_cog else platform_display_name
                        
                        # Fetch IGDB matches regardless of existing games
                        igdb_matches = []
                        if self.igdb_enabled:
                            try:
                                igdb_platform_slug = None
                                if platform_mapping:
                                    igdb_platform_slug = platform_mapping[4]  # igdb_slug from mapping
                                igdb_matches = await self.igdb.search_game(game, igdb_platform_slug)
                            except Exception as e:
                                logger.error(f"Error fetching IGDB data: {e}")
                        
                        # Create combined view showing both existing games AND IGDB options
                        view = ExistingGameWithIGDBView(
                            self.bot, 
                            matches,           # Existing games in collection
                            igdb_matches,      # IGDB search results
                            platform_display_name, 
                            game, 
                            ctx.author.id
                        )
                        
                        # Check if any IGDB games remain after filtering
                        has_other_games = bool(view.filtered_igdb_matches)
                        
                        embed = discord.Embed(
                            title="Games Found in Collection",
                            description=f"Found {len(matches)} game(s) matching '{game}' that are already available:",
                            color=discord.Color.blue()
                        )
                        
                        # Show first few existing games
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
                        
                        # Update instructions based on what's available
                        instructions = ["• **Select an existing game** from the dropdown to download it"]
                        
                        if has_other_games:
                            instructions.append(f"• **Request a different game** - Found {len(view.filtered_igdb_matches)} other game(s) on IGDB")
                        
                        instructions.append("• Click **Request Different Version** for ROM hacks, patches, or specific versions")
                        
                        embed.add_field(
                            name="What would you like to do?",
                            value="\n".join(instructions),
                            inline=False
                        )
                        
                        message = await ctx.respond(embed=embed, view=view)
                        
                        if isinstance(message, discord.Interaction):
                            view.message = await message.original_response()
                        else:
                            view.message = message
                        
                        await view.wait()
                        return
                
                # Search IGDB for game metadata if enabled
                igdb_matches = []
                if self.igdb_enabled:
                    try:
                        # Get IGDB slug from mapping to use for platform filtering
                        igdb_platform_slug = None
                        if platform_mapping:
                            igdb_platform_slug = platform_mapping[3]  # igdb_slug from mapping
                        
                        # Pass platform slug for filtering if available
                        igdb_matches = await self.igdb.search_game(game, igdb_platform_slug)
                    except Exception as e:
                        logger.error(f"Error fetching IGDB data: {e}")
                
                # Game doesn't exist OR user wants different version - show IGDB selection
                if igdb_matches:
                    select_view = GameSelectView(self.bot, igdb_matches, platform_display_name)
                    initial_embed = select_view.create_game_embed(igdb_matches[0])
                    
                    # Adjust message based on context
                    intro_text = "Please select the correct game from the list below:"
                    
                    select_view.message = await ctx.followup.send(
                        intro_text,
                        embed=initial_embed,
                        view=select_view
                    )
                    
                    await select_view.wait()
                    
                    if not select_view.selected_game:
                        # Timeout
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
                        await self.process_request_with_platform(
                            ctx, 
                            platform_display_name, 
                            game, 
                            details, 
                            selected_game, 
                            select_view.message,
                            mapping_id,
                            in_romm
                        )
                        return
                
                # No IGDB matches or manual entry selected - process without IGDB data
                await self.process_request_with_platform(
                    ctx, 
                    platform_display_name, 
                    game, 
                    details, 
                    None, 
                    None,
                    mapping_id,
                    in_romm
                )

        except Exception as e:
            logger.error(f"Error submitting request: {e}")
            await ctx.respond("❌ An error occurred while submitting your request.")
    
    @discord.slash_command(name="my_requests", description="View and manage your ROM requests")
    async def my_requests(
        self, 
        ctx: discord.ApplicationContext,
        show_pending_only: discord.Option(
            bool,
            "Show only pending requests instead of all",
            required=False,
            default=False
        )
    ):
        """View and manage your submitted requests with interactive controls"""
        await ctx.defer(ephemeral=True)

        try:
            async with self.db.get_connection() as db:
                # Fetch requests based on show_pending_only parameter
                if show_pending_only:
                    cursor = await db.execute(
                        "SELECT * FROM requests WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC",
                        (ctx.author.id,)
                    )
                    viewing_mode = "pending"
                else:
                    cursor = await db.execute(
                        "SELECT * FROM requests WHERE user_id = ? ORDER BY created_at DESC",
                        (ctx.author.id,)
                    )
                    viewing_mode = "all"
                
                requests = await cursor.fetchall()

                if not requests:
                    if show_pending_only:
                        embed = discord.Embed(
                            title="No Pending Requests",
                            description="You don't have any pending requests.\n\nUse `/my_requests` to view all your requests including fulfilled and cancelled ones.",
                            color=discord.Color.green()
                        )
                        await ctx.respond(embed=embed, ephemeral=True)
                    else:
                        embed = discord.Embed(
                            title="No Requests",
                            description="You haven't made any requests yet.\n\nUse `/request` to submit a ROM request!",
                            color=discord.Color.light_grey()
                        )
                        embed.set_footer(text="Start by requesting a game you'd like to see added")
                        await ctx.respond(embed=embed, ephemeral=True)
                    return

                # Count statuses for summary
                status_counts = {
                    'pending': 0,
                    'fulfilled': 0,
                    'cancelled': 0,
                    'reject': 0
                }
                for req in requests:
                    status = req[6]
                    if status in status_counts:
                        status_counts[status] += 1

                # Create paginated view
                view = UserRequestsView(self.bot, requests, ctx.author.id, self.bot.db)
                embed = view.create_request_embed(requests[0])
                
                # Build status summary
                status_parts = []
                if status_counts['pending'] > 0:
                    status_parts.append(f"⏳ {status_counts['pending']} pending")
                if status_counts['fulfilled'] > 0:
                    status_parts.append(f"✅ {status_counts['fulfilled']} fulfilled")
                if status_counts['cancelled'] > 0:
                    status_parts.append(f"🚫 {status_counts['cancelled']} cancelled")
                if status_counts['reject'] > 0:
                    status_parts.append(f"❌ {status_counts['reject']} rejected")
                
                status_summary = " | ".join(status_parts)
                
                # Add viewing mode indicator to the message
                mode_text = "⏳ **Viewing: Pending Requests Only**" if show_pending_only else "📋 **Viewing: All Your Requests**"
                hint_text = "\n*Use `/my_requests show_pending_only:True` to see only pending requests*" if not show_pending_only else "\n*Use `/my_requests` to see all requests*"
                
                message = await ctx.respond(
                    content=f"{mode_text}\n📊 **Summary:** {status_summary}",
                    embed=embed, 
                    view=view,
                    ephemeral=True
                )
                
                # Store message reference for editing
                if isinstance(message, discord.Interaction):
                    view.message = await message.original_response()
                else:
                    view.message = message

        except Exception as e:
            logger.error(f"Error fetching requests: {e}")
            await ctx.respond("❌ An error occurred while fetching your requests.", ephemeral=True)

    @discord.slash_command(name="request_admin", description="Interface for managing ROM requests (admin only)")
    @is_admin()
    async def request_admin(
        self,
        ctx: discord.ApplicationContext,
        show_all: discord.Option(
            bool,
            "Show all requests instead of just pending ones",
            required=False,
            default=False
        )
    ):
        """Admin interface for managing requests - shows pending by default"""
        await ctx.defer(ephemeral=True)

        try:
            async with self.db.get_connection() as db:
                # Fetch requests based on show_all parameter
                if show_all:
                    cursor = await db.execute(
                        "SELECT * FROM requests ORDER BY created_at DESC"
                    )
                    viewing_mode = "all"
                else:
                    cursor = await db.execute(
                        "SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at ASC"
                    )
                    viewing_mode = "pending"
                
                requests = await cursor.fetchall()

                if not requests:
                    if show_all:
                        await ctx.respond("📭 No requests found in the system.", ephemeral=True)
                    else:
                        embed = discord.Embed(
                            title="No Pending Requests",
                            description="There are currently no pending requests.\n\nUse `/request_admin show_all:True` to view all requests including fulfilled and rejected ones.",
                            color=discord.Color.green()
                        )
                        embed.set_footer(text="All requests have been processed!")
                        await ctx.respond(embed=embed)
                    return

                # Create paginated view
                view = RequestAdminView(self.bot, requests, ctx.author.id, self.bot.db)
                
                # Fetch user avatar for the first request
                user_avatar_url = None
                try:
                    user = self.bot.get_user(requests[0][1])  # requests[0][1] is user_id
                    if not user:
                        user = await self.bot.fetch_user(requests[0][1])
                    if user and user.avatar:
                        user_avatar_url = user.avatar.url
                    elif user:
                        user_avatar_url = user.default_avatar.url
                except:
                    pass

                embed = view.create_request_embed(requests[0], user_avatar_url)  # Pass avatar URL
                
                # Add viewing mode indicator to the message
                mode_text = "📋 **Viewing: All Requests**" if show_all else "⏳ **Viewing: Pending Requests Only**"
                hint_text = "\n *Use `/request_admin show_all:True` to see all requests*" if not show_all else ""
                
                message = await ctx.respond(
                    content=f"{mode_text}",
                    embed=embed, 
                    view=view
                )
                
                # Store message reference for editing
                if isinstance(message, discord.Interaction):
                    view.message = await message.original_response()
                else:
                    view.message = message

        except Exception as e:
            logger.error(f"Error in request admin command: {e}")
            await ctx.respond("❌ An error occurred while loading the requests interface.", ephemeral=True)

def setup(bot):
    bot.add_cog(Request(bot))
