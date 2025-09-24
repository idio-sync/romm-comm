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
        admin_id = os.getenv('ADMIN_ID')
        if not admin_id:
            return False
        return str(ctx.author.id) == admin_id
    return commands.check(predicate)

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
                # First, check if the table exists and has duplicates
                cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_links'")
                table_exists = await cursor.fetchone()
                
                if table_exists:
                    # Check for duplicates
                    cursor = await db.execute("""
                        SELECT discord_id, COUNT(*) as count 
                        FROM user_links 
                        GROUP BY discord_id 
                        HAVING count > 1
                    """)
                    duplicates = await cursor.fetchall()
                    
                    if duplicates:
                        logger.warning(f"Found {len(duplicates)} Discord IDs with duplicate entries. Cleaning up...")
                        
                        # For each duplicate, keep only the most recent entry
                        for discord_id, count in duplicates:
                            # Get all entries for this discord_id
                            cursor = await db.execute("""
                                SELECT discord_id, romm_username, romm_id, created_at 
                                FROM user_links 
                                WHERE discord_id = ? 
                                ORDER BY created_at DESC
                            """, (discord_id,))
                            entries = await cursor.fetchall()
                            
                            if entries:
                                # Keep the first (most recent) entry
                                keep_entry = entries[0]
                                logger.info(f"Keeping entry for Discord {discord_id}: {keep_entry[1]} (created: {keep_entry[3]})")
                                
                                # Delete all entries for this discord_id
                                await db.execute("DELETE FROM user_links WHERE discord_id = ?", (discord_id,))
                                
                                # Re-insert the most recent entry
                                await db.execute("""
                                    INSERT INTO user_links (discord_id, romm_username, romm_id, created_at)
                                    VALUES (?, ?, ?, ?)
                                """, keep_entry)
                        
                        await db.commit()
                        logger.info("Duplicate cleanup complete")
                    
                    # Now recreate the table with proper constraints
                    logger.info("Recreating table with proper constraints...")
                    
                    # Create a new table with the correct schema
                    await db.execute("""
                        CREATE TABLE IF NOT EXISTS user_links_new (
                            discord_id INTEGER PRIMARY KEY,
                            romm_username TEXT NOT NULL,
                            romm_id INTEGER NOT NULL,
                            discord_username TEXT,
                            discord_avatar TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                    
                    # Copy data from old table to new table (without duplicates)
                    await db.execute("""
                        INSERT OR IGNORE INTO user_links_new (discord_id, romm_username, romm_id, discord_username, discord_avatar, created_at)
                        SELECT discord_id, romm_username, romm_id, 
                               COALESCE(discord_username, ''), 
                               COALESCE(discord_avatar, ''),
                               created_at
                        FROM user_links
                    """)
                    
                    # Drop old table and rename new one
                    await db.execute("DROP TABLE user_links")
                    await db.execute("ALTER TABLE user_links_new RENAME TO user_links")
                    
                else:
                    # Create fresh table with proper constraints
                    await db.execute("""
                        CREATE TABLE user_links (
                            discord_id INTEGER PRIMARY KEY,
                            romm_username TEXT NOT NULL,
                            romm_id INTEGER NOT NULL,
                            discord_username TEXT,
                            discord_avatar TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                        )
                    """)
                
                # Create an index on romm_username for faster lookups
                await db.execute("""
                    CREATE INDEX IF NOT EXISTS idx_romm_username 
                    ON user_links(romm_username COLLATE NOCASE)
                """)
                
                await db.commit()
                self._initialized = True
                logger.info("Database initialized successfully with proper constraints")
                
        except Exception as e:
            logger.error(f"Error initializing database: {e}", exc_info=True)
            raise
            
    async def _ensure_initialized(self):
        """Ensure database is initialized before operations"""
        if not self._initialized:
            await self.initialize()

    async def add_user_link(self, discord_id: int, romm_username: str, romm_id: int, 
                       discord_username: str = None, discord_avatar: str = None) -> bool:
        """Add or update a Discord to RomM user link"""
        try:
            await self._ensure_initialized()
            
            async with aiosqlite.connect(self.db_path) as db:
                # Check if entry exists
                cursor = await db.execute(
                    "SELECT discord_id FROM user_links WHERE discord_id = ?",
                    (discord_id,)
                )
                existing = await cursor.fetchone()
                
                if existing:
                    # Update existing entry
                    logger.info(f"Updating existing link for Discord {discord_id} to RomM user {romm_username}")
                    await db.execute("""
                        UPDATE user_links 
                        SET romm_username = ?, 
                            romm_id = ?, 
                            discord_username = ?, 
                            discord_avatar = ?,  # This should be a URL now
                            updated_at = CURRENT_TIMESTAMP
                        WHERE discord_id = ?
                    """, (romm_username, romm_id, discord_username, discord_avatar, discord_id))
                else:
                    # Insert new entry
                    logger.info(f"Creating new link for Discord {discord_id} to RomM user {romm_username}")
                    await db.execute("""
                        INSERT INTO user_links 
                        (discord_id, romm_username, romm_id, discord_username, discord_avatar, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (discord_id, romm_username, romm_id, discord_username, discord_avatar))
                
                await db.commit()
                return True
                
        except Exception as e:
            logger.error(f"Error adding/updating user link: {e}", exc_info=True)
            return False
    
    async def get_user_link(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """Get RomM user info for a Discord user"""
        try:
            await self._ensure_initialized()
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT romm_username, romm_id, created_at, updated_at FROM user_links WHERE discord_id = ?",
                    (discord_id,)
                )
                result = await cursor.fetchone()
                if result:
                    return {
                        "romm_username": result[0],
                        "romm_id": result[1],
                        "created_at": result[2],
                        "updated_at": result[3] if len(result) > 3 else result[2]
                    }
                return None
        except Exception as e:
            logger.error(f"Error getting user link: {e}", exc_info=True)
            return None
    
    async def get_user_link_by_romm_username(self, romm_username: str) -> Optional[Dict[str, Any]]:
        """Get Discord user info for a RomM username"""
        try:
            await self._ensure_initialized()
            async with aiosqlite.connect(self.db_path) as db:
                # Use COLLATE NOCASE for case-insensitive comparison
                cursor = await db.execute(
                    "SELECT discord_id, romm_id, created_at FROM user_links WHERE romm_username = ? COLLATE NOCASE",
                    (romm_username,)
                )
                result = await cursor.fetchone()
                if result:
                    return {
                        "discord_id": result[0],
                        "romm_id": result[1],
                        "created_at": result[2]
                    }
                return None
        except Exception as e:
            logger.error(f"Error getting user link by RomM username: {e}", exc_info=True)
            return None

    async def delete_user_link(self, discord_id: int) -> bool:
        """Delete a Discord to RomM user link"""
        try:
            await self._ensure_initialized()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM user_links WHERE discord_id = ?", (discord_id,))
                await db.commit()
                logger.info(f"Deleted user link for Discord {discord_id}")
                return True
        except Exception as e:
            logger.error(f"Error deleting user link: {e}", exc_info=True)
            return False
    
    async def get_all_links(self) -> List[Dict[str, Any]]:
        """Get all user links for debugging"""
        try:
            await self._ensure_initialized()
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("""
                    SELECT discord_id, romm_username, romm_id, discord_username, created_at, updated_at
                    FROM user_links
                    ORDER BY created_at DESC
                """)
                rows = await cursor.fetchall()
                return [
                    {
                        "discord_id": row[0],
                        "romm_username": row[1],
                        "romm_id": row[2],
                        "discord_username": row[3],
                        "created_at": row[4],
                        "updated_at": row[5] if len(row) > 5 else row[4]
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Error getting all links: {e}", exc_info=True)
            return []

class UserManagementView(discord.ui.View):
    """Comprehensive user management interface for admins"""
    
    def __init__(self, bot, cog, guild: discord.Guild):
        super().__init__(timeout=600)  # 10 minute timeout
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
        
        # RomM User Select (Row 1)
        self.romm_select = discord.ui.Select(
            placeholder="Select a RomM user to link...",
            custom_id="romm_user_select",
            row=1,
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
        
        # Rest of the buttons remain the same...
        # Action Buttons (Rows 2-3)
        self.link_button = discord.ui.Button(
            label="Link Accounts",
            style=discord.ButtonStyle.success,
            row=2,
            disabled=True
        )
        self.link_button.callback = self.link_accounts_callback
        self.add_item(self.link_button)
        
        self.unlink_button = discord.ui.Button(
            label="Unlink Account",
            style=discord.ButtonStyle.danger,
            row=2,
            disabled=True
        )
        self.unlink_button.callback = self.unlink_account_callback
        self.add_item(self.unlink_button)
        
        self.create_button = discord.ui.Button(
            label="Create New RomM Account",
            style=discord.ButtonStyle.primary,
            row=2,
            disabled=True
        )
        self.create_button.callback = self.create_account_callback
        self.add_item(self.create_button)
        
        self.refresh_button = discord.ui.Button(
            label="🔄 Refresh",
            style=discord.ButtonStyle.secondary,
            row=3
        )
        self.refresh_button.callback = self.refresh_callback
        self.add_item(self.refresh_button)
        
        self.bulk_create_button = discord.ui.Button(
            label="Bulk Create for Role",
            style=discord.ButtonStyle.primary,
            row=3
        )
        self.bulk_create_button.callback = self.bulk_create_callback
        self.add_item(self.bulk_create_button)
        
    async def populate_discord_users(self):
        """Populate the Discord user dropdown"""
        self.discord_select.options.clear()
        
        # Get members with the auto-register role if configured
        if self.cog.auto_register_role_id:
            role = self.guild.get_role(self.cog.auto_register_role_id)
            members = role.members if role else self.guild.members
        else:
            members = self.guild.members
        
        # Sort members by display name
        sorted_members = sorted(members, key=lambda m: m.display_name.lower())[:25]
        
        # If no members found, add a placeholder
        if not sorted_members:
            self.discord_select.add_option(
                label="No users found",
                value="no_users",
                description="No Discord users available"
            )
            return
        
        # Check existing links and add options
        for member in sorted_members:
            link = await self.cog.db_manager.get_user_link(member.id)
            if link:
                # Store the actual username from database
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
    
    async def populate_romm_users(self):
        """Populate the RomM user dropdown"""
        self.romm_select.options.clear()
        
        # Fetch RomM users
        users_data = await self.bot.fetch_api_endpoint('users')
        if not users_data:
            self.romm_select.add_option(
                label="Failed to fetch RomM users",
                value="error"
            )
            return
        
        self.romm_users = users_data
        
        # Get ALL existing links from database to properly mark linked accounts
        linked_usernames = set()
        linked_romm_ids = set()
        discord_to_romm = {}  # Map of discord_id -> romm_username
        
        try:
            async with aiosqlite.connect(self.cog.db_manager.db_path) as db:
                # Get both username and romm_id for better matching
                cursor = await db.execute("SELECT discord_id, romm_username, romm_id FROM user_links")
                rows = await cursor.fetchall()
                for row in rows:
                    discord_id, romm_username, romm_id = row
                    # Store both username and ID for matching (case-insensitive)
                    linked_usernames.add(romm_username.lower() if romm_username else "")
                    linked_romm_ids.add(romm_id)
                    discord_to_romm[discord_id] = romm_username
        except Exception as e:
            logger.error(f"Error fetching linked users: {e}")
        
        # Sort users by username
        sorted_users = sorted(users_data, key=lambda u: u.get('username', '').lower())[:25]
        
        # If no users, add placeholder
        if not sorted_users:
            self.romm_select.add_option(
                label="No RomM users available",
                value="no_users",
                description="No users found in RomM"
            )
            return
        
        for user in sorted_users:
            username = user.get('username', 'Unknown')
            user_id = user.get('id')
            role = user.get('role', 'VIEWER')
            
            # Check if linked by either username (case-insensitive) or ID
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
                    except:
                        pass
            
            emoji = "🔗" if is_linked else "🆓"
            status = f" (Linked to: {linked_to})" if linked_to else " (Already linked)" if is_linked else ""
            
            self.romm_select.add_option(
                label=f"{emoji} {username[:40]}{status[:30]}",
                value=username,
                description=f"Role: {role}"[:100]
            )

    
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
        
        # Enable/disable buttons based on link status
        existing_link = self.discord_user_links.get(user_id)
        
        if existing_link:
            # User is linked
            self.unlink_button.disabled = False
            self.link_button.disabled = True
            self.create_button.disabled = True
            self.romm_select.disabled = True
        else:
            # User is not linked
            self.unlink_button.disabled = True
            self.link_button.disabled = False  # Will be enabled when RomM user selected
            self.create_button.disabled = False
            self.romm_select.disabled = False
            
            # Populate RomM users if not already done
            if not self.romm_select.options or (len(self.romm_select.options) == 1 and self.romm_select.options[0].value == "placeholder"):
                await self.populate_romm_users()
        
        # Update the message with new state
        await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
    
    async def romm_select_callback(self, interaction: discord.Interaction):
        """Handle RomM user selection"""
        await interaction.response.edit_message(embed=self.create_status_embed(), view=self)
        
        if not self.romm_select.values:
            self.selected_romm_user = None
            self.link_button.disabled = True
        else:
            username = self.romm_select.values[0]
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
        """Unlink selected Discord user from RomM"""
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
        
        # Check if it's an admin account
        user = await self.cog.find_user_by_username(link['romm_username'])
        if user and await self.cog.is_romm_admin(user):
            await interaction.followup.send(
                "⚠️ Cannot unlink admin accounts for safety. Remove admin role in RomM first.",
                ephemeral=True
            )
            return
        
        # Confirm deletion
        confirm_view = ConfirmView()
        await interaction.followup.send(
            f"Are you sure you want to unlink {discord_user.mention} from RomM user `{link['romm_username']}`?\n"
            "This will also delete the RomM account.",
            view=confirm_view,
            ephemeral=True
        )
        
        await confirm_view.wait()
        if not confirm_view.value:
            return
        
        # Delete RomM account and unlink
        if user and await self.cog.delete_user(user['id']):
            await self.cog.db_manager.delete_user_link(discord_user.id)
            
            # Refresh the view
            await self.populate_discord_users()
            
            # Clear selection after operations
            self.selected_discord_user = None
            self.update_button_states()
            
            await interaction.followup.send(
                f"✅ Successfully unlinked and deleted RomM account for {discord_user.mention}",
                ephemeral=True
            )
            await interaction.edit_original_response(embed=self.create_status_embed(), view=self)
        else:
            await interaction.followup.send("❌ Failed to delete RomM account", ephemeral=True)
    
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
        """Bulk create accounts for users with the auto-register role"""
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
        
        # Find users needing accounts
        members_to_create = []
        for member in role.members:
            link = await self.cog.db_manager.get_user_link(member.id)
            if not link:
                members_to_create.append(member)
        
        if not members_to_create:
            await interaction.followup.send("✅ All role members already have accounts", ephemeral=True)
            return
        
        # Create progress message
        progress_msg = await interaction.followup.send(
            f"Creating accounts for {len(members_to_create)} users...",
            ephemeral=True
        )
        
        created = 0
        failed = 0
        
        for member in members_to_create:
            if await self.cog.create_user_account(member, interactive=False):
                created += 1
            else:
                failed += 1
            
            # Update progress every 5 users
            if (created + failed) % 5 == 0:
                try:
                    await interaction.edit_original_response(
                        content=f"Progress: {created + failed}/{len(members_to_create)} processed..."
                    )
                except:
                    pass  # Ignore if message edit fails
        
        # Final summary
        try:
            await interaction.edit_original_response(
                content=f"✅ Created: {created} accounts\n❌ Failed: {failed} accounts"
            )
        except:
            await interaction.followup.send(
                f"✅ Created: {created} accounts\n❌ Failed: {failed} accounts",
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
            self.create_button.disabled = True
            self.romm_select.disabled = True
        else:
            existing_link = self.discord_user_links.get(self.selected_discord_user.id)
            if existing_link:
                self.unlink_button.disabled = False
                self.link_button.disabled = True
                self.create_button.disabled = True
                self.romm_select.disabled = True
            else:
                self.unlink_button.disabled = True
                self.create_button.disabled = False
                self.romm_select.disabled = False
                self.link_button.disabled = not self.selected_romm_user
        
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
                "   • **Link**: Connect to existing RomM account\n"
                "   • **Create**: Make new RomM account\n"
                "   • **Unlink**: Remove connection and delete account\n"
                "   • **Bulk Create**: Create accounts for all role members"
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
            async with aiosqlite.connect(self.db_manager.db_path) as db:
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
                
                await db.commit()
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
            self.temp_storage.pop(member.id, None)
            return result
                
        except Exception as e:
            logger.error(f"Error in link_existing_account for {member.display_name}: {e}", exc_info=True)
            return None    

    async def create_user_account(self, member: discord.Member, interactive: bool = True) -> bool:
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

    @discord.slash_command(name="user_manager", description="User management interface")
    @is_admin()
    async def user_manager(self, ctx: discord.ApplicationContext):
        """Open the comprehensive user management interface"""
        await ctx.defer(ephemeral=True)
        
        # Ensure we have valid authentication
        if not await self.bot.ensure_valid_token():
            await ctx.followup.send("❌ Failed to authenticate with API!", ephemeral=True)
            return
        
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

