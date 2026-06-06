"""
Telegram-бот v9 — PDF/Word выбор, анализ документа заказчика, умные вопросы.
"""

import os
import json
import logging
import base64
import tempfile
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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
WAITING_DOC = 7       # Ждём документ от заказчика
SAVE_FORMAT = 8       # Выбор формата сохранения

sessions: dict[int, TenderAgent] = {}
last_doc: dict[int, dict] = {}
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

# Динамически добавленные пользователи (через /adduser) — хранятся в файле
DYNAMIC_USERS_FILE = "/tmp/dynamic_users.json"

def load_dynamic_users() -> set[int]:
    try:
        if os.path.exists(DYNAMIC_USERS_FILE):
            with open(DYNAMIC_USERS_FILE) as f:
                data = json.load(f)
                return set(data.get("users", []))
    except Exception:
        pass
    return set()

def save_dynamic_users(users: set[int]):
    try:
        with open(DYNAMIC_USERS_FILE, "w") as f:
            json.dump({"users": list(users)}, f)
    except Exception as e:
        logger.error(f"Ошибка сохранения dynamic_users: {e}")

# Загружаем динамических пользователей при старте
DYNAMIC_USERS: set[int] = load_dynamic_users()


def main_keyboard():
    """Постоянная клавиатура внизу — всегда видна пользователю."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🆕 Новый запрос"), KeyboardButton("❓ Помощь")]],
        resize_keyboard=True,
        is_persistent=True,
    )


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
        logger.error(f"Ошибка: {e}")


def get_all_users() -> dict:
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def is_allowed(user_id: int) -> bool:
    # Если белый список пустой — пускаем всех
    if not ALLOWED_USERS and not DYNAMIC_USERS:
        return True
    return user_id in ALLOWED_USERS or user_id in DYNAMIC_USERS


def remember_bot_message(uid: int, text: str):
    if uid not in bot_messages:
        bot_messages[uid] = []
    bot_messages[uid].append(text)
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
Нумерованный список, каждый критерий одной строкой:
1. Опыт работы от 3 лет
2. Наличие лицензии
Выведи 5-10 критериев. Только список."""

NEGOTIATION_STEPS = [
    {"question": "Что закупаем?", "options": ["Клининговые услуги", "IT-услуги", "Ремонт оборудования", "Строительные работы", "Поставка товаров"], "free": True},
    {"question": "Кто придёт от участника?", "options": ["Директор / собственник", "Коммерческий директор", "Менеджер по продажам", "Неизвестно"], "free": False},
    {"question": "НМЦ (начальная цена контракта)?", "options": ["До 1 млн руб.", "1–5 млн руб.", "5–20 млн руб.", "Более 20 млн руб."], "free": True},
    {"question": "На сколько снижаем цену?", "options": ["На 5–10%", "На 10–20%", "На 20–30%", "Максимально возможно"], "free": False},
    {"question": "Есть ли альтернативные участники?", "options": ["Да, есть 2+ конкурента", "Есть 1 альтернатива", "Нет, единственный"], "free": False},
    {"question": "Дополнительные цели переговоров?", "options": ["Только снижение цены", "Цена + сроки", "Цена + гарантии", "Цена + объём работ"], "free": False},
]

NEGOTIATION_SYSTEM = """Ты эксперт по закупочным переговорам. Цель всегда — снижение цены и улучшение условий.

Сценарий без воды:
1. ПОЗИЦИЯ ЗАКУПЩИКА — наши козыри
2. ОТКРЫТИЕ — первые фразы (2-3 варианта)
3. АРГУМЕНТЫ — конкретные фразы для давления на цену
4. ВОЗРАЖЕНИЯ — топ-3 возражения и точные ответы
5. ЗАКРЫТИЕ — как зафиксировать договорённость

Только конкретные фразы и тактики. Никакой воды. Русский язык."""

# Системный промпт для анализа документа заказчика
DOC_ANALYSIS_SYSTEM = """Ты эксперт по тендерам. Проанализируй предоставленный документ и извлеки из него данные для составления {doc_type}.

Из документа нужно извлечь следующие данные (если есть):
{questions_list}

Верни ответ в формате JSON:
{{
  "found": {{
    "вопрос": "найденный ответ",
    ...
  }},
  "missing": ["вопрос которого нет", ...]
}}

Если данных нет — возвращай пустой found и все вопросы в missing.
Только JSON, без лишних слов."""


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


async def extract_document_text(update: Update) -> str | None:
    """Извлекает текст из загруженного документа (docx, pdf, txt)."""
    if not update.message.document:
        return None

    doc = update.message.document
    mime = doc.mime_type or ""
    fname = doc.file_name or ""

    file = await doc.get_file()
    data = await file.download_as_bytearray()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(fname)[1] or ".bin")
    tmp.write(bytes(data))
    tmp.close()

    try:
        if "pdf" in mime or fname.lower().endswith(".pdf"):
            try:
                from pypdf import PdfReader
                reader = PdfReader(tmp.name)
                text = "\n".join(page.extract_text() or "" for page in reader.pages)
                return text.strip()
            except Exception as e:
                logger.error(f"PDF ошибка: {e}")
                return None

        elif "word" in mime or fname.lower().endswith((".docx", ".doc")):
            try:
                from docx import Document as DocxDoc
                d = DocxDoc(tmp.name)
                text = "\n".join(p.text for p in d.paragraphs if p.text.strip())
                return text.strip()
            except Exception as e:
                logger.error(f"DOCX ошибка: {e}")
                return None

        elif "text" in mime or fname.lower().endswith(".txt"):
            return bytes(data).decode("utf-8", errors="replace").strip()

        else:
            return None
    finally:
        os.remove(tmp.name)


# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Техническое задание (ТЗ)", callback_data="menu_tz")],
        [InlineKeyboardButton("📋 Критерии допуска", callback_data="menu_criteria")],
        [InlineKeyboardButton("📄+📋 ТЗ и критерии", callback_data="menu_both")],
        [InlineKeyboardButton("🤝 Сценарий переговоров", callback_data="menu_negotiation")],
    ])


def has_doc_kb():
    """Есть ли документ от заказчика?"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, загружу документ", callback_data="hasdoc_yes")],
        [InlineKeyboardButton("❌ Нет, задавай вопросы", callback_data="hasdoc_no")],
    ])


def direction_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("💻 IT-услуги", callback_data="dir_it")],
        [InlineKeyboardButton("🔧 Ремонт оборудования", callback_data="dir_repair")],
    ])


def save_format_kb():
    """Выбор формата сохранения."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Word (.docx)", callback_data="fmt_docx")],
        [InlineKeyboardButton("📕 PDF", callback_data="fmt_pdf")],
        [InlineKeyboardButton("📄+📕 Оба формата", callback_data="fmt_both")],
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


def negotiation_step_kb(step_idx: int) -> InlineKeyboardMarkup:
    step = NEGOTIATION_STEPS[step_idx]
    buttons = [[InlineKeyboardButton(opt, callback_data=f"neg_{step_idx}_{i}")]
               for i, opt in enumerate(step["options"])]
    if step.get("free"):
        buttons.append([InlineKeyboardButton("✏️ Свой вариант", callback_data=f"neg_{step_idx}_custom")])
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


# ─── Анализ документа заказчика ─────────────────────────────────────────────

async def analyze_customer_doc(doc_text: str, doc_type: str, direction: str) -> dict:
    """
    Анализирует документ заказчика и возвращает найденные данные и список недостающих вопросов.
    """
    from agent import QUESTIONS, DIRECTION_NAMES

    questions = QUESTIONS.get(direction, [])
    questions_list = "\n".join(f"- {q['question']}" for q in questions)
    doc_type_name = {"tz_only": "Технического задания", "criteria_only": "Критериев допуска", "both": "ТЗ и Критериев допуска"}.get(doc_type, "документа")

    system = DOC_ANALYSIS_SYSTEM.format(
        doc_type=doc_type_name,
        questions_list=questions_list
    )

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": f"Документ заказчика:\n\n{doc_text[:8000]}"}]
        )
        raw = response.content[0].text.strip()
        # Убираем возможные ```json обёртки
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        return result
    except Exception as e:
        logger.error(f"Ошибка анализа документа: {e}")
        # Если не смогли разобрать — возвращаем все вопросы как missing
        return {"found": {}, "missing": [q["question"] for q in questions]}


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
        "📄 ТЗ, критерии, сценарии переговоров\n"
        "💬 Отвечу на любой вопрос\n"
        "🔍 Найду информацию в интернете\n"
        "📷 Распознаю текст с фото\n"
        "✉️ Составлю ответное письмо\n"
        "📝 Сохраню любое сообщение в Word или PDF\n\n"
        "Кнопка «🆕 Новый запрос» внизу — для создания документов 😏",
        reply_markup=main_keyboard()
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


async def cb_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["direction"] = q.data.replace("dir_", "")

    # Спрашиваем есть ли документ от заказчика
    await q.edit_message_text(
        "Есть ли у тебя уже какой-то документ от заказчика? 📄\n"
        "(ТЗ-черновик, письмо, описание — что угодно)\n\n"
        "Если есть — загрузи его, и я задам только те вопросы которых там не хватает 😊",
        reply_markup=has_doc_kb()
    )
    return CHOOSING


async def cb_hasdoc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "hasdoc_yes":
        await q.edit_message_text(
            "Отлично! Загрузи документ — принимаю Word (.docx), PDF или текстовый файл 📎"
        )
        return WAITING_DOC

    else:  # hasdoc_no
        return await begin_questions(update, context, from_cb=True, msg=q.message)


async def receive_customer_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем документ от заказчика, анализируем и задаём только недостающие вопросы."""
    if not update.message.document:
        await update.message.reply_text("Пожалуйста, загрузи файл (Word, PDF или txt) 📎")
        return WAITING_DOC

    await update.message.reply_text("📄 Читаю документ...")

    doc_text = await extract_document_text(update)
    if not doc_text:
        await update.message.reply_text(
            "Не смог прочитать файл 😕 Попробуй другой формат или скопируй текст вручную."
        )
        return WAITING_DOC

    await update.message.reply_text("🔍 Анализирую документ, нахожу что уже есть...")

    direction = context.user_data.get("direction", "cleaning")
    doc_type = context.user_data.get("doc_type", "tz_only")

    analysis = await analyze_customer_doc(doc_text, doc_type, direction)
    found = analysis.get("found", {})
    missing = analysis.get("missing", [])

    # Сохраняем найденные данные
    context.user_data["prefilled_answers"] = found
    context.user_data["customer_doc_text"] = doc_text

    if found:
        found_text = "\n".join(f"✅ {k}: {v}" for k, v in found.items())
        await update.message.reply_text(
            f"Нашёл в документе:\n\n{found_text}\n\n"
            f"Осталось уточнить {len(missing)} вопрос(а) 😊"
        )
    else:
        await update.message.reply_text(
            "В документе не нашёл нужных данных — задам все вопросы 🤔"
        )

    if not missing:
        # Все данные есть — сразу генерируем
        await update.message.reply_text("Все данные есть! Генерирую документ... ⚙️")
        return await generate_from_prefilled(update, context)

    # Создаём агента только с недостающими вопросами
    return await begin_questions_filtered(update, context, missing)


async def begin_questions_filtered(update: Update, context: ContextTypes.DEFAULT_TYPE, missing_questions: list[str]):
    """Запускает вопросы только для недостающих данных."""
    from agent import QUESTIONS, DIRECTION_NAMES
    uid = update.effective_user.id
    direction = context.user_data.get("direction", "cleaning")
    doc_type = context.user_data.get("doc_type", "tz_only")

    # Фильтруем вопросы — только те которых не хватает
    all_questions = QUESTIONS.get(direction, [])
    filtered = [q for q in all_questions if any(m.lower() in q["question"].lower() or q["question"].lower() in m.lower() for m in missing_questions)]

    if not filtered:
        filtered = all_questions  # fallback — все вопросы

    agent = TenderAgent(direction=direction, doc_type=doc_type)
    agent.questions = filtered  # Подменяем список вопросов
    sessions[uid] = agent

    result = await agent.get_next_question()
    await send_question(update.message, result)
    return ANSWERING


async def generate_from_prefilled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует документ из данных найденных в документе заказчика."""
    uid = update.effective_user.id
    direction = context.user_data.get("direction", "cleaning")
    doc_type = context.user_data.get("doc_type", "tz_only")
    prefilled = context.user_data.get("prefilled_answers", {})
    customer_doc = context.user_data.get("customer_doc_text", "")

    from agent import TenderAgent, DIRECTION_NAMES
    agent = TenderAgent(direction=direction, doc_type=doc_type)
    # Добавляем найденные ответы
    for q, a in prefilled.items():
        agent.answers.append({"question": q, "answer": a})
    sessions[uid] = agent

    return await do_generate(update, context)


# ─── Вопросы и генерация ────────────────────────────────────────────────────

async def begin_questions(update: Update, context: ContextTypes.DEFAULT_TYPE, from_cb=False, msg=None):
    uid = update.effective_user.id
    agent = TenderAgent(
        direction=context.user_data.get("direction", "cleaning"),
        doc_type=context.user_data.get("doc_type", "tz_only"),
    )
    sessions[uid] = agent
    result = await agent.get_next_question()
    target = msg or (update.callback_query.message if from_cb else update.message)
    await send_question(target, result)
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

    # Добавляем предзаполненные ответы если есть
    prefilled = context.user_data.get("prefilled_answers", {})
    if prefilled and not agent.answers:
        for q, a in prefilled.items():
            agent.answers.append({"question": q, "answer": a})

    result = await agent.submit_answer(answer)
    msg = update.callback_query.message if update.callback_query else update.message
    if result["status"] == "question":
        await send_question(msg, result)
        return ANSWERING
    else:
        await msg.reply_text("Данные собраны! Генерирую... ⚙️")
        return await do_generate(update, context)


# ─── Сохранение документа ───────────────────────────────────────────────────

async def send_document_with_format_choice(msg, content: str, doc_type: str, name: str, uid: int, context):
    """Показывает превью и предлагает выбрать формат."""
    last_doc[uid] = {"content": content, "type": doc_type, "name": name}
    await msg.reply_text("В каком формате сохранить? 📄", reply_markup=save_format_kb())
    return SAVE_FORMAT


async def cb_save_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохраняет документ в выбранном формате."""
    from docx_generator import generate_tz_docx, generate_criteria_docx, generate_pdf
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    doc_info = last_doc.get(uid, {})
    if not doc_info:
        await q.edit_message_text("Не нашёл документ 😕")
        return ConversationHandler.END

    fmt = q.data.replace("fmt_", "")
    content = doc_info["content"]
    doc_type = doc_info.get("type", "tz")
    name = doc_info.get("name", "Документ")
    await q.edit_message_text("📥 Создаю файл(ы)...")

    try:
        # Определяем генератор docx
        if doc_type == "criteria":
            gen_docx = lambda: generate_criteria_docx(content, name)
            base_name = f"Критерии_{name[:30]}"
        elif doc_type == "negotiation":
            gen_docx = lambda: generate_tz_docx(content, name)
            base_name = "Сценарий_переговоров"
        else:
            gen_docx = lambda: generate_tz_docx(content, name)
            base_name = f"ТЗ_{name[:35]}"

        if fmt in ("docx", "both"):
            path = await gen_docx()
            with open(path, "rb") as f:
                await q.message.reply_document(document=f, filename=f"{base_name}.docx", caption="📄 Word файл готов!")
            os.remove(path)

        if fmt in ("pdf", "both"):
            path = await generate_pdf(content, name)
            with open(path, "rb") as f:
                await q.message.reply_document(document=f, filename=f"{base_name}.pdf", caption="📕 PDF файл готов!")
            os.remove(path)

        await q.message.reply_text("Всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}", exc_info=True)
        await q.message.reply_text("Ошибка при создании файла 😕")
        return REVIEWING


# ─── Генерация документов ───────────────────────────────────────────────────

async def do_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    agent = sessions[uid]
    msg = update.callback_query.message if update.callback_query else update.message
    try:
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            remember_bot_message(uid, content)
            await msg.reply_text("📄 ТЗ сгенерировано! В каком формате сохранить?", reply_markup=save_format_kb())
            last_doc[uid] = {"content": content, "type": "tz", "name": agent.tender_name}

            if agent.doc_type == "both":
                # Сначала сохраняем ТЗ, потом критерии
                context.user_data["pending_criteria"] = True
            return SAVE_FORMAT

        if agent.doc_type == "criteria_only":
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


async def cb_save_format_and_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """После сохранения ТЗ — переходим к критериям если нужно."""
    result = await cb_save_format(update, context)
    uid = update.effective_user.id
    agent = sessions.get(uid)

    if context.user_data.get("pending_criteria") and agent:
        context.user_data.pop("pending_criteria", None)
        q = update.callback_query
        await q.message.reply_text("Теперь критерии! Покажу что планирую включить... 🤔")
        return await show_criteria_preview(q.message, agent, context)

    return result


# ─── Переговоры ─────────────────────────────────────────────────────────────

async def cb_negotiation_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    step_idx = int(parts[1])
    choice = parts[2]

    if choice == "custom":
        await q.edit_message_text(NEGOTIATION_STEPS[step_idx]["question"] + "\n\nВведи свой вариант:")
        context.user_data["negotiation_custom_step"] = step_idx
        return NEGOTIATION

    step = NEGOTIATION_STEPS[step_idx]
    answer = step["options"][int(choice)]
    await q.edit_message_text(f"{step['question']}\n→ {answer}")
    return await _save_neg_and_next(q.message, context, step_idx, answer)


async def negotiation_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_text(update)
    if not text:
        return NEGOTIATION
    step_idx = context.user_data.pop("negotiation_custom_step", context.user_data.get("negotiation_step", 0))
    return await _save_neg_and_next(update.message, context, step_idx, text)


async def _save_neg_and_next(msg, context, step_idx: int, answer: str):
    answers = context.user_data.get("negotiation_answers", {})
    answers[step_idx] = {"question": NEGOTIATION_STEPS[step_idx]["question"], "answer": answer}
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


async def generate_negotiation(msg, context):
    uid = msg.chat_id
    answers = context.user_data.get("negotiation_answers", {})
    context_text = "\n".join(
        f"{answers[i]['question']}: {answers[i]['answer']}"
        for i in range(len(NEGOTIATION_STEPS)) if i in answers
    )
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=NEGOTIATION_SYSTEM,
            messages=[{"role": "user", "content": f"Данные:\n{context_text}\n\nСоставь конкретный сценарий без воды."}]
        )
        content = response.content[0].text
        remember_bot_message(uid, content)
        last_doc[uid] = {"content": content, "type": "negotiation", "name": "Сценарий переговоров"}
        preview = content[:2500] + ("\n...(фрагмент)" if len(content) > 2500 else "")
        await msg.reply_text(f"🤝 Сценарий:\n\n{preview}")
        await msg.reply_text("В каком формате сохранить?", reply_markup=save_format_kb())
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 → /new")
        return ConversationHandler.END


# ─── Критерии ───────────────────────────────────────────────────────────────

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
        f"Убрал!\n\n{format_criteria_list(criteria_list)}",
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
        f"Добавил!\n\n{format_criteria_list(criteria_list)}\n\nЕщё? 😊",
        reply_markup=criteria_main_kb()
    )
    return CRITERIA_CONFIRM


async def generate_criteria_doc(msg, uid: int, agent: TenderAgent, context):
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
        remember_bot_message(uid, content)
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        await msg.reply_text("📋 Критерии готовы! В каком формате сохранить?", reply_markup=save_format_kb())
        return SAVE_FORMAT
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


# ─── Правки ─────────────────────────────────────────────────────────────────

async def cb_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    if q.data == "review_ok":
        await q.edit_message_text("Значит не зря старался 😄")
        await q.message.reply_text("Для нового запроса нажми кнопку внизу 👇", reply_markup=main_keyboard())
        sessions.pop(uid, None)
        last_doc.pop(uid, None)
        return ConversationHandler.END
    elif q.data == "review_edit":
        await q.edit_message_text("Слушаю замечания 📝")
        return REVIEWING


async def apply_edits(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        remember_bot_message(uid, new_content)
        await update.message.reply_text("Исправлено! В каком формате сохранить?", reply_markup=save_format_kb())
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await update.message.reply_text("Ошибка 😕")
        return REVIEWING


# ─── Чат ────────────────────────────────────────────────────────────────────

async def save_to_word_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    messages_list = bot_messages.get(uid, [])
    content = messages_list[-1] if messages_list else None
    if not content:
        history = context.user_data.get("chat_history", [])
        for msg in reversed(history):
            if msg["role"] == "assistant" and isinstance(msg["content"], str):
                content = msg["content"]
                break
    if not content:
        await update.message.reply_text("Не нашёл что сохранять 🤔")
        return
    last_doc[uid] = {"content": content, "type": "chat", "name": "Документ"}
    await update.message.reply_text("В каком формате сохранить? 📄", reply_markup=save_format_kb())


async def cb_save_to_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    messages_list = bot_messages.get(uid, [])
    content = messages_list[-1] if messages_list else None
    if content:
        last_doc[uid] = {"content": content, "type": "chat", "name": "Документ"}
    await q.edit_message_text("В каком формате сохранить?", reply_markup=save_format_kb())


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

    # Кнопки постоянной клавиатуры
    if text == "🆕 Новый запрос":
        sessions.pop(uid, None)
        context.user_data.clear()
        await update.message.reply_text("Ладно, что делаем? 😄", reply_markup=main_menu_kb())
        return

    if text == "❓ Помощь":
        await update.message.reply_text(
            "Вот что я умею 😄\n\n"
            "🆕 *Новый запрос* — создать ТЗ, критерии или сценарий переговоров\n"
            "💬 Просто напиши вопрос — отвечу\n"
            "🔍 Ищу в интернете по запросу\n"
            "📷 Отправь фото — опишу и найду инфу\n"
            "✉️ Распознаю письмо с фото и составлю ответ\n"
            "📝 Скажи «сохрани» — сохраню последний ответ в Word или PDF\n"
            "📎 При создании документа можно загрузить файл заказчика — задам только недостающие вопросы",
            reply_markup=main_keyboard()
        )
        return

    if any(w in tl for w in ["сохрани", "в ворд", "в pdf", "сделай файл", "скачать"]):
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
            model="claude-sonnet-4-5", max_tokens=2000, system=system,
            tools=[WEB_SEARCH_TOOL], messages=history,
        )
        messages_list = list(history)
        while response.stop_reason == "tool_use":
            tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query", "")}
                  for b in response.content if b.type == "tool_use"]
            messages_list.append({"role": "assistant", "content": response.content})
            messages_list.append({"role": "user", "content": tr})
            response = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=2000, system=system,
                tools=[WEB_SEARCH_TOOL], messages=messages_list)

        reply = "".join(b.text for b in response.content if hasattr(b, "text"))
        if not reply:
            reply = "Хм, не знаю что ответить 🤔"

        remember_bot_message(uid, reply)
        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history

        if len(reply) > 4000:
            reply = reply[:4000] + "...\n(сократил 😄)"
        await update.message.reply_text(reply)

        if len(reply) > 500:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Сохранить", callback_data="save_to_word")]])
            await update.message.reply_text("Сохранить в файл? 📄", reply_markup=kb)

    except Exception as e:
        logger.error(f"Ошибка чата: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось 😅")


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
        if description: parts.append(f"👁 {description}")
        if ocr_text and ocr_text != "текста нет": parts.append(f"\n📝 Текст:\n{ocr_text}")
        if search_text: parts.append(f"\n🔍 В интернете:\n{search_text}")
        final = "\n".join(parts)
        if len(final) > 4000: final = final[:4000] + "..."

        uid = update.effective_user.id
        remember_bot_message(uid, final)
        await update.message.reply_text(final)

        all_text = (description + ocr_text).lower()
        if any(w in all_text for w in ["уважаем", "прошу", "сообщаем", "исх.", "вх."]):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Составить ответ", callback_data="write_reply")]])
            await update.message.reply_text("Похоже на письмо 📄 Составить ответ?", reply_markup=kb)

    except Exception as e:
        logger.error(f"Ошибка фото: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать 😕")


async def cb_photo_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "write_reply":
        await q.edit_message_text("Напиши что ответить ✉️")
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
            system="Составляешь официальные деловые письма на русском.",
            messages=[{"role": "user", "content": f"Оригинал:\n{original}\n\nИнструкции:\n{instructions}\n\nСоставь ответ."}]
        )
        content = response.content[0].text
        uid = update.effective_user.id
        remember_bot_message(uid, content)
        last_doc[uid] = {"content": content, "type": "letter", "name": "Ответное письмо"}
        await update.message.reply_text("В каком формате сохранить?", reply_markup=save_format_kb())
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await update.message.reply_text("Не смог составить 😕")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /users — показывает всех пользователей бота (только для админа)."""
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("⛔ Только для администратора.")
        return

    all_users = get_all_users()  # Все кто писал боту (из /tmp)
    whitelist = ALLOWED_USERS | DYNAMIC_USERS

    lines = ["👥 *Все пользователи бота:*
"]
    if not all_users:
        lines.append("Пока никто не писал боту (или список сбросился после перезапуска).")
    else:
        for user_id_str, username in all_users.items():
            uid_int = int(user_id_str)
            status = "✅" if uid_int in whitelist else "🔒"
            name = f"@{username}" if username else "без username"
            lines.append(f"{status} {uid_int} — {name}")

    lines.append(f"
📋 *В белом списке:* {len(whitelist)} чел.")
    lines.append("
✅ — есть доступ
🔒 — нет доступа")
    lines.append("
Чтобы добавить: `/adduser ID`")
    lines.append("Чтобы убрать: `/removeuser ID`")

    await update.message.reply_text("
".join(lines), parse_mode="Markdown")


async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /adduser ID — добавляет пользователя в белый список."""
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("⛔ Только для администратора.")
        return

    if not context.args:
        await update.message.reply_text(
            "Укажи Telegram ID пользователя:
`/adduser 123456789`",
            parse_mode="Markdown"
        )
        return

    try:
        new_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID. Должно быть число, например: `/adduser 123456789`", parse_mode="Markdown")
        return

    DYNAMIC_USERS.add(new_uid)
    save_dynamic_users(DYNAMIC_USERS)

    # Пробуем уведомить пользователя
    try:
        await context.bot.send_message(
            chat_id=new_uid,
            text="✅ Тебе открыт доступ к боту! Напиши /start чтобы начать 😄"
        )
        notified = "Пользователь уведомлён."
    except Exception:
        notified = "Не смог уведомить (пользователь не писал боту)."

    await update.message.reply_text(
        f"✅ Пользователь `{new_uid}` добавлен в белый список.
{notified}",
        parse_mode="Markdown"
    )


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /removeuser ID — убирает пользователя из белого списка."""
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("⛔ Только для администратора.")
        return

    if not context.args:
        await update.message.reply_text("Укажи ID:
`/removeuser 123456789`", parse_mode="Markdown")
        return

    try:
        rem_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Неверный ID.")
        return

    if rem_uid in DYNAMIC_USERS:
        DYNAMIC_USERS.discard(rem_uid)
        save_dynamic_users(DYNAMIC_USERS)
        await update.message.reply_text(f"✅ Пользователь `{rem_uid}` удалён из белого списка.", parse_mode="Markdown")
    elif rem_uid in ALLOWED_USERS:
        await update.message.reply_text(
            f"⚠️ Пользователь `{rem_uid}` добавлен через переменную ALLOWED_USERS на Railway — "
            f"удали его оттуда вручную в настройках Railway.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(f"Пользователь `{rem_uid}` не найден в белом списке.", parse_mode="Markdown")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("⛔ Только для администратора.")
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Напиши:\n/broadcast Привет! Бот обновился...")
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
    await update.message.reply_text(f"✅ Отправлено: {sent}, не доставлено: {failed}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None)
    last_doc.pop(uid, None)
    context.user_data.clear()
    await update.message.reply_text("Отменено 👌 Пиши если что!", reply_markup=main_keyboard())
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND
    photo_f = filters.PHOTO | filters.Document.IMAGE
    doc_f = filters.Document.ALL

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_request)],
        states={
            CHOOSING: [
                CallbackQueryHandler(cb_main_menu, pattern="^menu_"),
                CallbackQueryHandler(cb_direction, pattern="^dir_"),
                CallbackQueryHandler(cb_hasdoc, pattern="^hasdoc_"),
            ],
            WAITING_DOC: [
                MessageHandler(doc_f, receive_customer_doc),
                MessageHandler(tv, lambda u, c: u.message.reply_text("Загрузи файл 📎 или нажми /cancel")),
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
            SAVE_FORMAT: [
                CallbackQueryHandler(cb_save_format_and_continue, pattern="^fmt_"),
            ],
            REVIEWING: [
                CallbackQueryHandler(cb_review, pattern="^review_(ok|edit)$"),
                MessageHandler(tv, apply_edits),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(conv)
    app.add_handler(MessageHandler(photo_f, chat_reply))
    app.add_handler(CallbackQueryHandler(cb_photo_actions, pattern="^write_reply$"))
    app.add_handler(CallbackQueryHandler(cb_save_to_word, pattern="^save_to_word$"))
    app.add_handler(MessageHandler(tv, chat_reply))

    logger.info("Бот v9 запущен 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
