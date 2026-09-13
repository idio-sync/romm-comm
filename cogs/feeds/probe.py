"""Does this server's download endpoint require authentication?

DISABLE_DOWNLOAD_ENDPOINT_AUTH=true opens GET /api/roms/{id}/content/... and
nothing else - the /api/feeds/ routes still want basic auth either way. The
two are constantly conflated, and the consequence of getting it wrong is a
user who sets everything up correctly and then watches every download fail.

The probe is deliberately not routed through bot.fetch_api_endpoint. That
method attaches the bot's own credentials, which would return 200 on a
locked server and produce exactly the wrong answer - and it cannot take a
URL anyway, only an endpoint name relative to API_BASE_URL.
"""

import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

import aiohttp

from cogs.search import build_rom_download_url

from .urls import with_scheme
from .verdict import DownloadAuth

logger = logging.getLogger(__name__)

__all__ = [
    "DownloadAuth",
    "classify",
    "detect_download_auth",
    "first_rom",
    "is_probeable",
]

# bot.py's default when DOMAIN is unset. Probing it would be meaningless.
UNSET_DOMAIN = "No website configured"

PROBE_TIMEOUT_SECONDS = 10


def is_probeable(domain: Optional[str]) -> bool:
    """Whether `domain` is a real address worth sending a request to."""
    return bool(domain) and domain.strip() != UNSET_DOMAIN


def first_rom(payload: Union[Dict[str, Any], List[Any], None]) -> Optional[Dict[str, Any]]:
    """One ROM out of either shape RomM returns for the roms endpoint.

    Older versions answer with a bare list, newer ones with a paged dict.
    Handling only one of them would report UNKNOWN on half the versions in
    the wild.
    """
    if isinstance(payload, dict):
        payload = payload.get("items")
    if not isinstance(payload, list) or not payload:
        return None
    first = payload[0]
    return first if isinstance(first, dict) else None


def classify(status: Optional[int]) -> DownloadAuth:
    """Turn the probe's HTTP status into a verdict.

    A 3xx counts as auth being on. Self-hosted RomM is commonly behind an
    auth proxy (Authelia, Authentik, oauth2-proxy, Cloudflare Access) that
    answers an unauthenticated request with a redirect to a login page - and
    that login page returns 200. Reading that as "downloads are open" would
    suppress the warning on precisely the servers that need it most.
    """
    if status is None:
        return DownloadAuth.UNKNOWN
    if status in (401, 403) or 300 <= status < 400:
        return DownloadAuth.ENABLED
    if status in (200, 206):
        return DownloadAuth.DISABLED
    return DownloadAuth.UNKNOWN


async def _aiohttp_get(url: str) -> Optional[int]:
    """One byte, no credentials, no shared session, no redirects.

    A fresh session is the point: the bot's own client carries tokens and
    cookies, and reusing it would defeat the probe entirely. Redirects are
    not followed for the reason classify() explains.
    """
    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            url, headers={"Range": "bytes=0-0"}, allow_redirects=False
        ) as response:
            return response.status


async def detect_download_auth(
    domain: str,
    fetch_api_endpoint: Callable[..., Awaitable[Any]],
    http_get: Optional[Callable[[str], Awaitable[Optional[int]]]] = None,
) -> DownloadAuth:
    """Probe `domain` for whether ROM downloads need credentials.

    `domain` is config.DOMAIN rather than API_BASE_URL on purpose: DOMAIN is
    the address the user's console will reach, reverse proxy included, and
    predicting what happens to the user is the entire point. API_BASE_URL is
    how the bot reaches the server, which is frequently a different address
    and would answer a question nobody asked.
    """
    if not is_probeable(domain):
        logger.debug("DOMAIN is unset; skipping the download-auth probe")
        return DownloadAuth.UNKNOWN

    get = http_get or _aiohttp_get

    try:
        payload = await fetch_api_endpoint("roms?limit=1")
    except Exception as e:
        logger.debug(f"Could not list ROMs for the download-auth probe: {e}")
        return DownloadAuth.UNKNOWN

    rom = first_rom(payload)
    if not rom or rom.get("id") is None:
        logger.debug("No ROMs available to probe with")
        return DownloadAuth.UNKNOWN

    # with_scheme, not the raw DOMAIN: build_rom_download_url does no
    # normalizing, so a scheme-less domain would produce a request that can
    # never succeed and a verdict stuck on UNKNOWN. The embed builder
    # normalizes the same way, so both talk about the same URL.
    url = build_rom_download_url(with_scheme(domain), rom["id"], rom.get("fs_name", "unknown_file"))

    try:
        status = await get(url)
    except Exception as e:
        logger.debug(f"Download-auth probe request failed: {e}")
        return DownloadAuth.UNKNOWN

    verdict = classify(status)
    logger.info(f"Download-auth probe: HTTP {status} -> {verdict.value}")
    return verdict
