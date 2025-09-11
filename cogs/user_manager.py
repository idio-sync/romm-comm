import discord
from discord.ext import commands, tasks
from typing import Optional, Dict, Any
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

class AsyncUserDatabaseManager:
    def __init__(self, db_path: str = "data/users.db"):
        self.db_path = db_path
        self._ensure_db_directory()
        self._initialized = False
        
    def _ensure_db_directory(self):
        """Ensure the data directory exists"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    async def initialize(self):
        """Initialize the database with required tables"""
        if self._initialized:
            return
            
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_links (
                        discord_id INTEGER PRIMARY KEY,
                        romm_username TEXT NOT NULL,
                        romm_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.commit()
                self._initialized = True
                logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing database: {e}", exc_info=True)
            raise
            
    async def _ensure_initialized(self):
        """Ensure database is initialized before operations"""
        if not self._initialized:
            await self.initialize()

    async def add_user_link(self, discord_id: int, romm_username: str, romm_id: int) -> bool:
        """Add a new Discord to RomM user link"""
        try:
            await self._ensure_initialized()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO user_links (discord_id, romm_username, romm_id) VALUES (?, ?, ?)",
                    (discord_id, romm_username, romm_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding user link: {e}", exc_info=True)
            return False

    async def get_user_link(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """Get RomM user info for a Discord user"""
        try:
            await self.initialize()  # Ensure table exists
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute(
                    "SELECT romm_username, romm_id, created_at FROM user_links WHERE discord_id = ?",
                    (discord_id,)
                ) as cursor:
                    result = await cursor.fetchone()
                    if result:
                        return {
                            "romm_username": result[0],
                            "romm_id": result[1],
                            "created_at": result[2]
                        }
                    return None
        except Exception as e:
            logger.error(f"Error getting user link: {e}", exc_info=True)
            return None

    async def delete_user_link(self, discord_id: int) -> bool:
        """Delete a Discord to RomM user link"""
        try:
            await self._ensure_initialized()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM user_links WHERE discord_id = ?", (discord_id,))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error deleting user link: {e}", exc_info=True)
            return False

class UserManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.auto_register_role_id = int(os.getenv('AUTO_REGISTER_ROLE_ID', 0))
        self.log_channel_id = self.bot.config.CHANNEL_ID
        self.temp_storage = {}
        self.db_manager = AsyncUserDatabaseManager()
        
        logger.info(
            f"Users Cog initialized with auto_register_role_id: {self.auto_register_role_id}, "
            f"using main channel_id: {self.log_channel_id}"
        )

    async def cog_load(self):
        """Initialize when cog is loaded"""
        await self.db_manager.initialize()
        logger.info("User Manager database initialized!")
    
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

    async def is_romm_admin(self, user_data: Dict[str, Any]) -> bool:
        """Check if a user is a RomM admin"""
        return user_data.get('role', '').upper() == 'ADMIN'

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
                                existing_user['id']
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
                                new_username,
                                existing_user['id']
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
            self.temp_storage.pop(member.id, None)
            return result
                
        except Exception as e:
            logger.error(f"Error in link_existing_account for {member.display_name}: {e}", exc_info=True)
            return None    

    async def create_user_account(self, member: discord.Member) -> bool:
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

            # Ensure we have valid token and CSRF token
            if not await self.ensure_token():
                logger.error("Failed to obtain OAuth token")
                return False
                
            session = await self.bot.ensure_session()
            
            # Get fresh CSRF token from heartbeat endpoint
            heartbeat_url = f"{self.bot.config.API_BASE_URL}/heartbeat"
            
            async with session.get(heartbeat_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get heartbeat. Status: {response.status}")
                    return False
                    
                set_cookie = response.headers.get('Set-Cookie')
                if not set_cookie:
                    logger.error("No Set-Cookie header in response")
                    return False
                
                csrf_token = set_cookie.split('romm_csrftoken=')[1].split(';')[0]
                logger.info(f"Got CSRF token for user creation: {csrf_token[:10]}...")
            
            username = await self.sanitize_username(member.display_name)
            password = await self.generate_secure_password()
            
            # Prepare form data
            form_data = aiohttp.FormData()
            form_data.add_field('username', username)
            form_data.add_field('password', password)
            form_data.add_field('email', 'none')  # Required field
            form_data.add_field('role', 'VIEWER')
            
            # Create user using bot's helper
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
                    response_data['id']
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

    def cog_unload(self):
        """Clean up when cog is unloaded"""
        self.refresh_token_task.cancel()
    
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
        """Initialize database when cog is ready"""
        logger.info("UserManager Cog is ready!")

    @discord.slash_command(name="sync_users")
    @commands.has_permissions(administrator=True)
    async def sync_users(self, ctx: discord.ApplicationContext):
        """Sync all users who have the auto-register role"""
        if self.auto_register_role_id == 0:
            await ctx.respond("❌ Auto-register role ID not configured!", ephemeral=True)
            return

        await ctx.defer()

        # Ensure we have a valid token before starting sync
        if not await self.ensure_token():
            await ctx.respond("❌ Failed to authenticate with API!", ephemeral=True)
            return

        role = ctx.guild.get_role(self.auto_register_role_id)
        if not role:
            await ctx.respond("❌ Auto-register role not found!", ephemeral=True)
            return

        members_to_sync = []
        for member in role.members:
            # Check if user already has a linked account
            existing_link = await self.db_manager.get_user_link(member.id)
            if not existing_link:
                members_to_sync.append(member)

        if not members_to_sync:
            await ctx.respond("✅ All users are already synced!", ephemeral=True)
            return

        progress_msg = await ctx.respond(f"Starting sync for {len(members_to_sync)} users...")
        
        created = 0
        failed = 0
        
        for member in members_to_sync:
            if await self.create_user_account(member):
                created += 1
            else:
                failed += 1

            if (created + failed) % 5 == 0:
                await ctx.edit(f"Progress: {created + failed}/{len(members_to_sync)} processed...")

        final_embed = discord.Embed(
            title="User Sync Complete",
            description=f"""
            ✅ Successfully created: {created} accounts
            ❌ Failed to create: {failed} accounts
            """,
            color=discord.Color.green() if failed == 0 else discord.Color.orange()
        )
        await ctx.respond(embed=final_embed)

def setup(bot):
    """Setup function with enable check"""
    if os.getenv('ENABLE_USER_MANAGER', 'TRUE').upper() == 'FALSE':
        logger.info("UserManager Cog is disabled via ENABLE_USER_MANAGER")
        return
    
    bot.add_cog(UserManager(bot))
    #logger.info("UserManager Cog enabled and loaded")
