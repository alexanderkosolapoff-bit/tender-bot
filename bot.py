"""
Telegram-бот v10 — чистая версия.
ТЗ, критерии, переговоры, фото, чат, PDF/Word, управление доступом.
"""

import os
import json
import logging
import base64
import tempfile
import anthropic
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes, ConversationHandler,
)
from agent import TenderAgent
from voice_handler import transcribe_voice

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Состояния ConversationHandler
CHOOSING      = 1
ANSWERING     = 2
CRITERIA_Q    = 3
REVIEWING     = 4
CRITERIA_CONF = 5
NEGOTIATION   = 6
WAITING_DOC   = 7
SAVE_FORMAT   = 8

# Хранилища в памяти
sessions:    dict[int, TenderAgent] = {}
last_doc:    dict[int, dict]        = {}
bot_msgs:    dict[int, list[str]]   = {}

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

# ─── Доступ ─────────────────────────────────────────────────────────────────
USERS_FILE   = "/tmp/bot_users.json"
DYNAMIC_FILE = "/tmp/dynamic_users.json"

ALLOWED_USERS: set[int] = set()
for _s in os.environ.get("ALLOWED_USERS", "").split(","):
    try: ALLOWED_USERS.add(int(_s.strip()))
    except ValueError: pass

def _load_dynamic() -> set[int]:
    try:
        if os.path.exists(DYNAMIC_FILE):
            return set(json.load(open(DYNAMIC_FILE)).get("users", []))
    except Exception: pass
    return set()

def _save_dynamic(s: set[int]):
    try:
        json.dump({"users": list(s)}, open(DYNAMIC_FILE, "w"))
    except Exception as e: logger.error(e)

DYNAMIC_USERS: set[int] = _load_dynamic()

def is_allowed(uid: int) -> bool:
    if not ALLOWED_USERS and not DYNAMIC_USERS: return True
    return uid in ALLOWED_USERS or uid in DYNAMIC_USERS

def save_user(uid: int, username: str = ""):
    try:
        data = {}
        if os.path.exists(USERS_FILE):
            data = json.load(open(USERS_FILE))
        data[str(uid)] = username
        json.dump(data, open(USERS_FILE, "w"))
    except Exception as e: logger.error(e)

def get_all_users() -> dict:
    try:
        if os.path.exists(USERS_FILE):
            return json.load(open(USERS_FILE))
    except Exception: pass
    return {}

def remember(uid: int, text: str):
    bot_msgs.setdefault(uid, []).append(text)
    if len(bot_msgs[uid]) > 20: bot_msgs[uid] = bot_msgs[uid][-20:]

# ─── Промпты ────────────────────────────────────────────────────────────────
CHAT_SYSTEM = """Ты - Макс, дерзкий помощник по тендерам. Как лучший друг: подкалываешь, шутишь, но всегда помогаешь.
Помни весь контекст разговора включая фото.
Шутки: "А самому слабо?", "Опять ты...", "Конец рабочего дня, но ладно", "Серьезно спрашиваешь? Окей".
Смайлики активно. Русский язык."""

REVIEW_SYSTEM = "Эксперт по тендерам. Внеси правки и верни ПОЛНЫЙ исправленный текст документа."

CRITERIA_SYSTEM = """Эксперт по тендерам и закупкам.
Составь список критериев допуска участников к закупке.

Формат — строго нумерованный список, каждый критерий в одну строку:
1. Опыт работы в данной сфере не менее 3 лет
2. Наличие необходимых лицензий и допусков
3. Отсутствие задолженности по налогам и сборам

Требования:
- 6-10 критериев
- Каждый критерий конкретный и измеримый
- Только список, без заголовков и пояснений
- Критерии реалистичные для данного типа закупки"""

NEGOTIATION_SYSTEM = """Эксперт по закупочным переговорам. Цель: снижение цены и улучшение условий.
Сценарий без воды:
1. ПОЗИЦИЯ ЗАКУПЩИКА
2. ОТКРЫТИЕ (2-3 варианта первых фраз)
3. АРГУМЕНТЫ для давления на цену
4. ВОЗРАЖЕНИЯ участника и точные ответы
5. ЗАКРЫТИЕ сделки
Только конкретные фразы. Русский язык."""

NEGOTIATION_STEPS = [
    {"q": "Что закупаем?", "opts": ["Клининговые услуги", "IT-услуги", "Ремонт оборудования", "Строительные работы", "Поставка товаров"], "free": True},
    {"q": "Кто придет от участника?", "opts": ["Директор/собственник", "Коммерческий директор", "Менеджер по продажам", "Неизвестно"], "free": False},
    {"q": "Начальная цена контракта (НМЦ)?", "opts": ["До 1 млн руб.", "1-5 млн руб.", "5-20 млн руб.", "Более 20 млн руб."], "free": True},
    {"q": "На сколько снижаем цену?", "opts": ["На 5-10%", "На 10-20%", "На 20-30%", "Максимально"], "free": False},
    {"q": "Есть альтернативные участники?", "opts": ["Да, 2+ конкурента", "Есть 1 альтернатива", "Нет, единственный"], "free": False},
    {"q": "Доп. цели переговоров?", "opts": ["Только снижение цены", "Цена + сроки", "Цена + гарантии", "Цена + объем работ"], "free": False},
]

# ─── Вспомогательные функции ────────────────────────────────────────────────
async def get_text(update: Update) -> str | None:
    if update.message.text:
        return update.message.text.strip()
    if update.message.voice:
        await update.message.reply_text("Слушаю...")
        file = await update.message.voice.get_file()
        data = await file.download_as_bytearray()
        text = await transcribe_voice(bytes(data))
        await update.message.reply_text(f'Услышал: "{text}"')
        return text
    return None

async def get_image_b64(update: Update) -> tuple[str, str] | None:
    photo = None
    if update.message.photo:
        photo = update.message.photo[-1]
    elif update.message.document and update.message.document.mime_type and \
         update.message.document.mime_type.startswith("image/"):
        photo = update.message.document
    if not photo: return None
    file = await photo.get_file()
    data = await file.download_as_bytearray()
    b64 = base64.standard_b64encode(bytes(data)).decode()
    mime = getattr(photo, "mime_type", None) or "image/jpeg"
    return b64, mime

async def extract_doc_text(update: Update) -> str | None:
    if not update.message.document: return None
    doc = update.message.document
    fname = doc.file_name or ""
    mime = doc.mime_type or ""
    file = await doc.get_file()
    data = bytes(await file.download_as_bytearray())
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(fname)[1] or ".bin")
    tmp.write(data); tmp.close()
    try:
        if "pdf" in mime or fname.lower().endswith(".pdf"):
            from pypdf import PdfReader
            r = PdfReader(tmp.name)
            return "\n".join(p.extract_text() or "" for p in r.pages).strip()
        elif "word" in mime or fname.lower().endswith((".docx", ".doc")):
            from docx import Document as D
            return "\n".join(p.text for p in D(tmp.name).paragraphs if p.text.strip())
        elif "text" in mime or fname.lower().endswith(".txt"):
            return data.decode("utf-8", errors="replace").strip()
    except Exception as e:
        logger.error(f"Doc read error: {e}")
    finally:
        os.remove(tmp.name)
    return None

def _parse_criteria(text: str) -> list[str]:
    result = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line: continue
        cleaned = line.lstrip("0123456789").lstrip(". ").strip()
        if cleaned: result.append(cleaned)
    return result

def _fmt_criteria(lst: list[str]) -> str:
    return "\n".join(f"{i+1}. {c}" for i, c in enumerate(lst))

async def send_q(msg, result: dict):
    text = result["question"]
    opts = result.get("options", [])
    if opts:
        kb = [[InlineKeyboardButton(o, callback_data=f"ans_{i}")] for i, o in enumerate(opts)]
        kb.append([InlineKeyboardButton("Свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text)

def main_kb():
    """Заглушка — больше не используем постоянную клавиатуру."""
    return None

def menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Техническое задание (ТЗ)", callback_data="menu_tz")],
        [InlineKeyboardButton("Критерии допуска", callback_data="menu_criteria")],
        [InlineKeyboardButton("ТЗ и критерии", callback_data="menu_both")],
        [InlineKeyboardButton("Сценарий переговоров", callback_data="menu_negotiation")],
    ])

def dir_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("IT-услуги", callback_data="dir_it")],
        [InlineKeyboardButton("Ремонт оборудования", callback_data="dir_repair")],
    ])

def hasdoc_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, загружу документ", callback_data="hasdoc_yes")],
        [InlineKeyboardButton("Нет, задавай вопросы", callback_data="hasdoc_no")],
    ])

def review_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Все отлично!", callback_data="review_ok")],
        [InlineKeyboardButton("Есть замечания", callback_data="review_edit")],
    ])

def criteria_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Подходит, генерируй!", callback_data="criteria_go")],
        [InlineKeyboardButton("Добавить критерий", callback_data="criteria_add")],
        [InlineKeyboardButton("Убрать критерий", callback_data="criteria_remove")],
    ])

def criteria_remove_kb(lst: list[str]) -> InlineKeyboardMarkup:
    btns = [[InlineKeyboardButton(f"X {c[:45]}", callback_data=f"del_criterion_{i}")] for i, c in enumerate(lst)]
    btns.append([InlineKeyboardButton("Готово, генерируй!", callback_data="criteria_go")])
    return InlineKeyboardMarkup(btns)

def save_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Word (.docx)", callback_data="fmt_docx")],
        [InlineKeyboardButton("PDF", callback_data="fmt_pdf")],
        [InlineKeyboardButton("Word + PDF", callback_data="fmt_both")],
    ])

def neg_kb(step: int) -> InlineKeyboardMarkup:
    s = NEGOTIATION_STEPS[step]
    btns = [[InlineKeyboardButton(o, callback_data=f"neg_{step}_{i}")] for i, o in enumerate(s["opts"])]
    if s["free"]: btns.append([InlineKeyboardButton("Свой вариант", callback_data=f"neg_{step}_custom")])
    return InlineKeyboardMarkup(btns)

# ─── Команды ────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа. Обратись к администратору.")
        return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None); context.user_data.clear()
    await update.message.reply_text(
        "Привет! Я Макс - помощник по тендерам.\n\n"
        "Умею:\n"
        "- Составить ТЗ, критерии допуска, сценарий переговоров\n"
        "- Ответить на любой вопрос\n"
        "- Найти информацию в интернете\n"
        "- Распознать текст с фото\n"
        "- Составить ответное письмо\n"
        "- Сохранить любой ответ в Word или PDF\n\n"
        "Кнопка 'Новый запрос' внизу или просто напиши что нужно!"
    )

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None); context.user_data.clear()
    await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
    return CHOOSING

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
    await update.message.reply_text("Отменено. Пиши если что!")
    return ConversationHandler.END

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("Только для администратора.")
        return
    all_users = get_all_users()
    whitelist = ALLOWED_USERS | DYNAMIC_USERS
    lines = ["Все пользователи бота:\n"]
    if not all_users:
        lines.append("Никто не писал боту.")
    else:
        for uid_str, uname in all_users.items():
            s = "OK" if int(uid_str) in whitelist else "NO"
            n = f"@{uname}" if uname else "без username"
            lines.append(f"[{s}] {uid_str} - {n}")
    lines.append(f"\nВ белом списке: {len(whitelist)} чел.")
    lines.append("OK = доступ есть, NO = нет доступа")
    lines.append("Добавить: /adduser ID")
    lines.append("Убрать: /removeuser ID")
    await update.message.reply_text("\n".join(lines))

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("Только для администратора.")
        return
    if not context.args:
        await update.message.reply_text("Укажи ID: /adduser 123456789")
        return
    try:
        new_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный ID. Должно быть число.")
        return
    DYNAMIC_USERS.add(new_uid); _save_dynamic(DYNAMIC_USERS)
    try:
        await context.bot.send_message(chat_id=new_uid, text="Тебе открыт доступ к боту! Напиши /start")
        notified = "Пользователь уведомлен."
    except Exception:
        notified = "Не смог уведомить (пользователь не писал боту)."
    await update.message.reply_text(f"Пользователь {new_uid} добавлен.\n{notified}")

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("Только для администратора.")
        return
    if not context.args:
        await update.message.reply_text("Укажи ID: /removeuser 123456789")
        return
    try:
        rem_uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Неверный ID.")
        return
    if rem_uid in DYNAMIC_USERS:
        DYNAMIC_USERS.discard(rem_uid); _save_dynamic(DYNAMIC_USERS)
        await update.message.reply_text(f"Пользователь {rem_uid} удален из белого списка.")
    elif rem_uid in ALLOWED_USERS:
        await update.message.reply_text(f"Пользователь {rem_uid} в ALLOWED_USERS на Railway - удали там вручную.")
    else:
        await update.message.reply_text(f"Пользователь {rem_uid} не найден в белом списке.")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id:
        await update.message.reply_text("Только для администратора.")
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Напиши текст:\n/broadcast Привет! Бот обновился...")
        return
    users = get_all_users()
    sent = failed = 0
    await update.message.reply_text(f"Рассылаю {len(users)} пользователям...")
    for uid_str in users:
        try:
            await context.bot.send_message(chat_id=int(uid_str), text=text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"Готово! Отправлено: {sent}, не доставлено: {failed}")

# ─── Меню и выбор направления ───────────────────────────────────────────────
async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    if not is_allowed(uid):
        await q.edit_message_text("Нет доступа.")
        return ConversationHandler.END

    if q.data == "menu_negotiation":
        context.user_data["neg_answers"] = {}
        context.user_data["neg_step"] = 0
        await q.edit_message_text(NEGOTIATION_STEPS[0]["q"], reply_markup=neg_kb(0))
        return NEGOTIATION

    doc_map = {"menu_tz": "tz_only", "menu_criteria": "criteria_only", "menu_both": "both"}
    context.user_data["doc_type"] = doc_map.get(q.data, "tz_only")
    await q.edit_message_text("Выбери направление закупки:", reply_markup=dir_kb())
    return CHOOSING

async def cb_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["direction"] = q.data.replace("dir_", "")
    await q.edit_message_text(
        "Есть документ от заказчика? (ТЗ-черновик, письмо, описание)\n"
        "Если да - загружу и задам только недостающие вопросы.",
        reply_markup=hasdoc_kb()
    )
    return CHOOSING

async def cb_hasdoc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "hasdoc_yes":
        await q.edit_message_text("Загрузи документ (Word, PDF или txt)")
        return WAITING_DOC
    else:
        return await _start_questions(q.message, context)

async def receive_customer_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Загрузи файл или нажми /cancel")
        return WAITING_DOC
    await update.message.reply_text("Читаю документ...")
    doc_text = await extract_doc_text(update)
    if not doc_text:
        await update.message.reply_text("Не смог прочитать. Попробуй другой формат.")
        return WAITING_DOC

    await update.message.reply_text("Анализирую что уже есть в документе...")
    direction = context.user_data.get("direction", "cleaning")
    doc_type = context.user_data.get("doc_type", "tz_only")

    from agent import QUESTIONS
    questions = QUESTIONS.get(direction, [])
    questions_list = "\n".join(f"- {q['question']}" for q in questions)

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=f"Проанализируй документ и найди ответы на вопросы. Верни JSON: {{\"found\": {{\"вопрос\": \"ответ\"}}, \"missing\": [\"вопрос\"]}}. Только JSON.",
            messages=[{"role": "user", "content": f"Вопросы:\n{questions_list}\n\nДокумент:\n{doc_text[:6000]}"}]
        )
        raw = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        analysis = json.loads(raw)
    except Exception:
        analysis = {"found": {}, "missing": [q["question"] for q in questions]}

    found = analysis.get("found", {})
    missing = analysis.get("missing", [])
    context.user_data["prefilled"] = found
    context.user_data["customer_doc"] = doc_text

    if found:
        found_text = "\n".join(f"- {k}: {v}" for k, v in found.items())
        await update.message.reply_text(f"Нашел в документе:\n{found_text}\n\nОсталось уточнить: {len(missing)} вопрос(а)")
    else:
        await update.message.reply_text("В документе не нашел нужных данных - задам все вопросы")

    if not missing:
        await update.message.reply_text("Все данные есть! Генерирую...")
        uid = update.effective_user.id
        from agent import DIRECTION_NAMES
        direction = context.user_data.get("direction", "cleaning")
        doc_type = context.user_data.get("doc_type", "tz_only")
        agent = TenderAgent(direction=direction, doc_type=doc_type)
        for fq, fa in found.items():
            agent.answers.append({"question": fq, "answer": fa})
        sessions[uid] = agent
        return await do_generate(update, context)

    # Фильтруем вопросы
    uid = update.effective_user.id
    from agent import QUESTIONS, DIRECTION_NAMES
    all_q = QUESTIONS.get(direction, [])
    filtered = [q for q in all_q if any(m.lower() in q["question"].lower() for m in missing)] or all_q
    agent = TenderAgent(direction=direction, doc_type=doc_type)
    agent.questions = filtered
    for fq, fa in found.items():
        agent.answers.append({"question": fq, "answer": fa})
    sessions[uid] = agent

    result = await agent.get_next_question()
    await send_q(update.message, result)
    return ANSWERING

# ─── Вопросы ТЗ ─────────────────────────────────────────────────────────────
async def _start_questions(msg, context: ContextTypes.DEFAULT_TYPE):
    uid = msg.chat_id
    agent = TenderAgent(
        direction=context.user_data.get("direction", "cleaning"),
        doc_type=context.user_data.get("doc_type", "tz_only"),
    )
    sessions[uid] = agent
    result = await agent.get_next_question()
    await send_q(msg, result)
    return ANSWERING

async def cb_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent:
        await q.edit_message_text("Сессия устарела. Начни заново.")
        return ConversationHandler.END
    if q.data == "ans_custom":
        await q.edit_message_text(q.message.text + "\n\nВведи свой вариант:")
        return ANSWERING
    idx = int(q.data.replace("ans_", ""))
    opts = agent.last_question.get("options", [])
    answer = opts[idx] if idx < len(opts) else ""
    await q.edit_message_text(f"{agent.last_question['question']}\n> {answer}")
    return await _handle_answer(update, context, answer)

async def text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if await keyboard_buttons(update, context):
        return ConversationHandler.END
    text = await get_text(update)
    agent = sessions.get(uid)
    if not agent:
        await update.message.reply_text("Сессия устарела. Начни заново.")
        return ConversationHandler.END
    if not text: return ANSWERING
    return await _handle_answer(update, context, text)

async def _handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: str):
    uid = update.effective_user.id
    agent = sessions[uid]
    prefilled = context.user_data.get("prefilled", {})
    if prefilled and not any(a.get("prefilled") for a in agent.answers):
        for fq, fa in prefilled.items():
            agent.answers.append({"question": fq, "answer": fa, "prefilled": True})
    result = await agent.submit_answer(answer)
    msg = update.callback_query.message if update.callback_query else update.message
    if result["status"] == "question":
        await send_q(msg, result)
        return ANSWERING
    await msg.reply_text("Данные собраны! Генерирую...")
    return await do_generate(update, context)

# ─── Генерация документов ───────────────────────────────────────────────────
async def do_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text("Сессия устарела. Начни заново.")
        return ConversationHandler.END
    msg = update.callback_query.message if update.callback_query else update.message
    try:
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            remember(uid, content)
            last_doc[uid] = {"content": content, "type": "tz", "name": agent.tender_name}
            await msg.reply_text("ТЗ готово! В каком формате сохранить?", reply_markup=save_kb())
            if agent.doc_type == "both":
                context.user_data["pending_criteria"] = True
            return SAVE_FORMAT

        if agent.doc_type == "criteria_only":
            await msg.reply_text("Показываю что планирую включить в критерии...")
            return await _show_criteria_preview(msg, agent, context)

        if agent.doc_type == "tz_only":
            await msg.reply_text(
                "Нужны критерии допуска?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Да, добавить критерии", callback_data="yes_criteria")],
                    [InlineKeyboardButton("Нет, все готово", callback_data="no_criteria")],
                ])
            )
            return CRITERIA_Q

        await msg.reply_text("Все устраивает?", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Generate error: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так. Попробуй /cancel и начни заново.")
        return ConversationHandler.END

async def cb_save_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx, generate_pdf
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    doc_info = last_doc.get(uid, {})
    if not doc_info:
        await q.edit_message_text("Не нашел документ. Попробуй заново.")
        return ConversationHandler.END

    fmt = q.data.replace("fmt_", "")
    content = doc_info["content"]
    doc_type = doc_info.get("type", "tz")
    name = doc_info.get("name", "Документ")

    if doc_type == "criteria":
        gen_docx = lambda: generate_criteria_docx(content, name)
        base = f"Kriterii_{name[:30]}"
    elif doc_type == "negotiation":
        gen_docx = lambda: generate_tz_docx(content, name)
        base = "Scenariy_peregovorov"
    else:
        gen_docx = lambda: generate_tz_docx(content, name)
        base = f"TZ_{name[:35]}"

    await q.edit_message_text("Создаю файл(ы)...")
    try:
        if fmt in ("docx", "both"):
            path = await gen_docx()
            with open(path, "rb") as f:
                await q.message.reply_document(document=f, filename=f"{base}.docx", caption="Word файл готов!")
            os.remove(path)
        if fmt in ("pdf", "both"):
            path = await generate_pdf(content, name)
            with open(path, "rb") as f:
                await q.message.reply_document(document=f, filename=f"{base}.pdf", caption="PDF файл готов!")
            os.remove(path)

        # Если было "both" ТЗ+критерии - переходим к критериям
        if context.user_data.get("pending_criteria"):
            context.user_data.pop("pending_criteria")
            agent = sessions.get(uid)
            if agent:
                await q.message.reply_text("Теперь критерии! Показываю что планирую включить...")
                return await _show_criteria_preview(q.message, agent, context)

        await q.message.reply_text("Все устраивает?", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Save error: {e}", exc_info=True)
        await q.message.reply_text("Ошибка при создании файла.")
        return REVIEWING

# ─── Критерии ───────────────────────────────────────────────────────────────
async def _show_criteria_preview(msg, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    response = claude.messages.create(
        model="claude-sonnet-4-5", max_tokens=800, system=CRITERIA_SYSTEM,
        messages=[{"role": "user", "content": f"Данные закупки:\n{agent._context()}"}]
    )
    lst = _parse_criteria(response.content[0].text)
    context.user_data["criteria_list"] = lst
    await msg.reply_text(
        f"Планирую включить в критерии:\n\n{_fmt_criteria(lst)}\n\nКак тебе?",
        reply_markup=criteria_kb()
    )
    return CRITERIA_CONF

async def cb_criteria_conf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    lst = context.user_data.get("criteria_list", [])

    if q.data == "criteria_go":
        await q.edit_message_text("Генерирую критерии...")
        return await _gen_criteria(q.message, uid, agent, context)
    elif q.data == "criteria_add":
        await q.edit_message_text(f"Список:\n\n{_fmt_criteria(lst)}\n\nЧто добавить?")
        context.user_data["criteria_action"] = "add"
        return CRITERIA_CONF
    elif q.data == "criteria_remove":
        await q.edit_message_text(
            f"Нажми чтобы убрать:\n\n{_fmt_criteria(lst)}",
            reply_markup=criteria_remove_kb(lst)
        )
        return CRITERIA_CONF

async def cb_del_criterion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.replace("del_criterion_", ""))
    lst = context.user_data.get("criteria_list", [])
    if 0 <= idx < len(lst): lst.pop(idx)
    context.user_data["criteria_list"] = lst
    await q.edit_message_text(
        f"Убрал! Список:\n\n{_fmt_criteria(lst)}",
        reply_markup=criteria_remove_kb(lst)
    )
    return CRITERIA_CONF

async def handle_criteria_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await keyboard_buttons(update, context):
        return ConversationHandler.END
    text = await get_text(update)
    lst = context.user_data.get("criteria_list", [])
    response = claude.messages.create(
        model="claude-sonnet-4-5", max_tokens=100,
        messages=[{"role": "user", "content": f"Сформулируй критерий допуска в 3-7 словах: '{text}'. Только текст критерия."}]
    )
    lst.append(response.content[0].text.strip().strip("."))
    context.user_data["criteria_list"] = lst
    context.user_data.pop("criteria_action", None)
    await update.message.reply_text(
        f"Добавил!\n\n{_fmt_criteria(lst)}\n\nЕще что-то?",
        reply_markup=criteria_kb()
    )
    return CRITERIA_CONF

async def _gen_criteria(msg, uid: int, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_criteria_docx
    from examples_loader import load_examples
    lst = context.user_data.get("criteria_list", [])
    approved = _fmt_criteria(lst)
    examples_text = ""
    texts = load_examples("criteria")
    if texts:
        examples_text = "ПРИМЕРЫ:\n" + "".join(f"=== {i} ===\n{t[:2000]}\n" for i, t in enumerate(texts[:3], 1))
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=3000,
            system=f"Эксперт по закупкам. Составляешь документ 'Критерии допуска'.\n{examples_text}",
            messages=[{"role": "user", "content": f"Данные:\n{agent._context()}\n\nОбязательные критерии:\n{approved}\n\nСоставь полный документ. Начни с 'КРИТЕРИИ ДОПУСКА'."}]
        )
        content = response.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        await msg.reply_text("Критерии готовы! В каком формате сохранить?", reply_markup=save_kb())
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Criteria error: {e}", exc_info=True)
        await msg.reply_text("Ошибка. Попробуй /cancel и заново.")
        return ConversationHandler.END

async def cb_criteria_q(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if q.data == "no_criteria":
        await q.edit_message_text("Все устраивает?", reply_markup=review_kb())
        return REVIEWING
    await q.edit_message_text("Показываю что планирую включить...")
    return await _show_criteria_preview(q.message, agent, context)

# ─── Правки ─────────────────────────────────────────────────────────────────
async def cb_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    if q.data == "review_ok":
        await q.edit_message_text("Отлично! Обращайся если что — никуда не денусь 😄\n\nДля нового запроса: /new")
        sessions.pop(uid, None); last_doc.pop(uid, None)
        return ConversationHandler.END
    await q.edit_message_text("Слушаю замечания:")
    return REVIEWING

async def apply_edits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await keyboard_buttons(update, context):
        return ConversationHandler.END
    uid = update.effective_user.id
    edits = await get_text(update)
    doc_info = last_doc.get(uid, {})
    if not doc_info:
        await update.message.reply_text("Не нашел документ. Попробуй заново.")
        return ConversationHandler.END
    await update.message.reply_text("Вношу правки...")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": f"Документ:\n{doc_info['content']}\n\nПравки:\n{edits}\n\nВерни исправленный документ."}]
        )
        new_content = response.content[0].text
        last_doc[uid]["content"] = new_content
        remember(uid, new_content)
        await update.message.reply_text("Исправлено! В каком формате сохранить?", reply_markup=save_kb())
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Edit error: {e}", exc_info=True)
        await update.message.reply_text("Ошибка. Попробуй еще раз.")
        return REVIEWING

# ─── Переговоры ─────────────────────────────────────────────────────────────
async def cb_neg_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    step = int(parts[1])
    choice = parts[2]
    if choice == "custom":
        await q.edit_message_text(NEGOTIATION_STEPS[step]["q"] + "\n\nВведи свой вариант:")
        context.user_data["neg_custom_step"] = step
        return NEGOTIATION
    answer = NEGOTIATION_STEPS[step]["opts"][int(choice)]
    await q.edit_message_text(f"{NEGOTIATION_STEPS[step]['q']}\n> {answer}")
    return await _save_neg(q.message, context, step, answer)

async def neg_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await keyboard_buttons(update, context):
        return ConversationHandler.END
    text = await get_text(update)
    if not text: return NEGOTIATION
    step = context.user_data.pop("neg_custom_step", context.user_data.get("neg_step", 0))
    return await _save_neg(update.message, context, step, text)

async def _save_neg(msg, context: ContextTypes.DEFAULT_TYPE, step: int, answer: str):
    answers = context.user_data.setdefault("neg_answers", {})
    answers[step] = {"q": NEGOTIATION_STEPS[step]["q"], "a": answer}
    next_step = step + 1
    context.user_data["neg_step"] = next_step
    if next_step < len(NEGOTIATION_STEPS):
        await msg.reply_text(NEGOTIATION_STEPS[next_step]["q"], reply_markup=neg_kb(next_step))
        return NEGOTIATION
    await msg.reply_text("Составляю сценарий переговоров...")
    return await _gen_negotiation(msg, context)

async def _gen_negotiation(msg, context: ContextTypes.DEFAULT_TYPE):
    uid = msg.chat_id
    answers = context.user_data.get("neg_answers", {})
    ctx = "\n".join(f"{answers[i]['q']}: {answers[i]['a']}" for i in range(len(NEGOTIATION_STEPS)) if i in answers)
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=NEGOTIATION_SYSTEM,
            messages=[{"role": "user", "content": f"Данные для сценария:\n{ctx}\n\nСоставь конкретный сценарий без воды."}]
        )
        content = response.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "negotiation", "name": "Scenariy peregovorov"}
        preview = content[:2500] + ("...(фрагмент)" if len(content) > 2500 else "")
        await msg.reply_text(f"Сценарий переговоров:\n\n{preview}")
        await msg.reply_text("В каком формате сохранить?", reply_markup=save_kb())
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Negotiation error: {e}", exc_info=True)
        await msg.reply_text("Ошибка. Попробуй заново.")
        return ConversationHandler.END

# ─── Чат ────────────────────────────────────────────────────────────────────
async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return
    save_user(uid, update.effective_user.username or "")

    # Фото
    if update.message.photo or (
        update.message.document and update.message.document.mime_type and
        update.message.document.mime_type.startswith("image/")
    ):
        await handle_photo(update, context)
        return

    text = await get_text(update)
    if not text: return

    # Ответное письмо
    if context.user_data.get("waiting_letter"):
        await gen_letter(update, context)
        return

    tl = text.lower()

    # Кнопки постоянной клавиатуры
    if text == "Новый запрос":
        sessions.pop(uid, None); context.user_data.clear()
        await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
        return

    if text == "Помощь":
        await update.message.reply_text(
            "Что умею:\n\n"
            "- 'Новый запрос' - создать ТЗ, критерии, сценарий переговоров\n"
            "- Любой вопрос - отвечу\n"
            "- Ищу в интернете\n"
            "- Фото - опишу, найду инфу, распознаю текст\n"
            "- 'сохрани' - сохраню последний ответ в Word или PDF\n"
            "- Письмо с фото - составлю ответ\n"
            "/cancel - отменить текущий запрос"
        )
        return

    # Сохранение в файл
    if any(w in tl for w in ["сохрани", "в ворд", "в pdf", "сделай файл", "скачать"]):
        await save_last(update, context)
        return

    # Обычный чат
    photo_ctx = context.user_data.get("last_photo_desc", "")
    system = CHAT_SYSTEM
    if photo_ctx:
        system += f"\n\nКонтекст последнего фото: {photo_ctx}"

    history = context.user_data.get("chat_history", [])
    history.append({"role": "user", "content": text})
    if len(history) > 20: history = history[-20:]

    await update.message.reply_text("Думаю...")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000, system=system,
            tools=[WEB_SEARCH_TOOL], messages=history,
        )
        msgs = list(history)
        while response.stop_reason == "tool_use":
            tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query", "")}
                  for b in response.content if b.type == "tool_use"]
            msgs.append({"role": "assistant", "content": response.content})
            msgs.append({"role": "user", "content": tr})
            response = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=2000, system=system,
                tools=[WEB_SEARCH_TOOL], messages=msgs)

        reply = "".join(b.text for b in response.content if hasattr(b, "text"))
        if not reply: reply = "Не знаю что ответить. Попробуй иначе."

        remember(uid, reply)
        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history

        if len(reply) > 4000: reply = reply[:4000] + "..."
        await update.message.reply_text(reply)

        if len(reply) > 500:
            await update.message.reply_text(
                "Сохранить в файл?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Сохранить", callback_data="save_to_word")]])
            )
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось. Попробуй еще раз.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Смотрю на фото и ищу инфу...")
    result = await get_image_b64(update)
    if not result:
        await update.message.reply_text("Не смог получить фото.")
        return
    b64, mime = result
    caption = update.message.caption or ""
    try:
        vision = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": f"Проанализируй:\n1. Опиши что изображено\n2. Распознай текст если есть\n3. Предложи поисковый запрос\n{'Запрос: ' + caption if caption else ''}\n\nФормат:\nОПИСАНИЕ: ...\nТЕКСТ: ...\nПОИСК: ..."}
            ]}]
        )
        vtext = vision.content[0].text
        desc = ocr = ""
        search_q = caption or "информация по изображению"
        for line in vtext.split("\n"):
            if line.startswith("ОПИСАНИЕ:"): desc = line.replace("ОПИСАНИЕ:", "").strip()
            elif line.startswith("ТЕКСТ:"): ocr = line.replace("ТЕКСТ:", "").strip()
            elif line.startswith("ПОИСК:"): search_q = line.replace("ПОИСК:", "").strip()

        uid = update.effective_user.id
        photo_mem = desc
        if ocr and ocr != "текста нет": photo_mem += f"\nТекст: {ocr}"
        context.user_data["last_photo_desc"] = photo_mem
        context.user_data["last_photo_text"] = ocr if ocr != "текста нет" else ""

        # Поиск
        sr = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500, tools=[WEB_SEARCH_TOOL],
            messages=[{"role": "user", "content": f"Найди: {search_q}"}]
        )
        sm = [{"role": "user", "content": f"Найди: {search_q}"}]
        while sr.stop_reason == "tool_use":
            tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query", "")} for b in sr.content if b.type == "tool_use"]
            sm.append({"role": "assistant", "content": sr.content})
            sm.append({"role": "user", "content": tr})
            sr = claude.messages.create(model="claude-sonnet-4-5", max_tokens=1500, tools=[WEB_SEARCH_TOOL], messages=sm)
        search_text = "".join(b.text for b in sr.content if hasattr(b, "text"))

        parts = []
        if desc: parts.append(f"На фото: {desc}")
        if ocr and ocr != "текста нет": parts.append(f"\nТекст с фото:\n{ocr}")
        if search_text: parts.append(f"\nНашел в интернете:\n{search_text}")
        final = "\n".join(parts)
        if len(final) > 4000: final = final[:4000] + "..."

        remember(uid, final)
        await update.message.reply_text(final)

        all_text = (desc + ocr).lower()
        if any(w in all_text for w in ["уважаем", "прошу", "сообщаем", "исх.", "вх."]):
            await update.message.reply_text(
                "Похоже на официальное письмо. Составить ответ?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Составить ответ", callback_data="write_reply")]])
            )
    except Exception as e:
        logger.error(f"Photo error: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать фото.")

async def cb_photo_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "write_reply":
        await q.edit_message_text("Напиши что ответить - составлю письмо в Word или PDF.")
        context.user_data["waiting_letter"] = True
        context.user_data["letter_original"] = context.user_data.get("last_photo_text", "")

async def gen_letter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    instructions = await get_text(update)
    original = context.user_data.pop("letter_original", "")
    context.user_data.pop("waiting_letter", None)
    uid = update.effective_user.id
    await update.message.reply_text("Составляю письмо...")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000,
            system="Составляешь официальные деловые письма на русском. Профессионально.",
            messages=[{"role": "user", "content": f"Оригинал:\n{original}\n\nИнструкции:\n{instructions}\n\nСоставь ответное письмо."}]
        )
        content = response.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "letter", "name": "Otvet"}
        await update.message.reply_text("Письмо готово! В каком формате сохранить?", reply_markup=save_kb())
    except Exception as e:
        logger.error(f"Letter error: {e}", exc_info=True)
        await update.message.reply_text("Не смог составить письмо.")

async def save_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msgs_list = bot_msgs.get(uid, [])
    content = msgs_list[-1] if msgs_list else None
    if not content:
        history = context.user_data.get("chat_history", [])
        for m in reversed(history):
            if m["role"] == "assistant" and isinstance(m["content"], str):
                content = m["content"]; break
    if not content:
        await update.message.reply_text("Нечего сохранять. Сначала задай вопрос!")
        return
    last_doc[uid] = {"content": content, "type": "chat", "name": "Dokument"}
    await update.message.reply_text("В каком формате сохранить?", reply_markup=save_kb())

async def cb_save_to_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    msgs_list = bot_msgs.get(uid, [])
    if msgs_list:
        last_doc[uid] = {"content": msgs_list[-1], "type": "chat", "name": "Dokument"}
    await q.edit_message_text("В каком формате сохранить?", reply_markup=save_kb())

async def keyboard_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Перехватывает нажатия постоянных кнопок клавиатуры из любого состояния."""
    text = update.message.text if update.message.text else ""
    uid = update.effective_user.id

    if text == "Новый запрос":
        sessions.pop(uid, None)
        last_doc.pop(uid, None)
        context.user_data.clear()
        await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
        return True  # Обработано

    if text == "Помощь":
        await update.message.reply_text(
            "Что умею:\n\n"
            "- Кнопка 'Новый запрос' — создать ТЗ, критерии, сценарий переговоров\n"
            "- Любой вопрос — отвечу\n"
            "- Ищу в интернете\n"
            "- Фото — опишу, найду инфу, распознаю текст\n"
            "- 'сохрани' — сохраню последний ответ в Word или PDF\n"
            "/cancel — отменить текущий запрос"
        )
        return True

    return False  # Не обработано


# ─── Запуск ─────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND
    photo_f = filters.PHOTO | filters.Document.IMAGE
    doc_f = filters.Document.ALL

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", cmd_new),
            CallbackQueryHandler(cb_menu, pattern="^menu_"),
        ],
        states={
            CHOOSING: [
                CallbackQueryHandler(cb_menu, pattern="^menu_"),
                CallbackQueryHandler(cb_direction, pattern="^dir_"),
                CallbackQueryHandler(cb_hasdoc, pattern="^hasdoc_"),
            ],
            WAITING_DOC: [
                MessageHandler(doc_f, receive_customer_doc),
                MessageHandler(tv, lambda u, c: u.message.reply_text("Загрузи файл или /cancel")),
            ],
            NEGOTIATION: [
                CallbackQueryHandler(cb_neg_answer, pattern="^neg_"),
                MessageHandler(tv, neg_text_answer),
            ],
            ANSWERING: [
                CallbackQueryHandler(cb_answer, pattern="^ans_"),
                MessageHandler(tv, text_answer),
            ],
            CRITERIA_Q: [
                CallbackQueryHandler(cb_criteria_q, pattern="^(yes|no)_criteria$"),
            ],
            CRITERIA_CONF: [
                CallbackQueryHandler(cb_criteria_conf, pattern="^criteria_"),
                CallbackQueryHandler(cb_del_criterion, pattern="^del_criterion_"),
                MessageHandler(tv, handle_criteria_edit),
            ],
            SAVE_FORMAT: [
                CallbackQueryHandler(cb_save_format, pattern="^fmt_"),
            ],
            REVIEWING: [
                CallbackQueryHandler(cb_review, pattern="^review_"),
                MessageHandler(tv, apply_edits),
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_photo_actions, pattern="^write_reply$"))
    app.add_handler(CallbackQueryHandler(cb_save_to_word, pattern="^save_to_word$"))
    app.add_handler(MessageHandler(photo_f, chat_handler))
    app.add_handler(MessageHandler(tv, chat_handler))

    logger.info("Bot v10 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
