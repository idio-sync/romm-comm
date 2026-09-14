"""Resolving RomM's platform list into hosted content and offerable devices.

The payload these read is the sanitized one bot.py caches, which keeps name
and custom_name and drops slug - so matching is by name, exactly as the
Switch check this replaces did. The custom_name case is the one most likely
to regress invisibly: a server whose admin renamed "Nintendo Switch" to
"Switch (Modded)" must still be offered Tinfoil.

The other thing pinned here is that hosting content and owning hardware are
different questions. A Vita is worth offering to someone whose server holds
only PSP games, because a Vita running pkgj is how those get installed.
"""

import unittest

from cogs.feeds.availability import (
    available_device_keys,
    available_devices,
    hosted_content_keys,
)


def platform(name, custom_name=None):
    """One entry shaped like bot.sanitize_data's platforms branch."""
    return {
        "id": 1,
        "name": name,
        "custom_name": custom_name,
        "display_name": custom_name or name,
        "rom_count": 10,
    }


class ContentMatchingTests(unittest.TestCase):
    def test_a_platform_named_for_the_content_matches_it(self):
        self.assertEqual(
            hosted_content_keys([platform("Nintendo Switch")]),
            frozenset({"switch"}),
        )

    def test_a_custom_name_matches_when_the_real_name_does_not(self):
        keys = hosted_content_keys([platform("Some Internal Slug", "Switch (Modded)")])
        self.assertEqual(keys, frozenset({"switch"}))

    def test_matching_ignores_case_and_surrounding_whitespace(self):
        self.assertEqual(
            hosted_content_keys([platform("  nintendo SWITCH  ")]),
            frozenset({"switch"}),
        )

    def test_unrelated_platforms_match_nothing(self):
        keys = hosted_content_keys([platform("Nintendo 64"), platform("Sega Genesis")])
        self.assertEqual(keys, frozenset())

    def test_several_platforms_resolve_to_several_content_keys(self):
        keys = hosted_content_keys([
            platform("Nintendo Switch"),
            platform("PlayStation Vita"),
            platform("Nintendo DS"),
        ])
        self.assertEqual(keys, frozenset({"switch", "psvita", "nds"}))


class OverMatchingTests(unittest.TestCase):
    """The Sony family and the DS family are where substring matching bites."""

    def test_playstation_3_is_not_also_the_original_playstation(self):
        self.assertEqual(hosted_content_keys([platform("PlayStation 3")]),
                         frozenset({"ps3"}))

    def test_the_original_playstation_matches_only_psx(self):
        self.assertEqual(hosted_content_keys([platform("PlayStation")]),
                         frozenset({"psx"}))

    def test_playstation_portable_is_psp_and_not_psx(self):
        self.assertEqual(hosted_content_keys([platform("PlayStation Portable")]),
                         frozenset({"psp"}))

    def test_nintendo_3ds_is_not_the_ds(self):
        self.assertEqual(hosted_content_keys([platform("Nintendo 3DS")]), frozenset())

    def test_ps5_and_ps4_stay_distinct(self):
        keys = hosted_content_keys([platform("PlayStation 4"), platform("PlayStation 5")])
        self.assertEqual(keys, frozenset({"ps4", "ps5"}))


class HardwareFromContentTests(unittest.TestCase):
    """What the server holds decides what hardware is worth setting up."""

    def test_a_library_of_only_psp_games_still_offers_the_vita(self):
        """The case that motivated splitting content from hardware.

        pkgj runs on a Vita and serves PSP content. Requiring a name match
        on "PlayStation Vita" itself would have hidden the one device that
        can install these games.
        """
        devices = available_devices([platform("PlayStation Portable")])
        keys = [device.key for device in devices]
        self.assertIn("psvita", keys)
        self.assertIn("psp", keys)

    def test_a_library_of_only_psx_games_offers_the_vita_alone(self):
        # Nothing else can install them, and no client runs on a PS1.
        devices = available_devices([platform("PlayStation")])
        self.assertEqual([device.key for device in devices], ["psvita"])

    def test_switch_content_offers_only_the_switch(self):
        self.assertEqual(available_device_keys([platform("Nintendo Switch")]),
                         frozenset({"switch"}))

    def test_content_nothing_can_install_offers_nothing(self):
        self.assertEqual(available_device_keys([platform("Nintendo 64")]), frozenset())

    def test_vita_content_offers_the_vita(self):
        self.assertIn("psvita", available_device_keys([platform("PlayStation Vita")]))


class FallbackTests(unittest.TestCase):
    def test_a_missing_cache_offers_every_device(self):
        # A stale-but-permissive picker beats a command that silently vanishes.
        self.assertEqual(len(available_device_keys(None)), 7)

    def test_an_empty_list_offers_every_device(self):
        self.assertEqual(len(available_device_keys([])), 7)

    def test_a_malformed_entry_is_skipped_rather_than_raising(self):
        keys = hosted_content_keys([{"nonsense": True}, platform("Nintendo Switch")])
        self.assertEqual(keys, frozenset({"switch"}))

    def test_a_null_name_is_survivable(self):
        # sanitize_data defaults name, but custom_name is passed through as
        # None, and the Switch check this replaces guarded for exactly this.
        self.assertEqual(hosted_content_keys([{"name": None, "custom_name": None}]),
                         frozenset())


class OrderingTests(unittest.TestCase):
    def test_available_devices_returns_catalog_order_not_payload_order(self):
        devices = available_devices([platform("Nintendo DS"), platform("Nintendo Switch")])
        self.assertEqual([d.key for d in devices], ["switch", "nds"])

    def test_available_devices_returns_device_objects(self):
        devices = available_devices([platform("Nintendo Switch")])
        self.assertEqual(devices[0].display_name, "Nintendo Switch")
