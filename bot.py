"""
Telegram-бот v12 — финальная версия после ревизии.

Правила вывода:
- ТЗ           → сразу Word
- Критерии     → сразу Word (таблица 4 колонки)
- Переговоры   → Word или PDF на выбор
- Письмо       → Word или PDF на выбор
- Фото         → описание + поиск в чате
- Ответ на письмо (фото) → сразу Word
- Чат          → только Word по запросу ("сохрани")
- После документа → спрашивает о замечаниях
"""

import os, json, logging, base64, tempfile
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

# Состояния
CHOOSING     = 1
ANSWERING    = 2
CRITERIA_Q   = 3
REVIEWING    = 4
NEGOTIATION  = 5
WAITING_DOC  = 6
SAVE_FORMAT  = 7  # Word+PDF выбор (переговоры, письмо)
LETTER_TYPE  = 8
LETTER_PHOTO = 9
LETTER_TASK  = 10
ANALYSIS_DOC = 11  # Ждём документ для анализа
ANALYSIS_QA  = 12  # Уточняющие вопросы после анализа
DOC_RECEIVED = 13  # Получили файл в чате, спрашиваем что делать
DOC_EDIT_CMT = 14  # Ждём комментарии для редактуры

sessions: dict[int, TenderAgent] = {}
last_doc: dict[int, dict]        = {}
bot_msgs: dict[int, list[str]]   = {}

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

# ─── Доступ ──────────────────────────────────────────────────────────────────
USERS_FILE   = "/tmp/bot_users.json"
DYNAMIC_FILE = "/tmp/dynamic_users.json"
ALLOWED_USERS: set[int] = set()
for _s in os.environ.get("ALLOWED_USERS", "").split(","):
    try: ALLOWED_USERS.add(int(_s.strip()))
    except ValueError: pass

def _load_dynamic():
    try:
        if os.path.exists(DYNAMIC_FILE):
            return set(json.load(open(DYNAMIC_FILE)).get("users", []))
    except: pass
    return set()

def _save_dynamic(s):
    try: json.dump({"users": list(s)}, open(DYNAMIC_FILE, "w"))
    except Exception as e: logger.error(e)

DYNAMIC_USERS: set[int] = _load_dynamic()

def is_allowed(uid: int) -> bool:
    if not ALLOWED_USERS and not DYNAMIC_USERS: return True
    return uid in ALLOWED_USERS or uid in DYNAMIC_USERS

def save_user(uid: int, username: str = ""):
    try:
        data = json.load(open(USERS_FILE)) if os.path.exists(USERS_FILE) else {}
        data[str(uid)] = username
        json.dump(data, open(USERS_FILE, "w"))
    except Exception as e: logger.error(e)

def get_all_users():
    try:
        if os.path.exists(USERS_FILE): return json.load(open(USERS_FILE))
    except: pass
    return {}

def remember(uid: int, text: str):
    bot_msgs.setdefault(uid, []).append(text)
    if len(bot_msgs[uid]) > 20: bot_msgs[uid] = bot_msgs[uid][-20:]

# ─── Промпты ─────────────────────────────────────────────────────────────────
CHAT_SYSTEM = """Ты - Джарвис, умный и немного саркастичный помощник по коммерческим закупкам. Как Джарвис из Железного человека: чёткий, профессиональный, с лёгкой иронией. Помогаешь всегда, но без лишней воды.

ВАЖНО: Ты эксперт именно по КОММЕРЧЕСКИМ закупкам (не государственным). Закупки только на РАБОТЫ и УСЛУГИ — товары не твоя тема. Если спрашивают про госзакупки (44-ФЗ, 223-ФЗ) — можешь ответить в общих чертах, но уточни что специализируешься на коммерческих закупках работ и услуг.

Помни весь контекст разговора включая фото.
Шутки: "А самому слабо?", "Опять ты...", "Конец рабочего дня, но ладно".
Смайлики активно. Русский язык."""

REVIEW_SYSTEM = "Эксперт по тендерам. Внеси правки и верни ПОЛНЫЙ исправленный текст документа. Только текст."

NEGOTIATION_SYSTEM = """Эксперт по закупочным переговорам. Цель: снижение цены и улучшение условий.
Сценарий без воды:
1. ПОЗИЦИЯ ЗАКУПЩИКА
2. ОТКРЫТИЕ (2-3 варианта первых фраз)
3. АРГУМЕНТЫ для давления на цену
4. ВОЗРАЖЕНИЯ участника и точные ответы
5. ЗАКРЫТИЕ сделки
Только конкретные фразы. Русский язык."""

ANALYSIS_SYSTEM = """Ты опытный эксперт по коммерческим закупкам работ и услуг.
Твоя задача — провести экспертизу проекта технического задания организатора коммерческого тендера.
Специализация: только работы и услуги (клининг, IT, ремонт, строительство, консалтинг и т.д.). Не товарные закупки.

Анализируй документ по следующим критериям:

1. ОШИБКИ И ПРОТИВОРЕЧИЯ
   - Внутренние противоречия в требованиях
   - Несоответствия между разделами
   - Некорректные ссылки на нормативные акты

2. ОГРАНИЧИВАЮЩИЕ ТРЕБОВАНИЯ (риск жалобы в ФАС)
   - Требования под конкретного поставщика
   - Избыточный опыт или квалификация
   - Нестандартные технические характеристики
   - Требования конкретных торговых марок без указания эквивалента

3. НЕЧЁТКИЕ ФОРМУЛИРОВКИ
   - Размытые критерии без измеримых показателей
   - Требования которые участник может трактовать по-своему
   - Отсутствие единиц измерения

4. НЕДОСТАЮЩИЕ ТРЕБОВАНИЯ
   - Важные условия которые не прописаны
   - Отсутствие порядка сдачи-приёмки
   - Нет требований к квалификации персонала (если нужны)

5. КОНКРЕТНЫЕ ПРЕДЛОЖЕНИЯ
   - По каждому замечанию — как именно исправить
   - Предлагай точные формулировки

Будь конкретным. Давай номера пунктов документа где найдены проблемы.
Пиши деловым стилем, по-русски."""

LETTER_SYSTEM = """Составляешь официальные деловые письма на русском языке.
Профессионально, структурированно, вежливо.
Начни с обращения, изложи суть, закончи подписью.
Только текст письма."""

NEGOTIATION_STEPS = [
    {"q": "Что закупаем?", "opts": ["Клининговые услуги", "IT-услуги", "Ремонт оборудования", "Строительные работы", "Консалтинг / аудит", "Другие услуги"], "free": True},
    {"q": "Кто придёт от участника?", "opts": ["Директор/собственник", "Коммерческий директор", "Менеджер по продажам", "Неизвестно"], "free": False},
    {"q": "НМЦ (начальная цена)?", "opts": ["До 1 млн руб.", "1–5 млн руб.", "5–20 млн руб.", "Более 20 млн руб."], "free": True},
    {"q": "На сколько снижаем цену?", "opts": ["На 5–10%", "На 10–20%", "На 20–30%", "Максимально"], "free": False},
    {"q": "Есть альтернативные участники?", "opts": ["Да, 2+ конкурента", "Есть 1 альтернатива", "Нет, единственный"], "free": False},
    {"q": "Доп. цели переговоров?", "opts": ["Только снижение цены", "Цена + сроки", "Цена + гарантии", "Цена + объём работ"], "free": False},
]

# ─── Вспомогательные ─────────────────────────────────────────────────────────
async def get_text(update: Update) -> str | None:
    if update.message.text: return update.message.text.strip()
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
    fname = doc.file_name or ""; mime = doc.mime_type or ""
    file = await doc.get_file()
    data = bytes(await file.download_as_bytearray())
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(fname)[1] or ".bin")
    tmp.write(data); tmp.close()
    try:
        if "pdf" in mime or fname.lower().endswith(".pdf"):
            from pypdf import PdfReader
            return "\n".join(p.extract_text() or "" for p in PdfReader(tmp.name).pages).strip()
        elif "word" in mime or fname.lower().endswith((".docx", ".doc")):
            from docx import Document as D
            return "\n".join(p.text for p in D(tmp.name).paragraphs if p.text.strip())
        elif "text" in mime or fname.lower().endswith(".txt"):
            return data.decode("utf-8", errors="replace").strip()
    except Exception as e: logger.error(f"Doc: {e}")
    finally: os.remove(tmp.name)
    return None

async def send_q(msg, result: dict):
    text = result["question"]; opts = result.get("options", [])
    if opts:
        kb = [[InlineKeyboardButton(o, callback_data=f"ans_{i}")] for i, o in enumerate(opts)]
        kb.append([InlineKeyboardButton("Свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text)

async def send_docx(msg, path: str, filename: str, caption: str):
    with open(path, "rb") as f:
        await msg.reply_document(document=f, filename=filename, caption=caption)
    os.remove(path)

async def send_pdf(msg, path: str, filename: str, caption: str):
    with open(path, "rb") as f:
        await msg.reply_document(document=f, filename=filename, caption=caption)
    os.remove(path)

def detect_intent(text: str) -> str | None:
    """Определяет намерение пользователя по тексту."""
    tl = text.lower().strip()

    # ТЗ + критерии вместе
    if any(w in tl for w in ["тз и критерии", "техзадание и критерии",
                               "и тз и критерии", "тз с критериями",
                               "техническое задание и критерии"]):
        return "menu_both"

    # Техническое задание
    # Анализ — проверяем РАНЬШЕ чем ТЗ, чтобы "проверь тз" шло в анализ
    analysis_early = ["проверь", "проанализируй", "экспертиза", "найди ошибки", "разбери"]
    if any(w in tl for w in analysis_early):
        return "menu_analysis"

    tz_words = ["тз", "техзадание", "техническое задание", "техническом задании",
                "технического задания", "техническое задан"]
    tz_actions = ["нужно", "нужен", "хочу", "хочется", "сделай", "составь",
                  "напиши", "подготовь", "создай", "помоги с", "нужна помощь с"]
    if any(w in tl for w in tz_words):
        if any(a in tl for a in tz_actions) or len(tl.split()) <= 3:
            return "menu_tz"

    # Критерии
    crit_words = ["критерии", "критериев", "критериях", "критерий допуска",
                  "критерии допуска", "требования к участникам"]
    if any(w in tl for w in crit_words):
        if any(a in tl for a in ["нужно", "нужны", "хочу", "сделай", "составь",
                                   "напиши", "подготовь", "создай"]) or len(tl.split()) <= 3:
            return "menu_criteria"

    # Переговоры
    neg_words = ["переговор", "переговоры", "сценарий переговор", "скрипт"]
    if any(w in tl for w in neg_words):
        return "menu_negotiation"

    # Анализ (расширенный)
    if any(w in tl for w in ["анализ документа", "проверить документ", "анализ тз"]):
        return "menu_analysis"

    # Письмо
    letter_words = ["письмо", "письма", "письме"]
    if any(w in tl for w in letter_words):
        if any(a in tl for a in ["напиши", "составь", "нужно", "хочу",
                                   "подготовь", "создай", "ответ на"]):
            return "menu_letter"

    return None


async def _handle_intent(update, context, intent: str):
    """Запускает нужный режим по распознанному намерению."""
    uid = update.effective_user.id
    sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
    if intent == "menu_negotiation":
        context.user_data["neg_answers"] = {}
        context.user_data["neg_step"] = 0
        await update.message.reply_text(
            "Запускаю сценарий переговоров!\n\n" + NEGOTIATION_STEPS[0]["q"],
            reply_markup=neg_kb(0)
        )
    elif intent == "menu_analysis":
        context.user_data["intent_analysis"] = True
        await update.message.reply_text(
            "Загрузи проект ТЗ (Word, PDF или txt) - "
            "найду ошибки, противоречия, завышенные требования "
            "и дам конкретные предложения по исправлению."
        )
    elif intent == "menu_letter":
        await update.message.reply_text("Что нужно?", reply_markup=letter_type_kb())
    else:
        doc_map = {"menu_tz": "tz_only", "menu_criteria": "criteria_only", "menu_both": "both"}
        context.user_data["doc_type"] = doc_map.get(intent, "tz_only")
        await update.message.reply_text(
            "Выбери направление закупки:", reply_markup=dir_kb()
        )


def nav_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/new — Новый документ"), KeyboardButton("/help — Помощь")]],
        resize_keyboard=True, is_persistent=True,
    )

def menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Техническое задание", callback_data="menu_tz")],
        [InlineKeyboardButton("📋 Критерии допуска", callback_data="menu_criteria")],
        [InlineKeyboardButton("📄+📋 ТЗ и критерии", callback_data="menu_both")],
        [InlineKeyboardButton("🤝 Сценарий переговоров", callback_data="menu_negotiation")],
        [InlineKeyboardButton("✉️ Написать письмо", callback_data="menu_letter")],
        [InlineKeyboardButton("🔎 Анализ документа", callback_data="menu_analysis")],
    ])

def dir_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("💻 IT-услуги и автоматизация", callback_data="dir_it")],
        [InlineKeyboardButton("🔧 Ремонт и техобслуживание", callback_data="dir_repair")],
    ])

def hasdoc_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Да, загружу документ", callback_data="hasdoc_yes")],
        [InlineKeyboardButton("Нет, задавай вопросы", callback_data="hasdoc_no")],
    ])

def review_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Всё отлично!", callback_data="review_ok")],
        [InlineKeyboardButton("✏️ Есть замечания", callback_data="review_edit")],
    ])

def word_pdf_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Word", callback_data="fmt_docx")],
        [InlineKeyboardButton("📕 PDF", callback_data="fmt_pdf")],
        [InlineKeyboardButton("📄+📕 Оба", callback_data="fmt_both")],
    ])

def neg_kb(step: int):
    s = NEGOTIATION_STEPS[step]
    btns = [[InlineKeyboardButton(o, callback_data=f"neg_{step}_{i}")] for i, o in enumerate(s["opts"])]
    if s["free"]: btns.append([InlineKeyboardButton("Свой вариант", callback_data=f"neg_{step}_custom")])
    return InlineKeyboardMarkup(btns)

def letter_type_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Ответ на письмо", callback_data="letter_reply")],
        [InlineKeyboardButton("📝 Новое письмо", callback_data="letter_new")],
    ])

def doc_action_kb():
    """Что делать с полученным документом."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Проанализировать (экспертиза)", callback_data="docact_analyze")],
        [InlineKeyboardButton("✏️ Отредактировать (по комментариям)", callback_data="docact_edit")],
        [InlineKeyboardButton("💬 Просто обсудить содержимое", callback_data="docact_discuss")],
    ])

async def check_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    text = (update.message.text or "") if update.message else ""
    uid = update.effective_user.id
    if "/new" in text:
        sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
        await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
        return True
    if "/help" in text:
        await update.message.reply_text(
            "Возможности:\n\n"
            "📄 ТЗ — техническое задание\n"
            "📋 Критерии допуска — таблица требований\n"
            "🤝 Переговоры — сценарий снижения цены\n"
            "✉️ Письмо — деловое письмо или ответ\n\n"
            "💬 Просто напиши вопрос — отвечу\n"
            "📷 Фото — опишу и найду инфу\n"
            "📝 'сохрани' — сохраню ответ в Word\n"
            "/cancel — отменить",
            reply_markup=nav_kb()
        )
        return True
    return False

# ─── Команды ─────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("Нет доступа. Обратись к администратору."); return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None); context.user_data.clear()
    await update.message.reply_text(
        "Привет! Я Джарвис — ваш персональный помощник по тендерам.\n\n"
        "Нажми /new чтобы создать документ или просто напиши что нужно!",
        reply_markup=nav_kb()
    )

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("Нет доступа."); return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None); context.user_data.clear()
    await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
    return CHOOSING

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
    await update.message.reply_text("Отменено. /new для нового запроса.", reply_markup=nav_kb())
    return ConversationHandler.END

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Возможности:\n\n📄 /new → ТЗ, критерии, переговоры, письмо\n"
        "💬 Напиши вопрос — отвечу\n📷 Фото — опишу\n"
        "📝 'сохрани' — Word файл\n/cancel — отменить",
        reply_markup=nav_kb()
    )

async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != int(os.environ.get("ADMIN_ID", "0")): await update.message.reply_text("Только для администратора."); return
    all_users = get_all_users(); whitelist = ALLOWED_USERS | DYNAMIC_USERS
    lines = ["Пользователи:\n"]
    for uid_str, uname in all_users.items():
        s = "OK" if int(uid_str) in whitelist else "NO"
        lines.append(f"[{s}] {uid_str} — @{uname or '—'}")
    lines += [f"\nВ белом списке: {len(whitelist)}", "Добавить: /adduser ID", "Убрать: /removeuser ID"]
    await update.message.reply_text("\n".join(lines))

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.environ.get("ADMIN_ID", "0")): await update.message.reply_text("Только для администратора."); return
    if not context.args: await update.message.reply_text("Укажи ID: /adduser 123456789"); return
    try: new_uid = int(context.args[0])
    except: await update.message.reply_text("Неверный ID."); return
    DYNAMIC_USERS.add(new_uid); _save_dynamic(DYNAMIC_USERS)
    try: await context.bot.send_message(chat_id=new_uid, text="Тебе открыт доступ! Напиши /start"); notified = "Уведомлён."
    except: notified = "Не смог уведомить."
    await update.message.reply_text(f"Пользователь {new_uid} добавлен. {notified}")

async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.environ.get("ADMIN_ID", "0")): await update.message.reply_text("Только для администратора."); return
    if not context.args: await update.message.reply_text("Укажи ID: /removeuser 123456789"); return
    try: rem_uid = int(context.args[0])
    except: await update.message.reply_text("Неверный ID."); return
    if rem_uid in DYNAMIC_USERS: DYNAMIC_USERS.discard(rem_uid); _save_dynamic(DYNAMIC_USERS); await update.message.reply_text(f"Пользователь {rem_uid} удалён.")
    elif rem_uid in ALLOWED_USERS: await update.message.reply_text(f"{rem_uid} в ALLOWED_USERS на Railway — удали там вручную.")
    else: await update.message.reply_text(f"{rem_uid} не найден.")

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != int(os.environ.get("ADMIN_ID", "0")): await update.message.reply_text("Только для администратора."); return
    text = " ".join(context.args) if context.args else ""
    if not text: await update.message.reply_text("Напиши:\n/broadcast текст"); return
    users = get_all_users(); sent = failed = 0
    await update.message.reply_text(f"Рассылаю {len(users)} пользователям...")
    for uid_str in users:
        try: await context.bot.send_message(chat_id=int(uid_str), text=text); sent += 1
        except: failed += 1
    await update.message.reply_text(f"Отправлено: {sent}, не доставлено: {failed}")

# ─── Меню ────────────────────────────────────────────────────────────────────
async def cb_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    if not is_allowed(uid): await q.edit_message_text("Нет доступа."); return ConversationHandler.END

    if q.data == "menu_negotiation":
        context.user_data["neg_answers"] = {}; context.user_data["neg_step"] = 0
        await q.edit_message_text(NEGOTIATION_STEPS[0]["q"], reply_markup=neg_kb(0))
        return NEGOTIATION

    if q.data == "menu_letter":
        await q.edit_message_text("Что нужно?", reply_markup=letter_type_kb())
        return LETTER_TYPE

    if q.data == "menu_analysis":
        await q.edit_message_text(
            "Анализ документа - экспертиза вашего ТЗ перед публикацией.\n\n"
            "Загрузи проект ТЗ (Word, PDF или txt).\n"
            "Найду ошибки, противоречия, завышенные требования "
            "и дам конкретные предложения по исправлению."
        )
        return ANALYSIS_DOC

    doc_map = {"menu_tz": "tz_only", "menu_criteria": "criteria_only", "menu_both": "both"}
    context.user_data["doc_type"] = doc_map.get(q.data, "tz_only")
    await q.edit_message_text("Выбери направление:", reply_markup=dir_kb())
    return CHOOSING

# ─── Письмо ──────────────────────────────────────────────────────────────────
async def cb_letter_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "letter_reply":
        context.user_data["letter_mode"] = "reply"
        await q.edit_message_text("Отправь фото письма или загрузи файл (Word/PDF/txt).\nИли напиши текст письма вручную:")
        return LETTER_PHOTO
    else:
        context.user_data["letter_mode"] = "new"; context.user_data["letter_original"] = ""
        await q.edit_message_text("Расскажи что написать — кому, по какому поводу, что сообщить:")
        return LETTER_TASK

async def receive_letter_original(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_nav(update, context): return ConversationHandler.END
    original_text = ""

    # Фото письма
    if update.message.photo or (update.message.document and update.message.document.mime_type and
       update.message.document.mime_type.startswith("image/")):
        result = await get_image_b64(update)
        if result:
            b64, mime = result
            await update.message.reply_text("Читаю письмо...")
            resp = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=2000,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": "Распознай весь текст с этого изображения письма. Выведи только текст."}
                ]}]
            )
            original_text = resp.content[0].text
    # Документ
    elif update.message.document:
        await update.message.reply_text("Читаю документ...")
        original_text = await extract_doc_text(update) or ""
    # Текст
    elif update.message.text:
        original_text = update.message.text.strip()

    if not original_text:
        await update.message.reply_text("Не смог прочитать. Попробуй ещё раз или напиши текст вручную.")
        return LETTER_PHOTO

    context.user_data["letter_original"] = original_text
    await update.message.reply_text("Прочитал! Теперь скажи что именно ответить:")
    return LETTER_TASK

async def receive_letter_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx
    if await check_nav(update, context): return ConversationHandler.END
    task = await get_text(update)
    if not task: return LETTER_TASK
    uid = update.effective_user.id
    original = context.user_data.get("letter_original", "")
    mode = context.user_data.get("letter_mode", "new")
    await update.message.reply_text("Составляю письмо...")
    try:
        if mode == "reply" and original:
            prompt = f"Оригинальное письмо:\n{original}\n\nЗадание: {task}\n\nСоставь ответное письмо."
        else:
            prompt = f"Задание: {task}\n\nСоставь деловое письмо."
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000, system=LETTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        content = resp.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "letter", "name": "Pismo"}
        await update.message.reply_text("В каком формате сохранить?", reply_markup=word_pdf_kb())
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Letter: {e}", exc_info=True)
        await update.message.reply_text("Ошибка. Попробуй ещё раз.")
        return LETTER_TASK

# ─── Направление и вопросы ───────────────────────────────────────────────────
async def cb_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    context.user_data["direction"] = q.data.replace("dir_", "")
    await q.edit_message_text("Есть документ от заказчика? (ТЗ-черновик, письмо)\nЕсли да — загрузи, задам только недостающие вопросы.", reply_markup=hasdoc_kb())
    return CHOOSING

async def cb_hasdoc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "hasdoc_yes":
        await q.edit_message_text("Загрузи документ (Word, PDF или txt):")
        return WAITING_DOC
    return await _start_questions(q.message, context)

async def receive_customer_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document: await update.message.reply_text("Загрузи файл или /cancel"); return WAITING_DOC
    await update.message.reply_text("Читаю документ...")
    doc_text = await extract_doc_text(update)
    if not doc_text: await update.message.reply_text("Не смог прочитать. Попробуй другой формат."); return WAITING_DOC
    await update.message.reply_text("Анализирую что уже есть...")
    direction = context.user_data.get("direction", "cleaning")
    from agent import QUESTIONS
    q_list = "\n".join(f"- {q['question']}" for q in QUESTIONS.get(direction, []))
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000,
            system='Проанализируй документ. Верни JSON: {"found": {"вопрос": "ответ"}, "missing": ["вопрос"]}. Только JSON.',
            messages=[{"role": "user", "content": f"Вопросы:\n{q_list}\n\nДокумент:\n{doc_text[:6000]}"}]
        )
        import re
        raw = re.sub(r'```.*?```', '', resp.content[0].text, flags=re.DOTALL).strip()
        analysis = json.loads(raw)
    except:
        analysis = {"found": {}, "missing": [q["question"] for q in QUESTIONS.get(direction, [])]}
    found = analysis.get("found", {}); missing = analysis.get("missing", [])
    context.user_data["prefilled"] = found
    if found:
        await update.message.reply_text("Нашёл в документе:\n" + "\n".join(f"- {k}: {v}" for k,v in found.items()) + f"\n\nОсталось уточнить: {len(missing)} вопр.")
    uid = update.effective_user.id; doc_type = context.user_data.get("doc_type", "tz_only")
    all_q = QUESTIONS.get(direction, [])
    filtered = [q for q in all_q if any(m.lower() in q["question"].lower() for m in missing)] if missing else []
    agent = TenderAgent(direction=direction, doc_type=doc_type)
    if filtered: agent.questions = filtered
    for fq, fa in found.items(): agent.answers.append({"question": fq, "answer": fa})
    sessions[uid] = agent
    if not missing: await update.message.reply_text("Все данные есть! Генерирую..."); return await do_generate(update, context)
    result = await agent.get_next_question()
    await send_q(update.message, result)
    return ANSWERING

async def _start_questions(msg, context: ContextTypes.DEFAULT_TYPE):
    uid = msg.chat_id
    agent = TenderAgent(direction=context.user_data.get("direction","cleaning"), doc_type=context.user_data.get("doc_type","tz_only"))
    sessions[uid] = agent
    result = await agent.get_next_question()
    await send_q(msg, result)
    return ANSWERING

async def cb_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent: await q.edit_message_text("Сессия устарела. /new"); return ConversationHandler.END
    if q.data == "ans_custom": await q.edit_message_text(q.message.text + "\n\nВведи свой вариант:"); return ANSWERING
    idx = int(q.data.replace("ans_", ""))
    opts = agent.last_question.get("options", [])
    answer = opts[idx] if idx < len(opts) else ""
    await q.edit_message_text(f"{agent.last_question['question']}\n> {answer}")
    return await _handle_answer(update, context, answer)

async def text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = await get_text(update)
    if not text: return ANSWERING
    # Только /new и /cancel прерывают опрос
    if text.strip() in ("/new", "/new — Новый документ"):
        sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
        await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
        return ConversationHandler.END
    agent = sessions.get(uid)
    if not agent:
        await update.message.reply_text("Сессия устарела. /new")
        return ConversationHandler.END
    return await _handle_answer(update, context, text)

async def _handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: str):
    uid = update.effective_user.id; agent = sessions[uid]
    prefilled = context.user_data.get("prefilled", {})
    if prefilled and not any(a.get("prefilled") for a in agent.answers):
        for fq, fa in prefilled.items(): agent.answers.append({"question": fq, "answer": fa, "prefilled": True})
    result = await agent.submit_answer(answer)
    msg = update.callback_query.message if update.callback_query else update.message
    if result["status"] == "question": await send_q(msg, result); return ANSWERING
    await msg.reply_text("Данные собраны! Генерирую...")
    return await do_generate(update, context)

# ─── Генерация ───────────────────────────────────────────────────────────────
async def do_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text("Сессия устарела. /new"); return ConversationHandler.END
    msg = update.callback_query.message if update.callback_query else update.message
    try:
        # ТЗ — сразу Word
        if agent.doc_type in ("tz_only", "both"):
            await msg.reply_text("Генерирую ТЗ...")
            content = await agent.generate_tz()
            remember(uid, content)
            last_doc[uid] = {"content": content, "type": "tz", "name": agent.tender_name}
            path = await generate_tz_docx(content, agent.tender_name)
            await send_docx(msg, path, f"TZ_{agent.tender_name[:30]}.docx", "📄 Техническое задание готово!")

        # Критерии — сразу Word
        if agent.doc_type in ("criteria_only", "both"):
            return await _gen_criteria(msg, uid, agent, context)

        # После ТЗ без критериев — предлагаем критерии
        if agent.doc_type == "tz_only":
            await msg.reply_text(
                "Нужны критерии допуска?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Да", callback_data="yes_criteria")],
                    [InlineKeyboardButton("Нет, всё готово", callback_data="no_criteria")],
                ])
            )
            return CRITERIA_Q

        await msg.reply_text("Всё устраивает?", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Generate: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так. /cancel и попробуй заново.")
        return ConversationHandler.END

async def _gen_criteria(msg, uid: int, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_criteria_docx
    from examples_loader import load_examples
    examples_texts = load_examples("criteria")
    examples_block = ""
    if examples_texts:
        examples_block = "ПРИМЕРЫ КРИТЕРИЕВ ИЗ РЕАЛЬНЫХ ТЕНДЕРОВ:\n\n"
        for i, t in enumerate(examples_texts[:5], 1):
            examples_block += f"=== Пример {i} ===\n{t[:3000]}\n\n"
    system = (
        f"Ты эксперт по тендерам и закупкам.\n\n"
        f"{examples_block}"
        f"Составь критерии допуска участников. Используй примеры как образец по уровню детализации.\n\n"
        f"Для каждого критерия строго в формате:\n"
        f"КРИТЕРИЙ: [краткое название]\n"
        f"ТРЕБОВАНИЕ: [конкретное измеримое требование]\n"
        f"ДОКУМЕНТ: [что предоставить]\n\n"
        f"Составь 6-10 критериев. Только список, без заголовков."
    )
    try:
        await msg.reply_text("Генерирую критерии допуска...")
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=3000, system=system,
            messages=[{"role": "user", "content": f"Данные закупки:\n{agent._context()}"}]
        )
        content = resp.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        path = await generate_criteria_docx(content, agent.tender_name)
        await send_docx(msg, path, f"Kriterii_{agent.tender_name[:25]}.docx", "📋 Критерии допуска готовы!")
        await msg.reply_text("Всё устраивает? Если есть замечания — напиши что изменить.", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Criteria: {e}", exc_info=True)
        await msg.reply_text("Ошибка генерации критериев. /cancel и попробуй заново.")
        return ConversationHandler.END

async def cb_criteria_q(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id; agent = sessions.get(uid)
    if q.data == "no_criteria":
        await q.edit_message_text("Всё устраивает?", reply_markup=review_kb()); return REVIEWING
    return await _gen_criteria(q.message, uid, agent, context)

# ─── Сохранение (Word/PDF выбор — для переговоров и писем) ───────────────────
async def cb_save_format(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx, generate_pdf
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    doc_info = last_doc.get(uid, {})
    if not doc_info: await q.edit_message_text("Не нашёл документ."); return ConversationHandler.END
    fmt = q.data.replace("fmt_", "")
    content = doc_info["content"]; doc_type = doc_info.get("type","tz"); name = doc_info.get("name","Doc")
    if doc_type == "letter": gen_docx = lambda: generate_tz_docx(content, name); base = "Pismo"
    elif doc_type == "negotiation": gen_docx = lambda: generate_tz_docx(content, name); base = "Scenariy"
    else: gen_docx = lambda: generate_tz_docx(content, name); base = f"Doc_{name[:20]}"
    await q.edit_message_text("Создаю файл(ы)...")
    try:
        chat_id = q.message.chat_id
        if fmt in ("docx", "both"):
            path = await gen_docx()
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=f"{base}.docx",
                    caption="📄 Word готов!"
                )
            os.remove(path)
        if fmt in ("pdf", "both"):
            path = await generate_pdf(content, name)
            with open(path, "rb") as f:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    filename=f"{base}.pdf",
                    caption="📕 PDF готов!"
                )
            os.remove(path)
        await context.bot.send_message(chat_id=chat_id, text="Всё устраивает?", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Save: {e}", exc_info=True)
        await q.message.reply_text("Ошибка при создании файла.")
        return REVIEWING

# ─── Правки ──────────────────────────────────────────────────────────────────
async def cb_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    if q.data == "review_ok":
        await q.edit_message_text("Отлично! Обращайся если что. /new для нового запроса.")
        sessions.pop(uid, None); last_doc.pop(uid, None); return ConversationHandler.END
    await q.edit_message_text("Напиши замечания — внесу правки:")
    return REVIEWING

async def apply_edits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx
    uid = update.effective_user.id
    edits = await get_text(update)
    if not edits: return REVIEWING
    if edits.strip() in ("/new", "/new — Новый документ"):
        sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
        await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
        return ConversationHandler.END
    doc_info = last_doc.get(uid, {})
    if not doc_info: await update.message.reply_text("Не нашёл документ. /new"); return ConversationHandler.END
    await update.message.reply_text("Вношу правки...")
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": f"Документ:\n{doc_info['content']}\n\nПравки:\n{edits}\n\nВерни исправленный документ."}]
        )
        new_content = resp.content[0].text
        last_doc[uid]["content"] = new_content; remember(uid, new_content)
        doc_type = doc_info.get("type","tz"); name = doc_info.get("name","Doc")
        if doc_type == "criteria":
            path = await generate_criteria_docx(new_content, name)
            await send_docx(update.message, path, f"Kriterii_{name[:25]}.docx", "📋 Критерии обновлены!")
        else:
            path = await generate_tz_docx(new_content, name)
            fname = "Pismo.docx" if doc_type=="letter" else "Scenariy.docx" if doc_type=="negotiation" else f"TZ_{name[:25]}.docx"
            cap = "✉️ Письмо обновлено!" if doc_type=="letter" else "🤝 Сценарий обновлён!" if doc_type=="negotiation" else "📄 ТЗ обновлено!"
            await send_docx(update.message, path, fname, cap)
        await update.message.reply_text("Теперь всё устраивает?", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Edit: {e}", exc_info=True)
        await update.message.reply_text("Ошибка. Попробуй ещё раз.")
        return REVIEWING

# ─── Переговоры ──────────────────────────────────────────────────────────────
async def cb_neg_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_"); step = int(parts[1]); choice = parts[2]
    if choice == "custom":
        await q.edit_message_text(NEGOTIATION_STEPS[step]["q"] + "\n\nВведи свой вариант:")
        context.user_data["neg_custom_step"] = step; return NEGOTIATION
    answer = NEGOTIATION_STEPS[step]["opts"][int(choice)]
    await q.edit_message_text(f"{NEGOTIATION_STEPS[step]['q']}\n> {answer}")
    return await _save_neg(q.message, context, step, answer)

async def neg_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_text(update)
    if not text: return NEGOTIATION
    # Только /new прерывает
    if text.strip() in ("/new", "/new — Новый документ"):
        uid = update.effective_user.id
        sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
        await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
        return ConversationHandler.END
    step = context.user_data.pop("neg_custom_step", context.user_data.get("neg_step", 0))
    return await _save_neg(update.message, context, step, text)

async def _save_neg(msg, context: ContextTypes.DEFAULT_TYPE, step: int, answer: str):
    answers = context.user_data.setdefault("neg_answers", {})
    answers[step] = {"q": NEGOTIATION_STEPS[step]["q"], "a": answer}
    next_step = step + 1; context.user_data["neg_step"] = next_step
    if next_step < len(NEGOTIATION_STEPS):
        await msg.reply_text(NEGOTIATION_STEPS[next_step]["q"], reply_markup=neg_kb(next_step)); return NEGOTIATION
    await msg.reply_text("Составляю сценарий переговоров...")
    return await _gen_negotiation(msg, context)

async def _gen_negotiation(msg, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_pdf
    uid = msg.chat_id
    answers = context.user_data.get("neg_answers", {})
    ctx = "\n".join(f"{answers[i]['q']}: {answers[i]['a']}" for i in range(len(NEGOTIATION_STEPS)) if i in answers)
    try:
        await msg.reply_text("Составляю сценарий переговоров...")
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=NEGOTIATION_SYSTEM,
            messages=[{"role": "user", "content": f"Данные:\n{ctx}\n\nСоставь конкретный сценарий без воды."}]
        )
        content = resp.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "negotiation", "name": "Scenariy"}

        # Спрашиваем формат
        await msg.reply_text(
            "Сценарий готов! В каком формате сохранить?",
            reply_markup=word_pdf_kb()
        )
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Neg: {e}", exc_info=True)
        await msg.reply_text("Ошибка. /cancel и попробуй заново."); return ConversationHandler.END

# ─── Чат ─────────────────────────────────────────────────────────────────────
async def chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("Нет доступа."); return
    save_user(uid, update.effective_user.username or "")

    # Фото — обрабатываем отдельно, учитываем caption
    has_photo = update.message.photo or (
        update.message.document and update.message.document.mime_type and
        update.message.document.mime_type.startswith("image/"))
    if has_photo:
        await handle_photo(update, context); return

    text = await get_text(update)
    if not text: return

    # Ждём комментарии для редактуры — проверяем ДО check_nav
    if context.user_data.get("waiting_doc_edit"):
        context.user_data.pop("waiting_doc_edit")
        await apply_doc_edit(update, context)
        return

    # Ждём ответ на письмо по фото
    if context.user_data.get("waiting_photo_reply"):
        context.user_data.pop("waiting_photo_reply")
        await gen_letter_from_photo(update, context, text)
        return

    if await check_nav(update, context): return

    tl = text.lower()
    if any(w in tl for w in ["сохрани", "в ворд", "в word", "сделай файл"]):
        await save_last(update, context); return

    photo_ctx = context.user_data.get("last_photo_desc", "")
    system = CHAT_SYSTEM + (f"\n\nКонтекст фото: {photo_ctx}" if photo_ctx else "")
    history = context.user_data.get("chat_history", [])
    history.append({"role": "user", "content": text})
    if len(history) > 20: history = history[-20:]
    await update.message.reply_text("Думаю...")
    try:
        resp = claude.messages.create(model="claude-sonnet-4-5", max_tokens=2000, system=system, tools=[WEB_SEARCH_TOOL], messages=history)
        msgs = list(history)
        while resp.stop_reason == "tool_use":
            tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query","")} for b in resp.content if b.type == "tool_use"]
            msgs.append({"role": "assistant", "content": resp.content}); msgs.append({"role": "user", "content": tr})
            resp = claude.messages.create(model="claude-sonnet-4-5", max_tokens=2000, system=system, tools=[WEB_SEARCH_TOOL], messages=msgs)
        reply = "".join(b.text for b in resp.content if hasattr(b,"text")) or "Не знаю что ответить."
        remember(uid, reply)
        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history
        if len(reply) > 4000: reply = reply[:4000] + "..."
        await update.message.reply_text(reply)
        if len(reply) > 500:
            await update.message.reply_text("Сохранить в Word?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📄 Сохранить в Word", callback_data="save_to_word")]]))
    except Exception as e:
        logger.error(f"Chat: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось. Попробуй ещё раз.")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    caption = update.message.caption or ""
    await update.message.reply_text("Смотрю на фото и ищу информацию...")
    result = await get_image_b64(update)
    if not result: await update.message.reply_text("Не смог получить фото."); return
    b64, mime = result
    try:
        # Шаг 1: анализируем фото через Vision
        vision_prompt = (
            "Проанализируй изображение детально:\n"
            "1. Опиши подробно что изображено (предметы, бренды, модели, текст на упаковке)\n"
            "2. Если есть текст — распознай его полностью\n"
            "3. Составь конкретный поисковый запрос для поиска информации об этом\n"
        )
        if caption:
            vision_prompt += f"\nВопрос пользователя: {caption}\n"
        vision_prompt += "\nФормат ответа:\nОПИСАНИЕ: [подробное описание]\nТЕКСТ: [текст с фото или 'текста нет']\nПОИСК: [конкретный поисковый запрос]"

        vision = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": vision_prompt}
            ]}]
        )
        vtext = vision.content[0].text
        desc = ocr = ""
        search_q = caption if caption else "информация по изображению"

        for line in vtext.split("\n"):
            line = line.strip()
            if line.startswith("ОПИСАНИЕ:"): desc = line.replace("ОПИСАНИЕ:", "").strip()
            elif line.startswith("ТЕКСТ:"): ocr = line.replace("ТЕКСТ:", "").strip()
            elif line.startswith("ПОИСК:"): search_q = line.replace("ПОИСК:", "").strip()

        # Если Vision не вернул структурированный ответ — используем весь текст как описание
        if not desc and not ocr:
            desc = vtext.strip()
            search_q = caption or desc[:100]

        context.user_data["last_photo_desc"] = desc + ("\nТекст: " + ocr if ocr and ocr != "текста нет" else "")
        context.user_data["last_photo_text"] = ocr if ocr and ocr != "текста нет" else ""

        # Шаг 2: поиск по описанию (передаём текст, не изображение!)
        search_text = ""
        if search_q and search_q != "информация по изображению":
            try:
                sr = claude.messages.create(
                    model="claude-sonnet-4-5", max_tokens=1500,
                    tools=[WEB_SEARCH_TOOL],
                    messages=[{"role": "user", "content": f"Найди информацию: {search_q}"}]
                )
                sm = [{"role": "user", "content": f"Найди: {search_q}"}]
                while sr.stop_reason == "tool_use":
                    tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query", "")}
                          for b in sr.content if b.type == "tool_use"]
                    sm.append({"role": "assistant", "content": sr.content})
                    sm.append({"role": "user", "content": tr})
                    sr = claude.messages.create(
                        model="claude-sonnet-4-5", max_tokens=1500,
                        tools=[WEB_SEARCH_TOOL], messages=sm
                    )
                search_text = "".join(b.text for b in sr.content if hasattr(b, "text"))
            except Exception as se:
                logger.error(f"Search error: {se}")

        # Формируем ответ
        parts = []
        if desc: parts.append(f"На фото: {desc}")
        if ocr and ocr != "текста нет": parts.append(f"\nТекст с фото:\n{ocr}")
        if search_text: parts.append(f"\nНашёл в интернете:\n{search_text}")
        final = "\n".join(parts)
        if not final: final = "Не смог получить описание фото."
        if len(final) > 4000: final = final[:4000] + "..."

        remember(uid, final)
        await update.message.reply_text(final)

        # Если похоже на письмо — предлагаем ответ
        all_text = (desc + ocr).lower()
        if any(w in all_text for w in ["уважаем", "прошу", "сообщаем", "исх.", "вх.", "направляем"]):
            await update.message.reply_text(
                "Похоже на официальное письмо. Составить ответ?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✉️ Составить ответ", callback_data="photo_write_reply")
                ]])
            )
    except Exception as e:
        logger.error(f"Photo: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать фото. Попробуй ещё раз.")

async def cb_photo_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("Напиши что именно ответить в письме:")
    context.user_data["waiting_photo_reply"] = True

async def gen_letter_from_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, task: str):
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id
    original = context.user_data.get("last_photo_text", "")
    await update.message.reply_text("Составляю ответное письмо...")
    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000, system=LETTER_SYSTEM,
            messages=[{"role": "user", "content": f"Оригинальное письмо:\n{original}\n\nЗадание: {task}\n\nСоставь ответное письмо."}]
        )
        content = resp.content[0].text
        remember(uid, content)
        path = await generate_tz_docx(content, "Pismo")
        # Ответное письмо по фото → сразу Word без выбора
        await send_docx(update.message, path, "Otvet_na_pismo.docx", "✉️ Ответное письмо готово!")
        last_doc[uid] = {"content": content, "type": "letter", "name": "Pismo"}
        await update.message.reply_text("Всё устраивает?", reply_markup=review_kb())
    except Exception as e:
        logger.error(f"PhotoLetter: {e}", exc_info=True)
        await update.message.reply_text("Ошибка при составлении письма.")

async def receive_analysis_doc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Получаем документ для анализа."""
    uid = update.effective_user.id
    if await check_nav(update, context): return ConversationHandler.END

    doc_text = ""
    has_photo = update.message.photo or (
        update.message.document and update.message.document.mime_type and
        update.message.document.mime_type.startswith("image/"))

    if has_photo:
        result = await get_image_b64(update)
        if result:
            b64, mime = result
            await update.message.reply_text("Читаю документ с фото...")
            resp = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=3000,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": "Распознай весь текст с изображения. Выведи только текст."}
                ]}]
            )
            doc_text = resp.content[0].text
    elif update.message.document:
        await update.message.reply_text("Читаю документ...")
        doc_text = await extract_doc_text(update) or ""
    elif update.message.text and len(update.message.text) > 50:
        doc_text = update.message.text.strip()

    if not doc_text:
        await update.message.reply_text(
            "Не смог прочитать. Попробуй Word, PDF, txt или вставь текст в чат."
        )
        return ANALYSIS_DOC

    context.user_data["analysis_doc"] = doc_text
    await update.message.reply_text(
        "Документ получен. Провожу экспертизу, это займёт около минуты..."
    )
    return await do_analysis(update, context)


async def do_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Проводит анализ ТЗ и сразу сохраняет заключение в Word."""
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id
    doc_text = context.user_data.get("analysis_doc", "")

    if not doc_text:
        await update.message.reply_text("Документ не найден. Загрузи заново.")
        return ANALYSIS_DOC

    try:
        prompt = (
            "Проведи экспертизу технического задания организатора тендера. "
            "Мы организаторы, нам нужно проверить качество документа перед публикацией. "
            "Ответь подробно, не обрезай текст.\n\n"
            "ДОКУМЕНТ:\n" + doc_text[:12000] + "\n\n"
            "Выдай полное структурированное заключение:\n\n"
            "ЭКСПЕРТНОЕ ЗАКЛЮЧЕНИЕ\n\n"
            "1. КРАТКОЕ РЕЗЮМЕ\n"
            "Общее впечатление о качестве документа, основные проблемы.\n\n"
            "2. ОШИБКИ И ПРОТИВОРЕЧИЯ\n"
            "Для каждой ошибки: раздел документа, описание проблемы, как исправить.\n\n"
            "3. ОГРАНИЧИВАЮЩИЕ ТРЕБОВАНИЯ (риск жалобы в ФАС)\n"
            "Требования которые могут быть расценены как ограничение конкуренции. "
            "Как смягчить формулировку.\n\n"
            "4. НЕЧЕТКИЕ ФОРМУЛИРОВКИ\n"
            "Что участник может трактовать иначе. Предложи точные варианты формулировок.\n\n"
            "5. НЕДОСТАЮЩИЕ ТРЕБОВАНИЯ\n"
            "Что важно прописать дополнительно для защиты интересов заказчика.\n\n"
            "6. КОНКРЕТНЫЕ ПРЕДЛОЖЕНИЯ ПО ДОРАБОТКЕ\n"
            "Список конкретных правок с готовыми формулировками.\n\n"
            "7. УТОЧНЯЮЩИЕ ВОПРОСЫ (если есть)\n"
            "Не более 3-5 вопросов если что-то неясно из документа."
        )

        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=ANALYSIS_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )

        analysis_text = response.content[0].text
        remember(uid, analysis_text)
        context.user_data["analysis_result"] = analysis_text

        # Сразу сохраняем в Word — никакого текста в чате
        await update.message.reply_text("Готово! Сохраняю заключение в Word...")
        path = await generate_tz_docx(analysis_text, "Ekspertiza")
        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=uid,
                document=f,
                filename="Ekspertiza_TZ.docx",
                caption="Экспертное заключение готово!"
            )
        os.remove(path)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Задать уточняющий вопрос", callback_data="analysis_question")],
            [InlineKeyboardButton("Всё понятно", callback_data="analysis_done")],
        ])
        await update.message.reply_text(
            "Заключение в файле. Есть уточняющие вопросы?",
            reply_markup=kb
        )
        return ANALYSIS_QA

    except Exception as e:
        logger.error(f"Analysis: {e}", exc_info=True)
        await update.message.reply_text("Ошибка при анализе. Попробуй ещё раз.")
        return ANALYSIS_DOC


async def cb_analysis_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает действия после анализа."""
    from docx_generator import generate_tz_docx
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    if q.data == "analysis_question":
        await q.edit_message_text(
            "Задай уточняющий вопрос или попроси развернуть любой пункт заключения:"
        )
        return ANALYSIS_QA

    elif q.data == "analysis_done":
        await q.edit_message_text("Хорошо! Для нового документа: /new")
        context.user_data.pop("analysis_doc", None)
        context.user_data.pop("analysis_result", None)
        return ConversationHandler.END


async def analysis_followup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отвечает на уточняющие вопросы по документу."""
    if await check_nav(update, context): return ConversationHandler.END
    uid = update.effective_user.id
    question = await get_text(update)
    if not question: return ANALYSIS_QA

    doc_text = context.user_data.get("analysis_doc", "")
    analysis_text = context.user_data.get("analysis_result", "")

    await update.message.reply_text("Думаю...")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=ANALYSIS_SYSTEM,
            messages=[
                {"role": "user", "content": "Документ ТЗ:\n" + doc_text[:8000]},
                {"role": "assistant", "content": analysis_text[:3000]},
                {"role": "user", "content": question}
            ]
        )
        reply = response.content[0].text
        remember(uid, reply)
        await update.message.reply_text(reply)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Сохранить заключение в Word", callback_data="save_analysis")],
            [InlineKeyboardButton("Ещё вопрос", callback_data="analysis_question")],
            [InlineKeyboardButton("Всё, спасибо", callback_data="analysis_done")],
        ])
        await update.message.reply_text("Ещё вопросы?", reply_markup=kb)
        return ANALYSIS_QA

    except Exception as e:
        logger.error(f"Analysis followup: {e}", exc_info=True)
        await update.message.reply_text("Ошибка. Попробуй ещё раз.")
        return ANALYSIS_QA


async def handle_doc_in_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает файл загруженный в свободный чат."""
    uid = update.effective_user.id
    doc = update.message.document
    if not doc: return

    fname = doc.file_name or "документ"
    mime = doc.mime_type or ""

    # Только текстовые документы — не изображения
    is_text_doc = ("word" in mime or "pdf" in mime or "text" in mime or
                   fname.lower().endswith((".docx", ".doc", ".pdf", ".txt")))
    if not is_text_doc:
        return  # Передаём обработку фото-обработчику

    await update.message.reply_text("Читаю документ...")
    doc_text = await extract_doc_text(update)

    if not doc_text:
        await update.message.reply_text(
            "Не смог прочитать файл. Попробуй Word (.docx), PDF или txt."
        )
        return

    # Сохраняем документ и спрашиваем что делать
    context.user_data["received_doc_text"] = doc_text
    context.user_data["received_doc_name"] = fname
    remember(uid, doc_text[:500])

    # Если пользователь до этого просил анализ — сразу анализируем
    if context.user_data.pop("intent_analysis", False):
        context.user_data["analysis_doc"] = doc_text
        await update.message.reply_text("Провожу экспертизу, это займёт около минуты...")
        class FakeUpdate:
            message = update.message
            effective_user = update.effective_user
            callback_query = None
        await do_analysis(FakeUpdate(), context)
        return

    await update.message.reply_text(
        f"Получил: *{fname}*\n\nЧто с ним делаем?",
        reply_markup=doc_action_kb(),
        parse_mode="Markdown"
    )


async def cb_doc_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор действия с полученным документом."""
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    doc_text = context.user_data.get("received_doc_text", "")
    doc_name = context.user_data.get("received_doc_name", "документ")

    if not doc_text:
        await q.edit_message_text("Документ не найден. Загрузи снова.")
        return

    if q.data == "docact_analyze":
        # Запускаем анализ как из меню
        context.user_data["analysis_doc"] = doc_text
        await q.edit_message_text("Провожу экспертизу, это займёт около минуты...")

        # Создаём временный update.message для do_analysis
        class FakeUpdate:
            message = q.message
            effective_user = update.effective_user
            callback_query = None

        await do_analysis(FakeUpdate(), context)

    elif q.data == "docact_edit":
        context.user_data["edit_doc_text"] = doc_text
        context.user_data["edit_doc_name"] = doc_name
        context.user_data["waiting_doc_edit"] = True
        await q.edit_message_text(
            f"Документ '{doc_name}' готов к редактуре.\n\n"
            "Напиши свои комментарии — что именно изменить, добавить или убрать.\n\n"
            "Например: 'Сделай тон более официальным', 'Добавь пункт про сроки'",
        )

    elif q.data == "docact_discuss":
        # Добавляем документ в контекст чата
        context.user_data["chat_doc_context"] = doc_text[:6000]
        context.user_data["chat_history"] = [{
            "role": "user",
            "content": f"Я загрузил документ '{doc_name}'. Вот его содержимое:\n{doc_text[:6000]}"
        }, {
            "role": "assistant",
            "content": f"Прочитал документ '{doc_name}'. Задавай вопросы — отвечу по его содержимому."
        }]
        await q.edit_message_text(
            f"Документ *{doc_name}* загружен в контекст разговора.\n"
            "Задавай любые вопросы по его содержимому!",
            parse_mode="Markdown"
        )


async def apply_doc_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Редактирует документ по комментариям пользователя."""
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id

    # Получаем текст — поддерживаем и текст и голос
    comments = await get_text(update)
    if not comments:
        # Если нет текста — ставим флаг обратно и ждём
        context.user_data["waiting_doc_edit"] = True
        await update.message.reply_text("Не расслышал. Напиши комментарии текстом.")
        return

    doc_text = context.user_data.get("edit_doc_text", "")
    doc_name = context.user_data.get("edit_doc_name", "документ")

    if not doc_text:
        await update.message.reply_text("Документ не найден. Загрузи снова.")
        return ConversationHandler.END

    await update.message.reply_text("Редактирую документ по вашим комментариям...")

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=(
                "Ты помощник по редактуре документов. "
                "Пользователь предоставил документ и комментарии по его доработке. "
                "Внеси все указанные правки и верни ПОЛНЫЙ исправленный текст документа. "
                "Сохрани структуру и стиль оригинала. Только текст документа."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Оригинальный документ:\n{doc_text}\n\n"
                    f"Комментарии и правки:\n{comments}\n\n"
                    f"Верни полный исправленный документ."
                )
            }]
        )
        new_content = response.content[0].text
        remember(uid, new_content)
        last_doc[uid] = {"content": new_content, "type": "edited", "name": doc_name}

        path = await generate_tz_docx(new_content, doc_name)
        with open(path, "rb") as f:
            await context.bot.send_document(
                chat_id=uid,
                document=f,
                filename=f"Edited_{doc_name[:30]}.docx",
                caption=f"Документ отредактирован по вашим комментариям!"
            )
        os.remove(path)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Ещё правки", callback_data="docact_edit_more")],
            [InlineKeyboardButton("Всё отлично!", callback_data="docact_edit_done")],
        ])
        await update.message.reply_text("Устраивает результат?", reply_markup=kb)

    except Exception as e:
        logger.error(f"DocEdit: {e}", exc_info=True)
        await update.message.reply_text("Ошибка при редактуре. Попробуй ещё раз.")


async def cb_doc_edit_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    if q.data == "docact_edit_more":
        await q.edit_message_text("Напиши дополнительные правки:")
        return DOC_EDIT_CMT
    elif q.data == "docact_edit_done":
        context.user_data.pop("edit_doc_text", None)
        context.user_data.pop("edit_doc_name", None)
        await q.edit_message_text("Отлично! Для нового запроса: /new")
        return ConversationHandler.END



async def save_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id
    msgs_list = bot_msgs.get(uid, [])
    content = msgs_list[-1] if msgs_list else None
    if not content:
        history = context.user_data.get("chat_history", [])
        for m in reversed(history):
            if m["role"] == "assistant" and isinstance(m["content"], str): content = m["content"]; break
    if not content: await update.message.reply_text("Нечего сохранять."); return
    await update.message.reply_text("Сохраняю в Word...")
    try:
        path = await generate_tz_docx(content, "Dokument")
        await send_docx(update.message, path, "Dokument.docx", "📄 Сохранено в Word!")
    except Exception as e:
        logger.error(f"Save: {e}", exc_info=True)
        await update.message.reply_text("Ошибка.")

async def cb_save_to_word(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    update.message = q.message
    await save_last(update, context)

# ─── Запуск ──────────────────────────────────────────────────────────────────
async def intent_or_doc_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает намерения и файлы. Если намерение не найдено — передаёт в chat_handler."""
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")

    # Документ (не фото) — обрабатываем сразу
    if update.message.document:
        mime = update.message.document.mime_type or ""
        fname = update.message.document.file_name or ""
        if mime.startswith("image/"):
            await handle_photo(update, context)
            return ConversationHandler.END
        is_text_doc = ("word" in mime or "pdf" in mime or "text" in mime or
                       fname.lower().endswith((".docx", ".doc", ".pdf", ".txt")))
        if is_text_doc:
            await handle_doc_in_chat(update, context)
            return ConversationHandler.END

    # Только для текстовых сообщений — ищем намерения
    if update.message.text:
        text = update.message.text.strip()

        if "/new" in text:
            sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
            await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
            return CHOOSING

        intent = detect_intent(text)
        if intent:
            sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
            await _handle_intent(update, context, intent)
            if intent == "menu_negotiation":
                return NEGOTIATION
            if intent in ("menu_tz", "menu_criteria", "menu_both"):
                return CHOOSING
            return ConversationHandler.END

    # Намерение не найдено — передаём в обычный chat_handler
    await chat_handler(update, context)
    return ConversationHandler.END

def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND
    pf = filters.PHOTO | filters.Document.IMAGE
    df = filters.Document.ALL

    # Фильтр для текстовых документов
    doc_filter = filters.Document.ALL & ~filters.PHOTO

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", cmd_new),
            CallbackQueryHandler(cb_menu, pattern="^menu_"),
            MessageHandler(doc_filter, intent_or_doc_handler),
        ],
        states={
            CHOOSING: [
                CallbackQueryHandler(cb_menu,      pattern="^menu_"),
                CallbackQueryHandler(cb_direction,  pattern="^dir_"),
                CallbackQueryHandler(cb_hasdoc,     pattern="^hasdoc_"),
                MessageHandler(doc_filter, intent_or_doc_handler),
            ],
            WAITING_DOC: [
                MessageHandler(df, receive_customer_doc),
                MessageHandler(tv, lambda u,c: u.message.reply_text("Загрузи файл или /cancel")),
            ],
            NEGOTIATION: [
                CallbackQueryHandler(cb_neg_answer, pattern="^neg_"),
                MessageHandler(doc_filter, intent_or_doc_handler),
                MessageHandler(tv, neg_text_answer),
            ],
            ANSWERING: [
                CallbackQueryHandler(cb_answer, pattern="^ans_"),
                # Документы обрабатываем отдельно
                MessageHandler(doc_filter, intent_or_doc_handler),
                # Текст ВСЕГДА идёт в text_answer когда бот задаёт вопросы
                MessageHandler(tv, text_answer),
            ],
            CRITERIA_Q: [
                CallbackQueryHandler(cb_criteria_q, pattern="^(yes|no)_criteria$"),
                MessageHandler(doc_filter, intent_or_doc_handler),
            ],
            SAVE_FORMAT: [
                CallbackQueryHandler(cb_save_format, pattern="^fmt_"),
                MessageHandler(doc_filter, intent_or_doc_handler),
            ],
            REVIEWING: [
                CallbackQueryHandler(cb_review, pattern="^review_"),
                MessageHandler(doc_filter, intent_or_doc_handler),
                MessageHandler(tv, apply_edits),
            ],
            LETTER_TYPE: [
                CallbackQueryHandler(cb_letter_type, pattern="^letter_"),
            ],
            LETTER_PHOTO: [
                MessageHandler(pf | df | tv, receive_letter_original),
            ],
            LETTER_TASK: [
                MessageHandler(tv, receive_letter_task),
            ],
            ANALYSIS_DOC: [
                MessageHandler(filters.Document.ALL | pf | tv, receive_analysis_doc),
            ],
            ANALYSIS_QA: [
                CallbackQueryHandler(cb_analysis_actions,
                                     pattern="^(save_analysis|analysis_question|analysis_done)$"),
                MessageHandler(doc_filter, intent_or_doc_handler),
                MessageHandler(tv, analysis_followup),
            ],
            DOC_RECEIVED: [
                CallbackQueryHandler(cb_doc_action, pattern="^docact_"),
            ],
            DOC_EDIT_CMT: [
                CallbackQueryHandler(cb_doc_edit_actions, pattern="^docact_edit_"),
                MessageHandler(doc_filter, intent_or_doc_handler),
                MessageHandler(tv, apply_doc_edit),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cmd_cancel),
            CommandHandler("new", cmd_new),
            MessageHandler(doc_filter, intent_or_doc_handler),
        ],
        allow_reentry=False,
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("new",        cmd_new))
    app.add_handler(CommandHandler("help",       cmd_help))
    app.add_handler(CommandHandler("cancel",     cmd_cancel))
    app.add_handler(CommandHandler("broadcast",  cmd_broadcast))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CommandHandler("adduser",    cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_photo_reply,      pattern="^photo_write_reply$"))
    app.add_handler(CallbackQueryHandler(cb_analysis_actions,  pattern="^(save_analysis|analysis_question|analysis_done)$"))
    app.add_handler(CallbackQueryHandler(cb_save_to_word,      pattern="^save_to_word$"))
    app.add_handler(CallbackQueryHandler(cb_doc_edit_actions,  pattern="^docact_edit_"))
    app.add_handler(CallbackQueryHandler(cb_doc_action,        pattern="^docact_"))
    app.add_handler(MessageHandler(pf, chat_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, chat_handler))
    app.add_handler(MessageHandler(tv, chat_handler))

    logger.info("Bot v12 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
