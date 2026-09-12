import unittest
from datetime import UTC, datetime, timedelta

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
    def __init__(self, link, pending=None, links=None):
        self.link = link
        self.deleted_links = []
        self.pending = dict(pending or {})
        self.links = list(links or [])
        self.added_links = []

    async def get_user_link(self, discord_id):
        return self.link

    async def delete_user_link(self, discord_id):
        self.deleted_links.append(discord_id)
        return True

    async def get_all_user_links(self):
        return self.links

    async def add_user_link(self, discord_id, romm_username, romm_id,
                            discord_username=None, discord_avatar=None,
                            created_by_bot=False):
        record = {
            'discord_id': discord_id,
            'romm_username': romm_username,
            'romm_id': romm_id,
            'created_by_bot': created_by_bot,
        }
        self.added_links.append(record)
        self.links.append(record)
        return True

    async def get_pending_invite(self, discord_id):
        return self.pending.get(discord_id)

    async def get_all_pending_invites(self):
        return list(self.pending.values())

    async def add_pending_invite(self, discord_id, jti, role, sent_at, expires_at):
        self.pending[discord_id] = {
            'discord_id': discord_id,
            'jti': jti,
            'role': role,
            'sent_at': sent_at,
            'expires_at': expires_at,
        }
        return True

    async def delete_pending_invite(self, discord_id):
        self.pending.pop(discord_id, None)
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
    # Config.validate() coerces this to an int before any cog sees it.
    GUILD_ID = 42


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


def build_invite_manager(link=None, invite_data=None, dm=None, pending=None):
    manager = object.__new__(UserManager)
    manager.db_manager = FakeDB(link, pending=pending)
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


def iso(dt):
    return dt.isoformat()


class FakeGuild:
    def __init__(self, members=None):
        self._members = members or {}

    def get_member(self, discord_id):
        return self._members.get(discord_id)


class FakeReconcileBot:
    def __init__(self, users, guild=None):
        self.users = users
        self.guild = guild
        self.config = FakeConfig()
        self.sent_to_log = []

    async def fetch_api_endpoint(self, endpoint, bypass_cache=False):
        return self.users

    def get_guild(self, guild_id):
        return self.guild

    def get_channel(self, channel_id):
        return None


def build_reconciler(pending, users, links=None, guild=None):
    manager = object.__new__(UserManager)
    manager.db_manager = FakeDB(None, pending=pending, links=links)
    manager.bot = FakeReconcileBot(users, guild=guild)
    manager.log_channel_id = None
    manager.ambiguous_invites_reported = set()
    return manager


class ReconcilePendingInvitesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(UTC)
        self.sent = self.now - timedelta(hours=1)
        self.expires = self.now + timedelta(days=7)

    def pending_row(self, discord_id, sent_at=None, expires_at=None):
        return {
            discord_id: {
                'discord_id': discord_id,
                'jti': f"jti-{discord_id}",
                'role': 'user',
                'sent_at': iso(sent_at or self.sent),
                'expires_at': iso(expires_at or self.expires),
            }
        }

    async def test_single_new_account_is_linked_to_the_invite(self):
        manager = build_reconciler(
            pending=self.pending_row(1234),
            users=[{'id': 9, 'username': 'casey', 'created_at': iso(self.now)}],
        )

        linked = await manager.reconcile_pending_invites()

        self.assertEqual(1, linked)
        self.assertEqual(
            [{'discord_id': 1234, 'romm_username': 'casey', 'romm_id': 9,
              'created_by_bot': True}],
            manager.db_manager.added_links,
        )
        self.assertEqual({}, manager.db_manager.pending, "invite should be cleared")

    async def test_account_predating_the_invite_is_ignored(self):
        manager = build_reconciler(
            pending=self.pending_row(1234),
            users=[{
                'id': 9,
                'username': 'someone-else',
                'created_at': iso(self.sent - timedelta(days=30)),
            }],
        )

        linked = await manager.reconcile_pending_invites()

        self.assertEqual(0, linked)
        self.assertEqual([], manager.db_manager.added_links)
        self.assertIn(1234, manager.db_manager.pending, "invite stays outstanding")

    async def test_already_linked_account_is_not_claimed_again(self):
        manager = build_reconciler(
            pending=self.pending_row(1234),
            users=[{'id': 9, 'username': 'casey', 'created_at': iso(self.now)}],
            links=[{'discord_id': 5678, 'romm_username': 'casey', 'romm_id': 9,
                    'created_by_bot': True}],
        )

        linked = await manager.reconcile_pending_invites()

        self.assertEqual(0, linked)
        self.assertEqual([], manager.db_manager.added_links)

    async def test_two_candidates_are_not_guessed_between(self):
        manager = build_reconciler(
            pending=self.pending_row(1234),
            users=[
                {'id': 9, 'username': 'casey', 'created_at': iso(self.now)},
                {'id': 10, 'username': 'jordan', 'created_at': iso(self.now)},
            ],
        )

        with self.assertLogs("romm_bot.users", level="INFO"):
            linked = await manager.reconcile_pending_invites()

        self.assertEqual(0, linked)
        self.assertEqual([], manager.db_manager.added_links)
        self.assertIn(1234, manager.db_manager.pending)
        self.assertIn(1234, manager.ambiguous_invites_reported)

    async def test_contended_account_is_claimed_by_neither_invite(self):
        pending = self.pending_row(1234)
        pending.update(self.pending_row(5678))
        manager = build_reconciler(
            pending=pending,
            users=[{'id': 9, 'username': 'casey', 'created_at': iso(self.now)}],
        )

        with self.assertLogs("romm_bot.users", level="INFO"):
            linked = await manager.reconcile_pending_invites()

        self.assertEqual(0, linked, "one account cannot belong to two invites")
        self.assertEqual([], manager.db_manager.added_links)

    async def test_expired_invite_with_no_registration_is_dropped(self):
        manager = build_reconciler(
            pending=self.pending_row(
                1234,
                sent_at=self.now - timedelta(days=30),
                expires_at=self.now - timedelta(days=1),
            ),
            users=[],
        )

        with self.assertLogs("romm_bot.users", level="INFO"):
            linked = await manager.reconcile_pending_invites()

        self.assertEqual(0, linked)
        self.assertEqual({}, manager.db_manager.pending)

    async def test_unreachable_api_leaves_everything_alone(self):
        manager = build_reconciler(pending=self.pending_row(1234), users=None)

        with self.assertLogs("romm_bot.users", level="WARNING"):
            linked = await manager.reconcile_pending_invites()

        self.assertEqual(0, linked)
        self.assertIn(1234, manager.db_manager.pending)
        self.assertEqual([], manager.db_manager.added_links)


class OutstandingInviteTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_invite_blocks_a_second_send(self):
        expires = datetime.now(UTC) + timedelta(days=1)
        manager = build_invite_manager(
            invite_data={'token': 'abc123'},
            pending={1234: {'discord_id': 1234, 'expires_at': expires.isoformat()}},
        )

        outcome = await manager.send_invite_link(FakeMember())

        self.assertIs(InviteOutcome.ALREADY_INVITED, outcome)
        self.assertEqual([], manager.bot.requests, "no second token should be minted")
        self.assertEqual([], manager.dm.sent)

    async def test_expired_invite_allows_a_fresh_send(self):
        expired = datetime.now(UTC) - timedelta(days=1)
        manager = build_invite_manager(
            invite_data={'token': 'abc123'},
            pending={1234: {'discord_id': 1234, 'expires_at': expired.isoformat()}},
        )

        outcome = await manager.send_invite_link(FakeMember())

        self.assertIs(InviteOutcome.SENT, outcome)

    async def test_sending_records_the_invite_as_outstanding(self):
        manager = build_invite_manager(invite_data={'token': 'abc123'})

        await manager.send_invite_link(FakeMember())

        self.assertIn(FakeMember.id, manager.db_manager.pending)

    async def test_blocked_dm_still_records_the_invite(self):
        manager = build_invite_manager(invite_data={'token': 'abc123'}, dm=ForbiddenDM())

        with self.assertLogs("romm_bot.users", level="WARNING"):
            await manager.send_invite_link(FakeMember())

        self.assertIn(FakeMember.id, manager.db_manager.pending,
                      "the token exists even though the DM did not land")
