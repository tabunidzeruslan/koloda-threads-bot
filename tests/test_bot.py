import json
import random
import tempfile
import unittest
from datetime import date, datetime
from unittest import mock

from bot import crypto, net, state as st, writer
from bot.gemini import parse_json, rank_models
from bot.main import FREEDOM_OFF, FREEDOM_ON, Bot
from bot.schedule import TZ, make_slots


class FakeTG:
    def __init__(self, updates=None):
        self.sent, self.edits, self._updates, self.n = [], [], list(updates or []), 100

    def updates(self, offset, wait=0):
        ups = [u for u in self._updates if offset is None or u["update_id"] >= offset]
        return ups

    def set_commands(self, commands):
        self.commands = commands

    def send(self, chat, text, buttons=None, keyboard=None):
        self.n += 1
        self.sent.append((text, buttons))
        return self.n

    def edit(self, chat, mid, text):
        self.edits.append((mid, text))

    def answer(self, *a):
        pass

    def delete(self, *a):
        self.deleted = True

    def get_me(self):
        return {"username": "kolodabot"}


class FakeThreads:
    def __init__(self):
        self.published = []

    def my_posts(self, n):
        return [{"text": t} for t in self.published]

    def publish_text(self, text):
        self.published.append(text)
        return {"id": str(len(self.published)), "permalink": "https://threads.net/p/x"}


GOOD = "Дуб чи граб? Знаю родини, де це питання гостріше за футбол.\nА у вас вдома хто перемагає?"


def make_bot(now, updates=None, s=None):
    s = s or st.load("/nonexistent")
    s["owner_chat_id"] = 1
    s.setdefault("plan_v", 5)
    b = Bot(s, tg=FakeTG(updates), gm=mock.Mock(model="m"), threads_secret="tok_secret_123",
            enc_key="k", now=now, rnd=random.Random(1))
    b.th = FakeThreads()
    b.talk_hours = (0, 24)  # quiet hours are tested separately
    b.s.setdefault("day_trends", {"day": b.today_key(), "topics": []})  # no network in tests
    return b


def kyiv(h, m=0, d=22):
    return datetime(2026, 11, d, h, m, tzinfo=TZ)


class FilterTests(unittest.TestCase):
    def chk(self, text, sell=False, month=11):
        return writer.check(text, sell=sell, today=date(2026, month, 22), recent=[], examples=[])

    def test_good_passes(self):
        self.assertIsNone(self.chk(GOOD))

    def test_rejects(self):
        self.assertIn("хештег", self.chk(GOOD + " #дрова"))
        self.assertEqual(self.chk(GOOD + " 🔥"), "емодзі")
        self.assertEqual(self.chk("x" * 230), "довше 220 символів (230)")
        self.assertEqual(self.chk(GOOD + " Дзвоніть 050 950 50 90"), "контакт у непродаючому пості")
        self.assertEqual(self.chk(GOOD + " Всего 4500 грн"), "ціна")
        self.assertEqual(self.chk("Это ещё один пост про дрова и печку, который написан по-русски, а не нужно."), "російські літери")
        self.assertIn("сезон", self.chk("Спека на дворі, а ви вже думаєте про дрова на зиму? Розумно, чи ні?"))
        self.assertEqual(self.chk("- перше\n- друге, і ще трохи тексту, щоб довжина була нормальна для посту"), "список")

    def test_sell_allows_phone(self):
        self.assertIsNone(self.chk("Привеземо колоті дрова по Києву і області в день замовлення. Пишіть у дірект або дзвоніть: 050 950 50 90.", sell=True))

    def test_similarity(self):
        r = writer.check(GOOD, sell=False, today=date(2026, 11, 22), recent=[GOOD + "!"], examples=[])
        self.assertIn("схожий", r)


class MiscTests(unittest.TestCase):
    def test_crypto_roundtrip(self):
        blob = crypto.encrypt("THAAsecret", "key1")
        self.assertEqual(crypto.decrypt(blob, "key1"), "THAAsecret")
        self.assertIsNone(crypto.decrypt(blob, "key2"))
        self.assertNotIn("THAAsecret", blob)

    def test_slots(self):
        for seed in range(50):
            sl = make_slots(random.Random(seed))
            self.assertEqual(len(sl), 4)
            self.assertTrue(all(b - a >= 100 for a, b in zip(sl, sl[1:])))

    def test_rank_models(self):
        names = ["gemini-2.0-flash", "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.5-flash-image",
                 "gemini-3.0-flash-preview", "gemini-2.5-pro", "text-embedding-004"]
        r = rank_models(names)
        self.assertEqual(r[0], "gemini-3.0-flash-preview")
        self.assertNotIn("gemini-2.5-flash-image", r)
        self.assertLess(r.index("gemini-2.5-flash"), r.index("gemini-2.5-flash-lite"))

    def test_parse_json(self):
        self.assertEqual(parse_json('```json\n{"ok": true}\n```'), {"ok": True})
        self.assertEqual(parse_json('Ось: {"ok": false} кінець'), {"ok": False})

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            s = st.load(f"{d}/s.json")
            s["posts"].append({"text": "т"})
            st.save(f"{d}/s.json", s)
            self.assertEqual(st.load(f"{d}/s.json")["posts"][0]["text"], "т")

    def test_pick_rubrics_season(self):
        rub = writer.load("rubrics.json")
        names = writer.pick_rubrics(rub, date(2026, 11, 22), ["Чесний куб"], 4, random.Random(2))
        self.assertEqual(len(names), 4)
        self.assertNotIn("Кора і сад", names)
        self.assertNotIn("Чесний куб", names)


class FlowTests(unittest.TestCase):
    def test_off_by_default_no_drafts(self):
        b = make_bot(kyiv(7))
        b.tick()
        self.assertEqual(b.s["days"], {})

    def test_full_approval_flow(self):
        b = make_bot(kyiv(7))
        b.s["schedule_on"] = True
        with mock.patch("bot.writer.write_post", return_value={"text": GOOD, "rubric": "Дуб чи граб", "score": 8,
                                                                 "fact": "ok", "fact_note": None}):
            b.tick()
        slots = b.slots()
        self.assertEqual(len(slots), 4)
        self.assertEqual([x["status"] for x in slots], ["sent", "sent", "new", "new"])  # 2 per run
        self.assertTrue(slots[0]["role"] and slots[-1]["kind"] == "sell")
        self.assertEqual(b.th.published, [])  # nothing without approval
        sl = slots[0]
        cb = {"update_id": 5, "callback_query": {"id": "c", "from": {"id": 1}, "data": f"ok|{sl['id']}|{sl['v']}",
                                                 "message": {"message_id": sl["msg_id"], "chat": {"id": 1}}}}
        b2 = make_bot(kyiv(8), [cb], s=b.s)
        b2.tick()
        self.assertEqual(sl["status"], "approved")
        self.assertEqual(b2.th.published, [])  # slot time not reached yet
        b3 = make_bot(kyiv(11, 20), s=b.s)
        b3.tick()
        self.assertEqual(sl["status"], "published")
        self.assertEqual(len(b3.th.published), 1)
        b4 = make_bot(kyiv(11, 50), s=b.s)
        b4.tick()  # nothing new approved -> no second post
        self.assertEqual(len(b4.th.published), 0)

    def test_stale_button_ignored(self):
        b = make_bot(kyiv(7))
        b.s["schedule_on"] = True
        with mock.patch("bot.writer.write_post", return_value={"text": GOOD, "rubric": "x", "score": 8, "fact": "ok", "fact_note": None}):
            b.tick()
        sl = b.slots()[0]
        cb = {"update_id": 5, "callback_query": {"id": "c", "from": {"id": 1}, "data": f"ok|{sl['id']}|999",
                                                 "message": {"message_id": 1, "chat": {"id": 1}}}}
        b2 = make_bot(kyiv(8), [cb], s=b.s)
        b2.handle_updates()
        self.assertEqual(sl["status"], "sent")

    def test_owner_edit_and_stranger(self):
        b = make_bot(kyiv(7))
        b.s["schedule_on"] = True
        with mock.patch("bot.writer.write_post", return_value={"text": GOOD, "rubric": "x", "score": 8, "fact": "ok", "fact_note": None}):
            b.tick()
        sl = b.slots()[0]
        ups = [{"update_id": 7, "message": {"message_id": 50, "chat": {"id": 2, "type": "private"}, "text": "/on"}},
               {"update_id": 8, "message": {"message_id": 51, "chat": {"id": 1, "type": "private"}, "text": "Мій текст поста",
                                            "reply_to_message": {"message_id": sl["msg_id"]}}}]
        b2 = make_bot(kyiv(8), ups, s=b.s)
        b2.handle_updates()
        self.assertEqual(sl["draft"]["text"], "Мій текст поста")
        self.assertEqual(sl["status"], "sent")
        self.assertEqual(b2.s["tg_offset"], 9)

    def test_publish_failure_pauses(self):
        b = make_bot(kyiv(12))
        b.s["schedule_on"] = True
        b.s["days"][b.today_key()] = {"slots": [
            {"id": "a", "at": 600, "kind": "hook", "status": "approved", "draft": {"text": GOOD}, "v": 1},
            {"id": "b", "at": 610, "kind": "hook", "status": "approved", "draft": {"text": GOOD + "?"}, "v": 1}]}
        b.th.publish_text = mock.Mock(side_effect=RuntimeError("boom"))
        b.publish_due()
        b.now = kyiv(12, 40)
        b.publish_due()
        self.assertEqual(b.s["fail_streak"], 2)
        self.assertTrue(b.s["paused_reason"])

    def test_claim_owner(self):
        s = st.load("/nonexistent")
        b = Bot(s, tg=FakeTG([{"update_id": 1, "message": {"message_id": 1, "chat": {"id": 42, "type": "private"}, "text": "/start"}}]),
                gm=None, threads_secret="", enc_key="k", now=kyiv(9))
        b.handle_updates()
        self.assertEqual(s["owner_chat_id"], 42)

    def test_token_message_deleted(self):
        b = make_bot(kyiv(9), [{"update_id": 1, "message": {"message_id": 3, "chat": {"id": 1, "type": "private"},
                                                            "text": "THAA" + "x" * 120}}])
        b.handle_updates()
        self.assertTrue(getattr(b.tg, "deleted", False))

    def test_day_rollover_expires(self):
        b = make_bot(kyiv(9, d=21))
        b.s["days"]["2026-11-21"] = {"slots": [{"id": "a", "at": 600, "status": "approved", "draft": {"text": "t"}, "msg_id": 5}]}
        b.now = kyiv(7, d=22)
        b.roll_days()
        self.assertEqual(b.s["days"]["2026-11-21"]["slots"][0]["status"], "expired")


if __name__ == "__main__":
    unittest.main()


class VoiceTests(unittest.TestCase):
    def chk(self, text):
        return writer.check(text, sell=False, today=date(2026, 11, 22), recent=[], examples=[])

    def test_voice_filters(self):
        self.assertEqual(self.chk("Знову обстріл, а ми про дрова. Ну а що ще робити цієї ночі, скажіть?"), "тема війни")
        self.assertEqual(self.chk("А ти знаєш, чим складометр відрізняється від насипного куба? Ото ж бо."), "звернення на «ти»")
        self.assertIsNone(self.chk("Капець, скільки людей досі купує «куб» навалом.\nДоторкніться до правди: складометр."))

    def test_quiet_day(self):
        b = make_bot(kyiv(12))
        b.s["schedule_on"] = True
        b.s["quiet_day"] = b.today_key()
        b.s["days"][b.today_key()] = {"slots": [{"id": "a", "at": 600, "kind": "hook", "status": "approved", "draft": {"text": GOOD}, "v": 1}]}
        b.publish_due()
        self.assertEqual(b.th.published, [])


from bot import trends as tr

tr.fetch_weather = lambda: {"days": [{"date": "2026-11-22", "tmin": 1, "tmax": 4, "snow_cm": 0, "rain_mm": 0},
                                      {"date": "2026-11-23", "tmin": -9, "tmax": -3, "snow_cm": 0, "rain_mm": 0}]}

RSS = """<?xml version="1.0"?><rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>мудрик</title><ht:approx_traffic>20000+</ht:approx_traffic>
<ht:news_item><ht:news_item_title>Мудрик дав інтервʼю</ht:news_item_title><ht:news_item_url>https://example.com/a</ht:news_item_url><ht:news_item_source>Sport</ht:news_item_source></ht:news_item>
</item><item><title>погода київ</title><ht:approx_traffic>5000+</ht:approx_traffic></item></channel></rss>"""


class TrendTests(unittest.TestCase):
    def test_parse_rss(self):
        t = tr.parse_trends(RSS)
        self.assertEqual(t[0]["title"], "мудрик")
        self.assertEqual(t[0]["news"][0]["url"], "https://example.com/a")
        self.assertEqual(t[1]["news"], [])

    def test_weather_event(self):
        self.assertIn("мороз", tr.weather_event(tr.fetch_weather()))
        self.assertIsNone(tr.weather_event({"days": [{"tmin": 2, "tmax": 5, "snow_cm": 0}, {"tmin": 1, "tmax": 4, "snow_cm": 0}]}))
        self.assertIn("Погода в Києві", tr.weather_line(tr.fetch_weather()))

    def test_plan_marks_weather_topic(self):
        b = make_bot(kyiv(7))
        b.s["schedule_on"] = True
        b.plan_today()
        self.assertTrue(any("мороз" in (x.get("topic") or "") for x in b.slots()))

    def test_trend_hype_flow(self):
        b = make_bot(kyiv(13, 7))
        b.s["schedule_on"] = True
        b.s["days"][b.today_key()] = {"slots": []}
        post = {"text": GOOD, "rubric": "Хайп", "score": 8, "fact": "ok", "fact_note": None}
        with mock.patch("bot.trends.fetch_trends", return_value=tr.parse_trends(RSS)), \
             mock.patch("bot.writer.pick_trend", return_value={"title": "мудрик", "topic": "мудрик: інтервʼю", "url": "https://example.com/a"}), \
             mock.patch("bot.writer.write_post", return_value=post):
            b.check_trends()
            b.check_trends()  # same hour: no second check
        hype = [x for x in b.slots() if x["kind"] == "hype"]
        self.assertEqual(len(hype), 1)
        self.assertEqual(hype[0]["status"], "sent")
        self.assertIn("https://example.com/a", b.tg.sent[-1][0])
        hype[0]["status"] = "approved"
        b.s["posts"].append({"text": "old", "at": kyiv(12, 30).isoformat()})  # 37 min ago: ok for hype
        b.publish_due()
        self.assertEqual(b.th.published, [GOOD])


class PlanTests(unittest.TestCase):
    def test_upgrade_turns_on_and_rebuilds_future(self):
        s = st.load("/nonexistent")
        b = make_bot(kyiv(3), s=s)
        b.s["plan_v"] = 0
        b.s["days"]["2026-11-23"] = {"slots": [{"id": "x", "at": 640, "kind": "hook", "status": "new"}]}
        b.upgrade_plan()
        self.assertTrue(b.s["schedule_on"])
        self.assertNotIn("2026-11-23", b.s["days"])
        self.assertTrue(any("Нова стратегія з 22.11" in t for t, _ in b.tg.sent))
        self.assertIn("2026-11-22", b.s["days"])  # today planned right away for approval

    def test_plan_view_and_approve_all(self):
        b = make_bot(kyiv(7))
        b.s["schedule_on"] = True
        with mock.patch("bot.writer.write_post", return_value={"text": GOOD, "rubric": "x", "score": 8, "fact": "ok", "fact_note": None}):
            b.tick()
        b.menu_plan()
        self.assertIn("План на сьогодні", b.tg.sent[-1][0])
        b.on_menu("okall")
        self.assertEqual([x["status"] for x in b.slots()][:2], ["approved", "approved"])
        b.on_menu(f"show|{b.slots()[0]['id']}")
        self.assertEqual(b.slots()[0]["status"], "approved")


class TrendPlanTests(unittest.TestCase):
    def test_morning_trends_schedule_hype_and_feed_posts(self):
        b = make_bot(kyiv(7, 10))
        b.s["schedule_on"] = True
        b.s.pop("day_trends")
        topics = [{"title": "Перший сніг у Києві", "angle": "сухі дрова до морозу", "heat": 5, "why": "", "source": "", "url": ""}]
        post = {"text": GOOD, "rubric": "Хайп", "score": 8, "fact": "ok", "fact_note": None, "tag": "Зима"}
        with mock.patch("bot.trends.signals", return_value=[{"title": "сніг"}]), \
             mock.patch("bot.writer.digest", return_value=topics), \
             mock.patch("bot.writer.pick_trend", return_value={"title": "сніг", "topic": "Перший сніг", "url": ""}), \
             mock.patch("bot.writer.write_post", return_value=post), \
             mock.patch("bot.writer.needs_image", return_value=(False, "")):
            b.morning_trends()
        hype = [x for x in b.slots() if x["kind"] == "hype"]
        self.assertEqual(len(hype), 1)
        self.assertEqual(hype[0]["at"], 12 * 60 + 10)
        self.assertIn("Перший сніг у Києві", b.background())

    def test_topic_tag_fallback(self):
        from bot.threads import Threads
        t = Threads("tok")
        calls = []
        def post(path, **kw):
            calls.append(kw)
            if "topic_tag" in kw:
                raise net.NetError(400, "bad tag")
            return {"id": "c1"}
        t._post = post
        self.assertEqual(t._container("Дрова", media_type="TEXT", text="x"), "c1")
        self.assertEqual(len(calls), 2)


class FreedomTests(unittest.TestCase):
    def setup_bot(self):
        b = make_bot(kyiv(7))
        b.s["schedule_on"] = True
        with mock.patch("bot.writer.write_post", return_value={"text": GOOD, "rubric": "x", "score": 8, "fact": "ok", "fact_note": None}):
            b.tick()
        return b

    def test_freedom_on_off(self):
        b = self.setup_bot()
        b.menu_freedom_on()
        self.assertTrue(all(x["status"] == "approved" for x in b.slots() if x.get("draft")))
        self.assertIn(FREEDOM_OFF, b.menu_kb()[1])
        b.menu_freedom_off()
        self.assertTrue(all(x["status"] == "sent" for x in b.slots() if x.get("draft")))
        self.assertIn(FREEDOM_ON, b.menu_kb()[1])

    def test_freedom_publish_checks_safety(self):
        b = self.setup_bot()
        b.menu_freedom_on()
        sl = b.slots()[0]
        b.now = kyiv(11, 20)
        with mock.patch("bot.trends.fetch_news", return_value=[{"title": "Масована атака на Київ вночі"}]):
            b.publish(sl)
        self.assertEqual(b.th.published, [])
        self.assertEqual(sl["status"], "sent")
        self.assertTrue(sl["owner_only"])
        sl2 = b.slots()[1]
        with mock.patch("bot.trends.fetch_news", return_value=[{"title": "У Києві відкрили ковзанку"}]), \
             mock.patch("bot.writer.safety_check", return_value=(True, "")):
            b.publish(sl2)
        self.assertEqual(len(b.th.published), 1)


class HumanTextTests(unittest.TestCase):
    def test_humanize(self):
        self.assertEqual(writer.humanize("Дрова — це тепло 🔥🔥😂\n— Ні, це любов 🙂"), "Дрова - це тепло 🔥\n- Ні, це любов")


class QuietTests(unittest.TestCase):
    def test_quiet_hours_and_freedom_silence(self):
        b = make_bot(kyiv(8))
        b.talk_hours = (10, 18)
        b.say("план")
        self.assertEqual(b.tg.sent, [])
        self.assertEqual(len(b.s["outbox"]), 1)
        b.owner_turn(b.say, "відповідь на кнопку")
        self.assertEqual(len(b.tg.sent), 1)  # answers to the owner go out at once
        b.now = kyiv(10, 5)
        b.flush_outbox()
        self.assertEqual(len(b.tg.sent), 2)
        b.s["autopilot"] = True
        b.say("опубліковано")
        self.assertEqual(len(b.tg.sent), 2)  # freedom: no trifles
        b.say("сумнів", important=True)
        self.assertEqual(len(b.tg.sent), 3)
        b.now = kyiv(19)
        b.say("сумнів увечері", important=True)
        self.assertEqual(len(b.tg.sent), 3)


class ImageTests(unittest.TestCase):
    def test_bot_decides_image_once_per_day(self):
        b = make_bot(kyiv(7))
        b.s["schedule_on"] = True
        imgs = mock.Mock(can_host=True)
        imgs.generate.return_value = {"bytes": b"x" * 9000, "provider": "cloudflare", "url": ""}
        b.images = imgs
        b.tg.send_photo = mock.Mock(return_value=(777, "FILEID"))
        post = {"text": GOOD, "rubric": "x", "score": 8, "fact": "ok", "fact_note": None}
        with mock.patch("bot.writer.write_post", side_effect=lambda *a, **k: dict(post)), \
             mock.patch("bot.writer.needs_image", return_value=(True, "абсурдна сцена")), \
             mock.patch("bot.writer.image_prompt", return_value={"mode": "scene", "prompt": "p", "idea": "i"}):
            b.tick()
        with_img = [x for x in b.slots() if (x.get("draft") or {}).get("image")]
        self.assertEqual(len(with_img), 1)
        self.assertEqual(with_img[0]["draft"]["image"]["file_id"], "FILEID")
        self.assertEqual(with_img[0]["msg_id"], 777)
        # publishing an image post uploads it and uses IMAGE
        sl = with_img[0]
        sl["status"] = "approved"
        b.tg.download = mock.Mock(return_value=b"img")
        imgs.host.return_value = "https://raw.example/img.png"
        b.th.publish_image = mock.Mock(return_value={"id": "9", "permalink": "p"})
        b.now = kyiv(21)
        b.publish_due()
        b.th.publish_image.assert_called_once_with(GOOD, "https://raw.example/img.png")


class SecretTests(unittest.TestCase):
    def test_clean_token_from_curl(self):
        from bot.main import clean_token
        tok = "AbCdEf1234567890AbCdEf1234567890AbCdEf12"
        blob = f'curl "https://api.cloudflare.com/client/v4/user/tokens/verify" \\\n -H "Authorization: Bearer {tok}"'
        self.assertEqual(clean_token(blob), tok)
        self.assertEqual(clean_token(tok), tok)


class ActivateButtonTest(unittest.TestCase):
    def test_activate_and_pause_buttons(self):
        from datetime import datetime
        from bot.schedule import TZ
        now = datetime(2026, 11, 23, 7, 0, tzinfo=TZ)
        ups = [{"update_id": 1, "message": {"message_id": 10, "chat": {"id": 1, "type": "private"}, "text": "🟢 Активувати"}}]
        b = make_bot(now, ups)
        b.s["schedule_on"] = False
        self.assertEqual(b.menu_kb()[1][0], "Активувати")
        b.handle_updates()
        self.assertTrue(b.s["schedule_on"])
        self.assertEqual(b.menu_kb()[1][0], "Пауза")
        b.on_message({"message_id": 11, "chat": {"id": 1, "type": "private"}, "text": "⏸ Пауза"})
        self.assertFalse(b.s["schedule_on"])


class ChatAndRelayTests(unittest.TestCase):
    def test_chat_turn_proposes_and_applies_rule(self):
        b = make_bot(kyiv(10))
        b.gm.generate = mock.Mock(return_value='{"reply": "Ок", "changes": [{"type": "rule_add", "text": "без сосни"}]}')
        b.chat_turn("не пиши про сосну")
        pend = b.s["chat_pending"]
        b.on_menu(f"apply|{pend['id']}")
        self.assertEqual(b.s["owner_rules"], ["без сосни"])

    def test_chat_focus_edit_text(self):
        b = make_bot(kyiv(10))
        b.s["days"][b.today_key()] = {"slots": [{"id": "1122-0", "at": 700, "kind": "hook", "status": "sent", "tries": 1,
                                                 "rejects": 0, "v": 1, "msg_id": 5, "draft": {"text": "старий", "score": 8, "fact": "ok"}}]}
        b.s["chat_focus"] = "1122-0"
        b.gm.generate = mock.Mock(return_value='{"reply": "Зробив", "changes": [{"type": "edit_text", "text": "новий текст"}]}')
        b.chat_turn("зроби смішніше")
        self.assertEqual(b.find_slot("1122-0")["draft"]["text"], "новий текст")

    def test_relay_updates_and_ack(self):
        b = make_bot(kyiv(10))
        r = mock.Mock()
        r.updates.return_value = [{"update_id": 7, "message": {"chat": {"id": 1, "type": "private"}, "text": "план", "message_id": 3}}]
        b.relay = r
        self.assertEqual(b.handle_updates(), 1)
        r.ack.assert_called_with([7])
        self.assertEqual(b.handle_relay(), 1)  # same update again (lost ack) is not handled twice


class LearnTests(unittest.TestCase):
    def setUp(self):
        writer.set_learned([], [], {})

    def tearDown(self):
        writer.set_learned([], [], {})

    def test_metrics_and_weekly_learning(self):
        b = make_bot(kyiv(11))
        b.th.post_stats = mock.Mock(return_value={"views": 200, "likes": 5, "replies": 4, "reposts": 1, "quotes": 0})
        b.s["posts"] = [{"id": str(i), "text": f"пост {i}?", "rubric": "Питання", "at": kyiv(10, d=18).isoformat()}
                        for i in range(6)] + [{"id": "new", "text": "свіжий", "at": kyiv(9).isoformat()}]
        b.collect_metrics()
        self.assertEqual(b.th.post_stats.call_count, 6)  # today's post is too fresh to measure
        b.collect_metrics()
        self.assertEqual(b.th.post_stats.call_count, 6)  # once a day
        b.gm.generate = mock.Mock(return_value='{"learnings": ["питання в першому рядку дають відповіді"], '
                                               '"rubric_boost": {"Питання": 5}, "best": [1], "summary": "Питання працюють."}')
        b.learn()
        self.assertEqual(b.s["rubric_boost"], {"Питання": 2.0})
        self.assertEqual(b.s["best_examples"], ["пост 1?"])
        self.assertEqual(writer.LEARNED["learnings"], ["питання в першому рядку дають відповіді"])
        calls = b.gm.generate.call_count
        b.learn()
        self.assertEqual(b.gm.generate.call_count, calls)  # weekly, not every run

    def test_no_learning_with_few_posts(self):
        b = make_bot(kyiv(11))
        b.s["posts"] = [{"id": "1", "text": "a", "metrics": {"views": 10}}]
        b.gm.generate = mock.Mock()
        b.learn()
        b.gm.generate.assert_not_called()

    def test_our_claims_rejected(self):
        chk = lambda t: writer.check(t, sell=False, today=date(2026, 11, 22), recent=[], examples=[])
        self.assertIn("камерна", chk("У нас дрова камерної сушки, а у вас які? Пишіть, порадимо по породі"))
        self.assertIsNone(chk(GOOD))


class OAuthTests(unittest.TestCase):
    def test_pasted_redirect_saves_token(self):
        b = make_bot(kyiv(4))
        b.tg.delete = mock.Mock()
        with mock.patch.dict("os.environ", {"THREADS_APP_SECRET": "appsecret123"}), \
                mock.patch("bot.main.Threads.exchange_code", return_value={"access_token": "NEWTOKEN_abcdefgh123", "expires_in": 5}) as ex, \
                mock.patch("bot.main.Threads.token_scopes", return_value=["threads_keyword_search"]):
            b.owner_turn(b.on_message, {"message_id": 9, "chat": {"id": 1, "type": "private"},
                                        "text": "https://koloda.ua/auth/?code=AQBxyz#_"})
        self.assertEqual(ex.call_args[0][3], "AQBxyz")
        self.assertEqual(b.threads_token(), "NEWTOKEN_abcdefgh123")
        b.tg.delete.assert_called()
        self.assertTrue(any("threads_keyword_search" in str(x) for x in b.tg.sent))


class RestartTests(unittest.TestCase):
    def test_restart_button(self):
        b = make_bot(kyiv(11))
        b.s["chat_mode"] = kyiv(11).isoformat()
        b.owner_turn(b.on_message, {"message_id": 3, "chat": {"id": 1, "type": "private"}, "text": "🔄"})
        self.assertNotIn("chat_mode", b.s)
        self.assertTrue(any("Перезапустив" in str(x) for x in b.tg.sent))

    def test_quiet_word(self):
        b = make_bot(kyiv(11))
        b.owner_turn(b.on_message, {"message_id": 4, "chat": {"id": 1, "type": "private"}, "text": "тиша"})
        self.assertEqual(b.s["quiet_day"], b.today_key())
