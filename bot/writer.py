"""Post pipeline: 4 variants -> hard filters -> editor score -> fact check."""
from __future__ import annotations

import difflib
import json
import random
import re
from datetime import date
from pathlib import Path

from .gemini import Gemini, parse_json

DATA = Path(__file__).resolve().parent.parent / "data"
SELL = "Продаючий"
MAX_LEN, MAX_SELL_LEN, MIN_LEN = 220, 260, 60
MIN_SCORE = 7

BANNED = ["варто зазначити", "важливо памʼятати", "важливо пам'ятати", "у підсумку", "отже,", "давайте розберемося",
          "давайте поговоримо", "в цій статті", "друзі,", "привіт, я", "уявіть собі світ", "незамінний помічник",
          "ключовий фактор", "шановні"]
RUSSIAN_LETTERS = re.compile(r"[ыэъёЫЭЪЁ]")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
PHONE = re.compile(r"(\+?38)?\s?\(?0\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")
PRICE = re.compile(r"\d[\d\s]*\s?(грн|₴|гривень|uah)", re.I)
LIST_LINE = re.compile(r"^\s*([-•*–]|\d+[.)])\s", re.M)
WARM_WORDS = ["спек", "пляж", "засмаг", "кондиціонер", "морозиво"]
COLD_WORDS = ["мороз", "снігопад", "заметіл", "опалювальний сезон", "кожух"]


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def season(d: date) -> str:
    return {12: "зима", 1: "зима", 2: "зима", 3: "весна", 4: "весна", 5: "весна",
            6: "літо", 7: "літо", 8: "літо"}.get(d.month, "осінь")


MONTHS_UK = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня",
             "жовтня", "листопада", "грудня"]


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def check(text: str, *, sell: bool, today: date, recent: list[str], examples: list[str]) -> str | None:
    """Return a reason to reject, or None if the text passes the hard filters."""
    t = text.strip()
    limit = MAX_SELL_LEN if sell else MAX_LEN
    if len(t) > limit:
        return f"довше {limit} символів ({len(t)})"
    if len(t) < MIN_LEN:
        return "закороткий"
    if "#" in t:
        return "хештег"
    if EMOJI.search(t):
        return "емодзі"
    if LIST_LINE.search(t):
        return "список"
    if RUSSIAN_LETTERS.search(t):
        return "російські літери"
    low = t.lower()
    for w in BANNED:
        if w in low:
            return f"штамп «{w}»"
    if PRICE.search(t):
        return "ціна"
    if not sell and (PHONE.search(t) or "koloda.ua" in low):
        return "контакт у непродаючому пості"
    if today.month in (10, 11, 12, 1, 2, 3) and any(w in low for w in WARM_WORDS):
        return "не той сезон (літо)"
    if today.month in (5, 6, 7, 8) and any(w in low for w in COLD_WORDS):
        return "не той сезон (зима)"
    for other in recent + examples:
        if other and similar(t, other) > 0.6:
            return "схожий на недавній пост або приклад"
    return None


def pick_rubrics(rubrics: list[dict], today: date, last: list[str], n: int, rnd: random.Random) -> list[str]:
    pool = [r for r in rubrics if not r["months"] or today.month in r["months"]]
    if len(last) >= 1:
        pool = [r for r in pool if r["name"] != last[-1]] or pool
    chosen: list[str] = []
    while pool and len(chosen) < n:
        total = sum(max(r["weight"], 0.3) for r in pool)
        x = rnd.uniform(0, total)
        for r in pool:
            x -= max(r["weight"], 0.3)
            if x <= 0:
                chosen.append(r["name"])
                pool = [p for p in pool if p is not r]
                break
    return chosen


SYSTEM = """Ти пишеш пости для Threads від імені київського продавця дров Koloda. Пиши українською, як жива людина з дров'яного бізнесу, а не як маркетолог чи ШІ.
Правила:
- 2-6 коротких рядків, 100-220 символів разом. Перший рядок - гачок до 60 символів: впізнаваний біль, абсурд або несподіваний факт.
- Мета: людина думає «та ну!», «у мене теж!» або «не згоден» і пише коментар.
- Пост не мусить бути про товар. Побут приватного будинку, зима, сусіди, звички - теж підходять.
- Кінцівки різні: інколи питання, інколи висновок чи спостереження.
- Без емодзі, хештегів, списків, мату, канцеляриту і штампів ШІ.
- НЕ вигадуй клієнтів, замовлення, випадки, відгуки, цифри. Гіпотетичне подавай як гіпотетичне («уявіть…»). Не пиши «у нас на днях був заказ».
- Технічні твердження (вологість, тепло, породи, об'єм) лише з бази фактів або загальновідомі.
- Не висміюй свою команду. Без політики і війни. Без паніки про відключення.
- Без цін. Контакт (телефон/дірект) тільки в продаючому пості.
- Не представляйся («Привіт, я…»)."""


def build_prompt(*, today: date, rubrics: list[str], sell: bool, recent: list[str]) -> str:
    biz, facts, style = load("business.json"), load("facts.json"), load("style.json")
    lines = [
        f"Сьогодні {today.day} {MONTHS_UK[today.month - 1]} {today.year}, сезон: {season(today)}. Не пиши про інший сезон.",
        "Факти про бізнес (єдине джерело): " + json.dumps(biz, ensure_ascii=False),
        "Перевірені факти ніші: " + json.dumps(facts["facts"], ensure_ascii=False),
        "Приклади тону (не копіюй, лише стиль): " + json.dumps(style["examples"], ensure_ascii=False),
        "Недавні пости (не повторюй теми і жарти): " + json.dumps(recent[-12:], ensure_ascii=False),
    ]
    if sell:
        lines.append("Це ПРОДАЮЧИЙ пост: 1-3 речення до 260 символів, м'яко, з контактом "
                     f"({biz['contacts']['phone']} або «пишіть у дірект»). Приклад: {style['sell_example']}")
        lines.append("Напиши 4 різні варіанти продаючого поста з різними кутами (швидкість, чесний об'єм, сухі дрова, зручність).")
    else:
        lines.append("Напиши 4 варіанти, по одному на кожну рубрику: " + json.dumps(rubrics, ensure_ascii=False))
    lines.append('Відповідь тільки JSON: {"variants": [{"rubric": "...", "text": "..."}]}')
    return "\n".join(lines)


def generate_variants(gm: Gemini, *, today: date, rubrics: list[str], sell: bool, recent: list[str]) -> list[dict]:
    raw = gm.generate(build_prompt(today=today, rubrics=rubrics, sell=sell, recent=recent), system=SYSTEM, json_mode=True)
    data = parse_json(raw)
    out = []
    for v in data.get("variants", []) if isinstance(data, dict) else []:
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            out.append({"rubric": SELL if sell else str(v.get("rubric", "")), "text": v["text"].strip()})
    return out


EDITOR = """Ти суворий редактор постів у Threads. Оціни кожен пост від 1 до 10 за критеріями:
гачок у першому рядку; хочеться відповісти або посперечатися; звучить як людина, а не маркетолог чи ШІ;
немає вигаданих фактів, цифр, випадків; не повторює недавні пости; короткий; відповідає сезону; без помилок української мови.
Відповідь тільки JSON: {"scores": [{"i": 0, "score": 8, "why": "коротко"}]}"""


def score(gm: Gemini, texts: list[str], today: date, recent: list[str]) -> list[tuple[int, str]]:
    prompt = (f"Дата: {today.isoformat()}, сезон: {season(today)}.\nНедавні пости: {json.dumps(recent[-8:], ensure_ascii=False)}\n"
              "Пости:\n" + "\n".join(f"{i}: {t}" for i, t in enumerate(texts)))
    data = parse_json(gm.generate(prompt, system=EDITOR, json_mode=True, temperature=0.2))
    res = [(0, "")] * len(texts)
    for s in data.get("scores", []):
        try:
            i = int(s["i"])
            if 0 <= i < len(texts):
                res[i] = (int(s["score"]), str(s.get("why", ""))[:200])
        except (KeyError, ValueError, TypeError):
            continue
    return res


FACTCHECK = """Ти фактчекер. Знайди в пості КОЖНЕ фактичне твердження (про деревину, вологість, тепловіддачу, об'єм, закони, ціни, події, статистику) і перевір його пошуком Google.
Жарти, емоції, питання і явно гіпотетичне («уявіть») не є фактами.
Якщо є помилка, виправ ТІЛЬКИ помилкове місце, решту залиш слово в слово.
Відповідь тільки JSON без markdown: {"ok": true/false, "problems": ["..."], "fixed_text": "..."}"""


FACTCHECK_NOSEARCH = FACTCHECK.replace("і перевір його пошуком Google", "і перевір його за своїми знаннями. Якщо не впевнений у твердженні, вважай його помилкою і прибери або зроби обережнішим")


def factcheck(gm: Gemini, text: str) -> tuple[str, str, str | None]:
    """Returns (status, text, note). status: ok | fixed | bad | unchecked."""
    current = text
    search_ok = True
    for round_ in range(3):
        try:
            data = parse_json(gm.generate(f"Пост:\n{current}", system=FACTCHECK if search_ok else FACTCHECK_NOSEARCH,
                                          search=search_ok, temperature=0.1))
        except Exception as e:  # search quota / unsupported model / bad JSON
            if search_ok:
                search_ok = False  # free tier often has no Google Search quota: check by model knowledge
                try:
                    data = parse_json(gm.generate(f"Пост:\n{current}", system=FACTCHECK_NOSEARCH, temperature=0.1))
                except Exception as e2:
                    return "unchecked", current, f"перевірка фактів не вдалася: {str(e2)[:120]}"
            else:
                return "unchecked", current, f"перевірка фактів не вдалася: {str(e)[:120]}"
        if data.get("ok") and not search_ok:
            return "nosearch", current, "перевірено без пошуку Google (немає квоти)"
        if data.get("ok"):
            return ("ok" if round_ == 0 else "fixed"), current, None
        fixed = str(data.get("fixed_text") or "").strip()
        problems = "; ".join(map(str, data.get("problems", [])))[:300]
        if not fixed or fixed == current:
            return "bad", current, problems
        current = fixed
    return "bad", current, "не вдалося виправити за 3 кола"


def write_post(gm: Gemini, *, today: date, sell: bool, recent: list[str], last_rubrics: list[str],
               rnd: random.Random | None = None, log=print) -> dict | None:
    rnd = rnd or random.Random()
    examples = load("style.json")["examples"]
    rubrics = [] if sell else pick_rubrics(load("rubrics.json"), today, last_rubrics, 4, rnd)
    variants = generate_variants(gm, today=today, rubrics=rubrics, sell=sell, recent=recent)
    passed = []
    for v in variants:
        why = check(v["text"], sell=sell, today=today, recent=recent, examples=examples)
        if why:
            log(f"  фильтр: {why}")
        else:
            passed.append(v)
    if not passed:
        return None
    scores = score(gm, [v["text"] for v in passed], today, recent)
    ranked = sorted(zip(passed, scores), key=lambda x: -x[1][0])
    for v, (sc, why) in ranked:
        if sc < MIN_SCORE:
            log(f"  оценка {sc} < {MIN_SCORE}")
            break
        status, text, note = factcheck(gm, v["text"])
        if status == "bad":
            log(f"  факты: {note}")
            continue
        if text != v["text"] and check(text, sell=sell, today=today, recent=recent, examples=examples):
            continue
        return {"text": text, "rubric": v["rubric"], "score": sc, "why": why, "fact": status, "fact_note": note}
    return None
