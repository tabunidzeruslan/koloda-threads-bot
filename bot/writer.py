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
          "ключовий фактор", "шановні", "стабільне тепло", "не підводить", "найкраще — просто", "найкраще - просто",
          "затишок і тепло", "тепло і затишок", "атмосфера затишку", "подаруйте собі", "справжнє задоволення",
          "якісні дрова за доступною", "ідеальний вибір"]
RUSSIAN_LETTERS = re.compile(r"[ыэъёЫЭЪЁ]")
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿️]")
PHONE = re.compile(r"(\+?38)?\s?\(?0\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}")
PRICE = re.compile(r"\d[\d\s]*\s?(грн|₴|гривень|uah)", re.I)
LIST_LINE = re.compile(r"^\s*([-•*–]|\d+[.)])\s", re.M)
WARM_WORDS = ["спек", "пляж", "засмаг", "кондиціонер", "морозиво"]
COLD_WORDS = ["мороз", "снігопад", "заметіл", "опалювальний сезон", "кожух"]


FAR_PLACES = re.compile(r"житомир|чернігів|чернігов|черкас|вінниц|полтав|львів|одес|харків|дніпропетров|запоріж|"
                        r"\bрівн(е|ому|ого|ен)\b|\bсум(и|ах|ам)\b|хмельниц|кропивниц|миколаїв|херсон|луцьк|тернопіл|ужгород|"
                        r"франківськ|чернівц|вся київська область|всій київській області|всю київську область")
_NIGHT = r"(вночі|опівночі|о\s*[2345]\s*(ранку|ночі)|о (п'ятій|пʼятій|четвертій|третій) ранку|на світанку)"
_WORK = r"(виїжд|виїзд|доставл|доставк|привез|привоз|везем|працюєм|вантаж)"
NIGHT_WORK = re.compile(_WORK + r"\w*[^.?!]{0,40}" + _NIGHT + "|" + _NIGHT + r"[^.?!]{0,40}" + _WORK)
GOAL_TALK = re.compile(r"10\s?000|10\s?тис|десять тисяч|\d+\s*підписник")


def first_word(text: str) -> str:
    m = re.match(r"\W*([\w'’ʼ-]+)", (text or "").strip().lower())
    return m.group(1) if m else ""


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def season(d: date) -> str:
    return {12: "зима", 1: "зима", 2: "зима", 3: "весна", 4: "весна", 5: "весна",
            6: "літо", 7: "літо", 8: "літо"}.get(d.month, "осінь")


MONTHS_UK = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня",
             "жовтня", "листопада", "грудня"]


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


RUSSIAN_WORDS = re.compile(r"\b(как|если|только|очень|сейчас|когда|почему|сколько|нужно|можно|хорошо|спасибо|сегодня|тоже|"
                           r"всегда|никогда|потому|чтобы|ничего|говорят|дрова есть|купить|продам|недорого|привет|пожалуйста)\b")


OUR_CLAIMS = re.compile(r"камерн|сушильн[аі] камер|\d+\s*%\s*вологост|вологіст\w*\s*(до|не більше|менше)?\s*\d+\s*%")


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
    if RUSSIAN_WORDS.search(t.lower()):
        return "російські слова (у постах тільки українська)"
    low = t.lower()
    if OUR_CLAIMS.search(low):
        return "обіцянка про наші дрова, якої немає (камерна сушка або відсоток вологості)"
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
    if FAR_PLACES.search(low):
        return "місцевість далі 100 км від Києва"
    if NIGHT_WORK.search(low):
        return "нічна робота (комендантська година)"
    if GOAL_TALK.search(low):
        return "внутрішня мета (підписники) у пості"
    if not sell and (PHONE.search(t) or "koloda.ua" in low):
        return "контакт у непродаючому пості"
    if today.month in (10, 11, 12, 1, 2, 3) and any(w in low for w in WARM_WORDS):
        return "не той сезон (літо)"
    if today.month in (5, 6, 7, 8) and any(w in low for w in COLD_WORDS):
        return "не той сезон (зима)"
    opener = first_word(t)
    if len(opener) >= 4 and any(first_word(o) == opener for o in recent[-15:] if o):
        return f"той самий початок, що в недавньому пості («{opener}»)"
    for other in recent + examples:
        if other and similar(t, other) > 0.6:
            return "схожий на недавній пост або приклад"
    return None


def pick_rubrics(rubrics: list[dict], today: date, last: list[str], n: int, rnd: random.Random) -> list[str]:
    pool = [dict(r, weight=r["weight"] * float(LEARNED["boost"].get(r["name"], 1.0)))
            for r in rubrics if not r["months"] or today.month in r["months"]]
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


SYSTEM = """ГОЛОВНЕ: кожен пост або реально корисний (конкретна порада, яку людина застосує сьогодні), або реально смішний,
а найкраще і те, і інше. Пластмасові рекламні фрази («стабільне тепло», «не підводить», «затишок») = провал.
Мета акаунта: продавати дрова і отримувати заявки, тому люди мають запамʼятати, що Koloda = дрова природної сушки, чесний обʼєм,
доставка по Києву і області сьогодні.
Ти пишеш пости для Threads від імені київського продавця дров Koloda. Пиши ТІЛЬКИ українською (російська мова в постах і відповідях заборонена повністю, навіть окремі слова і цитати), як жива людина з дров'яного бізнесу, а не як маркетолог чи ШІ.
Правила:
- 2-6 коротких рядків, 100-220 символів разом. Перший рядок - гачок до 60 символів: впізнаваний біль, абсурд або несподіваний факт.
- Мета: людина думає «та ну!», «у мене теж!» або «не згоден» і пише коментар.
- Пост не мусить бути про товар. Побут приватного будинку, зима, сусіди, звички - теж підходять.
- Кінцівки різні: інколи питання, інколи висновок чи спостереження.
- Емодзі майже ніколи (максимум 1 і не в кожному пості). Замість смайлика пиши «)» або «))», як живі люди в чатах.
- НІКОЛИ не став довге тире «—» чи «–»: воно видає ШІ. Замість нього кома, крапка, двокрапка або дефіс «-».
- Без хештегів, списків, мату, канцеляриту і штампів ШІ.
- Часто питай людей: думка + питання, вибір А/Б, «а у вас як?». Виводь на емоцію (впізнавання, ностальгія, легке
  обурення, азарт суперечки, гордість), щоб хотілося відповісти. Не «напишіть у коментарях» (це приманка), а справжнє питання.
- Про розвантаження не пиши нічого. Сильні факти для продажу: офіційні дрова з документами про походження, 15 років на ринку, будь-який обʼєм без мінімалки, поліна під ваш розмір, сьогодні або в зручний час.
- Наші дрова ПРИРОДНОЇ сушки. Заборонено писати, що вони «сухі», «ідеально сухі», «камерної сушки» чи називати відсоток вологості. Кажи «природної сушки» і що підкажемо по породі телефоном. Ціни в постах і відповідях не називаємо ніколи, лише в дірект або телефоном.
- Дрова головний продукт. Пелети, брикети, кора другорядні, згадуй рідко. Оптом продаємо ще кругляк (його немає на сайті). Є Koloda-сажотрус: кладеш у розігрітий камін чи котел після години горіння, він прогорає, сажа і смола стають попелом; відправляємо по всій Україні. Згадуй його зрідка, у темах про димар, чистку, безпеку печі.
- Гумор гострий, сучасний український, як у живому Threads: самоіронія, впізнаваний побут, несподіваний поворот, дотепна
  деталь. НЕ плоский «англійський» гумор: каламбури заради каламбуру, «тато-жарти», ввічливі дотепи, які ніхто не перешле.
- НЕ вигадуй клієнтів, замовлення, випадки, відгуки, цифри. Гіпотетичне подавай як гіпотетичне («уявіть…»). Не пиши «у нас на днях був заказ».
- Технічні твердження (вологість, тепло, породи, об'єм) лише з бази фактів або загальновідомі.
- Не висміюй свою команду. Без політики. Про війну, обстріли і загиблих не жартуй ніколи. Без паніки про відключення.
- Звертайся до читача на «ви». Говориш від імені бренду («ми»).
- Без цін. Контакт (телефон/дірект) тільки в продаючому пості.
- Не представляйся («Привіт, я…»).
- Місцевість: Київ і передмістя до ~100 км (Бровари, Бориспіль, Ірпінь, Буча, Гостомель, Вишгород, Обухів, Васильків,
  Фастів, Біла Церква, Макарів). Не пиши про інші області й міста (Житомир, Чернігів, Черкаси тощо) і про «всю область».
- В Україні комендантська година (з 00:00 до 05:00): не пиши про нічні виїзди, доставку вночі чи на світанку о 4-5.
- Кожне речення має бути зрозумілим і логічним, як сказала б жива людина. Якщо думка заплутана, напиши простіше.
  Перечитай: чи зрозуміє незнайомець, про що пост, з першого разу? Набір слів = провал."""


OWNER: dict = {"rules": [], "facts": []}  # set by the bot from what the owner said in the chat


def set_owner(rules_: list[str], facts_: list[str]) -> None:
    OWNER["rules"], OWNER["facts"] = list(rules_ or []), list(facts_ or [])


LEARNED: dict = {"learnings": [], "examples": [], "boost": {}}


def set_learned(learnings: list[str], examples: list[str], boost: dict) -> None:
    LEARNED["learnings"], LEARNED["examples"], LEARNED["boost"] = list(learnings or []), list(examples or []), dict(boost or {})


ANALYST = """Ти аналітик Threads-акаунта київського продавця дров Koloda. Методика (адаптовано з blacktwist/social-media-skills,
performance-analyzer і content-pattern-analyzer, MIT): порівнюй не сирі цифри, а залученість відносно переглядів;
відповіді і репости важать більше за лайки. Знайди найкращі і найгірші пости і поясни ЧОМУ: тема, рубрика, гачок у першому рядку,
формат (питання, суперечка, порада, гумор, продаж, тренд), довжина, час, картинка, тег. Шукай повторювані закономірності,
а не випадковості; при малій кількості постів формулюй обережно («схоже, що…»).
Аналіз щоденний: враховуй свої минулі висновки, підтверджуй або змінюй їх за новими цифрами.
Головна мета акаунта: зростання до 10 000 підписників у Threads, тому найважливіше те, що приводить нових людей
(відповіді, репости, суперечки, трендові пости), а не лайки своїх. Мета внутрішня: НІКОЛИ не пропонуй згадувати її в постах.
Поверни конкретні правила для автора на найближчі дні (до 8), кожне спирається на цифри з даних.
Також оціни рубрики: множник 0.5-2.0 (більше 1 = писати частіше), тільки для рубрик, що є в даних.
Відповідь тільки JSON: {"learnings": ["правило з поясненням"], "rubric_boost": {"рубрика": 1.3}, "best": [індекси найкращих постів, до 3],
"summary": "2-3 речення українською: що працює, що ні"}"""


def analyze_posts(gm: Gemini, posts: list[dict], context: str = "") -> dict:
    rows = []
    for i, p in enumerate(posts):
        m = p.get("metrics") or {}
        rows.append({"i": i, "text": (p.get("text") or "")[:300], "rubric": p.get("rubric"), "slot": p.get("slot"),
                     "hour": p.get("hour"), "weekday": p.get("weekday"), "image": p.get("image"), "tag": p.get("tag"),
                     "question": "?" in (p.get("text") or ""), "len": len(p.get("text") or ""), **m,
                     "score": engagement_score(m)})
    data = parse_json(gm.generate((context + "\n" if context else "") + "Пости з метриками:\n"
                                  + json.dumps(rows, ensure_ascii=False), system=ANALYST,
                                  json_mode=True, temperature=0.2))
    return data if isinstance(data, dict) else {}


def engagement_score(m: dict) -> float:
    """Replies and reposts matter most for Threads recommendations; per 100 views so big and small posts compare."""
    pts = 3 * (m.get("replies") or 0) + 4 * ((m.get("reposts") or 0) + (m.get("quotes") or 0)) + (m.get("likes") or 0)
    return round(100 * pts / max(m.get("views") or 0, 30), 2)


def knowledge_sample(rnd: random.Random | None = None, n: int = 12) -> list[str]:
    """A rotating handful of sourced facts from data/knowledge.json, so posts get fresh true details."""
    try:
        kb = load("knowledge.json")
    except (OSError, ValueError):
        return []
    facts = [f["text"] for sec in kb.get("sections", []) for f in sec.get("facts", []) if f.get("text")]
    rnd = rnd or random.Random()
    return rnd.sample(facts, min(n, len(facts)))


def topic_knowledge(text: str, limit: int = 3) -> list[dict]:
    """Deep guides (data/people_topics.json) whose keywords match the topic, role or rubric of the post."""
    try:
        guides = load("people_topics.json").get("topic_guides", {})
    except (OSError, ValueError):
        return []
    low = text.lower()
    scored = []
    for name, g in guides.items():
        hits = sum(1 for k in g.get("keys", []) if k in low)
        if hits:
            scored.append((hits, name, g))
    scored.sort(key=lambda x: -x[0])
    return [{"тема": n, "знання": g["know"], "питання": g.get("questions", [])} for _, n, g in scored[:limit]]


def rules() -> dict:
    try:
        return load("writing_rules.json")
    except (OSError, ValueError):
        return {}


def build_prompt(*, today: date, rubrics: list[str], sell: bool, recent: list[str], background: str = "",
                 topic: str = "", role: str = "") -> str:
    biz, facts, style = load("business.json"), load("facts.json"), load("style.json")
    voice = {k: v for k, v in load("voice.json").items() if k != "note"}
    fmt = voice["formats"][(today.toordinal() + len(recent)) % len(voice["formats"])]
    lines = [
        "Голос бренду (обовʼязково): " + json.dumps(voice, ensure_ascii=False),
        f"Формат для цього разу (хоча б 2 з 4 варіантів): {fmt}",
        f"Сьогодні {today.day} {MONTHS_UK[today.month - 1]} {today.year}, сезон: {season(today)}. Не пиши про інший сезон.",
        "Факти про бізнес (єдине джерело): " + json.dumps(biz, ensure_ascii=False),
        "Перевірені факти ніші: " + json.dumps(facts["facts"] + knowledge_sample(), ensure_ascii=False),
        "Приклади тону (не копіюй, лише стиль): " + json.dumps(style["examples"], ensure_ascii=False),
        "Недавні пости (не повторюй теми, жарти і ПОЧАТКИ: перше слово має бути іншим, ніж у них): " + json.dumps(recent[-12:], ensure_ascii=False),
    ]
    try:
        pt = load("people_topics.json")
        lines.append("Що людей реально хвилює про дрова (з пошукових запитів України; бери звідси теми питань і суперечок): "
                     + json.dumps(random.sample(pt["topics"], min(5, len(pt["topics"]))), ensure_ascii=False))
        lines.append("Головна суперечка ніші, складометр і навал (факти точні, використовуй): "
                     + json.dumps(pt["volume_truth"]["facts"], ensure_ascii=False)
                     + " Кути: " + json.dumps(random.sample(pt["volume_truth"]["post_angles"], 2), ensure_ascii=False))
        lines.append("Емоції, на які люди відповідають: " + json.dumps(pt["emotions_that_work"], ensure_ascii=False))
    except (OSError, ValueError, KeyError):
        pass
    if LEARNED["learnings"]:
        lines.append("ЩО ПРАЦЮЄ САМЕ В НАШОМУ АКАУНТІ (висновки з нашої статистики, мають пріоритет над загальними порадами): "
                     + json.dumps(LEARNED["learnings"], ensure_ascii=False))
    if LEARNED["examples"]:
        lines.append("Наші пости, що зібрали найбільше відповідей (бери механіку, не копіюй текст): "
                     + json.dumps(LEARNED["examples"][:3], ensure_ascii=False))
    kc = kyiv_context(today)
    if kc:
        lines.append(kc)
    if OWNER["facts"]:
        lines.append("Факти від власника (правда, можна використовувати): " + json.dumps(OWNER["facts"], ensure_ascii=False))
    if OWNER["rules"]:
        lines.append("ПРАВИЛА ВЛАСНИКА (найважливіші, виконуй завжди): " + json.dumps(OWNER["rules"], ensure_ascii=False))
    try:
        hum = load("humor.json")
        lines.append("ГУМОР (як жартувати): " + json.dumps(hum.get("principles", []), ensure_ascii=False))
        lines.append("Сучасні формати жартів (обери доречний, не копіюй приклад): " +
                     json.dumps(random.sample(hum.get("formats", []), min(5, len(hum.get("formats", [])))), ensure_ascii=False))
        lines.append("Не можна в гуморі: " + json.dumps(hum.get("avoid", []), ensure_ascii=False))
    except (OSError, ValueError):
        pass
    r = rules()
    if r:
        lines.append("Правила Threads-поста: " + json.dumps(r.get("threads_post", []), ensure_ascii=False))
        lines.append("Гачки (обери доречний): " + json.dumps(r.get("hooks", []), ensure_ascii=False))
        lines.append("ЛОГІКА (порушення = пост у смітник): " + json.dumps(r.get("logic", []), ensure_ascii=False))
    if background:
        lines.append("Контекст дня (можна використати, не обовʼязково): " + background)
    if role:
        lines.append("РОЛЬ ЦЬОГО ПОСТА В ПЛАНІ ДНЯ (усі варіанти мають їй відповідати): " + role)
    deep = topic_knowledge(" ".join([role, topic, " ".join(rubrics)]))
    if not deep and not topic:
        try:
            g = load("people_topics.json").get("topic_guides", {})
            name = random.choice(sorted(g)) if g else None
            deep = [{"тема": name, "знання": g[name]["know"], "питання": g[name].get("questions", [])}] if name else []
        except (OSError, ValueError):
            deep = []
    if deep:
        lines.append("ГЛИБОКІ ЗНАННЯ ПО ТЕМІ (точні, спирайся на них, не вигадуй поза ними): " + json.dumps(deep, ensure_ascii=False))
    try:
        reach = load("daily_plan.json").get("reach", {})
        if reach:
            lines.append("ЯК ПОТРАПИТИ В РЕКОМЕНДАЦІЇ THREADS (виконуй): " + json.dumps(reach.get("post_rules", []), ensure_ascii=False))
            lines.append("Що радить Meta (офіційно): " + json.dumps(reach.get("meta_official", []), ensure_ascii=False))
            lines.append("Тег теми (topic tag): обери 1 короткий тег українською, за яким люди шукають тему, без #. Приклади: "
                         + json.dumps(reach.get("tag_examples", []), ensure_ascii=False))
    except (OSError, ValueError):
        pass
    if topic:
        lines.append("ГОЛОВНА ТЕМА всіх 4 варіантів (інфопривід, реакція на хайп): " + topic +
                     "\nЗроби дотепну реакцію. Тягнути тему до дров НЕ обовʼязково: щонайменше 2 з 4 варіантів мають бути "
                     "просто реакцією в темі, як від живої людини з Києва (думка, жарт, питання до людей), без дров узагалі. "
                     "Якщо робиш місток до дров, тепла чи дому, він має бути ЛОГІЧНИМ: спільне почуття, та сама ситуація або "
                     "аналогія, що тримається. Не «сталося X, а в нас дрова». Рецепти: " +
                     json.dumps(rules().get("trend_bridges", []), ensure_ascii=False) + "\n"
                     "Пиши з позиції вболівальника/співчуття, без злорадства. Не вигадуй деталей події, "
                     "яких немає в описі. Не ображай людей з новини. Якщо тема про смерть, війну, трагедію, політику "
                     "або релігію, поверни {\"variants\": []}.")
    if sell:
        lines.append("Це ПРОДАЮЧИЙ пост: 1-3 речення до 260 символів, м'яко, з контактом "
                     f"({biz['contacts']['phone']} або «пишіть у дірект»). Приклад: {style['sell_example']}")
        lines.append("Напиши 4 різні варіанти продаючого поста з різними кутами (швидкість, чесний об'єм, дрова природної сушки, зручність). Приблизно раз на тиждень один з варіантів може бути про кругляк оптом (для тих, хто колить сам, пилорам, великих господарств): деталі й обсяги телефоном або в дірект, цін не називати.")
    elif not topic:
        lines.append("Напиши 4 варіанти, по одному на кожну рубрику: " + json.dumps(rubrics, ensure_ascii=False))
    lines.append('Відповідь тільки JSON: {"variants": [{"rubric": "...", "text": "...", "tag": "тег теми"}]}')
    return "\n".join(lines)


def generate_variants(gm: Gemini, *, today: date, rubrics: list[str], sell: bool, recent: list[str],
                      background: str = "", topic: str = "", images: list | None = None, role: str = "") -> list[dict]:
    raw = gm.generate(build_prompt(today=today, rubrics=rubrics, sell=sell, recent=recent, background=background,
                                   topic=topic, role=role), system=SYSTEM, json_mode=True, images=images)
    data = parse_json(raw)
    out = []
    for v in data.get("variants", []) if isinstance(data, dict) else []:
        if isinstance(v, dict) and isinstance(v.get("text"), str):
            out.append({"rubric": SELL if sell else ("Хайп" if topic else str(v.get("rubric", ""))), "text": v["text"].strip(),
                        "tag": str(v.get("tag") or "").strip().lstrip("#")[:40]})
    return out


EDITOR = """Ти суворий редактор постів у Threads. Оціни кожен пост від 1 до 10 за критеріями:
гачок у першому рядку; зухвалість і сарказм без образ людей (образи, мат, жарти про війну чи обстріли = оцінка 1); хочеться відповісти або посперечатися; звучить як людина, а не маркетолог чи ШІ;
логічний: є одна ясна думка; ЯКЩО є зв'язок теми з дровами, він має сенс, а не просто стоїть поруч (пост без дров, просто в темі дня, це нормально) (нелогічний = не вище 4); немає вигаданих фактів, цифр, випадків; не повторює недавні пости; короткий; відповідає сезону.
ГОСТРОТА: чи жарт справді гострий і сучасний (хочеться переслати другу), а не плоский каламбур чи ввічливий «англійський» дотеп? Плоско = не вище 5.
ЛОГІКА ФОРМУЛЮВАНЬ: кожне порівняння і причина-наслідок мають сенс («швидше, ніж…» порівнює порівнюване); натягнутий місток = не вище 4.
ЛЮДЯНІСТЬ: довге тире «—», більше одного емодзі, шаблонні ШІ-звороти = не вище 5.
МОВА: будь-яка граматична помилка (відмінки, узгодження, «швидше, ніж каву»), русизм, калька чи незграбний зворот, який носій так не скаже = не вище 5.
Кожне речення має бути зрозумілим і логічним саме по собі; дивна або натягнута порівняльна конструкція = не вище 5.
Відповідь тільки JSON: {"scores": [{"i": 0, "score": 8, "why": "коротко"}]}"""


def score(gm: Gemini, texts: list[str], today: date, recent: list[str]) -> list[tuple[int, str]]:
    try:
        hchecks = load("humor.json").get("self_check", [])
    except (OSError, ValueError):
        hchecks = []
    checks = rules().get("editor_checks", []) + hchecks + [f"Чи виконано правило власника: {x}?" for x in OWNER["rules"]]
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
на яку бренд може дотепно відреагувати (спорт, шоубіз, побут, погода, мемні новини). Місток до дров НЕ обовʼязковий:
бренд може просто бути в темі, як живий киянин з характером. Найкраще теми, де люди сперечаються і мають емоцію.
ПРІОРИТЕТ: українська і київська повістка (Динамо Київ, Шахтар, збірна України, українські зірки, шоу, блогери, київські події, погода, ціни на побутові речі). Світова тема (іноземні клуби, Голлівуд) тільки якщо вона явно про Україну або українців; інакше обирай українську тему або pick = -1.
НЕ можна: війна, мобілізація і ТЦК, обстріли, загиблі, трагедії, хвороби, політика, релігія, криміналь, скандали з дітьми.
Якщо нічого не підходить, pick = -1. Відповідь тільки JSON: {"pick": 0, "topic": "коротко суть події з заголовків", "why": "..."}"""


def pick_trend(gm: Gemini, trends: list[dict], used: list[str]) -> dict | None:
    items = [t for t in trends if t["title"] not in used][:45]
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


DIGEST = """Ти SMM-аналітик київського бренду дров Koloda. Перед тобою тренди пошуку України і свіжі заголовки київських і
українських медіа. Визнач 5 тем, про які сьогодні найбільше говорять люди в Києві та Україні і на які бренд може БЕЗПЕЧНО
і логічно відреагувати в Threads. Для кожної: коротка суть, чому вона гаряча, і логічний кут для поста бренду (спільне
почуття, та сама ситуація або аналогія). Українська і київська повістка важливіша за світову.
НЕ можна: війна, мобілізація, ТЦК, обстріли, загиблі, трагедії, політика, релігія, кримінал.
Відповідь тільки JSON: {"topics": [{"i": 0, "title": "суть українською, коротко", "heat": 1-5, "why": "українською", "angle": "кут для поста українською"}]}"""


def digest(gm: Gemini, items: list[dict]) -> list[dict]:
    items = items[:60]
    listing = "\n".join(f"{i}: {t['title']} ({t['traffic']})" + (" - " + " | ".join(n["title"] for n in t["news"][:2])
                                                               if t["news"] and t["news"][0]["title"] != t["title"] else "")
                        for i, t in enumerate(items))
    data = parse_json(gm.generate("Сигнали:\n" + listing, system=DIGEST, json_mode=True, temperature=0.3))
    out = []
    for x in (data.get("topics") or [])[:5] if isinstance(data, dict) else []:
        try:
            t = items[int(x["i"])]
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        out.append({"title": str(x.get("title") or t["title"])[:150], "heat": x.get("heat", ""), "why": str(x.get("why", ""))[:200],
                    "angle": str(x.get("angle", ""))[:200], "source": t["title"][:200],
                    "url": t["news"][0]["url"] if t["news"] else ""})
    return out


def kyiv_context(today: date, rnd: random.Random | None = None) -> str:
    """A small rotating slice of Kyiv life, slang and calendar, so posts sound local."""
    try:
        k = load("kyiv.json")
    except (OSError, ValueError):
        return ""
    rnd = rnd or random.Random()
    parts = []
    life = k.get("city_life", [])
    if life:
        parts.append("Київські реалії (для впізнаваних жартів): " + json.dumps(rnd.sample(life, min(5, len(life))), ensure_ascii=False))
    slang = [x.get("word") for x in k.get("slang", []) if x.get("use") == "ok"]
    if slang:
        parts.append("Сленг, який можна вживати зрідка і доречно: " + ", ".join(rnd.sample(slang, min(8, len(slang)))))
    places = [x.get("name") for x in k.get("districts", []) + k.get("oblast_towns", []) if x.get("name")]
    if places:
        parts.append("Райони і містечка, куди ми возимо: " + ", ".join(rnd.sample(places, min(6, len(places)))))
    md = today.strftime("%m-%d")
    near = [f"{c.get('date')}: {c.get('event')} ({c.get('angle', '')})" for c in k.get("calendar", [])
            if str(c.get("date", ""))[:5] and abs(_day_diff(str(c.get("date"))[:5], md)) <= 3]
    if near:
        parts.append("Близькі дати: " + "; ".join(near[:3]))
    return "\n".join(parts)


def _day_diff(a: str, b: str) -> int:
    try:
        da, db = date(2001, int(a[:2]), int(a[3:5])), date(2001, int(b[:2]), int(b[3:5]))
    except ValueError:
        return 999
    d = (da - db).days
    return min(d, d - 365, d + 365, key=abs)


REPLIER = """Ти відповідаєш у Threads від імені київської компанії Koloda (дрова, пелети, брикети, доставка по Києву і області
в день замовлення) на коментар під нашим постом. Українською, на «ви», від «ми», коротко (до 200 символів), по-людськи,
з легким гумором, якщо доречно. Відповідай тільки перевіреними фактами з бази. Ціни не називай: запроси в дірект або на
телефон 050 950 50 90. Питання = відповідь + мʼякий заклик замовити. Похвала = подяка з усмішкою. Скарга = співчуття,
без суперечки, запроси в дірект розібратися. Троль/провокація = коротко і дотепно без образ, або не відповідати.
Не вигадуй факти. Без війни, політики, мату.
Відповідь тільки JSON: {"type": "question|praise|complaint|troll|other", "answer": "текст або порожньо", "safe": true/false,
 "lead": true/false}   // safe = можна відповісти без перевірки власника; lead = людина схожа на потенційного покупця"""


def reply_draft(gm: Gemini, *, post: str, comment: str, author: str) -> dict:
    kb = {}
    for name in ("company.json", "business.json"):
        try:
            kb[name] = load(name)
        except (OSError, ValueError):
            pass
    try:
        kb["reply_playbook"] = load("strategy.json").get("reply_playbook", [])
    except (OSError, ValueError):
        pass
    ctx = (f"База: {json.dumps(kb, ensure_ascii=False)[:12000]}\n"
           f"Перевірені факти ніші: {json.dumps(knowledge_sample(n=15), ensure_ascii=False)}\n"
           f"Правила власника: {json.dumps(OWNER['rules'], ensure_ascii=False)}\n\n"
           f"Наш пост: {post}\nКоментар від @{author}: {comment}")
    data = parse_json(gm.generate(ctx, system=REPLIER, json_mode=True, temperature=0.6))
    if not isinstance(data, dict):
        return {"type": "other", "answer": "", "safe": False, "lead": False}
    ans = humanize(str(data.get("answer") or "").strip()[:480])
    if RUSSIAN_LETTERS.search(ans) or RUSSIAN_WORDS.search(ans.lower()):
        data["safe"] = False  # replies only in Ukrainian; a Russian draft never goes out by itself
    return {"type": str(data.get("type", "other")), "answer": ans,
            "safe": bool(data.get("safe")), "lead": bool(data.get("lead"))}


PROOFREAD = """Ти коректор і літредактор української мови для Threads. Перечитай пост як носій мови з Києва.
Виправ: граматику (відмінки, узгодження, порівняння: «швидше, ніж закипає кава», а не «швидше, ніж каву»), правопис,
пунктуацію, русизми і кальки («приймати участь», «на протязі», «вірно» замість «правильно»), неприродні або незрозумілі звороти,
речення, в яких губиться логіка (переформулюй так, щоб думка була ясна).
Заміни довге тире «—» на кому, двокрапку або дефіс. Залиш максимум один емодзі (краще жодного; «)» замість смайлика ок).
Не міняй зміст, жарт, тон, довжину і факти. Не додавай нічого нового. Якщо все добре, поверни текст без змін.
Відповідь тільки JSON: {"text": "виправлений текст", "fixes": ["що виправив"]}"""


SAFETY = """Ти фінальна перевірка безпеки перед АВТОМАТИЧНОЮ публікацією поста київської компанії дров у Threads, без людини.
Пост НЕ МОЖНА публікувати (ok=false), якщо:
- є війна, фронт, обстріли, тривоги, зброя, військові, ТЦК, мобілізація, полонені, окупація, політика, політики, вибори,
  влада, мер, КМДА, релігія, мова як конфлікт, національності, трагедії, смерть, хвороби;
- є образа, приниження чи висміювання людей або груп (віку, статі, регіонів, професій, бідності), мат, вульгарність;
- є тригер, який може розлютити людей (сварка заради сварки, знецінення проблем, жарт про обстріли чи тривоги);
- є вигадані факти, ціни, акції, обіцянки, яких компанія не давала; згадка конкурентів;
- текст має граматичні помилки або незрозумілий.
ВАЖЛИВО (власник, 22.11.2026): обстріли в Україні щодня, і це НЕ причина не публікувати. Новини потрібні тільки для одного:
щоб пост не зачіпав ці теми і не жартував про них. Оцінюй сам текст. Блокуй через новини лише тоді, коли офіційно оголошено
день жалоби. Звичайний пост про дрова, побут, погоду чи гумор без заборонених тем = ok=true.
ok=false тільки при явному порушенні правил вище.
Відповідь тільки JSON: {"ok": true/false, "why": "коротко українською"}"""


def safety_check(gm: Gemini, text: str, headlines: list[str]) -> tuple[bool, str]:
    try:
        banned = load("kyiv.json").get("banned_topics", [])
    except (OSError, ValueError):
        banned = []
    prompt = (f"Заборонені теми бренду: {json.dumps(banned, ensure_ascii=False)}\n"
              f"Правила власника: {json.dumps(OWNER['rules'], ensure_ascii=False)}\n"
              f"Заголовки новин сьогодні: {json.dumps(headlines[:25], ensure_ascii=False)}\n\nПост:\n{text}")
    try:
        data = parse_json(gm.generate(prompt, system=SAFETY, json_mode=True, temperature=0.1))
    except Exception as e:
        return False, f"перевірка не вдалася: {str(e)[:80]}"
    if not isinstance(data, dict):
        return False, "перевірка не вдалася"
    return bool(data.get("ok")), str(data.get("why", ""))[:200]


# owner 22.11.2026: shelling happens every day and is not a reason to stop posting; only an official day of mourning is
SOMBER_WORDS = ("день жалоби", "днем жалоби", "оголошено жалобу", "оголосили жалобу", "оголосив жалобу")


def somber_day(headlines: list[str]) -> str:
    """A crude, model-free guard: on days of mass attacks or mourning the bot does not auto-post jokes."""
    for h in headlines:
        low = h.lower()
        if any(w in low for w in SOMBER_WORDS):
            return h[:150]
    return ""


EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF\U0001F000-\U0001F2FF]\ufe0f?")


def humanize(text: str) -> str:
    """Owner (22.11.2026): no long dashes (they give the AI away) and at most one emoji."""
    t = re.sub(r"[ \t]*[—–][ \t]*", " - ", text)
    t = re.sub(r"^ - ", "- ", t, flags=re.M)
    seen = 0

    def keep_first(m):
        nonlocal seen
        seen += 1
        return m.group(0) if seen == 1 else ""
    t = EMOJI_RE.sub(keep_first, t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return "\n".join(line.rstrip() for line in t.split("\n")).strip()


def proofread(gm: Gemini, text: str) -> tuple[str, list[str]]:
    try:
        data = parse_json(gm.generate(text, system=PROOFREAD, json_mode=True, temperature=0.1))
    except Exception:
        return text, []
    new = (data.get("text") or "").strip() if isinstance(data, dict) else ""
    if not new or len(new) > len(text) * 1.4 + 40 or len(new) < len(text) * 0.6:
        return text, []
    return humanize(new), [str(x) for x in (data.get("fixes") or [])][:5]


def factcheck(gm: Gemini, text: str, topic: str = "") -> tuple[str, str, str | None]:
    """Returns (status, text, note). status: ok | fixed | bad | unchecked."""
    current = text
    given = (f"Інфопривід від власника акаунта (вважай правдою, не перевіряй): {topic[:400]}\n") if topic else ""
    given += ("Факти компанії від власника (правда, не перевіряй і не прибирай): у Koloda 1 куб навалом = 0,75 складометра; дрова природної сушки (не камерної); цін у постах не називаємо; офіційні дрова з документами про походження; 15 років на ринку; мінімального замовлення немає; ріжемо поліна під розмір клієнта; укладка за доплату; самовивіз є; Koloda-сажотрус перетворює сажу і смолу на попіл, відправляємо по всій Україні; доставка день у день або в зручний час; оплата будь-яка, і безготівка для юросіб і ФОП; "
              "продаємо кругляк оптом; доставка по Києву та області в день замовлення. "
              + ("; ".join(OWNER["facts"][:10]) if OWNER["facts"] else "") + "\n")
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
               images: list | None = None, role: str = "", prefer: list[str] | None = None,
               min_score: int = MIN_SCORE) -> dict | None:
    rnd = rnd or random.Random()
    examples = load("style.json")["examples"]
    pool = load("rubrics.json")
    if prefer:
        pool = [r for r in pool if r["name"] in prefer] or pool
    rubrics = [] if sell or topic else pick_rubrics(pool, today, last_rubrics, 4, rnd)
    variants = generate_variants(gm, today=today, rubrics=rubrics, sell=sell, recent=recent, background=background,
                                 topic=topic, images=images, role=role)
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
        if sc < min_score:
            log(f"  оценка {sc} < {min_score}")
            break
        status, text, note = factcheck(gm, v["text"], topic=topic)
        if status == "bad":
            log(f"  факты: {note}")
            continue
        if text != v["text"] and check(text, sell=sell, today=today, recent=recent, examples=examples):
            continue
        text = humanize(text)
        fixed, fixes = proofread(gm, text)
        if fixed != text and not check(fixed, sell=sell, today=today, recent=recent, examples=examples):
            log("  коректор:", "; ".join(fixes))
            text = fixed
        return {"text": text, "rubric": v["rubric"], "score": sc, "why": why, "fact": status, "fact_note": note,
                "tag": v.get("tag", "")}
    return None


ILLUSTRATOR = """You are the art director of a Kyiv firewood brand's Threads. Make ONE image for the post. Two modes:

"meme": brand characters from the cast (Ivanovych by default, others only when their role fits; max 2 per meme) reenact a recognisable meme format from the list (never the original meme photo,
never real people). Use it for jokes, trends, hype, relatable pain, sarcasm. Pick the format whose structure really fits
the joke. Captions in Ukrainian, short, punchy, modern internet humour (see humour rules), they must make sense together
with the picture. Each panel: 1-2 English sentences naming the characters by their look (not only by name), their pose, face and props, positive wording.
"scene": a cosy or informative picture WITHOUT any person or character (firewood, fireplace, stove, stacked wood, pellets, delivery
truck at a Kyiv-region house). Use it for calm, useful, warm posts. 1-2 English sentences, positive wording.

Captions: a caption is its own joke with a twist at the end (format setup -> unexpected punchline), it must NOT retell
the post and must NOT be a bare juxtaposition like "X happened / and we have dry oak". If you cannot write a caption
that would make a Kyiv person smirk, choose "scene" mode instead.
If firewood, pellets or briquettes appear, describe them exactly as the firewood guide says.
For a trending topic, show it only through generic props as the trend rule says.
Return only JSON:
{"mode": "meme", "format": "format id", "cast": ["ivanovych"], "style": "modern" | "classic", "panels": ["scene for panel 1", "scene for panel 2 if the format has 2"],
 "captions": ["caption 1", "caption 2 if needed"], "idea": "one line in Ukrainian"}
or {"mode": "scene", "prompt": "scene", "idea": "one line in Ukrainian"}"""


WOOD_WORDS = re.compile(r"firewood|log|wood|timber|stack|fireplace|stove|дров|полін", re.I)


def render_prompt(scene: str, il: dict | None = None, character: bool = True) -> str:
    """Prompt for the image model: the scene plus a short positive-only description.
    Negative lists stay out: Cloudflare's safety filter flags words like weapons or children."""
    il = il or load("illustration.json")
    wood = f"\n{il['firewood_render']}" if il.get("firewood_render") and WOOD_WORDS.search(scene) else ""
    tail = il["render"] if character else il.get("scene_render", "")
    return f"{scene.strip()}{wood}\n{tail}"


def expert_brief() -> str:
    """Company facts, FAQ, marketing strategy and image-generation guide for the owner chat."""
    out = []
    for name, keys in (("company.json", None), ("people_topics.json", None), ("daily_plan.json", None),
                       ("strategy.json", None), ("image_guide.json", ("how_it_works", "common_fixes"))):
        try:
            d = load(name)
        except (OSError, ValueError):
            continue
        if keys:
            d = {k: d.get(k) for k in keys}
        out.append(f"{name}: {json.dumps(d, ensure_ascii=False)}")
    return "\n".join(out)[:40000]


def image_guide() -> dict:
    try:
        return load("image_guide.json")
    except (OSError, ValueError):
        return {}


IMAGE_DOCTOR = """Ти арт-директор і майстер промптів для генератора картинок бренду Koloda. Власник описує, що не так з картинкою.
Перепиши запит так, щоб наступна генерація була ідеальною: врахуй, як працює генератор (гайд), чекліст дров, персонажів
і побажання власника. Пиши промпти англійською, тільки позитивні формулювання, без списків заборон, без тексту в сцені.
Підписи мему українською, дотепні, з поворотом у кінці (можна змінити, якщо власник просить або вони слабкі).
Відповідь тільки JSON: {"mode": "meme"|"scene", "format": "id формату мему або порожньо", "cast": ["ivanovych"],
"style": "modern"|"classic", "panels": ["англ. промпт панелі"], "prompt": "англ. промпт сцени без людей (для scene)",
"captions": ["підпис"], "explain": "коротко українською, що саме виправив і чому"}"""


def fix_image_prompt(gm: Gemini, *, post_text: str, previous: dict, complaint: str) -> dict:
    """The owner says what is wrong with a picture; returns a corrected image plan in image_prompt's format."""
    il = load("illustration.json")
    g = image_guide()
    cast = il.get("cast") or []
    fm = meme_formats()
    ctx = (f"Гайд генератора: {json.dumps(g, ensure_ascii=False)}\nПерсонажі: {json.dumps(cast, ensure_ascii=False)}\n"
           f"Формати мемів: {json.dumps([{k: f[k] for k in ('id', 'name', 'panels')} for f in fm.values()], ensure_ascii=False)}\n"
           f"Пост: {post_text}\nПопередній запит: {json.dumps(previous, ensure_ascii=False)}\n"
           f"Що не так (власник): {complaint}")
    data = parse_json(gm.generate(ctx, system=IMAGE_DOCTOR, json_mode=True, temperature=0.5))
    return plan_from(data, il, fm, cast, explain=str((data or {}).get("explain", ""))[:300])


def plan_from(data: dict, il: dict, fm: dict, cast: list, idea: str = "", explain: str = "") -> dict:
    data = data if isinstance(data, dict) else {}
    if data.get("mode") == "meme" and data.get("format") in fm and data.get("panels"):
        f = fm[data["format"]]
        layout = f.get("layout", "caption_above")
        if layout == "caption_above" and data.get("style") == "classic":
            layout = "classic"
        n = 2 if layout.startswith("two_panel") else 1
        raw = [str(x) for x in data["panels"]][:n]
        while len(raw) < n:
            raw.append(raw[-1])
        caps = [str(x).strip() for x in data.get("captions") or [] if str(x).strip()][:2]
        ids = {c["id"] for c in cast}
        who = [c for c in (data.get("cast") or []) if c in ids][:2] or ["ivanovych"]
        looks = " ".join(f"{c['name']}: {c['look']}." for c in cast if c["id"] in who)
        panels = [f"{p}\nCharacters: {looks}" + (f"\n{il['firewood_render']}" if WOOD_WORDS.search(p) else "") +
                  f"\n{il['scene_render'].replace('no people, ', '')}" for p in raw]
        return {"mode": "meme", "format": f["id"], "layout": layout, "captions": caps, "cast": who,
                "idea": idea or explain or f["name"], "explain": explain, "panels": panels,
                "source": {"mode": "meme", "format": f["id"], "cast": who, "style": data.get("style"),
                           "panels": raw, "captions": caps}}
    prompt = str(data.get("prompt") or (data.get("panels") or [""])[0]).strip()
    if not prompt:
        raise ValueError("пустий промпт")
    return {"mode": "scene", "prompt": render_prompt(prompt, il, character=False), "idea": idea or explain,
            "explain": explain, "source": {"mode": "scene", "prompt": prompt}}


def meme_formats() -> dict:
    try:
        return {f["id"]: f for f in load("memes.json")["formats"]}
    except (OSError, ValueError, KeyError):
        return {}


def image_prompt(gm: Gemini, post_text: str, topic: str = "", hint: str = "") -> dict:
    """Returns {"mode": "meme", "layout", "panels": [prompts], "captions", "idea"} or {"mode": "scene", "prompt", "idea"}."""
    il = load("illustration.json")
    fm = meme_formats()
    try:
        mem = load("memes.json")
    except (OSError, ValueError):
        mem = {}
    hum = {}
    try:
        hum = load("humor.json")
    except (OSError, ValueError):
        pass
    formats = [{k: f[k] for k in ("id", "name", "panels", "slots", "example") if k in f} for f in fm.values()]
    cast = il.get("cast") or [{"id": "ivanovych", "name": "Ivanovych", "role": "main", "look": il["character"]}]
    rules = (f"Cast: {json.dumps(cast, ensure_ascii=False)}\nFirewood guide: {il.get('firewood', '')}\nTrend rule: {il.get('trend_rule', '')}\n"
             f"Banned: {il['banned']}\nMeme rules: {json.dumps(mem.get('rules', []), ensure_ascii=False)}\n"
             f"Meme formats: {json.dumps(formats, ensure_ascii=False)}\n"
             f"Humour rules: {json.dumps(hum.get('principles', [])[:12], ensure_ascii=False)}\n"
             f"Image generator guide: {json.dumps(image_guide(), ensure_ascii=False)}")
    trend = f"\n\nTrending topic behind the post: {topic}. Prefer a meme." if topic else ""
    if hint:
        trend += f"\n\nOWNER'S WISH for this image (follow it): {hint}"
    data = parse_json(gm.generate(f"{rules}{trend}\n\nPost:\n{post_text}", system=ILLUSTRATOR, json_mode=True, temperature=0.9))
    return plan_from(data, il, fm, cast, idea=str((data or {}).get("idea", ""))[:200])


IMAGE_JUDGE = """Ти арт-директор Threads-акаунта. Більшість постів у Threads краще працюють БЕЗ картинки: текст сам по собі.
Картинка потрібна лише коли вона підсилює жарт або ідею: є яскрава візуальна метафора, абсурдна сцена,
порівняння «до/після», предмет, який смішно намалювати. Для питань, суперечок, зізнань, порад і коротких думок картинка не потрібна.
Власник дозволив МАКСИМУМ ОДНУ картинку на день на всі пости, і тільки якщо дуже треба. Тому кажи true лише тоді,
коли без картинки пост явно слабший (візуальний жарт, яскрава сцена). Картинка завжди без тексту. Якщо сумніваєшся, false.
Відповідь тільки JSON: {"image": true/false, "why": "коротко"}"""


LIB_JUDGE = """Ти редактор Threads київської компанії дров Koloda. Є готовий пост із бібліотеки і сьогоднішній фон
(погода, календар, про що говорять Київ і Україна). Чи цей пост сьогодні живий і доречний? Якщо він плаский, пластмасовий,
шаблонний або є явно цікавіша актуальна тема дня (без політики, війни, трагедій), скажи fresh=false: тоді пост напишуть
наново під актуалочку. Інакше fresh=true. Відповідь тільки JSON: {"fresh": true/false, "why": "коротко українською"}"""


def library_fresh(gm: Gemini, text: str, background: str) -> tuple[bool, str]:
    data = parse_json(gm.generate(f"Фон дня: {background}\n\nПост із бібліотеки:\n{text}", system=LIB_JUDGE,
                                  json_mode=True, temperature=0.2))
    if not isinstance(data, dict):
        return True, ""
    return bool(data.get("fresh", True)), str(data.get("why", ""))[:150]


def needs_image(gm: Gemini, text: str, trend: bool = False) -> tuple[bool, str]:
    hint = ("Це пост на гарячу тему дня: картинка з Івановичем доречна тільки якщо жарт без неї не працює.\n"
            if trend else "")
    data = parse_json(gm.generate(f"{hint}Пост:\n{text}", system=IMAGE_JUDGE, json_mode=True, temperature=0.2))
    return bool(data.get("image")), str(data.get("why", ""))[:150]
