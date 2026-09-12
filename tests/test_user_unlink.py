"""Tests for unlinking a Discord account from RomM.

Three options - unlink only, unlink and disable, unlink and delete - each with
a failure path, all previously inside one 287-line callback that could not be
reached without a Discord interaction and a RomM server.

The asymmetry between the two failure paths is the thing most worth pinning:
a failed disable still unlinks, a failed delete does not.
"""

import unittest

import discord

from cogs.user_manager import UserManagementView, build_unlink_options_embed


class FakeUser:
    def __init__(self, user_id=1234):
        self.id = user_id
        self.mention = "@casey"


class FakeDB:
    def __init__(self, link=None):
        self.link = link
        self.deleted = []

    async def get_user_link(self, discord_id):
        return self.link

    async def delete_user_link(self, discord_id):
        self.deleted.append(discord_id)
        return True


class FakeCog:
    def __init__(self, link=None, found_user=None):
        self.db_manager = FakeDB(link)
        self.log_channel_id = 99
        self.found_user = found_user
        self.lookups = []

    async def find_user_by_username(self, username):
        self.lookups.append(username)
        return self.found_user


class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, embed=None, **kwargs):
        self.sent.append(embed)


class FakeBot:
    # _MISSING distinguishes "not given" from a deliberate None result, which
    # is what a failed RomM call looks like.
    _MISSING = object()

    def __init__(self, users_data=None, request_result=_MISSING, raises=None, channel=None):
        self.users_data = users_data
        self.request_result = {} if request_result is self._MISSING else request_result
        self.raises = raises
        self.channel = channel
        self.requests = []

    async def fetch_api_endpoint(self, endpoint):
        return self.users_data

    async def make_authenticated_request(self, method, endpoint, **kwargs):
        self.requests.append((method, endpoint))
        if self.raises:
            raise self.raises
        return self.request_result

    def get_channel(self, channel_id):
        return self.channel


class FakeMessage:
    def __init__(self):
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)

    @property
    def content(self):
        return self.edits[-1].get("content", "") if self.edits else ""


def make_view(cog, bot):
    """A UserManagementView without running its component setup."""
    view = object.__new__(UserManagementView)
    view.cog = cog
    view.bot = bot
    view.selected_discord_user = FakeUser()
    view.populated = 0

    async def populate_discord_users():
        view.populated += 1

    view.populate_discord_users = populate_discord_users
    view.update_button_states = lambda: None
    return view


class OptionsEmbedTests(unittest.TestCase):
    def test_all_three_options_are_described(self):
        embed = build_unlink_options_embed(FakeUser(), "casey")

        names = [field.name for field in embed.fields]
        self.assertEqual(3, len(names))
        self.assertTrue(any("Unlink Only" in n for n in names))
        self.assertTrue(any("Disable" in n for n in names))
        self.assertTrue(any("Delete" in n for n in names))

    def test_it_names_both_sides_of_the_link(self):
        embed = build_unlink_options_embed(FakeUser(), "casey")

        self.assertIn("@casey", embed.description)
        self.assertIn("`casey`", embed.description)


class ResolveRommUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_stored_romm_id_avoids_a_lookup(self):
        cog = FakeCog(found_user={"id": 999})
        view = make_view(cog, FakeBot())

        user = await view._resolve_romm_user({"romm_username": "casey", "romm_id": 7})

        self.assertEqual({"id": 7, "username": "casey"}, user)
        self.assertEqual([], cog.lookups, "no lookup needed when the id is stored")

    async def test_without_an_id_the_username_is_looked_up(self):
        cog = FakeCog(found_user={"id": 999, "username": "casey"})
        view = make_view(cog, FakeBot())

        user = await view._resolve_romm_user({"romm_username": "casey", "romm_id": None})

        self.assertEqual({"id": 999, "username": "casey"}, user)
        self.assertEqual(["casey"], cog.lookups)


class AdminGuardTests(unittest.IsolatedAsyncioTestCase):
    async def check(self, users_data, user=None):
        view = make_view(FakeCog(), FakeBot(users_data=users_data))
        # `False` means "no user at all"; None means "not specified".
        resolved = {"id": 7} if user is None else (None if user is False else user)
        return await view._is_admin_account(resolved)

    async def test_an_admin_account_is_recognised(self):
        self.assertTrue(await self.check([{"id": 7, "role": "ADMIN"}]))

    async def test_the_role_check_is_case_insensitive(self):
        self.assertTrue(await self.check([{"id": 7, "role": "admin"}]))

    async def test_an_ordinary_account_is_not(self):
        self.assertFalse(await self.check([{"id": 7, "role": "user"}]))

    async def test_an_unknown_user_is_not(self):
        self.assertFalse(await self.check([{"id": 8, "role": "ADMIN"}]))

    async def test_no_user_at_all_is_not(self):
        self.assertFalse(await self.check([{"id": 7, "role": "ADMIN"}], user=False))

    async def test_an_unreachable_api_does_not_report_admin(self):
        """A failed lookup must not block the unlink; it is not evidence."""
        self.assertFalse(await self.check(None))


class RommAccountOperationTests(unittest.IsolatedAsyncioTestCase):
    def view_for(self, **bot_kwargs):
        return make_view(FakeCog(), FakeBot(**bot_kwargs))

    async def test_disable_sends_a_put_with_the_enabled_flag(self):
        view = self.view_for(request_result={})

        ok, error = await view._disable_romm_account({"id": 7}, "casey")

        self.assertTrue(ok)
        self.assertIsNone(error)
        self.assertEqual([("PUT", "users/7")], view.bot.requests)

    async def test_delete_sends_a_delete(self):
        view = self.view_for(request_result={})

        ok, error = await view._delete_romm_account({"id": 7}, "casey")

        self.assertTrue(ok)
        self.assertEqual([("DELETE", "users/7")], view.bot.requests)

    async def test_a_none_response_is_a_failure(self):
        view = self.view_for(request_result=None)

        with self.assertLogs("romm_bot.users", level="ERROR"):
            ok, error = await view._disable_romm_account({"id": 7}, "casey")

        self.assertFalse(ok)
        self.assertIsNotNone(error)

    async def test_an_unknown_user_never_reaches_the_api(self):
        view = self.view_for()

        with self.assertLogs("romm_bot.users", level="ERROR"):
            ok, error = await view._delete_romm_account(None, "casey")

        self.assertFalse(ok)
        self.assertEqual("Could not find user in RomM", error)
        self.assertEqual([], view.bot.requests)

    async def test_an_exception_becomes_an_error_message(self):
        view = self.view_for(raises=RuntimeError("boom"))

        with self.assertLogs("romm_bot.users", level="ERROR"):
            ok, error = await view._delete_romm_account({"id": 7}, "casey")

        self.assertFalse(ok)
        self.assertEqual("boom", error)


class UnlinkOutcomeTests(unittest.IsolatedAsyncioTestCase):
    """What each option leaves behind, including when RomM says no."""

    def setup(self, **bot_kwargs):
        channel = FakeChannel()
        cog = FakeCog(link={"romm_username": "casey", "romm_id": 7})
        view = make_view(cog, FakeBot(channel=channel, **bot_kwargs))
        return view, cog, channel, FakeMessage()

    async def test_unlink_only_drops_the_link_and_logs_it(self):
        view, cog, channel, msg = self.setup()

        await view._unlink_only(msg, FakeUser(), "casey")

        self.assertEqual([1234], cog.db_manager.deleted)
        self.assertIn("remains active", msg.content)
        self.assertEqual(1, len(channel.sent))
        self.assertIn("Unlinked", channel.sent[0].title)

    async def test_a_successful_disable_drops_the_link_and_logs_it(self):
        view, cog, channel, msg = self.setup(request_result={})

        await view._unlink_and_disable(msg, FakeUser(), "casey", {"id": 7})

        self.assertEqual([1234], cog.db_manager.deleted)
        self.assertIn("cannot login", msg.content)
        self.assertEqual(1, len(channel.sent))

    async def test_a_failed_disable_still_drops_the_link(self):
        """The admin asked for the link to go; the RomM side can be fixed
        separately. This is the opposite of the delete path below."""
        view, cog, channel, msg = self.setup(request_result=None)

        with self.assertLogs("romm_bot.users", level="ERROR"):
            await view._unlink_and_disable(msg, FakeUser(), "casey", {"id": 7})

        self.assertEqual([1234], cog.db_manager.deleted)
        self.assertIn("Failed to disable", msg.content)
        self.assertEqual([], channel.sent, "a failure is not logged as a success")

    async def test_a_successful_delete_drops_the_link_and_logs_it(self):
        view, cog, channel, msg = self.setup(request_result={})

        await view._unlink_and_delete(msg, FakeUser(), "casey", {"id": 7})

        self.assertEqual([1234], cog.db_manager.deleted)
        self.assertIn("deleted RomM account", msg.content)
        self.assertEqual(1, len(channel.sent))

    async def test_a_failed_delete_keeps_the_link_and_asks(self):
        """Deleting is the irreversible option, so a half-finished one is not
        assumed - the link stays until someone confirms."""
        view, cog, channel, msg = self.setup(request_result=None)

        with self.assertLogs("romm_bot.users", level="ERROR"):
            await view._unlink_and_delete(msg, FakeUser(), "casey", {"id": 7})

        self.assertEqual([], cog.db_manager.deleted, "the link must survive a failed delete")
        self.assertIn("unlink the accounts anyway", msg.content)
        self.assertIsInstance(msg.edits[-1]["view"], discord.ui.View)
        self.assertEqual([], channel.sent)

    async def test_the_refresh_leaves_nothing_selected(self):
        view, cog, _, msg = self.setup()

        await view._unlink_only(msg, FakeUser(), "casey")

        self.assertIsNone(view.selected_discord_user)
        self.assertEqual(1, view.populated)


class RecordingInteraction:
    """An interaction that logs what it was asked to do, in order.

    It is its own `response` and `followup`, which is all the dispatcher uses.
    """

    def __init__(self, events):
        self.events = events
        self.response = self
        self.followup = self
        self.messages = []

    async def defer(self):
        self.events.append("defer")

    async def send(self, *args, **kwargs):
        self.events.append("followup.send")
        self.messages.append(RecordingMessage(self.events))
        return self.messages[-1]

    async def edit_original_response(self, **kwargs):
        self.events.append("edit_original_response")


class RecordingMessage(FakeMessage):
    def __init__(self, events):
        super().__init__()
        self.events = events

    async def edit(self, **kwargs):
        self.events.append("confirm_msg.edit")
        await super().edit(**kwargs)


class RecordingDB(FakeDB):
    def __init__(self, events, link=None):
        super().__init__(link)
        self.events = events

    async def delete_user_link(self, discord_id):
        self.events.append("delete_user_link")
        return await super().delete_user_link(discord_id)


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    """unlink_account_callback itself, not just the helpers it calls.

    Every helper was covered; the method that sequences them was not, and two
    differences from the original 287-line version lived in exactly that gap -
    an extra panel refresh on the cancel path, and the disable path dropping
    the link before reporting the failure rather than after.
    """

    LINK = {"romm_username": "casey", "romm_id": 7}

    def drive(self, action, *, link=LINK, users_data=None,
              request_result=FakeBot._MISSING, selected=True):
        """Build a view whose confirmation dialog is already answered."""
        import cogs.user_manager as module

        events = []

        class AnsweredConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=1)
                self.action = action

            async def wait(self):
                return False

        original = module.UnlinkConfirmView
        module.UnlinkConfirmView = AnsweredConfirmView
        self.addCleanup(setattr, module, "UnlinkConfirmView", original)

        cog = FakeCog(found_user={"id": 7})
        cog.db_manager = RecordingDB(events, link)
        bot = FakeBot(users_data=users_data, request_result=request_result,
                      channel=FakeChannel())
        view = make_view(cog, bot)
        view.create_status_embed = lambda: discord.Embed(title="panel")
        if not selected:
            view.selected_discord_user = None

        return view, cog, events, RecordingInteraction(events)

    async def test_cancelling_leaves_the_panel_alone(self):
        """Nothing changed, so there is nothing to redraw behind the dialog."""
        view, cog, events, interaction = self.drive(None)

        await view.unlink_account_callback(interaction)

        self.assertEqual([], cog.db_manager.deleted)
        self.assertNotIn("edit_original_response", events)
        self.assertEqual("Operation cancelled.", interaction.messages[-1].content)

    async def test_acting_redraws_the_panel(self):
        view, cog, events, interaction = self.drive('unlink_only')

        await view.unlink_account_callback(interaction)

        self.assertEqual([1234], cog.db_manager.deleted)
        self.assertEqual("edit_original_response", events[-1])

    async def test_a_failed_disable_reports_before_it_unlinks(self):
        """Editing the message is the step that can still fail here, and if it
        does the safe place to fail is with the link still intact."""
        view, cog, events, interaction = self.drive('unlink_disable', request_result=None)

        with self.assertLogs("romm_bot.users", level="ERROR"):
            await view.unlink_account_callback(interaction)

        self.assertEqual([1234], cog.db_manager.deleted, "a failed disable still unlinks")
        self.assertLess(
            events.index("confirm_msg.edit"),
            events.index("delete_user_link"),
            "the failure is reported before the link is dropped",
        )

    async def test_a_successful_disable_unlinks_before_reporting(self):
        """The mirror image: nothing left to go wrong, so order the other way."""
        view, cog, events, interaction = self.drive('unlink_disable')

        await view.unlink_account_callback(interaction)

        self.assertEqual([1234], cog.db_manager.deleted)
        self.assertLess(events.index("delete_user_link"), events.index("confirm_msg.edit"))

    async def test_an_admin_account_is_refused_before_anything_happens(self):
        view, cog, events, interaction = self.drive(
            'unlink_delete', users_data=[{"id": 7, "role": "ADMIN"}]
        )

        await view.unlink_account_callback(interaction)

        self.assertEqual([], cog.db_manager.deleted)
        self.assertNotIn("edit_original_response", events)

    async def test_a_user_with_no_link_is_refused(self):
        view, cog, events, interaction = self.drive('unlink_only', link=None)

        await view.unlink_account_callback(interaction)

        self.assertEqual([], cog.db_manager.deleted)
        self.assertNotIn("edit_original_response", events)

    async def test_nothing_selected_is_refused(self):
        view, cog, events, interaction = self.drive('unlink_only', selected=False)

        await view.unlink_account_callback(interaction)

        self.assertEqual([], cog.db_manager.deleted)
        self.assertEqual(["defer", "followup.send"], events)


class LogChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_log_channel_is_not_an_error(self):
        view = make_view(FakeCog(), FakeBot(channel=None))

        await view._log_action("t", "d", discord.Color.blue())


if __name__ == "__main__":
    unittest.main()
