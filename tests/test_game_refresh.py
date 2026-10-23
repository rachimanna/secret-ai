import asyncio
import sqlite3
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import game

class QuizRefreshTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.quiz=game.Quiz(self.db,AsyncMock(side_effect=RuntimeError('offline')))
        self.g=game.Game(-1,1,'Host')
        self.quiz.games[-1]=self.g
        self.g.lobby_message=NS(text=None,edit_caption=AsyncMock())

    def query(self,data):
        return NS(data=data,message=NS(chat_id=-1),answer=AsyncMock())

    async def button(self,action,uid=1):
        q=self.query(f'g:{action}:{self.g.session}')
        await self.quiz.on_button(q,NS(id=uid),'Player',NS())
        return q

    async def test_old_party_buttons_cannot_join_reconfigure_or_answer(self):
        for action in ('j','m:blitz','s','a:0:0'):
            q=self.query(f'g:{action}:old-session')
            await self.quiz.on_button(q,NS(id=2),'Other',NS())
            self.assertIn('старой',q.answer.call_args.args[0])
        self.assertEqual(self.g.players,{1:'Host'})
        self.assertEqual(self.g.mode,'classic')
        self.assertEqual(self.g.state,'lobby')

    async def test_only_host_can_select_mode(self):
        await self.button('m:blitz',2)
        self.assertEqual(self.g.mode,'classic')
        await self.button('m:blitz')
        self.assertEqual(self.g.mode,'blitz')
        keyboard=self.quiz.lobby_keyboard(self.g)
        for row in keyboard.inline_keyboard:
            for b in row:
                self.assertTrue(b.callback_data.endswith(self.g.session))
                self.assertLess(len(b.callback_data.encode()),65)

    async def test_answers_reject_deadline_duplicates_and_invalid_options(self):
        self.g.state='question';self.g.index=0
        self.g.questions=[('Q',['a','b','c','d'],0,'')]
        self.g.asked_at=time.monotonic()-game.MODES['classic'][2]-1
        await self.button('a:0:0')
        self.assertEqual(self.g.answers,{})
        self.g.asked_at=time.monotonic()
        for action in ('a:0:9','a:0:-1','a:-1:0','a:8:0','a:no:0'):
            await self.button(action)
        self.assertEqual(self.g.answers,{})
        await self.button('a:0:2')
        await self.button('a:0:1')
        self.assertEqual(self.g.answers[1][0],2)

    async def test_player_cap(self):
        self.g.players={i:str(i) for i in range(game.MAX_PLAYERS)}
        await self.button('j',999)
        self.assertNotIn(999,self.g.players)

    async def test_modes_have_complete_fallback_and_distinct_rules(self):
        for mode,(_,count,seconds,_) in game.MODES.items():
            self.g.mode=mode
            questions=await self.quiz._questions(self.g)
            self.assertEqual(len(questions),count)
            self.assertTrue(all(len(q[1])==4 and 0<=q[2]<4 for q in questions))
        self.assertEqual(game.points_for('calm',35,0,3),game.points_for('calm',35,34,1))
        self.assertGreater(game.points_for('blitz',10,1,1),game.points_for('blitz',10,9,1))
        self.assertEqual(game.points_for('classic',20,1,3)-game.points_for('classic',20,1,2),200)

    async def test_start_double_click_runs_one_game(self):
        self.quiz._play=AsyncMock()
        await asyncio.gather(self.button('s'),self.button('s'))
        await asyncio.sleep(0)
        self.quiz._play.assert_awaited_once()

    async def test_tied_nonzero_scores_award_both_players(self):
        self.g.players={1:'A',2:'B',3:'C'};self.g.scores={1:1000,2:1000,3:0}
        bot=NS(send_animation=AsyncMock())
        with patch('game.asyncio.to_thread',new=AsyncMock(return_value=(b'clip',b'preview'))):
            await self.quiz._finish(bot,self.g)
        rows=self.db.execute('SELECT user_id,wins FROM game_scores ORDER BY user_id').fetchall()
        self.assertEqual(rows,[(1,1),(2,1),(3,0)])
        self.assertIn('Ничья',bot.send_animation.call_args.kwargs['caption'])

    async def test_missed_question_resets_streak_and_supports_text_cards(self):
        self.g.state='question';self.g.index=0;self.g.streak={1:2}
        self.g.questions=[('Q',['a','b','c','d'],0,'Fact')]
        future=asyncio.get_running_loop().create_future();future.set_result(None)
        self.g.clips=[future]
        message=NS(edit_text=AsyncMock())
        bot=NS(send_message=AsyncMock(return_value=message))
        with patch.dict(game.MODES,classic=('Classic',1,.001,'')),patch('game.asyncio.sleep',new=AsyncMock()):
            await self.quiz._ask(bot,self.g)
        self.assertEqual(self.g.streak[1],0)
        self.assertIn(self.g.session,str(bot.send_message.call_args.kwargs['reply_markup']))
        message.edit_text.assert_awaited_once()
        self.assertEqual(self.g.state,"reveal")
        self.g.asked_at=time.monotonic()
        await self.button("a:0:0")
        self.assertEqual(self.g.answers,{})

if __name__=='__main__':unittest.main()
