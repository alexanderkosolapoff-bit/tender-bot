"""
Telegram-бот для генерации ТЗ и критериев допуска.
v3 — юмор, правки документов, распознавание фото и текста с фото.
"""

import os
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CHOOSING = 1
ANSWERING = 2
CRITERIA_Q = 3
REVIEWING = 4  # Новое состояние — ожидаем отзыв на документ

sessions: dict[int, TenderAgent] = {}
last_doc: dict[int, dict] = {}  # Храним последний документ для правок

CHAT_SYSTEM = """Ты — Макс, остроумный помощник по тендерам и закупкам. 
У тебя есть чувство юмора — ты иногда подкалываешь пользователя, шутишь, используешь смайлики 😄
Но при этом всегда выполняешь задачу профессионально и качественно.

Примеры твоих шуток (используй их редко, к месту):
- "Хочешь, чтобы это я сделал? Я думал ты и сам справишься 😏"
- "А самому слабо? Ладно, сделаю... 😄"
- "Давай уже завтра, конец рабочего дня 😴 Шучу, сейчас всё сделаю!"
- "Опять ты со своими тендерами 😄 Ну давай, рассказывай"

Ты можешь:
- Составлять ТЗ и критерии допуска → /new
- Отвечать на любые вопросы
- Искать информацию в интернете
- Анализировать фотографии и документы
- Распознавать текст с фото и составлять ответные письма

Отвечай на русском языке. Используй смайлики умеренно.
Если пользователь хочет создать ТЗ — напомни про /new.
"""

REVIEW_SYSTEM = """Ты — эксперт по тендерам. Пользователь даёт замечания к документу.
Внеси все указанные правки и верни ПОЛНЫЙ исправленный документ.
Сохрани структуру и стиль оригинала. Выведи только текст документа без лишних слов."""

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


async def get_text(update: Update) -> str | None:
    if update.message.text:
        return update.message.text.strip()
    if update.message.voice:
        await update.message.reply_text("🎤 Распознаю голосовое...")
        file = await update.message.voice.get_file()
        data = await file.download_as_bytearray()
        text = await transcribe_voice(bytes(data))
        await update.message.reply_text(f'Распознано: "{text}"')
        return text
    return None


async def get_image_base64(update: Update) -> tuple[str, str] | None:
    """Скачивает фото и возвращает (base64, mime_type)."""
    photo = None
    if update.message.photo:
        photo = update.message.photo[-1]  # Берём самое большое фото
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        photo = update.message.document

    if not photo:
        return None

    file = await photo.get_file()
    data = await file.download_as_bytearray()
    b64 = base64.standard_b64encode(bytes(data)).decode("utf-8")
    mime = "image/jpeg"
    if hasattr(photo, "mime_type") and photo.mime_type:
        mime = photo.mime_type
    return b64, mime


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


async def send_question(msg, result: dict):
    text = result["question"]
    options = result.get("options", [])
    if options:
        kb = [[InlineKeyboardButton(o, callback_data=f"ans_{i}")] for i, o in enumerate(options)]
        kb.append([InlineKeyboardButton("✏️ Ввести свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(
        "Привет! Я Макс — твой помощник по тендерам 😄\n\n"
        "Могу:\n"
        "📄 Составить ТЗ и критерии допуска → /new\n"
        "💬 Ответить на любой вопрос\n"
        "🔍 Найти информацию в интернете\n"
        "📷 Распознать текст с фото\n"
        "✉️ Составить ответное письмо\n\n"
        "Ну что, начнём? 😏"
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(
        "Опять тендеры 😄 Ладно, выбирай направление:",
        reply_markup=direction_kb()
    )
    return CHOOSING


# ─── Чат и фото ────────────────────────────────────────────────────────────

async def chat_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Свободный чат — текст, голос или фото."""
    uid = update.effective_user.id

    # Фото — анализируем
    if update.message.photo or (update.message.document and
       update.message.document.mime_type and
       update.message.document.mime_type.startswith("image/")):
        await handle_photo(update, context)
        return

    text = await get_text(update)
    if not text:
        return

    history = context.user_data.get("chat_history", [])
    history.append({"role": "user", "content": text})
    if len(history) > 20:
        history = history[-20:]

    await update.message.reply_text("⏳ Думаю...")

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system=CHAT_SYSTEM,
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
                system=CHAT_SYSTEM,
                tools=[WEB_SEARCH_TOOL],
                messages=messages,
            )

        reply = "".join(b.text for b in response.content if hasattr(b, "text"))
        if not reply:
            reply = "Не смог найти ответ, попробуй переформулировать 🤔"

        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history

        if len(reply) > 4000:
            reply = reply[:4000] + "...\n(ответ сокращён)"

        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"Ошибка чата: {e}", exc_info=True)
        await update.message.reply_text("Что-то пошло не так 😅 Попробуй ещё раз.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализирует фото — описывает или распознаёт текст."""
    await update.message.reply_text("📷 Смотрю на фото...")

    result = await get_image_base64(update)
    if not result:
        await update.message.reply_text("Не смог получить фото 😕")
        return

    b64, mime = result
    caption = update.message.caption or ""

    # Определяем что делать с фото
    is_letter = any(w in caption.lower() for w in ["письмо", "документ", "текст", "прочитай", "распознай", "ответ"])

    if is_letter or not caption:
        prompt = (
            "Посмотри на это изображение.\n"
            "1. Если на нём есть текст — распознай его полностью.\n"
            "2. Опиши что изображено.\n"
            f"{'Пользователь просит: ' + caption if caption else ''}\n"
            "Если это письмо или документ с текстом — выведи весь текст."
        )
    else:
        prompt = f"Посмотри на фото и ответь: {caption}"

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": b64,
                        }
                    },
                    {"type": "text", "text": prompt}
                ]
            }]
        )

        reply = response.content[0].text
        context.user_data["last_photo_text"] = reply  # Сохраняем для возможного ответного письма

        await update.message.reply_text(reply)

        # Если распознали текст — предлагаем составить ответ
        if any(w in reply.lower() for w in ["уважаем", "прошу", "сообщаем", "направляем", "исх.", "вх."]):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Составить ответ на это письмо", callback_data="write_reply")],
                [InlineKeyboardButton("🔍 Найти информацию по фото", callback_data="search_photo")],
            ])
            await update.message.reply_text(
                "Это похоже на официальное письмо 📄 Что делаем?",
                reply_markup=kb
            )

    except Exception as e:
        logger.error(f"Ошибка фото: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать фото 😕 Попробуй ещё раз.")


async def cb_photo_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает действия с фото — ответ на письмо или поиск."""
    q = update.callback_query
    await q.answer()

    photo_text = context.user_data.get("last_photo_text", "")

    if q.data == "write_reply":
        await q.edit_message_text("✉️ Напиши что именно нужно ответить — я составлю письмо в формате Word.")
        context.user_data["waiting_letter_instructions"] = True
        context.user_data["letter_original"] = photo_text

    elif q.data == "search_photo":
        await q.edit_message_text("🔍 Ищу информацию...")
        try:
            response = claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1500,
                tools=[WEB_SEARCH_TOOL],
                messages=[{
                    "role": "user",
                    "content": f"Найди информацию по этому содержимому: {photo_text[:500]}"
                }]
            )
            messages = [{"role": "user", "content": f"Найди информацию: {photo_text[:500]}"}]
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
                    max_tokens=1500,
                    tools=[WEB_SEARCH_TOOL],
                    messages=messages,
                )
            reply = "".join(b.text for b in response.content if hasattr(b, "text"))
            await q.message.reply_text(reply or "Ничего не нашёл 🤷")
        except Exception as e:
            logger.error(f"Ошибка поиска по фото: {e}", exc_info=True)
            await q.message.reply_text("Ошибка поиска 😕")


async def generate_letter_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует ответное письмо в Word."""
    from docx_generator import generate_tz_docx

    instructions = await get_text(update)
    original = context.user_data.get("letter_original", "")
    context.user_data.pop("waiting_letter_instructions", None)

    await update.message.reply_text("✉️ Составляю ответное письмо...")

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system="Ты составляешь официальные деловые письма на русском языке. "
                   "Письмо должно быть структурированным, вежливым и профессиональным. "
                   "Начни с обращения, изложи суть, закончи подписью.",
            messages=[{
                "role": "user",
                "content": f"Оригинальное письмо:\n{original}\n\nИнструкции для ответа:\n{instructions}\n\nСоставь ответное письмо."
            }]
        )

        letter_text = response.content[0].text
        path = await generate_tz_docx(letter_text, "Ответное письмо")

        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="Ответное_письмо.docx",
                caption="✉️ Ответное письмо готово! Проверь и отредактируй если нужно 📝"
            )
        os.remove(path)

    except Exception as e:
        logger.error(f"Ошибка письма: {e}", exc_info=True)
        await update.message.reply_text("Ошибка при составлении письма 😕")


# ─── Правки документов ──────────────────────────────────────────────────────

async def cb_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Пользователь доволен или хочет правки."""
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    if q.data == "review_ok":
        await q.edit_message_text("Отлично! Рад что понравилось 😄 Для нового запроса → /new")
        sessions.pop(uid, None)
        last_doc.pop(uid, None)
        return ConversationHandler.END

    elif q.data == "review_edit":
        await q.edit_message_text(
            "Хорошо, слушаю замечания 📝\n"
            "Напиши что нужно исправить — я переделаю!"
        )
        return REVIEWING


async def apply_edits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Применяет правки к документу."""
    from docx_generator import generate_tz_docx, generate_criteria_docx

    uid = update.effective_user.id
    edits = await get_text(update)
    doc_info = last_doc.get(uid, {})

    if not doc_info:
        await update.message.reply_text("Не нашёл предыдущий документ 😕 Попробуй /new")
        return ConversationHandler.END

    await update.message.reply_text("✏️ Вношу правки, секунду...")

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=REVIEW_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Исходный документ:\n\n{doc_info['content']}\n\n"
                           f"Замечания пользователя:\n{edits}\n\n"
                           f"Внеси правки и верни полный исправленный документ."
            }]
        )

        new_content = response.content[0].text
        last_doc[uid]["content"] = new_content  # Обновляем для следующих правок

        doc_type = doc_info.get("type", "tz")
        name = doc_info.get("name", "Документ")

        if doc_type == "criteria":
            path = await generate_criteria_docx(new_content, name)
            filename = f"Критерии_{name[:35]}.docx"
            caption = "📋 Исправленные критерии допуска!"
        else:
            path = await generate_tz_docx(new_content, name)
            filename = f"ТЗ_{name[:40]}.docx"
            caption = "📄 Исправленное ТЗ!"

        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=caption
            )
        os.remove(path)

        await update.message.reply_text(
            "Готово! Теперь всё устраивает? 😊",
            reply_markup=review_kb()
        )
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка правок: {e}", exc_info=True)
        await update.message.reply_text("Ошибка при правке 😕 Попробуй ещё раз.")
        return REVIEWING


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
        await q.edit_message_text("Сессия устарела. Начни заново → /new")
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
        await msg.reply_text("Отлично, данные собраны! Генерирую... ⚙️")
        return await do_generate(update, context)


async def do_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx
    uid = update.effective_user.id
    agent = sessions[uid]
    msg = update.callback_query.message if update.callback_query else update.message

    try:
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            last_doc[uid] = {"content": content, "type": "tz", "name": agent.tender_name}
            path = await generate_tz_docx(content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(
                    document=f,
                    filename=f"ТЗ_{agent.tender_name[:40]}.docx",
                    caption="📄 Техническое задание готово!"
                )
            os.remove(path)

        if agent.doc_type in ("criteria_only", "both"):
            content = await agent.generate_criteria()
            last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
            path = await generate_criteria_docx(content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(
                    document=f,
                    filename=f"Критерии_{agent.tender_name[:35]}.docx",
                    caption="📋 Критерии допуска готовы!"
                )
            os.remove(path)

        if agent.doc_type == "tz_only":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Да, добавить критерии", callback_data="yes_criteria")],
                [InlineKeyboardButton("✅ Нет, всё готово", callback_data="review_check")],
            ])
            await msg.reply_text("Нужны критерии допуска? 🤔", reply_markup=kb)
            return CRITERIA_Q

        # Спрашиваем устраивает ли документ
        await msg.reply_text(
            "Ну как, всё устраивает? 😊",
            reply_markup=review_kb()
        )
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 Попробуй /new")
        return ConversationHandler.END


async def cb_criteria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_criteria_docx
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    if q.data == "review_check":
        await q.edit_message_text(
            "Отлично! Всё готово 😄",
            reply_markup=review_kb()
        )
        return REVIEWING

    if q.data == "no_criteria":
        await q.edit_message_text(
            "Понял! Всё устраивает? 😊",
            reply_markup=review_kb()
        )
        return REVIEWING

    agent = sessions.get(uid)
    if not agent:
        await q.edit_message_text("Сессия устарела → /new")
        return ConversationHandler.END

    await q.edit_message_text("Генерирую критерии... ⚙️")
    try:
        content = await agent.generate_criteria()
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        path = await generate_criteria_docx(content, agent.tender_name)
        with open(path, "rb") as f:
            await q.message.reply_document(
                document=f,
                filename=f"Критерии_{agent.tender_name[:35]}.docx",
                caption="📋 Критерии допуска готовы!"
            )
        os.remove(path)
        await q.message.reply_text("Всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await q.message.reply_text("Ошибка 😕 Попробуй /new")
        return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None)
    last_doc.pop(uid, None)
    context.user_data.clear()
    await update.message.reply_text("Отменено 👌 Пиши если что нужно!")
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND
    photo_filter = filters.PHOTO | (filters.Document.IMAGE)

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_request)],
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
                CallbackQueryHandler(cb_criteria, pattern="^(yes|no)_criteria$|^review_check$"),
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
    app.add_handler(conv)
    # Фото — обрабатываем вне диалога
    app.add_handler(MessageHandler(photo_filter, chat_reply))
    # Действия с фото
    app.add_handler(CallbackQueryHandler(cb_photo_actions, pattern="^(write_reply|search_photo)$"))
    # Ответное письмо
    app.add_handler(MessageHandler(
        tv & filters.ChatType.PRIVATE,
        lambda u, c: generate_letter_reply(u, c) if c.user_data.get("waiting_letter_instructions") else chat_reply(u, c)
    ))

    logger.info("Бот v3 запущен 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
