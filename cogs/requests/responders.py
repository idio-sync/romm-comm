"""Replying to either a slash-command context or a raw interaction.

The request flow is entered from both: a slash command hands over an
ApplicationContext, while a button on a view hands over an Interaction. They
do not share a reply method, so callers used to carry two copies of a local
`respond` closure and pick between them.
"""

import logging
from contextlib import asynccontextmanager

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


class ActionFailed(Exception):
    """Raised inside `report_action` to fail with a specific message.

    For a failure the code recognised itself, where a generic "an error
    occurred" would be less useful than saying what went wrong.
    """


@asynccontextmanager
async def report_action(followup, action: str, *, on_error_ephemeral: bool = True):
    """Guard an interaction callback, telling the user what actually happened.

    A callback that writes and then presents has two failure modes that read
    identically to the code and not at all to the user. Before the write, "an
    error occurred while <action>" is true. After it, the same message is a
    lie: the request really was fulfilled, the admin is told it was not, and
    the buttons are still live - so they press again, and everyone who was
    notified the first time is notified twice. Two of the bugs found on this
    branch were that exact sequence.

    Call `committed()` the moment the durable change lands:

        async with report_action(interaction.followup, "fulfilling the request") as act:
            await repo.mark_fulfilled(...)
            act.committed()
            ...              # anything here fails without disowning the write

    The broad catch stays: an interaction callback that raises leaves the user
    looking at a spinner, so staying alive matters more than propagating.
    """
    state = _ActionState()
    try:
        yield state
    except ActionFailed as e:
        logger.error(f"Error {action}: {e}")
        await _safe_send(followup, f"❌ {e}", on_error_ephemeral)
    except Exception as e:
        # Type and traceback, because `KeyError: 1` logged as "Error
        # fulfilling request: 1" is indistinguishable from ordinary noise.
        logger.error(f"Error {action}: {type(e).__name__}: {e}", exc_info=True)

        if state.is_committed:
            await _safe_send(
                followup,
                f"⚠️ The {state.noun} went through, but something failed afterwards "
                "and the message above may be out of date. Do not retry - refresh instead.",
                on_error_ephemeral,
            )
        else:
            await _safe_send(followup, f"❌ An error occurred while {action}.", on_error_ephemeral)


class _ActionState:
    def __init__(self):
        self.is_committed = False
        self.noun = "change"

    def committed(self, noun: str = "change") -> None:
        """The durable part is done; later failures must not disown it."""
        self.is_committed = True
        self.noun = noun


async def _safe_send(followup, content: str, ephemeral: bool) -> None:
    """Telling the user it failed can itself fail; that is not worth raising."""
    try:
        await followup.send(content, ephemeral=ephemeral)
    except Exception as e:
        logger.warning(f"Could not report the failure to the user: {e}")
