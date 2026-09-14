"""What each netplay state renders, and what must never appear in it.

render_key is tested alongside the embed because they have to agree: if the
key omits something the embed shows, the post silently stops updating when
only that field changes.
"""

import unittest

from cogs.netplay.embeds import (
    ENDED_HINT,
    STALE_NOTE,
    build_netplay_embed,
    render_key,
)
from cogs.netplay.watcher import NetplayState, NetplayWatcher

ROOM = {"r1": {"room_name": "Bomberman", "current": 2, "max": 4,
               "player_name": "idiosync", "hasPassword": False}}


def make_watcher(**overrides):
    values = {
        "rom_id": 50265,
        "rom_name": "Super Bomberman",
        "requester_id": 1,
        "requester_name": "alice",
        "channel_id": 2,
        "created_at": 1000.0,
    }
    values.update(overrides)
    return NetplayWatcher(**values)


def visible(embed):
    """Every string the reader sees, wherever the layout happens to put it.

    The sanitisation guarantees are about the rendered post, not about one
    field, so these assertions survive a re-layout instead of pinning the
    design in place.
    """
    parts = [embed.title or "", embed.description or ""]
    if embed.author:
        parts.append(embed.author.name or "")
    if embed.footer:
        parts.append(embed.footer.text or "")
    parts += [f"{f.name}{f.value}" for f in embed.fields]
    return "\n".join(parts)


def build(watcher, **kwargs):
    kwargs.setdefault("domain", "https://roms.example.com")
    return build_netplay_embed(watcher, **kwargs)


class RenderKeyTests(unittest.TestCase):
    def test_state_change_changes_the_key(self):
        pending = render_key(make_watcher())
        live = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        self.assertNotEqual(pending, live)

    def test_player_count_change_changes_the_key(self):
        before = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        grown = {"r1": dict(ROOM["r1"], current=3)}
        after = render_key(make_watcher(state=NetplayState.LIVE, rooms=grown))
        self.assertNotEqual(before, after)

    def test_identical_watchers_share_a_key(self):
        a = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        b = render_key(make_watcher(state=NetplayState.LIVE, rooms=dict(ROOM)))
        self.assertEqual(a, b)

    def test_going_stale_changes_the_key(self):
        """Otherwise the warning is computed but never actually delivered."""
        fresh = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        stale = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        stale.stale = True
        self.assertNotEqual(fresh, render_key(stale))

    def test_room_name_change_changes_the_key(self):
        before = render_key(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        renamed = {"r1": dict(ROOM["r1"], room_name="Different")}
        after = render_key(make_watcher(state=NetplayState.LIVE, rooms=renamed))
        self.assertNotEqual(before, after)


class EmbedTests(unittest.TestCase):
    def test_pending_names_the_game_and_links_the_player(self):
        embed = build(make_watcher())
        self.assertIn("Super Bomberman", embed.title)
        self.assertIn("https://roms.example.com/rom/50265/ejs", embed.description)

    def test_live_shows_host_and_seats(self):
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        body = embed.description + "".join(f.value for f in embed.fields)
        self.assertIn("idiosync", body)
        self.assertIn("2 of 4", body)

    def test_password_protected_room_is_marked(self):
        locked = {"r1": dict(ROOM["r1"], hasPassword=True)}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=locked))
        self.assertIn("🔒", visible(embed))

    def test_host_is_never_rendered_as_a_mention(self):
        """player_name is client-supplied and was spoofed in testing."""
        spoofed = {"r1": dict(ROOM["r1"], player_name="<@1234567890>")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=spoofed))
        body = embed.description + "".join(f.value for f in embed.fields)
        self.assertNotIn("<@1234567890>", body)

    def test_host_is_never_rendered_as_a_masked_link(self):
        """Brackets survive the other strips and would publish a live link."""
        spoofed = {"r1": dict(ROOM["r1"],
                              player_name="[Download the patch](https://evil.example)")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=spoofed))
        self.assertNotIn("[", visible(embed))
        self.assertNotIn("]", visible(embed))

    def test_room_name_is_never_rendered_as_a_masked_link(self):
        spoofed = {"r1": dict(ROOM["r1"],
                              room_name="[click me](https://evil.example)")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=spoofed))
        self.assertNotIn("](", visible(embed))

    def test_newlines_in_player_name_do_not_inject_rooms(self):
        """Newlines in player_name must be stripped to prevent visual line injection."""
        # A spoofed name with newline that would fake another room without sanitization
        injected = {"r1": dict(ROOM["r1"], player_name="idiosync\n**FAKE ROOM** - 1/1")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=injected))
        # The guarantee is that a hostile name cannot ADD a line - the text
        # itself may survive, defanged, but it must not become a second row
        # that reads as another room. Counting lines against a clean render
        # is what actually tests that; counting "hosted by" would not, since
        # an injected line would not contain the phrase either.
        clean = build(make_watcher(state=NetplayState.LIVE, rooms=ROOM))
        self.assertEqual(
            embed.description.count("\n"), clean.description.count("\n")
        )

    def test_multiple_rooms_all_render(self):
        two = {"r1": ROOM["r1"], "r2": dict(ROOM["r1"], room_name="Second")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=two))
        body = "".join(f.value for f in embed.fields)
        self.assertIn("Bomberman", body)
        self.assertIn("Second", body)

    def test_a_stale_live_session_says_so(self):
        watcher = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        watcher.stale = True
        self.assertIn(STALE_NOTE, build(watcher).description)

    def test_a_healthy_live_session_carries_no_warning(self):
        watcher = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        self.assertNotIn(STALE_NOTE, build(watcher).description)

    def test_a_stale_pending_session_says_so(self):
        """Saying "no room is open yet" with RomM unreachable is a claim
        we have no evidence for."""
        watcher = make_watcher()
        watcher.stale = True
        self.assertIn(STALE_NOTE, build(watcher).description)

    def test_a_stale_expired_session_says_so(self):
        watcher = make_watcher(state=NetplayState.EXPIRED)
        watcher.stale = True
        self.assertIn(STALE_NOTE, build(watcher).description)

    def test_a_healthy_pending_session_carries_no_warning(self):
        self.assertNotIn(STALE_NOTE, build(make_watcher()).description)

    def test_ended_tells_the_reader_how_to_start_another(self):
        embed = build(make_watcher(state=NetplayState.ENDED))
        self.assertIn(ENDED_HINT, embed.footer.text)

    def test_expired_says_no_session_started(self):
        embed = build(make_watcher(state=NetplayState.EXPIRED))
        self.assertIn("no session started", visible(embed).lower())

    def test_core_is_shown_when_known(self):
        embed = build(make_watcher(), core_name="snes9x")
        body = "".join(f.value for f in embed.fields)
        self.assertIn("snes9x", body)

    def test_missing_cover_art_is_not_fatal(self):
        embed = build(make_watcher(cover_url=None))
        self.assertIsNotNone(embed)

    def test_cover_art_is_used_when_present(self):
        embed = build(make_watcher(cover_url="https://example.com/c.png"))
        self.assertEqual(embed.thumbnail.url, "https://example.com/c.png")

    def test_platform_is_shown_when_known(self):
        embed = build(make_watcher(platform_display="SNES ⭐"))
        self.assertIn("SNES ⭐", [f.value for f in embed.fields])


if __name__ == "__main__":
    unittest.main()


class RosterTests(unittest.TestCase):
    """Who is playing, gathered from Discord rather than from RomM.

    RomM cannot supply this: /netplay/list returns the owner and a count, and
    the socket event that carries the roster is scoped to the room, so hearing
    it would mean calling join-room and occupying one of max_players. The
    roster here is who pressed the button, which Discord authenticates.
    """

    def test_the_label_conjugates_with_the_state(self):
        for state, label in (
            (NetplayState.PENDING, "Waiting"),
            (NetplayState.LIVE, "Playing"),
            (NetplayState.ENDED, "Played"),
        ):
            with self.subTest(state=state):
                w = make_watcher(state=state, rooms=ROOM if state is NetplayState.LIVE else {})
                w.roster = [4242]
                names = [f.name for f in build(w).fields]
                self.assertIn(label, names)

    def test_members_render_as_mentions(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        w.roster = [11, 22]
        field = next(f for f in build(w).fields if f.name == "Playing")
        self.assertEqual(field.value, "<@11> · <@22>")

    def test_an_empty_roster_shows_no_field(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        self.assertNotIn("Playing", [f.name for f in build(w).fields])

    def test_platform_and_roster_are_both_inline(self):
        """Two inline fields share a row; one alone would waste it."""
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM,
                         platform_display="SNES 🎮")
        w.roster = [11]
        inline = {f.name: f.inline for f in build(w).fields}
        self.assertTrue(inline["Platform"])
        self.assertTrue(inline["Playing"])

    def test_the_roster_is_in_the_render_key(self):
        """Otherwise the field is computed and never delivered."""
        empty = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        joined = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        joined.roster = [11]
        self.assertNotEqual(render_key(empty), render_key(joined))


class TimeTests(unittest.TestCase):
    def test_a_live_session_says_when_it_opened(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        w.live_since = 1700000000.0
        self.assertIn("<t:1700000000:R>", build(w).description)

    def test_a_pending_session_says_when_it_was_announced(self):
        w = make_watcher(created_at=1700000000.0)
        self.assertIn("<t:1700000000:R>", build(w).description)

    def test_an_ended_session_reports_how_long_it_ran(self):
        w = make_watcher(state=NetplayState.ENDED)
        w.live_since = 1700000000.0
        w.ended_at = 1700000000.0 + (38 * 60)
        self.assertIn("38 minutes", build(w).description)

    def test_the_relative_stamp_stays_out_of_the_render_key(self):
        """It re-renders client-side; in the key it would churn edits forever."""
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        w.live_since = 1700000000.0
        self.assertNotIn("1700000000", render_key(w))


class LayoutTests(unittest.TestCase):
    def test_the_title_is_just_the_game(self):
        self.assertEqual(build(make_watcher()).title, "Super Bomberman")

    def test_the_state_moves_to_the_author_line(self):
        self.assertIn("live", build(make_watcher(state=NetplayState.LIVE,
                                                 rooms=ROOM)).author.name.lower())

    def test_seats_lead_the_description(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        self.assertTrue(build(w).description.startswith("**2 seats open**"))

    def test_a_full_room_says_so(self):
        full = {"r1": dict(ROOM["r1"], current=4, max=4)}
        w = make_watcher(state=NetplayState.LIVE, rooms=full)
        self.assertIn("Full", build(w).description)

    def test_one_seat_is_singular(self):
        one = {"r1": dict(ROOM["r1"], current=3, max=4)}
        w = make_watcher(state=NetplayState.LIVE, rooms=one)
        self.assertIn("**1 seat open**", build(w).description)

    def test_a_single_room_folds_into_the_description(self):
        w = make_watcher(state=NetplayState.LIVE, rooms=ROOM)
        self.assertNotIn("Room", [f.name for f in build(w).fields])
        self.assertIn("Bomberman", build(w).description)

    def test_several_rooms_keep_their_own_field(self):
        two = {"r1": ROOM["r1"], "r2": dict(ROOM["r1"], room_name="Second")}
        w = make_watcher(state=NetplayState.LIVE, rooms=two)
        self.assertIn("Rooms (2)", [f.name for f in build(w).fields])
