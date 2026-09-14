"""The /feeds embed, as a pure function.

Three rules worth protecting here.

The credential split: pkgj and pkgi read URLs out of a config file and have
no password box, so their URLs carry user:<password>@, while Tinfoil, fpkgi
and Kekatsu have auth fields of their own and get a bare URL.

The download-auth warning: only Tinfoil cannot authenticate its downloads.
Showing that warning to a pkgj user would be telling them to get their
server weakened to fix a problem they do not have.

The config keys: pkgj's config.txt is key-value lines, so a URL rendered
without `url_psp_games` in front of it is inert when pasted.
"""

import unittest

from cogs.feeds.catalog import DEVICES_BY_KEY, AuthStyle
from cogs.feeds.embeds import (
    PASSWORD_PLACEHOLDER,
    USERNAME_PLACEHOLDER,
    build_device_embed,
    download_auth_notice,
    feed_config_line,
    feed_url,
)
from cogs.feeds.verdict import DownloadAuth

DOMAIN = "https://romm.example"
ALL = frozenset({"switch", "psvita", "psp", "psx", "ps3", "ps4", "ps5", "nds"})

# Unremarkable but long enough that pkgj's five feeds - each carrying the
# full domain plus username in their URL - can push a client's body past
# Discord's 1024-character field cap.
LONG_DOMAIN = "https://roms.mylongishdomain.example.org"


def embed_text(embed):
    """Everything a reader would see, flattened."""
    parts = [embed.title or "", embed.description or ""]
    for field in embed.fields:
        parts.append(field.name or "")
        parts.append(field.value or "")
    if embed.footer:
        parts.append(embed.footer.text or "")
    return "\n".join(parts)


def feed_for(device_key, client_key, path_end):
    return next(
        feed for feed in DEVICES_BY_KEY[device_key].feeds
        if feed.client.key == client_key and feed.path.endswith(path_end)
    )


class UrlTests(unittest.TestCase):
    def test_a_fields_client_gets_a_bare_url(self):
        tinfoil = DEVICES_BY_KEY["switch"].feeds[0]
        self.assertEqual(feed_url(DOMAIN, tinfoil, "ana"), f"{DOMAIN}/api/feeds/tinfoil")

    def test_pkgj_carries_the_username_and_a_password_placeholder(self):
        pkgj = feed_for("psvita", "pkgj", "/psvita/games")
        self.assertEqual(
            feed_url(DOMAIN, pkgj, "ana"),
            f"https://ana:{PASSWORD_PLACEHOLDER}@romm.example/api/feeds/pkgj/psvita/games",
        )

    def test_pkgi_also_embeds_credentials(self):
        # pkgi has no username or password setting either; RomM's docs put
        # its credentials in the config URL, same as pkgj.
        pkgi = feed_for("ps3", "pkgi", "/ps3/game")
        self.assertIn(f"ana:{PASSWORD_PLACEHOLDER}@", feed_url(DOMAIN, pkgi, "ana"))

    def test_pkgj_without_a_link_still_produces_a_usable_template(self):
        """The placeholder stays legible rather than percent-encoded.

        Encoding it turns "<your-romm-username>" into
        "%3Cyour-romm-username%3E", which is exactly the wrong thing to hand
        the one user who has to fill it in by hand.
        """
        pkgj = feed_for("psvita", "pkgj", "/psvita/games")
        url = feed_url(DOMAIN, pkgj, None)
        self.assertIn(USERNAME_PLACEHOLDER, url)
        self.assertIn(PASSWORD_PLACEHOLDER, url)
        self.assertNotIn("%3C", url)

    def test_a_trailing_slash_on_the_domain_does_not_double_up(self):
        tinfoil = DEVICES_BY_KEY["switch"].feeds[0]
        self.assertEqual(
            feed_url("https://romm.example/", tinfoil, "ana"),
            f"{DOMAIN}/api/feeds/tinfoil",
        )

    def test_a_domain_with_no_scheme_still_keeps_its_host(self):
        """DOMAIN is often configured bare.

        The command this replaces rendered it as a **Host:** field, so
        deployments have it without a scheme. urlsplit puts a scheme-less
        string entirely in `path`, which would drop the host from the URL.
        """
        pkgj = feed_for("psvita", "pkgj", "/psvita/games")
        self.assertEqual(
            feed_url("romm.example.com", pkgj, "ana"),
            f"https://ana:{PASSWORD_PLACEHOLDER}@romm.example.com"
            "/api/feeds/pkgj/psvita/games",
        )

    def test_a_scheme_less_domain_works_for_bare_urls_too(self):
        tinfoil = DEVICES_BY_KEY["switch"].feeds[0]
        self.assertEqual(
            feed_url("romm.example.com", tinfoil, "ana"),
            "https://romm.example.com/api/feeds/tinfoil",
        )

    def test_a_username_with_an_at_sign_cannot_repoint_the_host(self):
        """The one place user-controlled text lands in a URL's userinfo.

        Unencoded, "ana@evil.test" would make the host evil.test and send
        the user's password somewhere else entirely.
        """
        pkgj = feed_for("psvita", "pkgj", "/psvita/games")
        url = feed_url(DOMAIN, pkgj, "ana@evil.test")
        self.assertNotIn("@evil.test/", url)
        self.assertIn("ana%40evil.test", url)
        self.assertIn("@romm.example/", url)

    def test_other_reserved_characters_in_a_username_are_encoded(self):
        pkgj = feed_for("psvita", "pkgj", "/psvita/games")
        self.assertIn("a%2Fb%3Ac%3Fd%23e", feed_url(DOMAIN, pkgj, "a/b:c?d#e"))

    def test_an_http_domain_is_left_as_http(self):
        # Some self-hosters run plain HTTP on a LAN; rewriting it would
        # silently produce a URL that does not resolve.
        pkgj = feed_for("psvita", "pkgj", "/psvita/games")
        url = feed_url("http://192.168.1.5:8080", pkgj, "ana")
        self.assertTrue(url.startswith("http://ana:"))

    def test_a_password_placeholder_never_appears_in_a_fields_client_url(self):
        # The mirror of the URL-embedded rule, and the direction a credential
        # could leak into a message that did not need one.
        for device in DEVICES_BY_KEY.values():
            for feed in device.feeds:
                if feed.client.auth_style is not AuthStyle.URL_EMBEDDED:
                    with self.subTest(client=feed.client.key):
                        url = feed_url(DOMAIN, feed, "ana")
                        self.assertNotIn(PASSWORD_PLACEHOLDER, url)
                        self.assertNotIn("ana", url)


class ConfigLineTests(unittest.TestCase):
    """pkgj will not read a URL that has no key in front of it."""

    def test_a_pkgj_line_leads_with_its_config_key(self):
        psp = feed_for("psvita", "pkgj", "/psp/games")
        line = feed_config_line(DOMAIN, psp, "ana")
        self.assertTrue(line.startswith("url_psp_games https://ana:"))

    def test_each_pkgj_feed_gets_its_own_key(self):
        vita = DEVICES_BY_KEY["psvita"]
        lines = [
            feed_config_line(DOMAIN, feed, "ana")
            for feed in vita.feeds if feed.client.key == "pkgj"
        ]
        leading = [line.split(" ", 1)[0] for line in lines]
        self.assertEqual(
            leading,
            ["url_games", "url_dlcs", "url_psp_games", "url_psp_dlcs", "url_psx_games"],
        )

    def test_a_feed_with_no_config_key_renders_the_bare_url(self):
        pkgi = feed_for("ps3", "pkgi", "/ps3/game")
        self.assertEqual(
            feed_config_line(DOMAIN, pkgi, "ana"),
            feed_url(DOMAIN, pkgi, "ana"),
        )


class NoticeTests(unittest.TestCase):
    """Only Tinfoil needs DISABLE_DOWNLOAD_ENDPOINT_AUTH."""

    def test_a_switch_with_auth_enabled_gets_a_warning(self):
        notice = download_auth_notice(DownloadAuth.ENABLED, DEVICES_BY_KEY["switch"])
        self.assertIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", notice)
        self.assertIn("Tinfoil", notice)
        self.assertIn("⚠️", notice)

    def test_a_switch_with_auth_disabled_gets_no_notice(self):
        self.assertIsNone(
            download_auth_notice(DownloadAuth.DISABLED, DEVICES_BY_KEY["switch"])
        )

    def test_a_switch_with_an_unknown_verdict_gets_a_plain_advisory(self):
        notice = download_auth_notice(DownloadAuth.UNKNOWN, DEVICES_BY_KEY["switch"])
        self.assertIsNotNone(notice)
        self.assertIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", notice)
        self.assertNotIn("⚠️", notice)

    def test_a_vita_is_never_warned_whatever_the_verdict(self):
        """pkgj and pkgi authenticate their downloads.

        Warning here would tell a Vita owner to have their admin open up
        the download endpoint for no reason at all.
        """
        for verdict in DownloadAuth:
            with self.subTest(verdict=verdict):
                self.assertIsNone(
                    download_auth_notice(verdict, DEVICES_BY_KEY["psvita"])
                )

    def test_no_non_switch_device_is_ever_warned(self):
        for key, device in DEVICES_BY_KEY.items():
            if key == "switch":
                continue
            for verdict in DownloadAuth:
                with self.subTest(device=key, verdict=verdict):
                    self.assertIsNone(download_auth_notice(verdict, device))


class HostedFilteringTests(unittest.TestCase):
    """Feeds for content the server lacks are dead links, so they are cut."""

    def test_a_vita_on_a_psp_only_server_shows_only_psp_feeds(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED,
            hosted=frozenset({"psp"}),
        )
        text = embed_text(embed)
        self.assertIn("/api/feeds/pkgj/psp/games", text)
        self.assertNotIn("/api/feeds/pkgj/psvita/games", text)
        self.assertNotIn("/api/feeds/pkgj/psx/games", text)

    def test_a_vita_on_a_full_server_shows_everything(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL,
        )
        text = embed_text(embed)
        for path in ("pkgj/psvita/games", "pkgj/psp/games", "pkgj/psx/games"):
            with self.subTest(path=path):
                self.assertIn(path, text)

    def test_filtering_can_drop_a_whole_client(self):
        # A PSX-only server leaves the Vita with pkgj and no pkgi at all.
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED,
            hosted=frozenset({"psx"}),
        )
        names = " ".join(f.name for f in embed.fields)
        self.assertIn("pkgj", names)
        self.assertNotIn("pkgi", names)

    def test_an_unknown_hosted_set_keeps_every_feed(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=None,
        )
        self.assertIn("pkgj/psx/games", embed_text(embed))

    def test_an_empty_hosted_set_falls_back_rather_than_rendering_nothing(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED,
            hosted=frozenset(),
        )
        self.assertTrue(embed.fields)


class EmbedTests(unittest.TestCase):
    def test_a_single_client_device_gets_one_client_field(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        names = [f.name for f in embed.fields]
        self.assertEqual(sum("Tinfoil" in n for n in names), 1)

    def test_vita_shows_both_pkgj_and_pkgi(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        text = embed_text(embed)
        self.assertIn("pkgj", text)
        self.assertIn("pkgi", text)

    def test_a_fields_client_shows_the_username_as_a_separate_line(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        text = embed_text(embed)
        self.assertIn("ana", text)
        self.assertIn("your RomM password", text)

    def test_a_fields_client_never_prints_a_credentialed_url(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        self.assertNotIn("ana:", embed_text(embed))

    def test_an_unlinked_user_gets_a_placeholder_and_a_nudge(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, None, DownloadAuth.DISABLED, hosted=ALL
        )
        self.assertIn(USERNAME_PLACEHOLDER, embed_text(embed))

    def test_the_warning_appears_for_a_switch_when_download_auth_is_on(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.ENABLED, hosted=ALL
        )
        self.assertIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", embed_text(embed))

    def test_no_warning_clutter_when_download_auth_is_off(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        self.assertNotIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", embed_text(embed))

    def test_a_vita_embed_never_mentions_the_flag(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], DOMAIN, "ana", DownloadAuth.ENABLED, hosted=ALL
        )
        self.assertNotIn("DISABLE_DOWNLOAD_ENDPOINT_AUTH", embed_text(embed))

    def test_file_format_limits_are_stated(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["nds"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        self.assertIn(".nds", embed_text(embed))

    def test_client_caveats_reach_the_reader(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["nds"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        self.assertIn("WEP", embed_text(embed))

    def test_pkgi_extra_content_types_are_summarized_not_enumerated(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["ps3"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        text = embed_text(embed)
        self.assertIn("patch", text)
        self.assertLessEqual(text.count("/api/feeds/pkgi/ps3/"), 3)

    def test_the_extra_content_type_line_is_copy_pasteable(self):
        # A bare "/api/feeds/pkgi/ps3/<type>" is not something a user can
        # paste anywhere, so the template carries the domain too.
        embed = build_device_embed(
            DEVICES_BY_KEY["ps3"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        self.assertIn(f"{DOMAIN}/api/feeds/pkgi/ps3/<type>", embed_text(embed))

    def test_tinfoil_gets_its_connection_fields_not_just_a_url(self):
        """Tinfoil's dialog has no URL box.

        It asks for protocol, host, port and path separately - which is what
        the switch_shop_info command this replaces rendered. Emitting only a
        URL would make the replacement worse than the thing replaced.
        """
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED, hosted=ALL
        )
        text = embed_text(embed)
        self.assertIn("**Protocol:** `https`", text)
        self.assertIn("**Host:** `romm.example`", text)
        self.assertIn("**Port:** `443`", text)
        self.assertIn("**Path:** `/api/feeds/tinfoil`", text)

    def test_a_non_default_port_survives_into_the_fields(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], "http://192.168.1.5:8080", "ana",
            DownloadAuth.DISABLED, hosted=ALL,
        )
        text = embed_text(embed)
        self.assertIn("**Port:** `8080`", text)
        self.assertIn("**Protocol:** `http`", text)

    def test_a_supplied_title_overrides_the_device_name(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["switch"], DOMAIN, "ana", DownloadAuth.DISABLED,
            hosted=ALL, title="Nintendo Switch <:switch:1>",
        )
        self.assertIn("<:switch:1>", embed.title)

    def test_every_device_builds_without_raising(self):
        for key, device in DEVICES_BY_KEY.items():
            for verdict in DownloadAuth:
                for username in ("ana", None):
                    with self.subTest(device=key, verdict=verdict, user=username):
                        embed = build_device_embed(
                            device, DOMAIN, username, verdict, hosted=ALL
                        )
                        self.assertTrue(embed.fields)

    def test_no_field_exceeds_discord_s_value_limit(self):
        # A long domain is the harder case: pkgj's five feeds each repeat the
        # full domain and username, which is exactly what pushes a field
        # over the cap in the first place (see PkgjLongDomainTests below).
        for key, device in DEVICES_BY_KEY.items():
            for username in ("ana", None):
                embed = build_device_embed(
                    device, LONG_DOMAIN, username, DownloadAuth.ENABLED, hosted=ALL
                )
                for field in embed.fields:
                    with self.subTest(device=key, username=username, field=field.name):
                        self.assertLessEqual(len(field.value), 1024)

    def test_the_embed_stays_within_discord_s_field_and_size_caps(self):
        # The Vita is the widest device: two clients, seven feeds between
        # them, so it is the one most likely to threaten the 25-field /
        # 6000-character embed limits once continuation fields are added.
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], LONG_DOMAIN, None, DownloadAuth.ENABLED, hosted=ALL
        )
        self.assertLessEqual(len(embed.fields), 25)
        total = len(embed.title or "") + len(embed.description or "")
        total += sum(len(f.name) + len(f.value) for f in embed.fields)
        if embed.footer:
            total += len(embed.footer.text or "")
        self.assertLessEqual(total, 6000)


class PkgjLongDomainTests(unittest.TestCase):
    """Reproduces the truncation the review found: a long domain plus an
    unlinked user used to cut the pkgj field off mid-sentence, dropping the
    one caveat that makes URL_EMBEDDED usable at all - "Replace
    `<your-password>` in each line." Splitting into continuation fields
    instead of truncating is what this class protects.
    """

    def test_the_password_caveat_survives_a_long_domain_and_no_link(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], LONG_DOMAIN, None, DownloadAuth.DISABLED, hosted=ALL
        )
        pkgj_text = "\n".join(
            f.value for f in embed.fields if f.name.startswith("pkgj")
        )
        self.assertIn(PASSWORD_PLACEHOLDER, pkgj_text)
        self.assertIn(
            "Replace `<your-password>` in each line.", pkgj_text
        )

    def test_a_split_client_gets_a_labelled_continuation_field(self):
        embed = build_device_embed(
            DEVICES_BY_KEY["psvita"], LONG_DOMAIN, None, DownloadAuth.DISABLED, hosted=ALL
        )
        names = [f.name for f in embed.fields]
        self.assertIn("pkgj — setup", names)
        self.assertIn("pkgj — setup (continued)", names)
