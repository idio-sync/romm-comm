import logging
from typing import Any, Dict

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

# Stat type to emoji mapping
STAT_EMOJIS = {
    "Platforms": "🎮", "Roms": "👾", "Saves": "📁", 
    "States": "⏸", "Screenshots": "📸", "Storage Size": "💾",
    "User Count": "👤"
}

class Info(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.stat_channels = {}
        self.last_stats = {}  # Store previous stats for comparison

    async def get_or_create_category(self, guild: discord.Guild, category_name: str) -> discord.CategoryChannel:
        """Get or create a category in the guild."""
        category = discord.utils.get(guild.categories, name=category_name)
        if not category:
            category = await guild.create_category(name=category_name)
        return category
        
    async def update_voice_channel(self, channel: discord.VoiceChannel, new_name: str):
        """Update voice channel with rate limiting."""
        if channel.name != new_name:
            await self.bot.rate_limiter.acquire()
            await channel.edit(name=new_name)

    def has_stats_changed(self, new_stats: Dict[str, Any]) -> bool:
        """Compare new stats with last known stats to detect changes."""
        if not self.last_stats:
            return True
        
        return any(
            key not in self.last_stats or self.last_stats[key] != value
            for key, value in new_stats.items()
        )

    async def update_stat_channels(self, guild: discord.Guild):
        """Update stat channels when stats change."""
        if not self.bot.config.UPDATE_VOICE_NAMES:
            return
            
        # Copy so we never mutate the shared cached dict (APICache.get returns it by reference)
        cached_stats = self.bot.cache.get('stats')
        if not cached_stats:
            return
        stats_data = dict(cached_stats)
        user_count_data = self.bot.cache.get('user_count')

        if user_count_data and 'user_count' in user_count_data:
            stats_data['User Count'] = user_count_data['user_count']
        
        # Check if stats have changed
        if not self.has_stats_changed(stats_data):
            return
            
        try:
            # Get or create the category
            category = await self.get_or_create_category(guild, "Rom Server Stats")
            
            # Get existing channels efficiently
            existing_channels = {
                channel.name: channel 
                for channel in category.voice_channels
            }
            
            # Track channels to keep
            channels_to_keep = set()
            
            # Update or create channels
            for stat, value in stats_data.items():
                emoji = STAT_EMOJIS.get(stat, "📊")
                new_name = (f"{emoji} {stat}: {value:,} TB" if stat == "Storage Size" 
                           else f"{emoji} {stat}: {value:,}")
                
                # Find existing channel by checking if name starts with the expected prefix
                stat_prefix = f"{emoji} {stat}:"
                existing_channel = discord.utils.find(
                    lambda c, prefix=stat_prefix: c.name.startswith(prefix),
                    category.voice_channels
                )
                
                if existing_channel:
                    if existing_channel.name != new_name:
                        await self.update_voice_channel(existing_channel, new_name)
                    self.stat_channels[stat] = existing_channel
                    channels_to_keep.add(existing_channel.id)
                else:
                    await self.bot.rate_limiter.acquire()
                    self.stat_channels[stat] = await category.create_voice_channel(
                        name=new_name,
                        user_limit=0
                    )
                    channels_to_keep.add(self.stat_channels[stat].id)
            
            # Clean up old channels
            for channel in category.voice_channels:
                if channel.id not in channels_to_keep:
                    await self.bot.rate_limiter.acquire()
                    await channel.delete()
                    
            # Update last known stats
            self.last_stats = stats_data.copy()
                    
        except Exception as e:
            logger.error(f"Error updating stat channels: {e}", exc_info=True)
    
    # Update Discord Bot Status
    async def update_presence(self, status: bool):
        """Update bot's presence with rate limiting."""
        try:
            await self.bot.rate_limiter.acquire()
            if status and 'stats' in self.bot.cache.cache:
                stats_data = self.bot.cache.cache['stats']
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name=f"{stats_data['Roms']:,} games 🎮"
                    ),
                    status=discord.Status.online
                )
            else:
                await self.bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name="0 games ⚠️Check Romm connection⚠️"
                    ),
                    status=discord.Status.do_not_disturb
                )
        except Exception as e:
            logger.error(f"Failed to update presence: {e}")
    
    # Listener
    @commands.Cog.listener()
    async def on_ready(self):
        """Initialize channels when bot starts up."""
        for guild in self.bot.guilds:
            try:
                await self.update_stat_channels(guild)
            except Exception as e:
                logger.error(f"Error updating stats for guild {guild.id}: {e}", exc_info=True)

    # Add a method to be called when API data updates
    async def on_stats_update(self):
        """Called when new stats are fetched from the API."""
        for guild in self.bot.guilds:
            await self.update_stat_channels(guild)
    
    ## Info Commands
    # Help
    @discord.slash_command(
        name="help", 
        description="Lists all available commands and their functions"
    )
    async def help(self, ctx):
        # Create an embed for better formatting
        embed = discord.Embed(
            title="RomM Bot",
            description="Support for the bot can be found on [GitHub](https://github.com/idio-sync/romm-comm). \n \n Listed below are all available bot commands:",
            color=discord.Color.blue()
        )
        
        # Iterate through all cogs
        for cog in self.bot.cogs.values():
            # Get all slash commands from the cog
            commands_list = []
            for command in cog.walk_commands():
                if isinstance(command, discord.SlashCommand):
                    commands_list.append(f"• **/`{command.name}`** - {command.description}")
            
            # If cog has commands, add them to embed
            if commands_list:
                embed.add_field(
                    name=cog.__class__.__name__.replace("Cog", ""),
                    value="\n".join(commands_list),
                    inline=False
                )

        await ctx.respond(embed=embed)

    # Platforms
    @discord.slash_command(
        name="platforms", 
        description="Display all platforms w/ROM counts"
    )
    async def platforms(self, ctx: discord.ApplicationContext):
        """Platforms display command with cache usage."""
        try:
            # Defer the response since it might take a moment to fetch
            await ctx.defer()
        
            # Fetch raw platforms data to access custom_name field
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if raw_platforms:
                # Create platform data with display names
                platforms_data = []
                for platform in raw_platforms:
                    if platform.get('name') and platform.get('rom_count') is not None:
                        platforms_data.append({
                            'display_name': self.bot.get_platform_display_name(platform),
                            'rom_count': platform.get('rom_count', 0)
                        })
                
                # Sort platforms by display name
                platforms_data.sort(key=lambda x: x['display_name'])
            
                if platforms_data:
                    # Create embed with platform information
                    embed = discord.Embed(
                        title="Available Platforms w/ROM counts",
                        description="",
                        color=discord.Color.blue()
                    )
                
                    # Split into multiple fields if needed (Discord has a 25 field limit)
                    field_content = ""
                    for platform in platforms_data:
                        platform_line = f"**{platform['display_name']}**: {platform['rom_count']:,} ROMs\n"
                    
                        # If adding this line would exceed Discord's limit, create a new field
                        if len(field_content) + len(platform_line) > 1024:
                            embed.add_field(
                                name="", 
                                value=field_content, 
                                inline=False
                            )  
                            field_content = platform_line
                        else:
                            field_content += platform_line
                
                    # Add any remaining content
                    if field_content:
                        embed.add_field(
                            name="", 
                            value=field_content, 
                            inline=False
                        )
                
                    # Add total at the bottom
                    total_roms = sum(platform['rom_count'] for platform in platforms_data)
                    embed.set_footer(text=f"Total ROMs across all platforms: {total_roms:,}")
                
                    await ctx.respond(embed=embed)
                else:
                    await ctx.respond("No platform data available!")
            else:
                await ctx.respond("Failed to fetch platform data. Please try again later.")
            
        except Exception as e:
            logger.error(f"Error in platforms command: {e}", exc_info=True)
            await ctx.respond("❌ An error occurred while fetching platform data")
    
def setup(bot):
    bot.add_cog(Info(bot))
