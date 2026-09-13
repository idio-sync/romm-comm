"""Calling into the optional ggrequestz integration.

Two of the methods this package calls are not implemented on
GGRequestzIntegration and, as far as the endpoint map shows, never were:

    update_request_status   3 call sites  (manual fulfil, manual reject,
                                           auto-fulfilment)
    get_request_by_id       1 call site   (pulling status changes back in)

Calling a missing method raised AttributeError. In the two admin views that
landed after the database write had already committed, so the request really
was fulfilled or rejected, but everything queued behind the call -- refreshing
the embed, re-enabling the buttons, the requester's DM and the wait list's DMs
-- was skipped. The suite did not catch it because the only definition of
`update_request_status` in the tree is on a test double.

These wrappers make a missing or failing method a logged warning and a False
return, so a sync that cannot happen costs the sync and nothing else. They are
a guard, not an implementation: when the two methods are written, the
`_call` indirection can go and these become plain calls.
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


async def _call(ggr, method_name: str, *args, **kwargs) -> Optional[Any]:
    """Call a method on the integration if it has one. None if it does not."""
    method = getattr(ggr, method_name, None)
    if method is None:
        logger.warning(
            f"ggrequestz integration has no {method_name}(); skipping the sync. "
            "The request itself is unaffected."
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
