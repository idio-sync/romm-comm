"""Replying to either a slash-command context or a raw interaction.

The request flow is entered from both: a slash command hands over an
ApplicationContext, while a button on a view hands over an Interaction. They
do not share a reply method, so callers used to carry two copies of a local
`respond` closure and pick between them.
"""

import logging

import discord

logger = logging.getLogger(__name__)


def responder_for(ctx_or_interaction):
    """Return (author, author_name, respond) for a context or an interaction.

    `respond` takes content, embed or embeds and sends by whichever route the
    caller arrived through, following up when the interaction has already been
    responded to.
    """
    if hasattr(ctx_or_interaction, 'user'):
        author = ctx_or_interaction.user

        async def respond(content=None, embed=None, embeds=None):
            kwargs = _reply_kwargs(content, embed, embeds)
            try:
                if ctx_or_interaction.response.is_done():
                    return await ctx_or_interaction.followup.send(**kwargs)
                return await ctx_or_interaction.response.send_message(**kwargs)
            except discord.errors.InteractionResponded:
                # Something else replied between the check and the send.
                return await ctx_or_interaction.followup.send(**kwargs)
    else:
        author = ctx_or_interaction.author

        async def respond(content=None, embed=None, embeds=None):
            return await ctx_or_interaction.respond(**_reply_kwargs(content, embed, embeds))

    return author, str(author), respond


def _reply_kwargs(content, embed, embeds):
    """Only pass what was given; embed wins over embeds, as before."""
    kwargs = {}
    if content is not None:
        kwargs['content'] = content
    if embed is not None:
        kwargs['embed'] = embed
    elif embeds is not None:
        kwargs['embeds'] = embeds
    return kwargs
