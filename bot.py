"""
Telegram-бот для генерации ТЗ и критериев допуска.
Поддерживает голос и текст. Вопросы задаются кнопками.
"""

import os
import logging
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

CHOOSING_DIRECTION = 1
ANSWERING_QUESTIONS = 2
ASKING_CRITERIA = 3

sessions: dict[int, TenderAgent] = {}


async def get_text(update: Update) -> str | None:
    if update.message.text:
        return update.message.text.strip()
    if update.message.voice:
        await update.message.reply_text("Распознаю голосовое сообщение...")
        file = await update.message.voice.get_file()
        data = await file.download_as_bytearray()
        text = await transcribe_voice(bytes(data))
        await update.message.reply_text(f'Распознано: "{text}"')
        return text
    return None


def direction_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("IT-услуги", callback_data="dir_it")],
        [InlineKeyboardButton("Ремонт оборудования", callback_data="dir_repair")],
    ])


def doc_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Только ТЗ", callback_data="doc_tz_only")],
        [InlineKeyboardButton("Только критерии допуска", callback_data="doc_criteria_only")],
        [InlineKeyboardButton("ТЗ и критерии допуска", callback_data="doc_both")],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    await update.message.reply_text(
        "Добро пожаловать!\n\n"
        "Я помогу подготовить:\n"
        "- Техническое задание (ТЗ)\n"
        "- Критерии допуска к закупке\n\n"
        "Напишите запрос текстом или голосом, например:\n"
        "\"Нужно ТЗ на клининговые услуги\"\n\n"
        "Или нажмите /new чтобы начать пошагово."
    )
    return ConversationHandler.END


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    sessions.pop(user_id, None)
    context.user_data.clear()
    await update.message.reply_text("Выберите направление закупки:", reply_markup=direction_keyboard())
    return CHOOSING_DIRECTION


async def free_text_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_text(update)
    if not text:
        await update.message.reply_text("Пожалуйста, отправьте текст или голосовое сообщение.")
        return CHOOSING_DIRECTION

    context.user_data["initial_request"] = text
    tl = text.lower()

    if any(w in tl for w in ["клининг", "уборк", "чистот"]):
        context.user_data["direction"] = "cleaning"
    elif any(w in tl for w in ["it", " ит ", "информацион", "программ", "компьютер", "сервер"]):
        context.user_data["direction"] = "it"
    elif any(w in tl for w in ["ремонт", "обслуживан", "оборудован"]):
        context.user_data["direction"] = "repair"

    need_tz = any(w in tl for w in ["тз", "техническое задание"])
    need_cr = any(w in tl for w in ["критери", "допуск"])

    if need_tz and need_cr:
        context.user_data["doc_type"] = "both"
    elif need_cr and not need_tz:
        context.user_data["doc_type"] = "criteria_only"
    else:
        context.user_data["doc_type"] = "tz_only"

    if "direction" in context.user_data:
        if "doc_type" not in context.user_data:
            await update.message.reply_text("Что нужно подготовить?", reply_markup=doc_type_keyboard())
            return CHOOSING_DIRECTION
        return await start_questions(update, context)
    else:
        await update.message.reply_text("Уточните направление закупки:", reply_markup=direction_keyboard())
        return CHOOSING_DIRECTION


async def direction_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["direction"] = query.data.replace("dir_", "")
    if "doc_type" not in context.user_data:
        await query.edit_message_text("Что нужно подготовить?", reply_markup=doc_type_keyboard())
        return CHOOSING_DIRECTION
    return await start_questions(update, context, from_callback=True)


async def doc_type_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["doc_type"] = query.data.replace("doc_", "")
    return await start_questions(update, context, from_callback=True)


async def start_questions(update: Update, context: ContextTypes.DEFAULT_TYPE, from_callback=False):
    user_id = update.effective_user.id
    direction = context.user_data.get("direction", "cleaning")
    doc_type = context.user_data.get("doc_type", "tz_only")
    initial = context.user_data.get("initial_request", "")

    agent = TenderAgent(direction=direction, doc_type=doc_type)
    sessions[user_id] = agent

    result = await agent.get_next_question(initial_context=initial)
    msg = update.callback_query.message if from_callback else update.message
    await send_question(msg, result)
    return ANSWERING_QUESTIONS


async def send_question(msg, result: dict):
    text = result["question"]
    options = result.get("options", [])
    if options:
        keyboard = [[InlineKeyboardButton(opt, callback_data=f"ans_{i}")] for i, opt in enumerate(options)]
        keyboard.append([InlineKeyboardButton("Ввести свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await msg.reply_text(text)


async def answer_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    agent = sessions.get(user_id)
    if not agent:
        await query.edit_message_text("Сессия устарела. Начните заново с /new")
        return ConversationHandler.END

    if query.data == "ans_custom":
        await query.edit_message_text(query.message.text + "\n\nВведите ваш вариант ответа:")
        return ANSWERING_QUESTIONS

    idx = int(query.data.replace("ans_", ""))
    options = agent.last_question.get("options", [])
    answer_text = options[idx] if idx < len(options) else ""
    await query.edit_message_text(f"{agent.last_question['question']}\n-> {answer_text}")
    return await process_answer(update, context, answer_text)


async def answer_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    agent = sessions.get(user_id)
    if not agent:
        await update.message.reply_text("Сессия устарела. Начните заново с /new")
        return ConversationHandler.END
    text = await get_text(update)
    if not text:
        await update.message.reply_text("Пожалуйста, отправьте текст или голосовое сообщение.")
        return ANSWERING_QUESTIONS
    return await process_answer(update, context, text)


async def process_answer(update: Update, context: ContextTypes.DEFAULT_TYPE, answer: str):
    user_id = update.effective_user.id
    agent = sessions[user_id]
    result = await agent.submit_answer(answer)

    msg = update.callback_query.message if update.callback_query else update.message

    if result["status"] == "question":
        await send_question(msg, result)
        return ANSWERING_QUESTIONS
    else:
        await msg.reply_text("Все данные собраны. Генерирую документ(ы)...")
        return await generate_and_send(update, context)


async def generate_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx

    user_id = update.effective_user.id
    agent = sessions[user_id]
    msg = update.callback_query.message if update.callback_query else update.message

    try:
        if agent.doc_type in ("tz_only", "both"):
            tz_content = await agent.generate_tz()
            path = await generate_tz_docx(tz_content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(document=f, filename=f"ТЗ_{agent.tender_name[:40]}.docx",
                                         caption="Техническое задание готово!")
            os.remove(path)

        if agent.doc_type in ("criteria_only", "both"):
            cr_content = await agent.generate_criteria()
            path = await generate_criteria_docx(cr_content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(document=f, filename=f"Критерии_{agent.tender_name[:35]}.docx",
                                         caption="Критерии допуска готовы!")
            os.remove(path)

        if agent.doc_type == "tz_only":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, сформировать критерии", callback_data="yes_criteria")],
                [InlineKeyboardButton("Нет, спасибо", callback_data="no_criteria")],
            ])
            await msg.reply_text("Нужны ли критерии допуска для этой закупки?", reply_markup=kb)
            return ASKING_CRITERIA

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await msg.reply_text("Произошла ошибка при создании файла. Попробуйте /new")

    sessions.pop(user_id, None)
    await msg.reply_text("Для нового запроса нажмите /new")
    return ConversationHandler.END


async def criteria_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_criteria_docx
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "no_criteria":
        await query.edit_message_text("Хорошо! Для нового запроса нажмите /new")
        sessions.pop(user_id, None)
        return ConversationHandler.END

    agent = sessions.get(user_id)
    if not agent:
        await query.edit_message_text("Сессия устарела. Начните заново с /new")
        return ConversationHandler.END

    await query.edit_message_text("Генерирую критерии допуска...")
    try:
        cr_content = await agent.generate_criteria()
        path = await generate_criteria_docx(cr_content, agent.tender_name)
        with open(path, "rb") as f:
            await query.message.reply_document(document=f,
                                               filename=f"Критерии_{agent.tender_name[:35]}.docx",
                                               caption="Критерии допуска готовы!")
        os.remove(path)
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await query.message.reply_text("Ошибка при создании файла. Попробуйте /new")

    sessions.pop(user_id, None)
    await query.message.reply_text("Для нового запроса нажмите /new")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    await update.message.reply_text("Отменено. Нажмите /new для нового запроса.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_request), MessageHandler(tv, free_text_entry)],
        states={
            CHOOSING_DIRECTION: [
                CallbackQueryHandler(direction_chosen, pattern="^dir_"),
                CallbackQueryHandler(doc_type_chosen, pattern="^doc_"),
                MessageHandler(tv, free_text_entry),
            ],
            ANSWERING_QUESTIONS: [
                CallbackQueryHandler(answer_button, pattern="^ans_"),
                MessageHandler(tv, answer_text_handler),
            ],
            ASKING_CRITERIA: [
                CallbackQueryHandler(criteria_decision, pattern="^(yes|no)_criteria$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    logger.info("Бот запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
