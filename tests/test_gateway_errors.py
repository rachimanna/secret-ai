import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx
from openai import InternalServerError

import bot


def server_error():
    request = httpx.Request("POST", "https://api.xpiki.com/v1/chat/completions")
    return InternalServerError("upstream", response=httpx.Response(500, request=request), body=None)


class GatewayErrorTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_500_with_effort_retries_without_it_and_remembers(self):
        create = AsyncMock(side_effect=[server_error(), NS(choices=[]), NS(choices=[])])
        client = NS(chat=NS(completions=NS(create=create)))
        data = {}
        await bot.chat_completion(data, "claude-opus-5-5", client, model="claude-opus-5-5", messages=[])
        self.assertEqual(create.await_args_list[0].kwargs["reasoning_effort"], "low")
        self.assertNotIn("reasoning_effort", create.await_args_list[1].kwargs)
        self.assertIn("claude-opus-5-5", data["no_effort"])
        await bot.chat_completion(data, "claude-opus-5-5", client, model="claude-opus-5-5", messages=[])  # remembered
        self.assertNotIn("reasoning_effort", create.await_args_list[2].kwargs)

    async def test_a_plain_500_is_retried_once_then_raised(self):
        create = AsyncMock(side_effect=[server_error(), server_error(), server_error()])
        client = NS(chat=NS(completions=NS(create=create)))
        with patch.object(bot.asyncio, "sleep", AsyncMock()), self.assertRaises(InternalServerError):
            await bot.chat_completion({"no_effort": {"m"}}, "m", client, model="m", messages=[])
        self.assertEqual(create.await_count, 2)

    async def test_owner_alert_names_provider_failures_and_the_bots_own_code(self):
        tg = NS(send_message=AsyncMock())
        context = NS(bot_data={"connections": NS(get_meta=lambda name: 1)}, bot=tg, error=None)
        try:
            with patch.object(bot.asyncio, "sleep", AsyncMock()):
                await bot.chat_completion({"no_effort": {"m"}}, "m", NS(chat=NS(completions=NS(
                    create=AsyncMock(side_effect=[server_error(), server_error()])))), model="m", messages=[])
        except InternalServerError as exc:
            context.error = exc
        await bot.on_error(None, context)
        text = tg.send_message.call_args.args[1]
        self.assertIn("сбой на стороне провайдера", text)
        self.assertIn("bot.py", text)
        self.assertNotIn("_base_client", text)


if __name__ == "__main__":
    unittest.main()
