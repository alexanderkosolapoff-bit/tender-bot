"""
Генерация Word и PDF документов.
"""
import os
import re
import tempfile
import logging
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

logger = logging.getLogger(__name__)


def _make_doc() -> Document:
    doc = Document()
    for section in doc.sections:
        section.top_margin = Cm(2)
        section.bottom_margin = Cm(2)
        section.left_margin = Cm(3)
        section.right_margin = Cm(1.5)
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


def _parse_and_add(doc: Document, text: str):
    lines = text.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            doc.add_paragraph()
            i += 1
            continue

        if re.match(r"^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА|СЦЕНАРИЙ ПЕРЕГОВОРОВ|ОТВЕТ|УВАЖАЕМ)", line, re.I):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(6)
            run = p.add_run(line)
            _set_font(run, bold=True, size=14)

        elif re.match(r"^\d+\.\s+\S", line) and len(line) < 150:
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(3)
            run = p.add_run(line)
            _set_font(run, bold=True, size=12)

        elif re.match(r"^\d+\.\d+", line):
            p = doc.add_paragraph()
            run = p.add_run(line)
            _set_font(run, bold=True, size=12)

        elif "|" in line and line.count("|") >= 2:
            table_rows = []
            header_row = None
            while i < len(lines) and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].split("|") if c.strip()]
                if cells and not all(re.match(r"^-+$", c) for c in cells):
                    if header_row is None:
                        header_row = cells
                    else:
                        table_rows.append(cells)
                i += 1
            if header_row:
                cols = max(len(header_row), max((len(r) for r in table_rows), default=0))
                table = doc.add_table(rows=0, cols=cols)
                table.style = "Table Grid"
                hr = table.add_row()
                for j, cell_text in enumerate(header_row[:cols]):
                    p = hr.cells[j].paragraphs[0]
                    run = p.add_run(cell_text)
                    _set_font(run, bold=True, size=11)
                for row_data in table_rows:
                    row = table.add_row()
                    for j, cell_text in enumerate(row_data[:cols]):
                        p = row.cells[j].paragraphs[0]
                        run = p.add_run(cell_text)
                        _set_font(run, size=11)
                doc.add_paragraph()
            continue

        elif re.match(r"^[-•–]\s", line):
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(1)
            run = p.add_run(line.lstrip("-•– "))
            _set_font(run)

        elif re.match(r"^[A-ZА-ЯЁ\*#]{2,}", line) and len(line) < 100:
            # Жирный подзаголовок (CAPS или ##)
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            clean = line.lstrip("#* ").rstrip("#* ")
            run = p.add_run(clean)
            _set_font(run, bold=True, size=12)

        else:
            p = doc.add_paragraph(line)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.first_line_indent = Cm(1.25)

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


async def generate_pdf(content: str, name: str) -> str:
    """Генерирует PDF через reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="Doc_")
    tmp.close()

    doc = SimpleDocTemplate(
        tmp.name,
        pagesize=A4,
        leftMargin=3*cm,
        rightMargin=1.5*cm,
        topMargin=2*cm,
        bottomMargin=2*cm,
    )

    # Пробуем подключить кириллический шрифт
    font_name = "Helvetica"  # fallback
    try:
        # Ищем системный шрифт с кириллицей
        font_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                pdfmetrics.registerFont(TTFont("CyrFont", fp))
                font_name = "CyrFont"
                break
    except Exception:
        pass

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "DocTitle", fontName=font_name, fontSize=14,
        alignment=TA_CENTER, spaceAfter=12, spaceBefore=6, leading=18
    )
    heading_style = ParagraphStyle(
        "DocHeading", fontName=font_name, fontSize=12,
        alignment=TA_LEFT, spaceAfter=6, spaceBefore=8, leading=16
    )
    body_style = ParagraphStyle(
        "DocBody", fontName=font_name, fontSize=12,
        alignment=TA_JUSTIFY, spaceAfter=4, spaceBefore=0,
        firstLineIndent=1.25*cm, leading=16
    )
    bullet_style = ParagraphStyle(
        "DocBullet", fontName=font_name, fontSize=12,
        alignment=TA_LEFT, spaceAfter=2, spaceBefore=0,
        leftIndent=0.5*cm, leading=16
    )

    story = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            story.append(Spacer(1, 6))
            continue

        # Экранируем спецсимволы XML
        safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        if re.match(r"^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА|СЦЕНАРИЙ|ОТВЕТ)", safe, re.I):
            story.append(Paragraph(f"<b>{safe}</b>", title_style))
        elif re.match(r"^\d+\.\s+\S", safe) and len(safe) < 150:
            story.append(Paragraph(f"<b>{safe}</b>", heading_style))
        elif re.match(r"^\d+\.\d+", safe):
            story.append(Paragraph(f"<b>{safe}</b>", heading_style))
        elif re.match(r"^[-•–]\s", safe):
            story.append(Paragraph("• " + safe.lstrip("-•– "), bullet_style))
        elif re.match(r"^[A-ZА-ЯЁ]{3,}", safe) and len(safe) < 100:
            story.append(Paragraph(f"<b>{safe}</b>", heading_style))
        else:
            story.append(Paragraph(safe, body_style))

    doc.build(story)
    return tmp.name
