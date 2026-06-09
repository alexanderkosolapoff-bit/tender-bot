"""
Агент на базе Claude API.
Задаёт вопросы кнопками, генерирует ТЗ и критерии допуска.
"""

import os
import logging
import anthropic
from examples_loader import load_examples

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

QUESTIONS = {
    "cleaning": [
        {"question": "Какой тип помещений нужно убирать?",
         "options": ["Офисные помещения", "Производственные помещения", "Складские помещения", "Смешанный тип"]},
        {"question": "Какова общая площадь помещений?",
         "options": ["До 500 кв. м", "500–2000 кв. м", "2000–5000 кв. м", "Более 5000 кв. м"]},
        {"question": "Какая периодичность уборки требуется?",
         "options": ["Ежедневно", "Несколько раз в неделю", "Еженедельно", "По запросу"]},
        {"question": "Какие виды уборки необходимы?",
         "options": ["Ежедневная поддерживающая", "Генеральная уборка", "Оба вида", "Специализированная"]},
        {"question": "Исполнитель должен обеспечить химию и инвентарь?",
         "options": ["Да, исполнитель обеспечивает всё", "Частично (химия наша, инвентарь их)", "Нет, всё предоставляем мы"]},
        {"question": "Укажи адрес объекта или особые условия доступа:"},
        {"question": "Каков срок действия договора?",
         "options": ["6 месяцев", "1 год", "2 года", "Бессрочно"]},
    ],
    "it": [
        {"question": "Что именно закупаем в сфере IT?",
         "options": ["Разработка ПО", "Техническое обслуживание", "Поставка оборудования", "IT-аутсорсинг"]},
        {"question": "Сколько рабочих мест/пользователей?",
         "options": ["До 10", "10–50", "50–200", "Более 200"]},
        {"question": "Требуется ли круглосуточная поддержка?",
         "options": ["Да, 24/7", "В рабочее время (9:00–18:00)", "По запросу"]},
        {"question": "Какой уровень SLA (время реакции)?",
         "options": ["Критично — до 1 часа", "До 4 часов", "До 1 рабочего дня", "До 3 рабочих дней"]},
        {"question": "Опиши основные задачи или требования:"},
        {"question": "Каков срок действия договора?",
         "options": ["6 месяцев", "1 год", "2 года", "Бессрочно"]},
    ],
    "repair": [
        {"question": "Что именно ремонтируем/обслуживаем?",
         "options": ["Производственное оборудование", "Офисная техника", "Инженерные системы", "Транспортные средства", "Здания и сооружения"]},
        {"question": "Сколько единиц оборудования?",
         "options": ["1–5 единиц", "5–20 единиц", "20–100 единиц", "Более 100 единиц"]},
        {"question": "Нужно ли плановое техобслуживание?",
         "options": ["Да, регулярное ТО", "Только аварийный ремонт", "И ТО, и аварийный ремонт"]},
        {"question": "Время реагирования на аварийный вызов?",
         "options": ["До 2 часов", "До 4 часов", "До 8 часов", "На следующий рабочий день"]},
        {"question": "Укажи адрес объекта и особые условия:"},
        {"question": "Каков срок действия договора?",
         "options": ["6 месяцев", "1 год", "2 года", "Бессрочно"]},
    ],
}

DIRECTION_NAMES = {
    "cleaning": "Клининговые услуги",
    "it": "IT-услуги",
    "repair": "Ремонт и техническое обслуживание",
}

_cache: dict = {}


def _get_examples(direction: str) -> str:
    if direction in _cache:
        return _cache[direction]
    texts = load_examples(direction)
    if not texts:
        _cache[direction] = ""
        return ""
    result = "ПРИМЕРЫ ТЕХНИЧЕСКИХ ЗАДАНИЙ:\n\n"
    for i, t in enumerate(texts[:3], 1):
        result += f"=== Пример {i} ===\n{t[:4000]}\n\n"
    _cache[direction] = result
    return result


def _get_criteria_examples() -> str:
    if "criteria" in _cache:
        return _cache["criteria"]
    texts = load_examples("criteria")
    if not texts:
        _cache["criteria"] = ""
        return ""
    result = "ПРИМЕРЫ КРИТЕРИЕВ ДОПУСКА ИЗ РЕАЛЬНЫХ ТЕНДЕРОВ:\n\n"
    for i, t in enumerate(texts[:5], 1):
        result += f"=== Пример {i} ===\n{t[:3000]}\n\n"
    _cache["criteria"] = result
    return result


class TenderAgent:
    def __init__(self, direction: str = "cleaning", doc_type: str = "tz_only"):
        self.direction   = direction
        self.doc_type    = doc_type
        self.tender_name = DIRECTION_NAMES.get(direction, direction)
        self.questions   = list(QUESTIONS.get(direction, []))
        self.answers:    list[dict] = []
        self.current_q   = 0
        self.last_question: dict = {}

    async def get_next_question(self) -> dict:
        if self.current_q < len(self.questions):
            q = self.questions[self.current_q]
            self.last_question = q
            return {"status": "question", **q}
        return {"status": "generating"}

    async def submit_answer(self, answer: str) -> dict:
        if self.last_question:
            self.answers.append({
                "question": self.last_question.get("question", ""),
                "answer": answer,
            })
        self.current_q += 1
        return await self.get_next_question()

    def _context(self) -> str:
        lines = [f"Направление закупки: {self.tender_name}"]
        for item in self.answers:
            if not item.get("prefilled"):
                lines.append(f"{item['question']}: {item['answer']}")
        return "\n".join(lines)

    def _call(self, system: str, user: str, max_tokens: int = 4000) -> str:
        r = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        return r.content[0].text

    async def generate_tz(self) -> str:
        examples = _get_examples(self.direction)
        system = (
            f"Ты эксперт по коммерческим закупкам работ и услуг. Составляешь Технические задания для коммерческих тендеров.\n"
            f"Направление: {self.tender_name}\n\n"
            f"{examples}\n"
            f"Составь полноценное ТЗ по структуре примеров выше. "
            f"Деловой стиль, русский язык. Фокус на работы и услуги (не товары). "
            f"Начни с заголовка 'ТЕХНИЧЕСКОЕ ЗАДАНИЕ'."
        )
        return self._call(system, f"Данные закупки:\n\n{self._context()}", max_tokens=8000)

    async def generate_criteria(self) -> str:
        examples = _get_criteria_examples()
        system = (
            f"Ты эксперт по коммерческим закупкам работ и услуг. Составляешь документы 'Критерии допуска участников'.\n\n"
            f"{examples}\n"
            f"Составь критерии допуска участников к коммерческой закупке работ/услуг.\n"
            f"Используй примеры выше как образец по уровню детализации и структуре.\n\n"
            f"Для каждого критерия используй строго формат:\n"
            f"КРИТЕРИЙ: [краткое название]\n"
            f"ТРЕБОВАНИЕ: [конкретное измеримое требование]\n"
            f"ДОКУМЕНТ: [подтверждающий документ]\n\n"
            f"Составь 6-10 критериев. Деловой стиль, русский язык. "
            f"Только список критериев, без заголовков и пояснений."
        )
        return self._call(system, f"Данные закупки:\n\n{self._context()}", max_tokens=3000)
