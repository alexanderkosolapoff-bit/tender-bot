"""
Агент на базе Claude API.
Задаёт вопросы кнопками, генерирует ТЗ и критерии допуска.
"""

import os
import logging
import anthropic
from examples_loader import load_examples

logger = logging.getLogger(__name__)

QUESTIONS = {
    "cleaning": [
        {"question": "Какой тип помещений нужно убирать?",
         "options": ["Офисные помещения", "Производственные помещения", "Складские помещения", "Смешанный тип"]},
        {"question": "Какова общая площадь помещений?",
         "options": ["До 500 кв. м", "500-2000 кв. м", "2000-5000 кв. м", "Более 5000 кв. м"]},
        {"question": "Какая периодичность уборки требуется?",
         "options": ["Ежедневно", "Несколько раз в неделю", "Еженедельно", "По запросу"]},
        {"question": "Какие виды уборки необходимы?",
         "options": ["Только поддерживающая", "Поддерживающая + генеральная", "Полный комплекс (все виды)"]},
        {"question": "Исполнитель обеспечивает инвентарь и химию?",
         "options": ["Да, исполнитель обеспечивает всё", "Частично (химия наша, инвентарь их)", "Нет, всё предоставляем мы"]},
        {"question": "Каков срок оказания услуг?",
         "options": ["1 месяц", "3 месяца", "6 месяцев", "1 год"]},
        {"question": "Укажите адрес объекта (город, улица):", "options": []},
        {"question": "Есть ли особые требования к исполнителю?",
         "options": ["Нет особых требований", "Допуск к режимным объектам", "Работа в ночное время", "Только экологичные средства"]},
    ],
    "it": [
        {"question": "Какой вид IT-услуг требуется?",
         "options": ["Техобслуживание оборудования", "Поддержка программного обеспечения", "Информационная безопасность", "Разработка/доработка ПО"]},
        {"question": "Сколько рабочих мест охватывает закупка?",
         "options": ["До 20", "20-100", "100-500", "Более 500"]},
        {"question": "Какой режим поддержки необходим?",
         "options": ["Рабочие часы (9:00-18:00)", "Расширенный (8:00-22:00)", "Круглосуточно 24/7", "По заявкам без нормативов"]},
        {"question": "Максимальное время реакции на критическую заявку?",
         "options": ["До 1 часа", "До 4 часов", "До 8 часов", "До 24 часов"]},
        {"question": "Требуется ли выезд специалиста на объект?",
         "options": ["Да, обязательно", "По необходимости (основное - удалённо)", "Только удалённая поддержка"]},
        {"question": "Каков срок действия договора?",
         "options": ["3 месяца", "6 месяцев", "1 год", "2 года"]},
        {"question": "Укажите основные используемые системы или ПО:", "options": []},
        {"question": "Требования к квалификации специалистов?",
         "options": ["Нет особых требований", "Наличие сертификатов вендоров", "Допуск к гостайне", "Опыт работы с госструктурами"]},
    ],
    "repair": [
        {"question": "Какое оборудование подлежит ремонту/обслуживанию?",
         "options": ["Офисная техника (принтеры, МФУ)", "Промышленное оборудование", "Медицинское оборудование", "Инженерные системы (вентиляция, лифты)"]},
        {"question": "Сколько единиц оборудования?",
         "options": ["До 10", "10-50", "50-200", "Более 200"]},
        {"question": "Какой вид работ требуется?",
         "options": ["Только плановое ТО", "Только ремонт по заявкам", "ТО + ремонт по заявкам", "Полное сервисное обслуживание"]},
        {"question": "Как часто проводится плановое ТО?",
         "options": ["Ежемесячно", "Ежеквартально", "Раз в полгода", "Раз в год"]},
        {"question": "Максимальное время устранения неисправности?",
         "options": ["До 4 часов", "До 1 рабочего дня", "До 3 рабочих дней", "До 7 рабочих дней"]},
        {"question": "Кто обеспечивает запасные части?",
         "options": ["Исполнитель (включено в стоимость)", "Исполнитель (оплачивается отдельно)", "Заказчик предоставляет запчасти"]},
        {"question": "Каков срок действия договора?",
         "options": ["3 месяца", "6 месяцев", "1 год", "2 года"]},
        {"question": "Укажите адрес объекта или особые условия доступа:", "options": []},
    ],
}

DIRECTION_NAMES = {
    "cleaning": "Клининговые услуги",
    "it": "IT-услуги",
    "repair": "Ремонт и обслуживание оборудования",
}

_cache: dict = {}


def _get_examples(direction: str) -> str:
    if direction in _cache:
        return _cache[direction]
    texts = load_examples(direction)
    result = ""
    if texts:
        result = f"ПРИМЕРЫ ТЗ (используй их структуру и стиль):\n\n"
        for i, t in enumerate(texts[:5], 1):
            result += f"=== Пример {i} ===\n{t[:3000]}\n\n"
    _cache[direction] = result
    return result


def _get_criteria_examples() -> str:
    if "criteria" in _cache:
        return _cache["criteria"]
    texts = load_examples("criteria")
    result = ""
    if texts:
        result = "ПРИМЕРЫ КРИТЕРИЕВ ДОПУСКА (используй их структуру):\n\n"
        for i, t in enumerate(texts[:5], 1):
            result += f"=== Пример {i} ===\n{t[:3000]}\n\n"
    _cache["criteria"] = result
    return result


class TenderAgent:
    def __init__(self, direction: str, doc_type: str):
        self.direction = direction
        self.doc_type = doc_type
        self.tender_name = DIRECTION_NAMES.get(direction, direction)
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.questions = QUESTIONS.get(direction, [])
        self.current_q = 0
        self.answers: list[dict] = []
        self.last_question: dict = {}

    async def get_next_question(self, initial_context: str = "") -> dict:
        if self.current_q < len(self.questions):
            q = self.questions[self.current_q]
            self.last_question = q
            return q
        return {"question": "__done__", "options": []}

    async def submit_answer(self, answer: str) -> dict:
        if self.current_q < len(self.questions):
            self.answers.append({
                "question": self.questions[self.current_q]["question"],
                "answer": answer
            })
            self.current_q += 1

        if self.current_q < len(self.questions):
            next_q = self.questions[self.current_q]
            self.last_question = next_q
            return {"status": "question", **next_q}
        return {"status": "generating"}

    def _context(self) -> str:
        lines = [f"Направление закупки: {self.tender_name}"]
        for item in self.answers:
            lines.append(f"- {item['question']}: {item['answer']}")
        return "\n".join(lines)

    def _call(self, system: str, user: str, max_tokens: int = 4000) -> str:
        r = self.client.messages.create(
            model="claude-3-opus-20240229",
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}]
        )
        return r.content[0].text

    async def generate_tz(self) -> str:
        examples = _get_examples(self.direction)
        system = (
            f"Ты эксперт по закупкам. Составляешь Технические задания для тендеров.\n"
            f"Направление: {self.tender_name}\n\n"
            f"{examples}\n"
            f"Составь полноценное ТЗ по структуре примеров выше. "
            f"Деловой стиль, русский язык. "
            f"Начни с заголовка 'ТЕХНИЧЕСКОЕ ЗАДАНИЕ'."
        )
        return self._call(system, f"Данные закупки:\n\n{self._context()}", max_tokens=4000)

    async def generate_criteria(self) -> str:
        examples = _get_criteria_examples()
        system = (
            f"Ты эксперт по закупкам. Составляешь документы 'Критерии допуска участников'.\n\n"
            f"{examples}\n"
            f"Составь критерии допуска: сначала общие обязательные, "
            f"затем специфические для данной закупки. "
            f"Деловой стиль, русский язык. "
            f"Начни с заголовка 'КРИТЕРИИ ДОПУСКА'."
        )
        return self._call(system, f"Данные закупки:\n\n{self._context()}", max_tokens=3000)
