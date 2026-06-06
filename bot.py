"""
Telegram-бот v4 — ТЗ, критерии, юмор, фото, поиск, генерация изображений.
"""

import os
import logging
import base64
import anthropic
from openai import OpenAI
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
CRITERIA_CONFIRM = 5  # Подтверждение критериев

sessions: dict[int, TenderAgent] = {}
last_doc: dict[int, dict] = {}

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
oai = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

CHAT_SYSTEM = """Ты — Макс, дерзкий и остроумный помощник по тендерам. Ты как лучший друг на работе — можешь подколоть, пошутить, но всегда помогаешь и делаешь всё качественно.

Твои коронные фразы (используй их часто, добавляй свои похожие):
- "Слушай, а ты сам не пробовал? Нет? Ну тогда ладно, сделаю 😏"
- "Опять ты... Ну давай, рассказывай что случилось 😄"
- "А самому слабо было? Понятно, понятно 🙄"
- "Конец рабочего дня, между прочим! Но ладно, для тебя сделаю исключение 😴"
- "Это уже третий раз за сегодня, ты вообще сам что-нибудь делаешь? 😂"
- "О, опять тендеры. Моя любимая тема. Нет. Но раз надо — сделаем 🫠"
- "Ты серьёзно это спрашиваешь? Окей, без осуждения 😅"
- "Держись, сейчас помогу. Хотя мог бы и сам догадаться 😄"
- "Ладно, не буду говорить что это элементарно. Хотя это элементарно 😏"

Используй смайлики активно. Шути, но всегда выполняй задачу профессионально.
Отвечай на русском. Если нужно создать документ — /new."""

REVIEW_SYSTEM = """Ты эксперт по тендерам. Внеси правки в документ и верни ПОЛНЫЙ исправленный текст. Только текст документа, без лишних слов."""

CRITERIA_PREVIEW_SYSTEM = """Ты эксперт по тендерам и закупкам. 
На основе данных о закупке составь список критериев допуска участников.
Выведи их кратко — каждый критерий одной строкой, начиная с эмодзи и тире.
Например:
✅ — Опыт работы от 3 лет
✅ — Наличие лицензии МЧС
✅ — Собственный штат сотрудников от 10 человек
Выведи 5-10 критериев. Только список, без лишних слов."""


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


def criteria_confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подходит, генерируй!", callback_data="criteria_go")],
        [InlineKeyboardButton("➕ Добавить критерии", callback_data="criteria_add")],
        [InlineKeyboardButton("🗑 Убрать лишнее", callback_data="criteria_remove")],
    ])


async def send_question(msg, result: dict):
    text = result["question"]
    options = result.get("options", [])
    if options:
        kb = [[InlineKeyboardButton(o, callback_data=f"ans_{i}")] for i, o in enumerate(options)]
        kb.append([InlineKeyboardButton("✏️ Свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(
        "О, живой! Привет 😄\n\n"
        "Я Макс — твой личный раб по тендерам. Могу:\n"
        "📄 Составить ТЗ и критерии → /new\n"
        "💬 Поболтать и ответить на вопросы\n"
        "🔍 Найти что угодно в интернете\n"
        "📷 Распознать текст с фото и найти по нему инфу\n"
        "✉️ Написать ответное письмо\n"
        "🎨 Нарисовать картинку\n\n"
        "Ну что, начнём или ты ещё думаешь? 😏"
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(
        "Опять тендеры 🙄 Ладно, куда деваться. Выбирай направление:",
        reply_markup=direction_kb()
    )
    return CHOOSING


# ─── Превью критериев ───────────────────────────────────────────────────────

async def show_criteria_preview(msg, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    """Показывает список критериев перед генерацией документа."""
    context_text = agent._context()

    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=800,
        system=CRITERIA_PREVIEW_SYSTEM,
        messages=[{"role": "user", "content": f"Данные закупки:\n{context_text}"}]
    )
    preview = response.content[0].text
    context.user_data["criteria_preview"] = preview

    await msg.reply_text(
        f"Вот что планирую включить в критерии допуска:\n\n{preview}\n\n"
        "Как тебе? Добавим что-то или всё норм? 🤔",
        reply_markup=criteria_confirm_kb()
    )
    return CRITERIA_CONFIRM


async def cb_criteria_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает решение по критериям."""
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)

    if q.data == "criteria_go":
        await q.edit_message_text("Отлично! Генерирую документ... ⚙️")
        return await generate_criteria_doc(q.message, uid, agent, context)

    elif q.data == "criteria_add":
        await q.edit_message_text(
            "Напиши что добавить — и я включу это в критерии 📝"
        )
        context.user_data["criteria_action"] = "add"
        return CRITERIA_CONFIRM

    elif q.data == "criteria_remove":
        await q.edit_message_text(
            "Напиши что убрать из критериев 🗑"
        )
        context.user_data["criteria_action"] = "remove"
        return CRITERIA_CONFIRM


async def handle_criteria_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает правки к списку критериев."""
    uid = update.effective_user.id
    agent = sessions.get(uid)
    text = await get_text(update)
    action = context.user_data.get("criteria_action", "add")
    preview = context.user_data.get("criteria_preview", "")

    await update.message.reply_text("Обновляю список... ⚙️")

    instruction = f"{'Добавь в список' if action == 'add' else 'Убери из списка'}: {text}"
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=800,
        system=CRITERIA_PREVIEW_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Текущий список критериев:\n{preview}\n\n{instruction}\n\nВерни обновлённый список."
        }]
    )
    new_preview = response.content[0].text
    context.user_data["criteria_preview"] = new_preview
    context.user_data.pop("criteria_action", None)

    await update.message.reply_text(
        f"Обновил список:\n\n{new_preview}\n\nТеперь как? 😊",
        reply_markup=criteria_confirm_kb()
    )
    return CRITERIA_CONFIRM


async def generate_criteria_doc(msg, uid: int, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует документ с критериями с учётом утверждённого превью."""
    from docx_generator import generate_criteria_docx

    preview = context.user_data.get("criteria_preview", "")
    extra_instructions = f"\n\nОбязательно включи эти критерии:\n{preview}" if preview else ""

    original_generate = agent.generate_criteria

    async def patched_generate():
        from examples_loader import load_examples
        examples_text = ""
        texts = load_examples("criteria")
        if texts:
            examples_text = "ПРИМЕРЫ КРИТЕРИЕВ:\n\n"
            for i, t in enumerate(texts[:5], 1):
                examples_text += f"=== Пример {i} ===\n{t[:3000]}\n\n"

        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=3000,
            system=f"Ты эксперт по закупкам. Составляешь документы 'Критерии допуска'.\n\n{examples_text}",
            messages=[{
                "role": "user",
                "content": f"Данные закупки:\n{agent._context()}{extra_instructions}\n\nСоставь полный документ критериев допуска."
            }]
        )
        return response.content[0].text

    try:
        content = await patched_generate()
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        path = await generate_criteria_docx(content, agent.tender_name)
        with open(path, "rb") as f:
            await msg.reply_document(
                document=f,
                filename=f"Критерии_{agent.tender_name[:35]}.docx",
                caption="📋 Критерии допуска готовы!"
            )
        os.remove(path)
        await msg.reply_text("Ну как, устраивает? 😊", reply_markup=review_kb())
        return REVIEWING
    except Exception as e:
        logger.error(f"Ошибка критериев: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 Попробуй /new")
        return ConversationHandler.END


# ─── Чат, фото, генерация ───────────────────────────────────────────────────

async def chat_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Свободный чат — текст, голос, фото."""
    if update.message.photo or (
        update.message.document and update.message.document.mime_type and
        update.message.document.mime_type.startswith("image/")
    ):
        await handle_photo(update, context)
        return

    text = await get_text(update)
    if not text:
        return

    # Ответное письмо
    if context.user_data.get("waiting_letter_instructions"):
        await generate_letter_reply(update, context)
        return

    # Генерация изображения
    gen_keywords = ["нарисуй", "сгенерируй картинку", "создай изображение", "нарисовать", "картинку"]
    if any(w in text.lower() for w in gen_keywords):
        await generate_image(update, context, text)
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
            reply = "Хм, не знаю что сказать 🤔 Попробуй иначе."

        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history

        if len(reply) > 4000:
            reply = reply[:4000] + "...\n(сократил, слишком много умных мыслей 😄)"

        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"Ошибка чата: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось 😅 Попробуй ещё раз.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализирует фото + ищет в интернете."""
    await update.message.reply_text("📷 Смотрю на фото... Сейчас всё расскажу 🔍")

    result = await get_image_base64(update)
    if not result:
        await update.message.reply_text("Не смог получить фото 😕")
        return

    b64, mime = result
    caption = update.message.caption or ""

    try:
        # Шаг 1 — анализируем фото
        vision_response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                    {"type": "text", "text": (
                        f"Проанализируй это изображение:\n"
                        f"1. Если есть текст — распознай его полностью\n"
                        f"2. Опиши что изображено подробно\n"
                        f"3. Сформулируй 2-3 поисковых запроса для поиска доп. информации по этому фото\n"
                        f"{'Запрос пользователя: ' + caption if caption else ''}\n\n"
                        f"Формат ответа:\n"
                        f"ОПИСАНИЕ: [описание]\n"
                        f"ТЕКСТ: [распознанный текст или 'текста нет']\n"
                        f"ПОИСКОВЫЕ ЗАПРОСЫ: [запрос1 | запрос2 | запрос3]"
                    )}
                ]
            }]
        )

        vision_text = vision_response.content[0].text
        context.user_data["last_photo_text"] = vision_text

        # Шаг 2 — ищем в интернете
        search_queries = ""
        if "ПОИСКОВЫЕ ЗАПРОСЫ:" in vision_text:
            search_queries = vision_text.split("ПОИСКОВЫЕ ЗАПРОСЫ:")[-1].strip()
            first_query = search_queries.split("|")[0].strip()
        else:
            first_query = caption or "информация по изображению"

        search_response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            tools=[WEB_SEARCH_TOOL],
            messages=[{
                "role": "user",
                "content": f"Найди информацию по запросу: {first_query}"
            }]
        )

        messages = [{"role": "user", "content": f"Найди: {first_query}"}]
        while search_response.stop_reason == "tool_use":
            tool_results = []
            for block in search_response.content:
                if block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": block.input.get("query", ""),
                    })
            messages.append({"role": "assistant", "content": search_response.content})
            messages.append({"role": "user", "content": tool_results})
            search_response = claude.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1500,
                tools=[WEB_SEARCH_TOOL],
                messages=messages,
            )

        search_text = "".join(b.text for b in search_response.content if hasattr(b, "text"))

        # Формируем итоговый ответ
        clean_vision = vision_text.replace("ПОИСКОВЫЕ ЗАПРОСЫ:", "").split("\n")
        clean_vision = "\n".join(l for l in clean_vision if not l.startswith("ПОИСКОВЫЕ"))

        final_reply = f"{clean_vision}\n\n🔍 *Нашёл в интернете:*\n{search_text}"

        if len(final_reply) > 4000:
            final_reply = final_reply[:4000] + "..."

        await update.message.reply_text(final_reply)

        # Если похоже на письмо — предлагаем составить ответ
        photo_lower = vision_text.lower()
        if any(w in photo_lower for w in ["уважаем", "прошу", "сообщаем", "исх.", "вх.", "настоящим"]):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Составить ответ на письмо", callback_data="write_reply")],
            ])
            await update.message.reply_text(
                "Это похоже на официальное письмо 📄 Составить ответ?",
                reply_markup=kb
            )

    except Exception as e:
        logger.error(f"Ошибка фото: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать фото 😕")


async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Генерирует изображение через DALL-E."""
    await update.message.reply_text("🎨 Рисую... Художник из меня так себе, но стараюсь 😄")

    # Убираем команду из текста
    prompt = text
    for w in ["нарисуй", "сгенерируй картинку", "создай изображение", "нарисовать", "картинку"]:
        prompt = prompt.lower().replace(w, "").strip()

    if not prompt:
        prompt = "абстрактный деловой офисный рисунок"

    try:
        response = oai.images.generate(
            model="dall-e-3",
            prompt=prompt,
            size="1024x1024",
            quality="standard",
            n=1,
        )
        image_url = response.data[0].url
        await update.message.reply_photo(
            photo=image_url,
            caption=f"🎨 Вот что получилось!\nЕсли не то — опиши точнее, я не телепат 😄"
        )
    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}", exc_info=True)
        await update.message.reply_text(
            "Не смог нарисовать 😕 Либо запрос слишком странный, либо DALL-E сегодня не в настроении."
        )


async def cb_photo_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "write_reply":
        await q.edit_message_text(
            "Напиши что ответить — я составлю официальное письмо в Word ✉️"
        )
        context.user_data["waiting_letter_instructions"] = True
        context.user_data["letter_original"] = context.user_data.get("last_photo_text", "")


async def generate_letter_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует ответное письмо в Word."""
    from docx_generator import generate_tz_docx
    instructions = await get_text(update)
    original = context.user_data.get("letter_original", "")
    context.user_data.pop("waiting_letter_instructions", None)

    await update.message.reply_text("✉️ Составляю письмо... Постараюсь не облажаться 😄")

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system="Ты составляешь официальные деловые письма на русском языке. "
                   "Письмо должно быть структурированным, профессиональным. "
                   "Начни с обращения, изложи суть, закончи подписью.",
            messages=[{
                "role": "user",
                "content": f"Оригинальное письмо:\n{original}\n\nИнструкции:\n{instructions}\n\nСоставь ответное письмо."
            }]
        )

        letter_text = response.content[0].text
        path = await generate_tz_docx(letter_text, "Ответное письмо")
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename="Ответное_письмо.docx",
                caption="✉️ Письмо готово! Проверь перед отправкой — я всё-таки бот 😄"
            )
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
        await q.edit_message_text("Отлично! Значит я не зря старался 😄 → /new для нового запроса")
        sessions.pop(uid, None)
        last_doc.pop(uid, None)
        return ConversationHandler.END

    elif q.data == "review_edit":
        await q.edit_message_text("Ладно, слушаю замечания. Только по делу, без 'просто переделай всё' 😄")
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
            model="claude-sonnet-4-5",
            max_tokens=4000,
            system=REVIEW_SYSTEM,
            messages=[{
                "role": "user",
                "content": f"Документ:\n\n{doc_info['content']}\n\nПравки:\n{edits}\n\nВерни исправленный документ."
            }]
        )

        new_content = response.content[0].text
        last_doc[uid]["content"] = new_content
        doc_type = doc_info.get("type", "tz")
        name = doc_info.get("name", "Документ")

        if doc_type == "criteria":
            path = await generate_criteria_docx(new_content, name)
            filename = f"Критерии_{name[:35]}.docx"
            caption = "📋 Исправленные критерии!"
        else:
            path = await generate_tz_docx(new_content, name)
            filename = f"ТЗ_{name[:40]}.docx"
            caption = "📄 Исправленное ТЗ!"

        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename=filename, caption=caption)
        os.remove(path)

        await update.message.reply_text("Готово! Теперь устраивает? 😊", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка правок: {e}", exc_info=True)
        await update.message.reply_text("Ошибка 😕 Попробуй ещё раз.")
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
        # Генерируем ТЗ если нужно
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            last_doc[uid] = {"content": content, "type": "tz", "name": agent.tender_name}
            path = await generate_tz_docx(content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(
                    document=f,
                    filename=f"ТЗ_{agent.tender_name[:40]}.docx",
                    caption="📄 ТЗ готово!"
                )
            os.remove(path)

        # Для критериев — сначала показываем превью
        if agent.doc_type in ("criteria_only", "both"):
            await msg.reply_text("Сейчас покажу что планирую включить в критерии... 🤔")
            return await show_criteria_preview(msg, agent, context)

        # Если только ТЗ — спрашиваем про критерии
        if agent.doc_type == "tz_only":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Да, добавить критерии", callback_data="yes_criteria")],
                [InlineKeyboardButton("✅ Нет, всё готово", callback_data="no_criteria")],
            ])
            await msg.reply_text("Нужны критерии допуска? 🤔", reply_markup=kb)
            return CRITERIA_Q

        await msg.reply_text("Ну как, всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 → /new")
        return ConversationHandler.END


async def cb_criteria_q(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)

    if q.data == "no_criteria":
        await q.edit_message_text("Понял! Всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING

    if q.data == "yes_criteria":
        await q.edit_message_text("Сейчас покажу что планирую включить... 🤔")
        return await show_criteria_preview(q.message, agent, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sessions.pop(uid, None)
    last_doc.pop(uid, None)
    context.user_data.clear()
    await update.message.reply_text("Отменено 👌 Пиши если что — никуда не денусь 😄")
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND
    photo_f = filters.PHOTO | filters.Document.IMAGE

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
                CallbackQueryHandler(cb_criteria_q, pattern="^(yes|no)_criteria$"),
            ],
            CRITERIA_CONFIRM: [
                CallbackQueryHandler(cb_criteria_confirm, pattern="^criteria_(go|add|remove)$"),
                MessageHandler(tv, handle_criteria_edit),
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
    app.add_handler(MessageHandler(photo_f, chat_reply))
    app.add_handler(CallbackQueryHandler(cb_photo_actions, pattern="^(write_reply|search_photo)$"))
    app.add_handler(MessageHandler(tv, chat_reply))

    logger.info("Бот v4 запущен 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
