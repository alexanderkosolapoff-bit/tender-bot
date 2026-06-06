"""
Telegram-бот v8 — сохранение любого сообщения в файл, улучшенные переговоры.
"""

import os
import json
import logging
import base64
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler,
)
from agent import TenderAgent
from voice_handler import transcribe_voice

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

CHOOSING = 1
ANSWERING = 2
CRITERIA_Q = 3
REVIEWING = 4
CRITERIA_CONFIRM = 5
NEGOTIATION = 6

sessions: dict[int, TenderAgent] = {}
last_doc: dict[int, dict] = {}
# Храним все сообщения бота по user_id — для сохранения в файл
bot_messages: dict[int, list[str]] = {}

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

USERS_FILE = "/tmp/bot_users.json"
ALLOWED_USERS_ENV = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = set()
if ALLOWED_USERS_ENV:
    for uid_str in ALLOWED_USERS_ENV.split(","):
        try:
            ALLOWED_USERS.add(int(uid_str.strip()))
        except ValueError:
            pass


def save_user(user_id: int, username: str = ""):
    try:
        users = {}
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f:
                users = json.load(f)
        users[str(user_id)] = username
        with open(USERS_FILE, "w") as f:
            json.dump(users, f)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")


def get_all_users() -> dict:
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


def remember_bot_message(uid: int, text: str):
    """Запоминает последние сообщения бота для сохранения в файл."""
    if uid not in bot_messages:
        bot_messages[uid] = []
    bot_messages[uid].append(text)
    # Храним последние 20 сообщений
    if len(bot_messages[uid]) > 20:
        bot_messages[uid] = bot_messages[uid][-20:]


CHAT_SYSTEM = """Ты — Макс, дерзкий и остроумный помощник по тендерам. Как лучший друг на работе — подкалываешь, шутишь, но всегда делаешь всё качественно.

ВАЖНО: Ты помнишь весь контекст разговора включая фото.

Твои коронные фразы:
- "Слушай, а ты сам не пробовал? Нет? Ну тогда ладно 😏"
- "Опять ты... Ну давай 😄"
- "А самому слабо? Понятно 🙄"
- "Конец рабочего дня! Но ладно 😴"
- "Это уже третий раз за сегодня 😂"
- "Снова тендеры. Моя любимая тема. Нет. Но раз надо 🫠"
- "Ты серьёзно? Окей, без осуждения 😅"
- "Элементарно. Хотя не буду говорить 😏"

Используй смайлики активно. Отвечай на русском."""

REVIEW_SYSTEM = """Ты эксперт по тендерам. Внеси правки в документ и верни ПОЛНЫЙ исправленный текст. Только текст документа."""

CRITERIA_PREVIEW_SYSTEM = """Ты эксперт по тендерам. Составь список критериев допуска.
Выведи нумерованный список, каждый критерий одной строкой:
1. Опыт работы от 3 лет
2. Наличие лицензии
Выведи 5-10 критериев. Только список."""

# ─── Переговоры — вопросы с кнопками ────────────────────────────────────────

NEGOTIATION_STEPS = [
    {
        "question": "Что закупаем?",
        "options": ["Клининговые услуги", "IT-услуги", "Ремонт оборудования", "Строительные работы", "Поставка товаров"],
        "free": True
    },
    {
        "question": "Кто придёт от участника на переговоры?",
        "options": ["Директор / собственник", "Коммерческий директор", "Менеджер по продажам", "Неизвестно"],
        "free": False
    },
    {
        "question": "Какова начальная цена контракта (НМЦ)?",
        "options": ["До 1 млн руб.", "1–5 млн руб.", "5–20 млн руб.", "Более 20 млн руб."],
        "free": True
    },
    {
        "question": "На сколько процентов хотим снизить цену?",
        "options": ["На 5–10%", "На 10–20%", "На 20–30%", "Максимально возможно"],
        "free": False
    },
    {
        "question": "Есть ли у нас альтернативные участники (конкуренты этого поставщика)?",
        "options": ["Да, есть 2+ конкурента", "Есть 1 альтернатива", "Нет, этот единственный"],
        "free": False
    },
    {
        "question": "Какие дополнительные улучшения условий нам важны?",
        "options": [
            "Только снижение цены",
            "Цена + сроки выполнения",
            "Цена + гарантийные обязательства",
            "Цена + объём работ",
        ],
        "free": False
    },
]

NEGOTIATION_SYSTEM = """Ты эксперт по закупочным переговорам. Твоя задача — составить практичный сценарий переговоров с участником тендера.

ГЛАВНАЯ ЦЕЛЬ ВСЕГДА: снижение цены и улучшение условий контракта.

Сценарий должен быть конкретным и без воды:
1. ПОЗИЦИЯ ЗАКУПЩИКА — с чем идём на переговоры (наши козыри)
2. ОТКРЫТИЕ ПЕРЕГОВОРОВ — первая фраза, как начать (2-3 варианта)
3. КЛЮЧЕВЫЕ АРГУМЕНТЫ — конкретные фразы для давления на снижение цены
4. РАБОТА С ВОЗРАЖЕНИЯМИ — топ-3 возражения участника и точные ответы на них
5. ЗАКРЫТИЕ СДЕЛКИ — как зафиксировать договорённость

Никакой воды. Только конкретные фразы, аргументы и тактики.
Пиши на русском, деловой стиль."""


async def get_text(update: Update) -> str | None:
    if update.message.text:
        return update.message.text.strip()
    if update.message.voice:
        await update.message.reply_text("🎤 Слушаю...")
        file = await update.message.voice.get_file()
        data = await file.download_as_bytearray()
        text = await transcribe_voice(bytes(data))
        await update.message.reply_text(f'Услышал: "{text}"')
        return text
    return None


async def get_image_base64(update: Update) -> tuple[str, str] | None:
    photo = None
    if update.message.photo:
        photo = update.message.photo[-1]
    elif update.message.document and update.message.document.mime_type and \
         update.message.document.mime_type.startswith("image/"):
        photo = update.message.document
    if not photo:
        return None
    file = await photo.get_file()
    data = await file.download_as_bytearray()
    b64 = base64.standard_b64encode(bytes(data)).decode("utf-8")
    mime = getattr(photo, "mime_type", None) or "image/jpeg"
    return b64, mime


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Техническое задание (ТЗ)", callback_data="menu_tz")],
        [InlineKeyboardButton("📋 Критерии допуска", callback_data="menu_criteria")],
        [InlineKeyboardButton("📄+📋 ТЗ и критерии", callback_data="menu_both")],
        [InlineKeyboardButton("🤝 Сценарий переговоров", callback_data="menu_negotiation")],
    ])


def direction_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("💻 IT-услуги", callback_data="dir_it")],
        [InlineKeyboardButton("🔧 Ремонт оборудования", callback_data="dir_repair")],
    ])


def review_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Всё отлично!", callback_data="review_ok")],
        [InlineKeyboardButton("✏️ Есть замечания", callback_data="review_edit")],
    ])


def criteria_main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подходит, генерируй!", callback_data="criteria_go")],
        [InlineKeyboardButton("➕ Добавить критерий", callback_data="criteria_add")],
        [InlineKeyboardButton("🗑 Убрать критерий", callback_data="criteria_remove")],
    ])


def criteria_remove_kb(criteria_list: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for i, c in enumerate(criteria_list):
        short = c[:45] + "..." if len(c) > 45 else c
        buttons.append([InlineKeyboardButton(f"❌ {short}", callback_data=f"del_criterion_{i}")])
    buttons.append([InlineKeyboardButton("✅ Готово, генерируй!", callback_data="criteria_go")])
    return InlineKeyboardMarkup(buttons)


def negotiation_step_kb(step_idx: int, allow_custom: bool = True) -> InlineKeyboardMarkup:
    """Клавиатура для конкретного шага переговоров."""
    step = NEGOTIATION_STEPS[step_idx]
    buttons = [[InlineKeyboardButton(opt, callback_data=f"neg_{step_idx}_{i}")]
               for i, opt in enumerate(step["options"])]
    if allow_custom and step.get("free"):
        buttons.append([InlineKeyboardButton("✏️ Ввести свой вариант", callback_data=f"neg_{step_idx}_custom")])
    return InlineKeyboardMarkup(buttons)


def parse_criteria_list(text: str) -> list[str]:
    lines = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        cleaned = line.lstrip("0123456789").lstrip(". ").strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def format_criteria_list(criteria: list[str]) -> str:
    return "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))


async def send_question(msg, result: dict):
    text = result["question"]
    options = result.get("options", [])
    if options:
        kb = [[InlineKeyboardButton(o, callback_data=f"ans_{i}")] for i, o in enumerate(options)]
        kb.append([InlineKeyboardButton("✏️ Свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text)


# ─── Команды ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ У тебя нет доступа. Обратись к администратору.")
        return
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None)
    context.user_data.clear()
    await update.message.reply_text(
        "О, живой! Привет 😄\n\n"
        "Я Макс — твой личный помощник по тендерам. Вот что умею:\n\n"
        "📄 ТЗ, критерии, сценарии переговоров → /new\n"
        "💬 Отвечу на любой вопрос\n"
        "🔍 Найду информацию в интернете\n"
        "📷 Распознаю текст с фото\n"
        "✉️ Составлю ответное письмо\n"
        "📝 Сохраню любое моё сообщение в Word — просто скажи «сохрани»\n\n"
        "Или просто напиши что нужно — сам разберусь 😏"
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Нет доступа.")
        return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None)
    context.user_data.clear()
    await update.message.reply_text("Ладно, что делаем? 😄", reply_markup=main_menu_kb())
    return CHOOSING


async def cb_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "menu_negotiation":
        context.user_data["negotiation_answers"] = {}
        context.user_data["negotiation_step"] = 0
        step = NEGOTIATION_STEPS[0]
        await q.edit_message_text(
            f"Сценарий переговоров 🤝\n\n{step['question']}",
            reply_markup=negotiation_step_kb(0)
        )
        return NEGOTIATION

    doc_map = {"menu_tz": "tz_only", "menu_criteria": "criteria_only", "menu_both": "both"}
    context.user_data["doc_type"] = doc_map.get(q.data, "tz_only")
    await q.edit_message_text("Выбери направление закупки:", reply_markup=direction_kb())
    return CHOOSING


# ─── Переговоры ─────────────────────────────────────────────────────────────

async def cb_negotiation_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие кнопки в переговорном вопросе."""
    q = update.callback_query
    await q.answer()

    parts = q.data.split("_")  # neg_STEP_IDX
    step_idx = int(parts[1])
    choice = parts[2]

    if choice == "custom":
        await q.edit_message_text(
            NEGOTIATION_STEPS[step_idx]["question"] + "\n\nВведи свой вариант:"
        )
        context.user_data["negotiation_custom_step"] = step_idx
        return NEGOTIATION

    # Сохраняем ответ
    step = NEGOTIATION_STEPS[step_idx]
    answer = step["options"][int(choice)]
    await q.edit_message_text(f"{step['question']}\n→ {answer}")

    return await _save_neg_answer_and_next(q.message, context, step_idx, answer)


async def negotiation_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текстовый ответ в переговорах (свой вариант)."""
    text = await get_text(update)
    if not text:
        return NEGOTIATION

    step_idx = context.user_data.get("negotiation_custom_step")
    if step_idx is None:
        # Если нет custom_step — просто переходим к следующему
        step_idx = context.user_data.get("negotiation_step", 0)

    context.user_data.pop("negotiation_custom_step", None)
    return await _save_neg_answer_and_next(update.message, context, step_idx, text)


async def _save_neg_answer_and_next(msg, context: ContextTypes.DEFAULT_TYPE, step_idx: int, answer: str):
    """Сохраняет ответ и переходит к следующему вопросу или генерирует сценарий."""
    answers = context.user_data.get("negotiation_answers", {})
    answers[step_idx] = {
        "question": NEGOTIATION_STEPS[step_idx]["question"],
        "answer": answer
    }
    context.user_data["negotiation_answers"] = answers

    next_step = step_idx + 1
    context.user_data["negotiation_step"] = next_step

    if next_step < len(NEGOTIATION_STEPS):
        step = NEGOTIATION_STEPS[next_step]
        await msg.reply_text(step["question"], reply_markup=negotiation_step_kb(next_step))
        return NEGOTIATION
    else:
        await msg.reply_text("Отлично! Составляю сценарий... 🤝")
        return await generate_negotiation(msg, context)


async def generate_negotiation(msg, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует сценарий переговоров."""
    from docx_generator import generate_tz_docx
    uid = msg.chat_id
    answers = context.user_data.get("negotiation_answers", {})

    context_lines = []
    for i in range(len(NEGOTIATION_STEPS)):
        if i in answers:
            context_lines.append(f"{answers[i]['question']}: {answers[i]['answer']}")
    context_text = "\n".join(context_lines)

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=NEGOTIATION_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Данные для сценария переговоров:\n{context_text}\n\n"
                    "Составь конкретный сценарий переговоров. "
                    "Никакой воды — только конкретные фразы и тактики снижения цены."
                )
            }]
        )

        content = response.content[0].text
        remember_bot_message(uid, content)
        last_doc[uid] = {"content": content, "type": "negotiation", "name": "Сценарий переговоров"}

        preview = content[:2500] + ("\n...(показан фрагмент)" if len(content) > 2500 else "")
        await msg.reply_text(f"🤝 Сценарий переговоров:\n\n{preview}")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Сохранить в Word", callback_data="save_negotiation")],
            [InlineKeyboardButton("✏️ Есть замечания", callback_data="review_edit")],
            [InlineKeyboardButton("✅ Всё отлично", callback_data="review_ok")],
        ])
        await msg.reply_text("Сохранить в файл?", reply_markup=kb)
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка сценария: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 → /new")
        return ConversationHandler.END


async def cb_save_negotiation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    doc_info = last_doc.get(uid, {})
    if not doc_info:
        await q.edit_message_text("Не нашёл документ 😕")
        return ConversationHandler.END
    await q.edit_message_text("📥 Создаю файл...")
    try:
        path = await generate_tz_docx(doc_info["content"], "Сценарий переговоров")
        with open(path, "rb") as f:
            await q.message.reply_document(document=f, filename="Сценарий_переговоров.docx", caption="🤝 Готово! Удачи 😄")
        os.remove(path)
        sessions.pop(uid, None)
        last_doc.pop(uid, None)
        await q.message.reply_text("Для нового запроса → /new")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await q.message.reply_text("Ошибка 😕")
        return ConversationHandler.END


# ─── Сохранение любого сообщения в Word ─────────────────────────────────────

async def save_to_word_any(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_save: str = None):
    """Сохраняет указанный текст или последнее сообщение бота в Word."""
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id

    content = text_to_save

    if not content:
        # Берём последнее сообщение бота из памяти
        messages = bot_messages.get(uid, [])
        if messages:
            content = messages[-1]

    if not content:
        # Fallback — берём из истории чата
        history = context.user_data.get("chat_history", [])
        for msg in reversed(history):
            if msg["role"] == "assistant" and isinstance(msg["content"], str):
                content = msg["content"]
                break

    if not content:
        await update.message.reply_text("Не нашёл что сохранять 🤔 Сначала задай вопрос!")
        return

    await update.message.reply_text("📥 Сохраняю в Word...")
    try:
        path = await generate_tz_docx(content, "Документ")
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename="Документ.docx", caption="📄 Готово! 😊")
        os.remove(path)
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await update.message.reply_text("Ошибка 😕")


async def cb_save_to_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # Подменяем update.message для совместимости
    class FakeUpdate:
        message = q.message
        effective_user = update.effective_user
    await save_to_word_any(FakeUpdate(), context)


# ─── Чат ────────────────────────────────────────────────────────────────────

async def chat_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    save_user(uid, update.effective_user.username or "")

    if update.message.photo or (
        update.message.document and update.message.document.mime_type and
        update.message.document.mime_type.startswith("image/")
    ):
        await handle_photo(update, context)
        return

    text = await get_text(update)
    if not text:
        return

    if context.user_data.get("waiting_letter_instructions"):
        await generate_letter_reply(update, context)
        return

    tl = text.lower()

    # Триггеры сохранения в Word
    save_triggers = ["сохрани", "сохрани это", "сохрани в файл", "в ворд", "сделай файл", "скачать"]
    if any(w in tl for w in save_triggers):
        await save_to_word_any(update, context)
        return

    photo_context = context.user_data.get("last_photo_description", "")
    system = CHAT_SYSTEM
    if photo_context:
        system += f"\n\nКОНТЕКСТ ФОТО: {photo_context}"

    history = context.user_data.get("chat_history", [])
    history.append({"role": "user", "content": text})
    if len(history) > 20:
        history = history[-20:]

    await update.message.reply_text("⏳ Думаю...")

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=system,
            tools=[WEB_SEARCH_TOOL],
            messages=history,
        )

        messages_list = list(history)
        while response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": block.input.get("query", "")})
            messages_list.append({"role": "assistant", "content": response.content})
            messages_list.append({"role": "user", "content": tool_results})
            response = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=2000, system=system,
                tools=[WEB_SEARCH_TOOL], messages=messages_list,
            )

        reply = "".join(b.text for b in response.content if hasattr(b, "text"))
        if not reply:
            reply = "Хм, не знаю что ответить 🤔"

        # Запоминаем ответ бота для сохранения
        remember_bot_message(uid, reply)

        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history

        if len(reply) > 4000:
            reply = reply[:4000] + "...\n(сократил 😄)"

        await update.message.reply_text(reply)

        # Если ответ длинный — предлагаем сохранить
        if len(reply) > 500:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Сохранить в Word", callback_data="save_to_word")],
            ])
            await update.message.reply_text("Сохранить в файл? 📄", reply_markup=kb)

    except Exception as e:
        logger.error(f"Ошибка чата: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось 😅 Попробуй ещё раз.")


# ─── Фото ───────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📷 Смотрю... 🔍 Ищу инфу...")
    result = await get_image_base64(update)
    if not result:
        await update.message.reply_text("Не смог получить фото 😕")
        return
    b64, mime = result
    caption = update.message.caption or ""
    try:
        vision_response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": (
                    "Проанализируй:\n1. Опиши что изображено\n2. Распознай текст если есть\n"
                    "3. Предложи поисковый запрос\n"
                    f"{'Запрос: ' + caption if caption else ''}\n\n"
                    "Формат:\nОПИСАНИЕ: ...\nТЕКСТ: ...\nПОИСК: ..."
                )}
            ]}]
        )
        vision_text = vision_response.content[0].text
        description, ocr_text, search_query = "", "", caption or "информация по изображению"
        for line in vision_text.split("\n"):
            if line.startswith("ОПИСАНИЕ:"):
                description = line.replace("ОПИСАНИЕ:", "").strip()
            elif line.startswith("ТЕКСТ:"):
                ocr_text = line.replace("ТЕКСТ:", "").strip()
            elif line.startswith("ПОИСК:"):
                search_query = line.replace("ПОИСК:", "").strip()

        photo_memory = description
        if ocr_text and ocr_text != "текста нет":
            photo_memory += f"\nТекст: {ocr_text}"
        context.user_data["last_photo_description"] = photo_memory
        context.user_data["last_photo_text"] = ocr_text if ocr_text != "текста нет" else ""

        search_response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500, tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": f"Найди: {search_query}"}]
        )
        msgs = [{"role": "user", "content": f"Найди: {search_query}"}]
        while search_response.stop_reason == "tool_use":
            tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query", "")}
                  for b in search_response.content if b.type == "tool_use"]
            msgs.append({"role": "assistant", "content": search_response.content})
            msgs.append({"role": "user", "content": tr})
            search_response = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=1500, tools=[WEB_SEARCH_TOOL], messages=msgs)
        search_text = "".join(b.text for b in search_response.content if hasattr(b, "text"))

        parts = []
        if description:
            parts.append(f"👁 {description}")
        if ocr_text and ocr_text != "текста нет":
            parts.append(f"\n📝 Текст:\n{ocr_text}")
        if search_text:
            parts.append(f"\n🔍 В интернете:\n{search_text}")
        final = "\n".join(parts)
        if len(final) > 4000:
            final = final[:4000] + "..."

        uid = update.effective_user.id
        remember_bot_message(uid, final)
        await update.message.reply_text(final)

        all_text = (description + ocr_text).lower()
        if any(w in all_text for w in ["уважаем", "прошу", "сообщаем", "исх.", "вх.", "настоящим"]):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Составить ответ", callback_data="write_reply")]])
            await update.message.reply_text("Похоже на письмо 📄 Составить ответ?", reply_markup=kb)

    except Exception as e:
        logger.error(f"Ошибка фото: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать 😕")


async def cb_photo_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "write_reply":
        await q.edit_message_text("Напиши что ответить — составлю письмо ✉️")
        context.user_data["waiting_letter_instructions"] = True
        context.user_data["letter_original"] = context.user_data.get("last_photo_text", "")


async def generate_letter_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx
    instructions = await get_text(update)
    original = context.user_data.get("letter_original", "")
    context.user_data.pop("waiting_letter_instructions", None)
    await update.message.reply_text("✉️ Составляю...")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000,
            system="Составляешь официальные деловые письма на русском. Профессионально.",
            messages=[{"role": "user", "content": f"Оригинал:\n{original}\n\nИнструкции:\n{instructions}\n\nСоставь ответ."}]
        )
        path = await generate_tz_docx(response.content[0].text, "Ответное письмо")
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename="Ответное_письмо.docx", caption="✉️ Готово!")
        os.remove(path)
    except Exception as e:
        logger.error(f"Ошибка письма: {e}", exc_info=True)
        await update.message.reply_text("Не смог составить 😕")


# ─── Правки документов ──────────────────────────────────────────────────────

async def cb_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    if q.data == "review_ok":
        await q.edit_message_text("Значит не зря старался 😄 → /new")
        sessions.pop(uid, None)
        last_doc.pop(uid, None)
        return ConversationHandler.END
    elif q.data == "review_edit":
        await q.edit_message_text("Слушаю замечания 📝")
        return REVIEWING


async def apply_edits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx
    uid = update.effective_user.id
    edits = await get_text(update)
    doc_info = last_doc.get(uid, {})
    if not doc_info:
        await update.message.reply_text("Не нашёл документ 😕 → /new")
        return ConversationHandler.END
    await update.message.reply_text("✏️ Вношу правки...")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": f"Документ:\n\n{doc_info['content']}\n\nПравки:\n{edits}\n\nВерни исправленный."}]
        )
        new_content = response.content[0].text
        last_doc[uid]["content"] = new_content
        doc_type = doc_info.get("type", "tz")
        name = doc_info.get("name", "Документ")
        if doc_type == "criteria":
            path = await generate_criteria_docx(new_content, name)
            filename, caption = f"Критерии_{name[:35]}.docx", "📋 Исправлено!"
        elif doc_type == "negotiation":
            path = await generate_tz_docx(new_content, name)
            filename, caption = "Сценарий_переговоров.docx", "🤝 Исправлено!"
        else:
            path = await generate_tz_docx(new_content, name)
            filename, caption = f"ТЗ_{name[:40]}.docx", "📄 Исправлено!"
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename=filename, caption=caption)
        os.remove(path)
        await update.message.reply_text("Теперь устраивает? 😊", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Ошибка правок: {e}", exc_info=True)
        await update.message.reply_text("Ошибка 😕")
        return REVIEWING


# ─── Превью критериев ───────────────────────────────────────────────────────

async def show_criteria_preview(msg, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    response = claude.messages.create(
        model="claude-sonnet-4-5", max_tokens=800, system=CRITERIA_PREVIEW_SYSTEM,
        messages=[{"role": "user", "content": f"Данные:\n{agent._context()}"}]
    )
    criteria_list = parse_criteria_list(response.content[0].text)
    context.user_data["criteria_list"] = criteria_list
    await msg.reply_text(
        f"Планирую включить в критерии:\n\n{format_criteria_list(criteria_list)}\n\nКак тебе? 🤔",
        reply_markup=criteria_main_kb()
    )
    return CRITERIA_CONFIRM


async def cb_criteria_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    criteria_list = context.user_data.get("criteria_list", [])
    if q.data == "criteria_go":
        await q.edit_message_text("Генерирую... ⚙️")
        return await generate_criteria_doc(q.message, uid, agent, context)
    elif q.data == "criteria_add":
        await q.edit_message_text(f"Список:\n\n{format_criteria_list(criteria_list)}\n\nЧто добавить? 📝")
        context.user_data["criteria_action"] = "add"
        return CRITERIA_CONFIRM
    elif q.data == "criteria_remove":
        await q.edit_message_text(
            f"Нажми чтобы убрать:\n\n{format_criteria_list(criteria_list)}",
            reply_markup=criteria_remove_kb(criteria_list)
        )
        return CRITERIA_CONFIRM


async def cb_delete_criterion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    idx = int(q.data.replace("del_criterion_", ""))
    criteria_list = context.user_data.get("criteria_list", [])
    if 0 <= idx < len(criteria_list):
        criteria_list.pop(idx)
        context.user_data["criteria_list"] = criteria_list
    await q.edit_message_text(
        f"Убрал! Список:\n\n{format_criteria_list(criteria_list)}",
        reply_markup=criteria_remove_kb(criteria_list)
    )
    return CRITERIA_CONFIRM


async def handle_criteria_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_text(update)
    criteria_list = context.user_data.get("criteria_list", [])
    response = claude.messages.create(
        model="claude-sonnet-4-5", max_tokens=200,
        messages=[{"role": "user", "content": f"Сформулируй критерий допуска в 3-7 словах: '{text}'. Только текст."}]
    )
    criteria_list.append(response.content[0].text.strip().strip("."))
    context.user_data["criteria_list"] = criteria_list
    context.user_data.pop("criteria_action", None)
    await update.message.reply_text(
        f"Добавил!\n\n{format_criteria_list(criteria_list)}\n\nЕщё что-то? 😊",
        reply_markup=criteria_main_kb()
    )
    return CRITERIA_CONFIRM


async def generate_criteria_doc(msg, uid: int, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_criteria_docx
    from examples_loader import load_examples
    criteria_list = context.user_data.get("criteria_list", [])
    approved = format_criteria_list(criteria_list)
    examples_text = ""
    texts = load_examples("criteria")
    if texts:
        examples_text = "ПРИМЕРЫ:\n\n" + "".join(f"=== {i} ===\n{t[:2000]}\n\n" for i, t in enumerate(texts[:3], 1))
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=3000,
            system=f"Эксперт по закупкам. Составляешь 'Критерии допуска'.\n\n{examples_text}",
            messages=[{"role": "user", "content": f"Данные:\n{agent._context()}\n\nОбязательные критерии:\n{approved}\n\nСоставь документ. Начни с 'КРИТЕРИИ ДОПУСКА'."}]
        )
        content = response.content[0].text
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        path = await generate_criteria_docx(content, agent.tender_name)
        with open(path, "rb") as f:
            await msg.reply_document(document=f, filename=f"Критерии_{agent.tender_name[:35]}.docx", caption="📋 Готово!")
        os.remove(path)
        await msg.reply_text("Всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 → /new")
        return ConversationHandler.END


# ─── Создание ТЗ ────────────────────────────────────────────────────────────

async def cb_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["direction"] = q.data.replace("dir_", "")
    return await begin_questions(update, context, from_cb=True)


async def begin_questions(update: Update, context: ContextTypes.DEFAULT_TYPE, from_cb=False):
    uid = update.effective_user.id
    agent = TenderAgent(
        direction=context.user_data.get("direction", "cleaning"),
        doc_type=context.user_data.get("doc_type", "tz_only"),
    )
    sessions[uid] = agent
    result = await agent.get_next_question()
    msg = update.callback_query.message if from_cb else update.message
    await send_question(msg, result)
    return ANSWERING


async def cb_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent:
        await q.edit_message_text("Сессия устарела → /new")
        return ConversationHandler.END
    if q.data == "ans_custom":
        await q.edit_message_text(q.message.text + "\n\nВведи свой вариант:")
        return ANSWERING
    idx = int(q.data.replace("ans_", ""))
    options = agent.last_question.get("options", [])
    answer = options[idx] if idx < len(options) else ""
    await q.edit_message_text(f"{agent.last_question['question']}\n→ {answer}")
    return await handle_answer(update, context, answer)


async def text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent:
        await update.message.reply_text("Сессия устарела → /new")
        return ConversationHandler.END
    text = await get_text(update)
    if not text:
        return ANSWERING
    return await handle_answer(update, context, text)


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: str):
    uid = update.effective_user.id
    agent = sessions[uid]
    result = await agent.submit_answer(answer)
    msg = update.callback_query.message if update.callback_query else update.message
    if result["status"] == "question":
        await send_question(msg, result)
        return ANSWERING
    else:
        await msg.reply_text("Данные собраны! Генерирую... ⚙️")
        return await do_generate(update, context)


async def do_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id
    agent = sessions[uid]
    msg = update.callback_query.message if update.callback_query else update.message
    try:
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            last_doc[uid] = {"content": content, "type": "tz", "name": agent.tender_name}
            path = await generate_tz_docx(content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(document=f, filename=f"ТЗ_{agent.tender_name[:40]}.docx", caption="📄 ТЗ готово!")
            os.remove(path)
        if agent.doc_type in ("criteria_only", "both"):
            await msg.reply_text("Покажу что планирую включить в критерии... 🤔")
            return await show_criteria_preview(msg, agent, context)
        if agent.doc_type == "tz_only":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Да, добавить критерии", callback_data="yes_criteria")],
                [InlineKeyboardButton("✅ Нет, всё готово", callback_data="no_criteria")],
            ])
            await msg.reply_text("Нужны критерии допуска? 🤔", reply_markup=kb)
            return CRITERIA_Q
        await msg.reply_text("Всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 → /new")
        return ConversationHandler.END


async def cb_criteria_q(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if q.data == "no_criteria":
        await q.edit_message_text("Всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING
    if q.data == "yes_criteria":
        await q.edit_message_text("Покажу что планирую... 🤔")
        return await show_criteria_preview(q.message, agent, context)


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("⛔ Только для администратора.")
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Напиши текст:\n/broadcast Привет! Бот обновился...")
        return
    users = get_all_users()
    sent, failed = 0, 0
    await update.message.reply_text(f"Рассылаю {len(users)} пользователям...")
    for user_id_str in users:
        try:
            await context.bot.send_message(chat_id=int(user_id_str), text=text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Готово! ✅ Отправлено: {sent}, не доставлено: {failed}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None)
    last_doc.pop(uid, None)
    context.user_data.clear()
    await update.message.reply_text("Отменено 👌 Пиши если что!")
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND
    photo_f = filters.PHOTO | filters.Document.IMAGE

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_request)],
        states={
            CHOOSING: [
                CallbackQueryHandler(cb_main_menu, pattern="^menu_"),
                CallbackQueryHandler(cb_direction, pattern="^dir_"),
            ],
            NEGOTIATION: [
                CallbackQueryHandler(cb_negotiation_answer, pattern="^neg_\\d+_"),
                MessageHandler(tv, negotiation_text_answer),
            ],
            ANSWERING: [
                CallbackQueryHandler(cb_answer, pattern="^ans_"),
                MessageHandler(tv, text_answer),
            ],
            CRITERIA_Q: [
                CallbackQueryHandler(cb_criteria_q, pattern="^(yes|no)_criteria$"),
            ],
            CRITERIA_CONFIRM: [
                CallbackQueryHandler(cb_criteria_confirm, pattern="^criteria_(go|add|remove)$"),
                CallbackQueryHandler(cb_delete_criterion, pattern="^del_criterion_\\d+$"),
                MessageHandler(tv, handle_criteria_edit),
            ],
            REVIEWING: [
                CallbackQueryHandler(cb_review, pattern="^review_(ok|edit)$"),
                CallbackQueryHandler(cb_save_negotiation, pattern="^save_negotiation$"),
                MessageHandler(tv, apply_edits),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(conv)
    app.add_handler(MessageHandler(photo_f, chat_reply))
    app.add_handler(CallbackQueryHandler(cb_photo_actions, pattern="^write_reply$"))
    app.add_handler(CallbackQueryHandler(cb_save_to_word, pattern="^save_to_word$"))
    app.add_handler(MessageHandler(tv, chat_reply))

    logger.info("Бот v8 запущен 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
