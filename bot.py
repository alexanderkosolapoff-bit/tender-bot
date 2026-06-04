"""
Telegram-бот для генерации ТЗ и критериев допуска.
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

CHOOSING = 1
ANSWERING = 2
CRITERIA_Q = 3

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


def direction_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Клининговые услуги", callback_data="dir_cleaning")],
        [InlineKeyboardButton("IT-услуги", callback_data="dir_it")],
        [InlineKeyboardButton("Ремонт оборудования", callback_data="dir_repair")],
    ])


def doctype_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Только ТЗ", callback_data="doc_tz_only")],
        [InlineKeyboardButton("Только критерии допуска", callback_data="doc_criteria_only")],
        [InlineKeyboardButton("ТЗ и критерии допуска", callback_data="doc_both")],
    ])


async def send_question(msg, result: dict):
    text = result["question"]
    options = result.get("options", [])
    if options:
        kb = [[InlineKeyboardButton(o, callback_data=f"ans_{i}")] for i, o in enumerate(options)]
        kb.append([InlineKeyboardButton("Ввести свой вариант", callback_data="ans_custom")])
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await msg.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text(
        "Добро пожаловать!\n\n"
        "Я помогу подготовить Техническое задание и Критерии допуска для тендера.\n\n"
        "Нажмите /new чтобы начать."
    )


async def new_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text("Выберите направление закупки:", reply_markup=direction_kb())
    return CHOOSING


async def cb_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["direction"] = q.data.replace("dir_", "")
    if "doc_type" not in context.user_data:
        await q.edit_message_text("Что нужно подготовить?", reply_markup=doctype_kb())
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
        await q.edit_message_text("Сессия устарела. Начните заново с /new")
        return ConversationHandler.END

    if q.data == "ans_custom":
        await q.edit_message_text(q.message.text + "\n\nВведите ваш вариант:")
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
        await update.message.reply_text("Сессия устарела. Начните заново с /new")
        return ConversationHandler.END
    text = await get_text(update)
    if not text:
        await update.message.reply_text("Пожалуйста, отправьте текст или голосовое сообщение.")
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
        await msg.reply_text("Все данные собраны. Генерирую документ(ы)...")
        return await do_generate(update, context)


async def do_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_tz_docx, generate_criteria_docx
    uid = update.effective_user.id
    agent = sessions[uid]
    msg = update.callback_query.message if update.callback_query else update.message

    try:
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            path = await generate_tz_docx(content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(document=f,
                                         filename=f"ТЗ_{agent.tender_name[:40]}.docx",
                                         caption="Техническое задание готово!")
            os.remove(path)

        if agent.doc_type in ("criteria_only", "both"):
            content = await agent.generate_criteria()
            path = await generate_criteria_docx(content, agent.tender_name)
            with open(path, "rb") as f:
                await msg.reply_document(document=f,
                                         filename=f"Критерии_{agent.tender_name[:35]}.docx",
                                         caption="Критерии допуска готовы!")
            os.remove(path)

        if agent.doc_type == "tz_only":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, сформировать критерии", callback_data="yes_criteria")],
                [InlineKeyboardButton("Нет, спасибо", callback_data="no_criteria")],
            ])
            await msg.reply_text("Нужны ли критерии допуска для этой закупки?", reply_markup=kb)
            return CRITERIA_Q

    except Exception as e:
        logger.error(f"Ошибка генерации: {e}", exc_info=True)
        await msg.reply_text("Произошла ошибка. Попробуйте /new")

    sessions.pop(uid, None)
    await msg.reply_text("Для нового запроса нажмите /new")
    return ConversationHandler.END


async def cb_criteria(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from docx_generator import generate_criteria_docx
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id

    if q.data == "no_criteria":
        await q.edit_message_text("Хорошо! Для нового запроса нажмите /new")
        sessions.pop(uid, None)
        return ConversationHandler.END

    agent = sessions.get(uid)
    if not agent:
        await q.edit_message_text("Сессия устарела. Начните заново с /new")
        return ConversationHandler.END

    await q.edit_message_text("Генерирую критерии допуска...")
    try:
        content = await agent.generate_criteria()
        path = await generate_criteria_docx(content, agent.tender_name)
        with open(path, "rb") as f:
            await q.message.reply_document(document=f,
                                           filename=f"Критерии_{agent.tender_name[:35]}.docx",
                                           caption="Критерии допуска готовы!")
        os.remove(path)
    except Exception as e:
        logger.error(f"Ошибка: {e}", exc_info=True)
        await q.message.reply_text("Ошибка при создании файла. Попробуйте /new")

    sessions.pop(uid, None)
    await q.message.reply_text("Для нового запроса нажмите /new")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text("Отменено. Нажмите /new для нового запроса.")
    return ConversationHandler.END


def main():
    app = Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).build()
    tv = (filters.TEXT | filters.VOICE) & ~filters.COMMAND

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new", new_request),
        ],
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
                CallbackQueryHandler(cb_criteria, pattern="^(yes|no)_criteria$"),
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
