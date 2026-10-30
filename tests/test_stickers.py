import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageDraw

import media


def photo(alpha=False):
    out = io.BytesIO()
    if alpha:
        im = Image.new("RGBA", (900, 600), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((300, 100, 600, 500), fill=(200, 80, 40, 255))
        im.save(out, "PNG")
    else:
        Image.new("RGB", (1200, 900), (40, 90, 160)).save(out, "JPEG")
    return out.getvalue()


class StickerImageTest(unittest.TestCase):
    def test_cut_out_is_cropped_outlined_and_512(self):
        out = Image.open(io.BytesIO(media.sticker_png(photo(alpha=True), True)))
        self.assertEqual((out.mode, max(out.size)), ("RGBA", 512))
        self.assertEqual(out.getpixel((0, 0))[3], 0)  # transparent around
        self.assertEqual(out.height, 512)  # the empty margins were cropped: the tall oval fills the height
        self.assertLess(out.width, 450)

    def test_whole_photo_gets_round_corners(self):
        out = Image.open(io.BytesIO(media.sticker_png(photo(), False)))
        self.assertEqual(out.size, (512, 384))
        self.assertEqual(out.getpixel((0, 0))[3], 0)
        self.assertEqual(out.getpixel((256, 192))[3], 255)


try:
    import bot
    from telegram.error import BadRequest
except ImportError:
    bot = None


@unittest.skipIf(bot is None, "requires the bot's libraries")
class StickerJobTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = NS(gen_charge=Mock(return_value="free"), gen_refund=Mock())
        self.data = {"memory": self.memory, "http": NS(), "bot_name": "secret_bot",
                     "connections": NS(get_meta=lambda name, secret=False: {"owner": 99, "fal_key": "key"}.get(name))}
        self.tg = NS(send_chat_action=AsyncMock(), add_sticker_to_set=AsyncMock(), create_new_sticker_set=AsyncMock(),
                     get_sticker_set=AsyncMock(return_value=NS(stickers=[NS(file_id="s1")])), send_sticker=AsyncMock(),
                     send_message=AsyncMock())

    async def run_job(self, cut=True):
        with patch.object(bot, "photo_bytes", AsyncMock(return_value=[photo(alpha=True)])), \
                patch.object(bot, "cut_out", AsyncMock(return_value=(photo(alpha=True), cut))):
            await bot.sticker_job(self.tg, self.data, NS(id=7, first_name="Аня"), "ru", "file", "😂")

    async def test_first_sticker_creates_the_pack(self):
        self.tg.add_sticker_to_set.side_effect = BadRequest("Stickerset_invalid")
        await self.run_job()
        args = self.tg.create_new_sticker_set.call_args.args
        self.assertEqual(args[:2], (7, "u7_by_secret_bot"))
        self.assertIn("Аня", args[2])
        self.assertEqual(args[3][0].emoji_list, ("😂",))
        self.tg.send_sticker.assert_awaited_once_with(7, "s1")
        button = self.tg.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.url, "https://t.me/addstickers/u7_by_secret_bot")
        self.memory.gen_refund.assert_not_called()

    async def test_no_cut_out_gives_the_generation_back(self):
        await self.run_job(cut=False)
        self.tg.create_new_sticker_set.assert_not_called()  # the pack existed: added to it
        self.memory.gen_refund.assert_called_once_with(7, "free", media.COST["photo"])

    async def test_a_full_pack_is_explained_and_refunded(self):
        self.tg.add_sticker_to_set.side_effect = BadRequest("Stickers_too_much")
        await self.run_job()
        self.assertIn("максимум", self.tg.send_message.call_args.args[1])
        self.memory.gen_refund.assert_called_once()


if __name__ == "__main__":
    unittest.main()
