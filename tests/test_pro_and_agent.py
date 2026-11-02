import asyncio
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import approvals
import automations
import bot
import groupdocs
import embeddings
import research
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
        memory.subs.trial_days = 0
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


class GroupProTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.subs = self.memory.subs

    def test_payload(self):
        self.assertEqual(subscriptions.parse_group("gsub:-100:5"), (-100, 5))
        for bad in ("gsub:100:5", "gsub:x:5", "sub:pro:5", "", None):
            self.assertIsNone(subscriptions.parse_group(bad))

    def test_no_plan_spends_nothing(self):
        self.assertFalse(self.subs.spend_in_group(-1, 1, NOON))

    def test_member_cap_and_shared_pool(self):
        self.subs.group_pool = 3
        self.subs.activate_group(-1, 9, NOON + DAY, now=NOON)
        self.subs.set_member_limit(-1, 2)
        self.assertTrue(self.subs.spend_in_group(-1, 1, NOON))
        self.assertTrue(self.subs.spend_in_group(-1, 1, NOON))
        self.assertFalse(self.subs.spend_in_group(-1, 1, NOON))
        self.assertTrue(self.subs.spend_in_group(-1, 2, NOON))
        self.assertFalse(self.subs.spend_in_group(-1, 3, NOON))
        self.assertTrue(self.subs.spend_in_group(-1, 3, NOON + DAY))

    def test_plan_ends_after_grace(self):
        self.subs.activate_group(-1, 9, NOON, now=NOON - DAY)
        self.assertTrue(self.subs.group_is_pro(-1, NOON + subscriptions.GRACE - 1))
        self.assertFalse(self.subs.spend_in_group(-1, 1, NOON + subscriptions.GRACE))

    async def test_only_an_admin_passes_precheckout_and_payment_activates_once(self):
        data = {"memory": self.memory, "subs": self.subs}
        for status, expected in (("administrator", True), ("member", False)):
            tg = NS(get_chat_member=AsyncMock(return_value=NS(status=status)))
            query = NS(invoice_payload="gsub:-1:5", from_user=NS(id=5), currency="XTR", total_amount=self.subs.group_stars, answer=AsyncMock())
            await bot.precheckout(NS(pre_checkout_query=query), NS(bot_data=data, bot=tg))
            self.assertEqual(query.answer.call_args.kwargs["ok"], expected)
        payment = NS(invoice_payload="gsub:-1:5", telegram_payment_charge_id="g1", total_amount=self.subs.group_stars, subscription_expiration_date=None)
        update = NS(effective_message=NS(successful_payment=payment), effective_user=NS(id=5, language_code="ru"))
        tg = NS(send_message=AsyncMock())
        with patch.object(bot, "send", AsyncMock()):
            await bot.paid(update, NS(bot_data=data, bot=tg))
            await bot.paid(update, NS(bot_data=data, bot=tg))
        self.assertTrue(self.subs.group_is_pro(-1))
        self.assertEqual(tg.send_message.await_count, 1)
        self.assertEqual(self.subs.numbers()["groups_active"], 1)

    def test_numbers_show_the_funnel(self):
        for user in (1, 2, 3, 4):
            self.subs.note_limit_hit(user, NOON)
        self.subs.record_payment("a", 1, 299, now=NOON)
        self.subs.record_payment("b", 2, 299, now=NOON - 40 * DAY)
        self.subs.record_payment("c", 2, 299, now=NOON)
        self.subs.activate(1, NOON + DAY, now=NOON)
        numbers = self.subs.numbers(NOON + 60)
        self.assertEqual((numbers["limit_hit_week"], numbers["bought_week"], numbers["renewed_week"], numbers["conversion"], numbers["pro_active"], numbers["stars_week"]),
                         (4, 1, 1, 25.0, 1, 598))


class ButtonsInsteadOfCommandsTest(unittest.IsolatedAsyncioTestCase):
    def test_new_commands_are_not_in_the_menu_or_help(self):
        for language in bot.COMMANDS.values():
            names = {name for name, _ in language}
            self.assertFalse(names & {"pro", "auto", "antispam", "awards", "predict", "invite"})
        self.assertLessEqual(len(bot.COMMANDS["ru"]), 6)

    async def test_the_limit_message_has_a_buy_button_and_no_command(self):
        memory = bot.Memory(":memory:")
        self.addCleanup(memory.db.close)
        tg = NS(create_invoice_link=AsyncMock(return_value="https://t.me/invoice"))
        text, keyboard = await bot.pro_offer(tg, {"subs": memory.subs}, 1, True, limit_reached=True)
        self.assertIn("Бесплатный лимит закончился", text)
        self.assertNotIn("/pro", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].url, "https://t.me/invoice")
        self.assertIn("Купить Pro", keyboard.inline_keyboard[0][0].text)

    def test_private_chat_button_opens_the_plans(self):
        button = bot.private_chat_button({"bot_name": "secret_bot"}, True).inline_keyboard[0][0]
        self.assertEqual(button.url, "https://t.me/secret_bot?start=pro")
        self.assertIsNone(bot.private_chat_button({}, True))


class AutomationScreenTest(unittest.TestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.store = self.memory.automations
        self.rule = {"time": "09:00", "tz": "UTC", "days": list(range(7))}

    def test_pause_resume_belongs_to_the_owner(self):
        item = self.store.add(1, self.rule, "a", "ru", 1, now=NOON)
        self.assertFalse(self.store.set_paused(2, item, True))
        self.assertTrue(self.store.set_paused(1, item, True, now=NOON))
        self.assertEqual(self.store.claim_due(now=NOON + 9 * DAY), [])
        self.assertTrue(self.store.set_paused(1, item, False, now=NOON + 9 * DAY))
        self.assertFalse(self.store.list(1)[0]["paused"])

    def test_only_the_last_runs_are_kept_and_deleted_with_the_task(self):
        item = self.store.add(1, self.rule, "a", "ru", 1, now=NOON)
        for index in range(automations.RUNS_KEPT + 3):
            self.store.log_run(item, True, f"r{index}", now=NOON + index)
        runs = self.store.runs(item)
        self.assertEqual(len(runs), automations.RUNS_KEPT)
        self.assertEqual(runs[0]["text"], f"r{automations.RUNS_KEPT + 2}")
        self.store.delete(1, item)
        self.assertEqual(self.store.runs(item), [])


class ChatToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.context = NS(bot_data={"memory": self.memory, "subs": self.memory.subs, "connections": NS(get_meta=lambda *a: None)},
                          bot=NS(send_message=AsyncMock(), create_invoice_link=AsyncMock(return_value="https://t.me/i")), application=NS())

    async def test_a_task_is_created_from_the_conversation(self):
        tool = bot.schedule_tool(self.context, 1, "ru")
        self.assertIn("Created", await tool["run"]({"when": "по будням в 18:00", "task": "отчёт"}))
        self.assertEqual(self.memory.automations.list(1)[0]["task"], "отчёт")
        self.assertIn("slots", await tool["run"]({"when": "каждый день в 09:00", "task": "ещё"}))
        self.assertIn("Not created", await tool["run"]({"when": "завтра", "task": "x"}))

    async def test_show_pro_sends_a_button(self):
        await bot.pro_tool(self.context, 1, "ru")["run"]({})
        self.assertIsNotNone(self.context.bot.send_message.call_args.kwargs["reply_markup"])

    def test_words_that_need_these_tools(self):
        for text in ("каждое утро присылай новости", "сделай исследование про рынок", "сколько стоит подписка", "закончился лимит", "по будням напоминай"):
            self.assertTrue(bot.needs_tools(text), text)
        self.assertFalse(bot.needs_tools("как дела"))


class ResearchTest(unittest.IsolatedAsyncioTestCase):
    def test_queries_fall_back_to_the_topic(self):
        self.assertEqual(research.queries_from('Вот: ["а", "б", "а"]', "т"), ["а", "б"])
        for bad in ("нет json", '["', "", None, "[1, 2]"):
            self.assertEqual(research.queries_from(bad, "т"), ["т"])

    async def test_report_comes_only_from_what_was_found(self):
        ask = AsyncMock(side_effect=['["a", "b"]', "ОТЧЁТ"])
        async def search(query):
            return "Nothing found." if query == "b" else "факт https://x.example"
        report, count = await research.write_report("тема", "ru", ask, search)
        self.assertEqual((report, count), ("ОТЧЁТ", 1))
        notes = ask.call_args_list[1].args[1]
        self.assertIn("https://x.example", notes)
        self.assertNotIn("Nothing found", notes)

    async def test_no_sources_no_report(self):
        ask = AsyncMock(return_value='["a"]')
        with self.assertRaises(research.NoSources):
            await research.write_report("т", "ru", ask, AsyncMock(return_value="Web search is unavailable right now; x"))
        self.assertEqual(ask.await_count, 1)

    async def run_with(self, write):
        memory = bot.Memory(":memory:")
        self.addCleanup(memory.db.close)
        memory.subs.research_free = 1
        tg = NS(send_message=AsyncMock(), send_document=AsyncMock())
        data = {"memory": memory, "subs": memory.subs, "connections": NS(get_meta=lambda *a: None), "search_model": "m", "premium": {}}
        app = NS(bot_data=data, bot=tg)
        with patch.object(bot, "user_model", lambda d, u: "m"), patch.object(research, "write_report", write):
            self.assertIsNone(await bot.start_research(app, 1, "ru", "тема"))
            self.assertEqual(await bot.start_research(app, 1, "ru", "другая"), "busy")
            await asyncio.gather(*data["gen_tasks"])
        return memory, tg, app

    async def test_success_sends_the_file_and_uses_the_allowance(self):
        memory, tg, app = await self.run_with(AsyncMock(return_value=("Заголовок\nтекст", 2)))
        tg.send_document.assert_awaited_once()
        self.assertEqual(await bot.start_research(app, 1, "ru", "ещё"), "limit")

    async def test_failure_gives_the_allowance_back(self):
        memory, tg, app = await self.run_with(AsyncMock(side_effect=research.NoSources()))
        tg.send_document.assert_not_awaited()
        self.assertTrue(memory.subs.research_take(1))

    async def test_no_search_and_empty_topic(self):
        data = {"connections": NS(get_meta=lambda *a: None)}
        self.assertEqual(await bot.start_research(NS(bot_data=data), 1, "ru", "т"), "no_search")
        self.assertEqual(await bot.start_research(NS(bot_data=data), 1, "ru", "  "), "empty")


class EmbeddingsTest(unittest.IsolatedAsyncioTestCase):
    def finder(self, vectors):
        memory = bot.Memory(":memory:")
        self.addCleanup(memory.db.close)
        calls = []

        async def create(model, input):
            calls.append(list(input))
            return NS(data=[NS(embedding=vectors[text]) for text in input])
        return embeddings.Embeddings(memory.db, NS(embeddings=NS(create=create)), "m"), calls

    async def test_closest_first_weak_dropped_and_paid_once(self):
        finder, calls = self.finder({"штраф": [1.0, 0.0], "неустойка": [0.9, 0.1], "погода": [0.0, 1.0]})
        ranked = await finder.rank("штраф", ["погода", "неустойка"])
        self.assertEqual([index for index, _ in ranked], [1])
        count = sum(len(c) for c in calls)
        await finder.rank("штраф", ["погода", "неустойка"])
        self.assertEqual(sum(len(c) for c in calls), count)

    async def test_vectors_are_shortened_and_survive_storage(self):
        finder, _ = self.finder({"a": [1.0] * 1536})
        first = (await finder.vectors(["a"]))["a"]
        self.assertEqual(len(first), embeddings.SIZE)
        again = (await finder.vectors(["a"]))["a"]
        self.assertAlmostEqual(similarity_of(first, again), 1.0, places=5)

    async def test_service_failure_means_no_results(self):
        memory = bot.Memory(":memory:")
        self.addCleanup(memory.db.close)
        finder = embeddings.Embeddings(memory.db, NS(embeddings=NS(create=AsyncMock(side_effect=RuntimeError("x")))), "m")
        self.assertEqual(await finder.rank("q", ["a"]), [])


def similarity_of(a, b):
    return embeddings.similarity(a, b)


class TaskZoneTest(unittest.TestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)

    def test_someone_without_a_profile_still_gets_a_zone(self):
        self.assertEqual(bot.task_zone(self.memory, 1, "ru"), "Europe/Moscow")
        self.assertEqual(bot.task_zone(self.memory, 1, "en"), "UTC")

    def test_the_device_zone_wins_and_a_bad_one_is_ignored(self):
        self.assertEqual(bot.task_zone(self.memory, 1, "ru", "Asia/Almaty"), "Asia/Almaty")
        self.assertEqual(bot.task_zone(self.memory, 1, "ru", "Not/AZone"), "Europe/Moscow")

    def test_every_choice_on_the_screen_is_understood(self):
        for when in ("каждый день", "по будням", "по понедельникам", "по пятницам", "по воскресеньям", "каждое воскресенье",
                     "every day", "weekdays", "every monday", "every friday", "every sunday"):
            self.assertIsNotNone(bot.recurring.parse(when + " 09:00", "UTC"), when)


class TrialAndBonusTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.subs = self.memory.subs
        self.subs.free_limit, self.subs.pro_limit = 1, 5

    def test_trial_runs_once_and_lifts_only_the_message_limit(self):
        self.assertTrue(self.subs.start_trial(1, NOON))
        self.assertEqual(self.subs.limit(1, NOON), 5)
        self.assertFalse(self.subs.is_pro(1, NOON))
        self.assertEqual(self.subs.research_limit(1, NOON), self.subs.research_free)
        self.assertIsNone(self.subs.start_trial(1, NOON + 90 * DAY))
        self.assertEqual(self.subs.limit(1, NOON + 3 * DAY + 1), 1)

    def test_a_subscriber_gets_no_trial_and_zero_days_turn_it_off(self):
        self.subs.activate(1, NOON + DAY, now=NOON)
        self.assertIsNone(self.subs.start_trial(1, NOON))
        self.subs.trial_days = 0
        self.assertIsNone(self.subs.start_trial(2, NOON))

    def test_friend_days_add_up_to_a_cap_and_keep_the_trial(self):
        self.subs.grant_days(1, 3, NOON)
        self.assertEqual(self.subs.bonus_until(1), NOON + 3 * DAY)
        self.subs.grant_days(1, 3, NOON)
        self.assertEqual(self.subs.bonus_until(1), NOON + 6 * DAY)
        self.assertTrue(self.subs.trial_available(1))
        self.subs.grant_days(1, 500, NOON)
        self.assertEqual(self.subs.bonus_until(1), NOON + subscriptions.BONUS_CAP_DAYS * DAY)

    async def test_the_first_limit_starts_the_trial_and_answers(self):
        data = {"memory": self.memory, "subs": self.subs, "connections": NS(get_meta=lambda *a: None)}
        context = NS(bot_data=data, bot=NS(create_invoice_link=AsyncMock(return_value="https://t.me/i")))
        update = NS(effective_user=NS(id=1, language_code="ru"))
        with patch.object(bot, "send", AsyncMock()) as sent:
            self.assertTrue(await bot.take_message(update, context, object()))
            self.assertTrue(await bot.take_message(update, context, object()))
        self.assertIn("пробный период", sent.call_args.args[1])
        self.assertEqual(self.subs.numbers()["trials_week"], 1)
        with patch.object(bot, "send", AsyncMock()) as sent:
            for _ in range(3):
                self.assertTrue(await bot.take_message(update, context, object()))
            self.assertIsNone(await bot.take_message(update, context, object()))
        self.assertIn("Купить Pro", sent.call_args.args[2].inline_keyboard[0][0].text)

    async def test_an_invite_gives_days_to_the_person_who_invited(self):
        tg = NS(send_message=AsyncMock())
        update = NS(effective_user=NS(id=2, language_code="ru"), effective_message=NS(reply_text=AsyncMock()))
        with patch.object(bot, "send", AsyncMock()):
            await bot.accept_invite(update, NS(bot_data={"memory": self.memory, "subs": self.subs}, bot=tg), 1)
        self.assertTrue(self.subs.has_bonus(1))
        self.assertNotIn("/invite", tg.send_message.call_args.args[1])


class FirstStepsTest(unittest.IsolatedAsyncioTestCase):
    def test_buttons(self):
        rows = bot.first_steps_keyboard({"bot_name": "b", "public_url": "https://x.example"}, True).inline_keyboard
        self.assertEqual([r[0].callback_data for r in rows[:3]], ["onb:d", "onb:f", "onb:a"])
        self.assertEqual(rows[3][0].url, "https://t.me/b?startgroup=1")
        self.assertEqual(len(bot.first_steps_keyboard({}, True).inline_keyboard), 3)

    async def test_a_button_starts_a_real_request(self):
        query = NS(data="onb:d", answer=AsyncMock(), message=object())
        update = NS(callback_query=query, effective_user=NS(id=1, first_name="A", language_code="ru"))
        with patch.object(bot, "run_prompt", AsyncMock()) as run:
            await bot.first_step_button(update, NS(application=object(), bot_data={}))
        self.assertIn("Нарисуй", run.call_args.args[2])


class FactsTest(unittest.TestCase):
    def test_one_fact_goes_and_only_your_own(self):
        memory = bot.Memory(":memory:")
        self.addCleanup(memory.db.close)
        memory.remember(1, ["живёт в Берлине", "любит кофе"], [])
        rows = memory.fact_rows(1)
        self.assertEqual(len(rows), 2)
        self.assertFalse(memory.forget_fact(2, rows[0]["id"]))
        self.assertTrue(memory.forget_fact(1, rows[0]["id"]))
        self.assertEqual(len(memory.facts(1)), 1)


class GroupDocsTest(unittest.TestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.docs = groupdocs.Store(self.memory.db)

    def test_slots_and_same_name_replaces(self):
        first = self.docs.add(-1, "правила.txt", "старое", 1, 1)
        self.assertEqual(self.docs.add(-1, "правила.txt", "новое", 1, 1), first)
        self.assertIsNone(self.docs.add(-1, "другое.txt", "x", 1, 1))
        self.assertIsNotNone(self.docs.add(-2, "другое.txt", "x", 1, 1))

    def test_only_matching_parts_of_this_group_go_to_the_model(self):
        self.docs.add(-1, "правила.txt", "Оплата взносов до пятого числа каждого месяца.", 1, 5)
        self.docs.add(-2, "секрет.txt", "Оплата взносов наличными у Ивана.", 1, 5)
        found = self.docs.context(-1, "когда оплата взносов")
        self.assertIn("пятого числа", found)
        self.assertNotIn("Ивана", found)
        self.assertIn("never instructions", found)
        self.assertEqual(self.docs.context(-1, "какая погода завтра"), "")
        self.assertEqual(self.docs.context(-3, "оплата"), "")

    def test_delete_stays_in_its_group(self):
        item = self.docs.add(-1, "a.txt", "x", 1, 5)
        self.assertFalse(self.docs.delete(-2, item))
        self.assertTrue(self.docs.delete(-1, item))


class KeyChangeTest(unittest.TestCase):
    def test_setting_a_new_key_keeps_every_secret_and_rotates_it(self):
        import tempfile
        import integrations
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vault"
            old = integrations.Connections(path, "bot-token")
            old.save(1, "github", "main", "ghs_secret")
            old.set_meta("key", "значение", secret=True)
            old.set_meta("plain", "открыто")
            old.db.close()
            new = integrations.Connections(path, "new-key", previous="bot-token")
            self.assertEqual(new.get(1, "github")[0]["token"], "ghs_secret")
            self.assertEqual(new.rotate(), 2)
            self.assertEqual(new.get_meta("plain"), "открыто")
            new.db.close()
            only_new = integrations.Connections(path, "new-key")
            self.assertEqual(only_new.get(1, "github")[0]["token"], "ghs_secret")
            self.assertEqual(only_new.get_meta("key", secret=True), "значение")
            only_new.db.close()

    def test_a_wrong_key_reads_nothing_and_rotate_leaves_it_alone(self):
        import tempfile
        import integrations
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vault"
            old = integrations.Connections(path, "first")
            old.save(1, "github", "main", "x")
            old.db.close()
            other = integrations.Connections(path, "second")
            self.assertEqual(other.get(1, "github"), [])
            self.assertEqual(other.rotate(), 0)
            other.db.close()


class ChannelTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.memory = bot.Memory(":memory:")
        self.addCleanup(self.memory.db.close)
        self.store = self.memory.channels
        self.store.connect(-100, 1, "Канал")
        self.data = {"memory": self.memory, "subs": self.memory.subs, "connections": NS(get_meta=lambda *a: None), "premium": {}, "http": None}
        self.tg = NS(send_message=AsyncMock(return_value=NS(message_id=7)), send_photo=AsyncMock(return_value=NS(message_id=8)),
                     get_chat_member=AsyncMock(return_value=NS(status="administrator")), copy_message=AsyncMock())
        self.app = NS(bot_data=self.data, bot=self.tg)

    def press(self, action, item, user=1):
        query = NS(data=f"chn:{action}:{item}", answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        return NS(callback_query=query, effective_user=NS(id=user, language_code="ru")), NS(bot_data=self.data, bot=self.tg, application=self.app)

    async def draft(self):
        with patch.object(bot, "user_model", lambda d, u: "m"), patch.object(bot, "quick_answer", AsyncMock(return_value="Текст поста")):
            self.assertIsNone(await bot.channel_draft(self.app, 1, -100, "тема", False, "ru"))
        return self.store.db.execute("SELECT MAX(id) FROM channel_drafts").fetchone()[0]

    async def test_a_draft_goes_to_the_owner_not_the_channel(self):
        await self.draft()
        self.assertEqual(self.tg.send_message.call_args.args[0], 1)
        self.tg.copy_message.assert_not_awaited()

    async def test_publish_once_only_by_the_owner(self):
        item = await self.draft()
        await bot.channel_button(*self.press("p", item, user=2))
        self.tg.copy_message.assert_not_awaited()
        await bot.channel_button(*self.press("p", item))
        await bot.channel_button(*self.press("p", item))
        self.tg.copy_message.assert_awaited_once_with(-100, 1, 7)
        self.assertEqual(self.store.draft(item)["status"], "published")

    async def test_someone_no_longer_admin_cannot_publish(self):
        item = await self.draft()
        self.tg.get_chat_member.return_value = NS(status="member")
        await bot.channel_button(*self.press("p", item))
        self.tg.copy_message.assert_not_awaited()
        self.assertEqual(self.store.draft(item)["status"], "waiting")

    async def test_a_failed_publish_can_be_retried(self):
        item = await self.draft()
        self.tg.copy_message.side_effect = TelegramError("no rights")
        await bot.channel_button(*self.press("p", item))
        self.assertEqual(self.store.draft(item)["status"], "waiting")

    async def test_a_planned_post_is_only_a_draft(self):
        rule = {"time": "09:00", "tz": "UTC", "days": list(range(7))}
        self.assertIsNotNone(self.store.add_plan(1, -100, rule, "тема", False, "ru", 1, now=NOON))
        self.assertIsNone(self.store.add_plan(1, -100, rule, "ещё", False, "ru", 1, now=NOON))
        due = self.store.claim_due(now=NOON + 2 * DAY)
        self.assertEqual(len(due), 1)
        self.assertEqual(self.store.claim_due(now=NOON + 2 * DAY), [])
        with patch.object(bot, "user_model", lambda d, u: "m"), patch.object(bot, "quick_answer", AsyncMock(return_value="Пост")):
            await bot.run_channel_plan(self.app, due[0])
        self.tg.send_message.assert_awaited()
        self.tg.copy_message.assert_not_awaited()

    async def test_over_the_limit_no_draft_and_failure_gives_it_back(self):
        self.memory.subs.free_limit = 1
        self.tg.create_invoice_link = AsyncMock(return_value="https://t.me/i")
        with patch.object(bot, "user_model", lambda d, u: "m"), patch.object(bot, "quick_answer", AsyncMock(side_effect=RuntimeError("x"))):
            with self.assertRaises(RuntimeError):
                await bot.channel_draft(self.app, 1, -100, "тема", False, "ru")
        self.assertEqual(self.memory.subs.used(1), 0)
        await self.draft()
        with patch.object(bot, "user_model", lambda d, u: "m"):
            self.assertEqual(await bot.channel_draft(self.app, 1, -100, "тема", False, "ru"), "limit")

    async def test_adding_and_removing_the_bot(self):
        def change(status, can_post=True):
            return NS(my_chat_member=NS(chat=NS(type="channel", id=-200, title="Новый"), from_user=NS(id=5, language_code="ru"),
                                        new_chat_member=NS(status=status, can_post_messages=can_post)))
        context = NS(bot_data=self.data, bot=self.tg)
        await bot.channel_member(change("administrator"), context)
        self.assertTrue(self.store.is_owner(5, -200))
        await bot.channel_member(change("left"), context)
        self.assertFalse(self.store.is_owner(5, -200))
        await bot.channel_member(change("administrator", can_post=False), context)
        self.assertFalse(self.store.is_owner(5, -200))


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
