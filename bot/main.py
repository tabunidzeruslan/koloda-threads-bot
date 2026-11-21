"""One run of the bot (GitHub Actions calls it every 30 minutes)."""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time
from datetime import date, datetime

from . import crypto, net, state as st, writer
from .gemini import Gemini
from .schedule import (LAST_PUBLISH_MIN, SPACING_MIN, hhmm, make_slots, minutes_of, now_kyiv, since_minutes)
from .telegram import Telegram
from .threads import Threads

STATUS_RU = {"new": "ждёт черновика", "sent": "ждёт вашего решения", "approved": "одобрен",
             "publishing": "публикуется", "published": "опубликован", "failed": "ошибка",
             "skipped": "пропущен", "expired": "не успел"}
HELP = ("Команды:\n/status - что запланировано\n/on - включить ежедневные черновики (3 в день)\n"
        "/off - выключить\n/preview 3 - показать N пробных постов (ничего не публикуется)\n"
        "/selftest - проверить ключи и разрешения\n\n"
        "Под черновиком кнопки: Опубликовать / Другой вариант / Пропустить. "
        "Чтобы поправить текст, ответьте (Reply) на черновик своим текстом.\n"
        "Бот просыпается раз в 30 минут, поэтому ответ приходит с задержкой.")
MAX_GEN_PER_RUN = 2
TIME_BUDGET = 6 * 60


def log(*a):
    print(net.scrub(" ".join(str(x) for x in a)), flush=True)


class Bot:
    def __init__(self, s: dict, *, tg: Telegram | None, gm: Gemini | None, threads_secret: str,
                 enc_key: str, chat_id_env: str = "", persist=lambda: None, now: datetime | None = None,
                 rnd: random.Random | None = None):
        self.s, self.tg, self.gm = s, tg, gm
        self.threads_secret, self.enc_key = threads_secret, enc_key
        self.persist = persist
        self.now = now or now_kyiv()
        self.rnd = rnd or random.Random()
        self.started = time.monotonic()
        self.summary: list[str] = []
        if chat_id_env.strip().lstrip("-").isdigit():
            self.s["owner_chat_id"] = int(chat_id_env)
        self.th = Threads(self.threads_token()) if threads_secret else None

    # ---------- helpers
    @property
    def owner(self) -> int | None:
        return self.s.get("owner_chat_id")

    def say(self, text: str, buttons=None) -> int | None:
        self.summary.append(text)
        if self.tg and self.owner:
            try:
                return self.tg.send(self.owner, text, buttons)
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
    def handle_updates(self) -> None:
        if not self.tg:
            return
        try:
            ups = self.tg.updates(self.s.get("tg_offset"))
        except net.NetError as e:
            log("getUpdates failed:", e)
            return
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

    def on_message(self, m: dict) -> None:
        chat = m.get("chat", {})
        text = (m.get("text") or "").strip()
        if chat.get("type") != "private":
            return
        if not self.owner:
            if text.startswith("/start"):
                self.s["owner_chat_id"] = chat["id"]
                self.say("Готово, теперь бот пишет вам.\n\n" + HELP)
            return
        if chat["id"] != self.owner:
            return
        if re.fullmatch(r"[A-Za-z0-9_\-:.]{60,}", text):
            self.tg.delete(chat["id"], m["message_id"])
            self.say("Похоже на ключ или токен. Я удалил сообщение: секреты вводятся только в GitHub → Settings → Secrets.")
            return
        reply_to = (m.get("reply_to_message") or {}).get("message_id")
        if reply_to and not text.startswith("/"):
            return self.on_edit(reply_to, text)
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help"):
            self.say(HELP)
        elif cmd == "/status":
            self.say(self.status_text())
        elif cmd == "/on":
            self.s["schedule_on"], self.s["paused_reason"], self.s["fail_streak"] = True, None, 0
            self.say("Включено: каждый день 3 черновика, публикация только после вашей кнопки.")
        elif cmd == "/off":
            self.s["schedule_on"] = False
            self.say("Выключено. Новые черновики не готовлю, одобренные тоже не публикую.")
        elif cmd == "/preview":
            n = int(arg) if arg.strip().isdigit() else 3
            self.preview(max(1, min(n, 5)))
        elif cmd == "/selftest":
            self.say(self.selftest())
        else:
            self.say("Не понял. " + HELP)

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

    def on_callback(self, cq: dict) -> None:
        if cq.get("from", {}).get("id") != self.owner and cq.get("message", {}).get("chat", {}).get("id") != self.owner:
            return
        action, _, rest = (cq.get("data") or "").partition("|")
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
        elif action == "sk":
            sl["status"] = "skipped"
            self.tg.answer(cq["id"], "Пропускаю")
            self.tg.edit(self.owner, msg_id, f"⏭ Пропущено\n\n{text}")

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
        for day in sorted(self.s["days"])[:-3]:
            del self.s["days"][day]

    def plan_today(self) -> None:
        key = self.today_key()
        if key in self.s["days"] or not self.s["schedule_on"] or self.now.hour < 6:
            return
        slots = []
        for i, at in enumerate(make_slots(self.rnd)):
            if at < minutes_of(self.now) - 30:
                continue  # window already passed (bot switched on mid-day)
            self.s["planned_total"] += 1
            kind = "sell" if self.s["planned_total"] % 5 == 0 else "hook"
            slots.append({"id": f"{key[5:7]}{key[8:]}-{i}", "at": at, "kind": kind, "status": "new",
                          "tries": 0, "rejects": 0, "v": 0, "draft": None, "msg_id": None})
        self.s["days"][key] = {"slots": slots}

    def time_left(self) -> bool:
        return time.monotonic() - self.started < TIME_BUDGET

    def draft_pending(self) -> None:
        if not self.s["schedule_on"] or self.s.get("paused_reason") or not self.gm:
            return
        made = 0
        for sl in self.slots():
            if sl["status"] != "new" or made >= MAX_GEN_PER_RUN or not self.time_left():
                continue
            if sl.get("draft") and sl["draft"].get("fact") == "owner":
                self.send_draft(sl)
                continue
            made += 1
            sl["tries"] += 1
            post = self.write(sl["kind"] == "sell")
            if post:
                sl["draft"] = post
                self.send_draft(sl)
            elif sl["tries"] >= 5:
                sl["status"] = "skipped"
                self.say(f"⚠️ Не смог написать хороший пост на {hhmm(sl['at'])} за 5 попыток, слот пропущен.")
            self.persist()

    def write(self, sell: bool) -> dict | None:
        try:
            post = writer.write_post(self.gm, today=self.now.date(), sell=sell, recent=self.recent_texts(),
                                     last_rubrics=self.last_rubrics(), rnd=self.rnd, log=log)
        except Exception as e:
            log("writer failed:", e)
            return None
        finally:
            if self.gm and self.gm.model:
                self.s["gemini_model"] = self.gm.model
        return post

    def send_draft(self, sl: dict, note: str | None = None) -> None:
        d = sl["draft"]
        sl["v"] = sl.get("v", 0) + 1
        fact = {"ok": "факты ✅", "fixed": "факты исправлены ✅", "unchecked": "факты НЕ проверены ⚠️",
                "owner": "ваш текст"}.get(d.get("fact"), "")
        head = note or (f"📝 Черновик на {hhmm(sl['at'])}" + (" · продающий" if sl["kind"] == "sell" else "")
                        + f" · {d.get('rubric', '')} · оценка {d.get('score')}/10 · {fact}")
        if d.get("fact_note") and d.get("fact") != "ok":
            head += f"\n({d['fact_note']})"
        buttons = [[("✅ Опубликовать", f"ok|{sl['id']}|{sl['v']}")],
                   [("🔁 Другой вариант", f"re|{sl['id']}|{sl['v']}"), ("⏭ Пропустить", f"sk|{sl['id']}|{sl['v']}")]]
        msg_id = self.say(f"{head}\n\n{d['text']}", buttons)
        sl["msg_id"] = msg_id
        sl["status"] = "sent"

    # ---------- publishing
    def publish_due(self) -> None:
        if not self.th or not self.s["schedule_on"] or self.s.get("paused_reason"):
            return
        now_min = minutes_of(self.now)
        if now_min > LAST_PUBLISH_MIN:
            return
        last = self.s["posts"][-1]["at"] if self.s["posts"] else None
        if since_minutes(last, self.now) < SPACING_MIN:
            return
        if since_minutes(self.s.get("last_fail"), self.now) < 15:
            return
        for sl in self.slots():
            if sl["status"] == "approved" and now_min >= sl["at"]:
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

    # ---------- owner tools
    def status_text(self) -> str:
        lines = [f"Ежедневные черновики: {'включены' if self.s['schedule_on'] else 'выключены (/on)'}"]
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
                    self.s["gemini_model"] = self.gm.model
        else:
            out.append("❌ Нет секрета GEMINI_API_KEY")
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
                fact = {"ok": "факты ✅", "fixed": "факты исправлены ✅", "unchecked": "факты НЕ проверены ⚠️"}[post["fact"]]
                self.say(f"Пробный {i + 1}/{n}{' · продающий' if sell else ''} · {post['rubric']} · оценка {post['score']}/10 · {fact}\n\n{post['text']}")
            else:
                self.say(f"Пробный {i + 1}/{n}: не прошёл фильтры и оценку, пропускаю.")

    # ---------- one run
    def tick(self) -> None:
        self.handle_updates()
        self.persist()
        self.roll_days()
        self.plan_today()
        self.publish_due()
        self.draft_pending()
        self.publish_due()
        self.maybe_refresh_token()
        self.s["last_run"] = self.now.isoformat()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default="tick", choices=["tick", "selftest", "preview"])
    ap.add_argument("--state", default=".state/state.json")
    ap.add_argument("--n", type=int, default=3)
    a = ap.parse_args(argv)

    tg_token = net.env("TELEGRAM_BOT_TOKEN")
    th_token = net.env("THREADS_TOKEN")
    gm_key = net.env("GEMINI_API_KEY")
    enc_key = net.env("STATE_KEY") or tg_token or "no-key"
    s = st.load(a.state)
    bot = Bot(s, tg=Telegram(tg_token) if tg_token else None,
              gm=Gemini(gm_key, s.get("gemini_model")) if gm_key else None,
              threads_secret=th_token, enc_key=enc_key, chat_id_env=net.env("TELEGRAM_CHAT_ID"),
              persist=lambda: st.save(a.state, s))
    try:
        if a.task == "tick":
            bot.tick()
        else:
            bot.handle_updates()
            if a.task == "selftest":
                bot.say(bot.selftest())
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
