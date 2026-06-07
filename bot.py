"""
Генерация Word и PDF документов с правильным форматированием.
Markdown разметка убирается, кириллица отображается корректно.
"""
import os
import re
import tempfile
import logging
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH

logger = logging.getLogger(__name__)

# Путь к шрифтам в репозитории
FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_REGULAR = os.path.join(FONTS_DIR, "DejaVuSerif.ttf")
FONT_BOLD    = os.path.join(FONTS_DIR, "DejaVuSerif-Bold.ttf")
FONT_ITALIC  = os.path.join(FONTS_DIR, "DejaVuSerif-Italic.ttf")


def _strip_md(text: str) -> str:
    """Убирает Markdown символы из текста."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'^#+\s*', '', text)
    return text.strip()


def parse_content(text: str) -> list[dict]:
    """
    Разбирает текст (с Markdown) на элементы с типами.
    """
    elements = []
    lines = text.strip().split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        s = raw.strip()
        i += 1

        if not s:
            elements.append({"type": "space"})
            continue

        # Разделитель
        if re.match(r'^[-*]{3,}$', s):
            elements.append({"type": "divider"})
            continue

        # Markdown заголовки
        if s.startswith("### "):
            elements.append({"type": "h3", "text": _strip_md(s[4:])})
            continue
        if s.startswith("## "):
            elements.append({"type": "h2", "text": _strip_md(s[3:])})
            continue
        if s.startswith("# "):
            elements.append({"type": "h1", "text": _strip_md(s[2:])})
            continue

        # Цитата
        if s.startswith("> "):
            elements.append({"type": "quote", "text": _strip_md(s[2:])})
            continue

        # Маркированный список
        if re.match(r'^[-*•]\s', s):
            elements.append({"type": "bullet", "text": _strip_md(s[2:])})
            continue

        # Нумерованный список
        m = re.match(r'^(\d+)\.\s+(.+)', s)
        if m and len(s) < 200:
            elements.append({"type": "numbered", "num": m.group(1), "text": _strip_md(m.group(2))})
            continue

        # Жирный заголовок **Текст:**
        if s.startswith("**") and (":**" in s or s.endswith("**")):
            elements.append({"type": "h3", "text": s.strip("*").rstrip(":")})
            continue

        # Главный заголовок (CAPS)
        if re.match(r'^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА|СЦЕНАРИЙ|ОТВЕТН)', s, re.I):
            elements.append({"type": "title", "text": _strip_md(s)})
            continue

        # Обычный текст
        elements.append({"type": "body", "text": _strip_md(s)})

    return elements


# ─── WORD ────────────────────────────────────────────────────────────────────

def _make_doc() -> Document:
    doc = Document()
    for sec in doc.sections:
        sec.top_margin    = Cm(2)
        sec.bottom_margin = Cm(2)
        sec.left_margin   = Cm(3)
        sec.right_margin  = Cm(1.5)
    n = doc.styles["Normal"]
    n.font.name = "Times New Roman"
    n.font.size = Pt(12)
    n.paragraph_format.space_before = Pt(0)
    n.paragraph_format.space_after  = Pt(0)
    n.paragraph_format.line_spacing = Pt(14)
    return doc


def _run(para, text: str, bold=False, italic=False, size=12):
    run = para.add_run(text)
    run.font.name   = "Times New Roman"
    run.font.size   = Pt(size)
    run.font.bold   = bold
    run.font.italic = italic
    return run


def _build_docx(doc: Document, elements: list[dict]):
    for el in elements:
        t = el["type"]

        if t == "space":
            doc.add_paragraph()

        elif t == "divider":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(3)
            p.paragraph_format.space_after  = Pt(3)
            _run(p, "─" * 50)

        elif t == "title":
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after  = Pt(6)
            _run(p, el["text"], bold=True, size=14)

        elif t == "h1":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(8)
            p.paragraph_format.space_after  = Pt(4)
            _run(p, el["text"], bold=True, size=13)

        elif t in ("h2", "numbered"):
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after  = Pt(3)
            text = el["text"] if t == "h2" else f"{el['num']}. {el['text']}"
            _run(p, text, bold=True, size=12)

        elif t == "h3":
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(4)
            p.paragraph_format.space_after  = Pt(2)
            _run(p, el["text"], bold=True, size=12)

        elif t == "bullet":
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after  = Pt(1)
            _run(p, el["text"])

        elif t == "quote":
            p = doc.add_paragraph()
            p.paragraph_format.left_indent  = Cm(1.5)
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after  = Pt(2)
            _run(p, f'"{el["text"]}"', italic=True)

        elif t == "body":
            if not el["text"]: continue
            p = doc.add_paragraph(el["text"])
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.first_line_indent = Cm(1.25)
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after  = Pt(2)
            for r in p.runs:
                r.font.name = "Times New Roman"
                r.font.size = Pt(12)


async def generate_tz_docx(content: str, name: str) -> str:
    elements = parse_content(content)
    doc = _make_doc()
    _build_docx(doc, elements)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="TZ_")
    doc.save(tmp.name); tmp.close()
    return tmp.name


async def generate_criteria_docx(content: str, name: str) -> str:
    elements = parse_content(content)
    doc = _make_doc()
    _build_docx(doc, elements)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="Crit_")
    doc.save(tmp.name); tmp.close()
    return tmp.name


# ─── PDF ─────────────────────────────────────────────────────────────────────

def _register_fonts():
    """Регистрирует кириллический шрифт. Сначала ищет в папке fonts/ проекта."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Приоритет: шрифты из репозитория → системные
    candidates = [
        (FONT_REGULAR, FONT_BOLD, FONT_ITALIC),
        (
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
        ),
        (
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
        ),
    ]

    for reg, bold, italic in candidates:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont("CyrFont",        reg))
                pdfmetrics.registerFont(TTFont("CyrFont-Bold",   bold   if os.path.exists(bold)   else reg))
                pdfmetrics.registerFont(TTFont("CyrFont-Italic", italic if os.path.exists(italic) else reg))
                from reportlab.pdfbase.pdfmetrics import registerFontFamily
                registerFontFamily("CyrFont",
                    normal="CyrFont", bold="CyrFont-Bold", italic="CyrFont-Italic")
                logger.info(f"PDF font loaded: {reg}")
                return "CyrFont", "CyrFont-Bold", "CyrFont-Italic"
            except Exception as e:
                logger.warning(f"Font load failed {reg}: {e}")

    # Fallback — встроенные шрифты без кириллицы (лучше чем ничего)
    logger.warning("No cyrillic font found, using Helvetica")
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


def _build_pdf(elements: list[dict], output_path: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
    from reportlab.lib.colors import HexColor

    fn, fn_b, fn_i = _register_fonts()

    doc = SimpleDocTemplate(
        output_path, pagesize=A4,
        leftMargin=3*cm, rightMargin=1.5*cm,
        topMargin=2*cm, bottomMargin=2*cm,
    )

    def safe(t): return t.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    title_s   = ParagraphStyle("TT", fontName=fn_b, fontSize=14, alignment=TA_CENTER, spaceAfter=10, spaceBefore=6, leading=18)
    h1_s      = ParagraphStyle("H1", fontName=fn_b, fontSize=13, alignment=TA_LEFT,   spaceAfter=6,  spaceBefore=10, leading=17)
    h2_s      = ParagraphStyle("H2", fontName=fn_b, fontSize=12, alignment=TA_LEFT,   spaceAfter=4,  spaceBefore=7, leading=16)
    h3_s      = ParagraphStyle("H3", fontName=fn_b, fontSize=12, alignment=TA_LEFT,   spaceAfter=3,  spaceBefore=5, leading=16)
    body_s    = ParagraphStyle("BD", fontName=fn,   fontSize=12, alignment=TA_JUSTIFY, spaceAfter=4, spaceBefore=0, firstLineIndent=1.25*cm, leading=16)
    bullet_s  = ParagraphStyle("BL", fontName=fn,   fontSize=12, alignment=TA_LEFT,   spaceAfter=2,  spaceBefore=0, leftIndent=0.8*cm, leading=16)
    quote_s   = ParagraphStyle("QT", fontName=fn_i, fontSize=12, alignment=TA_LEFT,   spaceAfter=4,  spaceBefore=2, leftIndent=1.5*cm, leading=16, textColor=HexColor("#444444"))

    story = []
    for el in elements:
        t = el["type"]
        text = safe(el.get("text", ""))

        if t == "space":
            story.append(Spacer(1, 6))
        elif t == "divider":
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#888888")))
            story.append(Spacer(1, 4))
        elif t == "title":
            story.append(Paragraph(text, title_s))
        elif t == "h1":
            story.append(Paragraph(text, h1_s))
        elif t in ("h2", "numbered"):
            label = text if t == "h2" else f"{el['num']}. {text}"
            story.append(Paragraph(label, h2_s))
        elif t == "h3":
            story.append(Paragraph(text, h3_s))
        elif t == "bullet":
            story.append(Paragraph(f"• {text}", bullet_s))
        elif t == "quote":
            story.append(Paragraph(f"«{text}»", quote_s))
        elif t == "body":
            if text.strip():
                story.append(Paragraph(text, body_s))

    doc.build(story)


async def generate_pdf(content: str, name: str) -> str:
    elements = parse_content(content)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="Doc_")
    tmp.close()
    _build_pdf(elements, tmp.name)
    return tmp.name
