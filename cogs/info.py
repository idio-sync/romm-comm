import discord
from discord.ext import commands
from discord.commands import slash_command
from datetime import datetime
from typing import Dict, Any
import logging

logger = logging.getLogger('romm_bot')

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
        self.has_switch = False
        bot.loop.create_task(self.check_switch_platform())
     
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
        user_count_data = self.bot.cache.get('user_count')        
        if not stats_data:
            return
        
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
    
    async def check_switch_platform(self):
        """Check if Switch is available in platforms"""
        await self.bot.wait_until_ready()
        try:
            # Get platforms from API
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if raw_platforms:
                platforms_data = self.bot.sanitize_data(raw_platforms, data_type='platforms')
                # Check if Switch exists in platforms
                self.has_switch = any(
                    platform['name'].lower() in ['nintendo switch', 'switch'] 
                    for platform in platforms_data
                )
                logger.info(f"Switch platform {'found' if self.has_switch else 'not found'} in platform list")
        except Exception as e:
            logger.error(f"Error checking Switch platform: {e}")
            self.has_switch = False
            
    async def cog_slash_command_check(self, ctx: discord.ApplicationContext) -> bool:
        """This runs before any slash command in this cog"""
        # If it's the switch_shop_info command and Switch isn't available, block it
        if ctx.command.name == 'switch_shop_info' and not self.has_switch:
            await ctx.respond("❌ Switch platform is not available on this RomM server", ephemeral=True)
            return False
        return True

    
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
            user_count_data = self.bot.cache.get('user_count')
            
            if stats_data:
                # Merge user count into stats data if available
                if user_count_data and 'user_count' in user_count_data:
                    stats_data['User Count'] = user_count_data['user_count']
                
                last_fetch_time = self.bot.cache.last_fetch.get('stats')
                if last_fetch_time:
                    time_str = datetime.fromtimestamp(last_fetch_time).strftime('%Y-%m-%d %H:%M:%S')
                else:
                    time_str = "Unknown"
                
                embed = discord.Embed(
                    title="Server Stats",
                    description=f"Last updated: {time_str}",
                    color=discord.Color.blue()
                )
            
                # Display all stats including user count in the same format
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
                platforms_data = self.bot.sanitize_data(raw_platforms, data_type='platforms')
            
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
        name="switch_shop_info",
        description="Instructions for connecting your Switch to this server"
    )
    async def switch_shop_info(self, ctx):
        """Display Switch shop connection setup instructions."""      
        try:
            # Get emojis with fallbacks
                       
            embed = discord.Embed(
                title=f"{self.bot.emoji_dict['switch']}  Switch Shop Connection Guide  {self.bot.emoji_dict['switch']}",
                description="Follow these steps to configure your Switch for connection to this server.\n"
                            "\n*Note: This guide assumes you have Tinfoil installed and know how to use its basic functions.*",
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
                value=(
                    f" Your RomM content will now be available in:\n"
                    "• The `New Games` tab in Tinfoil\n"
                    "• The `File Browser` section you just configured"
                ),
                inline=False
            )

            # Add footer with note
            embed.set_footer(
                text=(
                    f"Need help? Check the RomM documentation "
                    "or ask for support on GitHub/Discord"
                )
            )

            await ctx.respond(embed=embed)

        except Exception as e:
            logger.error(f"Error in switch shop connection info command: {e}", exc_info=True)
            await ctx.respond("❌ An error occurred while displaying Switch shop connection info")
     
#    # Website
#    @discord.slash_command(
#        name="website",
#        description="Get the RomM instance URL"
#    )
#    async def website(self, ctx):
#        """Website information command."""
#        await ctx.respond(
#            embed=discord.Embed(
#                title="Website Information",
#                description=self.bot.config.DOMAIN,
#                color=discord.Color.blue()
#        ))
#        pass

#   @discord.slash_command(
#        name="sync",
#        description="Manually sync bot commands (Owner only)"
#    )
#    @commands.is_owner()  # This ensures only the bot owner can use it
#    async def sync(self, ctx):
#       """Manually sync slash commands."""
#        try:
#            await ctx.defer(ephemeral=True)  # Show thinking state and make response private
#            
#            logger.info("Manual command sync")
#            await self.bot.sync_commands()
#            
#            embed = discord.Embed(
#                title="✅ Command Sync Successful",
#                description="All slash commands have been synced to Discord.",
#                color=discord.Color.green()
#            )
#            
#            await ctx.respond(embed=embed, ephemeral=True)
#            
#        except discord.HTTPException as e:
#            if e.code == 429:  # Rate limit error
#                retry_after = e.retry_after if hasattr(e, 'retry_after') else 3600
#                embed = discord.Embed(
#                    title="⚠️ Rate Limited",
#                    description=f"Command sync is rate limited. Please try again in {retry_after:.1f} seconds.",
#                    color=discord.Color.orange()
#                )
#            else:
#                embed = discord.Embed(
#                    title="❌ Sync Failed",
#                    description=f"Error: {str(e)}",
#                    color=discord.Color.red()
#                )
#            
#            await ctx.respond(embed=embed, ephemeral=True)
#            logger.error(f"Sync command error: {e}")

def setup(bot):
    bot.add_cog(Info(bot))
