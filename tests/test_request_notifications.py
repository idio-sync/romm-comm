"""Tests for who gets told when a request is fulfilled.

Subscribers - people who asked for a game someone else had already requested -
were promised a DM when it landed and never got one: the admin fulfil path
fetched their ids and dropped them on the floor.

Also covers navigating a view after a request has been actioned, which broke
when rows started being stored back as dicts.
"""

import sqlite3
import unittest

import discord

from cogs.requests.repo import REQUEST_COLUMNS
from cogs.requests.views_admin import RequestAdminView

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
    def __init__(self, user_id, raises=None):
        self.id = user_id
        self.messages = []
        self.raises = raises
        self.avatar = None
        self.default_avatar = type("A", (), {"url": "https://example/default.png"})()

    async def send(self, content):
        if self.raises:
            raise self.raises
        self.messages.append(content)


class FakeBot:
    def __init__(self, users=None):
        self.users = {user.id: user for user in (users or [])}
        self.fetched = []

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
        if user_id not in self.users:
            raise discord.NotFound(type("R", (), {"status": 404, "reason": ""})(), "no user")
        return self.users[user_id]


class FakeRepo:
    """Only what the fulfil path asks of the repository."""

    def __init__(self, subscriber_ids=()):
        self._subscriber_ids = list(subscriber_ids)
        self.fulfilled = []

    async def mark_fulfilled(self, request_id, *, by_id, by_name):
        self.fulfilled.append((request_id, by_id, by_name))

    async def get_ggr_request_id(self, request_id):
        return None

    async def subscriber_ids(self, request_id):
        return list(self._subscriber_ids)


class FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self):
        self.deferred = True

    async def send_message(self, *args, **kwargs):
        return None

    async def edit_message(self, *args, **kwargs):
        return None


class FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)

    async def edit_message(self, **kwargs):
        return None


class FakeInteraction:
    def __init__(self, user_id=1):
        self.user = type("U", (), {"id": user_id, "__str__": lambda self: "an-admin"})()
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = None


async def no_sleep(_seconds):
    return None


class SubscriberNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def fulfil(self, subscriber_ids=(), extra_users=(), row=None):
        requester = FakeUser(42)
        bot_users = [requester, *extra_users]
        bot = FakeBot(bot_users)

        view = RequestAdminView(bot, [row or request_row()], admin_id=1, db=None)
        view.repo = FakeRepo(subscriber_ids)
        view.message = type("M", (), {"id": 1})()

        import cogs.requests.views_admin as module

        original_sleep = module.asyncio.sleep
        module.asyncio.sleep = no_sleep
        try:
            await view.fulfill_callback(FakeInteraction())
        finally:
            module.asyncio.sleep = original_sleep

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

        with self.assertLogs("cogs.requests.views_admin", level="WARNING"):
            await self.fulfil(subscriber_ids=[101, 102], extra_users=[blocked, reachable])

        self.assertEqual([], blocked.messages)
        self.assertEqual(1, len(reachable.messages))

    async def test_an_unknown_subscriber_does_not_stop_the_rest(self):
        reachable = FakeUser(102)

        with self.assertLogs("cogs.requests.views_admin", level="WARNING"):
            await self.fulfil(subscriber_ids=[999, 102], extra_users=[reachable])

        self.assertEqual(1, len(reachable.messages))

    async def test_no_subscribers_means_no_extra_lookups(self):
        _, _, bot = await self.fulfil(subscriber_ids=[])

        self.assertEqual([42], bot.fetched)


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


if __name__ == "__main__":
    unittest.main()
