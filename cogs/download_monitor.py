import discord
from discord.ext import commands
import re
from datetime import datetime, timedelta
import asyncio
import logging
import aiosqlite
import os
import docker

logger = logging.getLogger('romm_bot.download_monitor')

class DownloadMonitor(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.docker_client = docker.from_env()
        self.monitor_task = None
        self.db_path = 'data/downloads.db'
        self.download_pattern = re.compile(r'INFO:\s+\[RomM\]\[rom\]\[([^\]]+)\] User (\w+) is downloading (.+)')
        self.ensure_data_directory()
        self.init_task = asyncio.create_task(self.init_db())

    def ensure_data_directory(self):
        """Ensure the data directory exists"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    async def init_db(self):
        """Initialize the SQLite database"""
        try:
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
        except Exception as e:
            logger.error(f"Error initializing database: {e}")
            raise

    async def log_download(self, username: str, rom_name: str):
        """Log a download to the database"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'INSERT INTO downloads (username, rom_name, timestamp) VALUES (?, ?, ?)',
                    (username, rom_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                )
                await db.commit()
                logger.info(f"Download logged to database - User: {username}, ROM: {rom_name}")
        except Exception as e:
            logger.error(f"Database error in log_download: {e}")
            raise

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
            if not username:
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

    async def start_monitoring(self):
        """Start monitoring downloads"""
        await self.init_db()
        logger.info("Starting download monitoring...")
        print("Starting download monitoring...")  # Console print
    
        while True:
            try:
                # Get container synchronously
                romm_container = self.docker_client.containers.get('romm')
                logger.info(f"Connected to container: {romm_container.name}")
                print(f"Connected to container: {romm_container.name}")
    
                def handle_logs():
                    """Synchronous function to handle log streaming"""
                    try:
                        # Test getting a single recent log first
                        recent = romm_container.logs(tail=1).decode()
                        print(f"Last log line: {recent}")
    
                        # Start streaming logs
                        log_generator = romm_container.logs(
                            stream=True, 
                            follow=True, 
                            tail=0,
                            stdout=True,
                            stderr=True
                        )
    
                        print("Starting log stream...")
                        for log in log_generator:
                            line = log.decode('utf-8').strip()
                            print(f"Log received: {line}")  # Print every line
    
                            if "INFO:    [RomM][rom]" in line:
                                print(f"Found RomM log: {line}")
                                if "is downloading" in line:
                                    print("Found download entry")
                                    return line  # Return the line for async processing
                    except Exception as e:
                        print(f"Error in handle_logs: {e}")
                        return None
    
                while True:
                    # Run log handling in executor
                    download_line = await self.bot.loop.run_in_executor(None, handle_logs)
                    
                    if download_line:
                        match = self.download_pattern.search(download_line)
                        if match:
                            timestamp, username, rom_name = match.groups()
                            print(f"Matched download - Time: {timestamp}, User: {username}, ROM: {rom_name}")
    
                            # Log to database
                            await self.log_download(username, rom_name)
    
                            # Create and send embed
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
                                await channel.send(embed=embed)
                                print(f"Notification sent for {rom_name}")
                            else:
                                print(f"Channel not found: {self.bot.config.CHANNEL_ID}")
    
            except Exception as e:
                print(f"Monitor error: {e}")
                logger.error(f"Monitor error: {e}")
                await asyncio.sleep(30)
                continue
                
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
        await ctx.defer()
        
        try:
            await self.init_task
        except Exception as e:
            await ctx.respond("Error accessing database. Please try again later.")
            logger.error(f"Database initialization error in download_stats: {e}")
            return

        stats = await self.get_stats(days, username)
        
        if stats['total'] == 0:
            await ctx.respond("No downloads found for the specified period.")
            return

        embed = discord.Embed(
            title="📊 Download Statistics",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )

        if username:
            embed.description = f"Stats for user **{username}** over the last {days} days"
        else:
            embed.description = f"Download stats for the last {days} days"

        embed.add_field(
            name="Total Downloads",
            value=str(stats['total']),
            inline=False
        )

        daily_avg = stats['total'] / min(days, len(stats['daily']))
        embed.add_field(
            name="Daily Average",
            value=f"{daily_avg:.1f}",
            inline=True
        )

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
