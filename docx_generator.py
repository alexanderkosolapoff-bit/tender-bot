"""
Генерация Word и PDF документов.
"""
import os, re, tempfile, logging
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

logger = logging.getLogger(__name__)

_BASE    = os.path.dirname(os.path.abspath(__file__))
FONT_REG = next((p for p in [
    os.path.join(_BASE, "fonts", "DejaVuSerif.ttf"),
    os.path.join(_BASE, "DejaVuSerif.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
] if os.path.exists(p)), None)

FONT_BOLD = next((p for p in [
    os.path.join(_BASE, "fonts", "DejaVuSerif-Bold.ttf"),
    os.path.join(_BASE, "DejaVuSerif-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
] if os.path.exists(p)), None)

FONT_ITAL = next((p for p in [
    os.path.join(_BASE, "fonts", "DejaVuSerif-Italic.ttf"),
    os.path.join(_BASE, "DejaVuSerif-Italic.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
] if os.path.exists(p)), None)


def _strip_md(t):
    t = re.sub(r'\*\*(.+?)\*\*', r'\1', t)
    t = re.sub(r'\*(.+?)\*',   r'\1', t)
    t = re.sub(r'`(.+?)`',     r'\1', t)
    t = re.sub(r'\[(.+?)\]\(.+?\)', r'\1', t)
    t = re.sub(r'^#+\s*', '', t)
    return t.strip()


# ─── WORD helpers ────────────────────────────────────────────────────────────

def _make_doc():
    doc = Document()
    for s in doc.sections:
        s.top_margin = Cm(2); s.bottom_margin = Cm(2)
        s.left_margin = Cm(3); s.right_margin = Cm(1.5)
    n = doc.styles["Normal"]
    n.font.name = "Times New Roman"; n.font.size = Pt(12)
    n.paragraph_format.space_before = Pt(0)
    n.paragraph_format.space_after  = Pt(0)
    n.paragraph_format.line_spacing = Pt(14)
    return doc

def _run(p, text, bold=False, italic=False, size=12):
    r = p.add_run(text)
    r.font.name = "Times New Roman"; r.font.size = Pt(size)
    r.font.bold = bold; r.font.italic = italic
    return r

def _cell_bg(cell, hex_color):
    tc = cell._tc; pr = tc.get_or_add_tcPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_color)
    pr.append(shd)


# ─── Парсер контента ─────────────────────────────────────────────────────────

def parse_content(text):
    els = []
    for line in text.strip().split("\n"):
        s = line.strip()
        if not s:                              els.append({"type":"space"}); continue
        if re.match(r'^[-*]{3,}$', s):        els.append({"type":"divider"}); continue
        if s.startswith("### "):              els.append({"type":"h3","text":_strip_md(s[4:])}); continue
        if s.startswith("## "):               els.append({"type":"h2","text":_strip_md(s[3:])}); continue
        if s.startswith("# "):                els.append({"type":"h1","text":_strip_md(s[2:])}); continue
        if s.startswith("> "):                els.append({"type":"quote","text":_strip_md(s[2:])}); continue
        if re.match(r'^[-*•]\s', s):          els.append({"type":"bullet","text":_strip_md(s[2:])}); continue
        m = re.match(r'^(\d+)\.\s+(.+)', s)
        if m and len(s)<200:                  els.append({"type":"numbered","num":m.group(1),"text":_strip_md(m.group(2))}); continue
        if s.startswith("**") and (":**" in s or s.endswith("**")):
                                              els.append({"type":"h3","text":s.strip("*").rstrip(":")}); continue
        if re.match(r'^(ТЕХНИЧЕСКОЕ ЗАДАНИЕ|КРИТЕРИИ ДОПУСКА|СЦЕНАРИЙ|ОТВЕТН)', s, re.I):
                                              els.append({"type":"title","text":_strip_md(s)}); continue
        els.append({"type":"body","text":_strip_md(s)})
    return els


def _build_docx(doc, elements):
    for el in elements:
        t = el["type"]
        if t == "space":    doc.add_paragraph()
        elif t=="divider":
            p = doc.add_paragraph(); _run(p, "─"*50)
        elif t=="title":
            p=doc.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before=Pt(6); p.paragraph_format.space_after=Pt(6)
            _run(p,el["text"],bold=True,size=14)
        elif t=="h1":
            p=doc.add_paragraph(); p.paragraph_format.space_before=Pt(8); p.paragraph_format.space_after=Pt(4)
            _run(p,el["text"],bold=True,size=13)
        elif t in("h2","numbered"):
            p=doc.add_paragraph(); p.paragraph_format.space_before=Pt(6); p.paragraph_format.space_after=Pt(3)
            txt=el["text"] if t=="h2" else f"{el['num']}. {el['text']}"
            _run(p,txt,bold=True,size=12)
        elif t=="h3":
            p=doc.add_paragraph(); p.paragraph_format.space_before=Pt(4); p.paragraph_format.space_after=Pt(2)
            _run(p,el["text"],bold=True,size=12)
        elif t=="bullet":
            p=doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_before=Pt(0); p.paragraph_format.space_after=Pt(1)
            _run(p,el["text"])
        elif t=="quote":
            p=doc.add_paragraph(); p.paragraph_format.left_indent=Cm(1.5)
            _run(p,f'"{el["text"]}"',italic=True)
        elif t=="body" and el.get("text"):
            p=doc.add_paragraph(el["text"]); p.alignment=WD_ALIGN_PARAGRAPH.JUSTIFY
            p.paragraph_format.first_line_indent=Cm(1.25)
            for r in p.runs: r.font.name="Times New Roman"; r.font.size=Pt(12)


# ─── Таблица критериев ───────────────────────────────────────────────────────

def _parse_criteria_for_table(text):
    """
    Извлекает критерии из текста Claude.
    Возвращает список строк для таблицы.
    Каждая строка = один критерий.
    """
    rows = []
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    for line in lines:
        # Пропускаем заголовки и вступление
        if re.match(r'^(КРИТЕРИИ ДОПУСКА|Критерии допуска|Примечание)', line, re.I):
            continue
        if len(line) < 5:
            continue

        # Нумерованный пункт: "1. Текст"
        m = re.match(r'^(\d+)[.)]\s*(.+)', line)
        if m:
            num = m.group(1)
            criterion_text = _strip_md(m.group(2))

            # Пытаемся разбить на критерий / требование
            # Паттерн: "Критерий — требование" или просто весь текст как требование
            parts = re.split(r'\s*[–—:]\s*', criterion_text, maxsplit=1)
            if len(parts) == 2 and len(parts[0]) < 60:
                criterion = parts[0].strip()
                requirement = parts[1].strip()
            else:
                criterion = criterion_text
                requirement = criterion_text

            rows.append({
                "num": num,
                "criterion": criterion,
                "requirement": requirement,
                "document": _guess_document(criterion_text),
            })
            continue

        # Маркированный пункт
        m2 = re.match(r'^[-•*]\s*(.+)', line)
        if m2:
            text_part = _strip_md(m2.group(1))
            if len(text_part) > 5:
                rows.append({
                    "num": str(len(rows)+1),
                    "criterion": text_part,
                    "requirement": text_part,
                    "document": _guess_document(text_part),
                })

    return rows


def _guess_document(text):
    """Угадывает тип подтверждающего документа по тексту критерия."""
    t = text.lower()
    if any(w in t for w in ["лицензи", "допуск", "сро"]):
        return "Копия лицензии / допуска СРО"
    if any(w in t for w in ["опыт", "стаж", "договор"]):
        return "Копии договоров за последние 3 года"
    if any(w in t for w in ["сотрудник", "штат", "персонал"]):
        return "Справка о среднесписочной численности"
    if any(w in t for w in ["налог", "задолженност"]):
        return "Справка из ФНС об отсутствии задолженности"
    if any(w in t for w in ["сертификат"]):
        return "Копия сертификата"
    if any(w in t for w in ["оборудован", "техник"]):
        return "Перечень оборудования / документы о собственности"
    if any(w in t for w in ["финанс", "оборот"]):
        return "Бухгалтерская отчётность"
    return "Документы по запросу организатора"


def _build_criteria_table_docx(doc, rows):
    """Строит таблицу критериев в Word."""
    headers = ["№", "Критерий допуска", "Требование к участнику", "Документ-подтверждение"]
    col_widths = [Cm(1.0), Cm(5.0), Cm(5.2), Cm(5.5)]

    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"

    # Шапка
    hdr = table.rows[0]
    for i, (h, w) in enumerate(zip(headers, col_widths)):
        cell = hdr.cells[i]
        cell.width = w
        _cell_bg(cell, "1F5C99")  # Тёмно-синий
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = p.add_run(h)
        r.font.name = "Times New Roman"; r.font.size = Pt(10)
        r.font.bold = True; r.font.color.rgb = None
        # Белый текст
        from docx.shared import RGBColor
        r.font.color.rgb = RGBColor(255, 255, 255)

    # Строки
    for idx, row in enumerate(rows):
        tr = table.add_row()
        bg = "FFFFFF" if idx % 2 == 0 else "EEF4FB"
        vals = [row["num"], row["criterion"], row["requirement"], row["document"]]
        aligns = [WD_ALIGN_PARAGRAPH.CENTER, WD_ALIGN_PARAGRAPH.LEFT,
                  WD_ALIGN_PARAGRAPH.LEFT, WD_ALIGN_PARAGRAPH.LEFT]
        for i, (val, align) in enumerate(zip(vals, aligns)):
            cell = tr.cells[i]
            _cell_bg(cell, bg)
            p = cell.paragraphs[0]; p.alignment = align
            r = p.add_run(val)
            r.font.name = "Times New Roman"; r.font.size = Pt(10)

    doc.add_paragraph()


# ─── Публичные функции ───────────────────────────────────────────────────────

async def generate_tz_docx(content, name):
    doc = _make_doc()
    _build_docx(doc, parse_content(content))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="TZ_")
    doc.save(tmp.name); tmp.close()
    return tmp.name


async def generate_criteria_docx(content, name):
    doc = _make_doc()

    # Заголовок
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after  = Pt(10)
    _run(p, "КРИТЕРИИ ДОПУСКА УЧАСТНИКОВ К ЗАКУПКЕ", bold=True, size=14)

    # Таблица
    rows = _parse_criteria_for_table(content)
    if rows:
        _build_criteria_table_docx(doc, rows)
    else:
        _build_docx(doc, parse_content(content))

    # Примечание
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(6)
    _run(p, "Примечание: участник, не соответствующий хотя бы одному критерию, "
            "не допускается к участию в закупке.", italic=True, size=11)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".docx", prefix="Crit_")
    doc.save(tmp.name); tmp.close()
    return tmp.name


# ─── PDF ─────────────────────────────────────────────────────────────────────

def _reg_fonts():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.pdfmetrics import registerFontFamily
    if FONT_REG:
        try:
            pdfmetrics.registerFont(TTFont("CyrFont", FONT_REG))
            pdfmetrics.registerFont(TTFont("CyrFont-Bold",   FONT_BOLD or FONT_REG))
            pdfmetrics.registerFont(TTFont("CyrFont-Italic", FONT_ITAL or FONT_REG))
            registerFontFamily("CyrFont", normal="CyrFont", bold="CyrFont-Bold", italic="CyrFont-Italic")
            return "CyrFont", "CyrFont-Bold", "CyrFont-Italic"
        except Exception as e:
            logger.warning(f"Font error: {e}")
    return "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"


async def generate_pdf(content, name):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
    from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_LEFT
    from reportlab.lib.colors import HexColor

    fn, fn_b, fn_i = _reg_fonts()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", prefix="Doc_")
    tmp.close()

    doc = SimpleDocTemplate(tmp.name, pagesize=A4,
        leftMargin=3*cm, rightMargin=1.5*cm, topMargin=2*cm, bottomMargin=2*cm)

    def safe(t): return (t or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

    title_s  = ParagraphStyle("TT", fontName=fn_b, fontSize=14, alignment=TA_CENTER, spaceAfter=10, leading=18)
    h1_s     = ParagraphStyle("H1", fontName=fn_b, fontSize=13, alignment=TA_LEFT, spaceAfter=6, spaceBefore=10, leading=17)
    h2_s     = ParagraphStyle("H2", fontName=fn_b, fontSize=12, alignment=TA_LEFT, spaceAfter=4, spaceBefore=7, leading=16)
    h3_s     = ParagraphStyle("H3", fontName=fn_b, fontSize=12, alignment=TA_LEFT, spaceAfter=3, spaceBefore=5, leading=16)
    body_s   = ParagraphStyle("BD", fontName=fn,   fontSize=12, alignment=TA_JUSTIFY, spaceAfter=4, firstLineIndent=1.25*cm, leading=16)
    bullet_s = ParagraphStyle("BL", fontName=fn,   fontSize=12, alignment=TA_LEFT, spaceAfter=2, leftIndent=0.8*cm, leading=16)
    quote_s  = ParagraphStyle("QT", fontName=fn_i, fontSize=12, alignment=TA_LEFT, spaceAfter=4, leftIndent=1.5*cm, leading=16)
    cell_s   = ParagraphStyle("CL", fontName=fn,   fontSize=10, alignment=TA_LEFT, leading=13)
    hdr_s    = ParagraphStyle("CH", fontName=fn_b, fontSize=10, alignment=TA_CENTER, leading=13)
    note_s   = ParagraphStyle("NT", fontName=fn_i, fontSize=10, alignment=TA_LEFT, leading=13)

    is_criteria = bool(re.search(r'КРИТЕРИИ ДОПУСКА', content, re.I))
    story = []

    if is_criteria:
        story.append(Paragraph("КРИТЕРИИ ДОПУСКА УЧАСТНИКОВ К ЗАКУПКЕ", title_s))
        rows = _parse_criteria_for_table(content)
        if rows:
            td = [[Paragraph(safe(h), hdr_s) for h in ["№","Критерий","Требование","Документ-подтверждение"]]]
            for r in rows:
                td.append([Paragraph(safe(r[k]), cell_s) for k in ["num","criterion","requirement","document"]])
            t = Table(td, colWidths=[1.0*cm, 4.8*cm, 5.0*cm, 5.0*cm], repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0,0),(-1,0), HexColor("#1F5C99")),
                ("TEXTCOLOR",     (0,0),(-1,0), HexColor("#FFFFFF")),
                ("FONTNAME",      (0,0),(-1,0), fn_b),
                ("FONTSIZE",      (0,0),(-1,-1), 10),
                ("GRID",          (0,0),(-1,-1), 0.5, HexColor("#AAAAAA")),
                ("VALIGN",        (0,0),(-1,-1), "TOP"),
                ("TOPPADDING",    (0,0),(-1,-1), 4),
                ("BOTTOMPADDING", (0,0),(-1,-1), 4),
                ("ROWBACKGROUNDS",(0,1),(-1,-1), [HexColor("#FFFFFF"), HexColor("#EEF4FB")]),
            ]))
            story.append(t)
        story.append(Spacer(1,12))
        story.append(Paragraph(
            "Примечание: участник, не соответствующий хотя бы одному критерию, не допускается к участию в закупке.",
            note_s))
    else:
        for el in parse_content(content):
            t = el["type"]
            text = safe(el.get("text",""))
            if t=="space":    story.append(Spacer(1,6))
            elif t=="divider": story.append(HRFlowable(width="100%",thickness=0.5,color=HexColor("#888888")))
            elif t=="title":  story.append(Paragraph(text, title_s))
            elif t=="h1":     story.append(Paragraph(text, h1_s))
            elif t in("h2","numbered"):
                label = text if t=="h2" else f"{el['num']}. {text}"
                story.append(Paragraph(label, h2_s))
            elif t=="h3":     story.append(Paragraph(text, h3_s))
            elif t=="bullet": story.append(Paragraph(f"• {text}", bullet_s))
            elif t=="quote":  story.append(Paragraph(f"«{text}»", quote_s))
            elif t=="body" and text.strip(): story.append(Paragraph(text, body_s))

    doc.build(story)
    return tmp.name
