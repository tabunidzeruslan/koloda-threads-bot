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

WAR = re.compile(r"\b(обстріл|шахед|ракет|ппо\b|тривог|загибл|окупант|русн|орк(и|ів|ам|ами)?\b)", re.I)
SWEAR = re.compile(r"\b(бля|хуй|хуя|піздец|пизд|їбан|ебан|сука|нахуй)", re.I)
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
    if SWEAR.search(t):
        return "мат"
    if WAR.search(low):
        return "тема війни"
    if re.search(r"\b(ти|тобі|тебе|твій|твоя|твої)\b", low):
        return "звернення на «ти»"
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
- Не висміюй свою команду. Без політики. Про війну, обстріли і загиблих не жартуй ніколи. Без паніки про відключення.
- Звертайся до читача на «ви». Говориш від імені бренду («ми»).
- Без цін. Контакт (телефон/дірект) тільки в продаючому пості.
- Не представляйся («Привіт, я…»)."""


def rules() -> dict:
    try:
        return load("writing_rules.json")
    except (OSError, ValueError):
        return {}


def build_prompt(*, today: date, rubrics: list[str], sell: bool, recent: list[str], background: str = "",
                 topic: str = "") -> str:
    biz, facts, style = load("business.json"), load("facts.json"), load("style.json")
    voice = {k: v for k, v in load("voice.json").items() if k != "note"}
    fmt = voice["formats"][(today.toordinal() + len(recent)) % len(voice["formats"])]
    lines = [
        "Голос бренду (обовʼязково): " + json.dumps(voice, ensure_ascii=False),
        f"Формат для цього разу (хоча б 2 з 4 варіантів): {fmt}",
        f"Сьогодні {today.day} {MONTHS_UK[today.month - 1]} {today.year}, сезон: {season(today)}. Не пиши про інший сезон.",
        "Факти про бізнес (єдине джерело): " + json.dumps(biz, ensure_ascii=False),
        "Перевірені факти ніші: " + json.dumps(facts["facts"], ensure_ascii=False),
        "Приклади тону (не копіюй, лише стиль): " + json.dumps(style["examples"], ensure_ascii=False),
        "Недавні пости (не повторюй теми і жарти): " + json.dumps(recent[-12:], ensure_ascii=False),
    ]
    r = rules()
    if r:
        lines.append("Правила Threads-поста: " + json.dumps(r.get("threads_post", []), ensure_ascii=False))
        lines.append("Гачки (обери доречний): " + json.dumps(r.get("hooks", []), ensure_ascii=False))
        lines.append("ЛОГІКА (порушення = пост у смітник): " + json.dumps(r.get("logic", []), ensure_ascii=False))
    if background:
        lines.append("Контекст дня (можна використати, не обовʼязково): " + background)
    if topic:
        lines.append("ГОЛОВНА ТЕМА всіх 4 варіантів (інфопривід, реакція на хайп): " + topic +
                     "\nЗроби дотепну реакцію з ЛОГІЧНИМ містком до дров, тепла, дому чи чесного обʼєму: спільне почуття, "
                     "та сама ситуація або аналогія, що тримається. Не просто «сталося X, а в нас дрова». Рецепти: " +
                     json.dumps(rules().get("trend_bridges", []), ensure_ascii=False) + "\n"
                     "Пиши з позиції вболівальника/співчуття, без злорадства. Не вигадуй деталей події, "
                     "яких немає в описі. Не ображай людей з новини. Якщо тема про смерть, війну, трагедію, політику "
                     "або релігію, поверни {\"variants\": []}.")
    if sell:
        lines.append("Це ПРОДАЮЧИЙ пост: 1-3 речення до 260 символів, м'яко, з контактом "
                     f"({biz['contacts']['phone']} або «пишіть у дірект»). Приклад: {style['sell_example']}")
        lines.append("Напиши 4 різні варіанти продаючого поста з різними кутами (швидкість, чесний об'єм, сухі дрова, зручність).")
    elif not topic:
        lines.append("Напиши 4 варіанти, по одному на кожну рубрику: " + json.dumps(rubrics, ensure_ascii=False))
    lines.append('Відповідь тільки JSON: {"variants": [{"rubric": "...", "text": "..."}]}')
    return "\n".join(lines)


def generate_variants(gm: Gemini, *, today: date, rubrics: list[str], sell: bool, recent: list[str],
                      background: str = "", topic: str = "", images: list | None = None) -> list[dict]:
    raw = gm.generate(build_prompt(today=today, rubrics=rubrics, sell=sell, recent=recent, background=background,
                                   topic=topic), system=SYSTEM, json_mode=True, images=images)
    data = parse_json(raw)
    out = []
    for v in data.get("variants", []) if isinstance(data, dict) else []:
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            out.append({"rubric": SELL if sell else ("Хайп" if topic else str(v.get("rubric", ""))), "text": v["text"].strip()})
    return out


EDITOR = """Ти суворий редактор постів у Threads. Оціни кожен пост від 1 до 10 за критеріями:
гачок у першому рядку; зухвалість і сарказм без образ людей (образи, мат, жарти про війну чи обстріли = оцінка 1); хочеться відповісти або посперечатися; звучить як людина, а не маркетолог чи ШІ;
логічний: є одна ясна думка, зв'язок між темою і дровами має сенс, а не просто стоїть поруч (нелогічний = не вище 4); немає вигаданих фактів, цифр, випадків; не повторює недавні пости; короткий; відповідає сезону; без помилок української мови.
Відповідь тільки JSON: {"scores": [{"i": 0, "score": 8, "why": "коротко"}]}"""


def score(gm: Gemini, texts: list[str], today: date, recent: list[str]) -> list[tuple[int, str]]:
    checks = rules().get("editor_checks", [])
    logic = ("Перевірка логіки (якщо хоч одна відповідь погана, оцінка не вище 4): " +
             json.dumps(checks, ensure_ascii=False) + "\n") if checks else ""
    prompt = (logic +f"Дата: {today.isoformat()}, сезон: {season(today)}.\nНедавні пости: {json.dumps(recent[-8:], ensure_ascii=False)}\n"
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


PICKER = """Ти SMM-редактор зухвалого київського бренду дров Koloda в Threads. Із трендів пошуку України вибери ОДНУ тему,
на яку бренд може дотепно відреагувати (спорт, шоубіз, побут, погода, мемні новини), з містком до дров, тепла чи дому.
ПРІОРИТЕТ: українська і київська повістка (Динамо Київ, Шахтар, збірна України, українські зірки, шоу, блогери, київські події, погода, ціни на побутові речі). Світова тема (іноземні клуби, Голлівуд) тільки якщо вона явно про Україну або українців; інакше обирай українську тему або pick = -1.
НЕ можна: війна, мобілізація і ТЦК, обстріли, загиблі, трагедії, хвороби, політика, релігія, криміналь, скандали з дітьми.
Якщо нічого не підходить, pick = -1. Відповідь тільки JSON: {"pick": 0, "topic": "коротко суть події з заголовків", "why": "..."}"""


def pick_trend(gm: Gemini, trends: list[dict], used: list[str]) -> dict | None:
    items = [t for t in trends if t["title"] not in used][:20]
    if not items:
        return None
    listing = "\n".join(f"{i}: {t['title']} ({t['traffic']}) - " + " | ".join(n["title"] for n in t["news"][:2])
                        for i, t in enumerate(items))
    data = parse_json(gm.generate("Тренди:\n" + listing, system=PICKER, json_mode=True, temperature=0.3))
    try:
        i = int(data.get("pick", -1))
    except (TypeError, ValueError):
        return None
    if not 0 <= i < len(items):
        return None
    t = items[i]
    link = t["news"][0]["url"] if t["news"] else ""
    return {"title": t["title"], "topic": f"{t['title']}: {data.get('topic', '')}", "url": link}


def factcheck(gm: Gemini, text: str, topic: str = "") -> tuple[str, str, str | None]:
    """Returns (status, text, note). status: ok | fixed | bad | unchecked."""
    current = text
    given = (f"Інфопривід від власника акаунта (вважай правдою, не перевіряй): {topic[:400]}\n") if topic else ""
    search_ok = True
    for round_ in range(3):
        try:
            data = parse_json(gm.generate(f"{given}Пост:\n{current}", system=FACTCHECK if search_ok else FACTCHECK_NOSEARCH,
                                          search=search_ok, temperature=0.1))
        except Exception as e:  # search quota / unsupported model / bad JSON
            if search_ok:
                search_ok = False  # free tier often has no Google Search quota: check by model knowledge
                try:
                    data = parse_json(gm.generate(f"{given}Пост:\n{current}", system=FACTCHECK_NOSEARCH, temperature=0.1))
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
               rnd: random.Random | None = None, log=print, background: str = "", topic: str = "",
               images: list | None = None) -> dict | None:
    rnd = rnd or random.Random()
    examples = load("style.json")["examples"]
    rubrics = [] if sell or topic else pick_rubrics(load("rubrics.json"), today, last_rubrics, 4, rnd)
    variants = generate_variants(gm, today=today, rubrics=rubrics, sell=sell, recent=recent, background=background,
                                 topic=topic, images=images)
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
        status, text, note = factcheck(gm, v["text"], topic=topic)
        if status == "bad":
            log(f"  факты: {note}")
            continue
        if text != v["text"] and check(text, sell=sell, today=today, recent=recent, examples=examples):
            continue
        return {"text": text, "rubric": v["rubric"], "score": sc, "why": why, "fact": status, "fact_note": note}
    return None


ILLUSTRATOR = """You design one illustration for a Threads post of a Kyiv firewood brand. Invent ONE fresh, funny visual
scene for the post's main idea, in which the character Ivanovych performs the core action (not decoration).
If firewood, pellets or briquettes appear, describe them exactly as the firewood guide says (shape, bark, end faces, stacking).
If the post is about a trending topic, make the scene recognisable through generic props as the trend rule says.
Return only JSON: {"prompt": "1-2 English sentences describing only the scene and action, positive wording, no lists of
what to avoid, no style words", "idea": "one line in Ukrainian"}"""


WOOD_WORDS = re.compile(r"firewood|log|wood|timber|stack|fireplace|stove|дров|полін", re.I)


def render_prompt(scene: str, il: dict | None = None) -> str:
    """Prompt for the image model: the scene plus a short positive-only description.
    Negative lists stay out: Cloudflare's safety filter flags words like weapons or children."""
    il = il or load("illustration.json")
    wood = f"\n{il['firewood_render']}" if il.get("firewood_render") and WOOD_WORDS.search(scene) else ""
    return f"{scene.strip()}{wood}\n{il['render']}"


def image_prompt(gm: Gemini, post_text: str, topic: str = "") -> dict:
    il = load("illustration.json")
    rules = (f"Character: {il['character']}\nStyle: {il['style']}\nText: {il['text_rule']}\nBanned: {il['banned']}\n"
             f"Firewood guide: {il.get('firewood', '')}\nTrend rule: {il.get('trend_rule', '')}")
    trend = f"\n\nTrending topic behind the post: {topic}" if topic else ""
    data = parse_json(gm.generate(f"{rules}{trend}\n\nPost:\n{post_text}", system=ILLUSTRATOR, json_mode=True, temperature=0.9))
    prompt = str(data.get("prompt", "")).strip()
    if not prompt:
        raise ValueError("пустий промпт")
    full = render_prompt(prompt, il)
    return {"prompt": full, "idea": str(data.get("idea", ""))[:200]}


IMAGE_JUDGE = """Ти арт-директор Threads-акаунта. Більшість постів у Threads краще працюють БЕЗ картинки: текст сам по собі.
Картинка потрібна лише коли вона підсилює жарт або ідею: є яскрава візуальна метафора, абсурдна сцена,
порівняння «до/після», предмет, який смішно намалювати. Для питань, суперечок, зізнань і коротких думок картинка не потрібна.
Відповідь тільки JSON: {"image": true/false, "why": "коротко"}"""


def needs_image(gm: Gemini, text: str, trend: bool = False) -> tuple[bool, str]:
    hint = ("Це пост на гарячу тему дня: тут картинка з Івановичем, що реагує на тему, часто підсилює впізнаваність.\n"
            if trend else "")
    data = parse_json(gm.generate(f"{hint}Пост:\n{text}", system=IMAGE_JUDGE, json_mode=True, temperature=0.2))
    return bool(data.get("image")), str(data.get("why", ""))[:150]
