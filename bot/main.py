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

STATUS_RU = {"new": "ждёт черновика", "sent": "ждёт вашего решения", "approved": "одобрен",
             "publishing": "публикуется", "published": "опубликован", "failed": "ошибка",
             "skipped": "пропущен", "expired": "не успел"}
HELP = ("Всё есть в меню кнопками внизу чата:\n"
        "📅 План: что запланировано сегодня, включить или выключить расписание\n"
        "📊 Статистика: просмотры, лайки, подписчики за 7 дней и лучшие посты\n"
        "✍️ Пост сейчас: обычный или продающий черновик прямо сейчас\n"
        "🔥 Хайп: тренды Украины сейчас или своя тема (можно скриншот)\n"
        "🎨 Картинка: Иванович рисует что угодно, ничего не публикуется\n"
        "💬 Комментарии: свежие ответы под нашими постами\n"
        "🧠 Чат с ботом: пишите обычными словами, бот отвечает и меняет свои правила (после вашего ✅)\n"
        "⚙️ Настройки: тишина на день, авто-картинки, пробные посты, проверка\n\n"
        "Команды тоже работают:\n/status - что запланировано\n/on - включить ежедневные черновики (3 в день)\n"
        "/off - выключить\n/tysha - тишина на сегодня (день траура или тяжёлых обстрелов)\n/preview 3 - показать N пробных постов (ничего не публикуется)\n"
        "/selftest - проверить ключи и разрешения\n/draw тема - пробная картинка с Ивановичем (ничего не публикуется)\n\n"
        "Под черновиком кнопки: Опубликовать / Другой вариант / Пропустить. "
        "Чтобы поправить текст, ответьте (Reply) на черновик своим текстом.\n"
        "Бот просыпается раз в 30 минут, поэтому ответ приходит с задержкой.")
MENU = [["📅 План", "📊 Статистика"], ["✍️ Пост сейчас", "🔥 Хайп"],
        ["🎨 Картинка", "💬 Комментарии"], ["🧠 Чат с ботом", "⚙️ Настройки"], ["❓ Помощь"]]
MENU_VERSION = 2
CHAT_IDLE_MINUTES = 30
MENU_ACTIONS = {"📅 План": "menu_plan", "📊 Статистика": "menu_stats", "✍️ Пост сейчас": "menu_post",
                "🔥 Хайп": "menu_hype", "🎨 Картинка": "menu_draw", "💬 Комментарии": "menu_comments",
                "⚙️ Настройки": "menu_settings", "❓ Помощь": "menu_help", "🧠 Чат с ботом": "menu_chat"}
COMMANDS = [("menu", "Показать меню"), ("status", "План на сегодня"), ("stats", "Статистика Threads"),
            ("post", "Черновик прямо сейчас"), ("hype", "Пост на горячую тему"), ("draw", "Картинка с Ивановичем"),
            ("comments", "Новые комментарии"), ("on", "Включить расписание"), ("off", "Выключить расписание"),
            ("tysha", "Тишина на сегодня"), ("selftest", "Проверка"), ("help", "Помощь")]
AWAIT_MINUTES = 20
LISTEN_SECONDS = 240  # after a scheduled run the bot keeps listening to Telegram, so buttons answer within seconds
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
                out.append(f"ℹ️ CF_API_TOKEN: вставлен целый блок текста, бот вытащил из него токен (длина {len(fixed)})")
                v = raw = fixed
        issues = []
        if raw != v:
            issues.append("пробелы или перенос строки по краям")
        if " " in v or "\n" in v:
            issues.append("пробелы внутри")
        if not ok(v):
            issues.append(hint)
        out.append(f"{'✅' if not issues else '⚠️'} {name}: длина {len(v)}" + (f", {'; '.join(issues)}" if issues else ""))

    shape("CF_ACCOUNT_ID", lambda v: bool(_re.fullmatch(r"[0-9a-f]{32}", v)), "ожидается 32 символа 0-9 a-f (Account ID)")
    shape("CF_API_TOKEN", lambda v: 35 <= len(v) <= 60 and bool(_re.fullmatch(r"[A-Za-z0-9_\-]+", v)) and not _re.fullmatch(r"[0-9a-f]{32}", v),
          "не похоже на API Token Cloudflare (обычно 40 символов; не путать с Account ID и Global API Key)")
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
                self.say(f"⚠️ Не удалось продлить токен Threads: {e}. Если через несколько дней не пройдёт, "
                         "нужно будет сгенерировать новый и обновить секрет THREADS_TOKEN.")
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
            if text.lower().lstrip("/").startswith("start") or text.lower() in ("привет", "привіт"):
                self.s["owner_chat_id"] = chat["id"]
                self.say("Готово, теперь бот пишет вам.\n\n" + HELP, keyboard=MENU)
            return
        if chat["id"] != self.owner:
            return
        if re.fullmatch(r"[A-Za-z0-9_\-:.]{60,}", text):
            self.tg.delete(chat["id"], m["message_id"])
            self.say("Похоже на ключ или токен. Я удалил сообщение: секреты вводятся только в GitHub → Settings → Secrets.")
            return
        if text in MENU_ACTIONS:
            self.s.pop("await", None)
            return getattr(self, MENU_ACTIONS[text])()
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        aw = self.s.get("await")
        if aw and not reply_to and not text.startswith("/"):
            self.s.pop("await", None)
            if since_minutes(aw.get("at"), now_kyiv()) <= AWAIT_MINUTES:
                img = self.tg.download_photo(m["photo"][-1]["file_id"]) if m.get("photo") else None
                topic = text or ("тема на скріншоті" if img else "")
                if aw["what"] == "draw" and topic:
                    self.say("🎨 Рисую, это около минуты…")
                    return self.draw(topic)
                if aw["what"] == "hype" and topic:
                    self.say("🔥 Пишу пост на эту тему, около минуты…")
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
            return self.on_edit(reply_to, text)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help", "/menu"):
            self.say(HELP, keyboard=MENU)
        elif cmd == "/status":
            self.menu_plan()
        elif cmd == "/stats":
            self.menu_stats()
        elif cmd == "/post":
            self.menu_post()
        elif cmd == "/comments":
            self.menu_comments()
        elif cmd == "/on":
            self.s["schedule_on"], self.s["paused_reason"], self.s["fail_streak"] = True, None, 0
            self.say("Включено: каждый день 3 черновика, публикация только после вашей кнопки.")
        elif cmd == "/off":
            self.s["schedule_on"] = False
            self.say("Выключено. Новые черновики не готовлю, одобренные тоже не публикую.")
        elif cmd == "/hype" and not arg.strip() and not m.get("photo") and not m.get("reply_to_message"):
            self.menu_hype()
        elif cmd == "/hype":
            img = None
            if m.get("photo"):
                img = self.tg.download_photo(m["photo"][-1]["file_id"])
            fwd = (m.get("reply_to_message") or {}).get("text") or ""
            topic = " ".join(x for x in (arg.strip(), fwd.strip()) if x) or ("тема на скріншоті" if img else "")
            if not topic:
                self.say("Напишите после /hype, о чём хайп, или пришлите скрин с подписью /hype.")
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
            self.say("Тишина до конца дня: ничего не публикую и не присылаю черновики. Завтра продолжу сам.")
        elif cmd == "/preview":
            n = int(arg) if arg.strip().isdigit() else 3
            self.preview(max(1, min(n, 5)))
        elif cmd == "/selftest":
            self.say(self.selftest())
        else:
            self.say("Не понял. Выберите действие в меню внизу 👇", keyboard=MENU)

    def on_edit(self, msg_id: int, text: str) -> None:
        for d in self.s["days"].values():
            for sl in d["slots"]:
                if sl.get("msg_id") == msg_id and sl["status"] in ("sent", "approved"):
                    if len(text) > 500:
                        self.say("Threads принимает до 500 символов, ваш текст длиннее. Сократите, пожалуйста.")
                        return
                    sl["draft"] = {**(sl.get("draft") or {}), "text": text, "score": None, "fact": "owner"}
                    sl["status"] = "new"
                    self.send_draft(sl, note="Ваш вариант (публикую слово в слово):")
                    return
        self.say("Не нашёл черновик, на который вы ответили (возможно, он уже опубликован или устарел).")

    def slot_by_msg(self, msg_id: int) -> dict | None:
        for d in self.s["days"].values():
            for sl in d["slots"]:
                if sl.get("msg_id") == msg_id and sl["status"] in ("sent", "approved"):
                    return sl
        return None

    def on_owner_photo(self, msg_id: int, file_id: str) -> None:
        sl = self.slot_by_msg(msg_id)
        if not sl:
            self.say("Не нашёл черновик, на который вы ответили картинкой.")
            return
        sl["draft"]["image"] = {"file_id": file_id, "provider": "ваша картинка", "url": ""}
        self.send_draft(sl, note="Черновик с вашей картинкой:")

    def on_callback(self, cq: dict) -> None:
        if cq.get("from", {}).get("id") != self.owner and cq.get("message", {}).get("chat", {}).get("id") != self.owner:
            return
        action, _, rest = (cq.get("data") or "").partition("|")
        if action == "m":
            self.tg.answer(cq["id"])
            return self.on_menu(rest)
        sid, _, ver = rest.partition("|")
        sl = self.find_slot(sid)
        if not sl or str(sl.get("v")) != ver or sl["status"] not in ("sent", "approved"):
            self.tg.answer(cq["id"], "Этот черновик уже неактуален")
            return
        msg_id = cq["message"]["message_id"]
        text = sl["draft"]["text"]
        if action == "ok":
            sl["status"] = "approved"
            when = hhmm(sl["at"])
            self.tg.answer(cq["id"], "Одобрено")
            self.tg.edit(self.owner, msg_id, f"✅ Одобрено, выйдет около {when} (или при следующем запуске, если время прошло)\n\n{text}")
        elif action == "re":
            sl["rejects"] = sl.get("rejects", 0) + 1
            self.tg.answer(cq["id"], "Готовлю другой вариант")
            self.tg.edit(self.owner, msg_id, f"🔁 Отклонено, готовлю другой вариант\n\n{text}")
            sl["status"] = "new" if sl["rejects"] < 4 else "skipped"
            sl["draft"] = None
        elif action == "im":
            sl["want_image"] = True
            sl["draft"].pop("image", None)
            sl["status"] = "new"
            self.tg.answer(cq["id"], "Рисую картинку")
            self.tg.edit(self.owner, msg_id, f"🎨 Рисую картинку к этому посту, пришлю при следующем запуске\n\n{text}")
        elif action == "ni":
            sl["want_image"] = False
            sl["draft"].pop("image", None)
            self.tg.answer(cq["id"], "Убрал картинку")
            self.tg.edit(self.owner, msg_id, f"Картинку убрал, черновик ниже\n\n{text}")
            self.send_draft(sl)
        elif action == "ds":
            self.tg.answer(cq["id"], "Пишите, что не так")
            self.s["chat_mode"] = now_kyiv().isoformat()
            self.s["chat_focus"] = sl["id"]
            self.say("💬 Что поправить в этом посте или картинке? Пишите обычными словами: «сделай смешнее», "
                     "«убери последнюю фразу», «на картинке пусть Денис колет дрова», «картинка не нужна».",
                     buttons=[[("🚪 Закончить обсуждение", "m|chatoff")]])
        elif action == "sk":
            sl["status"] = "skipped"
            self.tg.answer(cq["id"], "Пропускаю")
            self.tg.edit(self.owner, msg_id, f"⏭ Пропущено\n\n{text}")

    # ---------- menu
    def menu_plan(self) -> None:
        on = self.s["schedule_on"] and not self.s.get("paused_reason")
        self.say(self.status_text(), buttons=[
            [("⏸ Выключить расписание", "m|off") if on else ("▶️ Включить расписание", "m|on")],
            [("📆 Завтра", "m|plan|1"), ("📆 Послезавтра", "m|plan|2")],
            [("🤫 Тишина сегодня", "m|quiet"), ("✍️ Пост сейчас", "m|postmenu")]])

    def menu_post(self) -> None:
        self.say("Какой черновик сделать прямо сейчас? Публикация всё равно только после вашей кнопки.", buttons=[
            [("📝 Обычный", "m|post"), ("💰 Продающий", "m|sell")],
            [("👀 3 пробных (без публикации)", "m|preview")]])

    def menu_hype(self) -> None:
        self.say("🔥 Хайп: что сделать?", buttons=[
            [("📰 Что сейчас обсуждают", "m|digest")],
            [("⚡ Сразу пост по лучшему тренду", "m|trends")],
            [("✏️ Своя тема или скриншот", "m|hypeown")]])

    def menu_digest(self) -> None:
        if not self.gm:
            return self.say("Нет ключа Gemini.")
        self.say("📰 Смотрю Google Trends и киевские новости, около минуты…")
        try:
            topics = writer.digest(self.gm, trends.signals(log=log))
        except Exception as e:
            return self.say(f"Не получилось: {net.scrub(str(e))[:200]}")
        if not topics:
            return self.say("Сейчас нет безопасных горячих тем для нас.")
        self.s["digest"] = topics
        lines = ["📰 О чём сейчас говорят в Киеве и Украине:"]
        for i, t in enumerate(topics):
            lines.append(f"\n{i + 1}. {t['title']} {'🔥' * min(5, int(t['heat'] or 0)) if str(t['heat']).isdigit() else ''}\n"
                         f"   Почему горячо: {t['why']}\n   Наш кут: {t['angle']}")
        btns = [[(f"✍️ Пост на тему {i + 1}", f"m|dig|{i}") for i in range(j, min(j + 2, len(topics)))]
                for j in range(0, len(topics), 2)]
        self.say("\n".join(lines), buttons=btns)

    def menu_draw(self) -> None:
        self.s["await"] = {"what": "draw", "at": now_kyiv().isoformat()}
        self.say("Напишите одним сообщением, что нарисовать. Например: «Иванович складывает граб в куб и меряет рулеткой».")

    def menu_settings(self) -> None:
        auto = self.s.get("auto_images", True)
        on = self.s["schedule_on"] and not self.s.get("paused_reason")
        self.say("⚙️ Настройки", buttons=[
            [("⏸ Выключить расписание", "m|off") if on else ("▶️ Включить расписание", "m|on")],
            [("🤫 Тишина сегодня", "m|quiet"), ("🔔 Отменить тишину", "m|unquiet")],
            [(f"🖼 Авто-картинки: {'вкл' if auto else 'выкл'}", "m|autoimg"), ("📋 Мои правила", "m|rules")],
            [(f"🤖 Автопилот: {'вкл' if self.s.get('autopilot') else 'выкл'}", "m|autopilot")],
            [("👀 3 пробных поста", "m|preview"), ("🩺 Проверка", "m|selftest")]])

    def menu_help(self) -> None:
        self.say(HELP, keyboard=MENU)

    def on_menu(self, what: str) -> None:
        if what == "on":
            self.s["schedule_on"], self.s["paused_reason"], self.s["fail_streak"] = True, None, 0
            self.say("▶️ Включено: каждый день 3 черновика, публикация только после вашей кнопки.")
        elif what == "off":
            self.s["schedule_on"] = False
            self.say("⏸ Выключено. Новые черновики не готовлю, одобренные тоже не публикую.")
        elif what == "quiet":
            self.s["quiet_day"] = self.today_key()
            self.say("🤫 Тишина до конца дня: ничего не публикую и не присылаю черновики. Завтра продолжу сам.")
        elif what == "unquiet":
            self.s.pop("quiet_day", None)
            self.say("🔔 Тишина отменена, работаю как обычно.")
        elif what == "autoimg":
            self.s["auto_images"] = not self.s.get("auto_images", True)
            self.say("🖼 Авто-картинки " + ("включены: бот сам решает, где нужна картинка (до 1 в день + к хайпу)."
                                           if self.s["auto_images"] else "выключены. Картинку можно добавить кнопкой под черновиком."))
        elif what == "autopilot":
            self.s["autopilot"] = not self.s.get("autopilot")
            self.say("🤖 Автопилот включён: хорошие черновики (оценка 8+, факты проверены) выходят сами в своё время. "
                     "Под каждым будет «Не публиковать» и «Обсудить»." if self.s["autopilot"] else
                     "🤖 Автопилот выключен: публикую только после вашей кнопки.")
        elif what.startswith("plan|"):
            self.plan_ahead(int(what.partition("|")[2] or 1))
        elif what == "postmenu":
            self.menu_post()
        elif what in ("post", "sell"):
            self.say("✍️ Пишу черновик, около минуты…")
            self.make_now(sell=what == "sell")
        elif what == "preview":
            self.preview(3)
        elif what == "trends":
            self.say("📈 Смотрю тренды Украины…")
            self.check_trends(force=True)
        elif what == "digest":
            self.menu_digest()
        elif what.startswith("dig|"):
            i = what.partition("|")[2]
            topics = self.s.get("digest") or []
            if i.isdigit() and int(i) < len(topics):
                t = topics[int(i)]
                self.say(f"🔥 Пишу пост: {t['title']}")
                self.make_hype(f"{t['source']}: {t['title']}. Кут: {t['angle']}", url=t.get("url", ""), source="ваш выбор из трендов")
        elif what == "hypeown":
            self.s["await"] = {"what": "hype", "at": now_kyiv().isoformat()}
            self.say("Напишите тему одним сообщением или пришлите скриншот из ленты (можно с подписью).")
        elif what == "selftest":
            self.say(self.selftest())
        elif what == "chatoff":
            self.s.pop("chat_mode", None)
            self.s.pop("chat_focus", None)
            self.say("Вышел из чата. Меню внизу 👇", keyboard=MENU)
        elif what == "rules":
            self.menu_rules()
        elif what.startswith("delrule|") or what.startswith("delfact|"):
            kind, _, i = what.partition("|")
            key = "owner_rules" if kind == "delrule" else "owner_facts"
            items = self.s.get(key, [])
            if i.isdigit() and int(i) < len(items):
                gone = items.pop(int(i))
                writer.set_owner(self.s.get("owner_rules", []), self.s.get("owner_facts", []))
                self.say(f"🗑 Убрал: {gone}")
                self.menu_rules()
        elif what.startswith("apply|") or what.startswith("reject|"):
            act, _, pid = what.partition("|")
            pend = self.s.get("chat_pending") or {}
            if str(pend.get("id")) != pid:
                return self.say("Это предложение уже неактуально.")
            self.s.pop("chat_pending", None)
            if act == "reject":
                return self.say("Хорошо, ничего не меняю.")
            self.apply_changes(pend["changes"])

    # ---------- owner chat
    def menu_chat(self) -> None:
        self.s["chat_mode"] = now_kyiv().isoformat()
        self.say("🧠 Чат с ботом. Пишите обычными словами, можно скриншот. Например:\n"
                 "• «не пиши про сосну, мы её почти не продаём»\n"
                 "• «у нас теперь доставка в Ирпень бесплатная»\n"
                 "• «сделай пост про то, как Иванович топит камин»\n"
                 "• «почему вчерашний пост так мало набрал?»\n\n"
                 "Изменения применяю только после вашей кнопки ✅. Чат сам закроется через 30 минут тишины.",
                 buttons=[[("📋 Мои правила", "m|rules"), ("🚪 Выйти из чата", "m|chatoff")]])

    def menu_rules(self) -> None:
        rules_, facts_ = self.s.get("owner_rules", []), self.s.get("owner_facts", [])
        if not rules_ and not facts_:
            return self.say("📋 Ваших правил пока нет. Скажите их в 🧠 Чате с ботом.")
        lines = ["📋 Ваши правила для постов:"] + [f"{i + 1}. {r}" for i, r in enumerate(rules_)]
        if facts_:
            lines += ["", "Факты о бизнесе:"] + [f"{i + 1}. {f}" for i, f in enumerate(facts_)]
        btns = [[(f"🗑 Правило {i + 1}", f"m|delrule|{i}")] for i in range(min(len(rules_), 8))]
        btns += [[(f"🗑 Факт {i + 1}", f"m|delfact|{i}")] for i in range(min(len(facts_), 4))]
        self.say("\n".join(lines), buttons=btns or None)

    def chat_turn(self, text: str, images: list | None = None) -> None:
        self.s["chat_mode"] = now_kyiv().isoformat()
        if not self.gm:
            return self.say("Нет ключа Gemini, чат не работает.")
        hist = self.s.setdefault("chat_log", [])
        try:
            expertise = (" | ".join(writer.knowledge_sample(n=25)) + "\n" + writer.kyiv_context(self.now.date()))
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
            return self.say(f"Не смог ответить: {net.scrub(str(e))[:200]}")
        hist += [{"owner": text[:500]}, {"bot": res["reply"][:500]}]
        del hist[:-20]
        rules_, facts_ = self.s.get("owner_rules", []), self.s.get("owner_facts", [])
        described = [(c, chat.describe(c, rules_, facts_)) for c in res["changes"]]
        described = [(c, d) for c, d in described if d]
        if not described:
            return self.say(res["reply"] or "…")
        instant = ("draft", "draw", "plan", "edit_text", "redraw", "remove_image", "approve", "skip")
        if all(c["type"] in instant for c, _ in described):
            self.say(res["reply"] or "Делаю.")
            return self.apply_changes([c for c, _ in described])
        pid = int(time.time()) % 100000
        self.s["chat_pending"] = {"id": pid, "changes": [c for c, _ in described]}
        self.say((res["reply"] + "\n\n" if res["reply"] else "") + "Предлагаю:\n" + "\n".join(d for _, d in described),
                 buttons=[[("✅ Применить", f"m|apply|{pid}"), ("❌ Не надо", f"m|reject|{pid}")]])

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
                done.append(f"убрал «{items.pop(i)}»")
        for c in changes:
            t = c["type"]
            if t == "rule_add" and c.get("text"):
                rules_.append(str(c["text"])[:300])
                done.append("правило добавлено")
            elif t == "fact_add" and c.get("text"):
                facts_.append(str(c["text"])[:300])
                done.append("факт добавлен")
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
            if t in ("edit_text", "redraw", "remove_image", "approve", "skip"):
                if not fsl or not fsl.get("draft") or fsl["status"] not in ("sent", "approved", "new"):
                    self.say("Не нашёл черновик для правки, он уже опубликован или пропущен.")
                    continue
                d = fsl["draft"]
                if t == "edit_text" and c.get("text"):
                    d.update({"text": str(c["text"]).strip()[:500], "score": d.get("score"), "fact": d.get("fact")})
                    touched = True
                elif t == "redraw":
                    fsl["want_image"], fsl["image_hint"] = True, str(c.get("hint") or "")[:400]
                    d.pop("image", None)
                    self.say("🎨 Перерисовываю…")
                    self.add_image(fsl)
                    touched = True
                elif t == "remove_image":
                    fsl["want_image"] = False
                    d.pop("image", None)
                    touched = True
                elif t == "approve":
                    fsl["status"] = "approved"
                    self.say(f"✅ Одобрено, выйдет около {hhmm(fsl['at'])}.")
                elif t == "skip":
                    fsl["status"] = "skipped"
                    self.say("⏭ Пропускаю этот пост.")
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
                self.tg.edit(self.owner, fsl["msg_id"], "✏️ Обновлено, новая версия ниже.")
            self.send_draft(fsl, note="✏️ Исправленный черновик:")

    def dev_request(self, text: str) -> None:
        """Things the bot cannot change itself go to GitHub Issues: the developer (Claude) picks them up."""
        if not text:
            return
        repo, token = (self.images.media_repo, self.images.media_token) if self.images else ("", "")
        if not (repo and token):
            self.s.setdefault("dev_requests", []).append(text[:1000])
            return self.say("🛠 Записал задачу для разработчика: " + text[:300])
        try:
            res = net.request("POST", f"https://api.github.com/repos/{repo}/issues",
                              json_body={"title": "Из чата с ботом: " + text[:80],
                                         "body": text + "\n\n_Создано ботом по просьбе владельца в Telegram-чате._"},
                              headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
            self.say(f"🛠 Это нужно менять в коде, создал задачу разработчику: {res.get('html_url', '')}\n"
                     "Скажите Claude в проекте «сделай задачи из GitHub», и он их выполнит.")
        except Exception as e:
            self.s.setdefault("dev_requests", []).append(text[:1000])
            self.say(f"🛠 Записал задачу для разработчика (GitHub не принял: {net.scrub(str(e))[:100]}).")

    def make_now(self, sell: bool) -> bool:
        if not self.gm:
            self.say("Нет ключа Gemini.")
            return False
        key = self.today_key()
        day = self.s["days"].setdefault(key, {"slots": []})
        n = sum(1 for x in day["slots"] if x["kind"] in ("now", "sell-now"))
        sl = {"id": f"{key[5:7]}{key[8:]}-n{n}", "at": minutes_of(now_kyiv()), "kind": "now", "status": "new",
              "tries": 1, "rejects": 0, "v": 0, "draft": None, "msg_id": None, "sell": sell}
        post = self.write(sell)
        if not post:
            self.say("Не получилось написать хороший пост с первого раза, попробуйте ещё раз.")
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
            return self.say("Нет доступа к Threads.")
        lines = ["📊 Статистика Threads за 7 дней"]
        until = int(time.time())
        try:
            st7 = self.th.account_stats(until - 7 * 86400, until)
            prev = self.s.get("followers_prev")
            f = st7.get("followers_count")
            lines += [f"👀 Просмотры: {st7.get('views', 0)}", f"❤️ Лайки: {st7.get('likes', 0)}",
                      f"💬 Ответы: {st7.get('replies', 0)}", f"🔁 Репосты: {st7.get('reposts', 0)}, цитаты: {st7.get('quotes', 0)}",
                      f"👥 Подписчики: {f}" + (f" ({f - prev:+d} с прошлой проверки)" if isinstance(prev, int) and f is not None else "")]
            if f is not None:
                self.s["followers_prev"] = f
        except Exception as e:
            lines.append(f"⚠️ Общая статистика недоступна: {net.scrub(str(e))[:150]}")
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
                lines.append("\n🏆 Лучшие из последних 10 постов:")
                for v, l, r, p in rows[:3]:
                    lines.append(f"{v} 👀 {l} ❤️ {r} 💬 · {(p.get('text') or '').strip()[:70]}… {p.get('permalink', '')}")
        except Exception as e:
            lines.append(f"⚠️ Посты недоступны: {net.scrub(str(e))[:150]}")
        week = [d for k, d in self.s["days"].items() if since_minutes(k + "T12:00:00+02:00", now_kyiv()) <= 7 * 1440]
        cnt = {}
        for d in week:
            for x in d["slots"]:
                cnt[x["status"]] = cnt.get(x["status"], 0) + 1
        lines.append(f"\n🤖 Бот за неделю: опубликовано {cnt.get('published', 0)}, пропущено {cnt.get('skipped', 0)}, "
                     f"ждёт решения {cnt.get('sent', 0)}")
        self.say("\n".join(lines))

    def menu_comments(self) -> None:
        if not self.th:
            return self.say("Нет доступа к Threads.")
        out = []
        try:
            for p in self.th.my_posts(5):
                for r in self.th.replies(p["id"])[:5]:
                    out.append((r.get("timestamp", ""), f"@{r.get('username', '?')}: {(r.get('text') or '').strip()[:150]}\n"
                                f"   ↳ под постом «{(p.get('text') or '').strip()[:40]}…» {p.get('permalink', '')}"))
        except Exception as e:
            return self.say(f"⚠️ Не смог загрузить комментарии: {net.scrub(str(e))[:150]}")
        if not out:
            return self.say("💬 Под последними 5 постами комментариев пока нет.")
        out.sort(reverse=True)
        self.say("💬 Свежие комментарии:\n\n" + "\n\n".join(x for _, x in out[:10]) +
                 "\n\nОтвечать пока лучше вручную в Threads. Скоро бот будет предлагать ответы кнопкой.")

    def ensure_menu(self) -> None:
        if not self.tg or not self.owner or self.s.get("menu_v") == MENU_VERSION:
            return
        try:
            self.tg.set_commands(COMMANDS)
        except net.NetError as e:
            log("setMyCommands failed:", e)
        self.say("Обновил бота: теперь всё в меню кнопками внизу 👇\n\n" + HELP, keyboard=MENU)
        self.s["menu_v"] = MENU_VERSION

    def listen(self, seconds: int) -> None:
        """Keep answering Telegram for a few minutes with long polling, so buttons react within seconds."""
        end = time.monotonic() + seconds
        while time.monotonic() < end - 5:
            n = self.handle_updates(wait=int(min(25, end - time.monotonic())))
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
                        self.tg.edit(self.owner, sl["msg_id"], "⌛ День закончился, этот черновик не опубликован.\n\n"
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
        self.say(f"📆 Готовлю план на {d.strftime('%d.%m')}: черновики пришлю по одному, одобряйте как обычно.")
        self.draft_pending(force=True)
        self.persist()

    def morning_brief(self) -> None:
        """Once a day after 8:00: what is planned today, after the drafts are ready."""
        key = self.today_key()
        if self.s.get("brief_day") == key or self.now.hour < 8 or not self.s["schedule_on"]:
            return
        sl = self.slots()
        if not sl or any(x["status"] == "new" for x in sl):
            return
        self.s["brief_day"] = key
        lines = ["☀️ Доброе утро! План на сегодня:"]
        for x in sl:
            text = ((x.get("draft") or {}).get("text") or "").split("\n")[0][:70]
            lines.append(f"{hhmm(x['at'])} {'💰' if x['kind'] == 'sell' else '📝'} {STATUS_RU.get(x['status'], x['status'])}: {text}")
        lines.append("\nЧерновики выше, одобряйте или жмите «Обсудить». " +
                     ("Автопилот включён: одобренные ботом выйдут сами." if self.s.get("autopilot") else ""))
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
                self.say(f"⚠️ Не смог написать хороший пост на {hhmm(sl['at'])} за 5 попыток, слот пропущен.")
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
            res, ip = self.render_image(d["text"], topic, hint=sl.pop("image_hint", ""))
        except Exception as e:
            log("image failed:", e)
            d["image_error"] = net.scrub(str(e))[:200]
            return
        d["image"] = {"provider": res["provider"], "url": "", "idea": ip["idea"], "mode": ip["mode"]}
        d.pop("image_error", None)
        self._image_bytes = res["bytes"]

    def render_image(self, text: str, topic: str = "", hint: str = "") -> tuple[dict, dict]:
        """Meme with Ivanovych (panels + captions in meme fonts) or a cosy scene without him."""
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

    def draw(self, topic: str) -> None:
        if not self.images or not self.gm:
            self.say("Картинки не настроены.")
            return
        if not topic:
            self.say("Напишите после /draw, что нарисовать, например: /draw Динамо выиграло, а у нас дрова сухие")
            return
        try:
            res, ip = self.render_image(topic, topic=topic)
            self.tg.send_photo(self.owner, res["bytes"], f"Пробная картинка (не публикуется): {ip['idea']}"[:1000])
        except Exception as e:
            self.say(f"Не получилось нарисовать: {net.scrub(str(e))[:200]}")

    def send_draft(self, sl: dict, note: str | None = None) -> None:
        d = sl["draft"]
        sl["v"] = sl.get("v", 0) + 1
        fact = {"ok": "факты ✅", "fixed": "факты исправлены ✅", "unchecked": "факты НЕ проверены ⚠️",
                "nosearch": "факты проверены без поиска ⚠️", "owner": "ваш текст"}.get(d.get("fact"), "")
        if sl["kind"] == "hype" and not note:
            note = (f"🔥 Хайп ({sl.get('source', '')}): {sl.get('topic', '')[:150]}\n{sl.get('url') or ''}\n"
                    f"оценка {d.get('score')}/10 · {fact}").strip()
        when = hhmm(sl["at"]) if sl.get("day", self.today_key()) == self.today_key() else \
            f"{sl['day'][8:10]}.{sl['day'][5:7]} {hhmm(sl['at'])}"
        head = note or (f"📝 Черновик на {when}" + (" · продающий" if sl["kind"] == "sell" else "")
                        + f" · {d.get('rubric', '')} · оценка {d.get('score')}/10 · {fact}")
        if d.get("fact_note") and d.get("fact") != "ok":
            head += f"\n({d['fact_note']})"
        img = d.get("image")
        ref = f"{sl['id']}|{sl['v']}"
        buttons = [[("✅ Опубликовать", f"ok|{ref}"), ("💬 Обсудить", f"ds|{ref}")],
                   [("🔁 Другой текст", f"re|{ref}"), ("⏭ Пропустить", f"sk|{ref}")],
                   [("🎨 Другая картинка", f"im|{ref}"), ("🗑 Убрать картинку", f"ni|{ref}")] if img
                   else [("🎨 Добавить картинку", f"im|{ref}")]]
        auto = (self.s.get("autopilot") and (d.get("score") or 0) >= 8 and d.get("fact") in ("ok", "fixed", "nosearch", "owner"))
        if auto:
            head = f"🤖 Автопилот: выйдет в {when} сам. Не так? «Пропустить» или «Обсудить».\n" + head
            buttons[0] = [("💬 Обсудить", f"ds|{ref}"), ("⏭ Не публиковать", f"sk|{ref}")]
        if img and d.get("image_auto"):
            head += f"\n🎨 Бот решил, что здесь нужна картинка: {d['image_auto']}"
        if d.get("image_error") and not img:
            head += f"\n(картинка не получилась: {d['image_error'][:120]})"
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
                    raise RuntimeError("такой пост уже есть в Threads, повтор не публикую")
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
                    self.say("⚠️ Нет хранилища для картинки, публикую пост без неё.")
                    res = self.th.publish_text(text)
            else:
                res = self.th.publish_text(text)
        except Exception as e:
            sl["status"] = "failed"
            self.s["fail_streak"] += 1
            self.s["last_fail"] = self.now.isoformat()
            msg = f"⚠️ Не удалось опубликовать пост на {hhmm(sl['at'])}: {net.scrub(str(e))[:300]}"
            if self.s["fail_streak"] >= 2:
                self.s["paused_reason"] = "две ошибки публикации подряд"
                msg += "\nДве ошибки подряд, ставлю бота на паузу. Включить снова: /on"
            self.say(msg)
            return
        sl["status"], sl["post"] = "published", res
        self.s["fail_streak"] = 0
        self.s["posts"].append({"text": text, "rubric": sl["draft"].get("rubric"), "id": res["id"],
                                "permalink": res.get("permalink"), "at": self.now.isoformat()})
        self.s["posts"] = self.s["posts"][-60:]
        if sl.get("msg_id") and self.tg and self.owner:
            self.tg.edit(self.owner, sl["msg_id"], f"🟢 Опубликовано {res.get('permalink', '')}\n\n{text}")

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
                self.say("Не смог сделать хороший пост на эту тему (не прошёл фильтры или тема запретная).")
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
                self.say(f"Не удалось получить тренды: {net.scrub(str(e))[:200]}")
            return
        if not pick:
            if force:
                self.say("Сейчас в трендах нет безопасной темы для нас (или всё про войну и политику).")
            return
        self.s["trends_used"] = (self.s.get("trends_used", []) + [pick["title"]])[-60:]
        self.make_hype(pick["topic"], url=pick["url"], source="тренды и новости")

    # ---------- owner tools
    def status_text(self) -> str:
        lines = [f"Ежедневные черновики: {'включены' if self.s['schedule_on'] else 'выключены (/on)'}"]
        if self.quiet():
            lines.append("Сегодня тишина (/tysha), завтра продолжу.")
        if self.s.get("paused_reason"):
            lines.append(f"Пауза: {self.s['paused_reason']} (включить: /on)")
        sl = self.slots()
        if sl:
            lines.append("Сегодня:")
            lines += [f"  {hhmm(x['at'])} {'продающий' if x['kind'] == 'sell' else ''} - {STATUS_RU.get(x['status'], x['status'])}"
                      for x in sl]
        if self.s["posts"]:
            p = self.s["posts"][-1]
            lines.append(f"Последний пост: {p['at'][:16].replace('T', ' ')} {p.get('permalink') or ''}")
        tok = self.s.get("token") or {}
        lines.append(f"Токен Threads продлён: {(tok.get('refreshed_at') or 'ещё нет')[:10]}")
        lines.append(f"Модель Gemini: {self.s.get('gemini_model') or 'ещё не выбрана'}")
        return "\n".join(lines)

    def selftest(self) -> str:
        out = ["Проверка:"]

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
            out.append("✅ Владелец найден" if self.owner else "⚠️ Владелец не найден: напишите боту /start")
        else:
            out.append("❌ Нет секрета TELEGRAM_BOT_TOKEN")
        if self.th:
            ok = chk("threads_basic", lambda: "@" + self.th.me().get("username", "?"))
            if ok:
                chk("threads_content_publish (лимит)", lambda: str(self.th.publishing_limit().get("data", [{}])[0].get("quota_usage", "?")) + " постов за 24ч")
                posts: list[dict] = []

                def load_posts():
                    posts.extend(self.th.my_posts(3))
                    return f"{len(posts)} последних"
                chk("свои посты", load_posts)
                if posts:
                    chk("threads_read_replies", lambda: f"{len(self.th.replies(posts[0]['id']))} ответов под последним постом")
                chk("threads_manage_insights", lambda: "ok" if self.th.insights() is not None else "")
                out.append("ℹ️ threads_manage_replies проверится, когда будем отвечать на комментарии")
        else:
            out.append("❌ Нет секрета THREADS_TOKEN")
        if self.gm:
            if chk("Gemini, модели", lambda: ", ".join(self.gm.list_models()[:3]) or "нет flash-моделей"):
                chk("Gemini, текст", lambda: (self.gm.generate("Відповідай одним словом: так", temperature=0) and self.gm.model))
                chk("Gemini, поиск Google", lambda: (self.gm.generate("Яка столиця України? Одне слово.", search=True, temperature=0)[:30]))
                if self.gm.model:
                    self.s["gemini_model"] = self.gm.model if not str(self.gm.model).startswith("cloudflare") else self.s.get("gemini_model")
        else:
            out.append("❌ Нет секрета GEMINI_API_KEY")
        out += secret_shapes()
        if self.images:
            def img_test():
                prompt = writer.render_prompt("The man stands next to a neat stacked cubic meter of firewood "
                                              "and proudly measures it with an orange tape measure.")
                res = self.images.generate(prompt, log=log)
                if self.tg and self.owner:
                    self.tg.send_photo(self.owner, res["bytes"], f"Пробная картинка: Иванович ({res['provider']})")
                return f"{res['provider']}, {len(res['bytes']) // 1024} КБ"
            out.append(f"ℹ️ Генераторы картинок: {', '.join(self.images.providers())}")
            chk("Картинка", img_test)
            if self.images.can_host:
                chk("Хранилище картинок", lambda: self.images.media_repo + " → ветка media" + (" (публичное)" if not net.request(
                    "GET", f"https://api.github.com/repos/{self.images.media_repo}",
                    headers={"Authorization": f"Bearer {self.images.media_token}"}).get("private") else " ⚠️ репозиторий приватный, нужен Public"))
            else:
                out.append("⚠️ Нет хранилища картинок: картинки не опубликовать")
        return "\n".join(out)

    def preview(self, n: int) -> None:
        if not self.gm:
            self.say("Нет ключа Gemini.")
            return
        self.say(f"Пробные посты ({n} шт.), ничего не публикуется. Пишу, это займёт пару минут.")
        for i in range(n):
            if not self.time_left():
                break
            sell = i == n - 1 and n >= 3
            post = self.write(sell)
            if post:
                fact = {"ok": "факты ✅", "fixed": "факты исправлены ✅", "unchecked": "факты НЕ проверены ⚠️",
                        "nosearch": "факты проверены без поиска ⚠️"}.get(post["fact"], "")
                self.say(f"Пробный {i + 1}/{n}{' · продающий' if sell else ''} · {post['rubric']} · оценка {post['score']}/10 · {fact}\n\n{post['text']}")
            else:
                self.say(f"Пробный {i + 1}/{n}: не прошёл фильтры и оценку, пропускаю.")

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
                    bot.say("Не задана тема (поле topic).")
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
