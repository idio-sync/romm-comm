import discord
from discord.ext import commands
import aiohttp
import asyncio
import os
import json
from typing import Dict, List, Tuple
import requests

class EmojiManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.emoji_url_list = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/emoji/emoji_urls.txt"
        self.extended_emoji_url = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/emoji/emoji_urls_extended.txt"
        
        # Create data directory if it doesn't exist
        self.data_dir = 'data'
        os.makedirs(self.data_dir, exist_ok=True)
        
        self.processed_servers_file = os.path.join(self.data_dir, 'emoji_processed_servers.json')
        self.processed_servers = self.load_processed_servers()
        #print(f"Loaded processed servers: {self.processed_servers}")  # Debug print
        
        self.bot.emoji_dict = {}  # Dictionary for all emojis (once uploaded)
        bot.loop.create_task(self.initialize_emoji_dict())

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
 
    def load_processed_servers(self) -> Dict[int, List[str]]:
        """Load the list of servers that have already had emojis uploaded."""
        try:
            if os.path.exists(self.processed_servers_file):
                with open(self.processed_servers_file, 'r') as f:
                    data = json.load(f)
                #print(f"Successfully loaded processed servers from {self.processed_servers_file}")  # Debug print
                return data
            else:
                print(f"No existing processed servers file found at {self.processed_servers_file}")  # Debug print
                return {}
        except Exception as e:
            print(f"Error loading processed servers file: {e}")  # Debug print
            return {}

    def save_processed_servers(self):
        """Save the list of processed servers to avoid duplicate uploads."""
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.processed_servers_file), exist_ok=True)
            
            with open(self.processed_servers_file, 'w') as f:
                json.dump(self.processed_servers, f)
            print(f"Successfully saved processed servers to {self.processed_servers_file}")  # Debug print
            #print(f"Saved data: {self.processed_servers}")  # Debug print
        except Exception as e:
            print(f"Error saving processed servers: {e}")  # Debug print

    async def load_emoji_list(self, guild: discord.Guild = None) -> List[Tuple[str, str]]:
        """Load emoji data from appropriate text file based on server's Nitro status."""
        try:
            # Determine which URL to use based on server's emoji limit
            emoji_url = self.extended_emoji_url if self.is_nitro_server(guild) else self.standard_emoji_url
            print(f"Using {'extended' if self.is_nitro_server(guild) else 'standard'} emoji list for {guild.name}")
            
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
            
        # Get the server's emoji limit based on boost level
        emoji_limit = guild.emoji_limit
        
        # Standard servers have a limit of 50
        return emoji_limit > 50
        
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
        guild_id_str = str(guild.id)
        
        print(f"Joined new guild: {guild.name} (ID: {guild_id_str})")
        print(f"Server has Nitro status: {self.is_nitro_server(guild)}")
        print(f"Emoji limit: {guild.emoji_limit}")
        
        if guild_id_str in self.processed_servers:
            # If server's Nitro status has changed, we might want to update emojis
            current_nitro_status = self.is_nitro_server(guild)
            stored_nitro_status = self.processed_servers[guild_id_str].get('nitro_status', False)
            
            if current_nitro_status != stored_nitro_status:
                print(f"Nitro status changed for {guild.name}. Updating emojis...")
                del self.processed_servers[guild_id_str]
                self.save_processed_servers()
            else:
                print(f"Already uploaded emojis to {guild.name}")
                return

        if not guild.me.guild_permissions.manage_emojis:
            print(f"Missing emoji permissions in {guild.name}")
            return

        emoji_list = await self.load_emoji_list(guild)
        if not emoji_list:
            return

        print(f"Starting emoji upload for {guild.name}")
        uploaded_emojis = []

        for name, url in emoji_list:
            if len(guild.emojis) >= guild.emoji_limit:
                print(f"Reached emoji limit for {guild.name}")
                break

            if uploaded_emojis:
                await asyncio.sleep(1.5)

            if await self.upload_emoji(guild, name, url):
                uploaded_emojis.append(name)

        # Store both uploaded emojis and Nitro status
        self.processed_servers[guild_id_str] = {
            'emojis': uploaded_emojis,
            'nitro_status': self.is_nitro_server(guild)
        }
        self.save_processed_servers()
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize emoji dictionary when bot starts"""
        # Wait a short moment to ensure guild data is available
        await asyncio.sleep(1)
        
        if self.bot.guilds:
            guild = self.bot.guilds[0]
            self.bot.emoji_dict = {emoji.name: emoji for emoji in guild.emojis}
            #print("\nEmoji Dictionary Contents:")
            #print(f"Total emojis loaded: {len(self.bot.emoji_dict)}")
            #for name, emoji in self.bot.emoji_dict.items():
                #print(f"Emoji: {name} -> {emoji.id}")
        else:
            print("No guilds found when loading emoji dictionary!")
    
    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild, before, after):
        # Update emoji dictionary when emojis change
        self.bot.emoji_dict = {emoji.name: emoji for emoji in guild.emojis}

    @discord.slash_command(
        name="emoji_upload",
        description="Force upload the bot's custom emojis to current server (owner only)"
    )
    @commands.has_permissions(manage_emojis=True)
    @commands.is_owner()
    async def emoji_upload(self, ctx):
        await ctx.defer()
        
        try:
            guild_id_str = str(ctx.guild.id)
            
            if guild_id_str in self.processed_servers:
                del self.processed_servers[guild_id_str]
                self.save_processed_servers()
            
            await self.on_guild_join(ctx.guild)
            await ctx.respond("✅ Emoji upload process completed!")
            
        except Exception as e:
            print(f"Error in force_emoji_upload: {e}")
            await ctx.respond("❌ An error occurred while uploading emojis")

def setup(bot):
    bot.add_cog(EmojiManager(bot))
