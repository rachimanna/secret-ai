import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import approvals
import automations
import bot
import mcpbridge
import moderation
import pdfscan
import playboard
import subscriptions
import workbench
from telegram.error import TelegramError

DAY = 86400
NOON = 1800000000


class LimitsTest(unittest.TestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.subs = self.memory.subs
        self.subs.free_limit, self.subs.pro_limit = 2, 4

    def test_free_limit_stops_and_a_new_day_starts_over(self):
        self.assertTrue(self.subs.spend(1, NOON))
        self.assertTrue(self.subs.spend(1, NOON))
        self.assertFalse(self.subs.spend(1, NOON))
        self.assertEqual(self.subs.left(1, NOON), 0)
        self.assertTrue(self.subs.spend(1, NOON + DAY))

    def test_refund_returns_a_failed_message(self):
        self.subs.spend(1, NOON)
        self.subs.spend(1, NOON)
        self.subs.refund(1, NOON)
        self.assertTrue(self.subs.spend(1, NOON))

    def test_zero_turns_the_limit_off(self):
        self.subs.free_limit = 0
        for _ in range(5):
            self.assertTrue(self.subs.spend(1, NOON))
        self.assertIsNone(self.subs.left(1, NOON))

    def test_pro_lifts_the_limit_and_ends_after_grace(self):
        until = self.subs.activate(1, NOON + DAY, now=NOON)
        self.assertEqual(self.subs.limit(1, NOON), 4)
        self.assertTrue(self.subs.is_pro(1, until + subscriptions.GRACE - 1))
        self.assertFalse(self.subs.is_pro(1, until + subscriptions.GRACE))

    def test_activation_never_shortens(self):
        self.subs.activate(1, NOON + 5 * DAY, now=NOON)
        self.assertEqual(self.subs.activate(1, NOON + DAY, now=NOON), NOON + 5 * DAY)

    def test_payload(self):
        self.assertEqual(subscriptions.parse("sub:pro:42"), 42)
        for bad in ("gen:s:42", "sub:pro:x", "sub:other:1", "", None, "sub:pro"):
            self.assertIsNone(subscriptions.parse(bad))


class PaymentTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.data = {"memory": self.memory, "subs": self.memory.subs}

    def payment_update(self, user=5, charge="c1"):
        payment = NS(invoice_payload=f"sub:pro:5", telegram_payment_charge_id=charge, total_amount=299,
                     subscription_expiration_date=None)
        return NS(effective_message=NS(successful_payment=payment), effective_user=NS(id=user, language_code="ru"))

    async def test_payment_activates_once_per_charge_and_gives_credits(self):
        with patch.object(bot, "send", AsyncMock()) as sent:
            await bot.paid(self.payment_update(), NS(bot_data=self.data))
            await bot.paid(self.payment_update(), NS(bot_data=self.data))
        self.assertTrue(self.memory.subs.is_pro(5))
        self.assertEqual(self.memory.gen_info(5)[1], self.memory.subs.bonus)
        self.assertEqual(sent.await_count, 1)

    async def test_renewal_is_a_new_charge(self):
        with patch.object(bot, "send", AsyncMock()):
            await bot.paid(self.payment_update(charge="c1"), NS(bot_data=self.data))
            await bot.paid(self.payment_update(charge="c2"), NS(bot_data=self.data))
        self.assertEqual(self.memory.gen_info(5)[1], 2 * self.memory.subs.bonus)

    async def test_someone_elses_payload_gives_nothing(self):
        await bot.paid(self.payment_update(user=6), NS(bot_data=self.data))
        self.assertFalse(self.memory.subs.is_pro(5))
        self.assertFalse(self.memory.subs.is_pro(6))

    async def test_precheckout_checks_price_and_person(self):
        for user, amount, expected in ((5, self.memory.subs.stars, True), (6, self.memory.subs.stars, False), (5, 1, False)):
            query = NS(invoice_payload="sub:pro:5", from_user=NS(id=user), currency="XTR", total_amount=amount, answer=AsyncMock())
            await bot.precheckout(NS(pre_checkout_query=query), NS(bot_data=self.data))
            self.assertEqual(query.answer.call_args.kwargs["ok"], expected)


class RespondLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_over_the_limit_shows_the_offer_and_the_owner_is_free(self):
        memory = bot.Memory(":memory:")
        self.addCleanup(memory.db.close)
        memory.subs.free_limit = 1
        data = {"memory": memory, "subs": memory.subs, "connections": NS(get_meta=lambda name, *a: 9)}
        context = NS(bot_data=data, bot=NS(create_invoice_link=AsyncMock(return_value="https://t.me/invoice")))
        update = NS(effective_user=NS(id=1, language_code="ru"))
        with patch.object(bot, "send", AsyncMock()) as sent:
            self.assertTrue(await bot.take_message(update, context, object()))
            self.assertIsNone(await bot.take_message(update, context, object()))
        self.assertIn("в месяц", sent.call_args.args[1])
        owner = NS(effective_user=NS(id=9, language_code="ru"))
        for _ in range(3):
            self.assertFalse(await bot.take_message(owner, context, object()))


class ApprovalTest(unittest.IsolatedAsyncioTestCase):
    def test_what_counts_as_reading(self):
        for name in ("gmail-find-email", "listEvents", "get_page", "search"):
            self.assertTrue(approvals.read_only({"name": name}), name)
        for name in ("gmail-send-email", "createPage", "delete_file", "do_thing", "get_and_update", "", "transfer"):
            self.assertFalse(approvals.read_only({"name": name}), name)

    def test_annotation_beats_the_name(self):
        self.assertTrue(approvals.read_only({"name": "send", "annotations": {"readOnlyHint": True}}))
        self.assertFalse(approvals.read_only({"name": "get_x", "annotations": {"readOnlyHint": False}}))
        self.assertFalse(approvals.read_only({"name": "get_x", "annotations": {"destructiveHint": True}}))

    async def asked(self, gate, press):
        tg = NS(send_message=AsyncMock(return_value=NS(edit_text=AsyncMock())))
        task = asyncio.create_task(gate.ask(tg, 1, "ru", "Gmail", "send", {"to": "a"}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        code = next(iter(gate.waiting))
        outcome = press(code)
        return await task, outcome, tg

    async def test_allow_decline_and_foreign_press(self):
        gate = approvals.Approvals()
        self.assertEqual((await self.asked(gate, lambda c: gate.resolve(c, 1, "y")))[0], "yes")
        self.assertEqual((await self.asked(gate, lambda c: gate.resolve(c, 1, "n")))[0], "no")
        self.assertEqual(gate.resolve("nothing", 1, "y"), "gone")
        tg = NS(send_message=AsyncMock(return_value=NS(edit_text=AsyncMock())))
        task = asyncio.create_task(gate.ask(tg, 1, "ru", "Gmail", "send", {}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        code = next(iter(gate.waiting))
        self.assertEqual(gate.resolve(code, 2, "y"), "foreign")
        self.assertFalse(task.done())
        gate.resolve(code, 1, "n")
        await task

    async def test_timeout_and_undelivered_do_nothing(self):
        gate = approvals.Approvals(wait=0.01)
        tg = NS(send_message=AsyncMock(return_value=NS(edit_text=AsyncMock())))
        self.assertEqual(await gate.ask(tg, 1, "ru", "Gmail", "send", {}), "timeout")
        tg = NS(send_message=AsyncMock(side_effect=TelegramError("no")))
        self.assertEqual(await gate.ask(tg, 1, "ru", "Gmail", "send", {}), "undelivered")
        self.assertFalse(gate.waiting)

    async def test_trust_lasts_for_one_tool_of_one_person(self):
        gate = approvals.Approvals()
        await self.asked(gate, lambda c: gate.resolve(c, 1, "t"))
        tg = NS(send_message=AsyncMock())
        self.assertEqual(await gate.ask(tg, 1, "ru", "Gmail", "send", {}), "yes")
        tg.send_message.assert_not_awaited()
        self.assertFalse(gate.trusts(2, "send"))
        self.assertFalse(gate.trusts(1, "delete"))

    async def test_gate_in_the_model_loop(self):
        apps = NS(read_only=lambda name: name == "get", app=lambda name: "Gmail")
        live = NS(status=lambda note: None)
        toolbox = NS(user_id=1, language="ru")
        gate = NS(ask=AsyncMock(return_value="no"))
        context = NS(bot_data={"approvals": gate}, bot=object())
        self.assertIsNone(await bot.app_gate(context, live, toolbox, apps, "get", {}))
        gate.ask.assert_not_awaited()
        self.assertIn("NOT performed", await bot.app_gate(context, live, toolbox, apps, "send", {}))
        gate.ask.return_value = "yes"
        self.assertIsNone(await bot.app_gate(context, live, toolbox, apps, "send", {}))
        unattended = NS(bot_data={"approvals": gate}, bot=object(), unattended=True)
        gate.ask.reset_mock()
        self.assertIn("only read", await bot.app_gate(unattended, live, toolbox, apps, "send", {}))
        self.assertIn("only read", await bot.app_gate(unattended, live, None, apps, "send", {}))
        gate.ask.assert_not_awaited()

    async def test_bridge_marks_reading_tools(self):
        tools = [{"name": "list_items"}, {"name": "send_item"}, {"name": "odd", "annotations": {"readOnlyHint": True}}]
        with patch.object(mcpbridge.Server, "open", AsyncMock()), patch.object(mcpbridge.Server, "tools", AsyncMock(return_value=tools)):
            bridge = await mcpbridge.load(object(), [{"server_label": "app-test-marks", "server_url": "https://example.com/mcp"}])
        names = {route[1]: name for name, route in bridge.routes.items()}
        self.assertTrue(bridge.read_only(names["list_items"]))
        self.assertTrue(bridge.read_only(names["odd"]))
        self.assertFalse(bridge.read_only(names["send_item"]))

    async def test_openai_gets_only_reading_tools(self):
        class Stop(Exception):
            pass
        seen = {}

        async def fake_round(context, live, prefix, request):
            seen.update(request)
            raise Stop()
        toolbox = NS(definitions=lambda: [{"type": "mcp", "server_label": "x"}, {"type": "function", "name": "y"}])
        context = NS(bot_data={"approvals": object(), "openai": object()})
        with patch.object(bot, "model_round", fake_round), self.assertRaises(Stop):
            await bot.generate(context, NS(text=""), toolbox, model="gpt-x", instructions="", input=[])
        self.assertEqual(seen["tools"][0]["allowed_tools"], {"read_only": True})
        self.assertNotIn("allowed_tools", seen["tools"][1])


class AutomationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.store = self.memory.automations
        self.rule = {"time": "09:00", "tz": "UTC", "days": list(range(7))}

    def test_parse(self):
        rule, task = automations.parse("каждый день в 09:00: собери письма", "UTC")
        self.assertEqual((rule["time"], len(rule["days"]), task), ("09:00", 7, "собери письма"))
        self.assertEqual(automations.parse("по будням 18:30 - отчёт", "UTC")[0]["days"], [0, 1, 2, 3, 4])
        for bad in ("завтра: что-то", "каждый день в 09:00:", "", "каждый день в 25:00: x"):
            self.assertIsNone(automations.parse(bad, "UTC"), bad)

    def test_slots_and_ownership(self):
        first = self.store.add(1, self.rule, "a", "ru", 1)
        self.assertIsNone(self.store.add(1, self.rule, "b", "ru", 1))
        self.assertFalse(self.store.delete(2, first))
        self.assertTrue(self.store.delete(1, first))

    def test_due_task_moves_on_before_it_runs(self):
        self.store.add(1, self.rule, "a", "ru", 1, now=NOON)
        due = self.store.list(1)[0]["next_at"]
        self.assertEqual(self.store.claim_due(now=due - 1), [])
        self.assertEqual(len(self.store.claim_due(now=due)), 1)
        self.assertEqual(self.store.claim_due(now=due), [])

    def test_repeated_failures_pause(self):
        item = self.store.add(1, self.rule, "a", "ru", 1, now=NOON)
        for _ in range(automations.FAILURES_TO_PAUSE - 1):
            self.assertFalse(self.store.finished(item, False))
        self.assertTrue(self.store.finished(item, False))
        self.assertEqual(self.store.claim_due(now=NOON + 9 * DAY), [])

    def test_reset_forgets_them(self):
        self.store.add(1, self.rule, "a", "ru", 1)
        self.memory.board.submit(1, "A", "vault", 10, now=NOON)
        self.memory.reset(1)
        self.assertEqual(self.store.list(1), [])
        self.assertIsNone(self.memory.board.top(1, now=NOON)["mine"])

    async def test_a_run_sends_the_result_and_refunds_a_failure(self):
        self.memory.subs.free_limit = 5
        data = {"memory": self.memory, "subs": self.memory.subs, "connections": NS(get_meta=lambda *a: None, public=lambda u: []),
                "http": None, "pending": {}, "allowed": None, "premium": {}}
        app = NS(bot_data=data, bot=NS(send_message=AsyncMock()))
        item = {"id": self.store.add(1, self.rule, "a", "ru", 1), "user_id": 1, "task": "a", "language": "ru"}
        with patch.object(bot, "user_model", lambda d, u: "m"), patch.object(bot, "app_tools", AsyncMock(return_value=([], []))), \
                patch.object(bot, "Toolbox", lambda *a, **k: NS()), patch.object(bot, "generate", AsyncMock(return_value=("готово", False))) as run:
            await bot.run_automation(app, item)
        self.assertTrue(run.call_args.args[0].unattended)
        self.assertIn("готово", app.bot.send_message.call_args.args[1])
        self.assertEqual(self.memory.subs.used(1), 1)
        with patch.object(bot, "user_model", lambda d, u: "m"), patch.object(bot, "app_tools", AsyncMock(return_value=([], []))), \
                patch.object(bot, "Toolbox", lambda *a, **k: NS()), patch.object(bot, "generate", AsyncMock(side_effect=ValueError("x"))):
            await bot.run_automation(app, item)
        self.assertEqual(self.memory.subs.used(1), 1)


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.board = self.memory.board

    def test_best_per_game_summed_and_place(self):
        self.board.submit(1, "Аня", "vault", 100, now=NOON)
        self.board.submit(1, "Аня", "vault", 50, now=NOON + 5)
        self.board.submit(1, "Аня", "route", 30, now=NOON + 10)
        self.board.submit(2, "Боря", "vault", 500, now=NOON + 15)
        top = self.board.top(1, now=NOON + 20)
        self.assertEqual([(x["name"], x["score"], x["me"]) for x in top["top"]], [("Боря", 500, False), ("Аня", 130, True)])
        self.assertEqual(top["mine"], {"score": 130, "place": 2})

    def test_forged_values_are_refused(self):
        for kind, score in (("vault", playboard.MAX_SCORE + 1), ("vault", -1), ("x", 5), ("vault", "9"), ("vault", True), ("vault", 1.5)):
            self.assertFalse(self.board.submit(1, "A", kind, score, now=NOON), (kind, score))

    def test_too_fast_is_refused_and_a_new_week_starts_clean(self):
        self.assertTrue(self.board.submit(1, "A", "vault", 5, now=NOON))
        self.assertFalse(self.board.submit(1, "A", "vault", 9, now=NOON + 0.5))
        self.assertEqual(self.board.top(now=NOON + 8 * DAY)["top"], [])


def group_message(text="привет", entities=()):
    return NS(chat_id=-1, text=text, caption=None, entities=entities, caption_entities=(), is_automatic_forward=False, delete=AsyncMock())


class ModerationTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.mod = moderation.Moderation(self.memory.db)
        self.tg = NS(get_chat_member=AsyncMock(return_value=NS(status="member")), send_message=AsyncMock())

    async def test_off_by_default(self):
        message = group_message("https://spam.example")
        self.assertFalse(await self.mod.check(self.tg, message, NS(id=1, first_name="A")))
        message.delete.assert_not_awaited()

    async def test_a_newcomers_link_goes_and_a_regular_stays(self):
        self.mod.set_enabled(-1, True)
        link = group_message("зайди на t.me/spam")
        self.assertTrue(await self.mod.check(self.tg, link, NS(id=1, first_name="A")))
        link.delete.assert_awaited_once()
        for index in range(moderation.TRUSTED_AFTER):
            self.assertIsNone(self.mod.verdict(-1, 2, group_message("привет"), now=index * 100))
        self.assertIsNone(self.mod.verdict(-1, 2, group_message("https://ok.example"), now=1000))

    async def test_admins_and_channel_posts_are_left_alone(self):
        self.mod.set_enabled(-1, True)
        self.tg.get_chat_member.return_value = NS(status="administrator")
        link = group_message("https://x.example")
        self.assertFalse(await self.mod.check(self.tg, link, NS(id=1, first_name="A")))
        link.delete.assert_not_awaited()
        self.tg.get_chat_member.return_value = NS(status="member")
        post = group_message("https://x.example")
        self.assertFalse(await self.mod.check(self.tg, post, NS(id=moderation.TELEGRAM_SERVICE, first_name="T")))

    def test_flood(self):
        results = [self.mod.verdict(-1, 3, group_message(), now=float(i)) for i in range(moderation.FLOOD_MESSAGES + 1)]
        self.assertEqual(results[-1], "flood")
        self.assertIsNone(self.mod.verdict(-1, 3, group_message(), now=100.0))

    async def test_no_right_to_delete_changes_nothing(self):
        self.mod.set_enabled(-1, True)
        link = group_message("https://x.example")
        link.delete.side_effect = TelegramError("no rights")
        self.assertFalse(await self.mod.check(self.tg, link, NS(id=1, first_name="A")))


class SearchTest(unittest.TestCase):
    def test_word_forms_match(self):
        self.assertGreater(workbench.score(workbench.terms("расчётов"), "В этих расчёты сходятся"), 0)
        self.assertGreater(workbench.score(workbench.terms("invoices"), "the invoice total"), 0)

    def test_substring_still_counts(self):
        self.assertGreater(workbench.score(["api"], "the apikey field"), 0)

    def test_rare_word_outweighs_a_common_one(self):
        texts = ["договор сумма", "договор срок", "договор неустойка"]
        rarity = workbench.weights(["договор", "неустойка"], texts)
        self.assertGreater(rarity["неустойка"], rarity["договор"])

    def test_short_words_keep_their_stem(self):
        self.assertEqual(workbench.stem("дом"), "дом")


class WiderSearchTest(unittest.IsolatedAsyncioTestCase):
    def controls(self, answer):
        import workbench_ui
        return workbench_ui.Controls(NS(user_model=lambda d, u: "m", quick_answer=answer))

    async def test_other_words_are_tried_and_duplicates_dropped(self):
        store = NS(files=lambda u, c, latest=False: [1], search=lambda u, c, q: [{"id": 1, "start": 0, "q": q}, {"id": 2, "start": 0, "q": q}])
        found = await self.controls(AsyncMock(return_value="штраф, пеня")).wider_search(NS(bot_data={}), store, 1, 1, "неустойка", [{"id": 1, "start": 0}])
        self.assertEqual([hit["id"] for hit in found], [1, 2])

    async def test_a_failing_model_keeps_what_was_found(self):
        store = NS(files=lambda u, c, latest=False: [1], search=lambda u, c, q: [])
        hits = [{"id": 1, "start": 0}]
        self.assertEqual(await self.controls(AsyncMock(side_effect=RuntimeError("x"))).wider_search(NS(bot_data={}), store, 1, 1, "q", hits), hits)


class ScanTest(unittest.IsolatedAsyncioTestCase):
    def document(self):
        return {"name": "s.pdf", "warnings": ["Only the PDF text layer is read; images/scans are not OCR processed."],
                "text": f"\n[Page 1]\nобычный текст\n\n[Page 2]\n{pdfscan.NO_TEXT}\n\n[Page 3]\n{pdfscan.NO_TEXT}\n"}

    def test_finds_pages_without_text(self):
        self.assertEqual(pdfscan.pages_without_text(self.document()["text"]), [2, 3])

    async def test_recognized_pages_replace_the_mark_and_warn(self):
        with patch.object(pdfscan, "_extract", AsyncMock(return_value={2: "x"})):
            result = await pdfscan.recognize(self.document(), b"", AsyncMock(return_value="текст скана"))
        self.assertIn("[Page 2]\nтекст скана", result["text"])
        self.assertIn(f"[Page 3]\n{pdfscan.NO_TEXT}", result["text"])
        self.assertTrue(any("read by an AI" in w for w in result["warnings"]))
        self.assertTrue(any("not read" in w for w in result["warnings"]))
        self.assertFalse(any(w.startswith("Only the PDF") for w in result["warnings"]))

    async def test_failure_leaves_the_document_as_it_was(self):
        original = self.document()
        with patch.object(pdfscan, "_extract", AsyncMock(side_effect=RuntimeError("x"))):
            self.assertEqual(await pdfscan.recognize(original, b"", AsyncMock()), original)
        with patch.object(pdfscan, "_extract", AsyncMock(return_value={2: "x"})):
            self.assertEqual(await pdfscan.recognize(original, b"", AsyncMock(side_effect=RuntimeError("x"))), original)

    async def test_real_pdf_picture_is_extracted(self):
        import io
        import tempfile
        from pathlib import Path
        from PIL import Image
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as folder:
            out = io.BytesIO()
            Image.new("RGB", (300, 200), "white").save(out, "PDF")
            path = Path(folder) / "scan.pdf"
            path.write_bytes(out.getvalue())
            found = pdfscan.extract(str(path), [1, 9])
        self.assertEqual(list(found), [1])


class AlertTest(unittest.IsolatedAsyncioTestCase):
    async def test_owner_is_told_once_an_hour_and_network_noise_is_skipped(self):
        tg = NS(send_message=AsyncMock())
        context = NS(bot_data={"connections": NS(get_meta=lambda name: 9)}, bot=tg)
        await bot.alert_owner(context, "KeyError", "bot.py:1 f")
        await bot.alert_owner(context, "KeyError", "bot.py:1 f")
        await bot.alert_owner(context, "TimedOut", "bot.py:2 g")
        self.assertEqual(tg.send_message.await_count, 1)
        self.assertEqual(tg.send_message.call_args.args[0], 9)

    async def test_a_broken_alert_changes_nothing(self):
        await bot.alert_owner(NS(bot_data={}), "KeyError", "x")


class GroupExtrasTest(unittest.TestCase):
    def test_awards_and_predictions_build_on_the_group_store(self):
        groups = bot.GroupStore(":memory:")
        self.addCleanup(groups.db.close)
        data = {"groups": groups}
        self.assertIs(bot.week_awards(data), bot.week_awards(data))
        self.assertIs(bot.predict_cards(data), bot.predict_cards(data))


if __name__ == "__main__":
    unittest.main()
