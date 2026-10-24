"""Oxion trial uses fake provider APIs and never consumes real account tokens."""
import asyncio
import re
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import httpx
from openai import AuthenticationError, NotFoundError, PermissionDeniedError

import bot


class Settings:
    def __init__(self, **values):
        self.values = values

    def get_meta(self, key):
        return self.values.get(key)

    def set_meta(self, key, value):
        self.values[key] = value


def gateway(ids):
    return NS(models=NS(list=AsyncMock(return_value=NS(data=[NS(id=i) for i in ids]))))


def data_for(ids):
    return dict(oxionai=gateway(ids), anymodel_chat=gateway(["xai/grok-4.7"]),
                reformboss=gateway(["grok-4.7-fast"]), connections=Settings())


class OxionTest(unittest.IsolatedAsyncioTestCase):
    async def test_trial_excludes_previous_routes_and_migrates_default(self):
        data = data_for(["claude-sonnet-5","claude-opus-5","claude-sonnet-5.1"])
        data["connections"] = Settings(default_model="anymodel:xai/grok-4.7",default_pick="old")
        data.update(models=["anymodel:xai/grok-4.7"],fallbacks=["reformboss:grok-4.7-fast"],
                    premium={"anymodel:xai/grok-4.7":10},
                    memory=NS(model=lambda uid:"anymodel:xai/grok-4.7"))
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data["models"],["oxionai:claude-sonnet-5"])
        self.assertEqual(data["fallbacks"],["oxionai:claude-sonnet-5"])
        self.assertEqual(data["chat_default"],"oxionai:claude-sonnet-5")
        self.assertEqual(bot.user_model(data,1),"oxionai:claude-sonnet-5")
        self.assertEqual(bot.codex_model(data,"reformboss:grok-4.7-fast"),"oxionai:claude-sonnet-5")
        self.assertEqual(data["premium"],{})
        data["anymodel_chat"].models.list.assert_not_awaited()
        data["reformboss"].models.list.assert_not_awaited()

    async def test_exact_requested_ids_only_and_explicit_multiple_models(self):
        data = data_for(["claude-sonnet-5","claude-sonnet-5.1","claude-sonnet-50","claude-opus-5","gpt-5.6-sol"])
        data["oxionai_models"] = ["claude-opus-5","claude-sonnet-5"]
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data["models"],["oxionai:claude-opus-5","oxionai:claude-sonnet-5"])
        self.assertEqual(data["chat_default"],"oxionai:claude-opus-5")

    async def test_missing_requested_model_never_falls_back_to_old_providers(self):
        data = data_for(["claude-sonnet-5.1","claude-sonnet-50","x/claude-sonnet-5"])
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data["models"],[])
        self.assertEqual(data["fallbacks"],[])
        self.assertIsNone(data["chat_default"])
        data["anymodel_chat"].models.list.assert_not_awaited()

    async def test_authentication_or_permissions_failure_ignores_cached_routes(self):
        for cls,status in ((AuthenticationError,401),(PermissionDeniedError,403)):
            with self.subTest(status=status):
                data = data_for([])
                data["connections"] = Settings(**{"confirmed_chat_models:oxionai":["oxionai:claude-sonnet-5"]})
                response = httpx.Response(status,request=httpx.Request("GET","https://provider.test/models"))
                data["oxionai"].models.list.side_effect = cls("rejected",response=response,body={})
                await bot.discover_models(NS(bot_data=data))
                self.assertEqual(data["models"],[])
                self.assertEqual(data["fallbacks"],[])

    async def test_network_failure_recovers_only_confirmed_oxion_ids_after_restart(self):
        data = data_for(["claude-sonnet-5"])
        await bot.discover_models(NS(bot_data=data))
        offline = data_for([])
        offline["connections"] = data["connections"]
        offline["oxionai"].models.list.side_effect = RuntimeError("offline")
        await bot.discover_models(NS(bot_data=offline))
        self.assertEqual(offline["models"],["oxionai:claude-sonnet-5"])
        self.assertEqual(offline["fallbacks"],["oxionai:claude-sonnet-5"])

    async def test_network_failure_without_confirmed_oxion_id_does_not_guess(self):
        data = data_for([])
        data["connections"] = Settings(default_model="anymodel:xai/grok-4.7")
        data["oxionai"].models.list.side_effect = RuntimeError("offline")
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data["models"],[])

    async def test_removing_key_restores_old_providers_without_removing_their_config(self):
        data = data_for(["claude-sonnet-5"])
        await bot.discover_models(NS(bot_data=data))
        del data["oxionai"]
        await bot.discover_models(NS(bot_data=data))
        self.assertEqual(data["models"],["anymodel:xai/grok-4.7"])
        self.assertNotIn("oxionai:claude-sonnet-5",data["fallbacks"])
        self.assertEqual(data["chat_default"],"anymodel:xai/grok-4.7")

    async def test_only_oxion_refreshes_when_previous_gateways_are_absent(self):
        data = dict(oxionai=gateway(["claude-sonnet-5"]))
        self.assertTrue(await bot.refresh_chat_models(data))
        self.assertEqual(data["models"],["oxionai:claude-sonnet-5"])

    async def test_quick_answer_routes_exact_raw_id(self):
        create = AsyncMock(return_value=NS(choices=[NS(message=NS(content="Привет"))]))
        data = dict(oxionai=NS(chat=NS(completions=NS(create=create))))
        answer = await bot.quick_answer_once(data,"oxionai:claude-sonnet-5","Be helpful","Привет",2000)
        self.assertEqual(answer,"Привет")
        self.assertEqual(create.call_args.kwargs["model"],"claude-sonnet-5")
        self.assertEqual(bot.EXTRA_PROVIDERS["oxionai"][0],"https://www.oxionai.com/v1")

    async def test_streaming_preserves_tools_and_uses_oxion_client(self):
        async def stream():
            yield NS(choices=[NS(delta=NS(content="Готово",tool_calls=[]),finish_reason="stop")])
        create = AsyncMock(return_value=stream())
        data = dict(oxionai=NS(chat=NS(completions=NS(create=create))))
        live = NS(update=AsyncMock(),status=lambda value:None)
        tools = NS(definitions=lambda:[{"type":"function",**bot.WEB_TOOL["function"]}])
        answer,incomplete = await bot.chat_answer(NS(bot_data=data),live,
            dict(model="oxionai:claude-sonnet-5",input=[{"role":"user","content":"Привет"}]),tools)
        self.assertEqual(answer,"Готово")
        self.assertFalse(incomplete)
        self.assertTrue(create.call_args.kwargs["stream"])
        self.assertEqual(create.call_args.kwargs["model"],"claude-sonnet-5")
        self.assertTrue(create.call_args.kwargs["tools"])

    async def test_owner_status_reports_oxion_without_old_gateway_probes_or_key_leak(self):
        data = data_for(["claude-sonnet-5"])
        data["connections"] = Settings(owner=1)
        data["oxionai"].api_key = "cc-private-key-never-print"
        status = NS(delete=AsyncMock())
        with patch.object(bot,"authorized",AsyncMock(return_value=True)), \
             patch.object(bot,"reply",AsyncMock(return_value=status)), \
             patch.object(bot,"send",AsyncMock()) as send:
            await bot.api_status(NS(effective_user=NS(id=1),effective_message=NS()),NS(bot_data=data))
        shown = send.call_args.args[1]
        self.assertIn("Oxion AI",shown)
        self.assertIn("claude-sonnet-5",shown)
        self.assertNotIn(data["oxionai"].api_key,shown)
        self.assertNotIn("REFORMBOSS_API_KEY",shown)
        data["anymodel_chat"].models.list.assert_not_awaited()
        data["reformboss"].models.list.assert_not_awaited()

    async def test_request_refusal_does_not_remove_only_model_or_enable_old_gateway(self):
        data = data_for(["claude-sonnet-5"])
        await bot.discover_models(NS(bot_data=data))
        response = httpx.Response(404,request=httpx.Request("POST","https://provider.test/chat/completions"))
        bot.drop_model(data,"oxionai:claude-sonnet-5",NotFoundError("temporarily unavailable",response=response,body={}))
        self.assertEqual(data["models"],["oxionai:claude-sonnet-5"])
        self.assertEqual(data["fallbacks"],["oxionai:claude-sonnet-5"])


if __name__ == "__main__":
    unittest.main()
