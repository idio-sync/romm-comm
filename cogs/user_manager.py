import discord
from discord.ext import commands, tasks
from typing import Optional, Dict, Any, List
import logging
import secrets
import string
import asyncio
import aiohttp
import aiosqlite
import os
from dotenv import load_dotenv
import time

# Load environment variables
load_dotenv()

logger = logging.getLogger('romm_bot.users')

def is_admin():
    """Check if the user is the admin"""
    async def predicate(ctx: discord.ApplicationContext):
        return ctx.bot.is_admin(ctx.author)
    return commands.check(predicate)

class UserManagementView(discord.ui.View):
    """Comprehensive user management interface for admins"""
    
    def __init__(self, bot, cog, guild: discord.Guild):
        super().__init__(timeout=300)  # 5 minute timeout
        self.bot = bot
        self.cog = cog
        self.guild = guild
        self.message = None
        self.interaction = None
        
        # State management
        self.selected_discord_user = None
        self.selected_romm_user = None
        self.romm_users = []
        self.discord_user_links = {}  # discord_id -> romm_username mapping
        
        # Discord dropdown pagination state
        self.discord_current_page = 0
        self.page_size = 25
        self.full_member_list = []
        
        # RomM dropdown pagination state
        self.romm_current_page = 0
        self.full_romm_list = []
        
        # Initialize components
        self._setup_components()
        
    def _setup_components(self):
        """Setup initial UI components"""
        # Discord User Select (Row 0)
        self.discord_select = discord.ui.Select(
            placeholder="Select a Discord user...",
            custom_id="discord_user_select",
            row=0,
            min_values=0,
            max_values=1
        )
        # Add a placeholder option initially to avoid empty select menu error
        self.discord_select.add_option(
            label="Loading users...",
            value="placeholder",
            description="Please wait..."
        )
        self.discord_select.callback = self.discord_select_callback
        self.add_item(self.discord_select)
        
        # Discord pagination buttons (Row 1)
        self.discord_prev_button = discord.ui.Button(
            label="⬅️ Previous Discord users", 
            style=discord.ButtonStyle.secondary, 
            row=1, 
            disabled=True
        )
        self.discord_prev_button.callback = self.discord_prev_page_callback
        self.add_item(self.discord_prev_button)

        self.discord_next_button = discord.ui.Button(
            label="Next Discord users ➡️", 
            style=discord.ButtonStyle.secondary, 
            row=1, 
            disabled=True
        )
        self.discord_next_button.callback = self.discord_next_page_callback
        self.add_item(self.discord_next_button)
        
        # RomM User Select (Row 2)
        self.romm_select = discord.ui.Select(
            placeholder="Select a RomM user to link...",
            custom_id="romm_user_select",
            row=2,
            disabled=True,
            min_values=0,
            max_values=1
        )
        # Add a placeholder option initially
        self.romm_select.add_option(
            label="Select Discord user first",
            value="placeholder",
            description="Waiting for Discord user selection..."
        )
        self.romm_select.callback = self.romm_select_callback
        self.add_item(self.romm_select)
        
        # RomM pagination buttons (Row 3)
        self.romm_prev_button = discord.ui.Button(
            label="⬅️ Previous RomM users", 
            style=discord.ButtonStyle.secondary, 
            row=3, 
            disabled=True
        )
        self.romm_prev_button.callback = self.romm_prev_page_callback
        self.add_item(self.romm_prev_button)

        self.romm_next_button = discord.ui.Button(
            label="Next RomM users ➡️", 
            style=discord.ButtonStyle.secondary, 
            row=3, 
            disabled=True
        )
        self.romm_next_button.callback = self.romm_next_page_callback
        self.add_item(self.romm_next_button)
        
        # Action Buttons (Row 4)
        self.link_button = discord.ui.Button(
            label="Link Accounts",
            style=discord.ButtonStyle.success,
            row=4,
            disabled=True
        )
        self.link_button.callback = self.link_accounts_callback
        self.add_item(self.link_button)
        
        self.unlink_button = discord.ui.Button(
            label="Unlink Account",
            style=discord.ButtonStyle.danger,
            row=4,
            disabled=True
        )
        self.unlink_button.callback = self.unlink_account_callback
        self.add_item(self.unlink_button)
                
        self.invite_button = discord.ui.Button(
            label="📧 Send Invite Link",
            style=discord.ButtonStyle.primary,
            row=4,
            disabled=True
        )
        self.invite_button.callback = self.send_invite_callback
        self.add_item(self.invite_button)
                
        self.bulk_create_button = discord.ui.Button(
            label="Bulk Send Invites",
            style=discord.ButtonStyle.primary,
            row=4
        )
        self.bulk_create_button.callback = self.bulk_create_callback
        self.add_item(self.bulk_create_button)
             
    async def populate_discord_users(self):
        """Populate the Discord user dropdown with pagination"""
        # Step 1: Fetch and sort the full list ONCE if we haven't already
        if not self.full_member_list:
            # MODIFIED: Always fetch all members from the guild, ignoring the role ID
            members = self.guild.members
            # Store the full sorted list
            self.full_member_list = sorted(members, key=lambda m: m.display_name.lower())

        # Clear previous options
        self.discord_select.options.clear()
        
        # Step 2: Calculate pagination variables
        total_members = len(self.full_member_list)
        total_pages = (total_members + self.page_size - 1) // self.page_size if self.page_size > 0 else 1
        start_index = self.discord_current_page * self.page_size
        end_index = start_index + self.page_size
        
        # Get the slice of members for the current page
        members_on_page = self.full_member_list[start_index:end_index]

        # Update placeholder to show page info
        self.discord_select.placeholder = f"Select a Discord user (Page {self.discord_current_page + 1}/{total_pages})"

        if not members_on_page:
            self.discord_select.add_option(label="No users found on this page", value="no_users")
            self.discord_prev_button.disabled = self.discord_current_page == 0
            self.discord_next_button.disabled = end_index >= total_members
            return

        # Step 3: Populate options for the current page
        for member in members_on_page:
            link = await self.cog.db_manager.get_user_link(member.id)
            if link:
                self.discord_user_links[member.id] = link['romm_username']
                description = f"Linked to: {link['romm_username']}"
                emoji = "✅"
            else:
                self.discord_user_links[member.id] = None
                description = "Not linked"
                emoji = "❌"
            
            self.discord_select.add_option(
                label=f"{emoji} {member.display_name[:75]}",
                value=str(member.id),
                description=description[:100]
            )
            
        # Step 4: Update pagination button states
        self.discord_prev_button.disabled = self.discord_current_page == 0
        self.discord_next_button.disabled = end_index >= total_members
    
    async def populate_romm_users(self):
        """Populate the RomM user dropdown with pagination"""
        # Always fetch fresh data to ensure we have current state
        users_data = await self.bot.fetch_api_endpoint('users', bypass_cache=True)
        if not users_data:
            self.romm_select.options.clear()
            self.romm_select.add_option(
                label="Failed to fetch RomM users",
                value="error"
            )
            return
        
        self.romm_users = users_data
        
        # Get all existing links from database
        linked_usernames = set()
        linked_romm_ids = set()
        discord_to_romm = {}
        
        try:
            async with self.cog.db_manager.get_connection() as db:
                cursor = await db.execute("SELECT discord_id, romm_username, romm_id FROM user_links")
                rows = await cursor.fetchall()
                for row in rows:
                    discord_id, romm_username, romm_id = row
                    linked_usernames.add(romm_username.lower() if romm_username else "")
                    linked_romm_ids.add(romm_id)
                    discord_to_romm[discord_id] = romm_username
        except Exception as e:
            logger.error(f"Error fetching linked users: {e}")
        
        # Sort users and prepare full list with metadata
        self.full_romm_list = []
        for user in sorted(users_data, key=lambda u: u.get('username', '').lower()):
            username = user.get('username', 'Unknown')
            user_id = user.get('id')
            role = user.get('role', 'VIEWER')
            
            # Check if linked
            is_linked = (username.lower() in linked_usernames) or (user_id in linked_romm_ids)
            
            # Find which Discord user this is linked to
            linked_to = None
            for d_id, r_username in discord_to_romm.items():
                if r_username and r_username.lower() == username.lower():
                    try:
                        member = self.guild.get_member(int(d_id))
                        if member:
                            linked_to = member.display_name
                            break
                    except (ValueError, TypeError) as e:
                        # ValueError: invalid int conversion, TypeError: None value
                        logger.debug(f"Could not get member for discord_id {d_id}: {e}")
            
            self.full_romm_list.append({
                'user': user,
                'username': username,
                'user_id': user_id,
                'role': role,
                'is_linked': is_linked,
                'linked_to': linked_to
            })
        
        # Clear options and paginate
        self.romm_select.options.clear()
        
        # Calculate pagination
        total_users = len(self.full_romm_list)
        if total_users == 0:
            self.romm_select.add_option(
                label="No RomM users available",
                value="no_users",
                description="No users found in RomM"
            )
            self.romm_prev_button.disabled = True
            self.romm_next_button.disabled = True
            return
        
        total_pages = (total_users + self.page_size - 1) // self.page_size
        start_index = self.romm_current_page * self.page_size
        end_index = start_index + self.page_size
        
        # Get users for current page
        users_on_page = self.full_romm_list[start_index:end_index]
        
        # Update placeholder
        self.romm_select.placeholder = f"Select a RomM user (Page {self.romm_current_page + 1}/{total_pages})"
        
        # Add options for current page
        for user_data in users_on_page:
            emoji = "🔗" if user_data['is_linked'] else "🆓"
            status = f" (Linked to: {user_data['linked_to']})" if user_data['linked_to'] else " (Already linked)" if user_data['is_linked'] else ""
            
            self.romm_select.add_option(
                label=f"{emoji} {user_data['username'][:40]}{status[:30]}",
                value=user_data['username'],
                description=f"Role: {user_data['role']}"[:100]
            )
        
        # Update pagination button states
        self.romm_prev_button.disabled = self.romm_current_page == 0 or self.romm_select.disabled
        self.romm_next_button.disabled = end_index >= total_users or self.romm_select.disabled
    
    async def discord_select_callback(self, interaction: discord.Interaction):
        """Handle Discord user selection"""
        # Respond to the interaction first
        await interaction.response.edit_message(embed=self.create_status_embed(), view=self)
        
        if not self.discord_select.values:
            self.selected_discord_user = None
            self.update_button_states()
            return
            
        user_id = int(self.discord_select.values[0])
        self.selected_discord_user = self.guild.get_member(user_id)
        
        # Reset RomM pagination when Discord user changes
        self.romm_current_page = 0
        
        # Enable/disable buttons based on link status
        existing_link = self.discord_user_links.get(user_id)
        
        if existing_link:
            # User is linked
            self.unlink_button.disabled = False
            self.link_button.disabled = True
            self.romm_select.disabled = True
            self.invite_button.disabled = True
            self.romm_prev_button.disabled = True
            self.romm_next_button.disabled = True
        else:
            # User is not linked
            self.unlink_button.disabled = True
            self.link_button.disabled = True  # Will be enabled when RomM user selected
            self.romm_select.disabled = False
            self.invite_button.disabled = False
            
            # Populate RomM users if not already done
            if not self.romm_select.options or (len(self.romm_select.options) == 1 and self.romm_select.options[0].value == "placeholder"):
                await self.populate_romm_users()
        
        # Update the message with new state
        await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
    
    async def discord_prev_page_callback(self, interaction: discord.Interaction):
        """Go to the previous page of Discord users"""
        if self.discord_current_page > 0:
            self.discord_current_page -= 1
            await self.populate_discord_users()
            await interaction.response.edit_message(view=self, embed=self.create_status_embed())

    async def discord_next_page_callback(self, interaction: discord.Interaction):
        """Go to the next page of Discord users"""
        total_pages = (len(self.full_member_list) + self.page_size - 1) // self.page_size
        if self.discord_current_page < total_pages - 1:
            self.discord_current_page += 1
            await self.populate_discord_users()
            await interaction.response.edit_message(view=self, embed=self.create_status_embed())
    
    async def romm_prev_page_callback(self, interaction: discord.Interaction):
        """Go to the previous page of RomM users"""
        if self.romm_current_page > 0:
            self.romm_current_page -= 1
            await self.populate_romm_users()
            await interaction.response.edit_message(view=self, embed=self.create_status_embed())

    async def romm_next_page_callback(self, interaction: discord.Interaction):
        """Go to the next page of RomM users"""
        total_pages = (len(self.full_romm_list) + self.page_size - 1) // self.page_size
        if self.romm_current_page < total_pages - 1:
            self.romm_current_page += 1
            await self.populate_romm_users()
            await interaction.response.edit_message(view=self, embed=self.create_status_embed())
    
    async def romm_select_callback(self, interaction: discord.Interaction):
        """Handle RomM user selection"""
        await interaction.response.edit_message(embed=self.create_status_embed(), view=self)
        
        if not self.romm_select.values:
            self.selected_romm_user = None
            self.link_button.disabled = True
        else:
            username = self.romm_select.values[0]
            # Find user in the full list
            user_data = next(
                (u for u in self.full_romm_list if u['username'] == username),
                None
            )
            if user_data:
                self.selected_romm_user = user_data['user']
                self.link_button.disabled = False
            else:
                # Fallback to old method if full_romm_list isn't populated
                self.selected_romm_user = next(
                    (u for u in self.romm_users if u.get('username') == username),
                    None
                )
                self.link_button.disabled = False
        
        await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
    
    async def link_accounts_callback(self, interaction: discord.Interaction):
        """Link selected Discord and RomM accounts"""
        await interaction.response.defer()
        
        if not self.selected_discord_user or not self.selected_romm_user:
            await interaction.followup.send("Please select both users to link", ephemeral=True)
            return
        
        # Store the values before clearing them
        discord_user = self.selected_discord_user
        romm_username = self.selected_romm_user['username']
        romm_id = self.selected_romm_user['id']
        
        avatar_url = self.cog.get_member_avatar_url(discord_user)
        
        success = await self.cog.db_manager.add_user_link(
            discord_user.id,
            romm_username,
            romm_id,
            discord_user.display_name,
            avatar_url
        )
        
        if success:
            # Refresh the view
            await self.populate_discord_users()
            
            # Clear selections
            self.selected_romm_user = None
            self.selected_discord_user = None
            
            # Reset the RomM select dropdown
            self.romm_select.disabled = True
            self.romm_select.options.clear()
            self.romm_select.add_option(
                label="Select Discord user first",
                value="placeholder",
                description="Waiting for Discord user selection..."
            )
            
            self.update_button_states()
            
            # Use the stored values in the success message
            await interaction.followup.send(
                f"✅ Successfully linked {discord_user.mention} to RomM user `{romm_username}`",
                ephemeral=True
            )
            await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
        else:
            await interaction.followup.send("❌ Failed to link accounts", ephemeral=True)
       
    async def unlink_account_callback(self, interaction: discord.Interaction):
        """Unlink selected Discord user from RomM with options"""
        await interaction.response.defer()
        
        if not self.selected_discord_user:
            await interaction.followup.send("Please select a Discord user", ephemeral=True)
            return
        
        # Store the Discord user before any operations
        discord_user = self.selected_discord_user
        
        # Get the existing link
        link = await self.cog.db_manager.get_user_link(discord_user.id)
        if not link:
            await interaction.followup.send("This user has no linked account", ephemeral=True)
            return
        
        # Get the RomM user details
        romm_username = link['romm_username']
        romm_id = link.get('romm_id')  # Get the stored RomM ID if available
        
        # Try to find the user in the API if we don't have the ID
        user = None
        if romm_id:
            # If we have the ID stored, we can use it directly
            user = {'id': romm_id, 'username': romm_username}
        else:
            # Fall back to finding by username
            user = await self.cog.find_user_by_username(romm_username)
        
        # Check if it's an admin account
        if user:
            # Fetch full user data if needed to check admin status
            users_data = await self.bot.fetch_api_endpoint('users')
            if users_data:
                full_user = next((u for u in users_data if u.get('id') == user['id']), None)
                if full_user and full_user.get('role', '').upper() == 'ADMIN':
                    await interaction.followup.send(
                        "⚠️ Cannot unlink, disable, or delete admin accounts for safety. Remove admin role in RomM first.",
                        ephemeral=True
                    )
                    return
        
        # Show confirmation with options
        confirm_view = UnlinkConfirmView()
        
        embed = discord.Embed(
            title="🔗 Unlink Account Options",
            description=f"How would you like to unlink {discord_user.mention} from RomM user `{romm_username}`?",
            color=discord.Color.blue()
        )
        embed.add_field(
            name="🔓 Unlink Only",
            value="Remove Discord-RomM connection but keep the RomM account active",
            inline=False
        )
        embed.add_field(
            name="🔒 Unlink & Disable",
            value="Remove connection AND disable the RomM account (user cannot login but account is preserved)",
            inline=False
        )
        embed.add_field(
            name="🗑️ Unlink & Delete", 
            value="Remove connection AND delete the RomM account completely",
            inline=False
        )
        
        confirm_msg = await interaction.followup.send(
            embed=embed,
            view=confirm_view,
            ephemeral=True
        )
        
        await confirm_view.wait()
        
        if not confirm_view.action:
            await confirm_msg.edit(content="Operation cancelled.", embed=None, view=None)
            return
        
        # Process based on selected action
        if confirm_view.action == 'unlink_only':
            # Just remove the link from database
            await self.cog.db_manager.delete_user_link(discord_user.id)
            
            # Refresh the view
            await self.populate_discord_users()
            self.selected_discord_user = None
            self.update_button_states()
            
            await confirm_msg.edit(
                content=f"✅ Successfully unlinked {discord_user.mention} from RomM user `{romm_username}`\nThe RomM account remains active.",
                embed=None,
                view=None
            )
            
            # Log the action
            log_channel = self.bot.get_channel(self.cog.log_channel_id)
            if log_channel:
                await log_channel.send(
                    embed=discord.Embed(
                        title="🔓 Account Unlinked",
                        description=f"{discord_user.mention} unlinked from `{romm_username}` (account preserved)",
                        color=discord.Color.blue()
                    )
                )
        
        elif confirm_view.action == 'unlink_disable':
            # Disable the RomM account
            disable_success = False
            error_msg = None
            
            if user and 'id' in user:
                try:
                    logger.info(f"Attempting to disable RomM user: ID={user['id']}, Username={romm_username}")
                    
                    # Create a FormData object
                    form_data = aiohttp.FormData()
                    form_data.add_field('enabled', 'false') # API expects a string 'false' for form data
                    
                    # Pass the form_data object instead of params
                    result = await self.bot.make_authenticated_request(
                        method="PUT",
                        endpoint=f"users/{user['id']}",
                        form_data=form_data,
                        require_csrf=True
                    )
                    
                    if result is not None:
                        disable_success = True
                        logger.info(f"Successfully disabled RomM user {romm_username}")
                    else:
                        error_msg = "Failed to disable user"
                        logger.error(f"Failed to disable user {romm_username}: {error_msg}")
                        
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Exception while disabling user {romm_username}: {e}", exc_info=True)
            else:
                error_msg = "Could not find user in RomM"
                logger.error(f"User not found in RomM: {romm_username}")
            
            if disable_success:
                # Remove from database
                await self.cog.db_manager.delete_user_link(discord_user.id)
                
                # Refresh the view
                await self.populate_discord_users()
                self.selected_discord_user = None
                self.update_button_states()
                
                await confirm_msg.edit(
                    content=f"✅ Successfully unlinked {discord_user.mention} and disabled RomM account `{romm_username}`\nThe account exists but cannot login.",
                    embed=None,
                    view=None
                )
                
                # Log the action
                log_channel = self.bot.get_channel(self.cog.log_channel_id)
                if log_channel:
                    await log_channel.send(
                        embed=discord.Embed(
                            title="🔒 Account Unlinked & Disabled",
                            description=f"{discord_user.mention} unlinked from `{romm_username}` (account disabled)",
                            color=discord.Color.yellow()
                        )
                    )
            else:
                # Disable failed but we can still offer to unlink
                await confirm_msg.edit(
                    content=(
                        f"❌ Failed to disable RomM account for `{romm_username}`\n"
                        f"Error: {error_msg or 'Unknown error'}\n\n"
                        "The accounts have been unlinked, but the RomM account may still be active."
                    ),
                    embed=None,
                    view=None
                )
                
                # Still unlink even if disable failed
                await self.cog.db_manager.delete_user_link(discord_user.id)
                await self.populate_discord_users()
                self.selected_discord_user = None
                self.update_button_states()
            
        elif confirm_view.action == 'unlink_delete':
            # Try to delete the RomM account
            delete_success = False
            error_msg = None
            
            if user and 'id' in user:
                try:
                    # Log the deletion attempt
                    logger.info(f"Attempting to delete RomM user: ID={user['id']}, Username={romm_username}")
                    
                    # Use the delete method with proper error handling
                    result = await self.bot.make_authenticated_request(
                        method="DELETE",
                        endpoint=f"users/{user['id']}",
                        require_csrf=True
                    )
                    
                    # Check if deletion was successful
                    if result is not None or result == {}:  # API might return empty dict on success
                        delete_success = True
                        logger.info(f"Successfully deleted RomM user {romm_username}")
                    else:
                        error_msg = "API returned unexpected response"
                        logger.error(f"Failed to delete user {romm_username}: {error_msg}")
                        
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"Exception while deleting user {romm_username}: {e}", exc_info=True)
            else:
                error_msg = "Could not find user in RomM"
                logger.error(f"User not found in RomM: {romm_username}")
            
            if delete_success:
                # Remove from database
                await self.cog.db_manager.delete_user_link(discord_user.id)
                
                # Refresh the view
                await self.populate_discord_users()
                self.selected_discord_user = None
                self.update_button_states()
                
                await confirm_msg.edit(
                    content=f"✅ Successfully unlinked {discord_user.mention} and deleted RomM account `{romm_username}`",
                    embed=None,
                    view=None
                )
                
                # Log the deletion
                log_channel = self.bot.get_channel(self.cog.log_channel_id)
                if log_channel:
                    await log_channel.send(
                        embed=discord.Embed(
                            title="🗑️ Account Unlinked & Deleted",
                            description=f"{discord_user.mention} unlinked from `{romm_username}` (account deleted)",
                            color=discord.Color.red()
                        )
                    )
            else:
                # Deletion failed but we can still offer to unlink
                retry_view = discord.ui.View(timeout=30)
                
                unlink_anyway_btn = discord.ui.Button(
                    label="Unlink Anyway",
                    style=discord.ButtonStyle.primary
                )
                cancel_btn = discord.ui.Button(
                    label="Cancel",
                    style=discord.ButtonStyle.secondary
                )
                
                async def unlink_anyway_callback(inter: discord.Interaction):
                    await inter.response.defer()
                    await self.cog.db_manager.delete_user_link(discord_user.id)
                    await self.populate_discord_users()
                    self.selected_discord_user = None
                    self.update_button_states()
                    await inter.followup.send(
                        f"✅ Unlinked {discord_user.mention} from RomM (account may still exist in RomM)",
                        ephemeral=True
                    )
                    retry_view.stop()
                
                async def cancel_callback(inter: discord.Interaction):
                    await inter.response.defer()
                    retry_view.stop()
                
                unlink_anyway_btn.callback = unlink_anyway_callback
                cancel_btn.callback = cancel_callback
                
                retry_view.add_item(unlink_anyway_btn)
                retry_view.add_item(cancel_btn)
                
                await confirm_msg.edit(
                    content=(
                        f"❌ Failed to delete RomM account for `{romm_username}`\n"
                        f"Error: {error_msg or 'Unknown error'}\n\n"
                        "Would you like to unlink the accounts anyway? "
                        "(The RomM account will remain active)"
                    ),
                    embed=None,
                    view=retry_view
                )
        
        await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
    
    async def send_invite_callback(self, interaction: discord.Interaction):
        """Send an invite link to the selected Discord user by calling the main cog method."""
        await interaction.response.defer(ephemeral=True)
        
        if not self.selected_discord_user:
            await interaction.followup.send("Please select a Discord user.", ephemeral=True)
            return
        
        # Call the standardized method from the cog
        success = await self.cog.send_invite_link(self.selected_discord_user)
        
        if success:
            await interaction.followup.send(
                f"✅ Successfully sent invite link to {self.selected_discord_user.mention}",
                ephemeral=True
            )
        else:
            # The send_invite_link function already handles logging and DM failure notifications
            await interaction.followup.send(
                f"❌ Failed to send invite link to {self.selected_discord_user.mention}. See logs for details.",
                ephemeral=True
            )
    
    async def create_account_callback(self, interaction: discord.Interaction):
        """Create new RomM account for selected Discord user"""
        await interaction.response.defer()
        
        if not self.selected_discord_user:
            await interaction.followup.send("Please select a Discord user", ephemeral=True)
            return
        
        # Store the Discord user before operations
        discord_user = self.selected_discord_user
        
        # Check if already linked
        if self.discord_user_links.get(discord_user.id):
            await interaction.followup.send("This user already has a linked account", ephemeral=True)
            return
        
        # Create account
        success = await self.cog.create_user_account(discord_user, interactive=False)
        
        if success:
            # Refresh the view
            await self.populate_discord_users()
            
            # Clear selection after operations
            self.selected_discord_user = None
            self.update_button_states()
            
            await interaction.followup.send(
                f"✅ Successfully created RomM account for {discord_user.mention}",
                ephemeral=True
            )
            await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
        else:
            await interaction.followup.send("❌ Failed to create RomM account", ephemeral=True)
    
    async def bulk_create_callback(self, interaction: discord.Interaction):
        """Bulk send invites for users with the auto-register role"""
        if not self.cog.auto_register_role_id:
            await interaction.response.send_message(
                "❌ Auto-register role not configured",
                ephemeral=True
            )
            return
        
        await interaction.response.defer(ephemeral=True)
        
        role = self.guild.get_role(self.cog.auto_register_role_id)
        if not role:
            await interaction.followup.send("❌ Auto-register role not found", ephemeral=True)
            return
        
        # Find users needing invites
        members_to_invite = []
        for member in role.members:
            link = await self.cog.db_manager.get_user_link(member.id)
            if not link:
                members_to_invite.append(member)
        
        if not members_to_invite:
            await interaction.followup.send("✅ All role members already have accounts or pending invites", ephemeral=True)
            return
        
        # Create progress message
        progress_msg = await interaction.followup.send(
            f"Sending invites to {len(members_to_invite)} users...",
            ephemeral=True
        )
        
        sent = 0
        failed = 0
        
        for member in members_to_invite:
            if await self.cog.send_invite_link(member):
                sent += 1
            else:
                failed += 1
            
            # Update progress every 5 users
            if (sent + failed) % 5 == 0:
                try:
                    await interaction.edit_original_response(
                        content=f"Progress: {sent + failed}/{len(members_to_invite)} processed..."
                    )
                except (discord.NotFound, discord.HTTPException):
                    pass  # Ignore if message edit fails
        
        # Final summary
        try:
            await interaction.edit_original_response(
                content=f"✅ Sent: {sent} invites\n❌ Failed: {failed} invites"
            )
        except (discord.NotFound, discord.HTTPException):
            await interaction.followup.send(
                f"✅ Sent: {sent} invites\n❌ Failed: {failed} invites",
                ephemeral=True
            )
        
        # Refresh the view
        await self.populate_discord_users()
        await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
        
    async def refresh_callback(self, interaction: discord.Interaction):
        """Refresh all data"""
        await interaction.response.defer()
        
        await self.populate_discord_users()
        if not self.romm_select.disabled:
            await self.populate_romm_users()
        
        await interaction.followup.send("✅ Data refreshed", ephemeral=True)
        await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
    
    def update_button_states(self):
        """Update button states based on selections"""
        if not self.selected_discord_user:
            self.link_button.disabled = True
            self.unlink_button.disabled = True
            self.romm_select.disabled = True
            self.invite_button.disabled = True
        else:
            existing_link = self.discord_user_links.get(self.selected_discord_user.id)
            if existing_link:
                self.unlink_button.disabled = False
                self.link_button.disabled = True
                self.romm_select.disabled = True
                self.invite_button.disabled = True
            else:
                self.unlink_button.disabled = True
                self.romm_select.disabled = False
                self.link_button.disabled = not self.selected_romm_user
                self.invite_button.disabled = False
        
    def create_status_embed(self):
        """Create status embed showing current selections and stats"""
        embed = discord.Embed(
            title="👥 User Management System",
            color=discord.Color.blue()
        )
        
        # Current selection info
        if self.selected_discord_user:
            existing_link = self.discord_user_links.get(self.selected_discord_user.id)
            status = f"Linked to: `{existing_link}`" if existing_link else "Not linked"
            embed.add_field(
                name="Selected Discord User",
                value=f"{self.selected_discord_user.mention}\n{status}",
                inline=True
            )
        else:
            embed.add_field(
                name="Selected Discord User",
                value="None selected",
                inline=True
            )
        
        if self.selected_romm_user:
            embed.add_field(
                name="Selected RomM User",
                value=f"`{self.selected_romm_user['username']}`\nRole: {self.selected_romm_user.get('role', 'VIEWER')}",
                inline=True
            )
        else:
            embed.add_field(
                name="Selected RomM User",
                value="None selected",
                inline=True
            )
        
        # Statistics
        total_discord = len(self.discord_select.options) if self.discord_select.options else 0
        linked_count = sum(1 for link in self.discord_user_links.values() if link)
        
        embed.add_field(
            name="Statistics",
            value=f"Discord Users: {total_discord}\nLinked Accounts: {linked_count}\nUnlinked: {total_discord - linked_count}",
            inline=True
        )
        
        # Instructions
        embed.add_field(
            name="Instructions",
            value=(
                "1. Select a Discord user from the dropdown\n"
                "2. Choose an action:\n"
                "   • **Link**: Connect Discord account to existing RomM account\n"
                "   • **Unlink**: Remove connection and delete account\n"
                "   • **Send Invite Link**: Send an invite link to a selected Discord user, prompting them to create an account\n"
                "   • **Bulk Send Invites**: Send an invite link to all Auto Regester role members"
            ),
            inline=False
        )
        
        embed.set_footer(text="Changes are applied immediately")
        
        return embed

class ConfirmView(discord.ui.View):
    """Simple confirmation view"""
    def __init__(self):
        super().__init__(timeout=30)
        self.value = None
    
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.value = True
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        self.value = False
        await interaction.response.defer()
        self.stop()

class UnlinkConfirmView(discord.ui.View):
    """Confirmation view for unlinking with options to keep, disable, or delete RomM account"""
    def __init__(self):
        super().__init__(timeout=30)
        self.action = None  # Will be 'unlink_only', 'unlink_disable', or 'unlink_delete'
    
    @discord.ui.button(label="Unlink Only", style=discord.ButtonStyle.primary, emoji="🔓")
    async def unlink_only(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Only unlink the accounts, keep RomM user active"""
        self.action = 'unlink_only'
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Unlink & Disable", style=discord.ButtonStyle.secondary, emoji="🔒")
    async def unlink_disable(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Unlink accounts AND disable RomM user (keeps account but prevents login)"""
        self.action = 'unlink_disable'
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Unlink & Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def unlink_delete(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Unlink accounts AND delete RomM user completely"""
        self.action = 'unlink_delete'
        await interaction.response.defer()
        self.stop()
    
    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        """Cancel the operation"""
        self.action = None
        await interaction.response.defer()
        self.stop()
        
class UserManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        auto_register_role_id_env = os.getenv('AUTO_REGISTER_ROLE_ID')
        if auto_register_role_id_env and auto_register_role_id_env.isdigit():
            self.auto_register_role_id = int(auto_register_role_id_env)
        else:
            self.auto_register_role_id = 0 
        self.log_channel_id = self.bot.config.CHANNEL_ID
        self.temp_storage = {}
        
        # Use shared db and set db_manager to point to it
        self.db = bot.db
        self.db_manager = bot.db  
        
        logger.info(
            f"Users Cog initialized with auto_register_role_id: {self.auto_register_role_id}, "
            f"using main channel_id: {self.log_channel_id}"
        )

    async def cog_load(self):
        """Initialize when cog is loaded"""
       
        if not self.db_manager._initialized:
            logger.warning("Database not initialized, attempting initialization...")
            await self.db_manager.initialize()
        
        # await self.store_discord_info_for_existing_links()
        
        logger.debug("User Manager cog loaded successfully")
    
    async def generate_secure_password(self, length=16):
        """Generate a secure random password"""
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    async def get_or_create_dm_channel(self, user: discord.Member):
        """Get or create DM channel with user"""
        if user.dm_channel is None:
            await user.create_dm()
        return user.dm_channel

    def get_member_avatar_url(self, member: discord.Member) -> str:
        """Get the full avatar URL for a Discord member"""
        if member.avatar:
            return str(member.avatar.url)
        else:
            return str(member.default_avatar.url)

    async def sanitize_username(self, display_name: str) -> str:
        """async def link_accounts_callback
        Sanitize display name to create a valid username:
        - Replace spaces with underscores
        - Remove special characters
        - Convert to lowercase
        - Ensure unique if name exists
        """
        # Basic sanitization
        username = display_name.lower()
        username = ''.join(c for c in username if c.isalnum() or c in '_-')
        username = username.replace(' ', '_')
        
        # Ensure username is not empty after sanitization
        if not username:
            return f"user_{str(hash(display_name))[-8:]}"
            
        # Check if username exists and make unique if needed
        users = await self.bot.fetch_api_endpoint('users')
        if users:
            existing_usernames = [user.get('username', '') for user in users]
            original_username = username
            counter = 1
            
            while username in existing_usernames:
                username = f"{original_username}_{counter}"
                counter += 1
        
        return username

    async def is_romm_admin(self, user_data: Dict[str, Any]) -> bool:
        """Check if a user is a RomM admin"""
        return user_data.get('role', '').upper() == 'ADMIN'

    async def store_discord_info_for_existing_links(self):
        """Update existing links with Discord username and avatar info"""
        try:
            async with self.db_manager.get_connection() as db:
                # Get all links without Discord info
                cursor = await db.execute("""
                    SELECT discord_id FROM user_links
                    WHERE discord_username IS NULL OR discord_username = ''
                """)
                rows = await cursor.fetchall()

                for row in rows:
                    discord_id = row[0]
                    try:
                        # Get Discord user info
                        guild = self.bot.get_guild(self.bot.config.GUILD_ID)
                        member = guild.get_member(discord_id) if guild else None

                        if member:
                            discord_username = member.display_name
                            discord_avatar = member.avatar.key if member.avatar else None
                        else:
                            # Fallback to fetch_user
                            discord_user = await self.bot.fetch_user(discord_id)
                            discord_username = discord_user.display_name
                            discord_avatar = discord_user.avatar.key if discord_user.avatar else None

                        # Update database
                        await db.execute("""
                            UPDATE user_links
                            SET discord_username = ?, discord_avatar = ?
                            WHERE discord_id = ?
                        """, (discord_username, discord_avatar, discord_id))

                    except Exception as e:
                        logger.warning(f"Could not update Discord info for {discord_id}: {e}")

                # Note: get_connection() context manager handles commit automatically
                logger.info(f"Updated Discord info for {len(rows)} existing user links")

        except Exception as e:
            logger.error(f"Error updating existing links: {e}")
    
    async def delete_user(self, user_id: int) -> bool:
        """Delete user from the API"""
        try:
            # Use bot's helper method
            result = await self.bot.make_authenticated_request(
                method="DELETE",
                endpoint=f"users/{user_id}",
                require_csrf=True  # Though not needed with Bearer auth
            )
            return result is not None
            
        except Exception as e:
            logger.error(f"Error deleting user {user_id}: {e}", exc_info=True)
            return False

    async def find_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Find a user by their username in the API"""
        try:
            # Use bot's standard fetch method
            users_data = await self.bot.fetch_api_endpoint('users')
            
            if users_data:
                logger.info(f"Searching for user with username: {username}")
                logger.info(f"Available usernames: {[user.get('username', '') for user in users_data]}")
                return next(
                    (user for user in users_data if user.get('username', '').lower() == username.lower()),
                    None
                )
            return None
        except Exception as e:
            logger.error(f"Error finding user {username}: {e}", exc_info=True)
            return None
    
    async def send_invite_link(self, member: discord.Member, role: str = "viewer") -> bool:
        """Send a standardized invite link to a Discord member."""
        try:
            existing_link = await self.db_manager.get_user_link(member.id)
            if existing_link:
                logger.info(f"User {member.display_name} already has a linked account.")
                return True

            invite_data = await self.bot.make_authenticated_request(
                method="POST",
                endpoint="users/invite-link",
                params={"role": role}
            )
            
            if not invite_data or 'token' not in invite_data:
                logger.error(f"Failed to create invite link for {member.display_name}")
                return False
            
            invite_token = invite_data.get('token')
            invite_url = f"{self.bot.config.DOMAIN}/register?token={invite_token}"
            
            try:
                dm_channel = await self.get_or_create_dm_channel(member)
                
                # This is the new, standardized embed
                embed = discord.Embed(
                    title="🎮 RomM Invitation",
                    description=f"You've been invited to access the game library at {self.bot.config.DOMAIN}!",
                    color=discord.Color.blue()
                )
                embed.add_field(
                    name="📧 Invitation Link",
                    value=f"[Click here to register your account]({invite_url})",
                    inline=False
                )
                embed.add_field(
                    name="ℹ️ What is RomM?",
                    value="RomM is a ROM management system for browsing and playing a game collection from any device.",
                    inline=False
                )
                embed.add_field(
                    name="⚠️ Important",
                    value="This invitation link is single-use. Once you register, it cannot be used again.",
                    inline=False
                )
                embed.set_footer(text=f"Your account will have {role} permissions. If you already have an account, you can ignore this.")
                
                await dm_channel.send(embed=embed)
                
                log_channel = self.bot.get_channel(self.log_channel_id)
                if log_channel:
                    log_embed = discord.Embed(
                        title="📧 Invite Link Sent",
                        description=f"Sent RomM invite to {member.mention}",
                        color=discord.Color.green()
                    )
                    await log_channel.send(embed=log_embed)
                
                logger.info(f"Successfully sent invite link to {member.display_name}")
                return True
                
            except discord.Forbidden:
                logger.warning(f"Could not DM {member.display_name} - DMs may be disabled.")
                log_channel = self.bot.get_channel(self.log_channel_id)
                if log_channel:
                    await log_channel.send(
                        embed=discord.Embed(
                            title="⚠️ Invite Delivery Failed",
                            description=(
                                f"Could not DM invite link to {member.mention}\n"
                                f"**Invite URL:** ||{invite_url}||\n"
                                "Please send this link to them manually."
                            ),
                            color=discord.Color.yellow()
                        )
                    )
                return False
                
        except Exception as e:
            logger.error(f"Error sending invite link to {member.display_name}: {e}", exc_info=True)
            return False
    
    async def handle_role_removal(self, member: discord.Member) -> bool:
        """Handle removal of the auto-register role"""
        try:
            # Check if user exists in our database
            user_link = await self.db_manager.get_user_link(member.id)
            if not user_link:
                logger.warning(f"No RomM account found for {member.display_name}")
                return False

            # Get the user from RomM API
            user = await self.find_user_by_username(user_link['romm_username'])
            if not user:
                logger.warning(f"User not found in RomM: {user_link['romm_username']}")
                return False
            
            # Check if user is a RomM admin
            if await self.is_romm_admin(user):
                logger.warning(f"Attempted to delete admin account for {member.display_name}, skipping")
                
                # Notify admins in log channel
                log_channel = self.bot.get_channel(self.log_channel_id)
                if log_channel:
                    await log_channel.send(
                        embed=discord.Embed(
                            title="⚠️ Admin Account Protection",
                            description=(
                                f"Attempted to delete admin account for {member.mention}\n"
                                "Account was preserved due to admin status."
                            ),
                            color=discord.Color.yellow()
                        )
                    )
                return False

            # Delete the non-admin user
            if await self.delete_user(user['id']):
                # Remove from our database
                await self.db_manager.delete_user_link(member.id)
                logger.info(f"Deleted user account for {member.display_name}")
                
                # Notify the user
                dm_channel = await self.get_or_create_dm_channel(member)
                embed = discord.Embed(
                    title="RomM Account Removed",
                    description=(
                        "Your RomM account has been removed due to role changes.\n"
                        "If this was a mistake, please contact an administrator."
                    ),
                    color=discord.Color.red()
                )
                await dm_channel.send(embed=embed)
                
                # Log the deletion
                log_channel = self.bot.get_channel(self.log_channel_id)
                if log_channel:
                    await log_channel.send(
                        embed=discord.Embed(
                            title="User Account Removed",
                            description=f"Account removed for {member.mention}",
                            color=discord.Color.red()
                        )
                    )
                return True
            return False
        except Exception as e:
            logger.error(f"Error handling role removal for {member.display_name}: {e}", exc_info=True)
            return False

    async def link_existing_account(self, member: discord.Member) -> Optional[Dict[str, Any]]:
        """Check for existing account and prompt user to link it"""
        try:
            dm_channel = await self.get_or_create_dm_channel(member)
            
            view = discord.ui.View(timeout=300)
            yes_button = discord.ui.Button(label="Yes", style=discord.ButtonStyle.green, custom_id="yes")
            no_button = discord.ui.Button(label="No", style=discord.ButtonStyle.red, custom_id="no")
            
            async def yes_callback(interaction: discord.Interaction):
                if interaction.user.id != member.id:
                    return
                await interaction.response.send_message("Please enter your existing RomM username:")
                
                def message_check(m):
                    return m.author.id == member.id and isinstance(m.channel, discord.DMChannel)

                try:
                    username_msg = await self.bot.wait_for('message', timeout=300.0, check=message_check)
                    existing_username = username_msg.content.strip()
                    logger.info(f"Received username from user: {existing_username}")
                    
                    existing_user = await self.find_user_by_username(existing_username)
                    
                    if existing_user:
                        # Check if it's an admin account
                        if await self.is_romm_admin(existing_user):
                            logger.info(f"Admin account found for {existing_username}")
                            # Store link in database
                            await self.db_manager.add_user_link(
                                member.id,
                                existing_username,
                                existing_user['id'],
                                member.display_name,
                                member.avatar.key if member.avatar else None
                            )
                            
                            await dm_channel.send(
                                embed=discord.Embed(
                                    title="✅ Admin Account Linked",
                                    description=(
                                        "Your admin account has been verified and linked.\n"
                                        f"Continue using your existing username `{existing_username}` to log in at {self.bot.config.DOMAIN}\n"
                                        "Your password remains unchanged."
                                    ),
                                    color=discord.Color.green()
                                )
                            )
                            view.stop()
                            self.temp_storage[member.id] = existing_user
                            
                            # Log admin account linking
                            log_channel = self.bot.get_channel(self.log_channel_id)
                            if log_channel:
                                await log_channel.send(
                                    embed=discord.Embed(
                                        title="Admin Account Linked",
                                        description=(
                                            f"User {member.mention} has linked their Discord account to "
                                            f"admin account `{existing_username}`"
                                        ),
                                        color=discord.Color.gold()
                                    )
                                )
                            return
                            
                        # For non-admin accounts, proceed with username update
                        new_username = await self.sanitize_username(member.display_name)
                        logger.info(f"Attempting to update username from {existing_username} to {new_username}")
                        
                        # Use bot's helper for update
                        update_params = {"username": new_username}
                        
                        result = await self.bot.make_authenticated_request(
                            method="PUT",
                            endpoint=f"users/{existing_user['id']}",
                            params=update_params,
                            require_csrf=True
                        )
                        
                        if result:
                            # Store link in database
                            await self.db_manager.add_user_link(
                                member.id,
                                existing_username,
                                existing_user['id'],
                                member.display_name,
                                member.avatar.key if member.avatar else None
                            )
                            
                            await dm_channel.send(
                                embed=discord.Embed(
                                    title="✅ Account Linked Successfully",
                                    description=(
                                        "Your existing account has been linked to your Discord profile.\n"
                                        f"You can now log in at {self.bot.config.DOMAIN} using:\n"
                                        f"Username: `{new_username}`\n"
                                        "Password: [Your existing password]"
                                    ),
                                    color=discord.Color.green()
                                )
                            )
                            view.stop()
                            self.temp_storage[member.id] = existing_user
                        else:
                                error_msg = await dm_channel.send(
                                    embed=discord.Embed(
                                        title="❌ Account Linking Failed",
                                        description=(
                                            "Failed to update your account. This could be because:\n"
                                            "1. The username is already taken\n"
                                            "2. There was a server error\n\n"
                                            "Creating new account instead..."
                                        ),
                                        color=discord.Color.red()
                                    )
                                )
                                view.stop()
                                self.temp_storage[member.id] = None

                    else:
                        await dm_channel.send(
                            embed=discord.Embed(
                                title="❌ Username Not Found",
                                description=(
                                    f"No account found with username: {existing_username}\n"
                                    "Creating new account instead..."
                                ),
                                color=discord.Color.red()
                            )
                        )
                        view.stop()
                        self.temp_storage[member.id] = None
                        
                except asyncio.TimeoutError:
                    await dm_channel.send("No response received. Creating new account instead...")
                    view.stop()
                    self.temp_storage[member.id] = None

            async def no_callback(interaction: discord.Interaction):
                if interaction.user.id != member.id:
                    return
                await interaction.response.send_message("Creating new account for you...")
                view.stop()
                self.temp_storage[member.id] = None

            yes_button.callback = yes_callback
            no_button.callback = no_callback
            view.add_item(yes_button)
            view.add_item(no_button)
            
            # Store for result
            self.temp_storage[member.id] = None
            
            # Send message with buttons
            initial_embed = discord.Embed(
                title="🔗 Link Existing RomM Account",
                description=(
                    f"You've been granted access to RomM at {self.bot.config.DOMAIN}. "
                    "Do you already have an account you'd like to link to your Discord profile?"
                ),
                color=discord.Color.blue()
            )
            
            await dm_channel.send(embed=initial_embed, view=view)
            
            # Wait for the view to finish
            await view.wait()
            
            # Return the result
            result = self.temp_storage.get(member.id)
            return result

        except Exception as e:
            logger.error(f"Error in link_existing_account for {member.display_name}: {e}", exc_info=True)
            return None
        finally:
            # Always clean up temp_storage to prevent memory leak
            self.temp_storage.pop(member.id, None)    

    async def create_user_account(self, member: discord.Member, interactive: bool = True, use_invite: bool = True) -> bool:
        """
        Create or invite a user account.
        
        Args:
            member: Discord member to create account for
            interactive: Whether to interact with the user (ask about existing accounts)
            use_invite: Whether to use invite links (True) or direct creation (False)
        """
        if use_invite:
            # Use the new invite-based system
            return await self.send_invite_link(member)
        
        try:
            # Check if user already has a linked account
            existing_link = await self.db_manager.get_user_link(member.id)
            if existing_link:
                logger.info(f"User {member.display_name} already has a linked account")
                # Notify user
                dm_channel = await self.get_or_create_dm_channel(member)
                await dm_channel.send(
                    embed=discord.Embed(
                        title="Existing Account Found",
                        description=(
                            f"You already have a RomM account with username: {existing_link['romm_username']}\n"
                            "You can continue using this account."
                        ),
                        color=discord.Color.blue()
                    )
                )
                return True

            # Check for existing account to link
            romm_user = await self.link_existing_account(member)
            if romm_user:
                return True  # Account was linked in link_existing_account

            # Fix: Use bot's method for token validation
            if not await self.bot.ensure_valid_token():
                logger.error("Failed to obtain OAuth token")
                return False
            
            username = await self.sanitize_username(member.display_name)
            password = await self.generate_secure_password()
            
            # Prepare form data
            form_data = aiohttp.FormData()
            form_data.add_field('username', username)
            form_data.add_field('password', password)
            form_data.add_field('email', 'none')  # Required field
            form_data.add_field('role', 'VIEWER')
            
            # Create user using bot's helper (it handles CSRF automatically)
            response_data = await self.bot.make_authenticated_request(
                method="POST",
                endpoint="users",
                form_data=form_data,
                require_csrf=True
            )
            
            if response_data:
                # Store link in database
                await self.db_manager.add_user_link(
                    member.id,
                    username,
                    response_data['id'],
                    member.display_name,
                    member.avatar.key if member.avatar else None
                )
                
                # Send DM with credentials
                dm_channel = await self.get_or_create_dm_channel(member)
                embed = discord.Embed(
                    title="🎉 RomM Account Created!",
                    description=f"Your account for {self.bot.config.DOMAIN} has been created.",
                    color=discord.Color.green()
                )
                embed.add_field(name="Username", value=username, inline=False)
                embed.add_field(name="Password", value=f"||{password}||", inline=False)
                embed.add_field(
                    name="⚠️ Important", 
                    value=f"Please login at {self.bot.config.DOMAIN} and change your password.", 
                    inline=False
                )
                
                await dm_channel.send(embed=embed)

                # Log success
                log_channel = self.bot.get_channel(self.log_channel_id)
                if log_channel:
                    await log_channel.send(
                        embed=discord.Embed(
                            title="New User Account Created",
                            description=f"Account created for {member.mention} with username `{username}`",
                            color=discord.Color.blue()
                        )
                    )
                
                return True
            else:
                logger.error(f"Failed to create user account for {member.display_name}")
                return False

        except Exception as e:
            logger.error(f"Error creating account for {member.display_name}: {e}", exc_info=True)
            return False
    
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Listen for role changes and handle account invitation/deletion"""
        if self.auto_register_role_id == 0:
            return

        had_role = any(role.id == self.auto_register_role_id for role in before.roles)
        has_role = any(role.id == self.auto_register_role_id for role in after.roles)
        
        if has_role and not had_role:
            # Role was added - send invite link instead of creating account
            logger.info(f"Auto-register role added to {after.display_name}, sending invite link")
            await self.send_invite_link(after)
        elif had_role and not has_role:
            # Role was removed
            logger.info(f"Auto-register role removed from {after.display_name}, removing account if exists")
            await self.handle_role_removal(after)

    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize database when cog is ready"""
        logger.info("UserManager Cog is ready!")

    @discord.slash_command(name="user_manager", description="User management interface (admin only)")
    @is_admin()
    async def user_manager(self, ctx: discord.ApplicationContext):
        """Open the comprehensive user management interface"""
        await ctx.defer(ephemeral=True)
        
        # Ensure we have valid authentication
        if not await self.bot.ensure_valid_token():
            await ctx.followup.send("❌ Failed to authenticate with API!", ephemeral=True)
            return
        
        # Force refresh of user data before opening the interface
        try:
            # Bypass cache to get fresh user data
            fresh_users = await self.bot.fetch_api_endpoint('users', bypass_cache=True)
            if fresh_users:
                logger.info(f"Refreshed user data: {len(fresh_users)} users found")
            else:
                logger.warning("Failed to refresh user data from API")
        except Exception as e:
            logger.error(f"Error refreshing user data: {e}")
            # Continue anyway - the view will try to fetch data itself
        
        # Create and populate the view
        view = UserManagementView(self.bot, self, ctx.guild)
        await view.populate_discord_users()
        
        # Send initial message
        embed = view.create_status_embed()
        message = await ctx.followup.send(embed=embed, view=view, ephemeral=True)
        
        if isinstance(message, discord.Interaction):
            view.message = await message.original_response()
        else:
            view.message = message

def setup(bot):
    """Setup function with enable check"""
    if os.getenv('ENABLE_USER_MANAGER', 'TRUE').upper() == 'FALSE':
        logger.info("UserManager Cog is disabled via ENABLE_USER_MANAGER")
        return
    
    bot.add_cog(UserManager(bot))
    #logger.info("UserManager Cog enabled and loaded")
