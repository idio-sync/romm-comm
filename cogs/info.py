import discord
from discord.ext import commands
from datetime import datetime
from typing import Dict, Any
import logging

logger = logging.getLogger('romm_bot')

# Stat type to emoji mapping
STAT_EMOJIS = {
    "Platforms": "🎮", "Roms": "👾", "Saves": "📁", 
    "States": "⏸", "Screenshots": "📸", "Storage Size": "💾",
    "Active Users": "👥"
}

class Info(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.stat_channels = {}
        self.last_stats = {}  # Store previous stats for comparison
     
    ## Update voice Channel stats
    #  Main logic
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
        
        for key, value in new_stats.items():
            if key not in self.last_stats or self.last_stats[key] != value:
                return True
        return False

    async def update_stat_channels(self, guild: discord.Guild):
        """Update stat channels when stats change."""
        if not self.bot.config.UPDATE_VOICE_NAMES:
            return
            
        stats_data = self.bot.cache.get('stats')
        if not stats_data:
            return
            
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
                
                # Find existing channel
                existing_channel = discord.utils.get(
                    category.voice_channels,
                    name__startswith=f"{emoji} {stat}:"
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
                        name=f"{stats_data['Roms']:,} games 🕹"
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
            description="Support can be found on the project's [GitHub page](https://github.com/idio-sync/romm-comm). \n \n Listed below are all available bot commands:",
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
                 
        # Add footer with bot info
        embed.set_footer(text=f"Requested by {ctx.author.name}", 
                        icon_url=ctx.author.avatar.url if ctx.author.avatar else None)
        
        await ctx.respond(embed=embed)
        pass
    
    # Website
    @discord.slash_command(
        name="website",
        description="Get the RomM instance URL"
    )
    async def website(self, ctx):
        """Website information command."""
        await ctx.respond(
            embed=discord.Embed(
                title="Website Information",
                description=self.bot.config.DOMAIN,
                color=discord.Color.blue()
        ))
        pass
    
    # Stats
    @discord.slash_command(
        name="stats",
        description="Display current RomM server stats"
    )
    async def stats(self, ctx):
        """Stats display command with cache usage."""
        try:
            stats_data = self.bot.cache.get('stats')
            if stats_data:
                last_fetch_time = self.bot.cache.last_fetch.get('stats')
                if last_fetch_time:
                    time_str = datetime.fromtimestamp(last_fetch_time).strftime('%Y-%m-%d %H:%M:%S')
                else:
                    time_str = "Unknown"
                
                embed = discord.Embed(
                    title="Collection Stats",
                    description=f"Last updated: {time_str}",
                    color=discord.Color.blue()
                )
            
                for stat, value in stats_data.items():
                    emoji = STAT_EMOJIS.get(stat, "📊")
                    field_value = f"{value:,} TB" if stat == "Storage Size" else f"{value:,}"
                    embed.add_field(name=f"{emoji} {stat}", value=field_value, inline=False)
            
                await ctx.respond(embed=embed)
            else:
                await ctx.respond("No API data available yet. Try using /refresh first!")
        except Exception as e:
            if hasattr(self.bot, 'logger'):
                self.bot.logger.error(f"Error in stats command: {e}", exc_info=True)
            await ctx.respond("❌ An error occurred while fetching stats")
        pass

    # Refresh
    @discord.slash_command(
        name="refresh",
        description="Manually refresh API data"
    )
    async def refresh(self, ctx):
        """Manually refresh API data and update channels."""
        await ctx.defer()
        try:
            # Call the main bot's update function directly
            await self.bot.update_api_data()
        
            # Send a success message after update completes
            embed = discord.Embed(
                title="✅ Manual Data Refresh Sucessful",
                description=f"Data update initiated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                color=discord.Color.green()
            )
            await ctx.respond(embed=embed)

        except Exception as e:
            logger.error(f"Error in refresh command: {e}", exc_info=True)
            await ctx.respond(
                embed=discord.Embed(
                    title="❌ Error",
                    description="An error occurred while refreshing API data.",
                    color=discord.Color.red()
                )
            )
    
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
        
            # Fetch platforms data
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if raw_platforms:
                platforms_data = self.bot.sanitize_platform_data(raw_platforms)
            
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
                        platform_line = f"**{platform['name']}**: {platform['rom_count']:,} ROMs\n"
                    
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
    
    @discord.slash_command(
        name="switch_shop_connection_info",
        description="Display setup instructions for connecting Switch Tinfoil to RomM"
    )
    async def switch_shop_connection_info(self, ctx):
        """Display Switch shop connection setup instructions."""
        try:
            embed = discord.Embed(
                title="🎮 Switch Shop Connection Guide",
                description="Follow these steps to configure your Switch for connection to this server.\n"
                           "*Note: This guide assumes you have Tinfoil installed and know how to use its basic functions.*",
                color=discord.Color.blue()
            )

            # Add steps as separate fields
            embed.add_field(
                name="Step 1: Access File Browser",
                value="Open Tinfoil and navigate to File Browser",
                inline=False
            )

            embed.add_field(
                name="Step 2: Access Settings",
                value="Scroll over to the selection and press `-` to access the new menu",
                inline=False
            )

            # Connection settings in a formatted table
            connection_settings = (
                "**Protocol:** `https`\n"
                f"**Host:** `{self.bot.config.DOMAIN}`\n"
                "**Port:** `443`\n"
                "**Path:** `/api/tinfoil/feed`\n"
                "**Username:** `Your RomM username`\n"
                "**Password:** `Your RomM password`\n"
                "**Title:** `Your choice (free text)`\n"
                "**Enabled:** `Yes`"
            )
            embed.add_field(
                name="Step 3: Enter Connection Settings",
                value=connection_settings,
                inline=False
            )

            embed.add_field(
                name="Step 4: Save Configuration",
                value="Press `X` to save your settings",
                inline=False
            )

            embed.add_field(
                name="Step 5: Restart Tinfoil",
                value="Close and reopen Tinfoil to scan TitleIDs\n"
                      "*If configured correctly, you'll see the custom message:* `RomM Switch Library`",
                inline=False
            )

            embed.add_field(
                name="Accessing Content",
                value="Your RomM content will now be available in:\n"
                      "• The `New Games` tab in Tinfoil\n"
                      "• The `File Browser` section you just configured",
                inline=False
            )

            # Add footer with note
            embed.set_footer(
                text="Need help? Check the RomM documentation or ask for support on GitHub/Discord"
            )

            await ctx.respond(embed=embed)

        except Exception as e:
            logger.error(f"Error in switch shop connection info command: {e}", exc_info=True)
            await ctx.respond("❌ An error occurred while displaying Switch shop connection info")
    
def setup(bot):
    bot.add_cog(Info(bot))
