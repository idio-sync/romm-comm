import unittest

import discord

from cogs.user_manager import InviteOutcome, UserManager


class FakeMember:
    id = 1234
    display_name = "Casey"
    mention = "@Casey"
    avatar = None
    dm_channel = None


class FakeDM:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class FakeDB:
    def __init__(self, link):
        self.link = link
        self.deleted_links = []

    async def get_user_link(self, discord_id):
        return self.link

    async def delete_user_link(self, discord_id):
        self.deleted_links.append(discord_id)
        return True


class FakeBot:
    def get_channel(self, channel_id):
        return None


def build_manager(link):
    manager = object.__new__(UserManager)
    manager.db_manager = FakeDB(link)
    manager.bot = FakeBot()
    manager.log_channel_id = None
    manager.deleted_users = []
    manager.dm = FakeDM()

    async def find_user_by_username(username):
        return {"id": link["romm_id"], "username": username, "role": "VIEWER"}

    async def delete_user(user_id):
        manager.deleted_users.append(user_id)
        return True

    async def get_or_create_dm_channel(member):
        return manager.dm

    manager.find_user_by_username = find_user_by_username
    manager.delete_user = delete_user
    manager.get_or_create_dm_channel = get_or_create_dm_channel
    return manager


class RoleRemovalOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_role_removal_preserves_unowned_linked_romm_account(self):
        manager = build_manager(
            {
                "romm_username": "existing-user",
                "romm_id": 55,
                "created_by_bot": False,
            }
        )

        with self.assertLogs("romm_bot.users", level="WARNING") as logs:
            result = await manager.handle_role_removal(FakeMember())

        self.assertFalse(result)
        self.assertEqual([], manager.deleted_users)
        self.assertEqual([], manager.db_manager.deleted_links)
        self.assertIn("Preserving RomM account", "\n".join(logs.output))

    async def test_role_removal_deletes_bot_managed_romm_account(self):
        manager = build_manager(
            {
                "romm_username": "bot-created-user",
                "romm_id": 56,
                "created_by_bot": True,
            }
        )

        with self.assertLogs("romm_bot.users", level="INFO") as logs:
            result = await manager.handle_role_removal(FakeMember())

        self.assertTrue(result)
        self.assertEqual([56], manager.deleted_users)
        self.assertEqual([FakeMember.id], manager.db_manager.deleted_links)
        self.assertIn("Deleted user account", "\n".join(logs.output))


class FakeConfig:
    DOMAIN = "https://example.com"


class FakeInviteBot:
    def __init__(self, invite_data):
        self.config = FakeConfig()
        self.invite_data = invite_data
        self.requests = []

    async def make_authenticated_request(self, **kwargs):
        self.requests.append(kwargs)
        return self.invite_data

    def get_channel(self, channel_id):
        return None


class FakeForbiddenResponse:
    status = 403
    reason = "Forbidden"


class ForbiddenDM:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        raise discord.Forbidden(FakeForbiddenResponse(), "cannot send messages to this user")


def build_invite_manager(link=None, invite_data=None, dm=None):
    manager = object.__new__(UserManager)
    manager.db_manager = FakeDB(link)
    manager.bot = FakeInviteBot(invite_data)
    manager.log_channel_id = None
    manager.dm = dm if dm is not None else FakeDM()

    async def get_or_create_dm_channel(member):
        return manager.dm

    manager.get_or_create_dm_channel = get_or_create_dm_channel
    return manager


def invite_url_from(dm):
    """Pull the registration URL out of the embed that was DMed."""
    _, kwargs = dm.sent[0]
    return kwargs["embed"].fields[0].value


class SendInviteLinkOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_linked_member_is_not_invited_again(self):
        manager = build_invite_manager(link={"romm_username": "casey", "romm_id": 7})

        outcome = await manager.send_invite_link(FakeMember())

        self.assertIs(InviteOutcome.ALREADY_LINKED, outcome)
        self.assertEqual([], manager.bot.requests, "no token should be burned")
        self.assertEqual([], manager.dm.sent)

    async def test_successful_send_reports_sent(self):
        manager = build_invite_manager(invite_data={"token": "abc123"})

        outcome = await manager.send_invite_link(FakeMember())

        self.assertIs(InviteOutcome.SENT, outcome)
        self.assertEqual(1, len(manager.dm.sent))

    async def test_server_supplied_url_wins_over_local_domain(self):
        manager = build_invite_manager(
            invite_data={
                "token": "abc123",
                "url": "https://romm.internal/register?token=abc123",
            }
        )

        await manager.send_invite_link(FakeMember())

        self.assertIn("https://romm.internal/register?token=abc123", invite_url_from(manager.dm))
        self.assertNotIn("example.com", invite_url_from(manager.dm))

    async def test_url_falls_back_to_domain_when_server_omits_it(self):
        manager = build_invite_manager(invite_data={"token": "abc123"})

        await manager.send_invite_link(FakeMember())

        self.assertIn("https://example.com/register?token=abc123", invite_url_from(manager.dm))

    async def test_null_url_falls_back_to_domain(self):
        manager = build_invite_manager(invite_data={"token": "abc123", "url": None})

        await manager.send_invite_link(FakeMember())

        self.assertIn("https://example.com/register?token=abc123", invite_url_from(manager.dm))

    async def test_missing_token_reports_failure(self):
        manager = build_invite_manager(invite_data=None)

        outcome = await manager.send_invite_link(FakeMember())

        self.assertIs(InviteOutcome.FAILED, outcome)
        self.assertEqual([], manager.dm.sent)

    async def test_blocked_dms_are_distinguished_from_failure(self):
        manager = build_invite_manager(invite_data={"token": "abc123"}, dm=ForbiddenDM())

        with self.assertLogs("romm_bot.users", level="WARNING"):
            outcome = await manager.send_invite_link(FakeMember())

        self.assertIs(InviteOutcome.DM_BLOCKED, outcome)
