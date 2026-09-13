"""Calling into the optional ggrequestz integration.

Two directions, and they are not equally possible.

Pulling status changes back IN works. ggrequestz has no "one request by id"
endpoint, so GGRequestzIntegration.get_request_by_id pages the caller's own
request list; that list authenticates with the bearer API key this integration
already holds.

Pushing a status change OUT cannot be done at all. ggrequestz does have the
endpoint -- POST /admin/api/requests/update, taking request_id, status and
admin_notes -- but its route handler accepts a `session` or
`basic_auth_session` cookie and nothing else, and checks a
request.approve/request.edit permission on the logged-in user. It does not
accept `Authorization: Bearer`, which is the only credential this integration
has. So GGRequestzIntegration has no update_request_status, and cannot have
one without the bot logging in as a user and holding a session.

(Verified against XTREEMMAK/ggrequestz: src/lib/openapi.json and
src/routes/admin/api/requests/update/+server.js, September 2026.)

That gap used to be an AttributeError. Because it was raised in the middle of
an admin action that had already committed its database write, the request
really was fulfilled or rejected, but the embed refresh, the requester's DM and
the wait list's DMs were all skipped. The wrappers here turn a missing or
failing method into a logged line and a False return, so a sync that cannot
happen costs the sync and nothing else.
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GGR_COG_NAME = 'GGRequestzIntegration'


def active_integration(bot):
    """The ggrequestz cog, but only when it is loaded and configured.

    The integration is optional twice over: the extension is loaded only when
    GGREQUESTZ_ENABLED is set, and it turns itself off when its URL or API key
    is missing. Callers should not have to know that.
    """
    ggr = bot.get_cog(GGR_COG_NAME)
    if ggr is None or not getattr(ggr, 'enabled', False):
        return None
    return ggr


# Logged once per process, not once per fulfilment. The outbound gap is a
# standing property of the upstream API, not an incident, and an admin working
# through a queue should not get a warning per button press.
_reported_missing = set()


async def _call(ggr, method_name: str, *args, **kwargs) -> Optional[Any]:
    """Call a method on the integration if it has one. None if it does not."""
    method = getattr(ggr, method_name, None)
    if method is None:
        if method_name not in _reported_missing:
            _reported_missing.add(method_name)
            logger.warning(
                f"ggrequestz integration has no {method_name}(); skipping this "
                "sync and any like it. The request itself is unaffected. See "
                "the module docstring in cogs/requests/ggr_sync.py for why."
            )
        return None
    try:
        return await method(*args, **kwargs)
    except Exception as e:
        logger.error(f"ggrequestz {method_name}() failed: {type(e).__name__}: {e}", exc_info=True)
        return None


async def sync_request_status(
    bot,
    repo,
    request_id: int,
    *,
    status: str,
    admin_name: str,
    notes: str,
) -> bool:
    """Mirror a closed request's new status into ggrequestz.

    Returns True only when ggrequestz confirmed the update. False covers every
    other case -- integration absent or disabled, request never synced there,
    method missing, call failed, or ggrequestz reporting failure -- because no
    caller does anything but log the difference, and every one of them has
    already committed the change locally before getting here.
    """
    ggr = active_integration(bot)
    if ggr is None:
        return False

    ggr_request_id = await repo.get_ggr_request_id(request_id)
    if not ggr_request_id:
        return False

    result = await _call(
        ggr,
        'update_request_status',
        ggr_request_id=ggr_request_id,
        status=status,
        admin_name=admin_name,
        notes=notes,
    )
    if result is None:
        return False

    if result.get('success'):
        logger.info(
            f"Synced {status} to ggrequestz for request #{request_id} (GGR ID: {ggr_request_id})"
        )
        return True

    logger.error(f"Failed to sync {status} to ggrequestz: {result.get('error')}")
    return False


async def fetch_request(ggr, ggr_request_id) -> Optional[Dict]:
    """One request as ggrequestz currently sees it, or None.

    Passed positionally: the method does not exist yet, so its parameter name
    is not ours to guess.
    """
    return await _call(ggr, 'get_request_by_id', ggr_request_id)
