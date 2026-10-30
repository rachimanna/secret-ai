import asyncio
import sqlite3
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import partygames as pg


def query(data, chat_id=-1):
    return NS(data=data, answer=AsyncMock(), message=NS(chat_id=chat_id))


class GuessTest(unittest.TestCase):
    def test_endings_and_phrases(self):
        self.assertTrue(pg.guessed("это велосипед!", "велосипед"))
        self.assertTrue(pg.guessed("Велосипеда что ли", "велосипед"))
        self.assertTrue(pg.guessed("ёлка", "ёлка"))
        self.assertTrue(pg.guessed("воздушный шарик", "воздушный шар"))
        self.assertFalse(pg.guessed("вело", "велосипед"))
        self.assertFalse(pg.guessed("слово про кота", "кит"))
        self.assertEqual(pg.hint("воздушный шар"), "В········ Ш··")

    def test_buttons_fit_telegram(self):
        for name, roles in pg.LOCATIONS:
            self.assertLessEqual(len(f"s:l:{name}:abcdef12".encode()), 64, name)
            self.assertGreaterEqual(len(roles), 3)


class SpyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.games = pg.PartyGames(sqlite3.connect(":memory:"), AsyncMock(return_value=b"img"))
        self.bot = NS(send_message=AsyncMock(return_value=NS(edit_text=AsyncMock())), send_photo=AsyncMock())
        await self.games.open_spy(self.bot, NS(chat_id=-1, reply_text=AsyncMock()), 1, "Аня")
        self.game = self.games.spies[-1]
        self.addAsyncCleanup(self.cancel)

    async def cancel(self):
        if self.game.task:
            self.game.task.cancel()
            await asyncio.gather(self.game.task, return_exceptions=True)

    async def press(self, action, user, extra=""):
        q = query(f"s:{action}:{extra + ':' if extra else ''}{self.game.session}")
        await self.games.on_spy_button(q, NS(id=user), f"P{user}", self.bot)
        return q

    async def start(self):
        for user in (2, 3, 4):
            await self.press("j", user)
        with patch.object(pg, "SPY_TALK_SECONDS", 3600):
            await self.press("s", 1)
            await asyncio.sleep(0)

    async def test_needs_three_players(self):
        q = await self.press("s", 1)
        self.assertIn("минимум", q.answer.call_args.args[0])
        self.assertEqual(self.game.state, "lobby")

    async def test_each_player_sees_only_their_role(self):
        await self.start()
        spy = self.game.spy
        agent = next(uid for uid in self.game.players if uid != spy)
        shown = (await self.press("r", agent)).answer.call_args
        self.assertIn(self.game.location, shown.args[0])
        self.assertTrue(shown.kwargs["show_alert"])
        self.assertNotIn(self.game.location, (await self.press("r", spy)).answer.call_args.args[0])
        self.assertIn("не в этой партии", (await self.press("r", 99)).answer.call_args.args[0])

    async def test_spy_guessing_the_place_wins(self):
        await self.start()
        self.assertIn("не шпион", (await self.press("g", next(u for u in self.game.players if u != self.game.spy))).answer.call_args.args[0])
        await self.press("g", self.game.spy)
        self.assertEqual(self.game.state, "guess")
        await self.press("l", self.game.spy, self.game.location)
        self.assertEqual(self.game.state, "done")
        self.assertIn("Победил шпион", self.bot.send_message.call_args.args[1])
        points = self.games.db.execute("SELECT points FROM party_scores WHERE user_id=?", (self.game.spy,)).fetchone()
        self.assertEqual(points[0], pg.POINTS["spy"])

    async def test_majority_on_the_spy_lets_agents_win(self):
        await self.start()
        spy = self.game.spy
        with patch.object(pg, "SPY_VOTE_SECONDS", 5):
            self.game.state = "vote"
            voting = asyncio.create_task(self.games._run_vote(self.bot, self.game))
            await asyncio.sleep(0)
            for uid in self.game.players:
                if uid != spy:
                    await self.press("x", uid, str(spy))
            await self.press("x", spy, str(next(u for u in self.game.players if u != spy)))
            await voting
        self.assertIn("Агенты победили", self.bot.send_message.call_args.args[1])
        winners = self.games.db.execute("SELECT COUNT(*) FROM party_scores").fetchone()[0]
        self.assertEqual(winners, 3)


class CrocTest(unittest.IsolatedAsyncioTestCase):
    async def test_drawing_hint_and_first_right_guess(self):
        games = pg.PartyGames(sqlite3.connect(":memory:"), AsyncMock(return_value=b"img"))
        photo = NS(edit_caption=AsyncMock())
        bot = NS(send_photo=AsyncMock(return_value=photo), send_message=AsyncMock())
        with patch.object(pg.random, "choice", return_value=("велосипед", "a bicycle")), \
                patch.object(pg, "CROC_HINT_SECONDS", 0), patch.object(pg, "CROC_SECONDS", 3600):
            self.assertTrue(await games.open_croc(bot, -1))
            self.assertFalse(await games.open_croc(bot, -1))  # one picture at a time
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertIn("В········", photo.edit_caption.call_args.kwargs["caption"])
            wrong = NS(chat_id=-1, text="самокат", reply_text=AsyncMock())
            self.assertFalse(await games.check_guess(bot, wrong, 5, "Боб"))
            right = NS(chat_id=-1, text="Это же велосипед", reply_text=AsyncMock())
            self.assertTrue(await games.check_guess(bot, right, 6, "Аня"))
            self.assertFalse(await games.check_guess(bot, right, 7, "Вика"))  # only the first
        self.assertIn("Аня", right.reply_text.call_args.args[0])
        self.assertIn("a bicycle", games.draw.call_args.args[0])
        await games.top(bot, -1, "Чат")
        self.assertIn("Аня", bot.send_message.call_args.args[1])

    async def test_a_failed_drawing_says_so(self):
        games = pg.PartyGames(sqlite3.connect(":memory:"), AsyncMock(side_effect=RuntimeError))
        bot = NS(send_photo=AsyncMock(), send_message=AsyncMock())
        await games.open_croc(bot, -1)
        await games.crocs[-1].task
        self.assertIn("Не получилось", bot.send_message.call_args.args[1])
        self.assertTrue(await games.open_croc(bot, -1))  # can try again
        await games.crocs[-1].task


try:
    import bot
except ImportError:
    bot = None


@unittest.skipIf(bot is None, "requires the bot's libraries")
class WiringTest(unittest.IsolatedAsyncioTestCase):
    async def test_commands_and_buttons_reach_the_games(self):
        games = NS(open_croc=AsyncMock(return_value=True), on_spy_button=AsyncMock(), open_spy=AsyncMock())
        context = NS(bot_data={"allowed": set(), "party": games}, bot=NS(send_chat_action=AsyncMock()))
        user = NS(id=1, full_name="Аня", username=None, language_code="ru")
        private = NS(text="/spy", chat=NS(type="private"), reply_text=AsyncMock())
        await bot.party_command(NS(effective_message=private, effective_user=user), context)
        self.assertIn("для групп", private.reply_text.call_args.args[0])
        group = NS(text="/croc@secret_bot", chat=NS(type="supergroup", title="Чат"), chat_id=-1, reply_text=AsyncMock())
        await bot.party_command(NS(effective_message=group, effective_user=user), context)
        games.open_croc.assert_awaited_once()
        q = query("s:j:abc")
        await bot.on_button(NS(callback_query=q, effective_user=user), context)
        games.on_spy_button.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
