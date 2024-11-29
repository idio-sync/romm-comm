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
        
    def _ensure_db_directory(self):
        """Ensure the data directory exists"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    async def initialize(self):
        """Initialize the database with required tables"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Create table for Discord to RomM user mapping
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS user_links (
                        discord_id INTEGER PRIMARY KEY,
                        romm_username TEXT NOT NULL,
                        romm_id INTEGER NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                await db.commit()
                logger.info("Database initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing database: {e}", exc_info=True)
            raise

    async def add_user_link(self, discord_id: int, romm_username: str, romm_id: int) -> bool:
        """Add a new Discord to RomM user link"""
        try:
            await self.initialize()  # Ensure table exists
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
            await self.initialize()  # Ensure table exists
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM user_links WHERE discord_id = ?", (discord_id,))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error deleting user link: {e}", exc_info=True)
            return False

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
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = 0
        self.refresh_token_task.start()
        self.db_manager = AsyncUserDatabaseManager()
        
        logger.info(
            f"Users Cog initialized with auto_register_role_id: {self.auto_register_role_id}, "
            f"using main channel_id: {self.log_channel_id}"
        )

    async def cog_load(self):
        """Initialize when cog is loaded"""
        await self.db_manager.initialize()
        logger.info("User Manager database initialized!")

    async def get_oauth_token(self) -> bool:
        """Get initial OAuth token with proper CSRF handling"""
        try:
            session = await self.bot.ensure_session()
            
            # First get CSRF token from heartbeat endpoint
            heartbeat_url = f"{self.bot.config.API_BASE_URL}/api/heartbeat"
            
            async with session.get(heartbeat_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get heartbeat. Status: {response.status}")
                    response_text = await response.text()
                    logger.error(f"Heartbeat response: {response_text}")
                    return False
                
                # Get CSRF token from Set-Cookie header
                set_cookie = response.headers.get('Set-Cookie')
                if not set_cookie:
                    logger.error("No Set-Cookie header in response")
                    return False
                
                # Parse the Set-Cookie header to get the CSRF token
                csrf_token = set_cookie.split('romm_csrftoken=')[1].split(';')[0]
              # logger.info(f"Extracted CSRF token: {csrf_token[:10]}...")

                # Now get OAuth token
                token_url = f"{self.bot.config.API_BASE_URL}/api/token"
                
                # Create form data
                data = aiohttp.FormData()
                data.add_field('grant_type', 'password')
                data.add_field('username', self.bot.config.USER)
                data.add_field('password', self.bot.config.PASS)
                data.add_field('scope', 'users.write users.read')
                
                # Include CSRF token in both header and cookie
                headers = {
                    "Accept": "application/json",
                    "X-CSRFToken": csrf_token,
                    "Cookie": f"romm_csrftoken={csrf_token}"
                }
                
                async with session.post(token_url, data=data, headers=headers) as response:
                    response_text = await response.text()
                  # logger.info(f"Token response status: {response.status}")
                  # logger.info(f"Token response body: {response_text}")
                    
                    if response.status == 200:
                        token_data = await response.json()
                        self.access_token = token_data.get('access_token')
                        self.refresh_token = token_data.get('refresh_token')
                        self.token_expiry = time.time() + token_data.get('expires', 840)
                      # logger.info("Successfully obtained OAuth token")
                        return True
                    else:
                        logger.error(f"Failed to get OAuth token. Status: {response.status}, Response: {response_text}")
                        return False
                    
        except Exception as e:
            logger.error(f"Error getting OAuth token: {e}", exc_info=True)
            return False

    async def refresh_oauth_token(self) -> bool:
        """Refresh OAuth token with CSRF handling"""
        try:
            if not self.refresh_token:
                return await self.get_oauth_token()

            session = await self.bot.ensure_session()
            
            # Get fresh CSRF token from heartbeat
            heartbeat_url = f"{self.bot.config.API_BASE_URL}/api/heartbeat"
            async with session.get(heartbeat_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get heartbeat. Status: {response.status}")
                    return False
                    
                set_cookie = response.headers.get('Set-Cookie')
                if not set_cookie:
                    logger.error("No Set-Cookie header in response")
                    return False
                
                csrf_token = set_cookie.split('romm_csrftoken=')[1].split(';')[0]
                logger.info(f"Got CSRF token for refresh: {csrf_token[:10]}...")

            # Now refresh the token
            token_url = f"{self.bot.config.API_BASE_URL}/api/token"
            
            data = aiohttp.FormData()
            data.add_field('grant_type', 'refresh_token')
            data.add_field('refresh_token', self.refresh_token)
            
            headers = {
                "Accept": "application/json",
                "X-CSRFToken": csrf_token,
                "Cookie": f"romm_csrftoken={csrf_token}"
            }
            
            logger.info("Attempting to refresh OAuth token")
            async with session.post(token_url, data=data, headers=headers) as response:
                response_text = await response.text()
                logger.info(f"Refresh response status: {response.status}")
                logger.info(f"Refresh response body: {response_text}")
                
                if response.status == 200:
                    token_data = await response.json()
                    self.access_token = token_data.get('access_token')
                    self.refresh_token = token_data.get('refresh_token')
                    self.token_expiry = time.time() + token_data.get('expires', 840)
                    logger.info("Successfully refreshed OAuth token")
                    return True
                else:
                    logger.error(f"Failed to refresh token, status: {response.status}")
                    return await self.get_oauth_token()
                    
        except Exception as e:
            logger.error(f"Error refreshing OAuth token: {e}")
            return False
            
    @tasks.loop(minutes=13)  # Refresh token every 13 minutes (before 15-minute expiry)
    async def refresh_token_task(self):
        """Periodic token refresh task"""
        if time.time() > self.token_expiry - 60:  # Refresh if within 1 minute of expiry
            await self.refresh_oauth_token()

    async def ensure_token(self) -> bool:
        """Ensure we have a valid OAuth token"""
        if not self.access_token or time.time() > self.token_expiry - 60:
            return await self.get_oauth_token()
        return True

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
            # Ensure we have a valid token
            if not await self.ensure_token():
                logger.error("Failed to obtain OAuth token")
                return False

            session = await self.bot.ensure_session()
            url = f"{self.bot.config.API_BASE_URL}/api/users/{user_id}"
            
            # Get fresh CSRF token
            heartbeat_url = f"{self.bot.config.API_BASE_URL}/api/heartbeat"
            async with session.get(heartbeat_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get heartbeat. Status: {response.status}")
                    return False
                
                set_cookie = response.headers.get('Set-Cookie')
                if not set_cookie:
                    logger.error("No Set-Cookie header in response")
                    return False
                
                csrf_token = set_cookie.split('romm_csrftoken=')[1].split(';')[0]
            
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
                "X-CSRFToken": csrf_token,
                "Cookie": f"romm_csrftoken={csrf_token}"
            }
            
            async with session.delete(url, headers=headers) as response:
                if response.status == 401:  # Unauthorized - token might be expired
                    if await self.get_oauth_token():  # Try refreshing token
                        return await self.delete_user(user_id)  # Retry once
                return response.status == 200
        except Exception as e:
            logger.error(f"Error deleting user {user_id}: {e}", exc_info=True)
            return False

    async def find_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Find a user by their username in the API"""
        try:
            # Ensure we have a valid token
            if not await self.ensure_token():
                logger.error("Failed to obtain OAuth token")
                return None

            url = f"{self.bot.config.API_BASE_URL}/api/users"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}"
            }

            session = await self.bot.ensure_session()
            async with session.get(url, headers=headers) as response:
                if response.status == 401:  # Unauthorized - token might be expired
                    if await self.get_oauth_token():  # Try refreshing token
                        return await self.find_user_by_username(username)  # Retry once
                        
                if response.status == 200:
                    users = await response.json()
                    logger.info(f"Searching for user with username: {username}")
                    if users:
                        # Log all usernames for debugging
                        logger.info(f"Available usernames: {[user.get('username', '') for user in users]}")
                        return next(
                            (user for user in users if user.get('username', '').lower() == username.lower()),
                            None
                        )
            return None
        except Exception as e:
            logger.error(f"Error finding user {username}: {e}", exc_info=True)
    
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
                        
                        # Get fresh CSRF token and make request
                        session = await self.bot.ensure_session()
                        heartbeat_url = f"{self.bot.config.API_BASE_URL}/api/heartbeat"
                        async with session.get(heartbeat_url) as response:
                            csrf_token = response.headers.get('Set-Cookie').split('romm_csrftoken=')[1].split(';')[0]
                        
                        update_url = f"{self.bot.config.API_BASE_URL}/api/users/{existing_user['id']}"
                        headers = {
                            "Accept": "application/json",
                            "Authorization": f"Bearer {self.access_token}",
                            "X-CSRFToken": csrf_token,
                            "Cookie": f"romm_csrftoken={csrf_token}"
                        }
                        
                        params = {
                            "username": new_username
                        }
                        
                        async with session.put(update_url, params=params, headers=headers) as response:
                            if response.status in (200, 201):
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
            heartbeat_url = f"{self.bot.config.API_BASE_URL}/api/heartbeat"
            
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
            
            url = f"{self.bot.config.API_BASE_URL}/api/users"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.access_token}",
                "X-CSRFToken": csrf_token,
                "Cookie": f"romm_csrftoken={csrf_token}"
            }

            params = {
                'username': username,
                'password': password,
                'role': 'VIEWER'  # Using correct role from backend enum
            }
            
            logger.info(f"Creating user {username} with URL: {url}")
            async with session.post(url, params=params, headers=headers) as response:
                response_text = await response.text()
                logger.info(f"API Response Status: {response.status}")
                logger.info(f"API Response Body: {response_text}")

                if response.status in (200, 201):
                    response_data = await response.json()
                    
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
                    logger.error(f"User creation failed: {response_text}")
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
