import aiosqlite
import asyncio
import os
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
import shutil
from datetime import datetime
from contextlib import asynccontextmanager
import aiohttp
import json
from pathlib import Path

logger = logging.getLogger('romm_bot.database')

class MasterDatabase:
    """Centralized database manager for all bot data"""
    
    def __init__(self, db_path: str = "data/romm_bot.db"):
        self.db_path = db_path
        self.data_dir = Path('data')
        self._initialized = False
        self._connection_pool = []
        self._pool_size = 5
        self._pool_lock = asyncio.Lock()
        
    async def initialize(self):
        """Initialize master database with all tables"""
        if self._initialized:
            logger.debug("Database already initialized, skipping")
            return
        
        try:
            logger.debug(f"Starting database initialization at {self.db_path}")
            
            # Ensure data directory exists
            self.data_dir.mkdir(exist_ok=True)
            logger.debug(f"Data directory ensured at {self.data_dir}")
            
            # Check if we need to migrate
            needs_migration = self._check_for_old_databases()
            if needs_migration:
                logger.info("Old databases detected, will migrate after table creation")
            
            # Create or open database with proper error handling
            try:
                async with aiosqlite.connect(self.db_path) as db:
                    logger.debug(f"Connected to database at {self.db_path}")
                    
                    # Enable WAL mode for better concurrency
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.execute("PRAGMA busy_timeout=10000")  # 10 second timeout
                    await db.execute("PRAGMA synchronous=NORMAL")
                    logger.debug("Database pragmas set successfully")
                    
                    # Create all tables with proper error handling
                    logger.debug("Creating database tables...")
                    
                    # Create each table set with error handling
                    try:
                        await self._create_recent_roms_tables(db)
                        logger.debug("✓ Recent ROMs tables created")
                    except Exception as e:
                        logger.error(f"Failed to create recent ROMs tables: {e}")
                        raise
                    
                    try:
                        await self._create_request_tables(db)
                        logger.debug("✓ Request tables created")
                    except Exception as e:
                        logger.error(f"Failed to create request tables: {e}")
                        raise
                    
                    try:
                        await self._create_user_tables(db)
                        logger.debug("✓ User tables created")
                    except Exception as e:
                        logger.error(f"Failed to create user tables: {e}")
                        raise
                    
                    try:
                        await self._create_emoji_tables(db)
                        logger.debug("✓ Emoji tables created")
                    except Exception as e:
                        logger.error(f"Failed to create emoji tables: {e}")
                        raise
                    
                    # Add version tracking
                    try:
                        await db.execute("""
                            CREATE TABLE IF NOT EXISTS db_version (
                                version INTEGER PRIMARY KEY,
                                migrated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                            )
                        """)
                        logger.debug("✓ Version tracking table created")
                    except Exception as e:
                        logger.error(f"Failed to create version table: {e}")
                        raise
                    
                    await db.commit()
                    logger.debug("All tables committed successfully")
                    
                    # Verify tables were created
                    cursor = await db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                    tables = await cursor.fetchall()
                    table_names = [t[0] for t in tables]
                    logger.debug(f"Tables in database: {', '.join(table_names)}")
                    
            except aiosqlite.Error as e:
                logger.error(f"Database connection error: {e}")
                raise
            except Exception as e:
                logger.error(f"Unexpected error during table creation: {e}")
                raise
            
            # Perform migration from seperate db setup if needed
            if needs_migration:
                logger.info("Starting migration of old databases...")
                try:
                    await self.migrate_existing_databases()
                except Exception as e:
                    logger.error(f"Migration failed: {e}")
                    # Migration failure is not critical for new installations
                    logger.warning("Continuing despite migration failure")
                    
            # Migrate for GGRequestz support (adds column if needed)
            await self.migrate_for_ggrequestz()
            
            logger.debug("Initializing platform mappings...")
            await self.initialize_platform_mappings()
            
            self._initialized = True
            logger.info("✅ Master database initialization completed successfully")
            
        except Exception as e:
            logger.error(f"❌ Database initialization failed: {e}", exc_info=True)
            raise
    
    @asynccontextmanager
    async def get_connection(self):
        """Provides a managed database connection."""
        if not self._initialized:
            logger.error("Database has not been initialized!")
            raise RuntimeError("Database is not available.")

        conn = None
        exception_occurred = False
        try:
            # This creates a new connection for each request.
            # It's safe and prevents concurrency issues.
            conn = await aiosqlite.connect(self.db_path)
            await conn.execute("PRAGMA busy_timeout=10000") # Set timeout
            yield conn
        except aiosqlite.Error as e:
            exception_occurred = True
            logger.error(f"Database connection error: {e}")
            raise
        except Exception:
            exception_occurred = True
            raise
        finally:
            if conn:
                if exception_occurred:
                    await conn.rollback()
                else:
                    await conn.commit()
                await conn.close()
    
    def _check_for_old_databases(self) -> bool:
        """Check if old database files exist"""
        old_dbs = [
            'data/recent_roms.db',
            'data/requests.db',
            'data/users.db'
        ]
        return any(os.path.exists(db) for db in old_dbs)
    
    async def migrate_existing_databases(self):
        """Migrate data from old separate databases to the master database"""
        migration_summary = {
            'recent_roms': {'migrated': 0, 'failed': 0},
            'requests': {'migrated': 0, 'failed': 0},
            'users': {'migrated': 0, 'failed': 0}
        }
        
        try:
            # Migrate recent_roms.db
            old_roms_db = 'data/recent_roms.db'
            if os.path.exists(old_roms_db):
                logger.info(f"Migrating data from {old_roms_db}...")
                try:
                    async with aiosqlite.connect(old_roms_db) as old_db:
                        # Check if the table exists
                        cursor = await old_db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='posted_roms'"
                        )
                        if await cursor.fetchone():
                            # Fetch all posted ROMs
                            cursor = await old_db.execute(
                                "SELECT rom_id, platform_name, rom_name, posted_at, batch_id FROM posted_roms"
                            )
                            roms = await cursor.fetchall()
                            
                            # Insert into new database - use direct connection during migration
                            async with aiosqlite.connect(self.db_path) as new_db:
                                for rom in roms:
                                    try:
                                        await new_db.execute(
                                            """INSERT OR IGNORE INTO posted_roms 
                                            (rom_id, platform_name, rom_name, posted_at, batch_id)
                                            VALUES (?, ?, ?, ?, ?)""",
                                            rom
                                        )
                                        migration_summary['recent_roms']['migrated'] += 1
                                    except Exception as e:
                                        logger.warning(f"Failed to migrate ROM {rom[0]}: {e}")
                                        migration_summary['recent_roms']['failed'] += 1
                                
                                await new_db.commit()
                            logger.info(f"✓ Migrated {migration_summary['recent_roms']['migrated']} posted ROMs")
                        else:
                            logger.warning("posted_roms table not found in old database")
                except Exception as e:
                    logger.error(f"Error migrating recent_roms.db: {e}")
            
            # Migrate requests.db
            old_requests_db = 'data/requests.db'
            if os.path.exists(old_requests_db):
                logger.info(f"Migrating data from {old_requests_db}...")
                try:
                    async with aiosqlite.connect(old_requests_db) as old_db:
                        # Migrate requests table
                        cursor = await old_db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='requests'"
                        )
                        if await cursor.fetchone():
                            # Get column names from old table
                            cursor = await old_db.execute("PRAGMA table_info(requests)")
                            columns_info = await cursor.fetchall()
                            column_names = [col[1] for col in columns_info]
                            
                            # Fetch all requests
                            cursor = await old_db.execute("SELECT * FROM requests")
                            requests = await cursor.fetchall()
                            
                            async with aiosqlite.connect(self.db_path) as new_db:
                                for request in requests:
                                    try:
                                        # Build dynamic insert based on available columns
                                        request_dict = dict(zip(column_names, request))
                                        
                                        # Prepare columns and values for insert
                                        insert_cols = []
                                        insert_vals = []
                                        for col in ['id', 'user_id', 'username', 'platform', 'game_name', 
                                                  'details', 'status', 'created_at', 'updated_at', 
                                                  'fulfilled_by', 'fulfiller_name', 'notes', 
                                                  'auto_fulfilled', 'igdb_id', 'platform_mapping_id',
                                                  'igdb_game_name', 'ggr_request_id']:
                                            if col in request_dict:
                                                insert_cols.append(col)
                                                insert_vals.append(request_dict[col])
                                        
                                        if insert_cols:
                                            placeholders = ','.join(['?' for _ in insert_cols])
                                            cols_str = ','.join(insert_cols)
                                            
                                            await new_db.execute(
                                                f"INSERT OR IGNORE INTO requests ({cols_str}) VALUES ({placeholders})",
                                                insert_vals
                                            )
                                            migration_summary['requests']['migrated'] += 1
                                    except Exception as e:
                                        logger.warning(f"Failed to migrate request {request_dict.get('id', 'unknown')}: {e}")
                                        migration_summary['requests']['failed'] += 1
                                
                                await new_db.commit()
                            logger.info(f"✓ Migrated {migration_summary['requests']['migrated']} requests")
                        
                        # Migrate request_subscribers table
                        cursor = await old_db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='request_subscribers'"
                        )
                        if await cursor.fetchone():
                            cursor = await old_db.execute(
                                "SELECT request_id, user_id, username, created_at FROM request_subscribers"
                            )
                            subscribers = await cursor.fetchall()
                            
                            async with aiosqlite.connect(self.db_path) as new_db:
                                subscriber_count = 0
                                for sub in subscribers:
                                    try:
                                        await new_db.execute(
                                            """INSERT OR IGNORE INTO request_subscribers 
                                            (request_id, user_id, username, created_at)
                                            VALUES (?, ?, ?, ?)""",
                                            sub
                                        )
                                        subscriber_count += 1
                                    except Exception as e:
                                        logger.warning(f"Failed to migrate subscriber: {e}")
                                
                                await new_db.commit()
                                logger.info(f"✓ Migrated {subscriber_count} request subscribers")
                        
                        # Migrate platform_mappings if it exists
                        cursor = await old_db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='platform_mappings'"
                        )
                        if await cursor.fetchone():
                            cursor = await old_db.execute(
                                """SELECT display_name, folder_name, igdb_slug, moby_slug, 
                                in_romm, romm_id FROM platform_mappings"""
                            )
                            platforms = await cursor.fetchall()
                            
                            async with aiosqlite.connect(self.db_path) as new_db:
                                platform_count = 0
                                for platform in platforms:
                                    try:
                                        await new_db.execute('''
                                            INSERT INTO platform_mappings
                                            (display_name, folder_name, igdb_slug, moby_slug)
                                            VALUES (?, ?, ?, ?)
                                            ON CONFLICT(display_name) DO UPDATE SET
                                                folder_name = excluded.folder_name,
                                                igdb_slug = excluded.igdb_slug,
                                                moby_slug = excluded.moby_slug
                                        ''', (platform[0], platform[1], platform[2], platform[3]))
                                        platform_count += 1
                                    except Exception as e:
                                        logger.warning(f"Failed to migrate platform: {e}")
                                
                                await new_db.commit()
                                logger.info(f"✓ Migrated {platform_count} platform mappings")
                                
                except Exception as e:
                    logger.error(f"Error migrating requests.db: {e}")
            
            # Migrate users.db
            old_users_db = 'data/users.db'
            if os.path.exists(old_users_db):
                logger.info(f"Migrating data from {old_users_db}...")
                try:
                    async with aiosqlite.connect(old_users_db) as old_db:
                        cursor = await old_db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_links'"
                        )
                        if await cursor.fetchone():
                            cursor = await old_db.execute(
                                """SELECT discord_id, romm_username, romm_id, 
                                discord_username, discord_avatar, created_at, updated_at 
                                FROM user_links"""
                            )
                            users = await cursor.fetchall()
                            
                            async with aiosqlite.connect(self.db_path) as new_db:
                                for user in users:
                                    try:
                                        await new_db.execute(
                                            """INSERT OR REPLACE INTO user_links 
                                            (discord_id, romm_username, romm_id, discord_username, 
                                             discord_avatar, created_at, updated_at)
                                            VALUES (?, ?, ?, ?, ?, ?, ?)""",
                                            user
                                        )
                                        migration_summary['users']['migrated'] += 1
                                    except Exception as e:
                                        logger.warning(f"Failed to migrate user {user[0]}: {e}")
                                        migration_summary['users']['failed'] += 1
                                
                                await new_db.commit()
                            logger.info(f"✓ Migrated {migration_summary['users']['migrated']} user links")
                        else:
                            logger.warning("user_links table not found in old database")
                except Exception as e:
                    logger.error(f"Error migrating users.db: {e}")
            
            # Create backup directory and move old databases
            backup_dir = Path('data/backup_old_dbs')
            backup_dir.mkdir(exist_ok=True)
            
            for old_db in ['data/recent_roms.db', 'data/requests.db', 'data/users.db']:
                if os.path.exists(old_db):
                    try:
                        backup_path = backup_dir / Path(old_db).name
                        shutil.move(old_db, backup_path)
                        logger.info(f"✓ Moved {old_db} to {backup_path}")
                    except Exception as e:
                        logger.warning(f"Could not move {old_db}: {e}")
            
            # Log migration summary
            logger.info("=== Migration Summary ===")
            for db_name, stats in migration_summary.items():
                if stats['migrated'] > 0 or stats['failed'] > 0:
                    logger.info(f"{db_name}: {stats['migrated']} migrated, {stats['failed']} failed")
            
            # Mark migration as complete
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("INSERT INTO db_version (version) VALUES (1)")
                await db.commit()
            
            logger.info("✅ Migration completed successfully")
            
        except Exception as e:
            logger.error(f"Critical error during migration: {e}", exc_info=True)
            raise
    
    async def migrate_for_ggrequestz(self):
        """Add GGRequestz integration support to existing databases"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Check if column already exists
                cursor = await db.execute("PRAGMA table_info(requests)")
                columns = await cursor.fetchall()
                column_names = [col[1] for col in columns]
                
                if 'ggr_request_id' in column_names:
                    logger.debug("GGRequestz column already exists")
                    return True
                
                # Add column
                logger.info("Adding ggr_request_id column for GGRequestz integration...")
                await db.execute(
                    "ALTER TABLE requests ADD COLUMN ggr_request_id INTEGER"
                )
                
                # Add index
                await db.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ggr_request_id ON requests(ggr_request_id)"
                )
                
                await db.commit()
                
                logger.info("✅ GGRequestz migration completed successfully")
                return True
                
        except Exception as e:
            logger.error(f"GGRequestz migration failed: {e}")
            return False
    
    async def _create_recent_roms_tables(self, db):
        """Create tables for RecentRoms cog"""
        logger.debug("Creating posted_roms table...")
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS posted_roms (
                rom_id INTEGER PRIMARY KEY,
                platform_name TEXT,
                rom_name TEXT,
                posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                message_id INTEGER,
                batch_id TEXT
            )
        ''')
        
        # Add index for better query performance
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_posted_roms_batch 
            ON posted_roms(batch_id)
        ''')
        
        logger.debug("posted_roms table and indexes created")
    
    async def _create_request_tables(self, db):
        """Create tables for Request cog"""
        
        logger.debug("Creating requests tables...")
        
        # Main requests table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                platform TEXT NOT NULL,
                game_name TEXT NOT NULL,
                details TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fulfilled_by INTEGER,
                fulfiller_name TEXT,
                notes TEXT,
                auto_fulfilled BOOLEAN DEFAULT 0,
                igdb_id INTEGER,
                platform_mapping_id INTEGER,
                igdb_game_name TEXT,
                ggr_request_id INTEGER
            )
        ''')
        
        logger.debug("requests table created")
        
        # Request subscribers
        await db.execute('''
            CREATE TABLE IF NOT EXISTS request_subscribers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(request_id, user_id)
            )
        ''')
        
        logger.debug("request_subscribers table created")
        
        # Platform mappings
        await db.execute('''
            CREATE TABLE IF NOT EXISTS platform_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                display_name TEXT NOT NULL UNIQUE,
                folder_name TEXT NOT NULL,
                igdb_slug TEXT,
                moby_slug TEXT,
                in_romm BOOLEAN DEFAULT 0,
                romm_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        logger.debug("platform_mappings table created")
        
        # Add indexes
        await db.execute('CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_requests_user ON requests(user_id)')
        await db.execute('CREATE INDEX IF NOT EXISTS idx_platform_mappings_name ON platform_mappings(display_name)')
        # await db.execute('CREATE INDEX IF NOT EXISTS idx_ggr_request_id ON requests(ggr_request_id)')

        logger.debug("Request table indexes created")
    
    async def _create_user_tables(self, db):
        """Create tables for UserManager cog"""
        logger.debug("Creating user_links table...")
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_links (
                discord_id INTEGER PRIMARY KEY,
                romm_username TEXT NOT NULL,
                romm_id INTEGER NOT NULL,
                discord_username TEXT,
                discord_avatar TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Add index for username lookups
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_romm_username 
            ON user_links(romm_username COLLATE NOCASE)
        ''')
        
        logger.debug("user_links table and indexes created")
    
    async def _create_emoji_tables(self, db):
        """Create tables for emoji management"""
        logger.debug("Creating emoji_sync_state table...")
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS emoji_sync_state (
                guild_id INTEGER PRIMARY KEY,
                emoji_list TEXT,  -- JSON array of emoji names
                nitro_status BOOLEAN DEFAULT 0,
                emoji_limit INTEGER DEFAULT 50,
                last_sync TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Add index for faster lookups
        await db.execute('''
            CREATE INDEX IF NOT EXISTS idx_emoji_sync_guild 
            ON emoji_sync_state(guild_id)
        ''')
        
        logger.debug("emoji_sync_state table and indexes created")
    
    async def verify_tables_exist(self) -> Dict[str, bool]:
        """Verify which tables exist in the database"""
        expected_tables = [
            'posted_roms',
            'requests',
            'request_subscribers',
            'platform_mappings',
            'user_links',
            'emoji_sync_state',
            'db_version'
        ]
        
        table_status = {}
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                existing_tables = [row[0] for row in await cursor.fetchall()]
                
                for table in expected_tables:
                    table_status[table] = table in existing_tables
                
                logger.debug(f"Table verification: {table_status}")
                
        except Exception as e:
            logger.error(f"Error verifying tables: {e}")
        
        return table_status
        
    async def get_user_link(self, discord_id: int) -> Optional[Dict[str, Any]]:
        """Get user link by Discord ID"""
        try:
            async with self.get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT discord_id, romm_username, romm_id, discord_username, 
                           discord_avatar, created_at, updated_at
                    FROM user_links 
                    WHERE discord_id = ?
                    """,
                    (discord_id,)
                )
                row = await cursor.fetchone()
                
                if row:
                    return {
                        'discord_id': row[0],
                        'romm_username': row[1],
                        'romm_id': row[2],
                        'discord_username': row[3],
                        'discord_avatar': row[4],
                        'created_at': row[5],
                        'updated_at': row[6]
                    }
                return None
        except Exception as e:
            logger.error(f"Error getting user link for {discord_id}: {e}")
            return None

    async def add_user_link(self, discord_id: int, romm_username: str, 
                           romm_id: int, discord_username: str = None, 
                           discord_avatar: str = None) -> bool:
        """Add or update a user link"""
        try:
            async with self.get_connection() as db:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO user_links 
                    (discord_id, romm_username, romm_id, discord_username, 
                     discord_avatar, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (discord_id, romm_username, romm_id, discord_username, discord_avatar)
                )
                await db.commit()
                logger.info(f"Added/updated user link for Discord ID {discord_id}")
                return True
        except Exception as e:
            logger.error(f"Error adding user link: {e}")
            return False

    async def delete_user_link(self, discord_id: int) -> bool:
        """Delete a user link"""
        try:
            async with self.get_connection() as db:
                await db.execute(
                    "DELETE FROM user_links WHERE discord_id = ?",
                    (discord_id,)
                )
                await db.commit()
                logger.info(f"Deleted user link for Discord ID {discord_id}")
                return True
        except Exception as e:
            logger.error(f"Error deleting user link: {e}")
            return False

    async def get_all_user_links(self) -> List[Dict[str, Any]]:
        """Get all user links"""
        try:
            async with self.get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT discord_id, romm_username, romm_id, discord_username, 
                           discord_avatar, created_at, updated_at
                    FROM user_links
                    """
                )
                rows = await cursor.fetchall()
                
                return [
                    {
                        'discord_id': row[0],
                        'romm_username': row[1],
                        'romm_id': row[2],
                        'discord_username': row[3],
                        'discord_avatar': row[4],
                        'created_at': row[5],
                        'updated_at': row[6]
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error(f"Error getting all user links: {e}")
            return []
    
    async def initialize_platform_mappings(self):
        """Initialize the master platform list from remote JSON file with local fallback"""
        url = "https://raw.githubusercontent.com/idio-sync/romm-comm/refs/heads/main/.backend/igdb/platform_mapping.json"
        platforms_file = Path('igdb') / 'platform_mapping.json'
        
        try:
            master_platforms = None
            
            # Try fetching from GitHub
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            text = await resp.text()
                            master_platforms = json.loads(text)
                            logger.debug(f"Loaded {len(master_platforms)} platforms from GitHub")
            except Exception as e:
                logger.warning(f"Could not fetch platforms from GitHub: {e}")
            
            # Fallback to local file
            if not master_platforms:
                if platforms_file.exists():
                    with open(platforms_file, 'r') as f:
                        master_platforms = json.load(f)
                        logger.info(f"Loaded {len(master_platforms)} platforms from local file")
                else:
                    logger.warning("No remote or local platforms found, using defaults")
                    # Create default platforms
                    master_platforms = [
                        {"display_name": "PC", "folder_name": "pc", "igdb_slug": "pc", "moby_slug": "windows"},
                        {"display_name": "Nintendo DS", "folder_name": "nds", "igdb_slug": "nds", "moby_slug": "nintendo-ds"},
                        {"display_name": "Nintendo 3DS", "folder_name": "3ds", "igdb_slug": "3ds", "moby_slug": "nintendo-3ds"},
                        {"display_name": "PlayStation Portable", "folder_name": "psp", "igdb_slug": "psp", "moby_slug": "psp"},
                        {"display_name": "Game Boy Advance", "folder_name": "gba", "igdb_slug": "gba", "moby_slug": "gameboy-advance"},
                        {"display_name": "Nintendo 64", "folder_name": "n64", "igdb_slug": "n64", "moby_slug": "nintendo-64"},
                        {"display_name": "PlayStation", "folder_name": "ps1", "igdb_slug": "ps", "moby_slug": "playstation"},
                        {"display_name": "PlayStation 2", "folder_name": "ps2", "igdb_slug": "ps2", "moby_slug": "playstation-2"},
                        {"display_name": "Xbox", "folder_name": "xbox", "igdb_slug": "xbox", "moby_slug": "xbox"},
                        {"display_name": "Xbox 360", "folder_name": "xbox360", "igdb_slug": "xbox360", "moby_slug": "xbox-360"},
                        {"display_name": "Wii", "folder_name": "wii", "igdb_slug": "wii", "moby_slug": "wii"},
                        {"display_name": "GameCube", "folder_name": "gamecube", "igdb_slug": "ngc", "moby_slug": "gamecube"},
                        {"display_name": "Super Nintendo", "folder_name": "snes", "igdb_slug": "snes", "moby_slug": "snes"},
                        {"display_name": "Nintendo Entertainment System", "folder_name": "nes", "igdb_slug": "nes", "moby_slug": "nes"},
                        {"display_name": "Sega Genesis", "folder_name": "genesis", "igdb_slug": "smd", "moby_slug": "genesis"},
                        {"display_name": "Sega Saturn", "folder_name": "saturn", "igdb_slug": "saturn", "moby_slug": "saturn"},
                        {"display_name": "Sega Dreamcast", "folder_name": "dreamcast", "igdb_slug": "dc", "moby_slug": "dreamcast"},
                        {"display_name": "Arcade", "folder_name": "arcade", "igdb_slug": "arcade", "moby_slug": "arcade"}
                    ]
                    
                    # Save defaults to file for next time
                    platforms_file.parent.mkdir(exist_ok=True)
                    with open(platforms_file, 'w') as f:
                        json.dump(master_platforms, f, indent=2)
            
            # Populate database - use direct connection since we're in initialization
            if master_platforms:
                # Use a direct connection here, not get_connection()
                async with aiosqlite.connect(self.db_path) as db:
                    # Enable pragmas for this connection
                    await db.execute("PRAGMA journal_mode=WAL")
                    await db.execute("PRAGMA busy_timeout=10000")
                    
                    # Reset all platforms to not in romm
                    # await db.execute("UPDATE platform_mappings SET in_romm = 0")
                    
                    # Insert/update platforms
                    for platform in master_platforms:
                        await db.execute('''
                            INSERT INTO platform_mappings 
                            (display_name, folder_name, igdb_slug, moby_slug)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(display_name) DO UPDATE SET
                                folder_name = excluded.folder_name,
                                igdb_slug = excluded.igdb_slug,
                                moby_slug = excluded.moby_slug
                        ''', (
                            platform['display_name'],
                            platform['folder_name'],
                            platform.get('igdb_slug'),
                            platform.get('moby_slug')
                        ))
                    
                    await db.commit()
                    
                    cursor = await db.execute("SELECT COUNT(*) FROM platform_mappings")
                    count = (await cursor.fetchone())[0]
                    logger.debug(f"Platform mappings initialized with {count} platforms")
                    
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error initializing platform mappings: {e}", exc_info=True)
            return False
    
    async def get_platform_mappings(self, search_term: str = None) -> List[Dict]:
        """Get platform mappings with optional search"""
        async with self.get_connection() as db:
            if search_term:
                cursor = await db.execute('''
                    SELECT display_name, folder_name, igdb_slug, moby_slug, in_romm, romm_id
                    FROM platform_mappings
                    WHERE LOWER(display_name) LIKE ? OR LOWER(folder_name) LIKE ?
                    ORDER BY in_romm DESC, display_name
                    LIMIT 25
                ''', (f'%{search_term.lower()}%', f'%{search_term.lower()}%'))
            else:
                cursor = await db.execute(
                    "SELECT display_name, folder_name, igdb_slug, moby_slug, in_romm, romm_id FROM platform_mappings"
                )
            
            rows = await cursor.fetchall()
            return [
                {
                    'display_name': row[0],
                    'folder_name': row[1],
                    'igdb_slug': row[2],
                    'moby_slug': row[3],
                    'in_romm': row[4],
                    'romm_id': row[5]
                }
                for row in rows
            ]
