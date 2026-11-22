"""Owner chat: the owner talks to the bot in plain words, the bot answers and proposes changes
(writing rules, business facts, settings, drafts). Changes apply only after the owner's button."""
from __future__ import annotations

import json

from .gemini import parse_json

SYSTEM = """Ти помічник власника київської компанії Koloda (дрова, пелети, брикети). Ти керуєш Threads-ботом компанії
і спілкуєшся з власником у Telegram. Пиши ПО-РОСІЙСЬКИ (власник так пише), коротко, по-людськи, без markdown.

Що вміє бот: 3 чернетки на день (публікація тільки після кнопки власника), продаючий пост кожен 5-й, хайп-пости
за трендами Google Trends Україна або на тему власника, картинки з персонажем Івановичем, статистика, коментарі.
Пости пишуться українською, на «ви», від «ми», зухвало з сарказмом, без війни, мату і образ.

Ти можеш запропонувати зміни. Повертай ТІЛЬКИ JSON:
{"reply": "відповідь власнику", "changes": [ ... ]}
Типи змін:
{"type": "rule_add", "text": "правило для постів українською, коротко"}      // стиль, заборони, теми, формат
{"type": "rule_remove", "index": 0}                                          // номер з поточного списку правил
{"type": "fact_add", "text": "факт про бізнес українською"}                  // новий товар, зона доставки, акція
{"type": "fact_remove", "index": 0}
{"type": "setting", "key": "schedule_on" | "auto_images", "value": true/false}
{"type": "draft", "kind": "post" | "sell" | "hype", "topic": "тема для hype або порожньо"}
{"type": "draw", "topic": "що намалювати"}
{"type": "dev", "text": "що змінити в коді бота, детально російською"}       // все, чого не можна зробити правилами
Правила:
- Якщо власник просто питає, відповідай без змін ("changes": []).
- Не вигадуй факти про бізнес: fact_add тільки з того, що сказав власник.
- Ціни, телефони, адреси змінюй тільки якщо власник прямо їх назвав.
- Якщо прохання неможливо виконати правилом чи налаштуванням (нова кнопка, нова функція, інша соцмережа),
  запропонуй "dev": це піде розробнику задачею на GitHub.
- Ніколи не проси і не приймай токени, паролі, ключі. Якщо власник їх надсилає, скажи, що ключі вводяться тільки в GitHub Secrets."""


def ask(gm, *, text: str, expertise: str = "", history: list[dict], rules: list[str], facts: list[str], settings: dict,
        status: str, images: list | None = None) -> dict:
    ctx = (f"Поточні правила власника: {json.dumps(list(enumerate(rules)), ensure_ascii=False)}\n"
           f"Факти від власника: {json.dumps(list(enumerate(facts)), ensure_ascii=False)}\n"
           f"Налаштування: {json.dumps(settings, ensure_ascii=False)}\n"
           f"Стан бота: {status}\n"
           f"Твої знання (факти з джерелами і київський контекст): {expertise}\n"
           f"Остання розмова: {json.dumps(history[-10:], ensure_ascii=False)}\n\n"
           f"Повідомлення власника: {text}")
    data = parse_json(gm.generate(ctx, system=SYSTEM, json_mode=True, temperature=0.4, images=images))
    if not isinstance(data, dict):
        return {"reply": str(data)[:1000], "changes": []}
    changes = [c for c in data.get("changes") or [] if isinstance(c, dict) and c.get("type")]
    return {"reply": str(data.get("reply") or "").strip()[:3000], "changes": changes[:6]}


def describe(c: dict, rules: list[str], facts: list[str]) -> str:
    t = c.get("type")
    try:
        if t == "rule_add":
            return f"➕ Правило: {c['text']}"
        if t == "rule_remove":
            return f"➖ Убрать правило: {rules[int(c['index'])]}"
        if t == "fact_add":
            return f"➕ Факт: {c['text']}"
        if t == "fact_remove":
            return f"➖ Убрать факт: {facts[int(c['index'])]}"
        if t == "setting":
            name = {"schedule_on": "Расписание", "auto_images": "Авто-картинки"}.get(c.get("key"), "")
            return f"⚙️ {name}: {'вкл' if c.get('value') else 'выкл'}" if name else ""
        if t == "draft":
            return {"post": "✍️ Сделать черновик поста", "sell": "💰 Сделать продающий черновик",
                    "hype": f"🔥 Хайп-черновик: {c.get('topic', '')}"}.get(c.get("kind"), "")
        if t == "draw":
            return f"🎨 Нарисовать: {c.get('topic', '')}"
        if t == "dev":
            return f"🛠 Задача разработчику: {c.get('text', '')}"
    except (KeyError, IndexError, ValueError, TypeError):
        return ""
    return ""
