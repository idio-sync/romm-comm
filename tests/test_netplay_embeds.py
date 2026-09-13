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
        self.assertIn("2/4", body)

    def test_password_protected_room_is_marked(self):
        locked = {"r1": dict(ROOM["r1"], hasPassword=True)}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=locked))
        body = "".join(f.value for f in embed.fields)
        self.assertIn("🔒", body)

    def test_host_is_never_rendered_as_a_mention(self):
        """player_name is client-supplied and was spoofed in testing."""
        spoofed = {"r1": dict(ROOM["r1"], player_name="<@1234567890>")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=spoofed))
        body = embed.description + "".join(f.value for f in embed.fields)
        self.assertNotIn("<@1234567890>", body)

    def test_newlines_in_player_name_do_not_inject_rooms(self):
        """Newlines in player_name must be stripped to prevent visual line injection."""
        # A spoofed name with newline that would fake another room without sanitization
        injected = {"r1": dict(ROOM["r1"], player_name="idiosync\n**FAKE ROOM** - 1/1")}
        embed = build(make_watcher(state=NetplayState.LIVE, rooms=injected))
        # The room field should be a single line (no newlines that could fake rooms)
        room_field = [f.value for f in embed.fields if f.name.startswith("Room")][0]
        self.assertEqual(room_field.count("\n"), 0)

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

    def test_ended_tells_the_reader_how_to_start_another(self):
        embed = build(make_watcher(state=NetplayState.ENDED))
        self.assertIn(ENDED_HINT, embed.description)

    def test_expired_says_no_session_started(self):
        embed = build(make_watcher(state=NetplayState.EXPIRED))
        self.assertIn("No session started", embed.description)

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
