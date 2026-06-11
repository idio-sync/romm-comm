import unittest

from cogs.user_manager import UserManager


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
