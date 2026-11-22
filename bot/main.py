"""One run of the bot (GitHub Actions calls it every 30 minutes)."""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from datetime import date, datetime

from . import chat, crypto, meme, net, state as st, trends, writer
from .cfai import CFText
from .images import Images
from .gemini import Gemini
from .schedule import (LAST_PUBLISH_MIN, SPACING_MIN, hhmm, make_slots, minutes_of, now_kyiv, since_minutes)
from .telegram import Telegram
from .threads import Threads

STATUS_UA = {"new": "чекає чернетки", "sent": "чекає вашого рішення", "approved": "схвалено",
             "publishing": "публікується", "published": "опубліковано", "failed": "помилка",
             "skipped": "пропущено", "expired": "не встиг"}
HELP = ("Усе є в меню кнопками внизу чату:\n"
        "🟢 Активувати / ⏸ Пауза: увімкнути або зупинити щоденні чернетки\n"
        "📅 План: що заплановано сьогодні, завтра, післязавтра\n"
        "📊 Статистика: перегляди, лайки, підписники за 7 днів і найкращі пости\n"
        "✍️ Пост зараз: звичайна або продаюча чернетка просто зараз\n"
        "🔥 Хайп: що обговорюють у Києві та Україні, або ваша тема (можна скриншот)\n"
        "🎨 Картинка: наші персонажі малюють що завгодно, нічого не публікується\n"
        "💬 Коментарі: свіжі коментарі під нашими постами\n"
        "🧠 Чат з ботом: пишіть звичайними словами, бот відповідає і змінює свої правила (після вашого ✅)\n"
        "⚙️ Налаштування: тиша на день, автопілот, авто-картинки, пробні пости, перевірка\n\n"
        "Під чернеткою кнопки: Опублікувати / Обговорити / Інший текст / Пропустити. "
        "Щоб виправити текст, дайте відповідь (Reply) на чернетку своїм текстом.")
MENU = [["📅 План", "📊 Статистика"], ["✍️ Пост зараз", "🔥 Хайп"],
        ["🎨 Картинка", "💬 Коментарі"], ["🧠 Чат з ботом", "⚙️ Налаштування"], ["❓ Допомога"]]
MENU_VERSION = 3
CHAT_IDLE_MINUTES = 30
MENU_ACTIONS = {"📅 План": "menu_plan", "📊 Статистика": "menu_stats", "✍️ Пост зараз": "menu_post",
                "🔥 Хайп": "menu_hype", "🎨 Картинка": "menu_draw", "💬 Коментарі": "menu_comments",
                "⚙️ Налаштування": "menu_settings", "❓ Допомога": "menu_help", "🧠 Чат з ботом": "menu_chat",
                "🟢 Активувати": "menu_on", "⏸ Пауза": "menu_off"}
WORD_ACTIONS = {  # plain words, Russian or Ukrainian, without the emoji
    "план": "menu_plan", "статистика": "menu_stats", "стата": "menu_stats", "пост": "menu_post", "пост зараз": "menu_post",
    "пост сейчас": "menu_post", "хайп": "menu_hype", "тренды": "menu_hype", "тренди": "menu_hype", "картинка": "menu_draw",
    "коментарі": "menu_comments", "комментарии": "menu_comments", "налаштування": "menu_settings", "настройки": "menu_settings",
    "допомога": "menu_help", "помощь": "menu_help", "меню": "menu_help", "чат": "menu_chat", "чат з ботом": "menu_chat",
    "чат с ботом": "menu_chat", "активувати": "menu_on", "активировать": "menu_on", "старт": "menu_on", "пауза": "menu_off"}


def menu_word(text: str) -> str | None:
    if text in MENU_ACTIONS:
        return MENU_ACTIONS[text]
    plain = re.sub(r"[^\w\s']", "", text.lower()).strip()
    return WORD_ACTIONS.get(plain)


COMMANDS = [("menu", "Показати меню"), ("status", "План на сьогодні"), ("stats", "Статистика Threads"),
            ("post", "Чернетка просто зараз"), ("hype", "Пост на гарячу тему"), ("draw", "Картинка з персонажами"),
            ("comments", "Нові коментарі"), ("on", "Активувати розклад"), ("off", "Пауза"),
            ("tysha", "Тиша на сьогодні"), ("selftest", "Перевірка"), ("help", "Допомога")]
AWAIT_MINUTES = 20
LISTEN_SECONDS = 480  # after a scheduled run the bot keeps listening to Telegram, so buttons answer within seconds
MAX_GEN_PER_RUN = 2
MAX_AUTO_IMAGES_PER_DAY = 1  # the bot decides itself which post deserves a picture
MAX_HYPE_IMAGES_PER_DAY = 1  # plus one for a trend post
TREND_HOURS = (9, 13, 17)   # Kyiv hours when the bot looks at Google Trends Ukraine
MAX_HYPE_PER_DAY = 2
HYPE_SPACING_MIN = 30
TIME_BUDGET = 6 * 60


def clean_token(v: str) -> str:
    """Owners sometimes paste the whole Cloudflare 'test your token' curl block: pull the token out of it."""
    if " " not in v and "\n" not in v:
        return v
    m = re.search(r"Bearer\s+([A-Za-z0-9_\-]{30,})", v)
    if m:
        cand = m.group(1)
    else:
        words = re.findall(r"[A-Za-z0-9_\-]{35,60}", v)
        cand = max(words, key=len) if words else v
    net.register_secret(cand)
    return cand


def secret_shapes() -> list[str]:
    """Describe the form of secrets without revealing them (helps the owner fix copy/paste mistakes)."""
    import re as _re
    out = []

    def shape(name, ok, hint):
        v = os.environ.get(name, "")
        if not v:
            return
        raw = v
        v = v.strip()
        if name == "CF_API_TOKEN" and (" " in v or "\n" in v):
            fixed = clean_token(v)
            if fixed != v:
                out.append(f"ℹ️ CF_API_TOKEN: вставлено цілий блок тексту, бот витягнув з нього токен (довжина {len(fixed)})")
                v = raw = fixed
        issues = []
        if raw != v:
            issues.append("пробіли або перенесення рядка по краях")
        if " " in v or "\n" in v:
            issues.append("пробіли всередині")
        if not ok(v):
            issues.append(hint)
        out.append(f"{'✅' if not issues else '⚠️'} {name}: довжина {len(v)}" + (f", {'; '.join(issues)}" if issues else ""))

    shape("CF_ACCOUNT_ID", lambda v: bool(_re.fullmatch(r"[0-9a-f]{32}", v)), "очікується 32 символи 0-9 a-f (Account ID)")
    shape("CF_API_TOKEN", lambda v: 35 <= len(v) <= 60 and bool(_re.fullmatch(r"[A-Za-z0-9_\-]+", v)) and not _re.fullmatch(r"[0-9a-f]{32}", v),
          "не схоже на API Token Cloudflare (зазвичай 40 символів; не плутати з Account ID і Global API Key)")
    return out


def log(*a):
    print(net.scrub(" ".join(str(x) for x in a)), flush=True)


class Bot:
    def __init__(self, s: dict, *, tg: Telegram | None, gm: Gemini | None, threads_secret: str,
                 enc_key: str, chat_id_env: str = "", persist=lambda: None, now: datetime | None = None,
                 rnd: random.Random | None = None, images: Images | None = None):
        self.s, self.tg, self.gm = s, tg, gm
        self.images = images
        self.threads_secret, self.enc_key = threads_secret, enc_key
        self.persist = persist
        self.now = now or now_kyiv()
        self.rnd = rnd or random.Random()
        self.started = time.monotonic()
        self.summary: list[str] = []
        if chat_id_env.strip().lstrip("-").isdigit():
            self.s["owner_chat_id"] = int(chat_id_env)
        self.th = Threads(self.threads_token()) if threads_secret else None
        writer.set_owner(self.s.get("owner_rules", []), self.s.get("owner_facts", []))

    # ---------- helpers
    @property
    def owner(self) -> int | None:
        return self.s.get("owner_chat_id")

    def say(self, text: str, buttons=None, keyboard=None) -> int | None:
        self.summary.append(text)
        if self.tg and self.owner:
            try:
                return self.tg.send(self.owner, text, buttons, keyboard=keyboard)
            except net.NetError as e:
                log("telegram send failed:", e)
        return None

    def today_key(self) -> str:
        return self.now.date().isoformat()

    def slots(self, day: str | None = None) -> list[dict]:
        return self.s["days"].get(day or self.today_key(), {}).get("slots", [])

    def find_slot(self, sid: str) -> dict | None:
        for d in self.s["days"].values():
            for sl in d["slots"]:
                if sl["id"] == sid:
                    return sl
        return None

    def recent_texts(self) -> list[str]:
        return [p["text"] for p in self.s["posts"][-20:]]

    def last_rubrics(self) -> list[str]:
        return [p.get("rubric", "") for p in self.s["posts"][-3:]]

    # ---------- token
    def threads_token(self) -> str:
        tok = self.s.get("token") or {}
        fp = crypto.fingerprint(self.threads_secret)
        if tok.get("src_fp") == fp and tok.get("enc"):
            plain = crypto.decrypt(tok["enc"], self.enc_key)
            if plain:
                net.register_secret(plain)
                return plain
        if tok.get("src_fp") != fp:
            self.s["token"] = {"src_fp": fp, "enc": None, "refreshed_at": None, "first_seen": self.now.isoformat()}
        return self.threads_secret

    def maybe_refresh_token(self) -> None:
        if not self.th:
            return
        tok = self.s.get("token") or {}
        if tok.get("refreshed_at"):
            if since_minutes(tok["refreshed_at"], self.now) < 7 * 24 * 60:
                return
        elif since_minutes(tok.get("first_seen"), self.now) < 24 * 60:
            return  # Meta refreshes only tokens older than 24h
        try:
            res = self.th.refresh()
        except net.NetError as e:
            if since_minutes(tok.get("refresh_warned"), self.now) > 24 * 60:
                tok["refresh_warned"] = self.now.isoformat()
                self.say(f"⚠️ Не вдалося продовжити токен Threads: {e}. Якщо за кілька днів не мине, "
                         "треба буде згенерувати новий і оновити секрет THREADS_TOKEN.")
            return
        new = res.get("access_token")
        if new:
            net.register_secret(new)
            tok.update(enc=crypto.encrypt(new, self.enc_key), refreshed_at=self.now.isoformat(),
                       expires_in=res.get("expires_in"))
            self.s["token"] = tok
            self.th = Threads(new)
            log("token refreshed")

    # ---------- telegram input
    def handle_updates(self, wait: int = 0) -> int:
        if not self.tg:
            return 0
        try:
            ups = self.tg.updates(self.s.get("tg_offset"), wait=wait)
        except net.NetError as e:
            log("getUpdates failed:", e)
            return 0
        for u in ups:
            self.s["tg_offset"] = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    self.on_callback(u["callback_query"])
                elif "message" in u:
                    self.on_message(u["message"])
            except Exception as e:  # one bad update must not stop the run
                log("update error:", e)
        if ups:
            try:  # confirm the last update so Telegram drops it
                self.tg.updates(self.s["tg_offset"])
            except net.NetError:
                pass
        return len(ups)

    def on_message(self, m: dict) -> None:
        chat = m.get("chat", {})
        text = (m.get("text") or m.get("caption") or "").strip()
        if chat.get("type") != "private":
            return
        if not self.owner:
            if text.lower().lstrip("/").startswith("start") or text.lower().strip("🟢 ") in ("привет", "привіт", "активувати"):
                self.s["owner_chat_id"] = chat["id"]
                self.say("Готово, тепер бот пише вам.\n\n" + HELP, keyboard=self.menu_kb())
            return
        if chat["id"] != self.owner:
            return
        if re.fullmatch(r"[A-Za-z0-9_\-:.]{60,}", text):
            self.tg.delete(chat["id"], m["message_id"])
            self.say("Схоже на ключ або токен. Я видалив повідомлення: секрети вводяться тільки в GitHub → Settings → Secrets.")
            return
        action = menu_word(text) if not self.s.get("chat_mode") or text in MENU_ACTIONS else None
        if action:
            self.s.pop("await", None)
            return getattr(self, action)()
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        aw = self.s.get("await")
        if aw and not reply_to and not text.startswith("/"):
            self.s.pop("await", None)
            if since_minutes(aw.get("at"), now_kyiv()) <= AWAIT_MINUTES:
                img = self.tg.download_photo(m["photo"][-1]["file_id"]) if m.get("photo") else None
                topic = text or ("тема на скріншоті" if img else "")
                if aw["what"] == "draw" and topic:
                    self.say("🎨 Малюю, це близько хвилини…")
                    return self.draw(topic)
                if aw["what"] == "hype" and topic:
                    self.say("🔥 Пишу пост на цю тему, близько хвилини…")
                    return self.make_hype(topic, url="", images=[img] if img else None, source="ваш хайп")
        cm = self.s.get("chat_mode")
        if cm and not reply_to and not text.startswith("/"):
            if since_minutes(cm, now_kyiv()) <= CHAT_IDLE_MINUTES:
                img = self.tg.download_photo(m["photo"][-1]["file_id"]) if m.get("photo") else None
                return self.chat_turn(text or "(скриншот)", [img] if img else None)
            self.s.pop("chat_mode", None)
        if reply_to and m.get("photo") and not text.startswith("/"):
            return self.on_owner_photo(reply_to, m["photo"][-1]["file_id"])
        if reply_to and not text.startswith("/"):
            for key, item in (self.s.get("replies") or {}).items():
                if item.get("msg_id") == reply_to and item["status"] == "sent":
                    item["answer"] = text[:480]
                    return self.send_reply(key)
            return self.on_edit(reply_to, text)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help", "/menu"):
            self.say(HELP, keyboard=self.menu_kb())
        elif cmd == "/status":
            self.menu_plan()
        elif cmd == "/stats":
            self.menu_stats()
        elif cmd == "/post":
            self.menu_post()
        elif cmd == "/comments":
            self.menu_comments()
        elif cmd in ("/on", "/activate"):
            self.menu_on()
        elif cmd == "/off":
            self.menu_off()
        elif cmd == "/hype" and not arg.strip() and not m.get("photo") and not m.get("reply_to_message"):
            self.menu_hype()
        elif cmd == "/hype":
            img = None
            if m.get("photo"):
                img = self.tg.download_photo(m["photo"][-1]["file_id"])
            fwd = (m.get("reply_to_message") or {}).get("text") or ""
            topic = " ".join(x for x in (arg.strip(), fwd.strip()) if x) or ("тема на скріншоті" if img else "")
            if not topic:
                self.say("Напишіть після /hype, про що хайп, або надішліть скрин з підписом /hype.")
                return
            self.make_hype(topic, url="", images=[img] if img else None, source="ваш /hype")
        elif cmd in ("/draw", "/малюй", "/рисуй") and not arg.strip() and not m.get("reply_to_message"):
            self.menu_draw()
        elif cmd in ("/draw", "/малюй", "/рисуй"):
            self.draw(arg.strip() or (m.get("reply_to_message") or {}).get("text") or "")
        elif cmd == "/trends":
            self.check_trends(force=True)
        elif cmd in ("/tysha", "/тиша", "/quiet"):
            self.s["quiet_day"] = self.today_key()
            self.say("Тиша до кінця дня: нічого не публікую і не надсилаю чернетки. Завтра продовжу сам.")
        elif cmd == "/preview":
            n = int(arg) if arg.strip().isdigit() else 3
            self.preview(max(1, min(n, 5)))
        elif cmd == "/selftest":
            self.say(self.selftest())
        else:
            self.say("Не зрозумів. Оберіть дію в меню внизу 👇", keyboard=self.menu_kb())

    def on_edit(self, msg_id: int, text: str) -> None:
        for d in self.s["days"].values():
            for sl in d["slots"]:
                if sl.get("msg_id") == msg_id and sl["status"] in ("sent", "approved"):
                    if len(text) > 500:
                        self.say("Threads приймає до 500 символів, ваш текст довший. Скоротіть, будь ласка.")
                        return
                    sl["draft"] = {**(sl.get("draft") or {}), "text": text, "score": None, "fact": "owner"}
                    sl["status"] = "new"
                    self.send_draft(sl, note="Ваш варіант (публікую слово в слово):")
                    return
        self.say("Не знайшов чернетку, на яку ви відповіли (можливо, її вже опубліковано або вона застаріла).")

    def slot_by_msg(self, msg_id: int) -> dict | None:
        for d in self.s["days"].values():
            for sl in d["slots"]:
                if sl.get("msg_id") == msg_id and sl["status"] in ("sent", "approved"):
                    return sl
        return None

    def on_owner_photo(self, msg_id: int, file_id: str) -> None:
        sl = self.slot_by_msg(msg_id)
        if not sl:
            self.say("Не знайшов чернетку, на яку ви відповіли картинкою.")
            return
        sl["draft"]["image"] = {"file_id": file_id, "provider": "ваша картинка", "url": ""}
        self.send_draft(sl, note="Чернетка з вашою картинкою:")

    def on_callback(self, cq: dict) -> None:
        if cq.get("from", {}).get("id") != self.owner and cq.get("message", {}).get("chat", {}).get("id") != self.owner:
            return
        action, _, rest = (cq.get("data") or "").partition("|")
        if action == "m":
            self.tg.answer(cq["id"])
            return self.on_menu(rest)
        if action in ("ra", "rr", "rs"):
            item = (self.s.get("replies") or {}).get(rest)
            if not item or item["status"] != "sent":
                return self.tg.answer(cq["id"], "Вже неактуально")
            self.tg.answer(cq["id"])
            if action == "ra":
                return self.send_reply(rest)
            if action == "rs":
                item["status"] = "skipped"
                return self.tg.edit(self.owner, cq["message"]["message_id"], f"⏭ Без відповіді: @{item['user']}: {item['comment'][:200]}")
            dr = writer.reply_draft(self.gm, post=item["post"], comment=item["comment"] + "\n(дай інший варіант відповіді)",
                                    author=item["user"] or "")
            item["answer"] = dr["answer"]
            return self.say(f"🔁 Інший варіант для @{item['user']}:\n{dr['answer']}",
                            buttons=[[("✅ Відповісти так", f"ra|{rest}"), ("🔁 Ще", f"rr|{rest}")], [("⏭ Не відповідати", f"rs|{rest}")]])
        sid, _, ver = rest.partition("|")
        sl = self.find_slot(sid)
        if not sl or str(sl.get("v")) != ver or sl["status"] not in ("sent", "approved"):
            self.tg.answer(cq["id"], "Ця чернетка вже неактуальна")
            return
        msg_id = cq["message"]["message_id"]
        text = sl["draft"]["text"]
        if action == "ok":
            sl["status"] = "approved"
            when = hhmm(sl["at"])
            self.tg.answer(cq["id"], "Схвалено")
            self.tg.edit(self.owner, msg_id, f"✅ Схвалено, вийде близько {when} (або при наступному запуску, якщо час минув)\n\n{text}")
        elif action == "re":
            sl["rejects"] = sl.get("rejects", 0) + 1
            self.tg.answer(cq["id"], "Готую інший варіант")
            self.tg.edit(self.owner, msg_id, f"🔁 Відхилено, готую інший варіант\n\n{text}")
            sl["status"] = "new" if sl["rejects"] < 4 else "skipped"
            sl["draft"] = None
        elif action == "im":
            sl["want_image"] = True
            sl["draft"].pop("image", None)
            sl["status"] = "new"
            self.tg.answer(cq["id"], "Малюю картинку")
            self.tg.edit(self.owner, msg_id, f"🎨 Малюю картинку до цього поста, надішлю за хвилину\n\n{text}")
        elif action == "ni":
            sl["want_image"] = False
            sl["draft"].pop("image", None)
            self.tg.answer(cq["id"], "Прибрав картинку")
            self.tg.edit(self.owner, msg_id, f"Картинку прибрав, чернетка нижче\n\n{text}")
            self.send_draft(sl)
        elif action == "ds":
            self.tg.answer(cq["id"], "Пишіть, що не так")
            self.s["chat_mode"] = now_kyiv().isoformat()
            self.s["chat_focus"] = sl["id"]
            self.say("💬 Що виправити в цьому пості чи картинці? Пишіть звичайними словами: «зроби смішніше», "
                     "«прибери останню фразу», «на картинці хай Денис колить дрова», «картинка не потрібна».",
                     buttons=[[("🚪 Закінчити обговорення", "m|chatoff")]])
        elif action == "sk":
            sl["status"] = "skipped"
            self.tg.answer(cq["id"], "Пропускаю")
            self.tg.edit(self.owner, msg_id, f"⏭ Пропущено\n\n{text}")

    # ---------- menu
    def menu_kb(self) -> list[list[str]]:
        on = self.s["schedule_on"] and not self.s.get("paused_reason")
        return [["⏸ Пауза" if on else "🟢 Активувати"]] + MENU

    def menu_on(self) -> None:
        self.s["schedule_on"], self.s["paused_reason"], self.s["fail_streak"] = True, None, 0
        self.say("🟢 Активовано: щодня 3 чернетки, публікація тільки після вашої кнопки"
                 + (" (або сама, бо увімкнено автопілот)." if self.s.get("autopilot") else "."), keyboard=self.menu_kb())

    def menu_off(self) -> None:
        self.s["schedule_on"] = False
        self.say("⏸ Пауза: нові чернетки не готую, схвалені теж не публікую.", keyboard=self.menu_kb())

    def menu_plan(self) -> None:
        on = self.s["schedule_on"] and not self.s.get("paused_reason")
        self.say(self.status_text(), buttons=[
            [("⏸ Пауза", "m|off") if on else ("🟢 Активувати", "m|on")],
            [("📆 Завтра", "m|plan|1"), ("📆 Післязавтра", "m|plan|2")],
            [("🤫 Тиша сьогодні", "m|quiet"), ("✍️ Пост зараз", "m|postmenu")]])

    def menu_post(self) -> None:
        self.say("Яку чернетку зробити просто зараз? Публікація однаково тільки після вашої кнопки.", buttons=[
            [("📝 Звичайний", "m|post"), ("💰 Продаючий", "m|sell")],
            [("👀 3 пробні (без публікації)", "m|preview")]])

    def menu_hype(self) -> None:
        self.say("🔥 Хайп: що зробити?", buttons=[
            [("📰 Що зараз обговорюють", "m|digest")],
            [("⚡ Одразу пост за найкращим трендом", "m|trends")],
            [("✏️ Своя тема або скриншот", "m|hypeown")]])

    def menu_digest(self) -> None:
        if not self.gm:
            return self.say("Немає ключа Gemini.")
        self.say("📰 Дивлюся Google Trends і київські новини, близько хвилини…")
        try:
            topics = writer.digest(self.gm, trends.signals(log=log))
        except Exception as e:
            return self.say(f"Не вийшло: {net.scrub(str(e))[:200]}")
        if not topics:
            return self.say("Зараз немає безпечних гарячих тем для нас.")
        self.s["digest"] = topics
        lines = ["📰 Про що зараз говорять у Києві та Україні:"]
        for i, t in enumerate(topics):
            lines.append(f"\n{i + 1}. {t['title']} {'🔥' * min(5, int(t['heat'] or 0)) if str(t['heat']).isdigit() else ''}\n"
                         f"   Чому гаряче: {t['why']}\n   Наш кут: {t['angle']}")
        btns = [[(f"✍️ Пост на тему {i + 1}", f"m|dig|{i}") for i in range(j, min(j + 2, len(topics)))]
                for j in range(0, len(topics), 2)]
        self.say("\n".join(lines), buttons=btns)

    def menu_draw(self) -> None:
        self.s["await"] = {"what": "draw", "at": now_kyiv().isoformat()}
        self.say("Напишіть одним повідомленням, що намалювати. Наприклад: «Іванович складає граб у куб і міряє рулеткою».")

    def menu_settings(self) -> None:
        auto = self.s.get("auto_images", True)
        on = self.s["schedule_on"] and not self.s.get("paused_reason")
        self.say("⚙️ Налаштування", buttons=[
            [("⏸ Пауза", "m|off") if on else ("🟢 Активувати", "m|on")],
            [("🤫 Тиша сьогодні", "m|quiet"), ("🔔 Скасувати тишу", "m|unquiet")],
            [(f"🖼 Авто-картинки: {'увімк' if auto else 'вимк'}", "m|autoimg"), ("📋 Мої правила", "m|rules")],
            [(f"🤖 Автопілот: {'увімк' if self.s.get('autopilot') else 'вимк'}", "m|autopilot")],
            [("👀 3 пробні пости", "m|preview"), ("🩺 Перевірка", "m|selftest")]])

    def menu_help(self) -> None:
        self.say(HELP, keyboard=self.menu_kb())

    def on_menu(self, what: str) -> None:
        if what == "on":
            self.menu_on()
        elif what == "off":
            self.menu_off()
        elif what == "quiet":
            self.s["quiet_day"] = self.today_key()
            self.say("🤫 Тиша до кінця дня: нічого не публікую і не надсилаю чернетки. Завтра продовжу сам.")
        elif what == "unquiet":
            self.s.pop("quiet_day", None)
            self.say("🔔 Тишу скасовано, працюю як завжди.")
        elif what == "autoimg":
            self.s["auto_images"] = not self.s.get("auto_images", True)
            self.say("🖼 Авто-картинки " + ("увімкнені: бот сам вирішує, де потрібна картинка (до 1 на день + до хайпу)."
                                           if self.s["auto_images"] else "вимкнені. Картинку можна додати кнопкою під чернеткою."))
        elif what == "autopilot":
            self.s["autopilot"] = not self.s.get("autopilot")
            self.say("🤖 Автопілот увімкнено: добрі чернетки (оцінка 8+, факти перевірені) виходять самі у свій час. "
                     "Під кожною буде «Не публікувати» і «Обговорити»." if self.s["autopilot"] else
                     "🤖 Автопілот вимкнено: публікую тільки після вашої кнопки.")
        elif what.startswith("plan|"):
            self.plan_ahead(int(what.partition("|")[2] or 1))
        elif what == "postmenu":
            self.menu_post()
        elif what in ("post", "sell"):
            self.say("✍️ Пишу чернетку, близько хвилини…")
            self.make_now(sell=what == "sell")
        elif what == "preview":
            self.preview(3)
        elif what == "trends":
            self.say("📈 Дивлюся тренди України…")
            self.check_trends(force=True)
        elif what == "digest":
            self.menu_digest()
        elif what.startswith("dig|"):
            i = what.partition("|")[2]
            topics = self.s.get("digest") or []
            if i.isdigit() and int(i) < len(topics):
                t = topics[int(i)]
                self.say(f"🔥 Пишу пост: {t['title']}")
                self.make_hype(f"{t['source']}: {t['title']}. Кут: {t['angle']}", url=t.get("url", ""), source="ваш вибір із трендів")
        elif what == "hypeown":
            self.s["await"] = {"what": "hype", "at": now_kyiv().isoformat()}
            self.say("Напишіть тему одним повідомленням або надішліть скриншот зі стрічки (можна з підписом).")
        elif what == "selftest":
            self.say(self.selftest())
        elif what == "chatoff":
            self.s.pop("chat_mode", None)
            self.s.pop("chat_focus", None)
            self.say("Вийшов із чату. Меню внизу 👇", keyboard=self.menu_kb())
        elif what == "rules":
            self.menu_rules()
        elif what.startswith("delrule|") or what.startswith("delfact|"):
            kind, _, i = what.partition("|")
            key = "owner_rules" if kind == "delrule" else "owner_facts"
            items = self.s.get(key, [])
            if i.isdigit() and int(i) < len(items):
                gone = items.pop(int(i))
                writer.set_owner(self.s.get("owner_rules", []), self.s.get("owner_facts", []))
                self.say(f"🗑 Прибрав: {gone}")
                self.menu_rules()
        elif what.startswith("apply|") or what.startswith("reject|"):
            act, _, pid = what.partition("|")
            pend = self.s.get("chat_pending") or {}
            if str(pend.get("id")) != pid:
                return self.say("Ця пропозиція вже неактуальна.")
            self.s.pop("chat_pending", None)
            if act == "reject":
                return self.say("Добре, нічого не змінюю.")
            self.apply_changes(pend["changes"])

    # ---------- owner chat
    def menu_chat(self) -> None:
        self.s["chat_mode"] = now_kyiv().isoformat()
        self.say("🧠 Чат з ботом. Пишіть звичайними словами, можна скриншот. Наприклад:\n"
                 "• «не пиши про сосну, ми її майже не продаємо»\n"
                 "• «у нас тепер доставка в Ірпінь безкоштовна»\n"
                 "• «зроби пост про те, як Іванович топить камін»\n"
                 "• «чому вчорашній пост так мало набрав?»\n• «що в нас на завтра?», «можеш постити без мене»\n\n"
                 "Зміни застосовую тільки після вашої кнопки ✅. Чат сам закриється після 30 хвилин тиші.",
                 buttons=[[("📋 Мої правила", "m|rules"), ("🚪 Вийти з чату", "m|chatoff")]])

    def menu_rules(self) -> None:
        rules_, facts_ = self.s.get("owner_rules", []), self.s.get("owner_facts", [])
        if not rules_ and not facts_:
            return self.say("📋 Ваших правил поки немає. Скажіть їх у 🧠 Чаті з ботом.")
        lines = ["📋 Ваші правила для постів:"] + [f"{i + 1}. {r}" for i, r in enumerate(rules_)]
        if facts_:
            lines += ["", "Факти про бізнес:"] + [f"{i + 1}. {f}" for i, f in enumerate(facts_)]
        btns = [[(f"🗑 Правило {i + 1}", f"m|delrule|{i}")] for i in range(min(len(rules_), 8))]
        btns += [[(f"🗑 Факт {i + 1}", f"m|delfact|{i}")] for i in range(min(len(facts_), 4))]
        self.say("\n".join(lines), buttons=btns or None)

    def chat_turn(self, text: str, images: list | None = None) -> None:
        self.s["chat_mode"] = now_kyiv().isoformat()
        if not self.gm:
            return self.say("Немає ключа Gemini, чат не працює.")
        hist = self.s.setdefault("chat_log", [])
        try:
            expertise = (" | ".join(writer.knowledge_sample(n=25)) + "\n" + writer.kyiv_context(self.now.date()) + "\n" +
                         writer.expert_brief())
            fsl = self.find_slot(self.s.get("chat_focus") or "")
            focus = ""
            if fsl and fsl.get("draft"):
                fd = fsl["draft"]
                focus = json.dumps({"text": fd.get("text"), "kind": fsl["kind"], "time": hhmm(fsl["at"]),
                                    "image": (fd.get("image") or {}).get("idea", "немає")}, ensure_ascii=False)
            res = chat.ask(self.gm, text=text, history=hist, expertise=expertise, focus=focus, rules=self.s.get("owner_rules", []),
                           facts=self.s.get("owner_facts", []),
                           settings={"schedule_on": self.s["schedule_on"], "auto_images": self.s.get("auto_images", True)},
                           status=self.status_text(), images=images)
        except Exception as e:
            return self.say(f"Не зміг відповісти: {net.scrub(str(e))[:200]}")
        hist += [{"owner": text[:500]}, {"bot": res["reply"][:500]}]
        del hist[:-20]
        rules_, facts_ = self.s.get("owner_rules", []), self.s.get("owner_facts", [])
        described = [(c, chat.describe(c, rules_, facts_)) for c in res["changes"]]
        described = [(c, d) for c, d in described if d]
        if not described:
            return self.say(res["reply"] or "…")
        instant = ("draft", "draw", "plan", "edit_text", "redraw", "remove_image", "approve", "skip")
        if all(c["type"] in instant for c, _ in described):
            self.say(res["reply"] or "Роблю.")
            return self.apply_changes([c for c, _ in described])
        pid = int(time.time()) % 100000
        self.s["chat_pending"] = {"id": pid, "changes": [c for c, _ in described]}
        self.say((res["reply"] + "\n\n" if res["reply"] else "") + "Пропоную:\n" + "\n".join(d for _, d in described),
                 buttons=[[("✅ Застосувати", f"m|apply|{pid}"), ("❌ Не треба", f"m|reject|{pid}")]])

    def apply_changes(self, changes: list[dict]) -> None:
        rules_ = self.s.setdefault("owner_rules", [])
        facts_ = self.s.setdefault("owner_facts", [])
        done, later = [], []
        # removals first, highest index first, so indexes stay valid
        for c in sorted([c for c in changes if c["type"] in ("rule_remove", "fact_remove")],
                        key=lambda c: -int(c.get("index", -1))):
            items = rules_ if c["type"] == "rule_remove" else facts_
            i = int(c.get("index", -1))
            if 0 <= i < len(items):
                done.append(f"прибрав «{items.pop(i)}»")
        for c in changes:
            t = c["type"]
            if t == "rule_add" and c.get("text"):
                rules_.append(str(c["text"])[:300])
                done.append("правило додано")
            elif t == "fact_add" and c.get("text"):
                facts_.append(str(c["text"])[:300])
                done.append("факт додано")
            elif t == "setting" and c.get("key") in ("schedule_on", "auto_images", "autopilot"):
                self.s[c["key"]] = bool(c.get("value"))
                done.append(f"{c['key']} = {bool(c.get('value'))}")
            elif t in ("draft", "draw", "dev", "plan", "edit_text", "redraw", "remove_image", "approve", "skip"):
                later.append(c)
        del rules_[30:]
        del facts_[30:]
        writer.set_owner(rules_, facts_)
        if done:
            self.say("✅ Готово: " + "; ".join(done))
        self.persist()
        fsl = self.find_slot(self.s.get("chat_focus") or "")
        touched = False
        for c in later:
            t = c["type"]
            if t == "redraw" and not fsl and self.s.get("last_draw"):
                self.say("🎨 Переписую запит і перемальовую…")
                self.draw(str(c.get("hint") or ""), fix_of=True)
                continue
            if t in ("edit_text", "redraw", "remove_image", "approve", "skip"):
                if not fsl or not fsl.get("draft") or fsl["status"] not in ("sent", "approved", "new"):
                    self.say("Не знайшов чернетку для правки, її вже опубліковано або пропущено.")
                    continue
                d = fsl["draft"]
                if t == "edit_text" and c.get("text"):
                    d.update({"text": str(c["text"]).strip()[:500], "score": d.get("score"), "fact": d.get("fact")})
                    touched = True
                elif t == "redraw":
                    fsl["want_image"], fsl["image_hint"] = True, str(c.get("hint") or "")[:400]
                    d.pop("image", None)
                    self.say("🎨 Перемальовую…")
                    self.add_image(fsl)
                    touched = True
                elif t == "remove_image":
                    fsl["want_image"] = False
                    d.pop("image", None)
                    touched = True
                elif t == "approve":
                    fsl["status"] = "approved"
                    self.say(f"✅ Схвалено, вийде близько {hhmm(fsl['at'])}.")
                elif t == "skip":
                    fsl["status"] = "skipped"
                    self.say("⏭ Пропускаю цей пост.")
                continue
            if t == "plan":
                self.plan_ahead(int(c.get("days") or 1))
            elif t == "draw":
                self.draw(str(c.get("topic") or ""))
            elif t == "draft":
                if c.get("kind") == "hype" and c.get("topic"):
                    self.make_hype(str(c["topic"]), url="", source="ваш чат")
                else:
                    self.make_now(sell=c.get("kind") == "sell")
            elif t == "dev":
                self.dev_request(str(c.get("text") or ""))
        if touched and fsl:
            if fsl.get("msg_id") and self.tg and self.owner:
                self.tg.edit(self.owner, fsl["msg_id"], "✏️ Оновлено, нова версія нижче.")
            self.send_draft(fsl, note="✏️ Виправлена чернетка:")

    def dev_request(self, text: str) -> None:
        """Things the bot cannot change itself go to GitHub Issues: the developer (Claude) picks them up."""
        if not text:
            return
        repo, token = (self.images.media_repo, self.images.media_token) if self.images else ("", "")
        if not (repo and token):
            self.s.setdefault("dev_requests", []).append(text[:1000])
            return self.say("🛠 Записав задачу для розробника: " + text[:300])
        try:
            res = net.request("POST", f"https://api.github.com/repos/{repo}/issues",
                              json_body={"title": "З чату з ботом: " + text[:80],
                                         "body": text + "\n\n_Створено ботом на прохання власника в Telegram-чаті._"},
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            self.say(f"🛠 Це треба змінювати в коді, створив задачу розробнику: {res.get('html_url', '')}\n"
                     "Скажіть Claude в проєкті «зроби задачі з GitHub», і він їх виконає.")
        except Exception as e:
            self.s.setdefault("dev_requests", []).append(text[:1000])
            self.say(f"🛠 Записав задачу для розробника (GitHub не прийняв: {net.scrub(str(e))[:100]}).")

    def make_now(self, sell: bool) -> bool:
        if not self.gm:
            self.say("Немає ключа Gemini.")
            return False
        key = self.today_key()
        day = self.s["days"].setdefault(key, {"slots": []})
        n = sum(1 for x in day["slots"] if x["kind"] in ("now", "sell-now"))
        sl = {"id": f"{key[5:7]}{key[8:]}-n{n}", "at": minutes_of(now_kyiv()), "kind": "now", "status": "new",
              "tries": 1, "rejects": 0, "v": 0, "draft": None, "msg_id": None, "sell": sell}
        post = self.write(sell)
        if not post:
            self.say("Не вийшло написати добрий пост з першого разу, спробуйте ще раз.")
            return False
        day["slots"].append(sl)
        sl["draft"] = post
        if not sell and self.should_illustrate(sl) and self.time_left():
            self.add_image(sl)
        self.send_draft(sl)
        self.persist()
        return True

    def menu_stats(self) -> None:
        if not self.th:
            return self.say("Немає доступу до Threads.")
        lines = ["📊 Статистика Threads за 7 днів"]
        until = int(time.time())
        try:
            st7 = self.th.account_stats(until - 7 * 86400, until)
            prev = self.s.get("followers_prev")
            f = st7.get("followers_count")
            lines += [f"👀 Перегляди: {st7.get('views', 0)}", f"❤️ Лайки: {st7.get('likes', 0)}",
                      f"💬 Відповіді: {st7.get('replies', 0)}", f"🔁 Репости: {st7.get('reposts', 0)}, цитати: {st7.get('quotes', 0)}",
                      f"👥 Підписники: {f}" + (f" ({f - prev:+d} з минулої перевірки)" if isinstance(prev, int) and f is not None else "")]
            if f is not None:
                self.s["followers_prev"] = f
        except Exception as e:
            lines.append(f"⚠️ Загальна статистика недоступна: {net.scrub(str(e))[:150]}")
        try:
            rows = []
            for p in self.th.my_posts(10):
                try:
                    ps = self.th.post_stats(p["id"])
                except Exception:
                    continue
                rows.append((ps.get("views", 0), ps.get("likes", 0), ps.get("replies", 0), p))
            rows.sort(key=lambda r: r[0], reverse=True)
            if rows:
                lines.append("\n🏆 Найкращі з останніх 10 постів:")
                for v, l, r, p in rows[:3]:
                    lines.append(f"{v} 👀 {l} ❤️ {r} 💬 · {(p.get('text') or '').strip()[:70]}… {p.get('permalink', '')}")
        except Exception as e:
            lines.append(f"⚠️ Пости недоступні: {net.scrub(str(e))[:150]}")
        week = [d for k, d in self.s["days"].items() if since_minutes(k + "T12:00:00+02:00", now_kyiv()) <= 7 * 1440]
        cnt = {}
        for d in week:
            for x in d["slots"]:
                cnt[x["status"]] = cnt.get(x["status"], 0) + 1
        lines.append(f"\n🤖 Бот за тиждень: опубліковано {cnt.get('published', 0)}, пропущено {cnt.get('skipped', 0)}, "
                     f"чекає рішення {cnt.get('sent', 0)}")
        self.say("\n".join(lines))

    def menu_comments(self) -> None:
        if not self.th:
            return self.say("Немає доступу до Threads.")
        out = []
        try:
            for p in self.th.my_posts(5):
                for r in self.th.replies(p["id"])[:5]:
                    out.append((r.get("timestamp", ""), f"@{r.get('username', '?')}: {(r.get('text') or '').strip()[:150]}\n"
                                f"   ↳ під постом «{(p.get('text') or '').strip()[:40]}…» {p.get('permalink', '')}"))
        except Exception as e:
            return self.say(f"⚠️ Не зміг завантажити коментарі: {net.scrub(str(e))[:150]}")
        if not out:
            return self.say("💬 Під останніми 5 постами коментарів поки немає.")
        out.sort(reverse=True)
        self.say("💬 Свіжі коментарі:\n\n" + "\n\n".join(x for _, x in out[:10]) +
                 "\n\nНа нові коментарі бот сам надсилає готову відповідь з кнопкою «Відповісти».")

    def ensure_menu(self) -> None:
        if not self.tg or not self.owner or self.s.get("menu_v") == MENU_VERSION:
            return
        try:
            self.tg.set_commands(COMMANDS)
        except net.NetError as e:
            log("setMyCommands failed:", e)
        self.say("Оновив бота: тепер усе українською і в меню кнопками внизу 👇\n\n" + HELP, keyboard=self.menu_kb())
        self.s["menu_v"] = MENU_VERSION

    def listen(self, seconds: int) -> None:
        """Keep answering Telegram for a few minutes with long polling, so buttons react within seconds."""
        end = time.monotonic() + seconds
        while time.monotonic() < end - 5:
            n = self.handle_updates(wait=int(min(25, end - time.monotonic())))
            self.check_comments()
            if n:
                self.now = now_kyiv()
                self.publish_due()
                self.draft_pending()
                self.persist()

    # ---------- planning
    def roll_days(self) -> None:
        today = self.today_key()
        for day in sorted(self.s["days"]):
            if day >= today:
                continue
            for sl in self.s["days"][day]["slots"]:
                if sl["status"] in ("new", "sent", "approved"):
                    sl["status"] = "expired"
                    if sl.get("msg_id") and self.tg and self.owner:
                        self.tg.edit(self.owner, sl["msg_id"], "⌛ День скінчився, цю чернетку не опубліковано.\n\n"
                                     + (sl.get("draft") or {}).get("text", ""))
        for day in [d for d in sorted(self.s["days"]) if d < today][:-2]:
            del self.s["days"][day]

    def plan_today(self) -> None:
        if self.now.hour < 6:
            return
        self.plan_day(self.today_key(), self.now.date())

    def plan_day(self, key: str, day_date) -> bool:
        if key in self.s["days"] or not self.s["schedule_on"]:
            return False
        today = key == self.today_key()
        slots = []
        times = make_slots(self.rnd)
        for i, at in enumerate(times):
            if today and at < minutes_of(self.now) - 30:
                continue  # window already passed (bot switched on mid-day)
            self.s["planned_total"] += 1
            kind = "sell" if i == len(times) - 1 else "hook"  # evening post sells: one per day, with wit
            slots.append({"id": f"{key[5:7]}{key[8:]}-{i}", "at": at, "kind": kind, "status": "new",
                          "tries": 0, "rejects": 0, "v": 0, "draft": None, "msg_id": None,
                          "want_image": False, "day": key})
        self.s["days"][key] = {"slots": slots}
        try:
            self.s["weather"] = trends.fetch_weather()
        except Exception as e:
            log("weather failed:", e)
        event = trends.weather_event(self.s.get("weather")) if today else None
        hooks = [x for x in slots if x["kind"] == "hook"]
        if event and hooks:
            hooks[-1]["topic"] = event + " Пост про це: погода як привід подумати про сухі дрова, з гумором."
        return True

    def plan_ahead(self, days: int) -> None:
        """Owner asked for tomorrow / the day after: make that day's plan and drafts now."""
        from datetime import timedelta
        days = max(1, min(int(days or 1), 2))
        d = self.now.date() + timedelta(days=days)
        key = d.isoformat()
        if key not in self.s["days"]:
            was = self.s["schedule_on"]
            self.s["schedule_on"] = True
            self.plan_day(key, d)
            self.s["schedule_on"] = was
        self.say(f"📆 Готую план на {d.strftime('%d.%m')}: чернетки надішлю по одній, схвалюйте як завжди.")
        self.draft_pending(force=True)
        self.persist()

    # ---------- comments under our posts
    def check_comments(self, force: bool = False) -> None:
        if not self.th or not self.gm or not self.tg or not self.owner:
            return
        if not force and since_minutes(self.s.get("comments_checked"), now_kyiv()) < 10:
            return
        self.s["comments_checked"] = now_kyiv().isoformat()
        try:
            me = (self.s.get("me") or self.th.me().get("username") or "").lower()
            self.s["me"] = me
            posts = self.th.my_posts(5)
        except Exception as e:
            log("comments: posts failed:", e)
            return
        first = "seen_replies" not in self.s
        seen = set(self.s.setdefault("seen_replies", []))
        made = 0
        for p in posts:
            try:
                reps = self.th.replies(p["id"])
            except Exception as e:
                log("comments: replies failed:", e)
                continue
            for r in reps:
                rid = r.get("id")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                if first or (r.get("username") or "").lower() == me or made >= 3 or not self.time_left():
                    continue  # first run: remember old comments without answering them
                made += 1
                self.draft_reply(p, r)
        self.s["seen_replies"] = list(seen)[-400:]
        self.persist()

    def draft_reply(self, post: dict, r: dict) -> None:
        try:
            dr = writer.reply_draft(self.gm, post=post.get("text") or "", comment=r.get("text") or "",
                                    author=r.get("username") or "")
        except Exception as e:
            log("reply draft failed:", e)
            dr = {"type": "other", "answer": "", "safe": False, "lead": False}
        reps = self.s.setdefault("replies", {})
        key = str(len(reps) + 1) + "-" + r["id"][-6:]
        item = {"id": r["id"], "user": r.get("username"), "comment": (r.get("text") or "")[:500],
                "post": (post.get("text") or "")[:120], "link": post.get("permalink", ""), "answer": dr["answer"],
                "type": dr["type"], "status": "sent"}
        reps[key] = item
        for k in list(reps)[:-30]:
            del reps[k]
        head = (f"💬 Коментар від @{item['user']}{' · 🎯 схоже на клієнта' if dr['lead'] else ''}"
                f"{' · ⚠️ скарга' if dr['type'] == 'complaint' else ''}:\n«{item['comment']}»\n"
                f"↳ під постом «{item['post'][:60]}…» {item['link']}")
        if dr["answer"] and self.s.get("autopilot") and dr["safe"] and dr["type"] in ("question", "praise"):
            self.send_reply(key, note=head)
            return
        body = f"{head}\n\nПропоную відповісти:\n{dr['answer']}" if dr["answer"] else f"{head}\n\nБот радить не відповідати."
        item["msg_id"] = self.say(body + "\n\nСвоя відповідь: дайте відповідь (Reply) на це повідомлення.", buttons=[
            [("✅ Відповісти так", f"ra|{key}"), ("🔁 Інша відповідь", f"rr|{key}")],
            [("⏭ Не відповідати", f"rs|{key}")]])

    def send_reply(self, key: str, note: str = "") -> None:
        item = (self.s.get("replies") or {}).get(key)
        if not item or item["status"] != "sent" or not item.get("answer"):
            return
        try:
            self.th.publish_reply(item["answer"], item["id"])
            item["status"] = "answered"
            self.say((note + "\n\n" if note else "") + f"✅ Відповів @{item['user']}: {item['answer']}")
        except Exception as e:
            self.say(f"⚠️ Не зміг відповісти @{item['user']}: {net.scrub(str(e))[:200]}")

    def morning_brief(self) -> None:
        """Once a day after 8:00: what is planned today, after the drafts are ready."""
        key = self.today_key()
        if self.s.get("brief_day") == key or self.now.hour < 8 or not self.s["schedule_on"]:
            return
        sl = self.slots()
        if not sl or any(x["status"] == "new" for x in sl):
            return
        self.s["brief_day"] = key
        lines = ["☀️ Доброго ранку! План на сьогодні:"]
        for x in sl:
            text = ((x.get("draft") or {}).get("text") or "").split("\n")[0][:70]
            lines.append(f"{hhmm(x['at'])} {'💰' if x['kind'] == 'sell' else '📝'} {STATUS_UA.get(x['status'], x['status'])}: {text}")
        lines.append("\nЧернетки вище, схвалюйте або тисніть «Обговорити». " +
                     ("Автопілот увімкнено: схвалені ботом вийдуть самі." if self.s.get("autopilot") else ""))
        self.say("\n".join(lines))

    def time_left(self) -> bool:
        return time.monotonic() - self.started < TIME_BUDGET

    def quiet(self) -> bool:
        return self.s.get("quiet_day") == self.today_key()

    def draft_pending(self, force: bool = False) -> None:
        if not force and (not self.s["schedule_on"] or self.s.get("paused_reason") or self.quiet()):
            return
        if not self.gm:
            return
        made = 0
        today = self.today_key()
        upcoming = [x for k in sorted(self.s["days"]) if k >= today for x in self.s["days"][k]["slots"]]
        for sl in upcoming:
            if sl["status"] != "new" or made >= MAX_GEN_PER_RUN or not self.time_left():
                continue
            if sl.get("draft") and (sl["draft"].get("fact") == "owner" or sl.get("want_image")):
                if sl.get("want_image") and not sl["draft"].get("image"):
                    made += 1
                    self.add_image(sl)
                self.send_draft(sl)
                self.persist()
                continue
            made += 1
            sl["tries"] += 1
            post = self.write(sl["kind"] == "sell" or bool(sl.get("sell")), topic=sl.get("topic") or "")
            if post:
                sl["draft"] = post
                if self.should_illustrate(sl) and self.time_left():
                    self.add_image(sl)
                self.send_draft(sl)
            elif sl["tries"] >= 5:
                sl["status"] = "skipped"
                self.say(f"⚠️ Не зміг написати добрий пост на {hhmm(sl['at'])} за 5 спроб, слот пропущено.")
            self.persist()

    def background(self) -> str:
        return " ".join(x for x in (trends.weather_line(self.s.get("weather")), trends.calendar_note(self.now.date())) if x)

    def write(self, sell: bool, topic: str = "", images: list | None = None) -> dict | None:
        try:
            post = writer.write_post(self.gm, today=self.now.date(), sell=sell, recent=self.recent_texts(),
                                     last_rubrics=self.last_rubrics(), rnd=self.rnd, log=log,
                                     background=self.background(), topic=topic, images=images)
        except Exception as e:
            log("writer failed:", e)
            return None
        finally:
            if self.gm and self.gm.model:
                self.s["gemini_model"] = self.gm.model if not str(self.gm.model).startswith("cloudflare") else self.s.get("gemini_model")
        return post

    def should_illustrate(self, sl: dict) -> bool:
        if sl.get("want_image"):
            return True
        if not self.images or not self.gm or sl["kind"] == "sell" or sl.get("sell") or not self.s.get("auto_images", True):
            return False
        hype = sl.get("kind") == "hype"
        today = [x for x in self.slots() if (x.get("draft") or {}).get("image_auto") and (x.get("kind") == "hype") == hype]
        if len(today) >= (MAX_HYPE_IMAGES_PER_DAY if hype else MAX_AUTO_IMAGES_PER_DAY):
            return False
        try:
            yes, why = writer.needs_image(self.gm, sl["draft"]["text"], trend=sl.get("kind") == "hype")
        except Exception as e:
            log("image judge failed:", e)
            return False
        if yes:
            sl["draft"]["image_auto"] = why
        return yes

    def add_image(self, sl: dict) -> None:
        if not self.images or not self.gm:
            return
        d = sl["draft"]
        try:
            topic = sl.get("topic", "") if sl.get("kind") == "hype" else ""
            res, ip = self.render_image(d["text"], topic, hint=sl.pop("image_hint", ""), previous=d.get("image_source"))
        except Exception as e:
            log("image failed:", e)
            d["image_error"] = net.scrub(str(e))[:200]
            return
        d["image"] = {"provider": res["provider"], "url": "", "idea": ip["idea"], "mode": ip["mode"]}
        d["image_source"] = ip.get("source")
        if ip.get("explain"):
            d["image_auto"] = "виправлено: " + ip["explain"]
        d.pop("image_error", None)
        self._image_bytes = res["bytes"]

    def render_image(self, text: str, topic: str = "", hint: str = "", previous: dict | None = None) -> tuple[dict, dict]:
        """Meme with the cast (panels + captions in meme fonts) or a cosy scene without people.
        With hint + previous plan, the image doctor rewrites the previous prompt from the owner's complaint."""
        if hint and previous:
            ip = writer.fix_image_prompt(self.gm, post_text=text, previous=previous, complaint=hint)
        else:
            ip = writer.image_prompt(self.gm, text, topic=topic, hint=hint)
        if ip["mode"] == "meme":
            shots = [self.images.generate(p, log=log, character=True, cast=ip.get("cast")) for p in ip["panels"]]
            data = shots[0]["bytes"]
            if meme.available() and ip["captions"]:
                data = meme.compose(ip["layout"], [x["bytes"] for x in shots], ip["captions"])
            ip["idea"] = f"мем «{ip['format']}»: {ip['idea']}"
            return {"bytes": data, "provider": shots[0]["provider"]}, ip
        res = self.images.generate(ip["prompt"], log=log, character=False)
        return {"bytes": res["bytes"], "provider": res["provider"]}, ip

    def draw(self, topic: str, fix_of: bool = False) -> None:
        if not self.images or not self.gm:
            self.say("Картинки не налаштовані.")
            return
        if not topic:
            self.say("Напишіть після /draw, що намалювати, наприклад: /draw Іванович міряє складометр рулеткою")
            return
        try:
            last = self.s.get("last_draw") or {}
            fix = topic if fix_of else ""
            res, ip = self.render_image(last.get("topic", topic) if fix_of else topic, topic=topic,
                                        hint=fix, previous=last.get("source") if fix_of else None)
            self.s["last_draw"] = {"topic": last.get("topic", topic) if fix_of else topic, "source": ip.get("source")}
            cap = f"Пробна картинка (не публікується): {ip['idea']}"
            if ip.get("explain"):
                cap += f"\nЩо виправив: {ip['explain']}"
            self.tg.send_photo(self.owner, res["bytes"], cap[:1000])
        except Exception as e:
            self.say(f"Не вийшло намалювати: {net.scrub(str(e))[:200]}")

    def send_draft(self, sl: dict, note: str | None = None) -> None:
        d = sl["draft"]
        sl["v"] = sl.get("v", 0) + 1
        fact = {"ok": "факти ✅", "fixed": "факти виправлено ✅", "unchecked": "факти НЕ перевірено ⚠️",
                "nosearch": "факти перевірено без пошуку ⚠️", "owner": "ваш текст"}.get(d.get("fact"), "")
        if sl["kind"] == "hype" and not note:
            note = (f"🔥 Хайп ({sl.get('source', '')}): {sl.get('topic', '')[:150]}\n{sl.get('url') or ''}\n"
                    f"оцінка {d.get('score')}/10 · {fact}").strip()
        when = hhmm(sl["at"]) if sl.get("day", self.today_key()) == self.today_key() else \
            f"{sl['day'][8:10]}.{sl['day'][5:7]} {hhmm(sl['at'])}"
        head = note or (f"📝 Чернетка на {when}" + (" · продаючий" if sl["kind"] == "sell" else "")
                        + f" · {d.get('rubric', '')} · оцінка {d.get('score')}/10 · {fact}")
        if d.get("fact_note") and d.get("fact") != "ok":
            head += f"\n({d['fact_note']})"
        img = d.get("image")
        ref = f"{sl['id']}|{sl['v']}"
        buttons = [[("✅ Опублікувати", f"ok|{ref}"), ("💬 Обговорити", f"ds|{ref}")],
                   [("🔁 Інший текст", f"re|{ref}"), ("⏭ Пропустити", f"sk|{ref}")],
                   [("🎨 Інша картинка", f"im|{ref}"), ("🗑 Прибрати картинку", f"ni|{ref}")] if img
                   else [("🎨 Додати картинку", f"im|{ref}")]]
        auto = (self.s.get("autopilot") and (d.get("score") or 0) >= 8 and d.get("fact") in ("ok", "fixed", "nosearch", "owner"))
        if auto:
            head = f"🤖 Автопілот: вийде о {when} сам. Не так? «Не публікувати» або «Обговорити».\n" + head
            buttons[0] = [("💬 Обговорити", f"ds|{ref}"), ("⏭ Не публікувати", f"sk|{ref}")]
        if img and d.get("image_auto"):
            head += f"\n🎨 Бот вирішив, що тут потрібна картинка: {d['image_auto']}"
        if d.get("image_error") and not img:
            head += f"\n(картинка не вийшла: {d['image_error'][:120]})"
        msg_id = None
        if img and self.tg and self.owner:
            photo = getattr(self, "_image_bytes", None) or img.get("file_id")
            self._image_bytes = None
            try:
                msg_id, img["file_id"] = self.tg.send_photo(self.owner, photo, f"{head}\n\n{d['text']}", buttons)
                self.summary.append(f"[картинка] {head}\n\n{d['text']}")
            except Exception as e:
                log("send_photo failed:", e)
                d.pop("image", None)
        if msg_id is None:
            msg_id = self.say(f"{head}\n\n{d['text']}", buttons)
        sl["msg_id"] = msg_id
        sl["status"] = "approved" if auto else "sent"

    # ---------- publishing
    def publish_due(self) -> None:
        if not self.th or not self.s["schedule_on"] or self.s.get("paused_reason") or self.quiet():
            return
        now_min = minutes_of(self.now)
        if now_min > LAST_PUBLISH_MIN:
            return
        last = self.s["posts"][-1]["at"] if self.s["posts"] else None
        gap = since_minutes(last, self.now)
        if gap < HYPE_SPACING_MIN or since_minutes(self.s.get("last_fail"), self.now) < 15:
            return
        ready = [x for x in self.slots() if x["status"] == "approved" and now_min >= x["at"]]
        ready.sort(key=lambda x: (x["kind"] not in ("hype", "now"), x["at"]))  # time-sensitive first
        for sl in ready:
            if sl["kind"] in ("hype", "now") or gap >= SPACING_MIN:
                self.publish(sl)
                return  # one post per run

    def publish(self, sl: dict) -> None:
        text = sl["draft"]["text"]
        sl["status"] = "publishing"
        self.persist()  # the slot is taken BEFORE publishing: never post twice
        try:
            for p in self.th.my_posts(5):
                if (p.get("text") or "").strip() == text.strip():
                    raise RuntimeError("такий пост уже є в Threads, повтор не публікую")
            img = sl["draft"].get("image")
            if img:
                url = ""
                if self.images and self.images.can_host and img.get("file_id"):
                    url = self.images.host(self.tg.download(img["file_id"]), sl["id"])
                elif img.get("url"):
                    url = img["url"]
                if url:
                    res = self.th.publish_image(text, url)
                else:
                    self.say("⚠️ Немає сховища для картинки, публікую пост без неї.")
                    res = self.th.publish_text(text)
            else:
                res = self.th.publish_text(text)
        except Exception as e:
            sl["status"] = "failed"
            self.s["fail_streak"] += 1
            self.s["last_fail"] = self.now.isoformat()
            msg = f"⚠️ Не вдалося опублікувати пост на {hhmm(sl['at'])}: {net.scrub(str(e))[:300]}"
            if self.s["fail_streak"] >= 2:
                self.s["paused_reason"] = "дві помилки публікації поспіль"
                msg += "\nДві помилки поспіль, ставлю бота на паузу. Увімкнути знову: 🟢 Активувати"
            self.say(msg)
            return
        sl["status"], sl["post"] = "published", res
        self.s["fail_streak"] = 0
        self.s["posts"].append({"text": text, "rubric": sl["draft"].get("rubric"), "id": res["id"],
                                "permalink": res.get("permalink"), "at": self.now.isoformat()})
        self.s["posts"] = self.s["posts"][-60:]
        if sl.get("msg_id") and self.tg and self.owner:
            self.tg.edit(self.owner, sl["msg_id"], f"🟢 Опубліковано {res.get('permalink', '')}\n\n{text}")

    # ---------- hype
    def make_hype(self, topic: str, *, url: str, images: list | None = None, source: str = "",
                  want_image: bool = False) -> bool:
        if not self.gm:
            return False
        key = self.today_key()
        day = self.s["days"].setdefault(key, {"slots": []})
        n = sum(1 for x in day["slots"] if x["kind"] == "hype")
        sl = {"id": f"{key[5:7]}{key[8:]}-h{n}", "at": minutes_of(self.now), "kind": "hype", "status": "new",
              "tries": 1, "rejects": 0, "v": 0, "draft": None, "msg_id": None, "topic": topic[:600],
              "url": url, "source": source, "want_image": want_image}
        post = self.write(False, topic=sl["topic"], images=images)
        if not post:
            if source.startswith("ваш"):
                self.say("Не зміг зробити добрий пост на цю тему (не пройшов фільтри або тема заборонена).")
            return False
        day["slots"].append(sl)
        sl["draft"] = post
        if self.should_illustrate(sl) and self.time_left():
            self.add_image(sl)
        self.send_draft(sl)
        self.persist()
        return True

    def check_trends(self, force: bool = False) -> None:
        if not self.gm or self.quiet() or (not force and (not self.s["schedule_on"] or self.s.get("paused_reason"))):
            return
        key = self.today_key()
        mark = f"{key}-{self.now.hour}"
        if not force:
            if self.now.hour not in TREND_HOURS or mark in self.s.get("trend_checks", []) or not self.time_left():
                return
            if sum(1 for x in self.slots() if x["kind"] == "hype") >= MAX_HYPE_PER_DAY:
                return
        self.s["trend_checks"] = (self.s.get("trend_checks", []) + [mark])[-20:]
        try:
            items = trends.signals(log=log)
            pick = writer.pick_trend(self.gm, items, self.s.get("trends_used", []))
        except Exception as e:
            log("trends failed:", e)
            if force:
                self.say(f"Не вдалося отримати тренди: {net.scrub(str(e))[:200]}")
            return
        if not pick:
            if force:
                self.say("Зараз у трендах немає безпечної теми для нас (або все про війну і політику).")
            return
        self.s["trends_used"] = (self.s.get("trends_used", []) + [pick["title"]])[-60:]
        self.make_hype(pick["topic"], url=pick["url"], source="тренди і новини")

    # ---------- owner tools
    def status_text(self) -> str:
        lines = [f"Щоденні чернетки: {'увімкнені' if self.s['schedule_on'] else 'вимкнені (🟢 Активувати)'}"]
        if self.quiet():
            lines.append("Сьогодні тиша, завтра продовжу.")
        if self.s.get("paused_reason"):
            lines.append(f"Пауза: {self.s['paused_reason']} (увімкнути: 🟢 Активувати)")
        sl = self.slots()
        if sl:
            lines.append("Сьогодні:")
            lines += [f"  {hhmm(x['at'])} {'продаючий' if x['kind'] == 'sell' else ''} - {STATUS_UA.get(x['status'], x['status'])}"
                      for x in sl]
        if self.s["posts"]:
            p = self.s["posts"][-1]
            lines.append(f"Останній пост: {p['at'][:16].replace('T', ' ')} {p.get('permalink') or ''}")
        tok = self.s.get("token") or {}
        lines.append(f"Токен Threads продовжено: {(tok.get('refreshed_at') or 'ще ні')[:10]}")
        lines.append(f"Модель Gemini: {self.s.get('gemini_model') or 'ще не вибрана'}")
        return "\n".join(lines)

    def selftest(self) -> str:
        out = ["Перевірка:"]

        def chk(name, fn):
            try:
                r = fn()
                out.append(f"✅ {name}" + (f": {r}" if r else ""))
                return True
            except Exception as e:
                out.append(f"❌ {name}: {net.scrub(str(e))[:200]}")
                return False

        if self.tg:
            chk("Telegram", lambda: "@" + self.tg.get_me()["username"])
            out.append("✅ Власника знайдено" if self.owner else "⚠️ Власника не знайдено: напишіть боту «Активувати»")
        else:
            out.append("❌ Немає секрету TELEGRAM_BOT_TOKEN")
        if self.th:
            ok = chk("threads_basic", lambda: "@" + self.th.me().get("username", "?"))
            if ok:
                chk("threads_content_publish (ліміт)", lambda: str(self.th.publishing_limit().get("data", [{}])[0].get("quota_usage", "?")) + " постів за 24 год")
                posts: list[dict] = []

                def load_posts():
                    posts.extend(self.th.my_posts(3))
                    return f"{len(posts)} останніх"
                chk("свої пости", load_posts)
                if posts:
                    chk("threads_read_replies", lambda: f"{len(self.th.replies(posts[0]['id']))} відповідей під останнім постом")
                chk("threads_manage_insights", lambda: "ok" if self.th.insights() is not None else "")
                out.append("ℹ️ threads_manage_replies перевіриться під час першої відповіді на коментар")
        else:
            out.append("❌ Немає секрету THREADS_TOKEN")
        if self.gm:
            if chk("Gemini, моделі", lambda: ", ".join(self.gm.list_models()[:3]) or "немає flash-моделей"):
                chk("Gemini, текст", lambda: (self.gm.generate("Відповідай одним словом: так", temperature=0) and self.gm.model))
                chk("Gemini, пошук Google", lambda: (self.gm.generate("Яка столиця України? Одне слово.", search=True, temperature=0)[:30]))
                if self.gm.model:
                    self.s["gemini_model"] = self.gm.model if not str(self.gm.model).startswith("cloudflare") else self.s.get("gemini_model")
        else:
            out.append("❌ Немає секрету GEMINI_API_KEY")
        out += secret_shapes()
        if self.images:
            def img_test():
                prompt = writer.render_prompt("The man stands next to a neat stacked cubic meter of firewood "
                                              "and proudly measures it with an orange tape measure.")
                res = self.images.generate(prompt, log=log)
                if self.tg and self.owner:
                    self.tg.send_photo(self.owner, res["bytes"], f"Пробна картинка: Іванович ({res['provider']})")
                return f"{res['provider']}, {len(res['bytes']) // 1024} КБ"
            out.append(f"ℹ️ Генератори картинок: {', '.join(self.images.providers())}")
            chk("Картинка", img_test)
            if self.images.can_host:
                chk("Сховище картинок", lambda: self.images.media_repo + " → гілка media" + (" (публічне)" if not net.request(
                    "GET", f"https://api.github.com/repos/{self.images.media_repo}",
                    headers={"Authorization": f"Bearer {self.images.media_token}"}).get("private") else " ⚠️ репозиторій приватний, потрібен Public"))
            else:
                out.append("⚠️ Немає сховища картинок: картинки не опублікувати")
        return "\n".join(out)

    def preview(self, n: int) -> None:
        if not self.gm:
            self.say("Немає ключа Gemini.")
            return
        self.say(f"Пробні пости ({n} шт.), нічого не публікується. Пишу, це займе кілька хвилин.")
        for i in range(n):
            if not self.time_left():
                break
            sell = i == n - 1 and n >= 3
            post = self.write(sell)
            if post:
                fact = {"ok": "факти ✅", "fixed": "факти виправлено ✅", "unchecked": "факти НЕ перевірено ⚠️",
                        "nosearch": "факти перевірено без пошуку ⚠️"}.get(post["fact"], "")
                self.say(f"Пробний {i + 1}/{n}{' · продаючий' if sell else ''} · {post['rubric']} · оцінка {post['score']}/10 · {fact}\n\n{post['text']}")
            else:
                self.say(f"Пробний {i + 1}/{n}: не пройшов фільтри й оцінку, пропускаю.")

    # ---------- one run
    def tick(self) -> None:
        self.ensure_menu()
        self.handle_updates()
        self.persist()
        self.roll_days()
        self.plan_today()
        self.publish_due()
        self.draft_pending()
        self.morning_brief()
        self.check_comments()
        self.check_trends()
        self.publish_due()
        self.maybe_refresh_token()
        self.s["last_run"] = self.now.isoformat()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="tick", choices=["tick", "selftest", "preview", "trends", "hype", "draw"])
    ap.add_argument("--state", default=".state/state.json")
    ap.add_argument("--n", type=int, default=3)
    a = ap.parse_args(argv)

    tg_token = net.env("TELEGRAM_BOT_TOKEN")
    th_token = net.env("THREADS_TOKEN")
    gm_key = net.env("GEMINI_API_KEY")
    enc_key = net.env("STATE_KEY") or tg_token or "no-key"
    s = st.load(a.state)
    cf_text = CFText(net.env("CF_ACCOUNT_ID"), clean_token(net.env("CF_API_TOKEN")))
    gm = Gemini(gm_key, s.get("gemini_model"), fallback=cf_text or None) if gm_key else None
    images = Images(cf_account=net.env("CF_ACCOUNT_ID"), cf_token=clean_token(net.env("CF_API_TOKEN")),
                    gemini=gm if net.env("GEMINI_IMAGES") == "1" else None,
                    media_repo=net.env("MEDIA_REPO") or net.env("GITHUB_REPOSITORY"), media_token=net.env("GH_MEDIA_TOKEN"))
    bot = Bot(s, images=images, tg=Telegram(tg_token) if tg_token else None,
              gm=gm,
              threads_secret=th_token, enc_key=enc_key, chat_id_env=net.env("TELEGRAM_CHAT_ID"),
              persist=lambda: st.save(a.state, s))
    try:
        if a.task == "tick":
            bot.tick()
            bot.persist()
            bot.listen(int(os.environ.get("LISTEN_SECONDS") or LISTEN_SECONDS))
        else:
            bot.handle_updates()
            if a.task == "selftest":
                bot.say(bot.selftest())
            elif a.task == "trends":
                bot.check_trends(force=True)
            elif a.task in ("hype", "draw"):
                topic = os.environ.get("BOT_TOPIC", "").strip()
                if not topic:
                    bot.say("Не задано тему (поле topic).")
                elif a.task == "hype":
                    bot.make_hype(topic, url="", source="ваш тест", want_image=True)
                else:
                    bot.draw(topic)
            else:
                bot.preview(max(1, min(a.n, 5)))
    finally:
        st.save(a.state, s)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary and bot.summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write("\n\n".join(net.scrub(x) for x in bot.summary) + "\n")
        for line in bot.summary:
            log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
