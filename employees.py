import re, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
import db, audit
from auth import roles_required, current_user

bp = Blueprint("employees", __name__, url_prefix="/employees")

STATUSES = {"new": "Оформляется", "working": "Работает", "on_leave": "В отпуске", "archived": "В архиве"}
GENDERS = ["Мужской", "Женский"]
FAMILY = ["Холост / не замужем", "Женат / замужем", "Разведён / разведена", "Вдовец / вдова"]
EDUCATION = ["Среднее", "Среднее специальное", "Неполное высшее", "Высшее", "Послевузовское"]

# Видимые руководителю поля. Всё остальное (паспорт, ПИН, адрес и т.д.) руководитель
# не получает уже на уровне SQL-запроса, а не только на экране.
PUBLIC_COLS = "e.id, e.full_name, e.status, e.hire_date, e.phone, e.email"

TEXT_FIELDS = {  # поле: (подпись, максимум символов)
    "citizenship": ("Гражданство", 100), "passport_number": ("Номер паспорта", 50),
    "passport_issued_by": ("Кем выдан паспорт", 200), "address_registration": ("Адрес прописки", 300),
    "address_residence": ("Адрес проживания", 300), "social_number": ("Номер социального страхования", 50),
}
FORM_FIELDS = ["full_name", "birth_date", "gender", "pin", "passport_issued_date", "phone", "email",
               "family_status", "education", "hire_date", "dismissal_date", *TEXT_FIELDS]

def _full():
    return current_user()["role"] in ("admin", "hr")

def _date(raw, label, errors):
    if not raw:
        return None
    try:
        d = datetime.date.fromisoformat(raw)
    except ValueError:
        errors.append(f"{label}: неверная дата.")
        return None
    if d.year < 1900 or d.year > 2100:
        errors.append(f"{label}: неверная дата.")
        return None
    return d

def parse_form(form, exclude_id=None):
    errors, data = [], {}
    clean = lambda k: " ".join(form.get(k, "").split())
    data["full_name"] = clean("full_name")
    if not data["full_name"] or len(data["full_name"]) > 200:
        errors.append("ФИО обязательно (до 200 символов).")
    birth = _date(form.get("birth_date", ""), "Дата рождения", errors)
    if birth and birth > datetime.date.today():
        errors.append("Дата рождения не может быть в будущем.")
    data["birth_date"] = birth.isoformat() if birth else None
    issued = _date(form.get("passport_issued_date", ""), "Дата выдачи паспорта", errors)
    data["passport_issued_date"] = issued.isoformat() if issued else None
    hire = _date(form.get("hire_date", ""), "Дата приёма", errors)
    dism = _date(form.get("dismissal_date", ""), "Дата увольнения", errors)
    if hire and dism and dism < hire:
        errors.append("Дата увольнения раньше даты приёма.")
    data["hire_date"] = hire.isoformat() if hire else None
    data["dismissal_date"] = dism.isoformat() if dism else None
    for key, options, label in (("gender", GENDERS, "Пол"), ("family_status", FAMILY, "Семейное положение"),
                                ("education", EDUCATION, "Образование")):
        v = form.get(key, "")
        if v and v not in options:
            errors.append(f"{label}: выберите значение из списка.")
            v = ""
        data[key] = v or None
    pin = clean("pin")
    if pin and not re.fullmatch(r"\d{14}", pin):
        errors.append("ПИН: 14 цифр.")
    data["pin"] = pin or None
    phone = clean("phone")
    if phone and not re.fullmatch(r"[0-9+()\- ]{5,30}", phone):
        errors.append("Телефон: цифры, пробелы, + ( ) -")
    data["phone"] = phone or None
    email = clean("email")
    if email and (len(email) > 120 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)):
        errors.append("Email: неверный формат.")
    data["email"] = email or None
    for key, (label, limit) in TEXT_FIELDS.items():
        v = clean(key)
        if len(v) > limit:
            errors.append(f"{label}: максимум {limit} символов.")
        data[key] = v or None
    conn = db.get_db()
    if data["pin"] and conn.execute("SELECT 1 FROM employees WHERE pin=? AND id IS NOT ?", (data["pin"], exclude_id)).fetchone():
        errors.append("Сотрудник с таким ПИН уже есть.")
    if data["birth_date"] and data["full_name"] and conn.execute(
            "SELECT 1 FROM employees WHERE birth_date=? AND full_name=? COLLATE NOCASE AND id IS NOT ?",
            (data["birth_date"], data["full_name"], exclude_id)).fetchone():
        errors.append("Сотрудник с таким ФИО и датой рождения уже есть.")
    return data, errors

def _get_employee(emp_id):
    cols = "e.*" if _full() else PUBLIC_COLS
    row = db.get_db().execute(f"SELECT {cols} FROM employees e WHERE e.id=?", (emp_id,)).fetchone()
    if not row:
        abort(404)
    return row

LIST_SQL = f"""
SELECT {PUBLIC_COLS}, d.id AS dept_id, d.name AS dept, p.name AS pos
FROM employees e
LEFT JOIN employee_history h ON h.id = (SELECT id FROM employee_history
    WHERE employee_id = e.id AND start_date <= date('now') ORDER BY start_date DESC, id DESC LIMIT 1)
LEFT JOIN departments d ON d.id = h.department_id
LEFT JOIN positions p ON p.id = h.position_id
"""

@bp.route("")
@roles_required("admin", "hr", "manager")
def list_page():
    import leaves
    leaves.refresh_statuses()
    q = " ".join(request.args.get("q", "").split()).casefold()
    status = request.args.get("status", "")
    dept = request.args.get("dept", "")
    sql, args, where = LIST_SQL, [], []
    if status in STATUSES:
        where.append("e.status = ?"); args.append(status)
    if dept.isdigit():
        where.append("d.id = ?"); args.append(int(dept))
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.get_db().execute(sql + " ORDER BY e.full_name", args).fetchall()
    if q:  # поиск только по ФИО, без учёта регистра (в том числе для кириллицы)
        rows = [r for r in rows if all(t in r["full_name"].casefold() for t in q.split())]
    depts = db.get_db().execute("SELECT * FROM departments ORDER BY name").fetchall()
    return render_template("employees_list.html", rows=rows, depts=depts, statuses=STATUSES,
                           q=request.args.get("q", ""), status=status, dept=dept, full=_full())

def _form_context(values=None, emp=None):
    return dict(v=values or {}, emp=emp, genders=GENDERS, family=FAMILY, education=EDUCATION)

@bp.route("/new", methods=["GET", "POST"])
@roles_required("admin", "hr")
def create():
    if request.method == "POST":
        data, errors = parse_form(request.form)
        if errors:
            for e in errors:
                flash(e, "err")
            return render_template("employee_form.html", **_form_context(request.form)), 400
        cols = list(data)
        cur = db.get_db().execute(
            f"INSERT INTO employees ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [data[c] for c in cols])
        db.get_db().commit()
        audit.log("create", "employee", cur.lastrowid, data["full_name"])
        flash("Сотрудник создан.", "ok")
        return redirect(url_for("employees.card", emp_id=cur.lastrowid))
    return render_template("employee_form.html", **_form_context())

@bp.route("/<int:emp_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "hr")
def edit(emp_id):
    emp = _get_employee(emp_id)
    if request.method == "POST":
        data, errors = parse_form(request.form, exclude_id=emp_id)
        if errors:
            for e in errors:
                flash(e, "err")
            return render_template("employee_form.html", **_form_context(request.form, emp)), 400
        changed = [k for k in data if (emp[k] or None) != data[k]]
        if changed:
            sets = ", ".join(f"{k}=?" for k in data)
            db.get_db().execute(f"UPDATE employees SET {sets}, updated_at=datetime('now') WHERE id=?",
                                [*data.values(), emp_id])
            db.get_db().commit()
            audit.log("update", "employee", emp_id, "изменены поля: " + ", ".join(changed))  # значения не пишем
        flash("Сохранено.", "ok")
        return redirect(url_for("employees.card", emp_id=emp_id))
    return render_template("employee_form.html", **_form_context({k: emp[k] or "" for k in FORM_FIELDS}, emp))

@bp.route("/<int:emp_id>")
@roles_required("admin", "hr", "manager")
def card(emp_id):
    import leaves
    leaves.refresh_statuses()
    emp = _get_employee(emp_id)
    leave_balance = leaves.balance(emp, datetime.date.today()) if _full() and emp["hire_date"] else None
    cols = "h.id, h.start_date, d.name AS dept, p.name AS pos"
    if _full():
        cols += ", h.salary, h.working_conditions, h.basis"  # оклад и условия труда руководителю не отдаются
    history = db.get_db().execute(
        f"SELECT {cols} FROM employee_history h LEFT JOIN departments d ON d.id=h.department_id "
        "LEFT JOIN positions p ON p.id=h.position_id WHERE h.employee_id=? ORDER BY h.start_date DESC, h.id DESC",
        (emp_id,)).fetchall()
    today = datetime.date.today().isoformat()
    current = next((h for h in history if h["start_date"] <= today), None)
    depts = db.get_db().execute("SELECT * FROM departments WHERE is_active=1 ORDER BY name").fetchall()
    poss = db.get_db().execute("SELECT * FROM positions WHERE is_active=1 ORDER BY name").fetchall()
    orders = []
    if _full():
        orders = db.get_db().execute("SELECT id, number, order_date, status, doc_type FROM orders WHERE employee_id=? ORDER BY order_date DESC, id DESC",
                                     (emp_id,)).fetchall()
    contracts = []
    if _full():
        contracts = db.get_db().execute("SELECT id, number, contract_date, contract_type, status FROM contracts WHERE employee_id=? ORDER BY contract_date DESC, id DESC",
                                        (emp_id,)).fetchall()
    return render_template("employee_card.html", emp=emp, history=history, current=current, full=_full(),
                           statuses=STATUSES, depts=depts, poss=poss, orders=orders, contracts=contracts, leave_balance=leave_balance)

@bp.route("/<int:emp_id>/history", methods=["POST"])
@roles_required("admin", "hr")
def add_history(emp_id):
    _get_employee(emp_id)
    errors = []
    start = _date(request.form.get("start_date", ""), "Дата начала", errors)
    if not start and not errors:
        errors.append("Укажите дату начала.")
    conn = db.get_db()
    dept = request.form.get("department_id", "")
    pos = request.form.get("position_id", "")
    if not (dept.isdigit() and conn.execute("SELECT 1 FROM departments WHERE id=? AND is_active=1", (dept,)).fetchone()):
        errors.append("Выберите подразделение из справочника.")
    if not (pos.isdigit() and conn.execute("SELECT 1 FROM positions WHERE id=? AND is_active=1", (pos,)).fetchone()):
        errors.append("Выберите должность из справочника.")
    salary = None
    raw = request.form.get("salary", "").replace(",", ".").strip()
    if raw:
        try:
            salary = round(float(raw), 2)
            if salary < 0 or salary > 100_000_000:
                raise ValueError
        except ValueError:
            errors.append("Оклад: неотрицательное число.")
    cond = " ".join(request.form.get("working_conditions", "").split())
    basis = " ".join(request.form.get("basis", "").split())
    if len(cond) > 300 or len(basis) > 300:
        errors.append("Условия труда и основание: максимум 300 символов.")
    if errors:
        for e in errors:
            flash(e, "err")
    else:
        cur = conn.execute(
            "INSERT INTO employee_history (employee_id, start_date, department_id, position_id, salary, working_conditions, basis, created_by) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (emp_id, start.isoformat(), int(dept), int(pos), salary, cond or None, basis or None, current_user()["id"]))
        conn.commit()
        audit.log("history_add", "employee", emp_id, f"запись истории #{cur.lastrowid}, с {start.isoformat()}")
        flash("Запись добавлена в историю.", "ok")
    return redirect(url_for("employees.card", emp_id=emp_id))

@bp.route("/<int:emp_id>/archive", methods=["POST"])
@roles_required("admin", "hr")
def archive(emp_id):
    emp = _get_employee(emp_id)
    if emp["status"] != "archived":
        db.get_db().execute("UPDATE employees SET status='archived', updated_at=datetime('now') WHERE id=?", (emp_id,))
        db.get_db().commit()
        audit.log("archive", "employee", emp_id, emp["full_name"])
    return redirect(url_for("employees.card", emp_id=emp_id))

@bp.route("/<int:emp_id>/restore", methods=["POST"])
@roles_required("admin", "hr")
def restore(emp_id):
    emp = _get_employee(emp_id)
    if emp["status"] == "archived":
        new = "working" if emp["hire_date"] and not emp["dismissal_date"] else "new"
        db.get_db().execute("UPDATE employees SET status=?, updated_at=datetime('now') WHERE id=?", (new, emp_id))
        db.get_db().commit()
        audit.log("restore", "employee", emp_id, emp["full_name"])
    return redirect(url_for("employees.card", emp_id=emp_id))
