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
        self.processed_servers_file = os.path.join('resources', 'emoji', 'processed_servers.json')
        self.processed_servers = self.load_processed_servers()
        self.bot.emoji_dict = {}  # Dictionary for all emojis (once uploaded)
        bot.loop.create_task(self.initialize_emoji_dict())

    async def initialize_emoji_dict(self):
        """Initialize emoji dictionary as soon as the bot is ready"""
        await self.bot.wait_until_ready()
        if self.bot.guilds:
            guild = self.bot.guilds[0]
            self.bot.emoji_dict = {emoji.name: emoji for emoji in guild.emojis}
            print(f"Initialized emoji dictionary with {len(self.bot.emoji_dict)} emojis")
    
    def load_processed_servers(self) -> Dict[int, List[str]]:
        """Load the list of servers that have already had emojis uploaded."""
        if os.path.exists(self.processed_servers_file):
            with open(self.processed_servers_file, 'r') as f:
                return json.load(f)
        return {}

    def save_processed_servers(self):
        """Save the list of processed servers to avoid duplicate uploads."""
        os.makedirs(os.path.dirname(self.processed_servers_file), exist_ok=True)
        with open(self.processed_servers_file, 'w') as f:
            json.dump(self.processed_servers, f)

    async def load_emoji_list(self) -> List[Tuple[str, str]]:
        """Load emoji data from the text file."""
        try:
            # Get the raw content from GitHub using aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(self.emoji_url_list) as response:
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
                        emoji_list.append((name.strip(), url.strip()))
                    except ValueError:
                        print(f"Warning: Invalid line format: {line}")
                        continue
            return emoji_list
        
        except Exception as e:
            print(f"Warning: Failed to load emoji list: {str(e)}")
            return []

    async def upload_emoji(self, guild: discord.Guild, name: str, url: str) -> bool:
        """Upload a single emoji to the server."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        print(f"Failed to download emoji {name}: {response.status}")
                        return False
                    
                    image_data = await response.read()

            emoji = await guild.create_custom_emoji(
                name=name,
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
        
        if guild_id_str in self.processed_servers:
            print(f"Already uploaded emojis to {guild.name}")
            return

        if not guild.me.guild_permissions.manage_emojis:
            print(f"Missing emoji permissions in {guild.name}")
            return

        emoji_list = await self.load_emoji_list()
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

        self.processed_servers[guild_id_str] = uploaded_emojis
        self.save_processed_servers()
    
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize emoji dictionary when bot starts"""
        # Wait a short moment to ensure guild data is available
        await asyncio.sleep(1)
        
        if self.bot.guilds:
            guild = self.bot.guilds[0]
            self.bot.emoji_dict = {emoji.name: emoji for emoji in guild.emojis}
            print("\nEmoji Dictionary Contents:")
            print(f"Total emojis loaded: {len(self.bot.emoji_dict)}")
            for name, emoji in self.bot.emoji_dict.items():
                print(f"Emoji: {name} -> {emoji.id}")
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
    @commands.is_owner()  # This ensures only the bot owner can use it
    async def emoji_upload(self, ctx):
        """Force upload emojis to the current server, even if they've been uploaded before. WILL CREATE DUPLICATES"""
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
