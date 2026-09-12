"""The admin-facing request browser."""

import logging

import discord

from .embeds import build_request_embed
from .repo import REQUEST_COLUMNS

logger = logging.getLogger(__name__)


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
        
        # Cache platform status for all requests
        self.platform_status = {}  # mapping_id -> in_romm status
        
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
        is_pending = current_request['status'] == 'pending'
        self.fulfill_button.disabled = not is_pending
        self.reject_button.disabled = not is_pending
    
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
            except (discord.NotFound, discord.HTTPException):
                pass  # User not found or API error

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
            except (discord.NotFound, discord.HTTPException):
                pass  # User not found or API error

            embed = self.create_request_embed(self.requests[self.current_index], user_avatar_url)
            await interaction.response.edit_message(embed=embed, view=self)
    
    async def fulfill_callback(self, interaction: discord.Interaction):
        """Mark current request as fulfilled"""
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only admins can use these controls.", ephemeral=True)
            return
        
        await interaction.response.defer()
        
        current_request = self.requests[self.current_index]
        request_id = current_request['id']
        
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
                
                logger.info(f"Request fulfilled manually - Admin: {interaction.user} | Request ID: #{request_id} | Discord: {current_request['username']} (ID: {current_request['user_id']}) | Game: '{current_request['game_name']}' | Platform: {current_request['platform']}")
                
                # Sync to ggrequestz if enabled
                ggr = self.bot.get_cog('GGRequestzIntegration')
                if ggr and ggr.enabled:
                    # Get the ggr_request_id for this Discord request
                    cursor = await db.execute(
                        "SELECT ggr_request_id FROM requests WHERE id = ?",
                        (request_id,)
                    )
                    result = await cursor.fetchone()
                    
                    if result and result['ggr_request_id']:
                        ggr_request_id = result['ggr_request_id']
                        # Update status in ggrequestz
                        sync_result = await ggr.update_request_status(
                            ggr_request_id=ggr_request_id,
                            status='fulfilled',
                            admin_name=str(interaction.user),
                            notes=f"Manually fulfilled by {interaction.user}"
                        )
                        
                        if sync_result.get('success'):
                            logger.info(f"✅ Synced manual fulfillment to ggrequestz for request #{request_id} (GGR ID: {ggr_request_id})")
                        else:
                            logger.error(f"❌ Failed to sync manual fulfillment to ggrequestz: {sync_result.get('error')}")
                
                # Get all subscribers for this request
                cursor = await db.execute(
                    "SELECT user_id FROM request_subscribers WHERE request_id = ?",
                    (request_id,)
                )
                subscribers = await cursor.fetchall()
                
                # Notify original requester
                try:
                    # Prioritize the stored IGDB name, fall back to the user's requested name
                    igdb_game_name = current_request['igdb_game_name']
                    display_game_name = igdb_game_name if igdb_game_name else current_request['game_name']

                    user = await self.bot.fetch_user(current_request['user_id'])
                    await user.send(f"✅ Your request for '{display_game_name}' has been fulfilled!")
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(f"Could not DM user {current_request['user_id']}: {e}")
            
            # Update the request in our list
            updated_request = dict(current_request)
            updated_request['status'] = 'fulfilled'
            updated_request['fulfilled_by'] = interaction.user.id
            updated_request['fulfiller_name'] = str(interaction.user)
            self.requests[self.current_index] = updated_request
            
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
            except (discord.NotFound, discord.HTTPException):
                pass  # User not found or API error

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
                
                request_id = self.request_data['id']
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
                        
                        logger.info(f"Request rejected - Admin: {modal_interaction.user} | Request ID: #{request_id} | Discord: {self.request_data['username']} (ID: {self.request_data['user_id']}) | Game: '{self.request_data['game_name']}' | Platform: {self.request_data['platform']} | Reason: {reason or 'No reason provided'}")
                        
                        # Sync to ggrequestz if enabled
                        ggr = self.view.bot.get_cog('GGRequestzIntegration')
                        if ggr and ggr.enabled:
                            cursor = await db.execute(
                                "SELECT ggr_request_id FROM requests WHERE id = ?",
                                (request_id,)
                            )
                            result = await cursor.fetchone()
                            
                            if result and result['ggr_request_id']:
                                ggr_request_id = result['ggr_request_id']
                                sync_result = await ggr.update_request_status(
                                    ggr_request_id=ggr_request_id,
                                    status='rejected',
                                    admin_name=str(interaction.user),
                                    notes=f"Rejected by {interaction.user}"
                                )
                                
                                if sync_result.get('success'):
                                    logger.info(f"✅ Synced rejection to ggrequestz for request #{request_id} (GGR ID: {ggr_request_id})")
                                else:
                                    logger.error(f"❌ Failed to sync rejection to ggrequestz: {sync_result.get('error')}")
                        
                        # Notify user
                        try:
                            # Prioritize the stored IGDB name, fall back to the user's requested name
                            igdb_game_name = self.request_data['igdb_game_name']
                            display_game_name = igdb_game_name if igdb_game_name else self.request_data['game_name']

                            user = await self.view.bot.fetch_user(self.request_data['user_id'])
                            message = f"❌ Your request for '{display_game_name}' has been rejected."
                            if reason:
                                message += f"\nReason: {reason}"
                            await user.send(message)
                        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                            logger.warning(f"Could not DM user {self.request_data['user_id']}: {e}")
                    
                    # Update the request in our list
                    updated_request = dict(self.request_data)
                    updated_request['status'] = 'reject'
                    updated_request['fulfilled_by'] = modal_interaction.user.id
                    updated_request['fulfiller_name'] = str(modal_interaction.user)
                    updated_request['notes'] = reason
                    self.view.requests[self.view.current_index] = updated_request
                    
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
                current_note = request_data['notes'] or ""
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
                
                request_id = self.request_data['id']
                note = self.note.value
                
                try:
                    async with self.view.db.get_connection() as db:
                        await db.execute(
                            "UPDATE requests SET notes = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (note, request_id)
                        )
                        await db.commit()
                    
                    # Update the request in our list
                    updated_request = dict(self.request_data)
                    updated_request['notes'] = note
                    self.view.requests[self.view.current_index] = updated_request
                    
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
                    f"SELECT {REQUEST_COLUMNS} FROM requests ORDER BY created_at DESC"
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
                except (discord.NotFound, discord.HTTPException):
                    pass  # User not found or API error

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

    async def on_timeout(self):
        """Disable all components when the view times out"""
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.HTTPException):
                pass  # Message was deleted or can't be edited
