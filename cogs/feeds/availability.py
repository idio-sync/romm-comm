"""Which content this RomM server hosts, and which devices that makes useful.

Reads the platforms payload bot.py already caches every SYNC_RATE rather
than fetching or scheduling anything of its own: bot.cache is the API
client's cache, and update_api_data refreshes the 'platforms' key on that
same loop. That payload is sanitized down to id/name/custom_name/
display_name/rom_count, so matching is by name - there is no slug to match
on - and platforms holding no ROMs have already been filtered out upstream,
which is the behavior we want for gating anyway.

The two questions are kept apart on purpose. What the server hosts is
content; what the user can point at it is hardware. A Vita is worth offering
to someone whose server holds only PSP games, because pkgj running on that
Vita is how those games get installed.
"""

import logging
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .catalog import CONTENT_PLATFORMS, DEVICES, ContentPlatform, Device

logger = logging.getLogger(__name__)


def _labels(entry: Dict[str, Any]) -> List[str]:
    """The names one platform entry can be recognized by, normalized."""
    raw = (entry.get("name"), entry.get("custom_name"))
    return [str(value).strip().lower() for value in raw if value]


def _matches(platform: ContentPlatform, label: str) -> bool:
    if label in platform.exact_names:
        return True
    return any(fragment in label for fragment in platform.name_fragments)


def hosted_content_keys(platforms: Optional[List[Dict[str, Any]]]) -> FrozenSet[str]:
    """ContentPlatform keys this server holds ROMs for.

    An absent or empty payload means we could not find out, which resolves
    to everything: a picker that offers too much is recoverable, one that
    offers nothing looks like the command is broken.
    """
    if not platforms:
        logger.debug("No cached platforms; treating every content platform as hosted")
        return frozenset(platform.key for platform in CONTENT_PLATFORMS)

    found = set()
    for entry in platforms:
        if not isinstance(entry, dict):
            continue
        for label in _labels(entry):
            for platform in CONTENT_PLATFORMS:
                if _matches(platform, label):
                    found.add(platform.key)
    return frozenset(found)


def available_devices(platforms: Optional[List[Dict[str, Any]]]) -> Tuple[Device, ...]:
    """Devices worth offering, in catalog order.

    A device qualifies when the server hosts content for *any* feed it can
    use. Requiring a name match on the hardware itself would hide the Vita
    from someone whose library is all PSP - the exact case pkgj exists for.
    """
    hosted = hosted_content_keys(platforms)
    return tuple(
        device for device in DEVICES
        if any(key in hosted for key in device.content_keys)
    )


def available_device_keys(platforms: Optional[List[Dict[str, Any]]]) -> FrozenSet[str]:
    """As above, but just the keys."""
    return frozenset(device.key for device in available_devices(platforms))
