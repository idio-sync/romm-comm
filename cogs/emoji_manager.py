import discord
from discord.ext import commands
import aiohttp
import asyncio
import os
import json
from typing import Dict, List, Tuple

class EmojiManager(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.emoji_file_path = os.path.join('resources', 'emoji', 'emoji_urls.txt')
        self.processed_servers_file = os.path.join('resources', 'emoji', 'processed_servers.json')
        self.processed_servers = self.load_processed_servers()

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
        if not os.path.exists(self.emoji_file_path):
            print(f"Warning: Emoji file not found at {self.emoji_file_path}")
            return []

        emoji_list = []
        with open(self.emoji_file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):  # Skip empty lines and comments
                    try:
                        name, url = line.split('|')
                        emoji_list.append((name.strip(), url.strip()))
                    except ValueError:
                        print(f"Warning: Invalid line format: {line}")
                        continue
        return emoji_list

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

    @commands.slash_command(
        name="emoji_force_upload",
        description="Force upload all emojis to the current server"
    )
    @commands.has_permissions(manage_emojis=True)
    async def emoji_force_upload(self, ctx):
        """Force upload emojis to the current server, even if they've been uploaded before."""
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

def setup(bot):  # Changed to non-async setup
    bot.add_cog(EmojiManager(bot))  # Removed await
