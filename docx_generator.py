"""
Генерация Word-документов из текста ТЗ и критериев допуска.
Форматирование максимально близко к оригинальным примерам.
"""
import os
import re
import tempfile
import logging
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

logger = logging.getLogger(__name__)


def _make_doc() -> Document:
    doc = Document()
    # Поля страницы как в оригинале
    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(3)
        section.right_margin = Cm(1.5)

    # Стиль Normal — Times New Roman 12pt, одинарный интервал
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"
    normal.font.size = Pt(12)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = Pt(14)
    return doc


def _set_font(run, bold=False, size=12):
    run.font.name = "Times New Roman"
    run.font.size = Pt(size)
    run.font.bold = bold


def _add_title(doc, text):
    """Заголовок по центру жирным."""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(text)
    _set_font(run, bold=True, size=14)


def _add_section_heading(doc, text):
    """Заголовок раздела — жирный, выравнивание по левому краю."""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(3)
    run = p.add_run(text)
    _set_font(run, bold=True, size=12)


def _add_body(doc, text, indent=False):
    """Обычный абзац текста."""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(2)
    if indent:
        p.paragraph_format.first_line_indent = Cm(1.25)
    run = p.add_run(text)
    _set_font(run)


def _add_table(doc, rows_data, header=None):
    """Добавляет таблицу с заданными данными."""
    cols = max(len(r) for r in rows_data)
    if header:
        cols = max(cols, len(header))

    table = doc.add_table(rows=0, cols=cols)
    table.style = "Table Grid"

    if header:
        row = table.add_row()
        for i, cell_text in enumerate(header):
            cell = row.cells[i]
            cell.text = ""
            p = cell.paragraphs[0]
            run = p.add_run(cell_text)
            _set_font(run, bold=True, size=11)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER

    for row_data in rows_data:
        row = table.add_row()
        for i, cell_text in enumerate(row_data[:cols]):
            cell = row.cells[i]
            cell.text = ""
            p = cell.paragraphs[0]
            run = p.add_run(str(cell_text))
            _set_font(run, size=11)

    doc.add_paragraph()  # Отступ после таблицы


def _parse_and_add(doc, text):
    """
    Разбирает текст ТЗ и добавляет элементы с правильным форматированием.
    Обрабатывает заголовки, подпункты, таблицы и обычный текст.
    """
    lines = text.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if not line:
            i += 1
            continue

        # Главный заголовок
        if re.match(r"^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА)", line, re.I):
            _add_title(doc, line)
            i += 1
            continue

        # Нумерованный раздел: "1. Название" или "1. НАЗВАНИЕ"
        if re.match(r"^\d+\.\s+\S", line) and len(line) < 150:
            _add_section_heading(doc, line)
            i += 1
            continue

        # Подпункт: "1.1." или "1.1.1."
        if re.match(r"^\d+\.\d+[\.\s]", line):
            _add_body(doc, line, indent=False)
            i += 1
            continue

        # Таблица — ищем строки с разделителем |
        if "|" in line and line.count("|") >= 2:
            table_rows = []
            header_row = None
            while i < len(lines) and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].split("|") if c.strip()]
                if cells:
                    # Пропускаем строки-разделители (---|---|---)
                    if all(re.match(r"^-+$", c) for c in cells):
                        i += 1
                        continue
                    if header_row is None and not table_rows:
                        header_row = cells
                    else:
                        table_rows.append(cells)
                i += 1
            if table_rows or header_row:
                _add_table(doc, table_rows if table_rows else [header_row],
                           header=header_row if table_rows else None)
            continue

        # Элемент списка
        if re.match(r"^[-•–]\s", line):
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(1)
            run = p.add_run(line.lstrip("-•– "))
            _set_font(run)
            i += 1
            continue

        # Обычный текст
        _add_body(doc, line)
        i += 1


async def generate_tz_docx(content: str, name: str) -> str:
    doc = _make_doc()
    _parse_and_add(doc, content)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="TZ_")
    doc.save(tmp.name)
    tmp.close()
    return tmp.name


async def generate_criteria_docx(content: str, name: str) -> str:
    doc = _make_doc()
    _parse_and_add(doc, content)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="Criteria_")
    doc.save(tmp.name)
    tmp.close()
    return tmp.name
