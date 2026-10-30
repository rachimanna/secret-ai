import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot
from fun import GroupStore


class OwnerStatsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.groups = GroupStore(":memory:")
        self.addCleanup(self.memory.db.close)
        self.addCleanup(self.groups.db.close)
        now = int(time.time())
        rows = [(1, "user", "старый", now - 30 * 86400), (1, "user", "снова", now - 3600),
                (2, "user", "новый", now - 2 * 86400), (2, "assistant", "ответ", now - 2 * 86400)]
        with self.memory.db:
            self.memory.db.executemany("INSERT INTO messages(user_id, role, content, created) VALUES (?, ?, ?, ?)", rows)
            self.memory.db.execute("INSERT INTO gen_payments VALUES ('c1', 1, 60, 60)")
        self.memory.add_referral(3, 1, 3)
        with self.groups.db:
            self.groups.db.execute("INSERT INTO group_info(chat_id, title) VALUES (-5, 'Наш круг'), (-6, '')")
            self.groups.db.executemany("INSERT INTO group_messages(chat_id, line, created) VALUES (?, ?, ?)",
                                       [(-5, "А: привет", now - 60)] * 3 + [(-6, "Б: ку", now - 60)])
        self.data = {"memory": self.memory, "groups": self.groups,
                     "connections": NS(get_meta=lambda name, *a, **k: 1 if name == "owner" else None)}

    def test_counts_people_activity_groups_and_money(self):
        stats = bot.owner_stats(self.data)
        self.assertEqual((stats["users"], stats["new_week"], stats["active_day"], stats["active_week"]), (2, 1, 1, 2))
        self.assertEqual(stats["messages_week"], 2)
        self.assertEqual((stats["groups"], stats["groups_active"]), (2, 2))
        self.assertEqual(stats["top_groups"][0], {"title": "Наш круг", "messages": 3})
        self.assertEqual(stats["top_groups"][1]["title"], "группа -6")
        self.assertEqual((stats["stars"], stats["payments"], stats["invited"]), (60, 1, 1))
        text = bot.stats_text(stats)
        self.assertIn("Наш круг", text)
        self.assertNotIn("привет", text)  # counts only, never what people wrote

    async def test_only_the_owner_gets_stats(self):
        message = NS(reply_text=AsyncMock())
        context = NS(bot_data=self.data)
        with patch.object(bot, "authorized", AsyncMock(return_value=True)):
            await bot.stats_command(NS(effective_user=NS(id=2, language_code="ru"), effective_message=message), context)
            self.assertIn("только владельцу", message.reply_text.call_args.args[0])
            await bot.stats_command(NS(effective_user=NS(id=1, language_code="ru"), effective_message=message), context)
        self.assertIn("Статистика Secret AI", message.reply_text.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
