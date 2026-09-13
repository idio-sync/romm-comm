"""What the download-auth probe concluded.

Its own module so embeds.py can name a verdict without importing probe.py,
which would pull aiohttp and cogs.search - and through cogs.search, qrcode
and PIL - into the pure presentation layer.
"""

from enum import Enum


class DownloadAuth(Enum):
    """What we found out about the download endpoint."""

    ENABLED = "enabled"    # Downloads will 403 for feed clients.
    DISABLED = "disabled"  # Downloads are open; feed clients will work.
    UNKNOWN = "unknown"    # We could not tell; fall back to a generic note.
