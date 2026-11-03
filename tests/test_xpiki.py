"""xpiki gateway: fake provider APIs only, never a real key."""
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
from openai import AuthenticationError

import bot


class Settings:
    def __init__(self, **values):
        self.values = values

    def get_meta(self, key):
        return self.values.get(key)

    def set_meta(self, key, value):
        self.values[key] = value


def gateway(ids=None, error=None):
    listing = AsyncMock(side_effect=error) if error else AsyncMock(return_value=NS(data=[NS(id=i) for i in ids]))
    return NS(models=NS(list=listing))


def data_for(xpiki):
    return dict(xpiki=xpiki, anymodel_chat=gateway(["cc/claude-opus-4-6", "xai/grok-4.7"]),
                reformboss=gateway(["grok-4.7-fast"]), connections=Settings(default_model="anymodel:cc/claude-opus-4-6",
                                                                           default_pick="old"),
                xpiki_models=list(bot.XPIKI_MODELS))


class XpikiTest(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_two_opus_models_and_no_other_gateway(self):
        data = data_for(gateway(["claude-opus-5-5", "claude-opus-4-8", "claude-sonnet-5-5", "gpt-6"]))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data["models"], ["xpiki:claude-opus-5-5", "xpiki:claude-opus-4-8"])
        self.assertEqual(data["chat_default"], "xpiki:claude-opus-5-5")
        self.assertEqual(data["fallbacks"], ["xpiki:claude-opus-5-5", "xpiki:claude-opus-4-8"])
        data["anymodel_chat"].models.list.assert_not_awaited()
        data["reformboss"].models.list.assert_not_awaited()
        self.assertEqual([bot.model_title(m) for m in data["models"]], ["Claude Opus 5.5", "Claude Opus 4.8"])

    async def test_a_closed_or_partial_model_list_still_offers_the_bought_models(self):
        for client in (gateway(["claude-opus-5-5"]), gateway(error=RuntimeError("closed"))):
            data = data_for(client)
            await bot.discover_models(NS(bot_data=data))
            self.assertEqual(data["models"], ["xpiki:claude-opus-5-5", "xpiki:claude-opus-4-8"])

    async def test_a_rejected_key_offers_nothing(self):
        request = httpx.Request("GET", "https://api.xpiki.com/v1/models")
        error = AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)
        data = data_for(gateway(error=error))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data["models"], [])

    async def test_without_the_key_the_usual_catalog_stays(self):
        data = data_for(None)
        self.assertEqual(bot.chat_sources(data), bot.CHAT_SOURCES)
        self.assertIs(bot.chat_model_catalog(data), bot.MODEL_CATALOG)

    def test_the_gateway_is_built_from_its_key(self):
        self.assertEqual(bot.EXTRA_PROVIDERS["xpiki"][0], "https://api.xpiki.com/v1")
        self.assertEqual(bot.CHAT_PROVIDERS["xpiki:"], "xpiki")


if __name__ == "__main__":
    unittest.main()
