"""Integrity checks for the feed catalog.

The catalog is a hand-maintained table of URLs. A typo in a path does not
fail anywhere in the bot - it reaches the user as a 404 from their console,
which is the worst possible place to discover it. These tests check shape
rather than behavior: every path well-formed, every device reachable, every
client referenced by a feed actually declared.

Two facts here are load-bearing and easy to get wrong from memory. Only
Tinfoil is unable to authenticate its downloads, and pkgj will not read a
config line that has no key in front of the URL.
"""

import unittest
from dataclasses import FrozenInstanceError

from cogs.feeds.catalog import (
    CONTENT_PLATFORMS,
    DEVICES,
    DEVICES_BY_KEY,
    AuthStyle,
    Device,
)


class CatalogShapeTests(unittest.TestCase):
    def test_every_device_offers_at_least_one_feed(self):
        for device in DEVICES:
            with self.subTest(device=device.key):
                self.assertTrue(device.feeds, f"{device.key} has no feeds")

    def test_every_feed_path_is_a_romm_feed_route(self):
        for device in DEVICES:
            for feed in device.feeds:
                with self.subTest(device=device.key, path=feed.path):
                    self.assertTrue(feed.path.startswith("/api/feeds/"))
                    self.assertFalse(feed.path.endswith("/"))

    def test_device_keys_are_unique(self):
        keys = [device.key for device in DEVICES]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_lookup_table_matches_the_device_tuple(self):
        self.assertEqual(set(DEVICES_BY_KEY), {device.key for device in DEVICES})
        for key, device in DEVICES_BY_KEY.items():
            self.assertEqual(device.key, key)

    def test_every_feed_names_a_content_platform_that_exists(self):
        known = {platform.key for platform in CONTENT_PLATFORMS}
        for device in DEVICES:
            for feed in device.feeds:
                with self.subTest(device=device.key, content=feed.content):
                    self.assertIn(feed.content, known)

    def test_every_content_platform_can_be_matched_by_something(self):
        # One with neither exact names nor fragments could never be resolved
        # from a platform list, so it would be invisible forever.
        for platform in CONTENT_PLATFORMS:
            with self.subTest(platform=platform.key):
                self.assertTrue(platform.exact_names or platform.name_fragments)

    def test_all_five_clients_are_represented(self):
        clients = {feed.client.key for device in DEVICES for feed in device.feeds}
        self.assertEqual(clients, {"tinfoil", "pkgj", "pkgi", "fpkgi", "kekatsu"})

    def test_only_url_reading_clients_embed_credentials(self):
        # pkgj and pkgi read URLs out of a config file and have no password
        # box. Anything else acquiring URL_EMBEDDED is a decision someone
        # must make deliberately, not a default that drifted.
        embedding = {
            feed.client.key
            for device in DEVICES
            for feed in device.feeds
            if feed.client.auth_style is AuthStyle.URL_EMBEDDED
        }
        self.assertEqual(embedding, {"pkgj", "pkgi"})

    def test_devices_are_frozen(self):
        # Named explicitly rather than as a blind `Exception`: ruff's B017
        # rejects that, and the repo has B selected.
        with self.assertRaises(FrozenInstanceError):
            DEVICES[0].key = "mutated"


class DownloadAuthTests(unittest.TestCase):
    """Which clients actually need DISABLE_DOWNLOAD_ENDPOINT_AUTH."""

    def test_tinfoil_is_the_only_client_that_needs_the_flag(self):
        """RomM documents the rest as authenticating both requests.

        Warning a pkgj or fpkgi user that they need the server's download
        auth turned off would be telling them to get a server weakened to
        fix a problem they do not have.
        """
        needing = {
            feed.client.key
            for device in DEVICES
            for feed in device.feeds
            if feed.client.needs_download_auth_disabled
        }
        self.assertEqual(needing, {"tinfoil"})


class ConfigKeyTests(unittest.TestCase):
    """pkgj's config.txt is key-value lines; a bare URL in it does nothing."""

    def test_every_pkgj_feed_carries_its_config_key(self):
        for device in DEVICES:
            for feed in device.feeds:
                if feed.client.key == "pkgj":
                    with self.subTest(path=feed.path):
                        self.assertTrue(feed.config_key)

    def test_the_documented_pkgj_keys_are_all_present(self):
        keys = {
            feed.config_key
            for feed in DEVICES_BY_KEY["psvita"].feeds
            if feed.client.key == "pkgj"
        }
        self.assertEqual(
            keys,
            {"url_games", "url_dlcs", "url_psp_games", "url_psp_dlcs", "url_psx_games"},
        )

    def test_pkgi_carries_no_config_key(self):
        # Every pkgi fork names its keys differently, so the catalog does not
        # guess - the client's caveat sends the user to their fork's README.
        for device in DEVICES:
            for feed in device.feeds:
                if feed.client.key == "pkgi":
                    with self.subTest(path=feed.path):
                        self.assertIsNone(feed.config_key)


class ContentTypeTests(unittest.TestCase):
    def test_extra_content_types_only_hang_off_pkgi_feeds(self):
        for device in DEVICES:
            for feed in device.feeds:
                if feed.extra_content_types:
                    with self.subTest(device=device.key):
                        self.assertEqual(feed.client.key, "pkgi")

    def test_the_vita_pkgi_game_feed_lists_its_extra_types(self):
        vita = DEVICES_BY_KEY["psvita"]
        game_feed = next(
            feed for feed in vita.feeds
            if feed.client.key == "pkgi" and feed.path.endswith("/game")
        )
        self.assertIn("translation", game_feed.extra_content_types)
        self.assertIn("prototype", game_feed.extra_content_types)

    def test_ps3_pkgi_offers_fewer_extra_types_than_vita(self):
        # PS3 and PSP accept five content types; Vita accepts eleven.
        ps3_extra = {t for feed in DEVICES_BY_KEY["ps3"].feeds
                     for t in feed.extra_content_types}
        vita_extra = {t for feed in DEVICES_BY_KEY["psvita"].feeds
                      for t in feed.extra_content_types}
        self.assertTrue(ps3_extra < vita_extra)


class HardwareVersusContentTests(unittest.TestCase):
    """The distinction the catalog exists to keep straight."""

    def test_the_seven_expected_devices_are_present(self):
        self.assertEqual(
            set(DEVICES_BY_KEY),
            {"switch", "psvita", "psp", "ps3", "ps4", "ps5", "nds"},
        )

    def test_psx_is_content_but_never_a_device(self):
        """No homebrew installer runs on an original PlayStation.

        PSX games are something a Vita pulls down through pkgj, so offering
        a "PlayStation" device would invite a user to set up hardware that
        cannot run any of this.
        """
        self.assertNotIn("psx", DEVICES_BY_KEY)
        self.assertIn("psx", {platform.key for platform in CONTENT_PLATFORMS})

    def test_the_vita_carries_psp_and_psx_content(self):
        # pkgj runs only on a Vita, and this is where its PSP and PSX feeds
        # belong as a result.
        self.assertEqual(
            set(DEVICES_BY_KEY["psvita"].content_keys),
            {"psvita", "psp", "psx"},
        )

    def test_pkgj_appears_on_no_device_but_the_vita(self):
        for key, device in DEVICES_BY_KEY.items():
            if key == "psvita":
                continue
            with self.subTest(device=key):
                self.assertNotIn("pkgj", {feed.client.key for feed in device.feeds})

    def test_the_psp_device_runs_pkgi_not_pkgj(self):
        # pkgi does run on a PSP; pkgj does not.
        clients = {feed.client.key for feed in DEVICES_BY_KEY["psp"].feeds}
        self.assertEqual(clients, {"pkgi"})

    def test_content_keys_are_deduplicated_in_order(self):
        self.assertEqual(DEVICES_BY_KEY["ps3"].content_keys, ("ps3",))

    def test_a_device_is_a_dataclass_instance(self):
        self.assertIsInstance(DEVICES_BY_KEY["switch"], Device)


class DocsUrlTests(unittest.TestCase):
    def test_every_client_links_a_distinct_anchor_on_one_page(self):
        # The base already contains the page path; appending it again gave
        # four clients a duplicated, 404-ing URL.
        urls = {
            feed.client.key: feed.client.docs_url
            for device in DEVICES for feed in device.feeds
        }
        for key, url in urls.items():
            with self.subTest(client=key):
                self.assertEqual(url.count("/ecosystem/feed-clients/"), 1)
                self.assertTrue(url.endswith(f"#{key}"))
