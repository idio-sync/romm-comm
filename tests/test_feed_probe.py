"""Detecting whether this RomM server's download endpoint requires auth.

The single most common way a correct feed setup still fails: the feed itself
authenticates fine, then every download 403s because
DISABLE_DOWNLOAD_ENDPOINT_AUTH is not set. The probe answers that before the
user wires anything up.

The probe must be credential-free - a probe that carries the bot's own token
always sees 200 and would cheerfully report "no auth required" on a locked
server, which is the exact wrong answer. That property has its own test.
"""

import unittest
from unittest import mock

from cogs.feeds import probe
from cogs.feeds.probe import (
    DownloadAuth,
    classify,
    detect_download_auth,
    first_rom,
    is_probeable,
)


class ClassifyTests(unittest.TestCase):
    def test_unauthorized_means_download_auth_is_on(self):
        self.assertIs(classify(401), DownloadAuth.ENABLED)

    def test_forbidden_means_download_auth_is_on(self):
        self.assertIs(classify(403), DownloadAuth.ENABLED)

    def test_ok_means_download_auth_is_off(self):
        self.assertIs(classify(200), DownloadAuth.DISABLED)

    def test_partial_content_means_download_auth_is_off(self):
        # The probe sends Range: bytes=0-0, so 206 is the expected success.
        self.assertIs(classify(206), DownloadAuth.DISABLED)

    def test_a_server_error_tells_us_nothing(self):
        self.assertIs(classify(500), DownloadAuth.UNKNOWN)

    def test_a_not_found_tells_us_nothing(self):
        # The ROM we picked may have been deleted between listing and probing.
        self.assertIs(classify(404), DownloadAuth.UNKNOWN)

    def test_no_status_at_all_tells_us_nothing(self):
        self.assertIs(classify(None), DownloadAuth.UNKNOWN)

    def test_a_redirect_counts_as_auth_being_on(self):
        """A 302 to a login page is what an auth proxy in front of RomM does.

        Followed, it would land on a 200 HTML login form and be read as
        "downloads are open" - suppressing the warning on exactly the
        servers that most need it. So the probe does not follow redirects,
        and a 3xx is treated as the challenge it is.
        """
        self.assertIs(classify(302), DownloadAuth.ENABLED)
        self.assertIs(classify(303), DownloadAuth.ENABLED)
        self.assertIs(classify(307), DownloadAuth.ENABLED)


class RomPayloadTests(unittest.TestCase):
    """RomM returns two shapes for this endpoint depending on version."""

    def test_a_paged_payload_yields_its_first_item(self):
        payload = {"items": [{"id": 7, "fs_name": "a.nsp"}], "total": 1}
        self.assertEqual(first_rom(payload)["id"], 7)

    def test_a_bare_list_yields_its_first_entry(self):
        self.assertEqual(first_rom([{"id": 9, "fs_name": "b.nsp"}])["id"], 9)

    def test_an_empty_library_yields_nothing(self):
        self.assertIsNone(first_rom([]))
        self.assertIsNone(first_rom({"items": []}))
        self.assertIsNone(first_rom(None))

    def test_a_shape_we_do_not_recognize_yields_nothing(self):
        self.assertIsNone(first_rom({"unexpected": "shape"}))


class ProbeableDomainTests(unittest.TestCase):
    def test_a_real_domain_is_probeable(self):
        self.assertTrue(is_probeable("https://romm.example"))

    def test_the_unset_domain_placeholder_is_not(self):
        # bot.py defaults DOMAIN to this literal string.
        self.assertFalse(is_probeable("No website configured"))

    def test_an_empty_domain_is_not(self):
        self.assertFalse(is_probeable(""))
        self.assertFalse(is_probeable(None))


class DetectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requested = []

    def recorder(self, status):
        async def http_get(url):
            self.requested.append(url)
            return status
        return http_get

    async def fetch_one_rom(self, endpoint, **kwargs):
        return {"items": [{"id": 42, "fs_name": "Some Game.nsp"}]}

    async def fetch_nothing(self, endpoint, **kwargs):
        return []

    async def test_a_401_from_the_content_url_reports_auth_enabled(self):
        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, self.recorder(401)
        )
        self.assertIs(verdict, DownloadAuth.ENABLED)

    async def test_a_206_reports_auth_disabled(self):
        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, self.recorder(206)
        )
        self.assertIs(verdict, DownloadAuth.DISABLED)

    async def test_the_probed_url_is_the_rom_content_endpoint(self):
        await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, self.recorder(200)
        )
        self.assertEqual(
            self.requested,
            ["https://romm.example/api/roms/42/content/Some+Game.nsp"],
        )

    async def test_an_empty_library_is_unknown_and_issues_no_request(self):
        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_nothing, self.recorder(200)
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)
        self.assertEqual(self.requested, [])

    async def test_a_scheme_less_domain_still_produces_a_valid_request(self):
        """DOMAIN is frequently configured as a bare host.

        build_rom_download_url does no normalizing of its own, so passing
        the raw DOMAIN would request something unresolvable and leave the
        verdict stuck on UNKNOWN forever - and it has to match what the
        embed shows, or the probe answers a question about a URL no user
        will ever visit.
        """
        await detect_download_auth(
            "romm.example.com", self.fetch_one_rom, self.recorder(206)
        )
        self.assertEqual(
            self.requested,
            ["https://romm.example.com/api/roms/42/content/Some+Game.nsp"],
        )

    async def test_a_placeholder_domain_is_unknown_and_issues_no_request(self):
        verdict = await detect_download_auth(
            "No website configured", self.fetch_one_rom, self.recorder(200)
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)
        self.assertEqual(self.requested, [])

    async def test_a_transport_failure_is_unknown_rather_than_a_crash(self):
        async def explode(url):
            raise OSError("connection refused")

        verdict = await detect_download_auth(
            "https://romm.example", self.fetch_one_rom, explode
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)

    async def test_a_failure_listing_roms_is_unknown_rather_than_a_crash(self):
        async def explode(endpoint, **kwargs):
            raise OSError("connection refused")

        verdict = await detect_download_auth(
            "https://romm.example", explode, self.recorder(200)
        )
        self.assertIs(verdict, DownloadAuth.UNKNOWN)


class _FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, calls, **kwargs):
        self.calls = calls
        self.init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(206)


class CredentialFreedomTests(unittest.IsolatedAsyncioTestCase):
    """The property the whole probe depends on.

    A probe carrying the bot's credentials sees 200 on a locked server and
    reports "no auth needed" - telling every user their downloads will work,
    right before none of them do.

    This has to exercise _aiohttp_get itself. Asserting against an injected
    fake http_get would prove nothing: the fake is not what runs in
    production, and such a test passes just as happily if the real function
    attaches a bearer token.
    """

    async def test_the_real_request_sends_only_a_range_header(self):
        calls = []

        with mock.patch.object(
            probe.aiohttp, "ClientSession", lambda **kw: _FakeSession(calls, **kw)
        ):
            status = await probe._aiohttp_get(
                "https://romm.example/api/roms/1/content/x.nsp"
            )

        self.assertEqual(status, 206)
        _url, kwargs = calls[0]
        headers = kwargs.get("headers") or {}
        self.assertEqual({key.lower() for key in headers}, {"range"})

    async def test_the_real_request_passes_no_auth_and_no_cookies(self):
        calls = []

        with mock.patch.object(
            probe.aiohttp, "ClientSession", lambda **kw: _FakeSession(calls, **kw)
        ):
            await probe._aiohttp_get("https://romm.example/api/roms/1/content/x.nsp")

        _url, kwargs = calls[0]
        self.assertIsNone(kwargs.get("auth"))
        self.assertIsNone(kwargs.get("cookies"))

    async def test_the_real_request_does_not_follow_redirects(self):
        calls = []

        with mock.patch.object(
            probe.aiohttp, "ClientSession", lambda **kw: _FakeSession(calls, **kw)
        ):
            await probe._aiohttp_get("https://romm.example/api/roms/1/content/x.nsp")

        _url, kwargs = calls[0]
        self.assertIs(kwargs.get("allow_redirects"), False)
