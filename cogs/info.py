import discord
from discord.ext import commands
from discord import Option
from datetime import datetime

class Info(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
    @commands.slash_command(
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
    
    @commands.slash_command(
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
        )
    )
    
    @commands.slash_command(
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

def setup(bot):
    bot.add_cog(Info(bot))
