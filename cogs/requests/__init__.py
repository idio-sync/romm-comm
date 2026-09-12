"""Game requests: browsing, creating, and fulfilling them.

Split into modules by role. This file re-exports the names other cogs and the
tests import, and is what `load_extension("cogs.requests")` loads.
"""

from .cog import Request
from .embeds import build_request_embed
from .matching import edit_distance_ratio, filter_out_existing, word_overlap_ratio
from .repo import REQUEST_COLUMNS
from .views_admin import RequestAdminView
from .views_game import (
    ExistingGameView,
    ExistingGameWithIGDBView,
    GameSelect,
    GameSelectView,
    VariantRequestModal,
)
from .views_user import UserRequestsView

__all__ = [
    "REQUEST_COLUMNS",
    "ExistingGameView",
    "ExistingGameWithIGDBView",
    "GameSelect",
    "GameSelectView",
    "Request",
    "RequestAdminView",
    "UserRequestsView",
    "VariantRequestModal",
    "build_request_embed",
    "edit_distance_ratio",
    "filter_out_existing",
    "word_overlap_ratio",
]


def setup(bot):
    bot.add_cog(Request(bot))
