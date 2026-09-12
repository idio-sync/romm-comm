"""Tests for who gets told when a request is fulfilled.

Subscribers - people who asked for a game someone else had already requested -
were promised a DM when it landed and never got one: the admin fulfil path
fetched their ids and dropped them on the floor.

Also covers navigating a view after a request has been actioned, which broke
when rows started being stored back as dicts.
"""

import contextlib
import sqlite3
import unittest

import discord

from cogs.requests.embeds import STATUS_EMOJI
from cogs.requests.notifications import (
    cancelled_message,
    fulfilled_message,
    rejected_message,
)
from cogs.requests.repo import REQUEST_COLUMNS
from cogs.requests.views_admin import RequestAdminView
from cogs.requests.views_user import UserRequestsView

_ROW_CONN = sqlite3.connect(":memory:")
_ROW_CONN.row_factory = sqlite3.Row

# Taken from the repository rather than restated, so a schema change cannot
# leave these fixtures describing a table that no longer exists.
COLUMNS = [name.strip() for name in REQUEST_COLUMNS.split(",")]


def request_row(**overrides):
    values = {name: None for name in COLUMNS}
    values.update({
        "id": 7,
        "user_id": 42,
        "username": "requester",
        "platform": "Nintendo 64",
        "game_name": "GoldenEye 007",
        "status": "pending",
        "auto_fulfilled": 0,
    })
    values.update(overrides)
    selected = ", ".join(f"? AS {name}" for name in values)
    return _ROW_CONN.execute(f"SELECT {selected}", tuple(values.values())).fetchone()


class FakeUser:
    def __init__(self, user_id, raises=None, events=None):
        self.id = user_id
        self.messages = []
        self.raises = raises
        self.events = events
        self.avatar = None
        self.default_avatar = type("A", (), {"url": "https://example/default.png"})()

    async def send(self, content):
        if self.events is not None:
            self.events.append(f"dm:{self.id}")
        if self.raises:
            raise self.raises
        self.messages.append(content)


class FakeBot:
    def __init__(self, users=None, fetch_errors=None):
        self.users = {user.id: user for user in (users or [])}
        self.fetched = []
        # Ids whose fetch_user blows up before a DM is ever attempted, which is
        # where a flaky connection actually fails.
        self.fetch_errors = dict(fetch_errors or {})

    def is_admin(self, user):
        return True

    def get_cog(self, name):
        return None

    def get_formatted_emoji(self, name):
        return f":{name}:"

    def get_user(self, user_id):
        return self.users.get(user_id)

    async def fetch_user(self, user_id):
        self.fetched.append(user_id)
        if user_id in self.fetch_errors:
            raise self.fetch_errors[user_id]
        if user_id not in self.users:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "no user")
        return self.users[user_id]


class FakeRepo:
    """Only what the request-ending paths ask of the repository."""

    def __init__(self, subscriber_ids=(), subscriber_ids_raises=None):
        self._subscriber_ids = list(subscriber_ids)
        self._subscriber_ids_raises = subscriber_ids_raises
        self.fulfilled = []
        self.rejected = []
        self.cancelled = []

    async def mark_fulfilled(self, request_id, *, by_id, by_name):
        self.fulfilled.append((request_id, by_id, by_name))

    async def mark_rejected(self, request_id, *, by_id, by_name, reason):
        self.rejected.append((request_id, reason))

    async def mark_cancelled(self, request_id, *, reason):
        self.cancelled.append((request_id, reason))

    async def get_ggr_request_id(self, request_id):
        return None

    async def subscriber_ids(self, request_id):
        if self._subscriber_ids_raises:
            raise self._subscriber_ids_raises
        return list(self._subscriber_ids)


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.modal = None

    async def defer(self):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        return None

    async def edit_message(self, *args, **kwargs):
        return None

    async def send_modal(self, modal):
        self.modal = modal


class FakeFollowup:
    def __init__(self, events=None):
        self.sent = []
        self.events = events

    async def send(self, content=None, **kwargs):
        if self.events is not None:
            self.events.append("followup")
        self.sent.append(content)

    async def edit_message(self, **kwargs):
        if self.events is not None:
            self.events.append("view-updated")
        return None


class FakeInteraction:
    def __init__(self, user_id=1, events=None):
        self.user = type("U", (), {"id": user_id, "__str__": lambda self: "an-admin"})()
        self.response = FakeResponse()
        self.followup = FakeFollowup(events)
        self.message = None


@contextlib.contextmanager
def instant_dms():
    """Skip the one-second pacing between DMs."""
    import cogs.requests.notifications as module

    async def no_sleep(_seconds):
        return None

    original = module.asyncio.sleep
    module.asyncio.sleep = no_sleep
    try:
        yield
    finally:
        module.asyncio.sleep = original


class SubscriberNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def fulfil(self, subscriber_ids=(), extra_users=(), row=None):
        requester = FakeUser(42)
        bot_users = [requester, *extra_users]
        bot = FakeBot(bot_users)

        view = RequestAdminView(bot, [row or request_row()], admin_id=1, db=None)
        view.repo = FakeRepo(subscriber_ids)
        view.message = type("M", (), {"id": 1})()

        with instant_dms():
            await view.fulfill_callback(FakeInteraction())

        return requester, view, bot

    async def test_the_requester_is_told(self):
        requester, _, _ = await self.fulfil()

        self.assertEqual(
            ["✅ Your request for 'GoldenEye 007' has been fulfilled!"],
            requester.messages,
        )

    async def test_every_subscriber_is_told(self):
        """The bug: these were fetched and then never messaged."""
        watchers = [FakeUser(101), FakeUser(102)]
        requester, _, _ = await self.fulfil(subscriber_ids=[101, 102], extra_users=watchers)

        expected = "✅ The request you're following for 'GoldenEye 007' has been fulfilled!"
        self.assertEqual([expected], watchers[0].messages)
        self.assertEqual([expected], watchers[1].messages)
        # The requester keeps their own wording.
        self.assertEqual(
            ["✅ Your request for 'GoldenEye 007' has been fulfilled!"],
            requester.messages,
        )

    async def test_the_igdb_name_is_preferred_for_subscribers_too(self):
        watcher = FakeUser(101)
        await self.fulfil(
            subscriber_ids=[101],
            extra_users=[watcher],
            row=request_row(game_name="goldeneye", igdb_game_name="GoldenEye 007"),
        )

        self.assertIn("GoldenEye 007", watcher.messages[0])

    async def test_a_closed_inbox_does_not_stop_the_rest_of_the_list(self):
        blocked = FakeUser(101, raises=discord.Forbidden(
            type("R", (), {"status": 403, "reason": ""})(), "DMs closed"
        ))
        reachable = FakeUser(102)

        with self.assertLogs("cogs.requests.notifications", level="WARNING"):
            await self.fulfil(subscriber_ids=[101, 102], extra_users=[blocked, reachable])

        self.assertEqual([], blocked.messages)
        self.assertEqual(1, len(reachable.messages))

    async def test_an_unknown_subscriber_does_not_stop_the_rest(self):
        reachable = FakeUser(102)

        with self.assertLogs("cogs.requests.notifications", level="WARNING"):
            await self.fulfil(subscriber_ids=[999, 102], extra_users=[reachable])

        self.assertEqual(1, len(reachable.messages))

    async def test_no_subscribers_means_no_extra_lookups(self):
        _, _, bot = await self.fulfil(subscriber_ids=[])

        self.assertEqual([42], bot.fetched)


class RejectionNotificationTests(unittest.IsolatedAsyncioTestCase):
    """A rejected request is a dead end for everyone waiting on it."""

    async def reject(self, subscriber_ids=(), extra_users=(), reason="not obtainable"):
        requester = FakeUser(42)
        bot = FakeBot([requester, *extra_users])

        view = RequestAdminView(bot, [request_row()], admin_id=1, db=None)
        view.repo = FakeRepo(subscriber_ids)
        view.message = type("M", (), {"id": 1})()

        interaction = FakeInteraction()
        await view.reject_callback(interaction)
        modal = interaction.response.modal
        modal.reason.value = reason

        with instant_dms():
            await modal.callback(FakeInteraction())

        return requester, view

    async def test_the_requester_keeps_their_own_wording(self):
        requester, _ = await self.reject()

        self.assertIn("Your request for 'GoldenEye 007' has been rejected", requester.messages[0])

    async def test_subscribers_are_told_and_pointed_at_request(self):
        watcher = FakeUser(101)
        await self.reject(subscriber_ids=[101], extra_users=[watcher])

        message = watcher.messages[0]
        self.assertIn("The request you're following for 'GoldenEye 007' was rejected", message)
        self.assertIn("Reason: not obtainable", message)
        self.assertIn("/request", message)

    async def test_no_reason_means_no_reason_line(self):
        watcher = FakeUser(101)
        await self.reject(subscriber_ids=[101], extra_users=[watcher], reason="")

        self.assertNotIn("Reason:", watcher.messages[0])
        self.assertIn("/request", watcher.messages[0])


class CancellationNotificationTests(unittest.IsolatedAsyncioTestCase):
    """The requester walking away strands everyone who joined their wait list."""

    async def cancel(self, subscriber_ids=(), extra_users=()):
        bot = FakeBot([FakeUser(42), *extra_users])

        view = UserRequestsView(bot, [request_row()], user_id=42, db=None)
        view.repo = FakeRepo(subscriber_ids)
        view.message = type("M", (), {"id": 1})()

        interaction = FakeInteraction(user_id=42)
        await view.cancel_callback(interaction)
        modal = interaction.response.modal
        modal.reason.value = "changed my mind"

        with instant_dms():
            await modal.callback(FakeInteraction(user_id=42))

        return view

    async def test_subscribers_are_told_who_cancelled_and_what_to_do(self):
        watcher = FakeUser(101)
        await self.cancel(subscriber_ids=[101], extra_users=[watcher])

        message = watcher.messages[0]
        self.assertIn("was cancelled by the person who made it", message)
        self.assertIn("GoldenEye 007", message)
        self.assertIn("/request", message)

    async def test_the_request_is_still_cancelled(self):
        view = await self.cancel()

        self.assertEqual([(7, "changed my mind")], view.repo.cancelled)


class DeliveryIsBestEffortTests(unittest.IsolatedAsyncioTestCase):
    """A DM that cannot be sent must not turn a completed action into a failure.

    By the time any of these run, the request's new status is already written
    and committed. Anything that escapes the notification step lands in the
    callback's `except Exception` and tells the admin the action failed - while
    leaving the embed stale and still offering the buttons they just pressed.
    """

    async def fulfil(self, *, subscriber_ids=(), extra_users=(), fetch_errors=None,
                     repo_raises=None):
        bot = FakeBot([FakeUser(42), *extra_users], fetch_errors=fetch_errors)
        view = RequestAdminView(bot, [request_row()], admin_id=1, db=None)
        view.repo = FakeRepo(subscriber_ids, subscriber_ids_raises=repo_raises)
        view.message = type("M", (), {"id": 1})()

        interaction = FakeInteraction()
        with instant_dms():
            await view.fulfill_callback(interaction)
        return view, interaction

    async def test_a_transport_error_does_not_stop_the_rest_of_the_list(self):
        """TimeoutError is not an HTTPException, so it used to escape.

        fetch_user goes over the wire. A connection that drops mid-list raises
        something the three Discord exception types do not cover.
        """
        reachable = FakeUser(102)

        with self.assertLogs("cogs.requests.notifications", level="WARNING"):
            await self.fulfil(
                subscriber_ids=[101, 102],
                extra_users=[reachable],
                fetch_errors={101: TimeoutError("connection dropped")},
            )

        self.assertEqual(1, len(reachable.messages))

    async def test_a_transport_error_is_not_reported_as_a_failed_fulfilment(self):
        with self.assertLogs("cogs.requests.notifications", level="WARNING"):
            view, interaction = await self.fulfil(
                subscriber_ids=[101],
                fetch_errors={101: OSError("connection reset")},
            )

        self.assertEqual([(7, 1, "an-admin")], view.repo.fulfilled)
        self.assertEqual([], interaction.followup.sent)
        self.assertEqual("fulfilled", view.requests[0]["status"])

    async def test_a_transport_error_reaching_the_requester_is_survivable_too(self):
        with self.assertLogs("cogs.requests.notifications", level="WARNING"):
            _, interaction = await self.fulfil(fetch_errors={42: TimeoutError("slow")})

        self.assertEqual([], interaction.followup.sent)

    async def test_an_unreadable_wait_list_does_not_fail_the_action(self):
        """The request is fulfilled and committed before the list is read."""
        with self.assertLogs("cogs.requests.notifications", level="ERROR"):
            view, interaction = await self.fulfil(repo_raises=RuntimeError("database is locked"))

        self.assertEqual([(7, 1, "an-admin")], view.repo.fulfilled)
        self.assertEqual([], interaction.followup.sent)


class ActionIsVisibleBeforeDmsTests(unittest.IsolatedAsyncioTestCase):
    """The embed is rebuilt before the wait list is walked.

    DMs are paced a second apart, so a request with a long wait list would
    otherwise leave the person who actioned it looking at a stale embed - one
    that still offers the button they just pressed - for as long as the fan-out
    takes.
    """

    async def test_the_admin_sees_the_request_closed_before_dms_go_out(self):
        events = []
        watchers = [FakeUser(101, events=events), FakeUser(102, events=events)]
        bot = FakeBot([FakeUser(42, events=events), *watchers])

        view = RequestAdminView(bot, [request_row()], admin_id=1, db=None)
        view.repo = FakeRepo([101, 102])
        view.message = type("M", (), {"id": 1})()

        with instant_dms():
            await view.fulfill_callback(FakeInteraction(events=events))

        self.assertEqual(["view-updated", "dm:42", "dm:101", "dm:102"], events)

    async def test_the_canceller_is_confirmed_before_dms_go_out(self):
        events = []
        watcher = FakeUser(101, events=events)
        bot = FakeBot([FakeUser(42), watcher])

        view = UserRequestsView(bot, [request_row()], user_id=42, db=None)
        view.repo = FakeRepo([101])
        view.message = type("M", (), {"id": 1})()

        interaction = FakeInteraction(user_id=42)
        await view.cancel_callback(interaction)
        modal = interaction.response.modal
        modal.reason.value = "changed my mind"

        with instant_dms():
            await modal.callback(FakeInteraction(user_id=42, events=events))

        self.assertEqual(["view-updated", "followup", "dm:101"], events)


class MessageWordingTests(unittest.TestCase):
    """The three messages a subscriber can receive, side by side."""

    def test_none_of_them_call_it_the_reader_s_own_request(self):
        for message in (
            fulfilled_message("X"),
            rejected_message("X"),
            cancelled_message("X"),
        ):
            self.assertIn("The request you're following", message)
            self.assertNotIn("Your request", message)

    def test_only_the_dead_ends_suggest_requesting_it_yourself(self):
        self.assertNotIn("/request", fulfilled_message("X"))
        self.assertIn("/request", rejected_message("X"))
        self.assertIn("/request", cancelled_message("X"))

    def test_each_carries_the_status_emoji_the_embeds_use(self):
        self.assertTrue(fulfilled_message("X").startswith(STATUS_EMOJI["fulfilled"]))
        self.assertTrue(rejected_message("X").startswith(STATUS_EMOJI["reject"]))
        self.assertTrue(cancelled_message("X").startswith(STATUS_EMOJI["cancelled"]))


class RowStorageAfterActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigating_after_a_fulfil_does_not_crash(self):
        """Regression: rows are stored back as dicts once actioned.

        The navigation callbacks read the requester id off the current row.
        While that read was positional, a dict row raised KeyError - so
        pressing Next after fulfilling something blew up the callback.
        """
        requester = FakeUser(42)
        bot = FakeBot([requester])
        rows = [request_row(id=7), request_row(id=8, user_id=42)]

        view = RequestAdminView(bot, rows, admin_id=1, db=None)
        view.repo = FakeRepo()
        view.message = type("M", (), {"id": 1})()

        await view.fulfill_callback(FakeInteraction())

        # The actioned row is now a dict, not an sqlite3.Row.
        self.assertIsInstance(view.requests[0], dict)

        await view.forward_callback(FakeInteraction())
        self.assertEqual(1, view.current_index)

        await view.back_callback(FakeInteraction())
        self.assertEqual(0, view.current_index)

    async def test_navigating_after_a_cancel_does_not_crash(self):
        """The same substitution happens on the user's own view.

        cancel_callback replaces the actioned row with a dict exactly as the
        admin paths do, and its navigation callbacks read the requester id off
        whatever is there - so it is the same trap, one class over.
        """
        requester = FakeUser(42)
        bot = FakeBot([requester])
        rows = [request_row(id=7, user_id=42), request_row(id=8, user_id=42)]

        view = UserRequestsView(bot, rows, user_id=42, db=None)
        view.repo = FakeRepo()
        view.message = type("M", (), {"id": 1})()

        interaction = FakeInteraction(user_id=42)
        await view.cancel_callback(interaction)
        modal = interaction.response.modal
        modal.reason.value = "changed my mind"

        with instant_dms():
            await modal.callback(FakeInteraction(user_id=42))

        self.assertIsInstance(view.requests[0], dict)
        self.assertEqual("cancelled", view.requests[0]["status"])

        await view.forward_callback(FakeInteraction(user_id=42))
        self.assertEqual(1, view.current_index)

        await view.back_callback(FakeInteraction(user_id=42))
        self.assertEqual(0, view.current_index)


if __name__ == "__main__":
    unittest.main()


class FailureReportingTests(unittest.IsolatedAsyncioTestCase):
    """The real fulfil callback, failing on either side of the write.

    The unit tests cover report_action; these check the callback hands it the
    right boundary - that `committed()` really is called after mark_fulfilled
    and not before.
    """

    def build(self, repo_raises=None, edit_raises=None):
        requester = FakeUser(42)
        bot = FakeBot([requester])
        view = RequestAdminView(bot, [request_row()], admin_id=1, db=None)
        view.repo = FakeRepo()
        view.message = type("M", (), {"id": 1})()

        if repo_raises:
            async def boom(*args, **kwargs):
                raise repo_raises
            view.repo.mark_fulfilled = boom

        return view, bot, edit_raises

    async def run_fulfil(self, view, edit_raises=None):
        interaction = FakeInteraction()
        if edit_raises:
            async def boom(**kwargs):
                raise edit_raises
            interaction.followup.edit_message = boom

        with instant_dms(), self.assertLogs("cogs.requests", level="ERROR"):
            await view.fulfill_callback(interaction)
        return interaction.followup.sent

    async def test_a_write_that_fails_says_the_action_failed(self):
        view, _, _ = self.build(repo_raises=RuntimeError("database is down"))

        sent = await self.run_fulfil(view)

        self.assertTrue(any("error occurred while fulfilling" in s for s in sent), sent)
        self.assertEqual([], view.repo.fulfilled)

    async def test_a_failure_after_the_write_does_not_call_it_a_failure(self):
        """The regression this exists for: the request really was fulfilled.

        The failure is the embed edit rather than a DM, because notify()
        swallows its own transport errors - so only something genuinely
        unexpected reaches the callback's guard, which is the design.
        """
        view, _, _ = self.build()

        sent = await self.run_fulfil(view, edit_raises=RuntimeError("Discord hiccup"))

        self.assertEqual(1, len(view.repo.fulfilled), "the write should have landed")
        self.assertFalse(
            any("error occurred while fulfilling" in s for s in sent),
            f"told the admin it failed after it succeeded: {sent}",
        )
        self.assertTrue(any("went through" in s for s in sent), sent)
