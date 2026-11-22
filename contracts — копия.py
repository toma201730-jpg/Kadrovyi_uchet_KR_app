import calendar, datetime, os
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response, send_file, current_app
import db, audit, params, documents, numbering, storage
from auth import roles_required, current_user

bp = Blueprint("contracts", __name__, url_prefix="/contracts")
TYPES = {"indefinite": "Бессрочный", "fixed": "Срочный"}
ST = {"draft": "Черновик", "active": "Действует", "terminated": "Прекращён"}
SCAN_TYPES = {b"%PDF": "pdf", b"\xff\xd8\xff": "jpg", b"\x89PNG": "png"}
MAX_SCAN = 10 * 1024 * 1024

def _date(raw):
    try:
        d = datetime.date.fromisoformat(raw)
        return d if 1900 <= d.year <= 2100 else None
    except (ValueError, TypeError):
        return None

def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return datetime.date(year, month, min(d.day, calendar.monthrange(year, month)[1]))

def max_probation_days(start):
    """Предел испытательного срока из настроек (в месяцах), в днях от даты начала работы."""
    return (add_months(start, params.get_int("probation_max_months")) - start).days

def probation_end(start, days):
    return start + datetime.timedelta(days=days - 1)

def validate(src, emp):
    """Возвращает (данные для записи, ошибки). Здесь проверяются условия договора по настройкам ТК КР."""
    conn, errors, d = db.get_db(), [], {}
    if emp["status"] not in ("new", "working", "on_leave"):
        errors.append("Договор можно оформить только сотруднику со статусом «Оформляется», «Работает» или «В отпуске».")
    d["contract_type"] = src.get("contract_type", "")
    if d["contract_type"] not in TYPES:
        errors.append("Выберите тип договора.")
    cdate = _date(src.get("contract_date", ""))
    start = _date(src.get("start_date", ""))
    if not cdate:
        errors.append("Дата договора: укажите корректную дату.")
    if not start:
        errors.append("Дата начала работы: укажите корректную дату.")
    end = None
    if d["contract_type"] == "fixed":
        end = _date(src.get("end_date", ""))
        if not end:
            errors.append("Для срочного договора укажите дату окончания.")
        elif start and end <= start:
            errors.append("Дата окончания должна быть позже даты начала.")
    d["contract_date"] = cdate.isoformat() if cdate else None
    d["start_date"] = start.isoformat() if start else None
    d["end_date"] = end.isoformat() if end else None
    raw = str(src.get("probation_days", "0")).strip() or "0"
    if not raw.isdigit():
        errors.append("Испытательный срок: целое число дней.")
        d["probation_days"] = 0
    else:
        days = d["probation_days"] = int(raw)
        if start and days:
            limit = max_probation_days(start)
            if days > limit:
                errors.append(f"Испытательный срок {days} дн. превышает допустимый: {params.get_int('probation_max_months')} мес. "
                              f"(не более {limit} дн. с {start.strftime('%d.%m.%Y')}).")
            if end and probation_end(start, days) > end:
                errors.append("Испытательный срок не может быть длиннее срока договора.")
    for key, table, label in (("department_id", "departments", "подразделение"), ("position_id", "positions", "должность")):
        v = str(src.get(key, ""))
        if v.isdigit() and conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND is_active=1", (v,)).fetchone():
            d[key] = int(v)
        else:
            errors.append(f"Выберите {label} из справочника.")
    try:
        d["salary"] = round(float(str(src.get("salary", "")).replace(",", ".").replace(" ", "")), 2)
        if not 0 < d["salary"] <= 100_000_000:
            raise ValueError
    except ValueError:
        errors.append("Оклад: положительное число.")
    d["work_mode"] = " ".join(str(src.get("work_mode", "")).split())
    if not d["work_mode"] or len(d["work_mode"]) > 200:
        errors.append("Режим рабочего времени обязателен (до 200 символов).")
    return d, errors

def _passport(emp):
    if not emp["passport_number"]:
        return ""
    s = emp["passport_number"]
    if emp["passport_issued_by"]:
        s += f", выдан {emp['passport_issued_by']}"
    if emp["passport_issued_date"]:
        s += f" {documents.fmt_date(emp['passport_issued_date'])}"
    return s

def build_ctx(c, number, emp):
    conn = db.get_db()
    org = conn.execute("SELECT * FROM organization WHERE id=1").fetchone()
    start = datetime.date.fromisoformat(c["start_date"])
    days = c["probation_days"]
    return {
        "org_name": org["full_name"], "head_position": org["head_position"], "head_name": org["head_name"],
        "contract_number": number, "contract_date": documents.fmt_date(c["contract_date"]),
        "employee_name": emp["full_name"],
        "employee_birth_date": documents.fmt_date(emp["birth_date"]) if emp["birth_date"] else "",
        "employee_passport": _passport(emp), "employee_address": emp["address_registration"] or "",
        "employee_pin": emp["pin"] or "",
        "department": conn.execute("SELECT name FROM departments WHERE id=?", (c["department_id"],)).fetchone()["name"],
        "position": conn.execute("SELECT name FROM positions WHERE id=?", (c["position_id"],)).fetchone()["name"],
        "start_date": documents.fmt_date(c["start_date"]),
        "term_text": f"на срок до {documents.fmt_date(c['end_date'])}" if c["contract_type"] == "fixed" else "на неопределённый срок",
        "probation_text": (f"{days} дн. (до {probation_end(start, days).strftime('%d.%m.%Y')})" if days else "без испытательного срока"),
        "salary": documents.fmt_money(c["salary"]), "work_mode": c["work_mode"],
    }

def _contract_or_404(cid):
    c = db.get_db().execute("SELECT * FROM contracts WHERE id=?", (cid,)).fetchone()
    if not c:
        abort(404)
    return c

def _employee(eid):
    e = db.get_db().execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    return e

def _preview(c, emp):
    """(текст, сообщение об ошибке)."""
    if c["status"] != "draft":
        return c["rendered_text"], None
    d, errors = validate(dict(c), emp)
    errors += documents.org_errors()
    if errors:
        return None, " ".join(errors)
    try:
        body = documents.active_template("contract")["body"]
        return documents.render_doc("contract", body, build_ctx(c, "___", emp)), None
    except ValueError as e:
        return None, str(e)

# ---------- список ----------
@bp.route("")
@roles_required("admin", "hr")
def list_page():
    sql, args, where = "SELECT c.*, e.full_name FROM contracts c JOIN employees e ON e.id=c.employee_id", [], []
    if request.args.get("type") in TYPES:
        where.append("c.contract_type=?"); args.append(request.args["type"])
    if request.args.get("status") in ST:
        where.append("c.status=?"); args.append(request.args["status"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.get_db().execute(sql + " ORDER BY c.contract_date DESC, c.id DESC", args).fetchall()
    q = " ".join(request.args.get("q", "").split()).casefold()
    if q:
        rows = [r for r in rows if all(t in r["full_name"].casefold() for t in q.split())]
    return render_template("contracts_list.html", rows=rows, types=TYPES, st=ST, f=request.args)

# ---------- создание и правка ----------
def _form(contract=None):
    conn = db.get_db()
    emp_id = request.values.get("employee_id", "") or (str(contract["employee_id"]) if contract else "")
    if request.method == "POST":
        v = request.form
    elif contract:
        v = dict(contract)
    else:
        v = {"contract_date": datetime.date.today().isoformat(), "start_date": datetime.date.today().isoformat(),
             "contract_type": "indefinite", "probation_days": 0, "work_mode": "5-дневная рабочая неделя, 40 часов"}
        if emp_id.isdigit():  # подсказка из текущей записи кадровой истории
            h = conn.execute("SELECT * FROM employee_history WHERE employee_id=? AND start_date<=date('now') "
                             "ORDER BY start_date DESC, id DESC LIMIT 1", (emp_id,)).fetchone()
            if h:
                v.update(department_id=h["department_id"], position_id=h["position_id"], salary=h["salary"] or "")
    return dict(c=contract, v=v, emp_id=emp_id, types=TYPES,
                candidates=conn.execute("SELECT id, full_name FROM employees WHERE status IN ('new','working','on_leave') ORDER BY full_name").fetchall(),
                depts=conn.execute("SELECT * FROM departments WHERE is_active=1 ORDER BY name").fetchall(),
                poss=conn.execute("SELECT * FROM positions WHERE is_active=1 ORDER BY name").fetchall(),
                probation_max=params.get_int("probation_max_months"))

def _save(contract):
    raw = request.form.get("employee_id", "")
    emp = db.get_db().execute("SELECT * FROM employees WHERE id=?", (raw,)).fetchone() if raw.isdigit() else None
    if not emp:
        flash("Выберите сотрудника.", "err")
        return None
    d, errors = validate(request.form, emp)
    if errors:
        for e in errors:
            flash(e, "err")
        return None
    conn = db.get_db()
    cols = ["contract_type", "contract_date", "start_date", "end_date", "probation_days", "department_id",
            "position_id", "salary", "work_mode"]
    if contract:
        conn.execute(f"UPDATE contracts SET employee_id=?, {', '.join(c + '=?' for c in cols)} WHERE id=? AND status='draft'",
                     [emp["id"], *[d[c] for c in cols], contract["id"]])
        cid, action = contract["id"], "draft_update"
    else:
        cur = conn.execute(f"INSERT INTO contracts (employee_id, created_by, {', '.join(cols)}) VALUES (?,?,{','.join('?' * len(cols))})",
                           [emp["id"], current_user()["id"], *[d[c] for c in cols]])
        cid, action = cur.lastrowid, "draft_create"
    conn.commit()
    audit.log(action, "contract", cid, f"сотрудник #{emp['id']}")
    return cid

@bp.route("/new", methods=["GET", "POST"])
@roles_required("admin", "hr")
def create():
    if request.method == "POST":
        cid = _save(None)
        if cid:
            return redirect(url_for("contracts.view", cid=cid))
    return render_template("contract_form.html", **_form()), (400 if request.method == "POST" else 200)

@bp.route("/<int:cid>/edit", methods=["GET", "POST"])
@roles_required("admin", "hr")
def edit(cid):
    c = _contract_or_404(cid)
    if c["status"] != "draft":
        flash("Оформленный договор изменить нельзя.", "err")
        return redirect(url_for("contracts.view", cid=cid))
    if request.method == "POST" and _save(c):
        return redirect(url_for("contracts.view", cid=cid))
    return render_template("contract_form.html", **_form(c)), (400 if request.method == "POST" else 200)

# ---------- просмотр, оформление, удаление, скачивание ----------
@bp.route("/<int:cid>")
@roles_required("admin", "hr")
def view(cid):
    c = _contract_or_404(cid)
    emp = _employee(c["employee_id"])
    text, problem = _preview(c, emp)
    scan = db.get_db().execute("SELECT * FROM files WHERE id=?", (c["scan_file_id"],)).fetchone() if c["scan_file_id"] else None
    return render_template("contract_view.html", c=c, emp=emp, types=TYPES, st=ST, text=text, problem=problem, scan=scan)

@bp.route("/<int:cid>/issue", methods=["POST"])
@roles_required("admin", "hr")
def issue(cid):
    c = _contract_or_404(cid)
    conn = db.get_db()
    try:
        if c["status"] != "draft":
            raise ValueError("Договор уже оформлен.")
        emp = _employee(c["employee_id"])
        d, errors = validate(dict(c), emp)  # повторная проверка по актуальным данным и настройкам
        errors += documents.org_errors()
        if conn.execute("SELECT 1 FROM contracts WHERE employee_id=? AND status='active'", (emp["id"],)).fetchone():
            errors.append("У сотрудника уже есть действующий договор.")
        if errors:
            raise ValueError(" ".join(errors))
        tpl = documents.active_template("contract")
        number = numbering.next_number("contract", int(c["contract_date"][:4]), commit=False)
        text = documents.render_doc("contract", tpl["body"], build_ctx(c, number, emp))
        n = conn.execute("UPDATE contracts SET number=?, status='active', template_version=?, rendered_text=?, issued_by=?, issued_at=datetime('now') "
                         "WHERE id=? AND status='draft'", (number, tpl["version"], text, current_user()["id"], cid)).rowcount
        if n != 1:
            raise ValueError("Данные изменились, повторите оформление.")
        audit.log("issue", "contract", cid, f"договор № {number}, шаблон v{tpl['version']}", commit=False)
        conn.commit()
        flash(f"Договор оформлен. Номер: {number}.", "ok")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "err")
    except Exception as e:  # например, нарушение уникальности при гонке
        conn.rollback()
        current_app.logger.exception("issue contract failed")
        flash("Не удалось оформить договор, попробуйте ещё раз.", "err")
    return redirect(url_for("contracts.view", cid=cid))

@bp.route("/<int:cid>/delete", methods=["POST"])
@roles_required("admin", "hr")
def delete_draft(cid):
    c = _contract_or_404(cid)
    if c["status"] != "draft":
        flash("Оформленный договор удалить нельзя.", "err")
        return redirect(url_for("contracts.view", cid=cid))
    db.get_db().execute("DELETE FROM contracts WHERE id=? AND status='draft'", (cid,))
    db.get_db().commit()
    audit.log("draft_delete", "contract", cid)
    return redirect(url_for("contracts.list_page"))

@bp.route("/<int:cid>/download/<fmt>")
@roles_required("admin", "hr")
def download(cid, fmt):
    c = _contract_or_404(cid)
    if fmt not in ("docx", "pdf"):
        abort(404)
    if c["status"] == "draft":
        abort(409)
    data = documents.to_docx(c["rendered_text"]) if fmt == "docx" else documents.to_pdf(c["rendered_text"])
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if fmt == "docx" else "application/pdf"
    audit.log("download", "contract", cid, fmt)
    return Response(data, mimetype=mime, headers={"Content-Disposition": f"attachment; filename=contract_{c['number']}_{c['contract_date'][:4]}.{fmt}"})

# ---------- скан подписанного договора ----------
@bp.route("/<int:cid>/scan", methods=["POST"])
@roles_required("admin", "hr")
def upload_scan(cid):
    c = _contract_or_404(cid)
    f = request.files.get("scan")
    if c["status"] == "draft":
        flash("Скан можно прикрепить только к оформленному договору.", "err")
    elif not f or not f.filename:
        flash("Выберите файл.", "err")
    else:
        head = f.stream.read(8)
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        if not any(head.startswith(sig) for sig in SCAN_TYPES):
            flash("Допустимы файлы PDF, JPG и PNG.", "err")
        elif size > MAX_SCAN:
            flash("Файл больше 10 МБ.", "err")
        else:
            fid = storage.save_file(f, current_user()["id"])
            db.get_db().execute("UPDATE contracts SET scan_file_id=? WHERE id=?", (fid, cid))
            db.get_db().commit()
            audit.log("scan_attach", "contract", cid, f"файл #{fid}")
            flash("Скан прикреплён.", "ok")
    return redirect(url_for("contracts.view", cid=cid))

@bp.route("/<int:cid>/scan/file")
@roles_required("admin", "hr")
def download_scan(cid):
    c = _contract_or_404(cid)
    row = db.get_db().execute("SELECT * FROM files WHERE id=?", (c["scan_file_id"],)).fetchone() if c["scan_file_id"] else None
    if not row:
        abort(404)
    audit.log("scan_download", "contract", cid)
    path = os.path.join(current_app.config["UPLOAD_DIR"], row["stored_name"])
    return send_file(os.path.abspath(path), as_attachment=True, download_name=row["original_name"])
