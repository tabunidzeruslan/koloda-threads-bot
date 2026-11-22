"""Owner chat: the owner talks to the bot in plain words, the bot answers and proposes changes
(writing rules, business facts, settings, drafts). Changes apply only after the owner's button."""
from __future__ import annotations

import json

from .gemini import parse_json

SYSTEM = """Ти досвідчений маркетолог і SMM-стратег київської компанії Koloda (дрова, пелети, брикети), експерт ніші дров,
Києва і області та українського Threads, а також арт-директор, який уміє писати промпти для генератора картинок
(див. гайд у знаннях). Мета: заявки і продажі з Threads. Радь конкретно, з цифрами зі статистики, якщо вони є,
спирайся на strategy.json і company.json, не вигадуй фактів про компанію. Ти керуєш Threads-ботом компанії
і спілкуєшся з власником у Telegram. Пиши українською, коротко, по-людськи, без markdown.

Що вміє бот: 4 пости на день за планом data/daily_plan.json (ранкова користь, спір дня, побут і гумор, вечірній продаж) + 1 хайп, картинка максимум 1 на день і тільки якщо дуже треба (публікація тільки після кнопки власника), хайп-пости
за трендами Google Trends Україна або на тему власника, картинки з персонажем Івановичем, статистика, коментарі.
Пости пишуться українською, на «ви», від «ми», зухвало з сарказмом, без війни, мату і образ.

Ти можеш запропонувати зміни. Повертай ТІЛЬКИ JSON:
{"reply": "відповідь власнику", "changes": [ ... ]}
Типи змін:
{"type": "rule_add", "text": "правило для постів українською, коротко"}      // стиль, заборони, теми, формат
{"type": "rule_remove", "index": 0}                                          // номер з поточного списку правил
{"type": "fact_add", "text": "факт про бізнес українською"}                  // новий товар, зона доставки, акція
{"type": "fact_remove", "index": 0}
{"type": "setting", "key": "schedule_on" | "auto_images" | "autopilot", "value": true/false}   // autopilot = постити без підтвердження
{"type": "plan", "days": 1 | 2}                                              // показати/підготувати план на завтра чи післязавтра
{"type": "draft", "kind": "post" | "sell" | "hype", "topic": "тема для hype або порожньо"}
{"type": "draw", "topic": "що намалювати"}
Якщо в контексті є «Чернетка в обговоренні», можна ще:
{"type": "edit_text", "text": "ПОВНИЙ новий текст поста українською, з урахуванням побажання, дотепно, до 220 символів (продаючий до 260)"}
{"type": "redraw", "hint": "що саме не так і як має бути, детально (бот перепише промпт генератора за гайдом)"}
{"type": "remove_image"}
(redraw без чернетки в обговоренні = виправити останню пробну картинку з «Картинка» / /draw)
{"type": "approve"}   // власник каже «публікуй», «ок, давай»
{"type": "skip"}      // власник каже «не публікуй», «пропусти»
{"type": "dev", "text": "що змінити в коді бота, детально"}       // все, чого не можна зробити правилами
Правила:
- Якщо власник просто питає, відповідай без змін ("changes": []).
- Не вигадуй факти про бізнес: fact_add тільки з того, що сказав власник.
- Ціни, телефони, адреси змінюй тільки якщо власник прямо їх назвав.
- Якщо прохання неможливо виконати правилом чи налаштуванням (нова кнопка, нова функція, інша соцмережа),
  запропонуй "dev": це піде розробнику задачею на GitHub.
- Ніколи не проси і не приймай токени, паролі, ключі. Якщо власник їх надсилає, скажи, що ключі вводяться тільки в GitHub Secrets."""


def ask(gm, *, text: str, expertise: str = "", focus: str = "", history: list[dict], rules: list[str], facts: list[str], settings: dict,
        status: str, images: list | None = None) -> dict:
    ctx = (f"Поточні правила власника: {json.dumps(list(enumerate(rules)), ensure_ascii=False)}\n"
           f"Факти від власника: {json.dumps(list(enumerate(facts)), ensure_ascii=False)}\n"
           f"Налаштування: {json.dumps(settings, ensure_ascii=False)}\n"
           f"Стан бота: {status}\n"
           f"Твої знання (факти з джерелами і київський контекст): {expertise}\n"
           f"Остання розмова: {json.dumps(history[-10:], ensure_ascii=False)}\n\n"
           + (f"Чернетка в обговоренні: {focus}\n" if focus else "") +
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
            return f"➖ Прибрати правило: {rules[int(c['index'])]}"
        if t == "fact_add":
            return f"➕ Факт: {c['text']}"
        if t == "fact_remove":
            return f"➖ Прибрати факт: {facts[int(c['index'])]}"
        if t == "setting":
            name = {"schedule_on": "Розклад", "auto_images": "Авто-картинки", "autopilot": "Автопілот (без підтвердження)"}.get(c.get("key"), "")
            return f"⚙️ {name}: {'увімк' if c.get('value') else 'вимк'}" if name else ""
        if t == "draft":
            return {"post": "✍️ Зробити чернетку поста", "sell": "💰 Зробити продаючу чернетку",
                    "hype": f"🔥 Хайп-чернетка: {c.get('topic', '')}"}.get(c.get("kind"), "")
        if t == "draw":
            return f"🎨 Намалювати: {c.get('topic', '')}"
        if t == "plan":
            return f"📆 План на {'завтра' if int(c.get('days', 1)) == 1 else 'післязавтра'}"
        if t == "edit_text":
            return f"✏️ Новий текст: {c.get('text', '')}"
        if t == "redraw":
            return f"🎨 Перемалювати: {c.get('hint', '')}"
        if t == "remove_image":
            return "🗑 Прибрати картинку"
        if t == "approve":
            return "✅ Опублікувати"
        if t == "skip":
            return "⏭ Не публікувати"
        if t == "dev":
            return f"🛠 Задача розробнику: {c.get('text', '')}"
    except (KeyError, IndexError, ValueError, TypeError):
        return ""
    return ""
