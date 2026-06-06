"""
Генерация Word и PDF документов с правильным форматированием.
"""
import os
import re
import tempfile
import logging
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

logger = logging.getLogger(__name__)


def clean_markdown(text: str) -> list[dict]:
    """
    Парсит Markdown текст и возвращает список элементов с типами:
    {'type': 'title'|'heading1'|'heading2'|'heading3'|'bullet'|'body'|'quote'|'divider', 'text': str}
    """
    elements = []
    lines = text.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Пустая строка
        if not stripped:
            elements.append({"type": "space", "text": ""})
            i += 1
            continue

        # Разделитель ---
        if re.match(r"^[-]{3,}$", stripped):
            elements.append({"type": "divider", "text": ""})
            i += 1
            continue

        # Заголовки Markdown: # ## ###
        if stripped.startswith("### "):
            elements.append({"type": "heading3", "text": stripped[4:].strip()})
            i += 1
            continue
        if stripped.startswith("## "):
            elements.append({"type": "heading2", "text": stripped[3:].strip()})
            i += 1
            continue
        if stripped.startswith("# "):
            elements.append({"type": "heading1", "text": stripped[2:].strip()})
            i += 1
            continue

        # Нумерованные разделы: "1. Название"
        if re.match(r"^\d+\.\d*\s+\S", stripped) and len(stripped) < 150:
            clean = _strip_md(stripped)
            elements.append({"type": "heading2", "text": clean})
            i += 1
            continue

        # Цитата > текст
        if stripped.startswith("> "):
            elements.append({"type": "quote", "text": stripped[2:].strip()})
            i += 1
            continue

        # Список: - или • или *
        if re.match(r"^[-•*]\s", stripped):
            clean = _strip_md(stripped.lstrip("-•* "))
            elements.append({"type": "bullet", "text": clean})
            i += 1
            continue

        # Главный заголовок документа (CAPS или известные слова)
        if re.match(r"^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА|СЦЕНАРИЙ ПЕРЕГОВОРОВ|ОТВЕТНОЕ ПИСЬМО)", stripped, re.I):
            elements.append({"type": "title", "text": _strip_md(stripped)})
            i += 1
            continue

        # Жирный заголовок **Текст:** или **ТЕКСТ**
        if stripped.startswith("**") and (stripped.endswith("**") or stripped.endswith(":**")):
            clean = stripped.strip("*").rstrip(":")
            elements.append({"type": "heading3", "text": clean})
            i += 1
            continue

        # Обычный текст
        elements.append({"type": "body", "text": _strip_md(stripped)})
        i += 1

    return elements


def _strip_md(text: str) -> str:
    """Убирает Markdown символы из текста."""
    # Убираем **жирный** и *курсив*
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    # Убираем `код`
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Убираем [ссылка](url)
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)
    return text.strip()


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


def _set_font(run, bold=False, size=12, italic=False):
    run.font.name = "Times New Roman"
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic


def _build_docx(doc: Document, elements: list[dict]):
    """Добавляет элементы в Word документ."""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    for el in elements:
        t = el["type"]
        text = el["text"]

        if t == "space":
            doc.add_paragraph()

        elif t == "divider":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(4)
            run = p.add_run("─" * 40)
            run.font.color.rgb = None

        elif t == "title":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(6)
            run = p.add_run(text)
            _set_font(run, bold=True, size=14)

        elif t == "heading1":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after = Pt(4)
            run = p.add_run(text)
            _set_font(run, bold=True, size=13)

        elif t == "heading2":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(3)
            run = p.add_run(text)
            _set_font(run, bold=True, size=12)

        elif t == "heading3":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after = Pt(2)
            run = p.add_run(text)
            _set_font(run, bold=True, size=12)

        elif t == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.left_indent = Cm(0.5)
            run = p.add_run(text)
            _set_font(run)

        elif t == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(1.5)
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            run = p.add_run(text)
            _set_font(run, italic=True)

        elif t == "body":
            p = doc.add_paragraph(text)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.first_line_indent = Cm(1.25)
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(2)
            for run in p.runs:
                _set_font(run)


def _build_pdf(elements: list[dict], output_path: str):
    """Создаёт PDF из элементов через reportlab."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
    from reportlab.lib.colors import HexColor

    # Подключаем кириллический шрифт
    font_name = "Helvetica"
    font_bold = "Helvetica-Bold"
    font_italic = "Helvetica-Oblique"

    font_paths = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"),
        ("/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSerifBold.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf"),
    ]

    for reg, bold_p, italic_p in font_paths:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont("CyrFont", reg))
                font_name = "CyrFont"
                if os.path.exists(bold_p):
                    pdfmetrics.registerFont(TTFont("CyrFont-Bold", bold_p))
                    font_bold = "CyrFont-Bold"
                if os.path.exists(italic_p):
                    pdfmetrics.registerFont(TTFont("CyrFont-Italic", italic_p))
                    font_italic = "CyrFont-Italic"
                break
            except Exception:
                pass

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=3*cm, rightMargin=1.5*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    # Стили
    title_s = ParagraphStyle("T", fontName=font_bold, fontSize=14,
                             alignment=TA_CENTER, spaceAfter=10, spaceBefore=6, leading=18)
    h1_s = ParagraphStyle("H1", fontName=font_bold, fontSize=13,
                          alignment=TA_LEFT, spaceAfter=6, spaceBefore=10, leading=17)
    h2_s = ParagraphStyle("H2", fontName=font_bold, fontSize=12,
                          alignment=TA_LEFT, spaceAfter=4, spaceBefore=7, leading=16)
    h3_s = ParagraphStyle("H3", fontName=font_bold, fontSize=12,
                          alignment=TA_LEFT, spaceAfter=3, spaceBefore=5, leading=16)
    body_s = ParagraphStyle("B", fontName=font_name, fontSize=12,
                            alignment=TA_JUSTIFY, spaceAfter=4, spaceBefore=0,
                            firstLineIndent=1.25*cm, leading=16)
    bullet_s = ParagraphStyle("BL", fontName=font_name, fontSize=12,
                              alignment=TA_LEFT, spaceAfter=2, spaceBefore=0,
                              leftIndent=0.8*cm, leading=16)
    quote_s = ParagraphStyle("Q", fontName=font_italic, fontSize=12,
                             alignment=TA_LEFT, spaceAfter=4, spaceBefore=2,
                             leftIndent=1.5*cm, leading=16,
                             textColor=HexColor("#444444"))

    def safe(text: str) -> str:
        """Экранирует XML символы."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    story = []
    for el in elements:
        t = el["type"]
        text = safe(el["text"])

        if t == "space":
            story.append(Spacer(1, 6))
        elif t == "divider":
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#999999")))
            story.append(Spacer(1, 4))
        elif t == "title":
            story.append(Paragraph(text, title_s))
        elif t == "heading1":
            story.append(Paragraph(text, h1_s))
        elif t == "heading2":
            story.append(Paragraph(text, h2_s))
        elif t == "heading3":
            story.append(Paragraph(text, h3_s))
        elif t == "bullet":
            story.append(Paragraph(f"• {text}", bullet_s))
        elif t == "quote":
            story.append(Paragraph(f"«{text}»", quote_s))
        elif t == "body":
            if text.strip():
                story.append(Paragraph(text, body_s))

    doc.build(story)


async def generate_tz_docx(content: str, name: str) -> str:
    elements = clean_markdown(content)
    doc = _make_doc()
    _build_docx(doc, elements)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="TZ_")
    doc.save(tmp.name)
    tmp.close()
    return tmp.name


async def generate_criteria_docx(content: str, name: str) -> str:
    elements = clean_markdown(content)
    doc = _make_doc()
    _build_docx(doc, elements)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="Criteria_")
    doc.save(tmp.name)
    tmp.close()
    return tmp.name


async def generate_pdf(content: str, name: str) -> str:
    elements = clean_markdown(content)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="Doc_")
    tmp.close()
    _build_pdf(elements, tmp.name)
    return tmp.name
