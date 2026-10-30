import asyncio
import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

import media


def picture(size=(400, 300)):
    out = io.BytesIO()
    Image.new("RGB", size, (20, 90, 160)).save(out, "PNG")
    return out.getvalue()


def size(data):
    return Image.open(io.BytesIO(data)).size


class UpscaleTest(unittest.IsolatedAsyncioTestCase):
    async def test_without_a_key_the_picture_is_resized_to_4k_at_most_4x(self):
        out, way = await media.upscale(None, "", picture((1920, 1080)))
        self.assertEqual((way, size(out)), ("resize", (3840, 2160)))
        out, _ = await media.upscale(None, "", picture((100, 50)))
        self.assertEqual(size(out), (400, 200))

    async def test_ai_result_is_used_and_brought_to_4k(self):
        http = NS(get=AsyncMock(return_value=NS(status_code=200, content=picture((4000, 3000)))))
        with patch.object(media, "fal_run", AsyncMock(return_value="https://fal.test/x.png")) as run:
            out, way = await media.upscale(http, "key", picture((1000, 750)))
        self.assertEqual(way, "ai")
        self.assertEqual(size(out), (3840, 2880))
        self.assertEqual(run.call_args.args[2], media.UPSCALE_MODEL)
        self.assertTrue(run.call_args.args[3]["image_url"].startswith("data:image/jpeg;base64,"))

    async def test_a_failed_ai_run_falls_back_to_the_resize(self):
        with patch.object(media, "fal_run", AsyncMock(side_effect=media.MediaError("bad_key"))):
            out, way = await media.upscale(NS(), "key", picture((960, 540)))
        self.assertEqual((way, size(out)), ("resize", (3840, 2160)))


try:
    import bot
except ImportError:
    bot = None


@unittest.skipIf(bot is None, "requires the bot's libraries")
class Deliver4kTest(unittest.IsolatedAsyncioTestCase):
    def data(self, key):
        memory = NS(gen_charge=Mock(return_value="free"), gen_refund=Mock())
        connections = NS(get_meta=lambda name, secret=False: {"owner": 99, "fal_key": key}.get(name), )
        return {"memory": memory, "connections": connections, "http": NS()}, memory

    async def send(self, data, way):
        tg = NS(send_chat_action=AsyncMock(), send_document=AsyncMock())
        with patch.object(bot, "upscale", AsyncMock(return_value=(picture((3840, 2160)), way))):
            await bot.deliver_4k(tg, data, 7, "ru", picture(), 7)
        return tg.send_document.call_args

    async def test_ai_detail_costs_one_generation(self):
        data, memory = self.data("key")
        sent = await self.send(data, "ai")
        memory.gen_charge.assert_called_once_with(7, "photo", media.COST["photo"])
        memory.gen_refund.assert_not_called()
        self.assertIn("3840×2160", sent.kwargs["caption"])
        self.assertIn("дорисовал", sent.kwargs["caption"])

    async def test_a_plain_resize_is_given_back(self):
        data, memory = self.data("key")
        await self.send(data, "resize")
        memory.gen_refund.assert_called_once_with(7, "free", media.COST["photo"])

    async def test_without_a_key_nothing_is_charged(self):
        data, memory = self.data("")
        await self.send(data, "resize")
        memory.gen_charge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
