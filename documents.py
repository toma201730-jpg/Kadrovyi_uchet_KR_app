"""Движок документов: шаблоны с версиями, подстановка полей, выгрузка DOCX и PDF."""
import io, os, re, datetime
from xml.sax.saxutils import escape
from db import get_db

PLACEHOLDER = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

# Поля, которые можно использовать в шаблоне, по типам документов.
FIELDS = {
    "order_hire": {
        "org_name": "Название организации", "head_position": "Должность руководителя", "head_name": "ФИО руководителя",
        "order_number": "Номер приказа", "order_date": "Дата приказа", "employee_name": "ФИО сотрудника",
        "department": "Подразделение", "position": "Должность", "hire_date": "Дата приёма", "salary": "Оклад",
        "probation_text": "Испытательный срок (текстом)", "basis_line": "Строка «Основание: …» (пусто, если не указано)",
    },
}
FIELDS["contract"] = {
    "org_name": "Название организации", "head_position": "Должность руководителя", "head_name": "ФИО руководителя",
    "contract_number": "Номер договора", "contract_date": "Дата договора", "employee_name": "ФИО сотрудника",
    "employee_birth_date": "Дата рождения сотрудника", "employee_passport": "Паспорт сотрудника (номер, кем и когда выдан)",
    "employee_address": "Адрес прописки сотрудника", "employee_pin": "ПИН сотрудника",
    "department": "Подразделение", "position": "Должность", "start_date": "Дата начала работы",
    "term_text": "Срок договора (текстом)", "probation_text": "Испытательный срок (текстом)",
    "salary": "Оклад", "work_mode": "Режим рабочего времени",
}
FIELDS["order_leave"] = {
    "org_name": "Название организации", "head_position": "Должность руководителя", "head_name": "ФИО руководителя",
    "order_number": "Номер приказа", "order_date": "Дата приказа", "employee_name": "ФИО сотрудника",
    "department": "Подразделение", "position": "Должность", "leave_type_text": "Вид отпуска (текстом)",
    "start_date": "Дата начала отпуска", "end_date": "Дата окончания отпуска", "days": "Количество календарных дней",
    "basis_line": "Строка «Основание: …» (пусто, если не указано)",
}
FIELDS["order_transfer"] = {
    "org_name": "Название организации", "head_position": "Должность руководителя", "head_name": "ФИО руководителя",
    "order_number": "Номер приказа", "order_date": "Дата приказа", "employee_name": "ФИО сотрудника",
    "old_department": "Прежнее подразделение", "old_position": "Прежняя должность", "department": "Новое подразделение",
    "position": "Новая должность", "transfer_date": "Дата перевода", "salary": "Новый оклад",
    "basis_line": "Строка «Основание: …» (пусто, если не указано)",
}
FIELDS["order_termination"] = {
    "org_name": "Название организации", "head_position": "Должность руководителя", "head_name": "ФИО руководителя",
    "order_number": "Номер приказа", "order_date": "Дата приказа", "employee_name": "ФИО сотрудника",
    "department": "Подразделение", "position": "Должность", "dismissal_date": "Дата прекращения договора",
    "ground_text": "Основание прекращения (из справочника оснований)",
    "leave_compensation_line": "Строка о неиспользованном отпуске (пусто, если остатка нет)",
}
REQUIRED = {
    "order_transfer": {"order_number", "order_date", "employee_name", "transfer_date", "department", "position"},
    "order_termination": {"order_number", "order_date", "employee_name", "dismissal_date", "ground_text"},
    "order_leave": {"order_number", "order_date", "employee_name", "leave_type_text", "start_date", "end_date", "days"},
    "order_hire": {"order_number", "order_date", "employee_name", "department", "position", "hire_date"},
    "contract": {"contract_number", "contract_date", "employee_name", "department", "position", "start_date",
                 "term_text", "probation_text", "salary"},
}
OPTIONAL = {"order_hire": {"basis_line"}, "order_leave": {"basis_line"}, "order_transfer": {"basis_line"},
            "order_termination": {"leave_compensation_line"}, "contract": set()}  # поля, которые могут быть пустыми
# Виды шаблонов в разделе «Шаблоны»: ключ -> (название, тип документа)
TEMPLATE_KINDS = {"hire": ("Приказ о приёме на работу", "order_hire"), "contract": ("Трудовой договор", "contract"),
                  "leave": ("Приказ о предоставлении отпуска", "order_leave"),
                  "transfer": ("Приказ о переводе", "order_transfer"),
                  "termination": ("Приказ о прекращении трудового договора", "order_termination")}

# ОБРАЗЕЦ. Замените текст на утверждённую юристом форму приказа в разделе «Шаблоны».
DEFAULT_BODIES = {"order_hire": """# {{org_name}}
# ПРИКАЗ № {{order_number}}
# от {{order_date}}

О приёме на работу

Принять {{employee_name}} на работу {{hire_date}} в подразделение «{{department}}» на должность «{{position}}» с окладом {{salary}} сом в месяц.

Испытательный срок: {{probation_text}}.

{{basis_line}}

{{head_position}} ________________ {{head_name}}

С приказом ознакомлен(а): ________________ {{employee_name}}   «___» ____________ 20__ г.
"""}

DEFAULT_BODIES["order_leave"] = """# {{org_name}}
# ПРИКАЗ № {{order_number}}
# от {{order_date}}

О предоставлении отпуска

Предоставить {{employee_name}}, {{position}} ({{department}}), {{leave_type_text}} с {{start_date}} по {{end_date}} ({{days}} календарных дн.).

{{basis_line}}

{{head_position}} ________________ {{head_name}}

С приказом ознакомлен(а): ________________ {{employee_name}}   «___» ____________ 20__ г.
"""

DEFAULT_BODIES["order_transfer"] = """# {{org_name}}
# ПРИКАЗ № {{order_number}}
# от {{order_date}}

О переводе на другую работу

Перевести {{employee_name}}, {{old_position}} ({{old_department}}), с {{transfer_date}} в подразделение «{{department}}» на должность «{{position}}» с окладом {{salary}} сом в месяц.

{{basis_line}}

{{head_position}} ________________ {{head_name}}

С приказом ознакомлен(а): ________________ {{employee_name}}   «___» ____________ 20__ г.
"""

DEFAULT_BODIES["order_termination"] = """# {{org_name}}
# ПРИКАЗ № {{order_number}}
# от {{order_date}}

О прекращении трудового договора

Прекратить трудовой договор с {{employee_name}}, {{position}} ({{department}}), {{dismissal_date}}.

Основание: {{ground_text}}.

{{leave_compensation_line}}

{{head_position}} ________________ {{head_name}}

С приказом ознакомлен(а): ________________ {{employee_name}}   «___» ____________ 20__ г.
"""

DEFAULT_GROUNDS = [
    ("Соглашение сторон", "agreement"), ("Истечение срока трудового договора", "term_expired"),
    ("Расторжение по инициативе работника", "employee"), ("Расторжение по инициативе работодателя", "employer"),
    ("Иное основание", "other"),
]

DEFAULT_BODIES["contract"] = """# ТРУДОВОЙ ДОГОВОР № {{contract_number}}
# от {{contract_date}}

{{org_name}}, в лице {{head_position}} {{head_name}}, именуемое далее «Работодатель», и {{employee_name}}, {{employee_birth_date}} года рождения, паспорт {{employee_passport}}, проживающий(ая) по адресу: {{employee_address}}, именуемый(ая) далее «Работник», заключили настоящий договор о нижеследующем.

1. Работник принимается на работу в подразделение «{{department}}» на должность «{{position}}».

2. Дата начала работы: {{start_date}}. Договор заключён {{term_text}}.

3. Испытательный срок: {{probation_text}}.

4. Оплата труда: оклад {{salary}} сом в месяц.

5. Режим рабочего времени: {{work_mode}}.

6. Иные условия, права и обязанности сторон определяются законодательством Кыргызской Республики.

Работодатель: {{head_position}} ________________ {{head_name}}

Работник: ________________ {{employee_name}}
"""

def fmt_date(iso):
    return datetime.date.fromisoformat(iso).strftime("%d.%m.%Y")

def fmt_money(x):
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")

def validate_body(doc_type, body):
    errors, allowed = [], set(FIELDS[doc_type])
    if not body.strip() or len(body) > 20000:
        return ["Текст шаблона обязателен (до 20 000 символов)."]
    used = set(PLACEHOLDER.findall(body))
    unknown = sorted(used - allowed)
    if unknown:
        errors.append("Неизвестные поля: " + ", ".join("{{" + u + "}}" for u in unknown))
    missing = sorted(REQUIRED[doc_type] - used)
    if missing:
        errors.append("В шаблоне обязательно должны быть поля: " + ", ".join("{{" + m + "}}" for m in missing))
    return errors

def ensure_defaults():
    conn = get_db()
    for doc_type, body in DEFAULT_BODIES.items():
        if not conn.execute("SELECT 1 FROM document_templates WHERE doc_type=?", (doc_type,)).fetchone():
            conn.execute("INSERT INTO document_templates (doc_type, version, body) VALUES (?,1,?)", (doc_type, body))
    if not conn.execute("SELECT 1 FROM termination_grounds").fetchone():
        conn.executemany("INSERT INTO termination_grounds (name, kind) VALUES (?,?)", DEFAULT_GROUNDS)
    conn.commit()

def active_template(doc_type):
    return get_db().execute("SELECT * FROM document_templates WHERE doc_type=? ORDER BY version DESC LIMIT 1",
                            (doc_type,)).fetchone()

def save_template(doc_type, body, user_id, commit=True):
    """Каждое сохранение создаёт новую версию; старые версии и уже проведённые документы не меняются."""
    version = active_template(doc_type)["version"] + 1
    get_db().execute("INSERT INTO document_templates (doc_type, version, body, created_by) VALUES (?,?,?,?)",
                     (doc_type, version, body.replace("\r\n", "\n"), user_id))
    if commit:
        get_db().commit()
    return version

def render(body, ctx):
    """Подставляет поля. Если для какого-то поля нет значения, выбрасывает ValueError."""
    missing = sorted({k for k in PLACEHOLDER.findall(body) if k not in ctx})
    if missing:
        raise ValueError("Нет данных для полей: " + ", ".join(missing))
    text = PLACEHOLDER.sub(lambda m: str(ctx[m.group(1)]), body)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"

def render_doc(doc_type, body, ctx):
    """Подстановка с понятным сообщением: какие данные не заполнены (по названиям полей)."""
    labels, optional = FIELDS[doc_type], OPTIONAL.get(doc_type, set())
    missing = [labels[k] for k in dict.fromkeys(PLACEHOLDER.findall(body))
               if k not in ctx or (ctx[k] in ("", None) and k not in optional)]
    if missing:
        raise ValueError("Не заполнено: " + ", ".join(missing) + ".")
    return render(body, {k: ("" if v is None else v) for k, v in ctx.items()})

def org_errors():
    org = get_db().execute("SELECT * FROM organization WHERE id=1").fetchone()
    names = {"full_name": "название организации", "head_name": "ФИО руководителя", "head_position": "должность руководителя"}
    missing = [v for k, v in names.items() if not org[k]]
    return ["Заполните в настройках: " + ", ".join(missing) + "."] if missing else []

def to_docx(text):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt, Cm
    doc = Document()
    for s in doc.sections:
        s.left_margin, s.right_margin, s.top_margin, s.bottom_margin = Cm(2.5), Cm(1.5), Cm(2), Cm(2)
    doc.styles["Normal"].font.name = "Times New Roman"
    doc.styles["Normal"].font.size = Pt(12)
    for line in text.split("\n"):
        p = doc.add_paragraph()
        if line.startswith("# "):
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.add_run(line[2:]).bold = True
        else:
            p.add_run(line)
        p.paragraph_format.space_after = Pt(4)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()

def to_pdf(text):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import cm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    for name, file in (("DejaVu", "DejaVuSans.ttf"), ("DejaVu-Bold", "DejaVuSans-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, os.path.join(FONT_DIR, file)))
    body = ParagraphStyle("b", fontName="DejaVu", fontSize=11, leading=16, spaceAfter=4)
    head = ParagraphStyle("h", parent=body, fontName="DejaVu-Bold", alignment=1)
    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=2.5 * cm, rightMargin=1.5 * cm, topMargin=2 * cm, bottomMargin=2 * cm)
    story = []
    for line in text.split("\n"):
        if not line.strip():
            story.append(Spacer(1, 8))
        elif line.startswith("# "):
            story.append(Paragraph(escape(line[2:]), head))
        else:
            story.append(Paragraph(escape(line), body))
    pdf.build(story)
    return buf.getvalue()
