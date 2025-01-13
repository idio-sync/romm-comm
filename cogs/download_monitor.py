import discord
from discord.ext import commands
import re
from datetime import datetime, timedelta
import asyncio
import logging
import aiosqlite
import os

logger = logging.getLogger('romm_bot.download_monitor')

class DownloadMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.download_pattern = re.compile(r'\[RomM\]\[rom\]\[.*?\] User (\w+) is downloading (.+)')
        self.monitor_task = None
        self.db_path = 'data/downloads.db'
        self.ensure_data_directory()

    def ensure_data_directory(self):
        """Ensure the data directory exists"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    async def init_db(self):
        """Initialize the SQLite database"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    rom_name TEXT NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            await db.commit()

    async def log_download(self, username: str, rom_name: str):
        """Log a download to the database"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                'INSERT INTO downloads (username, rom_name, timestamp) VALUES (?, ?, ?)',
                (username, rom_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            )
            await db.commit()

    async def get_stats(self, days: int, username: str = None):
        """Get download statistics for the specified period"""
        cutoff_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d %H:%M:%S')
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            
            # Base query conditions
            where_clause = "WHERE timestamp > ?"
            params = [cutoff_date]
            
            # Add username filter if specified
            if username:
                where_clause += " AND username = ?"
                params.append(username)

            # Get total downloads
            async with db.execute(
                f'SELECT COUNT(*) as count FROM downloads {where_clause}', 
                params
            ) as cursor:
                total_downloads = (await cursor.fetchone())['count']

            # Get downloads per day
            async with db.execute(f'''
                SELECT date(timestamp) as date, COUNT(*) as count 
                FROM downloads {where_clause}
                GROUP BY date(timestamp)
                ORDER BY date DESC
            ''', params) as cursor:
                daily_downloads = await cursor.fetchall()

            # Get most downloaded ROMs
            async with db.execute(f'''
                SELECT rom_name, COUNT(*) as count 
                FROM downloads {where_clause}
                GROUP BY rom_name 
                ORDER BY count DESC 
                LIMIT 5
            ''', params) as cursor:
                top_roms = await cursor.fetchall()

            # Get most active users
            if not username:  # Only if not filtering for specific user
                async with db.execute(f'''
                    SELECT username, COUNT(*) as count 
                    FROM downloads {where_clause}
                    GROUP BY username 
                    ORDER BY count DESC 
                    LIMIT 5
                ''', params) as cursor:
                    top_users = await cursor.fetchall()
            else:
                top_users = []

            # Get user's recent downloads if username specified
            user_history = []
            if username:
                async with db.execute(f'''
                    SELECT rom_name, timestamp
                    FROM downloads {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT 10
                ''', params) as cursor:
                    user_history = await cursor.fetchall()

            return {
                'total': total_downloads,
                'daily': daily_downloads,
                'top_roms': top_roms,
                'top_users': top_users,
                'user_history': user_history
            }

    @discord.slash_command(
        name="download_stats",
        description="Show download statistics for a specified period"
    )
    async def download_stats(
        self, 
        ctx, 
        days: discord.Option(int, "Number of days to show stats for", required=True, min_value=1, max_value=365),
        username: discord.Option(str, "Username to show stats for", required=False)
    ):
        await ctx.defer()  # Defer reply since this might take a moment

        stats = await self.get_stats(days, username)
        
        if stats['total'] == 0:
            await ctx.respond("No downloads found for the specified period.")
            return

        # Create the embed
        embed = discord.Embed(
            title="📊 Download Statistics",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )

        if username:
            embed.description = f"Stats for user **{username}** over the last {days} days"
        else:
            embed.description = f"Download stats for the last {days} days"

        # Total downloads
        embed.add_field(
            name="Total Downloads",
            value=str(stats['total']),
            inline=False
        )

        # Daily average
        daily_avg = stats['total'] / min(days, len(stats['daily']))
        embed.add_field(
            name="Daily Average",
            value=f"{daily_avg:.1f}",
            inline=True
        )

        # Top ROMs
        if stats['top_roms']:
            top_roms_text = "\n".join(
                f"• {rom['rom_name']} ({rom['count']} downloads)"
                for rom in stats['top_roms']
            )
            embed.add_field(
                name="Most Downloaded ROMs",
                value=top_roms_text or "No data",
                inline=False
            )

        # Top Users (only if not filtering by user)
        if not username and stats['top_users']:
            top_users_text = "\n".join(
                f"• {user['username']} ({user['count']} downloads)"
                for user in stats['top_users']
            )
            embed.add_field(
                name="Most Active Users",
                value=top_users_text or "No data",
                inline=False
            )

        # User's recent downloads (only if filtering by user)
        if username and stats['user_history']:
            history_text = "\n".join(
                f"• {download['rom_name']} ({download['timestamp']})"
                for download in stats['user_history']
            )
            embed.add_field(
                name="Recent Downloads",
                value=history_text or "No recent downloads",
                inline=False
            )

        await ctx.respond(embed=embed)

    async def start_monitoring(self):
        """Start monitoring downloads"""
        await self.init_db()  # Initialize database
        
        try:
            process = await asyncio.create_subprocess_shell(
                'docker logs -f romm',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            while True:
                line = await process.stdout.readline()
                if not line:
                    break

                line = line.decode('utf-8').strip()
                
                if "[RomM][rom]" in line and "is downloading" in line:
                    match = self.download_pattern.search(line)
                    if match:
                        username = match.group(1)
                        rom_name = match.group(2)
                        
                        # Log to database
                        await self.log_download(username, rom_name)
                        
                        embed = discord.Embed(
                            title="🎮 New Download",
                            description=f"**{rom_name}**",
                            color=discord.Color.blue(),
                            timestamp=datetime.now()
                        )
                        embed.add_field(
                            name="User",
                            value=username,
                            inline=True
                        )
                        
                        channel = self.bot.get_channel(self.bot.config.CHANNEL_ID)
                        if channel:
                            try:
                                await channel.send(embed=embed)
                                logger.info(f"Download notification sent for {rom_name}")
                            except Exception as e:
                                logger.error(f"Failed to send download notification: {e}")

        except Exception as e:
            logger.error(f"Error in download monitoring: {e}")
            await asyncio.sleep(30)
            self.monitor_task = asyncio.create_task(self.start_monitoring())

    @commands.Cog.listener()
    async def on_ready(self):
        """Start monitoring when the bot is ready."""
        if not self.monitor_task:
            self.monitor_task = asyncio.create_task(self.start_monitoring())
            logger.info("Download monitor started")

    def cog_unload(self):
        """Clean up when the cog is unloaded."""
        if self.monitor_task:
            self.monitor_task.cancel()
            logger.info("Download monitor stopped")

def setup(bot):
    bot.add_cog(DownloadMonitor(bot))