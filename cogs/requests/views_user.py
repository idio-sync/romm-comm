"""The request browser a user sees for their own requests."""

import logging

import discord

from .embeds import build_request_embed
from .repo import RequestsRepo

logger = logging.getLogger(__name__)


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
        self.repo = RequestsRepo(db)
        
        # Cache platform status for all requests
        self.platform_status = {}  # mapping_id -> in_romm status
        
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
        is_pending = current_request['status'] == 'pending'
        self.cancel_button.disabled = not is_pending
        
        # Update cancel button label and style based on status
        if not is_pending:
            if current_request['status'] == 'fulfilled':
                self.cancel_button.label = "Fulfilled"
                self.cancel_button.style = discord.ButtonStyle.success  # Green
            elif current_request['status'] == 'reject':
                self.cancel_button.label = "Rejected"
                self.cancel_button.style = discord.ButtonStyle.danger  # Red
            elif current_request['status'] == 'cancelled':
                self.cancel_button.label = "Cancelled"
                self.cancel_button.style = discord.ButtonStyle.danger  # Red
            else:
                self.cancel_button.label = "Not Available"
                self.cancel_button.style = discord.ButtonStyle.secondary  # Gray
        else:
            self.cancel_button.label = "Cancel Request"
            self.cancel_button.style = discord.ButtonStyle.danger  # Red for cancel action
    
    def create_request_embed(self, req, user_avatar_url=None):
        """Render the current request. Shared with the other requests view."""
        return build_request_embed(
            req,
            bot=self.bot,
            platform_status=self.platform_status,
            position=self.current_index + 1,
            total=len(self.requests),
            user_avatar_url=user_avatar_url,
        )
    
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
                user = self.bot.get_user(self.requests[self.current_index]['user_id'])
                if not user:
                    user = await self.bot.fetch_user(self.requests[self.current_index]['user_id'])
                if user and user.avatar:
                    user_avatar_url = user.avatar.url
                elif user:
                    user_avatar_url = user.default_avatar.url
            except (discord.NotFound, discord.HTTPException):
                pass  # User not found or API error

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
                user = self.bot.get_user(self.requests[self.current_index]['user_id'])
                if not user:
                    user = await self.bot.fetch_user(self.requests[self.current_index]['user_id'])
                if user and user.avatar:
                    user_avatar_url = user.avatar.url
                elif user:
                    user_avatar_url = user.default_avatar.url
            except (discord.NotFound, discord.HTTPException):
                pass  # User not found or API error

            embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def cancel_callback(self, interaction: discord.Interaction):
        """Cancel the current pending request"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't for you!", ephemeral=True)
            return
        
        current_request = self.requests[self.current_index]
        
        if current_request['status'] != 'pending':
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
                
                request_id = self.request_data['id']
                reason = self.reason.value or "User cancelled"
                
                try:
                    await self.view.repo.mark_cancelled(request_id, reason=reason)
                    
                    # Update the request in our list
                    updated_request = dict(self.request_data)
                    updated_request['status'] = 'cancelled'
                    updated_request['notes'] = reason
                    self.view.requests[self.view.current_index] = updated_request
                    
                    # Update button states
                    self.view.update_button_states()
                    
                    # Fetch user avatar
                    user_avatar_url = None
                    try:
                        user = self.view.bot.get_user(self.view.requests[self.view.current_index]['user_id'])
                        if not user:
                            user = await self.view.bot.fetch_user(self.view.requests[self.view.current_index]['user_id'])
                        if user and user.avatar:
                            user_avatar_url = user.avatar.url
                        elif user:
                            user_avatar_url = user.default_avatar.url
                    except (discord.NotFound, discord.HTTPException):
                        pass  # User not found or API error

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
                current_note = request_data['notes'] or ""
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
                
                request_id = self.request_data['id']
                note = self.note.value
                
                try:
                    await self.view.repo.set_notes(request_id, note)
                    
                    # Update the request in our list
                    updated_request = dict(self.request_data)
                    updated_request['notes'] = note
                    self.view.requests[self.view.current_index] = updated_request
                    
                    # Fetch user avatar
                    user_avatar_url = None
                    try:
                        user = self.view.bot.get_user(self.view.requests[self.view.current_index]['user_id'])
                        if not user:
                            user = await self.view.bot.fetch_user(self.view.requests[self.view.current_index]['user_id'])
                        if user and user.avatar:
                            user_avatar_url = user.avatar.url
                        elif user:
                            user_avatar_url = user.default_avatar.url
                    except (discord.NotFound, discord.HTTPException):
                        pass  # User not found or API error

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
            self.requests = await self.repo.list_for_user(self.user_id)
            
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

    async def on_timeout(self):
        """Disable all components when the view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass  # Message was deleted or can't be edited
