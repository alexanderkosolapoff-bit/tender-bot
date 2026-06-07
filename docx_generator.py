"""
Генерация Word и PDF документов.
Критерии допуска — в таблице с колонками: №, Критерий, Требование, Документ-подтверждение.
"""
import os
import re
import tempfile
import logging
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

logger = logging.getLogger(__name__)

# Ищем шрифты — сначала в папке fonts/, потом в корне проекта
_BASE = os.path.dirname(os.path.abspath(__file__))
FONT_REG  = os.path.join(_BASE, "fonts", "DejaVuSerif.ttf") if os.path.exists(os.path.join(_BASE, "fonts", "DejaVuSerif.ttf")) else os.path.join(_BASE, "DejaVuSerif.ttf")
FONT_BOLD = os.path.join(_BASE, "fonts", "DejaVuSerif-Bold.ttf") if os.path.exists(os.path.join(_BASE, "fonts", "DejaVuSerif-Bold.ttf")) else os.path.join(_BASE, "DejaVuSerif-Bold.ttf")
FONT_ITAL = os.path.join(_BASE, "fonts", "DejaVuSerif-Italic.ttf") if os.path.exists(os.path.join(_BASE, "fonts", "DejaVuSerif-Italic.ttf")) else os.path.join(_BASE, "DejaVuSerif-Italic.ttf")


def _strip_md(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', text)
    text = re.sub(r'^#+\s*', '', text)
    return text.strip()


def parse_content(text: str) -> list[dict]:
    elements = []
    for line in text.strip().split("\n"):
        s = line.strip()
        if not s:
            elements.append({"type": "space"})
            continue
        if re.match(r'^[-*]{3,}$', s):
            elements.append({"type": "divider"})
            continue
        if s.startswith("### "):
            elements.append({"type": "h3", "text": _strip_md(s[4:])})
            continue
        if s.startswith("## "):
            elements.append({"type": "h2", "text": _strip_md(s[3:])})
            continue
        if s.startswith("# "):
            elements.append({"type": "h1", "text": _strip_md(s[2:])})
            continue
        if s.startswith("> "):
            elements.append({"type": "quote", "text": _strip_md(s[2:])})
            continue
        if re.match(r'^[-*•]\s', s):
            elements.append({"type": "bullet", "text": _strip_md(s[2:])})
            continue
        m = re.match(r'^(\d+)\.\s+(.+)', s)
        if m and len(s) < 200:
            elements.append({"type": "numbered", "num": m.group(1), "text": _strip_md(m.group(2))})
            continue
        if s.startswith("**") and (":**" in s or s.endswith("**")):
            elements.append({"type": "h3", "text": s.strip("*").rstrip(":")})
            continue
        if re.match(r'^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА|СЦЕНАРИЙ|ОТВЕТН)', s, re.I):
            elements.append({"type": "title", "text": _strip_md(s)})
            continue
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


def _set_cell_bg(cell, hex_color: str):
    """Закрашивает ячейку таблицы."""
    tc = cell._tc
    tcPr = tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_color)
    tcPr.append(shd)


def _build_docx(doc: Document, elements: list[dict]):
    for el in elements:
        t = el["type"]
        if t == "space":
            doc.add_paragraph()
        elif t == "divider":
            p = doc.add_paragraph()
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
            _run(p, f'"{el["text"]}"', italic=True)
        elif t == "body":
            if not el.get("text"): continue
            p = doc.add_paragraph(el["text"])
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.first_line_indent = Cm(1.25)
            for r in p.runs:
                r.font.name = "Times New Roman"
                r.font.size = Pt(12)


def _parse_criteria_rows(text: str) -> list[dict]:
    """
    Извлекает критерии из текста.
    Возвращает список dict: {num, criterion, requirement, document}
    """
    rows = []
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    # Пробуем найти структурированные критерии
    i = 0
    num = 1
    while i < len(lines):
        line = lines[i]

        # Пропускаем заголовок
        if re.match(r'^КРИТЕРИИ ДОПУСКА', line, re.I):
            i += 1
            continue

        # Ищем паттерн: "1. Критерий"
        m = re.match(r'^(\d+)[.)]\s*(.+)', line)
        if m:
            criterion_text = _strip_md(m.group(2))
            requirement = ""
            document = ""

            # Смотрим следующие строки на предмет требования/документа
            j = i + 1
            while j < len(lines) and j < i + 4:
                next_line = lines[j].lower()
                if any(w in next_line for w in ["требован", "наличие", "не менее", "должн", "копия", "документ", "справка", "лицензи", "сертификат"]):
                    if not requirement:
                        requirement = _strip_md(lines[j])
                    else:
                        document = _strip_md(lines[j])
                j += 1

            if not requirement:
                requirement = criterion_text
                criterion_text = f"Критерий {num}"

            rows.append({
                "num": str(num),
                "criterion": criterion_text,
                "requirement": requirement,
                "document": document or "Документы по запросу организатора"
            })
            num += 1
            i += 1
            continue

        # Ищем строки-критерии без номера (маркеры - • *)
        m2 = re.match(r'^[-•*]\s*(.+)', line)
        if m2:
            criterion_text = _strip_md(m2.group(1))
            rows.append({
                "num": str(num),
                "criterion": criterion_text,
                "requirement": criterion_text,
                "document": "Документы по запросу организатора"
            })
            num += 1
            i += 1
            continue

        i += 1

    return rows


def _build_criteria_table(doc: Document, rows: list[dict]):
    """Строит таблицу критериев допуска."""
    # Заголовок таблицы
    headers = ["№", "Критерий допуска", "Требование", "Документ-подтверждение"]
    col_widths = [Cm(1.2), Cm(5.5), Cm(5.0), Cm(5.0)]

    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"

    # Устанавливаем ширину колонок
    for i, width in enumerate(col_widths):
        for cell in table.columns[i].cells:
            cell.width = width

    # Шапка таблицы
    hdr_row = table.rows[0]
    for i, header in enumerate(headers):
        cell = hdr_row.cells[i]
        _set_cell_bg(cell, "D6E4F0")  # Голубой фон
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(header)
        run.font.name = "Times New Roman"
        run.font.size = Pt(11)
        run.font.bold = True

    # Строки с данными
    for idx, row_data in enumerate(rows):
        row = table.add_row()
        bg = "FFFFFF" if idx % 2 == 0 else "F5F5F5"

        values = [
            row_data["num"],
            row_data["criterion"],
            row_data["requirement"],
            row_data["document"],
        ]
        aligns = [
            WD_ALIGN_PARAGRAPH.CENTER,
            WD_ALIGN_PARAGRAPH.LEFT,
            WD_ALIGN_PARAGRAPH.LEFT,
            WD_ALIGN_PARAGRAPH.LEFT,
        ]

        for i, (val, align) in enumerate(zip(values, aligns)):
            cell = row.cells[i]
            _set_cell_bg(cell, bg)
            p = cell.paragraphs[0]
            p.alignment = align
            run = p.add_run(val)
            run.font.name = "Times New Roman"
            run.font.size = Pt(11)

    doc.add_paragraph()  # Отступ после таблицы


async def generate_tz_docx(content: str, name: str) -> str:
    elements = parse_content(content)
    doc = _make_doc()
    _build_docx(doc, elements)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="TZ_")
    doc.save(tmp.name); tmp.close()
    return tmp.name


async def generate_criteria_docx(content: str, name: str) -> str:
    """Генерирует документ критериев допуска с таблицей."""
    doc = _make_doc()

    # Заголовок
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after  = Pt(12)
    _run(p, "КРИТЕРИИ ДОПУСКА УЧАСТНИКОВ К ЗАКУПКЕ", bold=True, size=14)

    # Вступительный текст если есть
    lines = content.strip().split("\n")
    intro_lines = []
    for line in lines:
        s = line.strip()
        if not s: continue
        if re.match(r'^КРИТЕРИИ ДОПУСКА', s, re.I): continue
        # Вступительный текст до первого критерия
        if re.match(r'^\d+[.)]\s', s) or re.match(r'^[-•*]\s', s):
            break
        intro_lines.append(_strip_md(s))

    for intro in intro_lines:
        if intro:
            p = doc.add_paragraph(intro)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.space_after = Pt(4)
            for r in p.runs:
                r.font.name = "Times New Roman"
                r.font.size = Pt(12)

    if intro_lines:
        doc.add_paragraph()

    # Таблица критериев
    rows = _parse_criteria_rows(content)
    if rows:
        _build_criteria_table(doc, rows)
    else:
        # Fallback — просто текст
        elements = parse_content(content)
        _build_docx(doc, elements)

    # Примечание внизу
    doc.add_paragraph()
    note_p = doc.add_paragraph()
    note_p.paragraph_format.space_before = Pt(6)
    _run(note_p,
         "Примечание: участник, не соответствующий хотя бы одному критерию, "
         "не допускается к участию в закупке.",
         italic=True, size=11)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="Crit_")
    doc.save(tmp.name); tmp.close()
    return tmp.name


# ─── PDF ─────────────────────────────────────────────────────────────────────

def _register_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.pdfmetrics import registerFontFamily

    candidates = [
        (FONT_REG, FONT_BOLD, FONT_ITAL),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf"),
    ]
    for reg, bold, ital in candidates:
        if os.path.exists(reg):
            try:
                pdfmetrics.registerFont(TTFont("CyrFont", reg))
                pdfmetrics.registerFont(TTFont("CyrFont-Bold", bold if os.path.exists(bold) else reg))
                pdfmetrics.registerFont(TTFont("CyrFont-Italic", ital if os.path.exists(ital) else reg))
                registerFontFamily("CyrFont", normal="CyrFont", bold="CyrFont-Bold", italic="CyrFont-Italic")
                return "CyrFont", "CyrFont-Bold", "CyrFont-Italic"
            except Exception as e:
                logger.warning(f"Font error {reg}: {e}")
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


def _build_pdf(elements: list[dict], output_path: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
    from reportlab.lib.colors import HexColor, white, black

    fn, fn_b, fn_i = _register_fonts()

    doc = SimpleDocTemplate(output_path, pagesize=A4,
        leftMargin=3*cm, rightMargin=1.5*cm, topMargin=2*cm, bottomMargin=2*cm)

    def safe(t): return (t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    title_s  = ParagraphStyle("TT", fontName=fn_b, fontSize=14, alignment=TA_CENTER, spaceAfter=10, spaceBefore=6, leading=18)
    h1_s     = ParagraphStyle("H1", fontName=fn_b, fontSize=13, alignment=TA_LEFT, spaceAfter=6, spaceBefore=10, leading=17)
    h2_s     = ParagraphStyle("H2", fontName=fn_b, fontSize=12, alignment=TA_LEFT, spaceAfter=4, spaceBefore=7, leading=16)
    h3_s     = ParagraphStyle("H3", fontName=fn_b, fontSize=12, alignment=TA_LEFT, spaceAfter=3, spaceBefore=5, leading=16)
    body_s   = ParagraphStyle("BD", fontName=fn, fontSize=12, alignment=TA_JUSTIFY, spaceAfter=4, firstLineIndent=1.25*cm, leading=16)
    bullet_s = ParagraphStyle("BL", fontName=fn, fontSize=12, alignment=TA_LEFT, spaceAfter=2, leftIndent=0.8*cm, leading=16)
    quote_s  = ParagraphStyle("QT", fontName=fn_i, fontSize=12, alignment=TA_LEFT, spaceAfter=4, leftIndent=1.5*cm, leading=16)
    cell_s   = ParagraphStyle("CL", fontName=fn, fontSize=10, alignment=TA_LEFT, leading=13)
    cell_hdr = ParagraphStyle("CH", fontName=fn_b, fontSize=10, alignment=TA_CENTER, leading=13)

    story = []
    for el in elements:
        t = el["type"]
        text = safe(el.get("text", ""))

        if t == "space":
            story.append(Spacer(1, 6))
        elif t == "divider":
            story.append(HRFlowable(width="100%", thickness=0.5, color=HexColor("#888888")))
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
        elif t == "body" and text.strip():
            story.append(Paragraph(text, body_s))

    doc.build(story)


def _build_criteria_pdf(content: str, output_path: str):
    """PDF с таблицей критериев."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
    from reportlab.lib.colors import HexColor

    fn, fn_b, fn_i = _register_fonts()

    doc = SimpleDocTemplate(output_path, pagesize=A4,
        leftMargin=3*cm, rightMargin=1.5*cm, topMargin=2*cm, bottomMargin=2*cm)

    title_s = ParagraphStyle("T", fontName=fn_b, fontSize=14, alignment=TA_CENTER, spaceAfter=12, leading=18)
    body_s  = ParagraphStyle("B", fontName=fn, fontSize=11, alignment=TA_JUSTIFY, spaceAfter=6, leading=15)
    note_s  = ParagraphStyle("N", fontName=fn_i, fontSize=10, alignment=TA_LEFT, leading=13)
    hdr_s   = ParagraphStyle("H", fontName=fn_b, fontSize=10, alignment=TA_CENTER, leading=13)
    cell_s  = ParagraphStyle("C", fontName=fn, fontSize=10, alignment=TA_LEFT, leading=13)

    def safe(t): return (t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    story = [Paragraph("КРИТЕРИИ ДОПУСКА УЧАСТНИКОВ К ЗАКУПКЕ", title_s)]

    rows_data = _parse_criteria_rows(content)

    if rows_data:
        # Шапка
        table_data = [[
            Paragraph("№", hdr_s),
            Paragraph("Критерий допуска", hdr_s),
            Paragraph("Требование", hdr_s),
            Paragraph("Документ-подтверждение", hdr_s),
        ]]
        for r in rows_data:
            table_data.append([
                Paragraph(safe(r["num"]), cell_s),
                Paragraph(safe(r["criterion"]), cell_s),
                Paragraph(safe(r["requirement"]), cell_s),
                Paragraph(safe(r["document"]), cell_s),
            ])

        col_w = [1.2*cm, 5.5*cm, 5.0*cm, 4.8*cm]
        t = Table(table_data, colWidths=col_w, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), HexColor("#D6E4F0")),
            ("FONTNAME",     (0,0), (-1,0), fn_b),
            ("FONTSIZE",     (0,0), (-1,-1), 10),
            ("GRID",         (0,0), (-1,-1), 0.5, HexColor("#AAAAAA")),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[HexColor("#FFFFFF"), HexColor("#F5F5F5")]),
        ]))
        story.append(t)
    else:
        # Fallback — текст
        for line in content.split("\n"):
            s = line.strip()
            if s:
                story.append(Paragraph(safe(s), body_s))

    story.append(Spacer(1, 12))
    story.append(Paragraph(
        "Примечание: участник, не соответствующий хотя бы одному критерию, не допускается к участию в закупке.",
        note_s
    ))

    doc.build(story)


async def generate_pdf(content: str, name: str) -> str:
    # Если это критерии — используем специальный PDF с таблицей
    if re.search(r'КРИТЕРИИ ДОПУСКА', content, re.I) or name.lower().startswith("crit"):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="Crit_")
        tmp.close()
        _build_criteria_pdf(content, tmp.name)
        return tmp.name

    elements = parse_content(content)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="Doc_")
    tmp.close()
    _build_pdf(elements, tmp.name)
    return tmp.name
