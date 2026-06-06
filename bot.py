"""
Telegram-бот v5 — исправлены критерии, генерация фото, память о фото.
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
CRITERIA_CONFIRM = 5

sessions: dict[int, TenderAgent] = {}
last_doc: dict[int, dict] = {}

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
oai = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

CHAT_SYSTEM = """Ты — Макс, дерзкий и остроумный помощник по тендерам. Ты как лучший друг на работе — можешь подколоть, пошутить, но всегда помогаешь и делаешь всё качественно.

ВАЖНО: Ты помнишь весь контекст разговора. Если пользователь прислал фото и спрашивает про него — ты помнишь что на нём было. Используй эту информацию в ответах.

Твои коронные фразы (используй часто, добавляй свои):
- "Слушай, а ты сам не пробовал? Нет? Ну тогда ладно 😏"
- "Опять ты... Ну давай 😄"
- "А самому слабо? Понятно 🙄"
- "Конец рабочего дня, между прочим! Но ладно 😴"
- "Это уже третий раз за сегодня, ты вообще сам что-нибудь делаешь? 😂"
- "О, опять тендеры. Моя любимая тема. Нет. Но раз надо 🫠"
- "Ты серьёзно это спрашиваешь? Окей, без осуждения 😅"
- "Ладно, не буду говорить что это элементарно. Хотя это элементарно 😏"

Используй смайлики активно. Отвечай на русском."""

REVIEW_SYSTEM = """Ты эксперт по тендерам. Внеси правки в документ и верни ПОЛНЫЙ исправленный текст. Только текст документа, без лишних слов."""

CRITERIA_PREVIEW_SYSTEM = """Ты эксперт по тендерам. На основе данных закупки составь список критериев допуска.
Выведи их кратко — каждый критерий на отдельной строке, пронумеровано:
1. Опыт работы от 3 лет
2. Наличие лицензии МЧС
3. Собственный штат от 10 человек
Выведи 5-10 критериев. Только нумерованный список, без заголовков и лишних слов."""


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


def criteria_main_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подходит, генерируй!", callback_data="criteria_go")],
        [InlineKeyboardButton("➕ Добавить критерий", callback_data="criteria_add")],
        [InlineKeyboardButton("🗑 Убрать критерий", callback_data="criteria_remove")],
    ])


def criteria_remove_kb(criteria_list: list[str]) -> InlineKeyboardMarkup:
    """Кнопки для каждого критерия — нажимаешь чтобы убрать."""
    buttons = []
    for i, criterion in enumerate(criteria_list):
        short = criterion[:40] + "..." if len(criterion) > 40 else criterion
        buttons.append([InlineKeyboardButton(f"❌ {short}", callback_data=f"del_criterion_{i}")])
    buttons.append([InlineKeyboardButton("✅ Готово, генерируй!", callback_data="criteria_go")])
    return InlineKeyboardMarkup(buttons)


def parse_criteria_list(text: str) -> list[str]:
    """Парсит нумерованный список критериев в массив строк."""
    lines = []
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Убираем номер в начале: "1. текст" → "текст"
        cleaned = line.lstrip("0123456789").lstrip(". ").strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def format_criteria_list(criteria: list[str]) -> str:
    """Форматирует список критериев обратно в нумерованный текст."""
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
        "🎨 Нарисовать картинку (напиши 'нарисуй ...')\n\n"
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
    """Показывает список критериев с кнопками перед генерацией."""
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=800,
        system=CRITERIA_PREVIEW_SYSTEM,
        messages=[{"role": "user", "content": f"Данные закупки:\n{agent._context()}"}]
    )
    raw = response.content[0].text
    criteria_list = parse_criteria_list(raw)
    context.user_data["criteria_list"] = criteria_list

    formatted = format_criteria_list(criteria_list)
    await msg.reply_text(
        f"Вот что планирую включить в критерии допуска:\n\n{formatted}\n\n"
        "Как тебе? Можем добавить или убрать что-то 🤔",
        reply_markup=criteria_main_kb()
    )
    return CRITERIA_CONFIRM


async def cb_criteria_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки управления критериями."""
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)
    criteria_list = context.user_data.get("criteria_list", [])

    if q.data == "criteria_go":
        await q.edit_message_text("Отлично! Генерирую документ... ⚙️")
        return await generate_criteria_doc(q.message, uid, agent, context)

    elif q.data == "criteria_add":
        await q.edit_message_text(
            f"Текущий список:\n\n{format_criteria_list(criteria_list)}\n\n"
            "Напиши что добавить 📝"
        )
        context.user_data["criteria_action"] = "add"
        return CRITERIA_CONFIRM

    elif q.data == "criteria_remove":
        # Показываем кнопки для каждого критерия
        formatted = format_criteria_list(criteria_list)
        await q.edit_message_text(
            f"Нажми на критерий чтобы убрать его:\n\n{formatted}",
            reply_markup=criteria_remove_kb(criteria_list)
        )
        return CRITERIA_CONFIRM


async def cb_delete_criterion(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Удаляет конкретный критерий по нажатию кнопки."""
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    agent = sessions.get(uid)

    idx = int(q.data.replace("del_criterion_", ""))
    criteria_list = context.user_data.get("criteria_list", [])

    if 0 <= idx < len(criteria_list):
        removed = criteria_list.pop(idx)
        context.user_data["criteria_list"] = criteria_list

    formatted = format_criteria_list(criteria_list)
    await q.edit_message_text(
        f"Убрал! Обновлённый список:\n\n{formatted}\n\nЕщё что-то убрать или всё норм?",
        reply_markup=criteria_remove_kb(criteria_list)
    )
    return CRITERIA_CONFIRM


async def handle_criteria_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Добавляет новый критерий."""
    uid = update.effective_user.id
    agent = sessions.get(uid)
    text = await get_text(update)
    criteria_list = context.user_data.get("criteria_list", [])
    action = context.user_data.get("criteria_action", "add")

    if action == "add":
        # Добавляем через Claude чтобы правильно сформулировал
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": f"Сформулируй критерий допуска в 3-7 словах на основе: '{text}'. Только текст критерия, без номера."
            }]
        )
        new_criterion = response.content[0].text.strip().strip(".")
        criteria_list.append(new_criterion)
        context.user_data["criteria_list"] = criteria_list
        context.user_data.pop("criteria_action", None)

    formatted = format_criteria_list(criteria_list)
    await update.message.reply_text(
        f"Добавил! Обновлённый список:\n\n{formatted}\n\nЕщё что-то или генерируем? 😊",
        reply_markup=criteria_main_kb()
    )
    return CRITERIA_CONFIRM


async def generate_criteria_doc(msg, uid: int, agent: TenderAgent, context: ContextTypes.DEFAULT_TYPE):
    """Генерирует документ критериев с утверждённым списком."""
    from docx_generator import generate_criteria_docx
    from examples_loader import load_examples

    criteria_list = context.user_data.get("criteria_list", [])
    approved_criteria = format_criteria_list(criteria_list)

    examples_text = ""
    texts = load_examples("criteria")
    if texts:
        examples_text = "ПРИМЕРЫ КРИТЕРИЕВ:\n\n"
        for i, t in enumerate(texts[:3], 1):
            examples_text += f"=== Пример {i} ===\n{t[:2000]}\n\n"

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=3000,
            system=f"Ты эксперт по закупкам. Составляешь документ 'Критерии допуска'.\n\n{examples_text}",
            messages=[{
                "role": "user",
                "content": (
                    f"Данные закупки:\n{agent._context()}\n\n"
                    f"Обязательно включи именно эти критерии (можно расширить формулировки):\n{approved_criteria}\n\n"
                    f"Составь полный профессиональный документ критериев допуска. "
                    f"Начни с заголовка 'КРИТЕРИИ ДОПУСКА'."
                )
            }]
        )

        content = response.content[0].text
        last_doc[uid] = {"content": content, "type": "criteria", "name": agent.tender_name}
        path = await generate_criteria_docx(content, agent.tender_name)
        with open(path, "rb") as f:
            await msg.reply_document(
                document=f,
                filename=f"Критерии_{agent.tender_name[:35]}.docx",
                caption="📋 Критерии допуска готовы!"
            )
        os.remove(path)
        await msg.reply_text("Ну как, всё устраивает? 😊", reply_markup=review_kb())
        return REVIEWING

    except Exception as e:
        logger.error(f"Ошибка критериев: {e}", exc_info=True)
        await msg.reply_text("Что-то пошло не так 😕 → /new")
        return ConversationHandler.END


# ─── Чат с памятью о фото ───────────────────────────────────────────────────

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

    if context.user_data.get("waiting_letter_instructions"):
        await generate_letter_reply(update, context)
        return

    gen_keywords = ["нарисуй", "нарисовать", "сгенерируй картинку", "создай картинку", "создай изображение"]
    if any(w in text.lower() for w in gen_keywords):
        await generate_image(update, context, text)
        return

    # История чата — включаем контекст фото если есть
    history = context.user_data.get("chat_history", [])

    # Добавляем контекст последнего фото в системный промпт если есть
    photo_context = context.user_data.get("last_photo_description", "")
    system = CHAT_SYSTEM
    if photo_context:
        system += f"\n\nКОНТЕКСТ: Пользователь недавно прислал фото. Вот что на нём было:\n{photo_context}\nИспользуй эту информацию если пользователь спрашивает про фото."

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
            reply = "Хм, не знаю что сказать 🤔 Попробуй иначе."

        history.append({"role": "assistant", "content": reply})
        context.user_data["chat_history"] = history

        if len(reply) > 4000:
            reply = reply[:4000] + "...\n(сократил 😄)"

        await update.message.reply_text(reply)

    except Exception as e:
        logger.error(f"Ошибка чата: {e}", exc_info=True)
        await update.message.reply_text("Что-то сломалось 😅 Попробуй ещё раз.")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Анализирует фото + ищет в интернете + запоминает для чата."""
    await update.message.reply_text("📷 Смотрю... 🔍 Ищу инфу...")

    result = await get_image_base64(update)
    if not result:
        await update.message.reply_text("Не смог получить фото 😕")
        return

    b64, mime = result
    caption = update.message.caption or ""

    try:
        # Анализируем фото
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
                        "3. Предложи поисковый запрос для поиска доп. информации (1 запрос)\n"
                        f"{'Запрос пользователя: ' + caption if caption else ''}\n\n"
                        "Формат:\n"
                        "ОПИСАНИЕ: [подробное описание]\n"
                        "ТЕКСТ: [текст с фото или 'текста нет']\n"
                        "ПОИСК: [поисковый запрос]"
                    )}
                ]
            }]
        )

        vision_text = vision_response.content[0].text

        # Извлекаем части
        description = ""
        ocr_text = ""
        search_query = caption or "информация по изображению"

        for line in vision_text.split("\n"):
            if line.startswith("ОПИСАНИЕ:"):
                description = line.replace("ОПИСАНИЕ:", "").strip()
            elif line.startswith("ТЕКСТ:"):
                ocr_text = line.replace("ТЕКСТ:", "").strip()
            elif line.startswith("ПОИСК:"):
                search_query = line.replace("ПОИСК:", "").strip()

        # Сохраняем для памяти в чате
        photo_memory = f"{description}"
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

        # Формируем ответ
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

        # Если письмо — предлагаем ответить
        all_text = (description + ocr_text).lower()
        if any(w in all_text for w in ["уважаем", "прошу", "сообщаем", "исх.", "вх.", "настоящим"]):
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Составить ответ на письмо", callback_data="write_reply")],
            ])
            await update.message.reply_text(
                "Похоже на официальное письмо 📄 Составить ответ?",
                reply_markup=kb
            )

    except Exception as e:
        logger.error(f"Ошибка фото: {e}", exc_info=True)
        await update.message.reply_text("Не смог обработать фото 😕")


async def generate_image(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Генерирует изображение через DALL-E."""
    await update.message.reply_text("🎨 Рисую... Художник из меня так себе, но стараюсь 😄")

    # Очищаем запрос
    prompt = text
    for w in ["нарисуй", "нарисовать", "сгенерируй картинку", "создай картинку", "создай изображение", "картинку"]:
        prompt = prompt.lower().replace(w, "").strip()

    if len(prompt) < 3:
        prompt = "абстрактный деловой рисунок"

    try:
        # Пробуем DALL-E 3
        try:
            response = oai.images.generate(
                model="dall-e-3",
                prompt=prompt,
                size="1024x1024",
                quality="standard",
                n=1,
            )
        except Exception:
            # Fallback на DALL-E 2
            response = oai.images.generate(
                model="dall-e-2",
                prompt=prompt[:1000],
                size="512x512",
                n=1,
            )

        image_url = response.data[0].url
        await update.message.reply_photo(
            photo=image_url,
            caption="🎨 Вот что получилось! Если не то — опиши точнее 😄"
        )

    except Exception as e:
        logger.error(f"Ошибка генерации изображения: {e}", exc_info=True)
        # Пробуем через Claude описать — хоть что-то
        await update.message.reply_text(
            f"DALL-E сегодня не в духе 😅\n"
            f"Попробуй описать точнее или чуть позже."
        )


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

    await update.message.reply_text("✉️ Составляю... Постараюсь не облажаться 😄")

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2000,
            system="Составляешь официальные деловые письма на русском. Профессионально, структурированно.",
            messages=[{
                "role": "user",
                "content": f"Оригинальное письмо:\n{original}\n\nИнструкции:\n{instructions}\n\nСоставь ответ."
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
        await q.edit_message_text("Значит я не зря старался 😄 → /new для нового запроса")
        sessions.pop(uid, None)
        last_doc.pop(uid, None)
        return ConversationHandler.END

    elif q.data == "review_edit":
        await q.edit_message_text("Слушаю замечания. Только по делу, без 'переделай всё' 😄")
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
            filename, caption = f"Критерии_{name[:35]}.docx", "📋 Исправленные критерии!"
        else:
            path = await generate_tz_docx(new_content, name)
            filename, caption = f"ТЗ_{name[:40]}.docx", "📄 Исправленное ТЗ!"

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
                MessageHandler(tv, apply_edits),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(MessageHandler(photo_f, chat_reply))
    app.add_handler(CallbackQueryHandler(cb_photo_actions, pattern="^write_reply$"))
    app.add_handler(MessageHandler(tv, chat_reply))

    logger.info("Бот v5 запущен 🚀")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
