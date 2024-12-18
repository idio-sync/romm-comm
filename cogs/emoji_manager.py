import discord
from discord.ext import commands
import aiohttp
import asyncio
import os
import json
from typing import Dict, List, Tuple
import requests
import logging

logger = logging.getLogger('romm_bot')

class EmojiManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.emoji_url_list = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/emoji/emoji_urls.txt"
        self.emoji_url_list_extended = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/emoji/emoji_urls_extended.txt"
        
        # Create data directory if it doesn't exist
        self.data_dir = 'data'
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.processed_servers_file = os.path.join(self.data_dir, 'emoji_processed_servers.json')
        self.processed_servers = self.load_processed_servers()
        #print(f"Loaded processed servers: {self.processed_servers}")  # Debug print
        
        self.bot.emoji_dict = {}  # Dictionary for all emojis (once uploaded)
        self.init_task = bot.loop.create_task(self.initialize_emoji_dict())
        self.boot_check_task = bot.loop.create_task(self.check_emojis_on_boot())

    async def initialize_emoji_dict(self):
        """Initialize emoji dictionary as soon as the bot is ready"""
        await self.bot.wait_until_ready()
        try:
            if self.bot.guilds:
                guild = self.bot.guilds[0]
                self.bot.emoji_dict = {emoji.name: emoji for emoji in guild.emojis}
                #print(f"Initialized emoji dictionary with {len(self.bot.emoji_dict)} emojis")
                #print("\nEmoji Dictionary Contents:")
                #for name, emoji in self.bot.emoji_dict.items():
                    #print(f"Emoji: {name} -> {emoji.id}")
            else:
                print("No guilds found during emoji dictionary initialization!")
        except Exception as e:
            print(f"Error initializing emoji dictionary: {e}")    
 
    async def handle_nitro_change(self, guild: discord.Guild, had_nitro: bool, has_nitro: bool):
        """Handle emoji changes when a server's Nitro status changes."""
        logger.info(f"Nitro status change for {guild.name}: {had_nitro} -> {has_nitro}")
        guild_id_str = str(guild.id)
        
        try:
            # Try to find a suitable channel for notification
            notification_channel = await self.find_notification_channel(guild)
            
            if had_nitro and not has_nitro:
                logger.info(f"Server lost Nitro status. Adjusting emojis for {guild.name}")
                
                if notification_channel:
                    notify_msg = await notification_channel.send(
                        embed=discord.Embed(
                            title="⚠️ Server Nitro Status Change Detected",
                            description=(
                                "This server's Nitro status has changed. Adjusting emojis to fit within Discord's limit of 50 emojis for non-Nitro servers."
                            ),
                            color=discord.Color.yellow()
                        )
                    )
                
                # Load the standard (non-Nitro) emoji list
                standard_emoji_list = await self.load_emoji_list(guild)
                standard_emoji_names = {name for name, _ in standard_emoji_list}
                
                # Get current emojis
                current_emojis = guild.emojis
                to_remove = []
                to_keep = []
                
                # Prioritize keeping emojis that are in our standard list
                for emoji in current_emojis:
                    if emoji.name in standard_emoji_names:
                        to_keep.append(emoji)
                    else:
                        to_remove.append(emoji)
                
                # Remove excess emojis
                removed_emojis = []
                failed_removals = []
                
                for emoji in to_remove:
                    try:
                        await emoji.delete(reason="Server lost Nitro status - removing excess emojis")
                        removed_emojis.append(emoji.name)
                        await asyncio.sleep(1.2)
                    except Exception as e:
                        logger.error(f"Error removing emoji {emoji.name}: {e}")
                        failed_removals.append(emoji.name)
                
                logger.info(f"Removed {len(removed_emojis)} excess emojis from {guild.name}")
                
                # Update stored data
                self.processed_servers[guild_id_str] = {
                    'emojis': [emoji.name for emoji in guild.emojis],
                    'nitro_status': has_nitro,
                    'emoji_limit': guild.emoji_limit
                }
                self.save_processed_servers()
                
                # Send completion notification for removals if any happened
                if notification_channel and (removed_emojis or failed_removals):
                    embed = discord.Embed(
                        title="🔄 Emoji Adjustment Complete",
                        color=discord.Color.blue()
                    )
                    
                    embed.add_field(
                        name="Summary",
                        value=(
                            f"• Previous emoji count: {len(current_emojis)}\n"
                            f"• New emoji count: {len(guild.emojis)}\n"
                            f"• Emojis removed: {len(removed_emojis)}"
                        ),
                        inline=False
                    )
                    
                    if removed_emojis:
                        removed_list = ", ".join(removed_emojis[:20])
                        if len(removed_emojis) > 20:
                            removed_list += f" and {len(removed_emojis) - 20} more"
                        embed.add_field(
                            name="Removed Emojis",
                            value=removed_list,
                            inline=False
                        )
                    
                    if failed_removals:
                        failed_list = ", ".join(failed_removals)
                        embed.add_field(
                            name="⚠️ Failed to Remove",
                            value=failed_list,
                            inline=False
                        )
                    
                    try:
                        if 'notify_msg' in locals():
                            await notify_msg.edit(embed=embed)
                        else:
                            await notification_channel.send(embed=embed)
                    except Exception as e:
                        logger.error(f"Failed to send completion notification: {e}")
            
            # Always try to upload standard emojis after a status change
            logger.info(f"Uploading standard emojis for {guild.name}")
            await self.process_guild_emojis(guild, skip_nitro_check=True)
            
        except Exception as e:
            logger.error(f"Error in handle_nitro_change: {e}")
    
    async def check_emojis_on_boot(self):
        """Check and upload emojis when bot starts."""
        await self.bot.wait_until_ready()
        logger.info("Checking emojis on boot...")
        
        try:
            for guild in self.bot.guilds:
                guild_id_str = str(guild.id)
                needs_upload = False
                
                # Always check if emojis are actually present
                if len(guild.emojis) == 0:
                    logger.info(f"No emojis found in {guild.name}, forcing upload")
                    needs_upload = True
                else:
                    # Check if this guild has been processed
                    if guild_id_str not in self.processed_servers:
                        logger.info(f"Guild {guild.name} not in processed servers")
                        needs_upload = True
                    else:
                        # Check if we need to update due to Nitro status change
                        if isinstance(self.processed_servers[guild_id_str], dict):
                            stored_nitro_status = self.processed_servers[guild_id_str].get('nitro_status', False)
                            current_nitro_status = self.is_nitro_server(guild)
                            stored_emoji_limit = self.processed_servers[guild_id_str].get('emoji_limit', 50)
                            current_emoji_limit = guild.emoji_limit
                            
                            if current_nitro_status != stored_nitro_status or current_emoji_limit != stored_emoji_limit:
                                logger.info(f"Nitro status or limit changed for {guild.name}")
                                needs_upload = True
                        else:
                            # Old format, needs update
                            needs_upload = True

                    # Check if any expected emojis are missing
                    expected_emojis = await self.load_emoji_list(guild)
                    existing_emoji_names = {e.name for e in guild.emojis}
                    missing_emojis = [name for name, _ in expected_emojis if name not in existing_emoji_names]
                    if missing_emojis:
                        logger.info(f"Found {len(missing_emojis)} missing emojis in {guild.name}")
                        needs_upload = True

                if needs_upload:
                    logger.info(f"Uploading emojis to {guild.name} on boot")
                    try:
                        # Force a clean slate for upload
                        if guild_id_str in self.processed_servers:
                            del self.processed_servers[guild_id_str]
                            self.save_processed_servers()
                        
                        # Process guild emojis with skip_nitro_check to avoid loops
                        await self.process_guild_emojis(guild, skip_nitro_check=True)
                        
                        # Update emoji dictionary after upload
                        for emoji in guild.emojis:
                            self.bot.emoji_dict[emoji.name] = emoji
                            
                    except Exception as e:
                        logger.error(f"Error uploading emojis to {guild.name} on boot: {e}")
                else:
                    logger.info(f"Emojis already present in {guild.name}")
                    # Update emoji dictionary with existing emojis
                    for emoji in guild.emojis:
                        self.bot.emoji_dict[emoji.name] = emoji

        except Exception as e:
            logger.error(f"Error checking emojis on boot: {e}", exc_info=True)
    
    async def process_guild_emojis(self, guild: discord.Guild, skip_nitro_check: bool = False):
        """Process emoji uploads for a guild."""
        if not guild.me.guild_permissions.manage_emojis:
            logger.warning(f"Missing emoji permissions in {guild.name}")
            return False

        try:
            guild_id_str = str(guild.id)
            current_nitro_status = self.is_nitro_server(guild)
            current_emoji_limit = guild.emoji_limit
            
            # Check for Nitro status change only if not skipping
            if not skip_nitro_check and guild_id_str in self.processed_servers:
                stored_data = self.processed_servers[guild_id_str]
                if isinstance(stored_data, dict):
                    stored_nitro_status = stored_data.get('nitro_status', False)
                    stored_emoji_limit = stored_data.get('emoji_limit', 50)
                    
                    if stored_nitro_status != current_nitro_status or stored_emoji_limit != current_emoji_limit:
                        await self.handle_nitro_change(
                            guild,
                            had_nitro=stored_nitro_status,
                            has_nitro=current_nitro_status
                        )
                        return True

            # Load emoji list for current server status
            emoji_list = await self.load_emoji_list(guild)
            if not emoji_list:
                return False

            # Get current emojis and their names
            current_emoji_names = {e.name: e for e in guild.emojis}
            missing_emojis = []
            
            # Check which emojis are missing
            for name, url in emoji_list:
                if name not in current_emoji_names:
                    missing_emojis.append((name, url))

            if missing_emojis:
                slots_available = guild.emoji_limit - len(current_emoji_names)
                if slots_available > 0:
                    notification_channel = await self.find_notification_channel(guild)
                    notify_msg = None

                    if notification_channel and len(missing_emojis) > 5:
                        notify_msg = await notification_channel.send(
                            embed=discord.Embed(
                                title="🔄 Uploading Missing Emojis",
                                description=f"Found {len(missing_emojis)} missing emojis. Starting upload process...",
                                color=discord.Color.blue()
                            )
                        )

                    uploaded_emojis = []
                    failed_uploads = []

                    for name, url in missing_emojis[:slots_available]:
                        if len(guild.emojis) >= guild.emoji_limit:
                            break

                        if uploaded_emojis:
                            await asyncio.sleep(1.5)

                        try:
                            if await self.upload_emoji(guild, name, url):
                                uploaded_emojis.append(name)
                                # Update emoji dictionary with new emoji
                                for emoji in guild.emojis:
                                    if emoji.name == name:
                                        self.bot.emoji_dict[name] = emoji
                                        break
                            else:
                                failed_uploads.append(name)
                        except Exception as e:
                            logger.error(f"Error uploading emoji {name}: {e}")
                            failed_uploads.append(name)

                    if notification_channel and len(missing_emojis) > 5:
                        embed = discord.Embed(
                            title="✅ Emoji Upload Complete",
                            description=f"Successfully uploaded {len(uploaded_emojis)} new emojis.",
                            color=discord.Color.green()
                        )
                        
                        if uploaded_emojis:
                            uploaded_list = ", ".join(uploaded_emojis[:20])
                            if len(uploaded_emojis) > 20:
                                uploaded_list += f" and {len(uploaded_emojis) - 20} more"
                            embed.add_field(
                                name="Uploaded Emojis",
                                value=uploaded_list,
                                inline=False
                            )
                        
                        if failed_uploads:
                            failed_list = ", ".join(failed_uploads)
                            embed.add_field(
                                name="⚠️ Failed Uploads",
                                value=failed_list,
                                inline=False
                            )

                        try:
                            if notify_msg:
                                await notify_msg.edit(embed=embed)
                            else:
                                await notification_channel.send(embed=embed)
                        except Exception as e:
                            logger.error(f"Failed to send completion notification: {e}")

            # Update processed servers record
            self.processed_servers[guild_id_str] = {
                'emojis': list(current_emoji_names.keys()) + (uploaded_emojis if 'uploaded_emojis' in locals() else []),
                'nitro_status': current_nitro_status,
                'emoji_limit': current_emoji_limit
            }
            self.save_processed_servers()
            
            return True

        except Exception as e:
            logger.error(f"Error processing guild emojis: {e}", exc_info=True)
            return False
    
    def load_processed_servers(self) -> Dict[int, List[str]]:
        """Load the list of servers that have already had emojis uploaded."""
        if os.path.exists(self.processed_servers_file):
            with open(self.processed_servers_file, 'r') as f:
                return json.load(f)
        return {}

    async def find_notification_channel(self, guild: discord.Guild):
        """Find a suitable channel for notifications."""
        for channel in guild.text_channels:
            if any(name in channel.name.lower() for name in ['bot', 'command', 'bot-command', 'bot-spam']):
                if channel.permissions_for(guild.me).send_messages:
                    return channel
        
        if guild.system_channel and guild.system_channel.permissions_for(guild.me).send_messages:
            return guild.system_channel
        
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                return channel
    
        return None
    
    def save_processed_servers(self):
        """Save the list of processed servers to avoid duplicate uploads."""
        os.makedirs(os.path.dirname(self.processed_servers_file), exist_ok=True)
        with open(self.processed_servers_file, 'w') as f:
            json.dump(self.processed_servers, f)

    async def load_emoji_list(self, guild: discord.Guild = None) -> List[Tuple[str, str]]:
        """Load emoji data from appropriate text file based on server's Nitro status."""
        try:
            # Determine which URL to use based on server's emoji limit
            emoji_url = self.emoji_url_list_extended if self.is_nitro_server(guild) else self.emoji_url_list
            print(f"Using {'extended' if self.is_nitro_server(guild) else 'standard'} emoji list for {guild.name}")
            print(f"Selected URL: {emoji_url}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(emoji_url) as response:
                    if response.status != 200:
                        print(f"Warning: Failed to fetch emoji list: {response.status}")
                        return []
                    content = await response.text()
        
            # Parse the content into emoji pairs
            emoji_list = []
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    try:
                        name, url = line.split('|')
                        # Clean the name and ensure consistent formatting
                        clean_name = name.strip().replace('-', '_').lower()
                        emoji_list.append((clean_name, url.strip()))
                    except ValueError:
                        print(f"Warning: Invalid line format: {line}")
                        continue
            
            print(f"Loaded {len(emoji_list)} emoji definitions")
            
            # If it's not a Nitro server, ensure we don't exceed the limit
            if not self.is_nitro_server(guild) and len(emoji_list) > 50:
                print(f"Trimming emoji list to 50 for non-Nitro server {guild.name}")
                emoji_list = emoji_list[:50]
            
            return emoji_list
            
        except Exception as e:
            print(f"Warning: Failed to load emoji list: {str(e)}")
            return []


    def is_nitro_server(self, guild: discord.Guild) -> bool:
        """Check if a server has Nitro boost level that allows more than 50 emojis."""
        if not guild:
            return False
        return guild.emoji_limit > 50
        
    async def upload_emoji(self, guild: discord.Guild, name: str, url: str) -> bool:
        """Upload a single emoji to the server."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        print(f"Failed to download emoji {name}: {response.status}")
                        return False
                    
                    image_data = await response.read()

            # Clean the emoji name by replacing hyphens with underscores for Discord compatibility
            clean_name = name.strip().replace('-', '_').lower()
            
            emoji = await guild.create_custom_emoji(
                name=clean_name,
                image=image_data,
                reason="Bulk emoji upload on server join"
            )
            print(f"Successfully added emoji {emoji.name} to {guild.name}")
            return True

        except Exception as e:
            print(f"Error uploading emoji {name}: {str(e)}")
            return False

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """When the bot joins a new server, upload the emojis if not already done."""
        logger.info(f"Joined new guild: {guild.name} (ID: {guild.id})")
        logger.info(f"Server has Nitro status: {self.is_nitro_server(guild)}")
        logger.info(f"Emoji limit: {guild.emoji_limit}")
        
        await self.process_guild_emojis(guild)
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize emoji dictionary when bot starts"""
        logger.info("EmojiManager on_ready event triggered")
        # We don't need to initialize the emoji dictionary here since it's done in initialize_emoji_dict
        # Just log that we're ready
        if self.bot.guilds:
            logger.info(f"Bot is ready in {len(self.bot.guilds)} guilds")
            for guild in self.bot.guilds:
                logger.info(f"Present in guild: {guild.name} (ID: {guild.id}) with {len(guild.emojis)} emojis")
        else:
            logger.warning("Bot is ready but no guilds found!")
    
    @discord.slash_command(
        name="emoji_upload",
        description="Force upload the bot's custom emojis to current server (owner only)"
    )
    @commands.has_permissions(manage_emojis=True)
    @commands.is_owner()
    async def emoji_upload(self, ctx):
        """Force upload emojis to the server."""
        await ctx.defer()
        
        try:
            guild_id_str = str(ctx.guild.id)
            
            if guild_id_str in self.processed_servers:
                del self.processed_servers[guild_id_str]
                self.save_processed_servers()
            
            success = await self.process_guild_emojis(ctx.guild)
            if success:
                await ctx.respond("✅ Emoji upload process completed!")
            else:
                await ctx.respond("❌ Failed to upload emojis")
            
        except Exception as e:
            logger.error(f"Error in force_emoji_upload: {e}")
            await ctx.respond("❌ An error occurred while uploading emojis")

def setup(bot):
    bot.add_cog(EmojiManager(bot))
