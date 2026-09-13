"""Tests for the guards around the optional ggrequestz integration.

`update_request_status` and `get_request_by_id` are called by this package and
are not implemented on GGRequestzIntegration. Before these guards, calling one
raised AttributeError in the middle of an admin action that had already
committed its database write, taking the embed refresh and every DM with it.
"""

import unittest
from unittest import IsolatedAsyncioTestCase

from cogs.requests.ggr_sync import active_integration, fetch_request, sync_request_status


class FakeRepo:
    def __init__(self, ggr_request_id=99):
        self._id = ggr_request_id

    async def get_ggr_request_id(self, request_id):
        return self._id


class FakeBot:
    def __init__(self, cog):
        self._cog = cog

    def get_cog(self, name):
        return self._cog


class NoStatusMethod:
    """The integration as it actually is today: enabled, method absent."""
    enabled = True


class Working:
    enabled = True

    def __init__(self, result):
        self.result = result
        self.calls = []

    async def update_request_status(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class Exploding:
    enabled = True

    async def update_request_status(self, **kwargs):
        raise RuntimeError("ggrequestz is down")


SYNC = dict(status='fulfilled', admin_name='admin', notes='note')


class ActiveIntegrationTests(unittest.TestCase):
    def test_absent_cog_is_no_integration(self):
        self.assertIsNone(active_integration(FakeBot(None)))

    def test_disabled_cog_is_no_integration(self):
        disabled = NoStatusMethod()
        disabled.enabled = False
        self.assertIsNone(active_integration(FakeBot(disabled)))

    def test_a_cog_without_an_enabled_flag_is_not_assumed_on(self):
        self.assertIsNone(active_integration(FakeBot(object())))

    def test_an_enabled_cog_comes_back(self):
        cog = NoStatusMethod()
        self.assertIs(active_integration(FakeBot(cog)), cog)


class SyncRequestStatusTests(IsolatedAsyncioTestCase):
    async def test_a_missing_method_is_a_warning_not_an_exception(self):
        with self.assertLogs("cogs.requests.ggr_sync", level="WARNING"):
            result = await sync_request_status(
                FakeBot(NoStatusMethod()), FakeRepo(), 1, **SYNC
            )
        self.assertFalse(result)

    async def test_a_failing_call_is_an_error_not_an_exception(self):
        with self.assertLogs("cogs.requests.ggr_sync", level="ERROR"):
            result = await sync_request_status(FakeBot(Exploding()), FakeRepo(), 1, **SYNC)
        self.assertFalse(result)

    async def test_no_integration_is_a_quiet_no(self):
        self.assertFalse(await sync_request_status(FakeBot(None), FakeRepo(), 1, **SYNC))

    async def test_a_request_never_synced_there_is_a_quiet_no(self):
        cog = Working({'success': True})
        result = await sync_request_status(FakeBot(cog), FakeRepo(None), 1, **SYNC)
        self.assertFalse(result)
        self.assertEqual(cog.calls, [])

    async def test_a_confirmed_update_is_a_yes(self):
        cog = Working({'success': True})
        self.assertTrue(await sync_request_status(FakeBot(cog), FakeRepo(99), 1, **SYNC))
        self.assertEqual(cog.calls, [dict(ggr_request_id=99, **SYNC)])

    async def test_ggrequestz_reporting_failure_is_a_no(self):
        cog = Working({'success': False, 'error': 'nope'})
        with self.assertLogs("cogs.requests.ggr_sync", level="ERROR"):
            self.assertFalse(await sync_request_status(FakeBot(cog), FakeRepo(99), 1, **SYNC))


class FetchRequestTests(IsolatedAsyncioTestCase):
    async def test_a_missing_method_yields_no_request(self):
        with self.assertLogs("cogs.requests.ggr_sync", level="WARNING"):
            self.assertIsNone(await fetch_request(NoStatusMethod(), 99))

    async def test_the_id_is_passed_positionally(self):
        class Fetcher:
            enabled = True

            def __init__(self):
                self.args = None

            async def get_request_by_id(self, *args, **kwargs):
                self.args = (args, kwargs)
                return {'status': 'fulfilled'}

        cog = Fetcher()
        self.assertEqual(await fetch_request(cog, 99), {'status': 'fulfilled'})
        # The method does not exist yet, so its parameter name is not ours to guess.
        self.assertEqual(cog.args, ((99,), {}))


class TheMissingMethodsAreStillMissingTests(unittest.TestCase):
    """A ratchet on the two methods the guards above exist for.

    When either is implemented on GGRequestzIntegration this fails, which is
    the prompt to drop the getattr() indirection in ggr_sync._call and let the
    call be a plain call again. tests/test_cog_lookups.py cannot see these,
    because reaching them through getattr() is not an attribute access.
    """

    def test_update_request_status_and_get_request_by_id_are_not_implemented(self):
        from integrations.ggrequestz import GGRequestzIntegration

        for name in ("update_request_status", "get_request_by_id"):
            with self.subTest(method=name):
                self.assertFalse(
                    hasattr(GGRequestzIntegration, name),
                    f"GGRequestzIntegration.{name} exists now - remove the "
                    "getattr() guard in cogs/requests/ggr_sync.py and this test.",
                )
