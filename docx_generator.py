"""
Генерация Word-документов из текста ТЗ и критериев допуска.
"""
import os
import re
import tempfile
import logging
from docx import Document
from docx.shared import Pt, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

logger = logging.getLogger(__name__)


def _make_doc() -> Document:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Times New Roman"
    style.font.size = Pt(12)
    # Поля страницы
    for section in doc.sections:
        section.top_margin = Inches(1)
        section.bottom_margin = Inches(1)
        section.left_margin = Inches(1.18)
        section.right_margin = Inches(0.79)
    return doc


def _add_content(doc: Document, text: str):
    """Разбирает текст и добавляет абзацы с форматированием."""
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            doc.add_paragraph()
            continue

        # Главный заголовок
        if re.match(r"^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА)", line, re.I):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(line)
            run.bold = True
            run.font.size = Pt(14)

        # Нумерованный раздел: "1. Название"
        elif re.match(r"^\d+\.\s+[А-ЯЁA-Z]", line):
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.bold = True
            run.font.size = Pt(13)
            p.paragraph_format.space_before = Pt(8)

        # Подраздел: "1.1. Название"
        elif re.match(r"^\d+\.\d+", line):
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.bold = True
            run.font.size = Pt(12)

        # Элемент списка
        elif re.match(r"^[-•–]", line):
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(line.lstrip("-•– "))

        # Обычный текст
        else:
            p = doc.add_paragraph(line)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.first_line_indent = Inches(0.5)


async def generate_tz_docx(content: str, name: str) -> str:
    doc = _make_doc()
    _add_content(doc, content)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="TZ_")
    doc.save(tmp.name)
    tmp.close()
    return tmp.name


async def generate_criteria_docx(content: str, name: str) -> str:
    doc = _make_doc()
    _add_content(doc, content)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="Criteria_")
    doc.save(tmp.name)
    tmp.close()
    return tmp.name
