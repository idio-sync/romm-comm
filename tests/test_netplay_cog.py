"""Registering a watcher, and the guards that run before one is created.

The command itself needs a live interaction, so these test the pieces it
delegates to: the watcher cap, and that a registered watcher starts PENDING
pointed at the right ROM.
"""

import unittest
from types import SimpleNamespace

import discord

from cogs.netplay.cog import Netplay, NetplayJoinView
from cogs.netplay.embeds import render_key
from cogs.netplay.watcher import NetplayState


def make_cog(max_watchers=25):
    cog = object.__new__(Netplay)
    cog.bot = SimpleNamespace(
        config=SimpleNamespace(
            DOMAIN="https://roms.example.com",
            NETPLAY_MAX_WATCHERS=max_watchers,
            NETPLAY_PENDING_TIMEOUT=900,
            NETPLAY_SESSION_TIMEOUT=3600,
        )
    )
    cog.enabled = True
    cog.server_enabled = True
    cog.watchers = {}
    return cog


ROM = {"id": 50265, "name": "Super Bomberman", "url_cover": "https://x/c.png"}


def register(cog, rom=ROM, requester_id=7):
    return cog.register_watcher(
        rom,
        requester_id=requester_id,
        requester_name="alice",
        channel_id=9,
        platform_display="SNES",
    )


class RegisterWatcherTests(unittest.TestCase):
    def test_new_watcher_starts_pending(self):
        watcher = register(make_cog())
        self.assertIs(watcher.state, NetplayState.PENDING)
        self.assertEqual(watcher.rom_id, 50265)
        self.assertEqual(watcher.rom_name, "Super Bomberman")

    def test_watcher_carries_what_the_embed_needs(self):
        """The poll loop re-renders without a ctx, so this is captured now."""
        watcher = register(make_cog())
        self.assertEqual(watcher.requester_name, "alice")
        self.assertEqual(watcher.platform_display, "SNES")
        self.assertEqual(watcher.cover_url, "https://x/c.png")

    def test_watcher_is_stored_by_rom_id(self):
        cog = make_cog()
        register(cog)
        self.assertIn(50265, cog.watchers)

    def test_at_capacity_is_reported(self):
        cog = make_cog(max_watchers=1)
        register(cog)
        self.assertTrue(cog.at_capacity())

    def test_below_capacity_is_not(self):
        self.assertFalse(make_cog(max_watchers=1).at_capacity())

    def test_a_live_watcher_for_the_rom_is_found(self):
        """Re-announcing must not orphan the first post."""
        cog = make_cog()
        register(cog)
        self.assertIsNotNone(cog.live_watcher_for(50265))

    def test_no_live_watcher_for_an_unannounced_rom(self):
        self.assertIsNone(make_cog().live_watcher_for(50265))

    def test_a_terminal_watcher_does_not_block_re_announcing(self):
        cog = make_cog()
        watcher = register(cog)
        watcher.state = NetplayState.ENDED
        self.assertIsNone(cog.live_watcher_for(50265))


class FakeMessage:
    def __init__(self):
        self.edits = 0
        self.last_edit = None

    async def edit(self, **kwargs):
        self.edits += 1
        self.last_edit = kwargs


class FlakyMessage(FakeMessage):
    """Rejects the first `fail_times` edits the way Discord would."""

    def __init__(self, fail_times):
        super().__init__()
        self.remaining_failures = fail_times

    async def edit(self, **kwargs):
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise discord.HTTPException(
                SimpleNamespace(status=500, reason="Internal Server Error"),
                "rate limited",
            )
        await super().edit(**kwargs)


class ForbiddenMessage(FakeMessage):
    """Rejects every edit the way a lost permission would."""

    async def edit(self, **kwargs):
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "Missing Permissions",
        )


class FakeChannel:
    def __init__(self, message):
        self._message = message

    async def fetch_message(self, message_id):
        return self._message


def make_polling_cog(poll_results, message=None):
    """A cog whose room polls return canned results, one per tick."""
    cog = make_cog()
    cog.bot.config.DOMAIN = "https://roms.example.com"
    cog._results = list(poll_results)
    message = message or FakeMessage()
    cog._message = message

    async def list_rooms(rom_id):
        return cog._results.pop(0) if cog._results else {}

    cog.bot.romm = SimpleNamespace(list_netplay_rooms=list_rooms)
    cog.bot.get_channel = lambda cid: FakeChannel(message)
    return cog


ROOM = {"r1": {"room_name": "Bomberman", "current": 2, "max": 4,
               "player_name": "idiosync", "hasPassword": False}}


class SanitizedRequesterTests(unittest.TestCase):
    def test_requester_name_is_defanged_before_it_is_stored(self):
        """A nickname is member-controlled and renders inside the description."""
        cog = make_cog()
        watcher = cog.register_watcher(
            ROM,
            requester_id=7,
            requester_name="[click](https://evil.example)**",
            channel_id=9,
            platform_display="SNES",
        )
        self.assertNotIn("[", watcher.requester_name)
        self.assertNotIn("]", watcher.requester_name)
        self.assertNotIn("*", watcher.requester_name)


class BeforePollTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_configured_interval_replaces_the_decoration_default(self):
        """tasks.loop fixes 20s at import; only before_loop can change it."""
        cog = make_cog()
        cog.bot.config.NETPLAY_POLL_INTERVAL = 45

        async def wait_until_ready():
            return None

        cog.bot.wait_until_ready = wait_until_ready

        await cog.before_poll()

        self.assertEqual(cog.poll_sessions.seconds, 45)


class TickTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_room_appearing_edits_the_message(self):
        cog = make_polling_cog([ROOM])
        watcher = register(cog)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        self.assertEqual(cog._message.edits, 1)
        self.assertIs(watcher.state, NetplayState.LIVE)

    async def test_an_unchanged_poll_does_not_edit(self):
        """Edit suppression: 25 watchers editing every 20s hits Discord limits."""
        cog = make_polling_cog([ROOM, ROOM])
        watcher = register(cog)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        await cog.tick(now=1020.0)
        self.assertEqual(cog._message.edits, 1)

    async def test_terminal_watchers_are_dropped(self):
        cog = make_polling_cog([ROOM, {}])
        watcher = register(cog)
        watcher.message_id = 1
        await cog.tick(now=1000.0)
        await cog.tick(now=1020.0)
        self.assertEqual(cog.watchers, {})

    async def test_registering_during_a_tick_does_not_raise(self):
        """The loop awaits per watcher; a command can mutate the registry."""
        cog = make_polling_cog([ROOM])
        watcher = register(cog)
        watcher.message_id = 1

        original = cog.bot.romm.list_netplay_rooms

        async def mutate_then_poll(rom_id):
            register(cog, rom={"id": 999, "name": "Other"})
            return await original(rom_id)

        cog.bot.romm.list_netplay_rooms = mutate_then_poll
        await cog.tick(now=1000.0)  # must not raise RuntimeError
        self.assertIn(999, cog.watchers)

    async def test_a_failed_poll_leaves_the_watcher_alone(self):
        cog = make_polling_cog([None])
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = ROOM
        watcher.message_id = 1
        # Already up to date, as it would be straight out of announce().
        watcher.last_render_key = render_key(watcher)
        await cog.tick(now=1000.0)
        self.assertIs(watcher.state, NetplayState.LIVE)
        self.assertEqual(cog._message.edits, 0)

    async def test_a_failed_edit_is_retried_on_the_next_tick(self):
        """The poll may report nothing new; the post is still wrong."""
        message = FlakyMessage(fail_times=1)
        cog = make_polling_cog([ROOM, ROOM], message=message)
        watcher = register(cog)
        watcher.message_id = 1

        await cog.tick(now=1000.0)
        self.assertEqual(message.edits, 0)
        self.assertIsNone(watcher.last_render_key)

        await cog.tick(now=1020.0)
        self.assertEqual(message.edits, 1)

    async def test_a_terminal_watcher_survives_a_failed_final_edit(self):
        """Dropping it would leave the post reading live for a dead session."""
        message = FlakyMessage(fail_times=99)
        cog = make_polling_cog([{}], message=message)
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = ROOM
        watcher.message_id = 1

        await cog.tick(now=1000.0)
        self.assertIs(watcher.state, NetplayState.ENDED)
        self.assertIn(50265, cog.watchers)

    async def test_a_forbidden_edit_drops_the_watcher(self):
        """Lost permissions are permanent: retrying holds a slot forever."""
        cog = make_polling_cog([ROOM], message=ForbiddenMessage())
        watcher = register(cog)
        watcher.message_id = 1

        await cog.tick(now=1000.0)

        self.assertEqual(cog.watchers, {})

    async def test_a_forbidden_edit_does_not_drop_a_replacement_watcher(self):
        """The replacement must land DURING the tick, not before it.

        Registering it up front would evict the original from the registry by
        key, leaving the tick's snapshot holding only the replacement - whose
        message_id is None, so refresh_message returns before the Forbidden
        handler ever runs. The test would then pass without exercising the
        identity check it exists to protect.
        """
        cog = make_polling_cog([ROOM], message=ForbiddenMessage())
        old = register(cog)
        old.message_id = 1

        original = cog.bot.romm.list_netplay_rooms
        replacement = None

        async def replace_then_poll(rom_id):
            nonlocal replacement
            result = await original(rom_id)
            replacement = register(cog)
            cog.watchers[50265] = replacement
            return result

        cog.bot.romm.list_netplay_rooms = replace_then_poll
        await cog.tick(now=1000.0)

        self.assertIs(cog.watchers.get(50265), replacement)
        self.assertIsNot(cog.watchers.get(50265), old)

    async def test_cleanup_does_not_delete_a_replacement_watcher(self):
        """A new /netplay for the same ROM can land during the awaits."""
        cog = make_polling_cog([{}])
        old = register(cog)
        old.state = NetplayState.LIVE
        old.rooms = ROOM
        old.message_id = 1

        original = cog.bot.romm.list_netplay_rooms

        async def replace_then_poll(rom_id):
            result = await original(rom_id)
            cog.watchers[50265] = register(cog)
            return result

        cog.bot.romm.list_netplay_rooms = replace_then_poll
        await cog.tick(now=1000.0)

        self.assertIn(50265, cog.watchers)
        self.assertIsNot(cog.watchers[50265], old)

    async def test_a_watcher_still_publishing_is_skipped(self):
        """message_id is None until announce() finishes sending."""
        cog = make_polling_cog([ROOM])
        watcher = register(cog)
        self.assertIsNone(watcher.message_id)

        await cog.tick(now=1000.0)

        self.assertEqual(cog._message.edits, 0)
        self.assertIsNone(watcher.last_render_key)

    async def test_a_terminal_watcher_with_no_channel_is_dropped(self):
        """A deleted channel is permanent: keep the watcher and it polls forever."""
        cog = make_polling_cog([{}])
        cog.bot.get_channel = lambda cid: None
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = ROOM
        watcher.message_id = 1

        await cog.tick(now=1000.0)

        self.assertEqual(cog.watchers, {})

    async def test_a_watcher_whose_poll_raises_does_not_starve_the_rest(self):
        """One deterministic failure must not skip the rest of the snapshot."""
        cog = make_polling_cog([])
        first = register(cog)
        second = register(cog, rom={"id": 999, "name": "Other"})
        first.message_id = 1
        second.message_id = 2
        polled = []

        async def explode_on_the_first(rom_id):
            polled.append(rom_id)
            if rom_id == first.rom_id:
                raise RuntimeError("boom")
            return ROOM

        cog.bot.romm.list_netplay_rooms = explode_on_the_first

        await cog.tick(now=1000.0)

        self.assertEqual(polled, [first.rom_id, second.rom_id])
        self.assertIs(second.state, NetplayState.LIVE)




class FakeInteraction:
    """Only what the Join callback touches."""

    def __init__(self, user_id):
        self.user = SimpleNamespace(id=user_id)
        self.sent = []

        async def send_message(content=None, **kwargs):
            self.sent.append((content, kwargs))

        self.response = SimpleNamespace(send_message=send_message)


class JoinButtonTests(unittest.IsolatedAsyncioTestCase):
    """The roster RomM cannot give us, gathered from Discord instead."""

    def _cog_and_watcher(self):
        cog = make_cog()
        cog.bot.config.DOMAIN = "https://roms.example.com"
        watcher = register(cog)
        watcher.message_id = 1
        message = FakeMessage()
        cog.bot.get_channel = lambda cid: FakeChannel(message)
        return cog, watcher, message

    async def test_pressing_join_records_the_presser(self):
        cog, watcher, _ = self._cog_and_watcher()
        view = NetplayJoinView(cog, watcher.rom_id)

        await view.on_join(FakeInteraction(4242))

        self.assertEqual(watcher.roster, [4242])

    async def test_the_presser_gets_the_link_privately(self):
        cog, watcher, _ = self._cog_and_watcher()
        view = NetplayJoinView(cog, watcher.rom_id)
        interaction = FakeInteraction(4242)

        await view.on_join(interaction)

        _, kwargs = interaction.sent[0]
        self.assertTrue(kwargs.get("ephemeral"))
        button = kwargs["view"].children[0]
        self.assertIn(f"/rom/{watcher.rom_id}/ejs", button.url)

    async def test_pressing_twice_does_not_duplicate(self):
        cog, watcher, _ = self._cog_and_watcher()
        view = NetplayJoinView(cog, watcher.rom_id)

        await view.on_join(FakeInteraction(4242))
        await view.on_join(FakeInteraction(4242))

        self.assertEqual(watcher.roster, [4242])

    async def test_the_roster_keeps_press_order(self):
        cog, watcher, _ = self._cog_and_watcher()
        view = NetplayJoinView(cog, watcher.rom_id)

        await view.on_join(FakeInteraction(11))
        await view.on_join(FakeInteraction(22))

        self.assertEqual(watcher.roster, [11, 22])

    async def test_pressing_refreshes_the_post_immediately(self):
        """Waiting up to a poll interval to see your own name is poor."""
        cog, watcher, message = self._cog_and_watcher()
        view = NetplayJoinView(cog, watcher.rom_id)

        await view.on_join(FakeInteraction(4242))

        self.assertEqual(message.edits, 1)

    async def test_the_edit_path_keeps_the_button(self):
        """Editing without view= is how a Join button silently disappears."""
        cog = make_polling_cog([ROOM])
        watcher = register(cog)
        watcher.message_id = 1

        await cog.tick(now=1000.0)

        self.assertIsInstance(cog._message.last_edit.get("view"), NetplayJoinView)

    async def test_a_finished_session_drops_the_button(self):
        """Nothing left to join, so the control should not linger."""
        cog = make_polling_cog([ROOM, {}])
        watcher = register(cog)
        watcher.message_id = 1

        await cog.tick(now=1000.0)
        await cog.tick(now=1020.0)

        self.assertIs(watcher.state, NetplayState.ENDED)
        # The key must be PRESENT and None. Asserting only on .get() would
        # pass just as happily against an edit that omits view= entirely,
        # which is the bug the sibling test above exists to catch.
        self.assertIn("view", cog._message.last_edit)
        self.assertIsNone(cog._message.last_edit["view"])

    async def test_a_press_on_a_finished_session_still_answers(self):
        """The watcher is gone, but the button lingers on the old post."""
        cog, watcher, _ = self._cog_and_watcher()
        view = NetplayJoinView(cog, watcher.rom_id)
        cog.watchers.clear()
        interaction = FakeInteraction(4242)

        await view.on_join(interaction)

        self.assertTrue(interaction.sent)


class FakeSentMessage(FakeMessage):
    """A message already on Discord, so it has an id and can be edited."""

    def __init__(self, message_id=777):
        super().__init__()
        self.id = message_id


class FakeCtx:
    """Only what announce() touches."""

    def __init__(self):
        self.author = SimpleNamespace(id=7, display_name="alice")
        self.channel_id = 9
        self.responses = []

    async def respond(self, content=None, **kwargs):
        self.responses.append((content, kwargs))
        return FakeSentMessage(message_id=900 + len(self.responses))


class OneMessagePerCommandTests(unittest.IsolatedAsyncioTestCase):
    """A command leaves exactly one message behind, whichever path it took.

    The picker is sent before we know which ROM was chosen, so the
    announcement has to become that message rather than follow it - otherwise
    a two-match search posts a dead "Which one?" above every embed.
    """

    async def test_the_picker_message_becomes_the_announcement(self):
        cog, ctx = make_cog(), FakeCtx()
        picker = FakeSentMessage()

        await cog.announce(ctx, ROM, "SNES", prompt=picker)

        self.assertEqual(picker.edits, 1)
        self.assertEqual(ctx.responses, [])

    async def test_the_watcher_points_at_the_edited_message(self):
        """Point it at the wrong id and every later refresh edits nothing."""
        cog, ctx = make_cog(), FakeCtx()
        picker = FakeSentMessage(message_id=4321)

        await cog.announce(ctx, ROM, "SNES", prompt=picker)

        self.assertEqual(cog.watchers[50265].message_id, 4321)

    async def test_the_announcement_clears_the_question_and_the_select(self):
        cog, ctx = make_cog(), FakeCtx()
        picker = FakeSentMessage()

        await cog.announce(ctx, ROM, "SNES", prompt=picker)

        self.assertIsNone(picker.last_edit["content"])
        self.assertIsInstance(picker.last_edit["view"], NetplayJoinView)
        self.assertIsNotNone(picker.last_edit["embed"])

    async def test_a_refusal_replaces_the_picker_too(self):
        """A guard that posts its own message would leave the picker sitting."""
        cog, ctx = make_cog(max_watchers=1), FakeCtx()
        register(cog, rom={"id": 1, "name": "Other"})
        picker = FakeSentMessage()

        await cog.announce(ctx, ROM, "SNES", prompt=picker)

        self.assertEqual(ctx.responses, [])
        self.assertIn("as many", picker.last_edit["content"])
        self.assertIsNone(picker.last_edit["view"])

    async def test_without_a_picker_it_still_posts(self):
        """The single-match path has no message to reuse yet."""
        cog, ctx = make_cog(), FakeCtx()

        await cog.announce(ctx, ROM, "SNES")

        self.assertEqual(len(ctx.responses), 1)
        self.assertIsNotNone(cog.watchers[50265].message_id)


class JoinLinkTests(unittest.IsolatedAsyncioTestCase):
    """One click from the reply, and a room named in the link.

    RomM ignores ?room= today. It is emitted anyway: the link then starts
    working the moment the player honours it, with nothing to change here.
    """

    def _cog_and_watcher(self, rooms=None, state=NetplayState.LIVE):
        cog = make_cog()
        cog.bot.config.DOMAIN = "https://roms.example.com"
        watcher = register(cog)
        watcher.state = state
        watcher.rooms = rooms or {}
        watcher.message_id = 1
        cog.bot.get_channel = lambda cid: FakeChannel(FakeMessage())
        return cog, watcher

    async def _press(self, cog, watcher, user_id=4242):
        view = NetplayJoinView(cog, watcher.rom_id)
        interaction = FakeInteraction(user_id)
        await view.on_join(interaction)
        return interaction.sent[0]

    async def test_the_reply_carries_a_link_button(self):
        """A URL in prose has to be found before it can be clicked."""
        cog, watcher = self._cog_and_watcher()

        _, kwargs = await self._press(cog, watcher)

        button = kwargs["view"].children[0]
        self.assertIs(button.style, discord.ButtonStyle.link)

    async def test_the_button_names_the_only_open_room(self):
        cog, watcher = self._cog_and_watcher(rooms={"sid-9": {"max": 2}})

        _, kwargs = await self._press(cog, watcher)

        self.assertTrue(kwargs["view"].children[0].url.endswith("?room=sid-9"))

    async def test_several_rooms_name_none_of_them(self):
        """Guessing which one they meant is worse than the menu."""
        cog, watcher = self._cog_and_watcher(
            rooms={"sid-9": {"max": 2}, "sid-4": {"max": 2}}
        )

        _, kwargs = await self._press(cog, watcher)

        self.assertNotIn("room=", kwargs["view"].children[0].url)

    async def test_a_session_with_no_room_yet_names_none(self):
        cog, watcher = self._cog_and_watcher(state=NetplayState.PENDING)

        _, kwargs = await self._press(cog, watcher)

        self.assertNotIn("room=", kwargs["view"].children[0].url)

    async def test_a_finished_session_still_hands_over_a_button(self):
        """The button outlives its watcher; the reply must not break."""
        cog, watcher = self._cog_and_watcher()
        cog.watchers.clear()

        _, kwargs = await self._press(cog, watcher)

        self.assertIn(f"/rom/{watcher.rom_id}/ejs", kwargs["view"].children[0].url)


class SeatAwareButtonTests(unittest.IsolatedAsyncioTestCase):
    """The seat state has to reach the button people actually see."""

    def _watcher(self, rooms):
        cog = make_cog()
        cog.bot.config.DOMAIN = "https://roms.example.com"
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = rooms
        return cog, watcher

    async def test_an_unlisted_session_relabels_but_stays_pressable(self):
        """A dead button would lock out a spectator and lose the roster."""
        cog, watcher = self._watcher({"a": {"current": 1, "max": 2}})
        watcher.unlisted_since = 1010.0

        button = cog.join_view(watcher).children[0]

        self.assertFalse(button.disabled)
        self.assertEqual(button.label, "Open the player")

    async def test_a_free_seat_stays_pressable(self):
        cog, watcher = self._watcher({"a": {"current": 1, "max": 2}})

        button = cog.join_view(watcher).children[0]

        self.assertFalse(button.disabled)
        self.assertIn("1 seat left", button.label)

    async def test_a_poll_that_unlists_the_room_relabels_the_button(self):
        """Renders in isolation are worth nothing if the edit drops them."""
        message = FakeMessage()
        cog = make_polling_cog([{}], message=message)
        watcher = register(cog)
        watcher.state = NetplayState.LIVE
        watcher.rooms = {"a": {"room_name": "r", "current": 1, "max": 2,
                               "player_name": "h", "hasPassword": False}}
        watcher.message_id = 1

        await cog.poll_sessions()

        button = message.last_edit["view"].children[0]
        self.assertEqual(button.label, "Open the player")


if __name__ == "__main__":
    unittest.main()
