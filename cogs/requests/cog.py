"""The Request cog: slash commands and the scan-completion listener."""

import asyncio
import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from admin_checks import is_admin

from ..igdb_client import IGDBClient
from ..search import build_rom_download_url
from .embeds import (
    PLATFORM_MISSING_NOTE,
    build_already_requested_embed,
    build_request_submitted_embed,
    build_subscribed_embed,
    format_igdb_details,
)
from .matching import edit_distance_ratio, find_duplicate_request
from .repo import REQUEST_COLUMNS, RequestsRepo
from .responders import responder_for
from .views_admin import RequestAdminView
from .views_game import ExistingGameWithIGDBView, GameSelectView
from .views_user import UserRequestsView

logger = logging.getLogger(__name__)

# How many requests one user may have open at once.
MAX_PENDING_REQUESTS = 25


class Request(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.igdb: Optional[IGDBClient] = None
        
        # Use master db
        self.db = bot.db
        self.repo = RequestsRepo(bot.db)
        
        self.requests_enabled = bot.config.REQUESTS_ENABLED
        self.ggr = None
        bot.loop.create_task(self.setup())
        self.processing_lock = asyncio.Lock()

    async def cog_check(self, ctx: discord.ApplicationContext) -> bool:
        """Check if requests are enabled before any command in this cog"""
        if not self.requests_enabled and not ctx.author.guild_permissions.administrator:
            await ctx.respond("❌ The request system is currently disabled.")
            return False
        return True

    async def cog_unload(self):
        """Cleanup resources when cog is unloaded"""
        if self.igdb:
            await self.igdb.close()
            logger.debug("IGDB client session closed")

    async def setup(self):
        """Set up database and initialize IGDB client"""
        try:
            # Initialize IGDB client
            self.igdb = IGDBClient(self.bot.config)
            
            # Sync Romm platforms (mappings already initialized by database)
            await self.sync_romm_platforms()
            
            logger.debug("Request cog setup completed successfully")
        except ValueError as e:
            logger.warning(f"IGDB integration disabled: {e}")
            self.igdb = None
            # Still try to sync platforms even without IGDB
            try:
                await self.sync_romm_platforms()
            except Exception as sync_error:
                logger.error(f"Failed to sync platforms: {sync_error}")
        
        # Get GGRequestz integration
        self.ggr = self.bot.get_cog('GGRequestzIntegration')
        if self.ggr and self.ggr.enabled:
            logger.info("✅ Request cog using GGRequestz integration")
        else:
            logger.info("📁 Request cog using local database only")
            
    async def _get_canonical_platform_name(self, platform_name: str) -> Optional[str]:
        """Looks up the canonical display_name for a given platform alias."""
        if not platform_name:
            return None
        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    """SELECT display_name FROM platform_mappings 
                       WHERE LOWER(display_name) = LOWER(?) OR LOWER(folder_name) = LOWER(?)
                       LIMIT 1""",
                    (platform_name, platform_name)
                )
                result = await cursor.fetchone()
                return result['display_name'] if result else platform_name
        except Exception:
            # If DB fails, fallback to using the original name
            return platform_name

    async def get_platform_request_context(self, platform_name: str) -> Tuple[str, Optional[int], bool]:
        """Return display name, mapping ID, and RomM availability for request creation."""
        if not platform_name:
            return platform_name, None, False

        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    """SELECT id, display_name, in_romm
                       FROM platform_mappings
                       WHERE LOWER(display_name) = LOWER(?) OR LOWER(folder_name) = LOWER(?)
                       LIMIT 1""",
                    (platform_name, platform_name)
                )
                result = await cursor.fetchone()

                if result:
                    mapping_id, display_name, in_romm = result
                    return display_name, mapping_id, bool(in_romm)

        except Exception as e:
            logger.warning(f"Could not resolve request platform context for '{platform_name}': {e}")

        return platform_name, None, False
    
    @property
    def igdb_enabled(self) -> bool:
        """Check if IGDB integration is enabled"""
        return self.igdb is not None

    async def sync_romm_platforms(self):
        """Sync current Romm platforms with master list"""
        try:
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                logger.warning("No platforms returned from Romm API")
                return
            
            logger.info(f"Syncing {len(raw_platforms)} platforms from Romm")
            
            async with self.db.get_connection() as db:
                for platform in raw_platforms:
                    custom_name = platform.get('custom_name', '').strip() if platform.get('custom_name') else None
                    platform_name = platform.get('name', '')
                    platform_id = platform.get('id')
                    
                    # Log what we're trying to match
                    logger.debug(f"Syncing platform - Name: '{platform_name}', Custom: '{custom_name}', ID: {platform_id}")
                    
                    # Build query to match by various name combinations
                    query_params = []
                    conditions = []
                    
                    # Match by regular name
                    if platform_name:
                        conditions.append("LOWER(display_name) = LOWER(?)")
                        query_params.append(platform_name)
                        conditions.append("LOWER(folder_name) = LOWER(?)")
                        query_params.append(platform_name)
                        conditions.append("LOWER(folder_name) = LOWER(?)")
                        query_params.append(platform_name.replace(' ', '-'))
                    
                    # Also match by custom name if it exists
                    if custom_name:
                        conditions.append("LOWER(display_name) = LOWER(?)")
                        query_params.append(custom_name)
                        conditions.append("LOWER(folder_name) = LOWER(?)")
                        query_params.append(custom_name)
                    
                    if not conditions:
                        logger.warning(f"Platform has no valid name: {platform}")
                        continue
                    
                    query = f'''
                        SELECT id, display_name FROM platform_mappings 
                        WHERE {' OR '.join(conditions)}
                        LIMIT 1
                    '''
                    
                    cursor = await db.execute(query, query_params)
                    result = await cursor.fetchone()
                    
                    if result:
                        mapping_id, mapping_name = result
                        # Update the mapping to show it exists in Romm
                        await db.execute('''
                            UPDATE platform_mappings 
                            SET in_romm = 1, romm_id = ?
                            WHERE id = ?
                        ''', (platform_id, mapping_id))
                        logger.debug(f"✓ Matched Romm platform '{custom_name or platform_name}' to mapping '{mapping_name}'")
                    else:
                        logger.warning(f"✗ No mapping found for Romm platform '{custom_name or platform_name}'")
                
                await db.commit()
                logger.info("Platform sync completed")
                
        except Exception as e:
            logger.error(f"Error syncing Romm platforms: {e}", exc_info=True)
    
    async def platform_autocomplete_all(self, ctx: discord.AutocompleteContext):
        """Autocomplete for all platforms, not just those in Romm"""
        try:
            user_input = ctx.value.lower()
            
            # Use the master database's connection method
            async with self.db.get_connection() as db:
                cursor = await db.execute('''
                    SELECT display_name, in_romm, folder_name
                    FROM platform_mappings
                    WHERE LOWER(display_name) LIKE ?
                    OR LOWER(folder_name) LIKE ?
                    ORDER BY 
                        in_romm DESC,
                        display_name
                    LIMIT 25
                ''', (f'%{user_input}%', f'%{user_input}%'))
                
                results = await cursor.fetchall()
            
            # Process results
            choices = []
            for display_name, in_romm, folder_name in results:
                if in_romm:
                    label = display_name
                else:
                    label = f"[+] {display_name}"
                
                choices.append(discord.OptionChoice(
                    name=label[:100],
                    value=display_name
                ))
            
            return choices
            
        except Exception as e:
            logger.error(f"Error in platform autocomplete: {e}")
            return []
    
    async def check_if_game_exists(self, platform: str, game_name: str) -> Tuple[bool, List[Dict]]:
        """Check if a game already exists in the database"""
        try:
            # Get raw platforms data to access custom_name field
            raw_platforms = await self.bot.fetch_api_endpoint('platforms')
            if not raw_platforms:
                return False, []

            # Find platform by name (including custom names)
            platform_id = None
            platform_lower = platform.lower()
            
            for p in raw_platforms:
                # Check custom name first
                custom_name = p.get('custom_name')
                if custom_name and custom_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    break
                
                # Check regular name
                regular_name = p.get('name', '')
                if regular_name.lower() == platform_lower:
                    platform_id = p.get('id')
                    break

            if not platform_id:
                return False, []

            # Search for the game
            search_response = await self.bot.fetch_api_endpoint(
                f'roms?platform_id={platform_id}&platform_ids={platform_id}&search_term={game_name}&limit=25'
            )

            # Handle paginated response
            if search_response and isinstance(search_response, dict) and 'items' in search_response:
                search_results = search_response['items']
            elif search_response and isinstance(search_response, list):
                search_results = search_response
            else:
                search_results = []

            if not search_results:
                return False, []

            # Return all reasonably matching games
            matches = []
            game_name_lower = game_name.lower()
            for rom in search_results:
                rom_name = rom.get('name', '').lower()
                # Simple matching - let the user decide what they want
                if (game_name_lower in rom_name or 
                    rom_name in game_name_lower or
                    edit_distance_ratio(game_name_lower, rom_name) > 0.7):
                    matches.append(rom)

            return bool(matches), matches

        except Exception as e:
            logger.error(f"Error checking game existence: {e}")
            return False, []
    

        
    @commands.Cog.listener()
    async def on_batch_scan_complete(self, new_games: List[Dict[str, str]]):
        """Handle batch scan completion event with improved matching logic."""
        async with self.processing_lock:
            try:
                if not new_games:
                    return

                logger.info(f"Processing batch of {len(new_games)} new games")

                pending_requests = []
                all_subscribers = defaultdict(list)
                
                async with self.db.get_connection() as db:
                    cursor = await db.execute("""
                        SELECT id, user_id, platform, game_name, igdb_id, igdb_game_name 
                        FROM requests WHERE status = 'pending'
                    """)
                    pending_requests = await cursor.fetchall()
                    
                    if not pending_requests:
                        return
                    
                    request_ids = [req['id'] for req in pending_requests]
                    placeholders = ','.join('?' * len(request_ids))
                    cursor = await db.execute(
                        f"SELECT request_id, user_id FROM request_subscribers WHERE request_id IN ({placeholders})",
                        request_ids
                    )
                    for req_id, user_id in await cursor.fetchall():
                        all_subscribers[req_id].append(user_id)
                
                fulfillments = []
                notifications = defaultdict(list)
                
                for req in pending_requests:
                    # Simplify the unpacking to match the new, explicit query
                    req_id, user_id, req_platform, req_game, req_igdb_id, req_igdb_game_name = req

                    for new_game in new_games:
                        new_game_igdb_id = new_game.get('igdb_id')

                        # NORMALIZE PLATFORM NAMES ---
                        canonical_req_platform = await self._get_canonical_platform_name(req_platform)
                        canonical_new_game_platform = await self._get_canonical_platform_name(new_game['platform'])
                        platform_match = canonical_req_platform and (canonical_req_platform == canonical_new_game_platform)
                        
                        # PRIORITIZE IGDB NAME FOR SIMILARITY CHECK ---
                        name_to_compare = req_igdb_game_name if req_igdb_game_name else req_game
                        name_match = edit_distance_ratio(name_to_compare, new_game['name']) > 0.8
                        
                        igdb_match = req_igdb_id is not None and new_game_igdb_id is not None and req_igdb_id == new_game_igdb_id

                        if platform_match and (igdb_match or name_match):
                            fulfillments.append({'req_id': req_id, 'game_name': new_game['name']})
                            notifications[user_id].append(new_game)
                            for subscriber_id in all_subscribers.get(req_id, []):
                                notifications[subscriber_id].append(new_game)
                            logger.info(f"Request #{req_id} ('{req_game}') matched to new game '{new_game['name']}'.")
                            break 
                
                if fulfillments:
                    async with self.db.get_connection() as db:
                        await db.executemany(
                            """UPDATE requests 
                               SET status = 'fulfilled', updated_at = CURRENT_TIMESTAMP, 
                                   notes = ?, auto_fulfilled = 1
                               WHERE id = ?""",
                            [(f"Automatically fulfilled - Found: {f['game_name']}", f['req_id']) for f in fulfillments]
                        )
                        await db.commit()
                
                # Sync status to ggrequestz
                if self.ggr and self.ggr.enabled:
                    for fulfillment in fulfillments:
                        req_id = fulfillment['req_id']
                        
                        # Get the ggr_request_id for this Discord request
                        async with self.db.get_connection() as db:
                            cursor = await db.execute(
                                "SELECT ggr_request_id FROM requests WHERE id = ?",
                                (req_id,)
                            )
                            result = await cursor.fetchone()
                            
                            if result and result['ggr_request_id']:
                                ggr_request_id = result['ggr_request_id']
                                # Update status in ggrequestz
                                result = await self.ggr.update_request_status(
                                    ggr_request_id=ggr_request_id,
                                    status='fulfilled',
                                    admin_name='Auto-Fulfillment Bot',
                                    notes=f"Automatically fulfilled - Found: {fulfillment['game_name']}"
                                )
                                
                                if result.get('success'):
                                    logger.info(f"✅ Synced fulfillment to ggrequestz for request #{req_id} (GGR ID: {ggr_request_id})")
                                else:
                                    logger.error(f"❌ Failed to sync fulfillment to ggrequestz: {result.get('error')}")
                
                if notifications:
                    logger.info(f"Sending DMs with links for {len(notifications)} user(s).")
                    for user_id, fulfilled_games in notifications.items():
                        try:
                            user = await self.bot.fetch_user(user_id)
                            if not user: continue

                            # De-duplicate games in case user subscribed to multiple similar requests
                            unique_games = {g['id']: g for g in fulfilled_games}.values()

                            for game in unique_games:
                                game_name = game['name']
                                rom_id = game['id']
                                filename = game.get('fs_name') or game.get('file_name')
                                
                                romm_url = f"{self.bot.config.DOMAIN}/rom/{rom_id}"
                                
                                message_parts = [f"✅ Your request for **{game_name}** is now available!"]
                                
                                romm_emoji = self.bot.get_formatted_emoji('romm')
                                link_parts = [f"{romm_emoji} [**View in RomM**]({romm_url})"]
                                
                                if filename:
                                    download_url = build_rom_download_url(self.bot.config.DOMAIN, rom_id, filename)
                                    link_parts.append(f"⬇️ [**Download**]({download_url})")
                                
                                # Join the links with a separator and add them as a single line
                                message_parts.append(" ".join(link_parts))

                                message = "\n".join(message_parts)
                                await user.send(message)
                                await asyncio.sleep(1)

                        except discord.Forbidden:
                            logger.warning(f"Could not notify user {user_id}: They have DMs disabled.")
                        except Exception as e:
                            logger.warning(f"Could not notify user {user_id}: {e}")
                    
            except Exception as e:
                logger.error(f"Error in batch scan completion handler: {e}", exc_info=True)

    async def check_pending_requests(self, platform: str, game_name: str) -> List[Tuple[int, int, str]]:
        """Check if there are any pending requests for this game"""
        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    """
                    SELECT id, user_id, game_name 
                    FROM requests 
                    WHERE platform = ? AND status = 'pending'
                    """,
                    (platform,)
                )
                pending_requests = await cursor.fetchall()

                fulfilled_requests = []
                for req_id, user_id, req_game in pending_requests:
                    if edit_distance_ratio(game_name.lower(), req_game.lower()) > 0.8:
                        fulfilled_requests.append((req_id, user_id, req_game))

                return fulfilled_requests

        except Exception as e:
            logger.error(f"Error checking pending requests: {e}")
            return []

    async def process_request(self, ctx_or_interaction, platform_name, game, details, selected_game, message):
        """Compatibility wrapper for callers that do not have platform metadata."""
        platform_display_name, mapping_id, platform_exists = await self.get_platform_request_context(
            platform_name
        )
        return await self.process_request_with_platform(
            ctx_or_interaction,
            platform_display_name,
            game,
            details,
            selected_game,
            message,
            mapping_id,
            platform_exists
        )
    
    async def process_request_with_platform(self, ctx_or_interaction, platform_display_name,
                                       game, details, selected_game, message,
                                       mapping_id, platform_exists, send_response: bool = True):
        """Process and save the request with platform mapping"""
        respond = None
        try:
            author, author_name, respond = responder_for(ctx_or_interaction)

            igdb_id = igdb_name = None
            if selected_game and selected_game.get('id'):
                igdb_id = selected_game['id']
                igdb_name = selected_game.get('name')

            # A platform RomM does not have yet cannot have duplicates worth
            # merging into, so the check only runs when the platform exists.
            if platform_exists and await self._merge_into_existing_request(
                respond,
                author=author,
                author_name=author_name,
                game=game,
                igdb_id=igdb_id,
                platform_display_name=platform_display_name,
                selected_game=selected_game,
                send_response=send_response,
            ):
                return

            pending_count = await self.repo.count_pending_for_user(author.id)
            if pending_count >= MAX_PENDING_REQUESTS:
                if send_response:
                    await respond(content=(
                        f"❌ You already have {MAX_PENDING_REQUESTS} pending requests. "
                        "Please wait for them to be fulfilled or cancel some."
                    ))
                return

            # The user's own words are kept separately: the details column also
            # carries the IGDB metadata block, which should not be echoed back.
            user_details = details
            if selected_game:
                igdb_details = format_igdb_details(selected_game)
                details = f"{details}\n\n{igdb_details}" if details else igdb_details

            request_id = await self._store_and_sync_request(
                author=author,
                author_name=author_name,
                platform_display_name=platform_display_name,
                game=game,
                details=details,
                user_details=user_details,
                igdb_id=igdb_id,
                igdb_name=igdb_name,
                mapping_id=mapping_id,
            )

            await self._confirm_new_request(
                respond,
                request_id=request_id,
                author_name=author_name,
                game=game,
                platform_display_name=platform_display_name,
                platform_exists=platform_exists,
                selected_game=selected_game,
                user_details=user_details,
                message=message,
                send_response=send_response,
            )

            return request_id

        except Exception as e:
            logger.error(f"Error processing request: {e}")
            if send_response:
                await self._report_request_failure(ctx_or_interaction, respond)
            return None

    def _platform_display(self, platform_display_name: str, with_emoji: bool = True) -> str:
        """Platform name, decorated with its emoji when the Search cog is loaded."""
        search_cog = self.bot.get_cog('Search')
        if search_cog and with_emoji:
            return search_cog.get_platform_with_emoji(platform_display_name)
        return platform_display_name

    async def _merge_into_existing_request(
        self, respond, *, author, author_name, game, igdb_id,
        platform_display_name, selected_game, send_response,
    ) -> bool:
        """Fold this request into an existing one if the game is already asked for.

        Returns True when the request was handled here and the caller should
        stop: either the user already has this request open, or they have just
        been subscribed to someone else's.
        """
        candidates = await self.repo.list_duplicate_candidates(platform_display_name, igdb_id)
        duplicate = find_duplicate_request(
            candidates, game_name=game, igdb_id=igdb_id, user_id=author.id
        )
        if not duplicate:
            return False

        already_waiting = duplicate.is_own_request or await self.repo.is_subscribed(
            duplicate.request_id, author.id
        )

        if already_waiting:
            if send_response:
                await respond(embed=build_already_requested_embed(
                    game=game,
                    platform_display=self._platform_display(platform_display_name),
                    request_id=duplicate.request_id,
                    selected_game=selected_game,
                ))
            return True

        await self.repo.add_subscriber(duplicate.request_id, author.id, author_name)
        subscriber_count = await self.repo.count_subscribers(duplicate.request_id)

        if send_response:
            await respond(embed=build_subscribed_embed(
                game=game,
                platform_display=self._platform_display(platform_display_name),
                request_id=duplicate.request_id,
                requester_name=duplicate.requester_name,
                subscriber_count=subscriber_count,
                selected_game=selected_game,
            ))
        return True

    async def _store_and_sync_request(
        self, *, author, author_name, platform_display_name, game, details,
        user_details, igdb_id, igdb_name, mapping_id,
    ) -> int:
        """Save the request, then mirror it to ggrequestz if that is configured.

        The local row is written first so ggrequestz can be handed the Discord
        request id; a failure to mirror leaves the local request intact.
        """
        request_id = await self.repo.create(
            user_id=author.id,
            username=author_name,
            platform=platform_display_name,
            game_name=game,
            details=details,
            igdb_id=igdb_id,
            platform_mapping_id=mapping_id,
            igdb_game_name=igdb_name,
        )

        ggr_request_id = None
        if self.ggr and self.ggr.enabled:
            try:
                result = await self.ggr.create_request(
                    game_name=igdb_name if igdb_name else game,
                    platform=platform_display_name,
                    user_id=author.id,
                    username=author_name,
                    igdb_id=igdb_id,
                    details=user_details,
                    discord_request_id=request_id
                )

                if result.get('success'):
                    ggr_request_id = result.get('request_id')
                    logger.info(f"Request created in GGRequestz: ID {ggr_request_id}")
                    await self.repo.set_ggr_request_id(request_id, ggr_request_id)
                else:
                    logger.warning(f"GGRequestz creation failed: {result.get('error')}")
            except Exception as e:
                logger.error(f"GGRequestz error: {e}")

        logger.info(
            f"Request created - Discord: {author_name} (ID: {author.id}) | Game: '{game}' | "
            f"Platform: {platform_display_name} | Local ID: #{request_id} | "
            f"GGR ID: {ggr_request_id or 'N/A'}"
        )
        return request_id

    async def _confirm_new_request(
        self, respond, *, request_id, author_name, game, platform_display_name,
        platform_exists, selected_game, user_details, message, send_response,
    ) -> None:
        """Show the request back to the user.

        A request that came from an IGDB pick edits the message that offered
        the choice; one typed by hand gets a fresh confirmation embed.
        """
        if message and selected_game:
            view = GameSelectView(
                self.bot, matches=[selected_game], platform_name=platform_display_name
            )
            embed = view.create_game_embed(selected_game)
            embed.set_footer(text=f"Request #{request_id} submitted by {author_name}")

            if not platform_exists:
                embed.add_field(
                    name="⚠️ Platform Status",
                    value=PLATFORM_MISSING_NOTE,
                    inline=False
                )

            await message.edit(embed=embed)
            return

        if send_response:
            await respond(embed=build_request_submitted_embed(
                game=game,
                platform_display=self._platform_display(
                    platform_display_name, with_emoji=platform_exists
                ),
                platform_exists=platform_exists,
                request_id=request_id,
                author_name=author_name,
                user_details=user_details,
            ))

    async def _report_request_failure(self, ctx_or_interaction, respond) -> None:
        """Best-effort apology; the original failure is already logged."""
        text = "❌ An error occurred while processing the request."
        try:
            if respond is not None:
                await respond(content=text)
            elif hasattr(ctx_or_interaction, 'user'):
                await ctx_or_interaction.followup.send(text)
            else:
                await ctx_or_interaction.respond(text)
        except Exception as error_e:
            logger.error(f"Could not send error message to user: {error_e}")
        
    async def get_request_igdb_data(self, request_id: int) -> Optional[Dict]:
        """Retrieve IGDB data for a request if IGDB ID was stored"""
        try:
            async with self.db.get_connection() as db:
                cursor = await db.execute(
                    "SELECT igdb_id, game_name, platform FROM requests WHERE id = ?",
                    (request_id,)
                )
                result = await cursor.fetchone()
                
                if result and result['igdb_id']:
                    igdb_id = result['igdb_id']
                    game_name = result['game_name']
                    platform_name = result['platform']
                    
                    # You can now use this IGDB ID for direct API calls
                    # For example, fetch fresh data from IGDB by ID
                    if self.igdb_enabled:
                        # This would require adding a method to IGDBClient to fetch by ID
                        # return await self.igdb.get_game_by_id(igdb_id)
                        return {"igdb_id": igdb_id, "game_name": game_name, "platform": platform_name}
                
                return None
                
        except Exception as e:
            logger.error(f"Error retrieving IGDB data for request: {e}")
            return None
    
    async def continue_request_flow(self, ctx, platform_display_name, game, details, igdb_matches):
        """Continue with the request flow when user wants different version"""
        
        if igdb_matches:
            # Show IGDB selection
            select_view = GameSelectView(self.bot, igdb_matches, platform_display_name)
            initial_embed = select_view.create_game_embed(igdb_matches[0])
            select_view.message = await ctx.followup.send(
                "Please select the correct game from the list below:",
                embed=initial_embed,
                view=select_view
            )
            
            await select_view.wait()
            
            if not select_view.selected_game:
                # Timeout
                return
            elif select_view.selected_game == "manual":
                selected_game = None
            else:
                selected_game = select_view.selected_game
                # Process request with IGDB data including ID
                platform_name, mapping_id, platform_exists = await self.get_platform_request_context(
                    platform_display_name
                )
                await self.process_request_with_platform(
                    ctx,
                    platform_name,
                    game,
                    details,
                    selected_game,
                    select_view.message,
                    mapping_id,
                    platform_exists
                )
                return
        
        # Process manual request (no IGDB ID)
        platform_name, mapping_id, platform_exists = await self.get_platform_request_context(
            platform_display_name
        )
        await self.process_request_with_platform(
            ctx,
            platform_name,
            game,
            details,
            None,
            None,
            mapping_id,
            platform_exists
        )
    
    async def _sync_statuses_from_ggrequestz(self, user_id: Optional[int] = None):
        """Sync request statuses from ggrequestz to Discord database"""
        if not self.ggr or not self.ggr.enabled:
            return
        
        try:
            async with self.db.get_connection() as db:
                # Build query based on whether we're syncing for a specific user or all
                if user_id:
                    cursor = await db.execute(
                        """
                        SELECT id, ggr_request_id, status, user_id, game_name 
                        FROM requests 
                        WHERE user_id = ?
                        AND ggr_request_id IS NOT NULL 
                        AND status IN ('pending', 'approved')
                        """,
                        (user_id,)
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT id, ggr_request_id, status, user_id, game_name 
                        FROM requests 
                        WHERE ggr_request_id IS NOT NULL 
                        AND status IN ('pending', 'approved')
                        """
                    )
                
                discord_requests = await cursor.fetchall()
                
                if not discord_requests:
                    return
                
                logger.debug(f"Syncing {len(discord_requests)} requests from ggrequestz")
                
                for discord_req in discord_requests:
                    discord_id, ggr_id, discord_status, req_user_id, game_name = discord_req
                    
                    # Get current status from ggrequestz
                    ggr_request = await self.ggr.get_request_by_id(ggr_id)
                    
                    if not ggr_request:
                        continue
                    
                    ggr_status = ggr_request.get('status')
                    
                    # Map ggrequestz status to Discord status.
                    # Local canonical statuses are: pending, fulfilled, reject, cancelled.
                    # 'approved' has no local equivalent, so it is intentionally omitted
                    # (status_mapping.get returns None -> no local update).
                    status_mapping = {
                        'pending': 'pending',
                        'fulfilled': 'fulfilled',
                        'rejected': 'reject',
                        'cancelled': 'cancelled'
                    }
                    
                    mapped_status = status_mapping.get(ggr_status)
                    
                    # If status changed, update Discord database
                    if mapped_status and mapped_status != discord_status:
                        logger.info(f"Syncing status for request #{discord_id}: {discord_status} → {mapped_status}")
                        
                        notes = ggr_request.get('admin_notes', '')
                        update_note = f"Synced from ggrequestz: {notes}" if notes else "Synced from ggrequestz"
                        
                        await db.execute(
                            """
                            UPDATE requests 
                            SET status = ?, 
                                notes = ?,
                                updated_at = CURRENT_TIMESTAMP 
                            WHERE id = ?
                            """,
                            (mapped_status, update_note, discord_id)
                        )
                        await db.commit()
                        
                        logger.info(f"✅ Synced status for request #{discord_id} from ggrequestz")
        
        except Exception as e:
            logger.error(f"Error syncing statuses from ggrequestz: {e}", exc_info=True)
    
    @discord.slash_command(name="request", description="Submit a ROM request")
    async def request(
        self,
        ctx: discord.ApplicationContext,
        platform: discord.Option(
            str,
            "Platform for the requested game (existing or new)",
            required=True,
            autocomplete=platform_autocomplete_all  # Use the new autocomplete
        ),
        game: discord.Option(str, "Name of the game", required=True),
        details: discord.Option(str, "Additional details (version, region, etc.)", required=False)
    ):
        """Submit a request for a ROM with platform validation"""
        await ctx.defer()

        try:
            # Clean platform name (remove [NEW] prefix if present)
            platform_clean = platform.replace("[NEW] ", "")
            # Check if platform exists in our mappings
            async with self.db.get_connection() as db:
                cursor = await db.execute('''
                    SELECT id, in_romm, romm_id, folder_name, igdb_slug, moby_slug
                    FROM platform_mappings
                    WHERE display_name = ?
                ''', (platform_clean,))
                
                platform_mapping = await cursor.fetchone()
                
                if not platform_mapping:
                    # Platform not in our master list - ask for confirmation
                    await ctx.respond(
                        f"⚠️ '{platform}' is not in our platform database. "
                        "Please use a platform from the autocomplete list or contact an admin to add a new platform.",
                        ephemeral=True
                    )
                    return
                
                mapping_id, in_romm, romm_id, folder_name, igdb_slug, moby_slug = platform_mapping
                platform_display_name = platform_clean
                
                # If platform exists in Romm, check for existing games
                if in_romm and romm_id:
                    # Get the actual Romm platform data
                    raw_platforms = await self.bot.fetch_api_endpoint('platforms')
                    if raw_platforms:
                        for p in raw_platforms:
                            if p.get('id') == romm_id:
                                platform_display_name = self.bot.get_platform_display_name(p)
                                break
                    
                    # Check if game exists in current collection
                    exists, matches = await self.check_if_game_exists(platform_display_name, game)
                    
                    if exists:
                        # Game exists in collection - but also fetch IGDB matches
                        search_cog = self.bot.get_cog('Search')
                        platform_with_emoji = search_cog.get_platform_with_emoji(platform_display_name) if search_cog else platform_display_name
                        
                        # Fetch IGDB matches regardless of existing games
                        igdb_matches = []
                        if self.igdb_enabled:
                            try:
                                igdb_platform_slug = None
                                if platform_mapping:
                                    igdb_platform_slug = platform_mapping['igdb_slug']
                                igdb_matches = await self.igdb.search_game(game, igdb_platform_slug)
                            except Exception as e:
                                logger.error(f"Error fetching IGDB data: {e}")
                        
                        # Create combined view showing both existing games AND IGDB options
                        view = ExistingGameWithIGDBView(
                            self.bot, 
                            matches,           # Existing games in collection
                            igdb_matches,      # IGDB search results
                            platform_display_name, 
                            game, 
                            ctx.author.id
                        )
                        
                        # Check if any IGDB games remain after filtering
                        has_other_games = bool(view.filtered_igdb_matches)
                        
                        embed = discord.Embed(
                            title="Games Found in Collection",
                            description=f"Found {len(matches)} game(s) matching '{game}' that are already available:",
                            color=discord.Color.blue()
                        )
                        
                        # Show first few existing games
                        for i, rom in enumerate(matches[:3]):
                            embed.add_field(
                                name=f"✅ {rom.get('name', 'Unknown')}",
                                value=f"Available now - {rom.get('fs_name', 'Unknown')}",
                                inline=False
                            )
                        
                        if len(matches) > 3:
                            embed.add_field(
                                name="...",
                                value=f"And {len(matches) - 3} more available",
                                inline=False
                            )
                        
                        # Update instructions based on what's available
                        instructions = ["• **Select an existing game** from the dropdown to download it"]
                        
                        if has_other_games:
                            instructions.append(f"• **Request a different game** - Found {len(view.filtered_igdb_matches)} other game(s) on IGDB")
                        
                        instructions.append("• Click **Request Different Version** for ROM hacks, patches, or specific versions")
                        
                        embed.add_field(
                            name="What would you like to do?",
                            value="\n".join(instructions),
                            inline=False
                        )
                        
                        message = await ctx.respond(embed=embed, view=view)
                        
                        if isinstance(message, discord.Interaction):
                            view.message = await message.original_response()
                        else:
                            view.message = message
                        
                        await view.wait()
                        return
                
                # Search IGDB for game metadata if enabled
                igdb_matches = []
                if self.igdb_enabled:
                    try:
                        # Get IGDB slug from mapping to use for platform filtering
                        igdb_platform_slug = None
                        if platform_mapping:
                            igdb_platform_slug = platform_mapping['igdb_slug']
                        
                        # Pass platform slug for filtering if available
                        igdb_matches = await self.igdb.search_game(game, igdb_platform_slug)
                    except Exception as e:
                        logger.error(f"Error fetching IGDB data: {e}")
                
                # Game doesn't exist OR user wants different version - show IGDB selection
                if igdb_matches:
                    select_view = GameSelectView(self.bot, igdb_matches, platform_display_name)
                    initial_embed = select_view.create_game_embed(igdb_matches[0])
                    
                    # Adjust message based on context
                    intro_text = "Please select the correct game from the list below:"
                    
                    select_view.message = await ctx.followup.send(
                        intro_text,
                        embed=initial_embed,
                        view=select_view
                    )
                    
                    await select_view.wait()
                    
                    if not select_view.selected_game:
                        # Timeout
                        timeout_view = discord.ui.View()
                        timeout_button = discord.ui.Button(
                            label="Selection Timed Out",
                            style=discord.ButtonStyle.secondary,
                            disabled=True
                        )
                        timeout_view.add_item(timeout_button)
                        await select_view.message.edit(view=timeout_view)
                        return
                    elif select_view.selected_game == "manual":
                        selected_game = None
                    else:
                        selected_game = select_view.selected_game
                        await self.process_request_with_platform(
                            ctx, 
                            platform_display_name, 
                            game, 
                            details, 
                            selected_game, 
                            select_view.message,
                            mapping_id,
                            in_romm
                        )
                        return
                
                # No IGDB matches or manual entry selected - process without IGDB data
                await self.process_request_with_platform(
                    ctx, 
                    platform_display_name, 
                    game, 
                    details, 
                    None, 
                    None,
                    mapping_id,
                    in_romm
                )

        except Exception as e:
            logger.error(f"Error submitting request: {e}")
            await ctx.respond("❌ An error occurred while submitting your request.")
    
    @discord.slash_command(name="my_requests", description="View and manage your ROM requests")
    async def my_requests(
        self, 
        ctx: discord.ApplicationContext,
        show_pending_only: discord.Option(
            bool,
            "Show only pending requests instead of all",
            required=False,
            default=False
        )
    ):
        """View and manage your submitted requests with interactive controls"""
        await ctx.defer(ephemeral=True)

        try:
            # Sync statuses from ggrequestz first
            await self._sync_statuses_from_ggrequestz(ctx.author.id)
            
            async with self.db.get_connection() as db:
                # Fetch requests based on show_pending_only parameter
                if show_pending_only:
                    cursor = await db.execute(
                        f"SELECT {REQUEST_COLUMNS} FROM requests WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC",
                        (ctx.author.id,)
                    )
                    viewing_mode = "pending"
                else:
                    cursor = await db.execute(
                        f"SELECT {REQUEST_COLUMNS} FROM requests WHERE user_id = ? ORDER BY created_at DESC",
                        (ctx.author.id,)
                    )
                    viewing_mode = "all"
                
                requests = await cursor.fetchall()

                if not requests:
                    if show_pending_only:
                        embed = discord.Embed(
                            title="No Pending Requests",
                            description="You don't have any pending requests.\n\nUse `/my_requests` to view all your requests including fulfilled and cancelled ones.",
                            color=discord.Color.green()
                        )
                        await ctx.respond(embed=embed, ephemeral=True)
                    else:
                        embed = discord.Embed(
                            title="No Requests",
                            description="You haven't made any requests yet.\n\nUse `/request` to submit a ROM request!",
                            color=discord.Color.light_grey()
                        )
                        embed.set_footer(text="Start by requesting a game you'd like to see added")
                        await ctx.respond(embed=embed, ephemeral=True)
                    return

                # PRE-FETCH platform status for all requests
                platform_status = {}
                for req in requests:
                    platform_mapping_id = req['platform_mapping_id']
                    if platform_mapping_id and platform_mapping_id not in platform_status:
                        cursor = await db.execute(
                            "SELECT in_romm FROM platform_mappings WHERE id = ?",
                            (platform_mapping_id,)
                        )
                        result = await cursor.fetchone()
                        platform_status[platform_mapping_id] = bool(result['in_romm']) if result else False
                        
                    # Also check by name for fallback
                    if not platform_mapping_id or platform_mapping_id not in platform_status:
                        platform_name = req['platform']
                        if f"name:{platform_name}" not in platform_status:
                            cursor = await db.execute(
                                "SELECT in_romm FROM platform_mappings WHERE LOWER(display_name) = LOWER(?)",
                                (platform_name,)
                            )
                            result = await cursor.fetchone()
                            platform_status[f"name:{platform_name}"] = bool(result['in_romm']) if result else False
                
                # Count statuses for summary
                status_counts = {
                    'pending': 0,
                    'fulfilled': 0,
                    'cancelled': 0,
                    'reject': 0
                }
                for req in requests:
                    status = req['status']
                    if status in status_counts:
                        status_counts[status] += 1

                # Create paginated view
                view = UserRequestsView(self.bot, requests, ctx.author.id, self.bot.db)
                view.platform_status = platform_status
                embed = view.create_request_embed(requests[0])
                
                # Build status summary
                status_parts = []
                if status_counts['pending'] > 0:
                    status_parts.append(f"⏳ {status_counts['pending']} pending")
                if status_counts['fulfilled'] > 0:
                    status_parts.append(f"✅ {status_counts['fulfilled']} fulfilled")
                if status_counts['cancelled'] > 0:
                    status_parts.append(f"🚫 {status_counts['cancelled']} cancelled")
                if status_counts['reject'] > 0:
                    status_parts.append(f"❌ {status_counts['reject']} rejected")
                
                status_summary = " | ".join(status_parts)
                
                # Add viewing mode indicator to the message
                mode_text = "⏳ **Viewing: Pending Requests Only**" if show_pending_only else "📋 **Viewing: All Your Requests**"
                hint_text = "\n*Use `/my_requests show_pending_only:True` to see only pending requests*" if not show_pending_only else "\n*Use `/my_requests` to see all requests*"
                
                message = await ctx.respond(
                    content=f"{mode_text}\n📊 **Summary:** {status_summary}",
                    embed=embed, 
                    view=view,
                    ephemeral=True
                )
                
                # Store message reference for editing
                if isinstance(message, discord.Interaction):
                    view.message = await message.original_response()
                else:
                    view.message = message

        except Exception as e:
            logger.error(f"Error fetching requests: {e}")
            await ctx.respond("❌ An error occurred while fetching your requests.", ephemeral=True)

    @discord.slash_command(name="request_admin", description="Interface for managing ROM requests (admin only)")
    @is_admin()
    async def request_admin(
        self,
        ctx: discord.ApplicationContext,
        show_all: discord.Option(
            bool,
            "Show all requests instead of just pending ones",
            required=False,
            default=False
        )
    ):
        """Admin interface for managing requests - shows pending by default"""
        await ctx.defer(ephemeral=True)

        try:
            # Sync statuses from ggrequestz first
            await self._sync_statuses_from_ggrequestz()
            
            async with self.db.get_connection() as db:
                # Fetch requests based on show_all parameter
                if show_all:
                    cursor = await db.execute(
                        f"SELECT {REQUEST_COLUMNS} FROM requests ORDER BY created_at DESC"
                    )
                    viewing_mode = "all"
                else:
                    cursor = await db.execute(
                        f"SELECT {REQUEST_COLUMNS} FROM requests WHERE status = 'pending' ORDER BY created_at ASC"
                    )
                    viewing_mode = "pending"
                
                requests = await cursor.fetchall()

                if not requests:
                    if show_all:
                        await ctx.respond("📭 No requests found in the system.", ephemeral=True)
                    else:
                        embed = discord.Embed(
                            title="No Pending Requests",
                            description="There are currently no pending requests.\n\nUse `/request_admin show_all:True` to view all requests including fulfilled and rejected ones.",
                            color=discord.Color.green()
                        )
                        embed.set_footer(text="All requests have been processed!")
                        await ctx.respond(embed=embed)
                    return

                # PRE-FETCH platform status for all requests
                platform_status = {}
                for req in requests:
                    platform_mapping_id = req['platform_mapping_id']
                    if platform_mapping_id and platform_mapping_id not in platform_status:
                        cursor = await db.execute(
                            "SELECT in_romm FROM platform_mappings WHERE id = ?",
                            (platform_mapping_id,)
                        )
                        result = await cursor.fetchone()
                        platform_status[platform_mapping_id] = bool(result['in_romm']) if result else False
                        
                    # Also check by name for fallback
                    if not platform_mapping_id or platform_mapping_id not in platform_status:
                        platform_name = req['platform']
                        if f"name:{platform_name}" not in platform_status:
                            cursor = await db.execute(
                                "SELECT in_romm FROM platform_mappings WHERE LOWER(display_name) = LOWER(?)",
                                (platform_name,)
                            )
                            result = await cursor.fetchone()
                            platform_status[f"name:{platform_name}"] = bool(result['in_romm']) if result else False
                
                # Create paginated view
                view = RequestAdminView(self.bot, requests, ctx.author.id, self.bot.db)
                view.platform_status = platform_status
                
                # Fetch user avatar for the first request
                user_avatar_url = None
                try:
                    user = self.bot.get_user(requests[0]['user_id'])
                    if not user:
                        user = await self.bot.fetch_user(requests[0]['user_id'])
                    if user and user.avatar:
                        user_avatar_url = user.avatar.url
                    elif user:
                        user_avatar_url = user.default_avatar.url
                except (discord.NotFound, discord.HTTPException):
                    pass  # User not found or API error

                embed = view.create_request_embed(requests[0], user_avatar_url)  # Pass avatar URL
                
                # Add viewing mode indicator to the message
                mode_text = "📋 **Viewing: All Requests**" if show_all else "⏳ **Viewing: Pending Requests Only**"
                hint_text = "\n *Use `/request_admin show_all:True` to see all requests*" if not show_all else ""
                
                message = await ctx.respond(
                    content=f"{mode_text}",
                    embed=embed, 
                    view=view
                )
                
                # Store message reference for editing
                if isinstance(message, discord.Interaction):
                    view.message = await message.original_response()
                else:
                    view.message = message

        except Exception as e:
            logger.error(f"Error in request admin command: {e}")
            await ctx.respond("❌ An error occurred while loading the requests interface.", ephemeral=True)
