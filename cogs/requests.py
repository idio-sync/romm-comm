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

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

def is_admin():
    """Check if the user is the admin"""
    async def predicate(ctx: discord.ApplicationContext):
        admin_id = os.getenv('ADMIN_ID')
        if not admin_id:
            return False
        return str(ctx.author.id) == admin_id
    return commands.check(predicate)

class RequestAdminView(discord.ui.View):
    """Paginated view for managing requests"""
    
    def __init__(self, bot, requests_data, admin_id):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
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
    
    def create_request_embed(self, req):
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
            embed.add_field(
                name="Links",
                value=f"[IGDB]({igdb_url})",
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
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        if self.current_index > 0:
            self.current_index -= 1
            self.update_button_states()
            embed = self.create_request_embed(self.requests[self.current_index])
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def forward_callback(self, interaction: discord.Interaction):
        """Navigate to next request"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        if self.current_index < len(self.requests) - 1:
            self.current_index += 1
            self.update_button_states()
            embed = self.create_request_embed(self.requests[self.current_index])
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def fulfill_callback(self, interaction: discord.Interaction):
        """Mark current request as fulfilled"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        current_request = self.requests[self.current_index]
        request_id = current_request[0]
        
        try:
            db_path = Path('data') / 'requests.db'
            async with aiosqlite.connect(str(db_path)) as db:
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
                
                # Notify user
                try:
                    user = await self.bot.fetch_user(current_request[1])  # user_id
                    await user.send(f"✅ Your request for '{current_request[4]}' has been fulfilled!")
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
            embed = self.create_request_embed(self.requests[self.current_index])
            await interaction.followup.edit_message(message_id=self.message.id, embed=embed, view=self)
            
        except Exception as e:
            logger.error(f"Error fulfilling request: {e}")
            await interaction.followup.send("❌ An error occurred while fulfilling the request.", ephemeral=True)
    
    async def reject_callback(self, interaction: discord.Interaction):
        """Show modal for rejection reason then reject"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        current_request = self.requests[self.current_index]
        
        class RejectModal(discord.ui.Modal):
            def __init__(self, view, request_data):
                super().__init__(title="Reject Request")
                self.view = view
                self.request_data = request_data
                
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
                    db_path = Path('data') / 'requests.db'
                    async with aiosqlite.connect(str(db_path)) as db:
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
                        
                        # Notify user
                        try:
                            user = await self.view.bot.fetch_user(self.request_data[1])
                            message = f"❌ Your request for '{self.request_data[4]}' has been rejected."
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
        
        modal = RejectModal(self, current_request)
        await interaction.response.send_modal(modal)
    
    async def note_callback(self, interaction: discord.Interaction):
        """Add a note to the current request"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        current_request = self.requests[self.current_index]
        
        class NoteModal(discord.ui.Modal):
            def __init__(self, view, request_data):
                super().__init__(title="Add Note to Request")
                self.view = view
                self.request_data = request_data
                
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
                    db_path = Path('data') / 'requests.db'
                    async with aiosqlite.connect(str(db_path)) as db:
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
        
        modal = NoteModal(self, current_request)
        await interaction.response.send_modal(modal)
    
    async def refresh_callback(self, interaction: discord.Interaction):
        """Refresh the requests list from database"""
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        try:
            db_path = Path('data') / 'requests.db'
            async with aiosqlite.connect(str(db_path)) as db:
                cursor = await db.execute(
                    "SELECT * FROM requests ORDER BY created_at DESC"
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
    
    def __init__(self, bot, requests_data, user_id):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
        self.requests = requests_data
        self.user_id = user_id
        self.current_index = 0
        self.message = None
        
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
    
    def create_request_embed(self, req):
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
                name="Your Additional Notes",
                value=additional_notes[:1024],
                inline=False
            )
        
        # Set images
        if cover_url and cover_url != 'None':
            embed.set_image(url=cover_url)
        
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
        
        # Notes field (admin notes or rejection reason)
        if req[11]:
            # Check if this is a rejection reason or admin note
            if req[6] == 'reject':
                embed.add_field(
                    name="❌ Rejection Reason",
                    value=req[11][:1024],
                    inline=False
                )
            else:
                embed.add_field(
                    name="📝 Notes",
                    value=req[11][:1024],
                    inline=False
                )
        
        # Fulfillment info if fulfilled/rejected
        if req[9] and req[10]:  # fulfilled_by and fulfiller_name
            action = "Fulfilled" if req[6] == 'fulfilled' else "Rejected" if req[6] == 'reject' else "Processed"
            embed.add_field(
                name=f"✍️ {action} By",
                value=req[10],
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
            embed.add_field(
                name="Links",
                value=f"[IGDB]({igdb_url})",
                inline=True
            )
        
        # Footer with request date and pagination
        try:
            created_at = datetime.fromisoformat(req[7].replace('Z', '+00:00'))
            date_str = created_at.strftime("%B %d, %Y at %I:%M %p")
        except:
            date_str = req[7]
        
        total = len(self.requests)
        embed.set_footer(
            text=f"Request {self.current_index + 1}/{total} • Submitted on {date_str}"
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
            embed = self.create_request_embed(self.requests[self.current_index])
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def forward_callback(self, interaction: discord.Interaction):
        """Navigate to next request"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        if self.current_index < len(self.requests) - 1:
            self.current_index += 1
            self.update_button_states()
            embed = self.create_request_embed(self.requests[self.current_index])
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
                reason = self.reason.value or "Cancelled by user"
                
                try:
                    db_path = Path('data') / 'requests.db'
                    async with aiosqlite.connect(str(db_path)) as db:
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
                    
                    # Update view
                    self.view.update_button_states()
                    embed = self.view.create_request_embed(self.view.requests[self.view.current_index])
                    await modal_interaction.followup.edit_message(
                        message_id=self.view.message.id, 
                        embed=embed, 
                        view=self.view
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
                    db_path = Path('data') / 'requests.db'
                    async with aiosqlite.connect(str(db_path)) as db:
                        # Only update notes, don't change status
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
                        "❌ An error occurred while updating the note.",
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
            db_path = Path('data') / 'requests.db'
            async with aiosqlite.connect(str(db_path)) as db:
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
        embed.add_field(
            name="Links",
            value=f"[IGDB]({igdb_url})",
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
        rom_embed = await rom_view.create_rom_embed(rom_data)
        
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
        
        return rom_embed
    
    async def select_callback(self, interaction: discord.Interaction):
        """Handle ROM selection"""
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        selected_rom_id = int(interaction.data['values'][0])
        self.selected_rom = next((rom for rom in self.matches if rom['id'] == selected_rom_id), None)
        
        if self.selected_rom:
            # Create full ROM embed
            rom_embed = await self.create_full_rom_view(self.selected_rom)
            
            # Edit the message with new embed and updated view
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

    async def process_request(self, ctx_or_interaction, platform_name, game, details, selected_game, message):
        """Process and save the request"""
        try:
            # Handle both ctx and interaction objects
            if hasattr(ctx_or_interaction, 'user'):
                # It's an interaction
                author = ctx_or_interaction.user
                author_name = str(ctx_or_interaction.user)
                
                # For responding, we need to check if it's already deferred
                async def respond(content=None, embed=None, embeds=None):
                    try:
                        return await ctx_or_interaction.followup.send(content=content, embed=embed, embeds=embeds)
                    except:
                        return await ctx_or_interaction.response.send_message(content=content, embed=embed, embeds=embeds)
            else:
                # It's a ctx
                author = ctx_or_interaction.author
                author_name = str(ctx_or_interaction.author)
                
                async def respond(content=None, embed=None, embeds=None):
                    return await ctx_or_interaction.respond(content=content, embed=embed, embeds=embeds)
            
            async with aiosqlite.connect(str(self.db_path)) as db:
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

                # Insert the request into the database
                await db.execute(
                    """
                    INSERT INTO requests (user_id, username, platform, game_name, details)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (author.id, author_name, platform_name, game, details)
                )
                await db.commit()

                if message and selected_game:
                    view = GameSelectView(self.bot, matches=[selected_game], platform_name=platform_name)
                    embed = view.create_game_embed(selected_game)
                    embed.set_footer(text=f"Request submitted by {author_name}")
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
                    embed.set_footer(text=f"Request submitted by {author_name}")
                    await respond(embed=embed)

        except Exception as e:
            logger.error(f"Error processing request: {e}")
            try:
                await respond(content="❌ An error occurred while processing the request.")
            except:
                logger.error("Could not send error message to user")
    
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
                await self.process_request(ctx, platform_display_name, game, details, selected_game, select_view.message)
                return
        
        # Process manual request
        await self.process_request(ctx, platform_display_name, game, details, None, None)
    
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
            platform_display_name = None
            platform_lower = platform.lower()
            
            for p in raw_platforms:
                # Check custom name first
                custom_name = p.get('custom_name')
                if custom_name and custom_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    platform_display_name = self.bot.get_platform_display_name(p)
                    break
                
                # Check regular name
                regular_name = p.get('name', '')
                if regular_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    platform_display_name = self.bot.get_platform_display_name(p)
                    break
                    
            if not platform_id:
                # Show available platforms with display names
                search_cog = self.bot.get_cog('Search')
                platforms_list = "\n".join(
                    f"• {search_cog.get_platform_with_emoji(self.bot.get_platform_display_name(p)) if search_cog else self.bot.get_platform_display_name(p)}" 
                    for p in sorted(raw_platforms, key=lambda x: self.bot.get_platform_display_name(x))
                )
                await ctx.respond(f"❌ Platform '{platform}' not found. Available platforms:\n{platforms_list}")
                return

            # Check if game exists in current collection
            exists, matches = await self.check_if_game_exists(platform_display_name, game)
            
            # Search IGDB for game metadata if available
            igdb_matches = []
            if self.igdb_enabled:
                try:
                    igdb_matches = await self.igdb.search_game(game, platform_display_name)
                except Exception as e:
                    logger.error(f"Error fetching IGDB data: {e}")
                    # Continue without IGDB data if there's an error
            
            if exists:
                # Create embed showing games found
                search_cog = self.bot.get_cog('Search')
                platform_with_emoji = search_cog.get_platform_with_emoji(platform_display_name) if search_cog else platform_display_name

                embed = discord.Embed(
                    title="Similar Games Found in Collection",
                    description=f"Found {len(matches)} game(s) matching '{game}' for platform '{platform_with_emoji}' that are already available:",
                    color=discord.Color.blue()
                )
                                
                # For single match, show ROM_View directly
                if len(matches) == 1:
                    # Fetch full ROM details
                    rom_data = matches[0]
                    try:
                        detailed_rom = await self.bot.fetch_api_endpoint(f'roms/{rom_data["id"]}')
                        if detailed_rom:
                            rom_data.update(detailed_rom)
                    except Exception as e:
                        logger.error(f"Error fetching ROM details: {e}")
                    
                    # Create ROM_View for immediate download access
                    rom_view = ROM_View(self.bot, [rom_data], ctx.author.id, platform_display_name)
                    rom_view.remove_item(rom_view.select)  # Remove selection since single item
                    rom_view._selected_rom = rom_data
                    
                    # Create embed using ROM_View's method
                    rom_embed = await rom_view.create_rom_embed(rom_data)
                    await rom_view.update_file_select(rom_data)
                    
                    # Collect all download buttons and file select
                    download_buttons = []
                    file_select = None
                    items_to_remove = []
                    
                    for item in rom_view.children[:]:  # Use slice to avoid modification during iteration
                        if isinstance(item, discord.ui.Button) and "Download" in item.label:
                            download_buttons.append(item)
                            items_to_remove.append(item)
                        elif isinstance(item, discord.ui.Select) and item.custom_id == "rom_file_select":
                            file_select = item
                    
                    # Remove all download buttons temporarily
                    for item in items_to_remove:
                        rom_view.remove_item(item)
                    
                    # Determine which row to use
                    button_row = 2 if file_select else 1
                    
                    # Re-add download buttons with correct row
                    for button in download_buttons:
                        button.row = button_row
                        rom_view.add_item(button)
                    
                    # Add custom buttons for request flow on the same row
                    request_different_btn = discord.ui.Button(
                        label="Request Different Version",
                        style=discord.ButtonStyle.primary,
                        row=button_row
                    )
                    
                    async def request_different(interaction):
                        if interaction.user.id != ctx.author.id:
                            await interaction.response.send_message("This isn't for you!", ephemeral=True)
                            return
                        
                        # Show modal for variant input
                        modal = VariantRequestModal(
                            self.bot,
                            platform_display_name,
                            game,
                            details,
                            igdb_matches,
                            ctx
                        )
                        await interaction.response.send_modal(modal)
                        rom_view.stop()

                    request_different_btn.callback = request_different
                    rom_view.add_item(request_different_btn)
                    
                    cancel_btn = discord.ui.Button(
                        label="Cancel",
                        style=discord.ButtonStyle.secondary,
                        row=button_row
                    )
                    
                    async def cancel_callback(interaction):
                        if interaction.user.id != ctx.author.id:
                            await interaction.response.send_message("This isn't for you!", ephemeral=True)
                            return
                        await interaction.response.defer()
                        rom_view.stop()

                    cancel_btn.callback = cancel_callback
                    rom_view.add_item(cancel_btn)
                    
                    message = await ctx.respond(
                        "✅ This game is already available! You can download it now:",
                        embed=rom_embed,
                        view=rom_view
                    )
                    
                    if isinstance(message, discord.Interaction):
                        message = await message.original_response()
                    rom_view.message = message
                    
                     # Wait for the view to complete
                    await rom_view.wait()
                    return  # prevent further execution
                    
                else:
                    # Multiple matches - show selection with download capabilities
                    view = ExistingGameView(self.bot, matches, platform_display_name, game, ctx.author.id)
                    
                    # Add match details to embed
                    search_cog = self.bot.get_cog('Search')
                    platform_with_emoji = search_cog.get_platform_with_emoji(platform_display_name) if search_cog else platform_display_name

                    embed = discord.Embed(
                        title="Similar Games Found in Collection",
                        description=f"Found {len(matches)} game(s) matching '{game}' for platform '{platform_with_emoji}' that are already available:",
                        color=discord.Color.blue()
                    )
                    
                    if igdb_matches:
                        # Find best matching IGDB game
                        best_match_score = 0
                        best_igdb_match = None
                        requested_game_lower = game.lower()
                        
                        for igdb_game in igdb_matches:
                            score = self.calculate_similarity(requested_game_lower, igdb_game['name'].lower())
                            if score > best_match_score:
                                best_igdb_match = igdb_game
                                best_match_score = score
                        
                        # Add IGDB thumbnail if available
                        if best_igdb_match and best_igdb_match.get('cover_url'):
                            embed.set_thumbnail(url=best_igdb_match['cover_url'])
                    
                    for i, rom in enumerate(matches[:5]):  # Show first 5
                        embed.add_field(
                            name=rom.get('name', 'Unknown'),
                            value=f"File: {rom.get('fs_name', 'Unknown')}",
                            inline=False
                        )
                    
                    if len(matches) > 5:
                        embed.set_footer(text=f"...and {len(matches) - 5} more")
                    
                    message = await ctx.respond(embed=embed, view=view)
                    
                    # Store message reference in the view
                    if isinstance(message, discord.Interaction):
                        view.message = await message.original_response()
                    else:
                        view.message = message
                    
                    await view.wait()
                    
                    if view.action == "request_different":
                        # Continue with request flow
                        await self.continue_request_flow(ctx, platform_display_name, game, details, igdb_matches)
                        return
                    elif view.action == "cancel":
                        return
                    # If they selected and downloaded, we're done
                    return

            # If we get here, either the game doesn't exist or user confirmed different version needed
            if igdb_matches:
                select_view = GameSelectView(self.bot, igdb_matches, platform_display_name)
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
                    await self.process_request(ctx, platform_display_name, game, details, selected_game, select_view.message)
                    return

            # If we get here, either no IGDB matches or manual entry selected
            selected_game = None
            await self.process_request(ctx, platform_display_name, game, details, selected_game, None)

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
            async with aiosqlite.connect(self.db_path) as db:
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
                view = UserRequestsView(self.bot, requests, ctx.author.id)
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

    @discord.slash_command(name="request_admin", description="Admin interface for managing ROM requests")
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
            async with aiosqlite.connect(self.db_path) as db:
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
                view = RequestAdminView(self.bot, requests, ctx.author.id)
                embed = view.create_request_embed(requests[0])
                
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
