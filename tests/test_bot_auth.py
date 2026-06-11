import importlib
import unittest


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
