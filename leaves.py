import calendar, datetime, json
from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
import db, audit, params, documents, numbering
from auth import roles_required, current_user

bp = Blueprint("leaves", __name__, url_prefix="/leaves")
LEAVE_TYPES = {"annual": "Ежегодный оплачиваемый", "unpaid": "Без сохранения заработной платы", "study": "Учебный", "other": "Другой"}
ORDER_TEXT = {"annual": "ежегодный оплачиваемый отпуск", "unpaid": "отпуск без сохранения заработной платы", "study": "учебный отпуск"}
ST = {"draft": "Запланирован (черновик)", "posted": "Оформлен приказом"}

def today():
    return datetime.date.today()

def _date(raw):
    try:
        d = datetime.date.fromisoformat(raw)
        return d if 1900 <= d.year <= 2100 else None
    except (ValueError, TypeError):
        return None

# ---------- расчёт остатка ----------
def full_months(a, b):
    if b < a:
        return 0
    m = (b.year - a.year) * 12 + b.month - a.month
    return m - 1 if b.day < a.day else m

def accrued(hire_date, as_of):
    """Начислено дней ежегодного отпуска на дату: норма из настроек × полных месяцев работы / 12 (с округлением вниз)."""
    return params.get_int("annual_leave_days") * full_months(hire_date, as_of) // 12

def balance(emp, as_of, exclude_leave_id=None):
    """Остаток = начислено + корректировки − оформленные ежегодные отпуска (кроме excluded)."""
    conn = db.get_db()
    hire = _date(emp["hire_date"] or "")
    acc = accrued(hire, as_of) if hire else 0
    adj = conn.execute("SELECT COALESCE(SUM(days),0) s FROM leave_adjustments WHERE employee_id=?", (emp["id"],)).fetchone()["s"]
    used = conn.execute("SELECT COALESCE(SUM(days),0) s FROM leaves WHERE employee_id=? AND leave_type='annual' AND status='posted' AND id IS NOT ?",
                        (emp["id"], exclude_leave_id)).fetchone()["s"]
    return {"accrued": acc, "adjust": adj, "used": used, "remaining": acc + adj - used}

def refresh_statuses(commit=True):
    """Статусы по датам: «В архиве» в день прекращения договора, «В отпуске» по оформленным отпускам."""
    conn, t = db.get_db(), today().isoformat()
    conn.execute("UPDATE employees SET status='archived', updated_at=datetime('now') WHERE status IN ('working','on_leave') "
                 "AND dismissal_date IS NOT NULL AND dismissal_date<=?", (t,))
    conn.execute("UPDATE contracts SET status='terminated' WHERE status='active' AND employee_id IN "
                 "(SELECT id FROM employees WHERE dismissal_date IS NOT NULL AND dismissal_date<=?)", (t,))
    cover = ("EXISTS (SELECT 1 FROM leaves l WHERE l.employee_id=employees.id AND l.status='posted' "
             "AND l.start_date<=? AND l.end_date>=?)")
    conn.execute(f"UPDATE employees SET status='on_leave' WHERE status='working' AND {cover}", (t, t))
    conn.execute(f"UPDATE employees SET status='working' WHERE status='on_leave' AND NOT {cover}", (t, t))
    if commit:
        conn.commit()

# ---------- проверки ----------
def validate(src, emp, exclude_id=None):
    """Возвращает (данные, ошибки, предупреждение о превышении остатка)."""
    conn, errors, d = db.get_db(), [], {}
    if emp["status"] not in ("working", "on_leave"):
        errors.append("Отпуск можно оформить только работающему сотруднику.")
    hire = _date(emp["hire_date"] or "")
    if not hire:
        errors.append("У сотрудника не указана дата приёма.")
    d["leave_type"] = src.get("leave_type", "")
    if d["leave_type"] not in LEAVE_TYPES:
        errors.append("Выберите вид отпуска.")
    start, end, od = _date(src.get("start_date", "")), _date(src.get("end_date", "")), _date(src.get("order_date", ""))
    if not start or not end:
        errors.append("Укажите корректные даты начала и окончания отпуска.")
    elif end < start:
        errors.append("Дата окончания раньше даты начала.")
    elif hire and start < hire:
        errors.append("Отпуск не может начинаться раньше даты приёма на работу.")
    if not od:
        errors.append("Дата приказа: укажите корректную дату.")
    d["start_date"], d["end_date"] = (start.isoformat() if start else None), (end.isoformat() if end else None)
    d["order_date"] = od.isoformat() if od else None
    d["days"] = (end - start).days + 1 if start and end and end >= start else 0  # календарные дни, включая крайние даты
    d["leave_name"] = " ".join(str(src.get("leave_name", "") or "").split())
    d["basis"] = " ".join(str(src.get("basis", "") or "").split())
    if d["leave_type"] == "other" and not d["leave_name"]:
        errors.append("Для вида «Другой» укажите название отпуска.")
    if len(d["leave_name"]) > 100 or len(d["basis"]) > 300:
        errors.append("Слишком длинное название или основание.")
    if d["start_date"] and d["end_date"] and d["days"] and conn.execute(
            "SELECT 1 FROM leaves WHERE employee_id=? AND status='posted' AND start_date<=? AND end_date>=? AND id IS NOT ?",
            (emp["id"], d["end_date"], d["start_date"], exclude_id)).fetchone():
        errors.append("Период пересекается с уже оформленным отпуском сотрудника.")
    warning = None
    if d["leave_type"] == "annual" and hire and start and d["days"]:
        b = balance(emp, start, exclude_id)
        if d["days"] > b["remaining"]:
            warning = (f"Запрошено {d['days']} дн., а в остатке на {start.strftime('%d.%m.%Y')} только {b['remaining']} дн. "
                       f"Оформить приказ нельзя, пока остаток не станет достаточным (корректировка остатка с указанием причины).")
    return d, errors, warning

def _current_position(emp_id, as_of):
    return db.get_db().execute(
        "SELECT d.name AS dept, p.name AS pos FROM employee_history h LEFT JOIN departments d ON d.id=h.department_id "
        "LEFT JOIN positions p ON p.id=h.position_id WHERE h.employee_id=? AND h.start_date<=? "
        "ORDER BY h.start_date DESC, h.id DESC LIMIT 1", (emp_id, as_of)).fetchone()

def build_ctx(lv, number, emp):
    org = db.get_db().execute("SELECT * FROM organization WHERE id=1").fetchone()
    cur = _current_position(emp["id"], lv["start_date"])
    return {
        "org_name": org["full_name"], "head_position": org["head_position"], "head_name": org["head_name"],
        "order_number": number, "order_date": documents.fmt_date(lv["order_date"]), "employee_name": emp["full_name"],
        "department": cur["dept"] if cur else "", "position": cur["pos"] if cur else "",
        "leave_type_text": lv["leave_name"] if lv["leave_type"] == "other" else ORDER_TEXT[lv["leave_type"]],
        "start_date": documents.fmt_date(lv["start_date"]), "end_date": documents.fmt_date(lv["end_date"]),
        "days": lv["days"], "basis_line": f"Основание: {lv['basis']}." if lv["basis"] else "",
    }

def _leave_or_404(lid):
    lv = db.get_db().execute("SELECT * FROM leaves WHERE id=?", (lid,)).fetchone()
    if not lv:
        abort(404)
    return lv

def _employee_or_404(eid):
    e = db.get_db().execute("SELECT * FROM employees WHERE id=?", (eid,)).fetchone()
    if not e:
        abort(404)
    return e

# ---------- график ----------
@bp.route("")
@roles_required("admin", "hr")
def schedule():
    refresh_statuses()
    year = int(request.args["year"]) if request.args.get("year", "").isdigit() and 1990 <= int(request.args["year"]) <= 2100 else today().year
    y1, y2 = f"{year}-01-01", f"{year}-12-31"
    conn = db.get_db()
    emps = conn.execute("SELECT * FROM employees WHERE status IN ('working','on_leave') OR id IN "
                        "(SELECT employee_id FROM leaves WHERE start_date<=? AND end_date>=?) ORDER BY full_name", (y2, y1)).fetchall()
    rows = []
    for e in emps:
        lvs = conn.execute("SELECT * FROM leaves WHERE employee_id=? AND start_date<=? AND end_date>=? ORDER BY start_date", (e["id"], y2, y1)).fetchall()
        months = []
        for m in range(1, 13):
            ms, me = datetime.date(year, m, 1), datetime.date(year, m, calendar.monthrange(year, m)[1])
            hit = [l for l in lvs if l["start_date"] <= me.isoformat() and l["end_date"] >= ms.isoformat()]
            months.append("posted" if any(l["status"] == "posted" for l in hit) else ("draft" if hit else ""))
        rows.append(dict(e=e, leaves=lvs, months=months, bal=balance(e, today()) if e["hire_date"] else None))
    return render_template("leaves_schedule.html", rows=rows, year=year, types=LEAVE_TYPES, st=ST)

# ---------- форма ----------
def _form(lv=None):
    conn = db.get_db()
    emp_id = request.values.get("employee_id", "") or (str(lv["employee_id"]) if lv else "")
    if request.method == "POST":
        v = request.form
    elif lv:
        v = dict(lv)
    else:
        v = {"order_date": today().isoformat(), "leave_type": "annual"}
    return dict(lv=lv, v=v, emp_id=emp_id, types=LEAVE_TYPES,
                candidates=conn.execute("SELECT id, full_name FROM employees WHERE status IN ('working','on_leave') ORDER BY full_name").fetchall())

def _save(lv):
    raw = request.form.get("employee_id", "")
    emp = db.get_db().execute("SELECT * FROM employees WHERE id=?", (raw,)).fetchone() if raw.isdigit() else None
    if not emp:
        flash("Выберите сотрудника.", "err")
        return None
    d, errors, warning = validate(request.form, emp, lv["id"] if lv else None)
    if errors:
        for e in errors:
            flash(e, "err")
        return None
    conn = db.get_db()
    cols = ["leave_type", "leave_name", "start_date", "end_date", "days", "basis", "order_date"]
    if lv:
        conn.execute(f"UPDATE leaves SET employee_id=?, {', '.join(c + '=?' for c in cols)} WHERE id=? AND status='draft'",
                     [emp["id"], *[d[c] for c in cols], lv["id"]])
        lid, action = lv["id"], "draft_update"
    else:
        cur = conn.execute(f"INSERT INTO leaves (employee_id, created_by, {', '.join(cols)}) VALUES (?,?,{','.join('?' * len(cols))})",
                           [emp["id"], current_user()["id"], *[d[c] for c in cols]])
        lid, action = cur.lastrowid, "draft_create"
    conn.commit()
    audit.log(action, "leave", lid, f"сотрудник #{emp['id']}, {d['days']} дн.")
    if warning:
        flash(warning, "err")
    return lid

@bp.route("/new", methods=["GET", "POST"])
@roles_required("admin", "hr")
def create():
    if request.method == "POST":
        lid = _save(None)
        if lid:
            return redirect(url_for("leaves.view", lid=lid))
    return render_template("leave_form.html", **_form()), (400 if request.method == "POST" else 200)

@bp.route("/<int:lid>/edit", methods=["GET", "POST"])
@roles_required("admin", "hr")
def edit(lid):
    lv = _leave_or_404(lid)
    if lv["status"] != "draft":
        flash("Оформленный отпуск изменить нельзя.", "err")
        return redirect(url_for("leaves.view", lid=lid))
    if request.method == "POST" and _save(lv):
        return redirect(url_for("leaves.view", lid=lid))
    return render_template("leave_form.html", **_form(lv)), (400 if request.method == "POST" else 200)

# ---------- просмотр, оформление ----------
@bp.route("/<int:lid>")
@roles_required("admin", "hr")
def view(lid):
    lv = _leave_or_404(lid)
    emp = _employee_or_404(lv["employee_id"])
    text = problem = warning = None
    order = None
    if lv["status"] == "posted":
        order = db.get_db().execute("SELECT * FROM orders WHERE id=?", (lv["order_id"],)).fetchone()
        text = order["rendered_text"]
    else:
        d, errors, warning = validate(dict(lv), emp, lid)
        errors += documents.org_errors()
        if errors:
            problem = " ".join(errors)
        else:
            try:
                text = documents.render_doc("order_leave", documents.active_template("order_leave")["body"], build_ctx(lv, "___", emp))
            except ValueError as e:
                problem = str(e)
    bal = balance(emp, datetime.date.fromisoformat(lv["start_date"]), lid) if emp["hire_date"] else None
    return render_template("leave_view.html", lv=lv, emp=emp, types=LEAVE_TYPES, st=ST, text=text, problem=problem,
                           warning=warning, order=order, bal=bal)

@bp.route("/<int:lid>/post", methods=["POST"])
@roles_required("admin", "hr")
def post(lid):
    lv = _leave_or_404(lid)
    conn = db.get_db()
    try:
        if lv["status"] != "draft":
            raise ValueError("Отпуск уже оформлен.")
        emp = _employee_or_404(lv["employee_id"])
        d, errors, warning = validate(dict(lv), emp, lid)
        errors += documents.org_errors()
        if warning:
            errors.append(warning)
        if errors:
            raise ValueError(" ".join(errors))
        tpl = documents.active_template("order_leave")
        number = numbering.next_number("order", int(lv["order_date"][:4]), commit=False)
        text = documents.render_doc("order_leave", tpl["body"], build_ctx(lv, number, emp))
        uid = current_user()["id"]
        cur = conn.execute(
            "INSERT INTO orders (doc_type, number, order_date, employee_id, params, status, template_version, rendered_text, created_by, posted_by, posted_at) "
            "VALUES ('leave',?,?,?,?, 'posted',?,?,?,?,datetime('now'))",
            (number, lv["order_date"], emp["id"], json.dumps({"leave_id": lid, "start_date": lv["start_date"], "end_date": lv["end_date"], "days": lv["days"]}),
             tpl["version"], text, lv["created_by"], uid))
        n = conn.execute("UPDATE leaves SET status='posted', order_id=?, posted_at=datetime('now') WHERE id=? AND status='draft'",
                         (cur.lastrowid, lid)).rowcount
        if n != 1:
            raise ValueError("Данные изменились, повторите оформление.")
        refresh_statuses(commit=False)
        audit.log("post", "leave", lid, f"приказ № {number}, {lv['days']} дн.", commit=False)
        conn.commit()
        flash(f"Отпуск оформлен приказом № {number}.", "ok")
    except ValueError as e:
        conn.rollback()
        flash(str(e), "err")
    return redirect(url_for("leaves.view", lid=lid))

@bp.route("/<int:lid>/delete", methods=["POST"])
@roles_required("admin", "hr")
def delete_draft(lid):
    lv = _leave_or_404(lid)
    if lv["status"] != "draft":
        flash("Оформленный отпуск удалить нельзя.", "err")
        return redirect(url_for("leaves.view", lid=lid))
    db.get_db().execute("DELETE FROM leaves WHERE id=? AND status='draft'", (lid,))
    db.get_db().commit()
    audit.log("draft_delete", "leave", lid)
    return redirect(url_for("leaves.schedule"))

# ---------- остаток по сотруднику и корректировки ----------
@bp.route("/employee/<int:emp_id>")
@roles_required("admin", "hr")
def employee_page(emp_id):
    refresh_statuses()
    emp = _employee_or_404(emp_id)
    conn = db.get_db()
    return render_template("leave_employee.html", emp=emp, bal=balance(emp, today()) if emp["hire_date"] else None,
                           adjustments=conn.execute("SELECT * FROM leave_adjustments WHERE employee_id=? ORDER BY id DESC", (emp_id,)).fetchall(),
                           leaves=conn.execute("SELECT * FROM leaves WHERE employee_id=? ORDER BY start_date DESC", (emp_id,)).fetchall(),
                           types=LEAVE_TYPES, st=ST, norm=params.get_int("annual_leave_days"))

@bp.route("/employee/<int:emp_id>/adjust", methods=["POST"])
@roles_required("admin", "hr")
def adjust(emp_id):
    _employee_or_404(emp_id)
    raw = request.form.get("days", "").strip()
    reason = " ".join(request.form.get("reason", "").split())
    try:
        days = int(raw)
        if days == 0 or abs(days) > 365:
            raise ValueError
    except ValueError:
        flash("Дни: целое ненулевое число (можно со знаком минус), не больше 365 по модулю.", "err")
        return redirect(url_for("leaves.employee_page", emp_id=emp_id))
    if not reason or len(reason) > 300:
        flash("Укажите причину корректировки (до 300 символов).", "err")
    else:
        db.get_db().execute("INSERT INTO leave_adjustments (employee_id, days, reason, created_by) VALUES (?,?,?,?)",
                            (emp_id, days, reason, current_user()["id"]))
        db.get_db().commit()
        audit.log("leave_adjust", "employee", emp_id, f"{days:+d} дн.: {reason}")
        flash("Остаток скорректирован.", "ok")
    return redirect(url_for("leaves.employee_page", emp_id=emp_id))
