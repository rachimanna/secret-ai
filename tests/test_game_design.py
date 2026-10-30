import re
import unittest

import game


class BunkerLookTest(unittest.TestCase):
    def test_cards_open_round_by_round(self):
        c = game.Character(1, "<Аня>")
        first, last = c.card(1), c.card(3)
        self.assertIn("&lt;Аня&gt;", first)
        self.assertIn(c.profession, first)
        self.assertNotIn(c.baggage, first)
        self.assertEqual(first.count("🔒"), 4)
        self.assertIn(c.baggage, last)
        self.assertNotIn("🔒", last)
        self.assertNotIn("✦", c.full_card())

    def test_a_big_round_is_split_between_whole_cards(self):
        players = [game.Character(i, f"Игрок номер {i}") for i in range(game.MAX_PLAYERS)]
        text = game.round_text(3, "Психология и багаж", players, "20 секунд")
        parts = game.pieces(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual("\n\n".join(parts), text)
        for part in parts:
            self.assertLessEqual(len(part), game.MESSAGE_LIMIT)
            self.assertEqual(part.count("<blockquote>"), part.count("</blockquote>"))

    def test_progress_bar(self):
        self.assertEqual(game.progress(2, 3, 3), "▰▰▱")
        self.assertEqual(game.progress(5, 0, 4), "▱▱▱▱")
        self.assertEqual(len(game.progress(30, 3)), 10)

    def test_no_shouting_headers(self):
        g = game.Game(-1, 1, "Host")
        text = game.BunkerGame.lobby_text(None, g)
        words = re.findall(r"[А-ЯЁA-Z]{6,}", re.sub(r"<[^>]+>", "", text))
        self.assertEqual(words, [])


class VoteCounterTest(unittest.IsolatedAsyncioTestCase):
    async def test_votes_show_up_on_the_voting_message(self):
        import sqlite3
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock, patch
        quiz = game.BunkerGame(sqlite3.connect(":memory:"), AsyncMock())
        g = game.Game(-1, 1, "A")
        g.characters = {i: game.Character(i, n) for i, n in ((1, "A"), (2, "B"), (3, "C"))}
        g.state = "voting"
        message = NS(text="vote", reply_markup="buttons", edit_text=AsyncMock())
        g.vote_message, g.vote_text = message, "⚖️ Голосование"
        with patch.object(game, "VOTE_REFRESH_SECONDS", 0):
            await quiz._handle_vote(NS(answer=AsyncMock()), g, NS(id=1), ["g", "v", "2", g.session])
            await g.vote_refresh
        shown = message.edit_text.call_args.args[0]
        self.assertIn("Проголосовали: <b>1</b> из 3", shown)
        self.assertEqual(message.edit_text.call_args.kwargs["reply_markup"], "buttons")


if __name__ == "__main__":
    unittest.main()
