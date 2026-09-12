"""Telling people what happened to a request they were waiting on.

Two audiences. The person who filed the request hears "Your request for X";
everyone who joined its wait list afterwards hears "The request you're
following for X", because calling it theirs would read as wrong to them.

Both admin and user views end requests, so the sending lives here rather than
on either one.
"""

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Seconds between consecutive DMs, matching the scan notifications.
DM_INTERVAL = 1

# A request only blocks a duplicate while it is pending, so once one is
# rejected or cancelled the people who were waiting on it can file their own.
# Saying so is the difference between a dead end and a next step.
REQUEST_IT_YOURSELF = "You can request it yourself with /request if you still want it."


def fulfilled_message(game_name: str) -> str:
    return f"✅ The request you're following for '{game_name}' has been fulfilled!"


def rejected_message(game_name: str, reason: Optional[str] = None) -> str:
    message = f"❌ The request you're following for '{game_name}' was rejected."
    if reason:
        message += f"\nReason: {reason}"
    return message + f"\n{REQUEST_IT_YOURSELF}"


def cancelled_message(game_name: str) -> str:
    return (
        f"🚫 The request you're following for '{game_name}' was cancelled "
        "by the person who made it."
        f"\n{REQUEST_IT_YOURSELF}"
    )


async def notify(bot, user_id: int, message: str) -> bool:
    """DM one user. Nothing that goes wrong here is worth propagating.

    A closed inbox raises Forbidden and a deleted account raises NotFound, but
    a flaky connection raises neither: fetch_user goes over the wire, so it can
    also fail with a timeout or a transport error that is not an HTTPException
    at all. Those used to escape, and because the caller has already written
    the request's new status to the database by the time it gets here, they
    surfaced as "an error occurred" for an action that had in fact succeeded -
    and took the rest of the wait list down with them.
    """
    try:
        user = await bot.fetch_user(user_id)
        await user.send(message)
        return True
    except Exception as e:
        logger.warning(f"Could not DM user {user_id}: {e}")
        return False


async def notify_subscribers(bot, repo, request_id: int, message: str) -> int:
    """Tell everyone on a request's wait list how it ended.

    Each DM is attempted independently, so one unreachable recipient does not
    cut the rest of the list short. Returns how many were reached.

    Delivery is best effort in both directions: failing to read the wait list
    is reported and returns nothing, rather than being raised at a caller whose
    own work is already done and committed.
    """
    try:
        subscriber_ids = await repo.subscriber_ids(request_id)
    except Exception as e:
        logger.error(f"Could not read the wait list for request #{request_id}: {e}")
        return 0

    if not subscriber_ids:
        return 0

    logger.info(f"Notifying {len(subscriber_ids)} subscriber(s) of request #{request_id}")

    reached = 0
    for index, user_id in enumerate(subscriber_ids):
        if index:
            await asyncio.sleep(DM_INTERVAL)
        if await notify(bot, user_id, message):
            reached += 1
    return reached
