import asyncio
import importlib
import os
import types
import unittest
from unittest.mock import patch


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def text(self):
        return ""


class FakeRequestContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.auth_headers = []

    def request(self, method, url, **kwargs):
        self.auth_headers.append(kwargs["headers"]["Authorization"])
        return FakeRequestContext(self.responses.pop(0))


class FakeConfig:
    API_BASE_URL = "https://romm.example"
    ROMM_CLIENT_TOKEN = None


class FakeBot:
    def __init__(self):
        self.config = FakeConfig()
        self.access_token = "expired-token"
        self.refresh_calls = 0
        self.session = FakeSession(
            [
                FakeResponse(401),
                FakeResponse(200, {"ok": True}),
            ]
        )

    async def ensure_valid_token(self):
        return True

    async def ensure_session(self):
        return self.session

    async def refresh_oauth_token(self):
        self.refresh_calls += 1
        self.access_token = "fresh-token"
        return True


class BotAuthTests(unittest.IsolatedAsyncioTestCase):
    def test_bot_module_imports_json_for_json_decode_handlers(self):
        bot_module = importlib.import_module("bot")

        self.assertTrue(hasattr(bot_module, "json"))

    async def test_authenticated_request_refreshes_token_immediately_after_401(self):
        bot_module = importlib.import_module("bot")
        fake_bot = FakeBot()

        result = await bot_module.RommBot.make_authenticated_request(
            fake_bot,
            "GET",
            "roms",
        )

        self.assertEqual({"ok": True}, result)
        self.assertEqual(1, fake_bot.refresh_calls)
        self.assertEqual(
            ["Bearer expired-token", "Bearer fresh-token"],
            fake_bot.session.auth_headers,
        )

    async def test_authenticated_request_does_not_refresh_client_token_on_401(self):
        bot_module = importlib.import_module("bot")
        fake_bot = FakeBot()
        fake_bot.config.ROMM_CLIENT_TOKEN = "rmm_clienttoken"
        fake_bot.session = FakeSession([FakeResponse(401)])

        with self.assertLogs("romm_bot", level="ERROR"):
            result = await bot_module.RommBot.make_authenticated_request(
                fake_bot,
                "GET",
                "roms",
            )

        self.assertIsNone(result)
        self.assertEqual(0, fake_bot.refresh_calls)

    async def test_ensure_valid_token_uses_client_token_without_oauth(self):
        bot_module = importlib.import_module("bot")

        class ClientTokenConfig:
            ROMM_CLIENT_TOKEN = "rmm_clienttoken"

        fake = types.SimpleNamespace(
            config=ClientTokenConfig(),
            access_token=None,
            token_lock=asyncio.Lock(),
        )

        result = await bot_module.RommBot.ensure_valid_token(fake)

        self.assertTrue(result)
        self.assertEqual("rmm_clienttoken", fake.access_token)


class ConfigCredentialTests(unittest.TestCase):
    def test_romm_specific_credentials_win_over_generic_user_environment(self):
        bot_module = importlib.import_module("bot")

        with patch.dict(
            os.environ,
            {
                "TOKEN": "discord-token",
                "GUILD": "123",
                "API_URL": "https://romm.example",
                "USER": "shell-user",
                "PASS": "legacy-password",
                "ROMM_USER": "romm-user",
                "ROMM_PASS": "romm-password",
            },
            clear=True,
        ):
            config = bot_module.Config()

        self.assertEqual("romm-user", config.USER)
        self.assertEqual("romm-password", config.PASS)

    def test_generic_user_environment_does_not_satisfy_missing_romm_user(self):
        bot_module = importlib.import_module("bot")

        with patch.dict(
            os.environ,
            {
                "TOKEN": "discord-token",
                "GUILD": "123",
                "API_URL": "https://romm.example",
                "USER": "shell-user",
                "ROMM_PASS": "romm-password",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError) as exc:
                bot_module.Config()

        self.assertIn("ROMM_USER", str(exc.exception))

    def test_legacy_user_and_pass_still_work_when_new_names_are_absent(self):
        bot_module = importlib.import_module("bot")

        with patch.dict(
            os.environ,
            {
                "TOKEN": "discord-token",
                "GUILD": "123",
                "API_URL": "https://romm.example",
                "USER": "legacy-user",
                "PASS": "legacy-password",
            },
            clear=True,
        ):
            with self.assertLogs("romm_bot", level="WARNING") as logs:
                config = bot_module.Config()

        self.assertEqual("legacy-user", config.USER)
        self.assertEqual("legacy-password", config.PASS)
        self.assertIn("USER/PASS are deprecated", "\n".join(logs.output))

    def test_client_token_alone_satisfies_validation(self):
        bot_module = importlib.import_module("bot")

        with patch.dict(
            os.environ,
            {
                "TOKEN": "discord-token",
                "GUILD": "123",
                "API_URL": "https://romm.example",
                "ROMM_CLIENT_TOKEN": "rmm_clienttoken",
            },
            clear=True,
        ):
            config = bot_module.Config()

        self.assertEqual("rmm_clienttoken", config.ROMM_CLIENT_TOKEN)
        self.assertIsNone(config.USER)
        self.assertIsNone(config.PASS)

    def test_missing_all_romm_credentials_raises(self):
        bot_module = importlib.import_module("bot")

        with patch.dict(
            os.environ,
            {
                "TOKEN": "discord-token",
                "GUILD": "123",
                "API_URL": "https://romm.example",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError) as exc:
                bot_module.Config()

        self.assertIn("ROMM_USER", str(exc.exception))
        self.assertIn("ROMM_PASS", str(exc.exception))
