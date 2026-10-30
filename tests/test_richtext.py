import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telegram import MessageEntity

import richtext

HAS_BOT = True
try:
    import bot
except ImportError:  # the bot's libraries are not installed: only the renderer is tested
    HAS_BOT = False


def spans(text, partial=False):
    plain, entities = richtext.render(text, partial)
    return plain, [(e.type, e.offset, e.length) + ((e.url,) if e.url else ()) + ((e.language,) if e.language else ())
                   for e in entities]


class RenderTest(unittest.TestCase):
    def test_bold_italic_code_and_links(self):
        plain, found = spans("**Важно:** запусти `pip install x` *спокойно*, см. [доку](https://ex.com/a_b).")
        self.assertEqual(plain, "Важно: запусти pip install x спокойно, см. доку.")
        self.assertEqual(found, [(MessageEntity.BOLD, 0, 6), (MessageEntity.CODE, 15, 13),
                                 (MessageEntity.ITALIC, 29, 8), (MessageEntity.TEXT_LINK, 43, 4, "https://ex.com/a_b")])

    def test_headings_bullets_rules_and_quotes(self):
        plain, found = spans("## План\n- раз\n  - вложенный\n* два\n---\n> цитата\n> ещё")
        self.assertEqual(plain, "План\n• раз\n  ◦ вложенный\n• два\n────────\nцитата\nещё")
        self.assertIn((MessageEntity.BOLD, 0, 4), found)
        self.assertIn((MessageEntity.BLOCKQUOTE, plain.index("цитата"), len("цитата\nещё")), found)

    def test_a_long_quote_folds(self):
        _, found = spans("\n".join(f"> строка {i}" for i in range(10)))
        self.assertEqual(found[0][0], MessageEntity.EXPANDABLE_BLOCKQUOTE)

    def test_code_block_keeps_its_text_and_language(self):
        plain, found = spans("Пример:\n```python\nprint(a_b * 2)\n```\nГотово")
        self.assertEqual(plain, "Пример:\nprint(a_b * 2)\nГотово")
        self.assertEqual(found, [(MessageEntity.PRE, 8, 14, "python")])

    def test_ordinary_text_is_left_alone(self):
        for text in ["2 * 3 * 4 = 24", "snake_case_name и file_name.py", "https://site.com/my_file_name.txt",
                     "Привет! Как дела?", "10% → 20%; № 7", "a ** b"]:
            self.assertEqual(spans(text), (text, []), text)

    def test_utf16_offsets_after_emoji(self):
        _, found = spans("🤖 **да**")
        self.assertEqual(found, [(MessageEntity.BOLD, 3, 2)])

    def test_unfinished_markers_never_flash_while_typing(self):
        self.assertEqual(spans("Это **важн", partial=True), ("Это важн", [(MessageEntity.BOLD, 4, 4)]))
        self.assertEqual(spans("Код `pip ins", partial=True), ("Код pip ins", [(MessageEntity.CODE, 4, 7)]))
        self.assertEqual(spans("```py\nprint(1", partial=True), ("print(1", [(MessageEntity.PRE, 0, 7, "py")]))
        # A finished answer keeps a stray marker as written.
        self.assertEqual(spans("Это **важн"), ("Это **важн", []))


@unittest.skipUnless(HAS_BOT, "requires the bot's libraries")
class ReplyFormattingTest(unittest.IsolatedAsyncioTestCase):
    async def test_draft_and_final_message_carry_the_formatting(self):
        api = NS(send_message_draft=AsyncMock())
        target = NS(chat_id=1, get_bot=lambda: api, reply_text=AsyncMock(return_value=NS(message_id=1)))
        draft = bot.DraftReply(target, "ru", {"logo": "✨"})
        await draft.update("**Итог:** всё ок")
        await draft._tick()
        call = api.send_message_draft.call_args
        self.assertEqual(call.args[2], "Итог: всё ок")
        self.assertEqual(call.kwargs["entities"][0].type, MessageEntity.BOLD)
        draft.closed = True
        await draft.finish("**Итог:** всё ок")
        sent = target.reply_text.call_args
        self.assertEqual(sent.args[0], "Итог: всё ок")
        self.assertEqual(sent.kwargs["entities"][0].type, MessageEntity.BOLD)

    def test_premium_emoji_stay_out_of_code(self):
        with patch.dict(bot.PREMIUM_EMOJI, {"✨": "103"}, clear=True):
            plain, entities = bot.formatted("✨ `✨`")
        kinds = [(e.type, e.offset) for e in entities]
        self.assertIn((MessageEntity.CUSTOM_EMOJI, 0), kinds)
        self.assertNotIn((MessageEntity.CUSTOM_EMOJI, 2), kinds)
        self.assertEqual(plain, "✨ ✨")


if __name__ == "__main__":
    unittest.main()
