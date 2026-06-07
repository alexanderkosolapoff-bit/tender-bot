"""
Telegram-бот v11
"""

import os, json, logging, base64, tempfile
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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Состояния
CHOOSING       = 1
ANSWERING      = 2
CRITERIA_Q     = 3
REVIEWING      = 4
NEGOTIATION    = 5
WAITING_DOC    = 6
SAVE_FORMAT    = 7
LETTER_TYPE    = 8   # Новое: тип письма (ответ/новое)
LETTER_PHOTO   = 9   # Ждём фото/файл оригинала
LETTER_TASK    = 10  # Ждём что написать в письме

sessions:  dict[int, TenderAgent] = {}
last_doc:  dict[int, dict]        = {}
bot_msgs:  dict[int, list[str]]   = {}

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

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

def is_allowed(uid):
    if not ALLOWED_USERS and not DYNAMIC_USERS: return True
    return uid in ALLOWED_USERS or uid in DYNAMIC_USERS

def save_user(uid, username=""):
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

def remember(uid, text):
    bot_msgs.setdefault(uid, []).append(text)
    if len(bot_msgs[uid]) > 20: bot_msgs[uid] = bot_msgs[uid][-20:]

# ─── Промпты ────────────────────────────────────────────────────────────────

CHAT_SYSTEM = """Ты - Макс, дерзкий помощник по тендерам. Как лучший друг: подкалываешь, шутишь, но всегда помогаешь.
Помни весь контекст разговора включая фото.
Шутки: "А самому слабо?", "Опять ты...", "Конец рабочего дня, но ладно", "Серьезно? Окей без осуждения".
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

NEGOTIATION_STEPS = [
    {"q": "Что закупаем?", "opts": ["Клининговые услуги", "IT-услуги", "Ремонт оборудования", "Строительные работы", "Поставка товаров"], "free": True},
    {"q": "Кто придет от участника?", "opts": ["Директор/собственник", "Коммерческий директор", "Менеджер по продажам", "Неизвестно"], "free": False},
    {"q": "НМЦ (начальная цена контракта)?", "opts": ["До 1 млн руб.", "1-5 млн руб.", "5-20 млн руб.", "Более 20 млн руб."], "free": True},
    {"q": "На сколько снижаем цену?", "opts": ["На 5-10%", "На 10-20%", "На 20-30%", "Максимально"], "free": False},
    {"q": "Есть альтернативные участники?", "opts": ["Да, 2+ конкурента", "Есть 1 альтернатива", "Нет, единственный"], "free": False},
    {"q": "Доп. цели переговоров?", "opts": ["Только снижение цены", "Цена + сроки", "Цена + гарантии", "Цена + объем работ"], "free": False},
]

LETTER_SYSTEM = """Ты составляешь официальные деловые письма на русском языке.
Письмо должно быть профессиональным, структурированным, вежливым.
Начни с обращения, изложи суть, закончи подписью.
Только текст письма, без лишних слов."""

# ─── Вспомогательные ────────────────────────────────────────────────────────

async def get_text(update):
    if update.message.text: return update.message.text.strip()
    if update.message.voice:
        await update.message.reply_text("Слушаю...")
        file = await update.message.voice.get_file()
        data = await file.download_as_bytearray()
        text = await transcribe_voice(bytes(data))
        await update.message.reply_text(f'Услышал: "{text}"')
        return text
    return None

async def get_image_b64(update):
    photo = None
    if update.message.photo: photo = update.message.photo[-1]
    elif update.message.document and update.message.document.mime_type and \
         update.message.document.mime_type.startswith("image/"): photo = update.message.document
    if not photo: return None
    file = await photo.get_file()
    data = await file.download_as_bytearray()
    b64 = base64.standard_b64encode(bytes(data)).decode()
    mime = getattr(photo, "mime_type", None) or "image/jpeg"
    return b64, mime

async def extract_doc_text(update):
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
            return "\n".join(p.extract_text() or "" for p in PdfReader(tmp.name).pages).strip()
        elif "word" in mime or fname.lower().endswith((".docx", ".doc")):
            from docx import Document as D
            return "\n".join(p.text for p in D(tmp.name).paragraphs if p.text.strip())
        elif "text" in mime or fname.lower().endswith(".txt"):
            return data.decode("utf-8", errors="replace").strip()
    except Exception as e: logger.error(f"Doc: {e}")
    finally: os.remove(tmp.name)
    return None

def _parse_criteria(text):
    rows = []; current = {}; num = 1
    for line in text.strip().split("\n"):
        s = line.strip()
        if not s:
            if current.get("criterion"):
                rows.append({"num": str(num), "criterion": current.get("criterion",""),
                    "requirement": current.get("requirement", current.get("criterion","")),
                    "document": current.get("document","По запросу организатора")}); num+=1; current={}
            continue
        up = s.upper()
        if up.startswith("КРИТЕРИЙ:"):
            if current.get("criterion"):
                rows.append({"num": str(num), "criterion": current.get("criterion",""),
                    "requirement": current.get("requirement", current.get("criterion","")),
                    "document": current.get("document","По запросу организатора")}); num+=1
            current = {"criterion": s.split(":",1)[1].strip()}
        elif up.startswith("ТРЕБОВАНИЕ:"): current["requirement"] = s.split(":",1)[1].strip()
        elif up.startswith("ДОКУМЕНТ:"): current["document"] = s.split(":",1)[1].strip()
        else:
            import re
            m = re.match(r'^(\d+)[.)]\s*(.+)', s)
            if m and not current:
                rows.append({"num": m.group(1), "criterion": m.group(2),
                    "requirement": m.group(2), "document": "По запросу организатора"})
    if current.get("criterion"):
        rows.append({"num": str(num), "criterion": current.get("criterion",""),
            "requirement": current.get("requirement", current.get("criterion","")),
            "document": current.get("document","По запросу организатора")})
    return rows

def send_q_sync(msg, result):
    return msg, result

async def send_q(msg, result):
    text = result["question"]; opts = result.get("options", [])
    if opts:
        kb = [[InlineKeyboardButton(o, callback_data=f"ans_{i}")] for i, o in enumerate(opts)]
        kb.append([InlineKeyboardButton("Свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text)

# ─── Клавиатуры ─────────────────────────────────────────────────────────────

def nav_kb():
    """Навигационная клавиатура — всегда видна."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("/new — Новый документ"), KeyboardButton("/help — Помощь")]],
        resize_keyboard=True, is_persistent=True,
    )

def menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Техническое задание (ТЗ)", callback_data="menu_tz")],
        [InlineKeyboardButton("📋 Критерии допуска", callback_data="menu_criteria")],
        [InlineKeyboardButton("📄+📋 ТЗ и критерии", callback_data="menu_both")],
        [InlineKeyboardButton("🤝 Сценарий переговоров", callback_data="menu_negotiation")],
        [InlineKeyboardButton("✉️ Написать письмо", callback_data="menu_letter")],
    ])

def dir_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧹 Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("💻 IT-услуги", callback_data="dir_it")],
        [InlineKeyboardButton("🔧 Ремонт оборудования", callback_data="dir_repair")],
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

def save_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Word (.docx)", callback_data="fmt_docx")],
        [InlineKeyboardButton("📄+📋 Word + PDF", callback_data="fmt_both")],
    ])

def save_docx_only_kb():
    """Только Word — для критериев (PDF не нужен по требованию)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Сохранить в Word", callback_data="fmt_docx")],
    ])

def neg_kb(step):
    s = NEGOTIATION_STEPS[step]
    btns = [[InlineKeyboardButton(o, callback_data=f"neg_{step}_{i}")] for i, o in enumerate(s["opts"])]
    if s["free"]: btns.append([InlineKeyboardButton("Свой вариант", callback_data=f"neg_{step}_custom")])
    return InlineKeyboardMarkup(btns)

def letter_type_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Ответ на письмо", callback_data="letter_reply")],
        [InlineKeyboardButton("📝 Новое письмо", callback_data="letter_new")],
    ])

# ─── Перехват кнопок навигации ───────────────────────────────────────────────

async def check_nav(update, context):
    """Проверяет нажатие навигационных кнопок. Возвращает True если обработано."""
    text = (update.message.text or "") if update.message else ""
    uid = update.effective_user.id
    if "/new" in text or text == "/new — Новый документ":
        sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
        await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
        return True
    if "/help" in text or text == "/help — Помощь":
        await update.message.reply_text(
            "Возможности бота:\n\n"
            "📄 ТЗ — техническое задание для тендера\n"
            "📋 Критерии допуска — таблица требований к участникам\n"
            "📄+📋 ТЗ и критерии — оба документа сразу\n"
            "🤝 Переговоры — сценарий для снижения цены\n"
            "✉️ Письмо — деловое письмо или ответ на письмо\n\n"
            "💬 Просто напиши вопрос — отвечу\n"
            "🔍 Ищу информацию в интернете\n"
            "📷 Анализирую фото, распознаю текст\n"
            "📝 'сохрани' — сохраню ответ в Word\n\n"
            "/new — начать новый документ\n"
            "/cancel — отменить текущий",
            reply_markup=nav_kb()
        )
        return True
    return False

# ─── Команды ────────────────────────────────────────────────────────────────

async def cmd_start(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа. Обратись к администратору.")
        return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None); context.user_data.clear()
    await update.message.reply_text(
        "Привет! Я Макс — помощник по тендерам.\n\n"
        "Умею составлять ТЗ, критерии допуска, сценарии переговоров, деловые письма.\n"
        "Также отвечаю на вопросы, ищу в интернете и анализирую фото.\n\n"
        "Нажми /new чтобы начать или просто напиши что нужно!",
        reply_markup=nav_kb()
    )

async def cmd_new(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid):
        await update.message.reply_text("Нет доступа.")
        return ConversationHandler.END
    save_user(uid, update.effective_user.username or "")
    sessions.pop(uid, None); context.user_data.clear()
    await update.message.reply_text("Что делаем?", reply_markup=menu_kb())
    return CHOOSING

async def cmd_cancel(update, context):
    uid = update.effective_user.id
    sessions.pop(uid, None); last_doc.pop(uid, None); context.user_data.clear()
    await update.message.reply_text("Отменено. Для нового запроса: /new", reply_markup=nav_kb())
    return ConversationHandler.END

async def cmd_help(update, context):
    await update.message.reply_text(
        "Возможности бота:\n\n"
        "📄 /new → ТЗ, критерии, переговоры, письмо\n"
        "💬 Просто напиши вопрос — отвечу\n"
        "🔍 Ищу в интернете\n"
        "📷 Анализирую фото\n"
        "📝 'сохрани' — сохраню в Word\n"
        "/cancel — отменить текущий запрос",
        reply_markup=nav_kb()
    )

async def cmd_users(update, context):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id: await update.message.reply_text("Только для администратора."); return
    all_users = get_all_users(); whitelist = ALLOWED_USERS | DYNAMIC_USERS
    lines = ["Пользователи бота:\n"]
    for uid_str, uname in (all_users.items() if all_users else [("—","нет данных")]):
        s = "OK" if int(uid_str) in whitelist else "NO"
        lines.append(f"[{s}] {uid_str} — @{uname or 'без username'}")
    lines.append(f"\nВ белом списке: {len(whitelist)} чел.")
    lines.append("Добавить: /adduser ID | Убрать: /removeuser ID")
    await update.message.reply_text("\n".join(lines))

async def cmd_adduser(update, context):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id: await update.message.reply_text("Только для администратора."); return
    if not context.args: await update.message.reply_text("Укажи ID: /adduser 123456789"); return
    try: new_uid = int(context.args[0])
    except: await update.message.reply_text("Неверный ID."); return
    DYNAMIC_USERS.add(new_uid); _save_dynamic(DYNAMIC_USERS)
    try:
        await context.bot.send_message(chat_id=new_uid, text="Тебе открыт доступ к боту! Напиши /start")
        notified = "Пользователь уведомлён."
    except: notified = "Не смог уведомить."
    await update.message.reply_text(f"Пользователь {new_uid} добавлен. {notified}")

async def cmd_removeuser(update, context):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id: await update.message.reply_text("Только для администратора."); return
    if not context.args: await update.message.reply_text("Укажи ID: /removeuser 123456789"); return
    try: rem_uid = int(context.args[0])
    except: await update.message.reply_text("Неверный ID."); return
    if rem_uid in DYNAMIC_USERS:
        DYNAMIC_USERS.discard(rem_uid); _save_dynamic(DYNAMIC_USERS)
        await update.message.reply_text(f"Пользователь {rem_uid} удалён.")
    elif rem_uid in ALLOWED_USERS:
        await update.message.reply_text(f"Пользователь {rem_uid} в ALLOWED_USERS на Railway — удали там вручную.")
    else:
        await update.message.reply_text(f"Пользователь {rem_uid} не найден в белом списке.")

async def cmd_broadcast(update, context):
    uid = update.effective_user.id
    admin_id = int(os.environ.get("ADMIN_ID", "0"))
    if uid != admin_id: await update.message.reply_text("Только для администратора."); return
    text = " ".join(context.args) if context.args else ""
    if not text: await update.message.reply_text("Напиши:\n/broadcast Привет!"); return
    users = get_all_users(); sent = failed = 0
    await update.message.reply_text(f"Рассылаю {len(users)} пользователям...")
    for uid_str in users:
        try: await context.bot.send_message(chat_id=int(uid_str), text=text); sent += 1
        except: failed += 1
    await update.message.reply_text(f"Готово! Отправлено: {sent}, не доставлено: {failed}")

# ─── Меню ───────────────────────────────────────────────────────────────────

async def cb_menu(update, context):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    if not is_allowed(uid): await q.edit_message_text("Нет доступа."); return ConversationHandler.END

    if q.data == "menu_negotiation":
        context.user_data["neg_answers"] = {}; context.user_data["neg_step"] = 0
        await q.edit_message_text(NEGOTIATION_STEPS[0]["q"], reply_markup=neg_kb(0))
        return NEGOTIATION

    if q.data == "menu_letter":
        await q.edit_message_text(
            "Что нужно сделать?",
            reply_markup=letter_type_kb()
        )
        return LETTER_TYPE

    doc_map = {"menu_tz": "tz_only", "menu_criteria": "criteria_only", "menu_both": "both"}
    context.user_data["doc_type"] = doc_map.get(q.data, "tz_only")
    await q.edit_message_text("Выбери направление закупки:", reply_markup=dir_kb())
    return CHOOSING

# ─── Письмо ─────────────────────────────────────────────────────────────────

async def cb_letter_type(update, context):
    q = update.callback_query; await q.answer()

    if q.data == "letter_reply":
        context.user_data["letter_mode"] = "reply"
        await q.edit_message_text(
            "Отправь фото письма или загрузи файл (Word, PDF, txt) — "
            "я прочитаю и составлю ответ.\n\n"
            "Или напиши текст письма вручную:"
        )
        return LETTER_PHOTO

    else:  # letter_new
        context.user_data["letter_mode"] = "new"
        context.user_data["letter_original"] = ""
        await q.edit_message_text(
            "Хорошо! Расскажи что написать в письме — "
            "кому, по какому поводу, что именно сообщить:"
        )
        return LETTER_TASK


async def receive_letter_original(update, context):
    """Получаем оригинал письма (фото, файл или текст)."""
    uid = update.effective_user.id
    original_text = ""

    # Фото
    if update.message.photo or (update.message.document and
       update.message.document.mime_type and
       update.message.document.mime_type.startswith("image/")):
        await update.message.reply_text("Читаю письмо...")
        result = await get_image_b64(update)
        if result:
            b64, mime = result
            response = claude.messages.create(
                model="claude-sonnet-4-5", max_tokens=2000,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": "Распознай текст с этого изображения письма. Выведи только текст письма."}
                ]}]
            )
            original_text = response.content[0].text

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
    await update.message.reply_text(
        f"Прочитал письмо. Теперь скажи — что именно ответить?\n\n"
        f"Например: «Согласиться с условиями, но попросить отсрочку на 2 недели» "
        f"или «Отказать, сославшись на занятость» и т.д."
    )
    return LETTER_TASK


async def receive_letter_task(update, context):
    """Получаем задание на письмо и генерируем."""
    from docx_generator import generate_tz_docx
    uid = update.effective_user.id

    if await check_nav(update, context): return ConversationHandler.END

    task = await get_text(update)
    if not task: return LETTER_TASK

    original = context.user_data.get("letter_original", "")
    mode = context.user_data.get("letter_mode", "new")

    await update.message.reply_text("Составляю письмо...")

    try:
        if mode == "reply" and original:
            prompt = (
                f"Оригинальное письмо, на которое нужно ответить:\n{original}\n\n"
                f"Задание: {task}\n\n"
                f"Составь профессиональное ответное письмо."
            )
        else:
            prompt = f"Задание: {task}\n\nСоставь профессиональное деловое письмо."

        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000, system=LETTER_SYSTEM,
            messages=[{"role": "user", "content": prompt}]
        )
        content = response.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "letter", "name": "Pismo"}

        # Показываем превью
        preview = content[:1500] + ("..." if len(content) > 1500 else "")
        await update.message.reply_text(f"Вот что получилось:\n\n{preview}")
        await update.message.reply_text("Сохранить в файл?", reply_markup=save_kb())
        return SAVE_FORMAT

    except Exception as e:
        logger.error(f"Letter error: {e}", exc_info=True)
        await update.message.reply_text("Что-то пошло не так. Попробуй ещё раз.")
        return LETTER_TASK

# ─── Направление и вопросы ──────────────────────────────────────────────────

async def cb_direction(update, context):
    q = update.callback_query; await q.answer()
    context.user_data["direction"] = q.data.replace("dir_", "")
    await q.edit_message_text(
        "Есть документ от заказчика? (ТЗ-черновик, письмо, описание)\n"
        "Загрузи — задам только недостающие вопросы.",
        reply_markup=hasdoc_kb()
    )
    return CHOOSING

async def cb_hasdoc(update, context):
    q = update.callback_query; await q.answer()
    if q.data == "hasdoc_yes":
        await q.edit_message_text("Загрузи документ (Word, PDF или txt):")
        return WAITING_DOC
    return await _start_questions(q.message, context)

async def receive_customer_doc(update, context):
    if not update.message.document:
        await update.message.reply_text("Загрузи файл или /cancel")
        return WAITING_DOC
    await update.message.reply_text("Читаю документ...")
    doc_text = await extract_doc_text(update)
    if not doc_text:
        await update.message.reply_text("Не смог прочитать. Попробуй другой формат.")
        return WAITING_DOC

    await update.message.reply_text("Анализирую что уже есть...")
    direction = context.user_data.get("direction", "cleaning")
    from agent import QUESTIONS
    questions = QUESTIONS.get(direction, [])
    q_list = "\n".join(f"- {q['question']}" for q in questions)

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=2000,
            system='Проанализируй документ. Верни JSON: {"found": {"вопрос": "ответ"}, "missing": ["вопрос"]}. Только JSON.',
            messages=[{"role": "user", "content": f"Вопросы:\n{q_list}\n\nДокумент:\n{doc_text[:6000]}"}]
        )
        import re
        raw = re.sub(r'```.*?```', '', response.content[0].text, flags=re.DOTALL).strip()
        analysis = json.loads(raw)
    except:
        analysis = {"found": {}, "missing": [q["question"] for q in questions]}

    found = analysis.get("found", {}); missing = analysis.get("missing", [])
    context.user_data["prefilled"] = found

    if found:
        found_text = "\n".join(f"- {k}: {v}" for k, v in found.items())
        await update.message.reply_text(f"Нашёл в документе:\n{found_text}\n\nОсталось уточнить: {len(missing)} вопр.")
    else:
        await update.message.reply_text("Данных не нашёл — задам все вопросы.")

    uid = update.effective_user.id
    doc_type = context.user_data.get("doc_type", "tz_only")
    all_q = questions
    filtered = [q for q in all_q if any(m.lower() in q["question"].lower() for m in missing)] or all_q if missing else []

    agent = TenderAgent(direction=direction, doc_type=doc_type)
    if filtered: agent.questions = filtered
    for fq, fa in found.items(): agent.answers.append({"question": fq, "answer": fa})
    sessions[uid] = agent

    if not missing:
        await update.message.reply_text("Все данные есть! Генерирую...")
        return await do_generate(update, context)

    result = await agent.get_next_question()
    await send_q(update.message, result)
    return ANSWERING

async def _start_questions(msg, context):
    uid = msg.chat_id
    agent = TenderAgent(
        direction=context.user_data.get("direction", "cleaning"),
        doc_type=context.user_data.get("doc_type", "tz_only"),
    )
    sessions[uid] = agent
    result = await agent.get_next_question()
    await send_q(msg, result)
    return ANSWERING

async def cb_answer(update, context):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent: await q.edit_message_text("Сессия устарела. /new"); return ConversationHandler.END
    if q.data == "ans_custom":
        await q.edit_message_text(q.message.text + "\n\nВведи свой вариант:"); return ANSWERING
    idx = int(q.data.replace("ans_", ""))
    opts = agent.last_question.get("options", [])
    answer = opts[idx] if idx < len(opts) else ""
    await q.edit_message_text(f"{agent.last_question['question']}\n> {answer}")
    return await _handle_answer(update, context, answer)

async def text_answer(update, context):
    if await check_nav(update, context): return ConversationHandler.END
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent: await update.message.reply_text("Сессия устарела. /new"); return ConversationHandler.END
    text = await get_text(update)
    if not text: return ANSWERING
    return await _handle_answer(update, context, text)

async def _handle_answer(update, context, answer):
    uid = update.effective_user.id; agent = sessions[uid]
    prefilled = context.user_data.get("prefilled", {})
    if prefilled and not any(a.get("prefilled") for a in agent.answers):
        for fq, fa in prefilled.items(): agent.answers.append({"question": fq, "answer": fa, "prefilled": True})
    result = await agent.submit_answer(answer)
    msg = update.callback_query.message if update.callback_query else update.message
    if result["status"] == "question":
        await send_q(msg, result); return ANSWERING
    await msg.reply_text("Данные собраны! Генерирую..."); return await do_generate(update, context)

# ─── Генерация ───────────────────────────────────────────────────────────────

async def do_generate(update, context):
    uid = update.effective_user.id
    agent = sessions.get(uid)
    if not agent:
        msg = update.callback_query.message if update.callback_query else update.message
        await msg.reply_text("Сессия устарела. /new"); return ConversationHandler.END
    msg = update.callback_query.message if update.callback_query else update.message
    try:
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            remember(uid, content)
            last_doc[uid] = {"content": content, "type": "tz", "name": agent.tender_name}
            await msg.reply_text("ТЗ готово! Сохранить?", reply_markup=save_kb())
            if agent.doc_type == "both": context.user_data["pending_criteria"] = True
            return SAVE_FORMAT

        if agent.doc_type == "criteria_only":
            await msg.reply_text("Генерирую критерии допуска...")
            return await _gen_criteria(msg, uid, agent, context)

        if agent.doc_type == "tz_only":
            await msg.reply_text(
                "Нужны критерии допуска?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Да, добавить", callback_data="yes_criteria")],
                    [InlineKeyboardButton("Нет, всё готово", callback_data="no_criteria")],
                ])
            )
            return CRITERIA_Q

        await msg.reply_text("Всё устраивает?", reply_markup=review_kb()); return REVIEWING

    except Exception as e:
        logger.error(f"Generate: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так. /cancel и попробуй заново.")
        return ConversationHandler.END

async def _gen_criteria(msg, uid, agent, context):
    """Генерирует критерии допуска на основе примеров из папки."""
    from docx_generator import generate_criteria_docx
    from examples_loader import load_examples

    # Загружаем примеры критериев
    examples_texts = load_examples("criteria")
    examples_block = ""
    if examples_texts:
        examples_block = "ПРИМЕРЫ КРИТЕРИЕВ ДОПУСКА ИЗ РЕАЛЬНЫХ ТЕНДЕРОВ:\n\n"
        for i, t in enumerate(examples_texts[:5], 1):
            examples_block += f"=== Пример {i} ===\n{t[:3000]}\n\n"

    system = f"""Ты эксперт по тендерам и закупкам.
Составь критерии допуска участников к закупке.

{examples_block}

Используй примеры выше как образец — такой же уровень детализации, похожие критерии, адаптированные под данную закупку.

Для каждого критерия выведи строго в формате:
КРИТЕРИЙ: [краткое название]
ТРЕБОВАНИЕ: [конкретное измеримое требование]
ДОКУМЕНТ: [что предоставить в подтверждение]

Составь 6-10 критериев. Только список, без заголовков."""

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=3000, system=system,
            messages=[{"role": "user", "content": f"Данные закупки:\n{agent._context()}\n\nСоставь критерии допуска."}]
        )
        content = response.content[0].text
        remember(uid, content)
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}

        # Сразу генерируем Word
        path = await generate_criteria_docx(content, agent.tender_name)
        with open(path, "rb") as f:
            await msg.reply_document(
                document=f,
                filename=f"Kriterii_{agent.tender_name[:30]}.docx",
                caption="📋 Критерии допуска готовы!"
            )
        os.remove(path)

        await msg.reply_text("Всё устраивает? Если есть замечания — напиши что изменить.", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Criteria: {e}", exc_info=True)
        await msg.reply_text("Ошибка. /cancel и попробуй заново.")
        return ConversationHandler.END

async def cb_save_format(update, context):
    from docx_generator import generate_tz_docx, generate_criteria_docx, generate_pdf
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    doc_info = last_doc.get(uid, {})
    if not doc_info: await q.edit_message_text("Не нашёл документ."); return ConversationHandler.END

    fmt = q.data.replace("fmt_", "")
    content = doc_info["content"]; doc_type = doc_info.get("type","tz"); name = doc_info.get("name","Doc")

    if doc_type == "criteria": gen_docx = lambda: generate_criteria_docx(content, name); base = f"Kriterii_{name[:30]}"
    elif doc_type == "negotiation": gen_docx = lambda: generate_tz_docx(content, name); base = "Scenariy"
    elif doc_type == "letter": gen_docx = lambda: generate_tz_docx(content, name); base = "Pismo"
    else: gen_docx = lambda: generate_tz_docx(content, name); base = f"TZ_{name[:30]}"

    await q.edit_message_text("Создаю файл(ы)...")
    try:
        if fmt in ("docx", "both"):
            path = await gen_docx()
            with open(path, "rb") as f: await q.message.reply_document(document=f, filename=f"{base}.docx", caption="📄 Word готов!")
            os.remove(path)
        if fmt == "both" and doc_type != "criteria":
            path = await generate_pdf(content, name)
            with open(path, "rb") as f: await q.message.reply_document(document=f, filename=f"{base}.pdf", caption="📕 PDF готов!")
            os.remove(path)

        # Если было "both" ТЗ+критерии — переходим к критериям
        if context.user_data.get("pending_criteria"):
            context.user_data.pop("pending_criteria")
            agent = sessions.get(uid)
            if agent:
                await q.message.reply_text("Теперь генерирую критерии допуска...")
                return await _gen_criteria(q.message, uid, agent, context)

        await q.message.reply_text("Всё устраивает?", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Save: {e}", exc_info=True)
        await q.message.reply_text("Ошибка при создании файла.")
        return REVIEWING

async def cb_criteria_q(update, context):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id; agent = sessions.get(uid)
    if q.data == "no_criteria":
        await q.edit_message_text("Всё устраивает?", reply_markup=review_kb()); return REVIEWING
    await q.edit_message_text("Генерирую критерии...")
    return await _gen_criteria(q.message, uid, agent, context)

# ─── Правки ─────────────────────────────────────────────────────────────────

async def cb_review(update, context):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    if q.data == "review_ok":
        await q.edit_message_text("Отлично! Обращайся если что. /new для нового запроса.")
        sessions.pop(uid, None); last_doc.pop(uid, None); return ConversationHandler.END
    await q.edit_message_text("Напиши замечания — внесу правки:"); return REVIEWING

async def apply_edits(update, context):
    from docx_generator import generate_tz_docx, generate_criteria_docx
    if await check_nav(update, context): return ConversationHandler.END
    uid = update.effective_user.id
    edits = await get_text(update)
    doc_info = last_doc.get(uid, {})
    if not doc_info: await update.message.reply_text("Не нашёл документ. /new"); return ConversationHandler.END
    await update.message.reply_text("Вношу правки...")
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=REVIEW_SYSTEM,
            messages=[{"role": "user", "content": f"Документ:\n{doc_info['content']}\n\nПравки:\n{edits}\n\nВерни исправленный документ."}]
        )
        new_content = response.content[0].text
        last_doc[uid]["content"] = new_content; remember(uid, new_content)
        doc_type = doc_info.get("type", "tz"); name = doc_info.get("name", "Doc")

        if doc_type == "criteria":
            path = await generate_criteria_docx(new_content, name)
            fname = f"Kriterii_{name[:30]}.docx"; cap = "📋 Критерии обновлены!"
        elif doc_type == "letter":
            path = await generate_tz_docx(new_content, name)
            fname = "Pismo.docx"; cap = "✉️ Письмо обновлено!"
        else:
            path = await generate_tz_docx(new_content, name)
            fname = f"TZ_{name[:30]}.docx"; cap = "📄 ТЗ обновлено!"

        with open(path, "rb") as f: await update.message.reply_document(document=f, filename=fname, caption=cap)
        os.remove(path)
        await update.message.reply_text("Теперь всё устраивает?", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Edit: {e}", exc_info=True)
        await update.message.reply_text("Ошибка. Попробуй ещё раз."); return REVIEWING

# ─── Переговоры ─────────────────────────────────────────────────────────────

async def cb_neg_answer(update, context):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_"); step = int(parts[1]); choice = parts[2]
    if choice == "custom":
        await q.edit_message_text(NEGOTIATION_STEPS[step]["q"] + "\n\nВведи свой вариант:")
        context.user_data["neg_custom_step"] = step; return NEGOTIATION
    answer = NEGOTIATION_STEPS[step]["opts"][int(choice)]
    await q.edit_message_text(f"{NEGOTIATION_STEPS[step]['q']}\n> {answer}")
    return await _save_neg(q.message, context, step, answer)

async def neg_text_answer(update, context):
    if await check_nav(update, context): return ConversationHandler.END
    text = await get_text(update)
    if not text: return NEGOTIATION
    step = context.user_data.pop("neg_custom_step", context.user_data.get("neg_step", 0))
    return await _save_neg(update.message, context, step, text)

async def _save_neg(msg, context, step, answer):
    answers = context.user_data.setdefault("neg_answers", {})
    answers[step] = {"q": NEGOTIATION_STEPS[step]["q"], "a": answer}
    next_step = step + 1; context.user_data["neg_step"] = next_step
    if next_step < len(NEGOTIATION_STEPS):
        await msg.reply_text(NEGOTIATION_STEPS[next_step]["q"], reply_markup=neg_kb(next_step))
        return NEGOTIATION
    await msg.reply_text("Составляю сценарий переговоров...")
    return await _gen_negotiation(msg, context)

async def _gen_negotiation(msg, context):
    from docx_generator import generate_tz_docx
    uid = msg.chat_id
    answers = context.user_data.get("neg_answers", {})
    ctx = "\n".join(f"{answers[i]['q']}: {answers[i]['a']}" for i in range(len(NEGOTIATION_STEPS)) if i in answers)
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5", max_tokens=4000, system=NEGOTIATION_SYSTEM,
            messages=[{"role": "user", "content": f"Данные:\n{ctx}\n\nСоставь конкретный сценарий без воды."}]
        )
        content = response.content[0].text
        remember(uid, content); last_doc[uid] = {"content": content, "type": "negotiation", "name": "Scenariy"}
        preview = content[:2500] + ("..." if len(content) > 2500 else "")
        await msg.reply_text(f"Сценарий переговоров:\n\n{preview}")
        await msg.reply_text("Сохранить в файл?", reply_markup=save_kb())
        return SAVE_FORMAT
    except Exception as e:
        logger.error(f"Neg: {e}", exc_info=True)
        await msg.reply_text("Ошибка. /cancel и попробуй заново."); return ConversationHandler.END

# ─── Чат ────────────────────────────────────────────────────────────────────

async def chat_handler(update, context):
    uid = update.effective_user.id
    if not is_allowed(uid): await update.message.reply_text("Нет доступа."); return
    save_user(uid, update.effective_user.username or "")

    if update.message.photo or (update.message.document and update.message.document.mime_type and
       update.message.document.mime_type.startswith("image/")):
        await handle_photo(update, context); return

    text = await get_text(update)
    if not text: return
    if await check_nav(update, context): return

    tl = text.lower()
    if any(w in tl for w in ["сохрани", "в ворд", "в pdf", "сделай файл", "скачать"]):
        await save_last(update, context); return

    photo_ctx = context.user_data.get("last_photo_desc", "")
    system = CHAT_SYSTEM + (f"\n\nКонтекст фото: {photo_ctx}" if photo_ctx else "")
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
            tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query","")}
                  for b in response.content if b.type == "tool_use"]
            msgs.append({"role": "assistant", "content": response.content})
            msgs.append({"role": "user", "content": tr})
            response = claude.messages.create(model="claude-sonnet-4-5", max_tokens=2000,
                system=system, tools=[WEB_SEARCH_TOOL], messages=msgs)

        reply = "".join(b.text for b in response.content if hasattr(b, "text")) or "Не знаю что ответить."
        remember(uid, reply)
        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history
        if len(reply) > 4000: reply = reply[:4000] + "..."
        await update.message.reply_text(reply)
        if len(reply) > 500:
            await update.message.reply_text("Сохранить в файл?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📄 Сохранить", callback_data="save_to_word")]]))
    except Exception as e:
        logger.error(f"Chat: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось. Попробуй ещё раз.")

async def handle_photo(update, context):
    await update.message.reply_text("Смотрю на фото и ищу инфу...")
    result = await get_image_b64(update)
    if not result: await update.message.reply_text("Не смог получить фото."); return
    b64, mime = result; caption = update.message.caption or ""
    try:
        vision = claude.messages.create(model="claude-sonnet-4-5", max_tokens=1500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                {"type": "text", "text": f"Анализируй:\n1. Опиши что изображено\n2. Распознай текст если есть\n3. Предложи поисковый запрос\n{'Запрос: '+caption if caption else ''}\n\nФормат:\nОПИСАНИЕ: ...\nТЕКСТ: ...\nПОИСК: ..."}
            ]}])
        vtext = vision.content[0].text
        desc = ocr = ""; search_q = caption or "информация по изображению"
        for line in vtext.split("\n"):
            if line.startswith("ОПИСАНИЕ:"): desc = line.replace("ОПИСАНИЕ:","").strip()
            elif line.startswith("ТЕКСТ:"): ocr = line.replace("ТЕКСТ:","").strip()
            elif line.startswith("ПОИСК:"): search_q = line.replace("ПОИСК:","").strip()

        uid = update.effective_user.id
        context.user_data["last_photo_desc"] = (desc + ("\nТекст: "+ocr if ocr and ocr!="текста нет" else ""))
        context.user_data["last_photo_text"] = ocr if ocr != "текста нет" else ""

        sr = claude.messages.create(model="claude-sonnet-4-5", max_tokens=1500,
            tools=[WEB_SEARCH_TOOL], messages=[{"role":"user","content":f"Найди: {search_q}"}])
        sm = [{"role":"user","content":f"Найди: {search_q}"}]
        while sr.stop_reason == "tool_use":
            tr = [{"type":"tool_result","tool_use_id":b.id,"content":b.input.get("query","")} for b in sr.content if b.type=="tool_use"]
            sm.append({"role":"assistant","content":sr.content}); sm.append({"role":"user","content":tr})
            sr = claude.messages.create(model="claude-sonnet-4-5", max_tokens=1500, tools=[WEB_SEARCH_TOOL], messages=sm)
        search_text = "".join(b.text for b in sr.content if hasattr(b,"text"))

        parts = []
        if desc: parts.append(f"На фото: {desc}")
        if ocr and ocr != "текста нет": parts.append(f"\nТекст с фото:\n{ocr}")
        if search_text: parts.append(f"\nНашёл в интернете:\n{search_text}")
        final = "\n".join(parts)
        if len(final) > 4000: final = final[:4000] + "..."
        remember(uid, final)
        await update.message.reply_text(final)

        all_text = (desc+ocr).lower()
        if any(w in all_text for w in ["уважаем","прошу","сообщаем","исх.","вх."]):
            await update.message.reply_text("Похоже на официальное письмо. Составить ответ?",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Составить ответ", callback_data="photo_write_reply")]]))
    except Exception as e:
        logger.error(f"Photo: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать фото.")

async def cb_photo_reply(update, context):
    q = update.callback_query; await q.answer()
    context.user_data["letter_mode"] = "reply"
    context.user_data["letter_original"] = context.user_data.get("last_photo_text", "")
    await q.edit_message_text("Напиши что именно ответить — составлю письмо:")
    return  # Просто ждём текст через chat_handler -> receive_letter_task

async def save_last(update, context):
    uid = update.effective_user.id
    msgs_list = bot_msgs.get(uid, [])
    content = msgs_list[-1] if msgs_list else None
    if not content:
        history = context.user_data.get("chat_history", [])
        for m in reversed(history):
            if m["role"] == "assistant" and isinstance(m["content"], str): content = m["content"]; break
    if not content: await update.message.reply_text("Нечего сохранять."); return
    last_doc[uid] = {"content": content, "type": "chat", "name": "Dokument"}
    await update.message.reply_text("В каком формате?", reply_markup=save_kb())

async def cb_save_to_word(update, context):
    q = update.callback_query; await q.answer()
    uid = update.effective_user.id
    msgs_list = bot_msgs.get(uid, [])
    if msgs_list: last_doc[uid] = {"content": msgs_list[-1], "type": "chat", "name": "Dokument"}
    await q.edit_message_text("В каком формате?", reply_markup=save_kb())

# ─── Запуск ─────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv  = (filters.TEXT | filters.VOICE) & ~filters.COMMAND
    pf  = filters.PHOTO | filters.Document.IMAGE
    df  = filters.Document.ALL

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", cmd_new),
            CallbackQueryHandler(cb_menu, pattern="^menu_"),
        ],
        states={
            CHOOSING: [
                CallbackQueryHandler(cb_menu,      pattern="^menu_"),
                CallbackQueryHandler(cb_direction,  pattern="^dir_"),
                CallbackQueryHandler(cb_hasdoc,     pattern="^hasdoc_"),
            ],
            WAITING_DOC: [
                MessageHandler(df, receive_customer_doc),
                MessageHandler(tv, lambda u,c: u.message.reply_text("Загрузи файл или /cancel")),
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
            SAVE_FORMAT: [
                CallbackQueryHandler(cb_save_format, pattern="^fmt_"),
            ],
            REVIEWING: [
                CallbackQueryHandler(cb_review, pattern="^review_"),
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
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("new",         cmd_new))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("cancel",      cmd_cancel))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))
    app.add_handler(CommandHandler("users",       cmd_users))
    app.add_handler(CommandHandler("adduser",     cmd_adduser))
    app.add_handler(CommandHandler("removeuser",  cmd_removeuser))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(cb_photo_reply,  pattern="^photo_write_reply$"))
    app.add_handler(CallbackQueryHandler(cb_save_to_word, pattern="^save_to_word$"))
    app.add_handler(MessageHandler(pf, chat_handler))
    app.add_handler(MessageHandler(tv, chat_handler))

    logger.info("Bot v11 started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
