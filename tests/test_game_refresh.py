import asyncio
import sqlite3
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import game


class BunkerGameTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.ask = AsyncMock(return_value="Герои смогли преодолеть все испытания и возродить мир!")
        self.quiz = game.Quiz(self.db, self.ask)
        self.g = game.Game(-1, 1, "Host")
        self.quiz.games[-1] = self.g
        self.g.lobby_message = NS(text="lobby", edit_text=AsyncMock(), edit_caption=AsyncMock())

    def query(self, data):
        return NS(data=data, message=NS(chat_id=-1), answer=AsyncMock())

    async def button(self, action, uid=1, name="Host"):
        q = self.query(f"g:{action}:{self.g.session}")
        await self.quiz.on_button(q, NS(id=uid), name, NS())
        return q

    async def test_old_party_buttons_rejected(self):
        for action in ("j", "c:zombie", "s", "v:1"):
            q = self.query(f"g:{action}:old-session")
            await self.quiz.on_button(q, NS(id=2), "Other", NS())
            self.assertIn("старой", q.answer.call_args.args[0])
        self.assertEqual(self.g.players, {1: "Host"})
        self.assertEqual(self.g.catastrophe, "nuclear")
        self.assertEqual(self.g.state, "lobby")

    async def test_player_join_and_cap(self):
        # Join player 2
        q2 = await self.button("j", 2, "Player 2")
        self.assertIn(2, self.g.players)
        self.assertIn("кандидаты", q2.answer.call_args.args[0].lower())

        # Joining again
        q2_dup = await self.button("j", 2, "Player 2")
        self.assertIn("уже в списке", q2_dup.answer.call_args.args[0])

        # Test player cap
        self.g.players = {i: f"P{i}" for i in range(game.MAX_PLAYERS)}
        q_overflow = await self.button("j", 999, "Overflow")
        self.assertNotIn(999, self.g.players)
        self.assertIn("переполнен", q_overflow.answer.call_args.args[0])

    async def test_only_host_can_select_catastrophe(self):
        # Non-host attempt
        await self.button("c:zombie", 2, "Player 2")
        self.assertEqual(self.g.catastrophe, "nuclear")

        # Host selects catastrophe
        q = await self.button("c:zombie", 1, "Host")
        self.assertEqual(self.g.catastrophe, "zombie")
        self.assertIn("Кордицепс", q.answer.call_args.args[0])

        # Check callback data lengths in keyboard
        keyboard = self.quiz.lobby_keyboard(self.g)
        for row in keyboard.inline_keyboard:
            for b in row:
                self.assertTrue(b.callback_data.endswith(self.g.session))
                self.assertLess(len(b.callback_data.encode("utf-8")), 65)

    async def test_start_double_click_protection(self):
        self.quiz._play = AsyncMock()
        self.g.players.update({2: "Player 2", 3: "Player 3"})

        # Non-host cannot start
        await self.button("s", 2, "Player 2")
        self.quiz._play.assert_not_called()

        # Host double click
        await asyncio.gather(self.button("s", 1, "Host"), self.button("s", 1, "Host"))
        await asyncio.sleep(0)
        self.quiz._play.assert_awaited_once()

    async def test_character_generation_and_cards(self):
        c = game.Character(10, "Tester")
        self.assertEqual(c.user_id, 10)
        self.assertEqual(c.name, "Tester")
        self.assertTrue(c.alive)

        # Check all fields exist
        self.assertTrue(len(c.profession) > 0)
        self.assertTrue(len(c.biology) > 0)
        self.assertTrue(len(c.health) > 0)
        self.assertTrue(len(c.hobby) > 0)
        self.assertTrue(len(c.phobia) > 0)
        self.assertTrue(len(c.baggage) > 0)

        # Check card texts
        r1 = c.card_round_1()
        self.assertIn(c.profession, r1)
        self.assertIn(c.biology, r1)

        r2 = c.card_round_2()
        self.assertIn(c.health, r2)
        self.assertIn(c.hobby, r2)

        full = c.full_card()
        for field in (c.profession, c.biology, c.health, c.hobby, c.phobia, c.baggage):
            self.assertIn(field, full)

    async def test_voting_and_elimination(self):
        c1 = game.Character(1, "Host")
        c2 = game.Character(2, "Player 2")
        c3 = game.Character(3, "Player 3")
        self.g.characters = {1: c1, 2: c2, 3: c3}
        self.g.state = "voting"
        self.g.votes = {}

        # Dead player cannot vote
        c_dead = game.Character(4, "Dead")
        c_dead.alive = False
        self.g.characters[4] = c_dead
        q_dead = await self.button("v:1", 4, "Dead")
        self.assertIn("только живые", q_dead.answer.call_args.args[0].lower())
        del self.g.characters[4]

        # Self-voting is prohibited
        q_self = self.query(f"g:v:1:{self.g.session}")
        await self.quiz.on_button(q_self, NS(id=1), "Host", NS())
        self.assertIn("против самого себя", q_self.answer.call_args.args[0].lower())

        # Start voting round in background task
        bot = NS(send_message=AsyncMock(return_value=NS(edit_text=AsyncMock(), edit_reply_markup=AsyncMock())))
        alive = [c for c in self.g.characters.values() if c.alive]
        vote_task = asyncio.create_task(self.quiz._run_voting_round(bot, self.g, alive, 1))
        await asyncio.sleep(0.01)

        # Player 1 votes to exile Player 2
        q1 = self.query(f"g:v:2:{self.g.session}")
        await self.quiz.on_button(q1, NS(id=1), "Host", NS())
        self.assertEqual(self.g.votes[1], 2)
        self.assertIn("голос принят", q1.answer.call_args.args[0].lower())

        # Player 3 votes to exile Player 2
        q3 = self.query(f"g:v:2:{self.g.session}")
        await self.quiz.on_button(q3, NS(id=3), "Player 3", NS())
        self.assertEqual(self.g.votes[3], 2)

        # Player 2 votes to exile Player 1 (all alive players voted -> wakes up round)
        q2 = self.query(f"g:v:1:{self.g.session}")
        await self.quiz.on_button(q2, NS(id=2), "Player 2", NS())
        self.assertTrue(self.g.voted_event.is_set())

        # Target elimination calculation
        exiled, verdict = await asyncio.wait_for(vote_task, timeout=2.0)
        self.assertEqual(exiled.user_id, 2)
        self.assertIn("Большинством", verdict)
        exiled.alive = False
        self.assertFalse(c2.alive)

    async def test_tie_is_settled_by_lot_not_by_first_vote(self):
        c1, c2 = game.Character(1, "Host"), game.Character(2, "Player 2")
        self.g.characters = {1: c1, 2: c2}
        self.g.state = "voting"
        bot = NS(send_message=AsyncMock(return_value=NS(edit_reply_markup=AsyncMock())))
        exiled = set()
        for _ in range(40):
            task = asyncio.create_task(self.quiz._run_voting_round(bot, self.g, [c1, c2], 1))
            await asyncio.sleep(0)
            await self.quiz.on_button(self.query(f"g:v:2:{self.g.session}"), NS(id=1), "Host", NS())
            await self.quiz.on_button(self.query(f"g:v:1:{self.g.session}"), NS(id=2), "Player 2", NS())
            target, verdict = await asyncio.wait_for(task, timeout=2.0)
            self.assertIn("поровну", verdict)
            exiled.add(target.user_id)
        self.assertEqual(exiled, {1, 2})

    async def test_start_needs_minimum_players(self):
        self.quiz._play = AsyncMock()
        q = await self.button("s", 1, "Host")
        self.assertIn(str(game.MIN_PLAYERS), q.answer.call_args.args[0])
        self.assertEqual(self.g.state, "lobby")
        self.quiz._play.assert_not_called()
        self.g.players.update({2: "P2", 3: "P3"})
        await self.button("s", 1, "Host")
        await asyncio.sleep(0)
        self.quiz._play.assert_awaited_once()

    async def test_new_lobby_does_not_cancel_a_finishing_game(self):
        self.g.state = "done"
        finale = asyncio.create_task(asyncio.sleep(10))
        self.g.task = finale
        bot = NS(send_message=AsyncMock(return_value=NS(text="lobby")))
        message = NS(chat_id=-1, reply_text=AsyncMock())
        await self.quiz.open_lobby(bot, message, NS(id=5), "New host")
        await asyncio.sleep(0)
        self.assertFalse(finale.cancelled())
        self.assertIsNot(self.quiz.games[-1], self.g)
        finale.cancel()
        self.quiz.games[-1].task.cancel()

    async def test_model_epilogue_is_escaped_and_fits_one_message(self):
        self.g.characters = {1: game.Character(1, "<Alice>"), 2: game.Character(2, "Bob")}
        self.g.characters[2].alive = False
        self.ask.return_value = "Эпилог <b>не закрыт & сломан " + "история " * 2000
        bot = NS(send_message=AsyncMock())
        await self.quiz._finish(bot, self.g)
        text = bot.send_message.call_args.args[1]
        self.assertLessEqual(len(text), game.MESSAGE_LIMIT)
        self.assertIn("&lt;b&gt;не закрыт &amp; сломан", text)
        self.assertIn("&lt;Alice&gt;", text)
        self.assertNotIn("<Alice>", text)
        self.assertTrue(text.rstrip().endswith(f"/{game.RATING}"))

    async def test_stop_game_permissions(self):
        bot = NS(send_message=AsyncMock())
        # Non-admin non-host cannot stop
        res = await self.quiz.stop(bot, -1, 2, is_admin=False)
        self.assertIn("только ведущий", res.lower())
        self.assertNotEqual(self.g.state, "done")

        # Host can stop
        res_host = await self.quiz.stop(bot, -1, 1, is_admin=False)
        self.assertIn("остановлен", res_host.lower())
        self.assertEqual(self.g.state, "done")

    async def test_epilogue_and_database_scoring(self):
        c1 = game.Character(1, "Alice")
        c2 = game.Character(2, "Bob")
        c1.alive = True
        c2.alive = False
        self.g.characters = {1: c1, 2: c2}

        bot = NS(send_message=AsyncMock())
        await self.quiz._finish(bot, self.g)

        # Check database scores
        rows = self.db.execute(
            "SELECT user_id, points, wins, games FROM game_scores ORDER BY user_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        # Alice won: 1500 points, 1 win, 1 game
        self.assertEqual(rows[0], (1, 1500, 1, 1))
        # Bob lost/exiled: 300 points, 0 wins, 1 game
        self.assertEqual(rows[1], (2, 300, 0, 1))

        # Check top leaderboard output
        await self.quiz.top(bot, -1, "Test Chat")
        bot.send_message.assert_awaited()
        call_text = bot.send_message.call_args.args[1]
        self.assertIn("ТОП ВЫЖИВШИХ", call_text)
        self.assertIn("Alice", call_text)
        self.assertIn("1500 очков", call_text)


if __name__ == "__main__":
    unittest.main()
