import json, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, Response
import db, audit, params, documents, numbering, leaves, contracts
from auth import roles_required, current_user

bp = Blueprint("orders", __name__)

# Тип приказа -> (название, тип шаблона документа)
ORDER_TYPES = {
    "hire": ("О приёме на работу", "order_hire"),
    "transfer": ("О переводе", "order_transfer"),
    "termination": ("О прекращении трудового договора", "order_termination"),
    "leave": ("О предоставлении отпуска", "order_leave"),
}
CREATABLE = ("hire", "transfer", "termination")  # приказ об отпуске формируется из раздела «Отпуска»
ELIGIBLE = {"hire": ("new",), "transfer": ("working", "on_leave"), "termination": ("working", "on_leave")}
ST = {"draft": "Черновик", "posted": "Проведён"}

def _today():
    return datetime.date.today().isoformat()

def _date(raw):
    try:
        d = datetime.date.fromisoformat(raw)
        return d.isoformat() if 1900 <= d.year <= 2100 else None
    except (ValueError, TypeError):
        return None

def _clean(v, limit=300):
    return " ".join(str(v or "").split())[:limit + 1]

def _job_fields(src, p, errors):
    """Подразделение, должность, оклад, основание — общие поля приёма и перевода."""
    conn = db.get_db()
    for key, table, label in (("department_id", "departments", "подразделение"), ("position_id", "positions", "должность")):
        v = str(src.get(key, ""))
        if v.isdigit() and conn.execute(f"SELECT 1 FROM {table} WHERE id=? AND is_active=1", (v,)).fetchone():
            p[key] = int(v)
        else:
            errors.append(f"Выберите {label} из справочника.")
    try:
        p["salary"] = round(float(str(src.get("salary", "")).replace(",", ".").replace(" ", "")), 2)
        if not 0 < p["salary"] <= 100_000_000:
            raise ValueError
    except ValueError:
        errors.append("Оклад: положительное число.")
    p["basis"] = _clean(src.get("basis", ""))
    if len(p["basis"]) > 300:
        errors.append("Основание: максимум 300 символов.")

def _current_record(emp_id, as_of):
    return db.get_db().execute(
        "SELECT h.*, d.name AS dept, p.name AS pos FROM employee_history h LEFT JOIN departments d ON d.id=h.department_id "
        "LEFT JOIN positions p ON p.id=h.position_id WHERE h.employee_id=? AND h.start_date<=? "
        "ORDER BY h.start_date DESC, h.id DESC LIMIT 1", (emp_id, as_of)).fetchone()

# ---------- проверки по типам ----------
def validate_hire(src, emp):
    errors, p = [], {}
    if emp["status"] != "new":
        errors.append("Приказ о приёме можно оформить только сотруднику со статусом «Оформляется».")
    p["hire_date"] = _date(src.get("hire_date", ""))
    if not p["hire_date"]:
        errors.append("Дата приёма: укажите корректную дату.")
    _job_fields(src, p, errors)
    limit = params.get_int("probation_max_months")
    raw = str(src.get("probation_months", "0")).strip() or "0"
    if raw.isdigit() and int(raw) <= limit:
        p["probation_months"] = int(raw)
    else:
        errors.append(f"Испытательный срок: целое число месяцев от 0 до {limit} (по настройкам ТК КР).")
    return p, errors

def validate_transfer(src, emp):
    errors, p = [], {}
    if emp["status"] not in ("working", "on_leave"):
        errors.append("Перевести можно только работающего сотрудника.")
    p["transfer_date"] = _date(src.get("transfer_date", ""))
    cur = None
    if not p["transfer_date"]:
        errors.append("Дата перевода: укажите корректную дату.")
    else:
        if emp["hire_date"] and p["transfer_date"] < emp["hire_date"]:
            errors.append("Дата перевода раньше даты приёма.")
        cur = _current_record(emp["id"], p["transfer_date"])
        if not cur:
            errors.append("У сотрудника нет записи в кадровой истории на эту дату.")
    n = len(errors)
    _job_fields(src, p, errors)
    if cur and len(errors) == n and (cur["department_id"], cur["position_id"], cur["salary"]) == (p["department_id"], p["position_id"], p["salary"]):
        errors.append("Перевод ничего не меняет: подразделение, должность и оклад те же.")
    return p, errors

def validate_termination(src, emp):
    conn, errors, p = db.get_db(), [], {}
    if emp["status"] not in ("working", "on_leave"):
        errors.append("Прекратить договор можно только у работающего сотрудника.")
    elif emp["dismissal_date"]:
        errors.append(f"Прекращение договора уже оформлено приказом, дата: {documents.fmt_date(emp['dismissal_date'])}.")
    p["dismissal_date"] = _date(src.get("dismissal_date", ""))
    if not p["dismissal_date"]:
        errors.append("Дата прекращения договора: укажите корректную дату.")
    elif emp["hire_date"] and p["dismissal_date"] < emp["hire_date"]:
        errors.append("Дата прекращения раньше даты приёма.")
    gid = str(src.get("ground_id", ""))
    ground = conn.execute("SELECT * FROM termination_grounds WHERE id=? AND is_active=1", (gid,)).fetchone() if gid.isdigit() else None
    p["ground_id"] = ground["id"] if ground else None
    p["ground_note"] = _clean(src.get("ground_note", ""), 200)
    p["notice_date"] = _date(src.get("notice_date", ""))
    if len(p["ground_note"]) > 200:
        errors.append("Пояснение к основанию: максимум 200 символов.")
    if not ground:
        errors.append("Выберите основание из справочника.")
    elif p["dismissal_date"]:
        kind, dd = ground["kind"], datetime.date.fromisoformat(p["dismissal_date"])
        if kind == "other" and not p["ground_note"]:
            errors.append("Для иного основания укажите пояснение.")
        if kind == "employee":
            months = params.get_int("resignation_notice_months")
            if not p["notice_date"]:
                errors.append("Для расторжения по инициативе работника укажите дату заявления.")
            else:
                earliest = contracts.add_months(datetime.date.fromisoformat(p["notice_date"]), months)
                if dd < earliest:
                    errors.append(f"Срок предупреждения по настройкам: {months} мес. Увольнение возможно не ранее {earliest.strftime('%d.%m.%Y')}. "
                                  "Если стороны договорились о более ранней дате, выберите основание «Соглашение сторон».")
        if kind == "term_expired":
            k = conn.execute("SELECT * FROM contracts WHERE employee_id=? AND status='active'", (emp["id"],)).fetchone()
            if not k or k["contract_type"] != "fixed":
                errors.append("Для истечения срока нужен действующий срочный договор в системе.")
            elif k["end_date"] != p["dismissal_date"]:
                errors.append(f"Договор действует до {documents.fmt_date(k['end_date'])}: дата прекращения должна совпадать с этой датой.")
    return p, errors

def termination_warnings(emp, p):
    """Предупреждения, не блокирующие проведение."""
    conn, out = db.get_db(), []
    if not conn.execute("SELECT 1 FROM contracts WHERE employee_id=? AND status='active'", (emp["id"],)).fetchone():
        out.append("В системе нет действующего трудового договора этого сотрудника.")
    n = conn.execute("SELECT COUNT(*) c FROM leaves WHERE employee_id=? AND status='posted' AND end_date>?",
                     (emp["id"], p.get("dismissal_date") or "9999")).fetchone()["c"]
    if n and p.get("dismissal_date"):
        out.append(f"Есть оформленные отпуска, продолжающиеся после даты прекращения договора ({n}): они не состоятся, учтите это в графике.")
    return out

VALIDATE = {"hire": validate_hire, "transfer": validate_transfer, "termination": validate_termination}

# ---------- данные для шаблонов ----------
def _org():
    o = db.get_db().execute("SELECT * FROM organization WHERE id=1").fetchone()
    return {"org_name": o["full_name"], "head_position": o["head_position"], "head_name": o["head_name"]}

def _name(table, rid):
    return db.get_db().execute(f"SELECT name FROM {table} WHERE id=?", (rid,)).fetchone()["name"]

def ctx_hire(order_date, number, emp, p):
    months = p["probation_months"]
    return {**_org(), "order_number": number, "order_date": documents.fmt_date(order_date), "employee_name": emp["full_name"],
            "department": _name("departments", p["department_id"]), "position": _name("positions", p["position_id"]),
            "hire_date": documents.fmt_date(p["hire_date"]), "salary": documents.fmt_money(p["salary"]),
            "probation_text": f"{months} мес." if months else "без испытательного срока",
            "basis_line": f"Основание: {p['basis']}." if p["basis"] else ""}

def ctx_transfer(order_date, number, emp, p):
    cur = _current_record(emp["id"], p["transfer_date"])
    return {**_org(), "order_number": number, "order_date": documents.fmt_date(order_date), "employee_name": emp["full_name"],
            "old_department": cur["dept"] if cur else "", "old_position": cur["pos"] if cur else "",
            "department": _name("departments", p["department_id"]), "position": _name("positions", p["position_id"]),
            "transfer_date": documents.fmt_date(p["transfer_date"]), "salary": documents.fmt_money(p["salary"]),
            "basis_line": f"Основание: {p['basis']}." if p["basis"] else ""}

def ctx_termination(order_date, number, emp, p):
    g = db.get_db().execute("SELECT * FROM termination_grounds WHERE id=?", (p["ground_id"],)).fetchone()
    text = g["name"] + (f": {p['ground_note']}" if p["ground_note"] else "")
    if g["article_ref"]:
        text += f" ({g['article_ref']})"
    cur = _current_record(emp["id"], p["dismissal_date"])
    left = leaves.balance(emp, datetime.date.fromisoformat(p["dismissal_date"]))["remaining"] if emp["hire_date"] else 0
    return {**_org(), "order_number": number, "order_date": documents.fmt_date(order_date), "employee_name": emp["full_name"],
            "department": cur["dept"] if cur else "", "position": cur["pos"] if cur else "",
            "dismissal_date": documents.fmt_date(p["dismissal_date"]), "ground_text": text,
            "leave_compensation_line": f"Выплатить компенсацию за неиспользованный отпуск: {left} календарных дн." if left > 0 else ""}

CTX = {"hire": ctx_hire, "transfer": ctx_transfer, "termination": ctx_termination}

# ---------- применение проведённого приказа (внутри транзакции) ----------
def _history(conn, emp_id, start, p, number, order_date, uid):
    conn.execute("INSERT INTO employee_history (employee_id, start_date, department_id, position_id, salary, basis, created_by) VALUES (?,?,?,?,?,?,?)",
                 (emp_id, start, p["department_id"], p["position_id"], p["salary"], f"Приказ № {number} от {documents.fmt_date(order_date)}", uid))

def apply_hire(conn, o, emp, p, number, uid):
    n = conn.execute("UPDATE employees SET status='working', hire_date=?, dismissal_date=NULL, updated_at=datetime('now') WHERE id=? AND status='new'",
                     (p["hire_date"], emp["id"])).rowcount
    if n != 1:
        raise ValueError("Данные изменились, повторите проведение.")
    _history(conn, emp["id"], p["hire_date"], p, number, o["order_date"], uid)
    audit.log("hire", "employee", emp["id"], f"по приказу № {number}", commit=False)

def apply_transfer(conn, o, emp, p, number, uid):
    n = conn.execute("UPDATE employees SET updated_at=datetime('now') WHERE id=? AND status IN ('working','on_leave')", (emp["id"],)).rowcount
    if n != 1:
        raise ValueError("Данные изменились, повторите проведение.")
    _history(conn, emp["id"], p["transfer_date"], p, number, o["order_date"], uid)
    audit.log("transfer", "employee", emp["id"], f"по приказу № {number}, с {p['transfer_date']}", commit=False)

def apply_termination(conn, o, emp, p, number, uid):
    n = conn.execute("UPDATE employees SET dismissal_date=?, updated_at=datetime('now') WHERE id=? AND status IN ('working','on_leave') AND dismissal_date IS NULL",
                     (p["dismissal_date"], emp["id"])).rowcount
    if n != 1:
        raise ValueError("Данные изменились, повторите проведение.")
    leaves.refresh_statuses(commit=False)  # если дата уже наступила: «В архиве», договор прекращён
    audit.log("terminate", "employee", emp["id"], f"по приказу № {number}, дата {p['dismissal_date']}", commit=False)

APPLY = {"hire": apply_hire, "transfer": apply_transfer, "termination": apply_termination}

# ---------- вспомогательное ----------
def _order_or_404(order_id):
    o = db.get_db().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o:
        abort(404)
    return o

def _employee(emp_id):
    e = db.get_db().execute("SELECT * FROM employees WHERE id=?", (emp_id,)).fetchone()
    if not e:
        abort(404)
    return e

def _preview(o, emp):
    """(текст, проблема, предупреждения)."""
    if o["status"] == "posted":
        return o["rendered_text"], None, []
    kind, p = o["doc_type"], json.loads(o["params"])
    _, errors = VALIDATE[kind](p, emp)
    errors += documents.org_errors()
    if errors:
        return None, " ".join(errors), []
    warns = termination_warnings(emp, p) if kind == "termination" else []
    try:
        body = documents.active_template(ORDER_TYPES[kind][1])["body"]
        return documents.render_doc(ORDER_TYPES[kind][1], body, CTX[kind](o["order_date"], "___", emp, p)), None, warns
    except ValueError as e:
        return None, str(e), warns

# ---------- список ----------
@bp.route("/orders")
@roles_required("admin", "hr")
def list_page():
    leaves.refresh_statuses()
    sql, args, where = ("SELECT o.*, e.full_name FROM orders o JOIN employees e ON e.id=o.employee_id", [], [])
    typ, d1, d2 = request.args.get("type", ""), _date(request.args.get("from", "")), _date(request.args.get("to", ""))
    if typ in ORDER_TYPES:
        where.append("o.doc_type=?"); args.append(typ)
    if d1:
        where.append("o.order_date>=?"); args.append(d1)
    if d2:
        where.append("o.order_date<=?"); args.append(d2)
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.get_db().execute(sql + " ORDER BY o.order_date DESC, o.id DESC", args).fetchall()
    q = " ".join(request.args.get("q", "").split()).casefold()
    if q:
        rows = [r for r in rows if all(t in r["full_name"].casefold() for t in q.split())]
    return render_template("orders_list.html", rows=rows, types=ORDER_TYPES, st=ST, f=request.args)

# ---------- создание и правка черновика ----------
def _form(order=None):
    conn = db.get_db()
    kind = order["doc_type"] if order else request.values.get("type", request.form.get("doc_type", "hire"))
    if kind not in CREATABLE:
        kind = "hire"
    emp_id = request.values.get("employee_id", "") or (str(order["employee_id"]) if order else "")
    marks = ",".join("?" * len(ELIGIBLE[kind]))
    if request.method == "POST":
        v = request.form
    elif order:
        v = {**json.loads(order["params"]), "order_date": order["order_date"]}
    else:
        v = {"order_date": _today(), "hire_date": _today(), "transfer_date": _today(), "dismissal_date": _today(), "probation_months": 0}
        if kind == "transfer" and emp_id.isdigit():  # подсказка: текущая запись истории
            cur = _current_record(int(emp_id), _today())
            if cur:
                v.update(department_id=cur["department_id"], position_id=cur["position_id"], salary=cur["salary"] or "")
    return dict(order=order, kind=kind, emp_id=emp_id, v=v, types={k: ORDER_TYPES[k] for k in CREATABLE},
                candidates=conn.execute(f"SELECT id, full_name FROM employees WHERE status IN ({marks}) ORDER BY full_name", ELIGIBLE[kind]).fetchall(),
                depts=conn.execute("SELECT * FROM departments WHERE is_active=1 ORDER BY name").fetchall(),
                poss=conn.execute("SELECT * FROM positions WHERE is_active=1 ORDER BY name").fetchall(),
                grounds=conn.execute("SELECT * FROM termination_grounds WHERE is_active=1 ORDER BY id").fetchall(),
                probation_max=params.get_int("probation_max_months"))

def _save(order):
    kind = order["doc_type"] if order else request.form.get("doc_type", "")
    if kind not in CREATABLE:
        flash("Выберите тип приказа.", "err")
        return None
    raw = request.form.get("employee_id", "")
    emp = db.get_db().execute("SELECT * FROM employees WHERE id=?", (raw,)).fetchone() if raw.isdigit() else None
    if not emp:
        flash("Выберите сотрудника.", "err")
        return None
    p, errors = VALIDATE[kind](request.form, emp)
    od = _date(request.form.get("order_date", ""))
    if not od:
        errors.append("Дата приказа: укажите корректную дату.")
    if errors:
        for e in errors:
            flash(e, "err")
        return None
    conn = db.get_db()
    if order:
        conn.execute("UPDATE orders SET order_date=?, params=?, employee_id=? WHERE id=? AND status='draft'",
                     (od, json.dumps(p), emp["id"], order["id"]))
        oid, action = order["id"], "draft_update"
    else:
        cur = conn.execute("INSERT INTO orders (doc_type, order_date, employee_id, params, created_by) VALUES (?,?,?,?,?)",
                           (kind, od, emp["id"], json.dumps(p), current_user()["id"]))
        oid, action = cur.lastrowid, "draft_create"
    conn.commit()
    audit.log(action, "order", oid, f"{ORDER_TYPES[kind][0]}, сотрудник #{emp['id']}")
    return oid

@bp.route("/orders/new", methods=["GET", "POST"])
@roles_required("admin", "hr")
def create():
    if request.method == "POST":
        oid = _save(None)
        if oid:
            return redirect(url_for("orders.view", order_id=oid))
    return render_template("order_form.html", **_form()), (400 if request.method == "POST" else 200)

@bp.route("/orders/<int:order_id>/edit", methods=["GET", "POST"])
@roles_required("admin", "hr")
def edit(order_id):
    o = _order_or_404(order_id)
    if o["status"] != "draft":
        flash("Проведённый приказ изменить нельзя.", "err")
        return redirect(url_for("orders.view", order_id=order_id))
    if request.method == "POST":
        if _save(o):
            return redirect(url_for("orders.view", order_id=order_id))
    return render_template("order_form.html", **_form(o)), (400 if request.method == "POST" else 200)

# ---------- просмотр, проведение, удаление черновика, скачивание ----------
@bp.route("/orders/<int:order_id>")
@roles_required("admin", "hr")
def view(order_id):
    o = _order_or_404(order_id)
    emp = _employee(o["employee_id"])
    text, problem, warns = _preview(o, emp)
    return render_template("order_view.html", o=o, emp=emp, st=ST, label=ORDER_TYPES[o["doc_type"]][0], text=text,
                           problem=problem, warns=warns)

@bp.route("/orders/<int:order_id>/post", methods=["POST"])
@roles_required("admin", "hr")
def post(order_id):
    o = _order_or_404(order_id)
    conn = db.get_db()
    try:
        kind = o["doc_type"]
        if o["status"] != "draft" or kind not in CREATABLE:
            raise ValueError("Приказ уже проведён.")
        emp = _employee(o["employee_id"])
        p, errors = VALIDATE[kind](json.loads(o["params"]), emp)
        errors += documents.org_errors()
        if errors:
            raise ValueError(" ".join(errors))
        tpl = documents.active_template(ORDER_TYPES[kind][1])
        number = numbering.next_number("order", int(o["order_date"][:4]), commit=False)
        text = documents.render_doc(ORDER_TYPES[kind][1], tpl["body"], CTX[kind](o["order_date"], number, emp, p))
        uid = current_user()["id"]
        n = conn.execute("UPDATE orders SET number=?, status='posted', template_version=?, rendered_text=?, posted_by=?, posted_at=datetime('now') "
                         "WHERE id=? AND status='draft'", (number, tpl["version"], text, uid, order_id)).rowcount
        if n != 1:
            raise ValueError("Данные изменились, повторите проведение.")
        APPLY[kind](conn, o, emp, p, number, uid)
        audit.log("post", "order", order_id, f"приказ № {number}, шаблон v{tpl['version']}", commit=False)
        conn.commit()
        flash(f"Приказ проведён. Номер: {number}.", "ok")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "err")
    return redirect(url_for("orders.view", order_id=order_id))

@bp.route("/orders/<int:order_id>/delete", methods=["POST"])
@roles_required("admin", "hr")
def delete_draft(order_id):
    o = _order_or_404(order_id)
    if o["status"] != "draft":
        flash("Проведённый приказ удалить нельзя.", "err")
        return redirect(url_for("orders.view", order_id=order_id))
    db.get_db().execute("DELETE FROM orders WHERE id=? AND status='draft'", (order_id,))
    db.get_db().commit()
    audit.log("draft_delete", "order", order_id)
    return redirect(url_for("orders.list_page"))

@bp.route("/orders/<int:order_id>/download/<fmt>")
@roles_required("admin", "hr")
def download(order_id, fmt):
    o = _order_or_404(order_id)
    if fmt not in ("docx", "pdf"):
        abort(404)
    if o["status"] != "posted":
        abort(409)
    data = documents.to_docx(o["rendered_text"]) if fmt == "docx" else documents.to_pdf(o["rendered_text"])
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document" if fmt == "docx" else "application/pdf"
    audit.log("download", "order", order_id, fmt)
    return Response(data, mimetype=mime, headers={"Content-Disposition": f"attachment; filename=order_{o['number']}_{o['order_date'][:4]}.{fmt}"})

# ---------- шаблоны документов (только администратор) ----------
@bp.route("/templates")
@roles_required("admin")
def templates_page():
    rows = [(k, v[0], documents.active_template(v[1])["version"]) for k, v in documents.TEMPLATE_KINDS.items()]
    return render_template("templates_list.html", rows=rows)

@bp.route("/templates/<kind>", methods=["GET", "POST"])
@roles_required("admin")
def template_edit(kind):
    if kind not in documents.TEMPLATE_KINDS:
        abort(404)
    doc_type = documents.TEMPLATE_KINDS[kind][1]
    tpl = documents.active_template(doc_type)
    body = tpl["body"]
    if request.method == "POST":
        body = request.form.get("body", "").replace("\r\n", "\n")
        errors = documents.validate_body(doc_type, body)
        if errors:
            for e in errors:
                flash(e, "err")
        else:
            if body != tpl["body"]:
                v = documents.save_template(doc_type, body, current_user()["id"])
                audit.log("template_save", "template", doc_type, f"версия {v}")
                flash(f"Сохранено как версия {v}. Уже проведённые документы не меняются.", "ok")
            return redirect(url_for("orders.template_edit", kind=kind))
    return render_template("template_edit.html", kind=kind, title=documents.TEMPLATE_KINDS[kind][0], body=body,
                           version=tpl["version"], fields=documents.FIELDS[doc_type], required=documents.REQUIRED[doc_type])
