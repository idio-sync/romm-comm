"""HTTP client for the RomM API.

Everything the bot needs to talk to RomM: the aiohttp session, OAuth and
client-token auth, CSRF, the response cache, and the two request helpers.

This used to live on RommBot, which meant none of it could be exercised
without constructing a Discord bot. RommClient takes a Config and nothing
else, so it can be built in a test.

Failure is reported the way callers already expect - None for a failed
request, False for a failed token grant. The exceptions below are raised and
handled inside this module, where they let the retry logic tell an auth
failure apart from a transport failure. They are exported so callers can
catch them if they ever want that distinction; nothing is forced to.
"""

import asyncio
import json
import logging
import re
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp

logger = logging.getLogger('romm_bot')


_VERSION_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)')


def parse_romm_version(raw: Any) -> Optional[Tuple[int, int, int]]:
    """Read a RomM version string such as "5.4.1" or "5.5.0-beta.1".

    Returns None for anything else, notably "development".
    """
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.match(raw.strip())
    if not match:
        return None
    return int(match[1]), int(match[2]), int(match[3])


class _NotFound:
    """RomM's own 404: what was asked about does not exist, or is hidden from us.

    Distinct from None, which means the request failed and says nothing about
    the thing asked about. Only returned by calls that opt in with
    `not_found=NOT_FOUND`; every other call still sees None for a 404.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "NOT_FOUND"


NOT_FOUND = _NotFound()


class RommApiError(Exception):
    """A request to RomM did not succeed."""


class RommAuthError(RommApiError):
    """RomM rejected our credentials.

    Distinct from a transport failure because it is not worth retrying: a
    revoked client token or bad password fails identically every time.
    """


class APICache:
    """Simple time-based cache for API responses."""
    def __init__(self, ttl_seconds: int = 3600):
        self.cache: Dict[str, Dict[str, Any]] = {}
        self.ttl = ttl_seconds
        self.last_update: Dict[str, float] = {}

    def is_fresh(self, endpoint: str) -> bool:
        """Check if cached data is still fresh."""
        return (endpoint in self.last_update
                and (time.time() - self.last_update[endpoint]) < self.ttl)

    def get(self, endpoint: str) -> Optional[Dict[str, Any]]:
        """Get cached data if fresh."""
        return self.cache.get(endpoint) if self.is_fresh(endpoint) else None

    def set(self, endpoint: str, data: Dict[str, Any]):
        """Cache data with current timestamp."""
        self.cache[endpoint] = data
        self.last_update[endpoint] = time.time()


class RateLimit:
    """Rate limiter for API calls."""
    def __init__(self, calls_per_minute: int = 30):
        self.calls_per_minute = calls_per_minute
        self.calls = []
        self.lock = asyncio.Lock()

    async def acquire(self):
        """Acquire rate limit slot."""
        async with self.lock:
            now = time.time()
            self.calls = [t for t in self.calls if now - t < 60]

            if len(self.calls) >= self.calls_per_minute:
                sleep_time = 60 - (now - self.calls[0])
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)

            self.calls.append(now)


class RommClient:
    """Authenticated access to a RomM server."""

    def __init__(self, config):
        self.config = config
        self.cache = APICache(config.CACHE_TTL)
        self.rate_limiter = RateLimit()

        self.session: Optional[aiohttp.ClientSession] = None
        self._session_lock = asyncio.Lock()

        # OAuth token management
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiry: float = 0
        self.token_lock = asyncio.Lock()

        # CSRF token management
        self.csrf_token: Optional[str] = None
        self.csrf_cookie: Optional[str] = None
        self.csrf_expiry: float = 0

    # -------------------------------------------------------------- transport

    async def ensure_session(self) -> aiohttp.ClientSession:
        """Ensure an active session exists with optimized settings."""
        async with self._session_lock:
            if self.session is None or self.session.closed:
                # Configure connector with keepalive
                connector = aiohttp.TCPConnector(
                    limit=10,                      # Max connections
                    limit_per_host=5,              # Max per host
                    ttl_dns_cache=300,             # DNS cache 5 min
                    force_close=False,             # Enable keepalive
                    enable_cleanup_closed=True,
                    keepalive_timeout=75           # Keep connections alive
                )

                self.session = aiohttp.ClientSession(
                    connector=connector,
                    timeout=aiohttp.ClientTimeout(
                        total=self.config.API_TIMEOUT,      # Overall timeout
                        connect=5,                           # Connection timeout
                        sock_read=self.config.API_TIMEOUT   # Read timeout
                    ),
                    headers={
                        "User-Agent": "RommBot/1.0",
                        "Accept": "application/json",
                        "Connection": "keep-alive"          # Explicit keepalive
                    }
                )
            return self.session

    async def close(self) -> None:
        """Close the HTTP session, if one is open."""
        if self.session and not self.session.closed:
            await self.session.close()

    # ------------------------------------------------------------------- auth

    async def get_oauth_token(self) -> bool:
        """Get initial OAuth token using username/password."""
        try:
            session = await self.ensure_session()

            # Token endpoint keeps the /api/ prefix
            token_url = f"{self.config.API_BASE_URL}/api/token"
            logger.debug(f"Requesting token from: {token_url}")

            # Prepare form data for OAuth2 password grant
            data = aiohttp.FormData()
            data.add_field('grant_type', 'password')
            data.add_field('username', self.config.USER)
            data.add_field('password', self.config.PASS)
            data.add_field('scope', 'roms.read platforms.read firmware.read users.read users.write me.write assets.read')

            # Simple headers for OAuth token request
            headers = {
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded"
            }

            async with session.post(token_url, data=data, headers=headers) as response:
                response_text = await response.text()
                logger.debug(f"Token response status: {response.status}")
                logger.debug(f"Token response content-type: {response.headers.get('content-type')}")

                if response.status != 200:
                    raise RommAuthError(
                        f"Token request rejected with status {response.status}: {response_text}"
                    )

                try:
                    token_data = await response.json()
                except Exception as e:
                    raise RommApiError(f"Could not parse token response: {response_text}") from e

                self.access_token = token_data.get('access_token')
                self.refresh_token = token_data.get('refresh_token')
                # Store expiry time (subtract 60 seconds for safety margin)
                self.token_expiry = time.time() + token_data.get('expires', 900) - 60

                if not self.access_token:
                    raise RommAuthError("OAuth response missing access_token")

                logger.debug("Successfully obtained OAuth tokens")
                return True

        except RommAuthError as e:
            logger.error(f"RomM rejected our credentials: {e}")
            return False
        except RommApiError as e:
            logger.error(f"Failed to obtain OAuth token: {e}")
            return False
        except Exception as e:
            logger.error(f"Error getting OAuth token: {e}", exc_info=True)
            return False

    async def refresh_oauth_token(self) -> bool:
        """Refresh the OAuth token using the refresh token."""
        if not self.refresh_token:
            logger.debug("No refresh token available, getting new token")
            return await self.get_oauth_token()

        try:
            session = await self.ensure_session()
            token_url = f"{self.config.API_BASE_URL}/api/token"

            data = aiohttp.FormData()
            data.add_field('grant_type', 'refresh_token')
            data.add_field('refresh_token', self.refresh_token)

            async with session.post(token_url, data=data) as response:
                if response.status == 200:
                    try:
                        token_data = await response.json()
                    except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                        logger.error(f"Invalid JSON response when refreshing token: {e}")
                        return await self.get_oauth_token()
                    self.access_token = token_data.get('access_token')
                    # Refresh token may or may not be returned
                    if 'refresh_token' in token_data:
                        self.refresh_token = token_data.get('refresh_token')
                    self.token_expiry = time.time() + token_data.get('expires', 900) - 60
                    logger.debug("Successfully refreshed OAuth token")
                    return True
                else:
                    logger.warning(f"Failed to refresh token, status: {response.status}")
                    # If refresh fails, try getting a new token
                    return await self.get_oauth_token()

        except Exception as e:
            logger.error(f"Error refreshing OAuth token: {e}")
            return await self.get_oauth_token()

    async def ensure_valid_token(self) -> bool:
        """Ensure we have a valid OAuth token, refreshing if necessary."""
        async with self.token_lock:
            # Client API token is a static credential: no OAuth grant or refresh needed.
            if self.config.ROMM_CLIENT_TOKEN:
                self.access_token = self.config.ROMM_CLIENT_TOKEN
                return True
            # Check if token is expired or missing
            if not self.access_token or time.time() >= self.token_expiry:
                logger.debug("Token expired or missing, refreshing...")
                # Only try refresh token if we have one and access token expired recently (within 1 hour)
                # This prevents using stale refresh tokens and aligns with typical OAuth best practices
                if self.refresh_token and time.time() < self.token_expiry + 3600:  # 1 hour grace period
                    return await self.refresh_oauth_token()
                else:
                    # Token expired too long ago or no refresh token - get fresh credentials
                    return await self.get_oauth_token()
            return True

    async def get_csrf_token(self) -> Optional[str]:
        """Get CSRF token from the heartbeat endpoint."""
        try:
            session = await self.ensure_session()
            heartbeat_url = f"{self.config.API_BASE_URL}/api/heartbeat"

            async with session.get(heartbeat_url) as response:
                if response.status != 200:
                    logger.error(f"Failed to get heartbeat. Status: {response.status}")
                    return None

                # Extract CSRF token from Set-Cookie header
                set_cookie = response.headers.get('Set-Cookie')
                if not set_cookie:
                    logger.debug("No Set-Cookie header in heartbeat response")
                    return None

                # Parse the CSRF token from cookie using regex for safety
                csrf_match = re.search(r'romm_csrftoken=([^;]+)', set_cookie)
                if csrf_match:
                    csrf_token = csrf_match.group(1)
                    self.csrf_token = csrf_token
                    self.csrf_cookie = f"romm_csrftoken={csrf_token}"
                    # CSRF tokens typically last for the session
                    self.csrf_expiry = time.time() + 3600  # 1 hour expiry
                    logger.debug(f"Extracted CSRF token: {csrf_token[:10]}...")
                    return csrf_token
                else:
                    logger.debug("CSRF token not found in Set-Cookie header")
                    return None

        except Exception as e:
            logger.error(f"Error getting CSRF token: {e}")
            return None

    async def ensure_csrf_token(self) -> Optional[str]:
        """Ensure we have a valid CSRF token."""
        if not self.csrf_token or time.time() >= self.csrf_expiry:
            logger.debug("CSRF token expired or missing, fetching new one")
            return await self.get_csrf_token()
        return self.csrf_token

    # --------------------------------------------------------------- requests

    async def make_authenticated_request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None,
        form_data: Optional[aiohttp.FormData] = None,
        require_csrf: bool = False,
        not_found: Any = None,
    ) -> Any:
        """Make an authenticated API request with proper headers.

        Returns the decoded body, or None on failure. A caller that needs to
        tell "RomM says this does not exist" apart from "the request failed"
        passes a marker as `not_found` (normally NOT_FOUND), which is returned
        instead of None for a 404 that RomM itself produced. See _read_response.

        require_csrf is a no-op against current RomM. Its CSRFMiddleware skips
        validation outright when the Authorization scheme is bearer or basic,
        and every request made here carries a bearer token, so the extra
        round-trip to fetch a CSRF token buys nothing. The flag is kept, and
        left set where it already was, in case an older or differently
        configured server does enforce it. New call sites do not need it,
        which is why users/invite-link omits it.
        """
        try:
            if not await self.ensure_valid_token():
                logger.error("Failed to obtain valid OAuth token")
                return None

            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/{endpoint}"

            headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

            if require_csrf:
                csrf_token = await self.ensure_csrf_token()
                if csrf_token:
                    headers["X-CSRFToken"] = csrf_token
                    headers["Cookie"] = self.csrf_cookie

            request_kwargs = {"headers": headers}
            if data:
                request_kwargs["json"] = data
            if params:
                request_kwargs["params"] = params
            if form_data:
                request_kwargs["data"] = form_data

            async with session.request(method, url, **request_kwargs) as response:
                logger.debug(f"API Response: {method} {url} -> Status {response.status}")

                if response.status == 401:
                    # A client API token can't be refreshed; a 401 means it is invalid/revoked.
                    if self.config.ROMM_CLIENT_TOKEN:
                        logger.error(
                            "Got 401 using ROMM_CLIENT_TOKEN - the client token may be "
                            "invalid, expired, or revoked. Verify it in RomM."
                        )
                        return None
                    logger.debug("Got 401, attempting to refresh token")
                    if await self.refresh_oauth_token():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        async with session.request(method, url, **request_kwargs) as retry_response:
                            return await self._read_response(retry_response, not_found)
                    else:
                        return None
                else:
                    return await self._read_response(response, not_found)

        except Exception as e:
            logger.error(f"Error making authenticated request: {e}", exc_info=True)
            return None

    @staticmethod
    async def _read_response(resp: aiohttp.ClientResponse, not_found: Any = None) -> Any:
        """Turn a response into a dict, or None if the request failed.

        With `not_found` set, a 404 carrying FastAPI's JSON error body
        ({"detail": ...}) returns `not_found` instead. The body check is what
        makes it RomM's answer: a reverse proxy that cannot route the request
        also says 404, usually as an HTML page, and that is a failed request,
        not a statement about the thing asked about.
        """
        if resp.status == 404 and not_found is not None:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = None
            if isinstance(body, dict) and 'detail' in body:
                logger.debug(f"RomM reports not found: {body.get('detail')}")
                return not_found
            logger.error("Request failed with status 404 and no RomM error body")
            return None

        if resp.status in (200, 201, 204):
            if resp.status == 204:
                return {}  # Success with no content is a valid response

            try:
                json_response = await resp.json()
                # A `null` response body becomes `None`. Treat this as a successful empty response.
                return json_response if json_response is not None else {}
            except Exception:
                # An empty body or non-JSON response on a success status code is also a success.
                logger.debug(
                    f"Could not parse JSON from successful response (Status {resp.status}), "
                    "but treating as success."
                )
                return {}

        logger.error(f"Request failed with status {resp.status}")
        try:
            response_text = await resp.text()
            logger.error(f"Response: {response_text}")
        except Exception:
            logger.error("Could not read response text.")
        return None

    async def fetch_api_endpoint(
        self, endpoint: str, bypass_cache: bool = False, max_retries: int = 2
    ) -> Optional[Dict]:
        """Fetch data from API with caching, error handling, and retries."""
        # Bypass cache if specified
        if not bypass_cache:
            cached_data = self.cache.get(endpoint)
            if cached_data:
                logger.debug(f"Returning cached data for {endpoint}")
                return cached_data

        for attempt in range(max_retries + 1):
            try:
                return await self._get_json(endpoint, attempt, max_retries)
            except RommAuthError as e:
                # Credentials will not become valid by trying again.
                logger.error(f"Authentication failed for {endpoint}: {e}")
                return None
            except RommApiError as e:
                logger.error(f"Request to {endpoint} failed: {e}")
                if attempt >= max_retries:
                    return None
            except TimeoutError:
                if attempt >= max_retries:
                    logger.error(f"Request failed after {max_retries + 1} attempts (timeout)")
                    return None
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                logger.warning(
                    f"Request timeout, retrying in {wait_time}s... "
                    f"(attempt {attempt + 1}/{max_retries + 1})"
                )
                await asyncio.sleep(wait_time)
            except Exception as e:
                if attempt >= max_retries:
                    logger.error(f"Request failed after {max_retries + 1} attempts: {e}")
                    return None
                wait_time = 2 ** attempt
                logger.warning(
                    f"Request error: {e}, retrying in {wait_time}s... "
                    f"(attempt {attempt + 1}/{max_retries + 1})"
                )
                await asyncio.sleep(wait_time)

        return None

    async def _get_json(self, endpoint: str, attempt: int, max_retries: int) -> Optional[Dict]:
        """One GET attempt. Raises RommApiError/RommAuthError on failure."""
        if not await self.ensure_valid_token():
            raise RommAuthError("Failed to obtain valid OAuth token")

        session = await self.ensure_session()
        url = f"{self.config.API_BASE_URL}/api/{endpoint}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }

        logger.debug(f"Making request to: {url} (attempt {attempt + 1}/{max_retries + 1})")

        async with session.get(url, headers=headers) as response:
            logger.debug(f"Response status: {response.status}")
            logger.debug(f"Response content-type: {response.headers.get('content-type', 'unknown')}")

            if response.status == 200:
                try:
                    data = await response.json()
                except (json.JSONDecodeError, aiohttp.ContentTypeError) as e:
                    logger.error(f"Invalid JSON response from {endpoint}: {e}")
                    return None
                logger.debug(f"Fetched fresh data for {endpoint}")
                self.cache.set(endpoint, data)
                return data

            error_text = await response.text()
            if response.status in (401, 403):
                raise RommAuthError(f"status {response.status}: {error_text}")
            raise RommApiError(f"status {response.status}: {error_text}")

    # --------------------------------------------------------------- netplay

    async def get_server_config(self) -> Optional[Dict[str, Any]]:
        """GET /api/config - what this RomM server supports.

        Carries EJS_NETPLAY_ENABLED and EJS_NETPLAY_ICE_SERVERS. Must be
        authenticated: RomM redacts the ICE server list to [] and the config
        parse error to None for anonymous callers, because ICE entries can
        hold TURN credentials. An unauthenticated check reports a correctly
        configured server as unconfigured.
        """
        return await self.make_authenticated_request('GET', 'config')

    async def get_server_version(self) -> Optional[Tuple[int, int, int]]:
        """RomM's release as (major, minor, patch), from /api/heartbeat.

        A pre-release reads as the release it leads to: "5.5.0-beta.1" is
        (5, 5, 0), which is what feature checks want, since a feature lands in
        the first alpha of its release. None for anything that is not a
        numbered release - "development" builds, an unreachable server - so
        callers must pick their own safe default.
        """
        body = await self.make_authenticated_request('GET', 'heartbeat')
        system = body.get('SYSTEM') if isinstance(body, dict) else None
        raw = system.get('VERSION') if isinstance(system, dict) else None
        return parse_romm_version(raw)

    async def list_netplay_rooms(self, rom_id: int) -> Any:
        """GET /api/netplay/list?game_id=<rom_id> - open rooms for one ROM.

        Returns a dict keyed by room session id, {} when nothing is open,
        NOT_FOUND when RomM says the ROM is gone, or None when the request
        failed. NOT_FOUND only happens on RomM 5.4+, which 404s a ROM that was
        deleted or hidden from us; earlier versions never looked the ROM up
        and answered {}.
        game_id is mandatory - omitting it is a 422 - so there is no way to
        list every room on the server, which is why callers poll a bounded
        set of ROMs rather than enumerating.

        Deliberately not routed through fetch_api_endpoint: that helper writes
        to the shared APICache even when bypass_cache is set, and a room list
        is stale within seconds. The cost is that there is no retry here;
        callers must tolerate a None and try again on the next tick.
        """
        return await self.make_authenticated_request(
            'GET', 'netplay/list', params={'game_id': rom_id}, not_found=NOT_FOUND
        )

    async def netplay_scope_ok(self) -> Optional[bool]:
        """Whether our token carries the scope /netplay/list needs.

        The scope moved in RomM 5.4.0: through 5.3 the route was guarded by
        assets.read, from 5.4 by roms.read. The probe asks the route itself
        rather than inspecting our scopes, so it is right on either version.
        It still answers 403 before 404 on 5.4: the scope guard wraps the
        handler, and the handler is what 404s game_id=0.

        True authorized, False definitely not (401/403), None undetermined -
        the server was unreachable, or answered something that says nothing
        about our scopes. The three-way answer is the whole point: a missing
        scope should stop the feature, a flaky network should not.

        This cannot go through make_authenticated_request or
        fetch_api_endpoint, both of which flatten every failure to None
        (_read_response returns None for all non-2xx alike). Distinguishing
        the two needs the status code, which is why this reads the response
        directly - the same reason integrations/romm_streaming.py returns
        (status, body) rather than a bare body.
        """
        try:
            if not await self.ensure_valid_token():
                return None

            session = await self.ensure_session()
            url = f"{self.config.API_BASE_URL}/api/netplay/list"
            headers = {
                "Authorization": f"Bearer {self.access_token}",
                "Accept": "application/json",
            }

            async with session.get(
                url, headers=headers, params={'game_id': 0}
            ) as response:
                if response.status in (401, 403):
                    return False
                if 200 <= response.status < 500:
                    # Includes 404/422: we were allowed to ask, which is all
                    # this is checking.
                    return True
                return None

        except Exception as e:
            logger.debug(f"Netplay scope probe could not complete: {e}")
            return None

    async def netplay_permission_ok(self) -> Optional[bool]:
        """Whether our RomM user holds the emulation-read permission.

        RomM 5.4.0 added a permission check inside /netplay/list on top of
        the scope check: a user without emulation read gets {} rather than a
        403, which is indistinguishable from "nobody is playing". Every
        permission group is granted it by default (migration 0153), so this
        only bites when an admin has revoked it.

        /api/permissions/me reports that grant as the UI action "rom.play"
        (revoking emulation read is what drops it). Admins bypass the check.

        True or False only on a successful answer. None when it cannot be
        determined: RomM before 5.4 has no such endpoint (404), and a token
        without me.read is refused - neither says anything about the grant.
        """
        body = await self.make_authenticated_request('GET', 'permissions/me')
        if not isinstance(body, dict) or not isinstance(body.get('grants'), list):
            return None
        if body.get('is_admin'):
            return True
        actions = {g.get('action') for g in body['grants'] if isinstance(g, dict)}
        return 'rom.play' in actions
