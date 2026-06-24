"""
web_api.py — HTTP-интерфейс Джарвиса для сайта BarsSwarm.

Запускается параллельно с bot.py на Railway.
bot.py не трогаем — этот файл полностью независим.

Эндпоинты:
  POST /api/chat              — отправить сообщение, получить ответ + кнопки
  POST /api/action            — нажать кнопку (как callback в Telegram)
  GET  /api/download/{sid}    — скачать готовый .docx файл
  GET  /api/health            — проверка живости

Архитектура сессий:
  Каждый пользователь сайта получает session_id (UUID).
  Сессия хранит: mode, agent, last_doc, chat_history — аналог context.user_data в боте.
"""

import os
import uuid
import logging
import asyncio
import tempfile
from typing import Optional
from datetime import datetime, timedelta

import anthropic
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent import TenderAgent, QUESTIONS, DIRECTION_NAMES

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Джарвис API", version="1.0")

# CORS — разрешаем сайту коллеги обращаться к API
# В продакшене замените "*" на конкретный домен BarsSwarm
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
WEB_SEARCH_TOOL = {"type": "web_search_20250305", "name": "web_search"}

# ─── Хранилище сессий ────────────────────────────────────────────────────────

class Session:
    def __init__(self):
        self.mode: Optional[str] = None
        self.agent: Optional[TenderAgent] = None
        self.last_doc: Optional[dict] = None      # {"content", "type", "name", "path"}
        self.chat_history: list = []
        self.user_data: dict = {}                 # аналог context.user_data
        self.created_at: datetime = datetime.utcnow()
        self.updated_at: datetime = datetime.utcnow()

    def touch(self):
        self.updated_at = datetime.utcnow()

sessions: dict[str, Session] = {}

def get_session(sid: str) -> Session:
    if sid not in sessions:
        sessions[sid] = Session()
    sessions[sid].touch()
    return sessions[sid]

def cleanup_old_sessions():
    """Удаляем сессии старше 2 часов."""
    cutoff = datetime.utcnow() - timedelta(hours=2)
    old = [k for k, v in sessions.items() if v.updated_at < cutoff]
    for k in old:
        # Удаляем временные файлы
        doc = sessions[k].last_doc
        if doc and doc.get("path"):
            try:
                os.remove(doc["path"])
            except Exception:
                pass
        del sessions[k]

# ─── Промпты (копия из bot.py) ───────────────────────────────────────────────

CHAT_SYSTEM = """Ты - Джарвис, умный и немного саркастичный помощник. Как Джарвис из Железного человека: чёткий, профессиональный, с лёгкой иронией. Помогаешь всегда, без лишней воды.

Твоя ОСНОВНАЯ ЭКСПЕРТИЗА - коммерческие закупки работ и услуг (не государственные закупки, не закупки товаров). В этой теме ты разбираешься глубоко и профессионально.

Но ты свободно общаешься и на любые другие темы - шутки, новости, общие вопросы, что угодно.

Используй поиск в интернете когда это полезно.
Смайлики активно. Русский язык."""

# ─── Модели запросов/ответов ─────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: str
    message: str

class ActionRequest(BaseModel):
    session_id: str
    action: str          # например: "menu_tz", "dir_cleaning", "ans_0", "ans_custom"
    value: Optional[str] = None   # для ans_custom — текст ответа

class ChatResponse(BaseModel):
    reply: str
    buttons: list[dict]  # [{"label": "...", "action": "..."}]
    status: str          # "ok" | "generating" | "doc_ready" | "question"
    doc_available: bool = False

# ─── Утилиты ─────────────────────────────────────────────────────────────────

def make_buttons(items: list[tuple[str, str]]) -> list[dict]:
    """items = [(label, action), ...]"""
    return [{"label": label, "action": action} for label, action in items]

MENU_BUTTONS = make_buttons([
    ("📄 Техническое задание",   "menu_tz"),
    ("📋 Критерии допуска",      "menu_criteria"),
    ("🤝 Сценарий переговоров",  "menu_negotiation"),
    ("✉️ Написать письмо",       "menu_letter"),
    ("🔎 Анализ документа",      "menu_analysis"),
])

DIR_BUTTONS_TZ = make_buttons([
    ("🧹 Клининговые услуги",        "dir_cleaning"),
    ("💻 IT-услуги и автоматизация", "dir_it"),
    ("🔧 Ремонт и техобслуживание",  "dir_repair"),
])

DIR_BUTTONS_CRIT = DIR_BUTTONS_TZ + make_buttons([("✏️ Свой вариант", "dir_custom")])

REVIEW_BUTTONS = make_buttons([
    ("✅ Всё отлично!", "review_ok"),
    ("✏️ Есть замечания", "review_edit"),
])

def question_buttons(options: list[str]) -> list[dict]:
    btns = [{"label": opt, "action": f"ans_{i}"} for i, opt in enumerate(options)]
    btns.append({"label": "Свой вариант", "action": "ans_custom"})
    return btns

# ─── Генерация документов ────────────────────────────────────────────────────

async def do_generate(session: Session) -> ChatResponse:
    """Генерирует ТЗ или критерии через TenderAgent, сохраняет файл."""
    from docx_generator import generate_tz_docx, generate_criteria_docx

    agent = session.agent
    if not agent:
        return ChatResponse(reply="Сессия устарела. Начните заново.", buttons=MENU_BUTTONS, status="ok")

    try:
        if agent.doc_type in ("tz_only", "both"):
            content = await agent.generate_tz()
            path = await generate_tz_docx(content, agent.tender_name)
            session.last_doc = {
                "content": content,
                "type": "tz",
                "name": agent.tender_name,
                "path": path,
                "filename": f"TZ_{agent.tender_name[:30]}.docx",
            }
            reply = f"📄 Техническое задание готово! Нажмите «Скачать», чтобы получить файл Word."

        elif agent.doc_type == "criteria_only":
            content = await agent.generate_criteria()
            path = await generate_criteria_docx(content, agent.tender_name)
            session.last_doc = {
                "content": content,
                "type": "criteria",
                "name": agent.tender_name,
                "path": path,
                "filename": f"Criteria_{agent.tender_name[:30]}.docx",
            }
            reply = f"📋 Критерии допуска готовы! Нажмите «Скачать», чтобы получить файл Word."
        else:
            reply = "Неизвестный тип документа."
            return ChatResponse(reply=reply, buttons=MENU_BUTTONS, status="ok")

        session.mode = None
        buttons = REVIEW_BUTTONS + make_buttons([("⬇️ Скачать документ", "download")])
        return ChatResponse(reply=reply, buttons=buttons, status="doc_ready", doc_available=True)

    except Exception as e:
        logger.error(f"Generate error: {e}", exc_info=True)
        return ChatResponse(reply="Ошибка генерации. Попробуйте ещё раз.", buttons=MENU_BUTTONS, status="ok")


async def do_generate_negotiation(session: Session) -> ChatResponse:
    """Генерирует сценарий переговоров."""
    from docx_generator import generate_tz_docx

    answers = session.user_data.get("neg_answers", {})
    context_lines = ["Сценарий переговоров по коммерческой закупке"]
    for q, a in answers.items():
        context_lines.append(f"{q}: {a}")
    context = "\n".join(context_lines)

    NEGOTIATION_SYSTEM = """Эксперт по закупочным переговорам. Цель: снижение цены и улучшение условий.
Сценарий без воды:
1. ПОЗИЦИЯ ЗАКУПЩИКА
2. ОТКРЫТИЕ (2-3 варианта первых фраз)
3. АРГУМЕНТЫ для давления на цену
4. ВОЗРАЖЕНИЯ участника и точные ответы
5. ЗАКРЫТИЕ сделки
Только конкретные фразы. Русский язык."""

    try:
        r = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=NEGOTIATION_SYSTEM,
            messages=[{"role": "user", "content": context}]
        )
        content = r.content[0].text
        path = await generate_tz_docx(content, "Сценарий переговоров")
        session.last_doc = {
            "content": content,
            "type": "negotiation",
            "name": "Сценарий переговоров",
            "path": path,
            "filename": "Negotiation.docx",
        }
        session.mode = None
        buttons = REVIEW_BUTTONS + make_buttons([("⬇️ Скачать документ", "download")])
        return ChatResponse(reply="🤝 Сценарий переговоров готов!", buttons=buttons, status="doc_ready", doc_available=True)
    except Exception as e:
        logger.error(f"Negotiation error: {e}", exc_info=True)
        return ChatResponse(reply="Ошибка. Попробуйте ещё раз.", buttons=MENU_BUTTONS, status="ok")


# ─── Шаги переговоров (копия из bot.py) ──────────────────────────────────────

NEGOTIATION_STEPS = [
    {"q": "Что закупаем?",
     "opts": ["Клининговые услуги", "IT-услуги", "Ремонт оборудования",
               "Строительные работы", "Консалтинг / аудит", "Другие услуги"]},
    {"q": "Кто придёт от участника?",
     "opts": ["Директор/собственник", "Коммерческий директор", "Менеджер по продажам", "Неизвестно"]},
    {"q": "НМЦ (начальная цена)?",
     "opts": ["До 1 млн руб.", "1-5 млн руб.", "5-20 млн руб.", "Более 20 млн руб."]},
    {"q": "На сколько снижаем цену?",
     "opts": ["На 5-10%", "На 10-20%", "На 20-30%", "Максимально"]},
    {"q": "Есть альтернативные участники?",
     "opts": ["Да, 2+ конкурента", "Есть 1 альтернатива", "Нет, единственный"]},
    {"q": "Доп. цели переговоров?",
     "opts": ["Только снижение цены", "Цена + сроки", "Цена + гарантии", "Цена + объём работ"]},
]

def neg_buttons(step: int) -> list[dict]:
    s = NEGOTIATION_STEPS[step]
    btns = [{"label": opt, "action": f"neg_{step}_{i}"} for i, opt in enumerate(s["opts"])]
    btns.append({"label": "Свой вариант", "action": f"neg_{step}_custom"})
    return btns

# ─── Обработка действий (кнопок) ─────────────────────────────────────────────

async def handle_action(session: Session, action: str, value: Optional[str] = None) -> ChatResponse:
    """Обработка нажатия кнопки — аналог callback_query в боте."""

    # ── Главное меню ──
    if action == "menu_tz":
        session.user_data["doc_type"] = "tz_only"
        session.mode = "choosing_direction"
        return ChatResponse(reply="Выбери направление закупки:", buttons=DIR_BUTTONS_TZ, status="ok")

    if action == "menu_criteria":
        session.user_data["doc_type"] = "criteria_only"
        session.mode = "choosing_direction"
        return ChatResponse(reply="Выбери направление закупки:", buttons=DIR_BUTTONS_CRIT, status="ok")

    if action == "menu_negotiation":
        session.user_data["neg_answers"] = {}
        session.user_data["neg_step"] = 0
        session.mode = "negotiation"
        step = NEGOTIATION_STEPS[0]
        return ChatResponse(reply=step["q"], buttons=neg_buttons(0), status="question")

    if action == "menu_letter":
        session.mode = "awaiting_letter_task"
        return ChatResponse(
            reply="Опишите задачу: что за письмо нужно написать, кому, по какому поводу.",
            buttons=[], status="ok"
        )

    if action == "menu_analysis":
        session.mode = "awaiting_analysis_doc"
        return ChatResponse(
            reply="Вставьте текст документа для экспертизы или опишите его содержание:",
            buttons=[], status="ok"
        )

    if action == "menu_main":
        session.mode = None
        session.agent = None
        session.user_data.clear()
        return ChatResponse(reply="Главное меню. Что делаем?", buttons=MENU_BUTTONS, status="ok")

    # ── Выбор направления ──
    if action.startswith("dir_"):
        direction = action.replace("dir_", "")
        doc_type = session.user_data.get("doc_type", "tz_only")

        if direction == "custom":
            session.mode = "awaiting_custom_direction"
            return ChatResponse(
                reply="Напиши направление закупки своими словами\n(например: охрана объектов, вывоз мусора, обслуживание лифтов):",
                buttons=[], status="ok"
            )

        session.user_data["direction"] = direction
        agent = TenderAgent(direction=direction, doc_type=doc_type)
        session.agent = agent
        session.mode = "answering"
        result = await agent.get_next_question()
        opts = result.get("options", [])
        btns = question_buttons(opts) if opts else []
        return ChatResponse(reply=result["question"], buttons=btns, status="question")

    # ── Ответы на вопросы агента ──
    if action.startswith("ans_"):
        agent = session.agent
        if not agent:
            return ChatResponse(reply="Сессия устарела. Начните заново.", buttons=MENU_BUTTONS, status="ok")

        if action == "ans_custom":
            return ChatResponse(
                reply="Введите свой вариант ответа:",
                buttons=[], status="question"
            )

        idx = int(action.replace("ans_", ""))
        opts = agent.last_question.get("options", [])
        answer = opts[idx] if idx < len(opts) else str(idx)

        result = await agent.submit_answer(answer)

        if result["status"] == "question":
            opts2 = result.get("options", [])
            btns = question_buttons(opts2) if opts2 else []
            return ChatResponse(reply=result["question"], buttons=btns, status="question")

        # Все вопросы отвечены — генерируем
        return await do_generate(session)

    # ── Шаги переговоров ──
    if action.startswith("neg_"):
        parts = action.split("_")
        # neg_{step}_{idx} или neg_{step}_custom
        step = int(parts[1])
        sub = parts[2]

        answers = session.user_data.setdefault("neg_answers", {})

        if sub == "custom":
            session.user_data["neg_step"] = step
            session.user_data["neg_awaiting_custom"] = True
            return ChatResponse(
                reply=f"{NEGOTIATION_STEPS[step]['q']}\n\nВведите свой вариант:",
                buttons=[], status="question"
            )

        idx = int(sub)
        q_text = NEGOTIATION_STEPS[step]["q"]
        answer = NEGOTIATION_STEPS[step]["opts"][idx]
        answers[q_text] = answer

        next_step = step + 1
        if next_step < len(NEGOTIATION_STEPS):
            session.user_data["neg_step"] = next_step
            ns = NEGOTIATION_STEPS[next_step]
            return ChatResponse(reply=ns["q"], buttons=neg_buttons(next_step), status="question")

        return await do_generate_negotiation(session)

    # ── Отзыв о документе ──
    if action == "review_ok":
        session.mode = None
        return ChatResponse(reply="Отлично! 🎉 Чем ещё могу помочь?", buttons=MENU_BUTTONS, status="ok")

    if action == "review_edit":
        session.mode = "awaiting_review_edits"
        return ChatResponse(reply="Опишите замечания — внесу правки и выдам новый файл:", buttons=[], status="ok")

    # ── Скачать ──
    if action == "download":
        if session.last_doc:
            return ChatResponse(
                reply="Файл готов к скачиванию. Нажмите кнопку ниже.",
                buttons=make_buttons([("⬇️ Скачать", "download")]),
                status="doc_ready",
                doc_available=True
            )
        return ChatResponse(reply="Файл не найден. Сгенерируйте документ заново.", buttons=MENU_BUTTONS, status="ok")

    return ChatResponse(reply=f"Неизвестное действие: {action}", buttons=MENU_BUTTONS, status="ok")


# ─── Обработка текстовых сообщений ───────────────────────────────────────────

async def handle_text(session: Session, text: str) -> ChatResponse:
    """Маршрутизация текстовых сообщений по режиму сессии."""

    mode = session.mode

    # ── Ожидаем название направления (свой вариант) ──
    if mode == "awaiting_custom_direction":
        doc_type = session.user_data.get("doc_type", "tz_only")
        session.user_data["direction"] = "cleaning"  # базовые вопросы
        session.user_data["custom_direction_name"] = text
        agent = TenderAgent(direction="cleaning", doc_type=doc_type, custom_name=text)
        session.agent = agent
        session.mode = "answering"
        result = await agent.get_next_question()
        opts = result.get("options", [])
        btns = question_buttons(opts) if opts else []
        return ChatResponse(reply=result["question"], buttons=btns, status="question")

    # ── Свой вариант ответа на вопрос агента ──
    if mode == "answering":
        agent = session.agent
        if not agent:
            return ChatResponse(reply="Сессия устарела.", buttons=MENU_BUTTONS, status="ok")
        result = await agent.submit_answer(text)
        if result["status"] == "question":
            opts = result.get("options", [])
            btns = question_buttons(opts) if opts else []
            return ChatResponse(reply=result["question"], buttons=btns, status="question")
        return await do_generate(session)

    # ── Свой вариант в переговорах ──
    if mode == "negotiation" and session.user_data.get("neg_awaiting_custom"):
        step = session.user_data.get("neg_step", 0)
        answers = session.user_data.setdefault("neg_answers", {})
        answers[NEGOTIATION_STEPS[step]["q"]] = text
        session.user_data["neg_awaiting_custom"] = False
        next_step = step + 1
        if next_step < len(NEGOTIATION_STEPS):
            session.user_data["neg_step"] = next_step
            ns = NEGOTIATION_STEPS[next_step]
            return ChatResponse(reply=ns["q"], buttons=neg_buttons(next_step), status="question")
        return await do_generate_negotiation(session)

    # ── Правки документа ──
    if mode == "awaiting_review_edits":
        doc = session.last_doc
        if not doc:
            session.mode = None
            return ChatResponse(reply="Документ не найден. Начните заново.", buttons=MENU_BUTTONS, status="ok")

        REVIEW_SYSTEM = ("Эксперт по тендерам и деловым документам. Внеси правки и верни ПОЛНЫЙ исправленный текст. "
                         "Только текст, без комментариев.")
        try:
            r = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=8000,
                system=REVIEW_SYSTEM,
                messages=[{"role": "user", "content": f"Документ:\n\n{doc['content']}\n\nЗамечания:\n{text}"}]
            )
            new_content = r.content[0].text
            from docx_generator import generate_tz_docx, generate_criteria_docx
            if doc["type"] == "criteria":
                path = await generate_criteria_docx(new_content, doc["name"])
            else:
                path = await generate_tz_docx(new_content, doc["name"])

            # Удаляем старый файл
            if doc.get("path"):
                try:
                    os.remove(doc["path"])
                except Exception:
                    pass

            session.last_doc = {**doc, "content": new_content, "path": path}
            session.mode = None
            buttons = REVIEW_BUTTONS + make_buttons([("⬇️ Скачать документ", "download")])
            return ChatResponse(reply="✅ Правки внесены! Новый документ готов.", buttons=buttons, status="doc_ready", doc_available=True)
        except Exception as e:
            logger.error(f"Review error: {e}", exc_info=True)
            return ChatResponse(reply="Ошибка при правке. Попробуйте ещё раз.", buttons=[], status="ok")

    # ── Задание на письмо ──
    if mode == "awaiting_letter_task":
        LETTER_SYSTEM = """Составляешь официальные деловые письма на русском языке.
Профессионально, структурированно, вежливо.
Начни с обращения, изложи суть, закончи подписью.
Только текст письма."""
        try:
            r = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=3000,
                system=LETTER_SYSTEM,
                messages=[{"role": "user", "content": text}]
            )
            content = r.content[0].text
            from docx_generator import generate_tz_docx
            path = await generate_tz_docx(content, "Деловое письмо")
            session.last_doc = {
                "content": content, "type": "letter",
                "name": "Деловое письмо", "path": path,
                "filename": "Letter.docx",
            }
            session.mode = None
            buttons = REVIEW_BUTTONS + make_buttons([("⬇️ Скачать документ", "download")])
            return ChatResponse(reply="✉️ Письмо готово!", buttons=buttons, status="doc_ready", doc_available=True)
        except Exception as e:
            logger.error(f"Letter error: {e}", exc_info=True)
            return ChatResponse(reply="Ошибка. Попробуйте ещё раз.", buttons=[], status="ok")

    # ── Анализ документа ──
    if mode == "awaiting_analysis_doc":
        if len(text) > 50:
            session.user_data["analysis_doc"] = text
            session.mode = "awaiting_analysis_comment"
            return ChatResponse(reply="Текст получен. На что обратить внимание? (или напишите «анализируй»)", buttons=[], status="ok")
        return ChatResponse(reply="Вставьте текст документа (минимум пару абзацев).", buttons=[], status="ok")

    if mode == "awaiting_analysis_comment":
        doc_text = session.user_data.get("analysis_doc", "")
        ANALYSIS_SYSTEM = """Ты опытный эксперт по коммерческим закупкам работ и услуг.
Анализируй по критериям:
1. Ошибки и противоречия
2. Ограничивающие требования
3. Нечёткие формулировки
4. Недостающие требования
5. Конкретные предложения по исправлению
Указывай разделы документа. Русский язык."""
        try:
            r = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                system=ANALYSIS_SYSTEM,
                messages=[{"role": "user", "content": f"Документ:\n\n{doc_text}\n\nЗадание: {text}"}]
            )
            content = r.content[0].text
            from docx_generator import generate_tz_docx
            path = await generate_tz_docx(content, "Анализ документа")
            session.last_doc = {
                "content": content, "type": "analysis",
                "name": "Анализ документа", "path": path,
                "filename": "Analysis.docx",
            }
            session.mode = None
            buttons = make_buttons([("⬇️ Скачать документ", "download"), ("🏠 В меню", "menu_main")])
            return ChatResponse(reply="🔎 Анализ готов!", buttons=buttons, status="doc_ready", doc_available=True)
        except Exception as e:
            logger.error(f"Analysis error: {e}", exc_info=True)
            return ChatResponse(reply="Ошибка. Попробуйте ещё раз.", buttons=[], status="ok")

    # ── Свободный чат ──
    history = session.chat_history
    history.append({"role": "user", "content": text})
    if len(history) > 20:
        history = history[-20:]

    try:
        resp = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            system=CHAT_SYSTEM,
            tools=[WEB_SEARCH_TOOL],
            messages=history
        )
        msgs = list(history)
        while resp.stop_reason == "tool_use":
            tr = [{"type": "tool_result", "tool_use_id": b.id, "content": b.input.get("query", "")}
                  for b in resp.content if b.type == "tool_use"]
            msgs.append({"role": "assistant", "content": resp.content})
            msgs.append({"role": "user", "content": tr})
            resp = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
                system=CHAT_SYSTEM,
                tools=[WEB_SEARCH_TOOL],
                messages=msgs
            )
        reply = "".join(b.text for b in resp.content if hasattr(b, "text")) or "Не знаю что ответить."
        history.append({"role": "assistant", "content": reply})
        session.chat_history = history

        # Если ответ длинный — предлагаем меню
        btns = MENU_BUTTONS if len(reply) < 300 else make_buttons([("📋 Меню", "menu_main")])
        return ChatResponse(reply=reply, buttons=btns, status="ok")

    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        return ChatResponse(reply="Что-то сломалось. Попробуй ещё раз.", buttons=MENU_BUTTONS, status="ok")


# ─── Эндпоинты ───────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "sessions": len(sessions)}


@app.post("/api/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """Отправить текстовое сообщение."""
    cleanup_old_sessions()
    session = get_session(req.session_id)
    return await handle_text(session, req.message.strip())


@app.post("/api/action", response_model=ChatResponse)
async def action(req: ActionRequest):
    """Нажать кнопку."""
    cleanup_old_sessions()
    session = get_session(req.session_id)
    return await handle_action(session, req.action, req.value)


@app.get("/api/start/{session_id}", response_model=ChatResponse)
async def start(session_id: str):
    """Инициализация новой сессии — возвращает приветствие и меню."""
    session = get_session(session_id)
    session.mode = None
    session.agent = None
    session.user_data.clear()
    return ChatResponse(
        reply=(
            "Привет! Я Джарвис — помощник по коммерческим закупкам. 👋\n\n"
            "Составлю ТЗ, критерии допуска, сценарий переговоров, деловое письмо "
            "или проанализирую ваш документ.\n\n"
            "Что делаем?"
        ),
        buttons=MENU_BUTTONS,
        status="ok"
    )


@app.get("/api/download/{session_id}")
async def download(session_id: str):
    """Скачать последний сгенерированный документ."""
    session = sessions.get(session_id)
    if not session or not session.last_doc:
        raise HTTPException(status_code=404, detail="Документ не найден")

    path = session.last_doc.get("path")
    filename = session.last_doc.get("filename", "document.docx")

    if not path or not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Файл не найден или устарел")

    return FileResponse(
        path=path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


# ─── Запуск ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("web_api:app", host="0.0.0.0", port=port, reload=False)
