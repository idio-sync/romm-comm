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

import discord

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
    """DM one user. A closed inbox is not an error worth propagating."""
    try:
        user = await bot.fetch_user(user_id)
        await user.send(message)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning(f"Could not DM user {user_id}: {e}")
        return False


async def notify_subscribers(bot, repo, request_id: int, message: str) -> int:
    """Tell everyone on a request's wait list how it ended.

    Each DM is attempted independently, so one person with DMs closed does not
    cut the rest of the list short. Returns how many were reached.
    """
    subscriber_ids = await repo.subscriber_ids(request_id)
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
