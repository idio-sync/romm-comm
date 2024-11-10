import discord
from discord.ext import commands
from typing import Optional, Dict, Any
import logging
import secrets
import string
import asyncio
import aiohttp
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

logger = logging.getLogger('romm_bot.users')

class UserManager(commands.Cog):
    """
    User management cog for RomM bot.
    Handles automatic user creation and management when roles are assigned or removed.
    """
    def __init__(self, bot):
        self.bot = bot
        self.auto_register_role_id = int(os.getenv('AUTO_REGISTER_ROLE_ID', 0))
        self.log_channel_id = self.bot.config.CHANNEL_ID
        self.temp_storage = {}
        
        logger.info(
            f"Users Cog initialized with auto_register_role_id: {self.auto_register_role_id}, "
            f"using main channel_id: {self.log_channel_id}"
        )

    async def generate_secure_password(self, length=16):
        """Generate a secure random password"""
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    async def get_or_create_dm_channel(self, user: discord.Member):
        """Get or create DM channel with user"""
        if user.dm_channel is None:
            await user.create_dm()
        return user.dm_channel

    async def sanitize_username(self, display_name: str) -> str:
        """
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

    async def update_user(self, user_id: int, update_data: Dict[str, Any]) -> bool:
        """Update existing user in the API"""
        try:
            session = await self.bot.ensure_session()
            url = f"{self.bot.config.API_BASE_URL}/api/users/{user_id}"
            auth = aiohttp.BasicAuth(self.bot.config.USER, self.bot.config.PASS)
            
            logger.info(f"Attempting to update user {user_id} with data: {update_data}")
            
            async with session.put(url, json=update_data, auth=auth) as response:
                response_text = await response.text()
                logger.info(f"Update response status: {response.status}")
                logger.info(f"Update response body: {response_text}")
                
                if response.status != 200:
                    logger.error(f"Failed to update user {user_id}. Status: {response.status}, Response: {response_text}")
                    return False
                return True
                
        except Exception as e:
            logger.error(f"Error updating user {user_id}: {e}", exc_info=True)
            return False

    async def delete_user(self, user_id: int) -> bool:
        """Delete user from the API"""
        try:
            session = await self.bot.ensure_session()
            url = f"{self.bot.config.API_BASE_URL}/api/users/{user_id}"
            auth = aiohttp.BasicAuth(self.bot.config.USER, self.bot.config.PASS)
            
            async with session.delete(url, auth=auth) as response:
                return response.status == 200
        except Exception as e:
            logger.error(f"Error deleting user {user_id}: {e}", exc_info=True)
            return False

    async def find_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Find a user by their username in the API"""
        try:
            users = await self.bot.fetch_api_endpoint('users')
            logger.info(f"Searching for user with username: {username}")
            
            if users:
                # Log all usernames for debugging
                logger.info(f"Available usernames: {[user.get('username', '') for user in users]}")
                
                found_user = next(
                    (user for user in users if user.get('username', '').lower() == username.lower()),
                    None
                )
                
                if found_user:
                    logger.info(f"Found user: {found_user}")
                else:
                    logger.info(f"No user found with username: {username}")
                    
                return found_user
            return None
        except Exception as e:
            logger.error(f"Error finding user {username}: {e}", exc_info=True)
            return None
    
    async def is_romm_admin(self, user_data: Dict[str, Any]) -> bool:
        """Check if a user is a RomM admin"""
        return user_data.get('role', '').upper() == 'ADMIN'
    
    async def handle_role_removal(self, member: discord.Member) -> bool:
        """Handle removal of the auto-register role"""
        try:
            # Find user by their username
            username = await self.sanitize_username(member.display_name)
            user = await self.find_user_by_username(username)
            
            if not user:
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
                        
                        update_result = await self.update_user(
                            existing_user['id'], 
                            {"username": new_username}
                        )
                        
                        logger.info(f"Update result: {update_result}")
                        
                        if update_result:
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
            self.temp_storage = {}
            
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
            self.temp_storage.pop(member.id, None)
            return result
                
        except Exception as e:
            logger.error(f"Error in link_existing_account for {member.display_name}: {e}", exc_info=True)
            return None

    async def get_csrf_token(self) -> Optional[str]:
        """Get CSRF token from the API"""
        try:
            session = await self.bot.ensure_session()
            url = f"{self.bot.config.API_BASE_URL}/api/auth/login"
            
            async with session.get(url) as response:
                if response.status == 200:
                    # Get CSRF token from cookies
                    cookies = response.cookies
                    csrf_token = cookies.get('csrf_token')
                    if csrf_token:
                        return csrf_token.value
                logger.error(f"Failed to get CSRF token. Status: {response.status}")
                return None
        except Exception as e:
            logger.error(f"Error getting CSRF token: {e}")
            return None


    async def create_user_account(self, member: discord.Member) -> bool:
        """Create a new user account and send credentials"""
        try:
            # First check for and attempt to link existing account
            existing_account = await self.link_existing_account(member)
            if existing_account:
                return True

            # Generate username from display name
            username = await self.sanitize_username(member.display_name)
            logger.info(f"Attempting to create new user with username: {username}")
            
            # Generate password and create new regular user
            password = await self.generate_secure_password()
            
            # Get CSRF token
            csrf_token = await self.get_csrf_token()
            if not csrf_token:
                logger.error("Failed to get CSRF token")
                return False
            
            session = await self.bot.ensure_session()
            url = f"{self.bot.config.API_BASE_URL}/api/users"
            auth = aiohttp.BasicAuth(self.bot.config.USER, self.bot.config.PASS)
            
            # Create form data
            form_data = aiohttp.FormData()
            form_data.add_field('username', username)
            form_data.add_field('password', password)
            form_data.add_field('role', 'USER')
            form_data.add_field('enabled', 'true')
            
            logger.info(f"Making POST request to {url} for new user {username}")
            
            headers = {
                "Accept": "application/json",
                "X-CSRF-TOKEN": csrf_token,
                "Cookie": f"csrf_token={csrf_token}"
            }
            
            async with session.post(
                url, 
                data=form_data,
                auth=auth,
                headers=headers,
                allow_redirects=True
            ) as response:
                response_text = await response.text()
                logger.info(f"Create user response status: {response.status}")
                logger.info(f"Create user response body: {response_text}")
                logger.info(f"Response headers: {response.headers}")
                
                if response.status not in (200, 201):
                    logger.error(f"Failed to create user account. Status: {response.status}, Response: {response_text}")
                    
                    # Send error notification to log channel
                    log_channel = self.bot.get_channel(self.log_channel_id)
                    if log_channel:
                        await log_channel.send(
                            embed=discord.Embed(
                                title="⚠️ User Creation Failed",
                                description=(
                                    f"Failed to create account for {member.mention}\n"
                                    f"Status: {response.status}\n"
                                    f"Response: {response_text}"
                                ),
                                color=discord.Color.red()
                            )
                        )
                    
                    # Notify user of the error
                    dm_channel = await self.get_or_create_dm_channel(member)
                    await dm_channel.send(
                        embed=discord.Embed(
                            title="❌ Account Creation Failed",
                            description=(
                                "Sorry, there was an error creating your account.\n"
                                "This has been reported to the administrators.\n"
                                "Please try again later or contact an administrator for help."
                            ),
                            color=discord.Color.red()
                        )
                    )
                    return False

            # If we got here, account was created successfully
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

        except Exception as e:
            logger.error(f"Error creating account for {member.display_name}: {e}", exc_info=True)
            
            # Log unexpected errors
            log_channel = self.bot.get_channel(self.log_channel_id)
            if log_channel:
                await log_channel.send(
                    embed=discord.Embed(
                        title="⚠️ User Creation Error",
                        description=(
                            f"Unexpected error creating account for {member.mention}\n"
                            f"Error: {str(e)}"
                        ),
                        color=discord.Color.red()
                    )
                )
            return False

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Listen for role changes and handle account creation/deletion"""
        if self.auto_register_role_id == 0:
            return

        had_role = any(role.id == self.auto_register_role_id for role in before.roles)
        has_role = any(role.id == self.auto_register_role_id for role in after.roles)
        
        if has_role and not had_role:
            # Role was added
            logger.info(f"Auto-register role added to {after.display_name}, creating account")
            await self.create_user_account(after)
        elif had_role and not has_role:
            # Role was removed
            logger.info(f"Auto-register role removed from {after.display_name}, removing account")
            await self.handle_role_removal(after)

    @commands.Cog.listener()
    async def on_ready(self):
        """Log when cog is ready"""
        logger.info("UserManager Cog is ready!")

    @discord.slash_command(
        name="sync_users",
        description="Sync all users who have the auto-register role (admin only)"
    )
    @commands.has_permissions(administrator=True)
    async def sync_users(self, ctx):
        """Sync all users who have the auto-register role"""
        if self.auto_register_role_id == 0:
            await ctx.respond("❌ Auto-register role ID not configured!", ephemeral=True)
            return

        await ctx.defer()

        role = ctx.guild.get_role(self.auto_register_role_id)
        if not role:
            await ctx.respond("❌ Auto-register role not found!", ephemeral=True)
            return

        existing_users = await self.bot.fetch_api_endpoint('users')
        if existing_users is None:
            await ctx.respond("❌ Failed to fetch existing users!", ephemeral=True)
            return

        # Get sanitized usernames for comparison
        members_to_sync = []
        processed_members = set()  # Keep track of processed members
        
        for member in role.members:
            if member.id in processed_members:
                continue
                
            username = await self.sanitize_username(member.display_name)
            if not any(user.get('username') == username for user in existing_users):
                members_to_sync.append(member)
            processed_members.add(member.id)

        if not members_to_sync:
            await ctx.respond("✅ All users are already synced!", ephemeral=True)
            return

        total_members = len(members_to_sync)
        progress_msg = await ctx.respond(f"Starting sync for {total_members} users...")
        
        created = 0
        failed = 0
        skipped = 0
        
        for index, member in enumerate(members_to_sync, 1):
            try:
                if await self.create_user_account(member):
                    created += 1
                else:
                    failed += 1

                # Update progress every few users
                if index % 3 == 0:
                    await ctx.edit(
                        f"Progress: {index}/{total_members}\n"
                        f"✅ Created: {created}\n"
                        f"❌ Failed: {failed}\n"
                        f"⏭️ Skipped: {skipped}"
                    )
                    
                # Add a small delay between users to prevent rate limiting
                await asyncio.sleep(1.5)
                
            except Exception as e:
                logger.error(f"Error processing member {member.display_name}: {e}")
                failed += 1

        final_embed = discord.Embed(
            title="User Sync Complete",
            description=(
                f"✅ Successfully created: {created} accounts\n"
                f"❌ Failed to create: {failed} accounts\n"
                f"⏭️ Skipped: {skipped} accounts"
            ),
            color=discord.Color.green() if failed == 0 else discord.Color.orange()
        )
        await ctx.respond(embed=final_embed)

def setup(bot):
    """Setup function with enable check"""
    if os.getenv('ENABLE_USER_MANAGER', 'TRUE').upper() == 'FALSE':
        logger.info("UserManager Cog is disabled via ENABLE_USER_MANAGER")
        return
    
    bot.add_cog(UserManager(bot))
    logger.info("UserManager Cog enabled and loaded")