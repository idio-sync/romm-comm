import logging

import discord
from discord.ext import commands

logger = logging.getLogger("romm_bot.admin_checks")


def is_admin():
    """
    Decorator to check if the user is an admin.

    The actual admin check logic is delegated to bot.is_admin().
    """

    async def predicate(ctx: discord.ApplicationContext):
        logger.debug(f"Admin check for command: {ctx.command.name} by user: {ctx.author} (ID: {ctx.author.id})")
        is_admin_result = ctx.bot.is_admin(ctx.author)
        if not is_admin_result:
            logger.warning(f"Admin check FAILED for user {ctx.author} (ID: {ctx.author.id}) on command {ctx.command.name}")
        else:
            logger.debug(f"Admin check PASSED for user {ctx.author} on command {ctx.command.name}")
        return is_admin_result

    return commands.check(predicate)
