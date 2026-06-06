"""
Telegram-бот v6 — ТЗ, критерии, сценарии переговоров, белый список, рассылка.
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

# Состояния диалога
CHOOSING = 1
ANSWERING = 2
CRITERIA_Q = 3
REVIEWING = 4
CRITERIA_CONFIRM = 5
NEGOTIATION = 6  # Сценарий переговоров

sessions: dict[int, TenderAgent] = {}
last_doc: dict[int, dict] = {}
negotiation_sessions: dict[int, list] = {}  # История вопросов для переговоров

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

# ─── Белый список ───────────────────────────────────────────────────────────
# Добавь сюда Telegram ID разрешённых пользователей
# Узнать свой ID: написать @userinfobot в Telegram
ALLOWED_USERS_ENV = os.environ.get("ALLOWED_USERS", "")
ALLOWED_USERS: set[int] = set()
if ALLOWED_USERS_ENV:
    for uid_str in ALLOWED_USERS_ENV.split(","):
        try:
            ALLOWED_USERS.add(int(uid_str.strip()))
        except ValueError:
            pass

# Файл для хранения всех пользователей (для рассылки)
USERS_FILE = "/tmp/bot_users.json"


def save_user(user_id: int, username: str = ""):
    """Сохраняет пользователя в файл для рассылки."""
    try:
        users = {}
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f:
                users = json.load(f)
        users[str(user_id)] = username
        with open(USERS_FILE, "w") as f:
            json.dump(users, f)
    except Exception as e:
        logger.error(f"Ошибка сохранения пользователя: {e}")


def get_all_users() -> dict:
    """Возвращает всех сохранённых пользователей."""
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def is_allowed(user_id: int) -> bool:
    """Проверяет есть ли пользователь в белом списке."""
    if not ALLOWED_USERS:  # Если список пустой — пускаем всех
        return True
    return user_id in ALLOWED_USERS


# ─── Промпты ────────────────────────────────────────────────────────────────

CHAT_SYSTEM = """Ты — Макс, дерзкий и остроумный помощник по тендерам. Ты как лучший друг на работе — подкалываешь, шутишь, но всегда делаешь всё качественно.

ВАЖНО: Ты помнишь весь контекст разговора включая фото. Если пользователь прислал фото — используй эту информацию.

ВАЖНО: Если пользователь просит создать файл, документ, сохранить что-то в Word — ВСЕГДА предложи это сделать. Скажи "Сделать файл Word?" и жди подтверждения или сразу делай.

Ты умеешь:
- Составлять ТЗ и критерии допуска → /new или просто скажи
- Составлять сценарии переговоров с участниками тендера → /negotiation или просто скажи
- Отвечать на любые вопросы
- Искать в интернете
- Распознавать текст с фото и составлять ответные письма
- Сохранять ЛЮБОЙ текст в Word файл по запросу

Твои коронные фразы:
- "Слушай, а ты сам не пробовал? Нет? Ну тогда ладно 😏"
- "Опять ты... Ну давай 😄"
- "А самому слабо? Понятно 🙄"
- "Конец рабочего дня, между прочим! Но ладно 😴"
- "Это уже третий раз за сегодня 😂"
- "О, снова тендеры. Моя любимая тема. Нет. Но раз надо 🫠"
- "Ты серьёзно? Окей, без осуждения 😅"
- "Элементарно. Хотя не буду говорить 😏"

Используй смайлики активно. Отвечай на русском.

ТРИГГЕРЫ для запуска создания документов (реагируй на них):
- "нужно ТЗ", "сделай ТЗ", "техническое задание", "тендер на" → предложи /new или сразу начни
- "критерии допуска", "критерии участника" → предложи создать критерии
- "сценарий переговоров", "переговоры с участником", "как провести переговоры" → предложи /negotiation
- "сохрани", "сделай файл", "в ворд", "скачать" → сохрани последний ответ в Word"""

REVIEW_SYSTEM = """Ты эксперт по тендерам. Внеси правки в документ и верни ПОЛНЫЙ исправленный текст. Только текст документа."""

CRITERIA_PREVIEW_SYSTEM = """Ты эксперт по тендерам. Составь список критериев допуска.
Выведи нумерованный список, каждый критерий одной строкой:
1. Опыт работы от 3 лет
2. Наличие лицензии
Выведи 5-10 критериев. Только список."""

NEGOTIATION_SYSTEM = """Ты эксперт по проведению переговоров в сфере тендеров и закупок.
Составляешь профессиональные сценарии переговоров с участниками тендера.
Сценарий должен включать:
- Цель переговоров
- Подготовительный этап
- Ключевые вопросы к участнику
- Возможные возражения и ответы на них
- Критерии оценки участника по итогам переговоров
- Рекомендуемые формулировки
Пиши на русском, профессионально и структурированно."""


# ─── Вспомогательные функции ────────────────────────────────────────────────

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


def check_access(func):
    """Декоратор для проверки доступа."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        uid = update.effective_user.id
        if not is_allowed(uid):
            await update.message.reply_text(
                "⛔ У тебя нет доступа к этому боту.\n"
                "Обратись к администратору."
            )
            return
        save_user(uid, update.effective_user.username or "")
        return await func(update, context, *args, **kwargs)
    return wrapper


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def direction_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("💻 IT-услуги", callback_data="dir_it")],
        [InlineKeyboardButton("🔧 Ремонт оборудования", callback_data="dir_repair")],
    ])


def doctype_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Только ТЗ", callback_data="doc_tz_only")],
        [InlineKeyboardButton("📋 Только критерии допуска", callback_data="doc_criteria_only")],
        [InlineKeyboardButton("📄+📋 ТЗ и критерии", callback_data="doc_both")],
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

@check_access
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(
        "О, живой! Привет 😄\n\n"
        "Я Макс — твой личный помощник по тендерам. Вот что умею:\n\n"
        "📄 /new — ТЗ и критерии допуска\n"
        "🤝 /negotiation — сценарий переговоров с участником\n"
        "💬 Просто пиши — отвечу на любой вопрос\n"
        "🔍 Ищу информацию в интернете\n"
        "📷 Распознаю текст с фото\n"
        "✉️ Составлю ответное письмо\n"
        "📝 Сохраню любой текст в Word — просто скажи\n\n"
        "Или просто напиши что нужно — сам разберусь 😏"
    )


@check_access
async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(
        "Опять тендеры 🙄 Ладно. Выбирай направление:",
        reply_markup=direction_kb()
    )
    return CHOOSING


@check_access
async def negotiation_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало сценария переговоров."""
    uid = update.effective_user.id
    negotiation_sessions[uid] = []
    context.user_data["negotiation_answers"] = {}

    await update.message.reply_text(
        "Сценарий переговоров — это серьёзно 🤝\n"
        "Задам пару вопросов чтобы сделать его под твою ситуацию.\n\n"
        "Первый вопрос: *Что закупаем?* Кратко опиши предмет тендера:"
    )
    context.user_data["negotiation_step"] = 0
    return NEGOTIATION


NEGOTIATION_QUESTIONS = [
    "Что закупаем? Кратко опиши предмет тендера:",
    "Сколько участников приглашено на переговоры?",
    "Какова начальная максимальная цена контракта?",
    "Есть ли особые требования или болевые точки в этой закупке?",
    "Какова главная цель переговоров — снижение цены, проверка компетенций или что-то другое?",
]


async def negotiation_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ответы на вопросы для сценария переговоров."""
    uid = update.effective_user.id
    text = await get_text(update)
    if not text:
        return NEGOTIATION

    step = context.user_data.get("negotiation_step", 0)
    answers = context.user_data.get("negotiation_answers", {})
    answers[NEGOTIATION_QUESTIONS[step]] = text
    context.user_data["negotiation_answers"] = answers
    step += 1
    context.user_data["negotiation_step"] = step

    if step < len(NEGOTIATION_QUESTIONS):
        await update.message.reply_text(NEGOTIATION_QUESTIONS[step])
        return NEGOTIATION
    else:
        # Все вопросы заданы — генерируем сценарий
        await update.message.reply_text("Отлично! Составляю сценарий переговоров... 🤝")
        return await generate_negotiation(update, context)


async def generate_negotiation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует сценарий переговоров и предлагает сохранить в Word."""
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id
    answers = context.user_data.get("negotiation_answers", {})

    context_text = "\n".join(f"{q}: {a}" for q, a in answers.items())

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=NEGOTIATION_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Составь сценарий переговоров на основе данных:\n\n{context_text}"
            }]
        )

        content = response.content[0].text
        context.user_data["last_negotiation"] = content

        # Показываем превью в чате
        preview = content[:2000] + ("..." if len(content) > 2000 else "")
        await update.message.reply_text(f"📋 Вот что получилось:\n\n{preview}")

        # Сохраняем для возможных правок
        last_doc[uid] = {"content": content, "type": "negotiation", "name": "Сценарий переговоров"}

        # Предлагаем сохранить в Word
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Сохранить в Word", callback_data="save_negotiation")],
            [InlineKeyboardButton("✏️ Есть замечания", callback_data="review_edit")],
            [InlineKeyboardButton("✅ Всё отлично", callback_data="review_ok")],
        ])
        await update.message.reply_text("Сохранить в файл? 😊", reply_markup=kb)
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка сценария: {e}", exc_info=True)
        await update.message.reply_text("Что-то пошло не так 😕 → /negotiation")
        return ConversationHandler.END


async def cb_save_negotiation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет сценарий переговоров в Word."""
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
            await q.message.reply_document(
                document=f,
                filename="Сценарий_переговоров.docx",
                caption="🤝 Сценарий переговоров готов!"
            )
        os.remove(path)
        await q.message.reply_text("Удачи на переговорах! Хотя с таким сценарием она и не нужна 😄")
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}", exc_info=True)
        await q.message.reply_text("Ошибка 😕")
        return ConversationHandler.END


# ─── Рассылка (только для администратора) ──────────────────────────────────

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /broadcast — рассылка всем пользователям."""
    uid = update.effective_user.id

    # Проверяем что это администратор (первый в списке разрешённых)
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("⛔ Только для администратора.")
        return

    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text(
            "Напиши текст рассылки после команды:\n"
            "/broadcast Привет! Бот обновился..."
        )
        return

    users = get_all_users()
    sent = 0
    failed = 0

    await update.message.reply_text(f"Рассылаю {len(users)} пользователям...")

    for user_id_str in users:
        try:
            await context.bot.send_message(
                chat_id=int(user_id_str),
                text=text
            )
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"Готово! ✅ Отправлено: {sent}, не доставлено: {failed}"
    )


# ─── Сохранение любого текста в Word ────────────────────────────────────────

async def save_last_to_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет последний ответ бота в Word файл."""
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id

    # Берём последний ответ из истории
    history = context.user_data.get("chat_history", [])
    last_answer = ""
    for msg in reversed(history):
        if msg["role"] == "assistant" and isinstance(msg["content"], str):
            last_answer = msg["content"]
            break

    if not last_answer:
        await update.message.reply_text("Не нашёл что сохранять 🤔 Сначала задай вопрос!")
        return

    await update.message.reply_text("📥 Сохраняю в Word...")
    try:
        path = await generate_tz_docx(last_answer, "Документ")
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="Документ.docx",
                caption="📄 Готово! Держи файл 😊"
            )
        os.remove(path)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}", exc_info=True)
        await update.message.reply_text("Ошибка 😕")


# ─── Чат с распознаванием намерений ─────────────────────────────────────────

@check_access
async def chat_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик — текст, голос."""
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

    # Сохранить в Word
    save_keywords = ["сохрани в файл", "сделай файл", "в ворд", "сохрани в ворд", "скачать", "сохрани это"]
    if any(w in tl for w in save_keywords):
        await save_last_to_word(update, context)
        return

    # Триггеры для создания ТЗ
    tz_keywords = ["нужно тз", "сделай тз", "техническое задание", "тендер на ", "закупка на ", "нужно техническое"]
    if any(w in tl for w in tz_keywords):
        sessions.pop(update.effective_user.id, None)
        context.user_data.clear()
        await update.message.reply_text(
            "О, тендер! Давай сделаем 😄 Выбирай направление:",
            reply_markup=direction_kb()
        )
        return

    # Триггеры для сценария переговоров
    neg_keywords = ["сценарий переговоров", "переговоры с участник", "как провести переговоры", "подготовиться к переговорам"]
    if any(w in tl for w in neg_keywords):
        uid = update.effective_user.id
        negotiation_sessions[uid] = []
        context.user_data["negotiation_answers"] = {}
        context.user_data["negotiation_step"] = 0
        await update.message.reply_text(
            "Переговоры — это я умею 🤝\n"
            "Первый вопрос: что закупаем? Кратко опиши предмет тендера:"
        )
        # Регистрируем обработчик для следующего сообщения
        context.user_data["in_negotiation"] = True
        return

    # Если идут переговорные вопросы вне ConversationHandler
    if context.user_data.get("in_negotiation"):
        step = context.user_data.get("negotiation_step", 0)
        answers = context.user_data.get("negotiation_answers", {})
        answers[NEGOTIATION_QUESTIONS[step]] = text
        context.user_data["negotiation_answers"] = answers
        step += 1
        context.user_data["negotiation_step"] = step

        if step < len(NEGOTIATION_QUESTIONS):
            await update.message.reply_text(NEGOTIATION_QUESTIONS[step])
        else:
            context.user_data.pop("in_negotiation", None)
            await update.message.reply_text("Отлично! Составляю сценарий... 🤝")
            await _generate_negotiation_free(update, context)
        return

    # Обычный чат
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

        messages = list(history)
        while response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": block.input.get("query", ""),
                    })
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            response = claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2000,
                system=system,
                tools=[WEB_SEARCH_TOOL],
                messages=messages,
            )

        reply = "".join(b.text for b in response.content if hasattr(b, "text"))
        if not reply:
            reply = "Хм, не знаю что ответить 🤔"

        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history

        if len(reply) > 4000:
            reply = reply[:4000] + "...\n(сократил 😄)"

        await update.message.reply_text(reply)

        # Если ответ длинный — предлагаем сохранить в Word
        if len(reply) > 500:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📥 Сохранить в Word", callback_data="save_to_word")],
            ])
            await update.message.reply_text("Сохранить в файл? 📄", reply_markup=kb)

    except Exception as e:
        logger.error(f"Ошибка чата: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось 😅 Попробуй ещё раз.")


async def _generate_negotiation_free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует сценарий переговоров из свободного чата."""
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id
    answers = context.user_data.get("negotiation_answers", {})
    context_text = "\n".join(f"{q}: {a}" for q, a in answers.items())

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=NEGOTIATION_SYSTEM,
            messages=[{"role": "user", "content": f"Составь сценарий переговоров:\n\n{context_text}"}]
        )
        content = response.content[0].text
        last_doc[uid] = {"content": content, "type": "negotiation", "name": "Сценарий переговоров"}

        preview = content[:2000] + ("..." if len(content) > 2000 else "")
        await update.message.reply_text(f"🤝 Сценарий:\n\n{preview}")

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Сохранить в Word", callback_data="save_negotiation")],
            [InlineKeyboardButton("✏️ Есть замечания", callback_data="review_edit")],
        ])
        await update.message.reply_text("Сохранить в файл?", reply_markup=kb)

    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await update.message.reply_text("Что-то пошло не так 😕")


async def cb_save_to_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет последний ответ чата в Word."""
    q = update.callback_query
    await q.answer()
    await save_last_to_word(q.message, context)


# ─── Фото ───────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("📷 Смотрю... 🔍 Ищу инфу...")

    result = await get_image_base64(update)
    if not result:
        await update.message.reply_text("Не смог получить фото 😕")
        return

    b64, mime = result
    caption = update.message.caption or ""

    try:
        vision_response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": (
                        "Проанализируй изображение:\n"
                        "1. Опиши подробно что изображено\n"
                        "2. Если есть текст — распознай его полностью\n"
                        "3. Предложи поисковый запрос для поиска доп. информации\n"
                        f"{'Запрос пользователя: ' + caption if caption else ''}\n\n"
                        "Формат:\nОПИСАНИЕ: ...\nТЕКСТ: ...\nПОИСК: ..."
                    )}
                ]
            }]
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
            photo_memory += f"\nТекст на фото: {ocr_text}"
        context.user_data["last_photo_description"] = photo_memory
        context.user_data["last_photo_text"] = ocr_text if ocr_text != "текста нет" else ""

        # Ищем в интернете
        search_response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": f"Найди информацию: {search_query}"}]
        )
        messages = [{"role": "user", "content": f"Найди: {search_query}"}]
        while search_response.stop_reason == "tool_use":
            tool_results = []
            for block in search_response.content:
                if block.type == "tool_use":
                    tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": block.input.get("query", "")})
            messages.append({"role": "assistant", "content": search_response.content})
            messages.append({"role": "user", "content": tool_results})
            search_response = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=1500, tools=[WEB_SEARCH_TOOL], messages=messages,
            )
        search_text = "".join(b.text for b in search_response.content if hasattr(b, "text"))

        reply_parts = []
        if description:
            reply_parts.append(f"👁 {description}")
        if ocr_text and ocr_text != "текста нет":
            reply_parts.append(f"\n📝 Текст на фото:\n{ocr_text}")
        if search_text:
            reply_parts.append(f"\n🔍 Нашёл в интернете:\n{search_text}")

        final_reply = "\n".join(reply_parts)
        if len(final_reply) > 4000:
            final_reply = final_reply[:4000] + "..."
        await update.message.reply_text(final_reply)

        all_text = (description + ocr_text).lower()
        if any(w in all_text for w in ["уважаем", "прошу", "сообщаем", "исх.", "вх.", "настоящим"]):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Составить ответ на письмо", callback_data="write_reply")]])
            await update.message.reply_text("Похоже на официальное письмо 📄 Составить ответ?", reply_markup=kb)

    except Exception as e:
        logger.error(f"Ошибка фото: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать фото 😕")


async def cb_photo_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "write_reply":
        await q.edit_message_text("Напиши что ответить — составлю письмо в Word ✉️")
        context.user_data["waiting_letter_instructions"] = True
        context.user_data["letter_original"] = context.user_data.get("last_photo_text", "")


async def generate_letter_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx
    instructions = await get_text(update)
    original = context.user_data.get("letter_original", "")
    context.user_data.pop("waiting_letter_instructions", None)
    await update.message.reply_text("✉️ Составляю... 😄")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system="Составляешь официальные деловые письма на русском. Профессионально.",
            messages=[{"role": "user", "content": f"Оригинал:\n{original}\n\nИнструкции:\n{instructions}\n\nСоставь ответ."}]
        )
        letter_text = response.content[0].text
        path = await generate_tz_docx(letter_text, "Ответное письмо")
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename="Ответное_письмо.docx", caption="✉️ Готово!")
        os.remove(path)
    except Exception as e:
        logger.error(f"Ошибка письма: {e}", exc_info=True)
        await update.message.reply_text("Не смог составить письмо 😕")


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
        await q.edit_message_text("Слушаю замечания 😄")
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
            messages=[{"role": "user", "content": f"Документ:\n\n{doc_info['content']}\n\nПравки:\n{edits}\n\nВерни исправленный документ."}]
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
        messages=[{"role": "user", "content": f"Данные закупки:\n{agent._context()}"}]
    )
    criteria_list = parse_criteria_list(response.content[0].text)
    context.user_data["criteria_list"] = criteria_list
    formatted = format_criteria_list(criteria_list)
    await msg.reply_text(
        f"Вот что планирую включить в критерии:\n\n{formatted}\n\nКак тебе? 🤔",
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
        await q.edit_message_text("Генерирую документ... ⚙️")
        return await generate_criteria_doc(q.message, uid, agent, context)
    elif q.data == "criteria_add":
        await q.edit_message_text(f"Текущий список:\n\n{format_criteria_list(criteria_list)}\n\nЧто добавить? 📝")
        context.user_data["criteria_action"] = "add"
        return CRITERIA_CONFIRM
    elif q.data == "criteria_remove":
        await q.edit_message_text(
            f"Нажми на критерий чтобы убрать:\n\n{format_criteria_list(criteria_list)}",
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
    formatted = format_criteria_list(criteria_list)
    await q.edit_message_text(
        f"Убрал! Обновлённый список:\n\n{formatted}",
        reply_markup=criteria_remove_kb(criteria_list)
    )
    return CRITERIA_CONFIRM


async def handle_criteria_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    agent = sessions.get(uid)
    text = await get_text(update)
    criteria_list = context.user_data.get("criteria_list", [])
    response = claude.messages.create(
        model="claude-sonnet-4-5", max_tokens=200,
        messages=[{"role": "user", "content": f"Сформулируй критерий допуска в 3-7 словах: '{text}'. Только текст критерия."}]
    )
    criteria_list.append(response.content[0].text.strip().strip("."))
    context.user_data["criteria_list"] = criteria_list
    context.user_data.pop("criteria_action", None)
    await update.message.reply_text(
        f"Добавил! Список:\n\n{format_criteria_list(criteria_list)}\n\nЕщё что-то? 😊",
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
            system=f"Эксперт по закупкам. Составляешь документ 'Критерии допуска'.\n\n{examples_text}",
            messages=[{"role": "user", "content": f"Данные закупки:\n{agent._context()}\n\nОбязательные критерии:\n{approved}\n\nСоставь полный документ. Начни с 'КРИТЕРИИ ДОПУСКА'."}]
        )
        content = response.content[0].text
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        path = await generate_criteria_docx(content, agent.tender_name)
        with open(path, "rb") as f:
            await msg.reply_document(document=f, filename=f"Критерии_{agent.tender_name[:35]}.docx", caption="📋 Критерии готовы!")
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
    if "doc_type" not in context.user_data:
        await q.edit_message_text("Что готовим? 📋", reply_markup=doctype_kb())
        return CHOOSING
    return await begin_questions(update, context, from_cb=True)


async def cb_doctype(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["doc_type"] = q.data.replace("doc_", "")
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
            await msg.reply_text("Сейчас покажу что планирую включить в критерии... 🤔")
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
        await q.edit_message_text("Покажу что планирую включить... 🤔")
        return await show_criteria_preview(q.message, agent, context)


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
        entry_points=[CommandHandler("new", new_request), CommandHandler("negotiation", negotiation_start)],
        states={
            CHOOSING: [
                CallbackQueryHandler(cb_direction, pattern="^dir_"),
                CallbackQueryHandler(cb_doctype, pattern="^doc_"),
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
            NEGOTIATION: [
                MessageHandler(tv, negotiation_answer),
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

    logger.info("Бот v6 запущен 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
